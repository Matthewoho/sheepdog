"""规格 7.99（REG-067）：Sheepdog 管理与可观测工作台（三级会话状态树、T0-T5 端到端与会话内耗时瀑布流、三层决策对账与自治晋升、VIP 花名册与运维干预）。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.request import Request, urlopen

from sheepdog import admin_web, duty
from sheepdog.config import Config
from sheepdog.hook import count_path, log_call
from sheepdog.models import Message
from sheepdog.people_roster import upsert_person_alias
from sheepdog.store import Store


class TestAdminWeb799(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "sheepdog.sqlite3"
        self.roster_file = self.root / "roster.toml"
        self.people_file = self.root / "people_roster.json"
        self.security_file = self.root / "security.toml"
        self.cfg_file = self.root / "config.toml"

        self.roster_file.write_text(
            '[topics.tp_bus]\ntitle = "中枢总线"\nduty = "分拣"\n\n[topics.tp_laohu]\ntitle = "人·老胡"\nduty = "老胡事务"\ngroup_id = "laohu"\n',
            encoding="utf-8",
        )
        self.security_file.write_text("", encoding="utf-8")
        self.cfg = Config(
            state_dir=self.state_dir,
            roster_path=str(self.roster_file),
            security_path=str(self.security_file),
            self_open_id="ou_owner",
        )
        upsert_person_alias(self.db_path, target="Klaus He", feishu_name="Klaus He", role_hint="colleague:Backend")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reg_067_end_to_end_workbench_observability_and_actions(self) -> None:
        st = Store(self.db_path)
        try:
            # 1. 创建三级会话结构：Session Group (laohu) -> Session (tp_laohu) -> Subagent (lamb_sub1)
            st.create_topic("tp_bus", "中枢总线", "adopted", "分拣", "active", group_id="")
            st.create_topic("tp_laohu", "人·老胡", "dynamic", "处理老胡需求", "running", group_id="laohu")
            st.update_topic("tp_laohu", conversation_id="cid-laohu-1", pending_batch_id="b_spec_1")
            st.upsert_card("tp_laohu", run_state="working", action="调用 gcloud 查日志", last_sig="gcloud logging read")

            st.create_lamb(
                lamb_id="lamb_sub1",
                parent_topic_id="tp_laohu",
                kind="subagent",
                task_title="核查测试环境配置",
                conversation_id="cid-lamb-1",
                pid=None,
                state="running",
                log_path="",
            )

            # 写入 Stop Hook 拦截状态与调用日志
            p_stop = count_path(self.cfg, "tp_laohu")
            p_stop.parent.mkdir(parents=True, exist_ok=True)
            p_stop.write_text(
                json.dumps({"batch": "b_spec_1", "count": 2, "last_blocked_at": "2026-11-01T06:10:00+08:00"}),
                encoding="utf-8",
            )
            log_call(
                self.state_dir,
                {
                    "at": "2026-11-01T06:10:00+08:00",
                    "conversation_id": "cid-laohu-1",
                    "managed": True,
                    "topic": "tp_laohu",
                    "batch": "b_spec_1",
                    "lamb_id": "",
                    "decision": "block",
                    "reason": "没交回执，第 2 次拦",
                    "ms": 3.2,
                },
            )

            # 2. 创建完整 T0->T1->T2->T3a->T3->T4->T5 消息链路并验证历史回执 acked_at 回填
            msg = Message(
                message_id="om_test_msg_1",
                chat_id="oc_chat_1",
                chat_name="工程群",
                chat_type="group",
                sender_id="ou_laohu",
                sender_name="Andy Hu",
                sender_type="user",
                content="请帮忙看一下灰度配置",
                msg_type="text",
                create_time="2026-11-01T06:00:00+08:00",
            )
            st.upsert_message(msg, "dispatch", "at_me", ["forwarded_from:b_bus_1"], "tp_laohu")
            with st.tx() as c:
                c.execute(
                    "UPDATE messages SET first_seen=?, dispatch_state='acked', batch_id='b_spec_1' WHERE message_id='om_test_msg_1'",
                    ("2026-11-01T06:00:02+08:00",),
                )
                c.execute(
                    "INSERT INTO acks(message_id,chat_id,chat_type,sender_id,reason,identity,reaction_id,added_at,removed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "om_test_msg_1",
                        "oc_chat_1",
                        "group",
                        "ou_laohu",
                        "at_me",
                        "user",
                        "r_1",
                        "2026-11-01T06:00:03+08:00",
                        "2026-11-01T06:01:10+08:00",
                    ),
                )
                # 先插入未带 acked_at 的历史批次，测试 backfill_dispatch_acked_at 从 duty_history 回填
                c.execute(
                    "INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state,receipt,acked_at) VALUES(?,?,?,?,?,?,NULL)",
                    ("b_bus_1", "tp_bus", '["om_test_msg_1"]', "2026-11-01T06:00:04+08:00", "acked", "[handled] 已转交 tp_laohu"),
                )
                c.execute(
                    "INSERT INTO duty_history(topic_id,kind,text,at) VALUES(?,?,?,?)",
                    ("tp_bus", "receipt", "b_bus_1 handled：已转交 tp_laohu", "2026-11-01T06:00:08+08:00"),
                )
                c.execute(
                    "INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                    ("b_spec_1", "tp_laohu", '["om_test_msg_1"]', "2026-11-01T06:00:10+08:00", "sent"),
                )
                c.execute(
                    "INSERT INTO sends(idem_key,kind,topic_id,message_id,target_message_id,text,sent_at) VALUES(?,?,?,?,?,?,?)",
                    ("idem_1", "reply", "tp_laohu", "om_reply_1", "om_test_msg_1", "灰度配置已核查正常", "2026-11-01T06:01:05+08:00"),
                )

            # finish_dispatch 必须写入 acked_at
            st.finish_dispatch("b_spec_1", "acked", receipt="[handled] 核查完成", acked_at="2026-11-01T06:01:08+08:00")
            backfilled = st.backfill_dispatch_acked_at()
            self.assertEqual(backfilled, 1)
            d_bus = st.get_dispatch("b_bus_1")
            self.assertEqual(d_bus["acked_at"], "2026-11-01T06:00:08+08:00")

            # 3. 构造三层决策数据：3 条连续采纳（触发自治晋升推荐）+ 1 条人类改判（进错题本）
            for i in range(1, 4):
                ask_id = f"om_ask_acc_{i}"
                ans_id = f"om_ans_acc_{i}"
                st.upsert_message(
                    Message(
                        message_id=ans_id,
                        chat_id="oc_dm",
                        chat_name="私聊",
                        chat_type="p2p",
                        sender_id="ou_owner",
                        sender_name="Matthew",
                        sender_type="user",
                        content="[卡片回调] A：同意发布",
                        msg_type="text",
                        create_time=f"2026-11-01T05:1{i}:30+08:00",
                    ),
                    "dispatch",
                    "owner_reply",
                    [],
                    "tp_release",
                )
                with st.tx() as c:
                    c.execute(
                        "INSERT INTO escalations(message_id,topic_id,chat_id,text,asked_at,answered_at,answer_message_id,closed_at,close_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            ask_id,
                            "tp_release",
                            "oc_dm",
                            f"需要你定：常规发布单 #{i}\nA: 同意发布 (Recommended)\nB: 暂缓\n建议选 A",
                            f"2026-11-01T05:1{i}:00+08:00",
                            f"2026-11-01T05:1{i}:30+08:00",
                            ans_id,
                            f"2026-11-01T05:1{i}:30+08:00",
                            "answered",
                        ),
                    )

            st.upsert_message(
                Message(
                    message_id="om_ans_ovr_1",
                    chat_id="oc_dm",
                    chat_name="私聊",
                    chat_type="p2p",
                    sender_id="ou_owner",
                    sender_name="Matthew",
                    sender_type="user",
                    content="[卡片回调] 其他：已经过时间了 忽略吧",
                    msg_type="text",
                    create_time="2026-11-01T06:22:59+08:00",
                ),
                "dispatch",
                "owner_reply",
                [],
                "tp_kaifeng",
            )
            with st.tx() as c:
                c.execute(
                    "INSERT INTO escalations(message_id,topic_id,chat_id,text,asked_at,answered_at,answer_message_id,closed_at,close_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "om_ask_ovr_1",
                        "tp_kaifeng",
                        "oc_dm",
                        "需要你定：万圣节大促零点清理生产订阅缓存请示\nA: 批准执行\nB: 取消\n建议选 A",
                        "2026-11-01T00:02:34+08:00",
                        "2026-11-01T06:22:59+08:00",
                        "om_ans_ovr_1",
                        "2026-11-01T06:22:59+08:00",
                        "answered",
                    ),
                )
            duty.set_work(st, "tp_laohu", "SD-799", title="搭管理后台", state="doing")
        finally:
            st.close()

        # 启动真实 HTTP Server 验证 API 与动作闭环
        srv = admin_web.create_server("127.0.0.1", 0, cfg=self.cfg)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}"
        try:
            def _get(p: str) -> dict:
                with urlopen(base + p, timeout=5) as r:
                    return json.loads(r.read().decode("utf-8"))

            def _post(payload: dict) -> dict:
                req = Request(
                    base + "/api/action",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=5) as r:
                    return json.loads(r.read().decode("utf-8"))

            ov = _get("/api/overview?minutes=1440")
            self.assertEqual(ov["counts"]["lambs_running"], 1)
            self.assertGreaterEqual(ov["latencies"]["t0_t5_e2e_reply"]["p50_ms"], 60_000)

            sess = _get("/api/sessions")
            laohu_sess = next(s for s in sess["sessions"] if s["topic_id"] == "tp_laohu")
            self.assertEqual(laohu_sess["group_id"], "laohu")
            self.assertEqual(laohu_sess["running_lamb_count"], 1)
            self.assertEqual(laohu_sess["hook_state"]["block_count"], 2)

            fake_tx = self.cfg.state_dir / "cid-laohu-1-transcript.jsonl"
            fake_tx.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "step_index": 10,
                                "type": "SYSTEM_MESSAGE",
                                "created_at": "2026-11-01T06:00:08+08:00",
                                "content": "[sheepdog] 新信号批次 b-laohu-002 (topic tp_laohu)\n请核查 PR #384",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "step_index": 11,
                                "type": "PLANNER_RESPONSE",
                                "created_at": "2026-11-01T06:00:12+08:00",
                                "content": "",
                                "tool_calls": [
                                    {"name": "invoke_subagent", "arguments": {"Role": "PR Code Reviewer"}}
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "step_index": 12,
                                "type": "PLANNER_RESPONSE",
                                "created_at": "2026-11-01T06:00:58+08:00",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "name": "run_command",
                                        "arguments": {"CommandLine": "sheepdog receipt --topic tp_laohu --batch b-laohu-002"},
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "step_index": 13,
                                "type": "PLANNER_RESPONSE",
                                "created_at": "2026-11-01T06:01:02+08:00",
                                "content": "已完成核查并交回执。",
                                "tool_calls": [],
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch("sheepdog.admin_web.transcript_path", return_value=fake_tx):
                trace = _get("/api/events/trace?message_id=om_test_msg_1")
            stage_codes = [s["code"] for s in trace["stages"]]
            self.assertEqual(stage_codes, ["T0", "T1", "T2", "T3a", "T3", "T4", "T5"])
            t5_stage = next(s for s in trace["stages"] if s["code"] == "T5")
            self.assertEqual(t5_stage["delta_ms"], 65_000)
            step_kinds = [st["kind"] for st in trace["session_steps"]]
            self.assertEqual(step_kinds, ["batch_inject", "subagent", "sd_receipt", "thinking"])
            self.assertEqual(trace["session_steps"][1]["duration_ms"], 46_000)

            dec = _get("/api/decisions")
            self.assertEqual(dec["summary"]["accepted"], 3)
            self.assertEqual(dec["summary"]["overridden"], 1)
            self.assertEqual(dec["summary"]["acceptance_rate_pct"], 75.0)
            self.assertEqual(len(dec["autonomy_candidates"]), 1)
            self.assertEqual(dec["autonomy_candidates"][0]["topic_id"], "tp_release")
            self.assertEqual(len(dec["overridden_book"]), 1)
            self.assertEqual(dec["overridden_book"][0]["topic_id"], "tp_kaifeng")

            # 验证写操作：切换 VIP + 清除 Hook 阻断 + 完成在办项
            vip_res = _post({"action": "toggle_vip", "name": "Klaus He"})
            self.assertTrue(vip_res["ok"])
            self.assertTrue(vip_res["vip"])

            clr_res = _post({"action": "clear_hook_block", "topic_id": "tp_laohu"})
            self.assertTrue(clr_res["ok"])
            self.assertFalse(count_path(self.cfg, "tp_laohu").exists())

            wk_res = _post({"action": "complete_work_item", "topic_id": "tp_laohu", "key": "SD-799"})
            self.assertTrue(wk_res["ok"])
            adm = _get("/api/admin/state")
            self.assertEqual(len(adm["work_items"]), 0)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
