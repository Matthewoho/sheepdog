"""REG-047 & REG-048 回归测试：forward --answers 双端绿卡同步与非卡片已答提问点击回调原地自愈（7.85）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sheepdog import cli, decision_hub, outbound
from sheepdog.card_callback import process_card_action_event
from sheepdog.config import Config, TopicGroupConfig
from sheepdog.models import Message
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
TG_CHAT = "oc_test_tg"
EXT_CHAT = "oc_test_ext_group"


class FakeSource785:
    """轻量飞书假消息源：记录发群、话题回复、改卡与置顶。"""

    def __init__(self) -> None:
        self.chat_sends: list[dict] = []
        self.replies: list[dict] = []
        self.patched: dict[str, list[dict]] = {}
        self.pins: list[str] = []

    def send_to_chat(
        self, chat_id: str, text: str, idem_key: str = "", identity: str = "bot", *, card: dict | None = None
    ) -> dict:
        mid = f"om_785_chat_{len(self.chat_sends) + 1}"
        thr = f"omt_785_chat_{len(self.chat_sends) + 1}"
        self.chat_sends.append(
            {"chat_id": chat_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "message_id": mid, "thread_id": thr}
        )
        return {"message_id": mid, "thread_id": thr}

    def reply_to(
        self, message_id: str, *, identity: str = "bot", idem_key: str = "", text: str = "", card: dict | None = None, in_thread: bool = False
    ) -> dict:
        mid = f"om_785_rep_{len(self.replies) + 1}"
        thr = f"omt_785_rep_{len(self.replies) + 1}"
        self.replies.append(
            {"target": message_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "in_thread": in_thread, "message_id": mid, "thread_id": thr}
        )
        return {"message_id": mid, "thread_id": thr}

    def patch_card(self, message_id: str, card: dict, *, identity: str = "bot") -> None:
        self.patched.setdefault(message_id, []).append(card)

    def pin(self, message_id: str) -> None:
        self.pins.append(message_id)


class TestForwardAnswersAndCardHeal785(unittest.TestCase):
    """REG-047 & REG-048：跨通道关结双端绿卡同步与卡片回调防重分支原地自愈。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        cfg_path = self.tmp_path / "config.toml"
        cfg_path.write_text('[notify]\nowner_card = true\n', encoding="utf-8")
        self.cfg = Config(state_dir=self.tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        self.cfg.source.enabled = True
        self.cfg.identity.enabled = True
        self.cfg.notify.owner_card = True
        self.cfg.notify.require_summary = False
        self.cfg.topic_group = TopicGroupConfig(chat_id=TG_CHAT, enabled=True)
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.store.create_topic("tp_bus", "总线", "bus", "总线职责", "active")
        self.store.create_topic("tp_3x-ui-ops", "家宽 VPS 与 3x-ui 运维", "adopted", "3x-ui 职责", "active")
        self.source = FakeSource785()
        self.hub_meta = decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)

    def test_reg_047_forward_answers_patches_dual_green_cards(self) -> None:
        ask = outbound.send_to_owner(
            self.cfg, self.store, self.source, "ask", "tp_3x-ui-ops",
            "US-SJC-1 流量重置确认", "选哪个？\nA: 立即重置\nB: 下月重置", key="3XUI-QUOTA", track=True,
        )
        ask_mid = ask.message_id
        esc_before = self.store.get_escalation(ask_mid)
        self.assertIsNotNone(esc_before)
        hub_mid = esc_before["hub_message_id"]
        self.assertTrue(hub_mid.startswith("om_785_rep_"))

        owner_msg = Message(
            message_id="om_owner_reset_1", chat_id=EXT_CHAT, chat_name="VPS 运维群", chat_type="group",
            sender_id=OWNER_ID, sender_name="Matthew Hong", sender_type="user",
            content="@Mattheuu 已经重置流量了", msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(owner_msg, "dispatch", "at_me", ["owner_verified"], topic_id="tp_bus")

        args = argparse.Namespace(
            topic="tp_3x-ui-ops", message_ids="om_owner_reset_1", note="", quote="", answers=ask_mid
        )
        with (
            mock.patch.object(cli, "_load_cfg", return_value=self.cfg),
            mock.patch.object(cli, "_source", return_value=self.source),
        ):
            rc = cli.cmd_forward(args)
        self.assertEqual(rc, 0)

        esc_after = self.store.get_escalation(ask_mid)
        self.assertIsNotNone(esc_after["answered_at"])
        self.assertEqual(esc_after["answer_message_id"], "om_owner_reset_1")
        self.assertEqual(esc_after["close_reason"], "answered")

        fwd_row = self.store.get_message("om_owner_reset_1")
        self.assertEqual(fwd_row["topic_id"], "tp_3x-ui-ops")
        tags = json.loads(fwd_row["tags_json"] or "[]")
        self.assertIn(f"escalation:{ask_mid}", tags)

        self.assertIn(ask_mid, self.source.patched)
        self.assertIn(hub_mid, self.source.patched)
        self.assertEqual(self.source.patched[ask_mid][-1]["header"]["template"], "green")
        self.assertEqual(self.source.patched[hub_mid][-1]["header"]["template"], "green")
        self.assertIn("已经重置流量了", json.dumps(self.source.patched[ask_mid][-1], ensure_ascii=False))
        self.assertIn("已经重置流量了", json.dumps(self.source.patched[hub_mid][-1], ensure_ascii=False))
        hub_root = self.hub_meta["root_message_id"]
        self.assertIn("当前 0 项待拍板", json.dumps(self.source.patched[hub_root][-1], ensure_ascii=False))

    def test_reg_048_card_callback_on_non_card_answered_escalation_self_heals_green_card(self) -> None:
        ask = outbound.send_to_owner(
            self.cfg, self.store, self.source, "ask", "tp_3x-ui-ops",
            "US-SJC-1 流量重置确认", "选哪个？\nA: 立即重置\nB: 下月重置", key="3XUI-QUOTA", track=True,
        )
        ask_mid = ask.message_id
        hub_mid = self.store.get_escalation(ask_mid)["hub_message_id"]

        # 模拟此前已通过非卡片回调路径（prev_cb is None）在 SQLite 标为 answered_at / closed_at，但飞书仍残留橙卡
        self.store.answer_escalation(ask_mid, "om_non_card_ans_1")
        self.source.patched.clear()

        ev_other = {
            "type": "card.action.trigger",
            "event_id": "ev_other_heal_1",
            "message_id": ask_mid,
            "chat_id": TG_CHAT,
            "operator_id": OWNER_ID,
            "action_tag": "form_submit",
            "action_value": {
                "sd_cb": "1", "kind": "other", "topic_id": "tp_3x-ui-ops", "key": "3XUI-QUOTA", "message_id": ask_mid
            },
            "form_value": {"other_input": "我已经处理了"},
        }
        res = process_card_action_event(ev_other, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNone(res)

        dup_row = self.store.get_message("sd_card_cb_ev_other_heal_1")
        self.assertIsNotNone(dup_row)
        self.assertEqual(dup_row["route"], "drop")
        self.assertEqual(dup_row["reason"], "card_callback_duplicate")

        self.assertIn(ask_mid, self.source.patched)
        self.assertIn(hub_mid, self.source.patched)
        self.assertEqual(self.source.patched[ask_mid][-1]["header"]["template"], "green")
        self.assertEqual(self.source.patched[hub_mid][-1]["header"]["template"], "green")
        self.assertIn("我已经处理了", json.dumps(self.source.patched[ask_mid][-1], ensure_ascii=False))
        self.assertIn("我已经处理了", json.dumps(self.source.patched[hub_mid][-1], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
