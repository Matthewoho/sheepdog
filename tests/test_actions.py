"""7.12 会话发起的 agentapi 动作交给常驻进程：非常驻进程入队不调 sink、常驻进程直接执行、run 先执行动作、
单条失败不阻塞、dry-run 本地执行、retire 补发退休通知（已关闭前任 / 裸 conversation_id）且回执转接手会话、actions 输出。"""

import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import actions, cli
from sheepdog import session as sm
from sheepdog.engine import BUS_TOPIC_ID, write_receipt
from sheepdog.roster import parse_roster

from test_handover_watch import _toml, handover_data
from test_roster import FIXTURES, ME, Base, msg

FIX = Path(__file__).resolve().parent / "fixtures"


def no_agentapi(*a, **kw):
    raise AssertionError("非常驻进程不应调 agentapi")


class ActionsTest(Base):
    def setUp(self):
        super().setUp()
        actions._resident = False
        self.d = self.disp()
        self.d.init()  # 基础名册（不含待建条目），之后换成带接手条目的名册
        self.d.roster = self.roster = parse_roster(handover_data())
        self.d.sync_roster()

    def tearDown(self):
        actions._resident = False
        super().tearDown()

    def cli(self, *argv, sink="agentapi", roster_data=None):
        (Path(self.tmp.name) / "roster.toml").write_text(_toml(roster_data or handover_data()), encoding="utf-8")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsink = "{sink}"\nroster_path = "roster.toml"\n'
                            f'playbook_dir = "{FIXTURES}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out), mock.patch("sys.stderr", err):
            rc = cli.main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def pending(self):
        return [(a["kind"], json.loads(a["args_json"])) for a in self.store.pending_actions()]

    # ---------- 入队 ----------
    def test_non_resident_enqueues_without_agentapi(self):
        with mock.patch("sheepdog.sink.agentapi.AgentApiSink._call", side_effect=no_agentapi):
            cases = [("init",), ("spawn", "--key", "beta"),
                     ("new-session", "--key", "dz", "--title", "T", "--duty", "D", "--chat", "oc_test_dz:all"),
                     ("push-rules",), ("retire", "--topic", "conv_test_old")]
            for argv in cases:
                rc, text = self.cli(*argv)
                self.assertEqual(rc, 0, argv)
                self.assertIn("已提交，sheepdog 下一轮执行", text)
        kinds = [k for k, _ in self.pending()]
        self.assertEqual(kinds, ["init", "spawn", "new_session", "push_rules", "retire"])
        self.assertEqual(self.pending()[2][1]["chats"], [["oc_test_dz", True]])
        self.assertIsNone(self.store.get_topic("tp_dz"))
        self.assertIsNone(self.store.get_topic("tp_beta")["conversation_id"])
        self.assertEqual(self.sink.to("conv_test_old"), [])

    def test_non_resident_validates_before_enqueue(self):
        for argv in (("spawn", "--key", "alpha"),
                     ("new-session", "--key", "alpha", "--title", "T", "--duty", "D"),
                     ("new-session", "--key", "dz", "--title", "T", "--duty", "D", "--chat", "oc_test_alpha_p2p"),
                     ("push-rules", "--topic", "tp_nobody"), ("retire", "--topic", "tp_nobody"),
                     ("retire", "--topic", BUS_TOPIC_ID)):
            rc, _ = self.cli(*argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(self.pending(), [])

    def test_dry_run_runs_locally(self):
        rc, text = self.cli("spawn", "--key", "beta", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("[dryrun] new-conversation", text)
        self.assertEqual(self.pending(), [])

    def test_resident_runs_directly(self):
        actions.mark_resident()
        rc, text = self.cli("new-session", "--key", "dz", "--title", "T", "--duty", "D", sink="dryrun")
        self.assertEqual(rc, 0)
        self.assertNotIn("已提交", text)
        self.assertEqual(self.store.get_topic("tp_dz")["kind"], "dynamic")
        self.assertEqual(self.pending(), [])

    # ---------- 执行 ----------
    def test_run_pending_in_order_failure_does_not_block(self):
        a1 = actions.enqueue(self.store, "spawn", {"key": "nobody"})
        a2 = actions.enqueue(self.store, "new_session", {"key": "dz", "title": "T", "duty": "D",
                                                         "chats": [["oc_test_dz", True]], "message_ids": [], "note": ""})
        a3 = actions.enqueue(self.store, "push_rules", {"topic": ""})
        self.assertEqual(actions.run_pending(self.d), 3)
        rows = {a["id"]: a for a in self.store.list_actions(include_done=True)}
        self.assertIn("名册里没有", rows[a1]["error"])
        self.assertIsNone(rows[a2]["error"])
        self.assertEqual(json.loads(rows[a2]["result"])["topic_id"], "tp_dz")
        self.assertIsNone(rows[a3]["error"])
        self.assertEqual(self.store.get_topic("tp_dz")["anchors_json"], '["oc_test_dz:all"]')
        self.assertEqual(self.store.pending_actions(), [])
        self.assertEqual(actions.run_pending(self.d), 0)  # 失败的不重试

    def test_run_loop_executes_actions_before_poll(self):
        actions.enqueue(self.store, "new_session", {"key": "dz", "title": "T", "duty": "D",
                                                    "chats": [], "message_ids": [], "note": ""})
        seen = {}

        def fake_cycle(collector, dispatcher):
            seen["pending"] = len(dispatcher.store.pending_actions())
            seen["topic"] = dispatcher.store.get_topic("tp_dz")
            seen["resident"] = actions.is_resident()

        with mock.patch.object(cli, "_cycle", fake_cycle), \
                mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("run", sink="dryrun")
        self.assertEqual(seen["pending"], 0)
        self.assertIsNotNone(seen["topic"])
        self.assertTrue(seen["resident"])

    # ---------- retire ----------
    def handover_flow(self, record_expected: str):
        """接手会话读完上下文、前任交接回执到达 → 转给 tp_beta。"""
        self.ack("tp_beta", "needs_decision")
        prev = self.store.get_topic(record_expected)
        write_receipt(self.cfg, record_expected, prev["pending_batch_id"], {"status": "done", "summary": "FX 交接要点"})
        self.d.dispatch_once()
        cid = self.store.get_topic("tp_beta")["conversation_id"]
        self.assertIn("FX 交接要点", self.sink.to(cid)[-1])

    def test_retire_closed_predecessor_resends(self):
        self.d.spawn("beta")
        self.assertEqual(len(self.sink.to("conv_test_old")), 1)
        self.assertEqual(self.store.get_topic("tp_beta.prev")["state"], sm.CLOSED)
        r = self.d.retire("tp_beta.prev")
        self.assertEqual((r["record_topic"], r["successor"]), ("tp_beta.prev", "tp_beta"))
        self.assertEqual(len(self.sink.to("conv_test_old")), 2)
        self.assertIn(f"batch={r['batch']}", self.sink.to("conv_test_old")[-1])
        self.assertIn("successor=[managed] Beta 接手", self.sink.to("conv_test_old")[-1])
        self.handover_flow("tp_beta.prev")

    def test_retire_bare_conversation_id_of_unretired_predecessor(self):
        d = handover_data()
        d["session"][2]["retire_predecessor"] = False  # 当时绕过了退休通知
        self.d.roster = self.roster = parse_roster(d)
        self.d.sync_roster()
        self.d.spawn("beta")
        self.assertEqual(self.sink.to("conv_test_old"), [])
        self.assertIsNone(self.store.get_topic("tp_beta.prev"))
        r = self.d.retire("conv_test_old")
        self.assertEqual((r["record_topic"], r["successor"]), ("tp_beta.prev", "tp_beta"))
        self.assertEqual(len(self.sink.to("conv_test_old")), 1)
        self.handover_flow("tp_beta.prev")

    def test_retire_unknown_conversation_goes_to_bus(self):
        r = self.d.retire("conv-test-unknown-123456")
        self.assertEqual((r["record_topic"], r["successor"]), ("tp_retired.conv-test-un", BUS_TOPIC_ID))
        self.assertIn("successor=总线", self.sink.to("conv-test-unknown-123456")[-1])
        write_receipt(self.cfg, r["record_topic"], r["batch"], {"status": "done", "summary": "FX 无主交接"})
        if self.store.get_topic(BUS_TOPIC_ID)["pending_batch_id"]:
            self.ack(BUS_TOPIC_ID)
        self.d.dispatch_once()
        self.assertIn("FX 无主交接", self.sink.to("conv_test_bus")[-1])

    def test_retire_rules(self):
        with self.assertRaisesRegex(ValueError, "总线不能退休"):
            self.d.retire(BUS_TOPIC_ID)
        with self.assertRaisesRegex(ValueError, "总线不能退休"):
            self.d.retire("conv_test_bus")
        self.d.new_session("dz", "T", "D", [])
        r = self.d.retire("tp_dz")
        self.assertEqual((r["record_topic"], r["closed"]), ("tp_dz.retired", "tp_dz"))
        self.assertEqual(self.store.get_topic("tp_dz")["state"], sm.CLOSED)
        r = self.d.retire("tp_alpha")
        self.assertIn("名册", r["note"])

    def test_actions_command(self):
        actions.enqueue(self.store, "spawn", {"key": "nobody"})
        actions.enqueue(self.store, "push_rules", {"topic": ""})
        actions.run_pending(self.d)
        actions.enqueue(self.store, "init", {})
        rc, text = self.cli("actions")
        self.assertEqual(rc, 0)
        self.assertIn("[排队中] init", text)
        self.assertIn("[失败 @", text)
        self.assertIn("错误: ValueError", text)
        self.assertIn("[完成 @", text)


if __name__ == "__main__":
    unittest.main()
