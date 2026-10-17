"""回归 7.21：只有「回执超时」进的 attention 会自动恢复；别的原因（这里用发送失败）进的 attention，晚到的回执照样
记下、会话记录又有新动作，都不解开、不补送、不发「已恢复」，要人 session-reset。

原有测试只覆盖了「发送失败 + 会话记录新动作」这一条证据，而那条路对非回执超时的 attention 本来就读不到（进 attention
时没设读取起点），所以把「只有回执超时才自动恢复」那道判断删掉也还是绿的；晚到回执这条证据没有测试守。

会话记录是临时目录里合成的 JSONL，发送用假的 sink 和假的 notifier；数据全是造的。"""

from datetime import datetime, timedelta

from sheepdog import session as sm
from sheepdog.engine import BUS_TOPIC_ID, write_receipt
from sheepdog.sink import SinkError

from test_attention import AttentionBase, planner, utc
from test_roster import msg


class SendFailedAttentionTest(AttentionBase):
    def send_failed_attention(self) -> str:
        """总线先投出一批、回执超时一次（failed），之后 agentapi 一直发不出去：失败计数满 3 次进 attention，
        原因是发送失败。返回回执超时的那一批的批次号。"""
        class Broken(type(self.sink)):
            def send_message(self, cid, content):
                if cid == "conv_test_bus":
                    raise SinkError("agentapi send-message 失败: fx")
                super().send_message(cid, content)

        self.poll([msg(message_id="om_test_rg721a", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        first = self.bus()["pending_batch_id"]
        self.d = self.make(Broken())
        self.age_bus()
        self.d.dispatch_once()
        self.d.dispatch_once()
        t = self.bus()
        self.assertEqual((t["state"], t["attention_reason"]), (sm.ATTENTION, "send_failed"))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("不会自动恢复", self.notices[0])
        return first

    def assert_still_stuck(self) -> None:
        t = self.bus()
        self.assertEqual((t["state"], t["attention_reason"]), (sm.ATTENTION, "send_failed"))
        self.assertTrue(t["attention_at"])
        self.assertEqual(len(self.notices), 1)  # 没有「已恢复」
        self.assertFalse(any("已恢复" in n for n in self.notices))

    def test_regression_7_21_send_failed_attention_not_unlocked_by_late_receipt(self):
        """回归 7.21：发送失败进的 attention，之前回执超时那一批的回执晚到：照样记成 acked_late，但不解开、不补送。"""
        first = self.send_failed_attention()
        self.poll([msg(message_id="om_test_rg721b", chat_type="p2p", chat_id="oc_test_stranger")])
        write_receipt(self.cfg, BUS_TOPIC_ID, first, {"status": "handled", "summary": "晚到的进展"})
        self.d.dispatch_once()
        self.assertEqual(self.store.get_dispatch(first)["state"], "acked_late")  # 晚到的回执照样记下
        self.assert_still_stuck()
        self.d.dispatch_once()
        self.assert_still_stuck()
        self.assertEqual(self.row("om_test_rg721b")["dispatch_state"], "pending")  # 新来的照样排队

    def test_regression_7_21_send_failed_attention_not_unlocked_by_new_activity(self):
        """回归 7.21：发送失败进的 attention，会话记录里接连出现进 attention 以后的新动作，也不解开。"""
        self.send_failed_attention()
        later = lambda s: utc(datetime.now() + timedelta(seconds=s))  # noqa: E731
        self.append("conv_test_bus", planner(later(5), 11))
        self.d.dispatch_once()
        self.append("conv_test_bus", planner(later(6), 12),
                    {"type": "GENERIC", "source": "MODEL", "status": "DONE", "step_index": 13,
                     "created_at": later(7), "content": "fx"})
        self.d.dispatch_once()
        self.assert_still_stuck()
