"""7.56 正式牧羊犬把测试群当真：「这个群该不该管」只在一处判定（Config.chat_scope），所有路由入口都过它。

- ignore 名单里的群：不送、不当背景、不当指令、不按话题归属（主人在里面 @ 机器人也一样）；
- 找主人那一侧的聊天（找主人聊天、话题群）走自己的分支，不受 ignore 影响；
- doctor 列出账本里出现过、群名以「🧪」开头、却不在 ignore 名单里的群。
全部使用合成数据（*_test_*）。
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sheepdog import cli
from sheepdog.config import Config, IGNORED, NORMAL, OWNER_SIDE
from sheepdog.engine import BUS_TOPIC_ID, OWNER_INSTRUCTION
from sheepdog.models import Message
from sheepdog.store import Store

from test_group_threads import OPS, TOPIC_GRP, GroupBase
from test_identity import APP
from test_roster import ME, msg


class ChatScopeTest(unittest.TestCase):
    def test_one_place_decides(self):
        cfg = Config()
        cfg.routing.ignore_chat_ids = ["oc_test_ign", "oc_test_esc"]
        cfg.escalation.chat_ids = ["oc_test_esc"]
        cfg.topic_group.chat_id = "oc_test_tg"
        self.assertEqual(cfg.chat_scope("oc_test_ign"), IGNORED)
        self.assertEqual(cfg.chat_scope("oc_test_esc"), OWNER_SIDE)   # 找主人那一侧先于 ignore（它有自己的分支）
        self.assertEqual(cfg.chat_scope("oc_test_tg"), OWNER_SIDE)
        self.assertEqual(cfg.chat_scope("oc_test_other"), NORMAL)
        self.assertTrue(cfg.routing.ignored("oc_test_ign"))
        self.assertFalse(cfg.routing.ignored("oc_test_other"))


class OwnerAtBotInIgnoredGroupTest(GroupBase):
    def owner_at(self, chat=OPS, content="@Mattheuu G5 只给抄送", **kw) -> str:
        return self.say(chat, sender=ME, content=content, mentions=[APP], **kw)

    def bus_prompts(self) -> list[str]:
        t = self.store.get_topic(BUS_TOPIC_ID)
        return self.sink.to(t["conversation_id"]) if t is not None and t["conversation_id"] else []

    def test_regression_7_56_owner_at_bot_in_ignored_group_is_not_instruction(self):
        """回归 7.56：ignore 名单里的群（测试群），主人在里面 @ 机器人，不变成指令、不进总线、也不当背景。"""
        self.cfg.routing.ignore_chat_ids.append(OPS)
        before = len(self.bus_prompts())
        mid = self.owner_at()
        self.pull(mid)
        r = self.row(mid)
        self.assertNotEqual(r["reason"], OWNER_INSTRUCTION)
        self.assertIsNone(r["topic_id"])
        self.assertNotIn(OWNER_INSTRUCTION, r["tags_json"])
        # 机器人身份实时收到同一条（事件那条路）也一样
        ev = self.say(OPS, sender=ME, content="@Mattheuu 事件那条路", mentions=[APP])
        self.bot_hears(self.lark.event(ev))
        self.assertIsNone(self.row(ev)["topic_id"])
        self.d.dispatch_once()
        self.assertEqual(len(self.bus_prompts()), before)

    def test_regression_7_56_owner_reply_in_ignored_topic_group_thread_is_not_instruction(self):
        """回归 7.56：10-17 01:55 的样子——测试话题群（在 ignore 里）的话题里，主人回一句带 @ 机器人的话。"""
        root = self.root_to("tp_beta", chat=TOPIC_GRP, content="话题群里的开头 @Matthew")
        self.cfg.routing.ignore_chat_ids.append(TOPIC_GRP)
        mid = self.owner_at(chat=TOPIC_GRP, thread_root=root)
        self.pull(mid)
        r = self.row(mid)
        self.assertNotEqual(r["reason"], OWNER_INSTRUCTION)
        self.assertIsNone(r["topic_id"])

    def test_regression_7_56_not_ignored_group_still_instruction(self):
        """回归 7.56 的对照：不在 ignore 的群照旧变成指令送总线（没有误伤）。"""
        mid = self.owner_at()
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), (OWNER_INSTRUCTION, BUS_TOPIC_ID))


class DoctorTestGroupsTest(unittest.TestCase):
    """doctor：账本里出现过、群名以「🧪」开头、却不在 [routing] ignore_chat_ids 里的群，逐个列出 chat_id 和群名，标警告。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(state_dir=Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)

    def add(self, mid, chat_id, chat_name):
        self.store.upsert_message(msg(message_id=mid, chat_id=chat_id, chat_name=chat_name), "inbox", "group", [], None)

    def test_lists_unignored_test_groups(self):
        self.add("om_test_1", "oc_test_t1", "🧪 牧羊犬测试群")
        self.add("om_test_2", "oc_test_t1", "🧪 牧羊犬测试群")
        self.add("om_test_3", "oc_test_t2", "🧪 话题测试")
        self.add("om_test_4", "oc_test_real", "正经工作群")
        self.add("om_test_5", "oc_test_t3", "  🧪 前面有空格")
        self.cfg.routing.ignore_chat_ids = ["oc_test_t2"]
        lines = cli._test_group_warnings(self.cfg, self.store)
        text = "\n".join(lines)
        self.assertIn("oc_test_t1", text)
        self.assertIn("🧪 牧羊犬测试群", text)
        self.assertIn("oc_test_t3", text)
        self.assertNotIn("oc_test_t2", text)       # 已经在 ignore 里
        self.assertNotIn("oc_test_real", text)
        self.assertEqual(sum("oc_test_t1" in ln for ln in lines), 1)   # 一个群只列一次
        self.cfg.routing.ignore_chat_ids = ["oc_test_t1", "oc_test_t2", "oc_test_t3"]
        self.assertEqual(cli._test_group_warnings(self.cfg, self.store), [])

    def test_owner_side_test_group_not_listed(self):
        """话题群、找主人聊天是主人那一侧，本来就不走普通路由，不用进 ignore，不报。"""
        self.add("om_test_1", "oc_test_tg", "🧪 话题群")
        self.cfg.topic_group.chat_id = "oc_test_tg"
        self.assertEqual(cli._test_group_warnings(self.cfg, self.store), [])
