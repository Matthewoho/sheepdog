"""REG-017 ~ REG-019 卡片交互组件构造、选项解析与回调处理单元测试（7.73）。"""

from __future__ import annotations

import json
import unittest

from sheepdog.card import MORE_DELIMITER, build_owner_card
from sheepdog.card_callback import (
    AskOption,
    attach_ask_interactions,
    build_answered_owner_card,
    is_card_action_event,
    parse_ask_options,
    process_card_action_event,
)
from test_owner_card import MD, PROD_ATTR_RX, PROD_HEADER_RX, OwnerCardBase, card_of
from test_roster import ME
from test_threads import ALICE, ESC_CHAT, _opt


class CardCallbackUnitTest(OwnerCardBase):
    def test_reg_017_parse_ask_options_auto_and_explicit(self):
        # 1) 自动提取列表格式 A..C
        list_text = (
            "🐕 需要你定 · 发版窗口 [tp_alpha]\n"
            "**请拍板**：\n"
            "1. A：周五晚 22:00 全量发布\n"
            "2. B：下周一早 10:00 灰度\n"
            "3. C：暂缓本次发布\n"
        )
        opts = parse_ask_options(list_text)
        self.assertEqual(
            opts,
            [
                AskOption("A", "周五晚 22:00 全量发布", "A：周五晚 22:00 全量发布"),
                AskOption("B", "下周一早 10:00 灰度", "B：下周一早 10:00 灰度"),
                AskOption("C", "暂缓本次发布", "C：暂缓本次发布"),
            ],
        )

        # 2) 自动提取句内并列格式
        inline_text = "**已切 10% 灰度。**\n要你定：回 A 今晚全量，回 B 保持 10% 观察。"
        opts_inline = parse_ask_options(inline_text)
        self.assertEqual(
            opts_inline,
            [
                AskOption("A", "今晚全量", "A：今晚全量"),
                AskOption("B", "保持 10% 观察", "B：保持 10% 观察"),
            ],
        )

        # 3) 显式 --options 传参（带 A:/B: 前缀与无前缀自动分配）
        exp1 = parse_ask_options("普通正文无选项", "A:同意发布|B:暂缓执行")
        self.assertEqual(
            exp1,
            [
                AskOption("A", "同意发布", "A：同意发布"),
                AskOption("B", "暂缓执行", "B：暂缓执行"),
            ],
        )
        exp2 = parse_ask_options("普通正文", "立即全量；继续灰度；立即回滚")
        self.assertEqual([o.key for o in exp2], ["A", "B", "C"])
        self.assertEqual(exp2[0].reply_text, "A：立即全量")

        # 4) --options none / off / false / 0 显式禁用
        for disabled in ("none", "NONE", "off", "false", "0"):
            self.assertEqual(parse_ask_options(list_text, disabled), [])

        # 5) 代码块内假选项忽略 & 只有单项 A 不误判
        code_fake = "说明如下：\n```yaml\nA: fake_opt_a\nB: fake_opt_b\n```\n请知悉。"
        self.assertEqual(parse_ask_options(code_fake), [])
        single_a = "1. A：只有一个 A 选项没有 B"
        self.assertEqual(parse_ask_options(single_a), [])

        # 6) auto 模式下提取「回『...』」口令或回退默认 A/B
        quote_cmd = "请点名确认：回「同意发布 model-manager-server v2.46.0 到生产」"
        q_opts = parse_ask_options(quote_cmd, "auto")
        self.assertEqual(len(q_opts), 2)
        self.assertEqual(q_opts[0].key, "A")
        self.assertEqual(q_opts[0].reply_text, "同意发布 model-manager-server v2.46.0 到生产")
        self.assertEqual(q_opts[1], AskOption("B", "暂缓执行", "B：暂缓执行"))

    def test_reg_017_ask_card_renders_buttons_and_other_form_and_updates_card(self):
        summary = "**灰度已稳，全量切流等你拍板。**\n回 A 今晚全量，回 B 保持 10% 观察。"
        detail = "### 监控指标\n- P99: 110ms\n```bash\nkubectl get pods\n```"
        composed = f"🐕 需要你定 · 全量切流 [tp_alpha]\n{summary}\n{MORE_DELIMITER}\n{detail}"

        base_card = build_owner_card(composed)
        card = attach_ask_interactions(
            base_card,
            text=composed,
            topic_id="tp_alpha",
            key="K-CANARY",
            title="全量切流",
            interactive=True,
        )
        els = card["body"]["elements"]
        # 顶层结构：[summary_md, collapsible_panel(more), hr, column_set(options), form(sd_other_form)]
        self.assertEqual(els[0], {"tag": "markdown", "content": summary})
        self.assertEqual(els[1]["tag"], "collapsible_panel")
        self.assertEqual(els[2], {"tag": "hr"})

        opt_cs = els[3]
        self.assertEqual((opt_cs["tag"], opt_cs["flex_mode"]), ("column_set", "flow"))
        btn_a = opt_cs["columns"][0]["elements"][0]
        btn_b = opt_cs["columns"][1]["elements"][0]
        self.assertEqual((btn_a["tag"], btn_a["element_id"], btn_a["type"], btn_a["width"]), ("button", "sd_opt_A", "primary", "default"))
        self.assertEqual((btn_b["tag"], btn_b["element_id"], btn_b["type"], btn_b["width"]), ("button", "sd_opt_B", "default", "default"))
        cb_a = btn_a["behaviors"][0]
        self.assertEqual(cb_a["type"], "callback")
        self.assertEqual(
            cb_a["value"],
            {
                "sd_cb": "1",
                "kind": "option",
                "choice": "A",
                "label": "今晚全量",
                "reply_text": "A：今晚全量",
                "topic_id": "tp_alpha",
                "key": "K-CANARY",
                "title": "全量切流",
            },
        )

        # 根节点 form 容器（绝不嵌套在 collapsible_panel 内，同行双列 column_set 消除 max_length）
        form_el = els[4]
        self.assertEqual((form_el["tag"], form_el["name"], form_el["element_id"]), ("form", "sd_other_form", "sd_form_other"))
        self.assertNotIn('"tag": "form"', json.dumps(els[1], ensure_ascii=False))
        form_row = form_el["elements"][0]
        self.assertEqual((form_row["tag"], form_row["flex_mode"]), ("column_set", "none"))
        inp = form_row["columns"][0]["elements"][0]
        sub_btn = form_row["columns"][1]["elements"][0]
        self.assertEqual((inp["tag"], inp["name"], inp["width"], inp["required"]), ("input", "other_input", "fill", True))
        self.assertNotIn("max_length", inp)
        self.assertEqual((sub_btn["tag"], sub_btn["name"], sub_btn["form_action_type"], sub_btn["width"]), ("button", "other_submit", "submit", "default"))
        self.assertEqual(sub_btn["value"]["kind"], "other")
        self.assertEqual(sub_btn["value"]["topic_id"], "tp_alpha")

        # 原地回写已拍板卡片：变绿、保留 header + summary + more、移除全部按钮与表单、追加 ✅ 已拍板
        answered = build_answered_owner_card(
            composed,
            chosen_display="A · 今晚全量",
            topic_id="tp_alpha",
            answered_at="2026-10-22T22:30:00+08:00",
        )
        self.assertEqual(answered["header"]["template"], "green")
        self.assertEqual(answered["header"]["title"]["content"], "🐕 需要你定 · 全量切流 [tp_alpha]")
        ans_els = answered["body"]["elements"]
        self.assertEqual(ans_els[0], {"tag": "markdown", "content": summary})
        self.assertEqual(ans_els[1]["tag"], "collapsible_panel")
        self.assertEqual(ans_els[2], {"tag": "hr"})
        self.assertIn("✅ **已拍板：A · 今晚全量**", ans_els[3]["content"])
        self.assertIn("`tp_alpha`", ans_els[3]["content"])
        dumped = json.dumps(answered, ensure_ascii=False)
        self.assertNotIn('"tag": "button"', dumped)
        self.assertNotIn('"tag": "form"', dumped)

    def test_reg_017_cli_ask_options_and_callback_unit_guards(self):
        # CLI 显式传 --options 时，即使 identity.enabled=False 也注入按钮与表单
        rc, out, err = self.cli(
            "ask", "--topic", "tp_alpha", "--key", "K-OPT",
            "--title", "发版确认", "--text", "是否立即发布 v2.46？",
            "--options", "A:立即发布|B:暂缓",
        )
        self.assertEqual(rc, 0, out + err)
        sent_call = self.sends()[-1]
        c = card_of(sent_call)
        tags = [e["tag"] for e in c["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "hr", "column_set", "form"])

        card_mid = self.store.thread_by_key("tp_alpha", "K-OPT")["root_message_id"]
        raw_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_cb_unit_1",
            "message_id": card_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": c["body"]["elements"][2]["columns"][0]["elements"][0]["behaviors"][0]["value"],
            "timestamp": "1792679400000",
        }
        self.assertTrue(is_card_action_event(raw_ev))

        # 1) 非主人点击（REG-019）：拦截留痕，不改卡、不返回 Message
        bad_ev = dict(raw_ev, event_id="ev_cb_non_owner", operator_id=ALICE)
        self.assertIsNone(process_card_action_event(bad_ev, cfg=self.cfg, store=self.store, source=self.source))
        bad_row = self.store.get_message("sd_card_cb_ev_cb_non_owner")
        self.assertEqual((bad_row["route"], bad_row["reason"]), ("drop", "card_callback_non_owner"))

        # 2) 主人正常点击：补齐 escalation、回写绿卡（patch_card）、返回归一化 Message
        msg = process_card_action_event(raw_ev, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_id, "sd_card_cb_ev_cb_unit_1")
        self.assertEqual(msg.reply_to, card_mid)
        self.assertEqual(msg.content, "[卡片回调] A：立即发布")
        self.assertEqual(msg.raw["_sd_card_callback"]["topic_id"], "tp_alpha")
        patch_calls = [c for c in self.lark.calls if c[:3] == ["im", "messages", "patch"]]
        self.assertEqual(len(patch_calls), 1)
        patched_card = json.loads(json.loads(_opt(patch_calls[0], "--data"))["content"])
        self.assertEqual(patched_card["header"]["template"], "green")

        # 3) 第一层幂等（同 event_id 重放）：直接返回 None
        self.store.upsert_message(msg, "dispatch", "owner_reply", ["owner_verified"], topic_id="tp_alpha", received_by="bot")
        self.assertIsNone(process_card_action_event(raw_ev, cfg=self.cfg, store=self.store, source=self.source))

        # 4) 第二层幂等（REG-018，不同 event_id 连点同一已拍板卡片）：drop 留痕且不重复改卡
        dup_ev = dict(raw_ev, event_id="ev_cb_unit_dup2")
        self.assertIsNone(process_card_action_event(dup_ev, cfg=self.cfg, store=self.store, source=self.source))
        dup_row = self.store.get_message("sd_card_cb_ev_cb_unit_dup2")
        self.assertEqual((dup_row["route"], dup_row["reason"]), ("drop", "card_callback_duplicate"))
        self.assertEqual(len([c for c in self.lark.calls if c[:3] == ["im", "messages", "patch"]]), 1)

    def test_reg_018_and_019_other_form_and_closed_slot_revival(self):
        # 羊圈子槽位 tp_alpha#2 发出提问后被标记为 closed；主人提交「其他：可输入」表单时原地重开并路由回 tp_alpha#2
        self.store.create_topic("tp_alpha#2", "[managed] Alpha #2", "adopted", "职责", "active", group_id="grp_alpha")
        rc, out, err = self.cli(
            "ask", "--topic", "tp_alpha#2", "--key", "K-OTHER",
            "--title", "灰度策略", "--text", "1. A：全量\n2. B：回滚", "--options", "auto",
        )
        self.assertEqual(rc, 0, out + err)
        card_mid = self.store.thread_by_key("tp_alpha#2", "K-OTHER")["root_message_id"]
        self.store.update_topic("tp_alpha#2", state="closed")

        other_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_cb_other_1",
            "message_id": card_mid,
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": {"sd_cb": "1", "kind": "other", "topic_id": "tp_alpha#2", "key": "K-OTHER", "title": "灰度策略"},
            "form_value": {"other_input": "  先切 25% 观察 30 分钟  "},
            "timestamp": "2026-10-22T22:35:00+08:00",
        }
        msg = process_card_action_event(other_ev, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "[卡片回调] 其他：先切 25% 观察 30 分钟")
        self.assertEqual(msg.raw["_sd_card_callback"]["topic_id"], "tp_alpha#2")
        self.assertEqual(msg.raw["_sd_card_callback"]["chosen_display"], "其他：先切 25% 观察 30 分钟")
        self.assertEqual(self.store.get_topic("tp_alpha#2")["state"], "active")
        self.assertEqual(self.store.get_escalation(card_mid)["topic_id"], "tp_alpha#2")


if __name__ == "__main__":
    unittest.main()
