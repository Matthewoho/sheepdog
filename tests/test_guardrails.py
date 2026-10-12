"""7.19 护栏：K2 检测到宿主压缩后下一批重推完整规则；K1 每批末尾附 reminders/<key>.md；K3 会话跑了危险命令时报警。

会话记录一律是临时目录里合成的 JSONL（格式照 2026-10-11 核过的 Antigravity transcript / Codex rollout），
发送用假的 sink 和假的 notifier。"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, guard
from sheepdog.config import DEFAULT_DANGER_PATTERNS, DEFAULT_IGNORE_PATTERNS, AuditConfig, ConfigError, load_config
from sheepdog.engine import BUS_TOPIC_ID, Dispatcher
from sheepdog.prompts import DEFAULT_COMPACTION_NOTE, RULES_UPDATE_HEADER
from sheepdog.sink.agentapi import transcript_path

from test_roster import FIXTURES, ME, REPO, Base, FakeSink, msg

CHECKPOINT = {"step_index": 5635, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE",
              "created_at": "2026-10-10T15:59:00Z",
              "content": "# Resuming from a compaction\n\nYou are continuing work on a task ..."}


def run_command(cmd: str, step: int = 5430, at: str = "2026-10-10T14:59:28Z", cwd: str = "/tmp/fx") -> dict:
    """Antigravity 的命令调用：CommandLine、Cwd 都是 JSON 编码过的字符串。"""
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": at,
            "thinking": "fx", "tool_calls": [{"name": "run_command", "args": {
                "CommandLine": json.dumps(cmd), "Cwd": json.dumps(cwd), "WaitMsBeforeAsync": 500,
                "toolAction": "fx", "toolSummary": "fx"}}]}


class CliSink(FakeSink):
    """命令行宿主的假 sink：记录文件由测试指定。"""

    def __init__(self, files: dict):
        super().__init__()
        self.files = files

    def send_message(self, cid, content, **_kw):
        super().send_message(cid, content)

    def busy(self, cid):
        return ""

    def record_files(self, cid):
        return [self.files[cid]] if cid in self.files else []


class GuardBase(Base):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(Path(self.tmp.name) / "app")})
        self.env.start()
        self.pdir = Path(self.tmp.name) / "playbook"
        shutil.copytree(FIXTURES, self.pdir)
        self.cfg.playbook_dir = str(self.pdir)
        self.notices: list[str] = []
        self.d = self.make()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def make(self, sinks=None) -> Dispatcher:
        return Dispatcher(self.cfg, self.store, sinks or self.sink, self.roster, notifier=self.notices.append)

    def append(self, cid: str, *rows, raw: str = "") -> Path:
        p = transcript_path(cid)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write(raw)
        return p

    def deliver_to(self, topic, mid):
        chat = "oc_test_alpha_p2p" if topic == "tp_alpha" else "oc_test_stranger"
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id=chat)])
        self.d.dispatch_once()
        conv = "conv_test_alpha" if topic == "tp_alpha" else "conv_test_bus"
        text = self.sink.to(conv)[-1]
        self.assertIn(mid, text)
        self.ack(topic)
        return text


# ======================= K2：压缩后重推规则 =======================
class CompactionTest(GuardBase):
    def test_bus_checkpoint_next_batch_has_full_rules_once(self):
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to(BUS_TOPIC_ID, "om_test_k0"))
        self.append("conv_test_bus", {"type": "USER_INPUT", "content": "hi"}, CHECKPOINT)
        with self.assertLogs("sheepdog", "INFO") as logs:
            text = self.deliver_to(BUS_TOPIC_ID, "om_test_k1")
        self.assertTrue(any("检测到压缩" in m for m in logs.output))
        self.assertTrue(text.startswith(RULES_UPDATE_HEADER))
        note = text.index("FX-COMPACTED title=Lark 信号·总线")
        self.assertLess(note, text.index("FX-SECURITY"))           # 那句话在规则前面
        self.assertLess(text.index("FX-BUS"), text.index("[sheepdog] 新信号批次"))
        t = self.store.get_topic(BUS_TOPIC_ID)
        self.assertIsNone(t["compacted_at"])
        self.assertEqual(self.d.rules_status(t), "最新")
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to(BUS_TOPIC_ID, "om_test_k2"))  # 只带一次
        # 再压缩一次，又带一次
        self.append("conv_test_bus", dict(CHECKPOINT, step_index=6000))
        self.assertTrue(self.deliver_to(BUS_TOPIC_ID, "om_test_k3").startswith(RULES_UPDATE_HEADER))

    def test_status_shows_compaction_until_delivered(self):
        self.append("conv_test_bus", CHECKPOINT)
        self.d.dispatch_once()  # 没有待推消息：只记下来，不单独发
        t = self.store.get_topic(BUS_TOPIC_ID)
        self.assertTrue(t["compacted_at"])
        self.assertIsNone(t["rules_hash"])
        self.assertIn("被压缩过", self.d.rules_status(t))

    def test_non_compaction_checkpoint_ignored(self):
        self.append("conv_test_bus", dict(CHECKPOINT, content="# Some other checkpoint"),
                    {"type": "PLANNER_RESPONSE", "content": "compaction 这个词出现在正文里"})
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to(BUS_TOPIC_ID, "om_test_k4"))

    def test_default_note_when_playbook_file_missing(self):
        (self.pdir / "rules_after_compaction.md").unlink()
        self.append("conv_test_bus", CHECKPOINT)
        text = self.deliver_to(BUS_TOPIC_ID, "om_test_k5")
        self.assertLess(text.index(DEFAULT_COMPACTION_NOTE), text.index("FX-SECURITY"))

    def test_first_sight_starts_at_end(self):
        # alpha 是名册里登记的现有会话：第一次看到它的记录时从末尾开始，旧的压缩点不算
        self.append("conv_test_alpha", CHECKPOINT)
        self.assertNotIn(RULES_UPDATE_HEADER, self.deliver_to("tp_alpha", "om_test_k6"))
        self.append("conv_test_alpha", dict(CHECKPOINT, step_index=9))
        text = self.deliver_to("tp_alpha", "om_test_k7")
        self.assertTrue(text.startswith(RULES_UPDATE_HEADER))
        self.assertIn("FX-COMPACTED title=Alpha 需求", text)
        self.assertIn("FX-ONBOARDING", text)

    def test_partial_line_waits_and_shrink_rereads(self):
        self.deliver_to(BUS_TOPIC_ID, "om_test_k8")
        line = json.dumps(CHECKPOINT)
        p = self.append("conv_test_bus", raw=line[:40])        # 还没写完的一行
        self.d.dispatch_once()
        self.assertFalse(self.store.get_topic(BUS_TOPIC_ID)["compacted_at"])
        self.append("conv_test_bus", raw=line[40:] + "\n")     # 写完了
        self.d.dispatch_once()
        self.assertTrue(self.store.get_topic(BUS_TOPIC_ID)["compacted_at"])
        self.store.update_topic(BUS_TOPIC_ID, compacted_at=None, rules_hash=self.d.rules_hash(
            self.store.get_topic(BUS_TOPIC_ID)))
        short = json.dumps(dict(CHECKPOINT, content="# Resuming from a compaction"))
        self.assertLess(len(short), len(line))
        p.write_text(short + "\n", encoding="utf-8")           # 文件变短（重写）：从头读
        self.d.dispatch_once()
        self.assertTrue(self.store.get_topic(BUS_TOPIC_ID)["compacted_at"])

    def test_push_rules_carries_note(self):
        self.append("conv_test_bus", CHECKPOINT)
        self.d.dispatch_once()
        self.assertEqual(self.d.push_rules(BUS_TOPIC_ID)[BUS_TOPIC_ID], "已发送")
        last = self.sink.to("conv_test_bus")[-1]
        self.assertTrue(last.startswith(f"[sheepdog] {RULES_UPDATE_HEADER}"))
        self.assertLess(last.index("FX-COMPACTED"), last.index("FX-SECURITY"))
        self.assertIsNone(self.store.get_topic(BUS_TOPIC_ID)["compacted_at"])

    def test_codex_and_claude_formats(self):
        self.assertTrue(guard.is_compaction("codex", {"type": "response_item", "payload": {
            "type": "compaction", "encrypted_content": "fx", "id": "fx"}}))
        self.assertTrue(guard.is_compaction("codex", {"type": "compacted", "payload": {"message": ""}}))
        self.assertFalse(guard.is_compaction("codex", {"type": "response_item", "payload": {"type": "message"}}))
        self.assertTrue(guard.is_compaction("claude", {"type": "system", "subtype": "compact_boundary"}))
        self.assertFalse(guard.is_compaction("antigravity", {"type": "response_item", "payload": {"type": "compaction"}}))

    def test_codex_session_rollout(self):
        rollout = Path(self.tmp.name) / "codex" / "rollout-2026-10-10T00-00-00-thr_test_1.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("", encoding="utf-8")
        cli_sink = CliSink({"thr_test_1": rollout})
        self.d = self.make({"antigravity": self.sink, "codex": cli_sink})
        self.store.create_topic("tp_cx", "Codex 会话", "dynamic", "fx", "active", "codex")
        self.store.update_topic("tp_cx", conversation_id="thr_test_1", anchors_json='["oc_test_cx:all"]')
        self.d._read_from_start("tp_cx", "thr_test_1")
        with rollout.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-10-10T15:00:00Z", "type": "response_item",
                                "payload": {"type": "compaction", "encrypted_content": "fx"}}) + "\n")
        self.d.dispatch_once()
        t = self.store.get_topic("tp_cx")
        self.assertTrue(t["compacted_at"])
        self.assertIsNone(t["rules_hash"])


# ======================= K1：每批附红线 =======================
class ReminderTest(GuardBase):
    def write(self, key, text):
        d = self.pdir / "reminders"
        d.mkdir(exist_ok=True)
        (d / f"{key}.md").write_text(text, encoding="utf-8")

    def test_appended_at_very_end(self):
        self.write("bus", "<!-- 维护者注释 -->FX-REMINDER-BUS {{session_title}}")
        self.write("alpha", "FX-REMINDER-ALPHA {{topic_id}}")
        bus = self.deliver_to(BUS_TOPIC_ID, "om_test_r1")
        self.assertEqual(bus.splitlines()[-1], "FX-REMINDER-BUS Lark 信号·总线")
        self.assertLess(bus.index("FX-FOOTER"), bus.index("FX-REMINDER-BUS"))
        self.assertNotIn("维护者注释", bus)
        self.assertNotIn("FX-REMINDER-ALPHA", bus)
        alpha = self.deliver_to("tp_alpha", "om_test_r2")
        self.assertEqual(alpha.splitlines()[-1], "FX-REMINDER-ALPHA tp_alpha")

    def test_missing_means_nothing(self):
        text = self.deliver_to(BUS_TOPIC_ID, "om_test_r3")
        self.assertTrue(text.splitlines()[-1].startswith("FX-FOOTER"))

    def test_not_in_openings(self):
        self.write("bus", "FX-REMINDER-BUS")
        self.write("deploy", "FX-REMINDER-DEPLOY")
        self.d.new_session("deploy", "发布排查", "FX 职责", [("oc_test_dall", True)])
        for title, prompt in self.sink.created:
            self.assertNotIn("FX-REMINDER", prompt, title)
        # 总线新开的会话用它自己的 key
        self.assertEqual(self.d.reminder(self.store.get_topic("tp_deploy")), "FX-REMINDER-DEPLOY")
        # 规则更新（push-rules）也不附
        self.d.push_rules(BUS_TOPIC_ID)
        self.assertNotIn("FX-REMINDER", self.sink.to("conv_test_bus")[-1])

    def test_key_must_be_safe(self):
        self.assertEqual(Dispatcher.reminder_key(BUS_TOPIC_ID), "bus")
        self.assertEqual(Dispatcher.reminder_key("tp_alpha"), "alpha")
        self.assertEqual(Dispatcher.reminder_key("tp_../x"), "")

    def test_doctor_lists_reminders_and_audit(self):
        self.write("bus", "FX")
        self.write("ghost", "FX")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nplaybook_dir = "{self.pdir}"\n'
                            '[audit]\nalso_notify = ["tp_release"]\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            cli.main(["doctor"])
        text = out.getvalue()
        self.assertIn("每批红线:", text)
        self.assertIn("2 个会话：bus, ghost", text)
        self.assertIn("没有对应的会话（key 写错了？）：ghost", text)
        self.assertIn(f"命令审计: 审 tp_bus，{len(DEFAULT_DANGER_PATTERNS)} 条危险命令、"
                      f"{len(DEFAULT_IGNORE_PATTERNS)} 条忽略规则", text)
        self.assertIn("提示 tp_release", text)


# ======================= K3：危险命令报警 =======================
class AuditTest(GuardBase):
    def setUp(self):
        super().setUp()
        self.cfg.audit = AuditConfig(also_notify=["tp_alpha"])

    def hits(self):
        return self.store.conn.execute("SELECT * FROM audit_hits ORDER BY id").fetchall()

    def test_bus_command_alerts_owner_offender_and_also_notify(self):
        self.deliver_to(BUS_TOPIC_ID, "om_test_a0")
        self.append("conv_test_bus", run_command(
            "gcloud builds triggers run 0980fx \\\n  --project=fx-project --region=asia-east1 --branch=main", step=5430))
        with self.assertLogs("sheepdog", "WARNING"):
            self.d.dispatch_once()
        # 1. 主人：一条私聊，写明会话、时间（本机时区）、命令（续行合并）
        self.assertEqual(len(self.notices), 1)
        n = self.notices[0]
        self.assertTrue(n.startswith("🐕 汇报 · 会话跑了危险命令 [tp_bus]"))
        hhmm = datetime.fromisoformat("2026-10-10T14:59:28+00:00").astimezone().strftime("%H:%M")
        self.assertIn(f"- {hhmm} `gcloud builds triggers run 0980fx --project=fx-project --region=asia-east1 "
                      f"--branch=main`", n)
        self.assertIn("已告诉 tp_alpha 别重复操作", n)
        # 2. 犯规的会话：这一轮就收到系统提示，措辞来自 audit_offender.md
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn("危险命令提醒", bus)
        self.assertIn("FX-AUDIT-OFFENDER title=Lark 信号·总线 topic=tp_bus", bus)
        self.assertIn("gcloud builds triggers run 0980fx", bus)
        # 3. also_notify：措辞来自 audit_notify.md
        alpha = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("FX-AUDIT-NOTIFY title=Alpha 需求 offender=Lark 信号·总线 (tp_bus)", alpha)
        self.assertIn("「Lark 信号·总线」（topic `tp_bus`）跑了下面的命令", alpha)

    def test_dedup_by_step_and_safe_commands_ignored(self):
        p = self.append("conv_test_bus", run_command("ls -la && sheepdog sessions", step=1),
                        run_command("kubectl apply -f fx.yaml", step=2))
        self.d.dispatch_once()
        self.assertEqual([h["step"] for h in self.hits()], ["2:0"])
        # 偏移被重置（比如换了路径又换回来）：同一个 step 不再报
        self.store.set_cursor(BUS_TOPIC_ID, guard.AUDIT, "conv_test_bus", "", 0)
        self.d.dispatch_once()
        self.assertEqual(len(self.hits()), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertTrue(p.exists())

    def test_merge_only_repeats_of_reported_commands(self):
        """合并只合并已经报过的同一条命令：10 分钟内新出现的命令马上再报（写明第几次），报过的命令再跑不再报。"""
        self.append("conv_test_bus", run_command("gh pr merge 12 --squash", step=1),
                    run_command("gh pr merge 12 --squash", step=2), run_command("gh workflow run deploy.yml", step=3))
        self.d.dispatch_once()
        self.assertEqual(len(self.notices), 1)                 # 同一轮三条合并成一条
        self.assertIn("`gh pr merge 12 --squash`（2 次）", self.notices[0])
        self.assertIn("`gh workflow run deploy.yml`", self.notices[0])
        self.assertIn("这是这个会话 10 分钟内第 1 次报警", self.notices[0])
        stops = lambda: [r["content"] for r in self.store.conn.execute(  # noqa: E731
            "SELECT content FROM messages WHERE topic_id=? AND reason='audit_offender' ORDER BY rowid",
            (BUS_TOPIC_ID,))]
        self.assertEqual(len(stops()), 1)
        # 10 分钟内出现一条没报过的命令：马上再报，犯规会话再收一条停手提示
        self.append("conv_test_bus", run_command("kubectl delete pod fx-1", step=4))
        self.d.dispatch_once()
        self.assertEqual(len(self.notices), 2)
        self.assertIn("kubectl delete pod fx-1", self.notices[1])
        self.assertNotIn("gh pr merge", self.notices[1])
        self.assertIn("这是这个会话 10 分钟内第 2 次报警", self.notices[1])
        self.assertEqual(len(stops()), 2)
        self.assertIn("第 2 次报警", stops()[-1])
        # 报过的同一条命令又跑：并进之前的报警，不再报、不再排提示
        self.append("conv_test_bus", run_command("gh pr merge 12 --squash", step=5))
        self.d.dispatch_once()
        self.assertEqual(len(self.notices), 2)
        self.assertEqual(len(stops()), 2)
        self.assertEqual(self.store.pending_audit_hits(), [])
        merged = self.hits()[-1]
        self.assertEqual((merged["merged"], merged["alert_id"]), (1, self.hits()[0]["id"]))
        # 10 分钟以后同一条命令再跑：再报一次，从第 1 次数起
        past = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE audit_hits SET notified_at=? WHERE notified_at IS NOT NULL", (past,))
        self.store.conn.commit()
        self.append("conv_test_bus", run_command("gh pr merge 12 --squash", step=6))
        self.d.dispatch_once()
        self.assertEqual(len(self.notices), 3)
        self.assertIn("gh pr merge 12 --squash", self.notices[2])
        self.assertIn("第 1 次报警", self.notices[2])

    def test_only_audited_topics(self):
        self.append("conv_test_alpha", {"type": "USER_INPUT"})   # 第一次看到：从末尾开始
        self.d.dispatch_once()
        self.append("conv_test_alpha", run_command("gh pr merge 1"))
        self.d.dispatch_once()
        self.assertEqual(self.hits(), [])
        self.cfg.audit = AuditConfig(topics=["tp_bus", "tp_alpha"])  # 热加载：下一轮生效
        self.d.dispatch_once()  # 第一次审这个会话：从末尾开始，之前跑的那条不补报
        self.append("conv_test_alpha", run_command("gh pr merge 2", step=7))
        self.d.dispatch_once()
        self.assertEqual([h["command"] for h in self.hits()], ["gh pr merge 2"])

    def test_disabled_by_empty_patterns(self):
        self.cfg.audit = AuditConfig(danger_patterns=[])
        self.append("conv_test_bus", run_command("gh pr merge 3"))
        self.d.dispatch_once()
        self.assertEqual(self.hits(), [])
        self.assertEqual(self.notices, [])

    def test_new_session_audited_from_first_round(self):
        self.cfg.audit = AuditConfig(topics=["tp_deploy"])
        self.d.new_session("deploy", "发布排查", "FX 职责", [("oc_test_dall", True)])
        cid = self.store.get_topic("tp_deploy")["conversation_id"]
        self.append(cid, run_command("gcloud run services update-traffic fx --to-latest", step=3))
        self.d.dispatch_once()
        self.assertEqual(len(self.hits()), 1)
        self.assertIn("[tp_deploy]", self.notices[0])

    def test_no_notifier_still_prompts_sessions(self):
        self.d.notifier = None
        self.append("conv_test_bus", run_command("gcloud secrets versions access latest --secret=fx-config"))
        self.d.dispatch_once()
        self.assertIn("FX-AUDIT-OFFENDER", self.sink.to("conv_test_bus")[-1])
        self.assertIn("--secret=***", self.sink.to("conv_test_bus")[-1])

    # 真实误报（10-10 总线记录，去掉了真实 ID、人名、服务名）
    HELP = "gcloud builds approve --help 2>&1 | head -n 20"
    PKM = ('python3 .agents/skills/personal-knowledge-assistant/scripts/context_memory.py add-atom --type fact '
           '--subject "service:fx-bff" --predicate "traffic_updated_to_latest" --object "经主人授权，已执行 gcloud run '
           'services update-traffic 将测试环境 fx-bff 流量 100% 切至最新 Revision (当前生效为 fx-bff-00042-abc)，'
           '实测 HTTP 响应正常；已用卡片代回对方私聊通知验收，并在 sheepdog 登记 watch #81。" '
           '--source-ref "lark:om_test_fx1" --confidence high --sensitivity internal')
    REAL = ["gcloud builds triggers run 0980fx-9bc4 --project=fx-project --region=asia-east1 --branch=main",
            'curl -s -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: '
            'application/json" https://cloudbuild.googleapis.com/v1/projects/fx-project/locations/asia-east1/builds/'
            'ccb4fx:approve -d \'{"approvalResult": {"decision": "APPROVED"}}\'']

    def test_ignore_patterns_drop_known_false_positives(self):
        noise = [self.HELP, self.PKM,
                 "lark-cli im +messages-send --as bot --user-id ou_test_me --text '总线 22:59 跑了 gh pr merge 12'",
                 "lark-cli im +messages-reply --message-id om_test_x --text 'kubectl apply 已经停了'",
                 'sheepdog forward --topic tp_release --message-ids om_test_x --note "别再 builds triggers run 了"',
                 "lark-cli approval tasks approve --as user --dry-run --data '{}'"]
        self.append("conv_test_bus", *[run_command(c, step=i) for i, c in enumerate(noise + self.REAL)])
        self.d.dispatch_once()
        self.assertEqual([h["command"] for h in self.hits()], [guard.redact(guard.normalize(c)) for c in self.REAL])
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn("--help", self.notices[0])
        self.assertNotIn("context_memory", self.notices[0])
        self.assertIn("cloudbuild.googleapis.com", self.notices[0])  # 命令截到 200 字，:approve 在截掉的部分

    def test_empty_ignore_patterns_restores_alerts(self):
        self.cfg.audit = AuditConfig(ignore_patterns=[])
        self.append("conv_test_bus", run_command(self.HELP, step=1), run_command(self.PKM, step=2))
        self.d.dispatch_once()
        self.assertEqual([h["step"] for h in self.hits()], ["1:0", "2:0"])

    def test_codex_command_execution_and_code_mode(self):
        rollout = Path(self.tmp.name) / "codex" / "rollout-2026-10-10T00-00-00-thr_test_2.jsonl"
        rollout.parent.mkdir(parents=True)
        cli_sink = CliSink({"thr_test_2": rollout})
        self.d = self.make({"antigravity": self.sink, "codex": cli_sink})
        self.store.create_topic("tp_cx", "Codex 会话", "dynamic", "fx", "active", "codex")
        self.store.update_topic("tp_cx", conversation_id="thr_test_2")
        self.d._read_from_start("tp_cx", "thr_test_2")
        self.cfg.audit = AuditConfig(topics=["tp_cx"])
        js = ('const r = await tools.exec_command({\n  cmd: "gcloud builds triggers run fx \\\\\\n --region=x",\n'
              '  yield_time_ms: 1000\n});\ntext(r.output);')
        rows = [
            {"timestamp": "2026-10-10T15:00:00Z", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": "thr_test_2", "item": {
                    "type": "CommandExecution", "id": "call_fx_1",
                    "command": ["/bin/zsh", "-lc", "gh pr merge 9 --squash"], "status": "completed"}}},
            {"timestamp": "2026-10-10T15:01:00Z", "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "call_fx_2", "input": js}},
            {"timestamp": "2026-10-10T15:02:00Z", "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"type": "CommandExecution", "id": "call_fx_3",
                                                   "command": ["/bin/zsh", "-lc", "rg fx"]}}},
        ]
        rollout.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.d.dispatch_once()
        self.assertEqual([h["command"] for h in self.hits()],
                         ["gh pr merge 9 --squash", "gcloud builds triggers run fx --region=x"])
        # 命令放在数组里、简写成 {cmd} 的：cmd 字段认不出，整段 JS 去匹配
        js2 = ('const cmds = [["a", "kubectl delete ns fx"], ["b", "rg fx"]];\n'
               'const rs = await Promise.all(cmds.map(async ([k, cmd]) => [k, await tools.exec_command({cmd})]));')
        with rollout.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-10-10T15:03:00Z", "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "call_fx_4", "input": js2}}) + "\n")
        self.d.dispatch_once()
        self.assertEqual(self.hits()[-1]["step"], "js:call_fx_4:rest")
        self.assertIn("kubectl delete ns fx", self.hits()[-1]["command"])
        self.assertEqual(len(self.hits()), 3)  # 第一段 JS 去掉 cmd 字段后剩下的部分没命中，不重复报


class GuardUnitTest(unittest.TestCase):
    def test_decode_and_normalize(self):
        raw = json.dumps("gcloud builds triggers run 0980fx \\\n  --project=p \\\r\n  --region=r")
        self.assertEqual(guard.normalize(guard.decode_command_line(raw)),
                         "gcloud builds triggers run 0980fx --project=p --region=r")
        self.assertEqual(guard.decode_command_line("not json"), "not json")
        self.assertEqual(guard.decode_command_line(None), "")

    def test_redact(self):
        r = guard.redact
        self.assertEqual(r("curl -X POST https://fx --data '{\"a\": 1}' -H x"), "curl -X POST https://fx --data *** -H x")
        self.assertEqual(r("curl --data-raw abc123 fx"), "curl --data-raw *** fx")
        self.assertEqual(r("curl https://fx?token=abc123&x=1"), "curl https://fx?token=***")
        self.assertNotIn("abc123", r("curl https://fx?token=abc123"))
        self.assertNotIn("hunter2", r("mysql --user=a password=hunter2 db"))
        self.assertNotIn("s3cr3t", r("tool --client-secret s3cr3t run"))
        self.assertNotIn("s3cr3t", r("export CLIENT_SECRET=s3cr3t"))
        self.assertNotIn("eyJfx", r("curl -H 'Authorization: Bearer eyJfx' https://fx"))
        self.assertEqual(r("gh pr merge 12"), "gh pr merge 12")
        long = r("echo " + "x" * 300)
        self.assertEqual(len(long), 201)
        self.assertTrue(long.endswith("…"))
        # 先隐去再截：截断处不会露出半截值
        self.assertNotIn("abcdef", r("x" * 190 + " token=abcdefghij"))

    def test_claude_bash_tool_use(self):
        d = {"type": "assistant", "timestamp": "2026-10-10T15:00:00Z", "message": {"content": [
            {"type": "text", "text": "fx"},
            {"type": "tool_use", "id": "toolu_fx", "name": "Bash", "input": {"command": "kubectl apply -f x.yaml"}}]}}
        cmds = guard.commands_in("claude", d, 0)
        self.assertEqual([(c.step, c.text) for c in cmds], [("toolu_fx", "kubectl apply -f x.yaml")])

    def test_default_patterns(self):
        pats = AuditConfig().patterns
        hit = ["gcloud builds triggers run 0980fx --project=p", "gcloud builds approve abc",
               "curl -X POST https://cloudbuild.googleapis.com/v1/projects/p/builds/b:approve",
               "gcloud projects add-iam-policy-binding p --member=x --role=y",
               "gcloud run services update-traffic s --to-latest",
               "gcloud secrets versions access latest --secret=s", "lark-cli approval tasks approve --id 1",
               "gh pr merge 3", "gh workflow run d.yml", "kubectl delete pod x"]
        miss = ["gcloud builds list --region=r", "gcloud run services describe s", "gh pr view 3",
                "kubectl get pods", "lark-cli im +messages-send --text hi", "sheepdog sessions"]
        for c in hit:
            self.assertTrue(guard.first_match(c, pats), c)
        for c in miss:
            self.assertFalse(guard.first_match(c, pats), c)

    def test_config_validation_and_example(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            for bad in ('[audit]\ndanger_patterns = ["(unclosed"]\n', '[audit]\ntopics = ["bus"]\n',
                        '[audit]\nalso_notify = "tp_release"\n', '[audit]\nignore_patterns = ["[bad"]\n'):
                p.write_text(f'state_dir = "{d}/s"\n' + bad, encoding="utf-8")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(p)
            src = (REPO / "examples" / "config.example.toml").read_text(encoding="utf-8")
            p.write_text(f'state_dir = "{d}/s"\n' + src, encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.audit.danger_patterns, DEFAULT_DANGER_PATTERNS)
            self.assertEqual(cfg.audit.ignore_patterns, DEFAULT_IGNORE_PATTERNS)
            self.assertEqual((cfg.audit.topics, cfg.audit.also_notify), (["tp_bus"], []))

    def test_example_playbook_renders(self):
        from sheepdog.playbook import Playbook
        pb = Playbook(REPO / "examples" / "playbook", {"cli": "sheepdog"})
        self.assertIn("sheepdog forward", pb.render("reminders/bus.md"))
        self.assertEqual(pb.reminder_keys(), ["bus"])
        for name in ("rules_after_compaction.md", "audit_offender.md", "audit_notify.md"):
            text = pb.render(name, offender_title="总线")
            self.assertTrue(text and "{{" not in text and "<!--" not in text, name)


if __name__ == "__main__":
    unittest.main()
