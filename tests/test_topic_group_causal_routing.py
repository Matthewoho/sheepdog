"""REG-020 ~ REG-022 回归测试：话题群入站因果路由、索引卡熔断解耦、首卡状态看板与 CLI 迁群。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sheepdog import duty, outbound, topicgroup as tg
from sheepdog.source import SourceError
from test_topic_group import TG, TGBase, _opt


class TopicGroupCausalRoutingTest(TGBase):
    """REG-020：入站批次（含 p... 插队批次与 #N 兄弟槽位）因果反查 T-n + 自动追认 alias_key。"""

    def test_priority_batch_and_changed_key_routes_back_to_item_and_binds_alias(self):
        rc, out, err = self.ask("--key", "elb-ssl-cert", "--track", title="证书续期", text="需要续期吗？")
        self.assertEqual(rc, 0, err)
        self.poll()
        it = self.item("elb-ssl-cert")
        self.assertIsNotNone(it)
        self.assertEqual(it["item_no"], 1)

        # 会话正在跑普通批次，主人在话题群 T-1 下回复触发 p... 插队批次
        self.store.add_system_message("tp_alpha", "bus_relay", "后台任务")
        self.d.dispatch_once()
        reply_mid = self.owner_tg("请继续跟进证书并回执", root=it["root_message_id"])
        self.poll()
        self.d.dispatch_once()

        # 确认当前存在 p... 插队批次，且 include_priority=True 能拿到该批次
        p_batch = outbound.current_batch(self.store, "tp_alpha", include_priority=True)
        self.assertTrue(p_batch.startswith("p"), p_batch)

        # 会话换了自拟的新钥匙 elb-ssl-cert-track-ack（且未传 --track），因果路由仍强制锁回 T-1
        rc, out, err = self.ask(
            "--key", "elb-ssl-cert-track-ack", kind="report",
            title="证书跟踪回执", text="已收到指令，正在核查 ELB 证书到期时间",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("T-1", out)
        last_reply = [c for c in self.lark.tg_sent() if c[1] == "+messages-reply"][-1]
        self.assertEqual(_opt(last_reply, "--message-id"), it["root_message_id"])

        # 新钥匙已自动追认为 T-1 的 alias_key（写进 tracked_items.extra.alias_keys 与 tracked_keys）
        found = tg.find_by_key(self.store, "elb-ssl-cert-track-ack")
        self.assertIsNotNone(found)
        self.assertEqual(found["item_no"], it["item_no"])
        extra = json.loads(found["extra"] or "{}")
        self.assertIn("elb-ssl-cert-track-ack", extra.get("alias_keys", []))
        self.assertIn("elb-ssl-cert-track-ack", tg.item_keys(self.store, it["item_no"]))
        self.assertEqual(self.row(reply_mid)["topic_id"], "tp_alpha")

    def test_slot_family_and_no_key_and_done_reopen_via_causal_route(self):
        self.ask("--key", "slot-item", "--track", title="多槽位事项", text="初始提问")
        self.poll()
        it = self.item("slot-item")
        tg.close(self.store, it, "tp_alpha", "阶段一完成")
        self.assertEqual(self.store.get_item(it["item_no"])["state"], tg.DONE)

        # 模拟主人在 T-1 回复后投递到了兄弟槽位 tp_alpha#2
        self.store.upsert_topic("tp_alpha#2", kind="managed", state="idle")
        mid = self.owner_tg("还有个尾项处理一下", root=it["root_message_id"])
        self.poll()
        self.store.conn.execute("UPDATE messages SET topic_id='tp_alpha#2' WHERE message_id=?", (mid,))
        self.store.conn.commit()

        # 即使调用方传 tp_alpha 或 tp_alpha#2 且不带 --key，resolve_inbound_item 與 route_for_send 仍命中 T-1 并重开
        resolved = tg.resolve_inbound_item(self.store, self.cfg, "tp_alpha#2")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["item_no"], it["item_no"])

        route = tg.route_for_send(self.store, self.cfg, "tp_alpha#2", "report", "", False)
        self.assertTrue(route.group)
        self.assertEqual(route.item_no, it["item_no"])
        self.assertEqual(self.store.get_item(it["item_no"])["state"], tg.ACTIVE)


class TopicGroupBreakerDecoupleTest(TGBase):
    """REG-021：索引卡 patch_card 失败与群可用性熔断解耦；已有 T-n 老话题优先直连回复并自愈。"""

    def test_index_card_patch_or_post_failure_never_marks_group_down(self):
        self.ask("--key", "IDX-SAFE", "--track", title="索引卡容错", text="首次进展")
        self.poll()
        self.d.dispatch_once()
        self.assertIsNone(tg.down_info(self.store))

        # 让下一次 patch_card 抛 SourceError（模拟飞书卡片限流或超限）
        info = tg.index_info(self.store)
        info["hash"] = "force-refresh"
        self.store.set_meta(tg.META_INDEX, json.dumps(info, ensure_ascii=False))
        tg.mark_dirty(self.store)
        with mock.patch.object(self.source, "patch_card", side_effect=SourceError("card table limit exceeded")):
            res = tg.refresh_index(self.cfg, self.store, self.source)
            self.assertEqual(res, "")
            self.assertFalse(tg.is_down(self.store))
            self.assertFalse(tg.sync_index(self.store, self.cfg, self.source))
            self.assertFalse(tg.is_down(self.store))

    def test_existing_item_reply_bypasses_tg_down_and_clears_down_on_success(self):
        self.ask("--key", "HEAL-1", "--track", title="自愈测试", text="建立话题")
        self.poll()
        it = self.item("HEAL-1")

        # 人工置 tg_down=True，验证已有 root_message_id 的老事项发消息不退回私聊，且成功后自动清除 tg_down
        tg.mark_down(self.cfg, self.store, "模拟前序偶发故障")
        self.assertTrue(tg.is_down(self.store))

        rc, out, err = self.ask("--key", "HEAL-1", kind="report", title="继续汇报", text="群内直连回复成功")
        self.assertEqual(rc, 0, err)
        self.assertIn("T-1", out)
        self.assertFalse(tg.is_down(self.store))

        # 只有当向该话题 reply_to 真正抛错时，才触发 mark_down 并降级私聊
        self.lark.plan["+messages-reply"] = ["error"]
        rc, out, err = self.ask("--key", "HEAL-1", kind="report", title="故障降级", text="这次真报错")
        self.assertEqual(rc, 0, err)
        self.assertIn("已退回私聊", out)
        self.assertTrue(tg.is_down(self.store))


class TopicGroupTitleCardAndMoveCliTest(TGBase):
    """REG-022：话题群首卡动态状态看板（sync_title_card）与 sheepdog topic-group move 命令。"""

    def test_sync_title_card_updates_template_summary_and_alias_keys(self):
        self.ask("--key", "CARD-1", "--track", title="首卡看板", text="请确认发布窗口")
        self.poll()
        it = self.item("CARD-1")
        root = it["root_message_id"]

        # ask 发送后首卡应自动 patch 为 orange（⏳ 等你定）并包含进展摘要与搜索钥匙
        self.assertIn(root, self.lark.patched)
        ask_card = self.lark.patched[root][-1]
        self.assertEqual(ask_card["header"]["template"], "orange")
        self.assertIn("⏳ 等你定", json.dumps(ask_card, ensure_ascii=False))

        # 绑定别名后发 report，首卡变为 blue（🔄 跟进中）且展示全部主/别名钥匙
        es = self.store.get_escalation(
            self.store.conn.execute("SELECT message_id FROM sends WHERE kind='ask'").fetchone()[0]
        )
        self.store.resolve_escalation(es["message_id"], "tp_alpha", "已确认", resolved_by="tp_alpha")
        tg.bind_alias_key(self.store, it["item_no"], "card-1-alias", "tp_alpha")
        self.ask("--key", "card-1-alias", kind="report", title="执行中", text="正在执行灰度切流")
        rep_card = self.lark.patched[root][-1]
        self.assertEqual(rep_card["header"]["template"], "blue")
        body_json = json.dumps(rep_card["body"], ensure_ascii=False)
        self.assertIn("🔄 跟进中", body_json)
        self.assertIn("正在执行灰度切流", body_json)
        self.assertIn("card-1-alias", body_json)

        # 结案后调用 sync_title_card，首卡变为 green（✅ 已结案）
        tg.close(self.store, self.store.get_item(it["item_no"]), "tp_alpha", "全量发布完成")
        self.assertTrue(tg.sync_title_card(self.store, self.cfg, self.source, it["item_no"], "全量发布完成", kind="done"))
        done_card = self.lark.patched[root][-1]
        self.assertEqual(done_card["header"]["template"], "green")
        self.assertIn("✅ 已结案", json.dumps(done_card, ensure_ascii=False))

    def test_cli_topic_group_move_is_idempotent(self):
        # 先在私聊起一个带钥匙的话题
        self.ask("--key", "dm-migrate-key", title="私聊长线任务", text="第一阶段汇报", kind="report")
        self.poll()
        self.assertIsNone(self.item("dm-migrate-key"))

        # 执行 sheepdog topic-group move --key dm-migrate-key --topic tp_alpha
        rc, out, err = self.cli("topic-group", "move", "--key", "dm-migrate-key", "--topic", "tp_alpha")
        self.assertEqual(rc, 0, err)
        self.assertIn("已迁入话题群：T-1", out)
        it = self.item("dm-migrate-key")
        self.assertIsNotNone(it)
        self.assertTrue(bool(it["root_message_id"]))

        # 重复执行幂等返回同一个 T-1，不重复开新话题
        rc2, out2, err2 = self.cli("topic-group", "move", "--key", "dm-migrate-key", "--topic", "tp_alpha")
        self.assertEqual(rc2, 0, err2)
        self.assertIn("已迁入话题群：T-1", out2)
        self.assertEqual(len(self.store.list_items(["active", "done", "archived"])), 1)


if __name__ == "__main__":
    unittest.main()
