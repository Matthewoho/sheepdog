"""补来源的时间口径（7.36 / 7.38 配套）：飞书 create_time 只到分钟，提问登记的 asked_at 因此截到整分；批次的 sent_at 到秒。

- 老提问（没经 sheepdog ask 发、sends 里没有记录）补来源：会话在收到一批的同一分钟里提问，asked_at 比那一批的 sent_at
  还早，按「sent_at <= asked_at」会挑到上一批，来源记错。现在把 asked_at 当成「那一分钟里的某一刻」：取那一分钟结束前送到的
  最近一批，并且不晚于这条提问入账的时间（first_seen：同一轮先拉取后投递，入账以后才送的那批不是它看到的）。
- 经 sheepdog ask 发的提问：来源在发的时候就定了（--about / 那一批只来自一个聊天），没记就是故意不记（一批来自多个聊天），
  每轮补来源不能再补，否则绕过单聊天规则，还可能补成上一批。

这里的时间都直接写进账本（不靠测试跑在哪一秒），跨不跨分钟边界结果都一样。
"""

from datetime import timedelta

from sheepdog import pending as pend

from test_pending import PendingBase
from test_threads import ALICE, ALPHA_P2P, BOT, ESC_CHAT, now8

GRP = "oc_test_alpha_grp"


class SourceTimingTest(PendingBase):
    def setUp(self):
        super().setUp()
        # 提问那一分钟：10 分钟前的整分（离现在够远，入账时间不会卡住；也离 open_hours 很远）
        self.T = now8().replace(second=0, microsecond=0) - timedelta(minutes=10)

    def at_sec(self, seconds: float) -> str:
        return (self.T + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def batch(self, bid: str, mids: list, seconds: float, state: str = "acked") -> str:
        """这几条（已入账）作为 tp_alpha 的一批，在那一分钟的第 seconds 秒送到。"""
        self.store.record_dispatch(bid, "tp_alpha", mids)
        self.store.finish_dispatch(bid, state)
        self.store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?", (self.at_sec(seconds), bid))
        self.store.conn.commit()
        return bid

    def msg(self, chat: str, text: str) -> str:
        m = self.lark.add(chat_id=chat, sender_id=ALICE, content=text, chat_type="p2p" if chat == ALPHA_P2P else "group",
                          at=self.T - timedelta(minutes=4))
        self.pull(m)
        return m

    def two_batches(self) -> tuple[str, str, str, str]:
        """上一批（Alice 私聊，提问前 3 分钟送到）和这一批（群里，提问那一分钟的第 30 秒送到）。"""
        a0 = self.msg(ALPHA_P2P, "私聊：上一件事")
        g = self.msg(GRP, "群里：这一件事")
        return a0, self.batch("b_test_prev", [a0], -180), g, self.batch("b_test_this", [g], 30)

    def old_ask(self) -> str:
        """老办法发的提问（sends 里没有记录），create_time 是那一分钟的整分。"""
        q = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", at=self.T,
                          content="🐕 需要你定 · 老办法发的 [tp_alpha]\n要不要？")
        self.pull(q)
        self.assertEqual(self.store.get_escalation(q)["asked_at"], self.at_sec(0))
        return q

    def sources(self, q: str) -> list:
        return [(r["about_message_id"], r["origin"]) for r in self.store.escalation_sources(q)]

    def test_old_ask_in_the_same_minute_as_its_batch_gets_that_batch(self):
        # 同一分钟里先收批次（:30）、后提问（asked_at 截成 :00）：来源是这一批（群里那句），不是上一批（私聊那句）
        _, _, g, b1 = self.two_batches()
        q = self.old_ask()
        self.assertEqual(self.sources(q), [(g, "backfill")])
        self.assertEqual(pend.backfill_batch(self.store, "tp_alpha", self.at_sec(0)), b1)

    def test_batch_sent_after_the_ask_was_ingested_is_not_its_batch(self):
        # 同一分钟里：:30 送到这一批，会话提问，:40 牧羊犬拉到提问（入账），:50 又送了一批。提问看到的是 :30 那批
        _, _, g, b1 = self.two_batches()
        q = self.old_ask()
        self.store.conn.execute("DELETE FROM escalation_sources")
        self.store.conn.execute("UPDATE messages SET first_seen=? WHERE message_id=?", (self.at_sec(40), q))
        self.store.conn.commit()
        b2 = self.batch("b_test_after", [self.msg(ALPHA_P2P, "私聊：入账以后才送的")], 50)
        self.assertEqual(pend.followup_round(self.cfg, self.store, self.d.playbook)["backfilled"], 1)
        self.assertEqual(self.sources(q), [(g, "backfill")])
        self.assertEqual(pend.backfill_batch(self.store, "tp_alpha", self.at_sec(0), self.at_sec(40)), b1)
        self.assertEqual(pend.backfill_batch(self.store, "tp_alpha", self.at_sec(0)), b2)   # 不给入账时间：取到 :50 那批

    def test_sheepdog_ask_without_sources_is_never_backfilled(self):
        # 经 sheepdog ask 发的、那一批来自两个聊天：故意不记来源。之后每轮补来源不能绕过这条规则（更不能补成上一批）
        a0 = self.msg(ALPHA_P2P, "私聊：上一件事")
        self.batch("b_test_prev", [a0], -180)
        b1 = self.batch("b_test_this", [self.msg(ALPHA_P2P, "私聊：这一件"), self.msg(GRP, "群里：这一件")], 20, "sent")
        self.store.update_topic("tp_alpha", state="running", pending_batch_id=b1)   # 会话手上正是这两个聊天的一批
        q = self.asked()
        self.assertIn("个聊天", self.last_out)                     # ask 当场提示：没记来源
        self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?", (self.at_sec(0), q))
        self.store.conn.commit()
        self.assertEqual(pend.followup_round(self.cfg, self.store, self.d.playbook)["backfilled"], 0)
        self.assertEqual(self.sources(q), [])
        mine = [a for g in pend.build(self.cfg, self.store)["decide"] for a in g["asks"] if a["message_id"] == q]
        self.assertEqual([(a["source_origin"], a["source_chats"]) for a in mine], [("", [])])   # 清单现算也不补
        self.assertIsNotNone(a0)
