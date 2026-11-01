"""REG-038 ~ REG-042 回归测试：Canonical 话题群路由与一会话一主贴、会话钥匙隔离与跨会话追问路标、waiting/话题群回执 0s 原帖带回、存量账本自愈归并、群顶层 reply Bot 优先发送（7.82）。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sheepdog import alert_group, decision_hub, identity as ident, outbound, topicgroup as tg
from sheepdog.config import Config, TopicGroupConfig
from sheepdog.engine import Collector, Dispatcher, forward_messages, write_receipt
from sheepdog.models import Message
from sheepdog.notify import OwnerDM
from sheepdog.source import SourceError
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
TG_CHAT = "oc_test_tg"
ALERT_CHAT = "oc_test_alert"
GROUP_CHAT = "oc_test_biz_group"
GROUP_230002 = "oc_test_group_230002"


class FakeSource782:
    """支持建群、发群、post_to、发私聊、话题回复、改卡、置顶与群成员/Bot 群列表的测试假消息源。"""

    def __init__(self) -> None:
        self.next_alert_chat_id = ALERT_CHAT
        self.bot_chats: set[str] = {TG_CHAT, ALERT_CHAT, GROUP_CHAT, GROUP_230002}
        self.fail_230002_chats: set[str] = {GROUP_230002}
        self.created_chats: list[tuple[str, str]] = []
        self.chat_sends: list[dict] = []
        self.posts: list[dict] = []
        self.user_sends: list[dict] = []
        self.replies: list[dict] = []
        self.patched: dict[str, list[dict]] = {}
        self.pins: list[str] = []

    def create_alert_chat(self, name: str, owner_id: str) -> dict:
        self.created_chats.append((name, owner_id))
        return {"chat_id": self.next_alert_chat_id}

    def bot_chat_ids(self) -> list[str]:
        return sorted(self.bot_chats)

    def muted_chat_ids(self) -> set[str]:
        return set()

    def chat_members(self, chat_id: str) -> dict:
        return {"chat_id": chat_id, "users": [OWNER_ID], "bots": ["cli_test_bot"]}

    def send_to_chat(self, chat_id: str, text: str, idem_key: str = "", identity: str = "bot", *, card: dict | None = None) -> dict:
        mid = f"om_test_chat_{len(self.chat_sends) + 1}"
        thr = f"omt_test_chat_{len(self.chat_sends) + 1}"
        self.chat_sends.append({"chat_id": chat_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "message_id": mid, "thread_id": thr})
        return {"message_id": mid, "thread_id": thr}

    def post_to(self, chat_id: str, *, identity: str = "bot", idem_key: str = "", text: str = "", card: dict | None = None) -> dict:
        if identity == "bot" and chat_id in self.fail_230002_chats:
            raise SourceError("feishu api error: code=230002 bot is not in chat")
        mid = f"om_test_post_{len(self.posts) + 1}"
        self.posts.append({"chat_id": chat_id, "text": text, "idem_key": idem_key, "identity": identity, "card": card, "message_id": mid})
        return {"message_id": mid}

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


class TestCanonicalTopicRouting782(unittest.TestCase):
    """REG-038 ~ REG-042：Canonical 话题群路由、会话钥匙隔离与跨会话转交路标、即时原帖回执带回、存量账本自愈、群顶层 reply。"""

    def setUp(self) -> None:
        self.cfg, self.store, self.source = self._make_env()

    def _make_env(self) -> tuple[Config, Store, FakeSource782]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[notify]\nowner_card = true\n', encoding="utf-8")
        cfg = Config(state_dir=tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        store = Store(cfg.db_path)
        self.addCleanup(store.close)
        cfg.source.enabled = True
        cfg.identity.enabled = True
        cfg.identity.bot_app_id = "cli_test_bot"
        cfg.notify.owner_card = True
        cfg.notify.require_summary = False
        cfg.threads.owner_chat_id = ""
        cfg.topic_group = TopicGroupConfig(chat_id=TG_CHAT, enabled=True)
        for tid, title in (
            ("tp_bus", "总线"),
            ("tp_alpha", "Alpha 会话"),
            ("tp_beta", "Beta 会话"),
            ("tp_release", "发版专席"),
            ("tp_wenbo-ops", "文博运维专席"),
            ("tp_3x-ui-ops", "家宽VPS运维专席"),
            ("tp_jerry-chu", "Jerry Chu 协同专席"),
        ):
            store.create_topic(tid, title, "bus" if tid == "tp_bus" else "adopted", f"{title}职责", "active")
        return cfg, store, FakeSource782()

    def test_reg_038_canonical_session_topic_and_no_dm_scatter(self) -> None:
        self.assertTrue(decision_hub.canonical_group_mode(self.cfg, self.store))
        r1 = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "初次汇报不带track与key", "已完成前置巡检")
        self.assertEqual(r1.item, "T-1")
        self.assertTrue(r1.new_item)
        self.assertEqual(len(self.source.user_sends), 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM owner_threads").fetchone()[0], 0)

        r2 = outbound.send_to_owner(self.cfg, self.store, self.source, "ask", "tp_alpha", "后续换新钥匙提问", "请确认切换窗口：\nA: 今晚\nB: 明早", key="BRAND-NEW-KEY-2", track=True)
        self.assertEqual(r2.item, "T-1")
        self.assertFalse(r2.new_item)
        self.assertEqual(r2.root_message_id, r1.root_message_id)

        r3 = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_alpha", "第三次换钥匙汇报", "窗口已锁定", key="THIRD-KEY-3", track=False)
        self.assertEqual(r3.item, "T-1")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tracked_items").fetchone()[0], 1)
        keys = {tg.track_norm(k) for k in tg.item_keys(self.store, 1)}
        self.assertIn(tg.track_norm("alpha"), keys)
        self.assertIn(tg.track_norm("BRAND-NEW-KEY-2"), keys)
        self.assertIn(tg.track_norm("THIRD-KEY-3"), keys)
        self.assertEqual(len(self.source.user_sends), 0)

    def test_reg_039_session_key_isolation_and_cross_domain_forward_signpost(self) -> None:
        # 1) 跨会话同单号钥匙严格隔离：tp_release 的 T-1 绝不被 tp_wenbo-ops 串入
        rel_res = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_release", "发版单 202610270023", "进入灰度验证", key="202610270023", track=True)
        self.assertEqual(rel_res.item, "T-1")
        self.assertIsNotNone(tg.find_by_key(self.store, "202610270023", topic_id="tp_release"))
        self.assertIsNone(tg.find_by_key(self.store, "202610270023", topic_id="tp_wenbo-ops"))

        wb_res = outbound.send_to_owner(self.cfg, self.store, self.source, "report", "tp_wenbo-ops", "文博跟进 202610270023", "配置核对完毕", key="202610270023")
        self.assertEqual(wb_res.item, "T-2")
        self.assertEqual(self.store.get_item(1)["owner_topic"], "tp_release")
        self.assertEqual(self.store.get_item(2)["owner_topic"], "tp_wenbo-ops")

        # 2) 主人在 T-1（tp_alpha）顺带问 tp_beta 的事并由 tp_alpha forward 给 tp_beta：T-1 不被夺舍，重绑 T-2 并发路标
        cfg2, store2, src2 = self._make_env()
        a_res = outbound.send_to_owner(cfg2, store2, src2, "report", "tp_alpha", "Alpha 专项主贴", "Alpha 进展正常", key="ALPHA-MAIN", track=True)
        self.assertEqual(a_res.item, "T-1")
        t1 = store2.get_item(1)
        cross_msg = Message(
            message_id="om_test_cross_beta", chat_id=TG_CHAT, chat_name="业务群", chat_type="group",
            sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="顺便把 Beta 服务的配额也查一下",
            msg_type="text", create_time=now_iso(), reply_to=t1["root_message_id"], root_id=t1["root_message_id"], thread_id=t1["thread_id"] or "",
        )
        store2.upsert_message(cross_msg, "dispatch", "owner_reply", ["owner_verified", "item:T-1"], topic_id="tp_alpha")
        forward_messages(store2, "tp_beta", ["om_test_cross_beta"], self_open_id=OWNER_ID)

        self.assertEqual(store2.get_item(1)["owner_topic"], "tp_alpha")
        t2 = store2.get_item(2)
        self.assertIsNotNone(t2)
        self.assertEqual(t2["owner_topic"], "tp_beta")
        rebound_tags = json.loads(store2.get_message("om_test_cross_beta")["tags_json"] or "[]")
        self.assertIn("item:T-2", rebound_tags)
        self.assertNotIn("item:T-1", rebound_tags)

        fake_disp = type("FakeDisp", (), {"cfg": cfg2, "store": store2, "source": src2})()
        tg.dispatcher_round(fake_disp)
        t1_replies = [r for r in src2.replies if r["target"] == t1["root_message_id"]]
        self.assertTrue(t1_replies)
        signpost_payload = json.dumps(t1_replies[-1], ensure_ascii=False)
        self.assertIn("↪️ 这件事归属「Beta 会话」，已转入专属话题 T-2", signpost_payload)
        self.assertIn("client/chat/open", signpost_payload)
        self.assertNotIn("client/thread/open", signpost_payload)

    def test_reg_040_instant_in_thread_waiting_receipt_relay_not_hijacked_to_alert_group(self) -> None:
        self.assertNotIn("回执带回", alert_group.SYSTEM_ALERT_KINDS)
        self.cfg.session.owner_receipt_relay = True
        self.cfg.session.owner_relay_after_minutes = 10
        alert_chat = alert_group.ensure_alert_group(self.cfg, self.store, self.source)
        self.assertEqual(alert_chat, ALERT_CHAT)
        alert_sends_before = len([s for s in self.source.chat_sends if s["chat_id"] == ALERT_CHAT])

        t1 = tg.open_item(
            self.store, "Alpha 巡检话题", "tp_alpha", "tp_alpha", tg.ORIGIN_TRACK, TG_CHAT,
            key="ALPHA-INSPECT", topic_id="tp_alpha", root_message_id="om_test_root_t1", thread_id="omt_test_t1",
        )

        collector = Collector(self.cfg, self.store, self.source)
        owner_msg = Message(
            message_id="om_test_owner_wait_1", chat_id=TG_CHAT, chat_name="业务群", chat_type="group",
            sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="跑一下全量账单核对",
            msg_type="text", create_time=now_iso(), reply_to=t1["root_message_id"], root_id=t1["root_message_id"], thread_id=t1["thread_id"] or "",
        )
        collector.ingest([owner_msg])
        self.store.record_dispatch("b_wait_782", "tp_alpha", ["om_test_owner_wait_1"])
        self.store.mark_batch(["om_test_owner_wait_1"], "b_wait_782", "tp_alpha", "delivered")
        self.store.update_topic("tp_alpha", state="running", pending_batch_id="b_wait_782", dispatched_at=now_iso())

        write_receipt(self.cfg, "tp_alpha", "b_wait_782", {"status": "waiting", "summary": "已派羔羊后台跑全量账单核对", "anchors": ["lamb:lamb_bill_01"]})
        dm = OwnerDM(self.source, OWNER_ID, cfg=self.cfg, store=self.store)
        disp = Dispatcher(self.cfg, self.store, {}, notifier=dm, source=self.source)
        disp._echo_round()

        echo_after = self.store.echo_get("om_test_owner_wait_1")
        self.assertEqual((echo_after["stage"], echo_after["settled_by"]), ("settled", "relay"))
        alert_sends_after = len([s for s in self.source.chat_sends if s["chat_id"] == ALERT_CHAT])
        self.assertEqual(alert_sends_after, alert_sends_before)
        relayed = [r for r in self.source.replies if r["target"] == "om_test_owner_wait_1"]
        self.assertEqual(len(relayed), 1)
        self.assertTrue(relayed[0]["in_thread"])
        relayed_dump = json.dumps(relayed[0], ensure_ascii=False)
        self.assertIn("已派后台任务处理中", relayed_dump)
        self.assertIn("已派羔羊后台跑全量账单核对", relayed_dump)

    def test_reg_041_heal_canonical_topics_merges_duplicates_and_cleans_cross_session_keys(self) -> None:
        # 1) 构造 T-1（tp_release）被跨会话污染了 tp_wenbo-ops 的钥匙，且 tp_wenbo-ops 已有 Canonical T-2
        t1 = tg.open_item(self.store, "发版事项", "tp_release", "tp_release", tg.ORIGIN_TRACK, TG_CHAT, key="REL-MAIN", topic_id="tp_release", root_message_id="om_test_root_t1")
        t2 = tg.open_item(self.store, "文博运维主贴", "tp_wenbo-ops", "tp_wenbo-ops", tg.ORIGIN_TRACK, TG_CHAT, key="WENBO-MAIN", topic_id="tp_wenbo-ops", root_message_id="om_test_root_t2")
        tg.bind_alias_key(self.store, t1["item_no"], "POLLUTED-2026", "tp_wenbo-ops")

        # 2) 构造单职责会话 tp_alpha 名下两个重复 active 事项 T-3（有 root）与 T-4（重复分裂）
        t3 = tg.open_item(self.store, "Alpha 主贴", "tp_alpha", "tp_alpha", tg.ORIGIN_TRACK, TG_CHAT, key="ALPHA-CANON", topic_id="tp_alpha", root_message_id="om_test_root_t3")
        t4 = tg.open_item(self.store, "Alpha 分裂帖", "tp_alpha", "tp_alpha", tg.ORIGIN_TRACK, TG_CHAT, key="ALPHA-SPLIT", topic_id="tp_alpha")

        # 3) 构造未关联的 owner_threads 私聊串
        self.store.add_thread("om_test_dm_orphan", "tp_alpha", "ALPHA-DM-OLD", "ALPHADMOLD", "旧私聊串", "report", "omt_test_dm_1")

        healed = decision_hub.heal_canonical_topics(self.cfg, self.store, self.source, dry_run=False)
        self.assertEqual(len(healed["rebound_keys"]), 1)
        self.assertEqual((healed["rebound_keys"][0]["from_item"], healed["rebound_keys"][0]["to_item"]), (t1["item_no"], t2["item_no"]))
        self.assertNotIn(tg.track_norm("POLLUTED-2026"), {tg.track_norm(k) for k in tg.item_keys(self.store, t1["item_no"])})
        self.assertIn(tg.track_norm("POLLUTED-2026"), {tg.track_norm(k) for k in tg.item_keys(self.store, t2["item_no"])})

        self.assertEqual(len(healed["merged_items"]), 1)
        self.assertEqual((healed["merged_items"][0]["from_item"], healed["merged_items"][0]["to_item"]), (t4["item_no"], t3["item_no"]))
        self.assertEqual(self.store.get_item(t4["item_no"])["state"], tg.ARCHIVED)
        self.assertIn(tg.track_norm("ALPHA-SPLIT"), {tg.track_norm(k) for k in tg.item_keys(self.store, t3["item_no"])})

        self.assertEqual(len(healed["migrated_dm_threads"]), 1)
        self.assertEqual(healed["migrated_dm_threads"][0]["to_item"], t3["item_no"])
        self.assertEqual(self.store.get_item(t3["item_no"])["dm_root_message_id"], "om_test_dm_orphan")
        self.assertIn(tg.track_norm("ALPHA-DM-OLD"), {tg.track_norm(k) for k in tg.item_keys(self.store, t3["item_no"])})

    def test_reg_042_reply_to_group_chat_id_uses_bot_identity_first_and_falls_back_on_230002(self) -> None:
        # 1) 机器人在群里：强制 identity="bot" 调 post_to 发卡片并记入 sends
        r1 = outbound.reply(self.cfg, self.store, self.source, "tp_alpha", GROUP_CHAT, text="各位好，测试环境巡检已完成")
        self.assertEqual((r1.identity, r1.why), (ident.BOT, ident.WHY_IN_CHAT))
        self.assertEqual(len(self.source.posts), 1)
        self.assertEqual((self.source.posts[0]["chat_id"], self.source.posts[0]["identity"]), (GROUP_CHAT, ident.BOT))
        self.assertEqual(self.source.posts[0]["card"]["schema"], "2.0")
        s1 = self.store.send_by_message(r1.message_id)
        self.assertEqual((s1["kind"], s1["target_message_id"], s1["identity"]), ("reply", GROUP_CHAT, ident.BOT))

        # 2) 飞书返回 230002（机器人不在群）：作废缓存、记入 bot_absent_chats 并降级为 identity="user" 重发
        r2 = outbound.reply(self.cfg, self.store, self.source, "tp_alpha", GROUP_230002, text="机器人不在群时自动退回本人身份")
        self.assertEqual((r2.identity, r2.why), (ident.USER, ident.WHY_230002))
        self.assertEqual(self.store.get_meta(ident.META_CHATS_AT), "")
        absent = {r["chat_id"]: r["last_reason"] for r in self.store.bot_absent_chats()}
        self.assertEqual(absent.get(GROUP_230002), ident.WHY_230002)
        self.assertEqual(self.source.posts[-1]["identity"], ident.USER)
        self.assertEqual(self.store.send_by_message(r2.message_id)["identity"], ident.USER)

        # 3) --to 指向找主人侧聊天时被 reject_owner_chat 拦截
        with self.assertRaises(outbound.OutboundError) as ctx:
            outbound.reply(self.cfg, self.store, self.source, "tp_alpha", TG_CHAT, text="企图在找主人侧群里用 reply")
        self.assertEqual(ctx.exception.code, outbound.EXIT_INVALID)

    def test_reg_043_p2p_dm_question_gets_signpost_when_answer_routes_to_canonical_topic(self) -> None:
        self.cfg.escalation.chat_ids = ["oc_owner_dm"]
        dm_msg = Message(
            message_id="om_dm_q1", chat_id="oc_owner_dm", chat_name="牧羊犬", chat_type="p2p",
            sender_id=OWNER_ID, sender_name="Matthew", sender_type="user", content="为啥不去做解析呢",
            msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(dm_msg, "dispatch", "owner_dm", ["owner_verified"], topic_id="tp_bus")
        forward_messages(self.store, "tp_3x-ui-ops", ["om_dm_q1"], self_open_id=OWNER_ID)
        batch_id = "b_dm_783"
        self.store.record_dispatch(batch_id, "tp_3x-ui-ops", ["om_dm_q1"])
        self.store.mark_batch(["om_dm_q1"], batch_id, "tp_3x-ui-ops", "delivered")
        self.store.update_topic("tp_3x-ui-ops", state="running", pending_batch_id=batch_id, dispatched_at=now_iso())

        r1 = outbound.send_to_owner(
            self.cfg, self.store, self.source, "report", "tp_3x-ui-ops",
            "未做解析原因", "因为阿里云AK不对", key="lsg5-domain-incident",
            batch=batch_id, summary="因为阿里云AK不对",
        )
        self.assertEqual(r1.item, "T-1")
        dm_replies = [r for r in self.source.replies if r["target"] == "om_dm_q1"]
        self.assertEqual(len(dm_replies), 1)
        signpost_dump = json.dumps(dm_replies[0], ensure_ascii=False)
        self.assertIn("↪️ 这件事已在话题群专属话题 T-1", signpost_dump)
        self.assertIn("回复你", signpost_dump)

        # 同一批次再发第 2 条 report 时，幂等键保护不会在私聊原消息下重复发第 2 条路标
        outbound.send_to_owner(
            self.cfg, self.store, self.source, "report", "tp_3x-ui-ops",
            "后续进展", "已重新申请AK", key="lsg5-domain-incident",
            batch=batch_id, summary="已重新申请AK",
        )
        dm_replies_after = [r for r in self.source.replies if r["target"] == "om_dm_q1"]
        self.assertEqual(len(dm_replies_after), 1)

    def test_reg_044_receipt_rejects_dead_ball_vacuum_when_waiting_on_owner_without_open_ask(self) -> None:
        from unittest.mock import patch
        from sheepdog import cli, duty, hook
        self.cfg.topic_group.decision_hub = True
        decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        self.assertTrue(decision_hub.canonical_group_mode(self.cfg, self.store))
        self.assertTrue(decision_hub.hub_active(self.cfg, self.store))

        batch_id = "b_jerry_783"
        self.store.record_dispatch(batch_id, "tp_jerry-chu", [])
        self.store.update_topic(
            "tp_jerry-chu", state="running", pending_batch_id=batch_id,
            dispatched_at=now_iso(), conversation_id="cid-jerry-783",
        )

        # ① 提交 status="needs_decision" 但账本 escalations 无未决 ask 时拒收（cmd_receipt 返回退出码 2）
        err1 = decision_hub.verify_receipt_no_dead_ball(
            self.cfg, self.store, "tp_jerry-chu", batch_id,
            {"status": "needs_decision", "summary": "待主人拍板重构窗口"},
        )
        self.assertTrue(err1)
        rc_args = type("Args", (), {"topic": "tp_jerry-chu", "batch": batch_id, "json": json.dumps({"status": "needs_decision", "summary": "待主人拍板重构窗口"})})()
        with patch("sheepdog.cli._load_cfg", return_value=self.cfg):
            self.assertEqual(cli.cmd_receipt(rc_args), 2)

        # ② work_items 有未关闭条目 waiting_on="主人" / state="waiting_owner" 且无 open ask/watch/lamb 时，handled 与 waiting 均被拦截
        nk = duty.norm_key("JERRY-PR-1")
        self.store.add_work(
            "tp_jerry-chu", "JERRY-PR-1", nk, duty.DOING, duty.DECLARED, True,
            title="Jerry PR 评审", waiting_on="主人",
        )
        for st in ("handled", "waiting"):
            self.assertTrue(decision_hub.verify_receipt_no_dead_ball(
                self.cfg, self.store, "tp_jerry-chu", batch_id,
                {"status": st, "summary": "已完成代码初审"},
            ))

        # 调用 duty.set_work(..., state="doing") 自动清空残留 waiting_on="主人"，且正常叙事 summary 不被脆弱词表误杀
        duty.set_work(self.store, "tp_jerry-chu", "JERRY-PR-1", state="doing", title="Jerry PR 评审跟进")
        self.assertEqual(self.store.get_work("tp_jerry-chu", nk)["waiting_on"] or "", "")
        self.assertIsNone(decision_hub.verify_receipt_no_dead_ball(
            self.cfg, self.store, "tp_jerry-chu", batch_id,
            {"status": "handled", "summary": "已获主人放行授权，向Matthew请示事项已全部梳理完毕"},
        ))

        # ③ Stop Hook 拦截面板直聊写「等你定的事和回法」却未发 sheepdog ask 的情况
        tp_path = self.cfg.state_dir / "jerry_transcript.jsonl"
        def _write_tp(planner_text: str) -> None:
            lines = [
                json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>\n看下这个方案\n</USER_REQUEST>"}, ensure_ascii=False),
                json.dumps({"type": "PLANNER_RESPONSE", "content": planner_text}, ensure_ascii=False),
            ]
            tp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _write_tp("结论如下：\n- **等你定的事**：无\n- **接下来做什么**：继续观察")
        raw_hook = json.dumps({"conversationId": "cid-jerry-783", "fullyIdle": True, "transcriptPath": str(tp_path)})
        self.assertIsNone(hook.stop_check(raw_hook, cfg=self.cfg))

        _write_tp("结论如下：\n- **等你定的事和回法（二选一）**：\n  - 选项 1：回「同意合并」\n  - 选项 2：回「暂缓」")
        hook_res = hook.stop_check(raw_hook, cfg=self.cfg)
        self.assertIsNotNone(hook_res)
        self.assertEqual(hook_res.get("decision"), "continue")

        # ④ 调用 send_to_owner(..., "ask", ...) 发出飞书决策卡后，回执校验与 Stop Hook 均放行
        outbound.send_to_owner(
            self.cfg, self.store, self.source, "ask", "tp_jerry-chu",
            "Jerry PR 合并请示", "是否允许合并该 PR？\nA: 允许合并\nB: 暂缓合并",
            key="JERRY-PR-1", batch=batch_id, track=True, summary="是否允许合并该 PR？",
        )
        self.assertIsNone(decision_hub.verify_receipt_no_dead_ball(
            self.cfg, self.store, "tp_jerry-chu", batch_id,
            {"status": "needs_decision", "summary": "等主人拍板 PR 合并"},
        ))
        self.assertIsNone(hook.stop_check(raw_hook, cfg=self.cfg))


if __name__ == "__main__":
    unittest.main()
