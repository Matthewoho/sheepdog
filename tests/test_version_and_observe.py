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
        cur_ver = sheepdog.__version__
        self.assertEqual(cur_ver, "0.7.75")
        self.assertEqual(sheepdog.parse_version_tuple(cur_ver), (0, 7, 75))
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
            (repo / "sheepdog" / "__init__.py").write_text(f'__version__ = "{cur_ver}"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "sync-cur-ver")
            cfg = Config(state_dir=tmp_path / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            rt = cli._record_daemon_runtime(store, repo_dir=repo)
            self.assertEqual(rt["version"], cur_ver)

            st_sync = cli.get_version_status(store, repo_dir=repo)
            self.assertTrue(st_sync["in_sync"])
            self.assertIn(cur_ver, st_sync["summary_line"])
            self.assertIn("一致", st_sync["summary_line"])

            with mock.patch("sheepdog.cli._load_cfg", return_value=cfg):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = cli.main(["--version"])
                self.assertEqual(rc, 0)
                self.assertIn(cur_ver, buf.getvalue())

            # 4. 常驻进程 _check_version_update：本机磁盘升版提醒 + 远端 tag 提醒 + 幂等防重
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.98"\n', encoding="utf-8")
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
                self.assertIn("0.7.98", sent1[0])
                self.assertIn(cur_ver, sent1[0])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

                # 同一版本第二次调用不重复发通知
                sent2 = cli._check_version_update(
                    cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(seconds=10)
                )
                self.assertEqual(sent2, [])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

            # 远端出现更高版本 v0.7.99 时单次提醒
            ls_remote_out = "9999999999999999999999999999999999999999\trefs/tags/v0.7.99\n"

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
                self.assertIn("v0.7.99", sent_remote1[0])
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


class Version769SignatureAndUpgradeTest(unittest.TestCase):
    """规格 7.69（REG-006 / REG-007 / REG-008）回归测试。"""

    def tearDown(self):
        from sheepdog import actions

        actions._resident = False

    def test_reg_006_reply_card_version_signature_and_legacy_anti_echo(self):
        from sheepdog.card import build_reply_card
        from sheepdog.config import (
            LEGACY_REPLY_SUFFIX,
            OwnerContextConfig,
            SessionConfig,
            default_reply_suffix,
            load_config,
        )
        from sheepdog.engine import Collector, has_reply_suffix, is_agent_reply
        from sheepdog.pending import is_agent_text

        # 1. 默认右下角签名动态跟随 __version__（🐕SheepDog v0.7.75）
        expected_sig = f"🐕SheepDog v{sheepdog.__version__}"
        self.assertEqual(expected_sig, "🐕SheepDog v0.7.75")
        self.assertEqual(default_reply_suffix(), expected_sig)
        self.assertEqual(SessionConfig().reply_suffix, expected_sig)

        card = build_reply_card("收到，马上处理", SessionConfig().reply_suffix)
        self.assertEqual(
            card["body"]["elements"][-1],
            {
                "tag": "markdown",
                "content": f"<font color='grey'>{expected_sig}</font>",
                "text_align": "right",
                "text_size": "notation",
            },
        )

        # 2. 旧配置文件写死 [🐕Sheepdog Reply] 自动升级为动态版本签名；自定义标记原样保留
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_legacy = tmp_path / "legacy.toml"
            cfg_legacy.write_text(
                f'self_open_id = "ou_test_me"\nstate_dir = "{tmp_path / "s1"}"\n'
                f'[session]\nreply_suffix = "{LEGACY_REPLY_SUFFIX}"\n',
                encoding="utf-8",
            )
            loaded_legacy = load_config(cfg_legacy)
            self.assertEqual(loaded_legacy.session.reply_suffix, expected_sig)

            cfg_custom = tmp_path / "custom.toml"
            cfg_custom.write_text(
                f'self_open_id = "ou_test_me"\nstate_dir = "{tmp_path / "s2"}"\n'
                '[session]\nreply_suffix = "[FX-Reply]"\n',
                encoding="utf-8",
            )
            loaded_custom = load_config(cfg_custom)
            self.assertEqual(loaded_custom.session.reply_suffix, "[FX-Reply]")

            # 3. 跨版本防回声识别：新版本卡片、未来版本卡片、旧版 [🐕Sheepdog Reply] 全链路互认
            new_card_text = f"<card>\n好的，已经安排\n{expected_sig}\n</card>"
            future_card_text = "<card>\n好的，已经安排\n🐕SheepDog v0.8.2\n</card>"
            legacy_card_text = f"<card>\n好的，已经安排\n{LEGACY_REPLY_SUFFIX}\n</card>"
            human_text = "我自己亲口说的话"

            for sample in (new_card_text, future_card_text, legacy_card_text):
                self.assertTrue(has_reply_suffix(sample, expected_sig), sample)
            self.assertFalse(has_reply_suffix(human_text, expected_sig))

            # 即使个人配置里的 agent_markers 仍停留在旧版 ["[🐕Sheepdog Reply]"]，也能认出新版签名
            oc_old_marker = OwnerContextConfig(agent_markers=[LEGACY_REPLY_SUFFIX])
            self.assertTrue(is_agent_reply(new_card_text, oc_old_marker))
            self.assertTrue(is_agent_reply(future_card_text, oc_old_marker))
            self.assertTrue(is_agent_reply(legacy_card_text, oc_old_marker))
            self.assertFalse(is_agent_reply(human_text, oc_old_marker))

            # 新配置 agent_markers = ["🐕SheepDog v"] 同样认出旧版 [🐕Sheepdog Reply]
            oc_new_marker = OwnerContextConfig(agent_markers=["🐕SheepDog v"])
            self.assertTrue(is_agent_reply(legacy_card_text, oc_new_marker))
            self.assertTrue(is_agent_reply(new_card_text, oc_new_marker))

            # 空 agent_markers 时对非内置合成标记 [FX-Reply] 保持 False（不破坏历史单测）
            self.assertFalse(is_agent_reply("<card>\nx\n[FX-Reply]\n</card>", OwnerContextConfig()))

            # Collector._agent_reply_text 与 pending.is_agent_text 均同时识别新旧签名
            store = Store(loaded_legacy.db_path)
            try:
                col = Collector(loaded_legacy, store, mock.MagicMock(), Roster(), SecurityConfig())
                for sample in (new_card_text, future_card_text, legacy_card_text):
                    self.assertTrue(col._agent_reply_text(sample))
                    self.assertTrue(is_agent_text(loaded_legacy, sample))
                self.assertFalse(col._agent_reply_text(human_text))
                self.assertFalse(is_agent_text(loaded_legacy, human_text))
            finally:
                store.close()

    def test_reg_007_version_notice_guidance_and_owner_upgrade_shortcut_execv(self):
        from sheepdog import actions
        from sheepdog.config import EscalationConfig
        from sheepdog.engine import BUS_TOPIC_ID, Collector, is_upgrade_command
        from sheepdog.models import Message

        # 1. 升级口令识别（剥离 <at> 标签、标点、大小写不敏感）
        for valid_cmd in (
            "你来升级",
            "升级！",
            "重启生效。",
            "<at id=ou_bot>@牧羊犬</at> 你来升级~",
            "  SHEEPDOG UPGRADE ",
        ):
            self.assertTrue(is_upgrade_command(valid_cmd), valid_cmd)
        for non_cmd in ("生产服务刚完成了升级", "帮我看看升级方案", "待办"):
            self.assertFalse(is_upgrade_command(non_cmd), non_cmd)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
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
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.68"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "v0.7.68")

            cfg = Config(self_open_id="ou_test_owner", state_dir=tmp_path / "state")
            cfg.escalation = EscalationConfig(
                chat_ids=["oc_owner_dm"],
                header_regex=r"^🐕 需要你定 · [^\n]*\[(?P<topic>tp_[a-z0-9_.-]+)\]",
                attribution_regex=r"\[(?P<topic>tp_[a-z0-9_.-]+)\]",
                owner_shortcut=True,
            )
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            try:
                # 2. _check_version_update 新版提醒文案带标题行与「你来升级」/ sheepdog upgrade 指引
                rt = {
                    "version": "0.7.68",
                    "git_sha": git("rev-parse", "HEAD"),
                    "display": "0.7.68 (oldsha)",
                    "pid": 12345,
                }
                store.set_meta("daemon_runtime", json.dumps(rt, ensure_ascii=False))
                (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.69"\n', encoding="utf-8")
                git("add", "-A")
                git("commit", "-q", "-m", "v0.7.69")

                notified_cards: list[tuple[str, str, str, str]] = []
                disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
                disp.runtime_info = rt
                disp._notify_owner = lambda text, what, topic_id="", reply_to="", **kw: (
                    notified_cards.append((text, what, topic_id, reply_to)) or True
                )

                now_dt = datetime.now(observe.TZ).replace(microsecond=0)
                real_run = subprocess.run
                ls_remote_out = "8888888888888888888888888888888888888888\trefs/tags/v0.7.70\n"

                def fake_git(cmd, *a, **kw):
                    if isinstance(cmd, list) and "ls-remote" in cmd:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=ls_remote_out, stderr="")
                    if isinstance(cmd, list) and "pull" in cmd:
                        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Already up to date.\n", stderr="")
                    return real_run(cmd, *a, **kw)

                with mock.patch("subprocess.run", side_effect=fake_git):
                    notices = cli._check_version_update(
                        cfg, store, disp, runtime_info=rt, repo_dir=repo, now=now_dt
                    )
                self.assertEqual(len(notices), 2)
                self.assertTrue(notices[0].startswith(f"🐕 汇报 · 新版提醒 [{BUS_TOPIC_ID}]\n"))
                self.assertIn("你来升级", notices[0])
                self.assertIn("sheepdog upgrade", notices[0])
                self.assertTrue(notices[1].startswith(f"🐕 汇报 · 新版提醒 [{BUS_TOPIC_ID}]\n"))
                self.assertIn("你来升级", notices[1])

                # 3. 主人在找主人聊天引用「新版提醒」卡片回复「你来升级」：直接拦截为 upgrade_command 并入队 actions('upgrade')
                store.add_bot_outbox("om_notice_card_1", BUS_TOPIC_ID, False, notices[0], observe.iso(now_dt))
                col = Collector(cfg, store, mock.MagicMock(), Roster(), SecurityConfig())
                m_upgrade = Message(
                    message_id="om_owner_upg_1",
                    chat_id="oc_owner_dm",
                    chat_name="私聊",
                    chat_type="p2p",
                    sender_id="ou_test_owner",
                    sender_name="Matthew",
                    sender_type="user",
                    content="你来升级",
                    msg_type="text",
                    create_time=observe.iso(now_dt + timedelta(seconds=5)),
                    reply_to="om_notice_card_1",
                )
                d = col._escalation_message(m_upgrade)
                self.assertEqual((d.route, d.reason), ("drop", "upgrade_command"))
                self.assertEqual(store.echo_open(), [])  # 不记 owner_echoes 欠账
                pending_acts = store.pending_actions()
                self.assertEqual(len(pending_acts), 1)
                self.assertEqual(pending_acts[0]["kind"], "upgrade")
                self.assertEqual(json.loads(pending_acts[0]["args_json"])["reply_to"], "om_owner_upg_1")

                # 队列中已有待执行 upgrade 时重复发「升级」不重复入队
                m_upgrade_dup = Message(
                    message_id="om_owner_upg_2",
                    chat_id="oc_owner_dm",
                    chat_name="私聊",
                    chat_type="p2p",
                    sender_id="ou_test_owner",
                    sender_name="Matthew",
                    sender_type="user",
                    content="升级",
                    msg_type="text",
                    create_time=observe.iso(now_dt + timedelta(seconds=6)),
                )
                col._escalation_message(m_upgrade_dup)
                self.assertEqual(len(store.pending_actions()), 1)

                # 对照：若主人引用的是未结 escalation 提问回答「升级」，则正常走 owner_reply 回答提问，不误判为自升级
                store.add_escalation("om_esc_ask_1", "tp_alpha", "oc_owner_dm", "🐕 需要你定 · 升级数据库 [tp_alpha]\n要升级吗？", observe.iso(now_dt))
                m_answer_esc = Message(
                    message_id="om_owner_ans_1",
                    chat_id="oc_owner_dm",
                    chat_name="私聊",
                    chat_type="p2p",
                    sender_id="ou_test_owner",
                    sender_name="Matthew",
                    sender_type="user",
                    content="升级",
                    msg_type="text",
                    create_time=observe.iso(now_dt + timedelta(seconds=7)),
                    reply_to="om_esc_ask_1",
                )
                d_ans = col._escalation_message(m_answer_esc)
                self.assertEqual((d_ans.route, d_ans.reason), ("dispatch", "owner_reply"))

                # 4. 常驻进程执行 pending upgrade 动作：发送「升级完成」确认卡片并置 restart_requested = True
                notified_cards.clear()
                with mock.patch("subprocess.run", side_effect=fake_git), \
                        mock.patch("sheepdog.default_repo_dir", return_value=repo):
                    ran_n = actions.run_pending(disp)
                self.assertEqual(ran_n, 1)
                self.assertTrue(disp.restart_requested)
                self.assertEqual(len(notified_cards), 1)
                card_text, card_what, card_topic, card_reply_to = notified_cards[0]
                self.assertEqual(card_what, "升级完成")
                self.assertEqual(card_topic, BUS_TOPIC_ID)
                self.assertEqual(card_reply_to, "om_owner_upg_1")
                self.assertTrue(card_text.startswith(f"🐕 汇报 · 升级完成 [{BUS_TOPIC_ID}]\n"))
                self.assertIn("0.7.69", card_text)
                self.assertIn("原地重启", card_text)

                # 5. CLI `sheepdog upgrade` 在常驻进程在线时入队 upgrade 动作；cmd_run 检测到 restart_requested 调用 os.execv
                store.set_meta(
                    "daemon_runtime",
                    json.dumps({**rt, "pid": os.getpid()}, ensure_ascii=False),
                )
                with mock.patch("sheepdog.cli._load_cfg", return_value=cfg), \
                        mock.patch("sheepdog.cli.default_repo_dir", return_value=repo), \
                        mock.patch("subprocess.run", side_effect=fake_git):
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", buf):
                        rc_upg = cli.main(["upgrade", "--no-pull"])
                    self.assertEqual(rc_upg, 0)
                    self.assertIn("已提交常驻进程原地热重启", buf.getvalue())
                self.assertEqual(len(store.pending_actions()), 1)

                # 验证 cmd_run 执行完 pending upgrade 后释放锁并在 finally 中触发 _exec_restart (os.execv)
                cfg_file = tmp_path / "config.toml"
                cfg_file.write_text(
                    f'self_open_id = "ou_test_owner"\nsink = "dryrun"\nstate_dir = "{cfg.state_dir}"\n'
                    '[source]\nenabled = false\n[identity]\nenabled = false\n',
                    encoding="utf-8",
                )
                env = {
                    "SHEEPDOG_CONFIG": str(cfg_file),
                    "SHEEPDOG_STATE_DIR": str(cfg.state_dir),
                    "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
                }
                with mock.patch.dict(os.environ, env), \
                        mock.patch("sheepdog.default_repo_dir", return_value=repo), \
                        mock.patch("subprocess.run", side_effect=fake_git), \
                        mock.patch("os.execv") as mock_execv:
                    rc_run = cli.main(["run"])
                self.assertEqual(rc_run, 0)
                mock_execv.assert_called_once_with(sys.executable, [sys.executable, *sys.argv])
            finally:
                store.close()
                actions._resident = False

    def test_reg_008_single_instance_flock_and_idempotent_upsert_message(self):
        from sheepdog import actions
        from sheepdog.models import Message

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            cfg_file = tmp_path / "config.toml"
            cfg_file.write_text(
                f'self_open_id = "ou_test_owner"\nsink = "dryrun"\nstate_dir = "{state_dir}"\n'
                '[source]\nenabled = false\n[identity]\nenabled = false\n',
                encoding="utf-8",
            )
            env = {
                "SHEEPDOG_CONFIG": str(cfg_file),
                "SHEEPDOG_STATE_DIR": str(state_dir),
                "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            }

            # 1. 第一个常驻进程持有 sheepdog.run.lock 排他锁时，第二个常驻进程拿锁失败立即退出 0，绝不覆盖 daemon_runtime 或进 _run_loop
            lock1 = cli._acquire_daemon_lock(state_dir)
            self.assertIsNotNone(lock1)
            try:
                lock2 = cli._acquire_daemon_lock(state_dir)
                self.assertIsNone(lock2)

                err_buf = io.StringIO()
                with mock.patch.dict(os.environ, env), \
                        mock.patch("sys.stderr", err_buf), \
                        mock.patch("sheepdog.cli._run_loop") as mock_loop:
                    rc_dup = cli.main(["run"])
                self.assertEqual(rc_dup, 0)
                mock_loop.assert_not_called()
                self.assertIn("已有 sheepdog run 常驻进程持有单实例锁", err_buf.getvalue())
            finally:
                cli._release_daemon_lock(lock1)
                actions._resident = False

            # 释放后可正常再次获取
            lock3 = cli._acquire_daemon_lock(state_dir)
            self.assertIsNotNone(lock3)
            cli._release_daemon_lock(lock3)

            # 2. Store.upsert_message 对相同 message_id 并发/重复写入完全幂等，不抛 sqlite3.IntegrityError
            store = Store(state_dir / "sheepdog.sqlite3")
            try:
                m = Message(
                    message_id="om_concurrent_dup_1",
                    chat_id="oc_test_chat",
                    chat_name="测试群",
                    chat_type="group",
                    sender_id="ou_alice",
                    sender_name="Alice",
                    sender_type="user",
                    content="同一毫秒被双通道扫到的消息",
                    msg_type="text",
                    create_time="2026-10-21T22:00:00+08:00",
                )
                store.upsert_message(m, "dispatch", "at_me", ["at_me"], "tp_bus", received_by="bot")
                # 第二次以相同 message_id 写入绝不抛 IntegrityError
                store.upsert_message(m, "dispatch", "at_me", ["at_me"], "tp_bus", received_by="user")
                row = store.get_message("om_concurrent_dup_1")
                self.assertIsNotNone(row)
                self.assertEqual(row["message_id"], "om_concurrent_dup_1")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
