"""最后好用的配置（第一期 B 部分发现的问题第 1 条）：配置写坏的那段时间，会话侧命令（每次都是新进程，没有「上一份」）
用常驻进程存下的那份照常执行，stderr 写一行；从没存过照旧报错；改好后不再提示。run / init / doctor / hook stop 不回退。"""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli, hook, lastgood

from test_stop_hook import stop_input
from test_threads import ThreadsBase

BROKEN = "[routing\nkeywords = [\"x\"]\n"


class LastGoodTest(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.dir = Path(self.tmp.name)
        self.good = 'sink = "dryrun"\n' + self.cfg_file.read_text(encoding="utf-8")
        self.cfg_file.write_text(self.good, encoding="utf-8")
        self.roster_file = self.dir / "roster.toml"
        self.security_file = self.dir / "security.toml"
        self.env = {"SHEEPDOG_CONFIG": str(self.cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name,
                    "XDG_CONFIG_HOME": str(self.dir / "xdg")}

    def resident_round(self):
        """常驻进程起来、跑一轮（_cycle 换成空的），存下最后好用的那份。"""
        with mock.patch.dict(os.environ, self.env), mock.patch.object(cli, "_cycle", lambda c, d: None), \
                mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt), self.assertLogs("root", "INFO"):
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["run"])

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), redirect_stdout(out), mock.patch("sys.stderr", err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def report(self, text="进度"):
        return self.run_cli("report", "--topic", "tp_alpha", "--title", "汇报", "--text", text)

    def receipt(self):
        b = self.store.get_topic("tp_alpha")["pending_batch_id"] or "b_test_receipt"
        return self.run_cli("receipt", "--topic", "tp_alpha", "--batch", b, "--json",
                            json.dumps({"status": "handled", "summary": "ok"}))

    def test_session_commands_use_the_last_good_config_while_it_is_broken(self):
        self.resident_round()
        saved = self.dir / lastgood.DIR_NAME / lastgood.CONFIG
        self.assertEqual(saved.read_text(encoding="utf-8"), self.good)
        self.cfg_file.write_text(BROKEN, encoding="utf-8")
        for name, (rc, _, err) in (("report", self.report()), ("receipt", self.receipt())):
            self.assertEqual(rc, 0, (name, err))
            self.assertIn(f"配置无效（{self.cfg_file} 第 1 行第 9 列 TOML 语法错误", err, name)
            self.assertIn(f"这次沿用 ", err)
            self.assertIn(f"最后好用的那份（{saved}）", err)
            self.assertEqual(err.count("配置无效"), 1)
        # 改好后不再提示
        self.cfg_file.write_text(self.good, encoding="utf-8")
        rc, _, err = self.report("再一次")
        self.assertEqual((rc, err), (0, ""))

    def test_never_saved_still_fails(self):
        self.cfg_file.write_text(BROKEN, encoding="utf-8")
        rc, _, err = self.report()
        self.assertEqual(rc, 2)
        self.assertIn("ERROR 配置无效", err)
        self.assertNotIn("沿用", err)

    def test_resident_commands_do_not_fall_back(self):
        # init（sidecar 启动时先跑它）照旧报错退出；doctor 说清楚会话命令在用哪一份
        self.resident_round()
        self.cfg_file.write_text(BROKEN, encoding="utf-8")
        rc, _, err = self.run_cli("init")
        self.assertEqual(rc, 2)
        self.assertNotIn("沿用", err)
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("配置文件: ✗ 当前无效", out)
        self.assertIn("会话命令在用 ", out)
        self.assertIn(str(self.dir / lastgood.DIR_NAME / lastgood.CONFIG), out)

    def test_doctor_without_a_saved_copy(self):
        self.cfg_file.write_text(BROKEN, encoding="utf-8")
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 2)
        self.assertIn("没有最后好用的那份", out)

    def test_hook_stop_never_uses_the_saved_copy(self):
        self.resident_round()
        self.cfg_file.write_text(BROKEN, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(hook.stop_main(io.BytesIO(stop_input().encode("utf-8")), out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_roster_and_security_fall_back_too(self):
        self.good += "[source]\nenabled = false\n"                       # inject 只在测试环境（不拉 IM）可用
        self.cfg_file.write_text(self.good, encoding="utf-8")
        self.roster_file.write_text("", encoding="utf-8")
        self.security_file.write_text("", encoding="utf-8")
        self.resident_round()
        self.roster_file.write_text("[[session]\n", encoding="utf-8")
        self.security_file.write_bytes(b"\xff\xfe")
        rc, out, err = self.run_cli("inject", "--text", "测试")
        self.assertEqual(rc, 0, err)
        self.assertIn("名册无效（", err)
        self.assertIn("安全规则无效（", err)
        self.assertIn(str(self.dir / lastgood.DIR_NAME / lastgood.ROSTER), err)
        rc, out, _ = self.run_cli("doctor")
        self.assertIn("名册: ✗", out)
        self.assertEqual(out.count("会话命令在用 "), 2)

    def test_custom_state_dir_written_in_the_config(self):
        # 配置里自己写了 state_dir：坏掉的原文里还认得出这一行，就到那里找
        other = self.dir / "elsewhere"
        self.good = f'state_dir = "{other}"\n' + self.good
        self.cfg_file.write_text(self.good, encoding="utf-8")
        self.env["SHEEPDOG_STATE_DIR"] = str(self.dir / "default_state")
        self.resident_round()
        self.assertTrue((other / lastgood.DIR_NAME / lastgood.CONFIG).is_file())
        self.cfg_file.write_text(f'state_dir = "{other}"\n' + BROKEN, encoding="utf-8")
        rc, _, err = self.run_cli("receipt", "--topic", "tp_alpha", "--batch", "b_test_x", "--json",
                                  json.dumps({"status": "handled", "summary": "ok"}))
        self.assertIn(f"最后好用的那份（{other / lastgood.DIR_NAME / lastgood.CONFIG}）", err)

    def test_save_skips_unchanged_and_broken_content(self):
        with mock.patch.dict(os.environ, self.env):
            cfg = cli.load_config()
            self.assertIn(lastgood.CONFIG, lastgood.save(cfg))
            self.assertEqual(lastgood.save(cfg), [])                       # 内容没变不重写
            self.cfg_file.write_text(BROKEN, encoding="utf-8")             # 读和存之间被改坏：不存
            self.assertEqual(lastgood.save(cfg, (lastgood.CONFIG,)), [])
        self.assertEqual((self.dir / lastgood.DIR_NAME / lastgood.CONFIG).read_text(encoding="utf-8"), self.good)

    def test_each_successful_reload_updates_the_saved_copy(self):
        # 每轮重读成功就更新那份（内容变了才写）；写坏的那轮不动它
        b = self.good + "[watch]\nexpire_minutes = 90\n"
        steps = iter([lambda: self.cfg_file.write_text(b, encoding="utf-8"),
                      lambda: self.cfg_file.write_text(BROKEN, encoding="utf-8")])

        def fake_sleep(_):
            try:
                next(steps)()
            except StopIteration:
                raise KeyboardInterrupt

        with mock.patch.dict(os.environ, self.env), mock.patch.object(cli, "_cycle", lambda c, d: None), \
                mock.patch.object(cli.time, "sleep", side_effect=fake_sleep), self.assertLogs("root", "INFO"):
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["run"])
        self.assertEqual((self.dir / lastgood.DIR_NAME / lastgood.CONFIG).read_text(encoding="utf-8"), b)
