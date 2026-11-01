"""REG-045 & REG-046 回归测试：飞书 batch_query 空 reaction_id 自点 Get 防误认回声与总线 at_me 真实动作门禁（7.84）。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sheepdog import actions, cli, decision_hub
from sheepdog.config import Config
from sheepdog.engine import Collector, forward_messages
from sheepdog.models import Message
from sheepdog.store import Store, now_iso

OWNER_ID = "ou_test_owner"
DEVOPS_CHAT = "oc_devops"
AT_ME_MID = "om_test_x100b61383e47a0a4ff9184401bc2588"


class FakeReactionSource784:
    """支持模拟飞书 im reactions batch_query 返回结构的假消息源。"""

    def __init__(self) -> None:
        self.reaction_map: dict[str, list[dict]] = {}

    def reactions_of(self, message_ids: list[str]) -> dict[str, list[dict]]:
        return {mid: list(self.reaction_map.get(mid, [])) for mid in message_ids if mid in self.reaction_map}


def _record_user_ack(store: Store, mid: str, chat_id: str, sender_id: str, reason: str, reaction_id: str) -> None:
    if hasattr(store, "record_ack"):
        store.record_ack(mid, chat_id, "group", sender_id, reason, reaction_id, identity="user")
    else:
        store.add_ack(mid, chat_id, "group", sender_id, reason, reaction_id, None, identity="user")


def _run_check_owner_reactions(collector: Collector, store: Store, *, baseline: bool = False) -> None:
    if not baseline:
        store.set_meta("owner_reactions_at", "2020-01-01T00:00:00+08:00")
    else:
        store.set_meta("owner_reactions_at", "")
    try:
        collector._check_owner_reactions(baseline=baseline)
    except TypeError:
        collector._check_owner_reactions()


class TestAntiSelfGetAndBusAtMe784(unittest.TestCase):
    """REG-045 & REG-046：空 reaction_id 自点确认表情防回声污染与 tp_bus at_me 防静默吞没门禁。"""

    def _make_env(self) -> tuple[Config, Store, FakeReactionSource784]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            f'self_open_id = "{OWNER_ID}"\n[owner_context]\nenabled = true\nreaction_check_minutes = 1\n',
            encoding="utf-8",
        )
        cfg = Config(state_dir=tmp_path, config_path=cfg_path, self_open_id=OWNER_ID)
        cfg.owner_context.enabled = True
        cfg.owner_context.reaction_check_minutes = 1
        cfg.ack.enabled = True
        cfg.ack.emoji_type = "Get"
        store = Store(cfg.db_path)
        self.addCleanup(store.close)
        for tid, title in (
            ("tp_bus", "总线"),
            ("tp_pr8305", "PR 8305 走查专席"),
            ("tp_sammy-shi", "Sammy 专席"),
        ):
            store.create_topic(tid, title, "bus" if tid == "tp_bus" else "adopted", f"{title}职责", "active")
        return cfg, store, FakeReactionSource784()

    def test_reg_045_self_ack_get_with_empty_reaction_id_not_mistaken_for_owner_reaction(self) -> None:
        cfg, store, source = self._make_env()
        collector = Collector(cfg, store, source)

        m1 = Message(
            message_id="om_at_me_1", chat_id=DEVOPS_CHAT, chat_name="产研群", chat_type="group",
            sender_id="ou_laohu", sender_name="老虎", sender_type="user",
            content="@Matthew Hong 请看下服务监控", msg_type="text", create_time=now_iso(),
        )
        m2 = Message(
            message_id="om_no_ack_2", chat_id=DEVOPS_CHAT, chat_name="产研群", chat_type="group",
            sender_id="ou_laohu", sender_name="老虎", sender_type="user",
            content="常规同步消息", msg_type="text", create_time=now_iso(),
        )
        store.upsert_message(m1, "dispatch", "at_me", [], topic_id="tp_bus")
        store.upsert_message(m2, "dispatch", "keyword", [], topic_id="tp_bus")
        store.record_dispatch("b_ack_784", "tp_bus", ["om_at_me_1", "om_no_ack_2"])
        store.mark_batch(["om_at_me_1", "om_no_ack_2"], "b_ack_784", "tp_bus", "delivered")

        # 记录 sheepdog 以主人身份自动点的 Get 确认表情
        _record_user_ack(store, "om_at_me_1", DEVOPS_CHAT, "ou_laohu", "at_me", "r_auto_get_1")

        # 1) 飞书 batch_query 返回 Get 但 reaction_id 为空串 ""：不得误认成主人点的表情
        source.reaction_map = {
            "om_at_me_1": [{"emoji_type": "Get", "operator_id": OWNER_ID, "reaction_id": ""}],
        }
        _run_check_owner_reactions(collector, store, baseline=False)
        self.assertFalse(store.owner_reaction_known("om_at_me_1", "Get", OWNER_ID))
        sys_msgs = store.conn.execute(
            "SELECT * FROM messages WHERE message_id LIKE 'sd_owner_context_%'"
        ).fetchall()
        self.assertEqual(len(sys_msgs), 0)

        # 2) 主人在 om_at_me_1 上点 THUMBSUP，或在未自动点 ack 的 om_no_ack_2 上点 Get：正常记录并生成 owner_context
        source.reaction_map = {
            "om_at_me_1": [
                {"emoji_type": "Get", "operator_id": OWNER_ID, "reaction_id": ""},
                {"emoji_type": "THUMBSUP", "operator_id": OWNER_ID, "reaction_id": ""},
            ],
            "om_no_ack_2": [
                {"emoji_type": "Get", "operator_id": OWNER_ID, "reaction_id": ""},
            ],
        }
        _run_check_owner_reactions(collector, store, baseline=False)
        self.assertFalse(store.owner_reaction_known("om_at_me_1", "Get", OWNER_ID))
        self.assertTrue(store.owner_reaction_known("om_at_me_1", "THUMBSUP", OWNER_ID))
        self.assertTrue(store.owner_reaction_known("om_no_ack_2", "Get", OWNER_ID))
        sys_msgs_after = store.conn.execute(
            "SELECT * FROM messages WHERE message_id LIKE 'sd_owner_context_%'"
        ).fetchall()
        self.assertEqual(len(sys_msgs_after), 2)

    def test_reg_045_purge_self_ack_owner_reactions_cleans_polluted_rows(self) -> None:
        cfg, store, _ = self._make_env()
        _record_user_ack(store, "om_polluted_1", DEVOPS_CHAT, "ou_laohu", "at_me", "r_auto_get_p1")
        # 污染行：acks 里有 user 身份确认表情，owner_reactions 误记了 reaction_id="" 的 Get，且生成了虚假系统消息
        store.add_owner_reaction("om_polluted_1", "Get", OWNER_ID, "", reported=True)
        store.add_system_message(
            "tp_bus", "owner_context",
            "📝 主人对这条消息点了 Get：群「产研群」 | 老虎（ou_laohu）：请看下（message_id `om_polluted_1`）",
        )
        # 真实行：THUMBSUP 与无 ack 记录的真实 Get
        store.add_owner_reaction("om_polluted_1", "THUMBSUP", OWNER_ID, "", reported=True)
        store.add_owner_reaction("om_real_get_2", "Get", OWNER_ID, "", reported=True)

        res = decision_hub.purge_self_ack_owner_reactions(cfg, store)
        purged_count = res if isinstance(res, int) else int(
            res.get("purged_reactions") or res.get("purged") or res.get("purged_count") or 0
        )
        self.assertEqual(purged_count, 1)
        self.assertFalse(store.owner_reaction_known("om_polluted_1", "Get", OWNER_ID))
        self.assertTrue(store.owner_reaction_known("om_polluted_1", "THUMBSUP", OWNER_ID))
        self.assertTrue(store.owner_reaction_known("om_real_get_2", "Get", OWNER_ID))

    def test_reg_046_bus_at_me_action_request_blocks_silent_noop_receipt(self) -> None:
        cfg, store, _ = self._make_env()
        batch_id = "b20261027234613"
        at_msg = Message(
            message_id=AT_ME_MID, chat_id=DEVOPS_CHAT, chat_name="产研群", chat_type="group",
            sender_id="ou_sammy", sender_name="Sammy", sender_type="user",
            content="@Matthew Hong 提醒Matthew在预生产走查PR 8305，验证通过后群里回复一声",
            msg_type="text", create_time=now_iso(),
        )
        store.upsert_message(at_msg, "dispatch", "at_me", [], topic_id="tp_bus")
        store.record_dispatch(batch_id, "tp_bus", [AT_ME_MID])
        store.mark_batch([AT_ME_MID], batch_id, "tp_bus", "delivered")
        store.update_topic("tp_bus", state="running", pending_batch_id=batch_id, dispatched_at=now_iso())

        # 1) 仅写 noop 或仅 tell 旁路会话，未对主人的 at_me 事项执行真实路由/上报动作：当场拦截并含 REG-046
        r_noop = {"status": "handled", "summary": "群内保持静默待主人走查", "actions": [{"type": "noop"}]}
        err_noop = decision_hub.verify_bus_at_me_no_silent_drop(cfg, store, "tp_bus", batch_id, r_noop)
        self.assertTrue(err_noop)
        self.assertIn("REG-046", err_noop)

        r_tell = {"status": "handled", "summary": "已顺手知会同事专席", "actions": [{"type": "tell", "target": "tp_sammy-shi"}]}
        err_tell = decision_hub.verify_bus_at_me_no_silent_drop(cfg, store, "tp_bus", batch_id, r_tell)
        self.assertTrue(err_tell)
        self.assertIn("REG-046", err_tell)

        # CLI cmd_receipt / main 同样返回退出码 2
        with patch("sheepdog.cli._load_cfg", return_value=cfg):
            rc = cli.main(["receipt", "--topic", "tp_bus", "--batch", batch_id, "--json", json.dumps(r_noop, ensure_ascii=False)])
            self.assertEqual(rc, 2)

        # 2a) 当 tp_bus 将该消息 forward 转交给专属会话后，校验放行
        cfg_a, store_a, _ = self._make_env()
        store_a.upsert_message(at_msg, "dispatch", "at_me", [], topic_id="tp_bus")
        store_a.record_dispatch(batch_id, "tp_bus", [AT_ME_MID])
        store_a.mark_batch([AT_ME_MID], batch_id, "tp_bus", "delivered")
        if hasattr(store_a, "mark_forwarded"):
            store_a.mark_forwarded([AT_ME_MID], "tp_pr8305")
        else:
            forward_messages(store_a, "tp_pr8305", [AT_ME_MID], self_open_id=OWNER_ID)
        self.assertIsNone(decision_hub.verify_bus_at_me_no_silent_drop(cfg_a, store_a, "tp_bus", batch_id, r_noop))

        # 2b) 当 tp_bus 入队了 new_session 动作后，校验放行
        cfg_b, store_b, _ = self._make_env()
        store_b.upsert_message(at_msg, "dispatch", "at_me", [], topic_id="tp_bus")
        store_b.record_dispatch(batch_id, "tp_bus", [AT_ME_MID])
        store_b.mark_batch([AT_ME_MID], batch_id, "tp_bus", "delivered")
        actions.enqueue(store_b, "new_session", {"key": "pr8305-check", "title": "PR 8305 走查", "duty": "走查", "message_ids": [AT_ME_MID]})
        self.assertIsNone(decision_hub.verify_bus_at_me_no_silent_drop(cfg_b, store_b, "tp_bus", batch_id, r_noop))

        # 2c) 当 tp_bus 调用了 ask / report / reply（记入 sends 表）后，校验放行
        cfg_c, store_c, _ = self._make_env()
        store_c.upsert_message(at_msg, "dispatch", "at_me", [], topic_id="tp_bus")
        store_c.record_dispatch(batch_id, "tp_bus", [AT_ME_MID])
        store_c.mark_batch([AT_ME_MID], batch_id, "tp_bus", "delivered")
        store_c.add_send("ik_784_ask", "ask", "tp_bus", "om_ask_784_1", "请确认 PR 8305 预生产走查", title="PR 8305 走查")
        self.assertIsNone(decision_hub.verify_bus_at_me_no_silent_drop(cfg_c, store_c, "tp_bus", batch_id, r_noop))


if __name__ == "__main__":
    unittest.main()
