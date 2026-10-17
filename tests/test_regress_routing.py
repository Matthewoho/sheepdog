"""7.60 补课：路由类已上线功能里，现有测试没守住的那几处（在导出副本里改坏对应代码时现有测试仍是绿的）。

- 7.13：名册负责的聊天，主人先开口（这个聊天还没投递过任何消息）也要当背景送给负责会话。现有测试里那个聊天先投递过
  别人的消息，按 follow_hours「最近处理过的会话」也能送对，名册归属这一步删掉也是绿的。
- 7.8：总线 forward 主人本人的话（不是在找主人聊天里说的、本来没带已核对标记）时，要补上「已核对」标注。现有测试转交的
  都是找主人聊天里本来就带标记的消息，转交时不补也是绿的。

数据一律是造的（*_test_*）。
"""

from sheepdog.engine import BUS_TOPIC_ID, forward_messages
from sheepdog.models import Mention
from sheepdog.prompts import OWNER_CONTEXT_LABEL, OWNER_REPLY_LABEL

from test_owner_context import _OwnerBase, mine
from test_roster import ME, msg


class OwnerContextRosterRegressionTest(_OwnerBase):
    def test_regression_7_13_owner_first_word_in_roster_chat_goes_to_owner(self):
        """回归 7.13：名册负责的群，主人在里面先开口（之前一条都没投递过），也当背景送给负责的会话，不因为「没人处理过」就丢。"""
        self.poll([mine("om_test_rr_m1", "oc_test_alpha_grp", "这个需求我下午看")])
        r = self.row("om_test_rr_m1")
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_context", "tp_alpha"))
        self.d.dispatch_once()
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertIn(OWNER_CONTEXT_LABEL, text)
        self.assertIn("> 这个需求我下午看", text)


class ForwardOwnerWordsRegressionTest(_OwnerBase):
    def test_regression_7_8_forward_owner_background_gets_verified_label(self):
        """回归 7.8：主人在普通群里的发言（背景，没有已核对标记）被总线 forward 给别的会话，送到时带「✅ 已核对」标注。"""
        self.poll([msg(message_id="om_test_rr_g1", chat_id="oc_test_rr_g9", mentions=[Mention(ME)])])
        self.d.dispatch_once()                       # 投到总线：之后主人在这个群的发言当背景送总线
        self.ack(BUS_TOPIC_ID)
        self.poll([mine("om_test_rr_m2", "oc_test_rr_g9", "这件事交给 Alpha 那边")])
        r = self.row("om_test_rr_m2")
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_context", BUS_TOPIC_ID))
        self.assertNotIn("owner_verified", r["tags_json"])
        forward_messages(self.store, "tp_alpha", ["om_test_rr_m2"], self_open_id=ME,
                         escalation_open_hours=self.cfg.escalation.open_hours)
        self.assertIn("owner_verified", self.row("om_test_rr_m2")["tags_json"])
        self.d.dispatch_once()
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertIn(OWNER_REPLY_LABEL, text)
        self.assertIn("> 这件事交给 Alpha 那边", text)
