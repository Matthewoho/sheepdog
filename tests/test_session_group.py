"""7.49 会话组（羊圈）：
1. 话题/回复链/watch/等待回复亲和性（绝不拆链）；
2. 主会话（#1）空闲优先吸收新工作；
3. 主会话繁忙时自动扩出或路由到副会话（tp_<key>#2..#N）及全满时按最少待处理消息排队；
4. 批次提示词前注入羊圈同侪状态面板（<= 10 行）；
5. 名册 [[session]] max_sessions / group 与 [[group]] 语法糖及老名册 content_hash 零变化。
"""

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from sheepdog import duty, prompts
from sheepdog import session as sm
from sheepdog.engine import Collector, Dispatcher
from sheepdog.roster import RosterError, parse_roster
from sheepdog.router import route_within_group
from test_roster import Base, FakeSource, msg


class SessionGroupTest(Base):
    def setUp(self):
        super().setUp()
        self.roster = parse_roster(
            tomllib.loads(
                """
                [[session]]
                key = "ops"
                mode = "managed"
                conversation_id = "conv_test_ops_1"
                title = "值班羊"
                duty = "负责值班群告警与发布"
                max_sessions = 3
                chats = [{ chat_id = "oc_test_ops_grp", name = "值班群", all_messages = true }]
                """
            )
        )
        self.d = self.disp(self.roster)
        self.d.sync_roster()
        self.store.update_topic("tp_ops", onboarded_at="2026-10-20T00:00:00+08:00")

    def test_749_group_thread_affinity_never_splits(self):
        # 先扩出副会话 tp_ops#2，并让某条消息 om_test_root_2 归在 tp_ops#2 名下
        self.store.create_topic(
            "tp_ops#2", "值班羊 #2", "adopted", "负责值班群告警与发布", sm.ACTIVE, group_id="ops"
        )
        self.store.upsert_message(
            msg(
                message_id="om_test_root_2",
                chat_id="oc_test_ops_grp",
                sender_id="ou_test_alice",
                content="请帮忙排查订单延迟",
            ),
            "dispatch",
            "roster_chat_all",
            [],
            "tp_ops#2",
            "delivered",
        )
        # 此时哪怕主会话 tp_ops 完全空闲（active 且 0 条待处理），针对 om_test_root_2 的回复链也必须回到 tp_ops#2，绝不拆到 #1
        target = route_within_group(
            self.store,
            self.roster,
            "tp_ops",
            chat_id="oc_test_ops_grp",
            reply_to_msg_id="om_test_root_2",
            sender_id="ou_test_alice",
        )
        self.assertEqual(target, "tp_ops#2")

        # 登记在 tp_ops#2 名下的未结 watch 同样吸附回 tp_ops#2
        self.store.add_watch("tp_ops#2", "ou_test_bob", "oc_test_ops_grp", "等 Bob 确认回滚")
        target_watch = route_within_group(
            self.store,
            self.roster,
            "tp_ops",
            chat_id="oc_test_ops_grp",
            sender_id="ou_test_bob",
        )
        self.assertEqual(target_watch, "tp_ops#2")

    def test_749_group_idle_primary_absorbs_new_work(self):
        # 即使组内已有空闲的副会话 tp_ops#2，只要主会话 tp_ops 空闲（active 且 0 待处理），新话题一律优先进主会话 #1
        self.store.create_topic(
            "tp_ops#2", "值班羊 #2", "adopted", "负责值班群告警与发布", sm.ACTIVE, group_id="ops"
        )
        target = route_within_group(
            self.store,
            self.roster,
            "tp_ops",
            chat_id="oc_test_ops_grp",
            sender_id="ou_test_new_user",
        )
        self.assertEqual(target, "tp_ops")

    def test_749_group_busy_primary_spawns_or_routes_to_secondary(self):
        # 1. 主会话 tp_ops 收到第一条消息并进入 running（繁忙）
        self.poll([
            msg(message_id="om_test_m1", chat_id="oc_test_ops_grp", sender_id="ou_test_u1", content="任务一"),
        ])
        self.assertEqual(self.store.get_message("om_test_m1")["topic_id"], "tp_ops")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_ops")["state"], sm.RUNNING)

        # 2. 主会话 running 期间来无关新消息 om_test_m2 -> 自动扩出并路由到 tp_ops#2
        self.poll([
            msg(message_id="om_test_m2", chat_id="oc_test_ops_grp", sender_id="ou_test_u2", content="任务二"),
        ])
        self.assertEqual(self.store.get_message("om_test_m2")["topic_id"], "tp_ops#2")
        t2 = self.store.get_topic("tp_ops#2")
        self.assertIsNotNone(t2)
        self.assertEqual(t2["group_id"], "ops")

        # 3. 投递时 tp_ops#2 自动新建宿主会话并进入 running；再来第三条无关消息 om_test_m3 -> 自动扩出 tp_ops#3
        self.d.dispatch_once()
        t2_after = self.store.get_topic("tp_ops#2")
        self.assertTrue(bool(t2_after["conversation_id"]))
        self.assertEqual(t2_after["state"], sm.RUNNING)

        self.poll([
            msg(message_id="om_test_m3", chat_id="oc_test_ops_grp", sender_id="ou_test_u3", content="任务三"),
        ])
        self.assertEqual(self.store.get_message("om_test_m3")["topic_id"], "tp_ops#3")

        # 4. 羊圈已满（3/3，且 #3 已有 1 条待处理，#1 和 #2 各 0 条待处理），第 4 条无关消息按最少待处理 + 最小编号落回 tp_ops
        self.poll([
            msg(message_id="om_test_m4", chat_id="oc_test_ops_grp", sender_id="ou_test_u4", content="任务四"),
        ])
        self.assertEqual(self.store.get_message("om_test_m4")["topic_id"], "tp_ops")

    def test_749_group_peer_status_injected_in_prompt(self):
        # 组内有 tp_ops（running，摘要「正在发版 v1.2」）与 tp_ops#2（active）
        self.store.update_topic("tp_ops", state=sm.RUNNING, summary="正在发版 v1.2")
        self.store.create_topic(
            "tp_ops#2", "值班羊 #2", "adopted", "负责值班群告警与发布", sm.ACTIVE, group_id="ops"
        )
        self.store.upsert_message(
            msg(message_id="om_test_p2", chat_id="oc_test_ops_grp", sender_id="ou_test_u2", content="查一下监控"),
            "dispatch",
            "roster_chat_all",
            [],
            "tp_ops#2",
            "pending",
        )
        self.d.dispatch_once()
        t2 = self.store.get_topic("tp_ops#2")
        sent_prompts = self.sink.to(t2["conversation_id"])
        self.assertTrue(sent_prompts)
        batch_text = sent_prompts[-1]
        self.assertIn("## 🏠 羊圈同侪状态", batch_text)
        self.assertIn("`tp_ops` [running]：正在发版 v1.2", batch_text)

        # 验证直接调用 render_group_peer_status 行数上限严格 <= 10 行
        group_topics = self.store.get_group_topics("ops")
        rendered = prompts.render_group_peer_status("tp_ops#2", group_topics)
        self.assertLessEqual(len(rendered.splitlines()), 10)

        # 验证看板渲染带 [羊圈: ops · 2会话]
        cards = [
            duty.card_data(self.store, self.store.get_topic("tp_ops"), None),
            duty.card_data(self.store, self.store.get_topic("tp_ops#2"), None),
        ]
        board_txt = duty.render_board(cards, "10-20 12:00")
        self.assertIn("[羊圈: ops · 2会话]", board_txt)

    def test_749_roster_group_syntax_and_content_hash_invariance(self):
        # 1. 不带 max_sessions / group 的老名册与显式写默认值的名册 content_hash 一字不差
        r_legacy = parse_roster(
            tomllib.loads(
                """
                [[session]]
                key = "alpha"
                mode = "managed"
                conversation_id = "conv_test_a"
                """
            )
        )
        r_explicit_default = parse_roster(
            tomllib.loads(
                """
                [[session]]
                key = "alpha"
                mode = "managed"
                conversation_id = "conv_test_a"
                max_sessions = 1
                group = ""
                """
            )
        )
        self.assertEqual(r_legacy.content_hash(), "176ddf71a20781da")
        self.assertEqual(r_legacy.content_hash(), r_explicit_default.content_hash())

        # 2. 顶层 [[group]] 语法糖解析与虚拟副会话合成
        r_grp = parse_roster(
            tomllib.loads(
                """
                [[group]]
                key = "sre"
                title = "SRE 羊圈"
                duty = "SRE 值班"
                max_sessions = 3
                chats = ["oc_test_sre_1"]
                """
            )
        )
        s1 = r_grp.by_topic("tp_sre")
        self.assertIsNotNone(s1)
        self.assertEqual(s1.group_id, "sre")
        self.assertEqual(s1.max_sessions, 3)
        s2 = r_grp.by_topic("tp_sre#2")
        self.assertIsNotNone(s2)
        self.assertEqual(s2.title, "SRE 羊圈 #2")
        self.assertEqual(s2.group_id, "sre")
        self.assertIsNone(r_grp.by_topic("tp_sre#4"))

        # 3. known 模式禁止 max_sessions > 1
        with self.assertRaises(RosterError):
            parse_roster(
                tomllib.loads(
                    """
                    [[session]]
                    key = "k1"
                    mode = "known"
                    max_sessions = 2
                    """
                )
            )

    def test_767_laohu_group_max_sessions_spawns_clean_secondary_and_coalesces_burst(self):
        r_laohu = parse_roster(
            tomllib.loads(
                """
                [[session]]
                key = "laohu"
                mode = "managed"
                conversation_id = ""
                predecessor_conversation_id = "conv_test_laohu_old"
                retire_predecessor = true
                self_polling = "sp_test_old"
                retire_self_polling = true
                title = "人·老胡"
                duty = "负责老胡私聊与需求跟进"
                max_sessions = 2
                chats = [{ chat_id = "oc_test_laohu_p2p", name = "老胡私聊", all_messages = true }]
                """
            )
        )
        # 1. 合成的 #2 副会话绝不继承主槽位一次性前任与自轮询接手属性
        s2 = r_laohu.by_topic("tp_laohu#2")
        self.assertIsNotNone(s2)
        self.assertEqual(s2.predecessor_conversation_id, "")
        self.assertFalse(s2.retire_predecessor)
        self.assertEqual(s2.self_polling, "")
        self.assertFalse(s2.retire_self_polling)

        # 2. 主会话 tp_laohu 处于 running 长任务期间，老胡在私聊连发两条无引用消息
        d_lh = self.disp(r_laohu)
        d_lh.sync_roster()
        self.store.update_topic(
            "tp_laohu",
            conversation_id="conv_test_laohu_1",
            spawned_at="2026-10-20T00:00:00+08:00",
            onboarded_at="2026-10-20T00:00:00+08:00",
        )
        Collector(self.cfg, self.store, FakeSource([[
            msg(message_id="om_test_767_lh0", chat_type="p2p", chat_id="oc_test_laohu_p2p",
                sender_id="ou_test_laohu", content="帮我拉一份上月全量报表"),
        ]]), r_laohu).poll_once()
        d_lh.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_laohu")["state"], sm.RUNNING)

        Collector(self.cfg, self.store, FakeSource([[
            msg(message_id="om_test_767_lh1", chat_type="p2p", chat_id="oc_test_laohu_p2p",
                sender_id="ou_test_laohu", content="另外看下这个报错"),
            msg(message_id="om_test_767_lh2", chat_type="p2p", chat_id="oc_test_laohu_p2p",
                sender_id="ou_test_laohu", content="截图刚发你了"),
        ]]), r_laohu).poll_once()

        # 两条连发消息聚拢进同一个空闲副会话 tp_laohu#2，不把第 2 条拆回繁忙的 #1
        self.assertEqual(self.store.get_message("om_test_767_lh1")["topic_id"], "tp_laohu#2")
        self.assertEqual(self.store.get_message("om_test_767_lh2")["topic_id"], "tp_laohu#2")

        res = d_lh.dispatch_once()
        self.assertEqual(res["topics"]["tp_laohu#2"]["sent"], 2)
        t2 = self.store.get_topic("tp_laohu#2")
        self.assertEqual(t2["state"], sm.RUNNING)
        prompt_text = self.sink.to(t2["conversation_id"])[-1]
        self.assertIn("另外看下这个报错", prompt_text)
        self.assertIn("截图刚发你了", prompt_text)


if __name__ == "__main__":
    unittest.main()
