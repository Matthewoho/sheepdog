"""7.37 第 4 件：主人插话（SD-190）。

会话在跑上一批（running，等回执）时，「找主人」聊天里主人本人的回复（owner_reply）不再排队等回执或 20 分钟超时：当轮单独
发一批「主人插话」。不改会话状态、不要求单独回执、不参与回执超时；账本记成已投递（acked），下一轮不会当待推消息再送一遍，
原来那批超时退回重投时也不会带上它。

不插队（照旧排队）：会话被判中断 / 失联；主人正在会话里（被主人占着）；会话 failed / attention / closed（不是 running）；
命令行宿主（Codex / Claude）在跑上一轮。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import duty
from sheepdog import session as sm
from sheepdog.engine import (BUS_TOPIC_ID, PRIORITY_DISPATCH, PRIORITY_PREFIX, Dispatcher, write_receipt)
from sheepdog.playbook import Playbook
from sheepdog.prompts import DEFAULT_OWNER_PRIORITY, priority_prompt
from sheepdog.sink import SinkError

from test_roster import ME, REPO, later, owner_says
from test_threads import ESC_CHAT, ThreadsBase, now8

ALPHA_P2P = "oc_test_alpha_p2p"
ALPHA_CID = "conv_test_alpha"


def acted(conversation_id: str, minutes_from_now: float) -> None:
    """会话在 minutes_from_now 分钟后有一次动作（往合成的 transcript 追加一行模型回答）。"""
    from sheepdog.sink.agentapi import transcript_path
    at = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    p = transcript_path(conversation_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 8, "type": "PLANNER_RESPONSE", "status": "DONE",
                            "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "content": "继续干活"}) + "\n")


class PriorityBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        # 会话 tp_alpha 找过主人（未结的「需要你定」）；随后手上接了一批 Alice 的消息，还没交回执（running）
        rc, out, _ = self.ask("--key", "PR #12")
        self.assertEqual(rc, 0, out)
        self.ask_mid = next(k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT)
        self.pull(self.ask_mid)
        self.alice = self.lark.add(chat_id=ALPHA_P2P, sender_id="ou_test_alice", content="Alice 的需求",
                                   at=now8() - timedelta(minutes=5))
        self.pull(self.alice)
        self.d.dispatch_once()
        t = self.topic()
        self.assertEqual(t["state"], sm.RUNNING)
        self.b1, self.b1_at = t["pending_batch_id"], t["dispatched_at"]
        self.assertTrue(self.b1.startswith("b"))

    def topic(self, tid="tp_alpha"):
        return self.store.get_topic(tid)

    def owner_reply(self, text="周五发") -> str:
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=text, at=now8() - timedelta(minutes=1))
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"]), ("dispatch", "owner_reply"))
        return mid

    def alpha_texts(self) -> list[str]:
        return self.sink.to(ALPHA_CID)


class PriorityDeliveryTest(PriorityBase):
    def test_running_owner_reply_goes_out_same_round_as_priority_batch(self):
        mid = self.owner_reply()
        self.assertEqual(self.row(mid)["topic_id"], "tp_alpha")       # 回答的是 alpha 名下唯一一条未结提问
        n = len(self.alpha_texts())
        res = self.d.dispatch_once()
        got = self.alpha_texts()[n:]
        self.assertEqual(len(got), 1)                                  # 当轮就发出去了
        text = got[0]
        pb = res["topics"]["tp_alpha"]["priority"]
        self.assertTrue(pb.startswith(PRIORITY_PREFIX), pb)
        self.assertIn(f"[sheepdog] 主人插话批次 `{pb}`", text)
        self.assertIn(f"FX-PRIORITY batch={pb} pending={self.b1} topic=tp_alpha", text)   # playbook 模板
        self.assertIn("周五发", text)
        self.assertNotIn("Alice 的需求", text)                          # 只放主人的回复
        self.assertNotIn("receipt", text)                              # 不要求单独回执
        # 会话状态一概不动：还在跑，等的还是原来那一批的回执
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"], t["dispatched_at"]), (sm.RUNNING, self.b1, self.b1_at))
        # 账本：消息记成已投递（acked）归到插话批次；批次是终态 priority；「需要你定」已答
        r = self.row(mid)
        self.assertEqual((r["dispatch_state"], r["batch_id"]), ("acked", pb))
        self.assertEqual(self.store.get_dispatch(pb)["state"], PRIORITY_DISPATCH)
        self.assertIsNotNone(self.store.get_escalation(self.ask_mid)["answered_at"])
        self.assertEqual(res["topics"]["tp_alpha"]["sent"], 1)

    def test_no_duplicate_delivery_next_rounds_and_after_receipt(self):
        mid = self.owner_reply()
        self.d.dispatch_once()
        n = len(self.alpha_texts())
        self.d.dispatch_once()                                         # 还在跑：下一轮不再送
        self.assertEqual(len(self.alpha_texts()), n)
        self.assertEqual(self.store.pending_dispatch(), [])
        # 原来那批交回执，会话回到可投递；再来一条普通消息，新批次里没有主人那句
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.assertEqual(self.topic()["state"], sm.ACTIVE)
        bob = self.lark.add(chat_id=ALPHA_P2P, sender_id="ou_test_alice", content="Alice 追问")
        self.pull(bob)
        self.d.dispatch_once()
        later_texts = "\n".join(self.alpha_texts()[n:])
        self.assertIn("Alice 追问", later_texts)
        self.assertNotIn("周五发", later_texts)
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")

    def test_late_receipt_of_original_batch_still_accepted(self):
        self.owner_reply()
        self.d.dispatch_once()
        # 会话误把回执交给了插话批次：不算数，状态不动
        pb = self.store.conn.execute("SELECT batch_id FROM dispatches WHERE state=?", (PRIORITY_DISPATCH,)).fetchone()[0]
        write_receipt(self.cfg, "tp_alpha", pb, {"status": "handled", "summary": "答了主人"})
        self.d.dispatch_once()
        self.assertEqual((self.topic()["state"], self.topic()["pending_batch_id"]), (sm.RUNNING, self.b1))
        self.assertEqual(self.store.get_dispatch(pb)["state"], PRIORITY_DISPATCH)
        # 原来那批的回执晚一点到：照常收
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "needs_decision", "summary": "等主人"})
        self.d.dispatch_once()
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"], t["summary"]), (sm.WAITING_HUMAN, None, "等主人"))
        self.assertEqual(self.store.get_dispatch(self.b1)["state"], "acked")
        self.assertEqual(self.row(self.alice)["dispatch_state"], "acked")

    def test_other_signals_stay_queued_until_receipt(self):
        bob = self.lark.add(chat_id=ALPHA_P2P, sender_id="ou_test_alice", content="Alice 第二条")
        self.pull(bob)
        mid = self.owner_reply()
        n = len(self.alpha_texts())
        res = self.d.dispatch_once()
        text = "\n".join(self.alpha_texts()[n:])
        self.assertIn("周五发", text)
        self.assertNotIn("Alice 第二条", text)
        self.assertEqual(self.row(bob)["dispatch_state"], "pending")
        self.assertEqual(res["topics"]["tp_alpha"]["pending"], 2)
        self.ack("tp_alpha")
        self.d.dispatch_once()
        text = "\n".join(self.alpha_texts()[n + 1:])
        self.assertIn("Alice 第二条", text)
        self.assertNotIn("周五发", text)
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")

    def test_receipt_timeout_of_original_batch_does_not_resend_priority(self):
        # adopted 会话：原来那批回执超时只记一次 missed，插话那条不受影响
        mid = self.owner_reply()
        self.d.dispatch_once()
        n = len(self.alpha_texts())
        with later(self.cfg.session.receipt_timeout_minutes + 1):
            self.d.dispatch_once()
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"]), (sm.ACTIVE, None))
        self.assertEqual(self.store.get_dispatch(self.b1)["state"], "missed")
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")
        self.assertNotIn("周五发", "\n".join(self.alpha_texts()[n:]))


class BusPriorityTest(ThreadsBase):
    """总线（回执超时会把整批退回重投）：插话那条不跟着退回、不重投。"""

    def test_bus_timeout_requeues_only_original_batch(self):
        stranger = self.lark.add(chat_id="oc_test_stranger", sender_id="ou_test_bob", content="陌生人的私聊")
        self.pull(stranger)
        self.d.dispatch_once()
        bus = self.store.get_topic(BUS_TOPIC_ID)
        self.assertEqual(bus["state"], sm.RUNNING)
        b1 = bus["pending_batch_id"]
        cid = bus["conversation_id"]
        # 没有未结提问时，主人在找主人聊天里的话投总线
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="今天先别发版")
        self.pull(mid)
        self.assertEqual((self.row(mid)["topic_id"], self.row(mid)["reason"]), (BUS_TOPIC_ID, "owner_reply"))
        n = len(self.sink.to(cid))
        res = self.d.dispatch_once()
        got = self.sink.to(cid)[n:]
        self.assertEqual(len(got), 1)
        self.assertIn("今天先别发版", got[0])
        self.assertIn("主人插话批次", got[0])
        pb = res["topics"][BUS_TOPIC_ID]["priority"]
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["pending_batch_id"], b1)
        # 原来那批回执超时：退回重投的只有原来那批的消息
        n = len(self.sink.to(cid))
        with later(self.cfg.session.receipt_timeout_minutes + 1):
            self.d.dispatch_once()
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")
        self.assertEqual(self.row(mid)["batch_id"], pb)
        resent = "\n".join(self.sink.to(cid)[n:])
        self.assertNotIn("今天先别发版", resent)
        self.assertEqual(self.store.get_dispatch(b1)["state"], "failed")


class NoJumpTest(PriorityBase):
    """不插队的每种情况：照旧排队，什么都不发、会话状态不动。"""

    def assert_queued(self, mid: str, tid: str = "tp_alpha", why: str = "") -> dict:
        n = len(self.alpha_texts())
        res = self.d.dispatch_once()
        self.assertEqual(len(self.alpha_texts()), n)
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")
        self.assertIsNone(self.row(mid)["batch_id"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM dispatches WHERE batch_id LIKE ?",
                                                 (PRIORITY_PREFIX + "%",)).fetchone()[0], 0)
        if why:
            self.assertIn(why, res["topics"][tid].get("queued", ""))
        return res

    def test_stalled_or_lost_session_queues(self):
        for state in duty.STUCK:
            with self.subTest(state=state):
                mid = self.owner_reply(f"插话-{state}")
                with mock.patch.object(Dispatcher, "_duty_state", return_value=(state, "测试判定", "", False)):
                    self.assert_queued(mid, why=duty.RUN_LABEL[state])
                self.assertEqual(self.topic()["state"], sm.RUNNING)
                self.store.conn.execute("UPDATE messages SET dispatch_state='acked' WHERE message_id=?", (mid,))
                self.store.conn.commit()

    def test_owner_in_session_queues_until_he_leaves(self):
        owner_says(ALPHA_CID)                                          # 主人在会话里亲自说话
        mid = self.owner_reply()
        self.assert_queued(mid, why="主人在会话里")
        self.assertEqual(self.topic()["state"], sm.RUNNING)            # running 不进 human_attached
        self.assertEqual(self.store.get_card("tp_alpha")["run_state"], duty.OCCUPIED)
        mins = self.cfg.session.human_attach_minutes + 1
        acted(ALPHA_CID, mins - 0.5)                                    # 会话一直在干活（不判中断）
        with later(mins):                                              # 主人走了：插话送到
            res = self.d.dispatch_once()
        self.assertIn("priority", res["topics"]["tp_alpha"])
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")

    def test_attention_session_queues(self):
        mid = self.owner_reply()
        self.store.update_topic("tp_alpha", state=sm.ATTENTION, pending_batch_id=None)
        self.assert_queued(mid)
        self.assertEqual(self.topic()["state"], sm.ATTENTION)

    def test_failed_session_goes_the_normal_way(self):
        # failed 不是 running：照原来的路重投（普通批次，带回执要求、改状态），不是插话批次
        mid = self.owner_reply()
        self.store.update_topic("tp_alpha", state=sm.FAILED, pending_batch_id=None)
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        text = "\n".join(self.alpha_texts()[n:])
        self.assertIn("周五发", text)
        self.assertNotIn("主人插话批次", text)
        self.assertEqual(self.topic()["state"], sm.RUNNING)
        self.assertTrue(self.row(mid)["batch_id"].startswith("b"))

    def test_closed_session_gets_nothing(self):
        # closed 的会话不在投递名单里（消息改投总线）；直接问 _dispatch_topic 也只排队
        mid = self.owner_reply()
        self.store.update_topic("tp_alpha", state=sm.CLOSED, pending_batch_id=None)
        n = len(self.alpha_texts())
        res = self.d._dispatch_topic(self.topic(), [self.row(mid)])
        self.assertEqual((res["sent"], len(self.alpha_texts())), (0, n))
        self.assertNotIn("priority", res)
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")

    def test_cli_host_running_previous_turn_queues(self):
        class BusyCodex:
            def busy(self, cid):
                return "会话还在跑上一轮"

            def send_message(self, *a, **k):
                raise AssertionError("不该发")

        mid = self.owner_reply()
        self.store.update_topic("tp_alpha", host="codex")
        d = Dispatcher(self.cfg, self.store, {"antigravity": self.sink, "codex": BusyCodex()}, self.roster)
        res = d._dispatch_topic(self.topic(), [self.row(mid)])
        self.assertIn("命令行宿主在跑上一轮", res["queued"])
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")
        self.assertEqual(self.topic()["state"], sm.RUNNING)


class PrioritySendFailureTest(PriorityBase):
    def test_send_failure_keeps_message_pending_and_state(self):
        mid = self.owner_reply()
        with mock.patch.object(self.sink, "send_message", side_effect=SinkError("agentapi 连不上")):
            res = self.d.dispatch_once()
        self.assertIn("连不上", res["topics"]["tp_alpha"]["error"])
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"], t["retries"] or 0), (sm.RUNNING, self.b1, 0))
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")
        failed = self.store.dispatches_in_state("failed")
        self.assertEqual([d["batch_id"][0] for d in failed], [PRIORITY_PREFIX])
        # 下一轮发得出去：送到一次
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual(len(self.alpha_texts()), n + 1)
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")

    def test_crash_mid_send_recovered_from_transcript_without_touching_state(self):
        from sheepdog.sink.agentapi import transcript_path
        mid = self.owner_reply()

        def crash(cid, content):
            # 已经送进会话（transcript 里有了），还没来得及记账进程就没了
            p = transcript_path(cid)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"step_index": 9, "type": "SYSTEM_MESSAGE", "content": content},
                                   ensure_ascii=False) + "\n")
            raise RuntimeError("进程被杀")

        with mock.patch.object(self.sink, "send_message", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.d.dispatch_once()
        pb = self.store.dispatches_in_state("sending")[0]["batch_id"]
        self.assertTrue(pb.startswith(PRIORITY_PREFIX))
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual(len(self.alpha_texts()), n)                    # 不重发
        self.assertEqual(self.store.get_dispatch(pb)["state"], PRIORITY_DISPATCH)
        self.assertEqual((self.row(mid)["dispatch_state"], self.row(mid)["batch_id"]), ("acked", pb))
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"], t["dispatched_at"]), (sm.RUNNING, self.b1, self.b1_at))


class PriorityPromptTest(PriorityBase):
    def test_example_playbook_and_default_text(self):
        rows = [self.row(self.owner_reply())]
        ex = priority_prompt(Playbook(REPO / "examples" / "playbook"), "tp_alpha", "p1", "b1", rows, "Alpha")
        self.assertIn("主人插话：先回答主人这条，再继续手上的事", ex)
        self.assertIn("`b1`", ex)
        self.assertNotIn("<!--", ex)
        empty = Path(self.tmp.name) / "empty_playbook"
        empty.mkdir()
        de = priority_prompt(Playbook(empty), "tp_alpha", "p1", "b1", rows, "Alpha")
        self.assertIn(DEFAULT_OWNER_PRIORITY.replace("{{pending_batch}}", "b1"), de)
        for text in (ex, de):
            self.assertIn("周五发", text)
            self.assertIn("[sheepdog] 主人插话批次 `p1`（topic `tp_alpha`，共 1 条）", text)
