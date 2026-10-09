"""批次里每条消息的标题行带发送人 open_id，有 @ 时另起一行列出被 @ 的人（会话代回时要用 open_id 才能真正 @ 到人）。"""

from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.roster import Roster

from test_roster import ME, Base, FakeSource, msg


class HeaderFormatTest(Base):
    def setUp(self):
        super().setUp()
        self.roster = Roster()
        self.d = self.disp(Roster())

    def batch(self, msgs):
        Collector(self.cfg, self.store, FakeSource([msgs]), self.roster).poll_once()
        self.d.dispatch_once()
        return self.sink.to("conv_test_bus")[-1]

    def test_sender_id_and_mentions(self):
        text = self.batch([
            msg(message_id="om_test_f1", chat_name="测试群", sender_name="Andy Hu", sender_id="ou_test_andy",
                mentions=[Mention(ME, "主人"), Mention("ou_test_bob", "Bob"), Mention("all", "所有人", is_all=True)]),
            msg(message_id="om_test_f2", chat_type="p2p", chat_id="oc_test_p1", sender_name="告警机器人",
                sender_id="cli_test_bot", sender_type="app", content="出故障了"),
            msg(message_id="om_test_f3", chat_type="p2p", chat_id="oc_test_p2", sender_name="Carol",
                sender_id="ou_test_carol"),
        ])
        lines = text.splitlines()
        h1 = next(i for i, ln in enumerate(lines) if ln.startswith("### ") and "Andy Hu" in ln)
        self.assertIn("| Andy Hu（ou_test_andy） |", lines[h1])
        self.assertEqual(lines[h1 + 1], f"@了：主人（{ME}）、Bob（ou_test_bob）、所有人")
        self.assertIn("| 告警机器人（cli_test_bot） |", text)  # 机器人发送方也带 id
        h3 = next(i for i, ln in enumerate(lines) if ln.startswith("### ") and "Carol" in ln)
        self.assertIn("| Carol（ou_test_carol） |", lines[h3])
        self.assertFalse(lines[h3 + 1].startswith("@了："))  # 没有 @ 不出现这一行
        self.assertEqual(sum(ln.startswith("@了：") for ln in lines), 1)
