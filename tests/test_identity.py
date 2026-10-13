"""7.26 区分智能体身份和本人身份。

假的飞书沿用 test_threads.FakeLark（只在内存里，绝不起真实进程），再加上机器人身份的几样：
- 机器人发的消息：搜索 / mget 里发送人是 app_id（cli_…）、sender_type app（真实账本里就是这样）；
- 机器人身份的群列表（+chat-list --as bot）、机器人往它不在的群里回复报 230002；
- 机器人身份实时接收的事件按 lark-cli event consume im.message.receive_v1 的真实格式造（10-13 03:0x 实测一条）：
  拍平的、没有群名 / 发送人名字 / 序号、sender_type 是 bot、create_time 是毫秒时间戳、卡片正文是原始 JSON。

定稿行为 1–8 每条都有：
1 两个身份进同一个账本：两边收到同一条只记一条、记下谁收到的；事件先到用 mget 补齐；正文渲染不同不当编辑；接收子进程
  退避重启、doctor 显示状态。
2 @机器人 = @主人（at_bot，免打扰之前）；回复机器人发的消息 = 回复主人。
3 机器人自己的消息：不分派、不算主人发言（背景、发前检查都不算），防循环照样计数。
4 别人私聊机器人：drop / bot_dm，自动回一句，同一个人 24 小时一次；主人和机器人的私聊照旧。
5 代回选身份：私聊本人；群里机器人在用机器人；不在退回本人并记下；两个身份都带代回标记（Matthew 10-13 15:2x「还是要带
  这是我的logo 有广告意义」），防重编号按带标记的正文算（同 9544894）；230002 退回、内容和编号不变；缓存 10 分钟。
6 主动新发（ask / report 以外没有新发命令）：不改。
7 找主人（ask / report）：机器人，不改。
8 开关关着：和以前完全一样（路由、代回、账本、飞书调用逐项对比）。
"""

import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, identity, outbound
from sheepdog.config import ConfigError, IdentityConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.router import DISPATCH, DROP, INBOX, SELF, RouteContext, route
from sheepdog.source.bot_events import BotEventConsumer
from sheepdog.source.lark import LarkCliSource, parse_event, parse_message
from sheepdog.store import Store

from test_roster import ME, Base, msg
from test_threads import ALICE, ALPHA_P2P, ESC_CHAT, MARK, TZ8, FakeLark, ThreadsBase, _opt, now8, snapshot

BOT_OID = "ou_test_botself"         # 机器人自己的 open_id（事件、@ 里）
APP = "cli_test_app"                # 机器人的 app_id（搜索结果里机器人发的消息的发送人）
GRP = "oc_test_alpha_grp"           # tp_alpha 负责的群（只推 dispatch 级）；机器人在
NOBOT = "oc_test_nobot_grp"         # 机器人不在的群
BOB = "ou_test_bob"
OTHER_BOT = "ou_test_otherbot"


class IdentityLark(FakeLark):
    """FakeLark + 机器人身份：机器人发的消息发送人是 app_id；机器人的群列表；不在群里回复报 230002。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bot_chats = {GRP}
        self.chat_types.update({GRP: "group", NOBOT: "group"})

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        args = list(argv[1:])
        if args[:2] == ["im", "+chat-list"] and _opt(args, "--as") == "bot" and not self.plan.get("+chat-list"):
            self.calls.append(args)
            return self._out({"ok": True, "data": {"chats": [{"chat_id": c} for c in sorted(self.bot_chats)],
                                                   "has_more": False}})
        if args[:2] == ["im", "+messages-reply"] and _opt(args, "--as") == "bot" \
                and not self.plan.get("+messages-reply"):
            target = self.msgs.get(_opt(args, "--message-id"))
            if target is not None and target["chat_type"] != "p2p" and target["chat_id"] not in self.bot_chats:
                self.calls.append(args)
                return self._out({"ok": False, "error": {"type": "api", "code": 230002,
                                                         "message": "Bot/User can NOT be out of the chat."}}, 1)
        if args[:3] == ["api", "GET", "/open-apis/bot/v3/info"]:
            self.calls.append(args)
            return self._out({"ok": True, "data": {"app_name": "测试机器人", "open_id": BOT_OID}})
        return super().__call__(argv, capture_output, text, timeout)

    def add(self, **kw) -> str:
        mid = super().add(**kw)
        if kw.get("sender_type") == "app" and kw.get("sender_id") in (None, "ou_test_bot"):
            self.msgs[mid]["sender"]["id"] = APP
        return mid

    def _reply(self, args):
        out = super()._reply(args)
        if _opt(args, "--as") == "bot":
            self.msgs[out["message_id"]]["sender"]["id"] = APP
        return out

    def raw(self, mid: str) -> dict:
        out = super().raw(mid)
        ms = self.msgs[mid].get("mentions")
        if ms:
            out["mentions"] = [{"id": i, "key": f"@_user_{n}", "name": "x"} for n, i in enumerate(ms, 1)]
        return out

    def event(self, mid: str, **over) -> dict:
        """这条消息按 event consume 的格式（机器人身份收到的样子）。"""
        m = self.msgs[mid]
        ev = {"type": "im.message.receive_v1", "event_id": "ev_" + mid, "timestamp": "0", "id": mid,
              "message_id": mid, "create_time": str(int(m["at"].timestamp() * 1000) + 37_000),
              "chat_id": m["chat_id"], "chat_type": "group" if m["chat_type"] == "topic" else m["chat_type"],
              "message_type": "text", "sender_id": m["sender"]["id"],
              "sender_type": "bot" if m["sender"]["sender_type"] == "app" else "user", "content": m["content"]}
        if m["thread_id"]:
            ev["thread_id"] = m["thread_id"]
        ev.update(over)
        return ev


class FakeConsumer:
    """代替 BotEventConsumer：poll() 一次交出排好的事件。"""

    def __init__(self):
        self.pending: list[dict] = []
        self.polls = 0

    def poll(self):
        self.polls += 1
        out, self.pending = self.pending, []
        return out


class IdentityBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.lark = IdentityLark()          # 换成带机器人身份的假飞书（ThreadsBase 开场用的那个不再用）
        p = mock.patch("sheepdog.source.lark.subprocess.run", self.lark)
        p.start()
        self.addCleanup(p.stop)
        self.cfg.identity = IdentityConfig(enabled=True, bot_open_id=BOT_OID, bot_app_id=APP)
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8")
                                 + f'[identity]\nenabled = true\nbot_open_id = "{BOT_OID}"\nbot_app_id = "{APP}"\n',
                                 encoding="utf-8")
        self.consumer = FakeConsumer()
        self.lark.calls.clear()

    def collector(self) -> Collector:
        c = Collector(self.cfg, self.store, self.source, self.roster)
        c.bot_events = self.consumer
        return c

    def poll(self):
        return self.collector().poll_once()

    def pull(self, *mids: str):
        msgs = [parse_message(self.lark.raw(m)) for m in mids]
        return self.collector().ingest(msgs)

    def bot_hears(self, *events: dict):
        """机器人身份实时收到这几条（下一轮 poll 时入账，不拉主人身份的搜索）。"""
        c = self.collector()
        self.consumer.pending += list(events)
        return c._poll_bot_events()

    def group_msg(self, content="大家看一下", chat=GRP, sender=ALICE, mentions=(), at=None, sender_type="user") -> str:
        mid = self.lark.add(chat_id=chat, sender_id=sender, content=content, chat_type="group", at=at,
                            sender_type=sender_type)
        if mentions:
            self.lark.msgs[mid]["mentions"] = list(mentions)
        return mid


# ======================= 1：两个身份进同一个账本 =======================
class DedupTest(IdentityBase):
    def test_user_first_then_bot_one_row_both(self):
        mid = self.group_msg("周五发版吗 故障")
        self.pull(mid)
        before = self.row(mid)
        self.assertEqual(before["received_by"], "user")
        st = self.bot_hears(self.lark.event(mid))
        self.assertEqual(st.get("merged"), 1)
        after = self.row(mid)
        self.assertEqual(after["received_by"], "both")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE message_id=?", (mid,)).fetchone()[0], 1)
        # 只合并：路由、正文、投递状态都不变
        for k in ("route", "reason", "topic_id", "content", "dispatch_state", "create_time"):
            self.assertEqual(after[k], before[k], k)
        self.assertNotIn("+messages-mget", self.lark.subcommands())   # 账本里已有的不 mget

    def test_bot_first_then_user_one_row_both_and_no_edit(self):
        mid = self.group_msg("周五发版吗 故障")
        st = self.bot_hears(self.lark.event(mid))
        self.assertEqual(st["new"], 1)
        r = self.row(mid)
        self.assertEqual((r["received_by"], r["route"], r["reason"], r["topic_id"]), ("bot", DISPATCH, "keyword", "tp_alpha"))
        # 事件先到：用 mget 取了一份，群名、序号、时间（只到分钟）和搜索一样
        self.assertIn("+messages-mget", self.lark.subcommands())
        self.assertEqual(r["chat_name"], "测试私聊")
        self.assertTrue(r["message_position"])
        self.pull(mid)                          # 一分钟后主人身份的搜索也搜到了
        r2 = self.row(mid)
        self.assertEqual(r2["received_by"], "both")
        for k in ("route", "reason", "content", "dispatch_state", "updated"):
            self.assertEqual(r2[k], r[k], k)
        self.pull(mid)                          # 再搜到一次（重叠窗口）也不变
        self.assertEqual(self.row(mid)["received_by"], "both")
        self.assertEqual(self.row(mid)["reason"], "keyword")

    def test_event_format_content_replaced_silently_when_mget_fails(self):
        mid = self.group_msg("看一下这张卡")
        card_json = json.dumps({"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "看一下这张卡"}]}})
        self.lark.plan["+messages-mget"] = ["error", "error"]
        self.bot_hears(self.lark.event(mid, message_type="interactive", content=card_json))
        r = self.row(mid)
        self.assertEqual((r["content"], r["chat_name"], r["received_by"]), (card_json, "", "bot"))
        self.d.dispatch_once()
        self.pull(mid)                          # 搜索的正文渲染不一样：换成它，不当编辑
        r2 = self.row(mid)
        self.assertEqual((r2["content"], r2["chat_name"], r2["received_by"]), ("看一下这张卡", "测试私聊", "both"))
        self.assertNotIn(r2["reason"], ("edited", "recalled"))
        self.assertEqual(r2["dispatch_state"], r["dispatch_state"])

    def test_recall_seen_by_user_after_bot_copy_still_handled(self):
        mid = self.group_msg("周五发版吗 故障")
        self.bot_hears(self.lark.event(mid))
        self.deliver()
        self.lark.msgs[mid]["deleted"] = True
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["received_by"]), ("recalled", "both"))

    def test_rows_from_before_switch_count_as_user(self):
        self.cfg.identity.enabled = False
        mid = self.group_msg("周五发版吗 故障")
        self.pull(mid)
        self.assertIsNone(self.row(mid)["received_by"])
        self.cfg.identity.enabled = True
        self.bot_hears(self.lark.event(mid))
        self.assertEqual(self.row(mid)["received_by"], "both")

    def test_same_poll_round_user_then_bot(self):
        mid = self.group_msg("周五发版吗 故障", at=now8() - timedelta(minutes=1))
        self.consumer.pending.append(self.lark.event(mid))
        st = self.poll()
        self.assertEqual((st["new"], st["bot"].get("merged")), (1, 1))
        self.assertEqual(self.row(mid)["received_by"], "both")

    def test_event_parse_matches_real_format(self):
        raw = {"type": "im.message.receive_v1", "event_id": "e1", "timestamp": "1791831971596",
               "id": "om_test_ev1", "message_id": "om_test_ev1", "create_time": "1791831971411",
               "chat_id": "oc_test_g", "chat_type": "group", "message_type": "text", "sender_id": "ou_test_x",
               "sender_type": "user", "root_id": "om_test_root", "thread_id": "omt_test_1", "reply_to": "om_test_p",
               "content": "收到，我看下", "mentions": [{"id": BOT_OID, "key": "@_user_1", "name": "Mattheuu"}]}
        m = parse_event(raw, "+08:00")
        self.assertEqual((m.message_id, m.chat_id, m.chat_type, m.sender_type, m.reply_to, m.thread_id),
                         ("om_test_ev1", "oc_test_g", "group", "user", "om_test_p", "omt_test_1"))
        self.assertEqual(m.create_time, "2026-10-13T03:06:00+08:00")   # 到分钟，和搜索一样
        self.assertEqual([x.id for x in m.mentions], [BOT_OID])
        self.assertEqual(parse_event({**raw, "sender_type": "bot"}).sender_type, "app")

    def test_only_chat_ids_applies_to_bot_events(self):
        self.cfg.source.only_chat_ids = [ALPHA_P2P]
        mid = self.group_msg("周五发版吗 故障")
        st = self.bot_hears(self.lark.event(mid))
        self.assertEqual(st, {"fetched": 0})
        self.assertIsNone(self.row(mid))


# ======================= 1：接收子进程 =======================
class FakeProc:
    def __init__(self, lines=(), err=(), pid=4242):
        import io as _io
        self.stdout = _io.StringIO("".join(x + "\n" for x in lines))
        self.stderr = _io.StringIO("".join(x + "\n" for x in err))
        self.stdin = _io.StringIO()
        self.pid = pid
        self.returncode = None
        self.closed_stdin = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.returncode = -15


class ConsumerTest(Base):
    def make(self, procs):
        self.procs = list(procs)
        self.started = []
        self.now = [100.0]
        self.states = []

        def popen(cmd, **kw):
            self.started.append(cmd)
            if not self.procs:
                raise OSError("没有 lark-cli")
            return self.procs.pop(0)
        return BotEventConsumer("lark-cli", 5, 40, state_writer=self.states.append, popen=popen,
                                clock=lambda: self.now[0])

    def drain(self, c):
        import time as _t
        for _ in range(50):
            if not c.queue.empty():
                break
            _t.sleep(0.01)
        _t.sleep(0.05)
        return c.poll()

    def test_reads_events_and_ready_marker(self):
        ev = {"message_id": "om_test_1", "chat_id": "oc_x"}
        p = FakeProc([json.dumps(ev), "不是 json", ""], ["[event] ready event_key=im.message.receive_v1"])
        c = self.make([p])
        c.start()
        self.assertEqual(self.started[0], ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"])
        got = self.drain(c)
        self.assertEqual(got, [ev])
        self.assertEqual((c.state["state"], c.state["events"], c.state["bad_lines"]), ("running", 1, 1))
        self.assertTrue(c.state["last_event_at"])
        self.assertEqual(self.states[-1]["pid"], 4242)

    def test_exit_restarts_with_backoff_and_resets_after_ready(self):
        p1, p2, p3 = FakeProc(err=['{"ok":false,"error":{"type":"auth"}}']), FakeProc(), FakeProc()
        c = self.make([p1, p2, p3])
        c.start()
        p1.returncode = 3
        self.drain(c)                                   # 退出：记下原因，5 秒后再起
        self.assertEqual(c.state["state"], "down")
        self.assertIn("退出码 3", c.state["last_exit"])
        self.assertIn("auth", c.state["last_error"])
        self.assertEqual(len(self.started), 1)
        self.now[0] += 5
        c.poll()
        self.assertEqual((len(self.started), c.state["restarts"]), (2, 1))
        p2.returncode = 1
        c.poll()
        self.now[0] += 9                                # 第二次退避 10 秒：9 秒还不到
        c.poll()
        self.assertEqual(len(self.started), 2)
        self.now[0] += 1
        c.poll()
        self.assertEqual(len(self.started), 3)
        self.assertEqual(c.delay, 20)                   # 退避 5 → 10 → 下一次 20
        c.queue.put(("ready", p3, "[event] ready"))
        c.poll()
        self.assertEqual((c.state["state"], c.delay), ("running", 5))

    def test_stop_closes_stdin(self):
        p = FakeProc()
        c = self.make([p])
        c.start()
        c.stop()
        self.assertTrue(p.stdin.closed)
        self.assertEqual(c.state["state"], "stopped")

    def test_spawn_failure_is_retried(self):
        c = self.make([])
        c.start()
        self.assertEqual(c.state["state"], "down")
        self.assertIn("起不来", c.state["last_error"])


# ======================= 2：@机器人、回复机器人 =======================
class RouteTest(Base):
    def ctx(self, **kw):
        return RouteContext(ME, bot_ids=frozenset({BOT_OID, APP}), **kw)

    def test_at_bot_same_as_at_me_and_before_muted(self):
        m = msg(mentions=[Mention(id=BOT_OID, name="Mattheuu")])
        self.assertEqual(route(m, self.ctx(), self.cfg.routing).reason, "at_bot")
        self.assertEqual(route(m, self.ctx(muted_chat_ids={"oc_test_g1"}), self.cfg.routing).reason, "at_bot")
        both = msg(mentions=[Mention(id=BOT_OID), Mention(id=ME)])
        self.assertEqual(route(both, self.ctx(), self.cfg.routing).reason, "at_me")
        # 关着：@机器人 认不出来，和以前一样只是群消息
        self.assertEqual(route(m, RouteContext(ME), self.cfg.routing).reason, "group")

    def test_bot_own_message_is_self(self):
        for sid in (APP, BOT_OID):
            d = route(msg(sender_id=sid, sender_type="app", content="故障 处理中"), self.ctx(), self.cfg.routing)
            self.assertEqual((d.route, d.reason), (SELF, "self_bot"))
        d = route(msg(sender_id=APP, sender_type="app", content="故障 处理中"), RouteContext(ME), self.cfg.routing)
        self.assertEqual(d.route, INBOX)      # 关着：机器人自己的群消息和别的机器人一样


class ReplyToBotTest(IdentityBase):
    def test_reply_to_bot_message_same_as_reply_to_owner(self):
        bot_said = self.lark.add(chat_id=NOBOT, sender_id="ou_test_bot", sender_type="app", content="我是机器人代回的",
                                 chat_type="group")
        self.pull(bot_said)
        self.assertEqual((self.row(bot_said)["route"], self.row(bot_said)["reason"]), (SELF, "self_bot"))
        r = self.lark.add(chat_id=NOBOT, sender_id=ALICE, content="好的谢谢", chat_type="group")
        self.lark.msgs[r]["reply_to"] = bot_said
        raw = self.lark.raw(r)
        raw["reply_to"] = bot_said
        self.collector().ingest([parse_message(raw)])
        self.assertEqual((self.row(r)["route"], self.row(r)["reason"]), (DISPATCH, "reply_to_me"))

    def test_reply_to_bot_message_not_in_ledger_uses_mget_sender(self):
        bot_said = self.lark.add(chat_id=NOBOT, sender_id="ou_test_bot", sender_type="app", content="旧的代回",
                                 chat_type="group")
        r = self.lark.add(chat_id=NOBOT, sender_id=ALICE, content="追问一下", chat_type="group")
        raw = self.lark.raw(r)
        raw["reply_to"] = bot_said
        self.collector().ingest([parse_message(raw)])
        self.assertEqual(self.row(r)["reason"], "reply_to_me")

    def test_disabled_reply_to_bot_is_plain_group(self):
        self.cfg.identity.enabled = False
        bot_said = self.lark.add(chat_id=NOBOT, sender_id="ou_test_bot", sender_type="app", content="代回", chat_type="group")
        self.pull(bot_said)
        r = self.lark.add(chat_id=NOBOT, sender_id=ALICE, content="好的", chat_type="group")
        raw = self.lark.raw(r)
        raw["reply_to"] = bot_said
        self.collector().ingest([parse_message(raw)])
        self.assertEqual(self.row(r)["reason"], "group")

    def test_at_bot_event_dispatches_to_owner_of_chat_and_acks_like_at_me(self):
        from sheepdog.ack import Acker
        from sheepdog.config import AckConfig
        mid = self.group_msg("@Mattheuu 帮忙看下", mentions=[BOT_OID])
        self.bot_hears(self.lark.event(mid, mentions=[{"id": BOT_OID, "key": "@_user_1", "name": "Mattheuu"}]))
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), (DISPATCH, "at_bot", "tp_alpha"))
        acker = Acker(AckConfig(enabled=True, emoji_type="OK", reasons=["at_me"], remove_on_reply_reasons=["at_me"]),
                      self.store, None)
        self.assertEqual(acker.identity_for(r), "user")


# ======================= 3：机器人自己的消息 =======================
class BotSelfTest(IdentityBase):
    def test_bot_reply_is_not_owner_speech_and_not_dispatched(self):
        bot_said = self.lark.add(chat_id=GRP, sender_id="ou_test_bot", sender_type="app", content="故障 已处理",
                                 chat_type="group")
        self.pull(bot_said)
        r = self.row(bot_said)
        self.assertEqual((r["route"], r["reason"], r["dispatch_state"]), (SELF, "self_bot", None))
        self.assertNotIn("故障 已处理", self.deliver())       # 不分派、也不当主人的发言送背景

    def test_owner_spoke_check_ignores_bot_messages(self):
        q = self.group_msg("什么时候发版？", mentions=[BOT_OID], at=now8() - timedelta(minutes=5))
        self.pull(q)
        self.deliver()
        self.lark.add(chat_id=GRP, sender_id="ou_test_bot", sender_type="app", content="机器人先回了一句",
                      chat_type="group", at=now8() - timedelta(minutes=3))
        res = outbound.reply(self.cfg, self.store, self.source, "tp_alpha", q, card_text="周五")
        self.assertIsNone(res.spoke)
        self.assertTrue(res.message_id)

    def test_loop_guard_counts_bot_replies(self):
        self.cfg.loop_guard.max_agent_replies = 2
        agent = self.group_msg("自动回复", sender=OTHER_BOT, sender_type="app")
        self.pull(agent)
        ids = [self.lark.add(chat_id=GRP, sender_id="ou_test_bot", sender_type="app", content=f"代回 {i}",
                             chat_type="group") for i in range(3)]
        self.pull(*ids)
        self.assertEqual(len(self.store.open_loop_events()), 1)

    def test_loop_guard_bot_replies_to_human_do_not_trip(self):
        self.cfg.loop_guard.max_agent_replies = 2
        self.pull(self.group_msg("人问的"))
        ids = [self.lark.add(chat_id=GRP, sender_id="ou_test_bot", sender_type="app", content=f"代回 {i}",
                             chat_type="group") for i in range(3)]
        self.pull(*ids)
        self.assertEqual(self.store.open_loop_events(), [])

    def test_bot_reply_removes_ack_like_owner_reply(self):
        from sheepdog.ack import Acker
        from sheepdog.config import AckConfig
        q = self.group_msg("@Mattheuu 看下", mentions=[BOT_OID])
        self.pull(q)
        acker = Acker(AckConfig(enabled=True, emoji_type="OK", reasons=["at_me"], remove_on_reply_reasons=["at_me"]),
                      self.store, self.source)
        acker.on_delivered([self.row(q)])
        self.assertEqual(self.store.get_ack(q)["identity"], "user")
        bot_said = self.lark.add(chat_id=GRP, sender_id="ou_test_bot", sender_type="app", content="看过了", chat_type="group")
        raw = self.lark.raw(bot_said)
        raw["reply_to"] = q
        c = self.collector()
        c.acker = acker
        c.ingest([parse_message(raw)])
        self.assertTrue(self.store.get_ack(q)["removed_at"])


# ======================= 4：别人私聊机器人 =======================
class BotDmTest(IdentityBase):
    def dm(self, sender=BOB, text="在吗", chat="oc_test_bob_bot", sender_type="user") -> str:
        mid = self.lark.add(chat_id=chat, sender_id=sender, content=text, chat_type="p2p", sender_type=sender_type)
        self.lark.plan["+messages-mget"] = ["error"] * 2   # 主人不在这个私聊里：mget 取不到
        self.bot_hears(self.lark.event(mid))
        return mid

    def bot_replies(self):
        return [c for c in self.lark.sent() if _opt(c, "--as") == "bot" and c[1] == "+messages-reply"]

    def test_not_dispatched_and_auto_reply_once_per_day(self):
        first = self.dm()
        r = self.row(first)
        self.assertEqual((r["route"], r["reason"], r["topic_id"], r["received_by"]), (DROP, "bot_dm", None, "bot"))
        sent = self.bot_replies()
        self.assertEqual(len(sent), 1)
        self.assertEqual(_opt(sent[0], "--message-id"), first)
        self.assertEqual(_opt(sent[0], "--text"), "FX-BOT-DM 请直接私聊主人本人")   # playbook 的 bot_dm.md
        self.dm(text="再问一下")                                  # 24 小时内同一个人：不再回
        self.assertEqual(len(self.bot_replies()), 1)
        self.dm(sender="ou_test_carol", chat="oc_test_carol_bot")  # 另一个人：回
        self.assertEqual(len(self.bot_replies()), 2)
        self.store.conn.execute("UPDATE bot_dm_replies SET replied_at=? WHERE sender_id=?",
                                ((datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds"), BOB))
        self.store.conn.commit()
        self.dm(text="过了一天")                                  # 过了 24 小时：再回一次
        self.assertEqual(len(self.bot_replies()), 3)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='bot_dm'").fetchone()[0], 4)
        self.assertEqual(self.store.pending_dispatch(), [])

    def test_other_bots_get_no_reply_and_failure_retries_next_time(self):
        self.dm(sender=OTHER_BOT, sender_type="app")
        self.assertEqual(self.bot_replies(), [])
        self.lark.plan["+messages-reply"] = ["error"]
        self.dm()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM bot_dm_replies").fetchone()[0], 0)
        self.dm(text="又来")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM bot_dm_replies").fetchone()[0], 1)

    def test_default_text_without_playbook_file(self):
        self.cfg.playbook_dir = str(Path(self.tmp.name) / "no_playbook")
        self.dm()
        self.assertEqual(_opt(self.bot_replies()[0], "--text"), identity.DEFAULT_BOT_DM)

    def test_owner_bot_chat_unchanged(self):
        # 主人和机器人的私聊（找主人聊天）：主人在里面的回复照 7.22 走，不是 bot_dm
        rc, _, _ = self.ask()
        self.assertEqual(rc, 0)
        q = self.lark.sent()[0]
        ask_mid = next(k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT)
        self.pull(ask_mid)
        ans = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="周五")
        self.bot_hears(self.lark.event(ans))
        r = self.row(ans)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), (DISPATCH, "owner_reply", "tp_alpha"))
        self.assertEqual(self.bot_replies(), [])
        self.assertEqual(_opt(q, "--as"), "bot")


# ======================= 5：代回选身份 =======================
class ReplyIdentityTest(IdentityBase):
    def target(self, chat=GRP, chat_type="group", **kw) -> str:
        if chat_type == "p2p":
            q = self.lark.add(chat_id=chat, sender_id=ALICE, content="今天能给结论吗？", at=now8() - timedelta(minutes=5))
        else:
            q = self.group_msg("@Matthew 今天能给结论吗？", chat=chat, mentions=[ME], at=now8() - timedelta(minutes=5))
        self.pull(q)
        self.store.conn.execute("UPDATE messages SET topic_id='tp_alpha' WHERE message_id=?", (q,))
        self.store.conn.commit()
        self.lark.calls.clear()
        return q

    def reply(self, q, text="收到，下班前给结论", **kw):
        return outbound.reply(self.cfg, self.store, self.source, "tp_alpha", q, card_text=text, **kw)

    def sent_reply(self):
        return [c for c in self.lark.sent() if c[1] == "+messages-reply"]

    def test_group_with_bot_uses_bot_with_mark(self):
        q = self.target()
        r = self.reply(q)
        self.assertEqual((r.identity, r.why), ("bot", identity.WHY_IN_CHAT))
        c = self.sent_reply()[-1]
        self.assertEqual((_opt(c, "--as"), _opt(c, "--msg-type")), ("bot", "interactive"))
        card = json.loads(_opt(c, "--content"))
        # 带标记，带法和主人身份完全一样（同一张卡片：正文 + 右下角灰字）
        self.assertEqual(card, outbound.reply_body(self.cfg, card_text="收到，下班前给结论")[1])
        self.assertIn(MARK, json.dumps(card, ensure_ascii=False))
        self.assertEqual(len(card["body"]["elements"]), 2)
        # 防重编号按带标记的正文算，和 9544894 的口径一样
        self.assertEqual(_opt(c, "--idempotency-key"),
                         outbound.idem_key("reply", "tp_alpha", q, outbound.text_fingerprint("收到，下班前给结论")))
        self.assertEqual(self.store.conn.execute("SELECT identity FROM sends WHERE kind='reply'").fetchone()[0], "bot")
        # 发前检查照样做（主人身份搜索）
        self.assertEqual(self.lark.subcommands()[0], "+messages-search")
        # 发出去的那条之后被拉到：机器人自己的，不分派
        self.pull(r.message_id)
        self.assertEqual(self.row(r.message_id)["reason"], "self_bot")

    def test_plain_text_bot_reply_has_mark(self):
        q = self.target()
        r = outbound.reply(self.cfg, self.store, self.source, "tp_alpha", q, text="好的")
        self.assertEqual(r.identity, "bot")
        c = self.sent_reply()[-1]
        self.assertEqual(_opt(c, "--text"), "好的\n" + MARK)
        self.assertEqual(_opt(c, "--idempotency-key"),
                         outbound.idem_key("reply", "tp_alpha", q, outbound.text_fingerprint("好的\n" + MARK)))

    def test_p2p_uses_owner_with_mark(self):
        q = self.target(chat=ALPHA_P2P, chat_type="p2p")
        r = self.reply(q)
        self.assertEqual((r.identity, r.why), ("user", identity.WHY_P2P))
        c = self.sent_reply()[-1]
        self.assertEqual(_opt(c, "--as"), "user")
        self.assertIn(MARK, _opt(c, "--content"))
        self.assertNotIn("+chat-list", self.lark.subcommands())    # 私聊不查机器人的群

    def test_group_without_bot_falls_back_and_is_listed(self):
        q = self.target(chat=NOBOT)
        r = self.reply(q)
        self.assertEqual((r.identity, r.why), ("user", identity.WHY_NOT_IN_CHAT))
        c = self.sent_reply()[-1]
        self.assertEqual(_opt(c, "--as"), "user")
        self.assertIn(MARK, _opt(c, "--content"))
        rows = self.store.bot_absent_chats()
        self.assertEqual([(x["chat_id"], x["fallbacks"], x["last_reason"]) for x in rows], [(NOBOT, 1, "not_in_chat")])
        lines = "\n".join(cli.identity_report(self.cfg, self.store))
        self.assertIn("机器人不在、代回退回本人的群 1 个", lines)
        self.assertIn(NOBOT, lines)
        # 机器人被拉进来以后：缓存过期重查，从清单里去掉，代回改用机器人
        self.lark.bot_chats.add(NOBOT)
        identity.invalidate_bot_chats(self.store)
        r2 = self.reply(q, text="第二条")
        self.assertEqual(r2.identity, "bot")
        self.assertEqual(self.store.bot_absent_chats(), [])

    def test_230002_falls_back_with_same_idem_key(self):
        q = self.target()
        self.store.set_meta(identity.META_CHATS, json.dumps([GRP]))
        self.store.set_meta(identity.META_CHATS_AT, datetime.now().astimezone().isoformat(timespec="seconds"))
        self.lark.bot_chats.discard(GRP)                     # 缓存说在，其实已经被移出群
        r = self.reply(q)
        self.assertEqual((r.identity, r.why), ("user", identity.WHY_230002))
        bot_try, user_try = self.sent_reply()[-2:]
        self.assertEqual((_opt(bot_try, "--as"), _opt(user_try, "--as")), ("bot", "user"))
        self.assertEqual(_opt(bot_try, "--idempotency-key"), _opt(user_try, "--idempotency-key"))
        self.assertEqual(_opt(bot_try, "--content"), _opt(user_try, "--content"))   # 内容一样，都带标记
        self.assertIn(MARK, _opt(user_try, "--content"))
        self.assertEqual(self.store.get_meta(identity.META_CHATS_AT), "")   # 缓存作废
        self.assertEqual(self.store.bot_absent_chats()[0]["last_reason"], "230002")
        self.assertEqual(self.store.conn.execute("SELECT identity FROM sends").fetchone()[0], "user")
        # 同样的内容重试：已经发过，不再调飞书
        n = len(self.lark.calls)
        r2 = self.reply(q)
        self.assertTrue(r2.already)
        self.assertEqual(len(self.lark.calls), n)

    def test_cache_ten_minutes(self):
        q = self.target()
        self.reply(q, text="一")
        self.reply(q, text="二")
        self.assertEqual(self.lark.subcommands().count("+chat-list"), 1)
        old = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat(timespec="seconds")
        self.store.set_meta(identity.META_CHATS_AT, old)
        self.reply(q, text="三")
        self.assertEqual(self.lark.subcommands().count("+chat-list"), 2)

    def test_list_failure_uses_stale_cache_or_owner(self):
        q = self.target()
        self.lark.plan["+chat-list"] = ["error"] * 3
        r = self.reply(q, text="一")
        self.assertEqual((r.identity, r.why), ("user", identity.WHY_LIST_FAILED))
        self.assertEqual(self.store.bot_absent_chats(), [])        # 查不到不算「机器人不在」
        self.store.set_meta(identity.META_CHATS, json.dumps([GRP]))
        self.store.set_meta(identity.META_CHATS_AT, "")
        self.lark.plan["+chat-list"] = ["error"] * 3
        self.assertEqual(self.reply(q, text="二").identity, "bot")  # 旧缓存兜底

    def test_owner_replied_check_applies_to_bot_identity(self):
        q = self.target()
        self.lark.add(chat_id=GRP, sender_id=ME, content="我来回", chat_type="group", at=now8())
        r = self.reply(q)
        self.assertTrue(r.spoke.messages)
        self.assertEqual(self.sent_reply(), [])

    def test_cli_reply_says_which_identity(self):
        q = self.target()
        self.d.dispatch_once()
        rc, out, _ = self.cli("reply", "--topic", "tp_alpha", "--to", q, "--card-text", "好")
        self.assertEqual(rc, 0, out)
        self.assertIn("机器人身份", out)

    def test_ask_report_still_bot(self):
        rc, _, _ = self.ask()
        self.assertEqual(rc, 0)
        c = self.lark.sent()[0]
        self.assertEqual((c[1], _opt(c, "--as")), ("+messages-send", "bot"))
        self.assertEqual(self.store.conn.execute("SELECT identity FROM sends WHERE kind='ask'").fetchone()[0], "bot")

    def test_session_interface_explains_identity_only_when_enabled(self):
        from sheepdog.playbook import Playbook
        from sheepdog.prompts import IDENTITY_REPLY_NOTE, interface_section
        pb = Playbook.from_config(self.cfg)
        self.assertTrue(pb.identity)
        self.assertIn(IDENTITY_REPLY_NOTE, interface_section("tp_alpha", bus=False, remind=[15], expire=60,
                                                             identity=pb.identity))
        self.cfg.identity.enabled = False
        pb = Playbook.from_config(self.cfg)
        self.assertNotIn("identity_enabled", pb.base_vars)
        self.assertNotIn(IDENTITY_REPLY_NOTE, interface_section("tp_alpha", bus=False, remind=[15], expire=60,
                                                                identity=pb.identity))


# ======================= 8：开关关着和以前完全一样 =======================
class DisabledTest(IdentityBase):
    def setUp(self):
        super().setUp()
        self.cfg.identity = IdentityConfig()     # 默认关

    def test_reply_unchanged(self):
        q = self.group_msg("@Matthew 今天能给结论吗？", mentions=[ME, BOT_OID], at=now8() - timedelta(minutes=5))
        self.pull(q)
        r = self.row(q)
        self.assertEqual((r["reason"], r["received_by"]), ("at_me", None))
        self.lark.calls.clear()
        res = outbound.reply(self.cfg, self.store, self.source, "tp_alpha", q, card_text="好")
        self.assertEqual(res.identity, "user")
        self.assertEqual(self.lark.subcommands(), ["+messages-search", "+messages-reply"])
        c = self.lark.sent()[0]
        self.assertEqual(_opt(c, "--as"), "user")
        self.assertIn(MARK, _opt(c, "--content"))
        self.assertEqual(_opt(c, "--idempotency-key"),
                         outbound.idem_key("reply", "tp_alpha", q, outbound.text_fingerprint("好")))
        self.assertIsNone(self.store.conn.execute("SELECT identity FROM sends").fetchone()[0])

    def test_bot_events_ignored_when_disabled(self):
        mid = self.group_msg("故障")
        self.consumer.pending.append(self.lark.event(mid))
        self.assertIsNone(self.collector()._poll_bot_events())
        self.assertIsNone(self.row(mid))

    def test_doctor_line(self):
        self.assertEqual(cli.identity_report(self.cfg, self.store),
                         ["身份区分: 关（[identity] enabled = false）：代回一律用主人本人身份、带代回标记"])


# ======================= 配置、doctor、run =======================
class ConfigDoctorTest(IdentityBase):
    def test_validation(self):
        base = self.cfg_file.read_text(encoding="utf-8").split("[identity]")[0]
        for extra, frag in (('enabled = "yes"', "enabled"), ('bot_open_id = "cli_x"', "bot_open_id"),
                            ('bot_app_id = "ou_x"', "bot_app_id"), ("bot_chats_cache_minutes = 0", "cache"),
                            ("bot_dm_reply_hours = -1", "bot_dm_reply_hours"),
                            ("restart_backoff_seconds = 10\nrestart_backoff_max_seconds = 5", "不能小于")):
            self.cfg_file.write_text(base + "[identity]\n" + extra + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, frag):
                load_config(self.cfg_file)
        self.cfg_file.write_text(base, encoding="utf-8")
        self.assertFalse(load_config(self.cfg_file).identity.enabled)

    def test_example_config_and_playbook(self):
        import tomllib
        from sheepdog.playbook import OPTIONAL_PLAYBOOK_FILES, PLAYBOOK_FILES
        from test_roster import REPO
        data = tomllib.loads((REPO / "examples" / "config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["identity"]["enabled"], False)
        ic = IdentityConfig(**data["identity"])
        ic.validate()
        self.assertIn("bot_dm.md", OPTIONAL_PLAYBOOK_FILES)
        self.assertNotIn("bot_dm.md", PLAYBOOK_FILES)          # 可选：关着时 doctor 不报缺
        self.assertTrue((REPO / "examples" / "playbook" / "bot_dm.md").read_text(encoding="utf-8").strip())
        for name in ("README.md", "README.zh-CN.md", "README.ja.md"):
            self.assertIn("[identity]", (REPO / name).read_text(encoding="utf-8"), name)
        self.assertIn("主人", (REPO / "README.zh-CN.md").read_text(encoding="utf-8"))

    def test_doctor_consumer_state(self):
        self.store.set_meta(identity.META_CONSUMER, json.dumps(
            {"state": "running", "pid": os.getpid(), "owner_pid": os.getpid(), "last_event_at": "2026-10-13T03:06:00+08:00",
             "events": 7, "restarts": 1, "updated_at": "2026-10-13T03:07:00+08:00", "last_exit": "x 退出码 3"}))
        text = "\n".join(cli.identity_report(self.cfg, self.store))
        self.assertIn("✓ 在收", text)
        self.assertIn("2026-10-13T03:06:00+08:00", text)
        self.assertIn("退出码 3", text)
        self.store.set_meta(identity.META_CONSUMER, json.dumps({"state": "running", "pid": 999999, "owner_pid": 999999}))
        self.assertIn("✗ 不在", "\n".join(cli.identity_report(self.cfg, self.store)))

    def test_resolve_bot_ids_looks_up_once_and_falls_back(self):
        self.cfg.identity = IdentityConfig(enabled=True)
        with mock.patch.object(LarkCliSource, "bot_app_id", return_value=APP):
            oid, app = identity.resolve_bot_ids(self.cfg, self.store, self.source)
        self.assertEqual((oid, app), (BOT_OID, APP))
        self.assertEqual(identity.bot_ids(self.cfg, self.store), frozenset({BOT_OID, APP}))
        self.lark.plan["api"] = ["error"] * 3
        with mock.patch.object(LarkCliSource, "bot_app_id", side_effect=RuntimeError("x")):
            self.assertEqual(identity.resolve_bot_ids(self.cfg, self.store, self.source), (BOT_OID, APP))

    def test_run_starts_and_stops_consumer_only_when_enabled(self):
        started = []

        class Spy:
            def __init__(self, *a, **kw):
                started.append(kw)

            def start(self):
                pass

            def stop(self):
                started.append("stop")

        args = mock.Mock(dry_run=False)
        with mock.patch("sheepdog.cli.BotEventConsumer", Spy), \
                mock.patch.object(LarkCliSource, "bot_app_id", return_value=APP):
            self.assertIsNotNone(cli._start_bot_events(self.cfg, self.store, self.source, args))
            self.cfg.identity.enabled = False
            self.assertIsNone(cli._start_bot_events(self.cfg, self.store, self.source, args))
            self.cfg.identity.enabled = True
            self.assertIsNone(cli._start_bot_events(self.cfg, self.store, self.source, mock.Mock(dry_run=True)))
        self.assertEqual(len(started), 1)


# ======================= 和 10-13 热修（92a7d2d）一起：找主人聊天 =======================
class OwnerChatWithIdentityTest(IdentityBase):
    """[identity] 开着时，热修的两条照样成立：reply 目标在找主人聊天里一律拒绝（在选身份之前）；找主人聊天里带代回标记的
    消息——不管是主人身份还是机器人身份发的——都不算主人回复。"""

    def setUp(self):
        super().setUp()
        rc, out, _ = self.ask("--key", "PR #12")
        self.assertEqual(rc, 0, out)
        self.ask_mid = next(k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT)
        self.pull(self.ask_mid)
        self.owner = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="周五发", at=now8() - timedelta(minutes=2))
        self.pull(self.owner)
        self.deliver()
        self.lark.calls.clear()

    def test_reply_in_owner_chat_rejected_before_choosing_identity(self):
        self.assertTrue(self.cfg.identity.enabled)
        before = snapshot(self.cfg.db_path)
        for to in (self.owner, self.ask_mid):
            with self.assertRaises(outbound.OutboundError) as cm:
                outbound.reply(self.cfg, self.store, self.source, "tp_alpha", to, card_text="收到")
            self.assertEqual(cm.exception.code, outbound.EXIT_INVALID)
            self.assertIn("sheepdog report --topic tp_alpha --key", str(cm.exception))
            rc, out, err = self.cli("reply", "--topic", "tp_alpha", "--to", to, "--card-text", "收到",
                                    "--even-if-owner-replied")
            self.assertEqual((rc, out), (outbound.EXIT_INVALID, ""))
        self.assertEqual(self.lark.calls, [])           # 不调飞书：没查机器人的群、没发前检查、没发
        from test_threads import diff_tables
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())

    def test_marked_messages_in_owner_chat_are_not_owner_reply(self):
        by_owner = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"<card>\n主人身份的代回\n{MARK}\n</card>")
        by_bot = self.lark.add(chat_id=ESC_CHAT, sender_id="ou_test_bot", sender_type="app",
                               content=f"<card>\n机器人身份的代回\n{MARK}\n</card>")
        self.pull(by_owner, by_bot)
        ev = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"事件先到的代回\n{MARK}")
        self.bot_hears(self.lark.event(ev))             # 机器人身份先收到也一样
        for mid in (by_owner, by_bot, ev):
            r = self.row(mid)
            self.assertNotEqual(r["reason"], "owner_reply", mid)
            self.assertNotEqual(r["route"], DISPATCH, mid)
        self.assertEqual(self.row(by_owner)["reason"], "agent_reply_in_owner_chat")
        self.assertEqual(self.row(ev)["reason"], "agent_reply_in_owner_chat")
        self.assertEqual(self.store.pending_dispatch(), [])
