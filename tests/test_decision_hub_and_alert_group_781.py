"""REG-034 ~ REG-037 回归测试：独立自身可观测与告警群、report 球权契约与结案门禁、全局待拍板聚合台双视图同步、三阶温和催办（7.81）。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sheepdog import alert_group, decision_hub, duty, outbound
from sheepdog.card_callback import process_card_action_event, record_card_callback_to_ledger
from sheepdog.config import Config, TopicGroupConfig
from sheepdog.engine import Collector
from sheepdog.models import Message
from sheepdog.notify import OwnerDM
from sheepdog.source import SourceError
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
TG_CHAT = "oc_test_tg"
ALERT_CHAT = "oc_test_alert"


class FakeSource781:
    """轻量可观测飞书假消息源：记录建群、发群、发私聊、话题回复、改卡与置顶。"""

    def __init__(self) -> None:
        self.next_alert_chat_id = ALERT_CHAT
        self.fail_create_alert_chat = False
        self.invalid_chats: set[str] = set()
        self.created_chats: list[tuple[str, str]] = []
        self.chat_sends: list[dict] = []
        self.user_sends: list[dict] = []
        self.replies: list[dict] = []
        self.patched: dict[str, list[dict]] = {}
        self.pins: list[str] = []
        self.run_once_calls: list[tuple[list[str], str]] = []

    def create_alert_chat(self, name: str, owner_id: str) -> dict:
        if self.fail_create_alert_chat:
            raise SourceError("create_alert_chat disabled")
        self.created_chats.append((name, owner_id))
        return {"chat_id": self.next_alert_chat_id}

    def _run_once(self, cmd: list[str], identity: str = "bot") -> dict:
        self.run_once_calls.append((list(cmd), identity))
        if "+chat-create" in cmd:
            self.created_chats.append((alert_group.ALERT_GROUP_NAME, OWNER_ID))
            return {"data": {"chat_id": self.next_alert_chat_id}}
        return {"ok": True, "data": {}}

    def send_to_chat(self, chat_id: str, text: str, idem_key: str = "", identity: str = "bot", *, card: dict | None = None) -> dict:
        if chat_id in self.invalid_chats:
            raise SourceError(f"chat not found: {chat_id}")
        mid = f"om_test_chat_{len(self.chat_sends) + 1}"
        thr = f"omt_test_chat_{len(self.chat_sends) + 1}"
        self.chat_sends.append({"chat_id": chat_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "message_id": mid, "thread_id": thr})
        return {"message_id": mid, "thread_id": thr}

    def send_to_user(self, user_id: str, text: str, idem_key: str = "", identity: str = "bot", *, card: dict | None = None) -> dict:
        mid = f"om_test_dm_{len(self.user_sends) + 1}"
        self.user_sends.append({"user_id": user_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "message_id": mid})
        return {"message_id": mid}

    def send_text(self, user_id: str, text: str, identity: str = "bot", idempotency_key: str = "", *, card: dict | None = None) -> dict:
        return self.send_to_user(user_id, text, idempotency_key, identity, card=card)

    def reply_to(self, message_id: str, *, identity: str = "bot", idem_key: str = "", text: str = "", card: dict | None = None, in_thread: bool = False) -> dict:
        mid = f"om_test_rep_{len(self.replies) + 1}"
        thr = f"omt_test_rep_{len(self.replies) + 1}"
        self.replies.append({"target": message_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "in_thread": in_thread, "message_id": mid, "thread_id": thr})
        return {"message_id": mid, "thread_id": thr}

    def patch_card(self, message_id: str, card: dict, *, identity: str = "bot") -> None:
        self.patched.setdefault(message_id, []).append(card)

    def pin(self, message_id: str) -> None:
        self.pins.append(message_id)

    def pin_message(self, message_id: str) -> None:
        self.pin(message_id)


class _Base781(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        cfg_path = self.tmp_path / "config.toml"
        cfg_path.write_text('[notify]\nowner_card = true\n', encoding="utf-8")
        self.store = Store(self.tmp_path / "sheepdog.db")
        self.addCleanup(self.store.close)
        self.cfg = Config(state_dir=self.tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        self.cfg.source.enabled = True
        self.cfg.identity.enabled = True
        self.cfg.notify.owner_card = True
        self.cfg.notify.require_summary = False
        self.cfg.topic_group = TopicGroupConfig(chat_id=TG_CHAT, enabled=True)
        self.store.create_topic("tp_bus", "总线", "bus", "总线职责", "active")
        self.store.create_topic("tp_alpha", "Alpha 会话", "adopted", "Alpha 职责", "active")
        self.source = FakeSource781()


class TestAlertGroupIsolation781(_Base781):
    """REG-034：自身可观测与告警群自动创建、绑定持久化、置顶欢迎卡与通知物理隔离。"""

    def test_reg_034_auto_create_bind_pin_and_route_owner_notices(self) -> None:
        self.assertEqual(self.cfg.notify.alert_chat_id, "")
        chat_id = alert_group.ensure_alert_group(self.cfg, self.store, self.source)
        self.assertEqual(chat_id, ALERT_CHAT)
        self.assertEqual(self.source.created_chats, [(alert_group.ALERT_GROUP_NAME, OWNER_ID)])
        self.assertEqual(self.store.get_meta(alert_group.META_ALERT_CHAT_ID), ALERT_CHAT)
        self.assertEqual(self.cfg.notify.alert_chat_id, ALERT_CHAT)
        self.assertIn(f'alert_chat_id = "{ALERT_CHAT}"', self.cfg.config_path.read_text(encoding="utf-8"))
        self.assertEqual(len(self.source.chat_sends), 1)
        welcome_mid = self.source.chat_sends[0]["message_id"]
        self.assertEqual(self.store.get_meta(alert_group.META_ALERT_PIN_MID), welcome_mid)
        self.assertEqual(self.source.pins, [welcome_mid])

        dm = OwnerDM(self.source, OWNER_ID, cfg=self.cfg, store=self.store)
        dm.send("🐕 汇报 · 新版提醒 [tp_bus]\n发现新稳定版 v1.9.0", idempotency_key="ik_test_ver_781")

        tg_msg = Message(message_id="om_test_tg_anchor", chat_id=TG_CHAT, chat_name="业务群", chat_type="group", sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="测试", msg_type="text", create_time=now_iso())
        self.store.upsert_message(tg_msg, "dispatch", "owner_reply", ["owner_verified"], topic_id="tp_alpha")
        nid = self.store.add_owner_notice("羊群看板通知", "tp_alpha", "🐕 汇报 · 会话中断了 [tp_alpha]\n15分钟没动静".encode("utf-8"), "ik_test_notice_781", reply_to="om_test_tg_anchor", in_thread=True)
        fake_disp = type("FakeDisp", (), {"cfg": self.cfg, "store": self.store, "source": self.source, "notifier": dm})()
        alert_group.retry_owner_notices(fake_disp)
        self.assertEqual(self.store.get_owner_notice(nid)["state"], "sent")

        wd_cards: list[tuple[str, str, str]] = []
        with mock.patch.object(alert_group, "send_watchdog_lark_card", side_effect=lambda cid, ttl, tpl, md: (wd_cards.append((cid, ttl, tpl)), True)[1]):
            st: dict = {}
            now = datetime.fromisoformat("2026-10-27T12:00:00+08:00")
            self.assertTrue(alert_group.notify_watchdog_result(self.cfg, self.store.path, st, self.tmp_path / "wd.json", ["会话失联"], None, now, notify_lark=True))
            self.assertTrue(alert_group.notify_watchdog_result(self.cfg, self.store.path, st, self.tmp_path / "wd.json", [], ["会话失联"], now, notify_lark=True))
        self.assertEqual([c[0] for c in wd_cards], [ALERT_CHAT, ALERT_CHAT])
        self.assertEqual([c[2] for c in wd_cards], ["orange", "green"])
        self.assertEqual(len(self.source.user_sends), 0)
        self.assertEqual([s["chat_id"] for s in self.source.chat_sends], [ALERT_CHAT, ALERT_CHAT, ALERT_CHAT])
        self.assertEqual(len(self.source.replies), 0)

    def test_reg_034_fallback_recreate_on_invalid_chat(self) -> None:
        first_chat = alert_group.ensure_alert_group(self.cfg, self.store, self.source)
        self.assertEqual(first_chat, ALERT_CHAT)
        self.source.invalid_chats.add(ALERT_CHAT)
        self.source.fail_create_alert_chat = True
        self.source.next_alert_chat_id = "oc_test_alert_reborn"
        dm = OwnerDM(self.source, OWNER_ID, cfg=self.cfg, store=self.store)
        with self.assertRaises(SourceError):
            dm.send("🐕 汇报 · 恢复通知 [tp_bus]\n旧群已解散", idempotency_key="ik_test_reborn")
        self.cfg.notify.alert_chat_id = ""
        self.store.set_meta(alert_group.META_ALERT_CHAT_ID, "")
        reborn_chat = alert_group.ensure_alert_group(self.cfg, self.store, self.source)
        self.assertEqual(reborn_chat, "oc_test_alert_reborn")
        self.assertEqual(self.store.get_meta(alert_group.META_ALERT_CHAT_ID), "oc_test_alert_reborn")
        self.assertEqual(self.cfg.notify.alert_chat_id, "oc_test_alert_reborn")
        dm.send("🐕 汇报 · 恢复通知 [tp_bus]\n新告警群已接管", idempotency_key="ik_test_reborn")
        self.assertEqual(self.source.chat_sends[-1]["chat_id"], "oc_test_alert_reborn")


class TestBallContractAndResolveGuard781(_Base781):
    """REG-035：report 球权契约（放行正常叙事、拦截 --ball owner 与伪装选项块）与 resolve 禁自关未答提问门禁。"""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.topic_group.decision_hub = True

    def test_reg_035_report_allows_normal_prose_with_authorization_words(self) -> None:
        prose = "已经过 Matthew 授权，Sammy 已拍板，请示单已归档，方案 A 已上线"
        ball = decision_hub.enforce_report_contract(self.cfg, self.store, "tp_alpha", "正常汇报", prose, key="K-OK", ball="doing")
        self.assertEqual(ball, "doing")
        res = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "正常汇报", prose, key="K-OK", ball="doing")
        self.assertTrue(res.message_id.startswith("om_test_"))

    def test_reg_035_report_blocks_ball_owner_and_disguised_option_prompts(self) -> None:
        with self.assertRaises(outbound.OutboundError) as ctx_owner:
            outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "违规球权", "已完成前置检查", ball="owner")
        self.assertEqual(ctx_owner.exception.code, outbound.EXIT_INVALID)

        disguised_opts = "前置核查完毕，请选：\nA. 方案一\nB. 方案二"
        with self.assertRaises(outbound.OutboundError) as ctx_opts:
            outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "伪装选项", disguised_opts)
        self.assertEqual(ctx_opts.exception.code, outbound.EXIT_INVALID)

        disguised_cmd = "灰度验证已通过，请回「同意迁移」"
        with self.assertRaises(outbound.OutboundError) as ctx_cmd:
            outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "伪装口令", disguised_cmd)
        self.assertEqual(ctx_cmd.exception.code, outbound.EXIT_INVALID)

        res_fyi = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "历史转述", disguised_opts, fyi=True)
        self.assertTrue(res_fyi.message_id.startswith("om_test_"))

        duty.set_work(self.store, "tp_alpha", "K1", state=duty.DOING, title="迁移活")
        decision_hub.apply_report_ball_state(self.store, "tp_alpha", "K1", "done")
        self.assertEqual(self.store.get_work("tp_alpha", "K1")["state"], duty.DONE)

    def test_reg_035_resolve_anti_self_close_guard(self) -> None:
        decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        ask1 = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "提问一", "选哪个？\nA: 一\nB: 二", key="K-ASK-1")
        ask2 = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "提问二", "选哪个？\nA: 三\nB: 四", key="K-ASK-2")
        self.store.add_bot_outbox("om_test_report_only", "tp_alpha", False, "🐕 汇报 [tp_alpha]\n普通汇报", now_iso())

        with self.assertRaises(outbound.OutboundError) as ctx_bare:
            outbound.resolve(self.cfg, self.store, self.source, "tp_alpha", "企图自关", message_id=ask1.message_id)
        self.assertEqual(ctx_bare.exception.code, outbound.EXIT_INVALID)

        with self.assertRaises(outbound.OutboundError) as ctx_fake_q:
            outbound.resolve(self.cfg, self.store, self.source, "tp_alpha", "假引用", message_id=ask1.message_id, quote="om_test_nonexistent")
        self.assertEqual(ctx_fake_q.exception.code, outbound.EXIT_INVALID)

        with self.assertRaises(outbound.OutboundError) as ctx_bad_sup:
            outbound.resolve(self.cfg, self.store, self.source, "tp_alpha", "非提问替代", message_id=ask1.message_id, superseded_by="om_test_report_only")
        self.assertEqual(ctx_bad_sup.exception.code, outbound.EXIT_INVALID)

        owner_msg = Message(message_id="om_test_owner_ok", chat_id=TG_CHAT, chat_name="业务群", chat_type="group", sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="同意按方案一执行", msg_type="text", create_time=now_iso())
        self.store.upsert_message(owner_msg, "dispatch", "owner_reply", ["owner_verified"], topic_id="tp_alpha")
        r1 = outbound.resolve(self.cfg, self.store, self.source, "tp_alpha", "主人已口头批复", message_id=ask1.message_id, quote="同意按方案一执行")
        self.assertEqual(r1.closed, [ask1.message_id])

        r2 = outbound.resolve(self.cfg, self.store, self.source, "tp_alpha", "由新提问替代", message_id=ask2.message_id, superseded_by=ask1.message_id)
        self.assertEqual(r2.closed, [ask2.message_id])


class TestDecisionHubDualViewAndKeyIsolation781(_Base781):
    """REG-036：全局待拍板聚合台自动初始化、双向镜像投射、同 key 原地双端 PATCH、按钮/文字一处拍板双端变绿。"""

    def test_reg_036_decision_hub_auto_init_and_dual_view_mirror(self) -> None:
        meta = decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        hub_root = meta["root_message_id"]
        self.assertTrue(hub_root.startswith("om_test_"))
        self.assertIn(hub_root, self.source.pins)

        res = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "GCP 迁移拍板", "选哪个？\nA: 立即切流\nB: 灰度切流", key="GCP-MIG", track=True)
        self.assertEqual(res.item, "T-1")
        esc = self.store.get_escalation(res.message_id)
        self.assertIsNotNone(esc)
        self.assertTrue(esc["hub_message_id"].startswith("om_test_rep_"))
        self.assertIn(hub_root, self.source.patched)
        hub_1f_md = json.dumps(self.source.patched[hub_root][-1], ensure_ascii=False)
        self.assertIn("GCP-MIG", hub_1f_md)
        self.assertIn("当前 1 项待拍板", hub_1f_md)

    def test_reg_036_same_key_ask_idempotent_in_place_dual_patch(self) -> None:
        decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        res1 = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "GCP 迁移拍板", "选哪个？\nA: 立即切流\nB: 灰度切流", key="GCP-MIG", track=True)
        esc1 = self.store.get_escalation(res1.message_id)
        hub_mid = esc1["hub_message_id"]
        replies_before = len(self.source.replies)

        res2 = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "GCP 迁移拍板", "选哪个（修订版）？\nA: 立即切流\nB: 分阶段灰度", key="GCP-MIG", track=True)
        self.assertTrue(res2.already)
        self.assertEqual(res2.message_id, res1.message_id)
        self.assertEqual(len(self.source.replies), replies_before)
        self.assertIn("选哪个（修订版）？", json.dumps(self.source.patched[res1.message_id][-1], ensure_ascii=False))
        self.assertIn("选哪个（修订版）？", json.dumps(self.source.patched[hub_mid][-1], ensure_ascii=False))

    def test_reg_036_button_click_in_either_view_settles_both_cards_atomic_cas(self) -> None:
        meta = decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        hub_root = meta["root_message_id"]
        res = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "GCP 迁移拍板", "选哪个？\nA: 立即切流\nB: 灰度切流", key="GCP-MIG", track=True)
        origin_mid = res.message_id
        hub_mid = self.store.get_escalation(origin_mid)["hub_message_id"]
        self.assertIsNone(self.store.send_by_message(hub_mid))
        self.assertEqual(self.store.get_bot_outbox(hub_mid)["is_question"], 0)

        collector = Collector(self.cfg, self.store, self.source)
        hub_echo_msg = Message(message_id=hub_mid, chat_id=TG_CHAT, chat_name="业务群", chat_type="group", sender_id="ou_test_bot", sender_name="Bot", sender_type="bot", content='<card title="🐕 需要你定 · GCP 迁移拍板 [tp_alpha]">选哪个？</card>', msg_type="interactive", create_time=now_iso(), reply_to=hub_root, root_id=hub_root)
        collector.ingest([hub_echo_msg])
        self.assertFalse(self.store.add_escalation(hub_mid, "tp_alpha", TG_CHAT, "phantom", now_iso()))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0], 1)

        ev_hub_b = {
            "type": "card.action.trigger", "event_id": "ev_test_hub_click_b", "message_id": hub_mid,
            "chat_id": TG_CHAT, "operator_id": OWNER_ID, "action_tag": "button",
            "action_value": {"sd_cb": "1", "kind": "option", "choice": "B", "label": "灰度切流", "reply_text": "B：灰度切流", "topic_id": "tp_alpha", "key": "GCP-MIG", "message_id": origin_mid},
        }
        msg_b = process_card_action_event(ev_hub_b, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(msg_b)
        self.assertEqual(msg_b.raw["_sd_card_callback"]["card"]["data"]["header"]["template"], "green")
        record_card_callback_to_ledger(self.store, msg_b, cfg=self.cfg, source=self.source)

        esc = self.store.get_escalation_by_any_mid(hub_mid)
        self.assertEqual(esc["message_id"], origin_mid)
        self.assertIsNotNone(esc["answered_at"])
        self.assertEqual(esc["close_reason"], "answered")
        self.assertEqual(self.source.patched[origin_mid][-1]["header"]["template"], "green")
        self.assertEqual(self.source.patched[hub_mid][-1]["header"]["template"], "green")
        self.assertIn("当前 0 项待拍板", json.dumps(self.source.patched[hub_root][-1], ensure_ascii=False))

        ev_origin_dup = dict(ev_hub_b, event_id="ev_test_origin_click_dup_b", message_id=origin_mid)
        self.assertIsNone(process_card_action_event(ev_origin_dup, cfg=self.cfg, store=self.store, source=self.source))

        ev_hub_change_a = {
            "type": "card.action.trigger", "event_id": "ev_test_hub_click_a", "message_id": hub_mid,
            "chat_id": TG_CHAT, "operator_id": OWNER_ID, "action_tag": "button",
            "action_value": {"sd_cb": "1", "kind": "option", "choice": "A", "label": "立即切流", "reply_text": "A：立即切流", "topic_id": "tp_alpha", "key": "GCP-MIG", "message_id": origin_mid},
        }
        msg_a = process_card_action_event(ev_hub_change_a, cfg=self.cfg, store=self.store, source=self.source)
        self.assertIsNotNone(msg_a)
        self.assertEqual(msg_a.content, "[卡片回调] A：立即切流")
        record_card_callback_to_ledger(self.store, msg_a, cfg=self.cfg, source=self.source)
        self.assertEqual(self.store.get_escalation(origin_mid)["answer_message_id"], msg_a.message_id)
        self.assertIn("立即切流", json.dumps(self.source.patched[origin_mid][-1], ensure_ascii=False))
        self.assertIn("立即切流", json.dumps(self.source.patched[hub_mid][-1], ensure_ascii=False))

    def test_reg_036_text_reply_in_hub_routes_and_settles_both_cards(self) -> None:
        meta = decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        hub_root = meta["root_message_id"]
        res = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "GCP 迁移拍板", "选哪个？\nA: 立即切流\nB: 灰度切流", key="GCP-MIG", track=True)
        origin_mid = res.message_id
        hub_mid = self.store.get_escalation(origin_mid)["hub_message_id"]

        collector = Collector(self.cfg, self.store, self.source)
        reply_msg = Message(message_id="om_test_hub_text_1", chat_id=TG_CHAT, chat_name="业务群", chat_type="group", sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="选 B 灰度切流", msg_type="text", create_time=now_iso(), reply_to=hub_mid, root_id=hub_root)
        self.assertTrue(decision_hub.handle_hub_text_reply(collector, reply_msg))
        esc = self.store.get_escalation(origin_mid)
        self.assertIsNotNone(esc["answered_at"])
        self.assertEqual(self.source.patched[origin_mid][-1]["header"]["template"], "green")
        self.assertEqual(self.source.patched[hub_mid][-1]["header"]["template"], "green")
        self.assertIn("当前 0 项待拍板", json.dumps(self.source.patched[hub_root][-1], ensure_ascii=False))


class TestEscalationNudgeLadder781(_Base781):
    """REG-037：待拍板三阶温和催办（+30m / +2h / 次日 10:00 CST，最多 3 次）与 00:00~09:00 CST 免打扰顺延。"""

    def test_reg_037_three_tier_nudge_ladder_and_quiet_hours(self) -> None:
        decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        self.store.add_escalation("om_test_ask_day", "tp_alpha", TG_CHAT, "🐕 需要你定 · 白天拍板 [tp_alpha]\n请确认", "2026-10-27T12:00:00+08:00")

        self.assertEqual(decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-27T12:20:00+08:00")), [])

        n1 = decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-27T12:31:00+08:00"))
        self.assertEqual(len(n1), 1)
        self.assertEqual((n1[0]["nudge_count"], n1[0]["idem_key"]), (1, "ask_nudge:om_test_ask_day:1"))
        self.assertEqual(decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-27T12:35:00+08:00")), [])

        n2 = decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-27T14:01:00+08:00"))
        self.assertEqual(len(n2), 1)
        self.assertEqual((n2[0]["nudge_count"], n2[0]["idem_key"]), (2, "ask_nudge:om_test_ask_day:2"))

        self.store.add_escalation("om_test_ask_night", "tp_alpha", TG_CHAT, "🐕 需要你定 · 夜间拍板 [tp_alpha]\n请确认", "2026-10-27T23:45:00+08:00")
        due_night = decision_hub.next_nudge_due_at(datetime.fromisoformat("2026-10-27T23:45:00+08:00"), 0)
        self.assertEqual(due_night, datetime.fromisoformat("2026-10-28T10:00:00+08:00"))
        self.assertEqual(decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-28T01:00:00+08:00")), [])

        n3 = decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-28T10:01:00+08:00"))
        by_mid = {x["message_id"]: x for x in n3}
        self.assertEqual(by_mid["om_test_ask_day"]["nudge_count"], 3)
        self.assertEqual(by_mid["om_test_ask_day"]["idem_key"], "ask_nudge:om_test_ask_day:3")
        self.assertEqual(by_mid["om_test_ask_night"]["nudge_count"], 1)
        self.assertEqual(by_mid["om_test_ask_night"]["idem_key"], "ask_nudge:om_test_ask_night:1")

        self.assertIsNone(decision_hub.next_nudge_due_at(datetime.fromisoformat("2026-10-27T12:00:00+08:00"), 3, datetime.fromisoformat("2026-10-28T10:01:00+08:00")))
        n4 = decision_hub.nudge_open_escalations(self.cfg, self.store, self.source, now=datetime.fromisoformat("2026-10-28T10:30:00+08:00"))
        self.assertNotIn("om_test_ask_day", {x["message_id"] for x in n4})


if __name__ == "__main__":
    unittest.main()
