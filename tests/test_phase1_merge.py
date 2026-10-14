"""7.38 合并（第三轮）：current_batch 跳过主人插话批次。

ask 的防重编号和默认来源都靠 outbound.current_batch。主人插话批次（p…）只装主人那句回复、送得又比手上那批晚：
手上那批交了回执以后，「最近一批」不能取成它，否则同一句话会被当成新内容再发一条，默认来源也会变成找主人聊天里的那句。
"""

from datetime import timedelta

from sheepdog import outbound, pending as pend
from sheepdog.engine import PRIORITY_PREFIX, write_receipt

from test_owner_priority import ALPHA_P2P, PriorityBase
from test_threads import _opt


class CurrentBatchSkipsPriorityTest(PriorityBase):
    def test_ask_after_priority_batch_uses_the_original_batch(self):
        self.owner_reply("先看这个")
        self.d.dispatch_once()
        pb = [d["batch_id"] for d in self.store.recent_dispatches("tp_alpha") if d["batch_id"].startswith(PRIORITY_PREFIX)]
        self.assertEqual(len(pb), 1)                               # 插话批次送出去了，比 b1 晚
        self.assertEqual(self.store.latest_batch_id("tp_alpha"), pb[0])
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "handled", "summary": "ok"})
        self.d.dispatch_once()
        self.assertIsNone(self.topic()["pending_batch_id"])
        self.assertEqual(outbound.current_batch(self.store, "tp_alpha"), self.b1)
        rc, out, _ = self.ask(title="接着问", text="插话以后的提问")
        self.assertEqual(rc, 0, out)
        call = self.lark.sent()[-1]
        text = outbound.compose(self.cfg, "ask", "tp_alpha", "接着问", "插话以后的提问")
        self.assertEqual(_opt(call, "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "", self.b1, text))
        mid = self.store.conn.execute("SELECT message_id FROM sends ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual([(r["about_message_id"], r["about_chat_id"], r["origin"])
                          for r in self.store.escalation_sources(mid)], [(self.alice, ALPHA_P2P, "batch")])

    def test_backfill_skips_priority_batch_too(self):
        self.owner_reply("先看这个")
        self.d.dispatch_once()
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "handled", "summary": "ok"})
        self.d.dispatch_once()
        asked = (self.store.recent_dispatches("tp_alpha")[0]["sent_at"])
        self.assertEqual(pend.backfill_batch(self.store, "tp_alpha", asked), self.b1)
