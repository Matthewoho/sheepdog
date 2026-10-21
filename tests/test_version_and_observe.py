"""规格 7.68（SD-207）回归测试：
1. 单一版本事实源 0.7.68、release_gate.py 版本号对齐强校验、CLI 版本展示与常驻进程三级版本巡检单次提醒；
2. Watchdog 2.0（sheepdog observe）L2 四阶段分钟级水流 SLA（Ingest Ack 90s / Priority 90s / Queue HOL 5m / Owner Echo 15m）；
3. L3 宿主 Transcript 深度探针（Prompt Filter 拦截、假 running 僵尸锁检测与 --self-heal 写 stop hook 驱动 7.64 解锁）；
4. L3 总线越权探针（tp_bus Boundary Guard）与 L4 ask_jumps 噪音剃刀及告警指纹归一化。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import sheepdog
from sheepdog import cli, observe
from sheepdog.config import Config
from sheepdog.engine import Dispatcher
from sheepdog.roster import Roster
from sheepdog.security import SecurityConfig
from sheepdog.sink.agentapi import DryRunSink
from sheepdog.store import Store

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_gate", REPO / "scripts" / "release_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


OK_TEST_CODE = """import unittest

class OkTest(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)
"""

REG_768 = """# 回归

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.68 | 版本号对齐与新版提醒 | `test_ok.OkTest.test_a` |
"""


class VersionGateAndNoticeTest(unittest.TestCase):
    def test_release_gate_rejects_version_mismatch_and_daemon_notifies_once(self):
        # 1. 单一版本事实源与 pyproject.toml 动态读取
        self.assertEqual(sheepdog.__version__, "0.7.68")
        self.assertEqual(sheepdog.parse_version_tuple(sheepdog.__version__), (0, 7, 68))
        pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", pyproject["project"].get("dynamic", []))
        self.assertNotIn("version", pyproject["project"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "sheepdog.__version__",
        )

        # 2. release_gate.check_specs 强校验 --specs 7.68 与 sheepdog/__init__.py 版本对齐
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            (repo / "tests").mkdir(parents=True)
            (repo / "sheepdog").mkdir(parents=True)

            def git(*args: str) -> str:
                return subprocess.run(
                    [
                        "git", "-C", str(repo),
                        "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        *args,
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            git("init", "-q")
            (repo / "tests" / "test_ok.py").write_text(OK_TEST_CODE, encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(REG_768, encoding="utf-8")
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "mismatch")
            sha_mismatch = git("rev-parse", "HEAD")

            problems_bad = gate.check_specs(repo, sha_mismatch, ["7.68"], sys.executable)
            self.assertTrue(
                any("版本号未对齐" in p and "0.7.68" in p for p in problems_bad),
                f"Expected version mismatch error, got: {problems_bad}",
            )

            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.68"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "aligned")
            sha_ok = git("rev-parse", "HEAD")

            problems_ok = gate.check_specs(repo, sha_ok, ["7.68"], sys.executable)
            self.assertEqual(problems_ok, [])

            # 3. daemon_runtime 登记、get_version_status 与 CLI --version 输出
            cfg = Config(state_dir=tmp_path / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            rt = cli._record_daemon_runtime(store, repo_dir=repo)
            self.assertEqual(rt["version"], "0.7.68")

            st_sync = cli.get_version_status(store, repo_dir=repo)
            self.assertTrue(st_sync["in_sync"])
            self.assertIn("0.7.68", st_sync["summary_line"])
            self.assertIn("一致", st_sync["summary_line"])

            with mock.patch("sheepdog.cli._load_cfg", return_value=cfg):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = cli.main(["--version"])
                self.assertEqual(rc, 0)
                self.assertIn("0.7.68", buf.getvalue())

            # 4. 常驻进程 _check_version_update：本机磁盘升版提醒 + 远端 tag 提醒 + 幂等防重
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.69"\n', encoding="utf-8")
            fake_dispatcher = mock.MagicMock()
            now_dt = datetime(2026, 10, 21, 20, 0, 0, tzinfo=observe.TZ)
            real_run = subprocess.run

            def no_remote(cmd, *a, **kw):
                if isinstance(cmd, list) and "ls-remote" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
                return real_run(cmd, *a, **kw)

            with mock.patch("subprocess.run", side_effect=no_remote):
                sent1 = cli._check_version_update(
                    cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt
                )
                self.assertEqual(len(sent1), 1)
                self.assertIn("0.7.69", sent1[0])
                self.assertIn("0.7.68", sent1[0])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

                # 同一版本第二次调用不重复发通知
                sent2 = cli._check_version_update(
                    cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(seconds=10)
                )
                self.assertEqual(sent2, [])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

            # 远端出现更高版本 v0.7.70 时单次提醒
            ls_remote_out = "9999999999999999999999999999999999999999\trefs/tags/v0.7.70\n"

            def has_remote_tag(cmd, *a, **kw):
                if isinstance(cmd, list) and "ls-remote" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=ls_remote_out, stderr="")
                return real_run(cmd, *a, **kw)

            with mock.patch("subprocess.run", side_effect=has_remote_tag):
                sent_remote1 = cli._check_version_update(
                    cfg,
                    store,
                    fake_dispatcher,
                    runtime_info=rt,
                    repo_dir=repo,
                    now=now_dt + timedelta(minutes=35),
                )
                self.assertEqual(len(sent_remote1), 1)
                self.assertIn("v0.7.70", sent_remote1[0])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 2)

                sent_remote2 = cli._check_version_update(
                    cfg,
                    store,
                    fake_dispatcher,
                    runtime_info=rt,
                    repo_dir=repo,
                    now=now_dt + timedelta(minutes=70),
                )
                self.assertEqual(sent_remote2, [])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 2)


class ObserveWaterflowSlaTest(unittest.TestCase):
    def test_l2_ack_90s_priority_90s_and_hol_5m_sla_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(state_dir=tmp_path / "state")
            cfg.ack.enabled = True
            cfg.ack.reasons = ["p2p", "at_me", "at_all"]
            cfg.ack.bot_reasons = ["owner_reply"]
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            ref_now = datetime(2026, 10, 21, 20, 0, 0, tzinfo=observe.TZ)

            # 注册一个处于 running 且占用批次 b-block-1 的会话 tp_hol
            store.conn.execute(
                """
                INSERT INTO topics (topic_id, title, kind, conversation_id, state, pending_batch_id, dispatched_at, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tp_hol", "阻塞测试会话", "adopted", "cid-hol", "running", "b-block-1",
                    observe.iso(ref_now - timedelta(seconds=60)),
                    observe.iso(ref_now - timedelta(minutes=30)),
                    observe.iso(ref_now - timedelta(seconds=60)),
                ),
            )

            # ① Ingest Ack SLA：入账 120s 的 dm 消息未点表情（acks 表无记录，dm 等价于 p2p 应告警）；
            #    而入账 150s 的 keyword 群消息按配置不点表情，绝不误报！
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, sender_id, content, create_time, route, reason, topic_id, dispatch_state, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_no_ack_01", "oc_1", "ou_1", "请帮忙看下",
                    observe.iso(ref_now - timedelta(seconds=120)),
                    "dispatch", "dm", "tp_hol", "delivered",
                    observe.iso(ref_now - timedelta(seconds=120)),
                    observe.iso(ref_now - timedelta(seconds=120)),
                ),
            )
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, sender_id, content, create_time, route, reason, topic_id, dispatch_state, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_kw_noack", "oc_group", "ou_other", "群关键词消息不点表情",
                    observe.iso(ref_now - timedelta(seconds=150)),
                    "dispatch", "keyword", "tp_hol", "delivered",
                    observe.iso(ref_now - timedelta(seconds=150)),
                    observe.iso(ref_now - timedelta(seconds=150)),
                ),
            )

            # ② Priority SLA：主人指令入账 100s 仍 pending（已点过入账 ack，测试阶段 2 Priority SLA）
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, sender_id, content, create_time, route, reason, topic_id, dispatch_state, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_prio_0002", "oc_owner", "ou_owner", "紧急插话指令",
                    observe.iso(ref_now - timedelta(seconds=100)),
                    "dispatch", "owner_instruction", "tp_bus", "pending",
                    observe.iso(ref_now - timedelta(seconds=100)),
                    observe.iso(ref_now - timedelta(seconds=100)),
                ),
            )
            store.conn.execute(
                "INSERT INTO acks (message_id, chat_id, reason, identity, reaction_id, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("om_prio_0002", "oc_owner", "owner_instruction", "bot", "r_0", observe.iso(ref_now - timedelta(seconds=98))),
            )

            # ③ Queue Head-of-Line SLA：普通消息排队 6 分钟（360s，在 5m~30m 之间），且已点过 ack
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, sender_id, content, create_time, route, reason, topic_id, dispatch_state, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_hol_queue3", "oc_1", "ou_2", "排队等前一批跑完",
                    observe.iso(ref_now - timedelta(seconds=360)),
                    "dispatch", "dm", "tp_hol", "pending",
                    observe.iso(ref_now - timedelta(seconds=360)),
                    observe.iso(ref_now - timedelta(seconds=360)),
                ),
            )
            store.conn.execute(
                "INSERT INTO acks (message_id, chat_id, reason, identity, reaction_id, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("om_hol_queue3", "oc_1", "dm", "user", "r_1", observe.iso(ref_now - timedelta(seconds=355))),
            )

            # ④ Owner Echo SLA：主人回音 16 分钟（960s，在 15m~30m 之间）未结清
            store.conn.execute(
                """
                INSERT INTO owner_echoes (message_id, chat_id, kind, said_at, stage, topic_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_echo_0004", "oc_owner", "owner_instruction",
                    observe.iso(ref_now - timedelta(seconds=960)),
                    "delivered", "tp_hol",
                    observe.iso(ref_now - timedelta(seconds=960)),
                ),
            )
            store.conn.commit()

            res = observe.run_observe(
                cfg,
                now_dt=ref_now,
                since_minutes=30.0,
                check_processes=False,
                check_log=False,
            )
            warns = res["warns"]
            self.assertTrue(
                any("Ingest Ack SLA）：1 条" in w and "no_ack_01"[-8:] in w for w in warns),
                f"Expected exactly 1 Ingest Ack SLA warn (excluding keyword), got: {warns}",
            )
            self.assertFalse(
                any("kw_noack" in w for w in warns),
                f"keyword reason should NOT trigger Ingest Ack SLA, got: {warns}",
            )
            # 10 秒后第 2 轮巡检（since 已推进）：30 分钟滚动窗口仍能捕获该未点表情消息，不从 warns 中消失
            res_second = observe.run_observe(
                cfg,
                now_dt=ref_now + timedelta(seconds=10),
                since_minutes=30.0,
                check_processes=False,
                check_log=False,
            )
            self.assertTrue(
                any("Ingest Ack SLA）：1 条" in w and "no_ack_01"[-8:] in w for w in res_second["warns"]),
                f"Ingest Ack SLA warn must persist across consecutive runs via rolling window, got: {res_second['warns']}",
            )
            self.assertTrue(
                any("Priority SLA" in w and "prio_0002"[-8:] in w for w in warns),
                f"Expected Priority SLA warn, got: {warns}",
            )
            self.assertTrue(
                any("Queue Head-of-Line SLA" in w and "tp_hol" in w and "b-block-1" in w for w in warns),
                f"Expected Queue Head-of-Line SLA warn with blocking batch, got: {warns}",
            )
            self.assertTrue(
                any("Owner Echo SLA" in w and "echo_0004"[-8:] in w for w in warns),
                f"Expected Owner Echo SLA warn, got: {warns}",
            )


class ObserveTranscriptProbesTest(unittest.TestCase):
    def test_l3_prompt_filter_and_zombie_running_self_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(state_dir=tmp_path / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            brain_dir = tmp_path / "brain"
            ref_now = datetime(2026, 10, 21, 20, 0, 0, tzinfo=observe.TZ)

            # ① 会话 A (tp_filter)：running 120s，宿主 transcript 出现 Prompt Filter 拦截报错
            disp_filter_at = observe.iso(ref_now - timedelta(seconds=120))
            store.conn.execute(
                """
                INSERT INTO topics (topic_id, title, kind, conversation_id, host, state, pending_batch_id, dispatched_at, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tp_filter", "过滤拦截会话", "adopted", "cid-filter", "antigravity", "running", "b-filter-1", disp_filter_at, disp_filter_at, disp_filter_at),
            )
            t_filter = brain_dir / "cid-filter" / ".system_generated" / "logs" / "transcript.jsonl"
            t_filter.parent.mkdir(parents=True, exist_ok=True)
            t_filter.write_text(
                json.dumps({
                    "type": "ERROR_MESSAGE",
                    "content": "The prompt could not be submitted due to safety filter",
                    "created_at": observe.iso(ref_now - timedelta(seconds=115)),
                }) + "\n",
                encoding="utf-8",
            )

            # ② 会话 B (tp_zombie)：running 240s，无活跃羔羊，无回执，最后一步 PLANNER_RESPONSE DONE 已静默 100s
            disp_zombie_at = observe.iso(ref_now - timedelta(seconds=240))
            store.conn.execute(
                """
                INSERT INTO topics (topic_id, title, kind, conversation_id, host, state, pending_batch_id, dispatched_at, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tp_zombie", "假运行僵尸会话", "adopted", "cid-zombie", "antigravity", "running", "b-zombie-1", disp_zombie_at, disp_zombie_at, disp_zombie_at),
            )
            store.conn.execute(
                "INSERT INTO dispatches (batch_id, topic_id, message_ids, sent_at, state, receipt) VALUES (?, ?, ?, ?, ?, ?)",
                ("b-zombie-1", "tp_zombie", "[]", disp_zombie_at, "sent", None),
            )
            store.conn.commit()

            t_zombie = brain_dir / "cid-zombie" / ".system_generated" / "logs" / "transcript.jsonl"
            t_zombie.parent.mkdir(parents=True, exist_ok=True)
            zombie_steps = [
                {
                    "type": "USER_MESSAGE",
                    "content": "[sheepdog:tp_zombie:b-zombie-1] 请执行任务",
                    "created_at": observe.iso(ref_now - timedelta(seconds=235)),
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "我已经处理完了，但忘了敲 sheepdog receipt。",
                    "created_at": observe.iso(ref_now - timedelta(seconds=100)),
                },
            ]
            t_zombie.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in zombie_steps) + "\n", encoding="utf-8")

            res = observe.run_observe(
                cfg,
                now_dt=ref_now,
                self_heal=True,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
            )
            warns = res["warns"]
            self.assertTrue(
                any("Prompt Filter" in w and "tp_filter" in w for w in warns),
                f"Expected Prompt Filter warn, got: {warns}",
            )
            self.assertTrue(
                any("假 running 僵尸锁" in w and "tp_zombie" in w for w in warns),
                f"Expected zombie running warn, got: {warns}",
            )
            self.assertIn("tp_zombie:b-zombie-1", res["self_healed"])

            stop_file = cfg.state_dir / "hooks" / "stop" / "tp_zombie.json"
            self.assertTrue(stop_file.exists())
            stop_data = json.loads(stop_file.read_text(encoding="utf-8"))
            self.assertEqual(stop_data["batch"], "b-zombie-1")
            self.assertEqual(stop_data["self_heal_by"], "sheepdog.observe")

            # 验证常驻进程 Dispatcher._check_stopped_in_flight 读取该 stop 自愈标记后第 1 轮自动催交、第 2 轮直接解锁
            sink = DryRunSink()
            dispatcher = Dispatcher(cfg, store, {"antigravity": sink}, Roster(), SecurityConfig())
            sent_dt = datetime.fromisoformat(disp_zombie_at)
            handled_round1 = dispatcher._check_stopped_in_flight(
                store.get_topic("tp_zombie"), "b-zombie-1", sent_dt, ref_now, True
            )
            self.assertTrue(handled_round1)
            stop_after_r1 = json.loads(stop_file.read_text(encoding="utf-8"))
            self.assertEqual(stop_after_r1.get("auto_resumed"), 1)

            # 若催交后仍静默满 200s，下一轮 observe --self-heal 保留 auto_resumed=True 并推进 passed_at，常驻进程立即解锁批次
            round2_now = ref_now + timedelta(seconds=200)
            observe.run_observe(
                cfg,
                now_dt=round2_now,
                self_heal=True,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
            )
            handled_round2 = dispatcher._check_stopped_in_flight(
                store.get_topic("tp_zombie"), "b-zombie-1", ref_now, round2_now, True
            )
            self.assertTrue(handled_round2)
            unlocked_topic = store.get_topic("tp_zombie")
            self.assertIsNone(unlocked_topic["pending_batch_id"])
            self.assertIn(unlocked_topic["state"], ("active", "waiting"))


class ObserveBusBoundaryAndNoiseTest(unittest.TestCase):
    def test_l3_bus_overreach_and_l4_ask_jumps_noise_razor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(state_dir=tmp_path / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            brain_dir = tmp_path / "brain"
            ref_now = datetime(2026, 10, 21, 20, 0, 0, tzinfo=observe.TZ)

            # 1. tp_bus 处于 running 达 180s（超过 150s 上限），且 transcript 中调用了 write_file 与 gcloud
            bus_disp_at = observe.iso(ref_now - timedelta(seconds=180))
            store.conn.execute(
                """
                INSERT INTO topics (topic_id, title, kind, conversation_id, host, state, pending_batch_id, dispatched_at, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tp_bus", "总线", "bus", "cid-bus", "antigravity", "running", "b-bus-over", bus_disp_at, bus_disp_at, bus_disp_at),
            )
            t_bus = brain_dir / "cid-bus" / ".system_generated" / "logs" / "transcript.jsonl"
            t_bus.parent.mkdir(parents=True, exist_ok=True)
            bus_step = {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": observe.iso(ref_now - timedelta(seconds=60)),
                "tool_calls": [
                    {"name": "write_file", "args": {"Path": "/tmp/hack.txt"}},
                    {"name": "run_command", "args": {"CommandLine": json.dumps("gcloud run services list --project=tap-testing-env")}},
                ],
            }
            t_bus.write_text(json.dumps(bus_step, ensure_ascii=False) + "\n", encoding="utf-8")

            # 2. 在 escalations 表中为 tp_work 插入 6 条已正常回答结案（close_reason='answered', closed_at 非空）的记录
            for i in range(6):
                asked = observe.iso(ref_now - timedelta(hours=3, minutes=i * 10))
                closed = observe.iso(ref_now - timedelta(hours=2, minutes=i * 10))
                store.conn.execute(
                    """
                    INSERT INTO escalations (message_id, topic_id, chat_id, text, asked_at, answered_at, closed_at, close_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"om_ans_{i}", "tp_work", "oc_owner", f"问题 {i}", asked, closed, closed, "answered"),
                )
            store.conn.commit()

            res1 = observe.run_observe(
                cfg,
                now_dt=ref_now,
                since_minutes=20.0,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
            )
            warns1 = res1["warns"]
            self.assertTrue(
                any("总线超时运行" in w and "tp_bus" in w for w in warns1),
                f"Expected tp_bus timeout warn, got: {warns1}",
            )
            self.assertTrue(
                any("总线越权干活" in w and "write_file" in w and "gcloud" in w for w in warns1),
                f"Expected tp_bus tool/cmd overreach warn, got: {warns1}",
            )
            # 已正常结案的 6 条提问绝不能触发 ask_jumps 噪音告警
            self.assertFalse(
                any("ask_jumps" in w or "tp_work" in w for w in warns1),
                f"Settled escalations should NOT trigger ask_jumps, got: {warns1}",
            )

            # 3. 再插入 5 条未结案（closed_at IS NULL）的提问，此时应准确触发 ask_jumps 告警
            for i in range(5):
                asked = observe.iso(ref_now - timedelta(minutes=30 + i))
                store.conn.execute(
                    """
                    INSERT INTO escalations (message_id, topic_id, chat_id, text, asked_at, closed_at, close_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"om_open_{i}", "tp_work", "oc_owner", f"未结问题 {i}", asked, None, None),
                )
            store.conn.commit()

            # 同时验证：tp_bus 执行合法的 sheepdog forward（--note 含 gcloud 字样）绝不误报越权
            safe_bus_step = {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": observe.iso(ref_now - timedelta(seconds=30)),
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": json.dumps(
                                'sheepdog forward --topic tp_auth --message-ids om_test_1 --note "申请开通 gcloud 权限"'
                            )
                        },
                    },
                ],
            }
            t_bus.write_text(json.dumps(safe_bus_step, ensure_ascii=False) + "\n", encoding="utf-8")
            store.conn.execute("UPDATE topics SET state = 'waiting', pending_batch_id = NULL WHERE topic_id = 'tp_bus'")
            store.conn.commit()

            # 连续两次运行 run_observe(..., notify_lark=True)，验证：
            # (a) sheepdog forward --note "...gcloud..." 不触发 tp_bus Boundary Guard；
            # (b) ask_jumps 持续存在于 warns 中，第 2 轮进入冷却而非变空误发恢复通知！
            sent_cards: list[tuple[str, str, str]] = []

            def fake_send_card(chat_id: str, title: str, template: str, body_md: str) -> bool:
                sent_cards.append((title, template, body_md))
                return True

            st_file = tmp_path / "state" / "cursor-cooldown.json"
            with mock.patch("sheepdog.observe._send_lark_card", side_effect=fake_send_card):
                res2 = observe.run_observe(
                    cfg,
                    state_path=st_file,
                    now_dt=ref_now + timedelta(seconds=10),
                    since_minutes=20.0,
                    notify_lark=True,
                    notify_chat_id="oc_test_owner",
                    antigravity_brain_dir=brain_dir,
                    check_processes=False,
                    check_log=False,
                )
                warns2 = res2["warns"]
                self.assertFalse(
                    any("总线越权干活" in w for w in warns2),
                    f"Legitimate sheepdog forward with gcloud in --note must NOT trigger bus guard, got: {warns2}",
                )
                self.assertTrue(
                    any("ask_jumps" in w and "tp_work" in w for w in warns2),
                    f"Expected ask_jumps warn for 5 unsettled escalations, got: {warns2}",
                )
                self.assertEqual(len(sent_cards), 1)
                self.assertEqual(sent_cards[0][1], "orange")

                # 5 秒后第 2 次运行：告警仍存在，warns 不变空，命中指纹冷却，绝不发送绿色恢复卡片
                res3 = observe.run_observe(
                    cfg,
                    state_path=st_file,
                    now_dt=ref_now + timedelta(seconds=15),
                    since_minutes=20.0,
                    notify_lark=True,
                    notify_chat_id="oc_test_owner",
                    antigravity_brain_dir=brain_dir,
                    check_processes=False,
                    check_log=False,
                )
                self.assertTrue(
                    any("ask_jumps" in w and "tp_work" in w for w in res3["warns"]),
                    f"ask_jumps warn must persist on consecutive runs while unsettled, got: {res3['warns']}",
                )
                self.assertEqual(len(sent_cards), 1, f"Should not send duplicate or recovery card, got: {sent_cards}")
                self.assertTrue(any("看门狗告警冷却中" in i for i in res3["infos"]))

            # 4. 验证 sheepdog observe --db <path> --state <path> --json CLI 参数正常工作
            with mock.patch("sheepdog.cli._load_cfg", return_value=cfg):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc_obs = cli.main(["observe", "--db", str(cfg.db_path), "--state", str(st_file), "--json"])
                parsed_obs = json.loads(buf.getvalue())
                self.assertIn("warns", parsed_obs)
                self.assertEqual(rc_obs, 1 if parsed_obs["warns"] else 0)

            # 5. 验证 _fingerprint_warns 对秒数与分钟数跳动归一化
            fp_a = observe._fingerprint_warns(["消息排队超 5 分钟：tp_hol 1 条 最久 6 分钟 (360s)"])
            fp_b = observe._fingerprint_warns(["消息排队超 5 分钟：tp_hol 1 条 最久 12 分钟 (720s)"])
            self.assertEqual(fp_a, fp_b)


if __name__ == "__main__":
    unittest.main()
