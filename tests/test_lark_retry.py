"""lark-cli 网络类错误（如 TLS handshake timeout）按退避重试；整轮失败时日志只记一行原因，traceback 只在 -v 时打。"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.source import SourceError
from sheepdog.source.lark import LarkCliSource

from test_roster import ME, Base

NET = {"ok": False, "error": {"type": "network", "subtype": "timeout",
                               "message": "Get \"https://open.feishu.cn/...\": net/http: TLS handshake timeout"}}
OK = {"ok": True, "data": {"has_more": False, "messages": [
    {"message_id": "om_test_r1", "chat_id": "oc_test_c", "chat_type": "p2p", "content": "x",
     "create_time": "2026-01-01 10:00", "sender": {"id": "ou_test_a", "sender_type": "user"}}]}}


def fake_cli(d: Path, outputs: list[dict]) -> Path:
    """依次返回 outputs 里的结果（用完重复最后一个），并记调用次数。"""
    (d / "outs.json").write_text(json.dumps(outputs), encoding="utf-8")
    (d / "n").write_text("0")
    script = d / "lark-cli"
    script.write_text(
        "#!/usr/bin/env python3\nimport json\n"
        f"outs = json.load(open({str(d / 'outs.json')!r}))\n"
        f"n = int(open({str(d / 'n')!r}).read()); open({str(d / 'n')!r}, 'w').write(str(n + 1))\n"
        "print(json.dumps(outs[min(n, len(outs) - 1)]))\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class RetryTest(unittest.TestCase):
    def fetch(self, outputs):
        with tempfile.TemporaryDirectory() as t:
            script = fake_cli(Path(t), outputs)
            with mock.patch("sheepdog.source.lark.time.sleep") as sleep:
                try:
                    got = LarkCliSource(binary=str(script)).fetch_since("2026-01-01T09:00:00+08:00", "2026-01-01T11:00:00+08:00")
                except SourceError as e:
                    got = e
            return got, [c.args[0] for c in sleep.call_args_list], int((Path(t) / "n").read_text())

    def test_network_timeout_twice_then_ok(self):
        got, sleeps, calls = self.fetch([NET, NET, OK])
        self.assertEqual([m.message_id for m in got], ["om_test_r1"])  # 本轮成功
        self.assertEqual((sleeps, calls), ([2.0, 4.0], 3))

    def test_subtype_timeout_also_retried(self):
        err = {"ok": False, "error": {"type": "api", "subtype": "gateway_timeout", "message": "x"}}
        got, sleeps, calls = self.fetch([err, OK])
        self.assertEqual((len(got), calls), (1, 2))

    def test_gives_up_after_retries_without_extra_sleep(self):
        got, sleeps, calls = self.fetch([NET])
        self.assertIsInstance(got, SourceError)
        self.assertIn("TLS handshake timeout", str(got))
        self.assertEqual((sleeps, calls), ([2.0, 4.0], 3))  # 最后一次失败后不再空等

    def test_other_errors_not_retried(self):
        got, sleeps, calls = self.fetch([{"ok": False, "error": {"type": "auth", "subtype": "token_expired", "message": "x"}}])
        self.assertIsInstance(got, SourceError)
        self.assertEqual((sleeps, calls), ([], 1))


class RoundErrorLogTest(Base):
    def run_once(self, *flags):
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsink = "dryrun"\n', encoding="utf-8")

        def boom(collector, dispatcher):
            raise SourceError("lark-cli 调用失败 ['im', '+messages-search']: network/timeout: TLS handshake timeout")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "_cycle", boom), \
                mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt), \
                self.assertLogs("root", "ERROR") as logs:
            with self.assertRaises(KeyboardInterrupt):
                cli.main([*flags, "run"])
        return [r for r in logs.records if "本轮失败" in r.getMessage()]

    def test_one_line_reason_without_traceback(self):
        rec = self.run_once()
        self.assertEqual(len(rec), 1)
        self.assertIn("SourceError: lark-cli 调用失败", rec[0].getMessage())
        self.assertIn("TLS handshake timeout", rec[0].getMessage())
        self.assertIsNone(rec[0].exc_info)

    def test_traceback_with_verbose(self):
        rec = self.run_once("-v")
        self.assertIsNotNone(rec[0].exc_info)
