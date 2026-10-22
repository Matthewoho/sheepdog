"""REG-020 ~ REG-022 话题群因果防串线、精简卡片布局、未建档话题兜底锚定与回音防串线回归测试（7.74）。

覆盖：
1. REG-020：Card JSON 2.0 精简交互布局（attach_ask_interactions / build_interactive_elements）：
   - 选项按钮收入单个 column_set（flex_mode=flow, horizontal_spacing=8px, vertical_spacing=8px），首项 primary 其余 default，width=default，超 20 字截断标签但完整保留 option_text；
   - 「其他」表单 sd_other_form 内嵌两列 column_set（左列 weight=1 含 input width=fill 且彻底移除 max_length，右列 auto 含「提交」按钮）。
2. REG-021：话题群卡片回调目标解析（resolve_callback_target）、账本落盘（record_card_callback_to_ledger）、端到端点击回传（带 item:T-n 标签、绿卡回写、自动结案）与私聊卡片一键「放进话题群」（tg_move）。
3. REG-022：话题群未建档话题主人回复自动兜底建档 T-n（杜绝悬空无主 thread）+ Dispatcher._echo_sync 话题群回音账本防私聊外溢误销账。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from sheepdog import topicgroup as tg
from sheepdog.card_callback import (
    attach_ask_interactions,
    build_interactive_elements,
    process_card_action_event,
    record_card_callback_to_ledger,
    resolve_callback_target,
)
from sheepdog.engine import Collector
from sheepdog.outbound import build_owner_card
from test_owner_card import card_of
from test_roster import ME
from test_threads import ESC_CHAT, _opt
from test_topic_group import TG, TGBase


class TopicGroupOrderE2ETest(TGBase):
    def _ingest_bot(self, msg):
        return Collector(self.cfg, self.store, self.source, self.roster).ingest([msg], received_by="bot")

    def test_reg_020_compact_card_json_v2_layout_and_no_max_length(self):
        long_opt = "立即执行全量生产发布并同步更新回滚预案与监控大盘及告警规则"
        body = f"🐕 需要你定 · 灰度发布窗口 [tp_alpha]\n请拍板：\n1. A：{long_opt}\n2. B：推迟到明早 10:00"
        card = build_owner_card(body)
        self.assertIsNotNone(card)
        patched = attach_ask_interactions(
            card, text=body, topic_id="tp_alpha", key="K-774-UI", title="灰度发布窗口", options_spec="auto",
        )
        elems = patched["body"]["elements"]
        self.assertEqual([e["tag"] for e in elems], ["markdown", "hr", "column_set", "form"])

        col_set = elems[2]
        self.assertEqual(
            (col_set["flex_mode"], col_set["horizontal_spacing"], col_set["margin"]),
            ("flow", "8px", "4px 0px 0px 0px"),
        )
        self.assertNotIn("vertical_spacing", col_set)
        self.assertEqual(len(col_set["columns"]), 2)
        col_a, col_b = col_set["columns"]
        self.assertEqual((col_a["width"], col_b["width"]), ("auto", "auto"))
        btn_a, btn_b = col_a["elements"][0], col_b["elements"][0]
        self.assertEqual((btn_a["type"], btn_a["size"], btn_a["width"]), ("primary", "medium", "default"))
        self.assertEqual((btn_b["type"], btn_b["size"], btn_b["width"]), ("default", "medium", "default"))
        self.assertTrue(btn_a["text"]["content"].endswith("…"))
        self.assertLessEqual(len(btn_a["text"]["content"]), 28)
        val_a = btn_a["behaviors"][0]["value"]
        self.assertEqual(
            (val_a["topic_id"], val_a["key"], val_a["choice"], val_a["reply_text"]),
            ("tp_alpha", "K-774-UI", "A", f"A：{long_opt}"),
        )

        form_el = elems[3]
        self.assertEqual(form_el["name"], "sd_other_form")
        self.assertEqual(len(form_el["elements"]), 1)
        form_cs = form_el["elements"][0]
        self.assertEqual((form_cs["tag"], form_cs["flex_mode"], len(form_cs["columns"])), ("column_set", "none", 2))
        left_col, right_col = form_cs["columns"]
        self.assertEqual((left_col["width"], left_col["weight"]), ("weighted", 1))
        inp = left_col["elements"][0]
        self.assertEqual((inp["tag"], inp["name"], inp["width"]), ("input", "other_input", "fill"))
        self.assertEqual(inp["placeholder"]["content"], "补充说明或直接拍板...")
        self.assertNotIn("max_length", inp)
        self.assertNotIn("max_length", json.dumps(patched, ensure_ascii=False))
        sub = right_col["elements"][0]
        self.assertEqual(
            (sub["tag"], sub["name"], sub["text"]["content"], sub["type"], sub["size"], sub["width"], sub["form_action_type"]),
            ("button", "other_submit", "提交", "primary", "medium", "default", "submit"),
        )
        self.assertEqual((sub["value"]["kind"], sub["value"]["topic_id"], sub["value"]["key"]), ("other", "tp_alpha", "K-774-UI"))

        built = build_interactive_elements([("A", "同意")], topic_id="tp_alpha", key="K-774-UI", title="灰度发布窗口")
        self.assertEqual([e["tag"] for e in built], ["hr", "column_set", "form"])

    def test_reg_021_topic_group_card_callback_routing_and_one_click_move(self):
        rc, out, err = self.ask(
            "--key", "TG-CB-774", "--track", "--options", "auto",
            title="蓝绿切流拍板", text="1. A：立即切流 100%\n2. B：先灰度 10%",
        )
        self.assertEqual(rc, 0, out + err)
        it = self.item("TG-CB-774")
        self.assertIsNotNone(it)
        tg_calls = self.lark.tg_sent()
        ask_call = tg_calls[-1]
        ask_card = card_of(ask_call)
        ask_sends = [
            r for r in self.store.conn.execute("SELECT * FROM sends WHERE topic_id='tp_alpha' AND kind='ask'")
            if r["root_message_id"] == it["root_message_id"]
        ]
        self.assertEqual(len(ask_sends), 1)
        card_mid = ask_sends[0]["message_id"]

        col_set = next(e for e in ask_card["body"]["elements"] if e.get("tag") == "column_set")
        btn_a_val = col_set["columns"][0]["elements"][0]["behaviors"][0]["value"]
        tgt = resolve_callback_target(self.store, card_mid, btn_a_val)
        self.assertEqual((tgt["topic_id"], tgt["owner_topic_id"], tgt["item_no"]), ("tp_alpha", "tp_alpha", it["item_no"]))

        cb_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_774_tg_click_a",
            "message_id": card_mid,
            "chat_id": TG,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": btn_a_val,
            "timestamp": "1792700000000",
        }
        msg = process_card_action_event(cb_ev, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(msg)
        self._ingest_bot(msg)

        row = self.row("sd_card_cb_ev_774_tg_click_a")
        self.assertIsNotNone(row)
        self.assertEqual((row["route"], row["reason"], row["topic_id"]), ("dispatch", "owner_reply", "tp_alpha"))
        tags = json.loads(row["tags_json"])
        self.assertIn("card_callback", tags)
        self.assertIn(f"item:T-{it['item_no']}", tags)
        self.assertIn(f"escalation:{card_mid}", tags)
        self.assertIn(card_mid, self.lark.patched)
        self.assertEqual(self.lark.patched[card_mid][-1]["header"]["template"], "green")

        self.d.dispatch_once()
        self.assertIn("[卡片回调] A：立即切流 100%", self.batch_for("conv_test_alpha"))
        self.assertTrue(self.store.get_escalation(card_mid)["answered_at"])

        # 验证 record_card_callback_to_ledger 独立调用也能为话题群卡片打上 item:T-n 标签
        direct_msg = record_card_callback_to_ledger(self.store, msg, target=tgt, value=btn_a_val)
        self.assertIsNotNone(direct_msg)
        self.assertIn(f"item:T-{it['item_no']}", json.loads(self.row(msg.message_id)["tags_json"]))

        # 验证私聊卡片一键「放进话题群」（tg_move）回调（使用未持有 T-1 因果锁的 tp_bus）
        rc2, out2, err2 = self.ask("--key", "DM-MOVE-774", topic="tp_bus", title="数据库迁移", text="今晚执行迁移吗？")
        self.assertEqual(rc2, 0, out2 + err2)
        dm_thr = self.store.thread_by_key("tp_bus", "DM-MOVE-774")
        move_ev = {
            "type": "card.action.trigger",
            "event_id": "ev_774_tg_move",
            "message_id": dm_thr["root_message_id"],
            "chat_id": ESC_CHAT,
            "operator_id": ME,
            "action_tag": "button",
            "action_value": {"kind": "tg_move", "topic_id": "tp_bus", "key": "DM-MOVE-774", "title": "数据库迁移"},
            "timestamp": "1792700010000",
        }
        move_msg = process_card_action_event(move_ev, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(move_msg)
        self._ingest_bot(move_msg)
        moved_it = self.item("DM-MOVE-774")
        self.assertIsNotNone(moved_it)
        move_row = self.row("sd_card_cb_ev_774_tg_move")
        self.assertEqual((move_row["route"], move_row["reason"]), ("drop", "tg_move"))
        self.assertIn(f"item:T-{moved_it['item_no']}", json.loads(move_row["tags_json"]))

    def test_reg_022_unregistered_thread_auto_anchor_and_echo_sync_isolation(self):
        # 1) 话题群中某条尚未建档为 tracked_item 的顶层消息，主人在其 thread 内回复时自动兜底建档 T-n 并绑定 thread_id
        untracked_root = self.lark.add(chat_id=TG, sender_id=ME, content="历史未建档话题开头", chat_type="topic")
        reply_mid = self.owner_tg("这个话题帮我跟进一下", root=untracked_root)
        self.pull(reply_mid)
        r = self.row(reply_mid)
        self.assertEqual(r["route"], "dispatch")
        self.assertEqual(r["reason"], "owner_reply")
        tags = json.loads(r["tags_json"])
        item_tags = [t for t in tags if t.startswith("item:T-")]
        self.assertEqual(len(item_tags), 1, tags)
        auto_it = self.store.item_by_thread(self.lark.msgs[reply_mid]["thread_id"])
        self.assertIsNotNone(auto_it)
        self.assertEqual(f"item:T-{auto_it['item_no']}", item_tags[0])
        self.assertEqual(auto_it["thread_id"], self.lark.msgs[reply_mid]["thread_id"])

        # 2) Dispatcher._echo_sync：话题群里的 owner_echo 绝不被会话发往私聊的 report 误销账；发回话题群后才正常销账
        rc, out, err = self.ask("--key", "ECHO-TG-774", "--track", title="回音隔离测试", text="今晚发版吗？")
        self.assertEqual(rc, 0, out + err)
        self.poll()
        self.d.dispatch_once()
        it = self.item("ECHO-TG-774")
        owner_mid = self.owner_tg("今晚 23:00 准时发", root=it["root_message_id"])
        self.poll()
        self.d.dispatch_once()
        echo_row = self.store.echo_get(owner_mid)
        self.assertEqual((echo_row["stage"], echo_row["settled_by"]), ("delivered", None))

        # 会话往私聊发了一条与该话题群无关（或外溢到私聊）的 report：不能把话题群的 owner_echo 销账！
        self.store.add_send("k_dm_leak_774", "report", "tp_alpha", "om_dm_leak_774", "私聊汇报一条别的事")
        self.d.dispatch_once()
        echo_after_dm = self.store.echo_get(owner_mid)
        self.assertEqual((echo_after_dm["stage"], echo_after_dm["settled_by"]), ("delivered", None))

        # 会话往话题群事项 it 的 thread 里发 report：正常将 owner_echo 销账为 report
        self.store.add_send(
            "k_tg_rep_774", "report", "tp_alpha", "om_tg_rep_774", "收到，今晚 23:00 发版",
            root_message_id=it["root_message_id"], key="ECHO-TG-774", norm_key="ECHO-TG-774",
        )
        self.d.dispatch_once()
        echo_after_tg = self.store.echo_get(owner_mid)
        self.assertEqual((echo_after_tg["stage"], echo_after_tg["settled_by"]), ("settled", "report"))


if __name__ == "__main__":
    unittest.main()
