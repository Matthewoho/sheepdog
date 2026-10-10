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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.config import ConfigError, HostConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, write_receipt
from sheepdog.prompts import is_sheepdog_prompt, standing_rules_managed
from sheepdog.roster import RosterError, check_hosts, parse_roster
from sheepdog.sink import SinkError
from sheepdog.sink.agentapi import DryRunSink
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
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CODEX_LOG": str(self.codex_log), "FAKE_CLAUDE_LOG": str(self.claude_log),
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
