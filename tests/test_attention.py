"""7.21 会话卡在 attention：进入时私聊主人一次；因回执超时卡住的，晚到的回执或会话记录里的新动作让它自动恢复。

会话记录是临时目录里合成的 JSONL（格式照 2026-10-12 核过的 Antigravity transcript），发送用假的 sink 和假的 notifier。"""

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli, guard
from sheepdog import session as sm
from sheepdog.engine import BUS_TOPIC_ID, display_time, write_receipt
from sheepdog.sink import RunResult, SinkError
from sheepdog.store import Store

from test_guardrails import GuardBase
from test_roster import msg


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def planner(at: str, step: int = 1) -> dict:
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": at,
            "tool_calls": [{"name": "view_file", "args": {}}]}


class AttentionBase(GuardBase):
    def bus(self):
        return self.store.get_topic(BUS_TOPIC_ID)

    def age_bus(self):
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic(BUS_TOPIC_ID, dispatched_at=old)

    def stall_bus(self) -> list[str]:
        """给总线投一批，然后连续三次回执超时，卡进 attention。返回三批的批次号。"""
        self.poll([msg(message_id="om_test_at1", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        batches = []
        for _ in range(3):
            batches.append(self.bus()["pending_batch_id"])
            self.age_bus()
            self.d.dispatch_once()
        self.assertEqual(self.bus()["state"], sm.ATTENTION)
        return batches

    def bus_messages(self, needle: str) -> list:
        return [r for r in self.store.conn.execute("SELECT * FROM messages WHERE topic_id=?", (BUS_TOPIC_ID,))
                if needle in (r["content"] or "")]


class NoticeTest(AttentionBase):
    def test_receipt_timeout_three_times_notifies_owner_once(self):
        start = len(self.sink.to("conv_test_bus"))
        self.append("conv_test_bus", planner("2026-10-11T15:16:00Z"))  # 最后一次动作：北京时间 10-11 23:16
        self.stall_bus()
        self.assertEqual(len(self.sink.to("conv_test_bus")) - start, 3)  # 第一批 + 两次重投，第三次超时后不再投
        t = self.bus()
        self.assertEqual(t["attention_reason"], "receipt_timeout")
        self.assertTrue(t["attention_at"])
        self.assertEqual(len(self.notices), 1)
        n = self.notices[0]
        self.assertIn("[tp_bus]", n.splitlines()[0])
        self.assertIn("topic `tp_bus`", n)
        self.assertIn("回执超时", n)
        self.assertIn("累计失败 3 次", n)
        self.assertIn("从10-11 23:16（北京时间）起没回应（依据：会话记录里最后一次动作）", n)
        self.assertIn("排着 1 条信号", n)
        self.assertIn("会自动恢复", n)
        self.assertIn("FX-ATTN-NOTICE host=Antigravity reason=回执超时 queued=1 auto=yes topic=tp_bus", n)
        # 之后还卡着：新消息排队，不再投，也不再发通知
        self.poll([msg(message_id="om_test_at2", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_bus")) - start, 3)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.row("om_test_at2")["dispatch_state"], "pending")
        # sessions / doctor 里看得到
        lines = cli.attention_report(self.cfg, self.store)
        self.assertIn("1 个会话卡住", lines[0])
        self.assertIn("receipt_timeout", lines[1])
        self.assertIn("会自动恢复", lines[1])

    def test_notice_without_playbook_file_sends_facts_only(self):
        (self.pdir / "attention_notice.md").unlink()
        self.stall_bus()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("回执超时", self.notices[0])
        self.assertIn("没有回执或动作记录", self.notices[0])
        self.assertNotIn("FX-ATTN-NOTICE", self.notices[0])

    def test_other_reason_notifies_but_does_not_auto_recover(self):
        class Broken(type(self.sink)):
            def send_message(self, cid, content):
                if cid == "conv_test_bus":
                    raise SinkError("agentapi send-message 失败: fx")
                super().send_message(cid, content)

        # 先投出去一批、回执超时一次（failed），之后 agentapi 一直发不出去：失败计数满 3 次，压垮它的是发送失败
        self.poll([msg(message_id="om_test_sf1", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        d = self.d = self.make(Broken())
        self.age_bus()
        d.dispatch_once()
        self.assertEqual((self.bus()["state"], self.bus()["retries"]), (sm.FAILED, 2))
        d.dispatch_once()
        t = self.bus()
        self.assertEqual((t["state"], t["attention_reason"]), (sm.ATTENTION, "send_failed"))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("投递失败", self.notices[0])
        self.assertIn("agentapi send-message 失败", self.notices[0])
        self.assertIn("不会自动恢复", self.notices[0])
        self.assertIn("session-reset --topic tp_bus", self.notices[0])
        self.assertIn("auto=no", self.notices[0])
        # 会话记录里有新动作也不解开
        self.append("conv_test_bus", planner(utc(datetime.now() + timedelta(seconds=5))))
        d.dispatch_once()
        self.assertEqual(self.bus()["state"], sm.ATTENTION)
        self.assertEqual(len(self.notices), 1)
        # 人手动复位：attention 的时间、原因一起清掉
        with mock.patch.object(cli, "load_config", return_value=self.cfg), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_session_reset(mock.Mock(topic=BUS_TOPIC_ID)), 0)
        t = self.bus()
        self.assertEqual((t["state"], t["attention_at"], t["attention_reason"], t["retries"]), (sm.ACTIVE, None, None, 0))
        self.assertEqual(cli.attention_report(self.cfg, self.store), ["attention: 没有卡住的会话"])

    def test_bus_itself_in_attention_gets_no_self_hint(self):
        for tid in (BUS_TOPIC_ID, "tp_alpha"):
            self.store.record_dispatch(f"b_test_{tid}", tid, [])
            self.store.update_topic(tid, state=sm.RUNNING, pending_batch_id=f"b_test_{tid}")
            self.d._finish_run(RunResult(f"b_test_{tid}", tid, "codex", "conv_x", "timeout", "timeout", "跑了 30 分钟"))
            t = self.store.get_topic(tid)
            self.assertEqual((t["state"], t["attention_reason"]), (sm.ATTENTION, "host_timeout"))
        hints = self.bus_messages("这一轮超时")
        self.assertEqual(len(hints), 1)  # 只有说 tp_alpha 的那条；总线自己的不发给它自己
        self.assertIn("tp_alpha", hints[0]["content"])
        self.assertEqual(len(self.notices), 2)  # 两个会话各私聊主人一次
        self.assertTrue(all("不会自动恢复" in n for n in self.notices))


class RecoveryTest(AttentionBase):
    def test_late_receipt_recovers_and_delivers_queue(self):
        batches = self.stall_bus()
        self.poll([msg(message_id="om_test_at3", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_at3")["dispatch_state"], "pending")
        sent = len(self.sink.to("conv_test_bus"))
        # 第三批（判超时、把总线压进 attention 的那批）回执晚到
        write_receipt(self.cfg, BUS_TOPIC_ID, batches[-1], {"status": "handled", "summary": "晚到的进展"})
        self.d.dispatch_once()
        d = self.store.get_dispatch(batches[-1])
        self.assertEqual(d["state"], "acked_late")
        self.assertEqual(json.loads(d["receipt"])["summary"], "晚到的进展")
        self.assertEqual(d["error"], "回执超时")
        # 那批里的消息会话已经处理过：记为已回执，不再重投；之后排队的照常投出
        self.assertEqual((self.row("om_test_at1")["dispatch_state"], self.row("om_test_at1")["batch_id"]),
                         ("acked", batches[-1]))
        self.assertEqual(len(self.sink.to("conv_test_bus")), sent + 1)
        text = self.sink.to("conv_test_bus")[-1]
        self.assertIn("om_test_at3", text)
        self.assertNotIn("om_test_at1", text)
        t = self.bus()
        self.assertEqual((t["state"], t["attention_at"], t["attention_reason"], t["retries"]),
                         (sm.RUNNING, None, None, 0))
        self.assertEqual(t["summary"], "晚到的进展")
        self.assertEqual(len(self.notices), 2)
        self.assertIn("已恢复，补送 1 条", self.notices[1])
        self.assertIn("晚到的回执", self.notices[1])
        self.assertIn("FX-ATTN-RECOVERED host=Antigravity queued=1 topic=tp_bus", self.notices[1])
        # 再来一轮不会重复恢复、重复通知
        self.d.dispatch_once()
        self.assertEqual(len(self.notices), 2)

    def test_late_receipt_of_earlier_batch_also_counts(self):
        batches = self.stall_bus()
        self.poll([msg(message_id="om_test_at4", chat_type="p2p", chat_id="oc_test_stranger")])
        write_receipt(self.cfg, BUS_TOPIC_ID, batches[0], {"status": "needs_decision", "summary": "要主人定"})
        self.d.dispatch_once()
        self.assertEqual(self.store.get_dispatch(batches[0])["state"], "acked_late")
        # 第一批里就有 om_test_at1，会话处理过了：不再补送；回执是 needs_decision，下一批照常带上「等主人定」的进展
        self.assertEqual(self.row("om_test_at1")["batch_id"], batches[0])
        text = self.sink.to("conv_test_bus")[-1]
        self.assertIn("om_test_at4", text)
        self.assertNotIn("om_test_at1", text)
        self.assertIn("要主人定", text)
        self.assertEqual(self.bus()["state"], sm.RUNNING)
        self.assertIn("已恢复，补送 1 条", self.notices[-1])

    def test_transcript_activity_after_attention_recovers(self):
        self.append("conv_test_bus", planner("2026-10-11T15:16:00Z"))
        self.stall_bus()
        sent = len(self.sink.to("conv_test_bus"))
        # 进 attention 之后写进来的、但时间在 attention 之前的行，以及断网时的报错行，都不算
        self.append("conv_test_bus", planner("2026-10-11T15:20:00Z", 2),
                    {"type": "ERROR_MESSAGE", "source": "SYSTEM", "status": "DONE",
                     "created_at": utc(datetime.now() + timedelta(seconds=5))})
        self.d.dispatch_once()
        self.assertEqual(self.bus()["state"], sm.ATTENTION)
        self.assertEqual(len(self.sink.to("conv_test_bus")), sent)
        # 会话又动起来了：工具调用的执行行（GENERIC / MODEL）
        self.append("conv_test_bus", {"type": "GENERIC", "source": "MODEL", "status": "DONE", "step_index": 3,
                                      "created_at": utc(datetime.now() + timedelta(seconds=5)), "content": "fx"})
        self.d.dispatch_once()
        t = self.bus()
        self.assertEqual((t["state"], t["attention_reason"], t["retries"]), (sm.RUNNING, None, 0))
        self.assertEqual(len(self.sink.to("conv_test_bus")), sent + 1)
        self.assertIn("om_test_at1", self.sink.to("conv_test_bus")[-1])  # 退回排队的那批补送
        self.assertEqual(len(self.notices), 2)
        self.assertIn("已恢复，补送 1 条", self.notices[1])
        self.assertIn("会话记录在", self.notices[1])

    def test_recovery_without_prior_notice_sends_nothing(self):
        self.d.notifier = None
        batches = self.stall_bus()
        self.d.notifier = self.notices.append
        write_receipt(self.cfg, BUS_TOPIC_ID, batches[-1], {"status": "handled", "summary": "ok"})
        self.d.dispatch_once()
        self.assertEqual(self.bus()["state"], sm.ACTIVE)  # 那批的消息记为已回执，没有要补送的
        self.assertEqual(self.notices, [])

    def test_late_receipt_while_redispatched_resets_retries(self):
        self.poll([msg(message_id="om_test_rr1", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        first = self.bus()["pending_batch_id"]
        self.age_bus()
        self.d.dispatch_once()  # 超时一次：failed，同一轮重投
        t = self.bus()
        self.assertEqual((t["state"], t["retries"]), (sm.RUNNING, 1))
        second = t["pending_batch_id"]
        write_receipt(self.cfg, BUS_TOPIC_ID, first, {"status": "handled", "summary": "ok"})
        self.d.dispatch_once()
        t = self.bus()
        self.assertEqual((t["state"], t["retries"], t["pending_batch_id"]), (sm.RUNNING, 0, second))
        self.assertEqual(self.store.get_dispatch(first)["state"], "acked_late")
        self.assertEqual(self.row("om_test_rr1")["batch_id"], second)  # 已随第二批重投的不动

    def test_adopted_missed_batch_late_receipt_is_recorded(self):
        self.poll([msg(message_id="om_test_ad1", chat_id="oc_test_alpha_p2p")])
        self.d.dispatch_once()
        batch = self.store.get_topic("tp_alpha")["pending_batch_id"]
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic("tp_alpha", dispatched_at=old)
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_ad1")["dispatch_state"], "missed")
        write_receipt(self.cfg, "tp_alpha", batch, {"status": "handled", "summary": "迟到"})
        self.d.dispatch_once()
        self.assertEqual(self.store.get_dispatch(batch)["state"], "acked_late")
        self.assertEqual(self.row("om_test_ad1")["dispatch_state"], "acked")
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)


class LedgerTest(unittest.TestCase):
    def test_old_ledger_gets_attention_columns(self):
        with self.subTest("补列"):
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "old.db"
                c = sqlite3.connect(path)
                c.execute("""CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT, duty TEXT,
                             conversation_id TEXT, state TEXT NOT NULL, anchors_json TEXT DEFAULT '[]',
                             summary TEXT DEFAULT '', pending_batch_id TEXT, dispatched_at TEXT,
                             retries INTEGER DEFAULT 0, created_at TEXT NOT NULL, last_active TEXT NOT NULL)""")
                c.execute("INSERT INTO topics(topic_id,title,kind,state,created_at,last_active) "
                          "VALUES('tp_bus','总线','bus','attention','x','x')")
                c.commit()
                c.close()
                store = Store(path)
                cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(topics)")}
                self.assertTrue({"attention_at", "attention_reason"} <= cols)
                t = store.get_topic("tp_bus")
                self.assertEqual((t["attention_at"], t["attention_reason"]), (None, None))
                # 老账本里原因没记录：不自动恢复，显示出来要人复位
                line = cli.attention_line(t)
                self.assertIn("原因没记录", line)
                self.assertIn("不会自动恢复", line)
                store.close()


class ActivityUnitTest(unittest.TestCase):
    def test_activity_lines(self):
        ag = guard.ANTIGRAVITY
        self.assertIsNotNone(guard.activity_at(ag, planner("2026-10-11T16:40:02Z")))
        self.assertIsNotNone(guard.activity_at(ag, {"type": "GENERIC", "source": "MODEL",
                                                    "created_at": "2026-10-11T16:40:14Z"}))
        for d in ({"type": "ERROR_MESSAGE", "source": "SYSTEM", "created_at": "2026-10-11T15:37:09Z"},
                  {"type": "SYSTEM_MESSAGE", "source": "SYSTEM", "created_at": "2026-10-11T15:36:39Z"},
                  {"type": "USER_INPUT", "created_at": "2026-10-11T15:36:39Z"},
                  {"type": "PLANNER_RESPONSE"}):  # 没有时间的不算
            self.assertIsNone(guard.activity_at(ag, d))
        cx = {"type": "event_msg", "timestamp": "2026-10-10T10:55:34.867Z",
              "payload": {"type": "item_completed", "item": {"type": "AgentMessage"}}}
        self.assertEqual(guard.activity_at("codex", cx), datetime(2026, 10, 10, 10, 55, 34, 867000, timezone.utc))
        self.assertIsNone(guard.activity_at("codex", {"type": "event_msg", "timestamp": "2026-10-10T10:55:34Z",
                                                      "payload": {"type": "token_count"}}))
        self.assertIsNone(guard.activity_at("claude", {"type": "assistant", "timestamp": "2026-10-10T10:55:34Z"}))

    def test_last_activity_reads_tail_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            with p.open("w", encoding="utf-8") as f:
                f.write(json.dumps(planner("2026-10-11T10:00:00Z")) + "\n")
                f.write(json.dumps({"type": "GENERIC", "source": "MODEL", "content": "x" * 5000}) + "\n")
                f.write(json.dumps(planner("2026-10-11T16:41:48Z")) + "\n")
                f.write(json.dumps({"type": "ERROR_MESSAGE", "created_at": "2026-10-11T17:00:00Z"}) + "\n")
            self.assertEqual(guard.last_activity_at(guard.ANTIGRAVITY, p),
                             datetime(2026, 10, 11, 16, 41, 48, tzinfo=timezone.utc))
            self.assertEqual(guard.last_activity_at(guard.ANTIGRAVITY, p, tail=400),
                             datetime(2026, 10, 11, 16, 41, 48, tzinfo=timezone.utc))
            # 末尾这段里没有完整的动作行：读不到就是读不到，不往前翻
            self.assertIsNone(guard.last_activity_at(guard.ANTIGRAVITY, p, tail=100))
            self.assertIsNone(guard.last_activity_at(guard.ANTIGRAVITY, Path(d) / "none.jsonl"))

    def test_display_time(self):
        at = datetime(2026, 10, 11, 15, 57, tzinfo=timezone.utc)
        self.assertEqual(display_time(at), "10-11 23:57（北京时间）")
        self.assertEqual(display_time(at, "-05:00"), "10-11 10:57（UTC-05:00）")


if __name__ == "__main__":
    unittest.main()
