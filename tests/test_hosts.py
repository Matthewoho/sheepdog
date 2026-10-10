"""规格 7.18：接通 Codex 和 Claude 会话。全部用假的 codex / claude 可执行文件（tests/fixtures/fake_hosts），
临时目录做 HOME、state_dir 和配置；不调用真实的 codex、claude、agentapi、lark-cli。"""

import contextlib
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.config import ConfigError, HostConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, write_receipt
from sheepdog.prompts import is_sheepdog_prompt, standing_rules_managed
from sheepdog.roster import RosterError, check_hosts, parse_roster
from sheepdog.sink import SinkError
from sheepdog.security import SecurityConfig
from sheepdog.sink.agentapi import DryRunSink, transcript_path
from sheepdog.sink.claude import ClaudeSink
from sheepdog.sink.cli_host import classify_error, extract_receipt
from sheepdog.sink.codex import CodexSink
from sheepdog.sink.codex_runner import DECLINE
from sheepdog.store import SCHEMA, Store, now_iso
from sheepdog.playbook import Playbook

from test_roster import FIXTURES, ME, FakeSink, FakeSource, msg, test_config

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "tests" / "fixtures" / "fake_hosts"
# 权限不归 sheepdog 管（7.18 第 3 条）：这些绝不能出现在命令行 / 请求参数里
FORBIDDEN_CLAUDE = ("--dangerously", "bypassPermissions", "--permission-mode")
FORBIDDEN_CODEX_KEYS = {"sandbox", "sandboxPolicy", "approvalPolicy", "approvalsReviewer"}
RECEIPT_ANSWER = '处理完了。\n```sheepdog-receipt\n{"status": "handled", "summary": "已处理"}\n```'


def all_keys(obj) -> set:
    """JSON 里出现过的所有键（递归）。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= all_keys(v)
    return out


def cx_roster(**over) -> dict:
    s = {"key": "cx", "mode": "managed", "conversation_id": "thr_test_existing", "title": "Codex 设计",
         "duty": "架构设计", "host": "codex", "chats": [{"chat_id": "oc_test_cx_p2p", "all_messages": True}]}
    s.update(over)
    return {"session": [s]}


class HostBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.work = self.root / "work"
        for d in (self.home, self.bin, self.work):
            d.mkdir()
        for name in ("codex", "claude"):
            exe = self.bin / name
            exe.write_text(f"#!{sys.executable}\n" + (FAKES / f"{name}.py").read_text(encoding="utf-8"),
                           encoding="utf-8")
            exe.chmod(0o755)
        self.codex_log = self.root / "codex.log"
        self.claude_log = self.root / "claude.log"
        self.codex_argv_log = self.root / "codex_argv.log"
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CODEX_LOG": str(self.codex_log), "FAKE_CLAUDE_LOG": str(self.claude_log),
            "FAKE_CODEX_ARGV_LOG": str(self.codex_argv_log),
            "FAKE_CODEX_MODE": "ok", "FAKE_CLAUDE_MODE": "ok", "FAKE_CODEX_ANSWER": "完成", "FAKE_CLAUDE_ANSWER": "完成",
            "SHEEPDOG_CONFIG": str(self.root / "config.toml"), "SHEEPDOG_STATE_DIR": str(self.root / "state"),
        })
        self.env.start()
        for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME"):
            os.environ.pop(k, None)
        self.cfg = test_config(self.root / "state")
        self.cfg.hosts["codex"] = HostConfig(name="codex", enabled=True, command=str(self.bin / "codex"),
                                             model="gpt-6-astra", effort="high", workdir=str(self.work))
        self.cfg.hosts["claude"] = HostConfig(name="claude", enabled=True, command=str(self.bin / "claude"),
                                              model="claude-opus-5-5", effort="high", workdir=str(self.work))
        self.store = Store(self.cfg.db_path)
        self.sinks_made: list = []

    def tearDown(self):
        for s in self.sinks_made:
            for p in s._procs.values():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(p.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    p.wait(5)
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def codex(self) -> CodexSink:
        s = CodexSink(self.cfg.hosts["codex"], self.store, self.cfg.runs_dir)
        self.sinks_made.append(s)
        return s

    def claude(self) -> ClaudeSink:
        s = ClaudeSink(self.cfg.hosts["claude"], self.store, self.cfg.runs_dir)
        self.sinks_made.append(s)
        return s

    @staticmethod
    def wait(*sinks, timeout=20):
        for s in sinks:
            for p in list(s._procs.values()):
                p.wait(timeout)

    def codex_requests(self) -> list[dict]:
        if not self.codex_log.exists():
            return []
        return [json.loads(x) for x in self.codex_log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def wait_for_method(self, method: str, timeout=15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not any(r.get("method") == method for r in self.codex_requests()):
            time.sleep(0.05)

    def claude_calls(self) -> list[dict]:
        if not self.claude_log.exists():
            return []
        return [json.loads(x) for x in self.claude_log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def write_rollout(self, thread_id: str, events: list[tuple[str, str, str]], legacy: bool = False,
                      injected: list[tuple[str, str]] = ()) -> Path:
        """手写的 Codex rollout 文件（结构照 codex-cli 0.153.4 app-server 建的线程，不拷真实文件）。

        events = [(时间, user_message / agent_message, 正文)]。legacy=False 写成 event_msg/item_completed 的
        UserMessage / AgentMessage，并像真实文件一样带上 response_item 和其他杂项条目；legacy=True 写老格式
        event_msg/user_message、agent_message（exec 建的线程）。injected = [(时间, 正文)]：response_item 里
        role=user 的系统注入内容（不是主人说的话）。
        """
        d = self.home / ".codex" / "sessions" / "2026" / "10" / "10"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"rollout-2026-10-10T10-00-00-{thread_id}.jsonl"
        rows = [{"timestamp": "2026-10-10T02:00:00Z", "type": "session_meta", "payload": {"id": thread_id}},
                {"timestamp": "2026-10-10T02:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                {"timestamp": "2026-10-10T02:00:00Z", "type": "turn_context", "payload": {"model": "gpt-6-astra"}}]
        for ts, text in injected:
            rows.append({"timestamp": ts, "type": "response_item",
                         "payload": {"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": text}]}})
        for i, (ts, kind, text) in enumerate(events):
            if legacy:
                rows.append({"timestamp": ts, "type": "event_msg", "payload": {"type": kind, "message": text}})
                continue
            role = "user" if kind == "user_message" else "assistant"
            rows.append({"timestamp": ts, "type": "response_item",
                         "payload": {"type": "message", "role": role,
                                     "content": [{"type": "input_text" if role == "user" else "output_text",
                                                  "text": text}]}})
            if kind == "user_message":
                item = {"type": "UserMessage", "id": f"item_{i}",
                        "content": [{"type": "text", "text": text, "text_elements": []}]}
            else:  # AgentMessage 的段 type 是大写的 Text；拆成两段，验证会拼起来
                half = len(text) // 2
                item = {"type": "AgentMessage", "id": f"item_{i}", "phase": "final_answer",
                        "content": [{"type": "Text", "text": text[:half]}, {"type": "Text", "text": text[half:]}]}
            rows.append({"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed", "item": item}})
        rows += [{"timestamp": "2026-10-10T02:59:00Z", "type": "event_msg", "payload": {"type": "token_count"}},
                 {"timestamp": "2026-10-10T02:59:00Z", "type": "event_msg", "payload": {"type": "task_complete"}}]
        with p.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p


# ======================= 配置、名册、账本 =======================
class ConfigTest(HostBase):
    def write_cfg(self, text: str) -> Path:
        p = self.root / "config.toml"
        p.write_text(f'state_dir = "{self.root / "state"}"\n' + text, encoding="utf-8")
        return p

    def test_example_config_defaults(self):
        src = (REPO / "examples" / "config.example.toml").read_text(encoding="utf-8")
        cfg = load_config(self.write_cfg(src))
        cx, cl = cfg.hosts["codex"], cfg.hosts["claude"]
        self.assertEqual((cx.enabled, cx.model, cx.effort, cx.executable), (False, "gpt-6-astra", "high", "codex"))
        self.assertEqual((cl.enabled, cl.model, cl.effort, cl.executable), (False, "claude-opus-5-5", "high", "claude"))
        self.assertEqual((cx.timeout_minutes, cx.max_concurrent), (60, 2))
        self.assertTrue(cfg.source.enabled)
        self.assertEqual(cfg.runs_dir, self.root / "state" / "runs")

    def test_defaults_without_sections(self):
        cfg = load_config(self.write_cfg(""))
        self.assertFalse(cfg.hosts["codex"].enabled)
        self.assertTrue(cfg.host_enabled("antigravity"))
        self.assertFalse(cfg.host_enabled("claude"))
        with self.assertRaisesRegex(ValueError, "宿主 claude 没开"):
            cfg.check_host("claude")
        with self.assertRaisesRegex(ValueError, "未知宿主"):
            cfg.check_host("gemini")

    def test_validation(self):
        with self.assertRaisesRegex(ConfigError, "workdir"):
            load_config(self.write_cfg("[hosts.codex]\nenabled = true\n"))
        with self.assertRaisesRegex(ConfigError, "settings 只给 Claude"):
            load_config(self.write_cfg('[hosts.codex]\nsettings = "x.json"\n'))
        with self.assertRaisesRegex(ConfigError, r"\[hosts\] 只认"):
            load_config(self.write_cfg("[hosts.gemini]\nenabled = true\n"))
        with self.assertRaisesRegex(ConfigError, "max_concurrent"):
            load_config(self.write_cfg("[hosts.claude]\nmax_concurrent = 0\n"))
        with self.assertRaisesRegex(ConfigError, r"\[source\] enabled"):
            load_config(self.write_cfg('[source]\nenabled = "no"\n'))
        cfg = load_config(self.write_cfg(f'[source]\nenabled = false\n[hosts.claude]\nenabled = true\n'
                                         f'workdir = "{self.work}"\nsettings = "s.json"\nmodel = "m"\n'))
        self.assertFalse(cfg.source.enabled)
        self.assertEqual((cfg.hosts["claude"].settings, cfg.hosts["claude"].model), ("s.json", "m"))


class RosterHostTest(unittest.TestCase):
    def test_host_fields(self):
        r = parse_roster(cx_roster(model="gpt-x", effort="xhigh"))
        s = r.sessions[0]
        self.assertEqual((s.host, s.model, s.effort, s.is_cli_host), ("codex", "gpt-x", "xhigh", True))
        self.assertEqual(parse_roster(cx_roster(host="")).sessions[0].host, "antigravity")
        with self.assertRaisesRegex(RosterError, "host 只能是"):
            parse_roster(cx_roster(host="gemini"))

    def test_check_hosts_reports_which_host(self):
        r = parse_roster(cx_roster())
        with self.assertRaisesRegex(RosterError, "宿主 codex 没开"):
            check_hosts(r, lambda h: h == "antigravity")
        check_hosts(r, lambda h: True)

    def test_default_host_does_not_change_hash(self):
        a = parse_roster(cx_roster(host="antigravity"))
        d = cx_roster()
        del d["session"][0]["host"]
        self.assertEqual(a.content_hash(), parse_roster(d).content_hash())
        self.assertNotEqual(a.content_hash(), parse_roster(cx_roster()).content_hash())

    def test_example_roster_has_host(self):
        text = (REPO / "examples" / "roster.example.toml").read_text(encoding="utf-8")
        self.assertIn('host = "antigravity"', text)
        self.assertIn('# host = "codex"', text)


class StoreMigrationTest(unittest.TestCase):
    def test_old_topics_table_gets_host_columns(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "old.sqlite3"
            old = SCHEMA
            for line in ("    host             TEXT DEFAULT 'antigravity',", "    model            TEXT,",
                         "    effort           TEXT,"):
                old = "\n".join(x for x in old.splitlines() if not x.startswith(line))
            conn = sqlite3.connect(db)
            conn.executescript(old)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)")}
            self.assertNotIn("host", cols)
            conn.execute("INSERT INTO topics(topic_id,title,kind,state,created_at,last_active) "
                          "VALUES('tp_old','旧','adopted','active','x','x')")
            conn.commit()
            conn.close()
            st = Store(db)
            t = st.get_topic("tp_old")
            self.assertEqual((t["host"], t["model"], t["effort"]), ("antigravity", None, None))
            st.close()


# ======================= Codex app-server =======================
class CodexSinkTest(HostBase):
    def test_new_thread_sequence_and_params(self):
        s = self.codex()
        tid = s.new_conversation("[managed] 设计", "开场提示词", "gpt-6-astra", "high", topic_id="tp_cx")
        self.assertTrue(tid.startswith("thr_test_"))
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.answer, res.conversation_id), ("done", "完成", tid))
        reqs = self.codex_requests()
        self.assertEqual([r.get("method") for r in reqs],
                         ["initialize", "initialized", "thread/start", "thread/name/set", "turn/start"])
        by = {r["method"]: r.get("params") for r in reqs}
        self.assertEqual(by["initialize"]["clientInfo"]["name"], "sheepdog")
        self.assertNotIn("id", reqs[1])  # initialized 是通知
        self.assertEqual(by["thread/start"], {"cwd": str(self.work), "model": "gpt-6-astra", "threadSource": "user"})
        self.assertEqual(by["thread/name/set"], {"threadId": tid, "name": "[managed] 设计"})
        self.assertEqual(by["turn/start"], {"threadId": tid, "model": "gpt-6-astra", "effort": "high",
                                            "input": [{"type": "text", "text": "开场提示词"}]})
        run = self.store.get_run(res.batch_id)
        self.assertEqual((run["state"], run["kind"], run["conversation_id"]), ("done", "new", tid))

    def test_no_permission_params_anywhere(self):
        s = self.codex()
        tid = s.new_conversation("t", "p", "gpt-6-astra", "high", topic_id="tp_cx")
        self.wait(s)
        s.collect_finished()
        s.send_message(tid, "续一轮", batch_id="b_test_1", topic_id="tp_cx", model="gpt-6-astra", effort="high")
        self.wait(s)
        s.collect_finished()
        reqs = self.codex_requests()
        self.assertIn("thread/resume", [r.get("method") for r in reqs])
        for r in reqs:
            self.assertFalse(all_keys(r.get("params") or {}) & FORBIDDEN_CODEX_KEYS, r)
        # runner 的命令行里也没有
        argv = " ".join(s._argv(self.store.get_run("b_test_1"), Path("x.in")))
        for k in FORBIDDEN_CODEX_KEYS:
            self.assertNotIn(k, argv)

    def test_resume_sequence(self):
        s = self.codex()
        s.send_message("thr_test_old", "批次内容", batch_id="b_test_r", topic_id="tp_cx", model="m1", effort="low")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual(res.state, "done")
        reqs = self.codex_requests()
        self.assertEqual([r.get("method") for r in reqs], ["initialize", "initialized", "thread/resume", "turn/start"])
        self.assertEqual(reqs[2]["params"], {"threadId": "thr_test_old", "model": "m1"})
        self.assertEqual(reqs[3]["params"]["effort"], "low")

    def test_server_requests_declined_and_recorded(self):
        os.environ["FAKE_CODEX_MODE"] = "approval"
        s = self.codex()
        s.send_message("thr_test_a", "x", batch_id="b_test_a", topic_id="tp_cx")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual(res.state, "done")
        self.assertEqual(res.denials, ["item/commandExecution/requestApproval", "some/unknownRequest"])
        replies = {r["id"]: r for r in self.codex_requests() if r.get("id") in (900, 901)}
        self.assertEqual(replies[900]["result"], {"decision": "decline"})
        self.assertIn("error", replies[901])
        self.assertEqual(json.loads(self.store.get_run("b_test_a")["denials"]), res.denials)
        for v in DECLINE.values():  # 拒绝的回复里也不带任何放行
            self.assertNotIn("accept", json.dumps(v))

    def test_retrying_errors_are_not_failures(self):
        os.environ["FAKE_CODEX_MODE"] = "retrying"
        s = self.codex()
        s.send_message("thr_test_r", "x", batch_id="b_test_rt", topic_id="tp_cx")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual(res.state, "done")

    def test_capacity_retries_then_fails(self):
        os.environ["FAKE_CODEX_MODE"] = "capacity"
        s = self.codex()
        s.send_message("thr_test_c", "x", batch_id="b_test_c", topic_id="tp_cx", model="gpt-6-astra", effort="high")
        waits = []
        for _ in range(3):
            self.wait(s)
            [res] = s.collect_finished()
            self.assertEqual((res.state, res.error_kind), ("retry_wait", "capacity"))
            run = self.store.get_run("b_test_c")
            waits.append(round((datetime.fromisoformat(run["retry_at"]) - datetime.now().astimezone())
                               .total_seconds() / 60))
            self.assertEqual(s.collect_finished(), [])  # 没到时间不重试
            self.assertTrue(s.run_active("b_test_c"))
            self.assertIn("还在跑上一轮", s.busy("thr_test_c"))
            past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(timespec="seconds")
            self.store.update_run("b_test_c", retry_at=past)
            self.assertEqual(s.collect_finished(), [])  # 重新起了一轮
        self.assertEqual(waits, [5, 10, 20])
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("failed", "capacity"))
        self.assertEqual(self.store.get_run("b_test_c")["attempts"], 4)
        # 重试用同一个模型（不换模型），走续接
        starts = [r for r in self.codex_requests() if r.get("method") == "turn/start"]
        self.assertEqual(len(starts), 4)
        self.assertEqual({r["params"]["model"] for r in starts}, {"gpt-6-astra"})

    def test_capacity_on_new_thread_retries_as_resume(self):
        os.environ["FAKE_CODEX_MODE"] = "capacity"
        s = self.codex()
        tid = s.new_conversation("t", "p", "", "", topic_id="tp_cx")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual(res.state, "retry_wait")
        os.environ["FAKE_CODEX_MODE"] = "ok"
        self.store.update_run(res.batch_id, retry_at="2000-01-01T00:00:00+08:00")
        s.collect_finished()
        self.wait(s)
        [res2] = s.collect_finished()
        self.assertEqual(res2.state, "done")
        methods = [r.get("method") for r in self.codex_requests()]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods[-2:], ["thread/resume", "turn/start"])
        self.assertEqual(self.store.get_run(res.batch_id)["conversation_id"], tid)

    def test_write_lock(self):
        os.environ["FAKE_CODEX_MODE"] = "locked"
        s = self.codex()
        s.send_message("thr_test_l", "x", batch_id="b_test_l", topic_id="tp_cx")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("locked", "locked"))
        # 实测报错在 thread/resume 的 JSON-RPC error 里（code -32600），不在 turn 的 error 里
        self.assertIn("thread/resume: thread thr_test_l already has an active writer", res.error)
        self.assertNotIn("turn/start", [r.get("method") for r in self.codex_requests()])
        self.assertIsNotNone(s.lock_streak_since("thr_test_l"))
        self.assertIsNotNone(s.last_human_activity("thr_test_l"))  # 撞上写锁视同主人正在用

    def test_logged_out_marks_host_unavailable(self):
        os.environ["FAKE_CODEX_MODE"] = "logout"
        s = self.codex()
        s.send_message("thr_test_o", "x", batch_id="b_test_o", topic_id="tp_cx")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("failed", "auth"))
        ok, why = s.available()
        self.assertFalse(ok)
        self.assertIn("登录失效", why)
        self.assertIn("logged out", s.unavailable_reason())
        self.assertTrue(s.busy("thr_test_other"))
        with self.assertRaises(SinkError):
            s.send_message("thr_test_other", "x", batch_id="b_test_o2", topic_id="tp_cx")
        # 30 分钟后放一轮试试；成功就恢复可用
        raw = json.loads(self.store.get_meta("host_unavailable:codex"))
        raw["since"] = "2000-01-01T00:00:00+08:00"
        self.store.set_meta("host_unavailable:codex", json.dumps(raw))
        os.environ["FAKE_CODEX_MODE"] = "ok"
        s.send_message("thr_test_other", "x", batch_id="b_test_o3", topic_id="tp_cx")
        self.wait(s)
        s.collect_finished()
        self.assertEqual(s.unavailable_reason(), "")

    def test_timeout_kills_process_group(self):
        os.environ["FAKE_CODEX_MODE"] = "slow"
        self.cfg.hosts["codex"].timeout_minutes = 0.002
        s = self.codex()
        s.send_message("thr_test_t", "x", batch_id="b_test_t", topic_id="tp_cx")
        pid = self.store.get_run("b_test_t")["pid"]
        self.wait_for_method("turn/start")
        time.sleep(0.2)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("timeout", "timeout"))
        with self.assertRaises(ProcessLookupError):  # 整个进程组（runner 和 app-server）都没了
            os.killpg(pid, 0)

    def test_new_thread_wait_limit(self):
        os.environ["FAKE_CODEX_MODE"] = "nothread"
        s = self.codex()
        s.NEW_WAIT_SECONDS = 1
        with self.assertRaisesRegex(SinkError, "没拿到线程编号"):
            s.new_conversation("t", "p", topic_id="tp_cx")
        run = self.store.runs_in_state("codex", "failed")[0]
        self.assertEqual(run["kind"], "new")
        with self.assertRaises(ProcessLookupError):
            os.killpg(run["pid"], 0)

    def test_restart_waits_then_collects_without_resend(self):
        os.environ["FAKE_CODEX_MODE"] = "slow"
        s1 = self.codex()
        s1.send_message("thr_test_rs", "x", batch_id="b_test_rs", topic_id="tp_cx")
        self.wait_for_method("turn/start")
        s2 = self.codex()  # 牧羊犬重启：新进程里没有子进程句柄
        self.assertEqual(s2.collect_finished(), [])
        self.assertEqual(self.store.get_run("b_test_rs")["state"], "running")
        os.killpg(self.store.get_run("b_test_rs")["pid"], signal.SIGKILL)
        s1._procs["b_test_rs"].wait(10)
        [res] = s2.collect_finished()
        self.assertEqual(res.state, "failed")
        self.assertEqual(sum(r.get("method") == "turn/start" for r in self.codex_requests()), 1)

    def test_restart_collects_finished_output(self):
        s1 = self.codex()
        s1.send_message("thr_test_rd", "x", batch_id="b_test_rd", topic_id="tp_cx")
        self.wait(s1)
        [res] = self.codex().collect_finished()
        self.assertEqual((res.state, res.answer), ("done", "完成"))

    def test_reused_pid_is_not_ours(self):
        self.store.add_run("b_test_pid", "tp_cx", "codex", "thr_test_p", "resume", "", "", "")
        self.store.update_run("b_test_pid", pid=os.getpid(), cmd_fingerprint="-m sheepdog.sink.codex_runner --nope")
        [res] = self.codex().collect_finished()
        self.assertEqual(res.state, "failed")
        self.assertEqual(res.error_kind, "other")

    def test_human_activity_from_rollout(self):
        s = self.codex()
        self.write_rollout("thr_test_h", [
            ("2026-10-10T02:00:01Z", "user_message", "初始提示词（不算主人）"),
            ("2026-10-10T02:00:05Z", "agent_message", "已就绪"),
            ("2026-10-10T02:10:00Z", "user_message", "[sheepdog] 新信号批次 `b_test_x`（topic `tp_cx`，共 1 条）"),
            ("2026-10-10T02:20:00Z", "user_message", "主人直接说：先别发"),
            ("2026-10-10T02:30:00Z", "user_message", "📌 规则已更新（以下为现行完整规则，取代之前的版本）\n..."),
        ])
        last = s.last_human_activity("thr_test_h")
        self.assertEqual(last, datetime.fromisoformat("2026-10-10T02:20:00+00:00"))
        self.assertTrue(s.transcript_contains("thr_test_h", "b_test_x"))
        self.assertFalse(s.transcript_contains("thr_test_h", "b_test_nope"))
        hist = s.history("thr_test_h", 2)
        self.assertEqual([h[0] for h in hist], ["问", "问"])
        self.write_rollout("thr_test_h2", [("2026-10-10T02:00:01Z", "user_message", "初始提示词")])
        self.assertIsNone(s.last_human_activity("thr_test_h2"))

    def test_injected_response_items_are_not_the_owner(self):
        s = self.codex()
        self.write_rollout("thr_test_inj", [
            ("2026-10-10T02:00:01Z", "user_message", "初始提示词"),
            ("2026-10-10T02:00:30Z", "agent_message", "已就绪，等第一批信号"),
            ("2026-10-10T02:10:00Z", "user_message", "[sheepdog] 新信号批次 `b_test_y`（topic `tp_cx`，共 1 条）"),
        ], injected=[("2026-10-10T02:40:00Z", "<recommended_plugins>...</recommended_plugins> b_test_only_ri"),
                     ("2026-10-10T02:41:00Z", "# AGENTS.md instructions for /work\n<environment_context>...")])
        self.assertIsNone(s.last_human_activity("thr_test_inj"))
        self.assertTrue(s.transcript_contains("thr_test_inj", "b_test_y"))
        self.assertFalse(s.transcript_contains("thr_test_inj", "b_test_only_ri"))
        hist = s.history("thr_test_inj")
        self.assertEqual([h[0] for h in hist], ["问", "答", "问"])  # response_item 不重复计入
        self.assertEqual(hist[1][1], "已就绪，等第一批信号")  # 两段 Text 拼起来

    def test_legacy_rollout_format(self):
        s = self.codex()
        self.write_rollout("thr_test_lg", [
            ("2026-10-10T02:00:01Z", "user_message", "初始提示词"),
            ("2026-10-10T02:05:00Z", "agent_message", "好"),
            ("2026-10-10T02:10:00Z", "user_message", "[sheepdog] 新信号批次 `b_test_lg`"),
            ("2026-10-10T02:20:00Z", "user_message", "主人：等一下"),
        ], legacy=True)
        self.assertEqual(s.last_human_activity("thr_test_lg"), datetime.fromisoformat("2026-10-10T02:20:00+00:00"))
        self.assertTrue(s.transcript_contains("thr_test_lg", "b_test_lg"))
        self.assertEqual(len(s.history("thr_test_lg", 10)), 4)


class CodexProfileTest(HostBase):
    """[hosts.codex] profile：sheepdog 读 ~/.codex/<profile>.config.toml，展开成 -c 传给 app-server（app-server 不接受
    -p，20:05 实测）。-c 只能来自配置档展开，除此之外不传任何沙箱、审批参数。"""
    FORBIDDEN_TOKENS = ("-p", "--profile", "--config", "--sandbox", "-s", "-a", "--ask-for-approval", "--full-auto")
    FORBIDDEN_SUBSTR = ("--dangerously", "danger-full-access")
    PROFILE = """# 测试用配置档
sandbox_mode = "workspace-write"
model_reasoning_summary = "say \\"hi\\" \\\\ done"
max_tokens = 4096
temperature = 0.5
tags = [["a", "b"], [1, 2]]

[sandbox_workspace_write]
network_access = true
writable_roots = ["/tmp/lark cli", "/tmp/state"]
"""

    def write_profile(self, name: str, text: str) -> Path:
        d = self.home / ".codex"
        d.mkdir(exist_ok=True)
        p = d / f"{name}.config.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def app_server_argvs(self) -> list[list[str]]:
        if not self.codex_argv_log.exists():
            return []
        return [json.loads(x) for x in self.codex_argv_log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def check_argv(self, argv: list[str]) -> None:
        for tok in argv:
            self.assertNotIn(tok, self.FORBIDDEN_TOKENS, argv)
            for bad in self.FORBIDDEN_SUBSTR:
                self.assertNotIn(bad, tok, argv)

    def run_one(self, expect: str = "done"):
        s = self.codex()
        s.send_message("thr_test_pf", "x", batch_id="b_test_pf", topic_id="tp_cx", model="gpt-6-astra", effort="high")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual(res.state, expect, res.error)
        for r in self.codex_requests():
            self.assertFalse(all_keys(r.get("params") or {}) & FORBIDDEN_CODEX_KEYS, r)
        runner = s._argv(self.store.get_run("b_test_pf"), Path("x.in"))
        self.assertNotIn("-c", runner)  # runner 自己的命令行里没有 -c
        return res, runner

    def test_no_profile_is_plain_app_server(self):
        self.run_one()
        [app] = self.app_server_argvs()
        self.assertEqual(app, ["app-server"])

    def test_profile_expanded_to_c_only(self):
        import tomllib
        self.write_profile("sheepdog_test-1", self.PROFILE)
        self.cfg.hosts["codex"].profile = "sheepdog_test-1"
        self.run_one()
        [app] = self.app_server_argvs()
        self.assertEqual(app[-1], "app-server")
        self.check_argv(app)
        flags = app[:-1]
        self.assertEqual(flags[0::2], ["-c"] * (len(flags) // 2))  # app-server 前面只有一对对 -c
        pairs = [x.split("=", 1) for x in flags[1::2]]
        self.assertEqual([k for k, _ in pairs], ["sandbox_mode", "model_reasoning_summary", "max_tokens", "temperature",
                                                 "tags", "sandbox_workspace_write.network_access",
                                                 "sandbox_workspace_write.writable_roots"])
        self.assertEqual(flags[1], 'sandbox_mode="workspace-write"')
        self.assertIn("sandbox_workspace_write.network_access=true", flags)
        self.assertIn('sandbox_workspace_write.writable_roots=["/tmp/lark cli", "/tmp/state"]', flags)
        # 展开结果和文件内容一一对应：每个值按 TOML 读回来，等于配置档里同一路径的值
        data = tomllib.loads(self.PROFILE)
        for k, lit in pairs:
            want = data
            for part in k.split("."):
                want = want[part]
            self.assertEqual(tomllib.loads(f"v = {lit}")["v"], want, k)
        self.assertEqual(len(pairs), 7)

    def test_profile_problems_fail_without_retry(self):
        self.cfg.hosts["codex"].profile = "missing"
        res, _ = self.run_one(expect="failed")
        self.assertEqual(res.error_kind, "config")
        self.assertIn("missing.config.toml 不存在", res.error)
        self.assertEqual(self.app_server_argvs(), [])  # 没起 app-server
        self.assertFalse(self.codex().run_active("b_test_pf"))  # 不重试
        from sheepdog.sink.codex_profile import ProfileError, profile_overrides
        bad = {"broken": "sandbox_mode = \n", "date": "when = 2026-10-10\n", "aot": "[[x]]\na = 1\n",
               "quoted": '"a.b" = 1\n'}
        for name, text in bad.items():
            with self.subTest(name):
                self.write_profile(name, text)
                with self.assertRaises(ProfileError):
                    profile_overrides(name)

    def test_config_error_marks_attention_and_tells_bus(self):
        self.cfg.hosts["codex"].profile = "missing"
        cx = self.codex()
        roster = parse_roster(cx_roster())
        d = Dispatcher(self.cfg, self.store, {"antigravity": FakeSink(), "codex": cx}, roster)
        d.sync_roster()
        d.ensure_bus_topic()
        self.store.update_topic("tp_cx", onboarded_at=now_iso())
        Collector(self.cfg, self.store, FakeSource([[msg(message_id="om_test_pf1", chat_id="oc_test_cx_p2p",
                                                          chat_type="p2p")]]), roster).poll_once()
        d.dispatch_once()
        self.wait(cx)
        d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_cx")["state"], sm.ATTENTION)
        self.assertEqual(self.store.get_message("om_test_pf1")["dispatch_state"], "pending")
        hints = [r["content"] for r in self.store.conn.execute("SELECT content FROM messages WHERE topic_id=?",
                                                                (BUS_TOPIC_ID,)) if "配置档有问题" in r["content"]]
        self.assertEqual(len(hints), 1)
        self.assertIn("missing.config.toml 不存在", hints[0])
        self.assertEqual(self.app_server_argvs(), [])

    def test_fake_codex_rejects_p_like_the_real_one(self):
        import subprocess
        out = subprocess.run([str(self.bin / "codex"), "-p", "x", "app-server"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--profile only applies to runtime commands", out.stderr)

    def test_profile_validation(self):
        for bad in ("a b", "../x", "x.y", "名字", "-c"):
            with self.subTest(bad):
                p = self.root / "config.toml"
                p.write_text(f'state_dir = "{self.root / "state"}"\n[hosts.codex]\nprofile = "{bad}"\n', encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "profile 只能是"):
                    load_config(p)
        p.write_text(f'state_dir = "{self.root / "state"}"\n[hosts.claude]\nprofile = "x"\n', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "profile 只给 Codex"):
            load_config(p)
        p.write_text(f'state_dir = "{self.root / "state"}"\n[hosts.codex]\nprofile = "sheepdog"\n', encoding="utf-8")
        self.assertEqual(load_config(p).hosts["codex"].profile, "sheepdog")

    def test_doctor_reads_profile_file(self):
        self.cfg.hosts["claude"].enabled = False
        codex_dir = self.home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text('sandbox_mode = "read-only"\n', encoding="utf-8")
        text = "\n".join(cli.host_report(self.cfg, self.store))
        self.assertIn("配置档: 没设", text)
        self.assertIn("sandbox_mode = read-only", text)
        self.cfg.hosts["codex"].profile = "sheepdog"
        text = "\n".join(cli.host_report(self.cfg, self.store))
        self.assertIn("配置档: profile = sheepdog", text)
        self.assertIn("展开成 -c 参数传给自己起的 app-server", text)
        self.assertIn("sandbox_mode = 读不到，网络 读不到，writable_roots = 读不到", text)
        self.assertIn("✗ 配置档有问题，这个宿主的每一轮都会直接失败", text)
        self.write_profile("sheepdog", 'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n'
                                       'writable_roots = ["/tmp/lark", "/tmp/state"]\n')
        text = "\n".join(cli.host_report(self.cfg, self.store))
        self.assertIn("sandbox_mode = workspace-write，网络 开，writable_roots = /tmp/lark, /tmp/state", text)
        self.assertIn('展开: -c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=true', text)
        self.assertNotIn("✗", text)
        self.assertNotIn("发不了飞书", text)

    def test_profile_change_needs_restart(self):
        p = self.root / "config.toml"
        p.write_text(f'state_dir = "{self.root / "state"}"\n[hosts.codex]\nenabled = true\nworkdir = "{self.work}"\n'
                     f'profile = "a"\n', encoding="utf-8")
        old = load_config()
        st = Store(old.db_path)
        d = Dispatcher(old, st, cli._sinks(old, st, dry=False))
        p.write_text(p.read_text(encoding="utf-8").replace('profile = "a"', 'profile = "b"'), encoding="utf-8")
        with self.assertLogs(level="WARNING") as logs:
            new = cli._reload_config(old, mock.Mock(), st, FakeSource([]), Collector(old, st, FakeSource([])), d)
        self.assertTrue(any("profile 改了" in x for x in logs.output))
        self.assertEqual(new.hosts["codex"].profile, "a")
        st.close()

    def test_example_profile_file(self):
        from sheepdog.sink.codex_profile import flatten
        import tomllib
        data = tomllib.loads((REPO / "examples" / "codex-profile.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["sandbox_mode"], "workspace-write")
        ws = data["sandbox_workspace_write"]
        self.assertTrue(ws["network_access"])
        self.assertTrue(any(r.endswith("/.lark-cli") for r in ws["writable_roots"]))
        self.assertTrue(any(r.endswith("/.local/state/sheepdog") for r in ws["writable_roots"]))
        self.assertEqual(len(flatten(data)), 3)  # 示例能原样展开
        self.assertIn("reply-card", (REPO / "examples" / "playbook" / "cli_host.md").read_text(encoding="utf-8"))


# ======================= Claude 命令行 =======================
class ClaudeSinkTest(HostBase):
    def test_new_and_resume_command_lines(self):
        self.cfg.hosts["claude"].settings = "/tmp/claude-settings.json"
        s = self.claude()
        cid = s.new_conversation("[managed] 写作", "开场", "claude-opus-5-5", "high", topic_id="tp_cl")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.conversation_id), ("done", cid))
        # 第二轮：会话已经有一轮成功，走 --resume
        s.send_message(cid, "第二轮", batch_id="b_test_cl", topic_id="tp_cl", model="claude-opus-5-5", effort="max")
        self.wait(s)
        s.collect_finished()
        calls = self.claude_calls()
        exe = str(self.bin / "claude")
        self.assertEqual(calls[0]["argv"], ["-p", "--output-format", "json", "--session-id", cid, "-n", "[managed] 写作",
                                            "--model", "claude-opus-5-5", "--effort", "high",
                                            "--settings", "/tmp/claude-settings.json"])
        self.assertEqual(calls[1]["argv"], ["-p", "--output-format", "json", "--resume", cid, "--model",
                                            "claude-opus-5-5", "--effort", "max", "--settings",
                                            "/tmp/claude-settings.json"])
        self.assertEqual([c["stdin"] for c in calls], ["开场", "第二轮"])
        for run in (self.store.get_run(res.batch_id), self.store.get_run("b_test_cl")):
            full = [exe] + calls[0]["argv"]
            self.assertTrue(run["cmd_fingerprint"])
            for bad in FORBIDDEN_CLAUDE:
                self.assertFalse(any(bad in a for a in full + run["cmd_fingerprint"].split()), bad)

    def test_resume_without_session_creates_it(self):
        s = self.claude()
        s.send_message("00000000-0000-4000-8000-00000000abcd", "x", batch_id="b_test_nos", topic_id="tp_cl")
        self.wait(s)
        s.collect_finished()
        argv = self.claude_calls()[0]["argv"]
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)

    def test_not_logged_in(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notlogged"
        s = self.claude()
        s.new_conversation("t", "p", topic_id="tp_cl")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("failed", "auth"))
        self.assertIn("Not logged in", s.unavailable_reason())

    def test_permission_denials(self):
        os.environ["FAKE_CLAUDE_MODE"] = "denied"
        s = self.claude()
        s.new_conversation("t", "p", topic_id="tp_cl")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.denials), ("done", ["Bash"]))

    def test_capacity_is_retried(self):
        os.environ["FAKE_CLAUDE_MODE"] = "capacity"
        s = self.claude()
        s.new_conversation("t", "p", topic_id="tp_cl")
        self.wait(s)
        [res] = s.collect_finished()
        self.assertEqual((res.state, res.error_kind), ("retry_wait", "capacity"))

    def test_transcript_and_interactive_resume(self):
        s = self.claude()
        cid = "00000000-0000-4000-8000-0000000000c1"
        d = self.home / ".claude" / "projects" / "-tmp-work"
        d.mkdir(parents=True)
        rows = [
            {"type": "user", "timestamp": "2026-10-10T02:00:00Z", "message": {"role": "user", "content": "开场"}},
            {"type": "assistant", "timestamp": "2026-10-10T02:00:05Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}},
            {"type": "user", "timestamp": "2026-10-10T02:05:00Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "timestamp": "2026-10-10T02:10:00Z",
             "message": {"role": "user", "content": "[sheepdog] 新信号批次 `b_test_cl1`"}},
            {"type": "user", "timestamp": "2026-10-10T02:15:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "主人：换个思路"}]}},
        ]
        (d / f"{cid}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        self.assertEqual(s.last_human_activity(cid), datetime.fromisoformat("2026-10-10T02:15:00+00:00"))
        self.assertTrue(s.transcript_contains(cid, "b_test_cl1"))
        self.assertIn("claude --resume", s.resume_hint(cid))
        with mock.patch.object(ClaudeSink, "_ps_lines", return_value=[(4242, f"node /x/claude --resume {cid}")]):
            self.assertTrue(s.interactive_attached(cid))
            self.assertGreater(s.last_human_activity(cid), datetime.now().astimezone() - timedelta(seconds=5))
        with mock.patch.object(ClaudeSink, "_ps_lines", return_value=[(4242, f"claude -p --resume {cid}")]):
            self.assertFalse(s.interactive_attached(cid))  # -p 是牧羊犬这类非交互调用，不算主人


# ======================= 解析与提示词 =======================
class ParsingTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_error("Selected model is at capacity"), "capacity")
        self.assertEqual(classify_error("HTTP 503 Service Unavailable"), "capacity")
        self.assertEqual(classify_error("thread x already has an active writer"), "locked")
        self.assertEqual(classify_error("token could not be refreshed"), "auth")
        self.assertEqual(classify_error("Not logged in · Please run /login"), "auth")
        self.assertEqual(classify_error("第 512 行语法错误"), "other")

    def test_extract_receipt(self):
        self.assertEqual(extract_receipt(RECEIPT_ANSWER)[0], {"status": "handled", "summary": "已处理"})
        self.assertEqual(extract_receipt("没有回执块"), (None, ""))
        data, bad = extract_receipt("```sheepdog-receipt\n{不是 JSON}\n```")
        self.assertIsNone(data)
        self.assertIn("JSON", bad)
        two = '```sheepdog-receipt\n{"status":"handled"}\n```\n```sheepdog-receipt\n{"status":"done"}\n```'
        self.assertEqual(extract_receipt(two)[0], {"status": "done"})

    def test_sheepdog_prompt_marker(self):
        self.assertTrue(is_sheepdog_prompt("[sheepdog] 新信号批次 `b1`"))
        self.assertTrue(is_sheepdog_prompt("📌 规则已更新（以下为现行完整规则，取代之前的版本）"))
        self.assertFalse(is_sheepdog_prompt("帮我看看这个 [sheepdog] 的问题"))

    def test_cli_host_rules_only_for_cli_sessions(self):
        pb = Playbook(FIXTURES)
        cx = parse_roster(cx_roster()).sessions[0]
        ag = parse_roster(cx_roster(host="antigravity")).sessions[0]
        a = standing_rules_managed(pb, cx, [15, 30], 60)
        self.assertIn("FX-CLI-HOST host=codex", a)
        self.assertIn("sheepdog-receipt", a)
        b = standing_rules_managed(pb, ag, [15, 30], 60)
        self.assertNotIn("FX-CLI-HOST", b)
        self.assertNotIn("sheepdog-receipt", b)

    def test_example_playbook_cli_host(self):
        text = (REPO / "examples" / "playbook" / "cli_host.md").read_text(encoding="utf-8")
        self.assertIn("sheepdog-receipt", text)
        self.assertIn("blocked", text)
        self.assertIn("Codex / Claude", (REPO / "examples" / "playbook" / "bus.md").read_text(encoding="utf-8"))

    def test_dryrun_sink_per_host(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cid = DryRunSink("codex").new_conversation("t", "p", "gpt-6-astra", "high", topic_id="tp_x")
            DryRunSink("claude").send_message("c1", "hi", model="claude-opus-5-5", effort="high")
        self.assertEqual(cid, "dryrun-codex-1")
        self.assertIn("[dryrun:codex] new-conversation title='t' model=gpt-6-astra effort=high", out.getvalue())
        self.assertIn("[dryrun:claude] send-message -> c1 model=claude-opus-5-5 effort=high", out.getvalue())


# ======================= engine 接上命令行宿主 =======================
class EngineHostTest(HostBase):
    def setUp(self):
        super().setUp()
        self.ag = FakeSink()
        self.notices: list[str] = []
        self.cx = self.codex()
        self.roster = parse_roster(cx_roster())

    def disp(self, roster=None) -> Dispatcher:
        return Dispatcher(self.cfg, self.store, {"antigravity": self.ag, "codex": self.cx}, roster or self.roster,
                          notifier=self.notices.append)

    def ready(self) -> Dispatcher:
        """名册里的 codex 会话：同步进账本，标记已 onboarding，免得先发登记通知。"""
        d = self.disp()
        d.sync_roster()
        d.ensure_bus_topic()
        self.store.update_topic("tp_cx", onboarded_at=now_iso())
        return d

    def poll(self, *msgs):
        Collector(self.cfg, self.store, FakeSource([list(msgs)]), self.roster).poll_once()

    def round(self, d):
        """等后台跑完，再跑一轮主循环（先收尾、再投递）。"""
        self.wait(self.cx)
        return d.dispatch_once()

    def bus_hints(self, needle: str) -> list:
        return [r for r in self.store.conn.execute("SELECT * FROM messages WHERE topic_id=?", (BUS_TOPIC_ID,))
                if needle in (r["content"] or "")]

    def test_topic_host_columns(self):
        d = self.ready()
        t = self.store.get_topic("tp_cx")
        self.assertEqual(t["host"], "codex")
        self.assertEqual(d.model_effort(t), ("gpt-6-astra", "high"))
        self.store.update_topic("tp_cx", effort="xhigh")
        self.cfg.hosts["codex"].model = "gpt-6-next"  # 热加载：默认值下一轮生效
        self.assertEqual(d.model_effort(self.store.get_topic("tp_cx")), ("gpt-6-next", "xhigh"))

    def test_batch_receipt_from_answer(self):
        os.environ["FAKE_CODEX_ANSWER"] = RECEIPT_ANSWER
        d = self.ready()
        self.poll(msg(message_id="om_test_cx1", chat_id="oc_test_cx_p2p", chat_type="p2p", content="帮我看看"))
        r = d.dispatch_once()
        self.assertEqual(r["topics"]["tp_cx"]["sent"], 1)
        t = self.store.get_topic("tp_cx")
        self.assertEqual(t["state"], sm.RUNNING)
        batch = t["pending_batch_id"]
        self.wait(self.cx)
        reqs = self.codex_requests()
        prompt = next(x for x in reqs if x.get("method") == "turn/start")["params"]["input"][0]["text"]
        self.assertIn(batch, prompt)
        self.assertIn("sheepdog-receipt", prompt)
        self.assertEqual(next(x for x in reqs if x.get("method") == "thread/resume")["params"]["threadId"],
                         "thr_test_existing")
        # 后台还在跑：下一轮不按回执超时算，也不再投
        self.store.update_topic("tp_cx", dispatched_at="2000-01-01T00:00:00+08:00")
        self.round(d)
        t = self.store.get_topic("tp_cx")
        self.assertEqual(t["state"], sm.ACTIVE)
        self.assertEqual(t["summary"], "已处理")
        self.assertTrue(d.receipt_path("tp_cx", batch).exists())
        self.assertEqual(self.store.get_dispatch(batch)["state"], "acked")

    def test_own_receipt_wins_over_block(self):
        os.environ["FAKE_CODEX_ANSWER"] = RECEIPT_ANSWER
        d = self.ready()
        self.poll(msg(message_id="om_test_cx2", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        batch = self.store.get_topic("tp_cx")["pending_batch_id"]
        write_receipt(self.cfg, "tp_cx", batch, {"status": "needs_decision", "summary": "会话自己交的"})
        self.round(d)
        t = self.store.get_topic("tp_cx")
        self.assertEqual((t["state"], t["summary"]), (sm.WAITING_HUMAN, "会话自己交的"))

    def test_bad_or_missing_block_counts_as_no_receipt(self):
        os.environ["FAKE_CODEX_ANSWER"] = '```sheepdog-receipt\n{"status": "whatever"}\n```'
        d = self.ready()
        self.poll(msg(message_id="om_test_cx3", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        batch = self.store.get_topic("tp_cx")["pending_batch_id"]
        with self.assertLogs("sheepdog", "WARNING") as logs:
            self.round(d)
        self.assertTrue(any("回执块格式不对" in x for x in logs.output))
        t = self.store.get_topic("tp_cx")
        self.assertEqual((t["state"], t["receipt_missed"]), (sm.ACTIVE, 1))
        self.assertEqual(self.store.get_dispatch(batch)["state"], "missed")

    def test_write_lock_requeues_as_human_attached(self):
        os.environ["FAKE_CODEX_MODE"] = "locked"
        d = self.ready()
        self.poll(msg(message_id="om_test_cx4", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        self.round(d)
        t = self.store.get_topic("tp_cx")
        self.assertEqual((t["state"], t["pending_batch_id"]), (sm.HUMAN_ATTACHED, None))
        self.assertEqual(self.store.get_message("om_test_cx4")["dispatch_state"], "pending")
        self.assertEqual(self.store.get_topic("tp_cx")["retries"], 0)
        # 连续锁满 60 分钟：提示总线一次
        self.store.conn.execute("UPDATE runs SET started_at='2000-01-01T00:00:00+08:00'")
        self.store.conn.commit()
        d._check_lock_streaks()
        d._check_lock_streaks()
        [hint] = self.bus_hints("写锁冲突")
        self.assertIn("FX-HOST-LOCKED host=Codex title=Codex 设计 topic=tp_cx since=2000-01-01T00:00:00+08:00",
                      hint["content"])

    def test_login_failure_queues_and_notifies_once(self):
        os.environ["FAKE_CODEX_MODE"] = "logout"
        d = self.ready()
        self.poll(msg(message_id="om_test_cx5", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        self.round(d)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("需要你重新登录 Codex", self.notices[0])
        self.assertIn("codex login", self.notices[0])
        self.assertEqual(self.store.get_message("om_test_cx5")["dispatch_state"], "pending")
        r = d.dispatch_once()
        self.assertIn("登录失效", r["topics"]["tp_cx"]["queued"])
        self.assertEqual(len(self.notices), 1)  # 24 小时内同一宿主只发一次
        self.assertEqual(sum(x.get("method") == "turn/start" for x in self.codex_requests()), 1)

    def test_login_failure_on_new_session_still_notifies(self):
        os.environ["FAKE_CODEX_MODE"] = "logout"
        self.cfg.hosts["codex"].ask_owner_before_new = False  # 这里测登录提醒，不测「先问主人」
        d = self.ready()
        tid = d.new_session("cxnew", "新的", "职责", [], host="codex")["conversation_id"]
        self.assertTrue(tid)
        self.round(d)
        self.assertEqual(len(self.notices), 1)

    def test_capacity_exhausted_marks_attention(self):
        os.environ["FAKE_CODEX_MODE"] = "capacity"
        d = self.ready()
        self.poll(msg(message_id="om_test_cx6", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        for _ in range(3):
            self.round(d)
            self.store.conn.execute("UPDATE runs SET retry_at='2000-01-01T00:00:00+08:00' WHERE state='retry_wait'")
            self.store.conn.commit()
            self.assertEqual(self.store.get_topic("tp_cx")["state"], sm.RUNNING)
            d.dispatch_once()  # 到点重试
        self.round(d)
        t = self.store.get_topic("tp_cx")
        self.assertEqual(t["state"], sm.ATTENTION)
        self.assertEqual(self.store.get_message("om_test_cx6")["dispatch_state"], "pending")
        self.assertEqual(len(self.bus_hints("capacity")), 1)
        self.assertEqual(sum(x.get("method") == "turn/start" for x in self.codex_requests()), 4)

    def test_timeout_marks_attention(self):
        os.environ["FAKE_CODEX_MODE"] = "slow"
        self.cfg.hosts["codex"].timeout_minutes = 0.002
        d = self.ready()
        self.poll(msg(message_id="om_test_cx7", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        self.wait_for_method("turn/start")
        time.sleep(0.2)
        d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_cx")["state"], sm.ATTENTION)
        self.assertEqual(len(self.bus_hints("超时")), 1)

    def test_max_concurrent_queues(self):
        os.environ["FAKE_CODEX_MODE"] = "slow"
        self.cfg.hosts["codex"].max_concurrent = 1
        self.roster = parse_roster({"session": [
            cx_roster()["session"][0],
            {**cx_roster()["session"][0], "key": "cy", "conversation_id": "thr_test_y",
             "chats": [{"chat_id": "oc_test_cy_p2p", "all_messages": True}]}]})
        d = self.ready()
        self.store.update_topic("tp_cy", onboarded_at=now_iso())
        self.poll(msg(message_id="om_test_c1", chat_id="oc_test_cx_p2p", chat_type="p2p"),
                  msg(message_id="om_test_c2", chat_id="oc_test_cy_p2p", chat_type="p2p"))
        r = d.dispatch_once()
        sent = {k: v for k, v in r["topics"].items() if v.get("sent")}
        queued = {k: v for k, v in r["topics"].items() if v.get("queued")}
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(queued), 1)
        self.assertIn("max_concurrent", next(iter(queued.values()))["queued"])

    def test_human_in_rollout_pauses_delivery(self):
        d = self.ready()
        now = datetime.now().astimezone().isoformat()
        self.write_rollout("thr_test_existing", [("2026-10-10T02:00:00Z", "user_message", "开场"),
                                                  (now, "user_message", "主人在应用里说话")])
        self.poll(msg(message_id="om_test_cx8", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_cx")["state"], sm.HUMAN_ATTACHED)
        self.assertEqual(self.codex_requests(), [])

    def test_recover_sending_uses_runs_and_rollout(self):
        d = self.ready()
        self.poll(msg(message_id="om_test_cx9", chat_id="oc_test_cx_p2p", chat_type="p2p"))
        self.store.record_dispatch("b_test_s1", "tp_cx", ["om_test_cx9"], state="sending")
        self.store.add_run("b_test_s1", "tp_cx", "codex", "thr_test_existing", "resume", "", "", "")
        self.store.update_run("b_test_s1", pid=999999, state="done", ended_at=now_iso())
        d._recover_sending()
        self.assertEqual(self.store.get_dispatch("b_test_s1")["state"], "sent")
        self.store.record_dispatch("b_test_s2", "tp_cx", [], state="sending")
        self.write_rollout("thr_test_existing", [("2026-10-10T02:00:00Z", "user_message", "[sheepdog] 批次 b_test_s2")])
        d._recover_sending()
        self.assertEqual(self.store.get_dispatch("b_test_s2")["state"], "context")  # 没有信号的批次补记为已送达
        self.store.record_dispatch("b_test_s3", "tp_cx", [], state="sending")
        d._recover_sending()
        self.assertEqual(self.store.get_dispatch("b_test_s3")["state"], "uncertain")

    def test_new_session_host_checks_and_shared_quota(self):
        self.cfg.hosts["codex"].ask_owner_before_new = False  # 「先问主人」另有 OwnerConsentTest
        d = self.ready()
        self.cfg.hosts["claude"].enabled = False
        with self.assertRaisesRegex(ValueError, "宿主 claude 没开"):
            d.new_session("cl1", "x", "y", [], host="claude")
        self.cfg.bus.max_new_sessions_per_day = 2
        r = d.new_session("cx1", "设计", "职责", [], host="codex", effort="xhigh")
        self.assertEqual(r["host"], "codex")
        t = self.store.get_topic("tp_cx1")
        self.assertEqual((t["host"], t["effort"], t["conversation_id"]), ("codex", "xhigh", r["conversation_id"]))
        start = next(x for x in self.codex_requests() if x.get("method") == "thread/start")
        self.assertEqual(start["params"]["threadSource"], "user")
        name = next(x for x in self.codex_requests() if x.get("method") == "thread/name/set")
        self.assertEqual(name["params"]["name"], "[managed] 设计")
        d.new_session("ag1", "普通", "职责", [])
        self.assertEqual(self.ag.created[-1][0], "[managed] 普通")
        with self.assertRaisesRegex(ValueError, "额度|上限|max_new_sessions_per_day"):
            d.new_session("cx2", "再开", "职责", [], host="codex")

    def test_cannot_change_host_of_existing_session(self):
        d = self.ready()
        d.roster = parse_roster(cx_roster(host="claude"))
        with self.assertLogs("sheepdog", "ERROR"):
            d.sync_roster()
        self.assertEqual(self.store.get_topic("tp_cx")["host"], "codex")
        # 换了会话（接手流程）才换宿主
        d.roster = parse_roster(cx_roster(host="claude", conversation_id="00000000-0000-4000-8000-0000000000aa"))
        d.sync_roster()
        self.assertEqual(self.store.get_topic("tp_cx")["host"], "claude")

    def test_spawn_codex_with_predecessor_receipt_block(self):
        os.environ["FAKE_CODEX_ANSWER"] = '已读完。\n```sheepdog-receipt\n{"status": "handled", "summary": "读完了"}\n```'
        roster = parse_roster(cx_roster(conversation_id="", predecessor_conversation_id="conv_test_old_ag"))
        d = self.disp(roster)
        d.sync_roster()
        rep = d.spawn("cx")
        t = self.store.get_topic("tp_cx")
        self.assertEqual(t["pending_batch_id"], rep["read_batch"])
        self.assertEqual(self.store.get_run(rep["read_batch"])["kind"], "new")  # 开场这一轮就是读前任的批次
        self.round(d)
        t = self.store.get_topic("tp_cx")
        self.assertEqual((t["state"], t["summary"]), (sm.ACTIVE, "读完了"))

    def test_retire_codex_predecessor_uses_its_host(self):
        # 前任是 Codex 线程（runs 里有记录），接手的是 Antigravity 会话
        self.store.add_run("x_test_old", "tp_cx", "codex", "thr_test_prev", "resume", "", "", "")
        self.store.update_run("x_test_old", state="done", ended_at=now_iso())
        os.environ["FAKE_CODEX_ANSWER"] = '```sheepdog-receipt\n{"status": "done", "summary": "交接完毕"}\n```'
        roster = parse_roster(cx_roster(host="antigravity", conversation_id="",
                                        predecessor_conversation_id="thr_test_prev", retire_predecessor=True))
        d = self.disp(roster)
        d.sync_roster()
        d.spawn("cx")
        self.wait(self.cx)
        retire = next(x for x in self.codex_requests() if x.get("method") == "turn/start")
        self.assertIn("退休通知", retire["params"]["input"][0]["text"])
        self.assertEqual(len(self.ag.created), 1)
        self.round(d)
        rows = self.store.conn.execute("SELECT * FROM messages WHERE topic_id='tp_cx'").fetchall()
        self.assertTrue(any("交接完毕" in (r["content"] or "") for r in rows))


    def _locked_run(self, bid: str, minutes_ago: float, state: str = "locked") -> None:
        ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        self.store.add_run(bid, "tp_cx", "codex", "thr_test_existing", "resume", "", "", "")
        self.store.update_run(bid, state=state, started_at=ts, ended_at=ts,
                              error_kind="locked" if state == "locked" else None)

    def test_lock_reminder_to_owner(self):
        d = self.ready()
        # 不满 60 分钟：不提醒
        self._locked_run("b_test_k1", 30)
        d._check_lock_streaks()
        self.assertEqual((self.notices, self.bus_hints("写锁冲突")), ([], []))
        # 同一段连续冲突满 60 分钟：总线提示一次、提醒主人一次
        self._locked_run("b_test_k0", 61)
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Codex 设计", self.notices[0])
        self.assertIn("FX-HOST-LOCKED host=Codex", self.notices[0])
        self.assertIn("一条不丢", self.notices[0])
        # 还锁着：不重复提醒
        self._locked_run("b_test_k2", 10)
        d._check_lock_streaks()
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(len(self.bus_hints("写锁冲突")), 1)
        # 解锁（一轮跑成功），之后又被锁满 60 分钟：重新计时；24 小时内同一会话不再提醒，过了 24 小时再提醒
        self._locked_run("b_test_k3", 9, state="done")
        self._locked_run("b_test_k4", 8)  # 新一段冲突，还不满 60 分钟
        d._check_lock_streaks()
        self.assertEqual(len(self.bus_hints("写锁冲突")), 1)
        self.store.update_run("b_test_k3", started_at=(datetime.now().astimezone() - timedelta(minutes=90))
                              .isoformat(timespec="seconds"))
        self.store.update_run("b_test_k4", started_at=(datetime.now().astimezone() - timedelta(minutes=65))
                              .isoformat(timespec="seconds"))
        d._check_lock_streaks()
        self.assertEqual(len(self.bus_hints("写锁冲突")), 2)  # 新一段冲突，总线照常提示
        self.assertEqual(len(self.notices), 1)  # 24 小时上限
        meta = json.loads(self.store.get_meta("lock_notice:thr_test_existing"))
        meta["at"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
        self.store.set_meta("lock_notice:thr_test_existing", json.dumps(meta))
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 2)
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 2)

    def test_lock_reminder_after_unlock_more_than_a_day_later(self):
        d = self.ready()
        self._locked_run("b_test_m1", 120)
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 1)
        meta = json.loads(self.store.get_meta("lock_notice:thr_test_existing"))
        meta["at"] = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
        self.store.set_meta("lock_notice:thr_test_existing", json.dumps(meta))
        self._locked_run("b_test_m2", 50, state="done")  # 解锁
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 1)
        self._locked_run("b_test_m3", 45)  # 再锁，不满 60 分钟不提醒
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 1)
        self.store.update_run("b_test_m3", started_at=(datetime.now().astimezone() - timedelta(minutes=61))
                              .isoformat(timespec="seconds"))
        self.store.update_run("b_test_m2", started_at=(datetime.now().astimezone() - timedelta(minutes=70))
                              .isoformat(timespec="seconds"))
        d._check_lock_streaks()
        self.assertEqual(len(self.notices), 2)


class CliNameTest(HostBase):
    CLI = "/tmp/sheep dog test/bin/sheepdog-test"

    def setUp(self):
        super().setUp()
        self.cfg.cli_name = self.CLI
        self.cfg.playbook_dir = str(REPO / "examples" / "playbook")  # 示例 playbook 里的 {{cli}} 也要换过来
        self.quoted = "'/tmp/sheep dog test/bin/sheepdog-test'"

    def assert_no_bare(self, text: str, has_cli: bool = True):
        self.assertNotIn("`sheepdog ", text)
        self.assertNotIn("$(sheepdog ", text)
        self.assertNotIn("{{cli}}", text)
        if has_cli:
            self.assertIn(f"`{self.quoted} ", text)

    def test_prompts_use_cli_name(self):
        from sheepdog.prompts import (adopted_bootstrap, batch_prompt, bootstrap_prompt, onboarding_prompt,
                                      retirement_prompt)
        self.assertEqual(self.cfg.cli_command, self.quoted)
        pb = Playbook.from_config(self.cfg)
        self.assertEqual(pb.cli, self.quoted)
        s = parse_roster(cx_roster(conversation_id="", predecessor_conversation_id="thr_test_pre")).sessions[0]
        texts = {
            "bus": bootstrap_prompt(pb, "总线", "总线", BUS_TOPIC_ID, None, "", [15, 30], 60),
            "onboarding": onboarding_prompt(pb, s, "o1", [15, 30], 60),
            "adopted": adopted_bootstrap(pb, s, "t", "h1", [15, 30], 60, "/x", "/y"),
            "retire": retirement_prompt(pb, "tp_x.prev", "前任", "r1", "接手", "tp_x", cli_host=True),
            "rules": standing_rules_managed(pb, s, [15, 30], 60),
        }
        self.store.upsert_message(msg(message_id="om_test_long", content="\n".join(["行"] * 30),
                                      reply_to="", chat_type="group"), "dispatch", "at_me", ["watch:7"], "tp_cx")
        row = self.store.get_message("om_test_long")
        inbox = [{"chat_name": "群", "chat_id": "oc_test_g", "unread": 1, "total": 2, "last_time": "x", "owner_last": ""}]
        texts["batch"] = batch_prompt(pb, "tp_cx", "b1", [row], inbox, "Codex 设计", max_lines=5, cli_host=True)
        ag = parse_roster(cx_roster(host="antigravity")).sessions[0]
        texts["ag_rules"] = standing_rules_managed(pb, ag, [15, 30], 60)
        texts["ag_batch"] = batch_prompt(pb, "tp_cx", "b2", [row], [], "Codex 设计")
        for name, text in texts.items():
            with self.subTest(name):
                self.assert_no_bare(text, has_cli=name != "retire")  # 命令行宿主的退休通知里只剩回执块，没有命令
        self.assertIn(f"`{self.quoted} receipt --topic tp_cx --batch b2", texts["ag_batch"])
        self.assertIn(f"`{self.quoted} receipt --topic tp_cx --batch <批次ID>", texts["ag_rules"])
        self.assertIn(f"`{self.quoted} watch-done --id 7`", texts["batch"])
        self.assertIn(f"`{self.quoted} show om_test_long`", texts["batch"])
        self.assertIn(f"`{self.quoted} inbox --chat", texts["batch"])
        self.assertIn(f"`{self.quoted} reply-card`", texts["batch"])  # batch_footer.md 的 {{cli}}
        self.assertIn("回执一律写成回执块", texts["retire"])  # 命令行宿主的回执提示
        self.assertIn(f"`{self.quoted} forward`", texts["bus"])  # bus.md 的 {{cli}}
        self.assertIn("`sheepdog-receipt`", texts["rules"])  # 围栏标记不是命令，不换

    def test_default_cli_name_unchanged(self):
        self.cfg.cli_name = "sheepdog"
        pb = Playbook.from_config(self.cfg)
        s = parse_roster(cx_roster(host="antigravity")).sessions[0]
        text = standing_rules_managed(pb, s, [15, 30], 60)
        self.assertIn("`sheepdog receipt --topic tp_cx", text)
        self.assertIn("`sheepdog watch`", text)

    def test_cli_host_receipt_only_via_block(self):
        """7.18 第三层实测：命令行宿主会话写不了状态目录，回执只给回执块这一种写法，不再让它先试回执命令。"""
        from sheepdog.prompts import adopted_bootstrap, batch_prompt, onboarding_prompt, retirement_prompt
        pb = Playbook.from_config(self.cfg)
        self.store.upsert_message(msg(message_id="om_test_rb"), "dispatch", "at_me", [], "tp_cx")
        row = self.store.get_message("om_test_rb")
        for host in ("codex", "claude"):
            with self.subTest(host):
                s = parse_roster(cx_roster(host=host)).sessions[0]
                sp = parse_roster(cx_roster(host=host, conversation_id="",
                                            predecessor_conversation_id="thr_test_pre")).sessions[0]
                batch = batch_prompt(pb, "tp_cx", "b1", [row], [], "Codex 设计", cli_host=True)
                texts = [batch, standing_rules_managed(pb, s, [15, 30], 60), onboarding_prompt(pb, s, "o1", [15, 30], 60),
                         adopted_bootstrap(pb, sp, "t", "h1", [15, 30], 60),
                         retirement_prompt(pb, "tp_x.prev", "前任", "r1", "接手", "tp_x", cli_host=True)]
                for text in texts:
                    self.assertNotIn(f"`{self.quoted} receipt", text)
                    self.assertNotIn("receipt --topic", text)
                    self.assertIn("```sheepdog-receipt", text)
                self.assertIn("在这一轮回答的最末尾附 ```sheepdog-receipt 回执块（内容同 receipt --json 的 JSON）", batch)
                self.assertIn("不算受阻", batch)
                # 其他命令照旧列出
                self.assertIn(f"`{self.quoted} watch --topic tp_cx", texts[1])
                self.assertIn(f"`{self.quoted} watch-done --id <id>`", texts[1])
        ag_batch = batch_prompt(pb, "tp_cx", "b1", [row], [], "Codex 设计")
        self.assertIn(f"`{self.quoted} receipt --topic tp_cx --batch b1 --json '...'`", ag_batch)
        self.assertNotIn("```sheepdog-receipt", ag_batch)
        ag = parse_roster(cx_roster(host="antigravity")).sessions[0]
        self.assertIn(f"`{self.quoted} receipt --topic tp_cx --batch o1 --json '{{\"status\":\"handled\",\"summary\":\"...\"}}'`",
                      onboarding_prompt(pb, ag, "o1", [15, 30], 60))
        self.assertIn(f"交接回执：`{self.quoted} receipt --topic tp_x.prev --batch r1",
                      retirement_prompt(pb, "tp_x.prev", "前任", "r1", "接手", "tp_x"))

    def test_engine_and_cli_texts(self):
        d = Dispatcher(self.cfg, self.store, {"antigravity": FakeSink()})
        d._bus_hint("x", "稍后用 `sheepdog retire --topic tp_x` 补发")
        row = self.store.conn.execute("SELECT content FROM messages WHERE topic_id=?", (BUS_TOPIC_ID,)).fetchone()
        self.assertIn(f"`{self.quoted} retire --topic tp_x`", row["content"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sheepdog.actions.is_resident", return_value=False):
            cli._queued(mock.Mock(dry_run=False), self.store, self.cfg, "push_rules", {"topic": ""})
        self.assertIn(f"`{self.quoted} actions`", out.getvalue())

    def test_config_doctor_and_reload(self):
        cfg_path = self.root / "config.toml"
        cfg_path.write_text(f'state_dir = "{self.root / "state"}"\ncli_name = "  "\n', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "cli_name"):
            load_config()
        cfg_path.write_text(f'state_dir = "{self.root / "state"}"\n', encoding="utf-8")
        old = load_config()
        self.assertEqual(old.cli_name, "sheepdog")
        self.assertIn("（默认）", cli.cli_name_report(old))
        wrapper = self.root / "sheep dog" / "sheepdog-test"
        wrapper.parent.mkdir()
        cfg_path.write_text(f'state_dir = "{self.root / "state"}"\ncli_name = "{wrapper}"\n', encoding="utf-8")
        st = Store(old.db_path)
        d = Dispatcher(old, st, {"antigravity": FakeSink()})
        new = cli._reload_config(old, mock.Mock(), st, FakeSource([]), Collector(old, st, FakeSource([])), d)
        self.assertEqual(new.cli_name, str(wrapper))  # 热加载：下一轮生效
        self.assertEqual(d.playbook.cli, f"'{wrapper}'")
        self.assertIn("✗ 找不到", cli.cli_name_report(new))
        wrapper.write_text("#!/bin/sh\n")
        self.assertIn(f"-> {wrapper} ✓", cli.cli_name_report(new))
        st.close()


class OwnerConsentTest(HostBase):
    """7.18（19:0x）：新开 Codex / Claude 会话前先问主人，new-session 必须带核对过的 --quote。"""
    SAID = "可以，用 Codex 开一个做 API 限流设计"

    def setUp(self):
        super().setUp()
        os.environ["ANTIGRAVITY_APP_DATA_DIR"] = str(self.home / "ag")
        self.ag = FakeSink()
        self.cx = self.codex()
        self.d = Dispatcher(self.cfg, self.store, {"antigravity": self.ag, "codex": self.cx}, parse_roster({}),
                            SecurityConfig())
        self.d.ensure_bus_topic()
        self.store.update_topic(BUS_TOPIC_ID, conversation_id="conv_test_bus")
        self.bus_says(self.SAID, minutes_ago=3)

    def bus_says(self, text: str, minutes_ago: float) -> None:
        """总线 transcript：第 0 步是开场（不算），之后是主人亲口说的。"""
        ts = lambda m: (datetime.now(tz=timezone.utc) - timedelta(minutes=m)).isoformat()  # noqa: E731
        p = transcript_path("conv_test_bus")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            if p.stat().st_size == 0:
                f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": ts(600),
                                    "content": "总线开场（sheepdog 写的）"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "created_at": ts(minutes_ago),
                                "content": f"<USER_REQUEST>{text}</USER_REQUEST>"}, ensure_ascii=False) + "\n")

    def opening(self) -> str:
        self.wait(self.cx)
        return next(r for r in self.codex_requests() if r.get("method") == "turn/start")["params"]["input"][0]["text"]

    def test_without_quote_rejected(self):
        with self.assertRaises(ValueError) as e:
            self.d.new_session("cx1", "限流设计", "职责", [], host="codex")
        self.assertIn("新开 Codex 会话要先问主人：发『需要你定』写清为什么用、做什么，拿到主人同意后，用 --quote 带上他的原话再开",
                      str(e.exception))
        self.assertIsNone(self.store.get_topic("tp_cx1"))
        self.assertEqual(self.codex_requests(), [])

    def test_unverified_quote_rejected(self):
        for bad in ("主人没说过这句", ""):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "要先问主人"):
                self.d.new_session("cx1", "限流设计", "职责", [], host="codex", quote=bad)
        # 主人的话，但超过回看时长
        self.bus_says("很久以前说的同意", minutes_ago=60 * 30)
        with self.assertRaisesRegex(ValueError, "核对没通过"):
            self.d.new_session("cx1", "限流设计", "职责", [], host="codex", quote="很久以前说的同意")
        self.assertEqual(self.codex_requests(), [])

    def test_quote_from_bus_passes_and_goes_into_opening(self):
        r = self.d.new_session("cx1", "限流设计", "职责", [], host="codex", quote="用 Codex 开一个")
        self.assertEqual(self.store.get_topic("tp_cx1")["conversation_id"], r["conversation_id"])
        text = self.opening()
        self.assertIn("### 主人同意开这个会话\n✅ 主人原话（已核对：主人在总线里亲口说过）：\n> 用 Codex 开一个", text)

    def test_quote_from_owner_lark_reply_passes(self):
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        self.store.upsert_message(msg(message_id="om_test_own1", sender_id=ME, chat_type="p2p", content="同意，开 Codex 吧",
                                      create_time=now), "dispatch", "owner_reply", ["owner_verified"], BUS_TOPIC_ID)
        # 主人账号发给别人的普通消息（没有 owner_verified）不算
        self.store.upsert_message(msg(message_id="om_test_own2", sender_id=ME, content="我跟同事说的：开 Claude",
                                      create_time=now), "self", "self", [], None)
        with self.assertRaisesRegex(ValueError, "核对没通过"):
            self.d.new_session("cx2", "x", "y", [], host="codex", quote="开 Claude")
        self.d.new_session("cx1", "限流设计", "职责", [], host="codex", quote="同意，开 Codex 吧")
        self.assertIn("✅ 主人原话（已核对：主人本人账号在飞书上的回复）：\n> 同意，开 Codex 吧", self.opening())

    def test_switch_off_needs_no_quote(self):
        self.cfg.hosts["codex"].ask_owner_before_new = False  # 热加载：dispatcher 每轮用新配置
        self.d.new_session("cx1", "限流设计", "职责", [], host="codex")
        self.assertNotIn("主人同意开这个会话", self.opening())

    def test_antigravity_spawn_and_forward_unaffected(self):
        self.d.new_session("ag1", "普通", "职责", [])
        self.assertEqual(self.ag.created[-1][0], "[managed] 普通")
        self.assertNotIn("主人同意开这个会话", self.ag.created[-1][1])
        # 名册里主人亲手写的 codex 会话：spawn 不走这道门
        d = Dispatcher(self.cfg, self.store, {"antigravity": self.ag, "codex": self.cx},
                       parse_roster(cx_roster(conversation_id="")), SecurityConfig())
        d.sync_roster()
        rep = d.spawn("cx")
        self.assertTrue(rep["conversation_id"].startswith("thr_test_"))
        # 往已有的 codex 会话 forward 不用再问
        from sheepdog.engine import forward_messages
        forward_messages(self.store, "tp_cx", [], note="补充一句")
        rows = self.store.conn.execute("SELECT content FROM messages WHERE topic_id='tp_cx'").fetchall()
        self.assertTrue(any("补充一句" in r["content"] for r in rows))

    def test_queued_path_checks_before_enqueue(self):
        (self.root / "config.toml").write_text(
            f'self_open_id = "{ME}"\nstate_dir = "{self.root / "state"}"\nplaybook_dir = "{FIXTURES}"\n'
            f'sink = "dryrun"\n[hosts.codex]\nenabled = true\nworkdir = "{self.work}"\n', encoding="utf-8")
        args = ["new-session", "--key", "cxq", "--title", "限流设计", "--duty", "职责", "--host", "codex"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sheepdog.actions.is_resident", return_value=False):
            self.assertEqual(cli.main(args), 2)
            self.assertEqual(cli.main(args + ["--quote", "没说过"]), 2)
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)
            self.assertEqual(cli.main(args + ["--quote", "用 Codex 开一个"]), 0)
        self.assertIn("要先问主人", err.getvalue())
        rows = self.store.conn.execute("SELECT * FROM actions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["args_json"])["quote"], "用 Codex 开一个")
        self.assertEqual(self.codex_requests(), [])  # 入队不建会话


# ======================= 命令行 =======================
class CliTest(HostBase):
    def write_cfg(self, extra: str = "") -> None:
        (self.root / "config.toml").write_text(
            f'self_open_id = "{ME}"\nstate_dir = "{self.root / "state"}"\nplaybook_dir = "{FIXTURES}"\n'
            f'sink = "dryrun"\n' + extra, encoding="utf-8")

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_inject_only_when_source_disabled(self):
        self.write_cfg()
        code, _, err = self.run_cli("inject", "--text", "测试消息")
        self.assertEqual(code, 2)
        self.assertIn("[source] enabled = false", err)
        self.write_cfg("[source]\nenabled = false\n")
        code, out, _ = self.run_cli("inject", "--text", "请帮忙看下", "--chat", "oc_test_inj")
        self.assertEqual(code, 0)
        st = Store(self.cfg.db_path)
        rows = st.conn.execute("SELECT * FROM messages WHERE message_id LIKE 'om_test_inject_%'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["content"], rows[0]["route"], rows[0]["chat_id"]), ("请帮忙看下", "dispatch", "oc_test_inj"))
        self.assertIn(rows[0]["message_id"], out)
        st.close()

    def test_source_disabled_uses_null_source(self):
        self.write_cfg("[source]\nenabled = false\n")
        cfg = load_config()
        self.assertEqual(type(cli._source(cfg)).__name__, "NullSource")
        self.assertIsNone(cli._notifier(cfg, cli._source(cfg), mock.Mock(dry_run=False)))

    def test_sinks_per_host(self):
        self.write_cfg(f'[hosts.codex]\nenabled = true\nworkdir = "{self.work}"\n')
        cfg = load_config()
        st = Store(cfg.db_path)
        sinks = cli._sinks(cfg, st, dry=False)
        self.assertEqual(set(sinks), {"antigravity", "codex"})
        self.assertIsInstance(sinks["codex"], CodexSink)
        dry = cli._sinks(cfg, st, dry=True)
        self.assertEqual(dry["codex"].host, "codex")
        st.close()

    def test_show_topic_and_sessions_columns(self):
        self.write_cfg(f'[hosts.codex]\nenabled = true\nworkdir = "{self.work}"\nmodel = "gpt-6-astra"\n'
                       f'effort = "high"\n')
        st = Store(self.cfg.db_path)
        st.create_topic("tp_cxs", "设计", "dynamic", "职责", sm.ACTIVE, "codex", "", "xhigh")
        st.update_topic("tp_cxs", conversation_id="thr_test_show")
        st.add_run("b_test_sh", "tp_cxs", "codex", "thr_test_show", "resume", "", "", "")
        st.update_run("b_test_sh", state="done", denials=json.dumps(["item/fileChange/requestApproval"]))
        st.close()
        self.write_rollout("thr_test_show", [("2026-10-10T02:00:00Z", "user_message", "开场"),
                                             ("2026-10-10T02:00:09Z", "agent_message", "方案是 A")])
        code, out, _ = self.run_cli("show", "--topic", "tp_cxs")
        self.assertEqual(code, 0)
        self.assertIn("方案是 A", out)
        self.assertIn("Codex 应用里找同名线程「[managed] 设计」", out)
        code, out, _ = self.run_cli("sessions")
        self.assertIn("宿主=codex", out)
        self.assertIn("模型=gpt-6-astra（默认）", out)
        self.assertIn("档位=xhigh", out)
        self.assertIn("被拒的工具: item/fileChange/requestApproval", out)

    def test_doctor_host_report(self):
        self.cfg.hosts["claude"].settings = "s.json"
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text('sandbox_mode = "read-only"\n', encoding="utf-8")
        os.environ["FAKE_CLAUDE_MODE"] = "notlogged"
        text = "\n".join(cli.host_report(self.cfg, self.store))
        self.assertIn(f"workdir {self.work}", text)
        self.assertIn("Codex 应用侧边栏只列登记过的项目下的线程；新建的线程要重开应用才看得到", text)
        self.assertIn("sandbox_mode = read-only", text)
        self.assertIn("发不了飞书", text)
        self.assertIn("没登录", text)
        self.assertIn("--settings s.json", text)
        self.cfg.hosts["codex"].enabled = False
        self.assertIn("Codex: 未启用", "\n".join(cli.host_report(self.cfg, self.store)))

    def test_reload_keeps_restart_keys_and_updates_sink(self):
        self.write_cfg(f'[hosts.codex]\nenabled = true\nworkdir = "{self.work}"\nmodel = "a"\ntimeout_minutes = 60\n')
        old = load_config()
        st = Store(old.db_path)
        sinks = cli._sinks(old, st, dry=False)
        d = Dispatcher(old, st, sinks)
        col = Collector(old, st, FakeSource([]))
        self.write_cfg(f'[hosts.codex]\nenabled = true\nworkdir = "{self.root}"\nmodel = "b"\ntimeout_minutes = 5\n'
                       f'max_concurrent = 1\n')
        with self.assertLogs(level="WARNING") as logs:
            new = cli._reload_config(old, mock.Mock(), st, FakeSource([]), col, d)
        self.assertTrue(any("workdir 改了" in x for x in logs.output))
        self.assertEqual(new.hosts["codex"].workdir, str(self.work))
        self.assertEqual((new.hosts["codex"].model, sinks["codex"].hcfg.timeout_minutes,
                          sinks["codex"].hcfg.max_concurrent), ("b", 5, 1))
        st.close()

    def test_prune_runs(self):
        self.cfg.runs_dir.mkdir(parents=True)
        self.store.add_run("b_test_old", "tp_x", "codex", "thr_test_x", "resume", "", "", "")
        self.store.update_run("b_test_old", state="done", ended_at="2000-01-01T00:00:00+08:00")
        self.store.add_run("b_test_live", "tp_x", "codex", "thr_test_x", "resume", "", "", "")
        for name in ("b_test_old.out", "b_test_live.out"):
            f = self.cfg.runs_dir / name
            f.write_text("x")
            os.utime(f, (0, 0))
        out = self.store.prune(7, None, self.cfg.runs_dir)
        self.assertEqual((out["runs"], out["run_files"]), (1, 1))
        self.assertIsNone(self.store.get_run("b_test_old"))
        self.assertTrue((self.cfg.runs_dir / "b_test_live.out").exists())


if __name__ == "__main__":
    unittest.main()
