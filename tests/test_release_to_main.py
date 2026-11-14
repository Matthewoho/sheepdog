"""单点收敛发布脚本（scripts/release_to_main.py）回归测试（REG-168）。

覆盖：
1. 分支不改 __version__、在 tests/REGRESSIONS.md 末尾写 | NEXT | ...（REG-NEXT） | 占位行，由 release_to_main.py 在隔离 integration-wt 单点分配版本号与 REG-(N+1)、跑通 release_gate.py、快进 main、打 tag 并切换只读快照软链；
2. 两个分支并发基于同一旧基线在 tests/REGRESSIONS.md 末尾各自追加 | NEXT | ...（REG-NEXT） | 行，先后发版时自动合并无冲突并依次递增为 v1.1.78 (REG-168) 与 v1.1.79 (REG-169)；
3. 分支私改 __version__、硬编码版本号/REG-XXX 或删改历史回归行时当场拒绝；
4. 集成树闸门测试变红时拒绝合入，main 分支零污染。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_to_main", REPO / "scripts" / "release_to_main.py")
rtm = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(rtm)


BASE_INIT = re.sub(
    r'^__version__\s*=\s*["\'][^"\']+["\']',
    '__version__ = "1.1.77"',
    (REPO / "sheepdog" / "__init__.py").read_text(encoding="utf-8"),
    count=1,
    flags=re.M,
)

BASE_REG = """# 回归

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 1.1.77 | 基线守卫（REG-167） | `test_ok.OkTest.test_base` |
"""

BASE_TEST = """import unittest


class OkTest(unittest.TestCase):
    def test_base(self):
        self.assertEqual(1 + 1, 2)
"""


class ReleaseToMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.gate_dir = self.root / "gate"
        self.releases_dir = self.root / "releases"
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

        # 拷贝真实的 release_gate.py 和 check_private_data.py 到测试小仓库
        (self.repo / "scripts").mkdir(parents=True)
        for sname in ("release_gate.py", "check_private_data.py", "release_to_main.py"):
            src_file = REPO / "scripts" / sname
            if src_file.is_file():
                (self.repo / "scripts" / sname).write_text(
                    src_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
        (self.repo / "sheepdog").mkdir(parents=True)
        (self.repo / "sheepdog" / "__init__.py").write_text(BASE_INIT, encoding="utf-8")
        (self.repo / "sheepdog" / "version_watch.py").write_text(
            (REPO / "sheepdog" / "version_watch.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "REGRESSIONS.md").write_text(BASE_REG, encoding="utf-8")
        (self.repo / "tests" / "test_ok.py").write_text(BASE_TEST, encoding="utf-8")

        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init v1.1.77 (REG-167)")
        self.git("tag", "v1.1.77")
        self.base_sha = self.git("rev-parse", "HEAD")

        env = mock.patch.dict(
            os.environ,
            {
                "SHEEPDOG_RELEASE_GATE_DIR": str(self.gate_dir),
                "SHEEPDOG_RELEASES_DIR": str(self.releases_dir),
                "SHEEPDOG_ENFORCE_RELEASE_GATE": "1",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_single_and_concurrent_two_branch_release_assigns_sequential_versions_and_regs(self):
        # 两个分支同时从 v1.1.77 (REG-167) 切出，各自追加 | NEXT | ...（REG-NEXT） | 行
        self.git("checkout", "-q", "-b", "feat/alpha", self.base_sha)
        (self.repo / "tests" / "test_ok.py").write_text(
            BASE_TEST + "\n    def test_alpha(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            BASE_REG + "| NEXT | Alpha 修复（REG-NEXT） | `test_ok.OkTest.test_alpha` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feat alpha")

        self.git("checkout", "-q", "-b", "feat/beta", self.base_sha)
        (self.repo / "tests" / "test_beta.py").write_text(
            "import unittest\n\nclass BetaTest(unittest.TestCase):\n    def test_beta(self):\n        self.assertEqual(2 * 2, 4)\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            BASE_REG + "| NEXT | Beta 修复（REG-NEXT） | `test_beta.BetaTest.test_beta` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feat beta")

        # 切回 main 保持主仓干净在 main
        self.git("checkout", "-q", "main")

        # 1) 先发 feat/alpha -> 应自动分配 v1.1.78 与 REG-168
        res1 = rtm.release_branch_to_main(
            self.repo,
            "feat/alpha",
            python=sys.executable,
            no_push=True,
            no_upgrade=True,
        )
        self.assertEqual(res1["version"], "1.1.78")
        self.assertEqual(res1["assigned_regs"], ["REG-168"])
        self.assertEqual(self.git("rev-parse", "v1.1.78^{commit}"), res1["commit"])
        self.assertTrue((self.gate_dir / f"{res1['commit']}.json").is_file())
        self.assertEqual((self.releases_dir / "current").resolve(), Path(res1["snapshot"]["snapshot_dir"]).resolve())

        # 2) 再发并发分支 feat/beta -> 自动消解 REGRESSIONS.md 尾行追加冲突，分配 v1.1.79 与 REG-169
        res2 = rtm.release_branch_to_main(
            self.repo,
            "feat/beta",
            python=sys.executable,
            no_push=True,
            no_upgrade=True,
        )
        self.assertEqual(res2["version"], "1.1.79")
        self.assertEqual(res2["assigned_regs"], ["REG-169"])
        self.assertEqual(self.git("rev-parse", "main"), res2["commit"])
        self.assertEqual(self.git("rev-parse", "v1.1.79^{commit}"), res2["commit"])
        reg_text = (self.repo / "tests" / "REGRESSIONS.md").read_text(encoding="utf-8")
        self.assertIn("| 1.1.78 | Alpha 修复（REG-168） | `test_ok.OkTest.test_alpha` |", reg_text)
        self.assertIn("| 1.1.79 | Beta 修复（REG-169） | `test_beta.BetaTest.test_beta` |", reg_text)
        self.assertNotIn("NEXT", reg_text)
        self.assertIn('__version__ = "1.1.79"', (self.repo / "sheepdog" / "__init__.py").read_text(encoding="utf-8"))

    def test_branch_touching_version_or_hardcoding_reg_is_rejected(self):
        # 分支私自改 __version__
        self.git("checkout", "-q", "-b", "feat/bad-ver", self.base_sha)
        (self.repo / "sheepdog" / "__init__.py").write_text(
            BASE_INIT.replace("1.1.77", "1.1.78"), encoding="utf-8"
        )
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            BASE_REG + "| NEXT | 违规改版本（REG-NEXT） | `test_ok.OkTest.test_base` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "bad ver")
        self.git("checkout", "-q", "main")

        with self.assertRaises(RuntimeError) as cm1:
            rtm.release_branch_to_main(self.repo, "feat/bad-ver", python=sys.executable, no_push=True, no_upgrade=True)
        self.assertIn("不得直接修改 sheepdog/__init__.py 的 __version__", str(cm1.exception))

        # 分支硬编码 REG-168 / 1.1.78
        self.git("checkout", "-q", "-b", "feat/hardcode", self.base_sha)
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            BASE_REG + "| 1.1.78 | 硬编码编号（REG-168） | `test_ok.OkTest.test_base` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "hardcode")
        self.git("checkout", "-q", "main")

        with self.assertRaises(RuntimeError) as cm2:
            rtm.release_branch_to_main(self.repo, "feat/hardcode", python=sys.executable, no_push=True, no_upgrade=True)
        self.assertIn("第一列必须写 NEXT", str(cm2.exception))

    def test_failed_gate_leaves_main_untouched(self):
        self.git("checkout", "-q", "-b", "feat/broken", self.base_sha)
        (self.repo / "tests" / "test_ok.py").write_text(
            BASE_TEST + "\n    def test_broken(self):\n        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            BASE_REG + "| NEXT | 坏测试（REG-NEXT） | `test_ok.OkTest.test_broken` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "broken")
        self.git("checkout", "-q", "main")

        main_before = self.git("rev-parse", "main")
        with self.assertRaises(RuntimeError) as cm:
            rtm.release_branch_to_main(
                self.repo,
                "feat/broken",
                python=sys.executable,
                no_push=True,
                no_upgrade=True,
            )
        self.assertIn("未通过 release_gate", str(cm.exception))
        self.assertEqual(self.git("rev-parse", "main"), main_before)


if __name__ == "__main__":
    unittest.main()
