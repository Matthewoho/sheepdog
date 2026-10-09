"""7.15 主人在 IM 里的引用回复必须按引用走：机器人发给主人的消息都登记来源（第一行 attribution_regex /
aliases / 标题唯一命中）；引用认不出来源的一律送总线并附原文、绝不当成没引用（复现 10-09 事故）；
引用会话普通回复送回会话；所有有引用的消息投递时附「↪ 回复的是」。提问开头用 fixtures 的合成格式。"""

import json

from sheepdog.config import ConfigError, EscalationConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention

from test_escalation import ESC_CHAT, ask, esc_config, local, reply
from test_roster import ME, Base, FakeSource, msg

ATTR = r"\[(?P<topic>tp_[a-z0-9_.-]+)\]"


def bot(mid, text, minutes_ago=3):
    return msg(message_id=mid, chat_id=ESC_CHAT, chat_type="p2p", sender_id="ou_test_bot", sender_name="bot",
               sender_type="app", content=text, create_time=local(minutes_ago))


class QuoteReplyTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg.escalation = esc_config()
        self.cfg.escalation.attribution_regex = ATTR
        self.cfg.escalation.aliases = {"总线": BUS_TOPIC_ID}
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def poll(self, msgs):
        return Collector(self.cfg, self.store, FakeSource([msgs]), self.roster).poll_once()

    def opens(self):
        return [r["message_id"] for r in self.store.open_escalations()]

    def test_incident_unregistered_quote_goes_to_bus_not_open_question(self):
        # 10-09 事故复现：alpha 有一条未结问题；总线发了一条认不出来源的结论；主人引用它回「方案1」
        self.poll([ask("om_test_mms", "tp_alpha", "MMS 要不要上？"),
                   bot("om_test_bff", "FX [FX·不认识的名字] BFF 触发器排查结论：方案1 或方案2", minutes_ago=2)])
        self.assertIsNone(self.store.get_bot_outbox("om_test_bff"))
        self.poll([reply("om_test_ans", "方案1", reply_to="om_test_bff")])
        r = self.row("om_test_ans")
        self.assertEqual(r["topic_id"], BUS_TOPIC_ID)                 # 没有被送给 alpha
        self.assertEqual(self.opens(), ["om_test_mms"])               # MMS 问题没被当成已答
        notice = self.store.conn.execute("SELECT content FROM messages WHERE reason='escalation_list'").fetchone()
        self.assertIn("认不出来源的消息", notice["content"])
        self.assertIn("BFF 触发器排查结论", notice["content"])
        self.d.dispatch_once()
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn("↪ 回复的是：bot（ou_test_bot）：FX [FX·不认识的名字] BFF 触发器排查结论", bus)
        self.assertNotIn("om_test_ans", "\n".join(self.sink.to("conv_test_alpha")))

    def test_quote_of_session_normal_reply_goes_back(self):
        self.poll([ask("om_test_q", "tp_alpha", "另一个问题"),
                   bot("om_test_rep", "FX-REPORT [tp_alpha] 进展：已经发给对方了\n详情……")])
        out = self.store.get_bot_outbox("om_test_rep")
        self.assertEqual((out["topic_id"], out["is_question"]), ("tp_alpha", 0))
        self.assertEqual(self.store.get_bot_outbox("om_test_q")["is_question"], 1)
        self.poll([reply("om_test_r", "好的，辛苦", reply_to="om_test_rep")])
        r = self.row("om_test_r")
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertIn("回复的是 topic `tp_alpha` 发给主人的消息", r["note"])
        self.d.dispatch_once()
        self.assertEqual(self.opens(), ["om_test_q"])  # 普通回复不是问题，不关任何问题
        self.assertIn("↪ 回复的是：bot（ou_test_bot）：FX-REPORT [tp_alpha] 进展", self.sink.to("conv_test_alpha")[-1])

    def test_attribution_only_first_line(self):
        self.poll([bot("om_test_l2", "FX 一条汇报\n第二行才有 [tp_alpha]")])
        self.assertIsNone(self.store.get_bot_outbox("om_test_l2"))

    def test_aliases_and_titles_unique(self):
        self.poll([bot("om_test_al", "FX [FX·总线] 结论")])
        self.assertEqual(self.store.get_bot_outbox("om_test_al")["topic_id"], BUS_TOPIC_ID)
        # 标题：去掉前缀后相等或互相包含，唯一命中才算
        self.d.new_session("deploy", "发布排查", "FX 职责", [])
        self.poll([bot("om_test_t1", "FX [FX·发布排查] 结论")])
        self.assertEqual(self.store.get_bot_outbox("om_test_t1")["topic_id"], "tp_deploy")
        self.d.new_session("deploy2", "权限排查", "FX 职责", [])
        self.poll([bot("om_test_t2", "FX [FX·排查] 结论")])  # 两个标题都包含「排查」
        self.assertIsNone(self.store.get_bot_outbox("om_test_t2"))
        self.poll([reply("om_test_r2", "收到", reply_to="om_test_t2")])
        self.assertEqual(self.row("om_test_r2")["topic_id"], BUS_TOPIC_ID)
        # 名册会话的标题同样可用
        self.poll([bot("om_test_t3", "FX [x·Alpha 需求] 汇报")])
        self.assertEqual(self.store.get_bot_outbox("om_test_t3")["topic_id"], "tp_alpha")

    def test_quote_line_everywhere(self):
        # 别人在群里引用回复主人
        self.poll([msg(message_id="om_test_mine", chat_id="oc_test_g1", sender_id=ME, sender_name="主人", content="周五前给")])
        self.poll([msg(message_id="om_test_o1", chat_id="oc_test_g1", sender_name="Bob", sender_id="ou_test_bob",
                       reply_to="om_test_mine", mentions=[Mention(ME)]),
                   msg(message_id="om_test_o2", chat_type="p2p", chat_id="oc_test_p9", reply_to="om_test_not_here")])
        self.d.dispatch_once()
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn(f"↪ 回复的是：主人（{ME}）：周五前给", bus)
        self.assertIn("↪ 回复的是：（原文不在账本里）", bus)
        lines = bus.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith("### ") and "Bob" in ln)
        self.assertTrue(lines[i + 2].startswith("↪ 回复的是"))  # 标题、@ 行之后

    def test_no_reply_to_unchanged(self):
        self.poll([ask("om_test_q1", "tp_alpha", "唯一问题")])
        self.poll([reply("om_test_r3", "可以")])
        self.assertEqual(self.row("om_test_r3")["topic_id"], "tp_alpha")


class AttributionConfigTest(Base):
    def test_validation(self):
        for bad in (EscalationConfig(["oc_test_x"], "(?P<topic>x)", 24, "no-group"),
                    EscalationConfig(["oc_test_x"], "(?P<topic>x)", 24, "", {"总线": "bus"})):
            with self.assertRaises(ConfigError):
                bad.validate()
        EscalationConfig(["oc_test_x"], "(?P<topic>x)", 24, ATTR, {"总线": "tp_bus"}).validate()
