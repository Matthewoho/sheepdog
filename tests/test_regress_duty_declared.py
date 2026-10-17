"""回归 7.20 第二轮「登记不改决定」：卡住的会话不管自报什么，排着的消息都不放行、给主人的通知不多发。

10-12 18:0x 架构侧在导出副本里改坏过一处（卡住、但自报「进行中」的会话照常投递），当时只有随机长跑
（test_duty_longrun，一次两三分钟）抓到；6 个乱登记的确定性场景没抓到，因为新消息到达时自报的状态已经换掉了。
这里补一个快的确定性守卫：新消息到达时，自报正好是「进行中」，之后再换成做完了 / 等别人 / 等主人，都照样排队。

会话记录是临时目录里合成的 JSONL，发送用假的 sink 和假的 notifier；数据全是造的。"""

from datetime import datetime, timedelta

from sheepdog import duty
from sheepdog import session as sm

from test_duty import DutyBase, ago, tool
from test_roster import msg

KEY = "202610179999"


class DeclaredStuckTest(DutyBase):
    def stuck_alpha_with_queued_message(self, mid: str) -> int:
        """alpha 40 分钟前动过一次，20 分钟前送出的批次一直没碰（adopted 回执超时以后回到可投递，但在岗表判中断），
        又来了一条给它的消息。返回此刻 alpha 已经收到的批次数。"""
        self.append("conv_test_alpha", tool(ago(40), "Check dashboards"))
        self.sent_batch("tp_alpha", 20)
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id="oc_test_alpha_p2p")])
        return len(self.sink.to("conv_test_alpha"))

    def assert_held(self, mid: str, before: int, r: dict) -> None:
        self.assertEqual(self.card("tp_alpha")["run_state"], duty.STALLED)
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)  # 会话状态可投递，挡住它的是在岗表
        self.assertIn("中断", r["topics"]["tp_alpha"]["queued"])
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before)
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("会话中断了 [tp_bus]", self.notices[0])

    def test_regression_7_20_stuck_session_declaring_doing_is_still_held(self):
        """回归 7.20：卡住的会话自报「进行中」（消息到达时就是这个自报），消息照样排队、只通知主人一次。"""
        before = self.stuck_alpha_with_queued_message("om_test_rg720a")
        duty.set_work(self.store, "tp_alpha", KEY, title="自报在做", state="doing")
        r = self.d.dispatch_once()
        self.assert_held("om_test_rg720a", before, r)
        r = self.d.dispatch_once()
        self.assert_held("om_test_rg720a", before, r)

    def test_regression_7_20_stuck_session_any_declared_state_is_still_held(self):
        """回归 7.20：卡住的会话把自报从「进行中」改成做完了、等别人、等主人，每一轮都照样排队，通知不多发；
        会话真动起来（会话记录里有新动作）才补送。"""
        before = self.stuck_alpha_with_queued_message("om_test_rg720b")
        for state in ("doing", "done", "waiting_other", "waiting_owner", "doing"):
            with self.subTest(state=state):
                duty.set_work(self.store, "tp_alpha", KEY, title="自报", state=state, result="fx")
                r = self.d.dispatch_once()
                self.assert_held("om_test_rg720b", before, r)
        # 对照：证据变了（会话动起来）才放行，说明上面挡住它的是在岗表，而不是别的原因
        self.append("conv_test_alpha", tool(datetime.now().astimezone() + timedelta(seconds=2), "Back online"))
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before + 1)
        self.assertIn("om_test_rg720b", self.sink.to("conv_test_alpha")[-1])
