"""7.11 规则改了自动同步：建会话记指纹；改 playbook 后下一批最前面带完整新规则、只带一次；没改不带；
无指纹视为不同；总线同样生效（名册变化不触发）；push-rules 立即投递并更新指纹；dry-run 不发不写。"""

import io
import os
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.engine import BUS_TOPIC_ID
from sheepdog.prompts import RULES_UPDATE_HEADER

from test_roster import FIXTURES, ME, Base, msg


class RulesSyncTest(Base):
    def setUp(self):
        super().setUp()
        self.pdir = Path(self.tmp.name) / "playbook"
        shutil.copytree(FIXTURES, self.pdir)
        self.cfg.playbook_dir = str(self.pdir)
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def edit(self, name, text):
        (self.pdir / name).write_text(text, encoding="utf-8")

    def deliver_to(self, topic, mid):
        chat = "oc_test_alpha_p2p" if topic == "tp_alpha" else "oc_test_stranger"
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id=chat)])
        self.d.dispatch_once()
        conv = "conv_test_alpha" if topic == "tp_alpha" else "conv_test_bus"
        text = self.sink.to(conv)[-1]
        self.assertIn(mid, text)
        self.ack(topic)
        return text

    def test_hash_recorded_at_creation(self):
        for tid in ("tp_alpha", BUS_TOPIC_ID):
            t = self.store.get_topic(tid)
            self.assertEqual(len(t["rules_hash"]), 64)
            self.assertEqual(self.d.rules_status(t), "最新")

    def test_no_change_no_rules(self):
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to("tp_alpha", "om_test_n1"))
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to(BUS_TOPIC_ID, "om_test_n2"))

    def test_common_change_sent_once_in_front(self):
        self.edit("common.md", "FX-COMMON-V2 title={{session_title}}")
        self.assertIn("待更新", self.d.rules_status(self.store.get_topic("tp_alpha")))
        text = self.deliver_to("tp_alpha", "om_test_c1")
        self.assertTrue(text.startswith(RULES_UPDATE_HEADER))
        self.assertIn("FX-COMMON-V2 title=Alpha 需求", text)
        # 完整现行规则：security + onboarding + common + 接口说明，都在批次头之前
        head = text.index("[sheepdog] 新信号批次")
        for part in ("FX-SECURITY", "FX-ONBOARDING", "FX-COMMON-V2", "## sheepdog 接口说明"):
            self.assertLess(text.index(part), head, part)
        self.assertEqual(self.d.rules_status(self.store.get_topic("tp_alpha")), "最新")
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to("tp_alpha", "om_test_c2"))  # 只带一次

    def test_missing_hash_treated_as_changed(self):
        self.store.update_topic("tp_alpha", rules_hash=None)
        self.assertIn("未记录", self.d.rules_status(self.store.get_topic("tp_alpha")))
        self.assertTrue(self.deliver_to("tp_alpha", "om_test_m1").startswith(RULES_UPDATE_HEADER))

    def test_bus_too_but_not_on_roster_change(self):
        self.d.new_session("deploy", "发布排查", "FX 职责", [("oc_test_dall", True)])  # 名册变化
        text = self.deliver_to(BUS_TOPIC_ID, "om_test_b1")
        self.assertNotIn(RULES_UPDATE_HEADER, text)
        self.assertIn("名册更新", text)
        self.edit("bus.md", "FX-BUS-V2\n{{roster}}")
        text = self.deliver_to(BUS_TOPIC_ID, "om_test_b2")
        self.assertTrue(text.startswith(RULES_UPDATE_HEADER))
        self.assertIn("FX-BUS-V2", text)
        self.assertIn("tp_deploy", text)  # 发出去的完整规则里名册是真实的

    def test_push_rules(self):
        self.edit("security.md", "FX-SECURITY-V2")
        report = self.d.push_rules()
        self.assertEqual(report["tp_alpha"], "已发送")
        self.assertEqual(report[BUS_TOPIC_ID], "已发送")
        self.assertNotIn("tp_ops", report)  # known 会话不投递
        self.assertIn("跳过", self.d.push_rules("tp_ops")["tp_ops"])
        for conv in ("conv_test_alpha", "conv_test_bus"):
            last = self.sink.to(conv)[-1]
            self.assertTrue(last.startswith(f"[sheepdog] {RULES_UPDATE_HEADER}"))
            self.assertIn("FX-SECURITY-V2", last)
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to("tp_alpha", "om_test_p1"))
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.d.push_rules("tp_nobody")

    def test_push_rules_cli_dry_run(self):
        self.edit("common.md", "FX-COMMON-V3")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsink = "dryrun"\nplaybook_dir = "{self.pdir}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        before = self.store.get_topic(BUS_TOPIC_ID)["rules_hash"]
        n_sent = len(self.sink.sent)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(cli.main(["push-rules", "--topic", BUS_TOPIC_ID, "--dry-run"]), 0)
        self.assertIn("tp_bus: 已发送", out.getvalue())
        self.assertIn("FX-COMMON-V3", out.getvalue())          # DryRunSink 只打印
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["rules_hash"], before)  # 不写真实账本
        self.assertEqual(len(self.sink.sent), n_sent)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            cli.main(["sessions"])
        self.assertIn("规则: 待更新", out.getvalue())


if __name__ == "__main__":
    unittest.main()
