"""[notify] file：给主人的通知写进文件（测试 sidecar 不拉飞书时用）。没设时行为不变；设了又拉飞书，doctor 提示。

全部在临时目录里：配置、账本、通知文件；不调 lark-cli、agentapi。"""

import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.config import ConfigError, load_config
from sheepdog.engine import BUS_TOPIC_ID
from sheepdog.notify import FileNotifier, OwnerDM, read_notices

from test_guardrails import GuardBase
from test_roster import ME, msg


class FakeLark:
    """有 send_text 的假消息源：记下发了什么，不碰网络。"""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.keys: list[str] = []

    def send_text(self, to, text, identity, idempotency_key=""):
        self.sent.append((to, text, identity))
        self.keys.append(idempotency_key)


class NotifyConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(self.root / "config.toml"),
                                                "SHEEPDOG_STATE_DIR": str(self.root / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_cfg(self, extra: str = "") -> Path:
        p = self.root / "config.toml"
        p.write_text(f'self_open_id = "{ME}"\nstate_dir = "{self.root / "state"}"\nsink = "dryrun"\n' + extra,
                     encoding="utf-8")
        return p

    def test_default_unchanged(self):
        cfg = load_config(self.write_cfg())
        self.assertIsNone(cfg.notify_path)
        lark = FakeLark()
        n = cli._notifier(cfg, lark, mock.Mock(dry_run=False))
        self.assertNotIsInstance(n, FileNotifier)
        n("hi")
        self.assertEqual(lark.sent, [(ME, "hi", "bot")])  # 照旧经机器人私聊主人
        # 走通知账本时带防重编号发（lark-cli --idempotency-key）
        self.assertIsInstance(n, OwnerDM)
        self.assertTrue(getattr(n, "ledger", True))
        n.send("again", "sd-fx")
        self.assertEqual((lark.sent[-1], lark.keys[-1]), ((ME, "again", "bot"), "sd-fx"))
        self.assertIsNone(cli._notifier(cfg, object(), mock.Mock(dry_run=False)))  # 不拉 IM：没有渠道
        self.assertEqual(cli.notify_report(cfg), "主人通知: 经机器人私聊")

    def test_file_relative_to_config_and_validated(self):
        cfg = load_config(self.write_cfg('[notify]\nfile = "out/notices.log"\n'))
        self.assertEqual(cfg.notify_path, self.root / "out" / "notices.log")
        cfg = load_config(self.write_cfg(f'[notify]\nfile = "{self.root / "abs.log"}"\n'))
        self.assertEqual(cfg.notify_path, self.root / "abs.log")
        with self.assertRaisesRegex(ConfigError, r"\[notify\] file"):
            load_config(self.write_cfg("[notify]\nfile = 3\n"))
        self.assertIsNone(load_config(self.write_cfg('[notify]\nfile = "  "\n')).notify_path)

    def test_file_wins_over_lark_and_dry_run_only_prints(self):
        cfg = load_config(self.write_cfg('[notify]\nfile = "notices.log"\n'))
        lark = FakeLark()
        n = cli._notifier(cfg, lark, mock.Mock(dry_run=False))
        self.assertIsInstance(n, FileNotifier)
        n.notify("🐕 汇报 · 会话中断了 [tp_bus]\n第二行", "羊群看板通知")
        self.assertEqual(lark.sent, [])  # 写文件优先，不发飞书
        got = read_notices(cfg.notify_path)
        self.assertEqual([(g["what"], g["text"]) for g in got], [("羊群看板通知", "🐕 汇报 · 会话中断了 [tp_bus]\n第二行")])
        self.assertEqual(got[0]["tz"], "北京时间")
        # dry-run：只打印，不写文件
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._notifier(cfg, lark, mock.Mock(dry_run=True))("试一下")
        self.assertIn("正式运行写进", out.getvalue())
        self.assertEqual(len(read_notices(cfg.notify_path)), 1)
        # 不拉 IM、没配 self_open_id 也能写文件（测试 sidecar 的情形）
        cfg.self_open_id = ""
        cfg.source.enabled = False
        self.assertIsInstance(cli._notifier(cfg, object(), mock.Mock(dry_run=False)), FileNotifier)

    def test_doctor_warns_when_file_set_while_pulling_lark(self):
        cfg = load_config(self.write_cfg('[notify]\nfile = "notices.log"\n'))
        line = cli.notify_report(cfg)
        self.assertIn("写到文件", line)
        self.assertIn("⚠ 主人通知写到文件，不发飞书", line)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["doctor"])
        self.assertIn("⚠ 主人通知写到文件，不发飞书", out.getvalue())
        # 测试环境（不拉飞书）设了文件：只说写到哪，不警告
        cfg = load_config(self.write_cfg('[notify]\nfile = "notices.log"\n[source]\nenabled = false\n'))
        self.assertNotIn("⚠", cli.notify_report(cfg))
        # 不拉飞书又没设文件：通知只记日志，提示一下
        cfg = load_config(self.write_cfg("[source]\nenabled = false\n"))
        self.assertIn("只记日志", cli.notify_report(cfg))

    def test_example_config_mentions_notify(self):
        text = (Path(__file__).resolve().parent.parent / "examples" / "config.example.toml").read_text(encoding="utf-8")
        self.assertIn("[notify]", text)
        p = self.root / "example.toml"
        p.write_text(f'state_dir = "{self.root / "state"}"\n' + text, encoding="utf-8")
        self.assertIsNone(load_config(p).notify_path)  # 示例里默认不设


class NotifyFileEngineTest(GuardBase):
    """真实的 Dispatcher：在岗表、attention 的通知都进文件，带通知类型和时间。"""

    def test_dispatcher_writes_type_and_time(self):
        path = Path(self.tmp.name) / "notices.log"
        self.d.notifier = FileNotifier(path, self.cfg.timezone_offset)
        self.store.update_topic("tp_alpha", state=sm.ATTENTION, attention_at="2026-10-12T01:00:00+08:00",
                                attention_reason="send_failed")
        self.d._attention_entered(self.store.get_topic("tp_alpha"), "fx")
        self.assertTrue(self.d._notify_owner("🐕 汇报 · 测试", "羊群看板通知"))
        got = read_notices(path)
        self.assertEqual([g["what"] for g in got], ["attention 通知", "羊群看板通知"])
        self.assertIn("「Alpha 需求」（topic `tp_alpha`", got[0]["text"])
        for g in got:
            at = datetime.strptime(g["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
            self.assertLess(abs((datetime.now(timezone.utc) - at).total_seconds()), 120)
        self.assertTrue(re.match(r"^### \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（北京时间）· attention 通知$",
                                 path.read_text(encoding="utf-8").splitlines()[0]))

    def test_unwritable_file_reports_not_sent(self):
        bad = Path(self.tmp.name) / "a_file"
        bad.write_text("x", encoding="utf-8")
        self.d.notifier = FileNotifier(bad / "under_a_file.log")  # 父目录是个文件：写不进去
        with self.assertLogs("sheepdog", "WARNING") as logs:
            self.assertFalse(self.d._notify_owner("x", "羊群看板通知"))
        self.assertTrue(any("给主人发羊群看板通知失败" in m for m in logs.output))

    def test_plain_notifier_still_gets_text_only(self):
        self.assertTrue(self.d._notify_owner("🐕 汇报 · 测试 `sheepdog board`", "羊群看板通知"))
        self.assertEqual(self.notices, ["🐕 汇报 · 测试 `sheepdog board`"])

    def test_reload_config_switches_notifier(self):
        p = Path(self.tmp.name) / "config.toml"
        p.write_text(f'self_open_id = "{ME}"\nstate_dir = "{self.cfg.state_dir}"\n[notify]\nfile = "n.log"\n'
                     "[source]\nenabled = false\n", encoding="utf-8")
        from test_roster import FakeSource
        from sheepdog.engine import Collector
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(p)}):
            new = cli._reload_config(self.cfg, mock.Mock(dry_run=False), self.store, FakeSource([]),
                                     Collector(self.cfg, self.store, FakeSource([])), self.d)
        self.assertIsInstance(self.d.notifier, FileNotifier)
        self.assertEqual(self.d.notifier.path, Path(self.tmp.name) / "n.log")
        self.assertEqual(new.notify_path, Path(self.tmp.name) / "n.log")


if __name__ == "__main__":
    unittest.main()
