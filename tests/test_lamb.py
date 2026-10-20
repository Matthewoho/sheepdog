"""7.63 羔羊（Subagent / Lamb）纳管 + 母羊 30 秒卸锁：
1. lamb register / transcript 自动发现与 sheepdog board 缩进展示；
2. lamb spawn 后台 CLI 子进程、日志落盘、lamb done 与 lamb list 生命周期；
3. 母羊交 waiting 回执（带 lamb:<id> 锚点）立即释放 pending_batch_id 锁，期间新消息不阻塞，羔羊完工后自动回流 lamb_report 并关掉 work_item；
4. 羔羊越权硬拦截（reply / reply-card / ask / report / forward / receipt / resolve / watch 均返回退出码 2）；
5. 活跃羔羊心跳透传防母羊误报 stalled / lost 失联告警，以及羔羊危险命令审计归属母羊。
"""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli, duty
from sheepdog import session as sm
from sheepdog.config import DEFAULT_DANGER_PATTERNS
from sheepdog.engine import BUS_TOPIC_ID, complete_lamb, register_lamb, write_receipt
from sheepdog.prompts import interface_section
from sheepdog.sink.agentapi import transcript_path

from test_duty import DutyBase, ago, tool, utc
from test_guardrails import run_command
from test_roster import msg


def invoke_subagent_line(at: datetime, title: str, cid: str = "", step: int = 20) -> dict:
    args = {
        "TaskTitle": json.dumps(title),
        "ConversationId": json.dumps(cid),
        "toolSummary": json.dumps(f"Subagent: {title}"),
    }
    return {
        "step_index": step,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": utc(at),
        "thinking": "spawn subagent",
        "content": "",
        "tool_calls": [{"name": "invoke_subagent", "args": args}],
    }


class LambTest(DutyBase):
    def test_763_lamb_register_and_board_display(self):
        # 1. 显式 CLI 注册一只 subagent 羔羊
        rc, out = self.cli(
            "lamb", "register",
            "--topic", "tp_alpha",
            "--title", "核验 PR #101 与 Tag",
            "--cid", "conv_test_lamb_sub1",
            "--lamb-id", "lamb_sub01",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("lamb_sub01", out)

        # 2. 母羊 transcript 中出现 invoke_subagent 调用时自动发现补建
        self.append("conv_test_alpha", invoke_subagent_line(ago(1), "并发核查 Cloud Run 流量", cid="conv_test_lamb_sub2"))
        self.d.dispatch_once()

        active = self.store.get_active_lambs("tp_alpha")
        self.assertEqual(len(active), 2)
        ids = {lb["lamb_id"] for lb in active}
        self.assertIn("lamb_sub01", ids)

        # 3. sheepdog board 文本模式在母羊卡片下缩进展示 ↳ 🐑
        rc_b, board_txt = self.cli("board")
        self.assertEqual(rc_b, 0, board_txt)
        self.assertIn("    ↳ 🐑 lamb_sub01 [subagent] 核验 PR #101 与 Tag · running · 心跳", board_txt)
        self.assertIn("[subagent] 并发核查 Cloud Run 流量 · running · 心跳", board_txt)

        # 4. sheepdog board --json 包含 lambs 列表
        rc_j, board_json = self.cli("board", "--json")
        self.assertEqual(rc_j, 0, board_json)
        cards = json.loads(board_json)
        alpha_card = next(c for c in cards if c["topic_id"] == "tp_alpha")
        self.assertIn("lambs", alpha_card)
        self.assertEqual(len(alpha_card["lambs"]), 2)
        sub01 = next(x for x in alpha_card["lambs"] if x["lamb_id"] == "lamb_sub01")
        self.assertEqual(sub01["kind"], "subagent")
        self.assertEqual(sub01["task_title"], "核验 PR #101 与 Tag")
        self.assertEqual(sub01["state"], "running")
        self.assertEqual(sub01["conversation_id"], "conv_test_lamb_sub1")

    def test_763_lamb_spawn_cli_and_done_lifecycle(self):
        # 1. lamb spawn 启动后台 CLI 命令并重定向日志到 <state_dir>/lambs/<lamb_id>.log
        rc, out = self.cli(
            "lamb", "spawn",
            "--topic", "tp_alpha",
            "--title", "监听 Cloud Build 构建 b-99",
            "--cmd", "echo build_ok_test_763",
            "--lamb-id", "lamb_cli01",
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("lamb_cli01", out)

        lb = self.store.get_lamb("lamb_cli01")
        self.assertIsNotNone(lb)
        self.assertEqual(lb["kind"], "cli")
        self.assertEqual(lb["state"], "running")
        self.assertTrue(lb["pid"] and lb["pid"] > 0)
        log_file = Path(lb["log_path"])
        self.assertTrue(log_file.exists())
        self.assertIn("build_ok_test_763", log_file.read_text(encoding="utf-8"))

        # 2. lamb list 默认列出运行中的羔羊
        rc_l1, list_running = self.cli("lamb", "list", "--topic", "tp_alpha")
        self.assertEqual(rc_l1, 0, list_running)
        self.assertIn("lamb_cli01", list_running)
        self.assertIn("[running]", list_running)

        # 3. lamb done 标记完工并写入摘要
        rc_d, out_d = self.cli(
            "lamb", "done",
            "--lamb-id", "lamb_cli01",
            "--summary", "构建 b-99 成功，Revision 流量 100%",
        )
        self.assertEqual(rc_d, 0, out_d)
        lb_after = self.store.get_lamb("lamb_cli01")
        self.assertEqual(lb_after["state"], "done")
        self.assertEqual(lb_after["result_summary"], "构建 b-99 成功，Revision 流量 100%")
        self.assertTrue(bool(lb_after["finished_at"]))

        # 4. lamb list 默认过滤掉已结束羔羊，--all 包含已结束羔羊
        rc_l2, list_after = self.cli("lamb", "list", "--topic", "tp_alpha")
        self.assertEqual(rc_l2, 0, list_after)
        self.assertNotIn("lamb_cli01", list_after)

        rc_all, list_all = self.cli("lamb", "list", "--topic", "tp_alpha", "--all")
        self.assertEqual(rc_all, 0, list_all)
        self.assertIn("lamb_cli01", list_all)
        self.assertIn("[done]", list_all)
        self.assertIn("构建 b-99 成功，Revision 流量 100%", list_all)

    def test_763_parent_waiting_receipt_releases_lock_and_lamb_done_reports_back(self):
        # 1. 给母羊投递第一批消息 b1，此时母羊处于 running 且持有 pending_batch_id
        self.poll([msg(message_id="om_test_ticket_1", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="请发版 202610200001")])
        self.d.dispatch_once()
        t_running = self.store.get_topic("tp_alpha")
        b1 = t_running["pending_batch_id"]
        self.assertTrue(bool(b1))
        self.assertEqual(t_running["state"], sm.RUNNING)

        # 2. 母羊登记羔羊并在 30 秒内交 status="waiting" 回执（带 anchors: ["lamb:lamb_w01"]）
        register_lamb(self.store, "tp_alpha", "核验 PR 与线上版本", kind="subagent", lamb_id="lamb_w01")
        rc, out = self.cli(
            "receipt",
            "--topic", "tp_alpha",
            "--batch", b1,
            "--json", json.dumps({
                "status": "waiting",
                "summary": "已派羔羊 lamb_w01 核验单据，等待结果回流",
                "anchors": ["lamb:lamb_w01"],
            }, ensure_ascii=False),
        )
        self.assertEqual(rc, 0, out)

        # 3. 立即验证：母羊 pending_batch_id 已释放为 None，状态回到 active，work_items 登记了 lamb:lamb_w01 (waiting_other)
        t_unlocked = self.store.get_topic("tp_alpha")
        self.assertIsNone(t_unlocked["pending_batch_id"])
        self.assertEqual(t_unlocked["state"], sm.ACTIVE)
        w = self.store.get_work("tp_alpha", duty.norm_key("lamb:lamb_w01"))
        self.assertIsNotNone(w)
        self.assertEqual(w["state"], duty.WAITING_OTHER)
        self.assertIn("lamb_w01", w["waiting_on"])
        self.assertIsNone(w["closed_at"])

        # 4. 羔羊运行期间，新消息可以立即投递给母羊，完全不阻塞
        self.poll([msg(message_id="om_test_urgent_ping", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="顺便问下进度")])
        self.d.dispatch_once()
        sent_to_alpha = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("om_test_urgent_ping", sent_to_alpha)
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 5. 羔羊完工：lamb done 触发 lamb_report 系统消息入队并关掉 work_items 中的 lamb:lamb_w01
        complete_lamb(self.store, "lamb_w01", summary="PR 已合入，线上 v1.2 < 待发 v1.3，可安全发布", state="done")
        w_closed = self.store.get_work("tp_alpha", duty.norm_key("lamb:lamb_w01"))
        self.assertEqual(w_closed["state"], duty.DONE)
        self.assertIsNotNone(w_closed["closed_at"])
        self.assertIn("可安全发布", w_closed["result"])

        # 下一轮 dispatch_once 将【系统提示 · 羔羊完工汇报】作为批次投递给母羊
        self.d.dispatch_once()
        report_batch = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("【系统提示 · 羔羊完工汇报】", report_batch)
        self.assertIn("lamb_w01", report_batch)
        self.assertIn("PR 已合入，线上 v1.2 < 待发 v1.3，可安全发布", report_batch)

    def test_763_lamb_permission_block_outbound_and_receipt(self):
        self.cfg.source.enabled = False
        self.cfg.notify.file = str(Path(self.tmp.name) / "notify.log")
        # 登记一只属于 tp_alpha 的羔羊
        register_lamb(
            self.store,
            "tp_alpha",
            "只读核查羔羊",
            kind="subagent",
            conversation_id="conv_test_lamb_only",
            lamb_id="lamb_blk01",
        )
        self.poll([msg(message_id="om_test_in_1", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="测试消息")])
        self.d.dispatch_once()
        b_id = self.store.get_topic("tp_alpha")["pending_batch_id"]

        commands = [
            ("reply", ["--topic", "tp_alpha", "--to", "om_test_in_1", "--text", "越权代回"]),
            ("reply-card", ["--topic", "tp_alpha", "--text", "越权卡片"]),
            ("ask", ["--topic", "tp_alpha", "--title", "越权提问", "--text", "能发吗"]),
            ("report", ["--topic", "tp_alpha", "--title", "越权汇报", "--text", "发完了"]),
            ("forward", ["--topic", "tp_alpha", "--note", "越权转交"]),
            ("receipt", ["--topic", "tp_alpha", "--batch", b_id, "--json", '{"status":"handled","summary":"越权回执"}']),
            ("resolve", ["--topic", "tp_alpha", "--key", "k1", "--why", "越权结案"]),
            ("watch", ["--topic", "tp_alpha", "--person", "ou_test_other", "--note", "越权等待"]),
        ]

        # 方式 A：环境变量 SHEEPDOG_LAMB_ID 命中拦截
        with mock.patch.dict(os.environ, {"SHEEPDOG_LAMB_ID": "lamb_blk01"}):
            for cmd_name, argv in commands:
                rc, out = self.cli(cmd_name, *argv)
                self.assertEqual(rc, 2, f"{cmd_name} should exit with 2, got {rc}: {out}")
                self.assertIn("禁止越权：当前执行体是羔羊", out)
                self.assertIn("lamb_blk01", out)
                self.assertIn("tp_alpha", out)

        # 方式 B：会话 conversation_id 匹配 lambs 表拦截
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": "conv_test_lamb_only"}, clear=False):
            rc_r, out_r = self.cli("reply", "--topic", "tp_alpha", "--to", "om_test_in_1", "--text", "越权代回")
            self.assertEqual(rc_r, 2, out_r)
            self.assertIn("禁止越权：当前执行体是羔羊", out_r)

        # 母羊批次依然保持原状态，未被羔羊篡改
        self.assertEqual(self.store.get_topic("tp_alpha")["pending_batch_id"], b_id)

    def test_763_active_lamb_prevents_parent_stalled_alert(self):
        # 1. 母羊 35 分钟前收到批次并做了最后一次动作，之后一直沉默等待羔羊
        self.append("conv_test_alpha", tool(ago(35), "Spawn subagent", "Spawning subagent", cmd="echo start"))
        self.sent_batch("tp_alpha", 35)
        self.set_mtime("conv_test_alpha", ago(35))

        # 2. 名下有一只心跳新鲜（2 分钟前刚活动）的活跃羔羊
        register_lamb(
            self.store,
            "tp_alpha",
            "Cloud Build 20 分钟构建",
            kind="subagent",
            conversation_id="conv_test_lamb_fresh",
            lamb_id="lamb_fresh01",
        )
        self.append("conv_test_lamb_fresh", tool(ago(2), "Polling Cloud Build", "Polling build", cmd="gcloud builds list"))
        self.d._tick_lambs()

        # 3. 跑在岗表巡检：母羊已有 35 分钟无动作（超过 stall_minutes=15 和 lost_minutes=30），
        #    但因名下有未超时的活跃羔羊，母羊状态必须保持 waiting，绝不判 stalled / lost，也不给主人发报警
        self.notices.clear()
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.WAITING)
        self.assertIn("等待羔羊 lamb_fresh01", c["detail"])
        self.assertEqual(self.notices, [])

        # 4. 羔羊在自己的 transcript 中执行了危险命令，审计必须归属到母羊 tp_alpha 并注明（由羔羊 lamb_fresh01 执行）
        self.cfg.audit.topics = ["tp_alpha"]
        self.cfg.audit.danger_patterns = list(DEFAULT_DANGER_PATTERNS)
        self.append(
            "conv_test_lamb_fresh",
            run_command("gcloud builds triggers run prod-api --tag=v1.0.0", step=88, at=utc(ago(1))),
        )
        self.d._audit_commands()
        self.assertTrue(any("由羔羊 lamb_fresh01 执行" in n and "tp_alpha" in n for n in self.notices), self.notices)

    def test_763_real_antigravity_transcript_subagent_lifecycle(self):
        cid1 = "5d3c8ada-5ac5-4a59-88cd-1db7a643471e"
        cid2 = "c0e09508-59d6-434b-8afc-3edf53a8ec13"

        # Step 1: 真实 Antigravity PLANNER_RESPONSE（Subagents 是 JSON 字符串，且此时尚无 conversationId）
        step1 = {
            "step_index": 30,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": utc(ago(5)),
            "thinking": "launch two subagents",
            "content": "",
            "tool_calls": [
                {
                    "name": "invoke_subagent",
                    "args": {
                        "Subagents": json.dumps([
                            {
                                "Model": "inherit",
                                "Prompt": "Implement 7.63 lamb lifecycle",
                                "Role": "Phase 1 Lamb Implementer",
                                "TypeName": "sheepdog-implement",
                            },
                            {
                                "Model": "inherit",
                                "Prompt": "Design 7.49 session group",
                                "Role": "Phase 2 Group Designer",
                                "TypeName": "sheepdog-design",
                            },
                        ]),
                        "toolSummary": "Launch two subagents",
                    },
                }
            ],
        }
        # Step 2: 真实 Antigravity GENERIC（Created the following subagents: 块带出真实 conversationId）
        step2 = {
            "step_index": 31,
            "source": "SYSTEM",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": utc(ago(4)),
            "content": (
                "Created At: 2026-10-20T05:14:57+08:00\n"
                "Completed At: 2026-10-20T05:14:58+08:00\n"
                "Created the following subagents:\n"
                "{\n"
                f'  "conversationId": "{cid1}",\n'
                f'  "logAbsoluteUri": "file:///tmp/test_home/.gemini/antigravity/brain/{cid1}/.system_generated/logs/transcript.jsonl",\n'
                '  "workspaceUris": []\n'
                "}\n"
                "{\n"
                f'  "conversationId": "{cid2}",\n'
                f'  "logAbsoluteUri": "file:///tmp/test_home/.gemini/antigravity/brain/{cid2}/.system_generated/logs/transcript.jsonl",\n'
                '  "workspaceUris": []\n'
                "}"
            ),
        }
        self.append("conv_test_alpha", step1, step2)

        # 子代理在各自 transcript 中活动（一个更新心跳，一个触发危险命令审计）
        self.cfg.audit.topics = ["tp_alpha"]
        self.cfg.audit.danger_patterns = list(DEFAULT_DANGER_PATTERNS)
        self.append(cid1, tool(ago(1), "Running unit tests", "Running tests", cmd="python3 -m unittest"))
        self.append(cid2, run_command("gcloud builds triggers run prod-api --tag=v2.0.0", step=12, at=utc(ago(1))))

        self.d.dispatch_once()

        active = self.store.get_active_lambs("tp_alpha")
        self.assertEqual(len(active), 2)
        by_cid = {lb["conversation_id"]: lb for lb in active}
        self.assertIn(cid1, by_cid)
        self.assertIn(cid2, by_cid)
        self.assertEqual(by_cid[cid1]["task_title"], "Phase 1 Lamb Implementer")
        self.assertEqual(by_cid[cid2]["task_title"], "Phase 2 Group Designer")
        # 心跳已从子代理 transcript 刷新，且危险命令审计已归属到母羊 tp_alpha
        self.assertTrue(any(f"由羔羊 {by_cid[cid2]['lamb_id']} 执行" in n and "tp_alpha" in n for n in self.notices), self.notices)

        # Step 3: 真实 Antigravity SYSTEM_MESSAGE（子代理回信，自动将对应羔羊标为 done）
        step3 = {
            "step_index": 32,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": utc(ago(0)),
            "content": f"[Message] timestamp=2026-10-19T21:21:22Z sender={cid2} priority=MESSAGE_PRIORITY_HIGH content=# Done report",
        }
        self.append("conv_test_alpha", step3)
        self.d.dispatch_once()

        lb1 = self.store.find_lamb_by_cid(cid1)
        lb2 = self.store.find_lamb_by_cid(cid2)
        self.assertEqual(lb1["state"], "running")
        self.assertEqual(lb2["state"], "done")
        self.assertIn("Done report", lb2["result_summary"])
        self.assertEqual(lb2["report_msg_id"], "native_system_message")

    def test_763_lamb_interface_in_standing_rules(self):
        managed_if = interface_section("tp_alpha", bus=False, remind=[15], expire=60)
        self.assertIn("### 羔羊（重活后台并发与 30 秒卸锁）", managed_if)
        self.assertIn("45 秒", managed_if)
        self.assertIn("sheepdog lamb spawn", managed_if)
        self.assertIn('"anchors":["lamb:<lamb_id>"]', managed_if)

        bus_if = interface_section("tp_bus", bus=True, remind=[15], expire=60)
        self.assertNotIn("sheepdog lamb spawn", bus_if)

    def test_763_lamb_register_and_auto_discover_from_transcript(self):
        self.test_763_lamb_register_and_board_display()

    def test_763_lamb_outbound_commands_hard_blocked(self):
        self.test_763_lamb_permission_block_outbound_and_receipt()

    def test_763_parent_waiting_lamb_releases_batch_lock(self):
        self.test_763_parent_waiting_receipt_releases_lock_and_lamb_done_reports_back()

    def test_763_lamb_heartbeat_prevents_parent_stalled(self):
        self.test_763_active_lamb_prevents_parent_stalled_alert()

    def test_763_lamb_completion_delivers_report_batch(self):
        self.test_763_parent_waiting_receipt_releases_lock_and_lamb_done_reports_back()

