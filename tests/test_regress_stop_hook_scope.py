"""回归 7.37 第 5 件：收工查回执（Stop hook）只拦牧羊犬管的 Antigravity 会话（总线、名册 managed、总线新开的）。

test_stop_hook.PassTest.test_unknown_or_unmanaged_conversation 里 known / retired 的会话没有会话记录，hook 读会话记录出错、
按「出错不拦」放行，所以把「只认牧羊犬管的」那条筛选删掉它照样绿（7.60 补课时证伪发现）。这里给这个会话备好会话记录，
让「不是牧羊犬管的」成为放行的唯一原因，并带阳性对照：同一个会话改成名册 managed 就拦。数据全是造的。
"""

import json

from sheepdog import hook

from test_stop_hook import ALPHA_CID, HookBase, delivered, record, stop_input

ALPHA_BATCH = "b20261014071500-alpha-scope"


class StopHookScopeTest(HookBase):
    def setUp(self):
        super().setUp()
        self.edit("INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                  ALPHA_BATCH, "tp_alpha", "[]", "2026-10-14T07:15:00+08:00", "sent")
        record(ALPHA_CID, delivered(ALPHA_BATCH))      # 会话记录末尾找得到这一批：不会因为读不到记录而放行

    def running(self, kind: str) -> None:
        self.edit("UPDATE topics SET kind=?, state='running', pending_batch_id=? WHERE topic_id='tp_alpha'",
                  kind, ALPHA_BATCH)

    def test_regression_7_37_unmanaged_session_with_batch_in_transcript_is_not_blocked(self):
        """回归 7.37：只认识（known）、已退役（retired）的会话不归牧羊犬投递，收工时不拦；会话记录里有这一批也不拦。"""
        for kind in ("known", "retired"):
            with self.subTest(kind=kind):
                self.running(kind)
                self.assertEqual(self.run_main(stop_input(ALPHA_CID)), (0, ""))
                self.assertFalse(hook.count_path(self.cfg, "tp_alpha").exists())   # 也不记拦的次数

    def test_regression_7_37_same_session_managed_is_blocked(self):
        """回归 7.37 阳性对照：同一个会话、同一份会话记录，改成名册 managed（adopted）就拦，reason 指向它的批次。"""
        self.running("adopted")
        rc, out = self.run_main(stop_input(ALPHA_CID))
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["decision"], "continue")
        self.assertIn(f"batch={ALPHA_BATCH}", d["reason"])
        self.assertIn("topic=tp_alpha", d["reason"])
