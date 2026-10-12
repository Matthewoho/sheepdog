"""在岗表（7.20）和 attention 自动恢复（7.21）的端到端场景：真实的 Collector / Dispatcher / 账本 / playbook，
宿主是 DryRunSink，会话记录是合成的 Antigravity transcript，时间用模拟时钟一分钟一轮往前走（见 duty_sim.py）。

每个场景都检查三件事：每个会话这一轮收到了什么、给主人发了几条通知且内容对、卡片状态对。
阈值一律用生产默认值（中断 15 分钟、失联 30 分钟、主人占着 15 分钟、回执超时 20 分钟），只有 S9 为了截断改了摘要行数。
"""

import contextlib
import io
import json
import unittest
from datetime import timedelta
from unittest import mock

from sheepdog import cli, duty
from sheepdog import session as sm
from sheepdog.config import Config, DutyConfig, SessionConfig
from sheepdog.engine import BUS_TOPIC_ID, RECEIPT_TIMEOUT, forward_messages
from sheepdog.prompts import RULES_UPDATE_HEADER

from duty_sim import START, World

KEY1, KEY2, KEY3 = "202610100035", "PR #123", "ABC-123"


class Scenario(unittest.TestCase):
    keys = ("alpha", "beta")

    def setUp(self):
        self.w = World(list(self.keys))
        self.addCleanup(self.w.close)

    # ---------- 时间 ----------
    def to(self, minute: float) -> None:
        """一分钟一轮，跑到 START 之后第 minute 分钟（含这一轮）。"""
        end = START + timedelta(minutes=minute)
        while self.w.now < end:
            self.w.round(1)

    def hm(self, minute: float) -> str:
        return (START + timedelta(minutes=minute)).astimezone().strftime("%H:%M")

    # ---------- 观察 ----------
    def got(self, tid: str, at_minute: float | None = None) -> list[list[str]]:
        """这个会话收到的批次（每批里的消息编号）；at_minute 给了就只看那一轮。"""
        cid = self.w.sessions[tid].cid
        out = []
        for d in self.w.sink.to(cid):
            if at_minute is not None and d["at"] != START + timedelta(minutes=at_minute):
                continue
            out.append(json.loads(self.w.store.get_dispatch(d["batch"])["message_ids"] or "[]") if d["batch"] else [])
        return out

    def texts(self, tid: str) -> list[str]:
        return [d["content"] for d in self.w.sink.to(self.w.sessions[tid].cid)]

    def notes(self) -> list[tuple[str, str, str]]:
        """给主人的通知：(时间 HH:MM, 类型, 标题行)。"""
        return [(n["at"][11:16], n["what"], n["text"].splitlines()[0]) for n in self.w.notices()]

    def cli(self, *argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.w.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def assert_card(self, tid: str, run_state: str, detail_has: str = "") -> None:
        c = self.w.card(tid)
        self.assertIsNotNone(c, tid)
        self.assertEqual(c["run_state"], run_state, f"{tid} @ {self.w.now}: {c['detail']}")
        if detail_has:
            self.assertIn(detail_has, c["detail"])

    def assert_nothing_else_received(self, *tids: str) -> None:
        for tid in tids:
            self.assertEqual(self.got(tid), [], tid)


# ======================= S1 正常一批 =======================
class S1NormalBatch(Scenario):
    def test_deliver_act_receipt_idle_without_notices(self):
        w, a = self.w, self.w["alpha"]
        mid = w.message("alpha", "帮忙看下这个需求")
        self.to(1)
        self.assertEqual(self.got("tp_alpha", 1), [[mid]])
        self.assert_card("tp_alpha", duty.WORKING)
        self.assertEqual(w.topic("alpha")["state"], sm.RUNNING)
        # 会话有动作：在做，卡片的动作是「此刻动作」
        self.to(2)
        a.act("Read the requirement doc")
        self.to(3)
        c = duty.board_cards(w.store, w.cfg, topic="tp_alpha")[0]
        self.assertEqual((c["run_state"], c["action"], c["action_label"]),
                         (duty.WORKING, "Read the requirement doc", "此刻动作"))
        # 交回执（带 work 字段）→ 空闲
        a.act("Submit receipt", cmd="sheepdog receipt --topic tp_alpha ...")
        bid = a.receipt(summary="需求已回复", work={"key": "ABC-7", "title": "需求", "state": "done", "result": "已回复"})
        self.to(4)
        self.assert_card("tp_alpha", duty.IDLE, "ABC-7")
        self.assertEqual(w.topic("alpha")["state"], sm.ACTIVE)
        self.assertEqual(w.store.get_dispatch(bid)["state"], "acked")
        self.assertEqual(w.state(mid), "acked")
        c = duty.board_cards(w.store, w.cfg, topic="tp_alpha")[0]
        self.assertEqual(c["action_label"], "最近动作")
        # 之后一直空闲：谁也不再收到东西，没有通知
        self.to(60)
        self.assertEqual(self.got("tp_alpha"), [[mid]])
        self.assert_nothing_else_received(BUS_TOPIC_ID, "tp_beta")
        self.assert_card("tp_alpha", duty.IDLE)
        self.assert_card("tp_beta", duty.IDLE)
        self.assertEqual(self.notes(), [])

    def test_bus_batch_same_flow(self):
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        mid = w.message("bus", "你好，请问找谁")
        self.to(1)
        self.assertEqual(self.got(BUS_TOPIC_ID, 1), [[mid]])
        self.assertIn("FX-DIGEST", self.texts(BUS_TOPIC_ID)[0])  # 总线第一批带摘要
        bus.act("Route the question")
        bus.receipt()
        self.to(30)
        self.assert_card(BUS_TOPIC_ID, duty.IDLE)
        self.assertEqual(self.notes(), [])
        self.assertEqual(len(self.got(BUS_TOPIC_ID)), 1)


# ======================= S2 长命令 =======================
class S2LongCommand(Scenario):
    def test_adopted_long_command_stall_once_then_new_segment(self):
        w, a = self.w, self.w["alpha"]
        w.message("alpha", "跑一下发版脚本")
        self.to(1)
        a.act("Run release script", cmd="./release.sh --wait")  # 09:01 一个工具调用，之后不动
        self.to(15)  # 14 分钟：不判中断
        self.assert_card("tp_alpha", duty.WORKING)
        self.assertEqual(self.notes(), [])
        self.to(17)  # 16 分钟：中断，通知 1 次
        self.assert_card("tp_alpha", duty.STALLED, "批次在跑，最近动作 09:01")
        self.assertEqual(self.notes(), [("09:16", "在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]")])
        n = w.notices()[0]["text"]
        self.assertIn("「会话alpha」（topic `tp_alpha`，Antigravity）判为中断", n)
        self.assertIn("最近一次工具调用「Run release script」", n)
        # 会话有了新动作：回到在做，不重复通知中断，补一句「已恢复」
        a.act("Check release status", cmd="./release.sh --status")
        self.to(18)
        self.assert_card("tp_alpha", duty.WORKING)
        self.assertEqual(self.notes()[1:], [("09:18", "恢复通知", "🐕 汇报 · 会话已恢复 [tp_bus]")])
        self.assertIn("「会话alpha」（topic `tp_alpha`）10-12 09:17（北京时间）又有动作了", w.notices()[1]["text"])
        # 回执超时（09:01 送出，超过 20 分钟 → 09:22）：adopted 会话这一批按「没交回执」收掉，回到可投递
        # （会话动过这一批，算它处理过）
        self.to(21)
        self.assert_card("tp_alpha", duty.WORKING)
        self.to(22)
        self.assertEqual(w.store.recent_dispatches("tp_alpha")[0]["state"], "missed")
        self.assert_card("tp_alpha", duty.IDLE)
        # 新的一批：会话碰了一下又停 16 分钟 —— 算新的一次，再通知 1 次
        mid = w.message("alpha", "再跑一次")
        self.to(23)
        self.assertEqual(self.got("tp_alpha", 23), [[mid]])
        a.act("Rerun release", cmd="./release.sh --again")
        self.to(37)
        self.assert_card("tp_alpha", duty.WORKING)
        self.to(39)
        self.assert_card("tp_alpha", duty.STALLED, "最近动作 09:23")
        self.assertEqual([x[:2] for x in self.notes()],
                         [("09:16", "在岗表通知"), ("09:18", "恢复通知"), ("09:38", "在岗表通知")])
        self.assertEqual(len(self.got("tp_alpha")), 2)
        self.assert_nothing_else_received(BUS_TOPIC_ID, "tp_beta")

    def test_bus_long_command_redelivery_keeps_stall_and_does_not_renotify(self):
        """总线回执必交：20 分钟超时会重投同一批（7.21 的 failed 重投）。会话从 09:01 起就没动过，
        卡片一直是中断（不因为重投变回「在做」），也不因为重投再通知一次。"""
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        mid = w.message("bus", "跑一下长命令")
        self.to(1)
        bus.act("Run long job", cmd="sleep 3600")
        self.to(17)
        self.assert_card(BUS_TOPIC_ID, duty.STALLED, "批次在跑，最近动作 09:01")
        self.assertEqual(len(self.notes()), 1)
        self.to(22)  # 09:22 回执超时，重投
        self.assertEqual(self.got(BUS_TOPIC_ID, 22), [[mid]])
        self.assertEqual(w.topic(BUS_TOPIC_ID)["retries"], 1)
        for m in range(22, 41):
            self.to(m)
            self.assert_card(BUS_TOPIC_ID, duty.STALLED, "最近动作 09:01")
        self.assertEqual(len(self.notes()), 1)
        # 命令跑完、会话动起来：在做；之后再停 16 分钟：新的一段，再通知一次
        bus.act("Long job finished, reading output")
        self.to(41)
        self.assert_card(BUS_TOPIC_ID, duty.WORKING)
        self.to(57)
        self.assert_card(BUS_TOPIC_ID, duty.STALLED, "最近动作 09:40")
        self.assertEqual([x[:2] for x in self.notes()],
                         [("09:16", "在岗表通知"), ("09:41", "恢复通知"), ("09:55", "在岗表通知")])
        self.assertIn("排着 0 条信号。", w.notices()[0]["text"])  # 总线的中断通知不写「暂不投递」（会照常重投）
        self.assertNotIn("暂不投递", w.notices()[0]["text"])

    def test_adopted_silent_across_receipt_timeout_notifies_stall_and_lost_once_each(self):
        """会话 09:01 碰了一下就再也不动（App 也死了，记录不再写）。20 分钟回执超时以后排队的消息会照现在的规则
        投出去（adopted 不交回执也算处理过），但这仍是同一段卡住：中断只通知一次、失联一次。"""
        w, a = self.w, self.w["alpha"]
        w.message("alpha", "第一件")
        self.to(1)
        a.act("Start the job", cmd="./job.sh")
        a.app_alive = False
        m2 = w.message("alpha", "第二件")
        self.to(17)
        self.assertEqual(self.notes(), [("09:16", "在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]")])
        self.to(22)  # 回执超时：adopted 收掉这一批，第二件送出去
        self.assertEqual(self.got("tp_alpha", 22), [[m2]])
        self.to(60)
        self.assert_card("tp_alpha", duty.LOST)
        self.assertEqual([x[1:] for x in self.notes()],
                         [("在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]"), ("在岗表通知", "🐕 汇报 · 会话失联了 [tp_bus]")])


# ======================= S3 没碰批次 =======================
class S3UntouchedBatch(Scenario):
    def test_untouched_stall_then_lost_queue_then_deliver_once(self):
        w, a = self.w, self.w["alpha"]
        a.app_alive = False  # 会话记录完全不动（连投递进来的那一行都没写）
        m1 = w.message("alpha", "第一件")
        self.to(1)
        self.assertEqual(self.got("tp_alpha", 1), [[m1]])
        m2 = w.message("alpha", "第二件")
        self.to(15)
        self.assert_card("tp_alpha", duty.WORKING)
        self.to(16)  # 15 分钟：中断
        self.assert_card("tp_alpha", duty.STALLED, "批次 09:01 送出后 15 分钟没有新动作")
        self.assertEqual(self.notes(), [("09:16", "在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]")])
        self.assertIn("排着 1 条信号", w.notices()[0]["text"])
        m3 = w.message("alpha", "第三件")
        self.to(25)  # 回执超时以后会话状态回到 active，但在岗表判中断：排队
        self.assertEqual(w.topic("alpha")["state"], sm.ACTIVE)
        self.assertIn("在岗表判为中断", w.last_result["topics"]["tp_alpha"]["queued"])
        m4 = w.message("alpha", "第四件")
        self.to(30)
        self.assert_card("tp_alpha", duty.STALLED)
        self.assertEqual(w.card("alpha")["queued"], 3)
        self.assertEqual(w.store.count_pending_signals("tp_alpha"), 3)
        self.to(31)  # 30 分钟：失联
        self.assert_card("tp_alpha", duty.LOST, "会话记录 30 分钟完全不动")
        self.assertEqual([x[:2] for x in self.notes()], [("09:16", "在岗表通知"), ("09:31", "在岗表通知")])
        self.assertIn("会话失联了", self.notes()[1][2])
        self.assertIn("排着 3 条信号", w.notices()[1]["text"])
        self.to(45)
        self.assertEqual(self.got("tp_alpha"), [[m1]])  # 一直没再投
        # 会话一动：补送排队的，每条只送一次
        a.app_alive = True
        a.act("Wake up and read backlog")
        self.to(46)
        self.assertEqual(self.got("tp_alpha"), [[m1], [m2, m3, m4]])
        self.assert_card("tp_alpha", duty.WORKING)
        a.act("Handle backlog")
        a.receipt()
        self.to(90)
        self.assertEqual(self.got("tp_alpha"), [[m1], [m2, m3, m4]])
        self.assertEqual([x[:2] for x in self.notes()],
                         [("09:16", "在岗表通知"), ("09:31", "在岗表通知"), ("09:46", "恢复通知")])
        self.assertIn("排着的 3 条信号接着补送", w.notices()[2]["text"])
        self.assert_nothing_else_received(BUS_TOPIC_ID, "tp_beta")


# ======================= S4 断网 =======================
class S4NetworkDown(Scenario):
    def outage(self) -> dict:
        """总线 09:02 起断网：收到批次只写 SYSTEM_MESSAGE 和 ERROR_MESSAGE。返回消息编号。"""
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        m0 = w.message("bus", "断网前")
        self.to(1)
        bus.act("Handle message")
        bus.receipt()
        self.to(2)
        bus.net_down = True
        last_act = w.card(BUS_TOPIC_ID)["last_act_at"]
        ids = {"m0": m0, "m1": w.message("bus", "断网后第 1 条")}
        self.to(3)
        self.assertEqual(self.got(BUS_TOPIC_ID, 3), [[ids["m1"]]])
        ids["m2"] = w.message("bus", "断网中 2")
        self.to(18)
        self.assert_card(BUS_TOPIC_ID, duty.STALLED, "批次 09:03 送出后 15 分钟没有新动作")
        self.to(24)  # 09:24 第一次回执超时：failed，同一轮重投（在岗表不扣 failed）
        self.assertEqual(self.got(BUS_TOPIC_ID, 24), [[ids["m1"], ids["m2"]]])
        ids["m3"] = w.message("bus", "断网中 3")
        self.to(45)  # 第二次超时：再重投
        self.assertEqual(self.got(BUS_TOPIC_ID, 45), [[ids["m1"], ids["m2"], ids["m3"]]])
        self.to(66)  # 第三次超时：attention，全部排队
        t = w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["attention_reason"], t["retries"]), (sm.ATTENTION, "receipt_timeout", 3))
        ids["m4"] = w.message("bus", "断网中 4")
        ids["m5"] = w.message("bus", "断网中 5")
        self.to(80)
        self.assertEqual(len(self.got(BUS_TOPIC_ID)), 4)  # 09:01、09:03、09:24、09:45，之后没再投
        self.assertEqual(w.store.count_pending_signals(BUS_TOPIC_ID), 5)
        self.assertEqual(w.card(BUS_TOPIC_ID)["last_act_at"], last_act)  # ERROR / SYSTEM 不算动作
        self.assertEqual(self.notes(), [("09:18", "在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]"),
                                        ("10:06", "attention 通知", "🐕 汇报 · 会话仍卡着 [tp_bus]")])
        first, second = w.notices()[0]["text"], w.notices()[1]["text"]
        self.assertNotIn("暂不投递", first)  # 总线回执超时照常重投
        # 在岗表已经说过中断：attention 只补一句「仍卡着，已停止重投」
        self.assertEqual(len(second.splitlines()), 2)
        self.assertIn("仍卡着，已停止重投：回执超时（累计失败 3 次），排着 3 条信号；它一交回执或会话记录又有动作，"
                      "就自动恢复、补送。", second)
        return ids

    def test_outage_then_late_receipt(self):
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        ids = self.outage()
        # 网络回来，总线把 09:45 那批处理完、交了晚到的回执
        bus.net_down = False
        last = self.w.store.recent_dispatches(BUS_TOPIC_ID)[0]["batch_id"]
        bus.act("Back online, handle the batch")
        bus.receipt(batch=last, summary="断网期间的三条都处理了")
        self.to(81)
        self.assertEqual(w.store.get_dispatch(last)["state"], "acked_late")
        # 那一批里的 m1–m3 已处理：不再重投；之后排队的 m4、m5 补送一次
        self.assertEqual(self.got(BUS_TOPIC_ID, 81), [[ids["m4"], ids["m5"]]])
        for k in ("m1", "m2", "m3"):
            self.assertEqual(w.state(ids[k]), "acked", k)
        self.assertEqual(w.topic(BUS_TOPIC_ID)["state"], sm.RUNNING)
        self.assertEqual(self.notes()[2:], [("10:21", "恢复通知", "🐕 汇报 · 会话已恢复 [tp_bus]")])
        self.assertIn("补送 2 条", w.notices()[2]["text"])
        self.assertIn("晚到的回执", w.notices()[2]["text"])
        bus.act("Handle m4 m5")
        bus.receipt()
        self.to(140)
        self.assertEqual(len(self.notes()), 3)  # 整个过程：中断、仍卡着、已恢复，各一条，最后一条是「已恢复」
        self.assertEqual(self.notes()[-1][1], "恢复通知")
        delivered = [m for batch in self.got(BUS_TOPIC_ID) for m in batch]
        self.assertEqual(delivered.count(ids["m4"]), 1)
        self.assertEqual(delivered.count(ids["m5"]), 1)
        self.assert_nothing_else_received("tp_alpha", "tp_beta")

    def test_outage_then_new_action(self):
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        ids = self.outage()
        bus.net_down = False
        bus.act("Back online, read backlog")  # 没交晚到回执，只是记录里又有动作
        self.to(81)
        # 09:45 那批没交回执：会话没处理过，排队的全部补送一次
        self.assertEqual(self.got(BUS_TOPIC_ID, 81), [[ids[k] for k in ("m1", "m2", "m3", "m4", "m5")]])
        self.assertEqual(self.notes()[2], ("10:21", "恢复通知", "🐕 汇报 · 会话已恢复 [tp_bus]"))
        self.assertIn("补送 5 条", w.notices()[2]["text"])
        self.assertIn("又有新动作", w.notices()[2]["text"])
        bus.act("Handle backlog")
        bus.receipt()
        self.to(140)
        self.assertEqual(len(self.notes()), 3)
        self.assert_card(BUS_TOPIC_ID, duty.IDLE)


    def test_outage_with_app_frozen_upper_bound_is_four(self):
        """断网时 App 连投递进来的那一行都不写（记录完全不动）：多一条「失联」。整个过程的上限是 4 条：
        中断、失联（在岗表，同一段各一次）、attention、恢复（7.21，各一次）；重投、回执超时都不再多发。"""
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        w.message("bus", "断网前")
        self.to(1)
        bus.act("Handle message")
        bus.receipt()
        self.to(2)
        bus.app_alive = False
        mids = [w.message("bus", "断网后第 1 条")]
        for m in (10, 30, 50, 70, 90):
            self.to(m)
            mids.append(w.message("bus", f"断网中 {m}"))
        self.to(120)
        self.assertEqual(self.notes(), [("09:18", "在岗表通知", "🐕 汇报 · 会话中断了 [tp_bus]"),
                                        ("09:33", "在岗表通知", "🐕 汇报 · 会话失联了 [tp_bus]"),
                                        ("10:06", "attention 通知", "🐕 汇报 · 会话仍卡着 [tp_bus]")])
        bus.app_alive = True
        bus.act("Back online")
        self.to(121)
        self.assertEqual(self.notes()[3], ("11:01", "恢复通知", "🐕 汇报 · 会话已恢复 [tp_bus]"))
        self.assertEqual(self.got(BUS_TOPIC_ID, 121), [mids])
        bus.act("Handle backlog")
        bus.receipt()
        self.to(240)
        self.assertEqual(len(self.notes()), 4)
        self.assertEqual(self.notes()[-1][1], "恢复通知")


# ======================= S5 被主人占着 =======================
class S5OwnerOccupied(Scenario):
    def test_owner_talking_holds_delivery_without_stall_then_resumes(self):
        w, a = self.w, self.w["alpha"]
        self.to(4)
        a.owner_says("我先自己看一下这个会话")
        a.answer_only()  # 模型回了主人一句
        self.to(5)
        self.assert_card("tp_alpha", duty.OCCUPIED, "说过话")
        mid = w.message("alpha", "同事的新问题")
        for m in range(6, 19):  # 09:04 说的话，15 分钟内一直占着：不投、不判中断
            self.to(m)
            self.assertEqual(self.got("tp_alpha", m), [])
            self.assert_card("tp_alpha", duty.OCCUPIED)
        self.assertEqual(w.topic("alpha")["state"], sm.HUMAN_ATTACHED)
        self.assertEqual(w.card("alpha")["queued"], 1)
        self.to(19)  # 09:04 + 15 分钟 = 09:19 起不算占着，这一轮恢复投递
        self.assertEqual(self.got("tp_alpha"), [[mid]])
        self.assert_card("tp_alpha", duty.WORKING)
        self.assertEqual(self.notes(), [])

    def test_owner_talking_during_running_batch_is_not_a_stall(self):
        w, a = self.w, self.w["alpha"]
        w.message("alpha", "第一件")
        self.to(1)
        self.to(10)
        a.owner_says("这一批先别动，我来")
        a.answer_only()
        for m in range(11, 25):
            self.to(m)
            self.assert_card("tp_alpha", duty.OCCUPIED)
        self.assertEqual(self.notes(), [])


    def test_owner_visits_a_stuck_session_then_leaves(self):
        """会话卡住（没碰批次、App 也不写记录）以后主人进去说了几句（模型没回）又走了：
        - 主人在的 15 分钟里卡片是「被主人占着」；
        - 主人走的那一轮，卡片按新状态重算，仍是中断，排队的消息不投进去（长跑发现：以前这一轮拿的是开头算的
          「被主人占着」卡片，会把消息投进卡住的会话）；
        - 主人说话让记录文件动了一下，卡片从失联回到中断、之后再到失联：同一段卡住，不再通知。"""
        w, a = self.w, self.w["alpha"]
        a.app_alive = False
        m1 = w.message("alpha", "第一件")
        self.to(1)
        m2 = w.message("alpha", "第二件")
        self.to(31)
        self.assert_card("tp_alpha", duty.LOST)
        self.assertEqual([x[2] for x in self.notes()], ["🐕 汇报 · 会话中断了 [tp_bus]", "🐕 汇报 · 会话失联了 [tp_bus]"])
        self.to(40)
        a.owner_says("怎么不动了？")  # 模型卡着，没回
        for m in range(41, 55):
            self.to(m)
            self.assert_card("tp_alpha", duty.OCCUPIED)
        self.to(55)  # 09:40 + 15 分钟：主人走了
        self.assertEqual(w.topic("alpha")["state"], sm.ACTIVE)
        self.assert_card("tp_alpha", duty.STALLED)
        self.assertEqual(self.got("tp_alpha"), [[m1]])
        self.assertEqual(w.state(m2), "pending")
        self.to(75)
        self.assert_card("tp_alpha", duty.LOST)  # 09:40 之后记录又 30 分钟不动
        self.assertEqual(len(self.notes()), 2)
        self.assertEqual(self.got("tp_alpha"), [[m1]])


# ======================= S6 failed 照常重投 =======================
class S6FailedRedelivered(Scenario):
    def test_receipt_timeout_failed_is_redelivered_even_when_card_says_stalled(self):
        w = self.w
        mid = w.message("bus", "总线的一条")
        self.to(1)
        self.to(16)
        self.assert_card(BUS_TOPIC_ID, duty.STALLED)
        self.to(22)  # 回执超时 → failed：卡片判中断也照样重投
        self.assertEqual(self.got(BUS_TOPIC_ID), [[mid], [mid]])
        first = w.store.recent_dispatches(BUS_TOPIC_ID)[1]
        self.assertEqual((first["state"], first["error"]), ("failed", RECEIPT_TIMEOUT))

    def test_send_failure_after_receipt_timeout_goes_attention_and_needs_reset(self):
        """总线回执超时成了 failed 之后 agentapi 一直发不出去：失败满 3 次进 attention（投递失败，不自动恢复），
        私聊主人一次；之后会话记录有动作也不解，session-reset 以后补送。"""
        w = self.w
        mid = w.message("bus", "发不出去的一条")
        self.to(1)
        w.sink.fail.add("conv_test_bus")
        self.to(22)  # 回执超时 → failed（第 1 次），同一轮重投 → 发送失败（第 2 次）
        t = w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["retries"]), (sm.FAILED, 2))
        self.assertEqual(w.state(mid), "pending")
        self.to(23)  # 第 3 次：attention
        t = w.topic(BUS_TOPIC_ID)
        self.assertEqual((t["state"], t["attention_reason"]), (sm.ATTENTION, "send_failed"))
        # 09:16 在岗表已经说过中断（同一段）：attention 只补一句「仍卡着」，写明要手动复位
        self.assertEqual(self.notes()[-1], ("09:23", "attention 通知", "🐕 汇报 · 会话仍卡着 [tp_bus]"))
        self.assertIn("session-reset --topic tp_bus", w.notices()[-1]["text"])
        n = len(self.notes())
        w.sink.fail.clear()
        w[BUS_TOPIC_ID].act("Something")
        self.to(30)
        self.assertEqual(self.got(BUS_TOPIC_ID), [[mid]])  # 投递失败的 attention 不自动解
        rc, out = self.cli("session-reset", "--topic", BUS_TOPIC_ID)
        self.assertEqual(rc, 0, out)
        self.to(31)
        self.assertEqual(self.got(BUS_TOPIC_ID), [[mid], [mid]])
        self.assertEqual(len(self.notes()), n)

    def test_send_failure_on_idle_session_notifies_once_then_recovered(self):
        """会话空闲时 agentapi 发不出去（状态机不允许 active → failed，不进 attention）：消息照常排队、每轮重试；
        连续 3 次失败私聊主人一次「投递不出去」，之后发出去了再说一句「投递已恢复」。同一段失败只说一次。"""
        w = self.w
        w.sink.fail.add("conv_test_alpha")
        mid = w.message("alpha", "发不出去的一条")
        self.to(2)
        self.assertEqual(self.notes(), [])
        self.to(3)  # 第 3 次失败
        self.assertEqual(self.notes(), [("09:03", "投递失败通知", "🐕 汇报 · 投递不出去 [tp_bus]")])
        text = w.notices()[0]["text"]
        self.assertIn("「会话alpha」（topic `tp_alpha`，Antigravity）从 10-12 09:01（北京时间）起连续 3 次投递失败", text)
        self.assertIn("agentapi send-message 失败（模拟）", text)
        self.assertIn("消息照常排队、一条不丢", text)
        self.to(10)
        t = w.topic("alpha")
        self.assertEqual((t["state"], t["retries"]), (sm.ACTIVE, 10))  # 不扣消息、不改状态
        self.assertEqual(w.sink.sends, 10)
        self.assertEqual(w.state(mid), "pending")
        self.assertEqual(len(self.notes()), 1)
        w.sink.fail.clear()
        self.to(11)
        self.assertEqual(self.got("tp_alpha"), [[mid]])
        self.assertEqual(self.notes()[1], ("09:11", "投递恢复通知", "🐕 汇报 · 投递已恢复 [tp_bus]"))
        self.assertIn("之前连续失败 10 次），这一批送出 1 条", w.notices()[1]["text"])
        # 新的一段失败：再说一次
        w["alpha"].act("Handle")
        w["alpha"].receipt()
        w.sink.fail.add("conv_test_alpha")
        w.message("alpha", "又发不出去")
        self.to(15)
        self.assertEqual([x[1] for x in self.notes()], ["投递失败通知", "投递恢复通知", "投递失败通知"])


# ======================= S7 撞车提醒 =======================
class S7Collision(Scenario):
    def test_collision_notes_from_routes_and_declared_work(self):
        w = self.w
        for key, title in ((KEY1, "审批单发版"), (KEY2, "合 PR"), (KEY3, "Linear 单")):
            rc, out = self.cli("work", "set", "--topic", "tp_alpha", "--key", key, "--title", title)
            self.assertEqual(rc, 0, out)
        m1 = w.message("bus", f"审批单 {KEY1} 到哪了？顺便问下 pr#123 和 {KEY3}")
        m2 = w.message("beta", f"{KEY3} 这单你也看看")
        m3 = w.message("alpha", f"{KEY1} 怎么样了")
        self.to(1)
        # 送给谁不变：总线、beta、alpha 各收到自己的
        self.assertEqual((self.got(BUS_TOPIC_ID, 1), self.got("tp_beta", 1), self.got("tp_alpha", 1)),
                         ([[m1]], [[m2]], [[m3]]))
        bus_text = self.texts(BUS_TOPIC_ID)[-1]
        for key in (KEY1, "pr#123", KEY3):  # 这一轮还没有路由事实：只有 alpha 自报的活，措辞写明「会话自报」
            self.assertIn(f"🔀 撞车提醒：{key} 在「会话alpha」（topic `tp_alpha`）手上；会话自报：进行中", bus_text)
        self.assertIn(f"FX-COLLISION key={KEY3} owner=tp_alpha state=进行中 run=空闲 to=tp_beta basis=会话自报",
                      self.texts("tp_beta")[-1])
        # alpha 自己手上的（自报的 KEY1）不提醒；但同一轮总线刚收到过提到 KEY1 的消息——这是事实，照样提醒
        alpha_text = self.texts("tp_alpha")[-1]
        self.assertEqual(alpha_text.count("撞车提醒"), 1)
        self.assertIn(f"🔀 撞车提醒：{KEY1} 的消息最近送到了「Lark 信号·总线」（topic `tp_bus`）：最近一次 刚刚，共 1 条",
                      alpha_text)
        self.assertIn("basis=路由", alpha_text)
        for tid in (BUS_TOPIC_ID, "tp_alpha", "tp_beta"):
            w[tid].act("Handle")
            w[tid].receipt()
        # 自报完成 / 转交以后不再提醒（同一编号送到 alpha 的路由事实也跟着收掉）；没结束的照旧提醒
        self.assertEqual(self.cli("work", "done", "--topic", "tp_alpha", "--key", KEY1, "--result", "发了")[0], 0)
        self.assertEqual(self.cli("work", "set", "--topic", "tp_alpha", "--key", KEY2, "--state", "handed_off",
                                  "--to", "beta")[0], 0)
        m4 = w.message("bus", f"{KEY1}、PR #123、{KEY3} 都怎样了")
        self.to(2)  # 总线这一轮先收回执、回到空闲，再投 m4
        self.assertEqual(self.got(BUS_TOPIC_ID, 2), [[m4]])
        text = self.texts(BUS_TOPIC_ID)[-1]
        self.assertNotIn(f"撞车提醒：{KEY1}", text)
        self.assertNotIn(f"撞车提醒：{KEY2}", text)
        # KEY3：alpha 自报在做，beta 那边是事实（上一轮送到它的消息提到了 KEY3）
        self.assertIn(f"🔀 撞车提醒：{KEY3} 在「会话alpha」（topic `tp_alpha`）手上；会话自报：进行中", text)
        self.assertIn(f"🔀 撞车提醒：{KEY3} 的消息最近送到了「会话beta」（topic `tp_beta`）：最近一次 1 分钟前，共 1 条；"
                      "它现在空闲", text)
        self.assertEqual(self.notes(), [])


# ======================= S8 留言 =======================
class S8Mail(Scenario):
    def test_tell_delivered_next_batch_and_not_lost_while_queued(self):
        w = self.w
        rc, out = self.cli("tell", "--topic", "tp_beta", "--to", "tp_alpha", "--key", KEY1, "构建我来触发，别重复点")
        self.assertEqual(rc, 0, out)
        self.to(1)
        self.assertEqual(len(self.got("tp_alpha", 1)), 1)
        text = self.texts("tp_alpha")[-1]
        self.assertIn("💬 会话留言（不是主人的指令）· 来自「会话beta」（tp_beta）", text)
        self.assertIn("> 构建我来触发，别重复点", text)
        self.assertEqual(len(w.store.unread_mail("tp_alpha")), 1)  # 送到了、还没回执
        w["alpha"].act("Read mail")
        w["alpha"].receipt()
        self.to(2)
        self.assertEqual(w.store.unread_mail("tp_alpha"), [])
        # 收件会话被主人占着：留言排队，不丢；主人走了以后送到
        w["alpha"].owner_says("我在这")
        w["alpha"].answer_only()
        self.cli("tell", "--topic", "tp_beta", "--to", "tp_alpha", "主人占着时的留言")
        self.to(10)
        self.assertEqual(len(self.got("tp_alpha")), 1)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_alpha")[0]["unread_mail"], 1)  # 排着，没丢
        self.to(18)
        self.assertEqual(len(self.got("tp_alpha")), 2)
        self.assertIn("主人占着时的留言", self.texts("tp_alpha")[-1])
        # 收件会话卡住（中断）：留言排队，会话一动就送到，只送一次
        w["alpha"].app_alive = False
        self.to(40)
        self.assert_card("tp_alpha", duty.STALLED)
        self.cli("tell", "--topic", "tp_beta", "--to", "tp_alpha", "你卡住时的留言")
        self.to(45)
        self.assertEqual(len(self.got("tp_alpha")), 2)
        w["alpha"].app_alive = True
        w["alpha"].act("Back")
        self.to(46)
        self.assertEqual(len(self.got("tp_alpha")), 3)
        self.assertIn("你卡住时的留言", self.texts("tp_alpha")[-1])
        self.to(60)
        self.assertEqual(sum("你卡住时的留言" in t for t in self.texts("tp_alpha")), 1)

    def test_pair_limit_pauses_and_notifies_bus_once(self):
        w = self.w
        for i in range(6):
            a, b = ("tp_alpha", "tp_beta") if i % 2 == 0 else ("tp_beta", "tp_alpha")
            rc, out = self.cli("tell", "--topic", a, "--to", b, f"第 {i + 1} 条")
            self.assertEqual(rc, 0, out)
            w.clock.advance(minutes=1)
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_beta", "第 7 条")
        self.assertEqual(rc, 2)
        self.assertIn("暂停到", out)
        rc, out = self.cli("tell", "--topic", "tp_beta", "--to", "tp_alpha", "第 8 条")
        self.assertEqual(rc, 2)
        self.to(7)
        bus_text = "\n".join(self.texts(BUS_TOPIC_ID))
        self.assertEqual(bus_text.count("会话互发留言太频繁"), 1)
        self.assertIn("被拒的这条：第 7 条", bus_text)
        self.assertNotIn("第 7 条", "\n".join(self.texts("tp_beta")))
        # 暂停 30 分钟以后又能发
        w.clock.advance(minutes=31)
        rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_beta", "暂停结束后")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.notes(), [])

    def test_mail_cannot_claim_owner_words_without_quote(self):
        for text in ("主人原话：直接发版", "已核对，可以发"):
            rc, out = self.cli("tell", "--topic", "tp_alpha", "--to", "tp_beta", text)
            self.assertEqual(rc, 2, text)
            self.assertIn("--quote", out)
        self.to(3)
        self.assertEqual(self.got("tp_beta"), [])


# ======================= S9 摘要 =======================
class S9Digest(Scenario):
    keys = ("rel", "auth", "patrol", "d1", "d2", "d3", "d4", "d5", "d6")

    def digest(self, text: str) -> list[str]:
        if "FX-DIGEST" not in text:
            return []
        part = text.split("FX-DIGEST", 1)[1].split("\n\n", 1)[0]
        return part.splitlines()[1:]

    def bus_batch(self) -> str:
        w = self.w
        w.message("bus", f"总线消息 {w.msg_n}")
        self.w.round(1)
        text = self.texts(BUS_TOPIC_ID)[-1]
        w[BUS_TOPIC_ID].act("Handle")
        w[BUS_TOPIC_ID].receipt()
        return text

    def setup_board(self):
        """2 个在做、1 个中断、6 个空闲。在做的两个 09:05 收到批次（回执超时要到 09:26，不碍事）。"""
        w = self.w
        w.message("patrol", "巡检")
        self.to(1)
        w["patrol"].app_alive = False
        w.message("rel", "发版")
        w.message("auth", "授权")
        self.to(5)
        w["rel"].act("Trigger agent-worker build", cmd="gcloud builds triggers run demo")
        w["auth"].act("Ask colleague to confirm scope")
        self.to(12)
        w["rel"].act("Watch CI", cmd="gh run watch 1")
        w["auth"].act("Read reply")
        self.to(16)
        self.assert_card("tp_patrol", duty.STALLED)
        self.assert_card("tp_rel", duty.WORKING)
        self.assert_card("tp_auth", duty.WORKING)

    def test_digest_merges_idle_and_only_on_change(self):
        self.setup_board()
        text = self.bus_batch()
        lines = self.digest(text)
        self.assertEqual(len(lines), 4, lines)
        self.assertTrue(lines[0].startswith("- 「会话patrol」tp_patrol · 中断（批次 09:01 送出后 16 分钟没有新动作）"), lines[0])
        self.assertTrue(lines[1].startswith("- 「会话auth」tp_auth · 在做 · 此刻动作「Read reply」"), lines[1])
        self.assertTrue(lines[2].startswith("- 「会话rel」tp_rel · 在做 · 此刻动作「Watch CI」"), lines[2])
        self.assertEqual(lines[3], "- 空闲 6 个：会话d1、会话d2、会话d3、会话d4、会话d5、会话d6")
        # 没变化：不附
        self.assertEqual(self.digest(self.bus_batch()), [])
        # 空闲会话自己动了一下（最近动作变了）：不算变化
        self.w["d3"].act("Self polling", cmd="./poll.sh")
        self.assertEqual(self.digest(self.bus_batch()), [])
        self.assertEqual(self.w.card("d3")["action"], "Self polling")
        # 在做的会话换了个动作：也不算变化（同一轮 d1 收到批次；总线先投，这一批还看不到 d1 的变化）
        self.w["rel"].act("Check logs")
        self.w.message("d1", "给 d1 的活")
        self.assertEqual(self.digest(self.bus_batch()), [])
        # 进出空闲：算
        lines = self.digest(self.bus_batch())
        self.assertTrue(any("tp_d1 · 在做" in ln for ln in lines), lines)
        self.assertEqual(lines[-1], "- 空闲 5 个：会话d2、会话d3、会话d4、会话d5、会话d6")
        # 不空闲的会话名下的活变了：算
        self.assertEqual(self.cli("work", "set", "--topic", "tp_rel", "--key", KEY1, "--title", "发版")[0], 0)
        lines = self.digest(self.bus_batch())
        self.assertTrue(any(f"手上 {KEY1}（进行中·自报）" in ln for ln in lines), lines)
        self.assertEqual(self.notes()[0][1], "在岗表通知")  # patrol 中断的那一条
        self.assertEqual(len(self.notes()), 1)

    def test_digest_cap_keeps_stuck_and_says_how_many_omitted(self):
        self.w.cfg.duty = DutyConfig(digest_max_lines=3)
        self.setup_board()
        lines = self.digest(self.bus_batch())
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("tp_patrol · 中断", lines[0])
        self.assertEqual(lines[-1], "- …另有 7 个会话没列出（在做 1、空闲 6），`sheepdog board` 看全部")


# ======================= S10 自动补活 =======================
class S10AutoWork(Scenario):
    def test_watch_escalation_forward_and_declared_wins(self):
        w = self.w
        # 会话先自己申报了 ABC-12（只写了标题），之后又登记等待：以申报为准，只补缺的字段
        self.assertEqual(self.cli("work", "set", "--topic", "tp_alpha", "--key", "ABC-12", "--title", "自己申报的")[0], 0)
        rc, out = self.cli("watch", "--topic", "tp_alpha", "--person", "ou_test_bob", "--note", "等 bob 确认 ABC-12")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("watch", "--topic", "tp_beta", "--person", "ou_test_carol", "--note", "等 carol 回 XYZ-3")
        self.assertEqual(rc, 0, out)
        w.store.add_escalation("om_test_esc1", "tp_beta", "oc_test_esc", "🐕 需要你定 · [tp_beta] PR #77 要不要合",
                               w.now.astimezone().isoformat(timespec="seconds"))
        self.to(1)
        a = {x["key"]: x for x in w.store.open_work("tp_alpha")}
        self.assertEqual((a["ABC-12"]["state"], a["ABC-12"]["title"], a["ABC-12"]["waiting_on"], a["ABC-12"]["declared"]),
                         (duty.DOING, "自己申报的", "ou_test_bob", 1))
        b = {x["key"]: x for x in w.store.open_work("tp_beta")}
        self.assertEqual((b["XYZ-3"]["state"], b["XYZ-3"]["source"]), (duty.WAITING_OTHER, "watch"))
        self.assertEqual((b["PR #77"]["state"], b["PR #77"]["waiting_on"]), (duty.WAITING_OWNER, "主人"))
        self.assert_card("tp_beta", duty.WAITING)
        # 运行状态只看事实：alpha 登记了等 bob 回复（watch），就是等回复——不管它自报的是「进行中」
        self.assert_card("tp_alpha", duty.WAITING, "等 ou_test_bob 回复（watch #1")
        # forward → 转出方名下「已转交」
        mid = w.message("bus", "XYZ-9 这单给 alpha")
        self.to(2)
        w[BUS_TOPIC_ID].act("Forward")
        w[BUS_TOPIC_ID].receipt()
        forward_messages(w.store, "tp_alpha", [mid])
        done = w.store.closed_work(BUS_TOPIC_ID)[0]
        self.assertEqual((done["key"], done["state"], done["handed_to"]), ("XYZ-9", duty.HANDED_OFF, "tp_alpha"))
        self.to(3)
        self.assertEqual(self.got("tp_alpha", 3), [[mid]])
        # 来源结束：自动补的跟着收掉；申报的不动
        for wt in w.store.open_watches():
            w.store.close_watch(wt["id"], "done")
        w.store.answer_escalation("om_test_esc1", "om_test_ans1")
        self.to(4)
        self.assertEqual([x["key"] for x in w.store.open_work("tp_beta")], [])
        self.assertEqual({x["key"]: x["result"] for x in w.store.closed_work("tp_beta")},
                         {"XYZ-3": "等待结束（done）", "PR #77": "主人已答"})
        self.assertEqual([x["key"] for x in w.store.open_work("tp_alpha")], ["ABC-12"])
        self.assert_card("tp_beta", duty.IDLE)


# ======================= S11 重启 =======================
class S11Restart(Scenario):
    def test_restart_while_stalled_does_not_renotify_or_redeliver(self):
        w, a = self.w, self.w["alpha"]
        a.app_alive = False
        m1 = w.message("alpha", "第一件")
        self.to(1)
        m2 = w.message("alpha", "第二件")
        self.to(17)
        self.assert_card("tp_alpha", duty.STALLED)
        self.assertEqual(len(self.notes()), 1)
        w.restart()
        self.to(25)
        self.assert_card("tp_alpha", duty.STALLED)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.got("tp_alpha"), [[m1]])
        self.assertEqual(w.state(m2), "pending")
        self.assertEqual(w.card("alpha")["queued"], 1)
        w.restart()
        self.to(32)
        self.assertEqual([x[2] for x in self.notes()],
                         ["🐕 汇报 · 会话中断了 [tp_bus]", "🐕 汇报 · 会话失联了 [tp_bus]"])
        w.restart()
        a.app_alive = True
        a.act("Back")
        self.to(33)
        self.assertEqual(self.got("tp_alpha"), [[m1], [m2]])
        a.act("Handle m2")
        a.receipt()
        w.restart()
        self.to(50)
        self.assertEqual(self.got("tp_alpha"), [[m1], [m2]])
        self.assertEqual([x[2] for x in self.notes()], ["🐕 汇报 · 会话中断了 [tp_bus]", "🐕 汇报 · 会话失联了 [tp_bus]",
                                                       "🐕 汇报 · 会话已恢复 [tp_bus]"])

    def test_restart_in_attention_still_auto_recovers(self):
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        bus.net_down = True
        m1 = w.message("bus", "断网")
        self.to(64)  # 09:01、09:22、09:43 三批都超时：10:04 进 attention
        self.assertEqual(w.topic(BUS_TOPIC_ID)["state"], sm.ATTENTION)
        n = len(self.notes())
        w.restart()
        self.to(70)
        self.assertEqual(len(self.notes()), n)
        bus.net_down = False
        bus.act("Back")
        self.to(71)
        self.assertEqual(w.topic(BUS_TOPIC_ID)["state"], sm.RUNNING)
        self.assertEqual(self.got(BUS_TOPIC_ID, 71), [[m1]])
        self.assertEqual(self.notes()[-1][1], "恢复通知")


# ======================= S12 规则重推 =======================
class S12RulesPush(Scenario):
    def test_rules_change_pushed_once_per_session(self):
        w = self.w
        for tid in (BUS_TOPIC_ID, "tp_alpha", "tp_beta"):
            w.message(tid.removeprefix("tp_"), "规则改之前")
        self.to(1)
        for tid in (BUS_TOPIC_ID, "tp_alpha", "tp_beta"):
            self.assertFalse(self.texts(tid)[-1].startswith(RULES_UPDATE_HEADER), tid)
            w[tid].act("Handle")
            w[tid].receipt()
        common = w.root / "config" / "playbook" / "common.md"
        common.write_text(common.read_text(encoding="utf-8") + "\nFX-RULE-V2 新加的一条规则\n", encoding="utf-8")
        for round_ in range(2):
            for tid in (BUS_TOPIC_ID, "tp_alpha", "tp_beta"):
                w.message(tid.removeprefix("tp_"), f"规则改之后 {round_}")
            self.to(2 + round_)
            for tid in (BUS_TOPIC_ID, "tp_alpha", "tp_beta"):
                text = self.texts(tid)[-1]
                if round_ == 0:
                    self.assertTrue(text.startswith(RULES_UPDATE_HEADER), tid)
                    self.assertIn("FX-RULE-V2", text)
                else:
                    self.assertFalse(text.startswith(RULES_UPDATE_HEADER), tid)
                w[tid].act("Handle")
                w[tid].receipt()
        self.assertEqual(self.notes(), [])


# ======================= S13 压缩 =======================
class S13Compaction(Scenario):
    def test_compaction_marks_card_and_next_batch_carries_full_rules(self):
        w, a = self.w, self.w["alpha"]
        self.to(2)
        a.compact()
        self.to(3)
        c = duty.board_cards(w.store, w.cfg, topic="tp_alpha")[0]
        self.assertEqual(c["compacted_label"], "刚被压缩（1 分钟前）")
        rc, out = self.cli("board")
        self.assertIn("刚被压缩（1 分钟前）", out)
        mid = w.message("alpha", "压缩以后的第一条")
        self.to(4)
        text = self.texts("tp_alpha")[-1]
        self.assertTrue(text.startswith(RULES_UPDATE_HEADER))
        self.assertIn("FX-COMPACTED title=会话alpha", text)
        self.assertIn(mid, text)
        a.act("Handle")
        a.receipt()
        w.message("alpha", "第二条")
        self.to(5)
        self.assertFalse(self.texts("tp_alpha")[-1].startswith(RULES_UPDATE_HEADER))
        a.act("Handle")
        a.receipt()
        # 超过 compacted_recent_minutes（60）不再标
        self.to(70)
        self.assertNotIn("compacted_label", duty.board_cards(w.store, w.cfg, topic="tp_alpha")[0])
        self.assertEqual(self.notes(), [])


# ======================= 其他关键路径 =======================
class S14OtherPaths(Scenario):
    def test_spinning_marked_and_cleared(self):
        w, a = self.w, self.w["alpha"]
        w.message("alpha", "盯一下 CI")
        self.to(1)
        for i in range(3):
            a.act("Wait for CI", cmd="sleep 60")
            self.to(2 + i)
        c = w.card("alpha")
        self.assertEqual((c["run_state"], c["spinning"]), (duty.WORKING, 1))
        rc, out = self.cli("board")
        self.assertIn("运行状态：在做（空转）", out)
        a.act("Check CI result", cmd="gh run view 1")
        self.to(5)
        self.assertEqual(w.card("alpha")["spinning"], 0)

    def test_session_reset_clears_stall_and_new_stall_notifies_again(self):
        w, a = self.w, self.w["alpha"]
        a.app_alive = False
        w.message("alpha", "第一件")
        self.to(17)
        self.assertEqual(len(self.notes()), 1)
        m2 = w.message("alpha", "第二件")
        rc, out = self.cli("session-reset", "--topic", "tp_alpha")
        self.assertEqual(rc, 0, out)
        self.to(22)  # 回执超时（09:22）以后可投递；reset 之前送出的批次不再算中断
        self.assertEqual(self.got("tp_alpha", 22), [[m2]])
        self.to(37)  # 新的一批又 15 分钟没碰：新的一段，再通知
        self.assert_card("tp_alpha", duty.STALLED, "批次 09:22 送出后")
        self.assertEqual(len(self.notes()), 2)

    def test_adopted_finishing_without_receipt_is_flagged_after_15_minutes(self):
        """现状记录（报告里列为风险）：adopted 会话回执可选。做完就停、不交回执，批次还挂着（20 分钟才算超时），
        最后一次动作满 15 分钟时会判中断并通知一次；20 分钟回执超时以后回到空闲。"""
        w, a = self.w, self.w["alpha"]
        w.message("alpha", "一个小问题")
        self.to(1)
        a.act("Answer the question")
        self.to(16)
        self.assert_card("tp_alpha", duty.STALLED)
        self.assertEqual(len(self.notes()), 1)
        self.to(22)
        self.assert_card("tp_alpha", duty.IDLE)

    def test_failed_bus_with_owner_talking(self):
        """总线 failed（回执超时）时主人正在总线里说话：human_attach 不允许从 failed 进，以前照样重投进他正说话的会话。
        现在：failed 加上卡片是「被主人占着」就排队，主人走了（15 分钟）再重投。"""
        w, bus = self.w, self.w[BUS_TOPIC_ID]
        mid = w.message("bus", "总线的一条")
        self.to(1)
        self.to(19)
        bus.owner_says("我来看看总线怎么了")
        bus.answer_only()
        self.to(22)  # 09:22 回执超时 → failed；主人还在 → 不重投
        self.assertEqual(w.topic(BUS_TOPIC_ID)["state"], sm.FAILED)
        self.assert_card(BUS_TOPIC_ID, duty.OCCUPIED)
        self.assertIn("主人在会话里", w.last_result["topics"][BUS_TOPIC_ID]["queued"])
        self.to(33)
        self.assertEqual(self.got(BUS_TOPIC_ID), [[mid]])
        self.to(34)  # 09:19 + 15 分钟：主人走了，重投
        self.assertEqual(self.got(BUS_TOPIC_ID), [[mid], [mid]])

    def test_crash_mid_send_is_not_redelivered(self):
        """发送中途崩溃（7.16）：批次记了 sending、消息已经进了会话记录 → 重启后补记已送达，不重投。"""
        w = self.w
        mid = w.message("alpha", "崩溃那一刻的消息")
        orig = w.store.set_dispatch_state
        calls = []

        def crash(batch_id, state, error=None):
            if state == "sent" and not calls:
                calls.append(batch_id)
                raise RuntimeError("SIM 进程在发完、记账前被杀")
            return orig(batch_id, state, error)

        with mock.patch.object(w.store, "set_dispatch_state", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.to(1)
        w.restart()
        self.to(2)
        self.assertEqual(w.store.get_dispatch(calls[0])["state"], "sent")
        self.assertEqual(self.got("tp_alpha"), [[mid]])  # 只投过那一次
        self.assertEqual(w.state(mid), "delivered")


class DefaultsGuardTest(unittest.TestCase):
    """场景测试一律用生产默认值；这里守住默认值本身，免得有人改了默认值、场景测试跟着悄悄换了含义。"""

    def test_production_defaults(self):
        d, s = DutyConfig(), SessionConfig()
        self.assertEqual((d.stall_minutes, d.lost_minutes, d.spin_repeat, d.digest_max_lines), (15, 30, 3, 15))
        self.assertEqual((d.mail_pair_limit, d.mail_window_minutes, d.mail_pause_minutes), (6, 10, 30))
        self.assertEqual((d.compacted_recent_minutes, d.history_days), (60, 7))
        self.assertEqual((s.human_attach_minutes, s.receipt_timeout_minutes), (15, 20))
        cfg = Config()
        self.assertEqual(cfg.notify.file, "")
        w = World(["alpha"])
        try:
            self.assertEqual((w.cfg.duty.stall_minutes, w.cfg.duty.lost_minutes, w.cfg.session.human_attach_minutes,
                              w.cfg.session.receipt_timeout_minutes), (15, 30, 15, 20))
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
