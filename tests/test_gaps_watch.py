"""7.16 缺口 3：等待被无关回复误关。对方回复后进入 replied（候选），回复照常投递、提醒暂停、不关闭；
会话 watch-done / 回执 anchors watch_done:N 确认才关闭；回执后仍未确认 → 回到 waiting、从回复时间重新计时。"""

import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.engine import Collector, add_watch, write_receipt

from test_roster import ME, Base, FakeSource, msg


def ago(minutes):
    return (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class WatchCandidateTest(Base):
    def setUp(self):
        super().setUp()
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.wid = add_watch(self.store, "tp_alpha", "ou_test_bob", note="等 Bob 给排期")

    def poll(self, msgs):
        Collector(self.cfg, self.store, FakeSource([msgs]), self.roster).poll_once()

    def nudges(self):
        return self.store.conn.execute("SELECT * FROM messages WHERE reason='watch_nudge'").fetchall()

    def chit_chat(self, mid="om_test_hi"):
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id="oc_test_bobp2p", sender_id="ou_test_bob",
                       content="早上好～")])
        self.d.dispatch_once()

    def test_chit_chat_does_not_close(self):
        self.store.update_watch(self.wid, started_at=ago(20))
        self.chit_chat()
        w = self.store.get_watch(self.wid)
        self.assertEqual((w["status"], w["closed_at"], w["last_reply_message_id"]), ("replied", None, "om_test_hi"))
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("om_test_hi", text)
        self.assertIn(f"watch-done --id {self.wid}", text)
        self.d.dispatch_once()
        self.assertEqual(self.nudges(), [])  # 提醒暂停
        # 回执没确认 → 回到 waiting，从回复时间重新计时
        self.ack("tp_alpha")
        self.d.dispatch_once()
        w = self.store.get_watch(self.wid)
        self.assertEqual((w["status"], w["closed_at"], w["nudged_15_at"]), ("waiting", None, None))
        self.d.dispatch_once()
        self.assertEqual(self.nudges(), [])  # 刚回复过，还没到 15 分钟
        self.store.update_watch(self.wid, last_reply_at=ago(16))
        self.d.dispatch_once()
        self.assertEqual(len(self.nudges()), 1)
        # 60 分钟到期按最后一次回复时间算：登记于 2 小时前，但最后一次回复在 20 分钟前 → 不到期
        self.store.update_watch(self.wid, started_at=ago(120), last_reply_at=ago(20))
        self.d.dispatch_once()
        self.assertIsNone(self.store.get_watch(self.wid)["closed_at"])

    def test_confirm_via_receipt_anchor(self):
        self.chit_chat()
        t = self.store.get_topic("tp_alpha")
        write_receipt(self.cfg, "tp_alpha", t["pending_batch_id"],
                      {"status": "handled", "anchors": [f"watch_done:{self.wid}", "project:FX"]})
        self.d.dispatch_once()
        w = self.store.get_watch(self.wid)
        self.assertEqual(w["close_reason"], "done")
        anchors = json.loads(self.store.get_topic("tp_alpha")["anchors_json"])
        self.assertIn("project:FX", anchors)
        self.assertFalse(any(a.startswith("watch_done:") for a in anchors))

    def test_confirm_via_cli(self):
        self.chit_chat()
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}), \
                redirect_stdout(out):
            cli.main(["watches"])
            cli.main(["watch-done", "--id", str(self.wid)])
        self.assertIn("对方已回复、等会话确认", out.getvalue())
        self.assertEqual(self.store.get_watch(self.wid)["close_reason"], "done")

    def test_receipt_of_earlier_batch_does_not_reset(self):
        # alpha 正在等一个更早批次的回执时，Bob 的回复还没送到；这个更早的回执不能把等待打回 waiting
        self.poll([msg(message_id="om_test_e0", chat_type="p2p", chat_id="oc_test_alpha_p2p")])
        self.d.dispatch_once()
        earlier = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.poll([msg(message_id="om_test_hi2", chat_type="p2p", chat_id="oc_test_bobp2p", sender_id="ou_test_bob")])
        write_receipt(self.cfg, "tp_alpha", earlier, {"status": "handled"})
        self.d.dispatch_once()  # 收早先的回执，然后投 Bob 的回复
        self.assertEqual(self.store.get_watch(self.wid)["status"], "replied")
        self.assertIn("om_test_hi2", self.sink.to("conv_test_alpha")[-1])

    def test_receipt_timeout_also_resets(self):
        # adopted 会话回执可选：超时没回执，也视为没确认，回到 waiting（不会永远卡在 replied）
        self.chit_chat()
        self.store.update_topic("tp_alpha", dispatched_at=ago(60))
        self.d.dispatch_once()
        self.assertEqual(self.store.get_watch(self.wid)["status"], "waiting")

    def test_paused_while_replied_even_if_long_ago(self):
        # 回复之后会话迟迟没回执（比如主人正在会话里）：等确认期间不提醒、不到期
        self.chit_chat()
        self.store.update_watch(self.wid, last_reply_at=ago(90))
        self.d.dispatch_once()
        self.assertEqual(self.nudges(), [])
        self.assertIsNone(self.store.get_watch(self.wid)["closed_at"])
