"""回归 7.11 规则同步的两处缺口：总线新开的会话（dynamic）和 spawn 新建的会话。
建会话时都要记下规则指纹（第一批不重复带规则），改了 playbook 以后下一批都要带上完整新规则。全部使用合成数据。"""

import shutil
import unittest

from sheepdog.prompts import RULES_UPDATE_HEADER
from sheepdog.roster import parse_roster

from test_handover_watch import handover_data
from test_roster import FIXTURES, Base, msg


def spawn_only_data() -> dict:
    """handover_data() 里的待建条目 beta 去掉前任：spawn 后直接可投递（没有「读完前任」那一批）。"""
    d = handover_data()
    del d["session"][2]["predecessor_conversation_id"]
    del d["session"][2]["retire_predecessor"]
    return d


class RulesSyncNewSessionsTest(Base):
    def setUp(self):
        super().setUp()
        self.pdir = self.tmp.name + "/playbook"
        shutil.copytree(FIXTURES, self.pdir)
        self.cfg.playbook_dir = self.pdir

    def edit_common(self, text):
        with open(self.pdir + "/common.md", "w", encoding="utf-8") as f:
            f.write(text)

    def deliver(self, d, topic_id, chat_id, mid) -> str:
        """往 chat_id 发一条私聊，投一批，返回送到 topic_id 那个会话的最新一条，再替它交回执。"""
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id=chat_id)])
        d.dispatch_once()
        cid = self.store.get_topic(topic_id)["conversation_id"]
        text = self.sink.to(cid)[-1]
        self.assertIn(mid, text)
        self.ack(topic_id)
        return text

    def test_regression_7_11_dynamic_session_hash_at_creation_and_gets_update(self):
        """回归 7.11（DEMO-144 规则转 skill 升级）：总线新开的会话建好时记下规则指纹，第一批不带更新提示；
        改了 common.md 以后下一批带『📘 规则更新到 ...』指针并刷新 SKILL.md，且只提示一次。"""
        d = self.disp()
        d.init()
        d.new_session("dz", "FX 临时排查", "FX 职责：排查", [("oc_test_dz", True)])
        self.assertEqual(d.rules_status(self.store.get_topic("tp_dz")), "最新")
        first_text = self.deliver(d, "tp_dz", "oc_test_dz", "om_test_dz1")
        self.assertNotIn("📘 规则更新到", first_text)

        self.edit_common("FX-COMMON-V2 title={{session_title}} topic={{topic_id}}")
        self.assertIn("待更新", d.rules_status(self.store.get_topic("tp_dz")))
        text = self.deliver(d, "tp_dz", "oc_test_dz", "om_test_dz2")
        self.assertIn("📘 规则", text)
        self.assertLess(text.index("📘 规则"), text.index("om_test_dz2"))
        skill_text = (d._ensure_skillpack().skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("FX-COMMON-V2", skill_text)
        self.assertNotIn("📘 规则更新到", self.deliver(d, "tp_dz", "oc_test_dz", "om_test_dz3"))  # 只提示一次

    def test_regression_7_11_spawned_session_hash_at_creation_and_gets_update(self):
        """回归 7.11（DEMO-144 规则转 skill 升级）：sheepdog spawn 新建的会话建好时记下规则指纹，第一批不带更新提示；
        改了 common.md 以后下一批带规则更新指针并刷新 SKILL.md。"""
        self.roster = parse_roster(spawn_only_data())
        d = self.disp()
        d.init()  # init 自动 spawn 待建条目 beta（没有前任，建好直接可投递）
        self.assertIsNotNone(self.store.get_topic("tp_beta")["spawned_at"])
        self.assertEqual(d.rules_status(self.store.get_topic("tp_beta")), "最新")
        self.ack("tp_alpha")
        self.assertNotIn("📘 规则更新到", self.deliver(d, "tp_beta", "oc_test_beta_p2p", "om_test_sp1"))

        self.edit_common("FX-COMMON-V2 title={{session_title}}")
        text = self.deliver(d, "tp_beta", "oc_test_beta_p2p", "om_test_sp2")
        self.assertIn("📘 规则", text)
        skill_text = (d._ensure_skillpack().skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("FX-COMMON-V2", skill_text)


if __name__ == "__main__":
    unittest.main()
