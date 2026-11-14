"""DEMO-145：动手前先回一声，任何人都能点的「终止」按钮（全链路验收与 T1 变异证伪单测）。

覆盖方案第七节全部 8 项验收与证伪场景（全合成假名与假 ID：Alice / Bob / Charlie，`ou_test_*`）：
1. `sheepdog reply --start` 发带「⏹ 终止」按钮的开工卡，并在 `work_items` 记一行 `doing`（`key=req:<om_>`、`source=start`）。
2. 非主人（Bob）点「⏹ 终止」→ 原卡 3 秒内变「⏹ 已被 Bob 终止」、按钮消失并带「▶ 接着做」；原处回一句「已被 Bob 终止。」；
   会话收到 `run_stopped` 插话告知；`sheepdog hook pretool` 拒绝该会话及其子代理羔羊后续除纯 `sheepdog receipt` 外的全部工具调用
   （含拒绝 `sheepdog receipt && rm -rf` 拼接命令，且任何异常或非托管会话均 0 字节输出退出 0）；命令行羔羊进程组（含忽略 SIGTERM 的进程）
   经 SIGTERM -> 3s SIGKILL 终止并标 `cancelled`。
3. 卡片上的 `stop_facts` 计数与 `sends`、会话记录工具调用、`lambs` 逐项对得上，且群卡绝不泄露命令原文或参数。
4. 并发与队列边界（4.6）：同人连点两次、两人同时点（先点者胜）、做完（`doing -> done`）后再点、点终止后晚到 `handled` 回执、
   批次还没起跑（`pending`）就点（显示「还没开始就被终止」）、同请求人排队信号摘除（`stopped_by:`）与同批他人信号退回重投（`redeliver_after_stop`）。
5. 请求人（Alice）或主人点「▶ 接着做」→ 新开 `doing` 行（`source_ref` 指向原行）、原卡恢复开工态并记「Alice HH:MM 让接着做」、
   重排 `run_resumed` 信号附停前事实；第三人（Charlie）点「▶ 接着做」被拒（`card_callback_non_requester_resume`）。
6. 非主人（Bob）点普通拍板卡（`sheepdog ask`）仍被 `card_callback_non_owner` 丢弃（回归守卫）。
7. 私聊（p2p）路径：`reply --start` 附「回复『停』可以终止」，对方在私聊回复「停」或「终止」自动触发终止。
8. T1 证伪：
   - 把钩子摘掉后会话在 `stopped_at` 之后继续跑工具调用，`evolve.audit_start_and_stop_discipline` 检出 `post_stop_tool_calls_count > 0` 并触发 `build_evolve_digest` 告警；请求类批次未发 `--start` 就跑危险命令检出 `missing_start_card_count > 0`。
   - 打开 `store._unconditional_work_update_for_test = True`（把条件更新改成无条件更新），「做完后再点终止」把已 `done` 的事项错误覆盖成 `stopped`。
"""

import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import card_callback, duty, engine, evolve, hook, outbound
from sheepdog import session as sm
from sheepdog.models import Mention, Message
from sheepdog.router import DISPATCH, DROP, INBOX
from sheepdog.source.lark import parse_message

from test_roster import ME
from test_threads import ALICE, BOT, ThreadsBase, _opt, now8

BOB = "ou_test_bob"
CHARLIE = "ou_test_charlie"
GROUP_CHAT = "oc_test_demo145_group"


def _make_callback_event(
    *,
    event_id: str,
    operator_id: str,
    open_message_id: str,
    value: dict,
    chat_id: str = GROUP_CHAT,
) -> dict:
    return {
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": operator_id},
            "context": {"open_message_id": open_message_id, "open_chat_id": chat_id},
            "action": {"tag": "button", "value": value},
        },
    }


def _patched_cards(lark_calls: list[list[str]]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for c in lark_calls:
        if len(c) >= 3 and c[:3] == ["im", "messages", "patch"]:
            params = json.loads(_opt(c, "--params") or "{}")
            data = json.loads(_opt(c, "--data") or "{}")
            card_obj = json.loads(data.get("content") or "{}")
            out.append((params.get("message_id", ""), card_obj))
    return out


def _card_buttons(card_obj: dict) -> list[dict]:
    btns: list[dict] = []
    for el in (card_obj.get("body") or {}).get("elements") or []:
        if el.get("tag") == "column_set":
            for col in el.get("columns") or []:
                for sub in col.get("elements") or []:
                    if sub.get("tag") == "button":
                        btns.append(sub)
        elif el.get("tag") == "button":
            btns.append(el)
    return btns


class StopButtonDemo145Test(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.lark.chat_types[GROUP_CHAT] = "group"
        self.lark.chat_names[GROUP_CHAT] = "研发协作测试群"
        self.app_data_dir = Path(self.tmp.name) / "appdata"
        self.brain_dir = self.app_data_dir / "brain"
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        p_env = mock.patch.dict("os.environ", {"ANTIGRAVITY_APP_DATA_DIR": str(self.app_data_dir)})
        p_env.start()
        self.addCleanup(p_env.stop)
        self.pretool_log_dir = Path(self.tmp.name) / "pretool-logs"
        p_pt_log = mock.patch.object(hook, "PRETOOL_LOG_DIR", self.pretool_log_dir)
        p_pt_log.start()
        self.addCleanup(p_pt_log.stop)

    def _write_transcript(self, cid: str, steps: list[dict]) -> Path:
        p = self.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        return p

    def _seed_group_request(
        self,
        *,
        sender_id: str = ALICE,
        sender_name: str = "Alice",
        text: str = "@Bot 帮我查下 staging api 502",
        mid: str = "om_test_req_001",
        chat_id: str = GROUP_CHAT,
        chat_type: str = "group",
        state: str = "dispatched",
        batch_id: str = "b20251107112500",
    ) -> str:
        t0 = now8()
        self.lark.add(
            chat_id=chat_id,
            sender_id=sender_id,
            content=text,
            at=t0,
            chat_type=chat_type,
            message_id=mid,
        )
        self.lark.msgs[mid]["sender"]["name"] = sender_name
        self.store.upsert_message(
            Message(
                message_id=mid,
                chat_id=chat_id,
                chat_name=self.lark.chat_names.get(chat_id, "测试私聊"),
                chat_type=chat_type,
                sender_id=sender_id,
                sender_name=sender_name,
                sender_type="user",
                content=text,
                msg_type="text",
                create_time=t0.isoformat(),
                mentions=[Mention(id=BOT, name="Bot")] if chat_type == "group" else [],
            ),
            DISPATCH,
            "at_bot" if chat_type == "group" else "p2p",
            [],
            topic_id="tp_alpha",
            received_by="bot",
        )
        if state == "dispatched":
            self.store.record_dispatch(batch_id, "tp_alpha", [mid])
            with self.store.tx() as c:
                c.execute(
                    "UPDATE messages SET dispatch_state='dispatched', batch_id=? WHERE message_id=?",
                    (batch_id, mid),
                )
            self.store.update_topic(
                "tp_alpha",
                state=sm.RUNNING,
                pending_batch_id=batch_id,
                dispatched_at=t0.isoformat(),
            )
        return mid

    def _reply_start(self, req_mid: str, text: str) -> outbound.ReplyResult:
        return outbound.reply(
            self.cfg,
            self.store,
            self.source,
            "tp_alpha",
            req_mid,
            text=text,
            start=True,
        )

    # ---------- 1. 开工卡与 work_items 登记 ----------
    def test_demo145_1_reply_start_sends_card_and_creates_doing_work_item(self):
        req_mid = self._seed_group_request()
        res = self._reply_start(req_mid, "收到，我现在去查 staging api 的 502。")
        self.assertTrue(bool(res.message_id))
        self.assertGreater(res.work_id, 0)

        w = self.store.get_work_by_id(res.work_id)
        self.assertIsNotNone(w)
        self.assertEqual(w["topic_id"], "tp_alpha")
        self.assertEqual(w["key"], f"req:{req_mid}")
        self.assertEqual(w["source"], duty.SRC_START)
        self.assertEqual(w["state"], duty.DOING)
        self.assertEqual(w["start_card_mid"], res.message_id)
        self.assertEqual(w["requester_id"], ALICE)
        self.assertEqual(w["requester_name"], "Alice")
        self.assertEqual(w["chat_id"], GROUP_CHAT)
        self.assertEqual(w["target_mid"], req_mid)
        self.assertEqual(w["batch_id"], "b20251107112500")

        # 核对发出的卡片带有 ⏹ 终止 按钮且参数齐全
        reply_calls = self.lark.sent("+messages-reply")
        self.assertEqual(len(reply_calls), 1)
        sent_card = json.loads(_opt(reply_calls[-1], "--content"))
        btns = _card_buttons(sent_card)
        self.assertEqual(len(btns), 1)
        self.assertEqual(btns[0]["text"]["content"], "⏹ 终止")
        self.assertEqual(
            btns[0]["value"],
            {
                "sd_action": "run_stop",
                "work_id": res.work_id,
                "topic_id": "tp_alpha",
                "target_mid": req_mid,
            },
        )

    # ---------- 2 & 3. 非主人点终止：改原卡 + 回一句 + 插话告知 + PreToolUse 硬拦 + 杀羔羊 + stop_facts 脱敏计数 ----------
    def test_demo145_2_and_3_non_owner_stop_blocks_pretool_kills_lambs_and_computes_safe_stop_facts(self):
        req_mid = self._seed_group_request()
        # 给 Bob 留一条历史消息以便解析名字 Bob
        self.store.upsert_message(
            Message(
                message_id="om_test_bob_hello",
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=BOB,
                sender_name="Bob",
                sender_type="user",
                content="大家好",
                msg_type="text",
                create_time=now8().isoformat(),
            ),
            INBOX,
            "ambient",
            [],
            topic_id="tp_alpha",
        )

        res = self._reply_start(req_mid, "收到，我现在去查 staging api 的 502。")
        work_row = self.store.get_work_by_id(res.work_id)
        start_dt = datetime.fromisoformat(work_row["created_at"])

        # 构造窗口内的：(a) 1 条对外发送消息；(b) 会话 transcript 里的 3 条命令（含 2 条危险命令、1 条未完结命令）；
        # (c) 1 只忽略 SIGTERM 的 CLI 羔羊进程 + 1 只 Subagent 羔羊
        self.store.add_send(
            "idem_extra_send_1",
            "reply",
            "tp_alpha",
            "om_test_progress_1",
            "正在拉取近 10 分钟日志",
            target_message_id=req_mid,
            identity="bot",
        )
        alpha_cid = self.store.get_topic("tp_alpha")["conversation_id"]
        t_step = (start_dt + timedelta(seconds=1)).isoformat()
        self._write_transcript(
            alpha_cid,
            [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": t_step,
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "gcloud run services update-traffic api --to-latest --project=secret-proj-123"},
                        },
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "kubectl delete pod api-0 -n prod-secret-ns"},
                        },
                    ],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "IN_PROGRESS",
                    "created_at": t_step,
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "python3 check_health.py"},
                        },
                    ],
                },
            ],
        )

        # 启动一只忽略 SIGTERM 的子进程模拟顽固 CLI 羔羊，验证 SIGTERM -> 3s SIGKILL 两段式必杀
        proc = subprocess.Popen(
            [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        time.sleep(0.15)
        self.store.create_lamb(
            lamb_id="lamb_cli_stubborn",
            parent_topic_id="tp_alpha",
            kind="cli",
            task_title="run long check",
            pid=proc.pid,
        )
        sub_cid = "subagent-cid-0001-alpha"
        self.store.create_lamb(
            lamb_id="lamb_sub_alpha",
            parent_topic_id="tp_alpha",
            kind="subagent",
            task_title="inspect logs",
            conversation_id=sub_cid,
        )

        # 非主人 Bob 点击「⏹ 终止」
        ev = _make_callback_event(
            event_id="ev_stop_001",
            operator_id=BOB,
            open_message_id=res.message_id,
            value={"sd_action": "run_stop", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        cb_msg = card_callback.process_card_action_event(ev, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(cb_msg)
        self.assertEqual(self.store.get_message(cb_msg.message_id)["reason"], "card_callback_run_stop")

        # 1) 核验 work_items 状态与操作人
        w_after = self.store.get_work_by_id(res.work_id)
        self.assertEqual(w_after["state"], duty.STOPPED)
        self.assertEqual(w_after["stopped_by"], BOB)
        self.assertEqual(w_after["stopped_by_name"], "Bob")
        self.assertTrue(w_after["stopped_at"])

        # 2) 核验 stop_facts 计数准确，且卡片上只写计数与工具名，不泄露命令参数
        facts = json.loads(w_after["stop_facts"])
        self.assertEqual(facts["sends_count"], 1)
        self.assertEqual(facts["commands_count"], 3)
        self.assertEqual(facts["danger_count"], 2)
        self.assertEqual(facts["danger_tools"], ["gcloud", "kubectl"])
        self.assertEqual(facts["in_flight_count"], 1)
        self.assertEqual(facts["lambs_count"], 2)
        self.assertEqual(len(facts["lambs_stopped"]), 2)

        patches = _patched_cards(self.lark.calls)
        self.assertGreaterEqual(len(patches), 1)
        patched_mid, patched_card = patches[-1]
        self.assertEqual(patched_mid, res.message_id)
        patched_md = json.dumps(patched_card, ensure_ascii=False)
        self.assertIn("**⏹ 已被 Bob 终止", patched_md)
        self.assertIn("发了 1 条消息", patched_md)
        self.assertIn("跑了 3 条命令，其中 2 条可能改东西（gcloud / kubectl）", patched_md)
        self.assertIn("羔羊 2 只，已停", patched_md)
        self.assertIn("还有 1 条后台命令在跑，停不住", patched_md)
        # 严禁泄露内部参数
        self.assertNotIn("secret-proj-123", patched_md)
        self.assertNotIn("prod-secret-ns", patched_md)
        # ⏹ 终止 按钮已移除，只剩 ▶ 接着做
        btns_after = _card_buttons(patched_card)
        self.assertEqual(len(btns_after), 1)
        self.assertEqual(btns_after[0]["text"]["content"], "▶ 接着做")
        self.assertEqual(btns_after[0]["value"]["sd_action"], "run_resume")

        # 3) 核验原处回了一句「已被 Bob 终止。」
        reply_calls = self.lark.sent("+messages-reply")
        self.assertEqual(len(reply_calls), 2)
        self.assertIn("已被 Bob 终止。", self.lark.sent_body(reply_calls[-1]))

        # 4) 核验 CLI 羔羊进程组已被 SIGKILL 杀掉且状态为 cancelled
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.store.get_lamb("lamb_cli_stubborn")["state"], "cancelled")
        self.assertEqual(self.store.get_lamb("lamb_sub_alpha")["state"], "cancelled")

        # 5) 核验插话通知送达会话
        self.d.dispatch_once()
        sent_to_alpha = self.sink.to(alpha_cid)
        self.assertTrue(any("已被 Bob 于" in s and "sheepdog receipt" in s for s in sent_to_alpha), sent_to_alpha)

        # 6) 核验 PreToolUse 钩子（真实 Antigravity toolCall.name / toolCall.args 格式，M11）：
        # 母羊与子代理羔羊调普通工具全被 deny，只放行纯 sheepdog receipt
        ag_ls_payload = json.dumps(
            {
                "conversationId": alpha_cid,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}},
            }
        )
        out_deny = hook.pretool_check(ag_ls_payload, self.cfg)
        self.assertIsNotNone(out_deny)
        self.assertEqual(out_deny["decision"], "deny")
        self.assertIn("已被 Bob", out_deny["reason"])

        out_sub_deny = hook.pretool_check(
            json.dumps(
                {
                    "conversationId": sub_cid,
                    "toolCall": {"name": "read_file", "args": {"Path": "/etc/hosts"}},
                }
            ),
            self.cfg,
        )
        self.assertIsNotNone(out_sub_deny)
        self.assertEqual(out_sub_deny["decision"], "deny")

        # 试图在 sheepdog receipt 后拼接别的命令（真实 toolCall.args.CommandLine 格式） -> 依然 deny
        out_chain_deny = hook.pretool_check(
            json.dumps(
                {
                    "conversationId": alpha_cid,
                    "toolCall": {
                        "name": "run_command",
                        "args": {
                            "CommandLine": "sheepdog receipt --topic tp_alpha --batch b1 --json '{}' && rm -rf /tmp/x"
                        },
                    },
                }
            ),
            self.cfg,
        )
        self.assertIsNotNone(out_chain_deny)
        self.assertEqual(out_chain_deny["decision"], "deny")

        # 纯 sheepdog receipt（真实 toolCall.args.CommandLine 格式） -> 放行（返回 None）
        ag_receipt_payload = json.dumps(
            {
                "conversationId": alpha_cid,
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": 'sheepdog receipt --topic tp_alpha --batch b20251107112500 --json \'{"status":"stopped"}\''
                    },
                },
            }
        )
        out_receipt_ok = hook.pretool_check(ag_receipt_payload, self.cfg)
        self.assertIsNone(out_receipt_ok)

        # M11 变异证伪：若钩子不解析真实 Antigravity 的 toolCall.name / toolCall.args 而只读顶层 toolName/toolInput，
        # 则纯 sheepdog receipt 会因为拿不到 tool_name/cmd_line 被误判为普通工具而 deny（证伪成立）
        ag_without_toolcall = json.dumps({"conversationId": alpha_cid})
        m11_broken = hook.pretool_check(ag_without_toolcall, self.cfg)
        self.assertIsNotNone(m11_broken)
        self.assertEqual(m11_broken["decision"], "deny")

        # 7.34 E5 铁律与 M6b：异常 JSON 或非托管会话调用 pretool_main 一律 0 字节 stdout/stderr、退出码 0
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stderr(buf_err):
            rc = hook.pretool_main(io.StringIO("{bad json"), buf_out, self.cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(buf_out.getvalue(), "")
        self.assertEqual(buf_err.getvalue(), "")

        buf_unmanaged_out, buf_unmanaged_err = io.StringIO(), io.StringIO()
        with redirect_stderr(buf_unmanaged_err):
            rc_unmanaged = hook.pretool_main(
                io.StringIO(
                    json.dumps(
                        {
                            "conversationId": "cid-unmanaged-random-999",
                            "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /tmp/test"}},
                        }
                    )
                ),
                buf_unmanaged_out,
                self.cfg,
            )
        self.assertEqual(rc_unmanaged, 0)
        self.assertEqual(buf_unmanaged_out.getvalue(), "")
        self.assertEqual(buf_unmanaged_err.getvalue(), "")

        # 7) 会话交 stopped 回执收尾 -> 会话回 active，钩子窗口关闭，后续工具调用放行
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107112500",
            {"status": "stopped", "summary": "已按终止指令停手", "note": "仅查了日志，未改线上资源"},
        )
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)
        out_after_receipt = hook.pretool_check(
            json.dumps({"conversationId": alpha_cid, "toolName": "run_command", "toolInput": {"CommandLine": "ls"}}),
            self.cfg,
        )
        self.assertIsNone(out_after_receipt)
        w_final = self.store.get_work_by_id(res.work_id)
        self.assertEqual(w_final["state"], duty.STOPPED)
        self.assertEqual(json.loads(w_final["stop_facts"]).get("receipt_note"), "仅查了日志，未改线上资源")

    # ---------- 4. 并发与队列边界（4.6） ----------
    def test_demo145_4_concurrency_races_and_batch_signal_isolation(self):
        # 4a: 同批里有 Alice 的请求 + Charlie 的无关信号，队列里还有 Alice 的追问
        req_mid = self._seed_group_request(mid="om_test_req_race1", batch_id="b20251107122500")
        charlie_mid = "om_test_charlie_in_batch"
        t0 = now8()
        self.store.upsert_message(
            Message(
                message_id=charlie_mid,
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=CHARLIE,
                sender_name="Charlie",
                sender_type="user",
                content="@Bot 顺便帮我看下账单",
                msg_type="text",
                create_time=t0.isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )
        with self.store.tx() as c:
            c.execute(
                "UPDATE messages SET dispatch_state='dispatched', batch_id=? WHERE message_id=?",
                ("b20251107122500", charlie_mid),
            )
            c.execute(
                "UPDATE dispatches SET message_ids=? WHERE batch_id=?",
                (json.dumps([req_mid, charlie_mid]), "b20251107122500"),
            )

        alice_queued_mid = "om_test_alice_queued_2"
        self.store.upsert_message(
            Message(
                message_id=alice_queued_mid,
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="补充一下日志截图",
                msg_type="text",
                create_time=t0.isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )

        res = self._reply_start(req_mid, "收到，我现在去查。")

        # Bob 先点，Charlie 紧跟着点（并发点击）
        ev_bob = _make_callback_event(
            event_id="ev_race_bob",
            operator_id=BOB,
            open_message_id=res.message_id,
            value={"sd_action": "run_stop", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        ev_charlie = _make_callback_event(
            event_id="ev_race_charlie",
            operator_id=CHARLIE,
            open_message_id=res.message_id,
            value={"sd_action": "run_stop", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        m1 = card_callback.process_card_action_event(ev_bob, cfg=self.cfg, store=self.store, source=self.source)
        m2 = card_callback.process_card_action_event(ev_charlie, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(m1)
        self.assertIsNone(m2)  # 第二次点击条件更新 0 行，只记 card_callback_duplicate
        self.assertEqual(self.store.get_work_by_id(res.work_id)["stopped_by"], BOB)
        self.assertEqual(self.store.get_message("sd_card_cb_ev_race_charlie")["reason"], "card_callback_duplicate")

        # 核对同请求人排队信号被标 drop（stopped_by:ou_test_bob），而同批 Charlie 的信号退回 pending 并带 redeliver_after_stop
        self.assertEqual(self.store.get_message(alice_queued_mid)["route"], DROP)
        self.assertEqual(self.store.get_message(alice_queued_mid)["reason"], f"stopped_by:{BOB}")
        charlie_row = self.store.get_message(charlie_mid)
        self.assertEqual(charlie_row["dispatch_state"], "pending")
        self.assertIn("redeliver_after_stop", json.loads(charlie_row["tags_json"] or "[]"))

        # 点击在先，会话晚到的 handled 回执照收，但 work_item 仍保持 stopped
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107122500",
            {"status": "handled", "summary": "晚到的回执"},
        )
        self.d.dispatch_once()
        self.assertEqual(self.store.get_work_by_id(res.work_id)["state"], duty.STOPPED)

        # 4b: 做完在先（doing -> done），迟到的点击不覆盖 done
        req_mid2 = self._seed_group_request(mid="om_test_req_race2", batch_id="b20251107123000")
        res2 = self._reply_start(req_mid2, "收到，查第二个问题。")
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107123000",
            {"status": "done", "summary": "第二个问题查完了"},
        )
        self.d.dispatch_once()
        self.assertEqual(self.store.get_work_by_id(res2.work_id)["state"], duty.DONE)
        _, done_card = _patched_cards(self.lark.calls)[-1]
        self.assertIn("✅ 做完了", json.dumps(done_card, ensure_ascii=False))

        ev_late = _make_callback_event(
            event_id="ev_race_late",
            operator_id=BOB,
            open_message_id=res2.message_id,
            value={"sd_action": "run_stop", "work_id": res2.work_id, "topic_id": "tp_alpha"},
        )
        m_late = card_callback.process_card_action_event(ev_late, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNone(m_late)
        self.assertEqual(self.store.get_work_by_id(res2.work_id)["state"], duty.DONE)

        # 4c: 批次还没起跑（target_mid 仍在 pending）就被点终止 -> 直接摘除，卡片写「还没开始就被终止」
        req_mid3 = self._seed_group_request(mid="om_test_req_race3", state="pending", batch_id="")
        res3 = self._reply_start(req_mid3, "收到，排队准备查第三个问题。")
        ev_pre = _make_callback_event(
            event_id="ev_race_prestart",
            operator_id=BOB,
            open_message_id=res3.message_id,
            value={"sd_action": "run_stop", "work_id": res3.work_id, "topic_id": "tp_alpha"},
        )
        m_pre = card_callback.process_card_action_event(ev_pre, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(m_pre)
        self.assertEqual(self.store.get_message(req_mid3)["route"], DROP)
        _, pre_card = _patched_cards(self.lark.calls)[-1]
        self.assertIn("还没开始就被终止", json.dumps(pre_card, ensure_ascii=False))

    # ---------- 5. 「▶ 接着做」权限校验与恢复流程 ----------
    def test_demo145_5_resume_allows_requester_and_owner_rejects_third_party(self):
        req_mid = self._seed_group_request(mid="om_test_req_resume", batch_id="b20251107132500")
        res = self._reply_start(req_mid, "收到，开始查问题。")
        ev_stop = _make_callback_event(
            event_id="ev_stop_for_resume",
            operator_id=BOB,
            open_message_id=res.message_id,
            value={"sd_action": "run_stop", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        card_callback.process_card_action_event(ev_stop, cfg=self.cfg, store=self.store, source=self.source)
        self.assertEqual(self.store.get_work_by_id(res.work_id)["state"], duty.STOPPED)

        # 第三人 Charlie 点「▶ 接着做」-> 被拒（card_callback_non_requester_resume）
        ev_charlie_resume = _make_callback_event(
            event_id="ev_resume_charlie",
            operator_id=CHARLIE,
            open_message_id=res.message_id,
            value={"sd_action": "run_resume", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        m_reject = card_callback.process_card_action_event(
            ev_charlie_resume, cfg=self.cfg, store=self.store, source=self.source
        )
        self.assertIsNone(m_reject)
        self.assertEqual(
            self.store.get_message("sd_card_cb_ev_resume_charlie")["reason"],
            "card_callback_non_requester_resume",
        )
        self.assertEqual(self.store.doing_start_works("tp_alpha"), [])

        # 请求人 Alice 点「▶ 接着做」-> 放行，新开一行 doing work_item（source_ref 指向被停行），原卡恢复带「Alice HH:MM 让接着做」
        ev_alice_resume = _make_callback_event(
            event_id="ev_resume_alice",
            operator_id=ALICE,
            open_message_id=res.message_id,
            value={"sd_action": "run_resume", "work_id": res.work_id, "topic_id": "tp_alpha"},
        )
        m_ok = card_callback.process_card_action_event(
            ev_alice_resume, cfg=self.cfg, store=self.store, source=self.source
        )
        self.assertIsNotNone(m_ok)
        self.assertEqual(self.store.get_message(m_ok.message_id)["reason"], "card_callback_run_resume")

        doing_rows = self.store.doing_start_works("tp_alpha")
        self.assertEqual(len(doing_rows), 1)
        new_w = doing_rows[0]
        self.assertNotEqual(new_w["id"], res.work_id)
        self.assertEqual(new_w["source_ref"], f"work:{res.work_id}")
        self.assertEqual(new_w["start_card_mid"], res.message_id)

        _, resumed_card = _patched_cards(self.lark.calls)[-1]
        resumed_md = json.dumps(resumed_card, ensure_ascii=False)
        self.assertIn("Alice", resumed_md)
        self.assertIn("让接着做", resumed_md)
        btns = _card_buttons(resumed_card)
        self.assertEqual(len(btns), 1)
        self.assertEqual(btns[0]["value"]["sd_action"], "run_stop")
        self.assertEqual(btns[0]["value"]["work_id"], new_w["id"])

        # 再次被 Bob 终止后，主人（ME）点「▶ 接着做」同样放行
        ev_stop2 = _make_callback_event(
            event_id="ev_stop_second",
            operator_id=BOB,
            open_message_id=res.message_id,
            value={"sd_action": "run_stop", "work_id": new_w["id"], "topic_id": "tp_alpha"},
        )
        card_callback.process_card_action_event(ev_stop2, cfg=self.cfg, store=self.store, source=self.source)
        ev_owner_resume = _make_callback_event(
            event_id="ev_resume_owner",
            operator_id=ME,
            open_message_id=res.message_id,
            value={"sd_action": "run_resume", "work_id": new_w["id"], "topic_id": "tp_alpha"},
        )
        m_owner_ok = card_callback.process_card_action_event(
            ev_owner_resume, cfg=self.cfg, store=self.store, source=self.source
        )
        self.assertIsNotNone(m_owner_ok)
        self.assertEqual(len(self.store.doing_start_works("tp_alpha")), 1)

    # ---------- 6. 非主人点拍板卡仍被拒（回归守卫） ----------
    def test_demo145_6_non_owner_click_decision_card_still_rejected_regression(self):
        ev_ask = _make_callback_event(
            event_id="ev_non_owner_ask_1",
            operator_id=BOB,
            open_message_id="om_test_ask_card_1",
            value={"action": "A", "reply_text": "A：同意发布", "topic": "tp_alpha"},
        )
        res = card_callback.process_card_action_event(ev_ask, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNone(res)
        saved = self.store.get_message("sd_card_cb_ev_non_owner_ask_1")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["route"], DROP)
        self.assertEqual(saved["reason"], "card_callback_non_owner")

    # ---------- 7. 私聊（p2p）路径：默认仅挂按钮，p2p_stop_mode=text 时附「回复『停』可以终止」+ 回复「停」触发终止 ----------
    def test_demo145_7_p2p_start_card_and_text_stop_fallback(self):
        p2p_chat = "oc_test_alpha_p2p"
        self.lark.chat_types[p2p_chat] = "p2p"
        req_mid_default = self._seed_group_request(
            sender_id=ALICE,
            sender_name="Alice",
            text="帮我拉一下昨天的导出文件",
            mid="om_test_p2p_req_default",
            chat_id=p2p_chat,
            chat_type="p2p",
            batch_id="b20251107142500",
        )
        # 默认未开启 p2p_stop_mode=text 时，只挂按钮，不默认塞「回复『停』可以终止」文字引导
        res_default = self._reply_start(req_mid_default, "收到，我现在去拉昨天导出文件。")
        self.assertTrue(bool(res_default.message_id))
        default_card_str = _opt(self.lark.sent("+messages-reply")[-1], "--content")
        self.assertNotIn("回复「停」可以终止", default_card_str)
        engine.write_receipt(self.cfg, "tp_alpha", "b20251107142500", {"status": "done", "summary": "done"})
        self.d.dispatch_once()

        # 开启 p2p_stop_mode=text（支持 sheepdog p2p-stop-mode text 命令入口或 [session] p2p_stop_mode 配置）后，附「回复『停』可以终止」
        from sheepdog import cli
        out_mode = io.StringIO()
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(self.cfg.config_path), "SHEEPDOG_STATE_DIR": str(self.cfg.state_dir)}), redirect_stdout(out_mode):
            self.assertEqual(cli.main(["p2p-stop-mode", "text"]), 0)
        self.assertIn("p2p_stop_mode=text", out_mode.getvalue())
        self.assertEqual(self.store.get_meta("p2p_stop_mode"), "text")
        req_mid = self._seed_group_request(
            sender_id=ALICE,
            sender_name="Alice",
            text="帮我拉一下今天的导出文件",
            mid="om_test_p2p_req_1",
            chat_id=p2p_chat,
            chat_type="p2p",
            batch_id="b20251107142600",
        )
        res = self._reply_start(req_mid, "收到，我现在去拉导出文件。")
        self.assertTrue(bool(res.message_id))
        reply_calls = self.lark.sent("+messages-reply")
        p2p_card_str = _opt(reply_calls[-1], "--content")
        self.assertIn("回复「停」可以终止", p2p_card_str)

        col = engine.Collector(self.cfg, self.store, self.source)
        # 主人本人（ME）在跟 Alice 的私聊里发一句「停」（非引用开工卡）不应误杀 Alice 的事项
        owner_msg_mid = self.lark.add(
            chat_id=p2p_chat,
            sender_id=ME,
            content="停",
            at=now8() + timedelta(seconds=30),
            chat_type="p2p",
            message_id="om_test_p2p_owner_say_stop",
        )
        col.ingest([parse_message(self.lark.raw(owner_msg_mid))])
        self.assertEqual(self.store.get_work_by_id(res.work_id)["state"], duty.DOING)

        # Alice 在私聊里回复一个「停」字 -> Collector.ingest 自动识别并触发 execute_run_stop
        stop_reply_mid = self.lark.add(
            chat_id=p2p_chat,
            sender_id=ALICE,
            content="停",
            at=now8() + timedelta(minutes=1),
            chat_type="p2p",
            message_id="om_test_p2p_stop_1",
        )
        self.lark.msgs[stop_reply_mid]["sender"]["name"] = "Alice"
        raw_msg = parse_message(self.lark.raw(stop_reply_mid))
        col.ingest([raw_msg])

        w_after = self.store.get_work_by_id(res.work_id)
        self.assertEqual(w_after["state"], duty.STOPPED)
        self.assertEqual(w_after["stopped_by"], ALICE)
        self.assertEqual(w_after["stopped_by_name"], "Alice")
        self.assertEqual(self.store.get_message(stop_reply_mid)["reason"], "p2p_stop_command")

    # ---------- 8. T1 证伪：摘掉钩子后进化巡检报警 + 无条件更新导致做完后被覆盖 ----------
    def test_demo145_8_t1_falsification_unconditional_update_and_evolve_post_stop_audit(self):
        # 证伪 A：把条件更新改成无条件更新（_unconditional_work_update_for_test = True），
        # 「做完后再点终止」就会把已 done 的事项错误覆盖成 stopped
        req_mid = self._seed_group_request(mid="om_test_t1_cond", batch_id="b20251107152500")
        res = self._reply_start(req_mid, "收到，开始处理。")
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107152500",
            {"status": "done", "summary": "已完成"},
        )
        self.d.dispatch_once()
        self.assertEqual(self.store.get_work_by_id(res.work_id)["state"], duty.DONE)

        def _unconditional_transition(work_id: int, from_state: str, to_state: str, **fields) -> bool:
            self.store.update_work(work_id, state=to_state, **fields)
            return True

        with mock.patch.object(self.store, "transition_work_state", side_effect=_unconditional_transition):
            ev_late = _make_callback_event(
                event_id="ev_t1_late_stop",
                operator_id=BOB,
                open_message_id=res.message_id,
                value={"sd_action": "run_stop", "work_id": res.work_id, "topic_id": "tp_alpha"},
            )
            card_callback.process_card_action_event(ev_late, cfg=self.cfg, store=self.store, source=self.source)
            # 无条件更新下，已 done 的事项被错误篡改为 stopped（证伪成立）
            self.assertEqual(self.store.get_work_by_id(res.work_id)["state"], duty.STOPPED)

        # 验证已取消的 Subagent 羔羊即使在母羊已交 stopped 回执后，仍被 pretool_check 拒绝
        self.store.create_lamb(
            lamb_id="lamb_post_receipt",
            parent_topic_id="tp_alpha",
            kind="subagent",
            task_title="check",
            conversation_id="cid-lamb-post-receipt-1",
        )
        self.store.update_lamb("lamb_post_receipt", state="cancelled", result_summary="已被用户终止")
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107152500",
            {"status": "stopped", "note": "已停"},
        )
        lamb_hook_res = hook.pretool_check(
            json.dumps(
                {
                    "conversationId": "cid-lamb-post-receipt-1",
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "echo still_running"},
                    },
                }
            ),
            cfg=self.cfg,
        )
        self.assertIsNotNone(lamb_hook_res)
        self.assertEqual(lamb_hook_res.get("decision"), "deny")

        # 证伪 B：模拟摘掉 PreToolUse 钩子后，会话在 stopped_at + 15s 仍成功跑了工具调用，
        # （且在 stopped_at + 5s 跑了真实 Antigravity 双重 JSON 编码的合法 sheepdog receipt 不应误报），
        # 以及另一个请求批次未发 --start 就在 t_disp + 10s 直接跑了危险命令，evolve 巡检必须检出并报警
        stopped_row = self.store.get_work_by_id(res.work_id)
        stop_dt = datetime.fromisoformat(stopped_row["stopped_at"])
        t_disp = (stop_dt + timedelta(minutes=1)).isoformat()
        alpha_cid = self.store.get_topic("tp_alpha")["conversation_id"]
        self._write_transcript(
            alpha_cid,
            [
                {
                    "step_index": 9,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": (stop_dt + timedelta(seconds=5)).isoformat(),
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {
                                "CommandLine": json.dumps(
                                    'sheepdog receipt --topic tp_alpha --batch b20251107152500 --json \'{"status":"stopped"}\''
                                )
                            },
                        }
                    ],
                },
                {
                    "step_index": 10,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": (stop_dt + timedelta(seconds=15)).isoformat(),
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "kubectl rollout restart deploy/api"},
                        }
                    ],
                },
                {
                    "step_index": 11,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": (stop_dt + timedelta(minutes=1, seconds=10)).isoformat(),
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "kubectl delete pod api-1"},
                        }
                    ],
                },
            ],
        )

        # 再造一个未发 --start 就在 t_disp + 10s 跑了危险命令的请求批次
        self.store.upsert_message(
            Message(
                message_id="om_test_no_start_req",
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="@Bot 重启下 staging",
                msg_type="text",
                create_time=t_disp,
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )
        self.store.record_dispatch("b20251107153500", "tp_alpha", ["om_test_no_start_req"])
        with self.store.tx() as c:
            c.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?", (t_disp, "b20251107153500"))
            c.execute("UPDATE messages SET batch_id='b20251107153500', dispatch_state='dispatched' WHERE message_id='om_test_no_start_req'")

        disc = evolve.audit_start_and_stop_discipline(
            self.cfg,
            self.store,
            (now8() - timedelta(hours=24)).isoformat(timespec="seconds"),
        )
        self.assertGreaterEqual(disc["post_stop_tool_calls_count"], 1)
        self.assertEqual(disc["post_stop_tool_calls"][0]["work_id"], res.work_id)
        self.assertGreaterEqual(disc["missing_start_card_count"], 1)
        self.assertIn("b20251107153500", [x["batch_id"] for x in disc["missing_start_card"]])

        self.cfg.sink = "dryrun"
        digest = evolve.build_evolve_digest(self.cfg, self.store, since_minutes=1440.0)
        self.assertFalse(digest["ok"])
        self.assertTrue(digest["needs_action"])
        self.assertTrue(any(r.startswith("post_stop_tool_calls") for r in digest["action_reasons"]))
        self.assertTrue(any(r.startswith("missing_start_card") for r in digest["action_reasons"]))
        warns = digest["d1_daemon_and_observe"]["warns"]
        self.assertTrue(any("终止后还在操作" in w for w in warns))
        self.assertTrue(any("没开工就动手" in w for w in warns))

    # ---------- 9. 独立审查复现与变异单测：R1 跨批次结清开工卡 + R2/M9 跨会话排队隔离 + hooks.json 规范 ----------
    def test_demo145_9_r1_cross_batch_settlement_r2_m9_cross_session_queue_isolation_and_hooks_json_schema(self):
        # (A) R1：会话在 b1 发开工卡并交 waiting，在 b2 收到回复后交 handled -> b1 的开工卡必须结清为 done 并改卡
        req_b1 = self._seed_group_request(mid="om_test_r1_b1", batch_id="b20251107162600")
        res_b1 = self._reply_start(req_b1, "收到，先等对方提供环境信息。")
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107162600",
            {"status": "waiting", "summary": "等待补充环境"},
        )
        self.d.dispatch_once()
        self.assertEqual(self.store.get_work_by_id(res_b1.work_id)["state"], duty.DOING)

        # b2 到来并交 handled
        self._seed_group_request(
            mid="om_test_r1_b2",
            text="@Bot 环境是 staging",
            batch_id="b20251107162700",
        )
        engine.write_receipt(
            self.cfg,
            "tp_alpha",
            "b20251107162700",
            {"status": "handled", "summary": "已按补充环境查清并回复"},
        )
        self.d.dispatch_once()
        w_b1_after = self.store.get_work_by_id(res_b1.work_id)
        self.assertEqual(w_b1_after["state"], duty.DONE)
        patched_mids = [mid for mid, _ in _patched_cards(self.lark.calls)]
        self.assertIn(res_b1.message_id, patched_mids)

        # (B) R2 & M9：请求人 Alice 在 tp_alpha 点终止时，只撤同一会话（tp_alpha）且同一聊天/话题、create_time <= stop_time 的排队消息，
        # 绝不误撤 Alice 刚发给 tp_beta 的排队消息、也绝不撤 stop_time 之后的新消息
        t_stop = now8()
        req_r2 = self._seed_group_request(mid="om_test_r2_alpha_req", batch_id="b20251107162800")
        res_r2 = self._reply_start(req_r2, "收到，正在处理 alpha 任务。")

        # 1) 同会话 tp_alpha、同群、stop_time 之前的排队消息 -> 应该被撤
        mid_alpha_pre = "om_test_r2_alpha_queued_pre"
        self.store.upsert_message(
            Message(
                message_id=mid_alpha_pre,
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="@Bot alpha 补充一句",
                msg_type="text",
                create_time=(t_stop - timedelta(seconds=5)).isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )
        # 2) 另一会话 tp_beta、同群、stop_time 之前的排队消息 -> 绝不能被撤（R2 / M9）
        mid_beta_pre = "om_test_r2_beta_queued_pre"
        self.store.upsert_message(
            Message(
                message_id=mid_beta_pre,
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="@Bot beta 帮我发一下版",
                msg_type="text",
                create_time=(t_stop - timedelta(seconds=3)).isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_beta",
        )
        # 3) 同会话 tp_alpha、但属于另一个群 oc_other_chat 的排队消息 -> 绝不能被撤
        mid_alpha_other_chat = "om_test_r2_alpha_other_chat"
        self.store.upsert_message(
            Message(
                message_id=mid_alpha_other_chat,
                chat_id="oc_other_group_chat",
                chat_name="别的群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="@Bot 别的群的请求",
                msg_type="text",
                create_time=(t_stop - timedelta(seconds=3)).isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )
        # 4) 同会话 tp_alpha、同群、但 create_time > stop_time 的后续新消息 -> 绝不能被撤
        mid_alpha_post = "om_test_r2_alpha_post_stop"
        self.store.upsert_message(
            Message(
                message_id=mid_alpha_post,
                chat_id=GROUP_CHAT,
                chat_name="研发协作测试群",
                chat_type="group",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                content="@Bot 停完之后的新问题",
                msg_type="text",
                create_time=(t_stop + timedelta(seconds=10)).isoformat(),
            ),
            DISPATCH,
            "at_bot",
            [],
            topic_id="tp_alpha",
        )

        # M9 证伪核对：如果按旧的 WHERE chat_id=? AND sender_id=? AND dispatch_state='pending' 查，
        # 会错误命中 tp_beta 的排队消息 mid_beta_pre 及 stop_time 之后的 mid_alpha_post
        unscoped_hits = [
            r["message_id"]
            for r in self.store.conn.execute(
                "SELECT message_id FROM messages WHERE chat_id=? AND sender_id=? AND dispatch_state='pending'",
                (GROUP_CHAT, ALICE),
            ).fetchall()
        ]
        self.assertIn(mid_beta_pre, unscoped_hits)
        self.assertIn(mid_alpha_post, unscoped_hits)

        # 执行 execute_run_stop（指定 stopped_at = t_stop.isoformat()）
        engine.execute_run_stop(
            self.cfg,
            self.store,
            self.source,
            work_row=self.store.get_work_by_id(res_r2.work_id),
            operator_id=ALICE,
            operator_name="Alice",
            stopped_at=t_stop.isoformat(),
            cb_msg_id="sd_card_cb_ev_r2_stop",
            chat_id=GROUP_CHAT,
            chat_type="group",
        )
        self.assertEqual(self.store.get_message(mid_alpha_pre)["route"], DROP)
        self.assertEqual(self.store.get_message(mid_beta_pre)["route"], DISPATCH)
        self.assertEqual(self.store.get_message(mid_beta_pre)["dispatch_state"], "pending")
        self.assertEqual(self.store.get_message(mid_alpha_other_chat)["route"], DISPATCH)
        self.assertEqual(self.store.get_message(mid_alpha_other_chat)["dispatch_state"], "pending")
        self.assertEqual(self.store.get_message(mid_alpha_post)["route"], DISPATCH)
        self.assertEqual(self.store.get_message(mid_alpha_post)["dispatch_state"], "pending")

        # (C) 核验 examples/antigravity-hooks.json：PreToolUse 为带 matcher 的分组结构，Stop 保持平铺结构，且 outbound.target_ok 不对非 managed 的 tp_sheepdog 放行
        hooks_path = Path(__file__).resolve().parents[1] / "examples" / "antigravity-hooks.json"
        hooks_data = json.loads(hooks_path.read_text(encoding="utf-8"))
        pre_hooks = hooks_data["sheepdog"]["PreToolUse"]
        self.assertIsInstance(pre_hooks, list)
        self.assertEqual(pre_hooks[0]["matcher"], "*")
        self.assertEqual(pre_hooks[0]["hooks"][0]["command"], "/Users/<you>/.local/bin/sheepdog hook pretool")
        stop_hooks = hooks_data["sheepdog"]["Stop"]
        self.assertIsInstance(stop_hooks, list)
        self.assertEqual((stop_hooks[0]["type"], stop_hooks[0]["timeout"]), ("command", 10))
        self.assertNotIn("matcher", stop_hooks[0])
        self.assertFalse(outbound.target_ok(self.store, "tp_sheepdog"))
