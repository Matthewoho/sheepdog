"""REG-018 ~ REG-019 飞书交互卡片回调端到端集成回归测试（7.73）。

覆盖：
1. BotEventConsumer 同时常驻消费 im.message.receive_v1 与 card.action.trigger 双流，共享 queue 与 arrived 实时信号；
2. Collector._poll_bot_events 接收卡片回调、落盘 messages 表 sd_card_cb_<event_id>、原地 patch_card 更新绿头卡片、投递并自动 answer_escalation 结案；
3. 双层幂等：同一 event_id WebSocket 重推忽略；主人连点同一张卡片产生不同 event_id 时第二次判为 card_callback_duplicate drop，目标会话仅收到唯一 1 次投递；
4. 双锚点零错路由与身份鉴权：非主人点击拦截为 card_callback_non_owner drop；多会话（如 tp_alpha 与羊圈副槽位 tp_laohu#2）并存时精准路由回 tp_laohu#2，且 running 态触发 priority 插话秒级送达。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from sheepdog.source.bot_events import CARD_ACTION_EVENT_KEY, DEFAULT_EVENT_KEYS, EVENT_KEY, BotEventConsumer
from sheepdog.store import now_iso
from test_identity import FakeProc, IdentityBase
from test_owner_card import card_of
from test_roster import ME
from test_threads import ALICE, ESC_CHAT, _opt


class CardCallbackE2ETest(IdentityBase):
    def _send_interactive_ask(self, topic_id: str, key: str, title: str, text: str, options: str = "auto") -> tuple[str, dict]:
        rc, out, err = self.cli(
            "ask", "--topic", topic_id, "--key", key,
            "--title", title, "--text", text, "--options", options,
        )
        self.assertEqual(rc, 0, out + err)
        sent_call = self.lark.sent()[-1]
        card = card_of(sent_call)
        card_mid = self.store.thread_by_key(topic_id, key)["root_message_id"]
        return card_mid, card

    def _patch_calls(self) -> list[list[str]]:
        return [c for c in self.lark.calls if c[:3] == ["im", "messages", "patch"]]

    def test_reg_018_bot_events_consumes_card_action_and_persists_message(self):
        # 1) 验证 BotEventConsumer 默认同时订阅 im.message.receive_v1 与 card.action.trigger 双事件流
        self.assertEqual(DEFAULT_EVENT_KEYS, (EVENT_KEY, CARD_ACTION_EVENT_KEY))
        card_mid, card = self._send_interactive_ask(
            "tp_alpha", "K-DEPLOY-773", "生产发版窗口",
            "1. A：今晚 22:00 全量发布\n2. B：明早 10:00 灰度\n3. C：暂缓本次发布",
        )
        btn_a_val = card["body"]["elements"][2]["behaviors"][0]["value"]
        cb_event = {
            "type": "card.action.trigger",
            "event_id": "ev_773_click_a",
            "message_id": card_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": btn_a_val,
            "timestamp": "1792680000000",
        }
        p_im = FakeProc([], ["[event] ready event_key=im.message.receive_v1"], pid=5101)
        p_cb = FakeProc([json.dumps(cb_event)], ["[event] ready event_key=card.action.trigger"], pid=5102)
        procs = [p_im, p_cb]
        started_cmds: list[list[str]] = []

        def dual_popen(cmd, **kw):
            started_cmds.append(cmd)
            return procs.pop(0)

        real_consumer = BotEventConsumer("lark-cli", 5, 40, popen=dual_popen)
        real_consumer.start()
        self.assertEqual(
            started_cmds,
            [
                ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
                ["lark-cli", "event", "consume", "card.action.trigger", "--as", "bot"],
            ],
        )
        self.assertTrue(real_consumer.wait(1.0))
        time.sleep(0.03)

        # 2) Collector._poll_bot_events 消费双流队列，落盘 messages 表并原地 patch_card 变绿
        c = self.collector()
        c.bot_events = real_consumer
        st = c._poll_bot_events()
        self.assertEqual(st["new"], 1)
        self.assertEqual(st["dispatch"], 1)
        self.assertNotIn("+messages-mget", self.lark.subcommands())

        cb_row = self.row("sd_card_cb_ev_773_click_a")
        self.assertIsNotNone(cb_row)
        self.assertEqual(
            (cb_row["route"], cb_row["reason"], cb_row["topic_id"], cb_row["dispatch_state"], cb_row["reply_to"]),
            ("dispatch", "owner_reply", "tp_alpha", "pending", card_mid),
        )
        self.assertEqual(cb_row["content"], "[卡片回调] A：今晚 22:00 全量发布")
        tags = json.loads(cb_row["tags_json"])
        self.assertIn("owner_verified", tags)
        self.assertIn("card_callback", tags)
        self.assertIn(f"escalation:{card_mid}", tags)

        patches = self._patch_calls()
        self.assertEqual(len(patches), 1)
        patched_card = json.loads(json.loads(_opt(patches[0], "--data"))["content"])
        self.assertEqual(patched_card["header"]["template"], "green")
        self.assertIn("✅ **已拍板：A · 今晚 22:00 全量发布**", json.dumps(patched_card, ensure_ascii=False))

        # 3) Dispatcher 投递给 tp_alpha 并自动 answer_escalation 结案
        prompt = self.deliver()
        self.assertIn("[卡片回调] A：今晚 22:00 全量发布", prompt)
        esc = self.store.get_escalation(card_mid)
        self.assertIsNotNone(esc)
        self.assertTrue(esc["answered_at"])
        self.assertEqual(esc["answer_message_id"], "sd_card_cb_ev_773_click_a")
        real_consumer.stop()

    def test_reg_018_duplicate_click_and_replay_idempotent_single_dispatch(self):
        card_mid, card = self._send_interactive_ask(
            "tp_alpha", "K-DUP-773", "数据库扩容确认",
            "1. A：批准扩容至 16C64G\n2. B：保持现状",
        )
        btn_a_val = card["body"]["elements"][2]["behaviors"][0]["value"]
        ev_first = {
            "type": "card.action.trigger",
            "event_id": "ev_773_first",
            "message_id": card_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": btn_a_val,
            "timestamp": "1792680100000",
        }
        # 同一 event_id WebSocket 重推 + 3 秒内主人第二次点击产生新 event_id
        ev_replay = dict(ev_first)
        ev_second_click = dict(
            ev_first,
            event_id="ev_773_second_click",
            action_value=card["body"]["elements"][3]["behaviors"][0]["value"],
            timestamp="1792680102000",
        )

        st1 = self.bot_hears(ev_first)
        self.assertEqual((st1["new"], st1["dispatch"]), (1, 1))
        self.assertEqual(len(self._patch_calls()), 1)

        # WebSocket 重推同一 event_id：完全幂等忽略
        st_replay = self.bot_hears(ev_replay)
        self.assertEqual(st_replay, {"fetched": 0})
        self.assertEqual(len(self._patch_calls()), 1)

        # 主人连点第二次（不同 event_id）：记入 messages 为 drop / card_callback_duplicate，不重复改卡、不重复入队
        st_dup = self.bot_hears(ev_second_click)
        self.assertEqual(st_dup, {"fetched": 0})
        dup_row = self.row("sd_card_cb_ev_773_second_click")
        self.assertIsNotNone(dup_row)
        self.assertEqual(
            (dup_row["route"], dup_row["reason"], dup_row["topic_id"], dup_row["dispatch_state"]),
            ("drop", "card_callback_duplicate", "tp_alpha", None),
        )
        self.assertEqual(len(self._patch_calls()), 1)

        # 目标会话 tp_alpha 只收到唯一 1 次投递
        before_cnt = len(self.sink.to("conv_test_alpha"))
        res = self.d.dispatch_once()
        self.assertEqual(res["topics"]["tp_alpha"]["sent"], 1)
        sent_alpha = self.sink.to("conv_test_alpha")[before_cnt:]
        self.assertEqual(len(sent_alpha), 1)
        self.assertIn("A：批准扩容至 16C64G", sent_alpha[0])

    def test_reg_019_dual_anchor_routes_exact_session_and_rejects_non_owner(self):
        # 准备多个活跃会话：tp_alpha、羊圈主槽位 tp_laohu 与 running 态副槽位 tp_laohu#2
        self.store.create_topic("tp_laohu", "[dynamic] 老胡 #1", "dynamic", "老胡主槽", "active", group_id="grp_laohu")
        self.store.update_topic("tp_laohu", conversation_id="conv_laohu_1")
        self.store.create_topic("tp_laohu#2", "[dynamic] 老胡 #2", "dynamic", "老胡副槽", "active", group_id="grp_laohu")
        self.store.update_topic("tp_laohu#2", conversation_id="conv_laohu_2")

        alpha_mid, _ = self._send_interactive_ask(
            "tp_alpha", "K-ALPHA-1", "Alpha 审批", "1. A：同意 Alpha\n2. B：拒绝 Alpha",
        )
        laohu2_mid, laohu2_card = self._send_interactive_ask(
            "tp_laohu#2", "K-LAOHU-2", "老胡副槽审批", "1. A：同意副槽方案\n2. B：调整副槽方案",
        )
        self.lark.calls.clear()
        before_alpha = len(self.sink.to("conv_test_alpha"))
        before_bus = len(self.sink.to("conv_test_bus"))

        # ① 群内/聊天内非主人（ALICE != ME）点击 tp_laohu#2 的卡片：被拦截为 card_callback_non_owner drop
        stranger_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_773_stranger",
            "message_id": laohu2_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ALICE,
            "action_tag": "button",
            "action_value": laohu2_card["body"]["elements"][2]["behaviors"][0]["value"],
            "timestamp": "1792680200000",
        }
        self.bot_hears(stranger_ev)
        stranger_row = self.row("sd_card_cb_ev_773_stranger")
        self.assertEqual((stranger_row["route"], stranger_row["reason"]), ("drop", "card_callback_non_owner"))
        self.assertEqual(self._patch_calls(), [])
        self.assertIsNone(self.store.get_escalation(laohu2_mid))
        self.assertEqual(self.store.pending_dispatch(), [])

        # ② & ③ 将 tp_laohu#2 置为 running 态（模拟正在跑长任务 b_running_laohu2），主人提交「其他：可输入」表单
        self.store.record_dispatch("b_running_laohu2", "tp_laohu#2", [])
        self.store.update_topic("tp_laohu#2", state="running", pending_batch_id="b_running_laohu2",
                                dispatched_at=now_iso())
        form_btn_val = laohu2_card["body"]["elements"][-1]["elements"][1]["value"]
        owner_other_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_773_owner_laohu2",
            "message_id": laohu2_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": form_btn_val,
            "form_value": {"other_input": "先在测试环境跑通冒烟再发生产"},
            "timestamp": "1792680210000",
        }
        st = self.bot_hears(owner_other_ev)
        self.assertEqual((st["new"], st["dispatch"]), (1, 1))
        cb_row = self.row("sd_card_cb_ev_773_owner_laohu2")
        self.assertEqual(
            (cb_row["route"], cb_row["reason"], cb_row["topic_id"]),
            ("dispatch", "owner_reply", "tp_laohu#2"),
        )

        # 执行投递：running 态的 tp_laohu#2 自动触发 priority 插话批次秒级送达，不串线到 tp_alpha / tp_laohu / tp_bus
        res = self.d.dispatch_once()
        t_res = res["topics"]["tp_laohu#2"]
        self.assertTrue(t_res.get("priority", "").startswith("p"), t_res)
        self.assertEqual(t_res["sent"], 1)
        self.assertEqual(self.sink.to("conv_test_alpha")[before_alpha:], [])
        self.assertEqual(self.sink.to("conv_laohu_1"), [])
        self.assertEqual(self.sink.to("conv_test_bus")[before_bus:], [])
        self.assertEqual(len(self.sink.to("conv_laohu_2")), 1)
        self.assertIn("[卡片回调] 其他：先在测试环境跑通冒烟再发生产", self.sink.to("conv_laohu_2")[0])

        # tp_laohu#2 的提问已自动标为 answered，而 tp_alpha 的提问仍保持未结
        esc_laohu2 = self.store.get_escalation(laohu2_mid)
        self.assertTrue(esc_laohu2["answered_at"])
        self.assertEqual(esc_laohu2["answer_message_id"], "sd_card_cb_ev_773_owner_laohu2")
        self.assertIsNone(self.store.get_escalation(alpha_mid))


if __name__ == "__main__":
    unittest.main()
