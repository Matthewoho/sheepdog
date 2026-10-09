"""7.16 缺口 2：先记 sending 再发送；发送后来不及记账就崩溃，下一轮从 transcript 认定已送达、不重投；
transcript 里找不到 → uncertain，不自动重发，sessions 显示，redeliver 人工确认后重发；发送失败照旧重试。"""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.roster import Roster
from sheepdog.sink import SinkError
from sheepdog.sink.agentapi import transcript_path

from test_roster import ME, Base, FakeSink, FakeSource, msg


class TranscriptSink(FakeSink):
    """像 App 一样把收到的消息写进会话 transcript；write=False 模拟 transcript 还没写进去。"""

    def __init__(self):
        super().__init__()
        self.write = True
        self.fail = False

    def send_message(self, cid, content):
        if self.fail:
            raise SinkError("fx 发送失败")
        super().send_message(cid, content)
        if self.write:
            p = transcript_path(cid)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "SYSTEM_MESSAGE", "content": content}, ensure_ascii=False) + "\n")


class DeliveryTest(Base):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(Path(self.tmp.name) / "app")})
        self.env.start()
        self.sink = TranscriptSink()
        self.d = self.disp(Roster())
        self.d.init()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def poll(self, msgs):
        Collector(self.cfg, self.store, FakeSource([msgs]), Roster()).poll_once()

    def crash_after_send(self):
        """sink 收到之后、记账之前进程崩溃。"""
        real = self.store.set_dispatch_state

        def boom(batch_id, state, error=None):
            if state == "sent":
                raise RuntimeError("fx 进程在记账前崩溃")
            return real(batch_id, state, error)
        with mock.patch.object(self.store, "set_dispatch_state", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.d.dispatch_once()

    def test_crash_after_send_recovered_from_transcript_no_resend(self):
        self.poll([msg(message_id="om_test_c1", chat_type="p2p", chat_id="oc_test_p1")])
        self.crash_after_send()
        self.assertEqual(len(self.sink.to("conv_test_bus")), 1)
        d = self.store.dispatches_in_state("sending")
        self.assertEqual(len(d), 1)
        self.assertEqual(self.row("om_test_c1")["dispatch_state"], "pending")
        self.d.dispatch_once()  # 下一轮：从 transcript 认出已送达
        self.assertEqual(len(self.sink.to("conv_test_bus")), 1)  # 没有重投
        self.assertEqual(self.store.get_dispatch(d[0]["batch_id"])["state"], "sent")
        self.assertEqual(self.row("om_test_c1")["dispatch_state"], "delivered")
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["pending_batch_id"], d[0]["batch_id"])

    def test_not_in_transcript_uncertain_then_redeliver(self):
        self.poll([msg(message_id="om_test_u1", chat_type="p2p", chat_id="oc_test_p1")])
        self.sink.write = False
        self.crash_after_send()
        batch = self.store.dispatches_in_state("sending")[0]["batch_id"]
        self.d.dispatch_once()
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_bus")), 1)  # 不确定的不自动重发
        self.assertEqual(self.store.get_dispatch(batch)["state"], "uncertain")
        self.assertEqual(self.row("om_test_u1")["dispatch_state"], "uncertain")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out), mock.patch("sys.stderr", err):
                rc = cli.main(list(argv))
            return rc, out.getvalue() + err.getvalue()
        self.assertIn(f"⚠ 批次 {batch} 发送中断、不确定是否送达", run("sessions")[1])
        self.assertEqual(run("redeliver", "--batch", "b_nope")[0], 2)
        rc, text = run("redeliver", "--batch", batch)
        self.assertEqual(rc, 0)
        self.assertEqual(run("redeliver", "--batch", batch)[0], 2)  # 只能重发一次
        self.sink.write = True
        self.store.update_topic(BUS_TOPIC_ID, state="active", pending_batch_id=None)
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_bus")), 2)
        self.assertEqual(self.row("om_test_u1")["dispatch_state"], "delivered")

    def test_send_failure_still_retried(self):
        self.poll([msg(message_id="om_test_f1", chat_type="p2p", chat_id="oc_test_p1")])
        self.sink.fail = True
        self.d.dispatch_once()
        self.assertEqual(self.store.dispatches_in_state("failed")[0]["state"], "failed")
        self.assertEqual(self.row("om_test_f1")["dispatch_state"], "pending")
        self.sink.fail = False
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_f1")["dispatch_state"], "delivered")
