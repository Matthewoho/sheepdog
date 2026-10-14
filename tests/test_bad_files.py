"""7.40 配套：常驻进程跑着时把配置 / 名册 / 安全规则 / playbook 改坏了（语法错、读不了、不是 UTF-8、结构不对），
这一轮照常拉取投递、用上一份并 WARNING（名册、安全规则是 ERROR），改好后下一轮用新的。启动时（没有上一份）配置就坏了：
照旧报错退出，stderr 一行带时间、文件和行号。Stop hook 遇到坏配置什么都不输出。"""

import io
import itertools
import os
import shutil
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.config import ConfigError, load_config
from sheepdog.playbook import Playbook

from test_roster import FIXTURES, ME, Base

BAD_UTF8 = b'self_open_id = "\xff\xfe"\n'


class ResidentReloadBadFilesTest(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(self.tmp.name)
        self.cfg_file = self.dir / "config.toml"
        self.base = f'self_open_id = "{ME}"\nsink = "dryrun"\n'
        self.env = {"SHEEPDOG_CONFIG": str(self.cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name,
                    "XDG_CONFIG_HOME": str(self.dir / "xdg")}

    def run_rounds(self, first, then) -> tuple[list, list]:
        """常驻进程 cli.main(["run"])：first() 写好第 1 轮的文件；每轮睡觉时按顺序调 then 里的下一个改文件，用完就停。
        _cycle（这一轮的拉取 + 投递）换成记录：这一轮跑了没有、手上用的是哪份配置 / 名册 / 规则。"""
        first()
        seen = []

        def fake_cycle(collector, dispatcher):
            seen.append(SimpleRound(collector, dispatcher))

        steps = iter(then)

        def fake_sleep(_):
            try:
                next(steps)()
            except StopIteration:
                raise KeyboardInterrupt

        with mock.patch.dict(os.environ, self.env), mock.patch.object(cli, "_cycle", fake_cycle), \
                mock.patch.object(cli.time, "sleep", side_effect=fake_sleep), \
                self.assertLogs("root", "WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["run"])
        return seen, logs.output

    def cfg_text(self, text: str):
        return lambda: self.cfg_file.write_text(text, encoding="utf-8")

    def cfg_bytes(self, b: bytes):
        return lambda: self.cfg_file.write_bytes(b)

    def test_config_broken_keeps_last_and_round_still_runs(self):
        good = lambda k: self.base + f'[routing]\nkeywords = ["{k}"]\n'   # noqa: E731
        seen, logs = self.run_rounds(self.cfg_text(good("fxA")), [
            self.cfg_text(self.base + '[routing\nkeywords = ["fxB"]\n'),   # 第 2 轮：语法错（第 3 行少了 ]）
            self.cfg_bytes(BAD_UTF8),                                      # 第 3 轮：不是 UTF-8
            self.cfg_text(self.base + "session = 1\n"),                    # 第 4 轮：某一节写成了单个值
            self.cfg_text(good("fxC")),                                    # 第 5 轮：改好了
        ])
        # 每一轮都照常跑了（5 轮 5 次 _cycle），坏的那几轮用的是上一份
        self.assertEqual([r.keywords for r in seen], [["fxA"], ["fxA"], ["fxA"], ["fxA"], ["fxC"]])
        self.assertTrue(all(r.same_cfg for r in seen))
        warns = [line for line in logs if "配置无效，沿用上一份" in line]
        self.assertEqual(len(warns), 3, logs)
        self.assertTrue(all(line.startswith("WARNING") for line in warns))
        self.assertIn(f"{self.cfg_file} 第 3 行第 9 列 TOML 语法错误", warns[0])
        self.assertIn(f"{self.cfg_file} 读不了", warns[1])
        self.assertIn(f"{self.cfg_file} 结构不对", warns[2])

    def test_unexpected_error_while_reading_config_keeps_last(self):
        # 兜底：load_config 漏网的别的异常（不是 ConfigError）也沿用上一份，这一轮照常跑
        real = cli.load_config
        # 第 1 次是启动时的 _build，第 2 次是第 1 轮的重读（出意外），之后都正常
        calls = itertools.chain([real, mock.Mock(side_effect=RuntimeError("意外"))], itertools.repeat(real))
        self.cfg_file.write_text(self.base + '[routing]\nkeywords = ["fxA"]\n', encoding="utf-8")
        with mock.patch.object(cli, "load_config", side_effect=lambda *a: next(calls)(*a)):
            seen, logs = self.run_rounds(lambda: None, [lambda: None])
        self.assertEqual([r.keywords for r in seen], [["fxA"], ["fxA"]])
        self.assertTrue(any("配置无效，沿用上一份（读配置出错 RuntimeError）" in line for line in logs))

    def test_roster_and_security_broken_keep_last(self):
        roster, security = self.dir / "roster.toml", self.dir / "security.toml"

        def first():
            self.cfg_file.write_text(self.base, encoding="utf-8")
            roster.write_text("", encoding="utf-8")
            security.write_text("", encoding="utf-8")

        def both(r, s):
            return lambda: (roster.write_bytes(r), security.write_bytes(s))

        seen, logs = self.run_rounds(first, [
            both(b"[[session]\n", b""),        # 第 2 轮：名册语法错
            both(BAD_UTF8, b"[rule\n"),        # 第 3 轮：名册不是 UTF-8，规则语法错
            both(b"", BAD_UTF8),               # 第 4 轮：名册改好了，规则不是 UTF-8
            both(b"", b""),                    # 第 5 轮：都好了
        ])
        self.assertEqual(len(seen), 5)
        # 每轮读成功就换一份新对象；坏了的那轮手上还是上一轮那份
        kept = lambda attr: [getattr(b, attr) is getattr(a, attr) for a, b in zip(seen, seen[1:])]   # noqa: E731
        self.assertEqual(kept("roster"), [True, True, False, False])      # 第 2、3 轮名册坏了
        self.assertEqual(kept("security"), [False, True, True, False])    # 第 3、4 轮规则坏了
        self.assertTrue(all(r.same_roster and r.same_security for r in seen))
        roster_err = [line for line in logs if "名册无效，沿用上一份" in line]
        sec_err = [line for line in logs if "安全规则无效，沿用上一份" in line]
        self.assertEqual(len(roster_err), 2, logs)
        self.assertIn("TOML 解析失败", roster_err[0])
        self.assertIn("line 1", roster_err[0])
        self.assertIn("名册读不了", roster_err[1])
        self.assertEqual(len(sec_err), 2, logs)
        self.assertIn("TOML 解析失败", sec_err[0])
        self.assertIn("读不了", sec_err[1])


class SimpleRound:
    def __init__(self, collector, dispatcher):
        self.keywords = list(collector.cfg.routing.keywords)
        self.same_cfg = collector.cfg is dispatcher.cfg
        self.roster, self.security = collector.roster, collector.security
        self.same_roster = collector.roster is dispatcher.roster
        self.same_security = collector.security is dispatcher.security


class StartupBadConfigTest(Base):
    def test_startup_exits_with_file_and_line(self):
        # 第一次读就坏了（没有上一份）：照旧报错退出 2；sidecar 跑的 init 和 run 都一样，stderr 一行带时间、文件、行号
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n[hooks\nstop = false\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name,
               "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg")}
        for cmd in ("init", "run"):
            err = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch("sys.stderr", err):
                self.assertEqual(cli.main([cmd]), 2, cmd)
            line = err.getvalue().strip()
            self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ERROR 配置无效: ")
            self.assertIn(f"{cfg_file} 第 2 行第 7 列 TOML 语法错误", line)

    def test_load_config_wraps_every_kind_of_breakage(self):
        p = Path(self.tmp.name) / "c.toml"
        for content in (b"[hooks\n", BAD_UTF8, b"session = 1\n", b"hooks = true\n", b"state_dir = 1\n"):
            p.write_bytes(content)
            with self.assertRaises(ConfigError, msg=content):
                load_config(p)
        p.unlink()
        p.mkdir()                                   # 路径是个目录：读不了
        with self.assertRaises(ConfigError):
            load_config(p)


class PlaybookBadFileTest(Base):
    def setUp(self):
        super().setUp()
        self.pb_dir = Path(self.tmp.name) / "playbook"
        shutil.copytree(FIXTURES, self.pb_dir)
        self.cfg.playbook_dir = str(self.pb_dir)

    def test_unreadable_file_keeps_last_good(self):
        pb = Playbook(self.pb_dir)
        f = self.pb_dir / "x_switch_test.md"
        f.write_text("第一版", encoding="utf-8")
        self.assertEqual(pb.read("x_switch_test.md"), "第一版")
        f.write_bytes(b"\xff\xfe")
        with self.assertLogs("sheepdog.playbook", "WARNING") as logs:
            self.assertEqual(pb.read("x_switch_test.md"), "第一版")          # 不是 UTF-8：沿用上一份
            f.unlink()
            f.mkdir()
            self.assertEqual(Playbook(self.pb_dir).read("x_switch_test.md"), "第一版")   # 读不了（重建对象也沿用）
        self.assertTrue(all("沿用上一份" in line for line in logs.output))
        f.rmdir()
        self.assertEqual(pb.read("x_switch_test.md"), "")                    # 删掉 = 用默认话（原来的行为）
        f.write_text("第二版", encoding="utf-8")
        self.assertEqual(pb.read("x_switch_test.md"), "第二版")              # 改好了下一次就用新的

    def test_never_good_file_falls_back_to_default(self):
        (self.pb_dir / "y_switch_test.md").write_bytes(b"\xff")
        with self.assertLogs("sheepdog.playbook", "WARNING") as logs:
            self.assertEqual(Playbook(self.pb_dir).render_or("y_switch_test.md", "默认话"), "默认话")
        self.assertIn("这一段先用默认话", logs.output[0])

    def test_round_survives_a_broken_playbook(self):
        # 组装 prompt 时 common.md 不是 UTF-8：这一轮照常投递，不抛出去让整轮失败
        (self.pb_dir / "common.md").write_bytes(b"\xff\xfe")
        d = self.disp()
        with self.assertLogs("sheepdog.playbook", "WARNING"):
            d.init()
            d.dispatch_once()
        self.assertTrue(self.sink.to("conv_test_alpha"))


class PromptOverlayBadFileTest(Base):
    def setUp(self):
        super().setUp()
        self.f = Path(self.tmp.name) / "overlay.md"
        self.cfg.prompt_overlay_path = str(self.f)

    def test_unreadable_overlay_keeps_last_good(self):
        self.f.write_text("第一版 overlay", encoding="utf-8")
        self.assertEqual(self.cfg.prompt_overlay(), "第一版 overlay")
        with self.assertLogs("sheepdog.config", "WARNING") as logs:
            self.f.write_bytes(b"\xff\xfe")
            self.assertEqual(self.cfg.prompt_overlay(), "第一版 overlay")       # 不是 UTF-8：沿用上一份
            self.f.unlink()
            self.f.mkdir()
            self.assertEqual(self.cfg.prompt_overlay(), "第一版 overlay")       # 读不了：沿用上一份
        self.assertTrue(all("沿用上一份" in line for line in logs.output))
        self.f.rmdir()
        self.assertEqual(self.cfg.prompt_overlay(), "")                         # 删掉 = 没有 overlay（原来的行为）
        self.f.write_text("第二版", encoding="utf-8")
        self.assertEqual(self.cfg.prompt_overlay(), "第二版")                   # 改好了下一次就用新的

    def test_never_good_overlay_is_empty(self):
        self.f.write_bytes(b"\xff")
        with self.assertLogs("sheepdog.config", "WARNING") as logs:
            self.assertEqual(self.cfg.prompt_overlay(), "")
        self.assertIn("这一轮先当没有", logs.output[0])

    def test_round_survives_a_broken_overlay(self):
        # 组装 prompt 时 overlay 不是 UTF-8：这一轮照常投递（onboarding、批次），不抛出去让整轮失败
        self.f.write_bytes(b"\xff\xfe")
        d = self.disp()
        with self.assertLogs("sheepdog.config", "WARNING"):
            d.init()
            d.dispatch_once()
        self.assertTrue(self.sink.to("conv_test_alpha"))
