"""回归 7.12：会话发起、排队等常驻进程执行的动作，按提交顺序（id）执行。全部使用合成数据。"""

import unittest

from sheepdog import actions
from sheepdog import session as sm

from test_roster import Base


class ActionsOrderTest(Base):
    def setUp(self):
        super().setUp()
        actions._resident = False
        self.addCleanup(setattr, actions, "_resident", False)

    def test_regression_7_12_actions_run_in_submission_order(self):
        """回归 7.12：先提交 new-session、再提交 retire 这个新会话，常驻进程要按提交顺序执行，retire 看得见刚建的会话。
        坏了会怎样：后提交、依赖前一条结果的动作先跑，报「不存在」失败，而且失败的动作不重试。"""
        d = self.disp()
        d.init()
        a1 = actions.enqueue(self.store, "new_session", {"key": "dz", "title": "FX 临时", "duty": "FX 职责", "chats": [],
                                                         "message_ids": [], "note": ""})
        a2 = actions.enqueue(self.store, "retire", {"target": "tp_dz"})
        self.assertEqual(actions.run_pending(d), 2)
        rows = {a["id"]: a for a in self.store.list_actions(include_done=True)}
        self.assertEqual((rows[a1]["error"], rows[a2]["error"]), (None, None))
        self.assertEqual(self.store.get_topic("tp_dz")["state"], sm.CLOSED)
        self.assertEqual(self.store.get_topic("tp_dz.retired")["kind"], "retired")


if __name__ == "__main__":
    unittest.main()
