"""7.55 上线测试闸门（scripts/release_gate.py）：在一个造出来的小仓库上跑真的 git archive + unittest，看它放行什么、拦什么。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_gate", REPO / "scripts" / "release_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

OK_TEST = '''import unittest


class OkTest(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)

    def test_b(self):
        """第二个。"""
        self.assertEqual(1 + 1, 2)
'''

REG = """# 回归

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 9.1 | 造的毛病 | `test_ok.OkTest.test_a` |
| 9.2 | 没有守卫的 | 缺 |
"""


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        self.git("init", "-q")
        env = mock.patch.dict(os.environ, {"SHEEPDOG_RELEASE_GATE_DIR": str(Path(self.tmp.name) / "gate")})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                               *args], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "x")
        return self.git("rev-parse", "HEAD")

    def run_gate(self, sha):
        with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = gate.run_gate(self.repo, sha, sys.executable)
        return rc, out.getvalue()

    def check(self, commit):
        with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = gate.check(self.repo, commit)
        return rc, out.getvalue()


class GateRunTest(GateBase):
    def test_green_writes_record_and_check_passes(self):
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": REG})
        rc, out = self.run_gate(sha[:8])
        self.assertEqual(rc, 0, out)
        rec = json.loads(gate.record_path(sha).read_text(encoding="utf-8"))
        self.assertEqual(rec["commit"], sha)
        self.assertEqual([r["tests"] for r in rec["runs"]], [2, 2, 2])
        self.assertTrue(all(r["ok"] and r["seconds"] >= 0 for r in rec["runs"]))
        self.assertEqual(rec["command"].split()[1:], ["-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual(rec["regressions"]["missing"], {})
        self.assertIn("created_at", rec)
        self.assertEqual(self.check(sha)[0], 0)
        self.assertEqual(self.check("HEAD")[0], 0)   # 提交号的简写、HEAD 都换成完整提交号再核

    def test_red_lists_failures_and_writes_nothing(self):
        bad = OK_TEST.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)")
        sha = self.commit({"tests/test_ok.py": bad, "tests/REGRESSIONS.md": REG})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("红：FAIL: test_ok.OkTest.test_b", out)
        self.assertFalse(gate.record_path(sha).exists())
        rc, out = self.check(sha)
        self.assertEqual(rc, 1)
        self.assertIn("没有闸门记录", out)

    def test_import_error_is_red(self):
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/test_broken.py": "import no_such_module_x\n",
                           "tests/REGRESSIONS.md": REG})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("test_broken", out)

    def test_flaky_second_run_is_red(self):
        """连跑 3 次要每次都绿：第二次才红的测试也拦住。"""
        counter = Path(self.tmp.name) / "count"
        flaky = OK_TEST + f'''

class FlakyTest(unittest.TestCase):
    def test_flaky(self):
        p = {str(counter)!r}
        import os
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        self.assertNotEqual(n, 1)
'''
        sha = self.commit({"tests/test_ok.py": flaky, "tests/REGRESSIONS.md": REG})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("第 1 次 ✓", out)
        self.assertIn("第 2 次 ✗", out)
        self.assertIn("第 3 次 ✓", out)
        self.assertFalse(gate.record_path(sha).exists())

    def test_regression_test_missing_is_red(self):
        reg = REG.replace("test_ok.OkTest.test_a", "test_ok.OkTest.test_gone")
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": reg})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("缺测试：test_ok.OkTest.test_gone", out)
        self.assertIn("回归登记 ✗ 1 个登记的测试不在或被跳过", out)
        self.assertFalse(gate.record_path(sha).exists())

    def test_skipped_regression_test_is_red(self):
        skipped = OK_TEST.replace("    def test_a(self):", "    @unittest.skip('x')\n    def test_a(self):")
        sha = self.commit({"tests/test_ok.py": skipped, "tests/REGRESSIONS.md": REG})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("被跳过", out)

    def test_row_without_test_or_que_is_red_and_no_file_is_red(self):
        reg = REG + "| 9.3 | 忘了写守卫 | 回头补 |\n"
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": reg})
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 1)
        self.assertIn("既没列测试也没写「缺」", out)
        self.git("rm", "-q", "tests/REGRESSIONS.md")
        self.git("commit", "-q", "-m", "y")
        rc, out = self.run_gate("HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("不在这个提交里", out)

    def test_exports_the_commit_not_the_worktree(self):
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": REG})
        (self.repo / "tests" / "test_ok.py").write_text(OK_TEST.replace("1 + 1, 2", "1 + 1, 3"), encoding="utf-8")
        (self.repo / "tests" / "test_dirty.py").write_text("import nope_x\n", encoding="utf-8")
        rc, out = self.run_gate(sha)
        self.assertEqual(rc, 0, out)

    def test_tests_run_isolated_from_real_home_and_code(self):
        """测试进程里：HOME、XDG、SHEEPDOG_* 都在临时目录；PYTHONPATH 只指导出副本；当前目录就是导出副本。"""
        real_home = str(Path.home())
        probe = f'''import os, unittest
from pathlib import Path


class EnvTest(unittest.TestCase):
    def test_env(self):
        home = os.environ["HOME"]
        self.assertNotEqual(home, {real_home!r})
        for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "SHEEPDOG_CONFIG", "SHEEPDOG_STATE_DIR",
                  "ANTIGRAVITY_APP_DATA_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            self.assertTrue(os.environ[k].startswith(home), k)
        self.assertEqual(Path(os.environ["PYTHONPATH"]).resolve(), Path.cwd().resolve())
        self.assertTrue((Path.cwd() / "tests" / "REGRESSIONS.md").is_file())
        self.assertNotIn(".git", os.listdir(Path.cwd()))
'''
        sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/test_env.py": probe, "tests/REGRESSIONS.md": REG})
        with mock.patch.dict(os.environ, {"SHEEPDOG_STATE_DIR": "/nonexistent/real-state",
                                          "PYTHONPATH": "/nonexistent/prod-code"}):
            rc, out = self.run_gate(sha)
        self.assertEqual(rc, 0, out)


class CheckTest(GateBase):
    def setUp(self):
        super().setUp()
        self.sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": REG})
        self.assertEqual(self.run_gate(self.sha)[0], 0)
        self.rec = json.loads(gate.record_path(self.sha).read_text(encoding="utf-8"))

    def tamper(self, **kw):
        rec = dict(self.rec, **kw)
        gate.record_path(self.sha).write_text(json.dumps(rec), encoding="utf-8")
        return self.check(self.sha)

    def test_tampered_records_fail(self):
        self.assertEqual(self.check(self.sha)[0], 0)
        self.assertEqual(self.tamper(commit="0" * 40)[0], 1)
        self.assertEqual(self.tamper(runs=self.rec["runs"][:2])[0], 1)
        self.assertEqual(self.tamper(runs=[dict(self.rec["runs"][0], ok=False)] + self.rec["runs"][1:])[0], 1)
        self.assertEqual(self.tamper(runs=[dict(self.rec["runs"][0], tests=0)] + self.rec["runs"][1:])[0], 1)
        self.assertEqual(self.tamper(regressions={"missing": {"test_ok.OkTest.test_a": "x"}})[0], 1)
        gate.record_path(self.sha).write_text("{坏的", encoding="utf-8")
        self.assertEqual(self.check(self.sha)[0], 1)

    def test_other_commit_has_no_record(self):
        other = self.commit({"README": "x"})
        rc, out = self.check(other)
        self.assertEqual(rc, 1)
        self.assertIn("没有闸门记录", out)
        self.assertEqual(self.check("no-such-ref")[0], 1)

    def test_cli_exit_codes(self):
        script = REPO / "scripts" / "release_gate.py"
        env = {**os.environ}
        ok = subprocess.run([sys.executable, str(script), "--repo", str(self.repo), "--check", self.sha], env=env,
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        gate.record_path(self.sha).unlink()
        bad = subprocess.run([sys.executable, str(script), "--repo", str(self.repo), "--check", self.sha], env=env,
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)


SPEC_REG = """# 回归

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 9.1 第 2 条 | 造的毛病 | `test_ok.OkTest.test_a` |
| 9.2 | 没有守卫的 | 缺 |
| 9.10 | 另一个编号，不算 9.1 | `test_ok.OkTest.test_b` |
"""


class SpecsCheckTest(GateBase):
    """回归 7.60：--check <提交> --specs 逐个核这次上的规格编号都登记了守它的测试、测试在那个提交里都在。"""

    def setUp(self):
        super().setUp()
        self.sha = self.commit({"tests/test_ok.py": OK_TEST, "tests/REGRESSIONS.md": SPEC_REG})
        self.assertEqual(self.run_gate(self.sha)[0], 0)

    def check_specs(self, commit, specs):
        with mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
            rc = gate.check(self.repo, commit, specs=specs, python=sys.executable)
        return rc, out.getvalue()

    def test_regression_760_registered_specs_pass(self):
        """回归 7.60：编号都登记了、测试都在才放行；「9.1 第 2 条」这种带后缀的行算 9.1。"""
        rc, out = self.check_specs(self.sha, ["9.1", "9.10"])
        self.assertEqual(rc, 0, out)
        self.assertIn("规格编号都登记了守它的测试：9.1、9.10", out)

    def test_regression_760_unregistered_spec_is_red(self):
        """回归 7.60：这次上的编号在 REGRESSIONS.md 里一行都没有，逐个点名，退出码 1。"""
        rc, out = self.check_specs(self.sha, ["9.1", "9.3", "9.4"])
        self.assertEqual(rc, 1, out)
        self.assertIn("9.3 没有登记", out)
        self.assertIn("9.4 没有登记", out)
        self.assertNotIn("9.1 没有登记", out)

    def test_regression_760_spec_number_matches_exactly(self):
        """回归 7.60：9.1 和 9.10 不混：只有 9.10 的行，9.1 不算登记了；反过来也一样。"""
        sha = self.commit({"tests/REGRESSIONS.md": SPEC_REG.replace("| 9.1 第 2 条 |", "| 9.11 |")})
        self.assertEqual(self.run_gate(sha)[0], 0)
        rc, out = self.check_specs(sha, ["9.1"])
        self.assertEqual(rc, 1, out)
        self.assertIn("9.1 没有登记", out)

    def test_regression_760_que_only_spec_is_red(self):
        """回归 7.60：只有写「缺」的行不算登记了守卫。"""
        rc, out = self.check_specs(self.sha, ["9.2"])
        self.assertEqual(rc, 1, out)
        self.assertIn("9.2 没有登记守它的测试：只有写「缺」的行", out)

    def test_regression_760_misspelled_test_is_red(self):
        """回归 7.60：登记的测试名写错（那个提交里没有），点名是哪个编号、哪个测试。"""
        reg = SPEC_REG + "| 9.5 | 名字写错了 | `test_ok.OkTest.test_a`、`test_ok.OkTest.test_typo` |\n"
        sha = self.commit({"tests/REGRESSIONS.md": reg})
        problems = gate.check_specs(self.repo, sha, ["9.5", "9.1"], sys.executable)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("9.5 登记的测试不存在：test_ok.OkTest.test_typo", problems[0])
        rc, out = self.check_specs(sha, ["9.5"])
        self.assertEqual(rc, 1, out)
        self.assertIn("test_ok.OkTest.test_typo", out)

    def test_regression_760_reads_the_commit_not_the_worktree(self):
        """回归 7.60：读的是那个提交里的 REGRESSIONS.md 和测试，工作树里没提交的登记、测试都不算。"""
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            SPEC_REG + "| 9.6 | 只在工作树里 | `test_new.NewTest.test_x` |\n", encoding="utf-8")
        (self.repo / "tests" / "test_new.py").write_text(
            "import unittest\n\n\nclass NewTest(unittest.TestCase):\n    def test_x(self):\n        pass\n",
            encoding="utf-8")
        rc, out = self.check_specs(self.sha, ["9.6"])
        self.assertEqual(rc, 1, out)
        self.assertIn("9.6 没有登记", out)
        # 登记提交了、测试没提交：测试不在那个提交里
        self.git("add", "tests/REGRESSIONS.md")
        self.git("commit", "-q", "-m", "reg only")
        sha2 = self.git("rev-parse", "HEAD")
        problems = gate.check_specs(self.repo, sha2, ["9.6"], sys.executable)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("9.6 登记的测试不存在：test_new.NewTest.test_x", problems[0])

    def test_regression_760_record_still_required(self):
        """回归 7.60：编号都登记了，但这个提交没过闸门（没有记录），照样退出 1。"""
        other = self.commit({"README": "x"})
        rc, out = self.check_specs(other, ["9.1"])
        self.assertEqual(rc, 1, out)
        self.assertIn("没有闸门记录", out)
        self.assertIn("规格编号都登记了守它的测试：9.1", out)

    def test_regression_760_cli(self):
        """回归 7.60：命令行 --specs：登记齐退出 0；缺一个退出非 0；--specs 不带 --check、编号写错都报参数错。"""
        script = REPO / "scripts" / "release_gate.py"

        def run(*args):
            return subprocess.run([sys.executable, str(script), "--repo", str(self.repo), "--python", sys.executable,
                                   *args], env={**os.environ}, capture_output=True, text=True)

        ok = run("--check", self.sha, "--specs", "9.1,9.10")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        bad = run("--check", self.sha, "--specs", "9.1, 9.7")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("9.7 没有登记", bad.stdout)
        self.assertEqual(run("--check", self.sha).returncode, 0)          # 不带 --specs：和 7.55 一样
        self.assertEqual(run("--specs", "9.1").returncode, 2)
        self.assertEqual(run("--check", self.sha, "--specs", "seven").returncode, 2)
        self.assertEqual(run("--check", self.sha, "--specs", "").returncode, 2)

    def test_parse_specs(self):
        self.assertEqual(gate.parse_specs("7.52,7.53，7.52 7.4"), ["7.52", "7.53", "7.4"])
        self.assertEqual(gate.spec_no("7.47 第 4 条返工"), "7.47")
        self.assertEqual(gate.spec_no("7.52上线后"), "7.52")
        self.assertIsNone(gate.spec_no("规格"))


class RegressionsFileTest(unittest.TestCase):
    """仓库自己的 tests/REGRESSIONS.md 认得出来：每行要么列了测试，要么写「缺」。"""

    def test_repo_regressions_parse(self):
        rows = gate.parse_regressions((REPO / "tests" / "REGRESSIONS.md").read_text(encoding="utf-8"))
        self.assertGreater(len(rows), 10)
        for r in rows:
            self.assertTrue(r["tests"] or r["missing_guard"], r)
        self.assertIn("7.54", {r["spec"] for r in rows})


if __name__ == "__main__":
    unittest.main()
