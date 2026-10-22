"""7.72 羊圈并发与零敏感指针派单（SD-772-SESSIONGROUP-SUBAGENT）：
- REG-014: sheepdog lamb prepare 零敏感文件指针派单协议（防 Conseca pre-tool hook 拦截）
- REG-015: 母羊整批 <=3 步硬预算 + 单线程长跑回执守卫
- REG-016: render_group_peer_status 渲染同羊圈最近收发与在跑羔羊快照
"""

import json
from datetime import timedelta

from sheepdog import guard, prompts
from sheepdog.engine import register_lamb
from sheepdog.sink.agentapi import transcript_path

from test_duty import DutyBase, ago, tool, utc


class Lamb772Test(DutyBase):
    def test_reg_014_lamb_prepare_writes_spec_file_and_emits_zero_sensitive_pointer_prompt(self):
        sensitive_spec = (
            "gcloud run services update model-manager-server --project=tpn-prod-506507 --region=asia-east1\n"
            "curl -s -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret' https://prod.internal/health"
        )
        rc, out = self.cli(
            "lamb", "prepare",
            "--topic", "tp_alpha",
            "--title", "生产变更与回读校验",
            "--spec", sensitive_spec,
            "--lamb-id", "lamb_prep01",
            "--json",
        )
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data["lamb_id"], "lamb_prep01")
        self.assertEqual(data["topic_id"], "tp_alpha")
        self.assertEqual(data["waiting_anchor"], "lamb:lamb_prep01")

        prompt = data["subagent_prompt"]
        self.assertIn(data["task_file"], prompt)
        self.assertIn("[lamb:lamb_prep01]", prompt)
        for forbidden in ("gcloud", "tpn-prod-506507", "Bearer", "eyJhbGciOi", "https://prod.internal"):
            self.assertNotIn(forbidden, prompt)

        task_path = self.cfg.state_dir / "lamb_tasks" / "lamb_prep01.md"
        self.assertEqual(str(task_path), data["task_file"])
        self.assertTrue(task_path.exists())
        md_text = task_path.read_text(encoding="utf-8")
        self.assertIn("# 羔羊任务单 lamb_prep01（母羊：tp_alpha）", md_text)
        self.assertIn("严禁调用 sheepdog reply / ask / report / receipt", md_text)
        self.assertIn("[lamb:lamb_prep01]", md_text)
        self.assertIn(sensitive_spec, md_text)

        lb = self.store.get_lamb("lamb_prep01")
        self.assertIsNotNone(lb)
        self.assertEqual(lb["state"], "running")
        self.assertEqual(lb["kind"], "subagent")
        self.assertEqual(lb["log_path"], str(task_path))

    def test_reg_015_monolithic_parent_turn_without_lamb_triggers_receipt_guard(self):
        t = self.store.get_topic("tp_alpha")
        cid = t["conversation_id"]
        bid = self.sent_batch("tp_alpha", minutes_ago=3, running=True, bid="b_mono_772")
        d = self.store.get_dispatch(bid)

        base_dt = ago(2.5)
        lines = [
            tool(base_dt, cmd="date '+%Y-%m-%d %H:%M:%S'", step=1),
            tool(base_dt + timedelta(seconds=5), cmd="sheepdog reply --topic tp_alpha --to om_1 --text '收到，稍等'", step=2),
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": utc(base_dt + timedelta(seconds=8)),
                "tool_calls": [{"name": "schedule", "args": {"DurationSeconds": 10}}],
            },
            tool(base_dt + timedelta(seconds=10), cmd="gcloud run services describe svc --project=tap-testing-env", step=4),
            tool(base_dt + timedelta(seconds=20), cmd="curl -s https://example.com/api", step=5),
            tool(base_dt + timedelta(seconds=30), summary="Read config", cmd="", step=6),
            tool(base_dt + timedelta(seconds=40), cmd="rg -n 'error' /tmp", step=7),
            tool(base_dt + timedelta(seconds=50), cmd="python3 -c 'print(1)'", step=8),
            tool(base_dt + timedelta(seconds=60), cmd="sqlite3 /tmp/a.db 'SELECT 1'", step=9),
            tool(base_dt + timedelta(seconds=70), cmd="sheepdog receipt --topic tp_alpha --batch b_mono_772 --json '{}'", step=10),
        ]
        tp = transcript_path(cid)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

        steps = guard.count_parent_tool_steps_since(tp, d["sent_at"])
        self.assertEqual(steps, 6)

        rc, out = self.cli(
            "receipt", "--topic", "tp_alpha", "--batch", bid,
            "--json", '{"status":"handled","summary":"串行跑完6步"}',
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("⚠️ [牧羊犬并发守卫 REG-015]", out)
        self.assertIn("6 步业务工具", out)
        self.assertIn("sheepdog lamb prepare", out)

        hist = [dict(r) for r in self.store.history("tp_alpha", "monolithic_turn")]
        self.assertEqual(len(hist), 1)
        self.assertIn("串行 6 步且 0 羔羊", hist[0]["text"])

        # 反例 A：已派生羔羊时不触发警告
        bid2 = self.sent_batch("tp_alpha", minutes_ago=3, running=True, bid="b_with_lamb_772")
        register_lamb(self.store, "tp_alpha", "后台排查子任务", lamb_id="lamb_ok01")
        rc2, out2 = self.cli(
            "receipt", "--topic", "tp_alpha", "--batch", bid2,
            "--json", '{"status":"waiting","summary":"已派羔羊","anchors":["lamb:lamb_ok01"]}',
        )
        self.assertEqual(rc2, 0, out2)
        self.assertNotIn("REG-015", out2)

        # 反例 B：业务步数 <= 3 时不触发警告
        self.store.update_lamb("lamb_ok01", state="done")
        bid3 = self.sent_batch("tp_alpha", minutes_ago=3, running=True, bid="b_light_772")
        tp.write_text("".join(json.dumps(x) + "\n" for x in lines[:5]), encoding="utf-8")  # 仅 2 步业务工具
        rc3, out3 = self.cli(
            "receipt", "--topic", "tp_alpha", "--batch", bid3,
            "--json", '{"status":"handled","summary":"轻活直办"}',
        )
        self.assertEqual(rc3, 0, out3)
        self.assertNotIn("REG-015", out3)

    def test_reg_016_render_group_peer_status_renders_snapshot(self):
        peers = [
            {"topic_id": "tp_ops", "state": "running", "summary": "处理 Sam 告警", "recent_activity": "在跑羔羊 lamb_a1: 查日志"},
            {"topic_id": "tp_ops#2", "state": "idle", "summary": ""},
        ]
        snap = [
            "[20:15] `tp_ops` reply -> om_101: 已回 Sam 正在核查",
            "[20:16] `tp_ops` [lamb:lamb_a1] running: 排查 Cloud Run 502",
        ]
        rendered = prompts.render_group_peer_status("ops", "tp_ops#2", peers, snapshot=snap)
        self.assertIn("## 🏠 羊圈同侪状态", rendered)
        self.assertIn("`tp_ops` [running]：处理 Sam 告警", rendered)
        self.assertIn("【同羊圈最近收发与在跑羔羊快照（防重复排查/防脑裂）】", rendered)
        self.assertIn("在跑羔羊 lamb_a1: 查日志", rendered)
        self.assertIn("[20:15] `tp_ops` reply -> om_101: 已回 Sam 正在核查", rendered)
        self.assertIn("[20:16] `tp_ops` [lamb:lamb_a1] running: 排查 Cloud Run 502", rendered)
