"""REG-053 ~ REG-055 回归测试：总线禁锁群话题与专席夺回、纯知会 tell 防乒乓与拍板总台超长截断、Antigravity 空壳重建继承（7.87）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sheepdog import ack, decision_hub, duty, engine, outbound
from sheepdog.config import Config, TopicGroupConfig
from sheepdog.engine import Collector, Dispatcher
from sheepdog.models import Message
from sheepdog.roster import Roster, RosterSession
from sheepdog.source.lark import LarkSource
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
OPS_CHAT = "oc_ops_group"
TG_CHAT = "oc_test_tg"


class FakeSource787:
    """轻量飞书假消息源。"""

    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, str]] = []
        self.chat_sends: list[dict] = []
        self.replies: list[dict] = []
        self.patched: dict[str, list[dict]] = {}
        self.fail_card_reply = False

    def muted_chat_ids(self) -> set[str]: return set()
    def mget_messages(self, ids: list[str]) -> dict[str, Message]: return {}
    def senders_of(self, ids: list[str]) -> dict[str, str]: return {}
    def pin(self, message_id: str) -> None: pass

    def add_reaction(self, message_id: str, emoji_type: str, identity: str = "user") -> str:
        self.reactions.append((message_id, emoji_type, identity))
        return f"r_{message_id}"

    def send_to_chat(self, chat_id: str, text: str, idem_key: str = "", identity: str = "bot", *, card: dict | None = None) -> dict:
        mid = f"om_787_chat_{len(self.chat_sends) + 1}"
        self.chat_sends.append({"chat_id": chat_id, "text": text, "card": card, "message_id": mid})
        return {"message_id": mid, "thread_id": f"omt_{mid}"}

    def reply_to(self, message_id: str, *, identity: str = "bot", idem_key: str = "", text: str = "", card: dict | None = None, in_thread: bool = False) -> dict:
        if card is not None and self.fail_card_reply:
            raise RuntimeError("card payload too large (28KB limit)")
        mid = f"om_787_rep_{len(self.replies) + 1}"
        self.replies.append({"target": message_id, "text": text, "card": card, "in_thread": in_thread, "message_id": mid})
        return {"message_id": mid, "thread_id": f"omt_{mid}"}

    def patch_card(self, message_id: str, card: dict, *, identity: str = "bot") -> None:
        self.patched.setdefault(message_id, []).append(card)


class FakeSink787:
    """轻量 Antigravity 假宿主：每次 new_conversation 自动创建带 transcript.jsonl 的新容器。"""
    name = "agentapi"

    def __init__(self, brain_dir: Path) -> None:
        self.brain_dir = brain_dir
        self.spawned: list[dict] = []
        self.sent: list[dict] = []

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "") -> str:
        cid = f"cid-rebuilt-{len(self.spawned) + 1}"
        log_dir = self.brain_dir / cid / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "transcript.jsonl").write_text('{"type":"USER_INPUT","content":"init"}\n', encoding="utf-8")
        self.spawned.append({"cid": cid, "title": title, "prompt": prompt})
        return cid

    def send_message(self, conversation_id: str, text: str, **kwargs) -> None:
        self.sent.append({"conversation_id": conversation_id, "text": text})


class TestThreadClaimMailHubAndContainerRebuild787(unittest.TestCase):
    """REG-053 ~ REG-055 回归测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.brain_dir = self.tmp_path / "brain"
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.tmp_path / "config.toml"
        cfg_path.write_text(f'self_open_id = "{OWNER_ID}"\n', encoding="utf-8")
        self.cfg = Config(state_dir=self.tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        self.cfg.brain_dir = self.brain_dir
        self.cfg.session.verify_transcript_minutes = 2
        self.cfg.ack.enabled = True
        self.cfg.ack.emoji_type = "Get"
        self.cfg.ack.bot_reasons = ["owner_reply"]
        self.cfg.topic_group = TopicGroupConfig(chat_id=TG_CHAT, enabled=True)
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.store.create_topic("tp_bus", "总线", "bus", "总线职责", "active")
        self.store.create_topic("tp_alpha", "Alpha 专席", "adopted", "Alpha 职责", "active")
        self.store.create_topic("tp_beta", "Beta 专席", "adopted", "Beta 职责", "active")
        for tid, cid in (("tp_bus", "cid-bus"), ("tp_alpha", "cid-alpha"), ("tp_beta", "cid-beta")):
            self.store.update_topic(tid, conversation_id=cid, onboarded_at=now_iso())
        self.source = FakeSource787()
        self.sink = FakeSink787(self.brain_dir)

    def test_reg_053_bus_never_locks_group_threads_and_specialist_claims_thread(self) -> None:
        ts = now_iso()
        self.store.add_group_thread("omt_bus_try", OPS_CHAT, "tp_bus", "delivered", "om_r0", "测试", ts)
        self.assertIsNone(self.store.group_thread("omt_bus_try"))

        with self.store.tx() as c:
            c.execute(
                "INSERT INTO group_threads(thread_id,chat_id,topic_id,source,root_message_id,root_summary,created_at,last_activity_at) VALUES(?,?,?,?,?,?,?,?)",
                ("omt_dirty_bus", OPS_CHAT, "tp_bus", "delivered", "om_r_dirty", "脏行", ts, ts),
            )
        self.assertEqual(self.store.purge_bus_group_threads(), 1)
        self.assertIsNone(self.store.group_thread("omt_dirty_bus"))

        m_root = Message(
            message_id="om_thr_root_1", chat_id=OPS_CHAT, chat_name="运维群", chat_type="group",
            sender_id="ou_user_1", sender_name="同事甲", sender_type="user",
            content="告警：服务异常", msg_type="text", create_time=ts,
            thread_id="omt_787_thr_1", thread_position="0",
        )
        self.store.upsert_message(m_root, "dispatch", "keyword", [], topic_id="tp_bus")
        self.store.record_dispatch("b_bus_787", "tp_bus", ["om_thr_root_1"])
        self.store.mark_batch(["om_thr_root_1"], "b_bus_787", "tp_bus", "delivered")

        engine.forward_messages(self.store, "tp_alpha", ["om_thr_root_1"], self_open_id=OWNER_ID)
        gt = self.store.group_thread("omt_787_thr_1")
        self.assertIsNotNone(gt)
        self.assertEqual(gt["topic_id"], "tp_alpha")

        collector = Collector(self.cfg, self.store, self.source)
        m_follow = Message(
            message_id="om_thr_follow_2", chat_id=OPS_CHAT, chat_name="运维群", chat_type="group",
            sender_id="ou_user_2", sender_name="同事乙", sender_type="user",
            content="我补充一下日志", msg_type="text", create_time=ts,
            thread_id="omt_787_thr_1", thread_position="1", reply_to="om_thr_root_1", root_id="om_thr_root_1",
        )
        collector.ingest([m_follow])
        self.assertEqual(self.store.get_message("om_thr_follow_2")["topic_id"], "tp_alpha")

    def test_reg_054_polite_mail_to_bus_blocked_wakeup_touched_and_virtual_msg_skips_reaction(self) -> None:
        with self.assertRaises(duty.DutyError):
            duty.send_mail(self.store, self.cfg, "tp_alpha", "tp_bus", "【tp_alpha 同步】收到，已知悉")

        wakeup = self.cfg.state_dir / ".wakeup"
        if wakeup.exists(): wakeup.unlink()
        mid_ok = duty.send_mail(self.store, self.cfg, "tp_alpha", "tp_bus", "请转交 om_1 给 tp_beta 处理")
        self.assertGreater(mid_ok, 0)
        self.assertTrue(wakeup.exists())

        duty.send_mail(self.store, self.cfg, "tp_bus", "tp_alpha", "请跟进告警", key="AL-1")
        sys_row = self.store.conn.execute(
            "SELECT content FROM messages WHERE topic_id='tp_alpha' AND reason='duty_mail' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIn('回它：`sheepdog tell --topic tp_alpha --to tp_bus "..." --key AL-1`', sys_row["content"])
        self.assertIn("（仅需对方执行动作或回答问题时再回；纯收到/知会勿回 tell）", sys_row["content"])

        self.assertEqual(LarkSource.add_reaction(object(), "sd_card_cb_virtual_1", "Get"), "")
        m_virt = Message(
            message_id="sd_card_cb_virtual_1", chat_id=TG_CHAT, chat_name="话题群", chat_type="group",
            sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="[卡片回调] A", msg_type="text", create_time=now_iso(),
        )
        m_real = Message(
            message_id="om_real_msg_1", chat_id=OPS_CHAT, chat_name="运维群", chat_type="group",
            sender_id=OWNER_ID, sender_name="Owner", sender_type="user", content="真消息", msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(m_virt, "dispatch", "owner_reply", ["owner_verified"], topic_id="tp_alpha")
        self.store.upsert_message(m_real, "dispatch", "owner_reply", ["owner_verified"], topic_id="tp_alpha")
        acker = ack.Acker(self.cfg.ack, self.store, self.source)
        acker.on_delivered([self.store.get_message("sd_card_cb_virtual_1"), self.store.get_message("om_real_msg_1")])
        self.assertEqual([r[0] for r in self.source.reactions], ["om_real_msg_1"])

        decision_hub.ensure_hub_topic(self.cfg, self.store, self.source)
        long_text = "🐕 需要你定 · 架构方案 [tp_alpha]\n选哪个？\nA: 方案一\nB: 方案二\n---more---\n" + ("详情段落" * 1000)
        self.source.fail_card_reply = True
        hub_mid = decision_hub.mirror_ask_to_hub(
            self.cfg, self.store, self.source, origin_mid="om_ask_long_1", origin_root_id="om_root_long_1",
            topic_id="tp_alpha", key="ARCH-1", title="架构方案", shown=long_text,
        )
        self.assertTrue(hub_mid.startswith("om_787_rep_"))
        self.assertIn("…（详情已截断，完整原文见来源业务话题）", self.source.replies[-1]["text"])

    def test_reg_055_recycled_antigravity_container_auto_rebuilds_and_redelivers_once(self) -> None:
        old_ts = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
        self.store.update_topic(
            "tp_alpha", conversation_id="cid-dead-1", dispatched_at=old_ts, onboarded_at=old_ts, summary="历史记忆摘要：跟进 JEV"
        )
        duty.set_work(self.store, "tp_alpha", "JEV-1", state="doing", title="排查 JEV 告警")
        m1 = Message(
            message_id="om_787_rebuild_1", chat_id=OPS_CHAT, chat_name="运维群", chat_type="group",
            sender_id="ou_user_1", sender_name="同事甲", sender_type="user", content="@Matthew Hong 请看下 JEV", msg_type="text", create_time=now_iso(),
        )
        self.store.upsert_message(m1, "dispatch", "at_me", [], topic_id="tp_alpha")

        with mock.patch.object(
            engine, "transcript_path", side_effect=lambda cid: self.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        ), mock.patch.object(
            engine, "conversation_dir", side_effect=lambda cid: self.brain_dir / cid
        ):
            roster = Roster([RosterSession(key="alpha", mode="managed", title="Alpha 专席", duty="Alpha 职责", conversation_id="cid-dead-1")])
            disp = Dispatcher(self.cfg, self.store, self.sink, roster=roster, source=self.source)
            res = disp.deliver_only()
            self.assertEqual(res["sent"], 1)
            t_after = self.store.get_topic("tp_alpha")
            self.assertEqual(t_after["conversation_id"], "cid-rebuilt-1")
            self.assertIn("历史记忆摘要：跟进 JEV", self.sink.spawned[0]["prompt"])

            bid = t_after["pending_batch_id"]
            (self.brain_dir / "cid-rebuilt-1" / ".system_generated" / "logs" / "transcript.jsonl").unlink()
            self.store.update_topic("tp_alpha", dispatched_at=old_ts)
            disp._check_receipt(self.store.get_topic("tp_alpha"))
            t_rebuilt2 = self.store.get_topic("tp_alpha")
            self.assertEqual(t_rebuilt2["conversation_id"], "cid-rebuilt-2")
            self.assertEqual(self.store.get_meta(f"respawned_batch:{bid}"), "cid-rebuilt-2")

            (self.brain_dir / "cid-rebuilt-2" / ".system_generated" / "logs" / "transcript.jsonl").unlink()
            self.store.update_topic("tp_alpha", state="running", pending_batch_id=bid, dispatched_at=old_ts)
            disp._check_receipt(self.store.get_topic("tp_alpha"))
            self.assertEqual(self.store.get_topic("tp_alpha")["conversation_id"], "cid-rebuilt-2")

            disp.sync_roster()
            self.assertEqual(self.store.get_topic("tp_alpha")["conversation_id"], "cid-rebuilt-2")


if __name__ == "__main__":
    unittest.main()
