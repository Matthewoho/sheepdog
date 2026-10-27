"""REG-049 ~ REG-052 回归测试：群引用回复直达、CLI 写后即时唤醒投递、Subagent lamb_id 防克隆与结案清理（7.86）。"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
import unittest

from sheepdog import duty, engine, guard
from sheepdog.config import Config
from sheepdog.engine import Collector, Dispatcher
from sheepdog.models import Mention, Message
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
OPS_CHAT = "oc_ops_group"


class FakeSink786:
    """轻量假宿主 Sink：记录投递出的批次消息。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, conversation_id: str, text: str, **kwargs) -> None:
        self.sent.append({"conversation_id": conversation_id, "text": text, **kwargs})


class FakeSource786:
    """轻量飞书假消息源。"""

    def mget_messages(self, message_ids: list[str]) -> dict[str, Message]:
        return {}

    def muted_chat_ids(self) -> set[str]:
        return set()


class TestBusQuoteWakeupLambAndClosed786(unittest.TestCase):
    """REG-049 ~ REG-052：群引用归属、写后唤醒与 deliver_only 收回执、Subagent lamb_id 复用与结案清理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        cfg_path = self.tmp_path / "config.toml"
        cfg_path.write_text(f'self_open_id = "{OWNER_ID}"\n', encoding="utf-8")
        self.cfg = Config(state_dir=self.tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.store.create_topic("tp_bus", "总线", "bus", "总线职责", "active")
        self.store.create_topic("tp_jev-decision-service", "JEV 决策服务", "adopted", "JEV 职责", "active")
        self.store.create_topic("tp_sammy-shi", "Sammy 专席", "adopted", "Sammy 职责", "active")
        for tid, cid in (
            ("tp_bus", "cid-bus"),
            ("tp_jev-decision-service", "cid-jev"),
            ("tp_sammy-shi", "cid-sammy"),
        ):
            self.store.update_topic(tid, conversation_id=cid, onboarded_at=now_iso())
        self.sink = FakeSink786()
        self.source = FakeSource786()

    def test_reg_049_group_quote_reply_and_bot_reply_route_directly_to_origin_topic(self) -> None:
        ts = now_iso()
        self.store.add_send(
            "ik_jev_card_1", "reply", "tp_jev-decision-service", "om_sent_card_1",
            "🐕 汇报 · JEV 决策结果 [tp_jev-decision-service]\n请确认", target_message_id=OPS_CHAT, identity="bot",
        )
        self.store.add_bot_outbox(
            "om_sent_card_1", "tp_jev-decision-service", False,
            "🐕 汇报 · JEV 决策结果 [tp_jev-decision-service]\n请确认", ts,
        )

        collector = Collector(self.cfg, self.store, self.source)
        m_user = Message(
            message_id="om_reply_user_1", chat_id=OPS_CHAT, chat_name="运维大群", chat_type="group",
            sender_id="ou_colleague_1", sender_name="同事甲", sender_type="user",
            content="@Matthew Hong 已核对无误", msg_type="text", create_time=ts,
            reply_to="om_sent_card_1", thread_id="", mentions=[Mention(id=OWNER_ID, name="Matthew Hong")],
        )
        m_bot = Message(
            message_id="om_reply_bot_1", chat_id=OPS_CHAT, chat_name="运维大群", chat_type="group",
            sender_id="cli_laohu_bot", sender_name="老胡 Lark cli", sender_type="app",
            content="@Matthew Hong 自动巡检通过", msg_type="text", create_time=ts,
            reply_to="om_sent_card_1", thread_id="", mentions=[Mention(id=OWNER_ID, name="Matthew Hong")],
        )
        collector.ingest([m_user, m_bot])

        r_user = self.store.get_message("om_reply_user_1")
        r_bot = self.store.get_message("om_reply_bot_1")
        self.assertEqual(r_user["route"], "dispatch")
        self.assertEqual(r_user["topic_id"], "tp_jev-decision-service")
        self.assertEqual(r_bot["route"], "dispatch")
        self.assertEqual(r_bot["topic_id"], "tp_jev-decision-service")

    def test_reg_050_cli_write_touches_wakeup_and_deliver_only_checks_receipt(self) -> None:
        wakeup = self.cfg.state_dir / ".wakeup"
        m1 = Message(
            message_id="om_fwd_wake_1", chat_id=OPS_CHAT, chat_name="运维大群", chat_type="group",
            sender_id="ou_colleague_1", sender_name="同事甲", sender_type="user",
            content="请 Sammy 跟进", msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(m1, "dispatch", "at_me", [], topic_id="tp_bus")
        engine.forward_messages(self.store, "tp_sammy-shi", ["om_fwd_wake_1"], self_open_id=OWNER_ID)
        self.assertTrue(wakeup.exists())
        mtime1 = wakeup.stat().st_mtime_ns

        self.store.record_dispatch("b_prev_786", "tp_sammy-shi", ["om_fwd_wake_1"])
        self.store.mark_batch(["om_fwd_wake_1"], "b_prev_786", "tp_sammy-shi", "delivered")
        self.store.update_topic("tp_sammy-shi", state="running", pending_batch_id="b_prev_786", dispatched_at=now_iso())

        time.sleep(0.01)
        engine.write_receipt(self.cfg, "tp_sammy-shi", "b_prev_786", {"status": "handled", "summary": "第一批完成"})
        self.assertGreaterEqual(wakeup.stat().st_mtime_ns, mtime1)

        # 模拟 running 状态下收到 completed 回执文件、同时名下有新待推消息，deliver_only 应先 _check_receipt 解锁并当场投递
        self.store.update_topic("tp_sammy-shi", state="running", pending_batch_id="b_prev_786", dispatched_at=now_iso())
        m2 = Message(
            message_id="om_new_sammy_2", chat_id=OPS_CHAT, chat_name="运维大群", chat_type="group",
            sender_id="ou_colleague_1", sender_name="同事甲", sender_type="user",
            content="第二条待推消息", msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(m2, "dispatch", "at_me", [], topic_id="tp_sammy-shi")

        disp = Dispatcher(self.cfg, self.store, self.sink, source=self.source)
        res = disp.deliver_only()
        self.assertEqual(res["sent"], 1)
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(self.sink.sent[0]["conversation_id"], "cid-sammy")
        self.assertEqual(self.store.get_message("om_new_sammy_2")["dispatch_state"], "delivered")

    def test_reg_051_subagent_lamb_id_extraction_reuses_prepared_lamb_and_auto_closes_on_receipt(self) -> None:
        ts = now_iso()
        tc = {"name": "invoke_subagent", "args": {"Role": "English Role", "Prompt": "请排查 [lamb:lamb_87c4f569]"}}
        invs = guard._extract_subagent_invocations(tc, {"created_at": ts}, "1:0")
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0].lamb_id, "lamb_87c4f569")

        self.store.create_lamb(
            "lamb_87c4f569", "tp_sammy-shi", kind="subagent", task_title="中文标题", conversation_id="", state="running"
        )
        disp = Dispatcher(self.cfg, self.store, self.sink, source=self.source)
        discovered: list[str] = []
        disp._handle_lamb_line(
            "tp_sammy-shi", "antigravity", 10,
            {"step_index": 1, "created_at": ts, "tool_calls": [tc]}, discovered,
        )
        cid = "11111111-2222-3333-4444-555555555555"
        disp._handle_lamb_line(
            "tp_sammy-shi", "antigravity", 11,
            {"type": "GENERIC", "created_at": ts, "content": f"Created the following subagent(s):\n- Role: English Role\n  conversationId: {cid}"},
            discovered,
        )

        lambs = self.store.list_lambs("tp_sammy-shi")
        self.assertEqual(len(lambs), 1)
        self.assertEqual(lambs[0]["lamb_id"], "lamb_87c4f569")
        self.assertEqual(lambs[0]["conversation_id"], cid)
        self.assertEqual(lambs[0]["task_title"], "中文标题")

        # 母羊提交非 waiting 终态回执（status="done"）时，名下 running 的 subagent 羔羊自动收口为 done
        self.store.record_dispatch("b_lamb_786", "tp_sammy-shi", [])
        self.store.update_topic("tp_sammy-shi", state="running", pending_batch_id="b_lamb_786", dispatched_at=ts)
        engine.write_receipt(self.cfg, "tp_sammy-shi", "b_lamb_786", {"status": "done", "summary": "全部排查完毕"})
        disp._check_receipt(self.store.get_topic("tp_sammy-shi"))
        self.assertEqual(self.store.get_lamb("lamb_87c4f569")["state"], "done")

    def test_reg_052_closed_topic_resets_duty_card_working_and_tracked_items(self) -> None:
        self.store.create_topic("tp_dyn_test", "临时排障专席", "dynamic", "临时排障职责", "active")
        self.store.update_topic("tp_dyn_test", conversation_id="cid-dyn", onboarded_at=now_iso())
        self.store.upsert_card("tp_dyn_test", run_state="working", queued=2)
        duty.set_work(self.store, "tp_dyn_test", "DYN-1", state="doing", title="临时跟进")

        engine.close_session(self.store, "tp_dyn_test")
        self.assertEqual(self.store.get_topic("tp_dyn_test")["state"], "closed")
        card = self.store.get_card("tp_dyn_test")
        self.assertIsNotNone(card)
        self.assertEqual(card["run_state"], "idle")
        self.assertEqual(card["queued"], 0)


if __name__ == "__main__":
    unittest.main()
