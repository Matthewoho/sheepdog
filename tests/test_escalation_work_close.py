"""「需要你定」关了，羊群看板上由它补出来的活跟着收（10-15 20:33 待办清单里的僵尸 felix-codex-verify）。

三条路：主人已答、会话已结案（resolve）、过期（open_hours 内没人答）。
- 自动补的（会话没申报过）：收掉，结果写是哪条路。
- 会话在关闭之前自报过同一把钥匙「等主人」的：不替会话结束这件活，但不再算等主人（改成进行中），履历记一笔；
  清单「各会话在等你」不再列它。关闭之后会话又报「等主人」的，以新申报为准。
"""

from datetime import datetime, timedelta

from sheepdog import duty, pending as pend
from sheepdog.engine import BUS_TOPIC_ID

from test_duty import DutyBase

TEXT = "🐕 需要你定 · 发版 [tp_alpha] PR #12 要不要合"
KEY = "PR #12"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class EscalationWorkCloseTest(DutyBase):
    def ask(self, mid="om_test_esc1", hours_ago: float = 0, key: str = "") -> str:
        self.store.add_escalation(mid, "tp_alpha", "oc_test_esc", TEXT,
                                  iso(datetime.now().astimezone() - timedelta(hours=hours_ago)))
        if key:   # 经 sheepdog ask --key 发的：sends 里记着钥匙
            self.store.add_send(f"ik-{mid}", "ask", "tp_alpha", mid, TEXT, key=key, norm_key=duty.norm_key(key))
        return mid

    def close(self, how: str, mid="om_test_esc1"):
        if how == "answered":
            self.store.answer_escalation(mid, "om_test_ans")
        elif how == "resolved":
            self.store.resolve_escalation(mid, "tp_alpha", "主人在群里说合了")
        else:   # 过期：提问时间挪到 open_hours 之前，下一轮 _expire_escalations 关
            self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                    (iso(datetime.now().astimezone() - timedelta(hours=25)), mid))
            self.store.conn.commit()
            self.d._expire_escalations()

    def work(self):
        return self.store.get_work("tp_alpha", duty.norm_key(KEY))

    def history(self) -> list[str]:
        return [r[0] for r in self.store.conn.execute(
            "SELECT text FROM duty_history WHERE topic_id='tp_alpha' AND kind='work' ORDER BY rowid")]

    def waiting_in_pending(self) -> list[str]:
        return [w["key"] for w in pend.build(self.cfg, self.store)["waiting_owner"]]

    WHY = {"answered": "主人已答", "resolved": "会话已结案：主人在群里说合了", "expired": "已过期（24 小时没人答）"}

    def test_auto_work_closes_on_each_path(self):
        for how, why in self.WHY.items():
            with self.subTest(how=how):
                self.store.conn.execute("DELETE FROM work_items")
                self.store.conn.execute("DELETE FROM escalations")
                self.store.conn.commit()
                self.ask()
                self.round()
                self.assertEqual((self.work()["state"], self.work()["closed_at"]), (duty.WAITING_OWNER, None))
                self.close(how)
                self.round()
                w = self.work()
                self.assertEqual((w["state"], w["result"]), (duty.DONE, why))
                self.assertTrue(w["closed_at"])

    def test_declared_before_close_stops_waiting_on_the_owner_on_each_path(self):
        # felix-codex-verify 的样子：自动补出来以后，会话 work set 同一把钥匙「等主人」（会话自报 + 自动）
        for how, why in self.WHY.items():
            with self.subTest(how=how):
                self.store.conn.execute("DELETE FROM work_items")
                self.store.conn.execute("DELETE FROM escalations")
                self.store.conn.execute("DELETE FROM duty_history")
                self.store.conn.commit()
                self.ask()
                self.round()
                duty.set_work(self.store, "tp_alpha", KEY, title="等主人定", state=duty.WAITING_OWNER)
                self.round()
                self.assertEqual(self.work()["state"], duty.WAITING_OWNER)   # 提问还开着：照旧等主人
                self.close(how)
                self.assertEqual(self.waiting_in_pending(), [KEY])           # 修之前就是这样一直列着
                self.round()
                w = self.work()
                self.assertEqual((w["state"], w["closed_at"], w["declared"]), (duty.DOING, None, 1))   # 不替会话结束
                self.assertIn(f"{KEY} 不再算等主人：{why}", self.history()[-1])
                self.assertEqual(self.waiting_in_pending(), [])

    def test_declared_after_close_is_left_alone(self):
        # 关了以后会话又报「等主人」：以新申报为准
        self.ask()
        self.round()
        self.close("answered")
        self.round()
        later_ts = iso(datetime.now().astimezone() + timedelta(seconds=5))
        duty.set_work(self.store, "tp_alpha", KEY, state=duty.WAITING_OWNER)
        self.store.conn.execute("UPDATE work_items SET declared_at=? WHERE id=?", (later_ts, self.work()["id"]))
        self.store.conn.commit()
        self.round()
        self.assertEqual(self.work()["state"], duty.WAITING_OWNER)

    def test_declared_first_linked_by_the_ask_key(self):
        # 会话先自报（没有自动补的来源），再用同一把钥匙 ask：提问关了也不再算等主人
        duty.set_work(self.store, "tp_alpha", KEY, state=duty.WAITING_OWNER)
        self.store.conn.execute("UPDATE work_items SET declared_at=?", (iso(datetime.now().astimezone() - timedelta(minutes=5)),))
        self.store.conn.commit()
        self.ask(key=KEY)
        self.round()
        self.assertEqual(self.work()["state"], duty.WAITING_OWNER)
        self.close("resolved")
        self.round()
        self.assertEqual(self.work()["state"], duty.DOING)

    def test_another_open_ask_with_the_same_key_keeps_it_waiting(self):
        duty.set_work(self.store, "tp_alpha", KEY, state=duty.WAITING_OWNER)
        self.ask("om_test_esc1", key=KEY)
        self.ask("om_test_esc2", key=KEY)
        self.round()
        self.close("answered", "om_test_esc1")
        self.round()
        self.assertEqual(self.work()["state"], duty.WAITING_OWNER)
        self.close("answered", "om_test_esc2")
        self.round()
        self.assertEqual(self.work()["state"], duty.DOING)

    def test_declared_waiting_without_any_ask_is_left_alone(self):
        duty.set_work(self.store, "tp_alpha", "无关的事", state=duty.WAITING_OWNER)
        self.round((BUS_TOPIC_ID, "tp_alpha"))
        self.assertEqual(self.store.get_work("tp_alpha", duty.norm_key("无关的事"))["state"], duty.WAITING_OWNER)
