"""7.38 第 7 条：名册 managed / 总线新开的会话的回执说法改成真话。

以前写「回执可选：没回执不会重投」。代码的真实行为（_check_receipt / _miss）：不交回执确实不重投，但会话在交回执或回执超时
（[session] receipt_timeout_minutes，正式 20）之前一直是 running，新消息排队收不到。现在写「要交回执：不交不会重投，但要等
回执超时（约 N 分钟）才收得到下一批」，N 取配置；批次末尾去掉「（可选）」。先用行为测试钉住这句话，再核对文字。
"""

from sheepdog import session as sm
from sheepdog.config import SessionConfig
from sheepdog.engine import BUS_TOPIC_ID
from sheepdog.playbook import DEFAULT_RECEIPT_TIMEOUT, Playbook
from sheepdog.prompts import interface_section, managed_receipt_line

from test_roster import Base, later, msg

TIMEOUT = 7


class ManagedReceiptTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg.session.receipt_timeout_minutes = TIMEOUT
        self.d = self.disp()
        self.d.init()                                                  # 给 alpha 发登记通知
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)

    def texts(self) -> list[str]:
        return self.sink.to("conv_test_alpha")

    def test_no_receipt_no_redelivery_but_next_batch_waits_for_timeout(self):
        """文字里那句话的每一半都对：不重投；收下一批要等回执超时。"""
        self.poll([msg(message_id="om_test_r1", chat_id="oc_test_alpha_p2p", content="第一件")])
        self.d.dispatch_once()
        b1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.poll([msg(message_id="om_test_r2", chat_id="oc_test_alpha_p2p", content="第二件")])
        n = len(self.texts())
        with later(TIMEOUT - 1):                                       # 还没到回执超时：一直 running，第二件排队
            self.d.dispatch_once()
        self.assertEqual(len(self.texts()), n)
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.RUNNING)
        self.assertEqual(self.row("om_test_r2")["dispatch_state"], "pending")
        with later(TIMEOUT + 1):                                       # 超时：记一次未回执，第二件送到，第一件不重投
            self.d.dispatch_once()
        new = "\n".join(self.texts()[n:])
        self.assertIn("第二件", new)
        self.assertNotIn("第一件", new)
        self.assertEqual(self.store.get_dispatch(b1)["state"], "missed")
        self.assertEqual(self.store.get_topic("tp_alpha")["receipt_missed"], 1)

    def test_texts_say_so_with_configured_minutes(self):
        onboarding = self.texts()[0]
        self.assertIn(managed_receipt_line(False, TIMEOUT), onboarding)
        self.assertIn(f"约 {TIMEOUT} 分钟", onboarding)
        self.assertNotIn("回执可选", onboarding)
        self.poll([msg(message_id="om_test_r3", chat_id="oc_test_alpha_p2p", content="第三件")])
        self.d.dispatch_once()
        batch = self.texts()[-1]
        self.assertIn("处理完成后提交回执：", batch)
        self.assertNotIn("（可选）", batch)

    def test_cli_host_and_bus_wording(self):
        cli = interface_section("tp_x", bus=False, remind=[15], expire=60, cli_host=True, receipt_timeout=TIMEOUT)
        self.assertIn("要交回执：这一轮结束时没有回执，记一次「未回执」，不重投。", cli)
        self.assertNotIn("分钟）才收得到", cli)                           # 命令行会话这一轮结束就收尾，不等超时
        bus = interface_section(BUS_TOPIC_ID, bus=True, remind=[15], expire=60, receipt_timeout=TIMEOUT)
        self.assertIn("没有提交回执会被判定为处理失败并重投。", bus)
        self.assertNotIn("回执可选", bus)

    def test_default_minutes(self):
        self.assertEqual(DEFAULT_RECEIPT_TIMEOUT, SessionConfig().receipt_timeout_minutes)
        self.assertEqual(Playbook(self.cfg.playbook_path).receipt_timeout, DEFAULT_RECEIPT_TIMEOUT)
        self.assertEqual(Playbook.from_config(self.cfg).receipt_timeout, TIMEOUT)
