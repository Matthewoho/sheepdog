"""发版闸门、只读快照与启动降级兜底回归单测（REG-168）。"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sheepdog import cli, default_repo_dir, get_build_info, version_info, version_watch
from sheepdog.config import Config
from sheepdog.engine import Dispatcher
from sheepdog.roster import Roster
from sheepdog.security import SecurityConfig
from sheepdog.sink.agentapi import DryRunSink
from sheepdog.store import Store


def _write_gate_record(gate_dir: Path, sha: str, *, ok: bool = True, tests_count: int = 12) -> Path:
    gate_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "commit": sha,
        "created_at": "2026-02-26T14:30:00+08:00",
        "command": "python3.12 -m unittest discover -s tests",
        "python": "3.12.14",
        "host": "test-host",
        "runs": [
            {"tests": tests_count, "seconds": 1.0, "reported_seconds": 1.0, "ok": ok},
            {"tests": tests_count, "seconds": 1.0, "reported_seconds": 1.0, "ok": ok},
            {"tests": tests_count, "seconds": 1.0, "reported_seconds": 1.0, "ok": ok},
        ],
        "regressions": {"rows": 5, "tests": tests_count, "missing": {}, "no_guard": []},
        "ledger_compat": {"ok": True, "skipped": False, "tables": 20, "errors": []},
    }
    p = gate_dir / f"{sha}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


class VersionWatchGateAndSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.gate_dir = self.root / "gate"
        self.releases_dir = self.root / "releases"
        self.state_dir = self.root / "state"
        (self.repo / "sheepdog").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)
        self.addCleanup(lambda: version_watch._make_tree_writable(self.releases_dir))

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "SHEEPDOG_RELEASE_GATE_DIR": str(self.gate_dir),
                "SHEEPDOG_RELEASES_DIR": str(self.releases_dir),
                "SHEEPDOG_ENFORCE_RELEASE_GATE": "1",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.git("init", "-q", "-b", "main")
        self.sha_v1 = self._commit_version("1.1.76", "feat: release v1.1.76")
        self.git("tag", "v1.1.76", self.sha_v1)

        self.cfg = Config(self_open_id="ou_test_owner", state_dir=self.state_dir, sink="dryrun")
        self.cfg.source.enabled = False
        self.cfg.identity.enabled = False
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)

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

    def _commit_version(self, ver: str, msg: str) -> str:
        (self.repo / "sheepdog" / "__init__.py").write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
        (self.repo / "tests" / "REGRESSIONS.md").write_text(
            f"# 回归\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n| {ver} | 守卫（REG-001） | `t.A.test_x` |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def _make_dispatcher(self, notices: list[tuple[str, str]]) -> Dispatcher:
        disp = Dispatcher(self.cfg, self.store, DryRunSink(), Roster(), SecurityConfig())
        disp.runtime_info = {
            "version": "1.1.76",
            "git_sha": self.sha_v1,
            "display": f"1.1.76 ({self.sha_v1[:7]})",
            "pid": 11111,
        }
        disp._notify_owner = lambda text, what, topic_id="", **kw: (notices.append((what, text)) or True)
        return disp

    def test_upgrade_rejects_dirty_worktree(self) -> None:
        """工作区存在未提交修改或未跟踪文件（dirty）时，execute_upgrade 与 cmd_upgrade 必须直接拒绝升级。"""
        _write_gate_record(self.gate_dir, self.sha_v1)
        # 制造未跟踪脏文件（复现 顺手带入未审文件场景）
        untracked = self.repo / "tests" / "test_untracked_wip.py"
        untracked.write_text("# wip\n", encoding="utf-8")

        notices: list[tuple[str, str]] = []
        disp = self._make_dispatcher(notices)
        res = version_watch.execute_upgrade(disp, no_pull=True, repo_dir=self.repo)
        self.assertTrue(res.get("rejected"))
        self.assertEqual(res.get("reject_reason"), "dirty_worktree")
        self.assertFalse(res.get("restart_requested"))
        self.assertFalse(disp.restart_requested)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][0], "升级拦截")
        self.assertIn("拒绝升级", notices[0][1])

        err_buf = io.StringIO()
        with mock.patch("sheepdog.cli._load_cfg", return_value=self.cfg), mock.patch("sys.stderr", err_buf):
            rc = cli.cmd_upgrade(
                type("Args", (), {"no_pull": True, "dry_run": False, "rollback": False, "beta": False, "to": "", "notes": ""})(),
                repo_dir=self.repo,
            )
        self.assertEqual(rc, 1)
        self.assertIn("拒绝升级：工作区有未提交或未跟踪改动", err_buf.getvalue())

    def test_upgrade_rejects_ungated_commit_and_installs_readonly_snapshot_when_gated(self) -> None:
        """工作区干净但 HEAD 缺少有效 release_gate 记录时拒绝升级；补齐过闸记录后放行并生成只读快照与 current 软链。"""
        sha_v2 = self._commit_version("1.1.77", "feat: release v1.1.77")
        self.git("tag", "v1.1.77", sha_v2)

        notices: list[tuple[str, str]] = []
        disp = self._make_dispatcher(notices)
        res_ungated = version_watch.execute_upgrade(disp, no_pull=True, repo_dir=self.repo)
        self.assertTrue(res_ungated.get("rejected"))
        self.assertEqual(res_ungated.get("reject_reason"), "release_gate_failed")
        self.assertFalse(disp.restart_requested)
        self.assertEqual(notices[-1][0], "升级拦截")
        self.assertIn("未通过上线测试闸门", notices[-1][1])

        err_buf = io.StringIO()
        with mock.patch("sheepdog.cli._load_cfg", return_value=self.cfg), mock.patch("sys.stderr", err_buf):
            rc_ungated = cli.cmd_upgrade(
                type("Args", (), {"no_pull": True, "dry_run": False, "rollback": False, "beta": False, "to": "", "notes": ""})(),
                repo_dir=self.repo,
            )
        self.assertEqual(rc_ungated, 1)
        self.assertIn("拒绝升级", err_buf.getvalue())
        self.assertIn("未通过上线测试闸门", err_buf.getvalue())

        # 写入有效闸门记录后再次升级：应成功并生成只读快照 + current 软链
        _write_gate_record(self.gate_dir, sha_v2)
        notices.clear()
        res_ok = version_watch.execute_upgrade(disp, no_pull=True, repo_dir=self.repo)
        self.assertFalse(res_ok.get("rejected", False))
        self.assertTrue(res_ok.get("restart_requested"))
        self.assertTrue(disp.restart_requested)

        current_link = self.releases_dir / "current"
        self.assertTrue(current_link.is_symlink())
        snap_dir = current_link.resolve()
        self.assertEqual(snap_dir.name, f"v1.1.77-{sha_v2[:7]}")
        bi = get_build_info(snap_dir / "sheepdog")
        self.assertIsNotNone(bi)
        self.assertEqual(bi["version"], "1.1.77")
        self.assertEqual(bi["git_sha"], sha_v2)
        # 验证快照文件为只读（无写权限）
        init_mode = (snap_dir / "sheepdog" / "__init__.py").stat().st_mode
        self.assertEqual(init_mode & stat.S_IWUSR, 0)

        # 验证从快照读取 version_info 时不受主工作树后续脏改动影响
        (self.repo / "sheepdog" / "__init__.py").write_text('__version__ = "9.9.99"\n', encoding="utf-8")
        snap_vinfo = version_info(snap_dir, prefer_disk_version=False)
        self.assertEqual(snap_vinfo["version"], "1.1.77")
        self.assertEqual(snap_vinfo["git_sha"], sha_v2)
        self.assertFalse(snap_vinfo["dirty"])
        self.git("checkout", "--", "sheepdog/__init__.py")

    def test_startup_gate_falls_back_to_latest_gated_snapshot_and_rejects_only_when_none_exist(self) -> None:
        """启动校验未过时：若存在历史过闸版本，自动降级切到最近一个过闸快照启动并飞书告警；一个都没有时才拒绝启动。"""
        sha_v2 = self._commit_version("1.1.77", "feat: release v1.1.77")
        self.git("tag", "v1.1.77", sha_v2)

        # 场景 A：没有任何过闸记录时 -> 拒绝启动 (ok=False, status='rejected')
        notices: list[tuple[str, str]] = []
        disp = self._make_dispatcher(notices)
        res_none = version_watch.ensure_startup_release_gate(
            self.repo, store=self.store, dispatcher=disp, gate_dir=self.gate_dir, releases_dir=self.releases_dir
        )
        self.assertFalse(res_none["ok"])
        self.assertEqual(res_none["status"], "rejected")
        self.assertEqual(notices[-1][0], "启动闸门拦截告警")
        self.assertIn("没有任何可用的历史过闸记录或快照，已拒绝启动", notices[-1][1])

        # 场景 B：v1.1.76 (sha_v1) 有过闸记录，但当前 HEAD v1.1.77 (sha_v2) 无过闸记录 -> 自动降级切到 v1.1.76 快照并告警
        _write_gate_record(self.gate_dir, self.sha_v1)
        notices.clear()
        res_fb = version_watch.ensure_startup_release_gate(
            self.repo, store=self.store, dispatcher=disp, gate_dir=self.gate_dir, releases_dir=self.releases_dir
        )
        self.assertTrue(res_fb["ok"])
        self.assertEqual(res_fb["status"], "fallback")
        self.assertEqual(res_fb["fallback"]["git_sha"], self.sha_v1)
        self.assertEqual(res_fb["fallback"]["version"], "1.1.76")
        self.assertEqual((self.releases_dir / "current").resolve().name, f"v1.1.76-{self.sha_v1[:7]}")
        self.assertEqual(notices[-1][0], "启动闸门降级告警")
        self.assertIn("未直接停掉服务，已自动切换至最近一个有闸门记录的只读快照", notices[-1][1])

        saved_fb = json.loads(self.store.get_meta("daemon_startup_fallback") or "{}")
        self.assertEqual(saved_fb["fallback"]["git_sha"], self.sha_v1)

    def test_rollback_switches_current_snapshot_back_to_previous_gated_release(self) -> None:
        """一键回滚校验目标过闸记录并将 current 软链原子切回上一版快照。"""
        _write_gate_record(self.gate_dir, self.sha_v1)
        sha_v2 = self._commit_version("1.1.77", "feat: release v1.1.77")
        self.git("tag", "v1.1.77", sha_v2)
        _write_gate_record(self.gate_dir, sha_v2)

        notices: list[tuple[str, str]] = []
        disp = self._make_dispatcher(notices)
        version_watch.execute_upgrade(disp, no_pull=True, repo_dir=self.repo)
        self.assertEqual((self.releases_dir / "current").resolve().name, f"v1.1.77-{sha_v2[:7]}")

        # 执行回滚到 v1.1.76
        disp.runtime_info = {"version": "1.1.77", "git_sha": sha_v2, "display": f"1.1.77 ({sha_v2[:7]})", "pid": 11111}
        res_rb = version_watch.execute_upgrade(
            disp, rollback=True, target_sha=self.sha_v1, repo_dir=self.repo
        )
        self.assertTrue(res_rb["rolled_back"])
        self.assertTrue(res_rb["restart_requested"])
        self.assertEqual(res_rb["git_sha"], self.sha_v1)
        self.assertEqual((self.releases_dir / "current").resolve().name, f"v1.1.76-{self.sha_v1[:7]}")


if __name__ == "__main__":
    unittest.main()
