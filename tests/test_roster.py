"""P0.5 名册、按聊天归属路由、onboarding、转交、代回前缀的单测。全部使用合成数据（*_test_*）。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sheepdog import session as sm
from sheepdog.config import Config, RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, forward_messages, write_receipt
from sheepdog.models import Mention, Message
from sheepdog.playbook import PLAYBOOK_FILES, Playbook
from sheepdog.prompts import batch_prompt, bootstrap_prompt, onboarding_prompt
from sheepdog.roster import Roster, RosterError, load_roster, parse_roster
from sheepdog.router import DISPATCH, DROP, INBOX, RouteContext, route
from sheepdog.store import Store

ME = "ou_test_me"
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "playbook"
PREFIX = "🐕 [Agent 代回] "
DROP_PREFIX = "🐕 [sheepdog·"


def test_config(state_dir: Path) -> Config:
    """测试配置：playbook 指向 tests/fixtures/playbook（只含标记和占位符，不含业务文字）。"""
    cfg = Config(self_open_id=ME, state_dir=state_dir)
    cfg.playbook_dir = str(FIXTURES)
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def msg(**kw) -> Message:
    base = dict(message_id="om_test_1", chat_id="oc_test_g1", chat_name="测试群", chat_type="group",
                sender_id="ou_test_alice", sender_name="Alice", sender_type="user", content="hello",
                msg_type="text", create_time="2026-01-01T10:00:00+08:00")
    base.update(kw)
    return Message(**base)


def roster_data(**over) -> dict:
    """一个 managed（全量私聊 + 只推 dispatch 级的群）+ 一个 known。"""
    data = {"session": [
        {"key": "alpha", "mode": "managed", "conversation_id": "conv_test_alpha", "title": "Alpha 需求",
         "duty": "跟进 Alpha 的需求", "authority": "可以直接回复 Alpha 的确认类问题",
         "self_polling": "每 10 分钟巡检", "retire_self_polling": True,
         "chats": [{"chat_id": "oc_test_alpha_p2p", "name": "Alpha 私聊", "all_messages": True},
                   {"chat_id": "oc_test_alpha_grp", "name": "Alpha 群", "all_messages": False}]},
        {"key": "ops", "mode": "known", "conversation_id": "conv_test_ops", "title": "值守", "duty": "发布值守"},
    ]}
    data.update(over)
    return data


class FakeSource:
    def __init__(self, batches, muted=()):
        self.batches = list(batches)
        self.muted = set(muted)

    def fetch_since(self, start_iso, end_iso=None):
        return self.batches.pop(0) if self.batches else []

    def muted_chat_ids(self):
        return set(self.muted)

    def read_status(self, ids):
        return {i: False for i in ids}

    def senders_of(self, ids):
        return {}


class FakeSink:
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.created: list[tuple[str, str]] = []
        self.human: dict[str, datetime] = {}
        # 调用顺序：("new", title) / ("send", cid)
        self.log: list[tuple[str, str]] = []

    def available(self):
        return True, "ok"

    def new_conversation(self, title, prompt, model=""):
        self.created.append((title, prompt))
        self.log.append(("new", title))
        return "conv_test_bus" if "总线" in title else f"conv_test_new{len(self.created)}"

    def send_message(self, cid, content):
        self.sent.append((cid, content))
        self.log.append(("send", cid))

    def last_human_activity(self, cid):
        return self.human.get(cid)

    def to(self, cid):
        return [c for k, c in self.sent if k == cid]


class RosterValidationTest(unittest.TestCase):
    def test_valid_and_owner(self):
        r = parse_roster(roster_data())
        s, c = r.owner_of("oc_test_alpha_p2p")
        self.assertEqual((s.topic_id, c.all_messages), ("tp_alpha", True))
        self.assertIsNone(r.owner_of("oc_test_other"))
        self.assertEqual([x.key for x in r.managed], ["alpha"])

    def test_duplicate_chat_rejected(self):
        d = roster_data()
        d["session"].append({"key": "beta", "mode": "managed", "conversation_id": "conv_test_beta",
                             "chats": [{"chat_id": "oc_test_alpha_grp"}]})
        with self.assertRaisesRegex(RosterError, "只能属于一个"):
            parse_roster(d)

    def test_managed_requires_conversation_id(self):
        d = roster_data()
        del d["session"][0]["conversation_id"]
        with self.assertRaisesRegex(RosterError, "conversation_id"):
            parse_roster(d)

    def test_known_cannot_have_chats(self):
        d = roster_data()
        d["session"][1]["chats"] = [{"chat_id": "oc_test_x"}]
        with self.assertRaisesRegex(RosterError, "known"):
            parse_roster(d)

    def test_key_rules_and_unknown_field(self):
        for bad in ({"key": "Alpha"}, {"key": "bus"}, {"key": "ops"}, {"chat": []}):
            d = roster_data()
            d["session"][0].update(bad)
            with self.assertRaises(RosterError, msg=bad):
                parse_roster(d)

    def test_load_missing_file(self):
        missing = Path(tempfile.gettempdir()) / "sheepdog_test_no_such_roster.toml"
        self.assertEqual(load_roster(missing, required=False).sessions, [])
        with self.assertRaises(RosterError):
            load_roster(missing, required=True)

    def test_example_file_is_valid(self):
        r = load_roster(REPO / "examples" / "roster.example.toml", required=True)
        self.assertEqual(len(r.managed), 2)
        self.assertEqual(len(r.sessions), 3)
        self.assertEqual([x.key for x in r.sessions if x.to_spawn], ["demo_handover"])

    def test_hash_ignores_formatting_but_tracks_content(self):
        a, b = parse_roster(roster_data()), parse_roster(roster_data())
        self.assertEqual(a.content_hash(), b.content_hash())
        d = roster_data()
        d["session"][0]["duty"] = "改了职责"
        self.assertNotEqual(a.content_hash(), parse_roster(d).content_hash())


class BotKeywordTest(unittest.TestCase):
    def setUp(self):
        self.ctx = RouteContext(ME)

    def test_group_bot_keyword_skipped(self):
        cfg = RoutingConfig(keywords=["告警"])
        self.assertEqual(route(msg(sender_type="app", content="告警：CPU 高"), self.ctx, cfg).route, INBOX)
        self.assertEqual(route(msg(sender_type="user", content="告警：CPU 高"), self.ctx, cfg).route, DISPATCH)
        # 机器人私聊的关键词规则不变
        self.assertEqual(route(msg(chat_type="p2p", sender_type="bot", content="告警"), self.ctx, cfg).reason,
                         "bot_p2p_keyword")

    def test_switch_off(self):
        cfg = RoutingConfig(keywords=["告警"], keyword_skip_bot_senders=False)
        self.assertEqual(route(msg(sender_type="app", content="告警"), self.ctx, cfg).route, DISPATCH)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_config(Path(self.tmp.name))
        self.cfg.routing = RoutingConfig(keywords=["故障"], ignore_chat_ids=["oc_test_ignored"],
                                         drop_bot_message_prefixes=[DROP_PREFIX])
        self.store = Store(self.cfg.db_path)
        self.sink = FakeSink()
        self.roster = parse_roster(roster_data())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster).poll_once()

    def disp(self, roster=None) -> Dispatcher:
        return Dispatcher(self.cfg, self.store, self.sink, roster if roster is not None else self.roster)

    def row(self, mid):
        return self.store.get_message(mid)

    def ack(self, topic_id, status="handled"):
        t = self.store.get_topic(topic_id)
        write_receipt(self.cfg, topic_id, t["pending_batch_id"], {"status": status, "summary": "ok"})


class OwnershipRoutingTest(Base):
    def test_all_messages_overrides_muted_and_inbox(self):
        self.poll([
            msg(message_id="om_test_m1", chat_id="oc_test_alpha_p2p", content="普通消息"),
            msg(message_id="om_test_m2", chat_id="oc_test_alpha_p2p", sender_type="app"),
            msg(message_id="om_test_m3", chat_id="oc_test_muted"),  # 不在名册的免打扰群照旧丢弃
        ], muted={"oc_test_alpha_p2p", "oc_test_muted"})
        r = self.row("om_test_m1")
        self.assertEqual((r["route"], r["topic_id"], r["reason"]), (DISPATCH, "tp_alpha", "muted_chat"))
        self.assertIn("owner:alpha", json.loads(r["tags_json"]))
        self.assertEqual(self.row("om_test_m2")["topic_id"], "tp_alpha")
        self.assertEqual(self.row("om_test_m3")["route"], DROP)

    def test_ignore_and_self_still_win(self):
        d = roster_data()
        d["session"][0]["chats"].append({"chat_id": "oc_test_ignored", "all_messages": True})
        self.roster = parse_roster(d)
        self.poll([msg(message_id="om_test_i1", chat_id="oc_test_ignored"),
                   msg(message_id="om_test_s1", chat_id="oc_test_alpha_p2p", sender_id=ME)])
        self.assertEqual(self.row("om_test_i1")["route"], DROP)
        self.assertEqual(self.row("om_test_s1")["route"], "self")
        self.assertIsNone(self.row("om_test_s1")["topic_id"])

    def test_all_messages_false_only_dispatch_level(self):
        self.poll([msg(message_id="om_test_g1", chat_id="oc_test_alpha_grp"),
                   msg(message_id="om_test_g2", chat_id="oc_test_alpha_grp", mentions=[Mention(ME)])])
        self.assertEqual((self.row("om_test_g1")["route"], self.row("om_test_g1")["topic_id"]), (INBOX, None))
        self.assertEqual((self.row("om_test_g2")["route"], self.row("om_test_g2")["topic_id"]), (DISPATCH, "tp_alpha"))
        self.assertEqual(self.row("om_test_g2")["reason"], "at_me")

    def test_unowned_dispatch_goes_to_bus(self):
        self.poll([msg(message_id="om_test_b1", chat_type="p2p", chat_id="oc_test_stranger"),
                   msg(message_id="om_test_b2", chat_id="oc_test_other_grp")])
        self.assertEqual(self.row("om_test_b1")["topic_id"], BUS_TOPIC_ID)
        self.assertEqual((self.row("om_test_b2")["route"], self.row("om_test_b2")["topic_id"]), (INBOX, None))

    def test_edit_upgrade_gets_owner(self):
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_e1", chat_id="oc_test_alpha_grp")],
            [msg(message_id="om_test_e1", chat_id="oc_test_alpha_grp", content="@我", mentions=[Mention(ME)], updated=True)],
        ]), self.roster)
        col.poll_once()
        col.poll_once()
        self.assertEqual((self.row("om_test_e1")["route"], self.row("om_test_e1")["topic_id"]), (DISPATCH, "tp_alpha"))


class DispatchTest(Base):
    def test_sync_and_per_topic_delivery_with_onboarding_once(self):
        self.poll([msg(message_id="om_test_a1", chat_id="oc_test_alpha_p2p"),
                   msg(message_id="om_test_x1", chat_type="p2p", chat_id="oc_test_stranger")])
        d = self.disp()
        res = d.dispatch_once()
        t = self.store.get_topic("tp_alpha")
        self.assertEqual((t["kind"], t["conversation_id"]), ("adopted", "conv_test_alpha"))
        self.assertEqual(self.store.get_topic("tp_ops")["kind"], "known")
        # 总线照常新建并投递；adopted 第一轮只发 onboarding，消息等下一轮
        self.assertEqual(len(self.sink.created), 1)
        self.assertEqual(res["topics"][BUS_TOPIC_ID]["sent"], 1)
        self.assertIn("onboarding", res["topics"]["tp_alpha"])
        alpha = self.sink.to("conv_test_alpha")
        self.assertEqual(len(alpha), 1)
        self.assertIn("登记通知", alpha[0])
        self.assertIn("FX-ONBOARDING title=Alpha 需求 duty=跟进 Alpha 的需求", alpha[0])
        self.assertIn("auth=可以直接回复 Alpha 的确认类问题", alpha[0])
        self.assertIn("FX-RETIRE-POLLING polling=每 10 分钟巡检", alpha[0])
        self.assertIn("FX-COMMON title=Alpha 需求 topic=tp_alpha", alpha[0])
        self.assertIn("sheepdog receipt --topic tp_alpha --batch o", alpha[0])
        self.assertEqual(self.row("om_test_a1")["dispatch_state"], "pending")

        self.ack("tp_alpha")
        d.dispatch_once()
        alpha = self.sink.to("conv_test_alpha")
        self.assertEqual(len(alpha), 2)
        self.assertIn("om_test_a1", alpha[1])
        self.assertNotIn("Inbox", alpha[1])  # Inbox 摘要只给总线
        self.assertEqual(self.row("om_test_a1")["dispatch_state"], "delivered")

        # 再次 init / 再来消息都不会重发 onboarding
        self.ack("tp_alpha")
        rep = d.init()
        self.assertIn("跳过", rep["onboarding"]["tp_alpha"])
        self.poll([msg(message_id="om_test_a2", chat_id="oc_test_alpha_p2p")])
        d.dispatch_once()
        self.assertEqual(sum("登记通知" in c for c in self.sink.to("conv_test_alpha")), 1)
        self.assertNotEqual(self.store.get_topic("tp_alpha")["pending_batch_id"],
                            self.store.get_topic(BUS_TOPIC_ID)["pending_batch_id"])

    def test_init_creates_bus_and_onboards_never_new_conversation_for_adopted(self):
        rep = self.disp().init()
        self.assertEqual(rep["roster"]["created"], ["tp_alpha", "tp_ops"])
        self.assertTrue(rep["bus"].startswith("已新建"))
        self.assertEqual([t for t, _ in self.sink.created], ["[managed] Lark 信号·总线"])
        self.assertIn("已发送", rep["onboarding"]["tp_alpha"])
        self.assertNotIn("tp_ops", rep["onboarding"])
        self.assertIsNotNone(self.store.get_topic("tp_alpha")["onboarded_at"])
        # 总线 bootstrap 带名册与转交规则
        bootstrap = self.sink.created[0][1]
        self.assertIn("FX-BUS topic=tp_bus", bootstrap)
        self.assertIn("FX-COMMON title=Lark 信号·总线 topic=tp_bus", bootstrap)
        # {{roster}} 被替换成代码渲染的名册表
        self.assertIn("Alpha 私聊（全部消息）", bootstrap)
        self.assertIn("conv_test_ops", bootstrap)
        self.assertNotIn("{{roster}}", bootstrap)
        # 接口说明由代码生成
        self.assertIn("sheepdog forward --topic", bootstrap)
        self.assertIn("sheepdog spawn --key", bootstrap)

    def test_adopted_timeout_counts_missed_without_requeue(self):
        d = self.disp()
        d.init()
        self.ack("tp_alpha")
        self.poll([msg(message_id="om_test_t1", chat_id="oc_test_alpha_p2p")])
        d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), 2)
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic("tp_alpha", dispatched_at=old)
        d.dispatch_once()
        t = self.store.get_topic("tp_alpha")
        self.assertEqual((t["state"], t["receipt_missed"], t["pending_batch_id"]), (sm.ACTIVE, 1, None))
        self.assertEqual(self.row("om_test_t1")["dispatch_state"], "missed")
        d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), 2)  # 没有重投

    def test_bus_timeout_still_requeues(self):
        self.poll([msg(message_id="om_test_bt", chat_type="p2p", chat_id="oc_test_stranger")])
        d = self.disp(Roster())
        d.dispatch_once()
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic(BUS_TOPIC_ID, dispatched_at=old)
        d.dispatch_once()
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["retries"], 1)
        self.assertEqual(len(self.sink.to("conv_test_bus")), 2)  # 超时后重投

    def test_adopted_human_attached_queues(self):
        d = self.disp()
        d.init()
        self.ack("tp_alpha")
        d.dispatch_once()
        self.sink.human["conv_test_alpha"] = datetime.now().astimezone() - timedelta(minutes=1)
        self.poll([msg(message_id="om_test_h1", chat_id="oc_test_alpha_p2p")])
        res = d.dispatch_once()
        self.assertEqual(res["topics"]["tp_alpha"]["sent"], 0)
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.HUMAN_ATTACHED)
        self.sink.human["conv_test_alpha"] = datetime.now().astimezone() - timedelta(minutes=30)
        res = d.dispatch_once()
        self.assertEqual(res["topics"]["tp_alpha"]["sent"], 1)

    def test_adopted_done_receipt_does_not_close(self):
        d = self.disp()
        d.init()
        self.ack("tp_alpha", "done")
        d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)

    def test_bus_done_receipt_does_not_close(self):
        self.poll([msg(message_id="om_test_bd1", chat_type="p2p", chat_id="oc_test_stranger")])
        d = self.disp(Roster())
        d.dispatch_once()
        self.ack(BUS_TOPIC_ID, "done")
        d.dispatch_once()
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["state"], sm.ACTIVE)
        # 之后的信号照常投递
        self.poll([msg(message_id="om_test_bd2", chat_type="p2p", chat_id="oc_test_stranger")])
        self.assertEqual(d.dispatch_once()["topics"][BUS_TOPIC_ID]["sent"], 1)

    def test_watch_cli_message_uses_config(self):
        import io, os
        from contextlib import redirect_stdout
        from unittest import mock
        from sheepdog import cli
        self.disp().init()
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text('[watch]\nremind_minutes = [5]\nexpire_minutes = 10\n', encoding="utf-8")
        out = io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(cli.main(["watch", "--topic", "tp_alpha", "--person", "ou_test_bob"]), 0)
        self.assertIn("5 分钟提醒，10 分钟到期", out.getvalue())

    def test_removed_from_roster_closes_and_falls_back_to_bus(self):
        self.poll([msg(message_id="om_test_r1", chat_id="oc_test_alpha_p2p")])
        d = self.disp()
        d.sync_roster()
        d.roster = Roster()  # 名册里删掉全部条目
        res = d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.CLOSED)
        self.assertEqual(self.store.get_topic("tp_ops")["state"], sm.CLOSED)
        self.assertIsNotNone(self.store.get_topic("tp_alpha"))  # 不删数据
        self.assertEqual(res["topics"][BUS_TOPIC_ID]["sent"], 1)
        self.assertEqual(self.row("om_test_r1")["topic_id"], BUS_TOPIC_ID)
        # 加回来就重新启用
        d.roster = self.roster
        d.sync_roster()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)

    def test_roster_change_notifies_bus_once(self):
        d = self.disp()
        d.init()  # 新建总线时 bootstrap 已含名册，不再重复通知
        self.poll([msg(message_id="om_test_n1", chat_type="p2p", chat_id="oc_test_stranger")])
        d.dispatch_once()
        self.assertNotIn("名册更新", self.sink.to("conv_test_bus")[-1])
        self.ack(BUS_TOPIC_ID)
        dd = roster_data()
        dd["session"][1]["duty"] = "发布值守（新）"
        d.roster = parse_roster(dd)
        self.poll([msg(message_id="om_test_n2", chat_type="p2p", chat_id="oc_test_stranger")])
        d.dispatch_once()
        self.assertIn("名册更新", self.sink.to("conv_test_bus")[-1])
        self.assertIn("发布值守（新）", self.sink.to("conv_test_bus")[-1])
        self.ack(BUS_TOPIC_ID)
        self.poll([msg(message_id="om_test_n3", chat_type="p2p", chat_id="oc_test_stranger")])
        d.dispatch_once()
        self.assertNotIn("名册更新", self.sink.to("conv_test_bus")[-1])

    def test_conversation_change_resets_onboarding(self):
        d = self.disp()
        d.init()
        dd = roster_data()
        dd["session"][0]["conversation_id"] = "conv_test_alpha2"
        d.roster = parse_roster(dd)
        d.sync_roster()
        t = self.store.get_topic("tp_alpha")
        self.assertEqual((t["conversation_id"], t["onboarded_at"], t["state"]), ("conv_test_alpha2", None, sm.ACTIVE))


class ForwardTest(Base):
    def test_forward_rejects_known_bus_and_unknown(self):
        self.poll([msg(message_id="om_test_f1", chat_type="p2p", chat_id="oc_test_stranger")])
        self.disp().sync_roster()
        with self.assertRaisesRegex(ValueError, "known"):
            forward_messages(self.store, "tp_ops", ["om_test_f1"])
        with self.assertRaisesRegex(ValueError, "总线"):
            forward_messages(self.store, BUS_TOPIC_ID, ["om_test_f1"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            forward_messages(self.store, "tp_nobody", ["om_test_f1"])
        with self.assertRaisesRegex(ValueError, "没有这些消息"):
            forward_messages(self.store, "tp_alpha", ["om_test_missing"])

    def test_forward_delivers_to_managed_with_note(self):
        d = self.disp()
        d.init()
        self.ack("tp_alpha")
        self.poll([msg(message_id="om_test_f2", chat_type="p2p", chat_id="oc_test_stranger")])
        d.dispatch_once()  # 先到总线
        self.assertEqual(self.row("om_test_f2")["topic_id"], BUS_TOPIC_ID)
        forward_messages(self.store, "tp_alpha", ["om_test_f2"], "这是 Alpha 的需求")
        r = self.row("om_test_f2")
        self.assertEqual((r["topic_id"], r["dispatch_state"]), ("tp_alpha", "pending"))
        self.assertIn("forwarded_from:tp_bus", json.loads(r["tags_json"]))
        d.dispatch_once()
        last = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("om_test_f2", last)
        self.assertIn("总线备注（不是主人原话）：这是 Alpha 的需求", last)


class PlaybookTest(unittest.TestCase):
    """7.6：模板内容被拼进 prompt、占位符被替换、未知占位符原样保留、缺文件为空且 doctor 报缺、改完现读。"""

    def setUp(self):
        self.s = parse_roster(roster_data()).by_topic("tp_alpha")
        self.pb = Playbook(FIXTURES, {"reply_prefix": PREFIX, "watch_remind_minutes": "15/30",
                                      "watch_expire_minutes": "60"})

    def test_render_substitutes_and_keeps_unknown(self):
        text = self.pb.render("common.md", session_title="T", topic_id="tp_t")
        self.assertEqual(text, f"FX-COMMON title=T topic=tp_t prefix={PREFIX} remind=15/30 expire=60 keep={{{{not_a_var}}}}")

    def test_values_are_not_re_substituted(self):
        text = self.pb.render("authority_default.md")
        self.assertEqual(text, "FX-AUTH-DEFAULT")
        s = self.s
        s.authority = "原话里有 {{topic_id}}"
        out = onboarding_prompt(self.pb, s, "o1", [15, 30], 60)
        self.assertIn("auth=原话里有 {{topic_id}}", out)

    def test_assembly_order_and_defaults(self):
        self.s.authority = ""
        self.s.retire_self_polling = False
        out = onboarding_prompt(self.pb, self.s, "o1", [15, 30], 60)
        self.assertIn("auth=FX-AUTH-DEFAULT", out)
        self.assertNotIn("FX-RETIRE-POLLING", out)
        self.assertLess(out.index("FX-ONBOARDING"), out.index("FX-COMMON"))
        self.assertLess(out.index("FX-COMMON"), out.index("## sheepdog 接口说明"))
        boot = bootstrap_prompt(self.pb, "[managed] 总线", "总线", BUS_TOPIC_ID, parse_roster(roster_data()),
                                "OVERLAY-MARK", [15, 30], 60)
        # overlay 照旧拼在 common.md 之后
        self.assertLess(boot.index("FX-BUS"), boot.index("FX-COMMON"))
        self.assertLess(boot.index("FX-COMMON"), boot.index("OVERLAY-MARK"))
        self.assertIn("15/30 分钟没回时提示你；60 分钟到期", boot)
        tail = batch_prompt(self.pb, "tp_alpha", "b1", [], [], "Alpha 需求").splitlines()[-1]
        self.assertEqual(tail, f"FX-FOOTER title=Alpha 需求 prefix={PREFIX}".rstrip())

    def test_missing_files_render_empty(self):
        with tempfile.TemporaryDirectory() as d:
            pb = Playbook(Path(d) / "nope")
            self.assertEqual(pb.missing(), list(PLAYBOOK_FILES))
            out = onboarding_prompt(pb, self.s, "o1", [15, 30], 60)
            self.assertNotIn("FX-", out)
            self.assertIn("## sheepdog 接口说明", out)  # 机制部分照常
            self.assertTrue(batch_prompt(pb, "tp_alpha", "b1", [], [], "A").splitlines()[-1].startswith("处理完成后提交回执"))

    def test_hot_reload(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "batch_footer.md").write_text("V1", encoding="utf-8")
            pb = Playbook(Path(d))
            self.assertEqual(pb.render("batch_footer.md"), "V1")
            (Path(d) / "batch_footer.md").write_text("V2 {{topic_id}}", encoding="utf-8")
            self.assertEqual(pb.render("batch_footer.md", topic_id="tp_x"), "V2 tp_x")

    def test_doctor_reports_missing(self):
        import io, os
        from contextlib import redirect_stdout
        from unittest import mock
        from sheepdog import cli
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "playbook"
            pdir.mkdir()
            for name in PLAYBOOK_FILES:
                if name != "retire.md":
                    (pdir / name).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
            (Path(d) / "config.toml").write_text('self_open_id = "ou_test_me"\n', encoding="utf-8")
            out = io.StringIO()
            env = {"SHEEPDOG_CONFIG": str(Path(d) / "config.toml"), "SHEEPDOG_STATE_DIR": str(Path(d) / "state")}
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                cli.main(["doctor"])
            text = out.getvalue()
            self.assertIn("缺 1 个", text)
            self.assertIn("✗ 缺 retire.md", text)
            self.assertNotIn("缺 common.md", text)

    def test_dispatcher_uses_config_values(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = test_config(Path(d))
            cfg.session.reply_prefix = "[P] "
            cfg.watch.remind_minutes, cfg.watch.expire_minutes = [5], 10
            sink = FakeSink()
            store = Store(cfg.db_path)
            Dispatcher(cfg, store, sink, parse_roster(roster_data())).init()
            self.assertIn("prefix=[P]  remind=5 expire=10", sink.created[0][1])
            self.assertIn("prefix=[P]  remind=5 expire=10", sink.to("conv_test_alpha")[0])
            store.close()

    def test_watch_config_validation(self):
        from sheepdog.config import ConfigError, WatchConfig
        for bad in (WatchConfig([15, 30, 45], 60), WatchConfig([30, 15], 60), WatchConfig([15, 60], 60)):
            with self.assertRaises(ConfigError):
                bad.validate()
        WatchConfig([5], 10).validate()

    def test_drop_prefix_off_by_default(self):
        d = route(msg(chat_type="p2p", sender_type="app", content=DROP_PREFIX + "x] 需要你定"), RouteContext(ME),
                  RoutingConfig())
        self.assertNotEqual(d.reason, "self_escalation")


class StoreTest(unittest.TestCase):
    def test_snapshot_does_not_write_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sqlite3"
            Store(path).set_meta("k", "real")
            snap = Store.snapshot(path)
            self.assertEqual(snap.get_meta("k"), "real")
            snap.set_meta("k", "dry")
            self.assertEqual(Store(path).get_meta("k"), "real")
            Store.snapshot(Path(d) / "none.sqlite3")
            self.assertFalse((Path(d) / "none.sqlite3").exists())

    def test_migrates_old_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite3"
            c = sqlite3.connect(path)
            c.execute("""CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT, duty TEXT,
                         conversation_id TEXT, state TEXT NOT NULL, anchors_json TEXT DEFAULT '[]', summary TEXT DEFAULT '',
                         pending_batch_id TEXT, dispatched_at TEXT, retries INTEGER DEFAULT 0,
                         created_at TEXT NOT NULL, last_active TEXT NOT NULL)""")
            c.execute("INSERT INTO topics VALUES('tp_bus','t','bus','d','conv_test_1','active','[]','',NULL,NULL,0,'x','x')")
            c.commit()
            c.close()
            t = Store(path).get_topic("tp_bus")
            self.assertEqual((t["conversation_id"], t["onboarded_at"], t["receipt_missed"]), ("conv_test_1", None, 0))


if __name__ == "__main__":
    unittest.main()
