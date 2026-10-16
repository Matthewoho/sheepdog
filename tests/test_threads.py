"""7.22 一事一串与发前再看一眼。

假的飞书（FakeLark）替换 lark-cli 的子进程调用，按架构侧 10-12 22:5x 在真实飞书上核实的格式造数据：
- 搜索 / mget / 话题列表里的消息没有 parent_id、root_id；
- 话题开头那条 thread_message_position 是 "-1"、回复从 "0" 起：普通聊天（p2p、group）和话题群（topic）都一样（7.45 用正式
  账本核实；7.22 当时以为话题群开头是 "0"，不对）；
- 话题列表只有回复、不含开头那条；mget 有话题的开头那条带 thread_id 和 thread_replies；
- --reply-in-thread 的返回只有 chat_id、create_time、message_id，没有 thread_id；
- 同一个防重编号再发：ok，返回第一次那条的 message_id，不多发。
它还记录每次调用的参数，可以模拟失败、超时、「发出去了但响应超时」。

A ask / report：同一把钥匙第一次起新消息、之后进话题；不带钥匙每次新消息；重试只发一条；失败返回非零。
B 主人在话题里回复：按话题送回、带开头那条；话题长了附最近几条和 sheepdog thread 提示；老消息下开的话题靠话题列表认；
  认不出交给总线；引用回复的老路不变。
C 发前再看一眼：检查跑多少次账本、已读状态、拉取进度都不变；主人已回不发、打印原话、「发送前已看到」幂等，
  之后那条作为背景只送一次并注明；没回就发、重试只发一条；检查失败不发。
"""

import io
import json
import os
import re
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
_NAMES = {ME: "主人"}


def now8() -> datetime:
    return datetime.now(TZ8).replace(second=0, microsecond=0)


def _opt(args: list[str], name: str, default: str = "") -> str:
    return args[args.index(name) + 1] if name in args else default


def sent_text(call: list[str]) -> str:
    """一次发送的正文（7.48 起发给主人的是卡片）：纯文本就是 --text；卡片是 header 标题 + 各 markdown 元素，换行连起来，
    @ 主人保留卡片写法 <at id=…></at>。"""
    if "--content" not in call:
        return _opt(call, "--text")
    card = json.loads(_opt(call, "--content"))
    title = ((card.get("header") or {}).get("title") or {}).get("content", "")
    parts = [el.get("content", "") for el in card["body"]["elements"]]
    return "\n".join(([title] if title else []) + parts)


def _card_text(content: str) -> str:
    """卡片拉回来时的样子（7.48，10-16 只读核过正式账本里机器人发的代回卡片和别的机器人的告警卡片）：
    有 header 是 <card title="标题">（带副标题是 <card title="…" subtitle="…">），没有 header 是 <card>；
    各 markdown 元素之间换行分开；<at id=ou_x></at> 变成 @名字(ou_x)；<font …> 标签去掉；最后一行 </card>。"""
    card = json.loads(content)
    head = (card.get("header") or {})
    title = (head.get("title") or {}).get("content", "")
    sub = (head.get("subtitle") or {}).get("content", "")
    parts = []
    for el in card["body"]["elements"]:
        text = el.get("content", "")
        text = re.sub(r"<at id=([\w-]+)></at>", lambda m: f"@{_NAMES.get(m.group(1), '某人')}({m.group(1)})", text)
        parts.append(re.sub(r"</?font[^>]*>", "", text))
    first = (f'<card title="{title}" subtitle="{sub}">' if sub else f'<card title="{title}">') if title else "<card>"
    return first + "\n" + "\n".join(parts) + "\n</card>"


class FakeLark:
    """假的飞书 + lark-cli。只在内存里，绝不起真实进程。"""

    def __init__(self, positions: bool = True):
        self.calls: list[list[str]] = []
        self.msgs: dict[str, dict] = {}
        self.idem: dict[str, str] = {}
        # 子命令 → 依次使用的模式：error（报错，什么都没发）/ timeout（超时，什么都没发）/ lost（发出去了但响应超时）/
        # card_reject（飞书拒了卡片，什么都没发，7.48）
        self.plan: dict[str, list[str]] = {}
        self.n = 0
        # 搜索结果带不带 message_position（消息在这个聊天里的序号，按发送先后，同一分钟里也分先后）
        self.positions = positions
        # 聊天 → 类型（没写的是 p2p）：机器人往群里发的消息按这里定类型
        self.chat_types: dict[str, str] = {}
        # 群名（搜索结果和 im chats get 用；没写的是「测试私聊」）。mget 不带群名，和真的一样（lark-cli 文档的输出字段里没有）
        self.chat_names: dict[str, str] = {}

    # ---------- 造数据 ----------
    def add(self, *, chat_id: str, sender_id: str, content: str, at: datetime | None = None, sender_type: str = "user",
            chat_type: str = "p2p", thread_root: str = "", message_id: str = "", msg_type: str = "text") -> str:
        self.n += 1
        mid = message_id or f"om_test_fk{self.n:04d}"
        m = {"message_id": mid, "chat_id": chat_id, "chat_type": chat_type, "content": content,
             "at": (at or now8()).astimezone(TZ8).replace(second=0, microsecond=0),
             "sender": {"id": sender_id, "sender_type": sender_type,
                        "name": "主人" if sender_id == ME else "机器人" if sender_type == "app" else "Alice"},
             "thread_root": "", "thread_id": "", "pos": None, "deleted": False, "seq": self.n, "msg_type": msg_type}
        base = -1   # 开头那条的序号：普通聊天和话题群都是 "-1"（7.45 正式账本核实）
        if thread_root:
            root = self.msgs[thread_root]
            if root["thread_root"]:
                thread_root = root["thread_root"]
                root = self.msgs[thread_root]
            if not root["thread_id"]:
                root["thread_id"] = "omt_test_" + thread_root.removeprefix("om_test_")
                root["pos"] = -1
            m["chat_type"] = root["chat_type"]
            m["thread_root"] = thread_root
            m["thread_id"] = root["thread_id"]
            m["pos"] = root["pos"] + 1 + sum(1 for x in self.msgs.values() if x["thread_root"] == thread_root)
        elif chat_type == "topic":
            # 话题群里每条顶层消息本身就是一个话题的开头
            m["thread_id"] = "omt_test_" + mid.removeprefix("om_test_")
            m["pos"] = base
        self.msgs[mid] = m
        return mid

    def raw(self, mid: str) -> dict:
        """搜索 / mget / 话题列表返回的字段（没有 parent_id、root_id）。"""
        m = self.msgs[mid]
        out = {"chat_id": m["chat_id"], "chat_name": self.chat_names.get(m["chat_id"], "测试私聊"),
               "chat_type": m["chat_type"], "content": m["content"],
               "create_time": m["at"].strftime("%Y-%m-%d %H:%M"), "deleted": m["deleted"],
               "message_app_link": f"https://example.invalid/{mid}", "message_id": mid,
               "msg_type": m["msg_type"], "sender": m["sender"], "updated": False}
        if self.positions:
            # 聊天里的序号：同一聊天按（时间、发送先后）排，所以同一分钟里谁先谁后也分得清
            out["message_position"] = str(1 + sum(1 for x in self.msgs.values() if x["chat_id"] == m["chat_id"]
                                                  and (x["at"], x["seq"]) < (m["at"], m["seq"])))
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
        if mode == "card_reject":
            # 7.48：飞书拒了卡片（卡片内容创建失败），什么都没发
            return self._out({"ok": False, "error": {"type": "api", "code": 230099,
                                                     "message": "Failed to create card content"}}, 1)
        handler = {"+messages-send": self._send, "+messages-reply": self._reply, "+messages-search": self._search,
                   "+threads-messages-list": self._thread_list, "+messages-mget": self._mget,
                   "+chat-list": lambda a: {"chats": [], "has_more": False},
                   "chats": self._chat_get,
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

    @staticmethod
    def _msg_type(args: list[str]) -> str:
        return "interactive" if "--content" in args else "text"

    def sent_body(self, call: list[str]) -> str:
        """一次发送的内容，按主人身份搜索读回的样子（卡片是 <card …> 那种，纯文本原样）。"""
        return self._content(call)

    def _created(self, mid: str) -> dict:
        m = self.msgs[mid]
        out = {"message_id": mid, "chat_id": m["chat_id"], "create_time": str(int(m["at"].timestamp()))}
        return out

    def _send(self, args: list[str]) -> dict:
        key = _opt(args, "--idempotency-key")
        if key and key in self.idem:
            return self._created(self.idem[key])
        ident = _opt(args, "--as")
        chat = ESC_CHAT if _opt(args, "--user-id") and ident == "bot" else _opt(args, "--chat-id")
        mid = self.add(chat_id=chat, sender_id=BOT if ident == "bot" else ME,
                       sender_type="app" if ident == "bot" else "user", content=self._content(args),
                       chat_type=self.chat_types.get(chat, "p2p"), msg_type=self._msg_type(args))
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
                       chat_type=target["chat_type"], msg_type=self._msg_type(args),
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
        ids = self.in_thread(root)   # 只有回复，不含开头那条
        if _opt(args, "--order") == "desc":
            ids.reverse()
        tid = self.msgs[root]["thread_id"] if root in self.msgs else t
        return {"has_more": False, "messages": [self.raw(i) for i in ids], "page_token": "", "thread_id": tid,
                "total": len(ids)}

    def _chat_get(self, args: list[str]) -> dict:
        """im chats get（GET /open-apis/im/v1/chats/:chat_id）：只认 get，返回群信息（name 等）。"""
        assert args[2] == "get", args
        cid = _opt(args, "--chat-id")
        return {"name": self.chat_names.get(cid, "测试私聊"), "chat_mode": "group", "chat_type": "private"}

    def _mget(self, args: list[str]) -> dict:
        out = []
        for i in _opt(args, "--message-ids").split(","):
            if i not in self.msgs:
                continue
            r = self.raw(i)
            r.pop("chat_name")      # 真的 mget 不带群名（搜索是另外用 chats batch_query 补的）
            if self.in_thread(i):   # 有话题的开头那条带上回复
                r["thread_replies"] = [{k: v for k, v in self.raw(x).items() if k != "chat_name"} for x in self.in_thread(i)]
            out.append(r)
        return {"messages": out, "total": len(out)}


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
        self.assertEqual(_opt(send, "--msg-type"), "interactive")   # 7.48：发给主人的一律是卡片
        self.assertTrue(sent_text(send).startswith("🐕 需要你定 · 发版时间 [tp_alpha]\n周五还是下周一？"))
        root = self.store.thread_by_key("tp_alpha", "PR#12")["root_message_id"]
        for r in (r1, r2):
            self.assertEqual(r[1], "+messages-reply")
            self.assertEqual((_opt(r, "--message-id"), _opt(r, "--as")), (root, "bot"))
            self.assertIn("--reply-in-thread", r)
        self.assertTrue(sent_text(r2).startswith("🐕 汇报 · 进度 [tp_alpha]"))
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

    def test_batch_defaults_to_current_then_latest_batch(self):
        # 不带 --batch：用这个会话当前在跑的批次；没有在跑的用最近一批；隔批说同样的话照常发
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="第一批的消息")
        self.poll()
        self.d.dispatch_once()
        b1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.assertTrue(b1)
        self.ask(text="同样的话")
        first = _opt(self.lark.sent()[0], "--idempotency-key")
        self.assertEqual(first, outbound.idem_key("ask", "tp_alpha", "", b1, outbound.compose(
            self.cfg, "ask", "tp_alpha", "发版时间", "同样的话")))
        self.ack("tp_alpha")
        self.d.dispatch_once()                                    # 回执以后没有在跑的批次：用最近一批，还是 b1
        self.assertIsNone(self.store.get_topic("tp_alpha")["pending_batch_id"])
        self.assertEqual(outbound.current_batch(self.store, "tp_alpha"), b1)
        self.assertEqual(self.ask(text="同样的话")[0], 0)
        self.assertEqual(len(self.lark.sent()), 1)                # 同一批里重跑只发一条
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="第二批的消息")
        self.poll()
        self.d.dispatch_once()
        b2 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.assertNotEqual(b2, b1)
        self.ask(text="同样的话")                                 # 隔一批：同样的话照常发
        self.assertEqual(len(self.lark.sent()), 2)
        self.assertNotEqual(_opt(self.lark.sent()[1], "--idempotency-key"), first)

    def test_batch_empty_without_any_batch_and_explicit_batch_wins(self):
        self.store.conn.execute("DELETE FROM dispatches WHERE topic_id='tp_alpha'")
        self.store.conn.execute("UPDATE topics SET pending_batch_id=NULL WHERE topic_id='tp_alpha'")
        self.store.conn.commit()
        self.assertEqual(outbound.current_batch(self.store, "tp_alpha"), "")
        self.ask(text="没有批次")
        text = outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", "没有批次")
        self.assertEqual(_opt(self.lark.sent()[0], "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "", "", text))
        self.ask("--batch", "b_test_given", text="没有批次")
        self.assertEqual(_opt(self.lark.sent()[1], "--idempotency-key"),
                         outbound.idem_key("ask", "tp_alpha", "", "b_test_given", text))

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
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "0")   # 普通聊天：话题里第一条回复是 "0"
        self.assertEqual(self.lark.raw(root)["thread_message_position"], "-1")  # 开头那条是 "-1"
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

    def age(self, mid: str, days: int = 3) -> None:
        """让这条变老：之后的拉取窗口里不再有它（账本里已有的那行不变）。"""
        self.lark.msgs[mid]["at"] -= timedelta(days=days)

    def bus_notes(self) -> list[str]:
        return [n[0] for n in self.store.conn.execute("SELECT content FROM messages WHERE reason='escalation_list'")]

    def test_old_message_thread_recognized_by_mget_candidates(self):
        # 老办法发的「需要你定」（没走 ask）：拉进账本时还没有话题；之后主人在它下面开了话题，开头那条不再被拉到
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app",
                            content="🐕 需要你定 · 老问题 [tp_alpha]\n怎么办？")
        self.poll()
        self.assertIsNotNone(self.store.get_escalation(old))
        self.assertFalse(self.row(old)["thread_id"])
        self.age(old)
        mid = self.owner_in_thread(old, "按 A 办")
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "0")   # 普通聊天的第一条回复是 "0"
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["topic_id"], r["reason"]), ("tp_alpha", "owner_reply"))
        thr = self.store.thread_by_root(old)
        self.assertEqual((thr["topic_id"], thr["source"], thr["key"]), ("tp_alpha", "recognized", None))
        self.assertEqual(thr["thread_id"], self.lark.msgs[old]["thread_id"])
        mgets = [c for c in self.lark.calls if c[1] == "+messages-mget"]
        self.assertEqual(len(mgets), 1)
        self.assertIn(old, _opt(mgets[0], "--message-ids").split(","))
        self.assertLessEqual(len(_opt(mgets[0], "--message-ids").split(",")), 50)
        self.assertNotIn("+threads-messages-list", self.lark.subcommands())   # 话题列表不用来找开头
        text = self.deliver()
        self.assertIn("怎么办？", text)
        self.assertEqual(self.store.get_escalation(old)["close_reason"], "answered")
        # 同一话题再回：按表直接认出，不再 mget
        self.owner_in_thread(old, "补充一句")
        self.poll()
        self.assertEqual(sum(1 for c in self.lark.calls if c[1] == "+messages-mget"), 1)

    def test_root_in_ledger_found_by_position_and_first_line(self):
        # 开头那条在账本里，拉取时带上了话题编号和开头序号 "-1"；它不在 escalations / bot_outbox 里（比如登记时会话不可投），
        # 按第一行认
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="🐕 汇报 · 早先的 [tp_alpha]\n…")
        self.poll()
        self.store.conn.execute("DELETE FROM bot_outbox WHERE message_id=?", (old,))
        self.store.conn.commit()
        mid = self.owner_in_thread(old, "收到")
        self.poll()                         # 开头那条还在拉取窗口里：再拉到时补上话题编号和序号
        self.assertEqual((self.row(old)["thread_id"], self.row(old)["thread_position"]),
                         (self.lark.msgs[old]["thread_id"], "-1"))
        self.assertEqual(self.row(mid)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.thread_by_root(old)["source"], "recognized")
        self.assertNotIn("+messages-mget", self.lark.subcommands())

    def test_topic_group_root_is_minus_one(self):
        # 话题群（chat_type = topic）：开头那条序号同样是 "-1"，回复从 "0" 起（7.45 正式账本：5 个话题群 54 个话题都是这样）
        topic_chat = "oc_test_esc_topic"
        self.cfg.escalation.chat_ids = [ESC_CHAT, topic_chat]
        old = self.lark.add(chat_id=topic_chat, chat_type="topic", sender_id=BOT, sender_type="app",
                            content="🐕 需要你定 · 话题群里问的 [tp_alpha]\n选哪个？")
        self.poll()
        self.assertEqual(self.row(old)["thread_position"], "-1")
        mid = self.owner_in_thread(old, "选 B")
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "0")
        self.poll()
        r = self.row(mid)
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertIn("🧵", r["note"])
        self.assertIn("选哪个？", r["note"])
        # 主人在话题群里新发一条（它自己就是开头，序号 "-1"）：不当成话题回复
        own = self.lark.add(chat_id=topic_chat, chat_type="topic", sender_id=ME, content="另起一件事")
        self.poll()
        self.assertNotIn("🧵", self.row(own)["note"] or "")
        self.assertFalse(any(t.startswith("thread:") for t in json.loads(self.row(own)["tags_json"])))

    def test_unrecognized_thread_goes_to_bus(self):
        # 主人自己发的一条下面开了话题：它不在候选里，认不出来源
        own = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="我自己记一笔")
        self.age(own)
        mid = self.owner_in_thread(own, "这条给谁？")
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["topic_id"], r["reason"]), (BUS_TOPIC_ID, "owner_reply"))
        self.assertTrue(any("认不出来源的话题" in n and mid in n and "开头那条查不到" in n for n in self.bus_notes()))
        self.assertIsNone(self.store.thread_by_root(own))

    def test_candidates_without_match_go_to_bus(self):
        self.ask(text="不带钥匙问的")                               # 候选里有它，但话题对不上
        own = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="我自己记一笔")
        self.age(own)
        mid = self.owner_in_thread(own, "这条给谁？")
        self.poll()
        self.assertEqual(self.row(mid)["topic_id"], BUS_TOPIC_ID)
        self.assertEqual(sum(1 for c in self.lark.calls if c[1] == "+messages-mget"), 1)

    def test_mget_failure_goes_to_bus_with_reason(self):
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="🐕 需要你定 · x [tp_alpha]\ny")
        self.poll()
        self.age(old)
        mid = self.owner_in_thread(old, "回复")
        self.lark.plan["+messages-mget"] = ["error"]
        with self.assertLogs("sheepdog", "WARNING"):
            self.poll()
        self.assertEqual(self.row(mid)["topic_id"], BUS_TOPIC_ID)
        self.assertTrue(any("开头那条查不到" in n and "假的失败" in n for n in self.bus_notes()))

    def test_thread_id_backfilled_by_mget_after_posting(self):
        # --reply-in-thread 的返回里没有 thread_id：发完以后只读 mget 一次开头那条，补进话题表（不等拉取）
        self.ask("--key", "K13")
        root = self.store.thread_by_key("tp_alpha", "K13")["root_message_id"]
        self.assertEqual(self.lark.subcommands(), ["+messages-send"])
        self.ask("--key", "K13", text="第二条")
        self.assertEqual(self.lark.subcommands(), ["+messages-send", "+messages-reply", "+messages-mget"])
        self.assertEqual(_opt(self.lark.calls[-1], "--message-ids"), root)
        self.assertEqual(self.store.thread_by_key("tp_alpha", "K13")["thread_id"], self.lark.msgs[root]["thread_id"])
        self.ask("--key", "K13", text="第三条")                    # 已经知道了，不再 mget
        self.assertEqual(self.lark.subcommands().count("+messages-mget"), 1)

    def test_thread_id_backfill_failure_does_not_fail_the_send(self):
        self.ask("--key", "K14")
        self.lark.plan["+messages-mget"] = ["error"]
        rc, out, _ = self.ask("--key", "K14", text="第二条")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.store.thread_by_key("tp_alpha", "K14")["thread_id"])
        self.poll()                                                 # 之后从拉取里补
        root = self.store.thread_by_key("tp_alpha", "K14")["root_message_id"]
        self.assertEqual(self.store.thread_by_key("tp_alpha", "K14")["thread_id"], self.lark.msgs[root]["thread_id"])

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
        self.assertNotIn("+messages-mget", self.lark.subcommands()[-6:])

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

    def test_thread_lists_root_via_mget_when_not_in_ledger(self):
        # 话题列表不含开头那条；开头那条不在账本、也不是 sheepdog 发的：从 mget 取
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", content="很早以前的一条")
        self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="主人的回复一", thread_root=old)
        self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="主人的回复二", thread_root=old)
        rc, out, _ = self.cli("thread", "--topic", BUS_TOPIC_ID, "--thread", old)
        self.assertEqual(rc, 0, out)
        self.assertIn("【开头】", out)
        self.assertIn("很早以前的一条", out)
        self.assertIn("主人的回复一", out)
        self.assertIn("主人的回复二", out)
        self.assertLess(out.index("很早以前的一条"), out.index("主人的回复一"))
        self.assertEqual(self.lark.subcommands(), ["+messages-mget", "+threads-messages-list"])

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
        self.assertEqual(_opt(c, "--idempotency-key"),
                         outbound.idem_key("reply", self.batch, self.q, outbound.text_fingerprint("收到，下班前给结论")))
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
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", self.q, "--card-text", "第二条")
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

    def test_ack_then_conclusion_both_sent_and_same_text_retried_once(self):
        # 处理要久的先回「ok，稍等」，处理完对同一条再回结论：两条都发；同样内容重试只发一条
        self.assertEqual(self.reply(text="ok，稍等")[0], 0)
        self.assertEqual(self.reply(text="结论：可以")[0], 0)
        for _ in range(3):
            self.assertEqual(self.reply(text="ok，稍等")[0], 0)
            self.assertEqual(self.reply(text="结论：可以")[0], 0)
        sent = self.lark.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual({_opt(c, "--message-id") for c in sent}, {self.q})
        self.assertEqual(len({_opt(c, "--idempotency-key") for c in sent}), 2)
        mine = [m for m in self.lark.msgs.values() if m["chat_id"] == ALPHA_P2P and m["sender"]["id"] == ME]
        self.assertEqual(len(mine), 2)

    def test_same_minute_owner_before_target_is_not_after(self):
        # 同一分钟里：主人先说、Alice 后问。按聊天里的序号比，主人那句在要回复的那条之前，不算「之后」
        m0 = now8()   # 两条用同一个分钟，免得跨分钟时测试悄悄变成按分钟就能分开
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ME, content="我先说一句", at=m0)
        q2 = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="同一分钟里问的", at=m0)
        self.pull(q2)
        self.deliver()
        self.assertTrue(self.row(q2)["message_position"])
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", q2, "--card-text", "好的")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.lark.sent()), 1)

    def test_same_minute_owner_after_target_counts(self):
        m0 = now8()
        q2 = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="同一分钟里问的", at=m0)
        said = self.lark.add(chat_id=ALPHA_P2P, sender_id=ME, content="我已经回了", at=m0)
        self.pull(q2)
        self.deliver()
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", q2, "--card-text", "好的")
        self.assertEqual(rc, outbound.EXIT_OWNER_REPLIED)
        self.assertIn(said, out)
        self.assertEqual(self.lark.sent(), [])

    def test_without_positions_falls_back_to_minutes(self):
        # 没有序号时退回按分钟比：同一分钟里主人在前也算「之后」（偏保守，宁可不发）
        self.lark.positions = False
        m0 = now8()   # 两条用同一个分钟，免得跨分钟时测试悄悄变成按分钟就能分开
        self.lark.add(chat_id=ALPHA_P2P, sender_id=ME, content="我先说一句", at=m0)
        q2 = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="同一分钟里问的", at=m0)
        self.pull(q2)
        self.deliver()
        self.assertIsNone(self.row(q2)["message_position"])
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", q2, "--card-text", "好的")
        self.assertEqual(rc, outbound.EXIT_OWNER_REPLIED)
        self.assertIn("我先说一句", out)

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
        self.assertEqual(_opt(self.lark.sent()[0], "--idempotency-key"),
                         outbound.idem_key("reply", "tp_alpha", self.q, outbound.text_fingerprint("收到，下班前给结论")))


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


# ======================= 第三轮：真实飞书测试用的两个开关 =======================
TEST_GROUP = "oc_test_owner_group"


class TestSwitchesTest(ThreadsBase):
    def use_switches(self, only: list[str] | None = None, owner_chat: str = "") -> None:
        """同时改测试里直接用的配置和 CLI 读的 config.toml。"""
        self.cfg.source.only_chat_ids = list(only or [])
        self.cfg.threads.owner_chat_id = owner_chat
        extra = ""
        if only is not None:
            extra += "[source]\nonly_chat_ids = [" + ", ".join(f'"{c}"' for c in only) + "]\n"
        if owner_chat:
            extra += f'[threads]\nowner_chat_id = "{owner_chat}"\n'
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + extra, encoding="utf-8")

    def chats_in_ledger(self) -> set[str]:
        return {r[0] for r in self.store.conn.execute("SELECT DISTINCT chat_id FROM messages")}

    def test_only_chat_ids_keeps_other_chats_out_of_the_ledger(self):
        self.lark.chat_types[TEST_GROUP] = "group"
        self.use_switches(only=[TEST_GROUP])
        before = self.chats_in_ledger()
        kept = self.lark.add(chat_id=TEST_GROUP, chat_type="group", sender_id=ALICE, content="测试群里的话")
        others = [self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="正式私聊里的话"),
                  self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="主人在机器人私聊里说的"),
                  self.lark.add(chat_id="oc_test_some_group", chat_type="group", sender_id=ALICE, content="别的群")]
        self.poll()
        self.d.dispatch_once()
        self.assertIsNotNone(self.row(kept))
        for mid in others:
            self.assertIsNone(self.row(mid))                       # 不入账本（不是入账本再丢）
        self.assertEqual(self.chats_in_ledger() - before - {"sheepdog"}, {TEST_GROUP})
        read_ids = {i for c in self.lark.calls if c[1] == "+messages-read-status"
                    for i in _opt(c, "--message-ids").split(",")}
        self.assertFalse(read_ids & set(others))                    # 不查已读
        self.assertNotIn("reactions", self.lark.subcommands())     # 不点表情

    def test_without_only_chat_ids_everything_is_ingested(self):
        mids = [self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="正式私聊里的话"),
                self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="主人在机器人私聊里说的")]
        self.poll()
        for mid in mids:
            self.assertIsNotNone(self.row(mid))

    def test_owner_chat_id_sends_to_group_and_thread_reply_goes_back(self):
        self.lark.chat_types[TEST_GROUP] = "group"
        self.use_switches(only=[TEST_GROUP], owner_chat=TEST_GROUP)
        rc, out, _ = self.ask("--key", "T1", text="测试群里问的")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.ask("--key", "T1", kind="report", title="进度", text="话题里汇报")
        self.assertEqual(rc, 0, out)
        send, reply = self.lark.sent()
        self.assertEqual((send[1], _opt(send, "--chat-id"), _opt(send, "--as")), ("+messages-send", TEST_GROUP, "bot"))
        self.assertNotIn("--user-id", send)                         # 不再私聊主人
        root = self.store.thread_by_key("tp_alpha", "T1")["root_message_id"]
        self.assertEqual(self.lark.msgs[root]["chat_id"], TEST_GROUP)
        self.assertEqual((reply[1], _opt(reply, "--message-id")), ("+messages-reply", root))
        self.assertIn("--reply-in-thread", reply)
        self.poll()
        self.assertIsNotNone(self.store.get_escalation(root))       # 测试群当成找主人聊天：登记「需要你定」
        mid = self.lark.add(chat_id=TEST_GROUP, sender_id=ME, content="测试群里回：可以", thread_root=root)
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "1")   # 开头 -1，机器人的汇报 0，主人这条 1
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_reply", "tp_alpha"))
        text = self.deliver()
        self.assertIn("🧵 主人在话题里回复", text)
        self.assertIn("测试群里问的", text)
        self.assertEqual(self.store.get_escalation(root)["close_reason"], "answered")

    def test_owner_chat_plain_message_and_agent_reply(self):
        self.lark.chat_types[TEST_GROUP] = "group"
        self.use_switches(owner_chat=TEST_GROUP)
        self.ask(text="一个问题")
        self.poll()
        # 主人在群里直接说（没引用、没话题）：按 owner_reply 走（唯一未结在 tp_alpha）
        plain = self.lark.add(chat_id=TEST_GROUP, chat_type="group", sender_id=ME, content="好的")
        # 会话以主人身份在群里代回的：不算主人发言
        agent = self.lark.add(chat_id=TEST_GROUP, chat_type="group", sender_id=ME,
                              content=f"<card>\n代回的话\n{MARK}\n</card>")
        self.poll()
        self.assertEqual((self.row(plain)["reason"], self.row(plain)["topic_id"]), ("owner_reply", "tp_alpha"))
        self.assertEqual((self.row(agent)["route"], self.row(agent)["reason"]), ("drop", "escalation_agent_reply"))
        self.assertNotIn("代回的话", self.deliver())

    def test_without_owner_chat_id_ask_still_dms_owner(self):
        self.ask("--key", "T2")
        send = self.lark.sent()[0]
        self.assertEqual((_opt(send, "--user-id"), _opt(send, "--chat-id")), (ME, ""))
        self.assertEqual(self.cfg.owner_chat_ids, [ESC_CHAT])

    def test_config_validation_and_doctor(self):
        from sheepdog.cli import switches_report
        from sheepdog.config import ConfigError, load_config
        base = self.cfg_file.read_text(encoding="utf-8")
        cases = [
            (f'[source]\nonly_chat_ids = ["oc_test_a"]\n[threads]\nowner_chat_id = "{TEST_GROUP}"\n', "不在"),
            ('[source]\nonly_chat_ids = ["ou_test_x"]\n', "only_chat_ids"),
            ('[threads]\nowner_chat_id = "ou_test_x"\n', "owner_chat_id"),
        ]
        for extra, frag in cases:
            self.cfg_file.write_text(base + extra, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, frag):
                load_config(self.cfg_file)
        no_regex = base.replace(f'chat_ids = ["{ESC_CHAT}"]\n', "").replace("header_regex", "unused_regex")
        self.cfg_file.write_text(no_regex + f'[threads]\nowner_chat_id = "{TEST_GROUP}"\n', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "owner_chat_id 已配置但 \\[escalation\\] header_regex 为空"):
            load_config(self.cfg_file)
        self.cfg_file.write_text(base + f'[source]\nonly_chat_ids = ["{TEST_GROUP}"]\n'
                                 f'[threads]\nowner_chat_id = "{TEST_GROUP}"\n', encoding="utf-8")
        cfg = load_config(self.cfg_file)
        self.assertEqual(cfg.owner_chat_ids, [ESC_CHAT, TEST_GROUP])
        lines = switches_report(cfg)
        self.assertTrue(any(x.startswith("⚠ 只拉这些聊天：" + TEST_GROUP) for x in lines))
        self.assertTrue(any(x.startswith("⚠ 找主人改发到群 " + TEST_GROUP) for x in lines))
        self.assertEqual(switches_report(self.cfg), [])


# ======================= 第四轮：thread 排序、doctor 的找主人聊天 =======================
class OrderAndDoctorTest(ThreadsBase):
    def test_thread_command_orders_same_minute_by_position(self):
        # 10-13 实测：同一分钟里机器人的「第二次进度」（话题里第 2 条）和主人的「B」（第 3 条），按时间排反了
        m0 = now8()
        self.ask("--key", "K40", text="开头")
        root = self.store.thread_by_key("tp_alpha", "K40")["root_message_id"]
        self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="A", thread_root=root, at=m0)
        self.ask("--key", "K40", kind="report", title="进度", text="第二次进度")
        b = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="B", thread_root=root, at=m0)
        progress = self.store.conn.execute("SELECT message_id FROM sends WHERE kind='report'").fetchone()[0]
        # sheepdog 发出的记录时间带秒（同一分钟的 50 秒），话题列表里的时间只到分钟：按时间排「B」会跑到前面
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE message_id=?",
                                ((m0 + timedelta(seconds=50)).isoformat(), progress))
        self.store.conn.commit()
        self.assertEqual(self.lark.raw(progress)["thread_message_position"], "1")
        self.assertEqual(self.lark.raw(b)["thread_message_position"], "2")
        rc, out, _ = self.cli("thread", "--topic", "tp_alpha", "--key", "K40")
        self.assertEqual(rc, 0, out)
        order = [out.index(x) for x in ("【开头】", "：\n  A", "第二次进度", "：\n  B")]
        self.assertEqual(order, sorted(order), out)

    def test_order_thread_items_rules(self):
        from sheepdog.engine import order_thread_items
        t = "2026-01-01T10:00:{:02d}+08:00"
        root = {"message_id": "om_r", "root": True, "at": t.format(30), "tpos": -1, "mpos": 1}
        a = {"message_id": "om_a", "root": False, "at": t.format(50), "tpos": 0, "mpos": 2}
        b = {"message_id": "om_b", "root": False, "at": t.format(0), "tpos": 1, "mpos": 3}
        ids = lambda xs: [x["message_id"] for x in xs]
        self.assertEqual(ids(order_thread_items([b, a, root])), ["om_r", "om_a", "om_b"])      # 按话题里的序号
        a2, b2 = dict(a, tpos=None), dict(b, tpos=None)
        self.assertEqual(ids(order_thread_items([b2, root, a2])), ["om_r", "om_a", "om_b"])    # 退回聊天里的序号
        a3, b3 = dict(a2, mpos=None), dict(b2, mpos=None)
        self.assertEqual(ids(order_thread_items([a3, root, b3])), ["om_r", "om_b", "om_a"])    # 都没有才按时间

    def test_doctor_owner_chats_follow_owner_chat_ids(self):
        from sheepdog.cli import owner_chats_report
        self.cfg.escalation.chat_ids = []
        self.assertIn("未配置", owner_chats_report(self.cfg))
        self.cfg.threads.owner_chat_id = TEST_GROUP
        line = owner_chats_report(self.cfg)
        self.assertNotIn("未配置", line)
        self.assertIn("✓ 1 个，含测试群", line)
        # 从配置文件读：只设 owner_chat_id、不设 [escalation] chat_ids
        from sheepdog.config import load_config
        text = self.cfg_file.read_text(encoding="utf-8").replace(f'chat_ids = ["{ESC_CHAT}"]\n', "")
        self.cfg_file.write_text(text + f'[threads]\nowner_chat_id = "{TEST_GROUP}"\n', encoding="utf-8")
        cfg = load_config(self.cfg_file)
        self.assertEqual(cfg.owner_chat_ids, [TEST_GROUP])
        self.assertIn("✓ 1 个", owner_chats_report(cfg))
        rc, out, _ = self.cli("doctor")
        self.assertIn("找主人聊天: ✓ 1 个，含测试群", out)
