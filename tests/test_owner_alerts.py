"""给主人的通知不能丢（F3）、合并窗口不能吞掉新的危险命令（F4）、压缩以后马上补规则（F5）。

起因是 2026-10-12 21:1x 正式环境的一次越权（北京时间）：
- 21:15:27 总线 grep「builds approve」命中命令审计，给主人的私聊报警遇到飞书限流（429），发送失败，只记了一行日志；
- 21:17:45 真正的 builds triggers run 和 builds approve 落在「同一会话 10 分钟内合并成一条」的窗口里，没有再单独报警，
  也没有再给总线排停手提示——最严重的命中被前面那条发送失败的轻命中吞掉了；
- 总线 21:02:53 被压缩，完整规则要等下一批才重推，它一直在跑 21:01 那一批，整个越权过程中没再收到规则。

真实的 Collector / Dispatcher / 账本 / playbook（duty_sim.World，模拟时钟一分钟一轮）。给主人的私聊换成假的飞书渠道
FakeLarkDM：记下每次调用（时间、防重编号、正文字节），按防重编号去重（同一个编号 1 小时内只发出一条，照飞书的规则），
可以排几次 429、一直 429、或者「发出去以后进程没了」。数据全是合成的。
"""

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, duty
from sheepdog import session as sm
from sheepdog.engine import (BUS_TOPIC_ID, NOTICE_GIVE_UP_HOURS, RECEIPT_TIMEOUT, compaction_resend_key,
                             unsent_notice_line)
from sheepdog.guard import COMPACTION
from sheepdog.notify import FileNotifier, OwnerDM
from sheepdog.prompts import RULES_UPDATE_HEADER
from sheepdog.source import SourceError
from sheepdog.source.lark import LarkCliSource
from sheepdog.store import now_iso

from duty_sim import World
from test_guardrails import CliSink, GuardBase

ALERT = "会话跑了危险命令"
GREP = 'grep -rn "builds approve" ~/notes/fx-release.md'
TRIGGER = "gcloud builds triggers run fx-bff-prod --sha=f00dfeed --region=asia-east1"
APPROVE = "gcloud beta builds approve fx-build-0001 --region=asia-east1"


def rate_limited() -> SourceError:
    """lark-cli 被飞书限流时 send_text 抛的错（格式照 _run_once）。"""
    return SourceError("lark-cli 调用失败 ['im', '+messages-send', '--user-id']: "
                       "{'type': 'api_error', 'subtype': 'rate_limit', 'message': 'TAT endpoint rate limited (HTTP 429)'}")


class SimCrash(BaseException):
    """进程在「发出去了、还没记成已发」之间没了。不是 Exception：engine 接不住，一路抛出 dispatch_once。"""


class FakeLarkDM:
    """假的「经机器人私聊主人」：send(text, idempotency_key)。飞书侧按防重编号去重：同一个编号 1 小时内只发出一条，
    重复的请求照样返回成功。fail：接下来几次调用依次抛的错；down：一直 429；crash_after_send：下一次发出去以后抛 SimCrash。"""

    def __init__(self, world: World):
        self.world = world
        self.calls: list[tuple] = []       # 每次调用：(时间, 防重编号, 正文字节)
        self.delivered: list[tuple] = []   # 主人真正收到的：(时间, 防重编号, 正文)
        self.fail: list[Exception] = []
        self.down = False
        self.crash_after_send = False

    def send(self, text: str, idempotency_key: str = "") -> None:
        now = self.world.clock.t
        self.calls.append((now, idempotency_key, text.encode("utf-8")))
        if self.fail:
            raise self.fail.pop(0)
        if self.down:
            raise rate_limited()
        dup = idempotency_key and any(k == idempotency_key and now - at < timedelta(hours=1)
                                      for at, k, _ in self.delivered)
        if not dup:
            self.delivered.append((now, idempotency_key, text))
        if self.crash_after_send:
            self.crash_after_send = False
            raise SimCrash()

    def alerts(self) -> list[str]:
        """主人收到的危险命令报警（正文）。"""
        return [t for _, _, t in self.delivered if ALERT in t]

    def alert_calls(self) -> list[tuple]:
        """发危险命令报警的每次调用。"""
        return [c for c in self.calls if ALERT.encode("utf-8") in c[2]]


class OwnerAlertBase(unittest.TestCase):
    keys = ("alpha",)

    def setUp(self):
        self.w = World(list(self.keys))
        self.addCleanup(self.w.close)
        self.ch = FakeLarkDM(self.w)
        self.use(self.ch)
        self.w.round()  # 第一次看到各会话的记录：审计、压缩检测的偏移从末尾开始
        self.t0 = self.w.now

    def tick(self, minutes: int = 1) -> None:
        """一分钟一轮。会话收到批次（比如停手提示）就动一下、交回执：场景里只看报警本身，不让在岗表判它中断、
        再冒出别的通知。"""
        for _ in range(minutes):
            self.w.round()
            for tid in (BUS_TOPIC_ID, *[f"tp_{k}" for k in self.keys]):
                t = self.w.topic(tid)
                if t["state"] == sm.RUNNING and t["pending_batch_id"]:
                    self.w[tid].act("Read the sheepdog hint")
                    self.w[tid].receipt()

    def use(self, notifier) -> None:
        """给主人的通知走这个渠道（重启以后也是它）。"""
        self.w.notifier = notifier
        self.w.d.notifier = notifier

    @property
    def bus(self):
        return self.w[BUS_TOPIC_ID]

    def notices(self, kind: str = "危险命令报警") -> list:
        return self.w.store.conn.execute("SELECT * FROM owner_notices WHERE kind=? ORDER BY id", (kind,)).fetchall()

    def stop_hints(self, tid: str = BUS_TOPIC_ID) -> list[str]:
        return [r["content"] for r in self.w.store.conn.execute(
            "SELECT content FROM messages WHERE topic_id=? AND reason='audit_offender' ORDER BY rowid", (tid,))]

    def cli(self, *argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.w.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue() + err.getvalue()


# ======================= F3：给主人的通知不能丢 =======================
class F3OwnerNoticeLedger(OwnerAlertBase):
    def offsets(self) -> list[float]:
        """每次发报警的时间（从 t0 起第几分钟）。"""
        return [(at - self.t0).total_seconds() / 60 for at, _, _ in self.ch.alert_calls()]

    def test_rate_limited_then_backoff_retry_sends_exactly_once(self):
        self.ch.fail = [rate_limited(), rate_limited()]
        self.bus.act(cmd=TRIGGER)
        self.tick()                                       # 第 1 分钟：报警落账本，第一次发 429
        [row] = self.notices()
        self.assertEqual((row["state"], row["attempts"]), ("pending", 1))
        self.assertIn("rate_limit", row["last_error"])
        self.assertEqual(self.ch.alerts(), [])
        self.tick()                                       # 第 2 分钟：退避 1 分钟后重试，又 429
        self.tick()                                       # 第 3 分钟：退避 2 分钟，还没到
        self.assertEqual(self.offsets(), [1, 2])
        self.tick()                                       # 第 4 分钟：发出去了
        self.tick(20)
        self.assertEqual(self.offsets(), [1, 2, 4])
        self.assertEqual(len(self.ch.alerts()), 1)        # 主人只收到一条
        # 每次都是同一个防重编号、同一份正文字节；编号不超过 lark-cli 的 50 个字符
        row = self.w.store.get_owner_notice(row["id"])
        self.assertEqual({k for _, k, _ in self.ch.alert_calls()}, {row["idem_key"]})
        self.assertEqual({b for _, _, b in self.ch.alert_calls()}, {bytes(row["body"])})
        self.assertLessEqual(len(row["idem_key"]), 50)
        self.assertEqual((row["state"], row["attempts"], row["inflight_at"]), ("sent", 3, None))
        self.assertIn(TRIGGER, self.ch.alerts()[0])
        self.assertEqual(len(self.stop_hints()), 1)       # 重试只重发给主人的那条，停手提示不重排
        self.assertEqual(unsent_notice_line(self.w.store), "")

    def test_gives_up_after_two_hours_shown_by_doctor_and_board_until_handled(self):
        self.ch.down = True
        self.bus.act(cmd=TRIGGER)
        self.tick(NOTICE_GIVE_UP_HOURS * 60)              # 第 1 到第 120 分钟：一直 429，还在重试
        [row] = self.notices()
        self.assertEqual(row["state"], "pending")
        # 退避 1、2、4、8 分钟，之后每次 10 分钟
        self.assertEqual(self.offsets(), [1, 2, 4, 8, 16, 26, 36, 46, 56, 66, 76, 86, 96, 106, 116])
        with self.assertLogs("sheepdog", "ERROR") as logs:
            self.tick()                                   # 第 121 分钟：落账本满 2 小时，标放弃，记 ERROR
        self.assertTrue(any("没发出去，放弃" in m and "rate_limit" in m for m in logs.output), logs.output)
        self.assertEqual(self.w.store.get_owner_notice(row["id"])["state"], "abandoned")
        self.tick(60)
        self.assertEqual(len(self.ch.alert_calls()), 15)  # 放弃以后不再发
        self.assertEqual(self.ch.delivered, [])
        # doctor、board 的开头都提示，直到人工处理
        warn = "⚠ 有 1 条给主人的通知没发出去（1 条已放弃）"
        _, doctor = self.cli("doctor")
        self.assertIn(warn, "\n".join(doctor.splitlines()[:2]))
        _, board = self.cli("board")
        self.assertTrue(board.startswith(warn), board[:200])
        _, listing = self.cli("notices")
        self.assertIn(f"#{row['id']} 放弃 · 危险命令报警 · tp_bus", listing)
        self.assertIn("rate_limit", listing)
        _, shown = self.cli("notices", "--show", str(row["id"]))
        self.assertIn(f"防重编号: {row['idem_key']}", shown)
        self.assertIn(bytes(row["body"]).decode(), shown)  # 正文原样
        # 人工 --retry：回到待发、2 小时重新算；飞书好了，下一轮发出去，提示消失
        self.ch.down = False
        rc, out = self.cli("notices", "--retry", "all")
        self.assertEqual(rc, 0, out)
        self.tick()
        self.assertEqual(len(self.ch.alerts()), 1)
        self.assertEqual(self.w.store.get_owner_notice(row["id"])["state"], "sent")
        self.assertEqual({k for _, k, _ in self.ch.alert_calls()}, {row["idem_key"]})  # 防重编号不变
        _, doctor = self.cli("doctor")
        self.assertNotIn("给主人的通知没发出去", doctor)
        self.assertFalse(self.cli("board")[1].startswith("⚠"))

    def test_ack_clears_the_warning_without_sending(self):
        self.ch.down = True
        self.bus.act(cmd=TRIGGER)
        self.tick(NOTICE_GIVE_UP_HOURS * 60 + 1)
        [row] = self.notices()
        self.assertEqual(row["state"], "abandoned")
        self.assertIn("1 条已放弃", self.cli("board")[1].splitlines()[0])
        rc, out = self.cli("notices", "--ack", str(row["id"]))
        self.assertEqual(rc, 0, out)
        self.ch.down = False
        self.tick(10)
        self.assertEqual(self.ch.alerts(), [])
        self.assertNotIn("没发出去", self.cli("board")[1])
        self.assertIn("都发出去了", self.cli("notices")[1])

    def test_crash_after_send_before_marked_sent_is_not_resent_after_restart(self):
        self.ch.crash_after_send = True
        self.bus.act(cmd=TRIGGER)
        with self.assertRaises(SimCrash):
            self.w.round()                                # 发出去了，还没记成已发，进程没了
        [row] = self.notices()
        self.assertEqual(row["state"], "pending")
        self.assertIsNotNone(row["inflight_at"])
        self.assertEqual(len(self.ch.alerts()), 1)
        self.w.restart()
        self.tick(30)                                     # 重启：用同一个防重编号补发，飞书去重
        self.assertEqual(len(self.ch.alert_calls()), 2)
        self.assertEqual({k for _, k, _ in self.ch.alert_calls()}, {row["idem_key"]})
        self.assertEqual(len(self.ch.alerts()), 1)        # 主人只收到一条
        self.assertEqual(len(self.notices()), 1)          # 没有为同一件事再拼一条新通知
        self.assertEqual(self.w.store.get_owner_notice(row["id"])["state"], "sent")
        self.assertEqual(len(self.stop_hints()), 1)       # 停手提示也只排了一条
        self.assertEqual(self.w.store.pending_audit_hits(), [])

    def test_crash_then_restart_past_dedup_window_is_not_resent_blindly(self):
        self.ch.crash_after_send = True
        self.bus.act(cmd=TRIGGER)
        with self.assertRaises(SimCrash):
            self.w.round()
        self.w.clock.advance(minutes=55)                  # 55 分钟以后才重启：飞书的防重窗口（1 小时）快过了
        self.w.restart()
        with self.assertLogs("sheepdog", "ERROR"):
            self.tick()
        [row] = self.notices()
        self.assertEqual(row["state"], "abandoned")
        self.assertIn("不知道发出去没有", row["last_error"])
        self.assertEqual(len(self.ch.alert_calls()), 1)   # 不敢补发（怕重复），等人看
        self.assertIn("1 条已放弃", self.cli("board")[1].splitlines()[0])

    def test_notify_file_mode_unchanged_no_ledger_no_retry(self):
        seen: list[str] = []

        class CountingFile(FileNotifier):
            broken = False

            def notify(self, text, what=""):
                seen.append(what)
                if self.broken:
                    raise OSError("SIM 写不进去")
                super().notify(text, what)

        n = CountingFile(self.w.cfg.notify_path, self.w.cfg.timezone_offset)
        self.use(n)
        self.bus.act(cmd=TRIGGER)
        self.tick()
        self.assertEqual([x["what"] for x in self.w.notices()], ["危险命令报警"])  # 照旧直接写进文件
        n.broken = True
        self.bus.act(cmd=APPROVE)
        with self.assertLogs("sheepdog", "WARNING") as logs:
            self.tick()                                   # 写不进去：只记日志
        self.assertTrue(any("给主人发危险命令报警失败" in m for m in logs.output))
        self.tick(30)
        self.assertEqual(seen, ["危险命令报警", "危险命令报警"])  # 不重试
        self.assertEqual(self.w.store.conn.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0], 0)
        self.assertEqual(unsent_notice_line(self.w.store), "")
        self.assertEqual(self.w.store.pending_audit_hits(), [])

    def test_duty_notice_goes_through_the_ledger_too(self):
        """在岗表的中断通知：第一次 429，下一轮补发；在岗表这一段不会因为没发出去再拼一条新的。"""
        self.ch.fail = [rate_limited()]
        self.w.message("alpha", "帮忙看下这个需求")
        self.w.run(17)                                    # 批次送出后 15 分钟没动：判中断（这里会话故意不动）
        self.w.run(10)
        rows = self.notices("在岗表通知")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["state"], rows[0]["attempts"]), ("sent", 2))
        self.assertEqual(len([t for _, _, t in self.ch.delivered if "会话中断了" in t]), 1)


class F3LarkCliArgs(unittest.TestCase):
    def test_send_text_passes_idempotency_key(self):
        src = LarkCliSource()
        seen = []
        with mock.patch.object(src, "_run_once", side_effect=lambda args, identity="user": seen.append((args, identity))):
            src.send_text("ou_test_owner", "SIM 正文", "bot", idempotency_key="sd-0123")
            src.send_text("ou_test_owner", "SIM 正文", "bot")
        self.assertEqual(seen[0], (["im", "+messages-send", "--user-id", "ou_test_owner", "--text", "SIM 正文",
                                    "--idempotency-key", "sd-0123"], "bot"))
        self.assertNotIn("--idempotency-key", seen[1][0])
        with self.assertRaises(SourceError):
            src.send_text("ou_test_owner", "x", "bot", idempotency_key="k" * 51)

    def test_owner_dm_sends_with_key(self):
        class Src:
            def __init__(self):
                self.sent = []

            def send_text(self, to, text, identity, idempotency_key=""):
                self.sent.append((to, text, identity, idempotency_key))

        s = Src()
        OwnerDM(s, "ou_test_owner").send("SIM", "sd-1")
        self.assertEqual(s.sent, [("ou_test_owner", "SIM", "bot", "sd-1")])


# ======================= F4：合并窗口不能吞掉新的危险命令 =======================
class F4AuditMerge(OwnerAlertBase):
    def test_incident_grep_then_trigger_and_approve_each_alert(self):
        """21:15 grep 命中、报警发送失败；21:17 触发和批准：必须单独再报，并给总线再排停手提示。"""
        self.ch.fail = [rate_limited(), rate_limited()]
        self.bus.act(cmd=GREP)
        self.tick()                                       # 第 1 分钟：第一条报警，429
        self.tick()                                       # 第 2 分钟：重试，又 429（下一次在第 4 分钟）
        self.assertEqual(self.ch.alerts(), [])
        self.bus.act(cmd=TRIGGER)
        self.bus.act(cmd=APPROVE)
        self.assertEqual(self.notices()[0]["state"], "pending")  # 第一条还在待发……
        self.tick()                                       # 第 3 分钟：……照样报第二条，两条命令合成一条
        alerts = self.ch.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn(TRIGGER, alerts[0])
        self.assertIn(APPROVE, alerts[0])
        self.assertNotIn("grep", alerts[0])
        self.assertIn("这是这个会话 10 分钟内第 2 次报警", alerts[0])
        hints = self.stop_hints()
        self.assertEqual(len(hints), 2)                   # 犯规会话再排一条停手提示
        self.assertIn(TRIGGER, hints[1])
        self.assertIn("第 2 次报警", hints[1])
        self.assertIn("FX-AUDIT-OFFENDER", hints[1])      # 措辞照旧走 playbook
        self.tick()                                       # 第 4 分钟：第一条也重试发出去了
        alerts = self.ch.alerts()
        self.assertEqual(len(alerts), 2)
        self.assertIn("grep -rn", alerts[1])
        self.assertIn("第 1 次报警", alerts[1])
        self.assertEqual([r["state"] for r in self.notices()], ["sent", "sent"])
        self.tick(20)
        self.assertEqual(len(self.ch.alerts()), 2)
        # 两条停手提示都真送到了总线手里
        texts = [d["content"] for d in self.w.sink.to(self.bus.cid)]
        self.assertTrue(any("第 1 次报警" in t and "grep -rn" in t for t in texts))
        self.assertTrue(any("第 2 次报警" in t and TRIGGER in t for t in texts))

    def test_same_command_repeated_alerts_once_per_window(self):
        self.bus.act(cmd=TRIGGER)
        self.tick()
        for _ in range(3):                                # 10 分钟内同一条命令又跑了三次
            self.tick(2)
            self.bus.act(cmd=TRIGGER)
        self.tick()
        self.assertEqual(len(self.ch.alerts()), 1)
        self.assertEqual(len(self.stop_hints()), 1)
        hits = self.w.store.conn.execute("SELECT * FROM audit_hits ORDER BY id").fetchall()
        self.assertEqual([h["merged"] for h in hits], [0, 1, 1, 1])
        self.assertEqual({h["alert_id"] for h in hits}, {hits[0]["id"]})
        self.tick(11)                                     # 上次报它已经过了 10 分钟：再跑就再报
        self.bus.act(cmd=TRIGGER)
        self.tick()
        self.assertEqual(len(self.ch.alerts()), 2)
        self.assertIn("第 1 次报警", self.ch.alerts()[1])
        self.assertEqual(len(self.stop_hints()), 2)

    def test_pending_first_alert_never_holds_back_new_ones(self):
        self.ch.down = True                               # 飞书一直 429
        for cmd in (GREP, TRIGGER, APPROVE):
            self.bus.act(cmd=cmd)
            self.tick()
        rows = self.notices()
        self.assertEqual(len(rows), 3)                    # 三条都落了账本，一条没被压住
        self.assertEqual([r["state"] for r in rows], ["pending"] * 3)
        self.assertEqual(len(self.stop_hints()), 3)
        self.assertIn("第 3 次报警", bytes(rows[2]["body"]).decode())
        self.ch.down = False
        self.tick(12)
        self.assertEqual(len(self.ch.alerts()), 3)
        self.assertEqual([r["state"] for r in self.notices()], ["sent"] * 3)


# ======================= F5：压缩以后马上补规则 =======================
class F5RulesAfterCompaction(OwnerAlertBase):
    def rules_messages(self) -> list[dict]:
        return [d for d in self.w.sink.to(self.bus.cid) if d["content"].startswith(f"[sheepdog] {RULES_UPDATE_HEADER}")]

    def start_batch(self) -> str:
        self.w.message("bus", "故障：SIM 服务报错")
        self.w.round()
        t = self.w.topic(BUS_TOPIC_ID)
        self.assertEqual(t["state"], sm.RUNNING)
        return t["pending_batch_id"]

    def test_compaction_mid_batch_resends_rules_in_the_same_round(self):
        bid = self.start_batch()
        before = self.w.topic(BUS_TOPIC_ID)
        n_dispatches = self.w.store.conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        self.w.run(2)
        self.bus.act("Read the incident")
        self.bus.compact()
        at = self.w.now + timedelta(minutes=1)
        with self.assertLogs("sheepdog", "INFO") as logs:
            self.w.round()                                # 同一轮就补发
        self.assertTrue(any("检测到压缩" in m and "已立即补发完整规则" in m for m in logs.output))
        [msg] = self.rules_messages()
        self.assertEqual(msg["at"], at)
        self.assertIsNone(msg["batch"])                   # 不是批次
        text = msg["content"]
        self.assertIn("认出你的上下文刚被压缩过", text.splitlines()[2])  # 开头写明为什么补发
        self.assertIn(f"手上的批次 `{bid}` 照常做完", text)
        self.assertIn("FX-COMPACTED", text)               # playbook 的 rules_after_compaction.md
        self.assertIn("FX-BUS", text)                     # 完整现行规则
        self.assertNotIn("新信号批次", text)
        # 批次记账不变：还在等 bid 的回执，送出时间不变，没有新的 dispatches 行；在岗表照旧是「在做 bid」
        t = self.w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["pending_batch_id"], t["dispatched_at"]),
                         (sm.RUNNING, bid, before["dispatched_at"]))
        self.assertEqual(self.w.store.conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], n_dispatches)
        self.assertEqual(self.w.run_state(BUS_TOPIC_ID), duty.WORKING)
        self.assertIn(bid, self.w.card(BUS_TOPIC_ID)["detail"])
        # 规则指纹记成最新：下一批不再重推
        self.assertEqual(t["rules_hash"], self.w.d.rules_hash(t))
        self.assertIsNone(t["compacted_at"])
        self.bus.receipt()
        self.w.message("bus", "故障：SIM 第二条")
        self.w.run(2)
        batches = [d for d in self.w.sink.to(self.bus.cid) if d["batch"]]
        self.assertEqual(len(batches), 2)
        self.assertFalse(batches[-1]["content"].startswith(RULES_UPDATE_HEADER))
        self.assertEqual(len(self.rules_messages()), 1)
        # 回执超时也照旧按 bid 算：bid 交了回执，是 acked
        self.assertEqual(self.w.store.get_dispatch(bid)["state"], "acked")

    def test_same_point_resent_once_even_after_restart_and_reread(self):
        self.start_batch()
        self.bus.compact()
        self.w.round()
        self.assertEqual(len(self.rules_messages()), 1)
        # 重启，而且压缩检测的偏移被重置（记录重读）：同一个压缩点认出来也不再补发
        self.w.restart()
        self.w.store.set_cursor(BUS_TOPIC_ID, COMPACTION, self.bus.cid, "", 0)
        with self.assertLogs("sheepdog", "INFO") as logs:
            self.w.round()
        self.assertTrue(any("这个压缩点已经补发过规则" in m for m in logs.output))
        self.assertEqual(len(self.rules_messages()), 1)
        self.assertTrue(json.loads(self.w.store.get_meta(compaction_resend_key(BUS_TOPIC_ID))))
        # 同一批还在跑，又压缩了一次（新的压缩点）：再补发一次
        self.w.run(3)
        self.bus.compact()
        self.w.round()
        self.assertEqual(len(self.rules_messages()), 2)

    def test_no_batch_in_hand_keeps_next_batch_push(self):
        self.bus.compact()
        self.w.round()
        self.assertEqual(self.rules_messages(), [])       # 手上没有批次：不单独发
        self.assertTrue(self.w.topic(BUS_TOPIC_ID)["compacted_at"])
        self.w.message("bus", "故障：SIM 服务报错")
        self.w.round()
        [batch] = [d for d in self.w.sink.to(self.bus.cid) if d["batch"]]
        self.assertTrue(batch["content"].startswith(RULES_UPDATE_HEADER))  # 照旧随下一批带上

    def test_unfinished_batch_after_receipt_timeout_also_gets_rules(self):
        """总线连续三次回执超时进了 attention，不再重投：它手上那一批还没交差（它其实在跑，只是没交回执），
        认出压缩也立即补发。"""
        self.start_batch()
        for _ in range(90):                               # 每批 20 分钟回执超时、退回重投，第三次进 attention
            self.w.round()
            if self.w.topic(BUS_TOPIC_ID)["state"] == sm.ATTENTION:
                break
        t = self.w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["pending_batch_id"]), (sm.ATTENTION, None))
        held = self.w.d._batch_in_hand(t)
        self.assertTrue(held.startswith("b"))
        self.assertEqual(self.w.store.get_dispatch(held)["state"], "failed")
        self.assertEqual(self.rules_messages(), [])
        self.bus.compact()
        self.w.round()
        [msg] = self.rules_messages()
        self.assertIn(f"手上的批次 `{held}` 照常做完", msg["content"])
        t = self.w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["pending_batch_id"]), (sm.ATTENTION, None))  # 状态不变


class F5CodexKeepsNextBatch(GuardBase):
    def test_codex_session_mid_run_is_not_sent_rules(self):
        rollout = Path(self.tmp.name) / "codex" / "rollout-2026-10-12T00-00-00-thr_test_f5.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("", encoding="utf-8")
        cli_sink = CliSink({"thr_test_f5": rollout})
        self.d = self.make({"antigravity": self.sink, "codex": cli_sink})
        self.store.create_topic("tp_cx", "Codex 会话", "dynamic", "fx", "active", "codex")
        self.store.update_topic("tp_cx", conversation_id="thr_test_f5", anchors_json='["oc_test_cx:all"]')
        self.d._read_from_start("tp_cx", "thr_test_f5")
        self.store.record_dispatch("b20261012000000-cx", "tp_cx", [])
        self.store.update_topic("tp_cx", state=sm.RUNNING, pending_batch_id="b20261012000000-cx",
                                dispatched_at=now_iso(),
                                rules_hash=self.d.rules_hash(self.store.get_topic("tp_cx")))
        with rollout.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-10-12T13:02:53Z", "type": "response_item",
                                "payload": {"type": "compaction", "encrypted_content": "fx"}}) + "\n")
        self.d.dispatch_once()
        t = self.store.get_topic("tp_cx")
        self.assertEqual(cli_sink.to("thr_test_f5"), [])  # Codex：一轮跑着送不进去，不单独补发
        self.assertTrue(t["compacted_at"])
        self.assertIsNone(t["rules_hash"])                # 照旧下一批带上完整规则
        self.assertEqual(t["pending_batch_id"], "b20261012000000-cx")

    def test_antigravity_receipt_timed_out_batch_counts_as_in_hand(self):
        self.store.record_dispatch("b20261012000000", BUS_TOPIC_ID, [])
        self.store.finish_dispatch("b20261012000000", "failed", error=RECEIPT_TIMEOUT)
        self.store.update_topic(BUS_TOPIC_ID, state=sm.ATTENTION, pending_batch_id=None,
                                attention_at="2026-10-12T21:00:00+08:00", attention_reason="receipt_timeout")
        self.assertEqual(self.d._batch_in_hand(self.store.get_topic(BUS_TOPIC_ID)), "b20261012000000")
        self.store.record_dispatch("b20261012000001", BUS_TOPIC_ID, [])
        self.store.finish_dispatch("b20261012000001", "acked")
        later = (datetime.now().astimezone() + timedelta(minutes=1)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id='b20261012000001'", (later,))
        self.store.conn.commit()
        self.assertEqual(self.d._batch_in_hand(self.store.get_topic(BUS_TOPIC_ID)), "")  # 之后交过回执：交差了


if __name__ == "__main__":
    unittest.main()
