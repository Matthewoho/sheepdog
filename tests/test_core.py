"""路由、状态机、Collector/Dispatcher 的单测。全部使用合成数据（ou_test_* / oc_test_*）。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sheepdog import session as sm
from sheepdog.config import Config, RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, write_receipt
from sheepdog.models import Mention, Message
from sheepdog.router import DISPATCH, DROP, INBOX, SELF, RouteContext, route
from sheepdog.source.lark import parse_message
from sheepdog.store import Store

ME = "ou_test_me"


def msg(**kw) -> Message:
    base = dict(message_id="om_test_1", chat_id="oc_test_g1", chat_name="测试群", chat_type="group",
                sender_id="ou_test_alice", sender_name="Alice", sender_type="user", content="hello",
                msg_type="text", create_time="2026-01-01T10:00:00+08:00")
    base.update(kw)
    return Message(**base)


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.cfg = RoutingConfig(keywords=["故障"], vip_sender_ids=["ou_test_boss"])
        self.ctx = RouteContext(ME, muted_chat_ids={"oc_test_muted"}, is_my_message=lambda mid: mid == "om_test_mine")

    def r(self, m):
        return route(m, self.ctx, self.cfg)

    def test_self(self):
        self.assertEqual(self.r(msg(sender_id=ME)).route, SELF)

    def test_p2p_human(self):
        self.assertEqual(self.r(msg(chat_type="p2p")).reason, "p2p")

    def test_p2p_bot_to_inbox_unless_keyword(self):
        self.assertEqual(self.r(msg(chat_type="p2p", sender_type="app")).route, INBOX)
        self.assertEqual(self.r(msg(chat_type="p2p", sender_type="app", content="生产故障")).route, DISPATCH)

    def test_at_me_and_at_all(self):
        self.assertEqual(self.r(msg(mentions=[Mention(ME)])).reason, "at_me")
        self.assertEqual(self.r(msg(mentions=[Mention("all", is_all=True)])).reason, "at_all")

    def test_muted_dropped_but_at_me_dispatched(self):
        self.assertEqual(self.r(msg(chat_id="oc_test_muted")).route, DROP)
        self.assertEqual(self.r(msg(chat_id="oc_test_muted", content="故障")).route, DROP)
        self.assertEqual(self.r(msg(chat_id="oc_test_muted", mentions=[Mention(ME)])).route, DISPATCH)

    def test_reply_vip_keyword_default(self):
        self.assertEqual(self.r(msg(reply_to="om_test_mine")).reason, "reply_to_me")
        self.assertEqual(self.r(msg(sender_id="ou_test_boss")).reason, "vip_sender")
        self.assertEqual(self.r(msg(content="出故障了")).reason, "keyword")
        self.assertEqual(self.r(msg()).route, INBOX)


class StateMachineTest(unittest.TestCase):
    def test_happy_path(self):
        s = sm.transition(sm.ACTIVE, "dispatch")
        self.assertEqual(s, sm.RUNNING)
        self.assertEqual(sm.transition(s, "receipt_needs_decision"), sm.WAITING_HUMAN)

    def test_invalid(self):
        with self.assertRaises(sm.InvalidTransition):
            sm.transition(sm.CLOSED, "dispatch")

    def test_human_attach(self):
        self.assertEqual(sm.transition(sm.ACTIVE, "human_attach"), sm.HUMAN_ATTACHED)
        self.assertEqual(sm.transition(sm.HUMAN_ATTACHED, "human_detach"), sm.ACTIVE)


class ParseTest(unittest.TestCase):
    def test_parse_lark_search_item(self):
        raw = {"chat_id": "oc_test_g1", "chat_name": "测试群", "chat_type": "group", "content": "hi",
               "create_time": "2026-01-01 10:00", "message_id": "om_test_9", "msg_type": "text",
               "mentions": [{"id": ME, "key": "@_user_1", "name": "Me"}, {"id": "all", "key": "@_all", "name": "所有人"}],
               "reply_to": "om_test_8", "sender": {"id": "ou_test_alice", "name": "Alice", "sender_type": "user"}}
        m = parse_message(raw)
        self.assertEqual(m.create_time, "2026-01-01T10:00:00+08:00")
        self.assertTrue(m.mentions[1].is_all)
        self.assertEqual(m.reply_to, "om_test_8")


class FakeSource:
    def __init__(self, batches):
        self.batches = list(batches)

    def fetch_since(self, start_iso, end_iso=None):
        return self.batches.pop(0) if self.batches else []

    def muted_chat_ids(self):
        return set()

    def read_status(self, ids):
        return {i: i.endswith("read") for i in ids}

    def senders_of(self, ids):
        return {}


class FakeSink:
    name = "fake"

    def __init__(self):
        self.sent = []
        self.created = []
        self.human = None

    def available(self):
        return True, "ok"

    def new_conversation(self, title, prompt, model=""):
        self.created.append(title)
        return "conv_test_1"

    def send_message(self, cid, content):
        self.sent.append((cid, content))

    def last_human_activity(self, cid):
        return self.human


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(self_open_id=ME, state_dir=Path(self.tmp.name))
        self.cfg.routing = RoutingConfig(keywords=["故障"])
        self.cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        self.cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.cfg.db_path)
        self.sink = FakeSink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end(self):
        batch1 = [
            msg(message_id="om_test_p2p_read", chat_type="p2p", chat_id="oc_test_p1"),
            msg(message_id="om_test_g_inbox"),
            msg(message_id="om_test_g_at", mentions=[Mention(ME)]),
        ]
        col = Collector(self.cfg, self.store, FakeSource([batch1]))
        stats = col.poll_once()
        self.assertEqual(stats["dispatch"], 2)
        self.assertEqual(stats["inbox"], 1)

        disp = Dispatcher(self.cfg, self.store, self.sink)
        res = disp.dispatch_once()
        self.assertEqual(res["sent"], 2)
        self.assertEqual(self.sink.created, ["[managed] Lark 信号·总线"])
        content = self.sink.sent[0][1]
        self.assertIn("✓已读", content)  # 已读仍然投递，只打标签
        self.assertIn("Inbox", content)  # @我 时附带 Inbox 摘要
        t = self.store.get_topic(BUS_TOPIC_ID)
        self.assertEqual(t["state"], sm.RUNNING)

        # 回执驱动状态
        write_receipt(self.cfg, BUS_TOPIC_ID, t["pending_batch_id"], {"status": "needs_decision", "summary": "等决策"})
        disp.dispatch_once()
        t = self.store.get_topic(BUS_TOPIC_ID)
        self.assertEqual(t["state"], sm.WAITING_HUMAN)
        self.assertEqual(t["summary"], "等决策")

    def test_edit_upgrades_inbox_to_dispatch(self):
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_e1")],
            [msg(message_id="om_test_e1", content="现在 @我", mentions=[Mention(ME)], updated=True)],
        ]))
        col.poll_once()
        self.assertEqual(self.store.get_message("om_test_e1")["route"], INBOX)
        col.poll_once()
        row = self.store.get_message("om_test_e1")
        self.assertEqual((row["route"], row["dispatch_state"]), (DISPATCH, "pending"))

    def test_recall_after_delivery_notifies(self):
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_r1", chat_type="p2p", content="原话")],
            [msg(message_id="om_test_r1", chat_type="p2p", content="", deleted=True)],
        ]))
        col.poll_once()
        Dispatcher(self.cfg, self.store, self.sink).dispatch_once()
        col.poll_once()
        row = self.store.get_message("om_test_r1")
        self.assertEqual(row["reason"], "recalled")
        self.assertIn("原话", row["content"])

    def test_human_attach_pauses(self):
        col = Collector(self.cfg, self.store, FakeSource([[msg(message_id="om_test_h1", chat_type="p2p")]]))
        disp = Dispatcher(self.cfg, self.store, self.sink)
        disp.ensure_bus_topic()
        self.store.update_topic(BUS_TOPIC_ID, conversation_id="conv_test_1")
        self.sink.human = datetime.now().astimezone() - timedelta(minutes=1)
        col.poll_once()
        res = disp.dispatch_once()
        self.assertEqual(res["sent"], 0)
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["state"], sm.HUMAN_ATTACHED)

    def test_invalid_receipt(self):
        with self.assertRaises(ValueError):
            write_receipt(self.cfg, BUS_TOPIC_ID, "b1", {"status": "whatever"})


if __name__ == "__main__":
    unittest.main()
