"""7.20 在岗表：会话卡片的运行状态、撞车提醒、总线摘要、会话留言、活的登记与自动补、履历清理。

会话记录是临时目录里合成的 JSONL（格式照 2026-10-12 核过的 Antigravity transcript），发送用假的 sink 和假的 notifier。"""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from sheepdog import cli, duty, guard
from sheepdog import session as sm
from sheepdog.config import ConfigError, DutyConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, forward_messages, write_receipt
from sheepdog.sink import RunResult
from sheepdog.sink.agentapi import transcript_path
from sheepdog.sink.cli_host import NO_RESULT

from test_guardrails import GuardBase
from test_roster import msg

KEY = "202610100035"


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: float) -> datetime:
    return datetime.now().astimezone() - timedelta(minutes=minutes)


def tool(at: datetime, summary: str = "Do something", action: str = "Doing something", cmd: str = "",
         step: int = 10, thinking: str = "SECRET-THINKING") -> dict:
    name = "run_command" if cmd else "view_file"
    args = {}
    if summary is not None:
        args["toolSummary"] = json.dumps(summary)
    if action is not None:
        args["toolAction"] = json.dumps(action)
    if cmd:
        args["CommandLine"] = json.dumps(cmd)
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": utc(at),
            "thinking": thinking, "content": "SECRET-ANSWER", "tool_calls": [{"name": name, "args": args}]}


def error_line(at: datetime) -> dict:
    return {"step_index": 99, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "DONE", "created_at": utc(at),
            "content": "network error"}


class DutyBase(GuardBase):
    def card(self, tid):
        return self.store.get_card(tid)

    def state(self, tid):
        return self.card(tid)["run_state"]

    def set_mtime(self, cid, dt: datetime):
        p = transcript_path(cid)
        os.utime(p, (dt.timestamp(), dt.timestamp()))

    def sent_batch(self, tid, minutes_ago: float, running: bool = True, bid: str = "") -> str:
        """给会话记一个 minutes_ago 分钟前送出、还在等回执的批次。"""
        bid = bid or f"b_test_{tid}_{int(minutes_ago)}"
        self.store.record_dispatch(bid, tid, [])
        sent = ago(minutes_ago).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?", (sent, bid))
        self.store.conn.commit()
        if running:
            self.store.update_topic(tid, state=sm.RUNNING, pending_batch_id=bid, dispatched_at=sent)
        return bid

    def round(self, tids=(BUS_TOPIC_ID, "tp_alpha")):
        self.d._duty_round(list(tids))

    def cli(self, *argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue() + err.getvalue()


# ======================= 第 4 步：运行状态 =======================
class RunStateTest(DutyBase):
    def test_working_with_recent_action_and_tool_summary(self):
        self.append("conv_test_alpha", tool(ago(9), "Trigger agent-worker build", "Triggering build",
                                            cmd="echo build"))
        self.sent_batch("tp_alpha", 10)
        self.append("conv_test_alpha", tool(ago(3), "Trigger agent-worker build", "Triggering build", cmd="echo b2"))
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.WORKING)
        self.assertEqual(c["action"], "Trigger agent-worker build")
        self.assertFalse(c["spinning"])
        self.assertIn("送出", c["detail"])

    def test_action_falls_back_to_tool_action_and_never_reads_thinking(self):
        self.append("conv_test_alpha", tool(ago(2), summary=None, action="Reading the release skill. Then more"))
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["action"], "Reading the release skill")  # 截成一句
        rows = self.store.conn.execute("SELECT * FROM duty_cards").fetchall()
        dump = json.dumps([dict(r) for r in rows], ensure_ascii=False)
        self.assertNotIn("SECRET-THINKING", dump)
        self.assertNotIn("SECRET-ANSWER", dump)
        # 只有名字的工具调用：用工具名
        self.append("conv_test_alpha", tool(ago(1), summary=None, action=None))
        self.round()
        self.assertEqual(self.card("tp_alpha")["action"], "view_file")

    def test_codex_action_is_the_command(self):
        line = {"type": "event_msg", "timestamp": utc(ago(1)), "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "c1", "command": ["/bin/zsh", "-lc", "gh run watch 123 --token=abc"]}}}
        acts = guard.tool_actions("codex", line, 0)
        self.assertEqual([a.summary for a in acts], ["gh run watch 123 --token=abc"])
        self.assertEqual(guard.tool_actions("claude", {"type": "assistant"}, 0), [])

    def test_spinning_same_command_three_times(self):
        self.sent_batch("tp_alpha", 6)
        for i in range(3):
            self.append("conv_test_alpha", tool(ago(5 - i), "Wait for CI", "Waiting", cmd="sleep 60", step=20 + i))
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual((c["run_state"], c["spinning"], c["sig_repeat"]), (duty.WORKING, 1, 3))
        # 换了一条命令就不算空转
        self.append("conv_test_alpha", tool(ago(1), "Check status", "Checking", cmd="gh run view 1", step=30))
        self.round()
        self.assertEqual((self.card("tp_alpha")["spinning"], self.card("tp_alpha")["sig_repeat"]), (0, 1))

    def test_stalled_when_batch_untouched_for_15_minutes_queues_and_notifies_once(self):
        self.append("conv_test_alpha", tool(ago(40), "Check dashboards"))
        self.sent_batch("tp_alpha", 20)
        self.append("conv_test_alpha", error_line(ago(5)))  # 断网时只写 ERROR_MESSAGE：不算动作，但记录在动
        self.poll([msg(message_id="om_test_st1", chat_type="p2p", chat_id="oc_test_alpha_p2p")])
        before = len(self.sink.to("conv_test_alpha"))
        r = self.d.dispatch_once()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.STALLED)
        self.assertIn("20 分钟没有新动作", c["detail"])
        # adopted 会话回执超时（20 分钟）以后回到可投递，但在岗表判中断：新消息排队
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before)
        self.assertIn("中断", r["topics"]["tp_alpha"]["queued"])
        self.assertEqual(self.row("om_test_st1")["dispatch_state"], "pending")
        self.assertEqual(len(self.notices), 1)
        n = self.notices[0]
        self.assertIn("会话中断了 [tp_bus]", n)
        self.assertIn("topic `tp_alpha`", n)
        self.assertIn("最近一次工具调用「Check dashboards」", n)
        self.assertIn("排着 1 条信号", n)
        self.assertIn("FX-STUCK state=中断 queued=1 host=Antigravity topic=tp_alpha", n)
        # 会话动起来：恢复投递
        self.append("conv_test_alpha", tool(datetime.now().astimezone() + timedelta(seconds=2), "Back online"))
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before + 1)
        self.assertIn("om_test_st1", self.sink.to("conv_test_alpha")[-1])
        self.assertEqual(len(self.notices), 1)

    def test_lost_when_record_does_not_move_for_30_minutes(self):
        self.append("conv_test_alpha", tool(ago(50), "Check dashboards"))
        self.sent_batch("tp_alpha", 35)
        self.set_mtime("conv_test_alpha", ago(50))
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.LOST)
        self.assertIn("完全不动", c["detail"])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("会话失联了", self.notices[0])

    def test_stalled_after_acting_then_going_quiet_while_running(self):
        self.sent_batch("tp_alpha", 25)
        self.append("conv_test_alpha", tool(ago(18), "Run the long job"))
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.STALLED)
        self.assertIn("批次在跑，最近动作", c["detail"])

    def test_lost_when_background_process_vanished(self):
        bid = self.sent_batch("tp_alpha", 3)
        self.store.update_topic("tp_alpha", host="codex")
        self.store.add_run(bid, "tp_alpha", "codex", "conv_test_alpha", "resume", "", "", "")
        self.store.update_run(bid, state="failed", error=NO_RESULT, error_kind="other", ended_at=ago(1).isoformat())
        self.store.update_topic("tp_alpha", state=sm.FAILED, pending_batch_id=None)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.LOST)
        self.assertIn("后台进程没了", self.card("tp_alpha")["detail"])

    def test_failed_round_is_stalled_but_left_to_7_21(self):
        self.store.update_topic("tp_alpha", state=sm.FAILED, retries=1)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.STALLED)
        self.assertEqual(self.notices, [])  # 连续失败由 attention 通知
        self.store.update_topic("tp_alpha", state=sm.ATTENTION, attention_reason="host_failed")
        self.round()
        self.assertIn("attention", self.card("tp_alpha")["detail"])
        self.assertEqual(self.notices, [])

    def test_occupied_by_owner(self):
        self.store.update_topic("tp_alpha", state=sm.HUMAN_ATTACHED)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.OCCUPIED)
        self.store.update_topic("tp_alpha", state=sm.ACTIVE)
        self.append("conv_test_alpha", {"type": "USER_INPUT", "step_index": 40, "source": "USER_EXPLICIT",
                                        "created_at": utc(ago(2)), "content": "我来看一下"})
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.OCCUPIED)
        self.assertIn("说过话", self.card("tp_alpha")["detail"])

    def test_occupied_when_codex_app_holds_the_lock(self):
        class LockSink(type(self.sink)):
            def lock_streak_since(self, cid):
                return ago(10) if cid == "conv_test_alpha" else None
        self.d = self.make(LockSink())
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.OCCUPIED)
        self.assertIn("写锁冲突", self.card("tp_alpha")["detail"])

    def test_waiting_from_watch_autofill_and_from_receipt(self):
        with mock.patch.object(cli, "load_config", return_value=self.cfg), redirect_stdout(io.StringIO()):
            cli.main(["watch", "--topic", "tp_alpha", "--person", "ou_test_bob", "--note", f"等对方确认 {KEY}"])
        self.round()
        c = self.card("tp_alpha")
        self.assertEqual(c["run_state"], duty.WAITING)
        w = self.store.open_work("tp_alpha")[0]
        self.assertEqual((w["key"], w["state"], w["source"], w["waiting_on"]),
                         (KEY, duty.WAITING_OTHER, "watch", "ou_test_bob"))
        # 等待结束：自动补的活跟着收掉，变成空闲、附上一件
        self.store.close_watch(self.store.open_watches()[0]["id"], "done")
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.IDLE)
        self.assertIn(KEY, self.card("tp_alpha")["detail"])
        self.store.update_topic("tp_alpha", state=sm.BLOCKED, summary="等对方给日志")
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.WAITING)
        self.assertIn("等对方给日志", self.card("tp_alpha")["detail"])

    def test_declared_work_wins_over_watch(self):
        duty.set_work(self.store, "tp_alpha", KEY, title="自己申报的", state="doing")
        self.store.add_watch("tp_alpha", "ou_test_bob", "", f"等 {KEY}")
        self.round()
        w = self.store.open_work("tp_alpha")
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["state"], w[0]["title"], w[0]["waiting_on"]), (duty.DOING, "自己申报的", "ou_test_bob"))

    def test_escalation_autofill_waits_on_owner_and_closes_when_answered(self):
        self.store.add_escalation("om_test_esc1", "tp_alpha", "oc_test_esc", "🐕 需要你定 · 发版 [tp_alpha] PR #12 要不要合",
                                  datetime.now().astimezone().isoformat(timespec="seconds"))
        self.round()
        w = self.store.open_work("tp_alpha")[0]
        self.assertEqual((w["key"], w["state"], w["waiting_on"]), ("PR #12", duty.WAITING_OWNER, "主人"))
        self.store.answer_escalation("om_test_esc1", "om_test_ans")
        self.round()
        self.assertEqual(self.store.open_work("tp_alpha"), [])
        self.assertEqual(self.store.closed_work("tp_alpha")[0]["result"], "主人已答")

    def test_session_reset_clears_stall(self):
        self.append("conv_test_alpha", tool(ago(40), "Check dashboards"))
        self.sent_batch("tp_alpha", 20, running=False)
        self.store.update_topic("tp_alpha", state=sm.ACTIVE)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.STALLED)
        rc, out = self.cli("session-reset", "--topic", "tp_alpha")
        self.assertEqual(rc, 0)
        self.assertIn("清掉在岗表的中断", out)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.IDLE)

    def test_thresholds_from_config(self):
        self.cfg.duty = DutyConfig(stall_minutes=30, lost_minutes=60)
        self.append("conv_test_alpha", tool(ago(40), "Check dashboards"))
        self.sent_batch("tp_alpha", 20)
        self.round()
        self.assertEqual(self.state("tp_alpha"), duty.WORKING)
        with self.assertRaises(ConfigError):
            DutyConfig(stall_minutes=30, lost_minutes=10).validate()
        with self.assertRaises(ConfigError):
            DutyConfig(key_patterns=["("]).validate()


# ======================= 第 3 步：撞车 =======================
class CollisionTest(DutyBase):
    def test_reminder_attached_but_delivery_unchanged(self):
        self.append("conv_test_alpha", tool(ago(4), "Trigger agent-worker build"))
        self.round()
        duty.set_work(self.store, "tp_alpha", KEY, title="发版", state="doing")
        self.poll([msg(message_id="om_test_cl1", chat_type="p2p", chat_id="oc_test_stranger",
                       content=f"审批单{KEY}到哪了")])
        self.d.dispatch_once()
        text = self.sink.to("conv_test_bus")[-1]
        self.assertIn(f"🔀 撞车提醒：{KEY} 登记在「Alpha 需求」（topic `tp_alpha`）名下，进行中", text)
        self.assertIn("最近动作「Trigger agent-worker build」", text)
        self.assertIn(f"FX-COLLISION key={KEY} owner=tp_alpha state=进行中", text)
        self.assertLess(text.index("撞车提醒"), text.index(f"> 审批单{KEY}到哪了"))  # 提醒在正文之前
        self.assertEqual(self.row("om_test_cl1")["topic_id"], BUS_TOPIC_ID)  # 只提醒不拦

    def test_no_reminder_for_own_or_finished_work(self):
        duty.set_work(self.store, BUS_TOPIC_ID, KEY, state="doing")
        duty.set_work(self.store, "tp_alpha", "PR #7", state="done", result="合了")
        self.poll([msg(message_id="om_test_cl2", chat_type="p2p", chat_id="oc_test_stranger",
                       content=f"{KEY} 和 pr#7 怎么样了")])
        self.d.dispatch_once()
        self.assertNotIn("撞车提醒", self.sink.to("conv_test_bus")[-1])

    def test_key_patterns_and_normalization(self):
        pats = DutyConfig().patterns
        self.assertEqual(duty.extract_keys(f"单号{KEY}、PR #12、见ABC-34；1234567890123 不算", pats),
                         [KEY, "PR #12", "ABC-34"])
        self.assertEqual(duty.norm_key("pr # 12"), duty.norm_key("PR#12"))
        duty.set_work(self.store, "tp_alpha", "PR#12", state="doing")
        _, others = duty.set_work(self.store, BUS_TOPIC_ID, "pr #12", state="doing")
        self.assertEqual([o["topic_id"] for o in others], ["tp_alpha"])


# ======================= 第 6 步：总线摘要 =======================
class DigestTest(DutyBase):
    def test_digest_only_when_board_changes(self):
        duty.set_work(self.store, "tp_alpha", KEY, title="发版", state="doing")
        t1 = self.deliver_to(BUS_TOPIC_ID, "om_test_dg1")
        self.assertIn("FX-DIGEST count=1", t1)
        self.assertIn("- 空闲 1 个：Alpha 需求", t1)  # 空闲的只合成一行，不列它名下的活
        t2 = self.deliver_to(BUS_TOPIC_ID, "om_test_dg2")
        self.assertNotIn("FX-DIGEST", t2)  # 没变化不附
        duty.set_work(self.store, "tp_alpha", KEY, state="waiting_other", waiting_on="ou_test_bob")
        t3 = self.deliver_to(BUS_TOPIC_ID, "om_test_dg3")
        self.assertIn("FX-DIGEST", t3)
        self.assertIn(f"「Alpha 需求」tp_alpha · 等回复 · 活 {KEY}（等别人）", t3)
        self.assertNotIn("空闲", t3.split("FX-DIGEST")[1].split("处理完成后")[0])

    @staticmethod
    def fake_card(i, state=duty.IDLE, title="", action="", work=(), unread=0, spinning=False):
        return {"topic_id": f"tp_x{i:02d}", "title": title or f"x{i}", "run_state": state,
                "run_label": duty.RUN_LABEL.get(state, ""), "spinning": spinning, "detail": "卡了一会儿",
                "action": action, "action_ago": "3 分钟前" if action else "",
                "work": [{"key": k, "norm_key": duty.norm_key(k), "state": s} for k, s in work], "unread_mail": unread}

    def test_idle_cards_merge_into_one_line(self):
        cards = [self.fake_card(i) for i in range(20)]
        self.assertEqual(duty.digest_lines(cards, 15), ["- 空闲 20 个：" + "、".join(f"x{i}" for i in range(20))])
        # 标题太长截短；整行超过约 120 字用「等」收尾
        long = [self.fake_card(i, title=f"第{i}个会话的标题写得特别特别特别长") for i in range(12)]
        line = duty.idle_line(long)
        self.assertTrue(line.startswith("- 空闲 12 个：第0个会话的标题写得特别特别特别…、"), line)
        self.assertTrue(line.endswith("等"), line)
        self.assertLessEqual(len(line), duty.IDLE_LINE_LIMIT)
        self.assertNotIn("第11个", line)

    def test_digest_order_and_cap_keeps_stuck_first(self):
        cards = ([self.fake_card(i, duty.WORKING, action="Run build") for i in range(10)]
                 + [self.fake_card(10 + i, duty.STALLED) for i in range(3)]
                 + [self.fake_card(20 + i, duty.OCCUPIED) for i in range(2)]
                 + [self.fake_card(30 + i) for i in range(6)])
        full = duty.digest_lines(cards, 100)
        self.assertEqual(len(full), 16)  # 15 个不空闲的各一行 + 空闲一行
        self.assertEqual([ln.split("」")[1].split(" · ")[0] for ln in full[:5]],
                         ["tp_x20", "tp_x21", "tp_x10", "tp_x11", "tp_x12"])  # 显示按 RUN_ORDER：被占、中断……
        self.assertIn("此刻动作「Run build」3 分钟前", full[5])
        self.assertEqual(full[-1], "- 空闲 6 个：x30、x31、x32、x33、x34、x35")
        capped = duty.digest_lines(cards, 8)
        self.assertEqual(len(capped), 8)
        kept = " ".join(capped)
        for i in (10, 11, 12, 20, 21):
            self.assertIn(f"tp_x{i}", kept)  # 卡住的、被占的都留着
        self.assertEqual(capped[-1], "- …另有 14 个会话没列出（在做 8、空闲 6），`sheepdog board` 看全部")
        self.assertEqual(len(duty.digest_lines(cards, 1)), 1)

    def test_fingerprint_ignores_idle_details_and_actions(self):
        base = [self.fake_card(0, action="Read a file"), self.fake_card(1, duty.WORKING, action="Run build")]
        fp = duty.digest_fingerprint(base)
        # 空闲会话的最近动作、活变了：不算变化
        same = [self.fake_card(0, action="Something else", work=[("PR #1", duty.DOING)], unread=2),
                self.fake_card(1, duty.WORKING, action="Run tests")]
        self.assertEqual(duty.digest_fingerprint(same), fp)
        # 不空闲的：状态、活、空转、未读留言变了才算
        for changed in ([self.fake_card(0, duty.WAITING), base[1]],
                        [base[0], self.fake_card(1, duty.STALLED)],
                        [base[0], self.fake_card(1, duty.WORKING, work=[("PR #1", duty.DOING)])],
                        [base[0], self.fake_card(1, duty.WORKING, spinning=True)],
                        [base[0], self.fake_card(1, duty.WORKING, unread=1)],
                        [base[0], base[1], self.fake_card(2)]):
            self.assertNotEqual(duty.digest_fingerprint(changed), fp, changed)

    def test_action_label_now_only_when_working(self):
        self.assertEqual(duty.action_label(duty.WORKING), "此刻动作")
        for s in (duty.IDLE, duty.WAITING, duty.STALLED, duty.LOST, duty.OCCUPIED, ""):
            self.assertEqual(duty.action_label(s), "最近动作")
        self.append("conv_test_alpha", tool(ago(40), "Check dashboards"))
        self.round()
        rc, out = self.cli("board", "--topic", "tp_alpha")
        self.assertIn("  最近动作：「Check dashboards」（40 分钟前）", out)
        self.assertNotIn("此刻动作", out)
        rc, out = self.cli("board", "--json")
        card = {c["topic_id"]: c for c in json.loads(out)}["tp_alpha"]
        self.assertEqual((card["action"], card["action_label"]), ("Check dashboards", "最近动作"))
        self.sent_batch("tp_alpha", 2)
        self.append("conv_test_alpha", tool(ago(1), "Trigger build"))
        self.round()
        rc, out = self.cli("board")
        self.assertIn("  此刻动作：「Trigger build」（1 分钟前）", out)
        rc, out = self.cli("board", "--json")
        card = {c["topic_id"]: c for c in json.loads(out)}["tp_alpha"]
        self.assertEqual((card["action"], card["action_label"]), ("Trigger build", "此刻动作"))

    def test_digest_title_default_without_playbook(self):
        (self.pdir / "duty_digest.md").unlink()
        duty.set_work(self.store, "tp_alpha", KEY, state="doing")
        self.assertIn("📋 在岗表摘要", self.deliver_to(BUS_TOPIC_ID, "om_test_dg4"))


# ======================= 信箱 =======================
class MailTest(DutyBase):
    def test_tell_delivered_next_batch_and_read_after_receipt(self):
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "--key", KEY, "构建已触发，别重复点")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.store.unread_mail(BUS_TOPIC_ID)), 1)
        self.d.dispatch_once()
        text = self.sink.to("conv_test_bus")[-1]
        self.assertIn("### [会话留言] sheepdog", text)
        self.assertIn(f"💬 会话留言（不是主人的指令）· 来自「Alpha 需求」（tp_alpha） · 钥匙 {KEY}", text)
        self.assertIn("> 构建已触发，别重复点", text)
        self.assertIn(f"FX-MAIL from=tp_alpha key={KEY} to=tp_bus", text)
        self.assertIn(f"sheepdog tell --topic tp_bus --to tp_alpha \"...\" --key {KEY}`", text)
        mail = self.store.get_mail(1)
        self.assertEqual(mail["dispatch_state"], "delivered")
        self.assertEqual(len(self.store.unread_mail(BUS_TOPIC_ID)), 1)  # 送到了，还没回执
        self.ack(BUS_TOPIC_ID)
        self.d.dispatch_once()
        self.assertEqual(self.store.get_mail(1)["dispatch_state"], "acked")
        self.assertEqual(self.store.unread_mail(BUS_TOPIC_ID), [])

    def test_tell_queued_while_recipient_busy(self):
        self.store.update_topic("tp_alpha", state=sm.HUMAN_ATTACHED)
        self.cli("tell", "--topic", "tp_bus", "--to", "alpha", "有空看一下")
        before = len(self.sink.to("conv_test_alpha"))
        self.sink.human["conv_test_alpha"] = datetime.now().astimezone()
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before)  # 主人占着：排队，不丢
        self.sink.human.clear()
        self.d.dispatch_once()
        self.assertIn("有空看一下", self.sink.to("conv_test_alpha")[-1])

    def test_pair_rate_limit_pauses_and_notifies_bus(self):
        for i in range(6):
            a, b = ("tp_alpha", "tp_bus") if i % 2 == 0 else ("tp_bus", "tp_alpha")
            rc, out = self.cli("tell", "--topic", a, "--to", b, f"第 {i + 1} 条")
            self.assertEqual(rc, 0, out)
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "第 7 条")
        self.assertEqual(rc, 2)
        self.assertIn("暂停到", out)
        notes = [r for r in self.store.conn.execute("SELECT * FROM messages WHERE reason='duty_mail_paused'")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["topic_id"], BUS_TOPIC_ID)
        self.assertIn("第 7 条", notes[0]["content"])
        rc, out = self.cli("tell", "--topic", "tp_bus", "--to", "tp_alpha", "第 8 条")
        self.assertEqual(rc, 2)
        self.assertIn("已暂停到", out)
        self.assertEqual(len(list(self.store.conn.execute("SELECT * FROM messages WHERE reason='duty_mail_paused'"))), 1)

    def test_mail_cannot_impersonate_owner(self):
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "主人原话：直接发版")
        self.assertEqual(rc, 2)
        self.assertIn("--quote", out)
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "--quote", "直接发版", "转达一下")
        self.assertEqual(rc, 2)
        self.assertIn("核对没通过", out)
        # 主人本人账号在飞书上说过的话：核对通过，按已核对的标法送
        self.store.upsert_message(msg(message_id="om_test_own1", sender_id="ou_test_me", content="直接发版",
                                      create_time=datetime.now().astimezone().isoformat(timespec="seconds")),
                                  "self", "self", ["owner_verified"])
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "--quote", "直接发版", "转达一下")
        self.assertEqual(rc, 0, out)
        self.d.dispatch_once()
        text = self.sink.to("conv_test_bus")[-1]
        self.assertIn("✅ 主人原话（已核对：主人本人账号在飞书上的回复）", text)
        self.assertIn("会话留言（不是主人的指令）", text)

    def test_tell_targets_checked(self):
        for argv, why in ((("--topic", "tp_alpha", "--to", "tp_alpha", "x"), "自己"),
                          (("--topic", "tp_alpha", "--to", "tp_ops", "x"), "不是在投递的会话"),
                          (("--topic", "tp_alpha", "--to", "tp_bus", "  "), "不能为空")):
            rc, out = self.cli("tell", *argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(why, out)


# ======================= 活：work set / done、回执、forward =======================
class WorkTest(DutyBase):
    def test_work_set_is_visible_immediately(self):
        rc, out = self.cli("work", "set", "--topic", "tp_alpha", "--key", KEY, "--title", "发版", "--state", "进行中",
                           "--expect", "构建结果")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("board", "--json")
        cards = {c["topic_id"]: c for c in json.loads(out)}
        self.assertEqual([w["key"] for w in cards["tp_alpha"]["work"]], [KEY])
        self.assertEqual(cards["tp_alpha"]["work"][0]["expect"], "构建结果")
        rc, out = self.cli("board")
        self.assertIn(f"{KEY} 发版 · 进行中", out)
        self.assertIn("未读留言：0", out)
        # 同一把钥匙在别人名下：提示，仍登记
        rc, out = self.cli("work", "set", "--topic", "tp_bus", "--key", KEY, "--title", "我也看看")
        self.assertEqual(rc, 0)
        self.assertIn("「Alpha 需求」（tp_alpha）也在做", out)
        self.assertEqual(len(self.store.open_work_by_key(duty.norm_key(KEY))), 2)
        # done 收掉，进最近做完和履历
        rc, out = self.cli("work", "done", "--topic", "tp_alpha", "--key", KEY, "--result", "已发版")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("board", "--topic", "tp_alpha")
        self.assertIn("最近做完：", out)
        self.assertIn("结果：已发版", out)
        self.assertIn("已完成", self.store.history("tp_alpha", "work")[0]["text"])

    def test_work_validation(self):
        for argv, why in ((("set", "--topic", "tp_alpha", "--key", "k", "--state", "busy"), "--state"),
                          (("set", "--topic", "tp_alpha", "--key", "k", "--state", "handed_off"), "--to"),
                          (("set", "--topic", "tp_ops", "--key", "k"), "不是在投递的会话"),
                          (("done", "--topic", "tp_alpha", "--key", "nope", "--result", "x"), "没有钥匙")):
            rc, out = self.cli("work", *argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(why, out)

    def test_receipt_work_field(self):
        bid = self.sent_batch("tp_alpha", 1)
        write_receipt(self.cfg, "tp_alpha", bid, {"status": "handled", "summary": "开工", "work": [
            {"key": KEY, "title": "发版", "state": "doing", "expect": "回报结果"}, {"state": "doing"}]})
        self.d.dispatch_once()
        w = self.store.open_work("tp_alpha")
        self.assertEqual([(x["key"], x["expect"]) for x in w], [(KEY, "回报结果")])
        self.assertIn("开工", self.store.history("tp_alpha", "receipt")[0]["text"])

    def test_forward_marks_handed_off(self):
        self.poll([msg(message_id="om_test_fw1", chat_type="p2p", chat_id="oc_test_stranger",
                       content=f"帮忙看下 {KEY}")])
        forward_messages(self.store, "tp_alpha", ["om_test_fw1"])
        w = self.store.closed_work(BUS_TOPIC_ID)[0]
        self.assertEqual((w["key"], w["state"], w["handed_to"]), (KEY, duty.HANDED_OFF, "tp_alpha"))


# ======================= 履历清理 =======================
class PruneTest(DutyBase):
    def test_history_and_closed_work_pruned_open_work_kept(self):
        old = (datetime.now().astimezone() - timedelta(days=8)).isoformat(timespec="seconds")
        duty.set_work(self.store, "tp_alpha", "old-done", state="done", result="x")
        duty.set_work(self.store, "tp_alpha", "old-open", state="doing")
        self.store.add_history("tp_alpha", "action", "很久以前", at=old)
        self.store.add_history("tp_alpha", "action", "刚才")
        self.store.conn.execute("UPDATE work_items SET closed_at=?, created_at=?, updated_at=?", (None, old, old))
        self.store.conn.execute("UPDATE work_items SET closed_at=? WHERE key='old-done'", (old,))
        self.store.conn.commit()
        self.cli("tell", "--topic", "tp_alpha", "--to", "tp_bus", "旧留言")
        self.store.conn.execute("UPDATE mailbox SET created_at=?", (old,))
        self.store.conn.commit()
        out = self.store.prune(30, history_days=7)
        self.assertEqual((out["duty_history"], out["work_items"]), (1, 1))
        self.assertEqual(out["mailbox"], 0)  # 还没送出去的留言不删
        self.assertEqual([h["text"] for h in self.store.history("tp_alpha", "action")], ["刚才"])
        self.assertEqual([w["key"] for w in self.store.open_work("tp_alpha")], ["old-open"])

    def test_config_history_days_loaded(self):
        p = self.cfg.config_path = __import__("pathlib").Path(self.tmp.name) / "c.toml"
        p.write_text('[duty]\nhistory_days = 3\nstall_minutes = 10\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"SHEEPDOG_STATE_DIR": self.tmp.name}):
            cfg = load_config(p)
        self.assertEqual((cfg.duty.history_days, cfg.duty.stall_minutes, cfg.duty.lost_minutes), (3, 10, 30))


class SnapshotTest(DutyBase):
    def test_dry_run_snapshot_of_old_ledger_has_duty_tables(self):
        import sqlite3
        from sheepdog.store import Store
        old = __import__("pathlib").Path(self.tmp.name) / "old.sqlite3"
        c = sqlite3.connect(str(old))
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()
        c.close()
        mem = Store.snapshot(old)
        self.assertIsNone(mem.get_card("tp_x"))
        self.assertEqual(mem.open_work(), [])
        self.assertEqual(mem.unread_mail("tp_x"), [])
