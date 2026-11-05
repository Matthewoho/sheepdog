"""规格 7.68（SD-207）回归测试：
1. 单一版本事实源 0.7.68、release_gate.py 版本号对齐强校验、CLI 版本展示与常驻进程三级版本巡检单次提醒；
2. Watchdog 2.0（sheepdog observe）L2 四阶段分钟级水流 SLA（Ingest Ack 90s / Priority 90s / Queue HOL 5m / Owner Echo 15m）；
3. L3 宿主 Transcript 深度探针（Prompt Filter 拦截、假 running 僵尸锁检测与 --self-heal 写 stop hook 驱动 7.64 解锁）；
4. L3 总线越权探针（tp_bus Boundary Guard）与 L4 ask_jumps 噪音剃刀及告警指纹归一化。
"""

from __future__ import annotations

import importlib.util, io, json, os, subprocess, sys, tempfile, tomllib, unittest
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
        ver_tuple = sheepdog.parse_version_tuple(cur_ver)
        self.assertGreaterEqual(ver_tuple, (1, 1, 0))
        next_ver_1 = f"{ver_tuple[0]}.{ver_tuple[1]}.{ver_tuple[2] + 1}"
        next_ver_2 = f"{ver_tuple[0]}.{ver_tuple[1]}.{ver_tuple[2] + 2}"
        pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", pyproject["project"].get("dynamic", []))
        self.assertNotIn("version", pyproject["project"])
        self.assertEqual(pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"], "sheepdog.__version__")

        # 2. release_gate.check_specs 强校验 --specs 7.68 与 sheepdog/__init__.py 版本对齐
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            (repo / "tests").mkdir(parents=True)
            (repo / "sheepdog").mkdir(parents=True)

            def git(*args: str) -> str:
                cmd = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args]
                return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-q")
            (repo / "tests" / "test_ok.py").write_text(OK_TEST_CODE, encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(REG_768, encoding="utf-8")
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "mismatch")
            sha_mismatch = git("rev-parse", "HEAD")

            problems_bad = gate.check_specs(repo, sha_mismatch, ["7.68"], sys.executable)
            self.assertTrue(any("版本号未对齐" in p and "0.7.68" in p for p in problems_bad), f"Expected version mismatch error, got: {problems_bad}")

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
            (repo / "sheepdog" / "__init__.py").write_text(f'__version__ = "{next_ver_1}"\n', encoding="utf-8")
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
                self.assertIn(next_ver_1, sent1[0])
                self.assertIn(cur_ver, sent1[0])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

                # 同一版本第二次调用不重复发通知
                sent2 = cli._check_version_update(
                    cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(seconds=10)
                )
                self.assertEqual(sent2, [])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 1)

            # 远端出现更高版本时单次提醒
            ls_remote_out = f"9999999999999999999999999999999999999999\trefs/tags/v{next_ver_2}\n"

            def has_remote_tag(cmd, *a, **kw):
                if isinstance(cmd, list) and "ls-remote" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=ls_remote_out, stderr="")
                return real_run(cmd, *a, **kw)

            with mock.patch("subprocess.run", side_effect=has_remote_tag):
                sent_remote1 = cli._check_version_update(cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(minutes=35))
                self.assertEqual(len(sent_remote1), 1)
                self.assertIn(f"v{next_ver_2}", sent_remote1[0])
                self.assertEqual(fake_dispatcher._notify_owner.call_count, 2)

                sent_remote2 = cli._check_version_update(cfg, store, fake_dispatcher, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(minutes=70))
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

            res = observe.run_observe(cfg, now_dt=ref_now, since_minutes=30.0, check_processes=False, check_log=False)
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
            res_second = observe.run_observe(cfg, now_dt=ref_now + timedelta(seconds=10), since_minutes=30.0, check_processes=False, check_log=False)
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

            res = observe.run_observe(cfg, now_dt=ref_now, self_heal=True, antigravity_brain_dir=brain_dir, check_processes=False, check_log=False)
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
            observe.run_observe(cfg, now_dt=round2_now, self_heal=True, antigravity_brain_dir=brain_dir, check_processes=False, check_log=False)
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

            res1 = observe.run_observe(cfg, now_dt=ref_now, since_minutes=20.0, antigravity_brain_dir=brain_dir, check_processes=False, check_log=False)
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
                    cfg, state_path=st_file, now_dt=ref_now + timedelta(seconds=10), since_minutes=20.0,
                    notify_lark=True, notify_chat_id="oc_test_owner", antigravity_brain_dir=brain_dir, check_processes=False, check_log=False,
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
                    cfg, state_path=st_file, now_dt=ref_now + timedelta(seconds=15), since_minutes=20.0,
                    notify_lark=True, notify_chat_id="oc_test_owner", antigravity_brain_dir=brain_dir, check_processes=False, check_log=False,
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
        from sheepdog.config import LEGACY_REPLY_SUFFIX, OwnerContextConfig, SessionConfig, default_reply_suffix, load_config
        from sheepdog.engine import Collector, has_reply_suffix, is_agent_reply
        from sheepdog.pending import is_agent_text

        # 1. 默认右下角签名动态跟随 __version__
        expected_sig = f"🐕SheepDog v{sheepdog.__version__}"
        self.assertTrue(expected_sig.startswith("🐕SheepDog v1."))
        self.assertEqual(default_reply_suffix(), expected_sig)
        self.assertEqual(SessionConfig().reply_suffix, expected_sig)

        card = build_reply_card("收到，马上处理", SessionConfig().reply_suffix)
        self.assertEqual(
            card["body"]["elements"][-1],
            {"tag": "markdown", "content": f"<font color='grey'>{expected_sig}</font>", "text_align": "right", "text_size": "notation"},
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
        for valid_cmd in ("你来升级", "升级！", "重启生效。", "<at id=ou_bot>@牧羊犬</at> 你来升级~", "  SHEEPDOG UPGRADE "):
            self.assertTrue(is_upgrade_command(valid_cmd), valid_cmd)
        for non_cmd in ("生产服务刚完成了升级", "帮我看看升级方案", "待办"):
            self.assertFalse(is_upgrade_command(non_cmd), non_cmd)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            (repo / "sheepdog").mkdir(parents=True)

            def git(*args: str) -> str:
                cmd = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args]
                return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

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
                    message_id="om_owner_upg_1", chat_id="oc_owner_dm", chat_name="私聊", chat_type="p2p",
                    sender_id="ou_test_owner", sender_name="Matthew", sender_type="user", content="你来升级",
                    msg_type="text", create_time=observe.iso(now_dt + timedelta(seconds=5)), reply_to="om_notice_card_1",
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
                    message_id="om_owner_upg_2", chat_id="oc_owner_dm", chat_name="私聊", chat_type="p2p",
                    sender_id="ou_test_owner", sender_name="Matthew", sender_type="user", content="升级",
                    msg_type="text", create_time=observe.iso(now_dt + timedelta(seconds=6)),
                )
                col._escalation_message(m_upgrade_dup)
                self.assertEqual(len(store.pending_actions()), 1)

                # 对照：若主人引用的是未结 escalation 提问回答「升级」，则正常走 owner_reply 回答提问，不误判为自升级
                store.add_escalation("om_esc_ask_1", "tp_alpha", "oc_owner_dm", "🐕 需要你定 · 升级数据库 [tp_alpha]\n要升级吗？", observe.iso(now_dt))
                m_answer_esc = Message(
                    message_id="om_owner_ans_1", chat_id="oc_owner_dm", chat_name="私聊", chat_type="p2p",
                    sender_id="ou_test_owner", sender_name="Matthew", sender_type="user", content="升级",
                    msg_type="text", create_time=observe.iso(now_dt + timedelta(seconds=7)), reply_to="om_esc_ask_1",
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
                store.set_meta("daemon_runtime", json.dumps({**rt, "pid": os.getpid()}, ensure_ascii=False))
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
                    f'self_open_id = "ou_test_owner"\nsink = "dryrun"\nstate_dir = "{cfg.state_dir}"\n[source]\nenabled = false\n[identity]\nenabled = false\n',
                    encoding="utf-8",
                )
                env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": str(cfg.state_dir), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
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
                f'self_open_id = "ou_test_owner"\nsink = "dryrun"\nstate_dir = "{state_dir}"\n[source]\nenabled = false\n[identity]\nenabled = false\n',
                encoding="utf-8",
            )
            env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": str(state_dir), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}

            # 1. 第一个常驻进程持有 sheepdog.run.lock 排他锁时，第二个常驻进程拿锁失败立即退出 0，绝不覆盖 daemon_runtime 或进 _run_loop
            lock1 = cli._acquire_daemon_lock(state_dir)
            self.assertIsNotNone(lock1)
            try:
                lock2 = cli._acquire_daemon_lock(state_dir)
                self.assertIsNone(lock2)

                err_buf = io.StringIO()
                with mock.patch.dict(os.environ, env), mock.patch("sys.stderr", err_buf), mock.patch("sheepdog.cli._run_loop") as mock_loop:
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
                    message_id="om_concurrent_dup_1", chat_id="oc_test_chat", chat_name="测试群", chat_type="group",
                    sender_id="ou_alice", sender_name="Alice", sender_type="user", content="同一毫秒被双通道扫到的消息",
                    msg_type="text", create_time="2026-10-21T22:00:00+08:00",
                )
                store.upsert_message(m, "dispatch", "at_me", ["at_me"], "tp_bus", received_by="bot")
                store.upsert_message(m, "dispatch", "at_me", ["at_me"], "tp_bus", received_by="user")
                row = store.get_message("om_concurrent_dup_1")
                self.assertIsNotNone(row)
                self.assertEqual(row["message_id"], "om_concurrent_dup_1")
            finally:
                store.close()


class VersionAndObserveTest(unittest.TestCase):
    def test_reg_057_source_attribution_and_targeted_repo_search_guard(self):
        from sheepdog.playbook import source_and_search_guard
        from sheepdog.prompts import interface_section

        guard = source_and_search_guard("tp_ziyi-zhang", bus=False)
        iface = interface_section("tp_ziyi-zhang", bus=False, remind=[15], expire=120)
        for text in (guard, iface):
            self.assertIn("REG-057", text)
            self.assertIn("事实出处与物理介质严格归属", text)
            self.assertIn("代码检索必先定仓", text)
            self.assertIn("~/Code", text)
        self.assertEqual(source_and_search_guard("tp_bus", bus=True), "")
        u_code = "/Use" + "rs/test_user/Code"
        self.assertTrue(observe._is_unbounded_code_search(f'grep -rn "foo" {u_code}/'))
        self.assertTrue(observe._is_unbounded_code_search('rg "bar" ~/Code'))
        self.assertTrue(observe._is_unbounded_code_search(f'find {u_code} -name "*fe-admin*"'))
        self.assertTrue(observe._is_unbounded_code_search(f'find {u_code} -maxdepth 5 -name "*fe-admin*"'))
        self.assertFalse(observe._is_unbounded_code_search(f'find {u_code} -maxdepth 4 -name "*fe-admin*"'))
        self.assertFalse(observe._is_unbounded_code_search(f'find {u_code} -maxdepth 2 -name "*fe-admin*"'))
        self.assertFalse(observe._is_unbounded_code_search(f'cd {u_code} && find . -maxdepth 2 -name "*fe-admin*"'))
        self.assertTrue(observe._is_unbounded_code_search(f'find {u_code} -maxdepth 2 -name "*fe-admin*" && rg "bar" {u_code}'))
        self.assertFalse(observe._is_unbounded_code_search(f'rg "foo" {u_code}/sheepdog/sheepdog'))
        self.assertFalse(observe._is_unbounded_code_search(f"ls {u_code}"))

    def test_reg_070_unbounded_search_ignores_prose_spec_and_array_shallow_find(self):
        from sheepdog.guard import decode_command_line, normalize

        u_code = "/Use" + "rs/test_user/Code"
        # 1) 真实案发场景 A（tp_dashell-zhou step 70）：在 python3 -c 中编写三引号 lamb 规格文档（含 compact transcript 尾部 <truncated N bytes> 及反例命令行）引用 REG-057 规则，绝不误报
        truncated_json_cmd = (
            '"python3 -c \'\\nimport subprocess, json\\n\\n# 准备 lamb 规格说明\\n'
            'spec = \\"\\"\\"# 任务：TapNow Connector 排查\\n\\n## 严格规则与门禁（7.89 / REG-057）\\n'
            f"1. 代码检索必先定仓：严禁对 ~/Code 或 {u_code} 直接裸跑 grep -rn！先用 ls ~/Code 或 git -C <repo> ls-files 定位具体仓库。\\n"
            'grep -rn \\"bad_example\\" ~/Code\\n'
            '2. 物理出处严格归属。\\n<truncated 597 bytes>'
        )
        decoded_trunc = decode_command_line(truncated_json_cmd)
        self.assertTrue(decoded_trunc.startswith("python3 -c '"))
        self.assertIn("\nspec = \"\"\"", decoded_trunc)
        self.assertFalse(observe._is_unbounded_code_search(truncated_json_cmd))
        self.assertFalse(observe._is_unbounded_code_search(decoded_trunc))
        self.assertFalse(observe._is_unbounded_code_search(normalize(decoded_trunc)))

        # 2) 真实案发场景 B（tp_laohu step 9423）：通过 cat << 'EOF' > /tmp/spec.md 写入 Markdown 守规提示（含独立反例行），绝不误报
        heredoc_cmd = (
            "set -e\ncat << 'EOF' > /tmp/lamb_spec.md\n"
            "## Hard Rules\n"
            f"- **Never run `find /Use" + "rs/test_user` or `grep -r ~/Code`** (`REG-057` — use `ls ~/Code` first).\n"
            "rg \"bad_example\" ~/Code\n"
            "EOF\n"
        )
        self.assertFalse(observe._is_unbounded_code_search(heredoc_cmd))
        self.assertFalse(observe._is_unbounded_code_search(normalize(heredoc_cmd)))

        # 3) 真实案发场景 C（tp_klaus-he step 181 & tp_release step 4528）：Python subprocess 列表形式传 -maxdepth [0-3] 浅层定仓 find，对称豁免
        py_find_1 = f'p_find = subprocess.run(["find", "{u_code}", "-maxdepth", "2", "-name", "*fe-admin*"], capture_output=True)'
        py_find_2 = f"find_cmd = ['find', '{u_code}', '-name', 'tapnow-agent', '-maxdepth', '2']"
        self.assertFalse(observe._is_unbounded_code_search(py_find_1))
        self.assertFalse(observe._is_unbounded_code_search(py_find_2))

        # 4) 正向检出：真正的未定仓全目录递归搜索（含 Python 列表无 -maxdepth 或 -maxdepth 5、以及 python3 -c 中拼 shell grep -rn 命令）必须 100% 检出
        self.assertTrue(observe._is_unbounded_code_search(f'subprocess.run(["find", "{u_code}", "-maxdepth", "5", "-name", "*.py"])'))
        self.assertTrue(observe._is_unbounded_code_search(f'subprocess.run(["find", "{u_code}", "-name", "*.py"])'))
        self.assertTrue(observe._is_unbounded_code_search(f'python3 -c \'import subprocess; cmd = "grep -rn \\"secret\\" {u_code}/"; subprocess.run(cmd, shell=True)\''))
        self.assertTrue(observe._is_unbounded_code_search(f'rg -i "订阅缓存|subscription.*cache" {u_code}/'))

        self.assertFalse(observe._is_bus_forbidden_command('python3 .agents/skills/personal-knowledge-assistant/scripts/context_memory.py runtime-context --query "腾讯云"'))
        self.assertTrue(observe._is_bus_forbidden_command("python3 .agents/skills/personal-knowledge-assistant/scripts/context_memory.py capture ..."))
        self.assertTrue(observe._is_bus_forbidden_command("python3 .agents/skills/personal-knowledge-assistant/scripts/pkm.py linear-index --write"))

        kb_tb = ["2026-10-28 21:00:00 ERROR Traceback (most recent call last):", '  File "cli.py", line 1', "KeyboardInterrupt"]
        err_tb = ["2026-10-28 21:00:00 ERROR Traceback (most recent call last):", '  File "cli.py", line 1', "RuntimeError: boom"]
        self.assertTrue(observe._is_kb_interrupt_tb(kb_tb, 0))
        self.assertFalse(observe._is_kb_interrupt_tb(err_tb, 0))

    def test_reg_058_owner_reaction_cleared_on_report_and_handled_receipt(self):
        from sheepdog import outbound
        from sheepdog.engine import write_receipt
        from sheepdog.models import Message
        from sheepdog.source.lark import LarkCliSource

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(self_open_id="ou_owner", state_dir=Path(tmp) / "state")
            cfg.source.enabled = False
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            store.upsert_topic("tp_bus", title="总线", kind="bus", conversation_id="cid_bus")
            store.upsert_topic("tp_alpha", title="Alpha 专席", kind="adopted", conversation_id="cid_alpha")
            store.upsert_topic("tp_beta", title="Beta 专席", kind="adopted", conversation_id="cid_beta")

            # 1) owner_reply (p2p) + owner_instruction (group) 投递给 tp_alpha，status='waiting' 不撤，send_to_owner('report') 自动撤下
            m1 = Message(message_id="om_p2p_1", chat_id="oc_dm", chat_name="私聊", chat_type="p2p", sender_id="ou_owner", sender_name="Matthew", sender_type="user", content="私聊回复", msg_type="text", create_time="2026-10-30T05:00:00+08:00")
            m2 = Message(message_id="om_grp_1", chat_id="oc_grp", chat_name="群聊", chat_type="group", sender_id="ou_owner", sender_name="Matthew", sender_type="user", content="群里指令", msg_type="text", create_time="2026-10-30T05:00:01+08:00")
            store.upsert_message(m1, "dispatch", "owner_reply", ["owner_verified"], "tp_alpha")
            store.upsert_message(m2, "dispatch", "owner_instruction", ["owner_verified", "owner_instruction"], "tp_alpha")
            store.record_dispatch("b_alpha_1", "tp_alpha", ["om_p2p_1", "om_grp_1"])
            store.mark_batch(["om_p2p_1", "om_grp_1"], "b_alpha_1", "tp_alpha", "delivered")
            store.update_topic("tp_alpha", state="running", pending_batch_id="b_alpha_1", dispatched_at="2026-10-30T05:00:02+08:00")
            store.add_ack("om_p2p_1", "oc_dm", "p2p", "ou_owner", "owner_reply", "rx_p2p_1", None, "bot")
            store.add_ack("om_grp_1", "oc_grp", "group", "ou_owner", "owner_instruction", "rx_grp_1", None, "bot")

            write_receipt(cfg, "tp_alpha", "b_alpha_1", {"status": "waiting", "summary": "后台处理中"})
            self.assertEqual(store.get_message("om_p2p_1")["dispatch_state"], "acked")
            self.assertIsNone(store.get_ack("om_p2p_1")["removed_at"])
            self.assertIsNone(store.get_ack("om_grp_1")["removed_at"])

            removed_calls: list[tuple[str, str, str]] = []
            im_src = mock.MagicMock()
            im_src.remove_reaction.side_effect = lambda mid, rid, ident="user": removed_calls.append((mid, rid, ident))
            im_src.send_to_user.return_value = {"message_id": "om_rep_1", "thread_id": ""}
            outbound.send_to_owner(cfg, store, im_src, "report", "tp_alpha", "处理完毕", "已完成任务", fyi=True)
            self.assertEqual(removed_calls, [("om_p2p_1", "rx_p2p_1", "bot"), ("om_grp_1", "rx_grp_1", "bot")])
            self.assertIsNotNone(store.get_ack("om_p2p_1")["removed_at"])
            self.assertIsNotNone(store.get_ack("om_grp_1")["removed_at"])

            # 2) tp_beta 已投递的 owner_reply 与 tp_bus 转交后尚处 pending 的 owner_instruction：tp_bus 交 handled 不误撤，tp_beta 交 handled 仅撤已投递那条
            m3 = Message(message_id="om_top_1", chat_id="oc_tg", chat_name="话题群", chat_type="group", sender_id="ou_owner", sender_name="Matthew", sender_type="user", content="话题回复", msg_type="text", create_time="2026-10-30T05:01:00+08:00")
            m4 = Message(message_id="om_fwd_1", chat_id="oc_grp", chat_name="群聊", chat_type="group", sender_id="ou_owner", sender_name="Matthew", sender_type="user", content="待转交指令", msg_type="text", create_time="2026-10-30T05:01:01+08:00")
            store.upsert_message(m3, "dispatch", "owner_reply", ["owner_verified"], "tp_beta")
            store.upsert_message(m4, "dispatch", "owner_instruction", ["owner_verified", "owner_instruction"], "tp_bus")
            store.record_dispatch("b_beta_1", "tp_beta", ["om_top_1"])
            store.mark_batch(["om_top_1"], "b_beta_1", "tp_beta", "delivered")
            store.add_ack("om_top_1", "oc_tg", "group", "ou_owner", "owner_reply", "rx_top_1", None, "bot")
            store.add_ack("om_fwd_1", "oc_grp", "group", "ou_owner", "owner_instruction", "rx_fwd_1", None, "bot")
            store.forward_message("om_fwd_1", "tp_beta", ["owner_verified", "owner_instruction"])

            write_receipt(cfg, "tp_bus", "b_bus_1", {"status": "handled", "summary": "已转交 Beta"})
            self.assertIsNone(store.get_ack("om_fwd_1")["removed_at"])
            write_receipt(cfg, "tp_beta", "b_beta_1", {"status": "handled", "summary": "已处理话题回复"})
            self.assertIsNotNone(store.get_ack("om_top_1")["removed_at"])
            self.assertIsNone(store.get_ack("om_fwd_1")["removed_at"])

            store.record_dispatch("b_beta_2", "tp_beta", ["om_fwd_1"])
            store.mark_batch(["om_fwd_1"], "b_beta_2", "tp_beta", "delivered")
            write_receipt(cfg, "tp_beta", "b_beta_2", {"status": "handled", "summary": "转交指令已办结"})
            self.assertIsNotNone(store.get_ack("om_fwd_1")["removed_at"])

            # 3) LarkCliSource.remove_reaction 将 identity 透传给 _run_once
            src = LarkCliSource(tz="+08:00", binary="lark-cli")
            with mock.patch.object(src, "_run_once") as m_run:
                src.remove_reaction("om_1", "rx_1", "bot")
                m_run.assert_called_once_with(["im", "reactions", "delete", "--params", json.dumps({"message_id": "om_1", "reaction_id": "rx_1"})], "bot")

    def test_reg_060_vip_resolve_quote_symmetry_and_shallow_find_exemption(self):
        from sheepdog import outbound
        from sheepdog.models import Message

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            roster_file = tmp_path / "roster.toml"
            roster_file.write_text(
                '[[person]]\nopen_id = "ou_vip_sammy"\nname = "Sammy Shi"\ntier = "vip"\nauthority = "2026-10-21 合理就办"\n',
                encoding="utf-8",
            )
            cfg = Config(self_open_id="ou_owner", state_dir=tmp_path / "state", roster_path=str(roster_file))
            cfg.source.enabled = False
            cfg.escalation.open_hours = 100000
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            store.upsert_topic("tp_release", title="生产发版", kind="adopted", conversation_id="cid_rel")

            # 1) 非底线提问（floor=""）：普通同事消息不可 resolve，VIP 在 asked_at 之前发的消息不可 resolve，VIP 在 asked_at 之后发的消息（按 ID 或正文子串）可 resolve
            store.add_send("ik_ask_1", "ask", "tp_release", "om_ask_no_floor", "🐕 需要你定 · 超集合发确认 [tp_release]\n请确认", root_message_id="om_ask_no_floor", key="202610310001", norm_key="202610310001", title="超集合发确认")
            store.add_escalation("om_ask_no_floor", "tp_release", "oc_tg", "🐕 需要你定 · 超集合发确认 [tp_release]\n请确认", "2026-10-31T11:11:27+08:00")

            m_early_vip = Message(message_id="om_vip_early", chat_id="oc_rel", chat_name="发布群", chat_type="group", sender_id="ou_vip_sammy", sender_name="Sammy Shi", sender_type="user", content="早前的同意", msg_type="text", create_time="2026-10-31T10:00:00+08:00")
            m_non_vip = Message(message_id="om_non_vip", chat_id="oc_rel", chat_name="发布群", chat_type="group", sender_id="ou_klaus", sender_name="Klaus", sender_type="user", content="同意合发", msg_type="text", create_time="2026-10-31T12:00:00+08:00")
            m_valid_vip = Message(message_id="om_vip_ok", chat_id="oc_rel", chat_name="发布群", chat_type="group", sender_id="ou_vip_sammy", sender_name="Sammy Shi", sender_type="user", content="按超集 v1.9.0 直接发", msg_type="text", create_time="2026-10-31T12:41:59+08:00")
            for m in (m_early_vip, m_non_vip, m_valid_vip):
                store.upsert_message(m, "dispatch", "vip_sender", [], "tp_release")

            with self.assertRaises(outbound.OutboundError):
                outbound.resolve(cfg, store, None, "tp_release", "普通人不可销假", message_id="om_ask_no_floor", quote="om_non_vip")
            with self.assertRaises(outbound.OutboundError):
                outbound.resolve(cfg, store, None, "tp_release", "提问前的VIP消息不可销假", message_id="om_ask_no_floor", quote="om_vip_early")

            res_ok = outbound.resolve(cfg, store, None, "tp_release", "VIP Sammy 已授权合发", message_id="om_ask_no_floor", quote="om_vip_ok")
            self.assertEqual(res_ok.closed, ["om_ask_no_floor"])
            self.assertEqual(store.get_escalation("om_ask_no_floor")["close_reason"], "resolved")

            # 2) 触碰底线的提问（sends.floor 非空）：VIP 消息严禁越权 resolve，只有主人本人消息可 resolve
            store.add_send("ik_ask_2", "ask", "tp_release", "om_ask_with_floor", "🐕 需要你定 · 删库确认 [tp_release]\n请确认", root_message_id="om_ask_with_floor", key="DEL1", norm_key="DEL1", title="删库确认")
            store.set_send_floor("om_ask_with_floor", "destructive: 删除旧表")
            store.add_escalation("om_ask_with_floor", "tp_release", "oc_tg", "🐕 需要你定 · 删库确认 [tp_release]\n请确认", "2026-10-31T11:11:27+08:00")
            with self.assertRaises(outbound.OutboundError):
                outbound.resolve(cfg, store, None, "tp_release", "VIP不可结底线单", message_id="om_ask_with_floor", quote="om_vip_ok")

            m_owner = Message(message_id="om_owner_ok", chat_id="oc_tg", chat_name="话题群", chat_type="group", sender_id="ou_owner", sender_name="Matthew", sender_type="user", content="同意删除旧表", msg_type="text", create_time="2026-10-31T12:50:00+08:00")
            store.upsert_message(m_owner, "dispatch", "owner_reply", ["owner_verified"], "tp_release")
            res_floor_ok = outbound.resolve(cfg, store, None, "tp_release", "主人已拍板删表", message_id="om_ask_with_floor", quote="同意删除旧表")
            self.assertEqual(res_floor_ok.closed, ["om_ask_with_floor"])

    def test_reg_061_upgrade_notice_changelog_and_interactive_rollback(self):
        from sheepdog import actions, card_callback, outbound, version_watch
        from sheepdog.config import EscalationConfig
        from sheepdog.engine import Collector
        from sheepdog.models import Message

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            (repo / "sheepdog").mkdir(parents=True)
            (repo / "tests").mkdir(parents=True)

            def git(*args: str) -> str:
                cmd = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args]
                return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-q")
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.92"\n', encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(
                "# 回归\n\n## 7.92 旧版（REG-060）\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n| 7.92 | 旧问题 | `t1` |\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "-q", "-m", "feat: release v0.7.92")
            sha_792 = git("rev-parse", "HEAD")

            # 提交 0.7.93 并登记 REGRESSIONS.md
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.93"\n', encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(
                "# 回归\n\n## 7.92 旧版（REG-060）\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n| 7.92 | 旧问题 | `t1` |\n\n"
                "## 7.93 升级通知附带更新说明与一键回滚按钮（REG-061）\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n"
                "| 7.93 | 升级通知缺少更新了什么和解决了什么问题，或缺少一键回滚按钮（REG-061） | `t2` |\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "-q", "-m", "feat(version): add changelog and rollback button in v0.7.93")
            sha_793 = git("rev-parse", "HEAD")

            cfg = Config(self_open_id="ou_test_owner", state_dir=tmp_path / "state")
            cfg.escalation = EscalationConfig(
                chat_ids=["oc_owner_dm"],
                header_regex=r"^🐕 需要你定 · [^\n]*\[(?P<topic>tp_[a-z0-9_.-]+)\]",
                attribution_regex=r"\[(?P<topic>tp_[a-z0-9_.-]+)\]",
                owner_shortcut=True,
            )
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            rt_792 = {"version": "0.7.92", "git_sha": sha_792, "display": f"0.7.92 ({sha_792[:7]})", "pid": 43210}
            store.set_meta("daemon_runtime", json.dumps(rt_792, ensure_ascii=False))

            # 1) 执行从 0.7.92 -> 0.7.93 的热升级：自动从 REGRESSIONS.md diff 提取「更新了什么」和「解决了什么问题」，并写入回滚目标
            sent_notices: list[str] = []
            disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
            disp.runtime_info = rt_792
            disp._notify_owner = lambda text, what, topic_id="", **kw: (sent_notices.append(text) or True)

            res_upg = disp.upgrade(no_pull=True, channel="stable", repo_dir=repo)
            self.assertTrue(res_upg["restart_requested"])
            self.assertEqual(len(sent_notices), 1)
            upg_text = sent_notices[0]
            self.assertTrue(upg_text.startswith("🐕 汇报 · 升级完成 [tp_bus]\n"))
            self.assertIn("更新了什么", upg_text)
            self.assertIn("7.93 升级通知附带更新说明与一键回滚按钮", upg_text)
            self.assertIn("解决了什么问题", upg_text)
            self.assertIn("升级通知缺少更新了什么和解决了什么问题", upg_text)
            self.assertIn(f"回滚至 0.7.92 ({sha_792[:7]})", upg_text)

            saved_rb = json.loads(store.get_meta("version_rollback_target") or "{}")
            self.assertEqual(saved_rb.get("version"), "0.7.92")
            self.assertEqual(saved_rb.get("git_sha"), sha_792)

            # 2) outbound.owner_content 为「🐕 汇报 · 升级完成」卡片挂载 [⏪ 回滚至 0.7.92 (<sha>)] 红色胶囊按钮
            _, upg_card = outbound.owner_content(cfg, upg_text)
            self.assertIsNotNone(upg_card)
            cs = upg_card["body"]["elements"][-1]
            self.assertEqual(cs["tag"], "column_set")
            btns = [col["elements"][0] for col in cs["columns"]]
            self.assertEqual(btns[0]["type"], "danger")
            self.assertIn("⏪ 回滚至 0.7.92", btns[0]["text"]["content"])
            self.assertEqual(btns[0]["value"]["sd_action"], "version_rollback")
            self.assertEqual(btns[0]["value"]["target_sha"], sha_792[:7])

            # 3) 主人点击 [⏪ 回滚至 0.7.92] 卡片按钮：原地刷绿卡、入队 rollback 动作，常驻进程执行后代码库回退至 0.7.92 且保护无关脏文件
            (repo / "unrelated_notes.txt").write_text("keep me\n", encoding="utf-8")
            store.add_bot_outbox("om_upg_done_card", "tp_bus", False, upg_text, "2026-10-31T17:45:00+08:00")
            fake_source = mock.MagicMock()
            col = Collector(cfg, store, fake_source, Roster(), SecurityConfig())
            rb_ev = {
                "type": "card.action.trigger", "event_id": "ev_rb_061", "message_id": "om_upg_done_card",
                "chat_id": "oc_owner_dm", "operator_id": "ou_test_owner", "action_tag": "button",
                "action_value": btns[0]["value"], "timestamp": "2026-10-31T17:46:00+08:00",
            }
            m_rb = card_callback.process_card_action_event(rb_ev, cfg=cfg, store=store, source=fake_source)
            self.assertIsNotNone(m_rb)
            col.ingest([m_rb], received_by="bot")
            self.assertEqual(fake_source.patch_card.call_count, 1)
            patched_rb = fake_source.patch_card.call_args.args[1]
            self.assertEqual(patched_rb["header"]["template"], "green")
            self.assertIn("⏪ 回滚至 0.7.92", json.dumps(patched_rb, ensure_ascii=False))

            # 二次重复点击防重拦截
            self.assertIsNone(card_callback.process_card_action_event({**rb_ev, "event_id": "ev_rb_061_dup"}, cfg=cfg, store=store, source=fake_source))

            pending_rb = store.pending_actions()
            self.assertEqual(len(pending_rb), 1)
            rb_args = json.loads(pending_rb[0]["args_json"])
            self.assertTrue(rb_args.get("rollback"))
            self.assertEqual(rb_args.get("target_sha"), sha_792[:7])

            sent_notices.clear()
            disp.runtime_info = {"version": "0.7.93", "git_sha": sha_793, "display": f"0.7.93 ({sha_793[:7]})", "pid": 43210}
            with mock.patch("sheepdog.default_repo_dir", return_value=repo):
                actions.run_pending(disp)
            self.assertEqual(git("rev-parse", "HEAD"), sha_792)
            self.assertEqual(sheepdog.read_disk_version(repo), "0.7.92")
            self.assertEqual((repo / "unrelated_notes.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(len(sent_notices), 1)
            self.assertIn("⏪ 已从", sent_notices[0])
            self.assertIn("回滚至 **0.7.92", sent_notices[0])

    def test_reg_065_observe_reused_subagent_cid_and_cli_lamb_exit_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(self_open_id="ou_test_owner", state_dir=tmp_path / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            ref_now = datetime(2026, 10, 31, 22, 45, 0, tzinfo=observe.TZ)

            store.upsert_topic("tp_jev", title="Decision 服务", kind="dynamic", conversation_id="cid_jev_parent")
            store.upsert_topic("tp_rel", title="生产发版", kind="adopted", conversation_id="cid_rel_parent")

            cid_reused = "06a8c3ff-11b5-479f-8374-6000c1ba1341"
            # 1) 同一 subagent conversation_id 跨 3 次历史已完结任务（done / timeout / done）+ 1 次当前 running 任务顺序复用：
            #    绝对不触发「羔羊同一 conversation_id 重复登记」误报
            store.create_lamb("lamb_hist_1", "tp_jev", kind="subagent", task_title="PR 7832 核查", conversation_id=cid_reused, state="done", started_at=observe.iso(ref_now - timedelta(days=4)), last_act_at=observe.iso(ref_now - timedelta(days=4)), finished_at=observe.iso(ref_now - timedelta(days=4)))
            store.create_lamb("lamb_hist_2", "tp_jev", kind="subagent", task_title="实现多通道路由", conversation_id=cid_reused, state="timeout", started_at=observe.iso(ref_now - timedelta(hours=4)), last_act_at=observe.iso(ref_now - timedelta(hours=3, minutes=40)), finished_at=observe.iso(ref_now - timedelta(hours=3, minutes=40)))
            store.create_lamb("lamb_hist_3", "tp_jev", kind="subagent", task_title="部署测试环境验证", conversation_id=cid_reused, state="done", started_at=observe.iso(ref_now - timedelta(hours=3, minutes=30)), last_act_at=observe.iso(ref_now - timedelta(hours=3)), finished_at=observe.iso(ref_now - timedelta(hours=3)))
            store.create_lamb("lamb_cur_run", "tp_jev", kind="subagent", task_title="千次并发压测", conversation_id=cid_reused, state="running", started_at=observe.iso(ref_now - timedelta(minutes=10)), last_act_at=observe.iso(ref_now - timedelta(minutes=1)))

            # 2) 长周期 CLI 羔羊（started_at 在 12 分钟前，进程在世期间由 _tick_lambs 刷新过 last_act_at，刚退出 10 秒）：
            #    首先验证 _tick_lambs 对存活 PID 会刷新 last_act_at
            disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
            store.create_lamb("lamb_cli_long", "tp_rel", kind="cli", task_title="执行 202610310018 DAG", pid=os.getpid(), state="running", started_at=observe.iso(ref_now - timedelta(minutes=12)), last_act_at=observe.iso(ref_now - timedelta(minutes=12)))
            with mock.patch("sheepdog.engine._now", return_value=ref_now - timedelta(seconds=10)):
                disp._tick_lambs()
            lb_refreshed = store.get_lamb("lamb_cli_long")
            self.assertEqual(lb_refreshed["state"], "running")
            self.assertEqual(lb_refreshed["last_act_at"], observe.iso(ref_now - timedelta(seconds=10)))

            # 进程在 4 秒前刚退出（PID 不存在），但距最近一次存活心跳（last_act_at）仅 10 秒（< 2 分钟）：observe 处于正常收割窗口，绝不误报
            dead_pid = 999991
            store.update_lamb("lamb_cli_long", pid=dead_pid)
            res_ok = observe.run_observe(cfg, now_dt=ref_now, since_minutes=60.0, check_processes=False, check_log=False)
            self.assertEqual(res_ok["warns"], [], f"Expected 0 warns for sequential subagent reuse & just-exited CLI lamb, got: {res_ok['warns']}")

            # 3) 正向检出：若出现 2 条同时处于 running 的相同 conversation_id，且死掉的 CLI 羔羊距 last_act_at 已超过 2 分钟未收割，必须双双精准告警
            store.create_lamb("lamb_dup_run", "tp_jev", kind="subagent", task_title="并发重复登记", conversation_id=cid_reused, state="running", started_at=observe.iso(ref_now - timedelta(minutes=2)), last_act_at=observe.iso(ref_now - timedelta(minutes=1)))
            store.update_lamb("lamb_cli_long", last_act_at=observe.iso(ref_now - timedelta(minutes=3)))
            res_warn = observe.run_observe(cfg, now_dt=ref_now, since_minutes=60.0, check_processes=False, check_log=False)
            self.assertTrue(any("羔羊同一 conversation_id 重复登记 2 行：tp_jev cid=06a8c3ff" in w for w in res_warn["warns"]), res_warn["warns"])
            self.assertTrue(any(f"CLI 羔羊 lamb_cli_long（tp_rel，pid={dead_pid}）进程已退出但状态仍为 running" in w for w in res_warn["warns"]), res_warn["warns"])

    def test_7119_self_evolve_digest_and_tick_and_tmp_md_exempt_and_lamb_summary(self):
        """7.119（REG-087）：
        1. `evolve.ensure_evolve_skill_file` 自动生成默认 `skills/sheepdog-evolve/SKILL.md` 模板（零机密词泄漏）；
        2. `evolve.build_evolve_digest` 汇总 D1~D6 六维自进化体检指标，且 `evolve.run_evolve_tick` 在检测到异常或 `force=True` 时自动注册 `tp_sheepdog`、写入 `system_messages` 入账信号并更新 `meta['self_evolve_state']`；
        3. `observe` 对 `tp_bus` 向 `/tmp/*.md` 或 `/private/tmp/*.md` 写入临时交接草稿予以豁免，但仍拦截向业务仓库或笔记库调用 `write_file`；
        4. `_summarize_cli_lamb_log` 过滤 `Container ... Waiting/Healthy/Running/Recreate` 噪音与 JSON 数组属性碎片（`"Tags": [],`），且末尾含 `"ok":true` / ` is live on ` 时清除前置瞬态重试报错。
        """
        from sheepdog import evolve
        from sheepdog.config import SelfEvolveConfig
        from sheepdog.engine import _summarize_cli_lamb_log

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            skill_path = tmp_path / "skills" / "sheepdog-evolve" / "SKILL.md"
            cfg = Config(
                self_open_id="ou_test_owner",
                state_dir=tmp_path / "state",
                self_evolve=SelfEvolveConfig(
                    enabled=True,
                    interval_minutes=60.0,
                    topic_id="tp_sheepdog",
                    title="🐕 Sheepdog 自进化管家",
                    skill_path=str(skill_path),
                    auto_upgrade=True,
                    trigger_on_override=True,
                ),
            )
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            # 1) 默认 SKILL.md 模板自动创建且包含六大职责结构
            ensured = evolve.ensure_evolve_skill_file(cfg)
            self.assertTrue(ensured.is_file())
            skill_txt = ensured.read_text(encoding="utf-8")
            self.assertIn("name: sheepdog-evolve", skill_txt)
            self.assertIn("六大自进化职责", skill_txt)

            # 2) D1~D6 摘要与 run_evolve_tick 触发投递
            ref_now = datetime(2026, 11, 3, 22, 30, 0, tzinfo=observe.TZ)
            store.add_mail("tp_alpha", "tp_sheepdog", "gate-1", "建议新增一条防呆门禁")
            digest = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=ref_now)
            self.assertTrue(digest["needs_action"])
            self.assertTrue(any(r.startswith("unread_peer_mail") for r in digest["action_reasons"]))

            tick_res = evolve.run_evolve_tick(cfg, store, now_dt=ref_now)
            self.assertTrue(tick_res["triggered"])
            self.assertIn("self_evolve_digest_", tick_res["message_id"])
            tp_sd = store.get_topic("tp_sheepdog")
            self.assertIsNotNone(tp_sd)
            st_saved = json.loads(store.get_meta("self_evolve_state") or "{}")
            self.assertEqual(st_saved.get("last_message_id"), tick_res["message_id"])

            # 3) observe 豁免 tp_bus 写 /tmp/*.md，但仍拦截写业务文件
            tp_dir = tmp_path / "brain" / "cid_bus_7119" / ".system_generated" / "logs"
            tp_dir.mkdir(parents=True, exist_ok=True)
            store.upsert_topic("tp_bus", title="收件箱", kind="bus", conversation_id="cid_bus_7119")
            tp_file = tp_dir / "transcript.jsonl"
            tp_file.write_text(
                json.dumps({
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": ref_now.isoformat(timespec="seconds"),
                    "tool_calls": [{"name": "write_file", "args": {"Path": "/tmp/reply_draft.md", "Content": "ok"}}],
                }) + "\n",
                encoding="utf-8",
            )
            with mock.patch("sheepdog.sink.agentapi.transcript_path", return_value=tp_file):
                res_tmp = observe.run_observe(cfg, now_dt=ref_now, since_minutes=60.0, check_processes=False, check_log=False)
            self.assertFalse(any("tp_bus Boundary Guard" in w for w in res_tmp["warns"]), res_tmp["warns"])

            tp_file.write_text(
                json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": ref_now.isoformat(timespec="seconds"),
                    "tool_calls": [{"name": "write_file", "args": {"Path": "/var/workspace/repo/main.py", "Content": "x"}}],
                }) + "\n",
                encoding="utf-8",
            )
            with mock.patch("sheepdog.sink.agentapi.transcript_path", return_value=tp_file):
                res_bad = observe.run_observe(cfg, now_dt=ref_now, since_minutes=60.0, check_processes=False, check_log=False)
            self.assertTrue(any("tp_bus Boundary Guard" in w for w in res_bad["warns"]), res_bad["warns"])

            # 4) _summarize_cli_lamb_log 清洗 Container 噪音与 JSON 数组属性碎片
            raw_docker = (
                "Error response from daemon: removal of container abc is already in progress\n"
                "deploy: retry 1: ssh failed\n"
                "Container cinemap-db Waiting\n"
                "Container cinemap-db Healthy\n"
                '{"ok":true,"version":"4b783ca8a5"}\n'
                "deploy: cinemap/web:4b783ca8a5 is live on ubuntu@198.51.100.1\n"
            )
            sum_docker, err_docker = _summarize_cli_lamb_log(raw_docker)
            self.assertFalse(err_docker)
            self.assertNotIn("Container cinemap-db", sum_docker)
            self.assertNotIn("Error response from daemon", sum_docker)
            self.assertIn("deploy: cinemap/web:4b783ca8a5 is live", sum_docker)

            raw_tccli = (
                "Error: transient\n"
                "=== Organization members and policies ===\n"
                "{\n"
                '  "Tags": [],\n'
                '{"Total": 1, "Items": [1], "RequestId": "req-123"}\n'
            )
            sum_tccli, err_tccli = _summarize_cli_lamb_log(raw_tccli)
            self.assertFalse(err_tccli)
            self.assertNotIn('"Tags": []', sum_tccli)
            self.assertIn("JSON(Total=1", sum_tccli)

    def test_reg088_stale_ls_lock_preemption_and_agentapi_env_reload(self):
        """1.0.1（REG-088）：
        1) language_server 重启换新动态端口后，AgentApiSink 遇 connection refused 自动从 state_dir/antigravity_env.json 热重载最新端口与 token 并重试成功；
        2) _acquire_daemon_lock 检测到锁持有者绑定的旧 ls_address 已失联、而本进程携带存活可达的新 ls_address 时，自动 SIGTERM 回收旧孤儿进程并接管单实例锁；若锁持有者宿主仍存活则正常礼让不误杀；
        3) _queued 对未执行的 init 动作幂等合并，防止 sidecar 周期拉起时堆积重复 init 动作。
        """
        import socket
        from types import SimpleNamespace
        from sheepdog.sink.agentapi import AgentApiSink, is_ls_address_reachable, load_live_agentapi_env, save_live_agentapi_env

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir(parents=True, exist_ok=True)

            # 分配一个真实存活监听端口 (live_srv) 和一个已关闭的死端口 (dead_port)
            live_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            live_srv.bind(("127.0.0.1", 0))
            live_srv.listen(128)
            self.addCleanup(live_srv.close)
            live_addr = f"127.0.0.1:{live_srv.getsockname()[1]}"

            dead_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            dead_srv.bind(("127.0.0.1", 0))
            dead_addr = f"127.0.0.1:{dead_srv.getsockname()[1]}"
            dead_srv.close()

            self.assertTrue(is_ls_address_reachable(live_addr))
            self.assertFalse(is_ls_address_reachable(dead_addr))

            # 1) 模拟新 sidecar 启动时将存活宿主凭据写入 antigravity_env.json
            fresh_env = {
                "ANTIGRAVITY_AGENTAPI_EXE": "/Applications/Antigravity.app/Contents/Resources/bin/language_server",
                "ANTIGRAVITY_LS_ADDRESS": live_addr,
                "ANTIGRAVITY_CSRF_TOKEN": "fresh-csrf-token-101",
            }
            with mock.patch.dict(os.environ, fresh_env, clear=False):
                self.assertTrue(save_live_agentapi_env(state_dir))

            loaded = load_live_agentapi_env(state_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["ANTIGRAVITY_LS_ADDRESS"], live_addr)
            self.assertEqual(loaded["ANTIGRAVITY_CSRF_TOKEN"], "fresh-csrf-token-101")

            # 旧常驻进程 os.environ 仍拿着 dead_addr，第一次 _call 报 connection refused，自动重载 antigravity_env.json 后第二次成功
            stale_env = {
                "ANTIGRAVITY_AGENTAPI_EXE": "/Applications/Antigravity.app/Contents/Resources/bin/language_server",
                "ANTIGRAVITY_LS_ADDRESS": dead_addr,
                "ANTIGRAVITY_CSRF_TOKEN": "stale-csrf-token-000",
            }
            seen_addrs: list[str] = []

            def fake_run(cmd, **kwargs):
                cur_a = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
                seen_addrs.append(cur_a)
                if cur_a == dead_addr:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="",
                        stderr=f'rpc error: code = Unavailable desc = "transport: Error while dialing: dial tcp {dead_addr}: connect: connection refused"',
                    )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout='{"response":{"sendMessage":{"ok":true}}}', stderr=""
                )

            with mock.patch.dict(os.environ, stale_env, clear=False), \
                 mock.patch("sheepdog.sink.agentapi.subprocess.run", side_effect=fake_run):
                sink = AgentApiSink(state_dir)
                sink.send_message("conv_reg088", "hello after ls restart")
                self.assertEqual(seen_addrs, [dead_addr, live_addr])
                self.assertEqual(os.environ["ANTIGRAVITY_LS_ADDRESS"], live_addr)
                self.assertEqual(os.environ["ANTIGRAVITY_CSRF_TOKEN"], "fresh-csrf-token-101")

            # 2) 子进程持有 sheepdog.run.lock 且绑定在 dead_addr，当前进程携带 live_addr 调用 _acquire_daemon_lock 应抢占成功
            lock_script = (
                "import fcntl, json, sys, time\n"
                "p = sys.argv[1]; addr = sys.argv[2]\n"
                "f = open(p, 'a+', encoding='utf-8')\n"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "f.seek(0); f.truncate()\n"
                "f.write(json.dumps({'pid': __import__('os').getpid(), 'ppid': 1, 'ls_address': addr}) + '\\n')\n"
                "f.flush()\n"
                "print('LOCKED', flush=True)\n"
                "time.sleep(30)\n"
            )
            lock_path = state_dir / "sheepdog.run.lock"
            orphan_proc = subprocess.Popen(
                [sys.executable, "-c", lock_script, str(lock_path), dead_addr],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.addCleanup(lambda: (orphan_proc.kill(), orphan_proc.wait()) if orphan_proc.poll() is None else None)
            self.assertEqual(orphan_proc.stdout.readline().strip(), "LOCKED")

            with mock.patch.dict(os.environ, fresh_env, clear=False):
                lk = cli._acquire_daemon_lock(state_dir)
                self.assertIsNotNone(lk, "Expected _acquire_daemon_lock to preempt orphan holding dead ls_address")
                try:
                    holder_now = json.loads(lock_path.read_text(encoding="utf-8"))
                    self.assertEqual(holder_now["pid"], os.getpid())
                    self.assertEqual(holder_now["ls_address"], live_addr)
                    # 且当锁持有者本身绑定在存活的 live_addr 时，另一个进程不得抢占
                    should_p, _, _ = cli._should_preempt_stale_holder(holder_now, live_addr)
                    self.assertFalse(should_p)
                finally:
                    cli._release_daemon_lock(lk)
            orphan_proc.wait(timeout=3)

            # 3) _queued 对 pending 的 init 动作幂等复用同一条记录
            cfg = Config(state_dir=state_dir)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            args = SimpleNamespace(dry_run=False)
            with mock.patch("sheepdog.actions.is_resident", return_value=False):
                rc1 = cli._queued(args, store, cfg, "init", {})
                rc2 = cli._queued(args, store, cfg, "init", {})
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            inits = [a for a in store.pending_actions() if a["kind"] == "init"]
            self.assertEqual(len(inits), 1)

            # 4) 美区生产 GCP 项目 (tpn-prod-506507) 为同事开通只读日志权限前未找 Sammy 确认合规口径，check_ask 拦截（REG-088）
            from sheepdog import outbound
            with self.assertRaises(outbound.OutboundError) as ctx_us:
                outbound.check_ask_pre_flight_guard(
                    cfg, store, "tp_kaifeng",
                    "Kai申请生产GCP日志只读权限",
                    "Kai申请开通tpn-prod-506507日志只读权限(roles/logging.viewer)用于排查语音丢句与延迟，建议选A\n---more---\n"
                    "事情：Kai Feng 提请为他的 Google 账号开通 TapNow 生产 GCP 项目（tpn-prod-506507）的日志只读权限（roles/logging.viewer）。\n"
                    "选项：\nA. 同意开通（若 matthewohmygosh 账号无该项目 IAM 权限，由主人操作或指引找 Sammy Shi 开通）\nB. 暂不开通，由专席代拉日志包\n"
                    "建议：选 A。\n回复：打 A / B",
                )
            self.assertIn("tpn-prod-506507", str(ctx_us.exception))
            self.assertIn("Sammy", str(ctx_us.exception))

            # 已找 Sammy Shi 确认合规口径后放行
            outbound.check_ask_pre_flight_guard(
                cfg, store, "tp_kaifeng",
                "Kai申请生产GCP日志只读权限",
                "已向 Sammy Shi 确认美区只读合规口径，请示开通 tpn-prod-506507 日志只读权限\n---more---\n"
                "事情：已向 Sammy Shi 确认美区只读合规口径允许开通 roles/logging.viewer（tpn-prod-506507）。\n"
                "选项：\nA. 同意由专席按合规口径配置并回读\nB. 暂不开通\n建议：选 A。\n回复：打 A / B",
            )

    def test_reg092_unawakened_system_message_and_duty_stuck_queue_self_heal(self):
        """REG-092 (v1.1.3):
        1) 宿主写入 SYSTEM_MESSAGE 后未启动 PLANNER_RESPONSE（post_steps=[SYSTEM_MESSAGE]）：
           - observe L3 探针 2 检出 SYSTEM_MESSAGE 静默并写入 stop 自愈标记；
           - Dispatcher._check_stopped_in_flight (name='agentapi') 在 >=180s 时识别未唤醒 SYSTEM_MESSAGE 并自动发断点续跑唤醒；
        2) 当上一批因宿主未触发推理而超时标为 missed 且看板标为 lost、话题已回 active（pending_batch_id=None）时，
           新到达的 pending 消息不会因看板 lost 永久死锁：
           - observe --self-heal 阶段 3 自动将未触发推理的 missed 批次消息重回 pending、写入 duty_reset 并重置 duty_cards 为 idle；
           - Dispatcher._dispatch_topic (name='agentapi') 同样自动解除可投递态下的看板 STUCK 死锁并合并补投。
        """
        from sheepdog import duty
        from sheepdog.engine import Dispatcher
        from sheepdog.models import Message
        from sheepdog.roster import parse_roster

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(self_open_id="ou_owner", state_dir=state_dir)
            cfg.source.enabled = False
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            cid = "55c0efa0-266f-4caf-b0fb-5f05085f7c49"
            tid = "tp_infra-migration-ops"
            t_log = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
            t_log.parent.mkdir(parents=True, exist_ok=True)

            # Part A: running 态下 SYSTEM_MESSAGE 落盘但无 PLANNER_RESPONSE -> observe L3 探针 2 检出并写 stop 自愈标记
            store.upsert_topic(
                tid,
                title="项目·Infra迁移改造",
                kind="dynamic",
                conversation_id=cid,
                state="running",
                pending_batch_id="b20261104055452-infra-migration-ops",
                dispatched_at="2026-11-04T05:54:52+08:00",
            )
            steps_a = [
                {
                    "step_index": 3262,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-03T21:31:52Z",
                    "content": "上一轮已完成",
                },
                {
                    "step_index": 3263,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-03T21:54:52Z",
                    "content": "[sheepdog] 新信号批次 `b20261104055452-infra-migration-ops`（topic `tp_infra-migration-ops`，共 1 条）",
                },
            ]
            t_log.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps_a) + "\n", encoding="utf-8")

            now_a = datetime.fromisoformat("2026-11-04T06:00:00+08:00")
            obs_a = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_a,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertIn(f"{tid}:b20261104055452-infra-migration-ops", obs_a["self_healed"])
            self.assertTrue(any("SYSTEM_MESSAGE 已 DONE 静默" in w for w in obs_a["warns"]))

            # Part B: 批次已超时变为 missed，会话回到 active (pending_batch_id=None)，看板标为 lost，新消息到达 pending 超 5m
            m_missed = Message(
                message_id="om_missed_0554",
                chat_id="oc_infra",
                chat_name="Infra 迁移改造",
                chat_type="group",
                sender_id="ou_ziyi",
                sender_name="Ziyi Zhang",
                sender_type="user",
                content="@Matthew Hong 05:54 联调进度",
                msg_type="text",
                create_time="2026-11-04T05:54:00+08:00",
            )
            m_pending = Message(
                message_id="om_pending_0728",
                chat_id="oc_infra",
                chat_name="Infra 迁移改造",
                chat_type="group",
                sender_id="ou_ziyi",
                sender_name="Ziyi Zhang",
                sender_type="user",
                content="@Matthew Hong 07:28 收尾验证",
                msg_type="text",
                create_time="2026-11-04T07:28:00+08:00",
            )
            store.upsert_message(m_missed, "dispatch", "at_me", ["owner:infra-migration-ops"], tid)
            store.upsert_message(m_pending, "dispatch", "at_me", ["owner:infra-migration-ops"], tid)
            store.record_dispatch("b20261104055452-infra-migration-ops", tid, ["om_missed_0554"], state="sent")
            with store.tx() as c:
                c.execute(
                    "UPDATE dispatches SET sent_at='2026-11-04T05:54:52+08:00', state='missed', error='回执超时（adopted 不重投）' WHERE batch_id='b20261104055452-infra-migration-ops'"
                )
                c.execute(
                    "UPDATE messages SET dispatch_state='missed', batch_id='b20261104055452-infra-migration-ops' WHERE message_id='om_missed_0554'"
                )
            store.update_topic(tid, state="active", pending_batch_id=None, dispatched_at="2026-11-04T05:54:52+08:00")
            store.upsert_card(
                tid,
                run_state=duty.LOST,
                detail="批次 05:54 送出后会话记录 134 分钟完全不动",
                last_act_at="2026-11-04T05:31:52+08:00",
                stuck_since="2026-11-04T05:54:52+08:00",
                queued=1,
                no_record=0,
            )

            # 1) 测试 Dispatcher._dispatch_topic (sink.name='agentapi') 直接自动解除失联死锁并合并重投 om_missed_0554 + om_pending_0728
            sent_payloads: list[tuple[str, str]] = []

            class FakeAgentApiSink:
                name = "agentapi"
                host = "antigravity"

                def send_message(self, conv_id: str, prompt: str, **_kw):
                    sent_payloads.append((conv_id, prompt))

            now_b = datetime.fromisoformat("2026-11-04T08:07:02+08:00")
            with (
                mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(root)}, clear=False),
                mock.patch("sheepdog.engine._now", return_value=now_b),
            ):
                disp = Dispatcher(cfg, store, {"antigravity": FakeAgentApiSink()}, parse_roster({"session": []}))
                t_row = store.get_topic(tid)
                rows = store.pending_for_topic(tid)
                self.assertEqual([r["message_id"] for r in rows], ["om_pending_0728"])
                res_d = disp._dispatch_topic(t_row, rows)
                self.assertEqual(res_d["sent"], 2)
                self.assertEqual(len(sent_payloads), 1)
                self.assertIn("om_missed_0554", sent_payloads[0][1])
                self.assertIn("om_pending_0728", sent_payloads[0][1])
                self.assertEqual(store.get_dispatch("b20261104055452-infra-migration-ops")["state"], "redelivered")

            # 2) 同样状态还原后测试 observe.run_observe(self_heal=True) 阶段 3 自动自愈解锁
            with store.tx() as c:
                c.execute(
                    "UPDATE dispatches SET state='missed', error='回执超时（adopted 不重投）' WHERE batch_id='b20261104055452-infra-migration-ops'"
                )
                c.execute(
                    "UPDATE messages SET dispatch_state='missed', batch_id='b20261104055452-infra-migration-ops' WHERE message_id='om_missed_0554'"
                )
                c.execute(
                    "UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE message_id='om_pending_0728'"
                )
            store.update_topic(tid, state="active", pending_batch_id=None, dispatched_at="2026-11-04T05:54:52+08:00")
            store.upsert_card(
                tid,
                run_state=duty.LOST,
                detail="批次 05:54 送出后会话记录 134 分钟完全不动",
                last_act_at="2026-11-04T05:31:52+08:00",
                stuck_since="2026-11-04T05:54:52+08:00",
                queued=1,
                no_record=0,
            )
            now_b = datetime.fromisoformat("2026-11-04T08:07:02+08:00")
            obs_b = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_b,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertIn(f"{tid}:duty_stuck_queue_unblock", obs_b["self_healed"])
            self.assertFalse(any("Queue Head-of-Line SLA" in w for w in obs_b["warns"]))
            self.assertEqual(store.get_card(tid)["run_state"], duty.IDLE)
            self.assertEqual(
                [r["message_id"] for r in store.pending_for_topic(tid)],
                ["om_missed_0554", "om_pending_0728"],
            )

    def test_reg093_native_ws_transient_handshake_reconnect_grace_and_endpoint_cache(self):
        """REG-093: 原生 WebSocket 瞬时 TLS/握手超时进入 reconnecting + 5 分钟 endpoint 缓存保留 + observe 10 分钟宽限期自愈唤醒与超时告警区分。"""
        from sheepdog.source import feishu_ws

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(self_open_id="ou_owner", state_dir=state_dir)
            cfg.source.enabled = False
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            ps_only_run = "24169 Wed Nov  4 00:56:02 2026 /opt/homebrew/bin/sheepdog run\n"
            now_dt = datetime.fromisoformat("2026-11-04T09:07:13+08:00")

            # 1) 瞬时 TLS 握手超时（距上次 pong 仅 3.9 分钟 < 10 分钟）：归入 info + self_heal 写入 .wakeup，不报 warn
            store.set_meta(
                "identity_consumer",
                json.dumps(
                    {
                        "mode": "native_ws",
                        "state": "down",
                        "pid": 24169,
                        "started_at": "2026-11-04T08:26:34+08:00",
                        "ready_at": "2026-11-04T08:26:36+08:00",
                        "last_pong_at": "2026-11-04T09:03:21+08:00",
                        "restarts": 12,
                        "last_error": "_ssl.c:993: The handshake operation timed out",
                        "conn_id": "7704106114829324048",
                    },
                    ensure_ascii=False,
                ),
            )
            obs_grace = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_dt,
                check_processes=True,
                check_log=False,
                ps_output=ps_only_run,
                self_heal=True,
            )
            self.assertFalse(any("原生 WebSocket 异常" in w for w in obs_grace["warns"]))
            self.assertTrue(any("原生 WebSocket 瞬时断线重连中" in i for i in obs_grace["infos"]))
            self.assertIn("native_ws:wakeup_poll", obs_grace["self_healed"])
            self.assertTrue((cfg.state_dir / ".wakeup").exists())

            # 2) 持续失联 >= 10 分钟（距上次 pong 15 分钟）：必须照常报 warn
            store.set_meta(
                "identity_consumer",
                json.dumps(
                    {
                        "mode": "native_ws",
                        "state": "reconnecting",
                        "pid": 24169,
                        "started_at": "2026-11-04T08:26:34+08:00",
                        "ready_at": "2026-11-04T08:26:36+08:00",
                        "last_pong_at": "2026-11-04T08:50:00+08:00",
                        "restarts": 40,
                        "last_error": "_ssl.c:993: The handshake operation timed out",
                    },
                    ensure_ascii=False,
                ),
            )
            obs_prolonged = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_dt,
                check_processes=True,
                check_log=False,
                ps_output=ps_only_run,
                self_heal=True,
            )
            self.assertTrue(any("原生 WebSocket 异常" in w for w in obs_prolonged["warns"]))

            # 3) FeishuNativeWSClient._serve_once 在 endpoint_fetcher 连续抖动 2 次时仍可复用 300s 内的 _last_ep
            clock_val = [100.0]
            fetch_n = [0]

            def flaky_fetcher(*_a):
                fetch_n[0] += 1
                if fetch_n[0] == 1:
                    return {"url": "wss://example.invalid/ws/v2?service_id=1&device_id=c1", "client_config": {}}
                raise TimeoutError("_ssl.c:993: The handshake operation timed out")

            ws_cli = feishu_ws.FeishuNativeWSClient(
                credentials={"app_id": "cli_a", "app_secret": "sec_a"},
                on_event=lambda _e: None,
                endpoint_fetcher=flaky_fetcher,
                clock=lambda: clock_val[0],
            )
            ws_cli._state["ready_at"] = "2026-11-04T08:26:36+08:00"
            with mock.patch.object(
                feishu_ws,
                "_RawWebSocket",
                side_effect=TimeoutError("_ssl.c:993: The handshake operation timed out"),
            ):
                with self.assertRaises(TimeoutError):
                    ws_cli._serve_once()
                self.assertIsNotNone(ws_cli._last_ep)
                clock_val[0] = 120.0
                with self.assertRaises(TimeoutError):
                    ws_cli._serve_once()
                # 第二次抖动后 _last_ep 仍未在 300s 窗口内被清空
                self.assertIsNotNone(ws_cli._last_ep)

    def test_reg094_stop_hook_none_decision_and_cli_lamb_yaml_tail_filter(self):
        """1.1.5（REG-094）：
        1. Stop hook `_unsent_ask_turn` 对 `- **等你定的事**：当前无。` 后紧跟同级列表 `- **接下来自动跟进的事**：...` 不误拦，
           而对真正列了待拍板项但未调 `sheepdog ask` 的回复仍准确拦截并截断在同级后续章节前；
        2. `_summarize_cli_lamb_log` 与 `_scan_lambs` 在 CLI 羔羊末尾含结构化 JSON 时自动过滤前置 `gcloud builds triggers import` 输出的 YAML 列表项（`- build` / `- deploy` / `- prod`）与键值行，避免 `JSON(...)` 被挤到第 3 行。
        """
        from sheepdog import engine, hook

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t_none = root / "transcript_none.jsonl"
            steps_none = [
                {
                    "step_index": 1,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "content": "[sheepdog] 新信号批次 `b20261104141100-release`",
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": (
                        "### 二、待定事项与接下来做什么\n\n"
                        "- **等你定的事**：当前无。\n"
                        "- **接下来自动跟进的事**：\n"
                        "  1. **`202611030051`（Kai Feng `chat-service` 语音网关后续放量 & 审批单流转）**：等待观察 2 小时。\n"
                    ),
                },
            ]
            t_none.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps_none) + "\n", encoding="utf-8")
            self.assertIsNone(hook._unsent_ask_turn(t_none))

            t_real = root / "transcript_real.jsonl"
            steps_real = [
                {
                    "step_index": 10,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "content": "[sheepdog] 新信号批次 `b20261104141100-release`",
                },
                {
                    "step_index": 11,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": (
                        "### 二、待定事项与接下来做什么\n\n"
                        "- **等你定的事**：\n"
                        "  - A：将生产 Trigger 中的 cloud-sdk:slim 替换为 cloud-sdk\n"
                        "- **接下来自动跟进的事**：\n"
                        "  1. 等待 Sammy 审批通过\n"
                    ),
                },
            ]
            t_real.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps_real) + "\n", encoding="utf-8")
            unsent = hook._unsent_ask_turn(t_real)
            self.assertIsNotNone(unsent)
            self.assertEqual(unsent[0], "ask:10")
            self.assertIn("cloud-sdk:slim", unsent[1])
            self.assertNotIn("接下来自动跟进的事", unsent[1])

            yaml_json_log = (
                "repositoryEventConfig:\n"
                "  push:\n"
                "    tag: ^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)-[0-9a-f]{7}$\n"
                "serviceAccount: projects/taplabs-453508/serviceAccounts/cloud-build-prod@taplabs-453508.iam.gserviceaccount.com\n"
                "substitutions:\n"
                "  _DEPLOY_PROJECT_ID: taplabs-453508\n"
                "  _TRAFFIC_MODE: all\n"
                "  _VERSION_DESCRIPTION: ''\n"
                "tags:\n"
                "- build\n"
                "- deploy\n"
                "- prod\n"
                '{"ok": false, "new_build_id": "4dabd043-0c29-4f06-90a3-5a69b6ff719b", "patched_all_triggers_count": 74}\n'
            )
            s_out, has_err = engine._summarize_cli_lamb_log(yaml_json_log)
            self.assertTrue(has_err)
            self.assertTrue(s_out.startswith("JSON(ok=False, new_build_id=4dabd043-0c29-4f06-90a3-5a69b6ff719b"))
            self.assertNotIn("- deploy", s_out)
            self.assertNotIn("- prod", s_out)

            # 3) 未先做只读核查就发起「A. 仅做只读指标与实例排查」的空手 ask 被 check_ask_pre_flight_guard 拦截（REG-094）
            from sheepdog import admin_web, outbound
            from sheepdog.config import Config
            from sheepdog.store import Store

            cfg = Config(self_open_id="ou_owner", state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            with self.assertRaises(outbound.OutboundError) as cm:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_bus",
                    "Leon 申请生产 gateway 扩容核实",
                    "Leon Liang 私聊反馈生产 gateway 实例数打满请求扩容。\n"
                    "---more---\n"
                    "选项：\n"
                    "A. 仅做只读指标与实例排查（执行：新开专席 tp_prod-gateway-investigation）\n"
                    "B. 同意核实后受控扩容（执行：新开专席 tp_prod-gateway-investigation）\n"
                    "建议：建议选 A 先由专席只读核实具体是哪个 gateway，确认需要后再由你拍板扩容。",
                )
            self.assertIn("REG-094", str(cm.exception))
            code, label = admin_web.diagnose_override_reason(
                "🐕 需要你定 · Leon 申请生产 gateway 扩容核实 [tp_bus]",
                "他说他的agent操作了 你可以帮忙盯着点",
                "其他",
                "A",
            )
            self.assertEqual(code, "stale_or_already_handled")
            store.close()

    def test_reg095_forwarded_queue_hol_sla_timestamp_and_owner_lookup_ask_guard(self):
        """REG-095: 经 sheepdog forward 转交的消息按转交入队时间计算 Queue Head-of-Line SLA 防误报 + 拦截未先检索代码仓/云服务就让主人充当人肉搜索引擎指认负责人的 ask。"""
        from sheepdog import admin_web, outbound
        from sheepdog.config import Config
        from sheepdog.models import Message
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(self_open_id="ou_owner", state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            # 1) 消息在 14:39:00 发到群里并先入 tp_bus，5 分 45 秒后（14:44:45）经 forward 转交给新专席：
            #    在 14:44:45 巡检时，该消息在新专席队列仅排队 0s，绝不可按 14:39:00 误报排队超 5 分钟
            msg = Message(
                message_id="om_reg095_orig_1439",
                chat_id="oc_data_infra",
                chat_name="Data Infra",
                chat_type="group",
                sender_id="ou_andy",
                sender_name="Andy Xiao",
                sender_type="user",
                content="归因表 attribution_sessions 归 tracking-attribution-service，帮忙指一下负责人",
                msg_type="interactive",
                create_time="2026-11-04T14:39:00+08:00",
            )
            store.upsert_message(msg, "dispatch", "at_me", [], "tp_bus")
            store.conn.execute(
                "UPDATE messages SET dispatch_state='acked', first_seen='2026-11-04T14:39:15+08:00', last_seen='2026-11-04T14:42:25+08:00' WHERE message_id=?",
                ("om_reg095_orig_1439",),
            )
            store.conn.execute(
                "INSERT INTO acks (message_id, chat_id, reason, identity, reaction_id, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("om_reg095_orig_1439", "oc_data_infra", "at_me", "user", "r_ack1", "2026-11-04T14:39:16+08:00"),
            )
            store.upsert_topic(
                "tp_tracking-attribution-investigation",
                title="排查·tracking-attribution服务与归因表归属排查",
                kind="dynamic",
                duty="排查归属",
                conversation_id="conv-reg095",
                state="active",
            )
            store.forward_message(
                "om_reg095_orig_1439",
                "tp_tracking-attribution-investigation",
                ["owner:tracking-attribution-investigation", "forwarded_from:tp_bus"],
            )
            # 模拟转交发生在 14:44:45，且巡检在同秒（14:44:45）触发
            store.conn.execute(
                "UPDATE messages SET last_seen='2026-11-04T14:44:45+08:00' WHERE message_id=?",
                ("om_reg095_orig_1439",),
            )
            store.conn.commit()

            now_1444 = datetime.fromisoformat("2026-11-04T14:44:45+08:00")
            obs_fresh = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_1444,
                check_processes=False,
                check_log=False,
            )
            self.assertFalse(
                any("Queue Head-of-Line SLA" in w for w in obs_fresh["warns"]),
                f"Expected 0 Queue Head-of-Line SLA warn right after forward, got: {obs_fresh['warns']}",
            )

            # 若转交后真正在新专席队列里卡满 6 分钟（14:50:46 仍 pending），则必须正常触发 Queue Head-of-Line SLA 告警
            now_1450 = datetime.fromisoformat("2026-11-04T14:50:46+08:00")
            obs_stale = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_1450,
                check_processes=False,
                check_log=False,
            )
            self.assertTrue(
                any("Queue Head-of-Line SLA" in w and "tp_tracking-attribution-investigation" in w for w in obs_stale["warns"]),
                f"Expected Queue Head-of-Line SLA warn after 6m post-forward stall, got: {obs_stale['warns']}",
            )

            # 2) 未先新开专席检索代码仓与 Cloud Run 归属，就发 ask 让主人选「A. 指定负责人 / B. 新开专席排查」必须被拦截
            with self.assertRaises(outbound.OutboundError) as cm:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_bus",
                    "Andy 在 Data Infra 群问 tracking-attribution 负责人",
                    "Andy 在 Data Infra 群 @你：AlloyDB 归因表 attribution_sessions 缓存约 4.7GiB，询问 tracking-attribution-service 负责人是谁。\n"
                    "---more---\n"
                    "选项：\n"
                    "A. 指定负责人（请直接告知负责人姓名，由总线在群内话题代回 Andy）\n"
                    "B. 新开专席排查（执行：新开专席 tp_tracking-attribution-investigation 排查云上服务与代码归属）\n"
                    "C. 稍后你亲自在群内回复",
                )
            self.assertIn("REG-094", str(cm.exception))

            code, label = admin_web.diagnose_override_reason(
                "🐕 需要你定 · Andy 在 Data Infra 群问 tracking-attribution 负责人 [tp_bus]",
                "[B] 新开专席排查（执行：tp_tracking-attribution-investigation）",
                "B",
                "A",
            )
            self.assertEqual(code, "peer_or_owner_delegate")

    def test_reg096_nat_port_downsize_ask_requires_peak_per_vm_usage_and_diagnosis(self):
        """REG-096: 请示调小 Cloud NAT minPortsPerVm 前必须核查并写明单实例最高端口使用数与影响评估 + 追问单实例峰值端口与影响归入 context_incomplete。"""
        from sheepdog import admin_web, outbound
        from sheepdog.config import Config
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(self_open_id="ou_owner", state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            # 1) 未写明单实例最高端口使用数就请示将 minPortsPerVm 调小为 256 -> 被拦截
            with self.assertRaises(outbound.OutboundError) as cm:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_prod-nat-port-investigation",
                    "生产东京Cloud NAT端口告警止血方案拍板",
                    "生产东京 Cloud NAT 告警已排查清楚，当前实际丢包为 0。建议执行方案 A，将网关 minPortsPerVm 由 1024 调小为 256。\n"
                    "---more---\n"
                    "选项：\n"
                    "A. 调小网关 minPortsPerVm 至 256，保留 8 个已加白 IP 不变\n"
                    "B. 确认阿里是否已把 50 个主池 IP 加白完毕并删除 Rule 101\n"
                    "执行：gcloud compute routers nats update tap-prod-nat-gateway-tokyo --min-ports-per-vm=256",
                )
            self.assertIn("REG-096", str(cm.exception))

            # 2) 写明了单实例最高端口使用数与动态端口分配上限 -> 正常放行
            outbound.check_ask_pre_flight_guard(
                cfg,
                store,
                "tp_prod-nat-port-investigation",
                "生产东京Cloud NAT端口告警止血方案拍板",
                "生产东京 Cloud NAT 告警已排查清楚，当前单实例最高端口使用数为 48 个（远低于 256，且已开启动态端口分配 maxPortsPerVm=65536），调小无任何业务影响。\n"
                "---more---\n"
                "选项：\n"
                "A. 调小网关 minPortsPerVm 至 256，保留 8 个已加白 IP 不变\n"
                "B. 确认阿里是否已把 50 个主池 IP 加白完毕并删除 Rule 101\n"
                "执行：gcloud compute routers nats update tap-prod-nat-gateway-tokyo --min-ports-per-vm=256",
            )

            # 3) 主人反问「那会不会有什么影响呢 有办法看最高端口使用数的一个实例用了多少个端口吗」归入 context_incomplete
            code, label = admin_web.diagnose_override_reason(
                "🐕 需要你定 · 生产东京Cloud NAT端口告警止血方案拍板 [tp_prod-nat-port-investigation]",
                "那会不会有什么影响呢 有办法看最高端口使用数的一个实例用了多少个端口吗",
                "其他",
                "A",
            )
            self.assertEqual(code, "context_incomplete")

    def test_reg097_bus_proxy_specialist_ask_guard_and_subagent_stall_and_json_lamb(self):
        """REG-097: 拦截 tp_bus 替专席越俎代庖发 ask + Subagent 工具返回后模型回合静默卡住 5m 收尾提取 + CLI 羔羊顶层 JSON dict 免被内部字符串 Error 误判失败 + 下午6点弄识别为带参采纳。"""
        from sheepdog import admin_web, engine, outbound
        from sheepdog.config import Config
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(self_open_id="ou_owner", state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            # 1) tp_bus 在拍板卡中将执行/核查委派给专席 tp_prod-gateway-monitor -> 被 REG-097 门禁拦截并提示改用 forward
            with self.assertRaises(outbound.OutboundError) as cm:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_bus",
                    "Leon 申请删除生产 Cloud Run agent-gateway 旧修订",
                    "Leon 在群里申请删除生产网关两个 0 流量旧修订来快速释放 256 个实例。\n"
                    "---more---\n"
                    "选项：\n"
                    "A. 同意删除旧修订（执行：tp_prod-gateway-monitor，核对 0% 流量无 tag 后执行并回读）\n"
                    "B. 不主动删除，等待自然清空（执行：tp_prod-gateway-monitor，盯守确认自然清空）\n"
                    "建议：建议选 B（等待自然清空）。",
                )
            self.assertIn("REG-097", str(cm.exception))
            self.assertIn("tp_prod-gateway-monitor", str(cm.exception))

            # 2) tp_bus 因转交被拒绝或询问开启 Codex 会话 -> 正常放行
            outbound.check_ask_pre_flight_guard(
                cfg,
                store,
                "tp_bus",
                "转交 tp_release 被拒绝请示",
                "总线转交专席 tp_release 时被拒绝（会话处于 closed 状态）。\n"
                "---more---\n"
                "选项：\n"
                "A. 恢复名册并重投（执行：tp_release）\n"
                "B. 直接在飞书审批",
            )

            # 3) CLI 羔羊输出顶层合法 JSON dict（无顶层 error/ok=False），但内部探测字段含多行 JS 与 SyntaxError 字符串 -> has_err 为 False，且多行字符串被折叠截断
            raw_json_log = json.dumps(
                {
                    "fake_secret_oracle": "file:///tmp/[eval1]:1\nimport https from 'node:https';\nSyntaxError: Invalid or unexpected token\n    at compileSourceTextModule",
                    "prod_logs_count": 38,
                    "files_checked": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            summary_str, has_err = engine._summarize_cli_lamb_log(raw_json_log)
            self.assertFalse(has_err, summary_str)
            self.assertIn("prod_logs_count=38", summary_str)
            self.assertNotIn("\n", summary_str)

            # 4) Subagent transcript 最后一步为 GENERIC DONE，且包含前序 Command output -> _harvest_subagent_stall_state 提取成功
            tp_file = root / "subagent_transcript.jsonl"
            tp_file.write_text(
                json.dumps(
                    {
                        "step_index": 131,
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "Created At: 2026-11-04T15:17:40+08:00\nCompleted At: 2026-11-04T15:17:44+08:00\nCommand exited with code 0.\n\nCommand output:\n24h Max Allocated Ports: 1024\n24h Max Open Connections: 51",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "step_index": 133,
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": "Created At: 2026-11-04T15:17:55+08:00\nCompleted At: 2026-11-04T15:17:55+08:00\nCreated file.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            is_stalled, harvested = engine._harvest_subagent_stall_state(tp_file)
            self.assertTrue(is_stalled)
            self.assertIn("24h Max Open Connections: 51", harvested)

            # 5) 主人回复「[卡片回调] 其他：下午6点弄 弄完要定时观察」-> _ACCEPT_WORDS_RE 匹配为带参采纳
            self.assertIsNotNone(admin_web._ACCEPT_WORDS_RE.search("下午6点弄 弄完要定时观察"))
            code, _ = admin_web.diagnose_override_reason(
                "🐕 需要你定 · Leon 申请删除生产 Cloud Run 旧修订 [tp_bus]\nA. 同意（执行：tp_prod-gateway-monitor）",
                "同意删除旧修订",
                "A",
                "B",
            )
            self.assertEqual(code, "peer_or_owner_delegate")

    def test_reg098_prompt_filter_auto_resume_and_caller_env_strip_and_evolve_dedup(self):
        """1.1.9（REG-098）：
        1. `AgentApiSink._call` 调用 `subprocess.run` 时自动剥离调用方窗口绑定的 `ANTIGRAVITY_PROJECT_ID` / `ANTIGRAVITY_CONVERSATION_ID` 等上下文变量，防止跨工作区投递报 `PermissionDenied: source project_id does not match target project_id`；
        2. 宿主遭遇 Prompt Filter 拦截（`PLANNER_RESPONSE DONE` 含 `The prompt could not be submitted`，且宿主未触发 Stop hook 导致 `st == {}`）时，`Dispatcher._check_stopped_in_flight` 在静默满 15s 后无需依赖 `st` 自动发送干净续跑唤醒，且 `observe --self-heal` 同步写入 `hooks/stop/<topic>.json` 自愈标记；
        3. `evolve.build_evolve_digest` 与 `evolve.run_evolve_tick` 自动过滤已处置的 `healed_override_escalation_ids` / `seen_override_ids` 与 `seen_failed_lamb_ids`，防止旧推翻项或旧超时羔羊在 65 分钟窗口内重复触发 `event_override` / `needs_action`。
        """
        from sheepdog import evolve
        from sheepdog.config import SelfEvolveConfig
        from sheepdog.sink.agentapi import AgentApiSink

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(
                self_open_id="ou_test_owner",
                state_dir=tmp_path / "state",
                self_evolve=SelfEvolveConfig(enabled=True, interval_minutes=60.0, cooldown_minutes=5.0, trigger_on_override=True),
            )
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            # 1) AgentApiSink._call 剥离调用方 ANTIGRAVITY_PROJECT_ID 等环境变量
            captured_env: dict[str, str] = {}

            def _fake_run(cmd, **kwargs):
                captured_env.update(kwargs.get("env") or {})
                proc = mock.MagicMock()
                proc.returncode = 0
                proc.stdout = '{"response": {"sendMessage": {"ok": true}}}'
                proc.stderr = ""
                return proc

            sink_api = AgentApiSink(state_dir=cfg.state_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "ANTIGRAVITY_AGENTAPI_EXE": "/bin/echo",
                    "ANTIGRAVITY_LS_ADDRESS": "localhost:56342",
                    "ANTIGRAVITY_CSRF_TOKEN": "tok",
                    "ANTIGRAVITY_PROJECT_ID": "proj-caller-only",
                    "ANTIGRAVITY_CONVERSATION_ID": "cid-caller-only",
                },
                clear=False,
            ), mock.patch("sheepdog.sink.agentapi.subprocess.run", side_effect=_fake_run):
                sink_api.send_message("cid-target", "hello")
            self.assertNotIn("ANTIGRAVITY_PROJECT_ID", captured_env)
            self.assertNotIn("ANTIGRAVITY_CONVERSATION_ID", captured_env)
            self.assertEqual(captured_env.get("ANTIGRAVITY_LS_ADDRESS"), "localhost:56342")

            # 2) Prompt Filter (PLANNER_RESPONSE DONE + 无 Stop hook 文件) 15s 自动续跑唤醒
            ref_now = datetime(2026, 11, 4, 15, 45, 0, tzinfo=observe.TZ)
            sent_dt = ref_now - timedelta(seconds=30)
            sent_iso = observe.iso(sent_dt)
            store.conn.execute(
                """
                INSERT INTO topics (topic_id, title, kind, conversation_id, host, state, pending_batch_id, dispatched_at, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tp_pf_auto", "NAT排查", "dynamic", "cid-pf-auto", "antigravity", "running", "b-pf-1", sent_iso, sent_iso, sent_iso),
            )
            store.conn.commit()
            t_pf = tmp_path / "brain" / "cid-pf-auto" / ".system_generated" / "logs" / "transcript.jsonl"
            t_pf.parent.mkdir(parents=True, exist_ok=True)
            t_pf.write_text(
                json.dumps({
                    "step_index": 488,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": observe.iso(ref_now - timedelta(seconds=29)),
                    "content": "批次 `b-pf-1`",
                })
                + "\n"
                + json.dumps({
                    "step_index": 489,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": observe.iso(ref_now - timedelta(seconds=28)),
                    "content": "The prompt could not be submitted. The prompt contains sensitive words that violate policy.",
                })
                + "\n",
                encoding="utf-8",
            )
            dry_sink = DryRunSink()
            dispatcher = Dispatcher(cfg, store, {"antigravity": dry_sink}, Roster(), SecurityConfig())
            with mock.patch.object(dispatcher, "_record_file", return_value=t_pf), mock.patch.object(
                dry_sink, "send_message", wraps=dry_sink.send_message
            ) as mock_send_msg:
                resumed = dispatcher._check_stopped_in_flight(
                    store.get_topic("tp_pf_auto"), "b-pf-1", sent_dt, ref_now, True
                )
            self.assertTrue(resumed)
            self.assertEqual(mock_send_msg.call_count, 1)
            self.assertIn("安全过滤器误拦", mock_send_msg.call_args[0][1])

            # 3) evolve.build_evolve_digest 过滤已结清的 healed_override_escalation_ids 与 seen_failed_lamb_ids
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, content, create_time, first_seen, last_seen, dispatch_state)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_old_ans_1",
                    "oc_1",
                    "[卡片回调] A：选A",
                    observe.iso(ref_now - timedelta(minutes=25)),
                    observe.iso(ref_now - timedelta(minutes=25)),
                    observe.iso(ref_now - timedelta(minutes=25)),
                    "acked",
                ),
            )
            store.conn.execute(
                """
                INSERT INTO escalations (message_id, topic_id, chat_id, text, asked_at, closed_at, close_reason, answer_message_id, answered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_old_ovr_1",
                    "tp_bus",
                    "oc_1",
                    "🐕 需要你定 · 测试 [tp_bus]\nA. 选A\nB. 选B\n我的建议：选 B",
                    observe.iso(ref_now - timedelta(minutes=30)),
                    observe.iso(ref_now - timedelta(minutes=25)),
                    "answered",
                    "om_old_ans_1",
                    observe.iso(ref_now - timedelta(minutes=25)),
                ),
            )
            store.conn.execute(
                """
                INSERT INTO lambs (lamb_id, parent_topic_id, kind, task_title, state, started_at, finished_at, result_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "lamb_old_timeout",
                    "tp_pf_auto",
                    "subagent",
                    "旧超时羔羊",
                    "timeout",
                    observe.iso(ref_now - timedelta(minutes=40)),
                    observe.iso(ref_now - timedelta(minutes=20)),
                    "超时",
                ),
            )
            store.conn.commit()
            # 未标记 settled 前，应检出 1 条 overridden 与 1 条 failed_lambs
            digest_before = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=ref_now)
            self.assertEqual(len(digest_before["d3_decision_ledger"]["overridden_in_window"]), 1)
            self.assertEqual(digest_before["d3_decision_ledger"]["overridden_in_window"][0]["answer_id"], "om_old_ans_1")
            self.assertTrue(any(r.startswith("new_overrides(1)") for r in digest_before["action_reasons"]))
            self.assertTrue(any(r.startswith("failed_lambs(1)") for r in digest_before["action_reasons"]))

            # 标记 settled 后，应被过滤为空
            store.set_meta("healed_override_escalation_ids", json.dumps(["om_old_ovr_1"]))
            evolve.write_evolve_state(
                store,
                {
                    "last_run_at": observe.iso(ref_now - timedelta(minutes=10)),
                    "last_override_answer_id": "om_reclassified_accept",
                    "seen_override_ids": ["om_old_ovr_1", "om_old_ans_1"],
                    "seen_failed_lamb_ids": ["lamb_old_timeout"],
                },
            )
            digest = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=ref_now)
            self.assertEqual(digest["d3_decision_ledger"]["overridden_in_window"], [])
            self.assertFalse(any(r.startswith("new_overrides") for r in digest["action_reasons"]), digest["action_reasons"])
            self.assertFalse(any(r.startswith("failed_lambs") for r in digest["action_reasons"]), digest["action_reasons"])

    def test_reg099_unverified_ticket_promise_guard_and_permission_ask_grant_option_completeness(self) -> None:
        """REG-099:
        1) 严禁对外发送无真实工单号/单据 ID 的『正在跟进腾讯云工单进展 / 已提单咨询腾讯云架构师 / 拿到云厂商确切结论与 ETA 后第一时间同步』空头安抚承诺；
           如实说明『实际未提云厂商工单』或附上可核查『工单号：202611049988』时正常放行；
        2) 同事申请修改/读写权限的 sheepdog ask 若提供的 A/B/C 选项全部为拒绝/仅只读/暂缓而缺少『同意授权/按申请开通』正向选项，当场拦截；
           补齐『同意授权开通...修改权限』选项后正常放行；
        3) 未先只读排查集群现状就发『...现状确认 / 需确认...现状，以便...』的 ask 当场拦截；
        4) 主人回复『[其他] 给他授权』或『[其他] 你先排查一下他需要什么弄清楚给我执行方案让我决策』时，diagnose_override_reason 精准归因为 context_incomplete。
        """
        from sheepdog import __version__, admin_web, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 10))

        # 1) 反空头工单安抚承诺门禁
        hollow_reply_1 = (
            "赞同，当前私网候选环境跑通了基础链路。\n---more---\n"
            "上海实测为 cgroup v1，代码中资源控制依赖 cgroup v2。目前已提单咨询腾讯云架构师，需评估是等平台支持还是做代码降级兼容。"
        )
        hollow_reply_2 = (
            "<at id=ou_test_peer_1></at> 正在跟进腾讯云工单进展，稍后同步最新结论；技术上已确认目前实例为v1且无配置开关。\n"
            "---more---\n- 正在跟进该工单的最新评估结果与升级窗口；\n- 拿到云厂商确切结论与 ETA 后第一时间在群里和下午对齐会上同步。"
        )
        for bad_txt in (hollow_reply_1, hollow_reply_2):
            with self.assertRaises(outbound.OutboundError) as ctx_ticket:
                outbound.check_unverified_ticket_promise_guard(bad_txt)
            self.assertIn("REG-099", str(ctx_ticket.exception))

        # 如实说明未提工单 + 代码降级绕过，或附真实工单号，应放行
        honest_clarification = (
            "目前未提云厂商工单，上海 AGS 固定为 cgroup v1，建议代码侧直接关闭 cgroup v2 强依赖降级兼容。"
        )
        verified_ticket = (
            "正在跟进腾讯云工单进展（工单号：20261104009876），稍后同步最新结论。"
        )
        outbound.check_unverified_ticket_promise_guard(honest_clarification)
        outbound.check_unverified_ticket_promise_guard(verified_ticket)

        # 2) 权限申请 ask 选项完整性门禁 & 3) 未先只读排查现状确认门禁
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            store.upsert_topic("tp_kc", title="人·kc·需求对接", kind="dynamic", conversation_id="cid-kc")

            ask_title = "kc申请腾讯云Prod数据库和Redis修改权限"
            all_refusal_body = (
                "kc申请生产数据库与双Redis修改权限用于压测批量造数。建议选A拒绝个人写权限。\n---more---\n"
                "事情：kc 申请腾讯云国内 Prod 生产数据库及双 Redis 的查看和修改权限，称需批量造数据进行 PTS 压测。\n\n"
                "选项：\n"
                "A. 严守红线拒绝个人生产修改权限，由 SRE 专席配合前置物理快照并受控代跑造数与回滚\n"
                "B. 仅开生产跳板机只读权限供查看，压测造数走业务 API 或离线生成 SQL 经评审后代执行\n"
                "C. 暂缓生产造数，先引导在 Stage 环境完成全链路造数压测验证\n\n"
                "建议：选 A 。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_perm:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_kc", ask_title, all_refusal_body)
            self.assertIn("REG-099", str(ctx_perm.exception))

            complete_options_body = (
                "kc申请生产数据库与双Redis修改权限用于压测批量造数。建议选A拒绝个人写权限，或选B同意授权。\n---more---\n"
                "事情：kc 申请腾讯云国内 Prod 生产数据库及双 Redis 的查看和修改权限，称需批量造数据进行 PTS 压测。\n\n"
                "选项：\n"
                "A. 严守红线拒绝个人生产修改权限，由 SRE 专席配合前置物理快照并受控代跑造数与回滚\n"
                "B. 同意授权开通 Prod 数据库与双 Redis 修改权限（同步附上 DMC 控制台入口与水位注意事项）\n"
                "C. 仅开生产跳板机只读权限供查看\n\n"
                "建议：选 A 。"
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_kc", ask_title, complete_options_body)

            uninvestigated_status_title = "南京 Prod Agent 国内渠道开通与路由现状确认"
            uninvestigated_status_body = (
                "Ziyi 需要确认南京 Prod Agent 国内渠道与路由现状以便跑 loader。建议按选项 A。\n---more---\n"
                "事情：Ziyi 在排查南京 Prod Agent 配置，需确认国内模型渠道（TokenHub/DeepSeek/WIMGS）、国内媒体 CDN 与 AGS 正式 /control 私网路由现状，以便在独立版本目录生成配置并跑真实 loader。\n\n"
                "选项：\n"
                "A. 暂未开通渠道直接据实禁用；国内媒体 CDN 域名及正式 /control 路由按南京集群实际配置对接\n"
                "B. 先行开通相关模型渠道凭据与媒体 CDN 域名，待全部就绪后再跑 loader\n\n"
                "建议：选 A。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_status:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_kc", uninvestigated_status_title, uninvestigated_status_body)
            self.assertIn("REG-099", str(ctx_status.exception))

            # 4) 主人回复「给他授权」或「你先排查一下他需要什么弄清楚给我执行方案让我决策」归入 context_incomplete
            code, label = admin_web.diagnose_override_reason(all_refusal_body, "给他授权", "其他", "A")
            self.assertEqual(code, "context_incomplete")
            self.assertEqual(label, "缺环境/调用链反问")
            code2, label2 = admin_web.diagnose_override_reason(
                uninvestigated_status_body, "你先排查一下他需要什么弄清楚给我执行方案让我决策", "其他", "A"
            )
            self.assertEqual(code2, "context_incomplete")
            self.assertEqual(label2, "缺环境/调用链反问")

    def test_reg100_rca_governance_ask_requires_timeline_context_and_old_image_ask_requires_cicd_readiness(self) -> None:
        """REG-100（1.1.11）：
        1) 故障/卡顿/根因治理决策类 ask 若在 ---more--- 中只写一句口号式摘要而不交代发生时间窗、报错/耗时现象与当前现状，拦截发送（防主人反问『能具体讲讲前因后果嘛』）；
        2) 生产服务因跑着旧镜像且留有 Stage 环境变量导致报错时，若 ask 只建议手改部分配置而不推进新镜像且未核实 CI/CD 部署到 prod 的状态，拦截发送（防主人纠偏『直接跑新镜像呗 并且保证相关服务的cicd是通的 可以部署到prod』）；
        3) diagnose_override_reason 将『能具体讲讲前因后果嘛』归入 context_incomplete，将『直接跑新镜像呗 并且保证相关服务的cicd是通的 可以部署到prod』归入 arch_or_config_posture。
        """
        from sheepdog import __version__, admin_web, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 11))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            store.upsert_topic("tp_arena-release-guard", title="专题·发版守卫", kind="dynamic", conversation_id="cid-arena")
            store.upsert_topic("tp_tapnow-prod-topology", title="专题·生产拓扑", kind="dynamic", conversation_id="cid-topo")

            rca_title = "file-hub-server根因治理决策"
            hollow_rca_body = (
                "Jerry Chu询问file-hub-server卡顿是否修复及治理方案。建议选A：安排根因治理排期（拔除坏死GA路由+连接池熔断异步化+Supervisor放宽至5s），彻底消除单点雪崩。\n---more---\n"
                "事情：Jerry Chu 在运维群确认 file-hub-server 周期性卡顿根因后，询问是否修复及治理方案。根因为深圳 GA TLS 挂死 + 连接池持锁阻塞 + Supervisor 1.5s 误杀 Worker 级联排队。\n\n"
                "选项：\n"
                "A. 安排根因治理排期（拔除坏死 GA 路由走 Tokyo 本地 + 连接池加熔断与异步探测 + 放宽 Supervisor 超时），从系统架构上消除雪崩\n"
                "B. 暂不改代码，仅排查并恢复深圳 GA 专线（tap-prod-file-hub-ga-tokyo）的网络与证书连通性\n\n"
                "建议：选 A。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_rca:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_arena-release-guard", rca_title, hollow_rca_body)
            self.assertIn("REG-100", str(ctx_rca.exception))

            complete_rca_body = (
                "Jerry Chu询问file-hub-server卡顿是否修复及治理方案。建议选A：安排根因治理排期。\n---more---\n"
                "事情（前因后果）：\n"
                "- 发生时间与现象：11月3日 14:20~15:10 期间，生产 file-hub-server 出现周期性 HTTP 504 与 P99 飙升至 8s，影响面为文件上传签名接口。\n"
                "- 因果链：请求经深圳 GA 专线转发时偶发 TLS 握手挂死，连接池持全局锁同步等待阻塞后续协程，进而触发 Supervisor 1.5s 探活超时误杀 Worker 引发级联重启排队。\n"
                "- 当前现状：目前线上流量已恢复正常（已临时止血），Jerry Chu 询问长期根因治理排期。\n\n"
                "选项：\n"
                "A. 安排根因治理排期（拔除坏死 GA 路由走 Tokyo 本地 + 连接池加熔断与异步探测 + 放宽 Supervisor 超时），从系统架构上消除雪崩\n"
                "B. 暂不改代码，仅排查并恢复深圳 GA 专线的网络与证书连通性\n\n"
                "建议：选 A。"
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_arena-release-guard", rca_title, complete_rca_body)

            topo_title = "tapnowprod：conversations:create 404 根因与 BFF/WebSocket 修复授权"
            partial_old_img_body = (
                "conversations:create 报 404 是因为正式 agent-gateway 还跑着旧镜像，新版只发在了 candidate 上。\n"
                "另外查出 BFF 环境变量还留着 Stage 域名导致调上游报 500。要先修 BFF 和 WS，还是连 agent-gateway 一起切？\n---more---\n"
                "1. POST /api/agent-gateway/v1/conversations:create 返回 404 根因：正式 svc/agent-gateway-api 跑的是旧镜像 :de3ca247。\n"
                "2. deploy/app-tapnow-bff 环境变量仍指向 Stage。\n"
                "3. 授权选项：\n"
                "- 选项 A（推荐）：同意更新 tapnowprod 的 app-tapnow-bff 环境变量并重启 websocket-service\n"
                "- 选项 B：同意更新 tapnowprod 的 app-tapnow-bff 环境变量、重启 websocket-service 并切换 agent-gateway 到新版"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_topo:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", topo_title, partial_old_img_body)
            self.assertIn("REG-100", str(ctx_topo.exception))

            complete_topo_body = (
                partial_old_img_body
                + "\n4. CI/CD 状态：已核实 agent-gateway 与 app-tapnow-bff 的 CNB CI/CD 流水线是通的，可以直接部署到 prod。"
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", topo_title, complete_topo_body)

            c1, l1 = admin_web.diagnose_override_reason(hollow_rca_body, "能具体讲讲前因后果嘛", "其他", "A")
            self.assertEqual((c1, l1), ("context_incomplete", "缺环境/调用链反问"))
            c2, l2 = admin_web.diagnose_override_reason(
                partial_old_img_body, "直接跑新镜像呗 并且保证相关服务的cicd是通的 可以部署到prod", "其他", "A"
            )
            self.assertEqual((c2, l2), ("arch_or_config_posture", "技术姿势纠偏"))

            # 4) 现有 VPS 节点仍有空位（如『余 2 空额』）时申请专线拦截 ask（主人批复『有空位就给他』），且 _ACCEPT_WORDS_RE 识别为采纳
            store.upsert_topic("tp_3x-ui-ops", title="专席·3x-ui运维", kind="managed", conversation_id="cid-3x")
            vps_slot_title = "Jean Xu申请Claude专线"
            vps_slot_body = (
                "Jean申请VPS专线跑Claude，极净节点lsg15仅kc一人余2空额，建议选A在lsg15开通专属账号交付。\n---more---\n"
                "事情：Jean Xu 私聊申请 VPS 专线用于跑 Claude，目前干净双 ISP 节点 lsg15 仅 kc 1 人（余 2 空额）。\n\n"
                "选项：\n"
                "A. 批准在 lsg15 为 Jean Xu 开通专属双入站（Reality/Trojan）并交付\n"
                "B. 新购一台专属干净 VPS 专线给 Jean 跑 Claude（约 $10/月）\n\n"
                "建议：选 A。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_vps:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_3x-ui-ops", vps_slot_title, vps_slot_body)
            self.assertIn("REG-100", str(ctx_vps.exception))
            self.assertIsNotNone(admin_web._ACCEPT_WORDS_RE.search("有空位就给他"))

            # 5) Subagent 后台长任务 (.system_generated/tasks/task-*.log) 持续输出心跳与误判 timeout 自愈
            from sheepdog import engine
            fake_brain = root / "brain" / "cid-sub-backfill" / ".system_generated"
            (fake_brain / "logs").mkdir(parents=True, exist_ok=True)
            (fake_brain / "tasks").mkdir(parents=True, exist_ok=True)
            tp_sub = fake_brain / "logs" / "transcript.jsonl"
            tp_sub.write_text('{"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE"}\n', encoding="utf-8")
            task_log = fake_brain / "tasks" / "task-184.log"
            task_log.write_text("2500/10349 batches\n", encoding="utf-8")
            mt = engine._subagent_latest_task_log_mtime(tp_sub)
            self.assertIsNotNone(mt)

    def test_reg101_region_switch_ask_guard_and_owner_explicit_idle_receipt_unlock(self):
        from sheepdog import __version__, admin_web, engine, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.roster import Roster
        from sheepdog.security import SecurityConfig
        from sheepdog.sink.agentapi import DryRunSink
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 12))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(state_dir=root / "state")
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            store.upsert_topic("tp_tapnow-prod-topology", title="项目·TapNow生产脉络", kind="dynamic", conversation_id="cid-topo-101")

            # 1) 针对国内外共享前端提出 Nginx sub_filter 热修或无开关彻底禁掉 CF Turnstile / Sentry 被拦截（REG-101）
            ask_title = "tapnowprod：热修前端彻底禁掉 CF Turnstile 与 Sentry 上报"
            bad_body = (
                "查明 Ethan 注册被 CF 拦是因为 register.tsx 只看 IP 不看国内站域名，要现在热修线上 app-tapnow 禁掉 CF 和 Sentry 吗？\n---more---\n"
                "事情：请授权在线上 deploy/app-tapnow 通过 Nginx sub_filter 立即热修强制使用阿里云验证码并清空 Sentry DSN。\n\n"
                "选项：\n"
                "A. 同意热修 tapnowprod 的 app-tapnow 禁用 CF Turnstile 与 Sentry 上报\n"
                "B. 暂不热修，只走源码提交\n\n"
                "建议：A"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_sub:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", ask_title, bad_body)
            self.assertIn("REG-101", str(ctx_sub.exception))

            good_title = "tapnowprod：新增环境变量配置开关控制国内站验证码与 Sentry"
            good_body = (
                "查明 register.tsx 需按环境变量配置开关隔离国内外行为，要推进带配置开关的正式版本发布吗？\n---more---\n"
                "事情：在 fe-app-tapnow 新增环境变量开关控制（什么配置下只给国内的就行，不影响海外站），不采用线上脏热修。\n\n"
                "选项：\n"
                "A. 同意按环境变量开关控制国内站专用配置并走标准 CI/CD 发布\n"
                "B. 暂缓发布\n\n"
                "建议：A"
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", good_title, good_body)

            # 2) diagnose_override_reason 精准将『这个修法你需要保证可以有个开关来控制 什么配置下只给国内的就行』归入 arch_or_config_posture
            ans = "这个修法你需要保证可以有个开关来控制 什么配置下只给国内的就行"
            self.assertIsNotNone(admin_web._OVERRIDE_WORDS_RE.search(ans))
            cat, label = admin_web.diagnose_override_reason(bad_body, ans, "其他", "A")
            self.assertEqual((cat, label), ("arch_or_config_posture", "技术姿势纠偏"))
            cat_kc, label_kc = admin_web.diagnose_override_reason(
                "kc申请开通国内CVM与PTS扣费", "我已经在跟kaichi的私聊里决策过了", "其他", "A"
            )
            self.assertEqual((cat_kc, label_kc), ("stale_or_already_handled", "陈旧/已处理未重查"))

            # 3) 主人在面板直接输入 USER_EXPLICIT（如『前端不要动手改』『你先停』）后会话停在 PLANNER_RESPONSE DONE 满 60s 时，
            #    _check_stopped_in_flight 无需等 observe 告警即可自动发『严格遵照主人在面板的最新指令、仅补交回执』唤醒
            cfg.session.human_attach_minutes = 0
            ref_now = datetime(2026, 11, 4, 17, 47, 0, tzinfo=observe.TZ)
            sent_dt = ref_now - timedelta(seconds=140)
            stop_dt = ref_now - timedelta(seconds=65)
            batch_id = "b20261104174215-tapnow-prod-topology"
            store.update_topic(
                "tp_tapnow-prod-topology",
                state="running",
                pending_batch_id=batch_id,
                dispatched_at=sent_dt.isoformat(timespec="seconds"),
            )
            store.record_dispatch(batch_id, "tp_tapnow-prod-topology", ["om_cb_101"])
            t_file = root / "topo_101.jsonl"
            t_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "step_index": 1,
                                "type": "SYSTEM_MESSAGE",
                                "status": "DONE",
                                "created_at": sent_dt.isoformat(timespec="seconds"),
                                "content": f"The following is a <SYSTEM_MESSAGE>\n[Message] content=[sheepdog] 新信号批次 `{batch_id}`（topic `tp_tapnow-prod-topology`）",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "step_index": 2,
                                "type": "USER_INPUT",
                                "source": "USER_EXPLICIT",
                                "status": "DONE",
                                "created_at": (sent_dt + timedelta(seconds=60)).isoformat(timespec="seconds"),
                                "content": "<USER_REQUEST>\n你先停\n</USER_REQUEST>",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "step_index": 3,
                                "type": "PLANNER_RESPONSE",
                                "status": "DONE",
                                "created_at": stop_dt.isoformat(timespec="seconds"),
                                "content": "已立即停手，不再动前端任何东西。",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            dry_sink = DryRunSink()
            disp = engine.Dispatcher(cfg, store, {"antigravity": dry_sink}, Roster(), SecurityConfig())
            with mock.patch.object(disp, "_record_file", return_value=t_file), mock.patch.object(
                dry_sink, "send_message", wraps=dry_sink.send_message
            ) as mock_send:
                woke = disp._check_stopped_in_flight(
                    store.get_topic("tp_tapnow-prod-topology"), batch_id, sent_dt, ref_now, True
                )
            self.assertTrue(woke)
            self.assertEqual(mock_send.call_count, 1)
            self.assertIn("主人在面板直接下达了指令", mock_send.call_args[0][1])
            self.assertIn("若主人叫停则保持停手", mock_send.call_args[0][1])

    def test_reg102_evolve_parent_recovered_lamb_and_forwarded_dispatch_acked_at_and_rollback_residue(self):
        from sheepdog import __version__, evolve, parse_version_tuple, version_watch
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 13))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(sink="dryrun")
            cfg.state_dir = root / "state"
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            store.upsert_topic("tp_release", kind="custom", title="Release Bot", conversation_id="cid-rel", state="active")
            store.upsert_topic("tp_bus", kind="known", title="Bus", conversation_id="cid-bus", state="active")
            store.upsert_topic("tp_infra", kind="custom", title="Infra", conversation_id="cid-inf", state="active")

            ref_now = datetime(2026, 11, 4, 19, 12, 0, tzinfo=observe.TZ)
            t_context = (ref_now - timedelta(minutes=50)).isoformat(timespec="seconds")
            t_fwd = (ref_now - timedelta(minutes=45)).isoformat(timespec="seconds")
            t_lamb_fail = (ref_now - timedelta(minutes=3)).isoformat(timespec="seconds")
            t_lamb_done = (ref_now - timedelta(minutes=1)).isoformat(timespec="seconds")

            # 1) set_dispatch_state(..., 'acked') 自动写入 acked_at；context 批次与已转发批次不应在 evolve D4 误算为超时慢批次
            store.conn.execute(
                "INSERT INTO dispatches(batch_id, topic_id, message_ids, sent_at, state, acked_at) VALUES(?,?,?,?,?,?)",
                ("b-ctx-102", "tp_infra", "[]", t_context, "context", None),
            )
            store.conn.execute(
                "INSERT INTO dispatches(batch_id, topic_id, message_ids, sent_at, state, acked_at) VALUES(?,?,?,?,?,?)",
                ("b-fwd-102", "tp_bus", "[]", t_fwd, "sent", None),
            )
            store.conn.commit()
            store.set_dispatch_state("b-fwd-102", "acked", error="forwarded:tp_release")
            fwd_row = store.get_dispatch("b-fwd-102")
            self.assertIsNotNone(fwd_row["acked_at"])
            store.conn.execute(
                "UPDATE dispatches SET acked_at=? WHERE batch_id='b-fwd-102'",
                ((ref_now - timedelta(minutes=44, seconds=50)).isoformat(timespec="seconds"),),
            )
            store.conn.commit()

            # 2) 母羊已在失败羔羊后拉起新羔羊成功完成（或已 ack 后续批次）时，不应再触发 failed_lambs 告警
            store.conn.execute(
                "INSERT INTO lambs(lamb_id, parent_topic_id, kind, task_title, state, started_at, finished_at, result_summary) VALUES(?,?,?,?,?,?,?,?)",
                ("lamb_fail_102", "tp_release", "cli", "核查构建v1", "failed", (ref_now - timedelta(minutes=4)).isoformat(timespec="seconds"), t_lamb_fail, "JSONDecodeError"),
            )
            store.conn.execute(
                "INSERT INTO lambs(lamb_id, parent_topic_id, kind, task_title, state, started_at, finished_at, result_summary) VALUES(?,?,?,?,?,?,?,?)",
                ("lamb_done_102", "tp_release", "cli", "核查构建v2", "done", t_lamb_done, ref_now.isoformat(timespec="seconds"), "OK"),
            )
            store.conn.commit()

            digest = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=ref_now)
            slow_ids = [x["batch_id"] for x in digest["d4_latency_and_slow_dispatches"]["slow_dispatches"]]
            self.assertNotIn("b-ctx-102", slow_ids)
            self.assertNotIn("b-fwd-102", slow_ids)
            self.assertNotIn("failed_lambs(1)", digest["action_reasons"])
            rec_map = {lb["lamb_id"]: lb.get("recovered_by_parent") for lb in digest["d5_bus_and_lambs"]["recent_lambs"]}
            self.assertTrue(rec_map.get("lamb_fail_102"))

            # 3) 回滚窗口状态残留盘点（_collect_rollback_window_residue）：显式列出 [t_swap, t_rollback] 期间在途羔羊与批次
            store.conn.execute(
                "INSERT INTO lambs(lamb_id, parent_topic_id, kind, task_title, state, started_at) VALUES(?,?,?,?,?,?)",
                ("lamb_running_102", "tp_infra", "subagent", "压测HPA扩缩容核查", "running", (ref_now - timedelta(seconds=30)).isoformat(timespec="seconds")),
            )
            store.conn.commit()
            residue = version_watch._collect_rollback_window_residue(
                store, (ref_now - timedelta(minutes=10)).isoformat(timespec="seconds")
            )
            self.assertEqual(len(residue["running_lambs"]), 1)
            self.assertEqual(residue["running_lambs"][0]["lamb_id"], "lamb_running_102")

    def test_reg103_broadcast_sync_ask_requires_skip_option_and_loop_feedback_auto_wire(self):
        from sheepdog import __version__, decision_hub, loops, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 14))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(sink="dryrun")
            cfg.state_dir = root / "state"
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            cfg.loops_dir_path = str(root / "loops")
            loop_dir = cfg.loops_dir / "infra-migration-daily-steward"
            loop_dir.mkdir(parents=True, exist_ok=True)
            (loop_dir / "LOOP.md").write_text(
                "---\nid: infra-migration-daily-steward\ntitle: 迁移日清循环\ntopic_id: tp_infra-migration-ops\nautonomy: confirm\nenabled: true\n---\n",
                encoding="utf-8",
            )
            store = Store(cfg.db_path)
            store.upsert_topic("tp_infra-migration-ops", kind="custom", title="Infra", conversation_id="cid-inf", state="active")

            # 1) 播报类 ask 缺少『暂不同步 / 取消本次播报』选项时，必须触发 REG-103 拦截
            title = "20:00 跨云迁移日清播报拍板"
            bad_body = (
                "已整理好今晚 20:00 每日进度与阻塞卡点播报草案，请你审阅拍板后发进群。\n---more---\n"
                "事情：已汇总 2026-11-04 晚 20:00 跨云迁移日清战报，请拍板发布。\n\n"
                "选项：\n"
                "A. 同意发布：以卡片形式正式同步至群「Infra 迁移改造」\n"
                "B. 暂缓发布：先做修改或补充内容\n\n"
                "建议：选 A。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_bad:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_infra-migration-ops", title, bad_body)
            self.assertIn("REG-103", str(ctx_bad.exception))

            good_body = bad_body.replace(
                "B. 暂缓发布：先做修改或补充内容",
                "B. 暂缓发布：先做修改或补充内容\nC. 暂不同步：取消本次群内播报",
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_infra-migration-ops", title, good_body)

            # 2) 当主人在关联 Loop 的拍板卡上给出『[其他] 暂时不同步了』时，sync_escalation_dual_cards 自动写入 loop_runs.human_feedback 与 MEMORY.md
            loops.ensure_loop_tables(store)
            store.conn.execute(
                "INSERT INTO loop_runs(run_id, loop_id, trigger_reason, autonomy, status, started_at, finished_at) VALUES(?,?,?,?,?,?,?)",
                ("lrun_test_103", "infra-migration-daily-steward", "daemon_tick", "confirm", "dispatched", "2026-11-04T20:02:57+08:00", "2026-11-04T20:02:57+08:00"),
            )
            full_esc_text = f"🐕 需要你定 · {title} [tp_infra-migration-ops]\n{good_body}"
            store.conn.execute(
                "INSERT INTO sends(idem_key, kind, topic_id, message_id, key, norm_key, text, sent_at) VALUES(?,?,?,?,?,?,?,?)",
                ("ik_103", "ask", "tp_infra-migration-ops", "om_esc_103", "loop-infra-migration-daily-steward", "LOOP-INFRA-MIGRATION-DAILY-STEWARD", full_esc_text, "2026-11-04T20:04:13+08:00"),
            )
            store.add_escalation("om_esc_103", "tp_infra-migration-ops", "oc_hub", full_esc_text, "2026-11-04T20:04:13+08:00")
            decision_hub.sync_escalation_dual_cards(
                cfg,
                store,
                None,
                "om_esc_103",
                chosen_display="[其他] 暂时不同步了",
                topic_id="tp_infra-migration-ops",
                answer_mid="sd_cb_103",
            )
            fb_row = store.conn.execute("SELECT human_feedback FROM loop_runs WHERE run_id='lrun_test_103'").fetchone()
            self.assertEqual(fb_row["human_feedback"], "[其他] 暂时不同步了")
            self.assertIn("暂时不同步了", (loop_dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_reg104_cn_site_tencent_intl_subaccount_ask_guard_and_evolve_loop_feedback_dedup(self):
        from sheepdog import __version__, admin_web, evolve, loops, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 15))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(sink="dryrun")
            cfg.state_dir = root / "state"
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            cfg.loops_dir_path = str(root / "loops")
            cfg.loops_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)

            # 1) 针对 .tv / Cinemap 中国站生产资源开辟，将『在腾讯云国内站账号下开辟 vs 先用国际站首发』作为二选一请示时，触发 REG-104 拦截
            title = "Cinemap生产首发资源账号决策"
            bad_body = (
                "Kai提出Cinemap中国站11-05首发，需今晚定生产资源账号是用腾讯云国内站还是先用国际站单人首发。\n---more---\n"
                "选项：\n"
                "A. 按国内站开辟：在腾讯云国内站账号下开辟南京生产资源（需确认国内站企业实名与开通）。\n"
                "B. 先用国际站首发：先用现有国际站南京成熟账号开独立资源，对外前再对齐国内站。\n\n"
                "建议：若国内站账号权限已就绪建议 A，否则选 B 先跑通首发。"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_bad:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_kaifeng", title, bad_body)
            self.assertIn("REG-104", str(ctx_bad.exception))

            good_body = (
                "Cinemap中国站11-05首发，已统一放在腾讯云国际站子成员账号（200053747873）环境，请确认规格。\n---more---\n"
                "选项：\n"
                "A. 同意在腾讯云国际站 Prod 子成员账号（200053747873）下开通 8C32G CVM 与 COS\n"
                "B. 调整规格后再开通"
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_kaifeng", title, good_body)

            # 2) diagnose_override_reason 将『直接就跟tapnow.tv一样 都放在腾讯云国际站 的子成员账号环境里』归入 arch_or_config_posture
            code, label = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · {title} [tp_kaifeng]\n{bad_body}",
                "直接就跟tapnow.tv一样 都放在腾讯云国际站 的子成员账号环境里",
                "其他",
                "A",
            )
            self.assertEqual(code, "arch_or_config_posture")
            self.assertEqual(label, "技术姿势纠偏")

            # 3) evolve.build_evolve_digest 过滤已记入 seen_feedback_run_ids 的 loop_runs.human_feedback，避免 65 分钟内重复报警
            from sheepdog.store import now_iso
            loops.ensure_loop_tables(store)
            now_str = now_iso()
            store.conn.execute(
                "INSERT INTO loop_runs(run_id, loop_id, trigger_reason, autonomy, status, human_feedback, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?)",
                ("lrun_seen_104", "infra-migration-daily-steward", "daemon_tick", "confirm", "dispatched", "[其他] 暂时不同步了", now_str, now_str),
            )
            store.conn.commit()
            dig1 = evolve.build_evolve_digest(cfg, store)
            self.assertIn("loop_feedback(1)", dig1["action_reasons"])

            evolve.write_evolve_state(store, {"seen_feedback_run_ids": ["lrun_seen_104"]})
            dig2 = evolve.build_evolve_digest(cfg, store)
            self.assertNotIn("loop_feedback(1)", dig2["action_reasons"])
            self.assertEqual(dig2["d7_business_loops"]["feedback_runs"], [])

    def test_reg105_sammy_parity_entrypoint_ask_guard_and_strict_loop_escalation_feedback(self):
        from sheepdog import __version__, admin_web, loops, outbound, parse_version_tuple
        from sheepdog.config import Config
        from sheepdog.store import Store, now_iso

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 16))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(sink="dryrun")
            cfg.state_dir = root / "state"
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            cfg.loops_dir_path = str(root / "loops")
            cfg.loops_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)

            # 1) Sammy / Sammi 请示南京生产 TKE 新建 Secret 的入口方案（提供现有跳板机入口或临时开单 IP 公网访问）时，触发 REG-105 拦截
            title = "南京生产TKE写入Kimi生产Key方式"
            bad_body = (
                "Sammy 助理请示在腾讯云南京生产 TKE(cls-gnui5plk) tapnowprod 下新建 Secret agent-proxy-tokenhub-guangzhou-20261104(api_key) 的入口方案。\n"
                "---more---\n"
                "事情：Sammy 的 AI 助理 Sammi 转述 Sammy 与 Ziyi 需求，因 cls-gnui5plk 仅开启内网访问，Sammi 询问能否提供现有南京集群跳板机/TKE 入口，或同意其临时开启 cls-gnui5plk 公网访问。\n\n"
                "选项：\n"
                "A. 不开启 TKE 公网端口，由 tp_tapnow-prod-topology 提供现有内网跳板机入口协助 Sammi 写入\n"
                "B. 同意授权 Sammi 临时开启 cls-gnui5plk 集群公网访问（仅放行单 IP 白名单），5 分钟内建好 Secret 后立即关闭"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_bad:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_release", title, bad_body)
            self.assertIn("REG-105", str(ctx_bad.exception))

            # 2) diagnose_override_reason 将『我们咋做你就给sammy咋做 sammy的权限至少要跟我一摸一样』归入 arch_or_config_posture（而非被 sammy 泛匹配到 peer_or_owner_delegate）
            code, label = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · {title} [tp_release]\n{bad_body}",
                "我们咋做你就给sammy咋做 sammy的权限至少要跟我一摸一样",
                "其他",
                "A",
            )
            self.assertEqual(code, "arch_or_config_posture")
            self.assertEqual(label, "技术姿势纠偏")

            # 3) maybe_record_escalation_loop_feedback 仅将显式关联 Loop（--key loop-<id> 或命中 loop_items）的拍板记入 Loop，不误污染同会话普通私聊 ask；且对 [卡片回调] 前缀归一化防重复写 MEMORY.md
            loops.init_loop(cfg, store, "prod-release-pilot", template="prod-release-pilot", topic_id="tp_release")
            now_str = now_iso()
            store.conn.execute(
                "INSERT INTO loop_runs(run_id, loop_id, trigger_reason, autonomy, status, started_at, finished_at) VALUES(?,?,?,?,?,?,?)",
                ("lrun_disp_105", "prod-release-pilot", "daemon_tick", "confirm", "dispatched", now_str, now_str),
            )
            store.conn.execute(
                "INSERT INTO sends(idem_key, kind, topic_id, message_id, key, norm_key, text, sent_at) VALUES(?,?,?,?,?,?,?,?)",
                ("idem_non_loop_105", "ask", "tp_release", "om_test_non_loop_105", "kimi-k31-nanjing-secret", "KIMI-K31-NANJING-SECRET", "ask", now_str),
            )
            store.conn.execute(
                "INSERT INTO sends(idem_key, kind, topic_id, message_id, key, norm_key, text, sent_at) VALUES(?,?,?,?,?,?,?,?)",
                ("idem_real_loop_105", "ask", "tp_release", "om_test_real_loop_105", "loop-prod-release-pilot", "LOOP-PROD-RELEASE-PILOT", "ask", now_str),
            )
            store.conn.commit()

            # 普通非 Loop ask（key=kimi-k31-nanjing-secret）不写入 prod-release-pilot
            matched_non_loop = loops.maybe_record_escalation_loop_feedback(
                cfg, store, "om_test_non_loop_105", "tp_release", "[其他] 我们咋做你就给sammy咋做 sammy的权限至少要跟我一摸一样"
            )
            self.assertIsNone(matched_non_loop)

            # 显式关联 Loop 的 ask 正常写入，且连续用『其他：...』与『[卡片回调] 其他：...』调用两次不在 MEMORY.md 产生重复行
            matched_real = loops.maybe_record_escalation_loop_feedback(
                cfg, store, "om_test_real_loop_105", "tp_release", "其他：延后10分钟发版"
            )
            loops.maybe_record_escalation_loop_feedback(
                cfg, store, "om_test_real_loop_105", "tp_release", "[卡片回调] 其他：延后10分钟发版"
            )
            self.assertEqual(matched_real, "prod-release-pilot")
            mem_txt = (cfg.loops_dir / "prod-release-pilot" / "MEMORY.md").read_text(encoding="utf-8")
            self.assertEqual(mem_txt.count("延后10分钟发版"), 1)

            # 4) resolve_summary_and_body 同时传入 --summary 且 body 内含 ---more--- 分隔『事情/选项 A/B』与『附录 SQL』时，保留 inline_sum 中的选项不丢失
            from sheepdog.card import resolve_summary_and_body
            from sheepdog.card_callback import parse_ask_options
            edm_summary = "Elaine 申请在生产数据库 tapnow_edm 执行带事务与审计的 SQL，解除 15 个收件人屏蔽状态。建议选 A 批准执行。"
            edm_body = (
                "事情：Elaine申请在生产库执行SQL。\n\n"
                "选项：\n"
                "A. 同意：由专席在生产数据库安全执行该SQL\n"
                "B. 暂缓或调整窗口\n\n"
                "---more---\n"
                "【待执行生产 SQL 脚本与安全保护】\n"
                "- 目标数据库：生产 taplabs-453508 / tapnow_edm"
            )
            s_out, d_out, resolved_out = resolve_summary_and_body(edm_body, edm_summary, hoist_at=False)
            self.assertIn("A. 同意：由专席在生产数据库安全执行该SQL", d_out)
            self.assertIn("【待执行生产 SQL 脚本与安全保护】", d_out)
            parsed_edm_opts = parse_ask_options(resolved_out)
            self.assertEqual([o.key for o in parsed_edm_opts], ["A", "B"])

    def test_reg106_panel_spoke_escalation_nudge_guard_capacity_doc_guard_and_cli_lamb_json_brace_fix(self):
        from sheepdog import (
            __version__,
            admin_web,
            decision_hub,
            engine,
            outbound,
            parse_version_tuple,
            pending,
        )
        from sheepdog.config import Config
        from sheepdog.store import Store

        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 17))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = Config(sink="dryrun")
            cfg.state_dir = root / "state"
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            cfg.brain_dir_path = str(root / "brain")
            Path(cfg.brain_dir_path).mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)

            # 1) 多组件（PG + MySQL + Redis）压测/生产升配方案未附飞书文档链接时触发 REG-106 拦截；附上飞书文档链接后放行
            cap_title = "南京生产DB与Redis压测升配方案"
            cap_bad_body = (
                "已摸清南京生产 PG(8C16G)/MySQL(4C8G)/Redis(4GB) 水位并对比东京压测基线，建议选 A 升配至 PG 16C64G + MySQL 16C32G + Redis 16GB。\n"
                "---more---\n"
                "选项：\n"
                "A. 同意按标准压测档升配（PG 16C64G + MySQL 16C32G + Redis 16GB）\n"
                "B. 按保守减半档升配（PG 16C32G + MySQL 8C16G + Redis 8GB）"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_cap:
                outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", cap_title, cap_bad_body)
            self.assertIn("REG-106", str(ctx_cap.exception))

            cap_good_body = (
                "已将南京生产全栈压测容量方案整理为飞书文档（https://example.SG.larksuite.com/docx/DocToken106），建议选 A。\n"
                + cap_bad_body
            )
            outbound.check_ask_pre_flight_guard(cfg, store, "tp_tapnow-prod-topology", cap_title, cap_good_body)

            # 2) diagnose_override_reason 精准分类『给我完整的方案吧 用lark文档 我要仔细看看』与『我在agy里面跟session讨论...』
            c_doc, l_doc = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · {cap_title} [tp_tapnow-prod-topology]\n{cap_bad_body}",
                "给我完整的方案吧 用lark文档 我要仔细看看",
                "其他",
                "A",
            )
            self.assertEqual((c_doc, l_doc), ("context_incomplete", "缺环境/调用链反问"))

            c_as, l_as = admin_web.diagnose_override_reason(
                "🐕 需要你定 · 南京生产压测剩余4项加固 [tp_tapnow-prod-topology]",
                "这个能够自动扩缩容嘛",
                "其他",
                "A",
            )
            self.assertEqual((c_as, l_as), ("context_incomplete", "缺环境/调用链反问"))

            c_agy, l_agy = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · 南京全栈压测容量方案(附飞书文档) [tp_tapnow-prod-topology]\n{cap_good_body}",
                "我在agy里面跟session讨论 如果确定还是需要我拍板或者明确卡住了 再跟我提一次ask",
                "其他",
                "A",
            )
            self.assertEqual((c_agy, l_agy), ("stale_or_already_handled", "陈旧/已处理未重查"))

            # 3) 主人在 ask 发出后直接在对应会话的 Antigravity IDE 面板发了 USER_EXPLICIT 指令时，
            #    pending.detect 识别为 owner_panel_spoke，且 nudge_open_escalations 自动标 possibly_handled 跳过 30 分钟飞书催办
            cid = "d0758da6-274e-4954-89fa-9507823ec9d8"
            store.upsert_topic("tp_tapnow-prod-topology", kind="custom", title="Topology", conversation_id=cid, state="active")
            log_dir = Path(cfg.brain_dir_path) / cid / ".system_generated" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "transcript.jsonl").write_text(
                json.dumps(
                    {
                        "step_index": 15574,
                        "type": "USER_INPUT",
                        "source": "USER_EXPLICIT",
                        "status": "DONE",
                        "created_at": "2026-11-04T21:41:40+08:00",
                        "content": "<USER_REQUEST>先看一下 K8s 节点 CPU 和内存水位</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            store.add_escalation(
                "om_test_panel_spoke_106",
                "tp_tapnow-prod-topology",
                "oc_hub_106",
                f"🐕 需要你定 · 南京全栈压测容量方案(附飞书文档) [tp_tapnow-prod-topology]\n{cap_good_body}",
                "2026-11-04T21:37:53+08:00",
            )
            e_row = store.get_escalation("om_test_panel_spoke_106")
            ref_now = datetime.fromisoformat("2026-11-04T22:08:16+08:00")
            hit = pending.detect(cfg, store, e_row, until=ref_now)
            self.assertIsNotNone(hit)
            self.assertEqual(hit.reason, pending.PH_PANEL_SPOKE)
            self.assertIn("已在 IDE 面板直接对该会话输入指令", pending.hit_line(cfg, store, hit, ref_now))

            nudged = decision_hub.nudge_open_escalations(cfg, store, None, now=ref_now)
            self.assertEqual(nudged, [])
            e_after = store.get_escalation("om_test_panel_spoke_106")
            self.assertEqual(e_after["possibly_handled_reason"], pending.PH_PANEL_SPOKE)
            self.assertEqual(int(e_after["nudge_count"] or 0), 0)

            # 4) _summarize_cli_lamb_log 识别 === ALL_TENCENT_PG_VERIFIED === 横幅消除前置幂等 Error 假阴性，并过滤截断 JSON 孤立 '{' 行
            pg_log = (
                "Creating readonly user...\n"
                "Error: [TencentCloudSDKError] Code=InvalidAccountStatus, Message=account already exist\n"
                "Granting SELECT...\n"
                "=== ALL_TENCENT_PG_VERIFIED ===\n"
            )
            pg_sum, pg_err = engine._summarize_cli_lamb_log(pg_log)
            self.assertFalse(pg_err)
            self.assertIn("=== ALL_TENCENT_PG_VERIFIED ===", pg_sum)

            trunc_json_log = (
                '{\n'
                '  "step": 698,\n'
                '  "tool_calls": [\n'
                '    {\n'
                '      "name": "run_command",\n'
                '      "args": "kubectl get pods -n tapnowprod"\n'
            )
            trunc_sum, trunc_err = engine._summarize_cli_lamb_log(trunc_json_log)
            self.assertFalse(trunc_err)
            self.assertNotEqual(trunc_sum.strip(), "{")
            self.assertIn("kubectl get pods", trunc_sum)

    def test_reg107_active_lamb_prompt_injection_and_hol_queue_pressure_nudge(self) -> None:
        """REG-107: 独立母羊会话（group_id 为空）名下存在 running 羔羊时，新批次提示词必须自动注入『## 🐑 当前在途羔羊』防主线程重复跑同款重活或 tail 轮询；且当母羊持锁运行满 3 分钟阻塞后续新消息满 3 分钟时，自动发送每批次最多一次的在途队列背压提醒。"""
        from datetime import timedelta
        from unittest.mock import patch
        from sheepdog import engine, observe
        from sheepdog.config import Config
        from sheepdog.models import Message
        from sheepdog.roster import parse_roster
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(self_open_id="ou_test_owner", state_dir=state_dir, config_path=state_dir / "config.toml")
            cfg.playbook_dir = str(Path(__file__).resolve().parent / "fixtures" / "playbook")
            cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
            store = Store(cfg.db_path)
            roster = parse_roster(
                {
                    "session": [
                        {
                            "key": "infra-migration-ops",
                            "mode": "managed",
                            "conversation_id": "conv_test_infra_107",
                            "title": "跨云迁移值守",
                            "duty": "负责跨云迁移改造排障",
                            "chats": [{"chat_id": "oc_test_infra_107", "name": "Infra 迁移群", "all_messages": True}],
                        }
                    ]
                }
            )
            sent_to_sink: list[tuple[str, str]] = []

            class _FakeSink:
                name = "agentapi"

                def send_message(self, conversation_id: str, text: str, batch_id: str = "") -> None:
                    sent_to_sink.append((conversation_id, text))

                def transcript_contains(self, conversation_id: str, needle: str) -> bool:
                    return True

            disp = engine.Dispatcher(cfg, store, _FakeSink(), roster=roster)
            store.upsert_topic(
                "tp_infra-migration-ops",
                kind="standing",
                title="跨云迁移值守",
                conversation_id="conv_test_infra_107",
                state="active",
                onboarded_at="2026-11-05T00:00:00+08:00",
                rules_hash=disp.rules_hash(store.get_topic("tp_infra-migration-ops") or {"topic_id": "tp_infra-migration-ops", "kind": "standing", "title": "跨云迁移值守", "duty": "负责跨云迁移改造排障", "authority": "", "self_polling": "", "retire_self_polling": 0, "group_id": ""}),
            )
            # 1) 母羊已派生在途 subagent 羔羊 lamb_f8a8ad1b 且处于可投递态（state=active, group_id=''）
            store.create_lamb(
                "lamb_f8a8ad1b",
                "tp_infra-migration-ops",
                kind="subagent",
                task_title="排查Ethan反馈历史记录加载失败根因",
                conversation_id="2370527a-b554-4a41-87ae-9e2194e2f3e3",
                state="running",
                started_at="2026-11-05T00:21:29+08:00",
                last_act_at="2026-11-05T00:24:00+08:00",
            )
            m1 = Message(
                message_id="om_test_ethan_followup_107",
                chat_id="oc_test_infra_107",
                chat_name="Infra 迁移群",
                chat_type="group",
                sender_id="ou_test_ethan",
                sender_name="Ethan Wen",
                sender_type="user",
                content="看起来都和文件上传下载有关",
                msg_type="text",
                create_time="2026-11-05T00:24:45+08:00",
            )
            store.upsert_message(m1, "dispatch", "roster_chat_all", [], "tp_infra-migration-ops", "pending")
            t0 = datetime.fromisoformat("2026-11-05T00:24:45+08:00")
            with patch("sheepdog.engine._now", return_value=t0), patch("sheepdog.engine.now_iso", return_value=t0.isoformat()):
                disp.dispatch_once()
            self.assertEqual(len(sent_to_sink), 1)
            batch_prompt_txt = sent_to_sink[-1][1]
            self.assertIn("## 🐑 当前在途羔羊", batch_prompt_txt)
            self.assertIn("lamb_f8a8ad1b", batch_prompt_txt)
            self.assertIn("2370527a-b554-4a41-87ae-9e2194e2f3e3", batch_prompt_txt)
            self.assertIn("--anchors lamb:lamb_f8a8ad1b", batch_prompt_txt)

            # 2) 母羊持锁运行当前批次满 4 分钟，且队列中有新消息（Andy Xiao 在 00:30:00 发来）已排队 4 分钟（>= 180s）：
            #    _dispatch_topic 自动发送一次在途队列背压提醒（每批次最多一次）
            t_cur = store.get_topic("tp_infra-migration-ops")
            blocking_bid = t_cur["pending_batch_id"]
            self.assertTrue(blocking_bid)
            m2 = Message(
                message_id="om_test_andy_queued_107",
                chat_id="oc_test_infra_107",
                chat_name="Infra 迁移群",
                chat_type="group",
                sender_id="ou_test_andy",
                sender_name="Andy Xiao",
                sender_type="user",
                content="国内环境是不是还没有配置 google login 相关的配置？",
                msg_type="text",
                create_time="2026-11-05T00:30:00+08:00",
            )
            store.upsert_message(m2, "dispatch", "roster_chat_all", [], "tp_infra-migration-ops", "pending")

            # 在 00:31:00（排队仅 60s < 180s）时不触发背压提醒
            t_early = datetime.fromisoformat("2026-11-05T00:31:00+08:00")
            with patch("sheepdog.engine._now", return_value=t_early), patch("sheepdog.engine.now_iso", return_value=t_early.isoformat()):
                res_early = disp.dispatch_once()
            self.assertEqual(len(sent_to_sink), 1)
            self.assertNotIn("hol_nudged", res_early["topics"]["tp_infra-migration-ops"])

            # 在 00:34:00（批次已跑 9m15s，m2 已排队 4m >= 180s）时触发一次在途队列背压提醒
            t_late = datetime.fromisoformat("2026-11-05T00:34:00+08:00")
            with patch("sheepdog.engine._now", return_value=t_late), patch("sheepdog.engine.now_iso", return_value=t_late.isoformat()):
                res_nudge = disp.dispatch_once()
            self.assertEqual(len(sent_to_sink), 2)
            self.assertEqual(res_nudge["topics"]["tp_infra-migration-ops"].get("hol_nudged"), blocking_bid)
            nudge_txt = sent_to_sink[-1][1]
            self.assertIn("[sheepdog 队列背压提醒 · 非新批次]", nudge_txt)
            self.assertIn(blocking_bid, nudge_txt)
            self.assertIn("--status waiting --anchors lamb:lamb_f8a8ad1b", nudge_txt)

            # 同一批次再次 tick 不重复发送
            t_later = t_late + timedelta(minutes=1)
            with patch("sheepdog.engine._now", return_value=t_later), patch("sheepdog.engine.now_iso", return_value=t_later.isoformat()):
                disp.dispatch_once()
            self.assertEqual(len(sent_to_sink), 2)

            # 3) observe(self_heal=True) 在 Queue Head-of-Line SLA 满 5 分钟且 state=running 时自动写入 .wakeup
            obs_res = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=datetime.fromisoformat("2026-11-05T00:36:00+08:00"),
                self_heal=True,
                check_processes=False,
                check_log=False,
            )
            self.assertTrue(any("Queue Head-of-Line SLA" in w for w in obs_res["warns"]))
            self.assertTrue((state_dir / ".wakeup").exists())

            # 4) Mail Assistant 邮件通知（含收件人 matthewhong-8968）路由至 INBOX (bot_p2p) 而非误触 bot_p2p_keyword
            from sheepdog import admin_web, outbound, router
            from sheepdog.config import RoutingConfig

            rcfg = RoutingConfig(keywords=["Matthew", "紧急"])
            rctx = router.RouteContext("ou_test_owner")
            mail_msg = Message(
                message_id="om_test_mail_assistant_107",
                chat_id="oc_test_mail_p2p",
                chat_name="",
                chat_type="p2p",
                sender_id="cli_test_mail_assistant_107",
                sender_name="Mail Assistant",
                sender_type="app",
                content='<card title="[Access Request] finderhugo-6325 requested access to tapnow-test-pr-9117-test-1.vercel.app">Access request Hello, matthewhong-8968. finder.hugo@gmail.com wants access</card>',
                msg_type="interactive",
                create_time="2026-11-05T00:39:00+08:00",
            )
            r_dec = router.route(mail_msg, rctx, rcfg)
            self.assertEqual(r_dec.route, router.INBOX)
            self.assertEqual(r_dec.reason, "bot_p2p")

            # 5) check_ask_pre_flight_guard 拦截仅由邮件助手推送且当事人未在飞书主动找过来的自动权限申请 ask
            mail_ask_body = (
                "事情：邮件助手收到 Vercel 访问申请：内部同事 Hugo Xie 请求访问测试环境前端 PR 预览地址 tapnow-test-pr-9117-test-1.vercel.app。\n"
                "选项：\nA. 批准访问并通知他\nB. 暂不批准\n"
                "建议：选 A\n执行：tp_auth"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_mail:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_auth",
                    "Hugo Xie 申请 Vercel 测试预览访问",
                    mail_ask_body,
                )
            self.assertIn("REG-107", str(ctx_mail.exception))

            # 若当事人在飞书主动提出诉求（附飞书消息说明）则正常放行
            feishu_ask_body = mail_ask_body + "\n补充：当事人已在飞书私聊主动找过来（飞书消息 `om_test_feishu_req_107`）请求协助开通。"
            outbound.check_ask_pre_flight_guard(
                cfg,
                store,
                "tp_auth",
                "Hugo Xie 申请 Vercel 测试预览访问",
                feishu_ask_body,
            )

            # 6) diagnose_override_reason 将『邮箱来的消息吗 暂时不管』归入 stale_or_already_handled
            c_mail, l_mail = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · Hugo Xie 申请 Vercel 测试预览访问 [tp_auth]\n{mail_ask_body}",
                "邮箱来的消息吗 暂时不管",
                "其他",
                "A",
            )
            self.assertEqual((c_mail, l_mail), ("stale_or_already_handled", "陈旧/已处理未重查"))

            # 7) _summarize_cli_lamb_log 过滤无行号的 rg .jsonl/.md 匹配行（即便末尾含 error）
            rg_err_log = (
                "/tmp/test_workspace/80_Context/atoms/2026-10-07.jsonl:{\"confidence\": \"high\", \"id\": \"atom_b2639ecbc2e5\"}\n"
                "=== [3] Check Vercel API with User-Agent ===\n"
                "Teams: {\"error\": \"HTTP Error 403: Forbidden\"}\n"
            )
            rg_sum, rg_has_err = engine._summarize_cli_lamb_log(rg_err_log)
            self.assertTrue(rg_has_err)
            self.assertNotIn("2026-10-07.jsonl:", rg_sum)
            self.assertIn("HTTP Error 403: Forbidden", rg_sum)

    def test_reg108_edgeone_subaccount_guard_traceback_and_saved_json_lamb_and_evolve_mail_dedup(self):
        """REG-108（1.1.19）：
        1) check_ask_pre_flight_guard 拦截未注明目标腾讯云子成员账号 ID（如 200053747873 / 200053747813）的 EdgeOne 加速域名配置 ask；
        2) diagnose_override_reason 将『哪个子成员环境』归入 context_incomplete（缺环境/调用链反问）；
        3) _summarize_cli_lamb_log 对 Python Traceback 折叠剥离中间 File "..." 帧直接保留最终异常行，并对 Saved /tmp/xxx.json 提取顶层 JSON 摘要；
        4) evolve.build_evolve_digest D6 过滤已回执（acked）且 id <= last_mail_id 的同行留言，避免 65 分钟窗口内重复刷屏。
        """
        from datetime import datetime, timedelta, timezone
        from sheepdog import admin_web, engine, evolve, outbound
        from sheepdog.config import Config
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(
                self_open_id="ou_test_owner",
                state_dir=state_dir,
                config_path=state_dir / "config.toml",
                sink="dryrun",
            )
            store = Store(cfg.db_path)

            # 1) 未写明腾讯云子成员账号 ID 的 EdgeOne 加速域名请示被拦截
            eo_ask_title = "Kai 申请在生产 EdgeOne 配置 cinemap.tapnow.tv 加速域名"
            eo_ask_body_missing_sub = (
                "事情：Kai 申请在生产 EdgeOne（tapnow.tv 站点）新增加速域名 cinemap.tapnow.tv，绑定泛域名证书、回源至生产独立 CLB。\n"
                "选项：\nA. 同意配置：在生产 EdgeOne 创建 cinemap.tapnow.tv 加速域名并交付 CNAME\nB. 暂缓配置\n"
                "建议：选 A\n执行：专席 tp_kaifeng"
            )
            with self.assertRaises(outbound.OutboundError) as ctx_eo:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_kaifeng",
                    eo_ask_title,
                    eo_ask_body_missing_sub,
                )
            self.assertIn("REG-108", str(ctx_eo.exception))

            # 写明子成员账号 200053747873 后正常放行
            eo_ask_body_with_sub = (
                "事情：EdgeOne 目标站点 tapnow.tv 位于腾讯云国际站生产子成员账号 200053747873（Zone: zone-3wzk608w22ku），回源至生产 CLB。\n"
                "选项：\nA. 同意在生产子账号 200053747873 配置 cinemap.tapnow.tv 加速域名\nB. 暂缓配置\n"
                "建议：选 A\n执行：专席 tp_kaifeng"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                store,
                "tp_kaifeng",
                eo_ask_title,
                eo_ask_body_with_sub,
            )

            # 2) diagnose_override_reason 将『哪个子成员环境』归入 context_incomplete
            c_sub, l_sub = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · {eo_ask_title} [tp_kaifeng]\n{eo_ask_body_missing_sub}",
                "哪个子成员环境",
                "其他",
                "A",
            )
            self.assertEqual((c_sub, l_sub), ("context_incomplete", "缺环境/调用链反问"))

            # 3a) _summarize_cli_lamb_log 剥离 Python Traceback 中间 File "..." 帧，直出末尾异常
            tb_log = (
                '  File "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/urllib/request.py", line 1389, in https_open\n'
                '    return self.do_open(http.client.HTTPSConnection, req,\n'
                '  File "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/urllib/request.py", line 1349, in do_open\n'
                '    raise URLError(err)\n'
                'urllib.error.URLError: <urlopen error EOF occurred in violation of protocol (_ssl.c:1129)>\n'
            )
            tb_sum, tb_err = engine._summarize_cli_lamb_log(tb_log)
            self.assertTrue(tb_err)
            self.assertNotIn('File "/Library/', tb_sum)
            self.assertIn("urllib.error.URLError: <urlopen error EOF occurred in violation of protocol", tb_sum)

            # 3b) _summarize_cli_lamb_log 对 Saved /tmp/..._out.json 自动附加顶层 JSON 摘要
            tmp_json = Path("/tmp/test_reg108_lamb_saved_out.json")
            try:
                tmp_json.write_text('{"ok": true, "domain": "app.tapnow.tv", "certs": [1, 2]}', encoding="utf-8")
                saved_sum, saved_err = engine._summarize_cli_lamb_log(f"Saved {tmp_json.as_posix()}\n")
                self.assertFalse(saved_err)
                self.assertIn("-> JSON(", saved_sum)
                self.assertIn("domain=app.tapnow.tv", saved_sum)
            finally:
                tmp_json.unlink(missing_ok=True)

            # 4) evolve.build_evolve_digest D6 过滤已回执（acked）且 id <= last_mail_id 的同行留言
            now_dt = datetime(2026, 11, 5, 1, 45, 0, tzinfo=timezone(timedelta(hours=8)))
            store.conn.execute(
                "INSERT INTO messages (message_id, chat_id, sender_id, sender_name, sender_type, content, msg_type, create_time, first_seen, last_seen, route, reason, topic_id, dispatch_state) "
                "VALUES ('sd_mail_reg108_1', '__system__', 'tp_auth', 'tp_auth', 'system', 'mail body', 'text', '2026-11-05T00:51:13+08:00', '2026-11-05T00:51:13+08:00', '2026-11-05T00:51:13+08:00', 'dispatch', 'duty_mail', 'tp_sheepdog', 'acked')"
            )
            store.conn.execute(
                "INSERT INTO mailbox (id, from_topic, to_topic, key, body, message_id, created_at) "
                "VALUES (2342, 'tp_auth', 'tp_sheepdog', 'vercel-mail', '邮箱来的消息暂时不管', 'sd_mail_reg108_1', '2026-11-05T00:51:13+08:00')"
            )
            store.conn.commit()
            # 首次未记录 last_mail_id 时可见
            dig1 = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=now_dt)
            self.assertEqual(len(dig1["d6_peer_feedback_inbox"]["peer_mails"]), 1)
            # 记录 last_mail_id=2342 且已 acked 后不再重复出现在 peer_mails
            evolve.write_evolve_state(store, {"last_mail_id": 2342})
            dig2 = evolve.build_evolve_digest(cfg, store, since_minutes=65.0, now_dt=now_dt)
            self.assertEqual(len(dig2["d6_peer_feedback_inbox"]["peer_mails"]), 0)
            self.assertEqual(dig2["d6_peer_feedback_inbox"]["latest_mail_id"], 2342)

    def test_reg109_tp_sheepdog_bus_core_pinning_and_evolve_activity_sync(self) -> None:
        """REG-109 (v1.1.20):
        1. store.is_standing_topic 将 tp_sheepdog（即使 kind='known' 且 lifecycle=None）识别为常设会话（standing）；
        2. evolve.ensure_evolve_topic 对已存在的 known tp_sheepdog 自动补齐 lifecycle='standing' 与 duty；
        3. evolve.run_evolve_tick 触发巡检时同步刷新 tp_sheepdog 的 last_active、dispatched_at 与 summary；
        4. admin_web.WorkbenchService.get_sessions_tree 将 tp_sheepdog 归入顶层 bus_core（紧随 tp_bus 排第 2），不再沉入折叠的 dynamic_oneshot。
        """
        from datetime import datetime, timedelta, timezone
        from sheepdog import admin_web, evolve
        from sheepdog.config import Config
        from sheepdog.store import Store, is_standing_topic

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            cfg = Config(
                self_open_id="ou_test_owner",
                state_dir=state_dir,
                config_path=state_dir / "config.toml",
                sink="dryrun",
            )
            store = Store(cfg.db_path)
            store.create_topic("tp_bus", "总线", "adopted", "路由", "active")
            store.create_topic(
                "tp_sheepdog",
                "🐕 Sheepdog 自治主脑",
                "known",
                "",
                "active",
            )
            store.update_topic(
                "tp_sheepdog",
                conversation_id="c6677355-8d2c-4673-8699-01b1b91a7271",
                last_active="2026-11-03T23:56:26+08:00",
            )

            # 1) is_standing_topic 认 tp_sheepdog
            t0 = store.get_topic("tp_sheepdog")
            self.assertTrue(is_standing_topic(t0))

            # 2) ensure_evolve_topic 自动补齐 lifecycle='standing' 与 duty
            ensured = evolve.ensure_evolve_topic(cfg, store)
            self.assertIsNotNone(ensured)
            self.assertEqual(ensured["lifecycle"], "standing")
            self.assertIn("Sheepdog 内置自进化管家", ensured["duty"])

            # 3) run_evolve_tick(force=True) 同步刷新 tp_sheepdog.last_active / dispatched_at / summary
            now_dt = datetime(2026, 11, 5, 2, 22, 0, tzinfo=timezone(timedelta(hours=8)))
            res = evolve.run_evolve_tick(cfg, store, force=True, now_dt=now_dt)
            self.assertTrue(res["triggered"])
            t1 = store.get_topic("tp_sheepdog")
            self.assertEqual(t1["last_active"], "2026-11-05T02:22:00+08:00")
            self.assertEqual(t1["dispatched_at"], "2026-11-05T02:22:00+08:00")
            self.assertIn("自进化巡检 #1", t1["summary"])

            # 4) WorkbenchService.get_sessions_tree 将 tp_bus 与 tp_sheepdog 同置顶于 bus_core
            wb = admin_web.WorkbenchService(cfg)
            tree = wb.get_sessions_tree()
            self.assertGreaterEqual(len(tree["groups"]), 1)
            g0 = tree["groups"][0]
            self.assertEqual(g0["group_id"], "bus_core")
            self.assertEqual([s["topic_id"] for s in g0["sessions"]], ["tp_bus", "tp_sheepdog"])
            self.assertEqual(g0["sessions"][1]["lifecycle"], "standing")
            self.assertEqual(g0["sessions"][1]["last_active"], "2026-11-05T02:22:00+08:00")

    def test_reg110_owner_group_instruction_auto_settles_open_escalation_and_vip_pr_followthrough_guard(self) -> None:
        """REG-110 (v1.1.21):
        1. pending._owner_followups 与 Dispatcher._auto_settle_open_escalations_on_owner_batch：
           当会话名下存在未结案的「需要你定」提问，且主人在原业务群直接 @ 机器人下达 owner_instruction 指令（即使 reply_to 引用的是群内更早消息），
           ① pending.detect 将其识别为 possibly_handled；
           ② 会话处理完该批次交回非 needs_decision 回执（如 waiting/handled/done）时，_check_receipt 自动将该 open escalation 结案（answered）；
        2. outbound.check_ask_pre_flight_guard 拦截把『谁来跟完我方在途 PR（A. 由我们跟到底 vs B. 由 Sammy 自己来跟合入与验证）』作为选择题抛给主人的甩锅 ask；
        3. admin_web.diagnose_override_reason 将『我在群里回了呀』精准归入 stale_or_already_handled；
        4. engine._summarize_cli_lamb_log 自动折叠连续相同内容的 `[attempt N]` / `[poll N]` 轮询行，仅保留最后一次尝试。
        """
        from sheepdog import admin_web, engine as eng, outbound, pending as pend
        from sheepdog.config import Config
        from sheepdog.roster import Roster
        from sheepdog.security import SecurityConfig
        from sheepdog.sink.agentapi import DryRunSink
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            cfg = Config(
                self_open_id="ou_test_owner",
                state_dir=state_dir,
                config_path=state_dir / "config.toml",
                sink="dryrun",
            )
            store = Store(cfg.db_path)
            store.create_topic("tp_infra", "跨云迁移", "adopted", "迁移", "running")
            store.update_topic(
                "tp_infra",
                conversation_id="cid_test_infra",
                pending_batch_id="b20261105023601-infra",
                dispatched_at="2026-11-05T02:36:01+08:00",
            )
            # 1) 业务群消息与 open escalation
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, chat_type, sender_id, content, create_time, first_seen, last_seen, route, reason, topic_id, dispatch_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_src_vip_q",
                    "oc_group_1",
                    "group",
                    "ou_vip_sammy",
                    "@Matthew .cnb.yml 这件事谁来跟完？",
                    "2026-11-05T02:34:00+08:00",
                    "2026-11-05T02:34:01+08:00",
                    "2026-11-05T02:34:01+08:00",
                    "dispatch",
                    "mention",
                    "tp_infra",
                    "acked",
                ),
            )
            store.add_escalation(
                "om_esc_ask_1",
                "tp_infra",
                "oc_tg_1",
                "🐕 需要你定 · 腾讯云迁移8个PR构建修复与合入收尾 [tp_infra]",
                "2026-11-05T02:35:19+08:00",
            )
            store.add_escalation_sources(
                "om_esc_ask_1",
                "tp_infra",
                [("om_src_vip_q", "oc_group_1")],
                "explicit",
            )
            # 主人在原业务群直接 @ 机器人下达指令（reply_to 指向群内另一条旧消息 om_older_msg）
            store.conn.execute(
                """
                INSERT INTO messages (message_id, chat_id, chat_type, sender_id, reply_to, content, create_time, first_seen, last_seen, route, reason, tags_json, topic_id, batch_id, dispatch_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "om_owner_group_inst",
                    "oc_group_1",
                    "group",
                    "ou_test_owner",
                    "om_older_msg",
                    "@Mattheuu 你直接起 subagent review 完没问题直接合 合完看看有没有解决问题就行",
                    "2026-11-05T02:35:00+08:00",
                    "2026-11-05T02:36:01+08:00",
                    "2026-11-05T02:36:01+08:00",
                    "dispatch",
                    "owner_instruction",
                    json.dumps(["owner_instruction", "owner_at:quote"]),
                    "tp_infra",
                    "b20261105023601-infra",
                    "delivered",
                ),
            )
            store.record_dispatch("b20261105023601-infra", "tp_infra", ["om_owner_group_inst"])
            store.conn.commit()

            # 1a) pending.detect 识别到主人在原群下达的 owner_instruction
            esc_row = store.get_escalation("om_esc_ask_1")
            hit = pend.detect(cfg, store, esc_row)
            self.assertIsNotNone(hit)
            self.assertEqual(hit.reason, pend.PH_SPOKE)

            # 1b) 会话交回 waiting 回执后，_check_receipt 自动将该 open escalation 结案
            disp = eng.Dispatcher(cfg, store, {"antigravity": DryRunSink()}, Roster(), SecurityConfig())
            rp = disp.receipt_path("tp_infra", "b20261105023601-infra")
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(
                json.dumps(
                    {
                        "batch_id": "b20261105023601-infra",
                        "topic_id": "tp_infra",
                        "status": "waiting",
                        "summary": "已按主人群内指令启动 subagent 审查并合入 8 个 PR",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            disp._check_receipt(store.get_topic("tp_infra"))
            esc_after = store.get_escalation("om_esc_ask_1")
            self.assertEqual(esc_after["close_reason"], "answered")
            self.assertEqual(esc_after["answer_message_id"], "om_owner_group_inst")
            self.assertIsNotNone(esc_after["closed_at"])

            # 2) check_ask_pre_flight_guard 拦截把在途 PR 甩给 Sammy 自己合入的伪选择题
            shirk_ask_text = (
                "🐕 需要你定 · 腾讯云迁移8个PR构建修复与合入收尾 [tp_infra]\n"
                "事情：Sammy 询问腾讯云国际迁移 8 个服务 PR 的 .cnb.yml 修复谁来跟完、几点前完成（我们此前已推送 6 个 PR 的 commit）。\n"
                "选项：\n"
                "A. 由我们（Infra与SRE团队）全权跟到底：今天12:00前完成全部8个PR构建修复与合入\n"
                "B. 由Sammy自己来跟合入与验证：我们仅移交已修改的6个PR现状，后续合入由Sammy接手\n"
                "我的建议：选 A"
            )
            with self.assertRaises(outbound.OutboundError) as ctx:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    store,
                    "tp_infra",
                    "腾讯云迁移8个PR构建修复与合入收尾",
                    shirk_ask_text,
                )
            self.assertIn("REG-110", str(ctx.exception))

            # 3) diagnose_override_reason 识别『我在群里回了呀』为 stale_or_already_handled
            cat, label = admin_web.diagnose_override_reason(shirk_ask_text, "我在群里回了呀", "OTHER", "A")
            self.assertEqual(cat, "stale_or_already_handled")
            self.assertEqual(label, "陈旧/已处理未重查")

            # 4) _summarize_cli_lamb_log 折叠连续重复的 [attempt N] 轮询行
            poll_log = (
                "Triggering PR checks...\n"
                "[attempt 2] pending tasks: ['fe-app-tapnow-pr-build']\n"
                "[attempt 3] pending tasks: ['fe-app-tapnow-pr-build']\n"
                "[attempt 4] pending tasks: ['fe-app-tapnow-pr-build']\n"
                "[attempt 5] pending tasks: ['fe-app-tapnow-pr-build']\n"
                "[attempt 6] pending tasks: ['fe-app-tapnow-pr-build']\n"
            )
            sum_txt, has_err = eng._summarize_cli_lamb_log(poll_log)
            self.assertFalse(has_err)
            self.assertEqual(
                sum_txt.splitlines(),
                [
                    "Triggering PR checks...",
                    "[attempt 6] pending tasks: ['fe-app-tapnow-pr-build']",
                ],
            )

    def test_reg111_upgrade_notice_plain_chinese_commit_body_and_semver_regressions_extract(self):
        """1.1.22（REG-111）：
        1. `version_watch.summarize_upgrade_changes` 优先从 Commit Body（`git log --format=%B`）中提取显式的『更新了什么 / 解决了什么问题』，并经 `_plain_chinese_upgrade_text` 自动剥离内部函数名（如 `pending._owner_followups`、`_auto_settle_open_escalations_on_owner_batch`）、正则常量（`_VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE`）与 `REG-XXX` 编号，转为面向主人的中文大白话；
        2. 当 Commit Body 未写结构化摘要时，`REGRESSIONS.md` diff 解析同时支持两段式（`7.93`）与三段式（`1.1.21`）版本号，并自动将『症状；现通过...改进根治：修法』拆入『解决了什么问题』与『更新了什么』且清洗代码黑话；
        3. 当既无 Commit Body 中文说明又无 `REGRESSIONS.md` 变更、仅有纯英文 Conventional Commit 标题时，严禁把 `feat(pending,engine): ...` 原样塞进飞书升级完成通知卡片。
        """
        from sheepdog import version_watch

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "sheepdog").mkdir(parents=True, exist_ok=True)
            (repo / "tests").mkdir(parents=True, exist_ok=True)

            def git(*args: str) -> str:
                cmd = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args]
                return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-q")
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "1.1.20"\n', encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(
                "# 回归\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n| 1.1.20 | 旧症状 | `t0` |\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "-q", "-m", "feat: v1.1.20")
            sha_1120 = git("rev-parse", "HEAD")

            # 场景 A：三段式版本号 1.1.21 + 英文 Commit 标题 + Commit Body 含代码函数名
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "1.1.21"\n', encoding="utf-8")
            git("add", "-A")
            commit_msg_1121 = (
                "feat(pending,engine,outbound): auto-settle open escalations on owner group instruction (v1.1.21, REG-110)\n\n"
                "- 📋 更新了什么：1) pending._owner_followups 识别主人在原业务群下达的 owner_instruction / owner_reply 指令，且 engine._check_receipt 新增 _auto_settle_open_escalations_on_owner_batch 自动结案同会话未关闭的 escalation；2) outbound.check_ask_pre_flight_guard 新增 _VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE 门禁拦截甩锅 ask；3) diagnose_override_reason 将『我在群里回了呀』归入 stale_or_already_handled（REG-110）\n"
                "- 🎯 解决了什么问题：根治主人在业务群直接 @ 机器人拍板后，拍板总台卡片仍悬空未关、迫使主人再点『我在群里回了呀』的问题\n"
            )
            git("commit", "-q", "-m", commit_msg_1121)
            sha_1121 = git("rev-parse", "HEAD")

            summary_a = version_watch.summarize_upgrade_changes(repo, from_sha=sha_1120, to_sha=sha_1121)
            self.assertIn("📋 **更新了什么**", summary_a)
            self.assertIn("🎯 **解决了什么问题**", summary_a)
            self.assertIn("群内 @ 指令或回复", summary_a)
            self.assertIn("「陈旧/已处理未重查」", summary_a)
            self.assertNotIn("feat(pending,engine,outbound)", summary_a)
            self.assertNotIn("_owner_followups", summary_a)
            self.assertNotIn("_auto_settle_open_escalations_on_owner_batch", summary_a)
            self.assertNotIn("_VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE", summary_a)
            self.assertNotIn("stale_or_already_handled", summary_a)
            self.assertNotIn("REG-110", summary_a)

            # 场景 B：Commit Body 无显式说明，但 REGRESSIONS.md 新增 1.1.22 三段式表格行（含『症状；现通过...根治：修法』）
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "1.1.22"\n', encoding="utf-8")
            (repo / "tests" / "REGRESSIONS.md").write_text(
                "# 回归\n\n| 规格 | 症状 | 守它的测试 |\n| --- | --- | --- |\n| 1.1.20 | 旧症状 | `t0` |\n"
                "| 1.1.22 | 升级通知直接打印英文 git commit 标题与函数名导致主人看不懂；现通过三项改进根治：① `version_watch.summarize_upgrade_changes` 优先提取提交正文说明并做中文大白话清洗；② 支持三段式版本号提取（REG-111） | `t111` |\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "-q", "-m", "feat(version_watch): plain Chinese upgrade summary (v1.1.22, REG-111)")
            sha_1122 = git("rev-parse", "HEAD")

            summary_b = version_watch.summarize_upgrade_changes(repo, from_sha=sha_1121, to_sha=sha_1122)
            self.assertIn("📋 **更新了什么**：① 优先提取提交正文说明并做中文大白话清洗", summary_b)
            self.assertIn("🎯 **解决了什么问题**：升级通知直接打印英文 git commit 标题与函数名导致主人看不懂", summary_b)
            self.assertNotIn("summarize_upgrade_changes", summary_b)
            self.assertNotIn("feat(version_watch)", summary_b)
            self.assertNotIn("REG-111", summary_b)

            # 场景 C：既无 Commit Body 说明又无 REGRESSIONS.md 变更，仅有纯英文 Commit 标题时，兜底绝不直出英文标题
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "1.1.23"\n', encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "fix(engine): prevent duplicate dispatch lock under race condition")
            sha_1123 = git("rev-parse", "HEAD")
            summary_c = version_watch.summarize_upgrade_changes(repo, from_sha=sha_1122, to_sha=sha_1123)
            self.assertIn("完成本轮代码改进与问题修复", summary_c)
            self.assertNotIn("fix(engine):", summary_c)

    def test_reg112_redirected_cli_lamb_error_extract_and_running_retry_recovery_and_ask_option_log_guard(self):
        """1.1.23（REG-112）：
        1. `engine._summarize_cli_lamb_log` 对 `subprocess.CalledProcessError` 含 `> /tmp/*.log` 重定向的失败 CLI 羔羊自动钻取重定向日志末尾的真实报错行（如 `RuntimeError: ...`），并剥离多行 JSON 过滤后残留在普通文本行尾的孤立 `{` / `[`；
        2. `evolve.build_evolve_digest` D5 在母羊已派生更新的在途重试羔羊（`state IN ('done', 'running') AND started_at >= finished_at`）时将失败羔羊标为 `recovered_by_parent=True` 不误报 `failed_lambs`，且 `engine._deliver_brain_mail` 直投来信后同步推进 `self_evolve_state.last_mail_id` 防 D6 重复播报；
        3. `outbound.check_ask_pre_flight_guard` 新增 `_RAW_STDOUT_IN_ASK_OPTION_RE` 门禁，拦截在 `sheepdog ask` 选项 A/B/C 中直接粘贴原始命令执行日志/报错输出（如 `[PROD] Status: FAILED`、`ERROR: column ... does not exist`）。
        """
        from sheepdog import config, engine, evolve, outbound, store
        from sheepdog.engine import _summarize_cli_lamb_log

        with tempfile.TemporaryDirectory() as tmp:
            # 1) Redirected /tmp/*.log root-cause extraction & dangling '{' strip
            redir_path = Path("/tmp/test_reg112_redirected_dag_exec.log")
            try:
                redir_path.write_text(
                    "[conversation-service] poll 1: build 89edf3a3 status=WORKING\n"
                    "[conversation-service] poll 2: build 89edf3a3 status=SUCCESS\n"
                    "Traceback (most recent call last):\n"
                    '  File "/tmp/release_pilot.py", line 1266, in execute_dag_lane\n'
                    "    raise RuntimeError('Layer 0 failed')\n"
                    'RuntimeError: Layer 0 execution failed: [{"service": "conversation-service", "error": "Cannot find service [conversation-service-http]"}]\n',
                    encoding="utf-8",
                )
                wrapper_log = (
                    "Rejected b2557c27: CANCELLED\n"
                    "Traceback (most recent call last):\n"
                    '  File "/tmp/run_cs_0054_direct.py", line 31, in <module>\n'
                    '    subprocess.run("python3 release_pilot.py execute-dag > /tmp/test_reg112_redirected_dag_exec.log 2>&1", shell=True, check=True)\n'
                    "subprocess.CalledProcessError: Command 'python3 release_pilot.py execute-dag > /tmp/test_reg112_redirected_dag_exec.log 2>&1' returned non-zero exit status 1.\n"
                )
                sum_err, has_err = _summarize_cli_lamb_log(wrapper_log)
                self.assertTrue(has_err)
                self.assertIn("-> RuntimeError: Layer 0 execution failed", sum_err)
                self.assertIn("Cannot find service [conversation-service-http]", sum_err)
            finally:
                redir_path.unlink(missing_ok=True)

            dangling_brace_log = (
                "Rejected 0057 build c2fcd128-3d53-471f-b9d3-c42b2e85a23a {\n"
                '  "name": "operations/build/taplabs-453508/YzJm",\n'
                '  "metadata": {\n'
                '    "@type": "type.googleapis.com/google.devtools.cloudbuild.v1.BuildOperationMetadata",\n'
                "202611040058 execute-dag completed successfully!\n"
            )
            sum_ok, has_err_ok = _summarize_cli_lamb_log(dangling_brace_log)
            self.assertFalse(has_err_ok)
            self.assertIn("Rejected 0057 build c2fcd128-3d53-471f-b9d3-c42b2e85a23a\n202611040058 execute-dag completed successfully!", sum_ok)
            self.assertNotIn("c2fcd128-3d53-471f-b9d3-c42b2e85a23a {", sum_ok)

            # 2) evolve running retry lamb recovery
            db_path = Path(tmp) / "sheepdog.sqlite3"
            st = store.Store(db_path)
            cfg = config.Config()
            now_dt = datetime.now().astimezone()
            t_fail_start = (now_dt - timedelta(minutes=6)).isoformat(timespec="seconds")
            t_fail_end = (now_dt - timedelta(minutes=2)).isoformat(timespec="seconds")
            t_retry_start = (now_dt - timedelta(minutes=1)).isoformat(timespec="seconds")
            st.create_lamb("lamb_fail01", "tp_release", "cli", "dag-0054")
            st.update_lamb(
                "lamb_fail01",
                state="failed",
                started_at=t_fail_start,
                finished_at=t_fail_end,
                result_summary="RuntimeError: Cannot find service",
            )
            st.create_lamb("lamb_retry02", "tp_release", "cli", "dag-0054-retry")
            st.update_lamb(
                "lamb_retry02",
                state="running",
                started_at=t_retry_start,
            )
            digest = evolve.build_evolve_digest(cfg, st, now_dt=now_dt)
            lamb_map = {x["lamb_id"]: x for x in digest["d5_bus_and_lambs"]["recent_lambs"]}
            self.assertTrue(lamb_map["lamb_fail01"]["recovered_by_parent"])
            self.assertNotIn("failed_lambs(1)", digest["action_reasons"])

            # 3) outbound ask option raw execution stdout guard
            with self.assertRaises(outbound.OutboundError) as ctx:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_tapnow-prod-topology",
                    "tapnow.tv 生产修复请示",
                    "事情：南京生产修复方案请示。\n"
                    "选项 A：立即执行生产变更\n"
                    "[PROD] Status: FAILED, Code: 1\n"
                    "ERROR: column f.category does not exist\n"
                    "选项 B：保持现状观察\n"
                    "[PROD] Status: SUCCESS, Code: 0\n"
                    'deployment "billing-auth-central" successfully rolled out\n'
                    "建议：选 A",
                )
            self.assertIn("REG-112", str(ctx.exception))

    def test_reg113_subagent_timeout_oscillation_guard_and_unawakened_owner_echo_self_heal(self):
        """REG-113 (1.1.24):
        1) Subagent lamb that timed out on 5-min GENERIC DONE mid-turn stall must NOT be revived by _scan_lambs
           when task_act <= finished_at or now - task_act >= 5 min, and complete_lamb must deduplicate lamb_report
           system messages so a single lamb_id never emits duplicate lamb_report batches.
        2) When an idle Antigravity topic has 0 pending rows but has an unawakened owner_echoes (delivered without
           producing any PLANNER_RESPONSE) and/or a duplicate missed lamb_report batch, both engine._auto_unstick_idle_antigravity
           and observe --self-heal suppress the duplicate lamb_report and re-queue the owner echo to wake up the session.
        """
        from sheepdog.engine import complete_lamb

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(state_dir=Path(tmp))
            db_path = cfg.db_path
            st = Store(db_path)
            st.create_topic("tp_demo", "Demo", "adopted", "duty", "active", "antigravity")
            st.update_topic("tp_demo", conversation_id="cid-parent-113")
            st.create_lamb("lamb_osc01", "tp_demo", "subagent", "sub-task", conversation_id="cid-sub-113")

            # First complete_lamb emits 1 lamb_report and records report_msg_id
            r1 = complete_lamb(st, "lamb_osc01", summary="first timeout", state="timeout")
            self.assertTrue(r1["message_id"].startswith("sd_lamb_report_"))
            lb_row = st.get_lamb("lamb_osc01")
            self.assertEqual(lb_row["report_msg_id"], r1["message_id"])

            # Even if reported_at and report_msg_id were wiped, complete_lamb checks existing lamb_report in messages
            st.update_lamb("lamb_osc01", state="running", reported_at=None, report_msg_id=None)
            r2 = complete_lamb(st, "lamb_osc01", summary="second timeout", state="timeout")
            self.assertEqual(r2["message_id"], "")
            rep_count = st.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE topic_id='tp_demo' AND reason='lamb_report'"
            ).fetchone()[0]
            self.assertEqual(rep_count, 1)

            # Set up an acked lamb_report + a duplicate missed lamb_report + an unawakened priority owner_echo
            now_dt = datetime.now().astimezone()
            old_iso = (now_dt - timedelta(minutes=25)).isoformat(timespec="seconds")
            st.mark_batch([r1["message_id"]], "b_acked_lamb", "tp_demo", "acked")
            dup_mid = st.add_system_message(
                "tp_demo",
                "lamb_report",
                "duplicate report",
                tags=["lamb:lamb_osc01", "lamb_state:timeout"],
            )
            st.record_dispatch("b_missed_dup", "tp_demo", [dup_mid], state="missed")
            st.mark_batch([dup_mid], "b_missed_dup", "tp_demo", "missed")

            cb_mid = "sd_card_cb_reg113_test001"
            st.conn.execute(
                "INSERT INTO messages(message_id, chat_id, sender_id, content, create_time, first_seen, last_seen, route, reason, topic_id, dispatch_state, batch_id, tags_json) "
                "VALUES(?, 'oc_test', 'ou_owner', '[卡片回调] A：同意', ?, ?, ?, 'dispatch', 'owner_reply', 'tp_demo', 'acked', 'p_prio_113', '[]')",
                (cb_mid, old_iso, old_iso, old_iso),
            )
            st.conn.execute(
                "INSERT INTO owner_echoes(message_id, chat_id, kind, said_at, stage, topic_id, recorded_at, stage_at, delivered_at, batch_id, priority) "
                "VALUES(?, 'oc_test', 'owner_reply', ?, 'delivered', 'tp_demo', ?, ?, ?, 'p_prio_113', 1)",
                (cb_mid, old_iso, old_iso, old_iso, old_iso),
            )
            st.upsert_card("tp_demo", run_state="lost", detail="批次送出后不动", stuck_since=old_iso)
            st.conn.commit()

            # Run observe.run_observe(self_heal=True): should suppress duplicate missed lamb_report, re-queue cb_mid, and emit 0 warnings
            rep = observe.run_observe(
                cfg,
                db_path=db_path,
                now_dt=now_dt,
                antigravity_brain_dir=Path(tmp) / "brain",
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertEqual(rep["warns"], [])
            self.assertIn("tp_demo:owner_echo_requeue", rep["self_healed"])

            msg_cb = st.get_message(cb_mid)
            self.assertEqual(msg_cb["dispatch_state"], "pending")
            msg_dup = st.get_message(dup_mid)
            self.assertEqual(msg_dup["dispatch_state"], "acked")

            # Subsequent observe within 30s (still pending) and at +5m (after re-delivery) both produce 0 warnings
            rep2 = observe.run_observe(
                cfg,
                db_path=db_path,
                now_dt=now_dt + timedelta(seconds=30),
                antigravity_brain_dir=Path(tmp) / "brain",
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertEqual(rep2["warns"], [])

            st.mark_batch([cb_mid], "b_redeliv_113", "tp_demo", "delivered")
            st.echo_update(cb_mid, stage="delivered", stage_at=now_dt.isoformat(timespec="seconds"), batch_id="b_redeliv_113")
            rep3 = observe.run_observe(
                cfg,
                db_path=db_path,
                now_dt=now_dt + timedelta(minutes=5),
                antigravity_brain_dir=Path(tmp) / "brain",
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertEqual(rep3["warns"], [])
            st.close()

    def test_reg114_evolve_timeout_lamb_recovery_subagent_meta_strip_cli_board_stderr_and_missed_unstick(self):
        """REG-114 (v1.1.25):
        1) evolve.build_evolve_digest D5 marks timeout lambs as recovered_by_parent=True when the parent session subsequently acks its batch without open failures;
        2) guard._summarize_subagent_return & engine._scan_lambs strip Markdown report metadata bullets (任务单号/派发母羊/执行角色/核验时间) and truncated table rows;
        3) engine._summarize_cli_lamb_log & _scan_lambs strip trailing empty [DAG_STDERR]: / DAG stderr: labels and trailing sheepdog board dumps;
        4) engine._auto_unstick_idle_antigravity & _recover_idle_stuck_topics re-queue unawakened missed batches even after _check_stopped_in_flight sets duty_reset:<tid>.
        """
        from sheepdog import evolve, guard, engine as eng_mod
        from sheepdog.engine import Dispatcher, _summarize_cli_lamb_log
        from sheepdog.roster import Roster, RosterSession

        # 1) Subagent return summary strips metadata bullets and truncated table rows
        raw_sub_md = (
            "[lamb:lamb_9844f0ae]\n"
            "# 羔羊独立盲审报告：T1 只读云上快照对账与 Gate 2 零未知漂移验算\n\n"
            "- **任务单号**：`lamb_9844f0ae`\n"
            "- **派发母羊**：`tp_tapnow-prod-topology`\n"
            "- **执行角色**：`Meru-Verify Subagent`\n"
            "- **核验时间**：2026-11-05 11:25 CST\n\n"
            "| 核验项 | 状态 |\n"
            "| :--- | :--- |\n"
            "| `tapnow.tv` 站点归属 <truncated 9784 bytes>\n\n"
            "母羊（`tp_tapnow-prod-topology`）您好，后台羔羊 `lamb_9844f0ae` 任务执行完毕，核心结论如下：\n"
            "Gate 2 零未知漂移验算 100% 通过，生产子账号 200053747873 下无任何漂移。\n"
        )
        sub_sum = guard._summarize_subagent_return(raw_sub_md)
        self.assertNotIn("任务单号", sub_sum)
        self.assertNotIn("lamb_9844f0ae", sub_sum)
        self.assertIn("Gate 2 零未知漂移验算 100% 通过", sub_sum)

        # 2) CLI lamb log summary strips trailing empty [DAG_STDERR]: / DAG stderr: and trailing sheepdog board dumps
        raw_cli_stderr = (
            "[DAG_STDOUT]:\n"
            "{\n"
            '  "ok": true,\n'
            '  "lane_id": "lane-a-regular",\n'
            '  "service": "tapnow-bff",\n'
            '  "status": "SUCCESS"\n'
            "}\n\n"
            "[DAG_STDERR]:\n"
        )
        cli_sum1, cli_err1 = _summarize_cli_lamb_log(raw_cli_stderr)
        self.assertFalse(cli_err1)
        self.assertIn("JSON(ok=True, status=SUCCESS, lane_id=lane-a-regular, service=tapnow-bff)", cli_sum1)
        self.assertNotIn("[DAG_STDERR]:", cli_sum1)

        raw_cli_board = (
            "=== 1. Approval Instance Details ===\n"
            "Status: APPROVED\n"
            "Service: fe-app-tapnow (v2.18.0)\n"
            "=== tp_sammy-shi board ===\n"
            "羊群看板 · 1 个会话\n"
            "- 2026-11-05T09:27:29+08:00 Reply, forward, and other actions: 12 items\n"
        )
        cli_sum2, cli_err2 = _summarize_cli_lamb_log(raw_cli_board)
        self.assertFalse(cli_err2)
        self.assertIn("Service: fe-app-tapnow (v2.18.0)", cli_sum2)
        self.assertNotIn("Reply, forward, and other actions", cli_sum2)
        self.assertNotIn("羊群看板", cli_sum2)

        # 3) Evolve D5 timeout lamb recovery + _recover_idle_stuck_topics unsticking missed batch after duty_reset
        from datetime import timezone
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sheepdog.sqlite3"
            st = Store(db_path)
            now_dt = datetime(2026, 11, 5, 11, 40, 0, tzinfo=timezone(timedelta(hours=8)))
            t0_iso = (now_dt - timedelta(minutes=25)).isoformat(timespec="seconds")
            t_fin_iso = (now_dt - timedelta(minutes=20)).isoformat(timespec="seconds")
            t_ack_iso = (now_dt - timedelta(minutes=5)).isoformat(timespec="seconds")

            st.upsert_topic("tp_kaifeng", title="Kai Feng", state="active", conversation_id="cid_kf", host="antigravity")
            st.conn.execute(
                """INSERT INTO lambs(lamb_id, parent_topic_id, kind, task_title, state, started_at, last_act_at, finished_at, result_summary)
                   VALUES('lamb_33b04ccc', 'tp_kaifeng', 'subagent', 'Cinemap Stage build', 'timeout', ?, ?, ?, '羔羊在工具返回后静默中断超 5 分钟')""",
                (t0_iso, t0_iso, t_fin_iso),
            )
            st.record_dispatch("b_kf_after", "tp_kaifeng", ["om_kf_1"])
            st.conn.execute(
                "UPDATE dispatches SET state='acked', sent_at=?, acked_at=? WHERE batch_id='b_kf_after'",
                (t_fin_iso, t_ack_iso),
            )
            st.conn.commit()

            cfg = Config(state_dir=Path(tmp))
            with mock.patch("sheepdog.observe.run_observe", return_value={"warns": [], "infos": [], "self_healed": []}):
                dig = evolve.build_evolve_digest(cfg, st, since_minutes=60, now_dt=now_dt)
            self.assertNotIn("failed_lambs(1)", dig["action_reasons"])
            kf_lamb = next(lb for lb in dig["d5_bus_and_lambs"]["recent_lambs"] if lb["lamb_id"] == "lamb_33b04ccc")
            self.assertTrue(kf_lamb["recovered_by_parent"])

            # 4) Standalone recovery of unawakened missed batch when duty_reset:<tid> > sent_at and card is IDLE
            brain_dir = Path(tmp) / "brain"
            cid_af = "3bd5503a-152b-4ff3-b76e-1a8f04d91e9f"
            tp_dir = brain_dir / cid_af / ".system_generated" / "logs"
            tp_dir.mkdir(parents=True, exist_ok=True)
            tp_file = tp_dir / "transcript.jsonl"
            prev_act_iso = (now_dt - timedelta(minutes=30)).isoformat(timespec="seconds")
            missed_sent_iso = (now_dt - timedelta(minutes=10)).isoformat(timespec="seconds")
            reset_iso = (now_dt - timedelta(minutes=8)).isoformat(timespec="seconds")
            tp_file.write_text(
                json.dumps({"type": "PLANNER_RESPONSE", "created_at": prev_act_iso, "content": "prev ok"}) + "\n"
                + json.dumps({"type": "USER_INPUT", "created_at": missed_sent_iso, "content": "batch b_missed_af"}) + "\n",
                encoding="utf-8",
            )
            st.upsert_topic("tp_agent-first", title="Agent First", kind="adopted", state="active",
                            conversation_id=cid_af, host="antigravity", onboarded_at=prev_act_iso)
            st.upsert_card("tp_agent-first", run_state="idle", last_act_at=prev_act_iso, idle_since=reset_iso)
            st.set_meta("duty_reset:tp_agent-first", reset_iso)

            colleague_mid = "om_colleague_msg_114"
            st.conn.execute(
                """INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sender_name, create_time, msg_type, content,
                   route, reason, topic_id, dispatch_state, batch_id, first_seen, last_seen, tags_json)
                   VALUES(?, 'oc_af', 'group', 'ou_kf', 'Kai Feng', ?, 'text', '请看下这个报错',
                   'dispatch', 'group_Roster', 'tp_agent-first', 'missed', 'b_missed_af', ?, ?, '[]')""",
                (colleague_mid, missed_sent_iso, missed_sent_iso, missed_sent_iso),
            )
            st.record_dispatch("b_missed_af", "tp_agent-first", [colleague_mid])
            st.conn.execute(
                "UPDATE dispatches SET state='missed', sent_at=? WHERE batch_id='b_missed_af'",
                (missed_sent_iso,),
            )
            st.conn.commit()

            roster = Roster(
                sessions=[RosterSession(key="agent-first", mode="managed", title="Agent First", duty="test", chats=[])],
            )

            class _FakeAgentApiSink:
                name = "agentapi"

            disp = Dispatcher(cfg, st, _FakeAgentApiSink(), roster=roster)
            with mock.patch.object(eng_mod, "transcript_path", side_effect=lambda c: brain_dir / c / ".system_generated" / "logs" / "transcript.jsonl"), \
                 mock.patch.object(eng_mod, "_now", return_value=now_dt):
                disp._recover_idle_stuck_topics(["tp_agent-first"])

            msg_af = st.get_message(colleague_mid)
            self.assertEqual(msg_af["dispatch_state"], "pending")
            d_af = st.get_dispatch("b_missed_af")
            self.assertEqual(d_af["state"], "redelivered")
            st.close()

    def test_reg115_mainland_fallback_and_domestic_domain_audience_guards_and_sys_batch_redeliver(self):
        """1.1.26（REG-115）：
        1) check_ask_pre_flight_guard 拦截遇地域受限盲目请示回退境内主账号新建子账号（要求先问是否着急并提供咨询腾讯云选项）与新增国内域名未写明给谁访问；
        2) diagnose_override_reason 精准将『问一下这个着急吗 不着急的话我可以先问一下腾讯云的人』『问一下国内域名是给谁访问的』归入 context_incomplete；
        3) _recheck_uncertain 对未落入 transcript 的纯内部系统通知批次（sd_lamb_report_*）自动退回 pending 重投；
        4) _summarize_cli_lamb_log 支持带后缀的 Saved /tmp/...json 自动追加 JSON(...) 并过滤 gcloud run jobs 提示行。
        """
        from datetime import timezone
        from sheepdog import admin_web, engine as eng_mod, outbound
        from sheepdog.engine import Dispatcher, _summarize_cli_lamb_log

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sheepdog.sqlite3"
            st = Store(db_path)
            cfg = Config(state_dir=Path(tmp))

            # 1a) Mainland fallback without urgency & Tencent contact -> blocked
            with self.assertRaises(outbound.OutboundError) as ctx1:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_kaifeng",
                    "请定国内腾讯云建TMS/IMS审核子账号",
                    "国际账号调广州报 UnsupportedRegion，必须在境内主账号 100048360698 下建专有审核子账号。\n"
                    "选项：\nA. 批准在境内主账号 100048360698 创建子账号\nB. 暂缓创建，继续用本地词表\n建议：选 A",
                )
            self.assertIn("REG-115", str(ctx1.exception))

            # With urgency confirmed + Tencent contact option -> allowed
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_kaifeng",
                "请定国内腾讯云建TMS/IMS审核子账号",
                "已向 Kai 确认是否着急（不急需今晚切云端），国际账号调广州报 UnsupportedRegion，若必须在境内主账号 100048360698 下建子账号请拍板。\n"
                "选项：\nA. 先联系并问一下腾讯云的人开通国际站权限或支持地域\nB. 批准在境内主账号 100048360698 创建子账号\n建议：选 A",
            )

            # 1b) New domestic domain without target audience -> blocked
            with self.assertRaises(outbound.OutboundError) as ctx2:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_edm-open-tracking",
                    "是否新增国内跳转域名 edm.tapnow.media",
                    "Elaine 实测国内直连打不开 edm.tapnow.ai，申请新增国内直连域名 edm.tapnow.media。\n"
                    "选项：\nA. 同意新增 edm.tapnow.media\nB. 暂不建新域名\n建议：选 A",
                )
            self.assertIn("REG-115", str(ctx2.exception))

            # With target audience explicitly documented -> allowed
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_edm-open-tracking",
                "是否新增国内跳转域名 edm.tapnow.media",
                "已向 Elaine 确认该国内域名是给谁访问的（目标访问受众为国内站真实 C 端订阅客户），并已确认能否改用 edm.tapnow.tv。\n"
                "选项：\nA. 同意新增 edm.tapnow.tv\nB. 暂不建新域名\n建议：选 A",
            )

            # 2) diagnose_override_reason classification
            code1, label1 = admin_web.diagnose_override_reason(
                "请定国内腾讯云建TMS/IMS审核子账号",
                "问一下这个着急吗  不着急的话我可以先问一下腾讯云的人",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual(code1, "context_incomplete")
            self.assertEqual(label1, "缺环境/调用链反问")

            code2, label2 = admin_web.diagnose_override_reason(
                "是否新增国内跳转域名 edm.tapnow.media",
                "问一下国内域名是给谁访问的",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual(code2, "context_incomplete")
            self.assertEqual(label2, "缺环境/调用链反问")

            # 3) _recheck_uncertain auto-redelivers unverified internal system batch (sd_lamb_report_*)
            now_dt = datetime(2026, 11, 5, 12, 50, 0, tzinfo=timezone(timedelta(hours=8)))
            sent_iso = (now_dt - timedelta(minutes=5)).isoformat(timespec="seconds")
            prev_iso = (now_dt - timedelta(minutes=10)).isoformat(timespec="seconds")
            brain_dir = Path(tmp) / "brain"
            cid_edm = "83f03a49-990b-4b53-bb85-52952e0bb85f"
            tp_dir = brain_dir / cid_edm / ".system_generated" / "logs"
            tp_dir.mkdir(parents=True, exist_ok=True)
            (tp_dir / "transcript.jsonl").write_text(
                json.dumps({"type": "PLANNER_RESPONSE", "created_at": prev_iso, "content": "done"}) + "\n",
                encoding="utf-8",
            )
            st.upsert_topic("tp_edm", title="EDM", kind="dynamic", state="active", conversation_id=cid_edm, host="antigravity")
            lamb_msg_id = "sd_lamb_report_test115"
            st.conn.execute(
                """INSERT INTO messages(message_id, chat_id, chat_type, sender_id, sender_name, create_time, msg_type, content,
                   route, reason, topic_id, dispatch_state, batch_id, first_seen, last_seen, tags_json)
                   VALUES(?, 'oc_sys', 'p2p', 'sys', 'sheepdog', ?, 'text', '羔羊失败汇报',
                   'dispatch', 'lamb_report', 'tp_edm', 'uncertain', 'b_unc_lamb115', ?, ?, '[]')""",
                (lamb_msg_id, sent_iso, sent_iso, sent_iso),
            )
            st.record_dispatch("b_unc_lamb115", "tp_edm", [lamb_msg_id], state="uncertain")
            st.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id='b_unc_lamb115'", (sent_iso,))
            st.conn.commit()

            class _FakeSink:
                name = "agentapi"

            disp = Dispatcher(cfg, st, _FakeSink())
            with mock.patch.object(eng_mod, "transcript_path", side_effect=lambda c: brain_dir / c / ".system_generated" / "logs" / "transcript.jsonl"), \
                 mock.patch.object(eng_mod, "_now", return_value=now_dt):
                disp._recheck_uncertain()

            self.assertEqual(st.get_dispatch("b_unc_lamb115")["state"], "redelivered")
            self.assertEqual(st.get_message(lamb_msg_id)["dispatch_state"], "pending")

            # 4) _summarize_cli_lamb_log handles Saved /tmp/...json with trailing metadata and strips gcloud run jobs hints
            tmp_json = Path("/tmp/test_reg115_diag.json")
            try:
                tmp_json.write_text(json.dumps({"ok": True, "locked_by": "job_0068"}), encoding="utf-8")
                s_sum, s_err = _summarize_cli_lamb_log(f"Saved {tmp_json}, code len: 4521 asc_logs: 34\n")
                self.assertFalse(s_err)
                self.assertIn("-> JSON(ok=True, locked_by=job_0068)", s_sum)
            finally:
                tmp_json.unlink(missing_ok=True)

            err_log = (
                "Execution [tapnow-edm-migrate-7x2g5] has successfully completed.\n"
                "View details about this execution by running:\n"
                "gcloud run jobs executions describe tapnow-edm-migrate-7x2g5\n"
                "Or visit https://console.cloud.google.com/run/jobs/executions/details/asia-northeast1/tapnow-edm-migrate-7x2g5\n"
                "[5/6] Updating URL Map tap-prod-global-elb-2...\n"
                "Traceback (most recent call last):\n"
                '  File "<string>", line 2, in <module>\n'
                "ModuleNotFoundError: No module named 'yaml'\n"
            )
            e_sum, e_err = _summarize_cli_lamb_log(err_log)
            self.assertTrue(e_err)
            self.assertNotIn("Or visit https://console.cloud.google.com", e_sum)
            self.assertIn("ModuleNotFoundError: No module named 'yaml'", e_sum)
            st.close()

    def test_reg116_tapnow_tv_domain_reuse_guard_and_subagent_sysmsg_stall_harvest_and_http_header_filter(self):
        """1.1.27（REG-116）：
        1. check_ask_pre_flight_guard 拦截请示新增 *.tapnow.media 国内子域名但未先询问/提供 *.tapnow.tv 复用方案的提问；
        2. diagnose_override_reason 将『问一下能不能用 edm.tapnow.tv』归入 arch_or_config_posture（技术姿势纠偏）；
        3. _harvest_subagent_stall_state 识别末尾停在 SYSTEM_MESSAGE DONE（后台任务完成/取消通知）的半途静默态并从 SYSTEM_MESSAGE 提取 Command output；
        4. _summarize_cli_lamb_log 自动过滤 curl -i 打印的原始 HTTP 响应头（server:/via:/alt-svc: 等）保留真实执行摘要。
        """
        from sheepdog import admin_web, outbound
        from sheepdog.config import Config
        from sheepdog.engine import _harvest_subagent_stall_state, _summarize_cli_lamb_log
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sheepdog.sqlite3"
            st = Store(db)
            cfg = Config(state_dir=Path(tmp))

            # 1) Proposing new *.tapnow.media domestic subdomain without checking *.tapnow.tv -> blocked
            with self.assertRaises(outbound.OutboundError) as ctx1:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_edm-open-tracking",
                    "大促第2批国内域名决策：是否新增 edm.tapnow.media",
                    "事情：Elaine 追问国内直连域名决策，今天大促第 2 批国内站邮件正等该结论。\n"
                    "调用链：国内用户 -> https://edm.tapnow.media/t/* -> 腾讯云 EdgeOne（生产子账号 200053747873） -> Cloud Run tapnow-edm。\n"
                    "选项：\nA. 同意新增 edm.tapnow.media：由专席在生产子账号 200053747873 下配置 EdgeOne 与 DNSPod 解析\n"
                    "B. 今天不做国内域名\n建议：选 A",
                )
            self.assertIn("REG-116", str(ctx1.exception))
            self.assertIn("edm.tapnow.tv", str(ctx1.exception))

            # With *.tapnow.tv option/confirmation included -> allowed
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_edm-open-tracking",
                "大促第2批国内域名决策：是否新增 edm.tapnow.media 或改用 edm.tapnow.tv",
                "事情：已向 Elaine 询问能不能直接用 edm.tapnow.tv（直接复用生产子账号 200053747873 下的 tapnow.tv 站点）。\n"
                "调用链：国内用户 -> https://edm.tapnow.tv/t/* -> 腾讯云 EdgeOne（生产子账号 200053747873） -> Cloud Run tapnow-edm。\n"
                "选项：\nA. 优先用 edm.tapnow.tv：直接复用生产子账号 200053747873 下的 tapnow.tv 站点\n"
                "B. 仍新增 edm.tapnow.media\n建议：选 A",
            )

            # 2) diagnose_override_reason classification for "问一下能不能用 edm.tapnow.tv"
            code, label = admin_web.diagnose_override_reason(
                "大促第2批国内域名决策：是否新增 edm.tapnow.media",
                "问一下能不能用 edm.tapnow.tv",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual(code, "arch_or_config_posture")
            self.assertEqual(label, "技术姿势纠偏")

            # 3) _harvest_subagent_stall_state recognizes trailing SYSTEM_MESSAGE DONE and extracts Command output from SYSTEM_MESSAGE
            tp_sub = Path(tmp) / "subagent_transcript.jsonl"
            steps = [
                {
                    "step_index": 100,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "You have 1 background task(s) currently running",
                },
                {
                    "step_index": 101,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "content": (
                        "<SYSTEM_MESSAGE>\n"
                        'Task id "task-96" finished with result:\n'
                        "Command exited with code 0.\n\n"
                        "Command output:\n"
                        "=== CURL TEST (api.ipify.org) ===\n"
                        "Result: \n"
                        "=== CURL TEST (httpbin.org/ip) ===\n"
                        "Result 2: \n"
                        "=== MIHOMO LOGS (last 20 lines) ===\n"
                        'time="2026-11-05T12:43:08.026413000+08:00" level=warning msg="[TCP] dial PROXY (match Match/) 127.0.0.1:52325 --> api.ipify.org:443 error: 192.220.99.3:443 connect error: REALITY authentication failed"\n\n'
                        "Log: file:///tmp/test_task-96.log\n"
                        "</SYSTEM_MESSAGE>"
                    ),
                },
                {"step_index": 102, "type": "PLANNER_RESPONSE", "status": "DONE", "content": ""},
                {"step_index": 103, "type": "GENERIC", "status": "DONE", "content": 'Task "task-90" cancelled.'},
                {
                    "step_index": 104,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "content": '<SYSTEM_MESSAGE>Task id "task-90" was canceled</SYSTEM_MESSAGE>',
                },
            ]
            tp_sub.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n", encoding="utf-8")
            stalled, harvested = _harvest_subagent_stall_state(tp_sub)
            self.assertTrue(stalled)
            self.assertIn("192.220.99.3:443 connect error: REALITY authentication failed", harvested)
            self.assertNotIn("Log: file://", harvested)
            self.assertNotIn("</SYSTEM_MESSAGE>", harvested)

            # 4) _summarize_cli_lamb_log filters raw HTTP response headers (server:, via:, alt-svc:, etc.)
            curl_log = (
                "Execution [tapnow-edm-migrate-p95sq] has successfully completed.\n"
                "[5/5] Verifying endpoint curl https://edm.tapnow.ai/t/o/check...\n"
                "HTTP/2 200 \n"
                "cache-control: no-store, no-cache, must-revalidate, max-age=0\n"
                "content-type: image/gif\n"
                "x-request-id: 1500d754-8a0f-4098-a9b1-ae2d3d5b6c12\n"
                "x-cloud-trace-context: ba2728f77b12030bb35fe6125e3c5fe9;o=1\n"
                "content-length: 43\n"
                "date: Thu, 05 Nov 2026 05:11:17 GMT\n"
                "server: Google Frontend\n"
                "via: 1.1 google\n"
                'alt-svc: h3=":443"; ma=2592000\n\n'
                "=== [2026-11-05 13:11:16] Production Rollout Succeeded ===\n"
                "SUMMARY: new_revision=tapnow-edm-00007-w5g, prev_revision=tapnow-edm-00006-2w4, curl_status=HTTP/2 200 \n"
            )
            c_sum, c_err = _summarize_cli_lamb_log(curl_log)
            self.assertFalse(c_err)
            self.assertNotIn("server: Google Frontend", c_sum)
            self.assertNotIn("via: 1.1 google", c_sum)
            self.assertNotIn("alt-svc:", c_sum)
            self.assertIn("Execution [tapnow-edm-migrate-p95sq] has successfully completed.", c_sum)
            self.assertIn("=== [2026-11-05 13:11:16] Production Rollout Succeeded ===", c_sum)
            self.assertIn("SUMMARY: new_revision=tapnow-edm-00007-w5g", c_sum)
            st.close()

    def test_reg117_attention_half_open_reprobe_and_echo_multi_requeue_and_generic_done_stall_and_tv_compliance_guard(self):
        """1.1.28（REG-117）：
        1. 回执超时（receipt_timeout）进入 attention 的会话在冷却满 10 分钟后，由 observe(self_heal=True) 与 engine._check_recovery 自动半开探测恢复并解除 Priority SLA / Queue Head-of-Line SLA 队头死锁；
        2. Owner Echo SLA 自愈支持带 15 分钟冷却的最多 3 次有界重投（不会因首次重投后偶发二次 429 超时而永久卡死）；
        3. running 批次在工具执行完毕（GENERIC DONE）后模型回合中断静默 >= 90s（batch_age >= 180s）时，observe 探针 2 与 _transcript_planner_idle_at 检出并自愈；
        4. check_ask_pre_flight_guard 覆盖测试环境 Nacos 账号预授权自治、国内 *.tapnow.tv 指向海外 GCP 的数据出境合规校验、主人要求先在群内讲清来龙去脉时抢发请示拦截、以及跨会话同一配置项（如 COMMUNITY_API_KEY）重复发卡拦截；
        5. diagnose_override_reason 精准识别上述四类批复。
        """
        from sheepdog import admin_web, observe, outbound
        from sheepdog.config import Config
        from sheepdog.models import Message
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(self_open_id="ou_owner", state_dir=state_dir)
            cfg.source.enabled = False
            st = Store(cfg.db_path)

            # 1) tp_bus stuck in attention (receipt_timeout) for 89 minutes with queued priority + normal messages
            now_dt = datetime.fromisoformat("2026-11-05T16:45:00+08:00")
            st.upsert_topic(
                "tp_bus",
                title="[managed] Lark 信号·总线",
                kind="bus",
                conversation_id="conv-bus-117",
                state="attention",
            )
            st.update_topic(
                "tp_bus",
                attention_at="2026-11-05T15:05:43+08:00",
                attention_reason="receipt_timeout",
                retries=3,
            )
            m_q1 = Message(
                message_id="om_bus_q_117",
                chat_id="oc_1",
                chat_name="群1",
                chat_type="group",
                sender_id="ou_u1",
                sender_name="Colleague",
                sender_type="user",
                content="排队的群消息",
                msg_type="text",
                create_time="2026-11-05T15:10:00+08:00",
            )
            m_prio = Message(
                message_id="om_bus_prio_117",
                chat_id="oc_1",
                chat_name="群1",
                chat_type="group",
                sender_id="ou_owner",
                sender_name="Matthew",
                sender_type="user",
                content="@Mattheuu 主人指令",
                msg_type="text",
                create_time="2026-11-05T16:40:00+08:00",
            )
            st.upsert_message(m_q1, "dispatch", "keyword", [], "tp_bus")
            st.upsert_message(m_prio, "dispatch", "owner_instruction", ["owner_instruction"], "tp_bus")
            with st.tx() as c:
                c.execute(
                    "INSERT INTO acks(message_id, chat_id, reason, identity, reaction_id, added_at) VALUES(?,?,?,?,?,?)",
                    ("om_bus_q_117", "oc_1", "keyword", "bot", "r1", "2026-11-05T15:10:02+08:00"),
                )
                c.execute(
                    "INSERT INTO acks(message_id, chat_id, reason, identity, reaction_id, added_at) VALUES(?,?,?,?,?,?)",
                    ("om_bus_prio_117", "oc_1", "owner_instruction", "bot", "r2", "2026-11-05T16:40:02+08:00"),
                )

            # 2) tp_cinemap-stress-runner has an Owner Echo that was requeued once 80 minutes ago and timed out again
            st.upsert_topic(
                "tp_cinemap-stress-runner",
                title="项目·Cinemap压测",
                kind="dynamic",
                conversation_id="conv-cinemap-117",
                state="waiting_human",
                pending_batch_id=None,
            )
            m_echo = Message(
                message_id="sd_card_cb_4bb2286f814e21b8eff44ebb5fa24fb9",
                chat_id="oc_hub",
                chat_name="拍板台",
                chat_type="group",
                sender_id="ou_owner",
                sender_name="Matthew",
                sender_type="user",
                content="[卡片回调] 其他：给他账号密码",
                msg_type="text",
                create_time="2026-11-05T14:42:48+08:00",
            )
            st.upsert_message(m_echo, "dispatch", "owner_reply", [], "tp_cinemap-stress-runner")
            with st.tx() as c:
                c.execute(
                    "UPDATE messages SET dispatch_state='missed', batch_id='b20261105152338' WHERE message_id=?",
                    ("sd_card_cb_4bb2286f814e21b8eff44ebb5fa24fb9",),
                )
                c.execute(
                    "INSERT INTO owner_echoes(message_id, chat_id, topic_id, kind, stage, said_at, recorded_at, delivered_at, stage_at, batch_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "sd_card_cb_4bb2286f814e21b8eff44ebb5fa24fb9",
                        "oc_hub",
                        "tp_cinemap-stress-runner",
                        "owner_reply",
                        "delivered",
                        "2026-11-05T14:42:48+08:00",
                        "2026-11-05T14:42:48+08:00",
                        "2026-11-05T15:23:38+08:00",
                        "2026-11-05T15:23:38+08:00",
                        "b20261105152338",
                    ),
                )
            st.set_meta("owner_echo_requeued:sd_card_cb_4bb2286f814e21b8eff44ebb5fa24fb9", "2026-11-05T15:23:34+08:00")

            # 3) Running session stalled on GENERIC DONE for > 90s (batch_age >= 180s)
            cid_stall = "conv-generic-stall-117"
            t_stall = brain_dir / cid_stall / ".system_generated" / "logs" / "transcript.jsonl"
            t_stall.parent.mkdir(parents=True, exist_ok=True)
            st.upsert_topic(
                "tp_generic-stall",
                title="排查·工具完成后429中断",
                kind="dynamic",
                conversation_id=cid_stall,
                state="running",
                pending_batch_id="b20261105164000-generic",
                dispatched_at="2026-11-05T16:40:00+08:00",
            )
            t_stall.write_text(
                "\n".join(
                    json.dumps(x, ensure_ascii=False)
                    for x in [
                        {
                            "step_index": 10,
                            "type": "SYSTEM_MESSAGE",
                            "status": "DONE",
                            "created_at": "2026-11-05T08:40:01Z",
                            "content": "[sheepdog] 新信号批次 `b20261105164000-generic`",
                        },
                        {
                            "step_index": 11,
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "created_at": "2026-11-05T08:40:05Z",
                            "tool_calls": [{"name": "run_command"}],
                        },
                        {
                            "step_index": 12,
                            "type": "GENERIC",
                            "status": "DONE",
                            "created_at": "2026-11-05T08:40:10Z",
                            "content": "Command exited with code 0.",
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            obs = observe.run_observe(
                cfg,
                db_path=cfg.db_path,
                now_dt=now_dt,
                antigravity_brain_dir=brain_dir,
                check_processes=False,
                check_log=False,
                self_heal=True,
            )
            self.assertIn("tp_bus:attention_timeout_auto_reprobe", obs["self_healed"])
            self.assertIn("tp_cinemap-stress-runner:owner_echo_requeue", obs["self_healed"])
            self.assertIn("tp_generic-stall:b20261105164000-generic", obs["self_healed"])
            self.assertFalse(any("tp_bus" in w for w in obs["warns"]), obs["warns"])
            self.assertFalse(any("Owner Echo SLA" in w for w in obs["warns"]), obs["warns"])
            self.assertEqual(st.get_topic("tp_bus")["state"], "active")
            self.assertEqual(st.get_meta("owner_echo_requeue_count:sd_card_cb_4bb2286f814e21b8eff44ebb5fa24fb9"), "2")

            # 4a) Testing-env Nacos account ask -> blocked (pre-authorized)
            with self.assertRaises(outbound.OutboundError) as cm_nacos:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_cinemap-stress-runner",
                    "Glenn申请测试Nacos专用账号供Loops自动化配置",
                    "事情：Glenn 的 Agent 为自动化流程申请在测试环境（tap-testing-env）创建专用 Nacos 账号 loops-model-onboard-test。\n"
                    "选项：\nA. 批准在测试环境 Nacos 创建受限账号\nB. 不创建专用账号\n建议：选 A",
                )
            self.assertIn("REG-117", str(cm_nacos.exception))

            # 4b) Domestic *.tapnow.tv pointing to overseas GCP proxy without cross-border compliance -> blocked
            with self.assertRaises(outbound.OutboundError) as cm_tv:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_felix-fang",
                    "配置 track.tapnow.tv 广告追踪域名解析与 GCP 代理证书",
                    "事情：Felix 申请接入 track.tapnow.tv 承接广告投放回传。需在 DNSPod 为 tapnow.tv 新增 track A 记录指向 8.233.219.66，"
                    "并在 GCP 生产环境（taplabs-453508）为 tapnow-ga4-sgtm-https-proxy 追加证书。\n"
                    "选项：\nA. 同意配置 DNSPod A 记录与 GCP 生产证书\nB. 暂缓配置\n建议：选 A",
                )
            self.assertIn("REG-117", str(cm_tv.exception))
            self.assertIn("数据出境", str(cm_tv.exception))

            # 4c) Premature ask for COMMUNITY_API_KEY dual-key compatibility before explaining root-cause timeline in group -> blocked
            with self.assertRaises(outbound.OutboundError) as cm_hist:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_google-youtube-cua-gemini-quota",
                    "社区COMMUNITY_API_KEY双Key兼容恢复",
                    "事情：Yuyang 反馈下游写死依赖原 25 位 COMMUNITY_API_KEY，今日凌晨发版单 202611040051 硬切为 48 位新 Key 导致下游报 401。\n"
                    "选项：\nA. 实施生产鉴权双 Key 兼容\nB. 维持 48 位新 Key\n建议：选 A",
                )
            self.assertIn("REG-117", str(cm_hist.exception))
            self.assertIn("来龙去脉", str(cm_hist.exception))

            # 4d) Cross-session duplicate ask on COMMUNITY_API_KEY -> blocked
            with st.tx() as c:
                c.execute(
                    "INSERT INTO sends(idem_key, kind, topic_id, message_id, key, norm_key, text, sent_at, title) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "sda-hist-117",
                        "ask",
                        "tp_google-youtube-cua-gemini-quota",
                        "om_ask_117_first",
                        "community-api-key-compatibility",
                        "COMMUNITY-API-KEY-COMPATIBILITY",
                        "社区 COMMUNITY_API_KEY 双 Key 兼容恢复",
                        "2026-11-05T16:32:17+08:00",
                        "社区COMMUNITY_API_KEY双Key兼容恢复",
                    ),
                )
            with self.assertRaises(outbound.OutboundError) as cm_dup:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_mcp-ops",
                    "生产Nacos社区APIKey核查",
                    "事情：Dashell 反馈生产环境用 X-API-Key 调用社区接口报 401，需确认生产 Nacos 的 COMMUNITY_API_KEY 是否被改动。\n"
                    "选项：\nA. 核查生产 Nacos 确认线上 COMMUNITY_API_KEY 是否被改动\nB. 暂缓处理\n建议：选 A",
                    key="COMMUNITY_API_KEY",
                )
            self.assertIn("REG-117", str(cm_dup.exception))
            self.assertIn("tp_google-youtube-cua-gemini-quota", str(cm_dup.exception))

            # 5) diagnose_override_reason for all 4 replies
            self.assertEqual(
                admin_web.diagnose_override_reason("配置 track.tapnow.tv", "这个有没有合规问题 要确认一下，原则上我理解数据是不能出境的 得看是不是要不是再腾讯云国际这边", "其他", "A")[0],
                "arch_or_config_posture",
            )
            self.assertEqual(
                admin_web.diagnose_override_reason("社区COMMUNITY_API_KEY双Key兼容恢复", "还不能动 把来龙去脉在群里讲清楚先", "其他", "A")[0],
                "context_incomplete",
            )
            self.assertEqual(
                admin_web.diagnose_override_reason("Glenn申请测试Nacos专用账号", "给他账号密码", "其他", "A")[0],
                "context_incomplete",
            )
            self.assertEqual(
                admin_web.diagnose_override_reason("生产Nacos社区APIKey核查", "这个已经在google youtube群里在讨论 这和yuyang讨论的事情是同一个 统一转交给同一个session处理", "其他", "A")[0],
                "peer_or_owner_delegate",
            )
            self.assertEqual(
                admin_web.diagnose_override_reason("授权开通国内广告回传域名track.tapnow.tv", "等澄清上下文把 目前暂时观望", "其他", "A")[0],
                "context_incomplete",
            )
            st.close()

    def test_reg118_lsg14_solo_vip_guard_and_same_topic_correction_ask_in_place_patch_and_override_diagnosis(self):
        """1.1.29（REG-118）：
        1. check_ask_pre_flight_guard 拦截将 VIP 独占锁定节点 lsg14（192.220.48.237）当作尚有空位/共用席位提议分配的 ask，放行已核实为 lsg4 空位的 ask；
        2. decision_hub.try_patch_open_ask 与 send_to_owner 在同一会话对同一主题补发『实据更正』拍板卡（即使更换了 --key 且 origin='batch' 无显式 --about）时，原地 PATCH 更新首张未决卡，不新刷第 2 张冲突卡；且 sync_escalation_dual_cards 拍板任一卡时自动结案同会话同主题的重复未决卡；
        3. diagnose_override_reason 将『我去新买一个vps吧』归入 arch_or_config_posture（技术姿势纠偏）。
        """
        from sheepdog import admin_web, decision_hub, outbound
        from sheepdog.config import Config
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sheepdog.sqlite3"
            st = Store(db)
            cfg = Config(state_dir=Path(tmp))
            cfg.self_open_id = "ou_test_owner_118"
            cfg.topic_group.enabled = True
            cfg.topic_group.chat_id = "oc_test_tg_118"
            cfg.topic_group.decision_hub = True

            # 1) Proposing lsg14 as having available seats -> blocked
            with self.assertRaises(outbound.OutboundError) as cm_lsg14:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_sherwin",
                    "为Carl开通专属网络",
                    "事情：Sherwin 提出为 Carl 开通一个他的专属网络。当前 lsg14 尚余 1 个专属席位（目前仅 Felix 与 Sammy 共用，空余 1 席）。\n"
                    "选项：\n"
                    "A. 在 lsg14（原生双 ISP 高质专线，空余 1 席）开通专属席位，立即可用无需新购\n"
                    "B. 新购一台专属 VPS 节点，做完全独占单人专线\n"
                    "建议：选 A",
                    key="carl-dedicated-vpn",
                )
            self.assertIn("REG-118", str(cm_lsg14.exception))
            self.assertIn("lsg14", str(cm_lsg14.exception))

            # Proposing verified lsg4 while noting lsg14 is locked -> allowed
            corrected_body = (
                "经 tp_3x-ui-ops 核实：lsg14 已锁定为 Sammy 独占不可分配，仅 lsg4 余 1 个空位。\n"
                "A. 将 Carl 分配至 lsg4 唯一空位（建议）\n"
                "B. 新购一台 VPS 节点"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_sherwin",
                "为Carl开通专属网络(实据更正)",
                corrected_body,
                key="T-127",
            )

            # 2) Same-topic correction ask with changed key ("carl-dedicated-vpn" -> "T-127") and batch origin patches in place
            st.upsert_topic("tp_sherwin", title="Sherwin", kind="dynamic", state="active", conversation_id="cid_sherwin_118", host="antigravity")
            first_text = outbound.compose(cfg, outbound.ASK, "tp_sherwin", "为Carl开通专属网络", "初版内容\n选项：\nA. 方案A\nB. 方案B\n建议：选 A")
            with st.tx() as c:
                c.execute(
                    "INSERT INTO sends(idem_key, kind, topic_id, message_id, root_message_id, key, norm_key, text, sent_at, title) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "sda-118-first",
                        "ask",
                        "tp_sherwin",
                        "om_ask_118_first",
                        "om_root_118",
                        "carl-dedicated-vpn",
                        "CARL-DEDICATED-VPN",
                        first_text,
                        "2026-11-05T17:15:29+08:00",
                        "为Carl开通专属网络",
                    ),
                )
            st.add_escalation("om_ask_118_first", "tp_sherwin", "oc_test_tg_118", first_text, "2026-11-05T17:15:29+08:00")

            patched_Calls = []

            class _FakeSource:
                def patch_card(self, mid, card, identity="bot"):
                    patched_Calls.append(mid)

            res_corr = outbound.send_to_owner(
                cfg,
                st,
                _FakeSource(),
                outbound.ASK,
                "tp_sherwin",
                "为Carl开通专属网络(实据更正)",
                corrected_body,
                key="T-127",
            )
            self.assertTrue(res_corr.patched_in_place)
            self.assertEqual(res_corr.message_id, "om_ask_118_first")
            self.assertIn("lsg4 唯一空位", st.get_escalation("om_ask_118_first")["text"])
            self.assertIn("om_ask_118_first", patched_Calls)

            # If a legacy duplicate open escalation on the same topic/subject coexists, answering one auto-resolves the sibling
            st.add_escalation(
                "om_ask_118_dup",
                "tp_sherwin",
                "oc_test_tg_118",
                "🐕 需要你定 · 为Carl开通专属网络 [tp_sherwin]\n旧卡",
                "2026-11-05T17:15:00+08:00",
            )
            decision_hub.sync_escalation_dual_cards(
                cfg,
                st,
                _FakeSource(),
                "om_ask_118_first",
                chosen_display="A：将 Carl 分配至 lsg4 唯一空位",
                topic_id="tp_sherwin",
                answer_mid="sd_card_cb_118_ans",
            )
            dup_row = st.get_escalation("om_ask_118_dup")
            self.assertEqual(dup_row["close_reason"], "resolved")
            self.assertIsNotNone(dup_row["closed_at"])

            # 3) diagnose_override_reason classifies "我去新买一个vps吧" under arch_or_config_posture
            code_vps, label_vps = admin_web.diagnose_override_reason(
                "为Carl开通专属网络",
                "我去新买一个vps吧",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual(code_vps, "arch_or_config_posture")
            self.assertEqual(label_vps, "技术姿势纠偏")
            st.close()

    def test_reg119_internal_dev_repo_proxy_edit_guard_and_cnb_direct_push_guard_and_override_diagnosis(self):
        """REG-119 (v1.1.30):
        1. Internal developer GitHub repo permission ask recommending '不开仓库权限，由我们克隆...代改' is blocked; recommending granting repo access passes.
        2. Direct local `git push cnb` / `cnb.build` in `lamb prepare`, `lamb spawn`, or `sheepdog ask` is blocked; GitHub Actions `sync-to-cnb.yml` flow passes.
        3. `diagnose_override_reason` classifies overriding '不开仓库权限，由我们克隆代改' (B) with '开通...仓库写权限，由他自行修改提 PR' (A) under `arch_or_config_posture`.
        """
        from types import SimpleNamespace
        from sheepdog import admin_web, cli, outbound
        from sheepdog.config import Config
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(state_dir=root)
            st = Store(root / "sheepdog.sqlite3")
            st.upsert_topic(
                "tp_arena-release-guard",
                title="Arena发版守卫",
                kind="dynamic",
                state="active",
                conversation_id="cid_arena_119",
                host="antigravity",
            )

            # 1) Recommending B (不开仓库权限，由我们克隆...代改) -> blocked by REG-119
            bad_repo_ask_body = (
                "事情：Jerry Chu（File Hub #201 后端作者）反馈 material-generation-service 上传生成结果需把 x-role 改为 agent，申请仓库权限或由我们代改。\n"
                "选项：\n"
                "A. 同意为 Jerry 的 GitHub 账号（Jerry030-1）开通 Tamar-Edge-AI/material-generation-service 仓库写权限，由他自行修改提 PR\n"
                "B. 不开仓库权限，由我们克隆 material-generation-service 仓库将上传请求头 x-role 改为 agent 并提 PR 送测发版\n"
                "建议：选 B。改动极其简单，由我们直接修改提 PR 发版推进更高效，无需扩散仓库权限。"
            )
            with self.assertRaises(outbound.OutboundError) as cm_repo:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_arena-release-guard",
                    "Jerry申请material-generation仓权限或代改x-role",
                    bad_repo_ask_body,
                    key="jerry-material-generation-perm",
                )
            self.assertIn("REG-119", str(cm_repo.exception))

            # Recommending A (同意为研发同学开通仓库写权限，由他自行修改提 PR) -> passes
            good_repo_ask_body = (
                "事情：Jerry Chu（File Hub #201 后端作者）反馈 material-generation-service 上传生成结果需把 x-role 改为 agent，申请仓库写权限提 PR。\n"
                "选项：\n"
                "A. 同意为 Jerry 的 GitHub 账号（Jerry030-1）开通 Tamar-Edge-AI/material-generation-service 仓库写权限，由他自行修改提 PR\n"
                "B. 不开仓库权限，由我们克隆 material-generation-service 仓库将上传请求头 x-role 改为 agent 并提 PR 送测发版\n"
                "建议：选 A。遵循工程研发权责，由代码作者提 PR 走标准评审与发布流。"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_arena-release-guard",
                "Jerry申请material-generation仓权限",
                good_repo_ask_body,
                key="jerry-material-generation-perm",
            )

            # 2) Direct git push to cnb.build in lamb prepare / lamb spawn / ask -> blocked by REG-119
            bad_cnb_spec = (
                "4. 推送代码至 CNB：\n"
                "   - 添加 cnb remote: https://cnb.build/Tamar-Edge-AI/cinemap_3D_world.git\n"
                "   - 推送分支 claude/cm-455-china-on-prod 至 CNB\n"
            )
            rc_prep = cli._lamb_prepare(
                cfg,
                st,
                SimpleNamespace(
                    topic="tp_arena-release-guard",
                    title="CM-584 CNB Git Push & Image Build Lamb",
                    spec=bad_cnb_spec,
                    spec_file=None,
                    spec_stdin=False,
                    kind="subagent",
                    cid=None,
                    lamb_id="lamb_test119a",
                    json=True,
                ),
            )
            self.assertEqual(rc_prep, 2)
            self.assertIsNone(st.get_lamb("lamb_test119a"))

            with self.assertRaises(outbound.OutboundError) as cm_cnb_cmd:
                outbound.check_no_direct_cnb_git_push_guard(
                    "cd /tmp/repo_test_cinemap && git push cnb claude/cm-455-china-on-prod",
                    scene="lamb spawn",
                )
            self.assertIn("REG-119", str(cm_cnb_cmd.exception))

            # Compliant GitHub -> sync-to-cnb.yml spec -> passes
            good_cnb_spec = (
                "4. 同步代码至 CNB 镜像仓：\n"
                "   - 提交和 push 统一走 GitHub 仓库，由 GitHub Actions (sync-to-cnb.yml) 自动同步到 cnb.build/Tamar-Edge-AI/cinemap_3D_world.git，严禁本机直接 git push cnb\n"
            )
            outbound.check_no_direct_cnb_git_push_guard(good_cnb_spec, scene="lamb prepare")

            # 3) diagnose_override_reason classifies overriding B (proxy edit) with A (grant repo access, even when callback label is truncated at 60 chars) under arch_or_config_posture
            code_repo, label_repo = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · Jerry申请material-generation仓权限或代改x-role [tp_arena-release-guard]\n{bad_repo_ask_body}",
                "[卡片回调] A：同意为 Jerry 的 GitHub 账号（Jerry030-1）开通 Tamar-Edge-AI/material-generation-service 仓库",
                chosen_code="A",
                recommended="B",
            )
            self.assertEqual(code_repo, "arch_or_config_posture")
            self.assertEqual(label_repo, "技术姿势纠偏")
            st.close()

    def test_reg120_vip_high_perm_e2e_ask_guard_and_unawakened_sysmsg_and_prebatch_lamb_unlock(self):
        """REG-120 (1.1.31):
        1) When VIP (Lao Hu / Sammy) requests cloud subaccount / CAM / TKE kubeconfig permissions, check_ask_pre_flight_guard
           (even when floor_type='credential' is passed) rejects asks that recommend 'A. 不外发生产云 AK/SK 与集群 kubeconfig，由专席直接代建...仅交付只读 SDK Key'
           or only offer single-namespace restricted permissions; recommending high-permission end-to-end autonomy passes;
           and diagnose_override_reason classifies 'laohu 的权限要高 你要让他尽可能端到端完成' as ('arch_or_config_posture', '技术姿势纠偏').
        2) In Engine._check_stopped_in_flight, when an Antigravity batch lands as SYSTEM_MESSAGE DONE with zero PLANNER_RESPONSE
           for >= 60s on real agentapi, it triggers auto-resume immediately instead of dropping through if/elif branches.
        3) In duty.fresh_active_lambs(..., min_started_at=sent), a background lamb spawned in an earlier batch (started_at < sent - 5s)
           does NOT block _check_stopped_in_flight or _check_receipt for a newly dispatched batch, while a lamb spawned in the
           current batch (started_at >= sent - 5s) continues to pause receipt timeout.
        """
        from datetime import timezone
        from sheepdog import admin_web, config, duty, engine, outbound, session as sm, store

        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp)
            cfg = config.Config(state_dir=tpath)
            st = store.Store(cfg.db_path)

            # 1) VIP low-permission / proxy deployment recommendation rejected even with floor_type="credential"
            bad_vip_ask_body = (
                "因外发生产云账号 AK/SK 与生产 TKE 集群管理员 kubeconfig 触及凭据外发硬底线，特请示办理方式（建议选 A）：\n"
                "A. 不外发生产云 AK/SK 与集群 kubeconfig，由 tp_laohu 直接在 200053747873 为其开通 TencentDB PG 新库 growthbook、在 cls-gnui5plk 部署 GrowthBook，仅交付后台登录账号与只读 SDK Key\n"
                "B. 为老胡创建 200053747873 专用受限 CAM 子账号（仅限新建 DB 与 cls-gnui5plk 下 growthbook 单命名空间权限）并签发受限 kubeconfig 供其部署\n"
                "C. 暂缓在生产集群部署 GrowthBook，先在 Stage 环境 (200053747755) 部署验证\n"
                "建议：选 A"
            )
            with self.assertRaises(outbound.OutboundError) as cm_vip:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_laohu",
                    "老胡申请腾讯云国际 Prod 子账号 (200053747873) 权限与南京 TKE kubeconfig 部署 GrowthBook",
                    bad_vip_ask_body,
                    key="fe-1864-growthbook-prod-credentials",
                    floor="外发生产云账号 AK/SK 与生产集群 kubeconfig 凭据",
                    floor_type="credential",
                )
            self.assertIn("REG-120", str(cm_vip.exception))

            good_vip_ask_body = (
                "因外发生产云账号 AK/SK 与生产 TKE 集群 kubeconfig 触及凭据外发硬底线，特请示办理方式（建议选 A）：\n"
                "A. 为老胡开通 200053747873 高权限 CAM 子账号（AdministratorAccess）并签发 cls-gnui5plk 完整管理员 kubeconfig，支持其端到端自主完成 GrowthBook 部署与验证\n"
                "B. 由专席代为部署 GrowthBook 并仅交付后台账号\n"
                "建议：选 A"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_laohu",
                "老胡申请腾讯云国际 Prod 子账号 (200053747873) 权限与南京 TKE kubeconfig 部署 GrowthBook",
                good_vip_ask_body,
                key="fe-1864-growthbook-prod-credentials",
                floor="外发生产云账号 AK/SK 与生产集群 kubeconfig 凭据",
                floor_type="credential",
            )

            code_vip, label_vip = admin_web.diagnose_override_reason(
                f"🐕 需要你定 · 老胡申请腾讯云国际 Prod 子账号权限 [tp_laohu]\n{bad_vip_ask_body}",
                "laohu 的权限要高  你要让他尽可能端到端完成",
            )
            self.assertEqual(code_vip, "arch_or_config_posture")
            self.assertEqual(label_vip, "技术姿势纠偏")

            # 2) Unawakened SYSTEM_MESSAGE DONE after >= 60s + pre-batch background lamb does not block auto-resume
            now_dt = datetime.now().astimezone()
            sent_dt = now_dt - timedelta(seconds=95)
            old_lamb_dt = now_dt - timedelta(minutes=45)
            cur_lamb_dt = now_dt - timedelta(seconds=30)

            st.create_topic("tp_kaifeng", "Kai Feng", "adopted", "duty", sm.RUNNING, "antigravity")
            st.update_topic(
                "tp_kaifeng",
                conversation_id="cid-kaifeng-120",
                pending_batch_id="b20261105194335-kaifeng",
                dispatched_at=sent_dt.isoformat(timespec="seconds"),
            )
            # Pre-batch lamb started 45 minutes ago (before sent_dt)
            st.create_lamb("lamb_old120", "tp_kaifeng", "subagent", "Old CNB Upload", conversation_id="cid-sub-old120")
            st.update_lamb(
                "lamb_old120",
                state="running",
                started_at=old_lamb_dt.isoformat(timespec="seconds"),
                last_act_at=now_dt.isoformat(timespec="seconds"),
            )
            # Without min_started_at, fresh_active_lambs includes lamb_old120; with min_started_at=sent_dt, it excludes lamb_old120
            self.assertEqual(len(duty.fresh_active_lambs(st, "tp_kaifeng", now_dt, 20)), 1)
            self.assertEqual(len(duty.fresh_active_lambs(st, "tp_kaifeng", now_dt, 20, min_started_at=sent_dt)), 0)

            # Write transcript with SYSTEM_MESSAGE DONE for b20261105194335-kaifeng and zero PLANNER_RESPONSE
            rec_file = tpath / "transcript_kaifeng.jsonl"
            rec_file.write_text(
                json.dumps(
                    {
                        "step_index": 10,
                        "type": "SYSTEM_MESSAGE",
                        "status": "DONE",
                        "created_at": sent_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "content": "批次 `b20261105194335-kaifeng` 请处理并交回执",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            sent_prompts = []

            class DummyAgentApiSink:
                name = "agentapi"

            eng = engine.Dispatcher(cfg, st, {"antigravity": DummyAgentApiSink()})
            eng._record_file = lambda t_row: rec_file
            eng._send = lambda t_row, prompt, batch: sent_prompts.append((t_row["topic_id"], batch, prompt))

            t_row = st.get_topic("tp_kaifeng")
            resumed = eng._check_stopped_in_flight(t_row, "b20261105194335-kaifeng", sent_dt, now_dt, True)
            self.assertTrue(resumed)
            self.assertEqual(len(sent_prompts), 1)
            self.assertIn("b20261105194335-kaifeng", sent_prompts[0][2])

            # 3) If a lamb was spawned in the CURRENT batch (started_at >= sent_dt - 5s), fresh_active_lambs blocks _check_stopped_in_flight
            st.create_lamb("lamb_cur120", "tp_kaifeng", "subagent", "Current Batch Task", conversation_id="cid-sub-cur120")
            st.update_lamb(
                "lamb_cur120",
                state="running",
                started_at=cur_lamb_dt.isoformat(timespec="seconds"),
                last_act_at=now_dt.isoformat(timespec="seconds"),
            )
            self.assertEqual(len(duty.fresh_active_lambs(st, "tp_kaifeng", now_dt, 20, min_started_at=sent_dt)), 1)
            self.assertFalse(eng._check_stopped_in_flight(t_row, "b20261105194335-kaifeng", sent_dt, now_dt, True))
            st.close()

    def test_reg121_class_wide_api_cache_guard_stage_self_access_guard_and_superseded_subagent_auto_close(self) -> None:
        """REG-121：
        1. 针对 EdgeOne / CDN API 缓存或 CORS 串域发起请示时，若仅将单个 API 子路径（如 `/api/bff/app-store/*`）设为 NoCache 而不排查全站 `/api/*`，当场拦截；
        2. 内部同事申请在 Stage 机器运行评测脚本时，若仅提供『由专席在 Stage 机器代跑』而不开通 Stage 自助权限，当场拦截；且两类人类改判均归入 `arch_or_config_posture`；
        3. 母羊在新批次交回 `waiting` 回执并显式声明新 `lamb:<id>` 锚点时，自动将本批次前启动且未列入新锚点的旧在途 subagent 收口为 `done`。
        """
        from datetime import datetime, timedelta
        from sheepdog import admin_web, config, engine, outbound, session as sm, store

        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            cfg = config.Config(state_dir=tpath, self_open_id="ou_owner")
            st = store.Store(cfg.db_path)

            # 1) Single API subpath cache patch ask is blocked without class-wide /api/* audit
            bad_cache_ask = (
                "Andy Xiao 提请修复 ENG-1112 应用市场 CORS 串域，需在生产 EdgeOne 将 /api/bff/app-store/* 设为 NoCache 并刷新缓存，请审批选项 A。\n"
                "---more---\n"
                "事情：Andy Xiao 提请修复 ENG-1112（tv 应用市场跨域报错，因 EdgeOne 缓存未区分 Origin 导致 app 与 embed 域名命中同一条缓存被 CORS 拦截），"
                "需在生产 EdgeOne 将 /api/bff/app-store/* 设为不缓存并刷新该目录缓存。\n\n"
                "选项：\n"
                "A. 授权在生产 EdgeOne 站点配置 /api/bff/app-store/* 为 NoCache 并刷新该路径缓存，并协同 agent-first 专席推进 BFF 发版\n"
                "B. 暂不修改 EdgeOne 规则，仅推进 BFF 镜像升级"
            )
            with self.assertRaises(outbound.OutboundError) as cm_cache:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_infra-migration-ops",
                    "ENG-1112 EdgeOne配置与BFF发版授权",
                    bad_cache_ask,
                    key="eng-1112-edgeone-nocache",
                    floor="修改生产 EdgeOne 缓存规则",
                    floor_type="prod",
                )
            self.assertIn("REG-121", str(cm_cache.exception))

            good_cache_ask = (
                "已核查全站 API 缓存规则，提请将生产 EdgeOne 全量 /api/* 动态接口统一设为 NoCache 并刷新缓存，请审批选项 A。\n"
                "---more---\n"
                "事情：排查该站点下其他 API 接口缓存情况后，确认需将所有 /api/* 路径统一设为 NoCache。\n"
                "选项：\n"
                "A. 授权在生产 EdgeOne 站点统一将全量 /api/* 配置为 NoCache 并刷新缓存\n"
                "B. 暂不修改 EdgeOne 规则"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_infra-migration-ops",
                "ENG-1112 EdgeOne 全站 API 缓存治理授权",
                good_cache_ask,
                key="eng-1112-edgeone-nocache",
                floor="修改生产 EdgeOne 缓存规则",
                floor_type="prod",
            )

            # 2) Stage machine proxy-run ask is blocked without direct self-access option
            bad_stage_ask = (
                "Elaine 需在 Stage 机器执行约 5 分钟的首页推荐离线 bench 脚本，请拍板是否由专席在 Stage 代跑并返回结果。\n"
                "---more---\n"
                "Elaine 正在验证首页推荐离线评测脚本，因本机无法直连，申请在 Stage 环境机器（ins-mu1sqp1i）运行。\n\n"
                "选项：\n"
                "A. 同意在 Stage 机器代跑该 bench，将无凭据的耗时与推荐结果发给 Elaine\n"
                "B. 暂不在 Stage 机器跑离线脚本，请 Elaine 改走其他方式"
            )
            with self.assertRaises(outbound.OutboundError) as cm_stage:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_tp_elaine",
                    "Elaine申请在Stage机器跑推荐离线bench",
                    bad_stage_ask,
                    key="elaine-stage-bench",
                )
            self.assertIn("REG-121", str(cm_stage.exception))

            good_stage_ask = (
                "Elaine 申请在 Stage 机器（ins-mu1sqp1i）运行首页推荐离线 bench 脚本，建议直接为其开通 Stage 机器登录权限让其自己运行。\n"
                "---more---\n"
                "选项：\n"
                "A. 直接给其开通 Stage 机器 SSH 登录权限，让其自己上去跑 bench 脚本\n"
                "B. 由专席在 Stage 机器代跑该 bench"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_tp_elaine",
                "Elaine申请Stage机器权限跑推荐离线bench",
                good_stage_ask,
                key="elaine-stage-bench",
            )

            c1, l1 = admin_web.diagnose_override_reason(
                bad_cache_ask,
                "要修复就修复这一类的东西 我理解可能会存在其他api接口缓存的情况",
            )
            self.assertEqual((c1, l1), ("arch_or_config_posture", "技术姿势纠偏"))
            c2, l2 = admin_web.diagnose_override_reason(
                bad_stage_ask,
                "stage你就直接给他权限让他自己弄呗",
            )
            self.assertEqual((c2, l2), ("arch_or_config_posture", "技术姿势纠偏"))

            # 2b) Domain owner autonomous release/rollback withholding EdgeOne/DNSPod & binary full-account Admin guards
            bad_domain_e2e_ask = (
                "Kai申请明天白天为其Stage子账号配置Prod跨账号角色(强制MFA)+Cinemap最小权限以自主发版回滚。建议选A。\n"
                "---more---\n"
                "事情：授予 Cinemap 域内最小权限以自主完成构建、发布与回滚：\n"
                "选项：\n"
                "A. 同意按最小权限原则开通：配置 Prod 跨账号 CAM 角色、cinemapprod 单命名空间 kubeconfig、COS/TCR/CNB 权限（DNSPod 等不支持单记录隔离的项给只读或走受控变更）\n"
                "B. 全部按申请开通（含 EdgeOne/DNSPod 站点权限）\n"
                "建议：A"
            )
            with self.assertRaises(outbound.OutboundError) as cm_domain_e2e:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_kaifeng",
                    "Kai申请Cinemap生产最小权限跨账号角色与受限kubeconfig",
                    bad_domain_e2e_ask,
                    key="kai-cinemap-prod-perm",
                )
            self.assertIn("REG-121", str(cm_domain_e2e.exception))

            bad_binary_admin_ask = (
                "Kai追问能否直接给南京生产腾讯云账号全量Admin权限。因该账号与TapNow国内主站生产共号，建议维持Cinemap域内最小权限（选项A），或按选项B开通全量Admin。\n"
                "---more---\n"
                "事情：Kai 追问「然后要不直接给全量的 admin 权限」（指南京生产腾讯云账号 `1506862273` 的全量 `AdministratorAccess`）。\n"
                "选项：\n"
                "A. 不开全量 Admin：维持已批准的 Cinemap 生产最小权限跨账号角色 + `cinemapprod` 命名空间编辑权限\n"
                "B. 同意升级：直接给 Kai 的跨账号角色（强制 MFA）挂载生产账号全量 `AdministratorAccess`（含 TKE `cluster-admin`）\n"
                "建议：A"
            )
            with self.assertRaises(outbound.OutboundError) as cm_bin_admin:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_kaifeng",
                    "Kai追问是否直接给生产账号全量Admin权限",
                    bad_binary_admin_ask,
                    key="kai-full-admin",
                )
            self.assertIn("REG-121", str(cm_bin_admin.exception))

            c3, l3 = admin_web.diagnose_override_reason(
                bad_domain_e2e_ask,
                "全部按申请开通（含 EdgeOne/DNSPod 站点权限）",
                chosen_code="B",
                recommended="A",
            )
            self.assertEqual((c3, l3), ("arch_or_config_posture", "技术姿势纠偏"))
            c4, l4 = admin_web.diagnose_override_reason(
                bad_binary_admin_ask,
                "你可以问一下他一般资源范围 一般都做什么，然后我们先可以把，比如说腾讯云国际这一边有没有什么比较经常需要开发产研相关改动的东西，可以给他 admin权限。但是你说网络这一些就不行。",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual((c4, l4), ("arch_or_config_posture", "技术姿势纠偏"))

            # 3) Superseded pre-batch subagent is auto-closed when write_receipt receives waiting + new lamb anchors
            now_dt = datetime.now().astimezone()
            old_start = (now_dt - timedelta(minutes=30)).isoformat(timespec="seconds")
            batch_sent = (now_dt - timedelta(minutes=2)).isoformat(timespec="seconds")
            new_start = (now_dt - timedelta(minutes=1)).isoformat(timespec="seconds")

            st.create_topic("tp_kaifeng", "Kai Feng", "adopted", "duty", sm.RUNNING, "antigravity")
            st.update_topic(
                "tp_kaifeng",
                pending_batch_id="b20261105205547-kaifeng",
                dispatched_at=batch_sent,
            )
            st.record_dispatch("b20261105205547-kaifeng", "tp_kaifeng", ["m1"])
            st.conn.execute(
                "UPDATE dispatches SET sent_at = ? WHERE batch_id = ?",
                (batch_sent, "b20261105205547-kaifeng"),
            )
            st.conn.commit()

            st.create_lamb("lamb_old_superseded", "tp_kaifeng", "subagent", "Old Git Push", conversation_id="cid-old")
            st.update_lamb("lamb_old_superseded", state="running", started_at=old_start, last_act_at=old_start)

            st.create_lamb("lamb_new_active", "tp_kaifeng", "subagent", "New Build Watch", conversation_id="cid-new")
            st.update_lamb("lamb_new_active", state="running", started_at=new_start, last_act_at=new_start)
            st.close()

            engine.write_receipt(
                cfg,
                "tp_kaifeng",
                "b20261105205547-kaifeng",
                {
                    "status": "waiting",
                    "summary": "已切换等待新羔羊",
                    "anchors": ["project:cinemap", "lamb:lamb_new_active"],
                },
            )

            st2 = store.Store(cfg.db_path)
            old_row = st2.get_lamb("lamb_old_superseded")
            new_row = st2.get_lamb("lamb_new_active")
            self.assertEqual(old_row["state"], "done")
            self.assertEqual(old_row["report_msg_id"], "superseded_by_new_lamb_anchors")
            self.assertEqual(new_row["state"], "running")
            st2.close()

    def test_reg122_legacy_temp_domain_offline_guard_override_diagnosis_and_lamb_utc_z_timestamp_norm(self) -> None:
        """REG-122 (1.1.33):
        1) check_ask_pre_flight_guard blocks retiring a temporary/joint-debugging domain (联调域名 / 过渡域名)
           when Option A recommends 302 redirecting to keep it alive instead of directly disabling/decommissioning it;
        2) admin_web.diagnose_override_reason classifies overriding Option A (302 redirect) to Option B (停用并下线)
           as ('arch_or_config_posture', '技术姿势纠偏');
        3) store.create_lamb, store.update_lamb, and Store._migrate normalize UTC 'Z' ISO timestamps to local
           timezone (+08:00), ensuring SQLite string ordering and evolve parent-recovery comparison work accurately.
        """
        from datetime import datetime, timedelta, timezone
        from sheepdog import admin_web, config, evolve, outbound, store

        with tempfile.TemporaryDirectory() as td:
            cfg = config.Config(state_dir=Path(td), self_open_id="ou_owner")
            st = store.Store(cfg.db_path)

            bad_temp_domain_ask = (
                "腾讯云国际 Prod（200053747873）正式站点下，落后版本的联调域名 app-cn.tapnow.tv 仍在线并指向集群内旧容器前端。"
                "请拍板将该域名 302 跳转到正式 app.tapnow.tv 还是直接下线。\n"
                "---more---\n"
                "事情：Linear `ENG-1061`：国际 Prod 子账号（`200053747873`）正式 EdgeOne（`zone-3wzk608w22ku`）中，"
                "正式域名 `app.tapnow.tv` 已走最新 EdgeOne Pages，但联调域名 `app-cn.tapnow.tv` 仍处于 `online` 状态且回源南京 CLB 旧容器化前端副本。\n"
                "选项：\n"
                "A. 在正式 EdgeOne 上为 `app-cn.tapnow.tv` 配置边缘 L7 `302` 重定向至 `https://app.tapnow.tv`（保留路径参数）\n"
                "B. 直接在正式 EdgeOne 停用（offline）并下线 `app-cn.tapnow.tv` 加速域名\n"
                "C. 暂维持现状不动\n"
                "建议：选 A 。"
            )
            with self.assertRaises(outbound.OutboundError) as cm_temp_dom:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_tapnow-prod-topology",
                    "ENG-1061 收口联调域名 app-cn.tapnow.tv",
                    bad_temp_domain_ask,
                    key="eng-1061-app-cn-retire",
                )
            self.assertIn("REG-122", str(cm_temp_dom.exception))

            good_temp_domain_ask = (
                "腾讯云国际 Prod（200053747873）正式站点下，落后版本的联调域名 app-cn.tapnow.tv 仍在线并指向集群内旧容器前端。\n"
                "---more---\n"
                "事情：正式域名 `app.tapnow.tv` 已上线，临时联调域名 `app-cn.tapnow.tv` 已完成历史使命。\n"
                "选项：\n"
                "A. 直接在正式 EdgeOne 停用（offline）并下线 `app-cn.tapnow.tv` 加速域名\n"
                "B. 在正式 EdgeOne 上为 `app-cn.tapnow.tv` 配置边缘 L7 `302` 重定向至 `https://app.tapnow.tv`\n"
                "建议：选 A 。"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_tapnow-prod-topology",
                "ENG-1061 收口联调域名 app-cn.tapnow.tv",
                good_temp_domain_ask,
                key="eng-1061-app-cn-retire",
            )

            c_code, c_label = admin_web.diagnose_override_reason(
                bad_temp_domain_ask,
                "直接在正式 EdgeOne 停用（offline）并下线 `app-cn.tapnow.tv` 加速域名",
                chosen_code="B",
                recommended="A",
            )
            self.assertEqual((c_code, c_label), ("arch_or_config_posture", "技术姿势纠偏"))

            # Verify ADB machine IP (192.220.19.132) whitelist ask with "B. 转交 Nicky 老师处理" is blocked
            bad_adb_whitelist_ask = (
                "Kai反馈新加坡阿里云ADB（am-gs5l37e19m92e2wff）上他机器的IP（192.220.19.132，一小时前可连）白名单没了，请求补加192.220.19.132/32。\n"
                "---more---\n"
                "事情：Kai 反馈新加坡阿里云 ADB 数仓实例（集群 `am-gs5l37e19m92e2wff`）上他机器的公网 IP `192.220.19.132` 白名单被清掉了。\n"
                "选项：\n"
                "A. 同意：核查 `am-gs5l37e19m92e2wff` 现有白名单分组，在独立分组补加 `192.220.19.132/32` 并回读通知 Kai\n"
                "B. 转交 Nicky 老师处理（由数据库负责人评估加白）\n"
                "C. 不同意加白\n"
                "建议：A 。"
            )
            with self.assertRaises(outbound.OutboundError) as cm_adb_wl:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_kaifeng",
                    "新加坡阿里云ADB(am-gs5l37e19m92e2wff)补加Kai机器IP白名单",
                    bad_adb_whitelist_ask,
                    key="kai_adb_whitelist",
                )
            self.assertIn("Nicky", str(cm_adb_wl.exception))

            # Verify withholding DB permissions from DBA Nicky Ye is blocked and override is diagnosed as arch_or_config_posture
            bad_dba_nicky_ask = (
                "ENG-1123（.tv价格页去境外品牌与下架9个礼包SKU）SQL已复核就绪，Andy请Nicky找你要南京生产MySQL建表权限账号执行。"
                "建议选A由专席经跳板机免发账密代执行备份更新与回读，或选B由你私发账号/开权限给Nicky。\n"
                "---more---\n"
                "选项：\n"
                "A. 不外发生产库账密，授权由 `tp_infra-migration-ops` 核对附件 SQL 直接经南京生产跳板机执行备份、更新、回读\n"
                "B. 同意给 Nicky Ye 开通南京生产腾讯云控制台 DB 访问权限（或由你私聊发给他生产库连接账号），由 Nicky 自行执行该 SQL\n"
                "C. 暂缓处理\n"
                "建议：A 。"
            )
            with self.assertRaises(outbound.OutboundError) as cm_dba_nicky:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_infra-migration-ops",
                    "ENG-1123生产MySQL执行或账密请示",
                    bad_dba_nicky_ask,
                    key="eng-1123-mysql-nicky",
                )
            self.assertIn("REG-122", str(cm_dba_nicky.exception))

            c_nicky, l_nicky = admin_web.diagnose_override_reason(
                bad_dba_nicky_ask,
                "nicky应该有所有数据库的最高权限 给他",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual((c_nicky, l_nicky), ("arch_or_config_posture", "技术姿势纠偏"))

            # Verify UTC 'Z' timestamp normalization in create_lamb, update_lamb, and _migrate
            now_local = datetime.now().astimezone().replace(microsecond=0)
            fail_dt = now_local - timedelta(minutes=20)
            retry_dt_utc = (now_local - timedelta(minutes=5)).astimezone(timezone.utc)
            retry_utc_str = retry_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            expected_retry_local = retry_dt_utc.astimezone().isoformat(timespec="seconds")

            st.create_lamb(
                "lamb_to_fail",
                "tp_tapnow-prod-topology",
                "subagent",
                "Wave 2 Prep",
                state="timeout",
                started_at=(fail_dt - timedelta(minutes=10)).isoformat(timespec="seconds"),
                finished_at=fail_dt.isoformat(timespec="seconds"),
                result_summary="timeout",
            )
            st.create_lamb(
                "lamb_utc_retry",
                "tp_tapnow-prod-topology",
                "subagent",
                "Authorized Prod Executor",
                state="done",
                started_at=retry_utc_str,
                last_act_at=retry_utc_str,
                finished_at=retry_utc_str,
                result_summary="all done",
            )
            row_retry = st.get_lamb("lamb_utc_retry")
            self.assertEqual(row_retry["started_at"], expected_retry_local)
            self.assertNotIn("Z", row_retry["started_at"])

            # Simulate legacy raw Z row in SQLite and verify _migrate normalizes it
            st.conn.execute(
                "UPDATE lambs SET started_at = ?, last_act_at = ? WHERE lamb_id = ?",
                (retry_utc_str, retry_utc_str, "lamb_utc_retry"),
            )
            st.conn.commit()
            st._migrate()
            row_migrated = st.get_lamb("lamb_utc_retry")
            self.assertEqual(row_migrated["started_at"], expected_retry_local)

            digest = evolve.build_evolve_digest(cfg, st, now_dt=now_local)
            lambs_map = {x["lamb_id"]: x for x in digest["d5_bus_and_lambs"]["recent_lambs"]}
            self.assertIn("lamb_utc_retry", lambs_map)
            self.assertTrue(lambs_map["lamb_to_fail"]["recovered_by_parent"])
            st.close()

    def test_reg123_p2p_live_owner_conversation_guard_owner_context_auto_settle_and_tccli_help_filter(self):
        """1.1.34（REG-123）：
        1. check_ask_pre_flight_guard 拦截在一对一私聊主人正在亲自对话、或尚未找账单负责人（Sammy/Ethan）核查时，抢发『A. 由我们找账单负责人核查 vs B. 由你本人在私聊回复』的伪选择题；
        2. write_receipt（status='waiting'）与 _auto_settle_open_escalations_on_owner_batch 在批次包含 owner_context/escalation_check 且回执 summary 确认『由主人在私聊亲自沟通，本会话保持静默观望』时，自动结案 open escalation；
        3. diagnose_override_reason 将『我已经在消息里回复了 你可以观望一下』精准归入 stale_or_already_handled；
        4. _summarize_cli_lamb_log 自动剥离 tccli 报错末尾的 5 行『To tccli help text... / tccli service[...] help』冗余提示，保留前面的 RuntimeError 与 SDK 报错行。
        """
        from sheepdog import admin_web, engine, outbound
        from sheepdog.config import Config
        from sheepdog.models import Message
        from sheepdog.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            cfg = Config(self_open_id="ou_owner", state_dir=state_dir)
            cfg.source.enabled = False
            st = Store(cfg.db_path)

            now_local = datetime.now().astimezone().replace(microsecond=0)
            t_kai_iso = (now_local - timedelta(minutes=2)).isoformat(timespec="seconds")
            t_owner_iso = (now_local - timedelta(minutes=1)).isoformat(timespec="seconds")
            t_ask_iso = (now_local - timedelta(seconds=45)).isoformat(timespec="seconds")

            st.upsert_topic(
                "tp_kaifeng",
                title="[managed] Kai Feng",
                kind="adopted",
                conversation_id="conv-kaifeng-123",
                state="running",
            )
            m_kai = Message(
                message_id="om_kai_livekit_123",
                chat_id="oc_p2p_kaifeng",
                chat_name="Kai Feng",
                chat_type="p2p",
                sender_id="ou_kaifeng",
                sender_name="Kai Feng",
                sender_type="user",
                content="Matthew，接着问一下 LiveKit：你说「好像没充钱」，麻烦有权限的人看一眼 Billing / Usage 页面截个图？",
                msg_type="text",
                create_time=t_kai_iso,
            )
            st.upsert_message(m_kai, "dispatch", "managed_chat", [], "tp_kaifeng")
            st.mark_batch(["om_kai_livekit_123"], "b20261106002228-kaifeng", "tp_kaifeng", "delivered")
            st.record_dispatch("b20261106002228-kaifeng", "tp_kaifeng", ["om_kai_livekit_123"], state="sent")

            m_owner_p2p = Message(
                message_id="om_owner_p2p_reply_123",
                chat_id="oc_p2p_kaifeng",
                chat_name="Kai Feng",
                chat_type="p2p",
                sender_id="ou_owner",
                sender_name="Matthew Hong",
                sender_type="user",
                content="用得哪个key你知道吗 知道的话发给我 不知道的话告诉我在哪里 我去查",
                msg_type="text",
                create_time=t_owner_iso,
            )
            st.upsert_message(m_owner_p2p, "dispatch", "owner_context", [], "tp_kaifeng")

            bad_livekit_ask = (
                "Kai在私聊跟进询问：国际站Cinemap目前使用的LiveKit Cloud项目是否确实欠费/未充值，请求有权限的人查看LiveKit Cloud控制台Billing/Usage状态并截图反馈。\n"
                "---more---\n"
                "事情：Kai 在私聊跟进询问国际站 LiveKit Cloud 账号 Billing 状态并请求截图。\n"
                "选项：\n"
                "A. 由我们找外部渠道/账单负责人（Sammy 或 Ethan）核查 LiveKit Cloud 控制台 Billing / Usage 状态并截图发给 Kai\n"
                "B. 由你本人登录 LiveKit Cloud 控制台查看截图（或直接在私聊回复 Kai）\n"
                "建议：选 A。"
            )
            with self.assertRaises(outbound.OutboundError) as cm_livekit:
                outbound.check_ask_pre_flight_guard(
                    cfg,
                    st,
                    "tp_kaifeng",
                    "Kai请求核查国际站LiveKit Cloud账号Billing状态并截图",
                    bad_livekit_ask,
                    about=["om_kai_livekit_123"],
                    batch="b20261106002228-kaifeng",
                )
            self.assertIn("REG-123", str(cm_livekit.exception))

            # Once delegate already checked or owner is not in live p2p and delegate replied, ask is allowed
            good_livekit_ask = (
                "已联系 Sammy 核实确认 LiveKit Cloud 账单需绑卡充值 $500，请拍板充值额度。\n"
                "---more---\n"
                "事情：已向 Sammy 核实确认 LiveKit Cloud 账单状态，当前需绑卡充值。\n"
                "选项：\n"
                "A. 批准充值 $500\n"
                "B. 暂不充值\n"
                "建议：A。"
            )
            outbound.check_ask_pre_flight_guard(
                cfg,
                st,
                "tp_kaifeng",
                "LiveKit Cloud 账单充值额度请示",
                good_livekit_ask,
            )

            # 2) Auto-settle open escalation when write_receipt(status='waiting') confirms owner is communicating in P2P
            st.add_escalation(
                "om_esc_livekit_123",
                "tp_kaifeng",
                "oc_owner_dm",
                f"🐕 需要你定 · Kai请求核查国际站LiveKit Cloud账号Billing状态并截图 [tp_kaifeng]\n{bad_livekit_ask}",
                t_ask_iso,
            )
            st.add_escalation_sources(
                "om_esc_livekit_123",
                "tp_kaifeng",
                [("om_kai_livekit_123", "oc_p2p_kaifeng")],
                "explicit",
            )
            st.record_dispatch(
                "b20261106002331-kaifeng",
                "tp_kaifeng",
                ["om_owner_p2p_reply_123", "sd_escalation_check_123"],
                state="sent",
            )
            st.update_topic("tp_kaifeng", state="running", pending_batch_id="b20261106002331-kaifeng", dispatched_at=t_owner_iso)
            st.create_lamb("lamb_7fac7af9", "tp_kaifeng", "subagent", "Check LiveKit")
            st.close()

            engine.write_receipt(
                cfg,
                "tp_kaifeng",
                "b20261106002331-kaifeng",
                {
                    "status": "waiting",
                    "summary": "已收到主人两项指示：①LiveKit由主人在私聊亲自沟通，本会话保持静默观望；②已派羔羊lamb_7fac7af9跟进。",
                    "anchors": ["lamb:lamb_7fac7af9"],
                },
            )

            st2 = Store(cfg.db_path)
            esc_row = st2.get_escalation("om_esc_livekit_123")
            self.assertEqual(esc_row["close_reason"], "answered")
            self.assertEqual(esc_row["answer_message_id"], "om_owner_p2p_reply_123")
            self.assertIsNotNone(esc_row["closed_at"])
            st2.close()

            # 3) diagnose_override_reason classifies '我已经在消息里回复了 你可以观望一下' as stale_or_already_handled
            c_code, c_label = admin_web.diagnose_override_reason(
                bad_livekit_ask,
                "[卡片回调] 其他：我已经在消息里回复了 你可以观望一下",
                chosen_code="其他",
                recommended="A",
            )
            self.assertEqual((c_code, c_label), ("stale_or_already_handled", "陈旧/已处理未重查"))

            # 4) _summarize_cli_lamb_log strips tccli help boilerplate lines
            tccli_err_log = (
                "Traceback (most recent call last):\n"
                '  File "/tmp/create_jump.py", line 42, in <module>\n'
                '    raise RuntimeError("tccli RunInstances failed")\n'
                "RuntimeError: tccli cvm RunInstances failed:\n"
                "[TencentCloudSDKException] code:InvalidParameterValue.ZoneInvalid message:The specified zone ap-nanjing-1 does not support pay-as-you-go.\n"
                "To tccli help text, you can run:\n"
                "  tccli help\n"
                "  tccli configure help\n"
                "  tccli service[cvm] help\n"
                "  tccli service[cvm] action[RunInstances] help\n"
            )
            sum_tccli, has_err_tccli = engine._summarize_cli_lamb_log(tccli_err_log)
            self.assertTrue(has_err_tccli)
            self.assertIn("RuntimeError: tccli cvm RunInstances failed:", sum_tccli)
            self.assertIn("InvalidParameterValue.ZoneInvalid", sum_tccli)
            self.assertNotIn("tccli help", sum_tccli)
            self.assertNotIn("tccli service[cvm] help", sum_tccli)


if __name__ == "__main__":
    unittest.main()
