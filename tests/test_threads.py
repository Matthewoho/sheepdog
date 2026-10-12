"""7.22 一事一串与发前再看一眼。

假的飞书（FakeLark）替换 lark-cli 的子进程调用：按架构侧核实过的字段造数据（搜索 / mget / 话题列表里的话题消息
没有 parent_id、root_id，开头那条 thread_message_position 是 "0"），记录每次调用的参数，按防重编号去重
（同一个编号只建一条消息，返回第一次的结果），可以模拟失败、超时、「发出去了但响应超时」。

A ask / report：同一把钥匙第一次起新消息、之后进话题；不带钥匙每次新消息；重试只发一条；失败返回非零。
B 主人在话题里回复：按话题送回、带开头那条；话题长了附最近几条和 sheepdog thread 提示；老消息下开的话题靠话题列表认；
  认不出交给总线；引用回复的老路不变。
C 发前再看一眼：检查跑多少次账本、已读状态、拉取进度都不变；主人已回不发、打印原话、「发送前已看到」幂等，
  之后那条作为背景只送一次并注明；没回就发、重试只发一条；检查失败不发。
"""

import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli, outbound
from sheepdog.config import EscalationConfig, OwnerContextConfig, ThreadsConfig
from sheepdog.engine import BUS_TOPIC_ID, SEEN_BEFORE_SEND_NOTE, Collector
from sheepdog.source.lark import LarkCliSource, parse_message
from sheepdog.store import Store

from test_roster import ME, REPO, Base

TZ8 = timezone(timedelta(hours=8))
ESC_CHAT = "oc_test_esc"
BOT = "ou_test_bot"
ALICE = "ou_test_alice"
ALPHA_P2P = "oc_test_alpha_p2p"
MARK = "[🐕Sheepdog Reply]"
HEADER_RX = r"^🐕 需要你定 · [^\n]*\[(?P<topic>tp_[a-z0-9_.-]+)\]"
ATTR_RX = r"\[(?P<topic>tp_[a-z0-9_.-]+)\]"
WRITE_CMDS = ("+messages-send", "+messages-reply")


def now8() -> datetime:
    return datetime.now(TZ8).replace(second=0, microsecond=0)


def _opt(args: list[str], name: str, default: str = "") -> str:
    return args[args.index(name) + 1] if name in args else default


def _card_text(content: str) -> str:
    """卡片拉回来时的样子：<card>\\n正文\\n标记\\n</card>（同 test_reply_card）。"""
    card = json.loads(content)
    parts = []
    for el in card["body"]["elements"]:
        text = el.get("content", "")
        parts.append(text.replace("<font color='grey'>", "").replace("</font>", ""))
    return "<card>\n" + "\n".join(parts) + "\n</card>"


class FakeLark:
    """假的飞书 + lark-cli。只在内存里，绝不起真实进程。"""

    def __init__(self, list_includes_root: bool = True, reply_returns_thread_id: bool = False):
        self.calls: list[list[str]] = []
        self.msgs: dict[str, dict] = {}
        self.idem: dict[str, str] = {}
        # 子命令 → 依次使用的模式：error（报错，什么都没发）/ timeout（超时，什么都没发）/ lost（发出去了但响应超时）
        self.plan: dict[str, list[str]] = {}
        self.n = 0
        self.list_includes_root = list_includes_root
        self.reply_returns_thread_id = reply_returns_thread_id

    # ---------- 造数据 ----------
    def add(self, *, chat_id: str, sender_id: str, content: str, at: datetime | None = None, sender_type: str = "user",
            chat_type: str = "p2p", thread_root: str = "", message_id: str = "") -> str:
        self.n += 1
        mid = message_id or f"om_test_fk{self.n:04d}"
        m = {"message_id": mid, "chat_id": chat_id, "chat_type": chat_type, "content": content,
             "at": (at or now8()).astimezone(TZ8).replace(second=0, microsecond=0),
             "sender": {"id": sender_id, "sender_type": sender_type,
                        "name": "主人" if sender_id == ME else "机器人" if sender_type == "app" else "Alice"},
             "thread_root": "", "thread_id": "", "pos": None, "deleted": False}
        if thread_root:
            root = self.msgs[thread_root]
            if root["thread_root"]:
                thread_root = root["thread_root"]
                root = self.msgs[thread_root]
            if not root["thread_id"]:
                root["thread_id"] = "omt_test_" + thread_root.removeprefix("om_test_")
                root["pos"] = 0
            m["thread_root"] = thread_root
            m["thread_id"] = root["thread_id"]
            m["pos"] = 1 + sum(1 for x in self.msgs.values() if x["thread_root"] == thread_root)
        self.msgs[mid] = m
        return mid

    def raw(self, mid: str) -> dict:
        """搜索 / mget / 话题列表返回的字段（没有 parent_id、root_id）。"""
        m = self.msgs[mid]
        out = {"chat_id": m["chat_id"], "chat_name": "测试私聊", "chat_type": m["chat_type"], "content": m["content"],
               "create_time": m["at"].strftime("%Y-%m-%d %H:%M"), "deleted": m["deleted"],
               "message_app_link": f"https://example.invalid/{mid}", "message_id": mid,
               "message_position": str(list(self.msgs).index(mid) + 1), "msg_type": "text", "sender": m["sender"],
               "updated": False}
        if m["thread_id"]:
            out["thread_id"] = m["thread_id"]
            out["thread_message_position"] = str(m["pos"])
        return out

    def in_thread(self, root: str) -> list[str]:
        return [k for k, v in self.msgs.items() if v["thread_root"] == root]

    def sent(self, sub: str | None = None) -> list[list[str]]:
        return [c for c in self.calls if (sub is None and len(c) > 1 and c[1] in WRITE_CMDS) or (sub and c[1:2] == [sub])]

    def subcommands(self) -> list[str]:
        return [c[1] if c[0] == "im" else c[0] for c in self.calls]

    # ---------- 假的 lark-cli ----------
    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        args = list(argv[1:])
        self.calls.append(args)
        sub = args[1] if args[0] == "im" and len(args) > 1 else args[0]   # 表情：im reactions create → "reactions"
        mode = self.plan[sub].pop(0) if self.plan.get(sub) else None
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout or 60)
        if mode == "error":
            return self._out({"ok": False, "error": {"type": "api", "subtype": "server_error", "message": "假的失败"}}, 1)
        handler = {"+messages-send": self._send, "+messages-reply": self._reply, "+messages-search": self._search,
                   "+threads-messages-list": self._thread_list, "+messages-mget": self._mget,
                   "+chat-list": lambda a: {"chats": [], "has_more": False},
                   "reactions": lambda a: {"reaction_id": "rc_test_1", "success_msg_reaction_details": []},
                   "+messages-read-status": lambda a: {"items": [{"message_id": i, "is_read": False}
                                                                for i in _opt(a, "--message-ids").split(",")]},
                   }.get(sub, lambda a: {})
        data = handler(args)
        if mode == "lost":
            raise subprocess.TimeoutExpired(argv, timeout or 60)
        return self._out({"ok": True, "data": data})

    @staticmethod
    def _out(obj: dict, rc: int = 0):
        return subprocess.CompletedProcess([], rc, stdout=json.dumps(obj, ensure_ascii=False), stderr="")

    def _content(self, args: list[str]) -> str:
        if "--content" in args:
            return _card_text(_opt(args, "--content"))
        return _opt(args, "--text")

    def _created(self, mid: str) -> dict:
        m = self.msgs[mid]
        out = {"message_id": mid, "chat_id": m["chat_id"], "create_time": str(int(m["at"].timestamp()))}
        if self.reply_returns_thread_id and m["thread_id"]:
            out["thread_id"] = m["thread_id"]
        return out

    def _send(self, args: list[str]) -> dict:
        key = _opt(args, "--idempotency-key")
        if key and key in self.idem:
            return self._created(self.idem[key])
        ident = _opt(args, "--as")
        chat = ESC_CHAT if _opt(args, "--user-id") and ident == "bot" else _opt(args, "--chat-id")
        mid = self.add(chat_id=chat, sender_id=BOT if ident == "bot" else ME,
                       sender_type="app" if ident == "bot" else "user", content=self._content(args))
        if key:
            self.idem[key] = mid
        return self._created(mid)

    def _reply(self, args: list[str]) -> dict:
        key = _opt(args, "--idempotency-key")
        if key and key in self.idem:
            return self._created(self.idem[key])
        target = self.msgs[_opt(args, "--message-id")]
        ident = _opt(args, "--as")
        mid = self.add(chat_id=target["chat_id"], sender_id=BOT if ident == "bot" else ME,
                       sender_type="app" if ident == "bot" else "user", content=self._content(args),
                       chat_type=target["chat_type"],
                       thread_root=target["message_id"] if "--reply-in-thread" in args else "")
        if key:
            self.idem[key] = mid
        return self._created(mid)

    def _search(self, args: list[str]) -> dict:
        chats = set(filter(None, _opt(args, "--chat-id").split(",")))
        senders = set(filter(None, _opt(args, "--sender").split(",")))
        start = datetime.fromisoformat(_opt(args, "--start")) if "--start" in args else None
        end = datetime.fromisoformat(_opt(args, "--end")) if "--end" in args else None
        out = []
        for mid, m in self.msgs.items():
            if chats and m["chat_id"] not in chats or senders and m["sender"]["id"] not in senders:
                continue
            if start and m["at"] < start or end and m["at"] > end:
                continue
            out.append(self.raw(mid))
        return {"messages": out, "has_more": False}

    def _thread_list(self, args: list[str]) -> dict:
        t = _opt(args, "--thread")
        root = next((k for k, v in self.msgs.items() if v["thread_id"] == t and not v["thread_root"]), t)
        ids = ([root] if self.list_includes_root and root in self.msgs else []) + self.in_thread(root)
        if _opt(args, "--order") == "desc":
            ids.reverse()
        return {"messages": [self.raw(i) for i in ids], "has_more": False}

    def _mget(self, args: list[str]) -> dict:
        return {"messages": [self.raw(i) for i in _opt(args, "--message-ids").split(",") if i in self.msgs]}


def snapshot(path: Path) -> dict:
    """整库快照：每张表的全部行。"""
    conn = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        conn.close()


def diff_tables(a: dict, b: dict) -> set[str]:
    return {t for t in set(a) | set(b) if a.get(t) != b.get(t)}


class ThreadsBase(Base):
    def setUp(self):
        super().setUp()
        self.cfg.escalation = EscalationConfig(chat_ids=[ESC_CHAT], header_regex=HEADER_RX, attribution_regex=ATTR_RX)
        self.cfg.owner_context = OwnerContextConfig(enabled=True, agent_markers=[MARK], reaction_check_minutes=0)
        self.cfg.threads = ThreadsConfig()
        self.lark = FakeLark()
        p = mock.patch("sheepdog.source.lark.subprocess.run", self.lark)
        p.start()
        self.addCleanup(p.stop)
        self.cfg_file = Path(self.tmp.name) / "config.toml"
        self.cfg_file.write_text(
            f'self_open_id = "{ME}"\n'
            f"[escalation]\nchat_ids = [\"{ESC_CHAT}\"]\nheader_regex = '{HEADER_RX}'\nattribution_regex = '{ATTR_RX}'\n"
            f"[owner_context]\nenabled = true\nagent_markers = [\"{MARK}\"]\nreaction_check_minutes = 0\n",
            encoding="utf-8")
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.source = LarkCliSource()

    def poll(self):
        return Collector(self.cfg, self.store, self.source, self.roster).poll_once()

    def pull(self, *mids: str):
        """把假飞书里的这几条按拉取时的格式入账（等于下一轮拉到了它们；不受拉取窗口影响）。"""
        msgs = [parse_message(self.lark.raw(m)) for m in mids]
        return Collector(self.cfg, self.store, self.source, self.roster).ingest(msgs)

    def cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(self.cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), mock.patch("sys.stderr", err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def ask(self, *extra, topic="tp_alpha", kind="ask", title="发版时间", text="周五还是下周一？") -> tuple[int, str, str]:
        return self.cli(kind, "--topic", topic, "--title", title, "--text", text, *extra)

    def deliver(self) -> str:
        """投一轮，返回 tp_alpha 这一轮收到的批次正文（没有就空串），并交回执。"""
        before = len(self.sink.to("conv_test_alpha"))
        self.d.dispatch_once()
        got = self.sink.to("conv_test_alpha")[before:]
        if got:
            self.ack("tp_alpha")
        return "\n".join(got)


# ======================= A：一事一串，ask / report =======================
class AskTest(ThreadsBase):
    def test_same_key_first_new_message_then_in_thread(self):
        rc, out, _ = self.ask("--key", "PR #12")
        self.assertEqual(rc, 0, out)
        rc2, out2, _ = self.ask("--key", "pr#12", text="再确认一下回滚方案")  # 同一把钥匙（norm_key 口径）
        rc3, _, _ = self.ask("--key", "PR #12", kind="report", title="进度", text="已合入")
        self.assertEqual((rc2, rc3), (0, 0))
        send, r1, r2 = self.lark.sent()
        self.assertEqual(send[1], "+messages-send")
        self.assertEqual((_opt(send, "--as"), _opt(send, "--user-id")), ("bot", ME))
        self.assertTrue(_opt(send, "--text").startswith("🐕 需要你定 · 发版时间 [tp_alpha]\n周五还是下周一？"))
        root = self.store.thread_by_key("tp_alpha", "PR#12")["root_message_id"]
        for r in (r1, r2):
            self.assertEqual(r[1], "+messages-reply")
            self.assertEqual((_opt(r, "--message-id"), _opt(r, "--as")), (root, "bot"))
            self.assertIn("--reply-in-thread", r)
        self.assertTrue(_opt(r2, "--text").startswith("🐕 汇报 · 进度 [tp_alpha]"))
        for c in (send, r1, r2):
            k = _opt(c, "--idempotency-key")
            self.assertTrue(k and len(k) <= 50, k)
        self.assertEqual(len({_opt(c, "--idempotency-key") for c in (send, r1, r2)}), 3)
        # 飞书那边：主聊天里一条，话题里两条
        self.assertEqual(len(self.lark.in_thread(root)), 2)
        self.assertIn("新起了一个话题", out)
        self.assertIn("话题里", out2)
        # 记账：话题表一行、sends 三行（都指向同一个开头）、bot_outbox 三行（ask 是问题，report 不是）
        rows = self.store.conn.execute("SELECT kind, root_message_id, norm_key FROM sends ORDER BY sent_at, rowid").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("ask", root, "PR#12")] * 2 + [("report", root, "PR#12")])
        outbox = self.store.conn.execute("SELECT is_question FROM bot_outbox ORDER BY sent_at, rowid").fetchall()
        self.assertEqual(sorted(r[0] for r in outbox), [0, 1, 1])

    def test_without_key_each_send_is_a_new_message(self):
        self.ask(text="第一件")
        self.ask(text="第二件")
        self.ask(kind="report", text="汇报一下")
        self.assertEqual([c[1] for c in self.lark.sent()], ["+messages-send"] * 3)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM owner_threads").fetchone()[0], 0)

    def test_different_sessions_same_key_get_separate_threads(self):
        self.ask("--key", "ABC-1")
        self.ask("--key", "ABC-1", topic=BUS_TOPIC_ID)
        self.assertEqual([c[1] for c in self.lark.sent()], ["+messages-send"] * 2)

    def test_retry_n_times_sends_once(self):
        for _ in range(5):
            rc, out, _ = self.ask("--key", "K1")
            self.assertEqual(rc, 0)
        self.assertEqual(len(self.lark.sent()), 1)
        self.assertIn("之前已经发过", out)
        # 同一把钥匙在话题里的那条，重试也只发一次
        for _ in range(3):
            self.ask("--key", "K1", text="补充一句")
        self.assertEqual(len(self.lark.sent("+messages-reply")), 1)

    def test_timeout_after_send_then_retry_keeps_one_message(self):
        # 第一次：飞书其实发出去了，但响应超时 → 非零、说不确定；重试带同一个防重编号，飞书那边只有一条
        self.lark.plan["+messages-send"] = ["lost"]
        rc, _, err = self.ask("--key", "K2")
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        self.assertIn("不确定发出去没有", err)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0], 0)
        rc, _, _ = self.ask("--key", "K2")
        self.assertEqual(rc, 0)
        calls = self.lark.sent()
        self.assertEqual(len(calls), 2)
        self.assertEqual(_opt(calls[0], "--idempotency-key"), _opt(calls[1], "--idempotency-key"))
        self.assertEqual(sum(1 for m in self.lark.msgs.values() if m["sender"]["id"] == BOT), 1)
        self.assertIsNotNone(self.store.thread_by_key("tp_alpha", "K2"))

    def test_send_failure_returns_nonzero_and_records_nothing(self):
        before = snapshot(self.cfg.db_path)
        for mode, code in (("error", outbound.EXIT_SEND_FAILED), ("timeout", outbound.EXIT_SEND_FAILED)):
            self.lark.plan["+messages-send"] = [mode]
            rc, out, err = self.ask("--key", "K3")
            self.assertEqual((rc, out), (code, ""))
            self.assertIn("没发", err)
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())
        # 话题里那条失败也一样
        self.ask("--key", "K3")
        self.lark.plan["+messages-reply"] = ["error"]
        rc, _, err = self.ask("--key", "K3", text="第二条")
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        self.assertIn("假的失败", err)

    def test_invalid_arguments(self):
        rc, _, err = self.ask(topic="tp_nobody")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        rc, _, err = self.ask(title="   ")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertEqual(self.lark.calls, [])

    def test_header_registers_escalation_and_work_uses_the_key(self):
        self.ask("--key", "AGIF-7", text="要不要延期？")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "AGIF-7")["root_message_id"]
        e = self.store.get_escalation(root)
        self.assertEqual(e["topic_id"], "tp_alpha")
        self.d.dispatch_once()
        w = self.store.get_work("tp_alpha", "AGIF-7")
        self.assertEqual((w["state"], w["source"], w["key"]), ("waiting_owner", "escalation", "AGIF-7"))
        # report 不登记「需要你定」
        self.ask("--key", "AGIF-7", kind="report", text="顺便汇报")
        self.poll()
        self.assertEqual(len(self.store.open_escalations("tp_alpha")), 1)


# ======================= B：主人在话题里回复，按话题送回 =======================
class ThreadReplyTest(ThreadsBase):
    def owner_in_thread(self, root: str, text: str) -> str:
        return self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=text, thread_root=root)

    def test_thread_reply_goes_back_with_the_root(self):
        self.ask("--key", "PR #9", text="周五发还是下周一发？")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "PR#9")["root_message_id"]
        mid = self.owner_in_thread(root, "下周一")
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_reply", "tp_alpha"))
        self.assertIn(f"escalation:{root}", json.loads(r["tags_json"]))
        text = self.deliver()
        self.assertIn("> 下周一", text)
        self.assertIn("🧵 主人在话题里回复", text)
        self.assertIn("这件事开头那条", text)
        self.assertIn("周五发还是下周一发？", text)
        self.assertNotIn("最近", text)          # 一般情况不附最近几条
        self.assertNotIn("sheepdog thread", text)
        self.assertEqual(self.store.get_escalation(root)["close_reason"], "answered")
        # 话题编号已补进表；没有查话题列表
        self.assertTrue(self.store.thread_by_key("tp_alpha", "PR#9")["thread_id"].startswith("omt_"))
        self.assertNotIn("+threads-messages-list", self.lark.subcommands())

    def test_complex_thread_attaches_recent_and_thread_hint(self):
        self.ask("--key", "K9", text="开头那条的问题")
        root = self.store.thread_by_key("tp_alpha", "K9")["root_message_id"]
        self.owner_in_thread(root, "主人第一句")
        self.poll()
        self.assertNotIn("最近", self.deliver())            # 话题里第 2 条（开头之外 1 条）：一般情况
        self.ask("--key", "K9", kind="report", title="进度", text="机器人在话题里的汇报")
        self.owner_in_thread(root, "主人第二句")
        self.poll()
        text = self.deliver()                                 # 话题里第 4 条（开头之外 3 条）：复杂情况
        self.assertIn("开头那条的问题", text)
        self.assertIn("已有 3 条（含主人这一条），最近 2 条", text)
        self.assertIn("主人第一句", text)
        self.assertIn("机器人在话题里的汇报", text)
        self.assertIn("`sheepdog thread --topic tp_alpha --key K9`", text)
        self.assertIn("`sheepdog board --topic tp_alpha`", text)
        # 再来 6 条：只附最近 5 条
        for i in range(6):
            self.ask("--key", "K9", kind="report", title="进度", text=f"第{i}次汇报")
        self.owner_in_thread(root, "主人第三句")
        self.poll()
        text = self.deliver()
        self.assertIn("最近 5 条", text)
        self.assertNotIn("主人第一句", text)
        self.assertNotIn("第0次汇报", text)
        self.assertIn("第5次汇报", text)

    def test_threshold_from_config(self):
        self.cfg.threads.complex_min_replies = 1
        self.ask("--key", "K10")
        root = self.store.thread_by_key("tp_alpha", "K10")["root_message_id"]
        self.owner_in_thread(root, "一句")
        self.poll()
        self.assertIn("最近 0 条", self.deliver())

    def test_old_message_thread_recognized_by_thread_list(self):
        # 老办法发的「需要你定」（没走 ask）：先被拉进账本、登记成未结问题
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app",
                            content="🐕 需要你定 · 老问题 [tp_alpha]\n怎么办？")
        self.poll()
        self.assertIsNotNone(self.store.get_escalation(old))
        mid = self.owner_in_thread(old, "按 A 办")
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["topic_id"], r["reason"]), ("tp_alpha", "owner_reply"))
        thr = self.store.thread_by_root(old)
        self.assertEqual((thr["topic_id"], thr["source"], thr["key"]), ("tp_alpha", "recognized", None))
        self.assertTrue(thr["thread_id"].startswith("omt_"))
        lists = [c for c in self.lark.calls if c[1] == "+threads-messages-list"]
        self.assertEqual(len(lists), 1)
        self.assertEqual((_opt(lists[0], "--order"), _opt(lists[0], "--as")), ("asc", "user"))
        text = self.deliver()
        self.assertIn("怎么办？", text)
        self.assertEqual(self.store.get_escalation(old)["close_reason"], "answered")
        # 同一话题再回：按表直接认出，不再查话题列表
        self.owner_in_thread(old, "补充一句")
        self.poll()
        self.assertEqual(sum(1 for c in self.lark.calls if c[1] == "+threads-messages-list"), 1)

    def test_old_report_thread_recognized_by_first_line(self):
        # 开头那条不在 bot_outbox / escalations 里（比如过了保留期），按第一行认
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="🐕 汇报 · 早先的 [tp_alpha]\n…")
        mid = self.owner_in_thread(old, "收到")
        self.lark.msgs[old]["at"] -= timedelta(days=3)  # 不在拉取窗口里
        self.poll()
        self.assertEqual(self.row(mid)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.thread_by_root(old)["source"], "recognized")

    def test_unrecognized_thread_goes_to_bus(self):
        # 主人自己发的一条下面开了话题：开头那条认不出来源
        own = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="我自己记一笔")
        self.lark.msgs[own]["at"] -= timedelta(days=3)
        mid = self.owner_in_thread(own, "这条给谁？")
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["topic_id"], r["reason"]), (BUS_TOPIC_ID, "owner_reply"))
        notes = self.store.conn.execute("SELECT content FROM messages WHERE reason='escalation_list'").fetchall()
        self.assertTrue(any("认不出来源的话题" in n[0] and mid in n[0] and "我自己记一笔" in n[0] for n in notes))
        self.assertIsNone(self.store.thread_by_root(own))

    def test_thread_list_failure_goes_to_bus_with_reason(self):
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="🐕 需要你定 · x [tp_alpha]\ny")
        self.lark.msgs[old]["at"] -= timedelta(days=3)
        mid = self.owner_in_thread(old, "回复")
        self.lark.plan["+threads-messages-list"] = ["error"]
        with self.assertLogs("sheepdog", "WARNING"):
            self.poll()
        self.assertEqual(self.row(mid)["topic_id"], BUS_TOPIC_ID)
        notes = [n[0] for n in self.store.conn.execute("SELECT content FROM messages WHERE reason='escalation_list'")]
        self.assertTrue(any("开头那条查不到" in n and "假的失败" in n for n in notes))

    def test_list_without_root_is_unrecognized(self):
        self.lark.list_includes_root = False
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="🐕 需要你定 · x [tp_alpha]\ny")
        self.lark.msgs[old]["at"] -= timedelta(days=3)
        mid = self.owner_in_thread(old, "回复")
        self.poll()
        self.assertEqual(self.row(mid)["topic_id"], BUS_TOPIC_ID)

    def test_quote_reply_path_unchanged(self):
        # 有 reply_to（引用回复）时照旧按引用走，哪怕消息也带着话题编号
        self.ask("--key", "K11")
        self.ask(topic=BUS_TOPIC_ID, text="总线的问题")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "K11")["root_message_id"]
        bus_q = self.store.conn.execute("SELECT message_id FROM sends WHERE topic_id='tp_bus'").fetchone()[0]
        mid = self.owner_in_thread(root, "回总线那条")
        raw = self.lark.raw(mid)
        raw["parent_id"] = bus_q   # 引用回复带 parent_id
        orig = self.lark.raw
        self.lark.raw = lambda i: raw if i == mid else orig(i)
        self.poll()
        r = self.row(mid)
        self.assertEqual(r["topic_id"], BUS_TOPIC_ID)
        self.assertNotIn("🧵", r["note"] or "")
        self.assertNotIn("+threads-messages-list", self.lark.subcommands())

    def test_root_itself_is_not_a_thread_reply(self):
        # 主人自己发的一条，后来成了话题开头：它本身照旧按「没引用」处理（这里唯一未结在 tp_alpha）
        self.ask("--key", "K12")
        self.poll()
        own = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="好的")
        self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="话题里", thread_root=own)
        self.poll()
        r = self.row(own)
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertNotIn("🧵", r["note"] or "")


class ThreadCommandTest(ThreadsBase):
    def test_thread_lists_whole_thread_read_only(self):
        self.ask("--key", "K20", text="开头")
        root = self.store.thread_by_key("tp_alpha", "K20")["root_message_id"]
        self.poll()
        self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="账本里还没有的一句", thread_root=root)
        self.ask("--key", "K20", kind="report", title="r", text="sheepdog 发的")
        before = snapshot(self.cfg.db_path)
        self.lark.calls.clear()
        rc, out, _ = self.cli("thread", "--topic", "tp_alpha", "--key", "K20")
        self.assertEqual(rc, 0, out)
        self.assertIn("【开头】", out)
        self.assertIn("开头", out)
        self.assertIn("账本里还没有的一句", out)
        self.assertIn("sheepdog 发的", out)
        self.assertEqual(out.count("message_id"), 3)
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())
        self.assertEqual(self.lark.subcommands(), ["+threads-messages-list"])

    def test_thread_refuses_other_sessions_thread(self):
        self.ask("--key", "K21")
        root = self.store.thread_by_key("tp_alpha", "K21")["root_message_id"]
        rc, _, err = self.cli("thread", "--topic", "tp_beta", "--thread", root)
        self.assertEqual(rc, outbound.EXIT_INVALID)
        rc, _, err = self.cli("thread", "--topic", "tp_alpha", "--key", "NOPE")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("没有钥匙", err)
        rc, out, _ = self.cli("thread", "--topic", BUS_TOPIC_ID, "--thread", root)  # 总线能看
        self.assertEqual(rc, 0)

    def test_thread_lookup_failure_still_lists_ledger(self):
        self.ask("--key", "K22", text="开头那条")
        self.lark.plan["+threads-messages-list"] = ["error"]
        rc, out, _ = self.cli("thread", "--topic", "tp_alpha", "--key", "K22")
        self.assertEqual(rc, 0)
        self.assertIn("开头那条", out)
        self.assertIn("查飞书话题列表失败", out)


# ======================= C：发前再看一眼 =======================
class ReplyCheckTest(ThreadsBase):
    def setUp(self):
        super().setUp()
        # Alice 在 tp_alpha 负责的私聊里问了一句，送到 tp_alpha
        self.t0 = now8() - timedelta(minutes=8)
        self.q = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="今天能给结论吗？", at=self.t0)
        self.poll()
        self.deliver()
        self.batch = self.row(self.q)["batch_id"]
        self.assertTrue(self.batch)
        # 拉取进度放一个和「现在」差一分钟的值：检查要是推进了水位线，一定看得出来（同一秒内写同样的值看不出）
        self.store.set_meta("watermark", (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds"))
        self.lark.calls.clear()

    def reply(self, *extra, text="收到，下班前给结论") -> tuple[int, str, str]:
        return self.cli("reply", "--topic", "tp_alpha", "--to", self.q, "--card-text", text, *extra)

    def owner_says(self, text: str, minutes_after: int = 2) -> str:
        return self.lark.add(chat_id=ALPHA_P2P, sender_id=ME, content=text, at=self.t0 + timedelta(minutes=minutes_after))

    def test_owner_replied_check_is_read_only_and_idempotent(self):
        said = self.owner_says("我人工看一眼")
        meta = dict(self.store.conn.execute("SELECT key, value FROM meta").fetchall())
        before = snapshot(self.cfg.db_path)
        rc, out, _ = self.reply()
        self.assertEqual(rc, outbound.EXIT_OWNER_REPLIED)
        self.assertIn("我人工看一眼", out)
        self.assertIn(said, out)
        self.assertIn("--even-if-owner-replied", out)
        after1 = snapshot(self.cfg.db_path)
        # 只多了一条「发送前已看到」，其他表（消息、已读、拉取进度、表情……）一行不变
        self.assertEqual(diff_tables(before, after1), {"seen_before_send"})
        self.assertEqual([r[:2] for r in after1["seen_before_send"]], [("tp_alpha", said)])
        for _ in range(4):
            rc, out2, _ = self.reply()
            self.assertEqual((rc, out2), (outbound.EXIT_OWNER_REPLIED, out))
        self.assertEqual(diff_tables(after1, snapshot(self.cfg.db_path)), set())
        self.assertEqual(dict(self.store.conn.execute("SELECT key, value FROM meta").fetchall()), meta)
        self.assertIsNone(self.row(said))                                  # 检查看到的不进账本
        # 只调了搜索：没发、没点表情、没查已读、没 mget
        self.assertEqual(set(self.lark.subcommands()), {"+messages-search"})
        s = self.lark.calls[0]
        self.assertEqual((_opt(s, "--chat-id"), _opt(s, "--sender"), _opt(s, "--as")), (ALPHA_P2P, ME, "user"))
        self.assertEqual(datetime.fromisoformat(_opt(s, "--start")), self.t0)

    def test_seen_message_arrives_once_as_context_with_note(self):
        said = self.owner_says("我人工看一眼")
        self.assertEqual(self.reply()[0], outbound.EXIT_OWNER_REPLIED)
        self.assertEqual(self.reply()[0], outbound.EXIT_OWNER_REPLIED)
        self.pull(said)
        r = self.row(said)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_context", "tp_alpha"))
        text = self.deliver()
        self.assertIn("> 我人工看一眼", text)
        self.assertEqual(text.count(SEEN_BEFORE_SEND_NOTE), 1)
        self.pull(said)                                   # 再被拉到一次（重叠窗口）也不重送
        self.poll()
        self.assertNotIn("我人工看一眼", self.deliver())  # 只送一次
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM seen_before_send").fetchone()[0], 1)

    def test_not_replied_sends_once_with_batch_idempotency_key(self):
        for _ in range(5):
            rc, out, _ = self.reply()
            self.assertEqual(rc, 0, out)
        sent = self.lark.sent()
        self.assertEqual(len(sent), 1)
        c = sent[0]
        self.assertEqual((c[1], _opt(c, "--message-id"), _opt(c, "--as"), _opt(c, "--msg-type")),
                         ("+messages-reply", self.q, "user", "interactive"))
        self.assertNotIn("--reply-in-thread", c)
        self.assertEqual(_opt(c, "--idempotency-key"), outbound.idem_key("reply", self.batch, self.q))
        card = json.loads(_opt(c, "--content"))
        self.assertIn(MARK, json.dumps(card, ensure_ascii=False))
        mid = self.store.conn.execute("SELECT message_id FROM sends WHERE kind='reply'").fetchone()[0]
        self.assertEqual(self.store.get_bot_outbox(mid)["is_question"], 0)
        # 之后拉进账本：是会话代回，不当成主人的发言送背景
        self.poll()
        self.assertEqual(self.row(mid)["route"], "self")
        self.assertNotIn("收到，下班前给结论", self.deliver())

    def test_lost_response_then_retry_sends_one_message(self):
        self.lark.plan["+messages-reply"] = ["lost"]
        rc, _, err = self.reply()
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        self.assertIn("不确定发出去没有", err)
        rc, _, _ = self.reply()        # 重试：检查会看到刚才那条代回，但它带代回标记，不算主人发言
        self.assertEqual(rc, 0)
        mine = [m for m in self.lark.msgs.values() if m["chat_id"] == ALPHA_P2P and m["sender"]["id"] == ME]
        self.assertEqual(len(mine), 1)

    def test_check_failure_does_not_send(self):
        before = snapshot(self.cfg.db_path)
        for mode in ("error", "timeout"):
            self.lark.plan["+messages-search"] = [mode]
            rc, out, err = self.reply()
            self.assertEqual((rc, out), (outbound.EXIT_CHECK_FAILED, ""))
            self.assertIn("发前检查", err)
        self.assertEqual(self.lark.sent(), [])
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())

    def test_send_failure_after_clean_check_changes_nothing(self):
        before = snapshot(self.cfg.db_path)
        self.lark.plan["+messages-reply"] = ["error"]
        rc, _, err = self.reply()
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())

    def test_start_is_latest_message_of_same_chat_in_batch(self):
        # 同一批里同一聊天还有更晚的一条：以它为准，之前主人说的话不算「新发言」
        later = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="补充一句", at=self.t0 + timedelta(minutes=4))
        self.owner_says("在这两条之间说的", minutes_after=2)
        # 把 later 塞进同一批（模拟两条一起送到）
        self.pull(later)
        self.store.conn.execute("UPDATE messages SET batch_id=?, dispatch_state='delivered' WHERE message_id=?",
                                (self.batch, later))
        self.store.conn.commit()
        self.lark.calls.clear()
        rc, out, _ = self.reply()
        self.assertEqual(rc, 0, out)
        self.assertEqual(datetime.fromisoformat(_opt(self.lark.calls[0], "--start")), self.t0 + timedelta(minutes=4))
        self.owner_says("这句在最后", minutes_after=6)
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", self.q, "--card-text", "第二条", "--followup")
        self.assertEqual(rc, outbound.EXIT_OWNER_REPLIED)
        self.assertIn("这句在最后", out)
        self.assertNotIn("在这两条之间说的", out)

    def test_agent_replies_and_other_chats_do_not_count(self):
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ME, at=self.t0 + timedelta(minutes=1),
                      content=f"<card>\n别的会话代回的\n{MARK}\n</card>")
        self.lark.add(chat_id="oc_test_other", sender_id=ME, content="别的聊天", at=self.t0 + timedelta(minutes=1))
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="Alice 又说", at=self.t0 + timedelta(minutes=1))
        rc, out, _ = self.reply()
        self.assertEqual(rc, 0, out)

    def test_even_if_owner_replied_skips_check_and_sends(self):
        self.owner_says("我来")
        rc, out, _ = self.reply("--even-if-owner-replied")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.lark.subcommands(), ["+messages-reply"])

    def test_duplicate_with_different_text_and_followup(self):
        self.assertEqual(self.reply(text="ok，稍等")[0], 0)
        rc, _, err = self.reply(text="结论：可以")
        self.assertEqual(rc, outbound.EXIT_DUPLICATE)
        self.assertIn("--followup", err)
        self.assertEqual(len(self.lark.sent()), 1)
        rc, _, _ = self.reply("--followup", text="结论：可以")
        self.assertEqual(rc, 0)
        rc, _, _ = self.reply("--followup", text="结论：可以")   # 重试只发一次
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.lark.sent()), 2)

    def test_plain_text_gets_marker_and_target_must_be_in_ledger(self):
        rc, _, _ = self.cli("reply", "--topic", "tp_alpha", "--to", self.q, "--text", "纯文本")
        self.assertEqual(rc, 0)
        self.assertEqual(_opt(self.lark.sent()[0], "--text"), f"纯文本\n{MARK}")
        rc, _, err = self.cli("reply", "--topic", "tp_alpha", "--to", "om_test_unknown", "--card-text", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("账本里没有", err)
        self.assertEqual(len(self.lark.sent()), 1)

    def test_no_batch_uses_topic_and_message(self):
        self.store.conn.execute("UPDATE messages SET batch_id=NULL WHERE message_id=?", (self.q,))
        self.store.conn.commit()
        self.reply()
        self.assertEqual(_opt(self.lark.sent()[0], "--idempotency-key"), outbound.idem_key("reply", "tp_alpha", self.q))


# ======================= D：规则、文档、测试环境 =======================
class RulesAndDocsTest(ThreadsBase):
    def test_example_playbook_uses_sheepdog_commands(self):
        common = (REPO / "examples" / "playbook" / "common.md").read_text(encoding="utf-8")
        self.assertIn("{{cli}} ask", common)
        self.assertIn("{{cli}} reply", common)
        self.assertNotIn("--as bot --user-id", common)            # 不再教直接用 lark-cli 私聊主人
        self.assertNotIn("+messages-reply", common)               # 不再教直接用 lark-cli 代回
        for name in ("nudge_expire.md", "cli_host.md", "batch_footer.md"):
            text = (REPO / "examples" / "playbook" / name).read_text(encoding="utf-8")
            self.assertNotIn("用 lark-cli 汇报", text, name)
        ref = (REPO / "docs" / "reference.zh-CN.md").read_text(encoding="utf-8")
        for frag in ("sheepdog ask", "sheepdog reply", "sheepdog thread", "--even-if-owner-replied"):
            self.assertIn(frag, ref)

    def test_interface_section_documents_commands(self):
        from sheepdog.prompts import interface_section
        text = interface_section("tp_alpha", bus=False, remind=[15, 30], expire=60, cli="'/x y/sd'")
        self.assertIn("`'/x y/sd' ask --topic tp_alpha", text)
        self.assertIn("`'/x y/sd' reply --topic tp_alpha", text)
        self.assertIn("3 主人已经回过", text)

    def test_doctor_checks_header_against_regex(self):
        from sheepdog.cli import threads_report
        self.assertTrue(any("✓ 能被 header_regex 认出来" in x for x in threads_report(self.cfg)))
        self.cfg.threads.ask_header = "需要你定：{title}（{topic}）"
        self.assertTrue(any("✗ 认不出来" in x for x in threads_report(self.cfg)))

    def test_test_env_writes_notify_file_and_records_nothing(self):
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            "[escalation]", '[source]\nenabled = false\n[notify]\nfile = "notices.md"\n[escalation]'), encoding="utf-8")
        before = snapshot(self.cfg.db_path)
        rc, out, _ = self.ask("--key", "K30")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", "om_test_x", "--card-text", "代回")
        self.assertEqual(rc, 0, out)
        notices = (Path(self.tmp.name) / "notices.md").read_text(encoding="utf-8")
        self.assertIn("🐕 需要你定 · 发版时间 [tp_alpha]", notices)
        self.assertIn("回复 om_test_x", notices)
        self.assertEqual(self.lark.calls, [])
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())

    def test_example_config_header_matches_example_regex(self):
        import re
        import tomllib
        data = tomllib.loads((REPO / "examples" / "config.example.toml").read_text(encoding="utf-8"))
        tc = ThreadsConfig(**data["threads"])
        tc.validate()
        rx = re.compile(data["escalation"]["header_regex"])
        ax = re.compile(data["escalation"]["attribution_regex"])
        ask = tc.header("ask", "发版时间", "tp_alpha") + "\n正文"
        report = tc.header("report", "进度", "tp_alpha") + "\n正文里提到需要你定也不算"
        self.assertEqual(rx.search(ask).group("topic"), "tp_alpha")
        self.assertIsNone(rx.search(report))
        self.assertEqual(ax.search(report).group("topic"), "tp_alpha")

    def test_config_validation(self):
        from sheepdog.config import ConfigError, load_config
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + '[threads]\nask_header = "没有占位"\n',
                                 encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.cfg_file)
