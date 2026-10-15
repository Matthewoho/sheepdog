"""主人插话（7.37 第 4 件）的丢 / 重复 / 送错 / 状态。

前半是 7.46 独立审查（10-16）写的 7 个复现测试，原样搬进来（断言「应该」的行为，在 33bed2b 上 6 红 1 绿）；
后半是 7.47 七条修补各自的测试。
"""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import session as sm
from sheepdog.engine import (BUS_TOPIC_ID, PRIORITY_DISPATCH, PRIORITY_FOLLOWUP_PREFIX, PRIORITY_PREFIX, Collector,
                             _parse, clock, forward_messages, send_fail_key, uncertain_notice_key, write_receipt)
from sheepdog.hook import BATCH_OPENINGS, DEFAULT_STOP_REASON, opens_batch, owner_typed_after_batch
from sheepdog.playbook import Playbook
from sheepdog.prompts import (adopted_bootstrap, batch_prompt, compaction_rules_message, onboarding_prompt,
                              priority_prompt, retirement_prompt, rules_update_message)
from sheepdog.roster import parse_roster
from sheepdog.sink import SinkError, SinkTimeout
from sheepdog.sink.agentapi import AgentApiSink, transcript_path
from sheepdog.source.lark import parse_message

from test_owner_priority import ALPHA_CID, ALPHA_P2P, PriorityBase
from test_roster import ME, REPO, later, roster_data
from test_threads import ESC_CHAT


class ForwardAfterPriorityDuplicateTest(PriorityBase):
    """正式环境 10-16 00:31 的真实时序：主人回复 A（送 alpha），同一轮又引用 A 回复了 B（认不出来源 → 总线 + 未结列表，
    列表里把 A 的编号写成反引号）。总线按列表把 A、B 一起 forward 给 alpha。"""

    def run_case(self) -> int:
        a = self.owner_reply("周五发")
        b = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="补一句：发完告诉我")
        m = replace(parse_message(self.lark.raw(b)), reply_to=a)
        Collector(self.cfg, self.store, self.source, self.roster).ingest([m])
        self.assertEqual(self.row(b)["topic_id"], BUS_TOPIC_ID)
        self.d.dispatch_once()                                   # alpha 在跑：A 插话送到（或开关关着时排队）
        # 总线照列表转交（正式环境 sd_bus_relay_a90ae8201e65 就是这么做的）
        forward_messages(self.store, "tp_alpha", [a, b], note="主人说发完告诉他", self_open_id=ME)
        self.ack("tp_alpha")                                     # 原来那批交回执
        self.d.dispatch_once()
        self.d.dispatch_once()
        sent = [d["batch_id"] for d in self.store.recent_dispatches("tp_alpha", 100)
                if a in json.loads(d["message_ids"]) and d["state"] not in ("failed", "sending", "uncertain")]
        print("\n  A 所在的已送出批次:", sent, "（开关", self.cfg.session.owner_priority, "）")
        return len(sent)

    def test_priority_on_owner_reply_reaches_alpha_once(self):
        self.assertEqual(self.run_case(), 1, "主人同一句话送进 alpha 不止一次")

    def test_control_priority_off_reaches_once(self):
        self.cfg.session.owner_priority = False
        self.assertEqual(self.run_case(), 1)


class CorrectionDoesNotJumpTest(PriorityBase):
    """主人插话已经送到，紧接着撤回 / 改口：撤回、改口的通知应该同样当轮送到（不然会话照着撤回的话干到交回执）。"""

    def test_recall_of_priority_reply_reaches_running_session_same_round(self):
        a = self.owner_reply("周五发")
        self.d.dispatch_once()
        self.assertTrue(self.row(a)["batch_id"].startswith(PRIORITY_PREFIX))
        self.lark.msgs[a]["deleted"] = True                       # 主人撤回
        self.pull(a)
        self.assertEqual(self.row(a)["reason"], "recalled")
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual(self.topic()["state"], sm.RUNNING)
        got = "\n".join(self.alpha_texts()[n:])
        self.assertIn("已撤回", got, "撤回通知没有插队，排在原来那批回执后面")

    def test_edit_of_priority_reply_reaches_running_session_same_round(self):
        a = self.owner_reply("周五发")
        self.d.dispatch_once()
        self.lark.msgs[a]["content"] = "改成周六发"                # 主人改口（编辑）
        self.pull(a)
        self.assertEqual(self.row(a)["reason"], "edited")
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        got = "\n".join(self.alpha_texts()[n:])
        self.assertIn("改成周六发", got, "改口没有插队，排在原来那批回执后面")


class CrashRecoveryUncertainTest(PriorityBase):
    """发送途中进程被杀：agentapi 已经收下（会话忙，下一次调模型才写进记录），重启那一轮记录里还没有这一行。"""

    def test_priority_inserted_later_is_reconciled_or_owner_told(self):
        from sheepdog.sink.agentapi import transcript_path
        a = self.owner_reply("周五发")
        sent = []

        def accepted_then_killed(cid, content):
            sent.append(content)                                   # agentapi 已收下，还没写进会话记录
            raise RuntimeError("进程被杀")

        with mock.patch.object(self.sink, "send_message", side_effect=accepted_then_killed):
            with self.assertRaises(RuntimeError):
                self.d.dispatch_once()
        pb = self.store.dispatches_in_state("sending")[0]["batch_id"]
        self.d.dispatch_once()                                     # 重启后第一轮：记录里还没有
        self.assertEqual(self.store.get_dispatch(pb)["state"], "uncertain")
        # 几秒后会话下一次调模型，插话写进了记录
        p = transcript_path(ALPHA_CID)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "SYSTEM_MESSAGE", "content": sent[0]}, ensure_ascii=False) + "\n")
        self.d.dispatch_once()
        notices = self.store.conn.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0]
        state = self.store.get_dispatch(pb)["state"]
        self.assertTrue(state != "uncertain" or notices > 0,
                        f"插话已进会话，账本仍是 {state}、消息 {self.row(a)['dispatch_state']}，主人没收到任何通知")


class StopHookNeedleTest(PriorityBase):
    """收工查回执（hook）找「送达那一行」用的是 `原批次号`；插话批次正文里也写着 `原批次号`。"""

    def lines(self, tp: Path, with_priority: bool) -> None:
        b1 = self.b1
        rows = [self.row(self.owner_reply("周五发"))]
        pr = priority_prompt(Playbook(REPO / "examples" / "playbook"), "tp_alpha", "p1", b1, rows, "Alpha")
        out = [json.dumps({"type": "SYSTEM_MESSAGE", "content": f"[sheepdog] 新信号批次 `{b1}`"}),
               json.dumps({"type": "PLANNER_RESPONSE", "content": "x" * 4000}),     # 批次那一行被挤出末尾
               json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "主人亲自打字"})]
        if with_priority:
            out.append(json.dumps({"type": "SYSTEM_MESSAGE", "content": pr}, ensure_ascii=False))
        out.append(json.dumps({"type": "PLANNER_RESPONSE", "content": "继续"}))
        tp.write_text("\n".join(out) + "\n", encoding="utf-8")

    def test_owner_typing_not_hidden_by_priority_line(self):
        tp = Path(self.tmp.name) / "t.jsonl"
        self.lines(tp, with_priority=False)
        data = tp.read_bytes()
        tail = len(data) - (data.index(b"\n") + 50)            # 末尾这一段不含原批次那一行
        base = owner_typed_after_batch(tp, self.b1, tail)
        self.lines(tp, with_priority=True)
        data = tp.read_bytes()
        tail2 = len(data) - (data.index(b"\n") + 50)
        got = owner_typed_after_batch(tp, self.b1, tail2)
        self.assertIsNone(base)                                    # 没插话：找不到 → 不拦
        self.assertIsNot(got, False, "插话那一行被当成原批次送达那一行，主人打过字也判成没打过 → 会拦")


class StaleWaitingAfterPriorityAnswerTest(PriorityBase):
    """正式环境 10-15 21:52–21:57（tp_andyxiao）：会话提问 → 主人 1 分钟内回答（插话送到、提问关闭）→ 会话这一批的回执
    12 秒后才交，写的是 needs_decision（回执是在看到插话之前写好的）。"""

    def test_board_and_next_batch_do_not_claim_waiting_for_owner_after_answer(self):
        from sheepdog import duty
        self.owner_reply("周五发")
        self.d.dispatch_once()
        self.assertIsNotNone(self.store.get_escalation(self.ask_mid)["answered_at"])
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "needs_decision", "summary": "等主人定发版时间"})
        self.d.dispatch_once()
        card = self.store.get_card("tp_alpha")
        print("\n  会话状态:", self.topic()["state"], "| 看板:", card["run_state"], card["detail"])
        bob = self.lark.add(chat_id="oc_test_alpha_p2p", sender_id="ou_test_alice", content="Alice 追问")
        self.pull(bob)
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        nxt = "\n".join(self.alpha_texts()[n:])
        self.assertNotIn("仍在等待主人决策", nxt, "主人已经答了，下一批还告诉会话「仍在等待主人决策」")
        self.assertFalse(card["run_state"] == duty.WAITING and "等主人" in card["detail"],
                         f"主人已经答了，看板还写 {card['detail']}")


# ======================= 7.47 修补：每条至少一个测试 =======================
def in_record(cid: str, text: str) -> None:
    """会话记录里出现了这段文字（agentapi 收下的消息写进了记录）。"""
    p = transcript_path(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "SYSTEM_MESSAGE", "content": text}, ensure_ascii=False) + "\n")


class ForwardSkipTest(PriorityBase):
    """第 1 条：已经送到同一会话的不重发；命令输出逐条说明；认不出来源的引用说明里写清被引用的那条已送到哪。"""

    def delivered_a(self):
        a = self.owner_reply("周五发")
        self.d.dispatch_once()
        return a, self.row(a)["batch_id"]

    def test_forward_skips_message_already_delivered_to_target(self):
        a, pb = self.delivered_a()
        before = dict(self.row(a))
        res = forward_messages(self.store, "tp_alpha", [a], self_open_id=ME)
        self.assertEqual(list(res), [])
        self.assertEqual([(m, b) for m, b, _ in res.skipped], [(a, pb)])
        after = dict(self.row(a))
        for k in ("dispatch_state", "batch_id", "topic_id", "tags_json", "note"):
            self.assertEqual(after[k], before[k], k)          # 不回待推、不改标签、不改「回复的问题」说明
        self.assertEqual(self.store.count_pending_signals("tp_alpha"), 0)

    def test_forward_uncertain_message_still_goes(self):
        a, pb = self.delivered_a()
        self.store.mark_batch([a], pb, "tp_alpha", "uncertain")   # 不确定送到没有的照旧可以转
        res = forward_messages(self.store, "tp_alpha", [a], self_open_id=ME)
        self.assertEqual((list(res), res.skipped), ([a], []))
        self.assertEqual(self.row(a)["dispatch_state"], "pending")

    def test_forward_cli_says_already_delivered_and_exits_zero(self):
        a, pb = self.delivered_a()
        rc, out, err = self.cli("forward", "--topic", "tp_alpha", "--message-ids", a)
        self.assertEqual(rc, 0, err)
        hhmm = clock(_parse(self.store.get_dispatch(pb)["sent_at"]), self.cfg.timezone_offset)
        self.assertIn(f"`{a}` 已在 {hhmm} 随批次 `{pb}` 送到 topic `tp_alpha`，这次不重发", out)
        self.assertIn("没有要转交的", out)
        self.assertEqual(self.row(a)["dispatch_state"], "acked")

    def test_unrecognized_quote_note_says_where_it_went(self):
        a, pb = self.delivered_a()
        b = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="补一句：发完告诉我")
        m = replace(parse_message(self.lark.raw(b)), reply_to=a)
        Collector(self.cfg, self.store, self.source, self.roster).ingest([m])
        notes = [r["content"] for r in self.store.conn.execute(
            "SELECT content FROM messages WHERE reason='escalation_list' AND topic_id=?", (BUS_TOPIC_ID,))]
        hhmm = clock(_parse(self.store.get_dispatch(pb)["sent_at"]), self.cfg.timezone_offset)
        self.assertTrue(any(f"被引用的这条已在 {hhmm} 随批次 `{pb}` 送到 topic `tp_alpha`" in n and f"`{b}`" in n
                            for n in notes), notes)


class CorrectionScopeTest(PriorityBase):
    """第 2 条的边界：别人改了已送出的消息不插队；主人改口不论原来是插队送的还是随普通批次送的都插队。"""

    def test_edit_by_someone_else_stays_queued(self):
        self.lark.msgs[self.alice]["content"] = "Alice 改了需求"
        self.pull(self.alice)
        self.assertEqual((self.row(self.alice)["reason"], self.row(self.alice)["dispatch_state"]), ("edited", "pending"))
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual(self.alpha_texts()[n:], [])
        self.assertEqual(self.row(self.alice)["dispatch_state"], "pending")

    def test_owner_edit_of_normally_delivered_reply_jumps(self):
        self.ack("tp_alpha")
        self.d.dispatch_once()
        a = self.owner_reply("周五发")
        self.d.dispatch_once()                                    # 会话空着：随普通批次送到，会话又在跑了
        self.assertTrue(self.row(a)["batch_id"].startswith("b"))
        self.assertEqual(self.topic()["state"], sm.RUNNING)
        self.lark.msgs[a]["content"] = "改成周六发"
        self.pull(a)
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        got = "\n".join(self.alpha_texts()[n:])
        self.assertIn("主人插话批次", got)
        self.assertIn("改成周六发", got)


class NeedsDecisionScopeTest(PriorityBase):
    """第 3 条的边界：名下还有开着的「需要你定」，照旧等主人。"""

    def test_still_open_question_keeps_waiting_human(self):
        rc, out, _ = self.ask("--key", "PR #13", title="回滚", text="要不要回滚？")
        self.assertEqual(rc, 0, out)
        q2 = [k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT and k != self.ask_mid][-1]
        self.pull(q2)
        a = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="周五发")   # 引用回答第一个问题
        Collector(self.cfg, self.store, self.source, self.roster).ingest(
            [replace(parse_message(self.lark.raw(a)), reply_to=self.ask_mid)])
        self.assertEqual(self.row(a)["topic_id"], "tp_alpha")
        self.d.dispatch_once()
        self.assertTrue(self.row(a)["batch_id"].startswith(PRIORITY_PREFIX))
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "needs_decision", "summary": "等主人定回滚"})
        self.d.dispatch_once()
        t = self.topic()
        self.assertEqual((t["state"], t["summary"]), (sm.WAITING_HUMAN, "等主人定回滚"))

    def test_answered_by_priority_becomes_handled_with_note(self):
        self.owner_reply("周五发")
        self.d.dispatch_once()
        pb = self.store.conn.execute("SELECT batch_id FROM dispatches WHERE state='priority'").fetchone()[0]
        write_receipt(self.cfg, "tp_alpha", self.b1, {"status": "needs_decision", "summary": "等主人定发版时间"})
        self.d.dispatch_once()
        t = self.topic()
        self.assertEqual(t["state"], sm.ACTIVE)
        self.assertEqual(t["summary"], f"（回执说等主人拍板，但主人已在插话批次 `{pb}` 回复）等主人定发版时间")


# Antigravity 写会话记录的格式（10-16 架构侧和实现侧只读核过正式记录；以下全是合成的数据）
AG_SYSTEM_WRAP = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as "
                  "important information to pay attention to.\n\n<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-16T01:02:03Z "
                  "sender=system priority=MESSAGE_PRIORITY_HIGH content={body}\n</SYSTEM_MESSAGE>")
AG_USER_WRAP = ("<USER_REQUEST>\n{body}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: "
                "2026-10-16T09:02:03+08:00.\n</ADDITIONAL_METADATA>")
AG_KEEP_BYTES = 2048
LONG = "规则正文，规则正文，规则正文。\n" * 700          # 约 30KB


def ag_truncate(content: str) -> tuple[str, bool]:
    """照 Antigravity 的截法：太长就留开头约 2048 字节、结尾约 2048 字节，中间换成 `\n<truncated N bytes>\n`（截在字符边界上）。"""
    raw = content.encode("utf-8")
    if len(raw) <= 2 * AG_KEEP_BYTES:
        return content, False
    head = raw[:AG_KEEP_BYTES].decode("utf-8", errors="ignore")
    tail = raw[-AG_KEEP_BYTES:].decode("utf-8", errors="ignore")
    cut = len(raw) - len(head.encode()) - len(tail.encode())
    return f"{head}\n<truncated {cut} bytes>\n{tail}", True


def ag_record(body: str, kind: str = "SYSTEM_MESSAGE") -> tuple[dict, bool]:
    """sheepdog 发的正文在会话记录里的样子（一行 JSON）：套包装、按需截断。"""
    wrap = AG_USER_WRAP if kind == "USER_INPUT" else AG_SYSTEM_WRAP
    content, cut = ag_truncate(wrap.format(body=body))
    row = {"step_index": 0 if kind == "USER_INPUT" else 9, "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "SYSTEM",
           "type": kind, "status": "DONE", "created_at": "2026-10-16T01:02:03Z", "content": content}
    if cut:
        row["truncated_fields"] = ["content"]
    return row, cut


class BatchOpeningTest(unittest.TestCase):
    """第 4 条：Stop hook 只认真正开启一批（占用 pending_batch_id）的那条消息。开头的清单拿真的 prompt 核对；返工后按真记录的
    截断格式核（长消息只留首尾各约 2KB），批次头必须在正文第一行。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pb = Playbook(REPO / "examples" / "playbook")
        # 正式的 security.md 比示例长得多：拷一份示例 playbook，把它垫到 30KB，开场一定会被截断
        d = Path(self.tmp.name) / "playbook"
        import shutil
        shutil.copytree(REPO / "examples" / "playbook", d)
        (d / "security.md").write_text((d / "security.md").read_text(encoding="utf-8") + LONG, encoding="utf-8")
        (d / "retire.md").write_text((d / "retire.md").read_text(encoding="utf-8") + LONG, encoding="utf-8")
        self.long_pb = Playbook(d)
        roster = parse_roster(roster_data())
        self.s = roster.by_topic("tp_alpha")

    def openings(self, pb=None) -> dict[str, tuple[str, str, str, str]]:
        """各种占用 pending_batch_id 的批次：种类 → (开场正文, 批次号, 记在哪个 topic, 会话记录里的类型)。"""
        s, pb = self.s, pb or self.pb
        heir = replace(s, predecessor_conversation_id="conv_test_prev")
        b, o, h, r = ("b20261016010101-alpha", "o20261016010101-alpha", "h20261016010101-alpha",
                      "r20261016010101-alpha~prev")
        return {
            "b": (batch_prompt(pb, "tp_alpha", b, [], [], "Alpha"), b, "tp_alpha", "SYSTEM_MESSAGE"),
            "b+规则": (batch_prompt(pb, "tp_alpha", b, [], [], "Alpha", rules_update=LONG), b, "tp_alpha",
                       "SYSTEM_MESSAGE"),
            "b+长红线": (batch_prompt(pb, "tp_alpha", b, [], [], "Alpha", reminder=LONG), b, "tp_alpha", "SYSTEM_MESSAGE"),
            "o": (onboarding_prompt(pb, s, o, [15, 30], 60), o, "tp_alpha", "SYSTEM_MESSAGE"),
            "h": (adopted_bootstrap(pb, heir, "🐕 Alpha", h, [15, 30], 60), h, "tp_alpha", "USER_INPUT"),
            "r": (retirement_prompt(pb, "tp_alpha~prev", "Alpha（前任）", r, "🐕 Alpha", "tp_alpha"), r, "tp_alpha~prev",
                  "SYSTEM_MESSAGE"),
        }

    def mentions(self, pb=None) -> dict[str, tuple[str, str]]:
        """提到批次号、但不是开启那一批的消息：名字 → (正文, 会话记录里的类型)。"""
        pb = pb or self.pb
        b1 = "b20261016010101-alpha"
        stop = (Playbook(REPO / "examples" / "playbook").render("stop_receipt.md", topic_id="tp_alpha", batch_id=b1,
                receipt_command=f"sheepdog receipt --topic tp_alpha --batch {b1} --json '{{}}'",
                waiting_command=f"sheepdog receipt --topic tp_alpha --batch {b1} --json '{{}}'", blocked="1")
                or DEFAULT_STOP_REASON.replace("{{batch_id}}", b1))
        return {
            "插话": (priority_prompt(pb, "tp_alpha", "p20261016010202-alpha", b1, [], "Alpha", reminder=LONG),
                     "SYSTEM_MESSAGE"),
            "压缩后补发": (compaction_rules_message(LONG, "压缩说明", b1), "SYSTEM_MESSAGE"),
            "规则更新": (rules_update_message(LONG + f"\n手上的批次 `{b1}`"), "SYSTEM_MESSAGE"),
            "Stop hook 提示": ("Stop hook blocked termination: " + stop, "SYSTEM_MESSAGE"),
            "Stop hook 提示（短）": ("Stop hook blocked termination: " + DEFAULT_STOP_REASON.replace("{{batch_id}}", b1)
                                    .replace("{{receipt_command}}", f"sheepdog receipt --topic tp_alpha --batch {b1}"),
                                    "SYSTEM_MESSAGE"),
        }

    def test_every_batch_kind_opening_is_recognized(self):
        ops = self.openings()
        self.assertEqual({k[0] for k in ops}, {"b", "o", "h", "r"})
        self.assertEqual(len(BATCH_OPENINGS), 4)                  # 占用 pending_batch_id 的批次：b / o / h / r
        for kind, (text, bid, tid, _) in ops.items():
            self.assertTrue(opens_batch(text, bid, tid), kind)
            self.assertTrue(opens_batch(text, bid), kind)         # 不知道 topic 也认
            self.assertFalse(opens_batch(text, bid[:-1] + "9", tid), kind)
            if not kind.startswith("b"):                          # 普通批次的开头只认批次号（批次号里本来就带 topic）
                self.assertFalse(opens_batch(text, bid, "tp_other"), kind)

    def test_truncated_openings_still_recognized(self):
        """真记录的截断格式：包装 + 截断以后，b（带规则 / 不带规则）、o、h、r 都还认得出。"""
        for pb in (self.pb, self.long_pb):
            for kind, (text, bid, tid, rtype) in self.openings(pb).items():
                row, cut = ag_record(text, rtype)
                if kind in ("b+规则", "b+长红线") or (pb is self.long_pb and kind != "b"):
                    self.assertTrue(cut, kind)                    # 确实被截断了
                    self.assertIn("<truncated ", row["content"])
                self.assertTrue(opens_batch(row["content"], bid, tid), f"{kind} 截断={cut}")
                self.assertTrue(opens_batch(row["content"], bid), f"{kind} 截断={cut}")

    def test_truncated_b_with_rules_found_in_record(self):
        tp = Path(self.tmp.name) / "t.jsonl"
        text, bid, tid, rtype = self.openings()["b+规则"]
        row, cut = ag_record(text, rtype)
        self.assertTrue(cut)
        lines = [row, {"type": "PLANNER_RESPONSE", "content": "继续"},
                 {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "主人亲自打字"}]
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        self.assertIs(owner_typed_after_batch(tp, bid, 1 << 20, tid), True)
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines[:2]) + "\n", encoding="utf-8")
        self.assertIs(owner_typed_after_batch(tp, bid, 1 << 20, tid), False)

    def test_mentions_of_batch_are_not_openings(self):
        b1 = "b20261016010101-alpha"
        for name, (text, rtype) in self.mentions().items():
            self.assertIn(b1, text, name)
            row, _ = ag_record(text, rtype)
            for content in (text, row["content"]):              # 截断前、套包装截断后都不算
                self.assertFalse(opens_batch(content, b1, "tp_alpha"), name)
                self.assertFalse(opens_batch(content, b1), name)

    def test_onboarding_opening_found_in_record(self):
        tp = Path(self.tmp.name) / "o.jsonl"
        text, bid, tid, rtype = self.openings(self.long_pb)["o"]
        row, cut = ag_record(text, rtype)
        self.assertTrue(cut)
        lines = [row, {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "主人亲自打字"}]
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        self.assertIs(owner_typed_after_batch(tp, bid, 1 << 20, tid), True)


class UncertainRecheckTest(PriorityBase):
    """第 5 条：uncertain 批次每轮接着核；30 分钟核不到私聊主人一次；不自动重发；24 小时以后不再核。"""

    def setUp(self):
        super().setUp()
        self.notices: list[str] = []
        self.d.notifier = self.notices.append

    def killed(self, fn):
        sent = []

        def accepted_then_killed(cid, content):
            sent.append(content)
            raise RuntimeError("进程被杀")

        with mock.patch.object(self.sink, "send_message", side_effect=accepted_then_killed):
            with self.assertRaises(RuntimeError):
                fn()
        bid = self.store.dispatches_in_state("sending")[0]["batch_id"]
        self.d.dispatch_once()                                    # 重启后第一轮：记录里还没有
        self.assertEqual(self.store.get_dispatch(bid)["state"], "uncertain")
        return bid, sent[0]

    def told(self) -> list[str]:
        return [n for n in self.notices if "不确定送到没有" in n]

    def test_not_found_tells_owner_once_after_30_minutes_and_never_resends(self):
        a = self.owner_reply("周五发")
        pb, _ = self.killed(self.d.dispatch_once)
        n = len(self.alpha_texts())
        with later(29):
            self.d.dispatch_once()
        self.assertEqual(self.told(), [])
        with later(31):
            self.d.dispatch_once()
        with later(45):
            self.d.dispatch_once()
        told = self.told()
        self.assertEqual(len(told), 1, told)
        self.assertIn(f"`{pb}`", told[0])
        self.assertIn("1 条消息", told[0])
        self.assertIn(f"redeliver --batch {pb}", told[0])
        self.assertIn("Alpha", told[0])
        self.assertTrue(self.store.get_meta(uncertain_notice_key(pb)))
        self.assertEqual(self.store.get_dispatch(pb)["state"], "uncertain")
        self.assertEqual(self.row(a)["dispatch_state"], "uncertain")
        self.assertEqual(self.alpha_texts()[n:], [])                # 不自动重发

    def test_normal_batch_found_late_restarts_receipt_clock(self):
        self.ack("tp_alpha")
        self.d.dispatch_once()
        bob = self.lark.add(chat_id=ALPHA_P2P, sender_id="ou_test_alice", content="Alice 追问")
        self.pull(bob)
        bid, content = self.killed(self.d.dispatch_once)
        self.assertEqual(self.topic()["state"], sm.ACTIVE)
        in_record(ALPHA_CID, content)
        with later(25):
            self.d.dispatch_once()                                 # 核到了：补记送达，会话从这时起在做这批
            t = self.topic()
            self.assertEqual((t["state"], t["pending_batch_id"]), (sm.RUNNING, bid))
            self.assertEqual(self.row(bob)["dispatch_state"], "delivered")
            self.d.dispatch_once()                                 # 回执超时从核到时算，不是一补记就超时
            self.assertEqual(self.topic()["state"], sm.RUNNING)
        self.assertEqual(self.told(), [])

    def test_older_than_24_hours_not_rechecked(self):
        self.owner_reply("周五发")
        pb, content = self.killed(self.d.dispatch_once)
        in_record(ALPHA_CID, content)
        with later(24 * 60 + 1):
            self.d.dispatch_once()
        self.assertEqual(self.store.get_dispatch(pb)["state"], "uncertain")
        self.assertEqual(self.told(), [])


class PriorityFollowupTest(PriorityBase):
    """第 6 条：插话送到后 N 分钟内会话一条消息都没发，私聊主人一次。"""

    def setUp(self):
        super().setUp()
        self.notices: list[str] = []
        self.d.notifier = self.notices.append

    def told(self) -> list[str]:
        return [n for n in self.notices if "插话没回音" in n]

    def priority(self, text="周五发") -> tuple[str, str]:
        a = self.owner_reply(text)
        res = self.d.dispatch_once()
        return a, res["topics"]["tp_alpha"]["priority"]

    def test_silent_session_owner_told_once(self):
        a, pb = self.priority()
        self.assertEqual(len(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX)), 1)
        with later(9):
            self.d.dispatch_once()
        self.assertEqual(self.told(), [])
        with later(11):
            self.d.dispatch_once()
        with later(30):
            self.d.dispatch_once()
        told = self.told()
        self.assertEqual(len(told), 1, told)
        hhmm = clock(_parse(self.row(a)["create_time"]), self.cfg.timezone_offset)
        self.assertIn(f"你 {hhmm} 回的「周五发」插话送到了「", told[0])
        self.assertIn("Alpha", told[0])
        self.assertIn(f"批次 `{pb}`", told[0])
        self.assertIn("10 分钟没见它发任何消息", told[0])
        self.assertEqual(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX), [])

    def test_session_sent_something_no_reminder(self):
        self.priority()
        self.store.add_send("k_test_1", "report", "tp_alpha", "om_test_sent", "收到，周五发")
        at = (datetime.now().astimezone() + timedelta(minutes=2)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE idem_key='k_test_1'", (at,))
        self.store.conn.commit()
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.told(), [])
        self.assertEqual(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX), [])

    def test_send_before_priority_does_not_count(self):
        self.store.add_send("k_test_0", "report", "tp_alpha", "om_test_old", "之前发的")
        at = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE idem_key='k_test_0'", (at,))
        self.store.conn.commit()
        self.priority()
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(len(self.told()), 1)

    def test_zero_means_off(self):
        self.cfg.session.owner_priority_followup_minutes = 0
        self.priority()
        self.assertEqual(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX), [])
        self.cfg.session.owner_priority_followup_minutes = 10       # 之后再打开：关着时送的那批不补提醒
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.told(), [])

    def test_switched_off_while_waiting_clears_without_reminder(self):
        self.priority()
        self.assertEqual(len(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX)), 1)
        self.cfg.session.owner_priority_followup_minutes = 0        # 热加载改成 0
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.told(), [])
        self.assertEqual(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX), [])

    def test_custom_minutes(self):
        self.cfg.session.owner_priority_followup_minutes = 3
        self.priority()
        with later(4):
            self.d.dispatch_once()
        self.assertEqual(len(self.told()), 1)
        self.assertIn("3 分钟没见它发任何消息", self.told()[0])

    def test_session_removed_from_roster_not_reminded(self):
        self.priority()
        self.d.roster = self.roster = parse_roster(roster_data(session=[s for s in roster_data()["session"]
                                                                        if s["key"] != "alpha"]))
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.topic()["state"], sm.CLOSED)
        self.assertEqual(self.told(), [])
        self.assertEqual(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX), [])

    def test_crash_recovery_path_starts_followup_too(self):
        self.owner_reply("周五发")
        sent = []

        def accepted_then_killed(cid, content):
            sent.append(content)
            raise RuntimeError("进程被杀")

        with mock.patch.object(self.sink, "send_message", side_effect=accepted_then_killed):
            with self.assertRaises(RuntimeError):
                self.d.dispatch_once()
        in_record(ALPHA_CID, sent[0])                               # 重启那一轮记录里已经有了：走 _recover_sending 找到那支
        self.d.dispatch_once()
        self.assertEqual(len(self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX)), 1)
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(len(self.told()), 1)


class SendTimeoutTest(PriorityBase):
    """第 7 条：agentapi 超时抛 SinkTimeout；有「正在发」记录的投递留在 sending、下一轮核；不中断同一轮的其他会话。"""

    def test_agentapi_call_timeout_raises_sink_timeout(self):
        env = {"ANTIGRAVITY_AGENTAPI_EXE": "/nonexistent/agentapi", "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
               "ANTIGRAVITY_CSRF_TOKEN": "test"}
        boom = subprocess.TimeoutExpired(cmd=["agentapi"], timeout=60)
        with mock.patch.dict(os.environ, env), \
                mock.patch("sheepdog.sink.agentapi.subprocess.run", side_effect=boom) as run:
            with self.assertRaises(SinkTimeout):
                AgentApiSink().send_message("conv_x", "hi")
            with self.assertRaises(SinkError):                    # 新建会话超时：按普通 SinkError 处理
                AgentApiSink().new_conversation("t", "p")
        self.assertEqual(run.call_count, 2)

    def timeout_for(self, cid: str):
        real = self.sink.send_message
        sent = []

        def send(c, content):
            if c == cid:
                sent.append(content)
                raise SinkTimeout("agentapi send-message 超过 60 秒没返回（可能已经送到）")
            return real(c, content)
        return mock.patch.object(self.sink, "send_message", side_effect=send), sent

    def test_priority_timeout_stays_sending_then_reconciled(self):
        a = self.owner_reply("周五发")
        p, sent = self.timeout_for(ALPHA_CID)
        with p:
            res = self.d.dispatch_once()
        pb = res["topics"]["tp_alpha"]["timeout"]
        self.assertEqual(self.store.get_dispatch(pb)["state"], "sending")
        self.assertEqual(self.row(a)["dispatch_state"], "pending")     # 没标走，也没判失败
        self.assertEqual(self.store.get_meta(send_fail_key("tp_alpha")), "")
        t = self.topic()
        self.assertEqual((t["state"], t["pending_batch_id"], t["retries"]), (sm.RUNNING, self.b1, 0))
        in_record(ALPHA_CID, sent[0])
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual(self.store.get_dispatch(pb)["state"], PRIORITY_DISPATCH)
        self.assertEqual((self.row(a)["dispatch_state"], self.row(a)["batch_id"]), ("acked", pb))
        self.assertEqual(self.alpha_texts()[n:], [])                   # 没再发一遍

    def test_batch_timeout_does_not_stop_other_sessions_same_round(self):
        self.ack("tp_alpha")
        self.d.dispatch_once()
        bob = self.lark.add(chat_id=ALPHA_P2P, sender_id="ou_test_alice", content="Alice 追问")
        stranger = self.lark.add(chat_id="oc_test_stranger", sender_id="ou_test_bob", content="陌生人的私聊")
        self.pull(bob, stranger)
        bus_cid = self.store.get_topic(BUS_TOPIC_ID)["conversation_id"]
        nb = len(self.sink.to(bus_cid))
        p, sent = self.timeout_for(ALPHA_CID)
        with p:
            res = self.d.dispatch_once()                               # 不抛出：整轮照常跑完
        self.assertIn("陌生人的私聊", "\n".join(self.sink.to(bus_cid)[nb:]))   # 同一轮的总线照常送到
        bid = res["topics"]["tp_alpha"]["timeout"]
        self.assertEqual(self.store.get_dispatch(bid)["state"], "sending")
        t = self.topic()
        self.assertEqual((t["state"], t["retries"]), (sm.ACTIVE, 0))     # 不判失败
        self.assertEqual(self.row(bob)["dispatch_state"], "pending")
        self.d.dispatch_once()                                          # 下一轮核：记录里没有 → uncertain，不重发
        self.assertEqual(self.store.get_dispatch(bid)["state"], "uncertain")
        self.assertEqual(self.row(bob)["dispatch_state"], "uncertain")
        self.assertNotIn("Alice 追问", "\n".join(self.alpha_texts()))

    def test_timeout_before_dispatch_record_is_plain_failure(self):
        # 推规则这类没有「正在发」记录的发送：超时按普通 SinkError 处理（报失败）
        p, _ = self.timeout_for(ALPHA_CID)
        with p:
            rep = self.d.push_rules("tp_alpha")
        self.assertTrue(rep["tp_alpha"].startswith("失败：agentapi send-message 超过"), rep)


class FollowupConfigTest(unittest.TestCase):
    def test_followup_minutes_config(self):
        from sheepdog.config import ConfigError, load_config
        d = Path(tempfile.mkdtemp())
        p = d / "config.toml"
        self.assertEqual(load_config(d / "missing.toml").session.owner_priority_followup_minutes, 10)
        p.write_text("[session]\nowner_priority_followup_minutes = 0\n", encoding="utf-8")
        self.assertEqual(load_config(p).session.owner_priority_followup_minutes, 0)
        for bad in ("-1", "true", '"10"', "1.5"):
            p.write_text(f"[session]\nowner_priority_followup_minutes = {bad}\n", encoding="utf-8")
            with self.assertRaises(ConfigError, msg=bad):
                load_config(p)
        # 示例配置里写着它（默认值）
        example = (REPO / "examples" / "config.example.toml").read_text(encoding="utf-8")
        self.assertIn("owner_priority_followup_minutes = 10", example)
