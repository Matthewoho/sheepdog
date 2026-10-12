"""在岗表（7.20）/ attention 自动恢复（7.21）的随机长跑：8 个会话（含总线），固定随机种子，默认每个种子 2000 轮，
每一轮随机发生新消息、会话动作、回执、卡住、断网、App 死掉、回执超时、投递失败、主人在会话里说话、tell 留言、
work set / done、压缩、等待、重启、session-reset，然后检查不变量：

1. 同一条消息不会投给会话两次。合法的重投只有一种：它上一次所在的批次被判回执超时（failed，7.21 的重投），
   消息退回排队、再投；判过 acked / acked_late / missed 的批次里的消息不会再投。
2. 消息不丢：每条要投的消息要么已投出，要么在排队，而且排队有原因（会话状态不可投递、在岗表判中断 / 失联、
   这一轮发送失败）；最后把所有会话治好，排队的全部投出去。
3. 每个会话每一段卡住（从会话最后一次动作或 session-reset 算起的一段沉默），给主人的在岗表通知中断最多一条、
   失联最多一条；attention 通知每进一次 attention 最多一条，恢复通知不多于 attention 通知。
4. 卡片和账本、会话记录对得上：卡片是「被主人占着」⇔ 这一轮因为主人没投它（决定那一刻卡片被主人占着，就一定
   没投；因为主人排队——会话是 human_attached，或 failed 时「主人在会话里」——卡片一定是被主人占着）；主人在会话
   里说话只有一个判断源：卡片上的 human_at 和真相一致，engine 从不问 sink（SimSink 的 last_human_activity 像以前的
   DryRunSink 一样永远答「没有」，旧写法在这里会露馅）；在做 ⇒ 批次在跑、且从「最后一次动作 / 这一串没交差的批次
   最早送出」起不到中断阈值；路径判的中断 / 失联 ⇒ 会话真的沉默够久（失联还要记录文件真的不动）；
   排队数 = 账本里排队的信号数。
5. board、摘要、--json 渲染不报错，摘要不超过行数上限。
6. 指纹稳定：在岗表（按规格的口径，自己独立算）没变的轮次不附摘要，变了的下一批一定附。

环境变量：SHEEPDOG_LONGRUN_ROUNDS（默认 2000，可以调到 20000）、SHEEPDOG_LONGRUN_SEEDS（逗号分隔，默认 3 个种子）。
直接跑这个文件会打印每个种子的统计：python tests/test_duty_longrun.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sys
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta
from unittest import mock

from sheepdog import cli, duty
from sheepdog import session as sm
from sheepdog.engine import BUS_TOPIC_ID, RECEIPT_TIMEOUT

from duty_sim import START, World

KEYS = ["rel", "auth", "patrol", "ops", "data", "docs", "infra"]
KEY_POOL = ["202610100035", "202610100036", "PR #12", "PR #77", "ABC-1", "ABC-2", "OPS-9", "DATA-42"]
DEFAULT_SEEDS = (20261012, 7, 4242)
DELIVERED = ("delivered", "acked", "missed")


def my_fingerprint(cards: list[dict]) -> str:
    """按规格独立算的在岗表指纹：空闲的只看「空闲」，不空闲的看运行状态、空转、手上的事、未读留言。
    手上的事 = 还开着的活（钥匙、状态、自报还是自动、可能过期）+ 还算数的路由事实（钥匙）。"""
    data = {}
    for d in cards:
        if d["run_state"] == duty.IDLE:
            data[d["topic_id"]] = "idle"
            continue
        items = {}
        for w in d["work"]:
            items[w["norm_key"]] = [w["state"], bool(w["declared"]), bool(w["stale"]), False]
        for r in d["routes"]:
            items.setdefault(r["norm_key"], ["", False, False, False])[3] = True
        data[d["topic_id"]] = [d["run_state"], bool(d["spinning"]), sorted([k, *v] for k, v in items.items()),
                               d["unread_mail"]]
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


class Behaviour:
    """一个会话的「脾气」：健康 / 模型卡死（App 活着）/ App 死了 / 断网，及处理批次的节奏。"""

    def __init__(self):
        self.mode = "healthy"
        self.mode_until = None
        self.task_until = None      # 正在处理的批次什么时候做完
        self.next_act = None        # 下一次动作的时间
        self.long_quiet = False     # 这一批里有一段长命令（很久不动）
        self.spin = 0               # 还要重复几次同一条命令
        self.handled: set[str] = set()
        self.owner_until = None     # 主人在会话里说话，持续到什么时候


class LongRun:
    def __init__(self, seed: int, rounds: int, mode: str = "chaos"):
        """mode：chaos = 会话随机登记、还乱登记（登错编号、该收不收、重复登记、自报和实际相反、回执里带坏的 work）；
        none = 完全不登记。两种模式用同一个种子，除了登记以外的随机事件一模一样（登记用另一个随机数发生器）。"""
        self.seed, self.rounds, self.mode = seed, rounds, mode
        self.rng = random.Random(seed)
        self.reg_rng = random.Random(seed * 7919 + 17)
        self.trace: list[tuple] = []     # 每一轮：投了什么、给主人发了什么通知、每张卡片的运行状态
        self.owed: dict[str, int] = {}   # 说过中断 / 失联 / attention、还没说「已恢复」的：会话 → 这一段说了几条
        self.owed_at: dict[str, object] = {}  # 最后一条「卡住」类通知的时间
        self.fail_told: dict[str, bool] = {}
        self.w = World(KEYS, fast_db=True)
        self.tids = [BUS_TOPIC_ID] + [f"tp_{k}" for k in KEYS]
        self.beh = {tid: Behaviour() for tid in self.tids}
        self.stats: Counter = Counter()
        self.deliveries: dict[str, list[tuple[str, str]]] = {}   # message_id → [(批次, 会话)]
        self.seen_deliveries = 0
        self.seen_notices = 0
        self.reset_at: dict[str, object] = {}
        self.segments: Counter = Counter()
        self.attn_seen: dict[str, str] = {}
        self.attn_entries: Counter = Counter()
        self.attn_notices: Counter = Counter()
        self.recover_notices: Counter = Counter()
        self.last_digest_fp: str | None = None
        self.quiet_until = None
        self.decisions: dict[str, dict] = {}
        self.failures: list[str] = []
        self.wrap()

    # ---------- 记下每个会话这一轮的投递决定（决定那一刻的状态、卡片、摘要指纹） ----------
    def wrap(self) -> None:
        d = self.w.d
        orig = d._dispatch_topic

        def wrapped(t, rows):
            tid = t["topic_id"]
            card = self.w.store.get_card(tid)
            info = {"state": t["state"], "card": card["run_state"] if card is not None else None,
                    "rows": [r["message_id"] for r in rows], "n": len(self.w.sink.deliveries)}
            if tid == BUS_TOPIC_ID:
                info["fp"] = my_fingerprint(duty.board_cards(self.w.store, self.w.cfg, exclude=(BUS_TOPIC_ID,)))
            res = orig(t, rows)
            info["res"] = res
            info["sent"] = self.w.sink.deliveries[info["n"]:]
            self.decisions[tid] = info
            return res

        d._dispatch_topic = wrapped

    def fail(self, msg: str) -> None:
        self.failures.append(f"[seed {self.seed} 第 {self.w.round_no} 轮 {self.w.now.astimezone():%m-%d %H:%M}] {msg}")

    def cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.w.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    # ======================= 随机事件 =======================
    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def mins(self, a: float, b: float) -> timedelta:
        return timedelta(minutes=self.rng.uniform(a, b))

    def events(self) -> None:
        w, rng = self.w, self.rng
        now = w.now
        # 安静的一段：只有总线有消息（在岗表不怎么变，用来检查「没变就不附摘要」）
        if self.quiet_until is None and self.chance(0.01):
            # 多数是半小时到一个半小时；偶尔一整晚（5–8 小时，自报的活会「可能过期」）
            self.quiet_until = now + (self.mins(300, 480) if self.chance(0.2) else self.mins(30, 90))
            self.stats["安静时段"] += 1
        quiet = self.quiet_until is not None and now < self.quiet_until
        if self.quiet_until is not None and not quiet:
            self.quiet_until = None
        # 新消息：私聊 / 群里 @主人 / 陌生人私聊（给总线），有的提到编号
        for _ in range(rng.choice((0, 0, 0, 1, 1, 2))):
            key = rng.choice(KEY_POOL) if self.chance(0.3) else ""
            text = f"SIM 随机消息 {w.msg_n + 1}" + (f"，关于 {key}" if key else "")
            r = 1.0 if quiet else rng.random()
            if r < 0.5:
                w.message(rng.choice(KEYS), text)
            elif r < 0.65:
                w.message(rng.choice(KEYS), text, group=True, at_me=True)
            else:
                w.message("bus", text)
            self.stats["消息"] += 1
        for tid in self.tids:
            b, s = self.beh[tid], w.sessions[tid]
            # 会话进入坏状态一段时间
            if b.mode == "healthy" and not quiet and self.chance(0.004):
                b.mode = rng.choice(("stuck", "dead", "net_down"))
                b.mode_until = now + self.mins(10, 120)
                s.app_alive = b.mode != "dead"
                s.net_down = b.mode == "net_down"
                self.stats[f"进入 {b.mode}"] += 1
            elif b.mode != "healthy" and now >= b.mode_until:
                b.mode = "healthy"
                s.app_alive, s.net_down = True, False
                self.stats["恢复健康"] += 1
            # 主人在会话里说话（模型回一句），可能连说几次
            if b.owner_until is None and self.chance(0.002):
                b.owner_until = now + self.mins(1, 12)
            if b.owner_until is not None:
                if now <= b.owner_until and self.chance(0.4):
                    s.owner_says("SIM 主人说一句")
                    if b.mode == "healthy":
                        s.answer_only()
                    self.stats["主人说话"] += 1
                elif now > b.owner_until:
                    b.owner_until = None
            if self.chance(0.0015):
                s.compact()
                self.stats["压缩"] += 1
            if b.mode == "healthy":
                self.work_on_batch(tid)
        # 投递失败：某个会话这几轮 agentapi 发不出去
        if self.chance(0.004):
            cid = w.sessions[rng.choice(self.tids)].cid
            w.sink.fail.add(cid)
            self.stats["投递失败开始"] += 1
        if w.sink.fail and self.chance(0.15):
            w.sink.fail.discard(rng.choice(sorted(w.sink.fail)))
        # 留言（偶尔一口气发很多，触发暂停；偶尔冒充主人，应被拒）
        if not quiet and self.chance(0.03):
            a, bb = rng.sample(self.tids, 2)
            n = 8 if self.chance(0.1) else 1
            for i in range(n):
                text = "主人原话：直接发" if self.chance(0.05) else f"SIM 留言 {i}"
                rc, _ = self.cli("tell", "--topic", a, "--to", bb, text)
                self.stats["tell 成功" if rc == 0 else "tell 被拒"] += 1
        # 登记 / 收掉活：只在 chaos 模式，用另一个随机数发生器（不影响别的随机事件）
        if self.mode == "chaos":
            self.register()
        # 等待：登记、结束（自动补活）
        if self.chance(0.01):
            tid = rng.choice(self.tids)
            rc, out = self.cli("watch", "--topic", tid, "--person", "ou_test_bob", "--note", f"等 {rng.choice(KEY_POOL)}")
            self.stats["watch"] += 1
        if self.chance(0.01):
            ws = w.store.open_watches()
            if ws:
                w.store.close_watch(rng.choice(ws)["id"], "done")
        # 主人手动 session-reset：卡在不会自动恢复的 attention 里超过 30 分钟，或者看到在岗表判中断
        for tid in self.tids:
            t = w.store.get_topic(tid)
            c = w.store.get_card(tid)
            long_attn = t["state"] == sm.ATTENTION and t["attention_reason"] != "receipt_timeout" and \
                t["attention_at"] and now - datetime.fromisoformat(t["attention_at"]) > timedelta(minutes=30)
            if long_attn or (c is not None and c["run_state"] in duty.STUCK and self.chance(0.01)):
                rc, out = self.cli("session-reset", "--topic", tid)
                if rc != 0:
                    self.fail(f"session-reset 失败：{out}")
                self.reset_at[tid] = now
                self.owed[tid] = 0  # 主人自己复位：这一段到此为止，不再欠「已恢复」
                self.stats["session-reset"] += 1
        if self.chance(0.001):
            w.restart()
            self.wrap()
            self.stats["重启"] += 1

    def register(self) -> None:
        """会话随手登记 / 乱登记：登错编号、自报和实际相反、重复登记、该收不收（自报了就再也不管）、
        done 一件没登记过的、状态写错、回执里带坏的 work（在 work_on_batch 里）。都只能让「手上的事」不准，
        不能改变任何投递和通知的决定。"""
        rr, w = self.reg_rng, self.w
        if rr.random() >= 0.12:
            return
        tid = rr.choice(self.tids)
        t = w.store.get_topic(tid)
        wrong_key = f"2026{rr.randint(10000000, 99999999)}"  # 登错的编号：消息里从没出现过
        key = rr.choice(KEY_POOL + [wrong_key])
        kind = rr.random()
        if kind < 0.35:      # 随手登记一件（状态随机）
            state = rr.choice(("doing", "waiting_other", "waiting_owner", "done", "handed_off"))
        elif kind < 0.55:    # 和实际相反：在跑批次的说在等、空闲的说在做、卡住的说做完了
            state = "waiting_other" if t["state"] == sm.RUNNING else "doing"
            if w.store.get_card(tid) is not None and w.store.get_card(tid)["run_state"] in duty.STUCK:
                state = "done"
        elif kind < 0.65:    # 重复登记同一件
            state = "doing"
            self.cli("work", "set", "--topic", tid, "--key", key, "--title", "SIM 重复登记", "--state", state)
        elif kind < 0.75:    # 状态写错 / 转交不写 --to：被拒，什么都不该变
            rc, _ = self.cli("work", "set", "--topic", tid, "--key", key, "--state", rr.choice(("busy", "handed_off")))
            self.stats["登记被拒"] += rc != 0
            return
        elif kind < 0.9:     # 收掉一件（有的是自报的、有的只有路由事实、有的根本没有）
            rc, _ = self.cli("work", "done", "--topic", tid, "--key", key, "--result", "SIM 做完")
            self.stats["登记 done"] += 1
            return
        else:                # 自报「进行中」以后再也不管（4 小时后应标「可能过期」）
            state = "doing"
        argv = ["work", "set", "--topic", tid, "--key", key, "--title", "SIM 自报", "--state", state]
        if state == "handed_off":
            argv += ["--to", rr.choice([x for x in self.tids if x != tid])]
        rc, out = self.cli(*argv)
        if rc != 0:
            self.fail(f"work set 失败：{out}")
        self.stats["登记"] += 1

    def receipt_work(self) -> dict:
        """chaos 模式下回执里带的 work 字段：有好有坏（缺 key、状态写错、登错编号）。"""
        if self.mode != "chaos" or self.reg_rng.random() >= 0.2:
            return {}
        rr = self.reg_rng
        item = rr.choice(({"key": rr.choice(KEY_POOL), "title": "SIM 回执里的活", "state": "doing"},
                          {"key": "", "state": "doing"}, {"key": rr.choice(KEY_POOL), "state": "飞起来"},
                          {"key": f"2026{rr.randint(10000000, 99999999)}", "state": "done", "result": "登错的"}))
        return {"work": item}

    def work_on_batch(self, tid: str) -> None:
        """健康的会话处理它收到的批次：先动起来，中间可能有一段长命令、可能空转，做完交回执（adopted 有时不交）。
        之前判过超时的批次，醒过来以后有时补交晚到的回执。"""
        w, rng, b, s = self.w, self.rng, self.beh[tid], self.w.sessions[tid]
        now = w.now
        t = w.store.get_topic(tid)
        bid = t["pending_batch_id"]
        if bid and bid in s.batches and bid not in b.handled:
            if b.task_until is None:
                b.task_until = now + (self.mins(1, 6) if self.chance(0.8) else self.mins(8, 45))
                b.next_act = now + self.mins(0, 2)
                b.long_quiet = self.chance(0.2)
                b.spin = 4 if self.chance(0.05) else 0
            if now >= b.task_until:
                s.act("Submit receipt", cmd=f"sheepdog receipt --topic {tid} --batch {bid}")
                b.handled.add(bid)
                b.task_until = None
                if tid == BUS_TOPIC_ID or not self.chance(0.15):  # adopted 回执可选
                    status = rng.choice(("handled",) * 6 + ("needs_decision", "waiting_external"))
                    extra = self.receipt_work()
                    s.receipt(bid, status=status, **extra)
                    self.stats["回执"] += 1
                else:
                    self.stats["做完不交回执"] += 1
            elif now >= b.next_act:
                if b.spin:
                    s.act("Wait for CI", cmd="sleep 60")
                    b.spin -= 1
                else:
                    s.act(rng.choice(("Read the request", "Run a check", "Draft a reply")),
                          cmd=rng.choice(("", "", "gh pr view 12")))
                b.next_act = now + (self.mins(16, 30) if b.long_quiet and self.chance(0.3) else self.mins(1, 6))
                self.stats["会话动作"] += 1
        elif not bid:
            b.task_until = None
            # 醒过来补交之前判超时的那一批（晚到的回执）
            if s.batches and self.chance(0.05):
                last = s.batches[-1]
                d = w.store.get_dispatch(last)
                if d is not None and d["state"] in ("failed", "missed") and (d["error"] or "").startswith(RECEIPT_TIMEOUT) \
                        and last not in b.handled:
                    s.act("Late receipt", cmd=f"sheepdog receipt --topic {tid} --batch {last}")
                    s.receipt(last, summary="SIM 晚到的回执")
                    b.handled.add(last)
                    self.stats["晚到回执"] += 1

    # ======================= 不变量 =======================
    def check(self) -> None:
        w, store, cfg, now = self.w, self.w.store, self.w.cfg, self.w.now
        stall = timedelta(minutes=cfg.duty.stall_minutes)
        lost = timedelta(minutes=cfg.duty.lost_minutes)
        # ---- 1. 同一条消息不会投两次（除了回执超时退回的重投） ----
        self.round_deliveries = w.sink.deliveries[self.seen_deliveries:]
        for dlv in self.round_deliveries:
            bid = dlv["batch"]
            if not bid:
                continue
            tid = store.get_dispatch(bid)["topic_id"]
            for mid in json.loads(store.get_dispatch(bid)["message_ids"] or "[]"):
                prev = self.deliveries.setdefault(mid, [])
                for pbid, _ in prev:
                    pd = store.get_dispatch(pbid)
                    if pd is None:
                        self.fail(f"不变量 1：{mid} 又投了一次（{bid}），上一次的批次 {pbid} 已经被保留期清理删了")
                        continue
                    if not (pd["state"] == "failed" and (pd["error"] or "").startswith(RECEIPT_TIMEOUT)):
                        self.fail(f"不变量 1：{mid} 又投了一次（{bid}），上一次的批次 {pbid} 是 {pd['state']}")
                if prev:
                    self.stats["合法重投"] += 1
                prev.append((bid, tid))
                self.stats["inv1 检查"] += 1
        self.seen_deliveries = len(w.sink.deliveries)
        # ---- 2. 不丢：排队要有原因 ----
        self.stats["inv2 检查"] += store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE route='dispatch'").fetchone()[0]
        # 投出去、还没回执的消息，所在的批次一定还在等回执；批次判了回执超时，消息就该退回排队（不然它就丢了）
        for r in store.conn.execute(
                "SELECT m.message_id, m.batch_id, d.state AS dstate FROM messages m LEFT JOIN dispatches d "
                "ON d.batch_id = m.batch_id WHERE m.route='dispatch' AND m.dispatch_state='delivered'"):
            if r["dstate"] != "sent":
                self.fail(f"不变量 2：{r['message_id']} 记着已投出，但它的批次 {r['batch_id']} 是 {r['dstate']}"
                          "（没回执也没退回排队，等于丢了）")
        for r in store.conn.execute("SELECT message_id, topic_id, dispatch_state FROM messages WHERE route='dispatch' "
                                    "AND dispatch_state NOT IN ('delivered','acked','missed')"):
            st = r["dispatch_state"]
            if st != "pending":
                self.fail(f"不变量 2：{r['message_id']} 状态是 {st}")
                continue
            info = self.decisions.get(r["topic_id"] or BUS_TOPIC_ID)
            if info is None or r["message_id"] not in info["rows"]:
                self.fail(f"不变量 2：{r['message_id']} 在排队，但这一轮没经过 {r['topic_id']} 的投递决定")
                continue
            res = info["res"]
            why = ("queued" if "queued" in res else "error" if "error" in res else
                   "state" if not (info["state"] in sm.DISPATCHABLE or info["state"] == sm.FAILED) else "")
            if not why or res.get("sent"):
                self.fail(f"不变量 2：{r['message_id']} 在排队却说不出原因：{info['state']} {info['card']} {res}")
            elif why == "queued" and "羊群看板" in res["queued"] and info["card"] not in duty.STUCK:
                self.fail(f"不变量 2：按在岗表排队，但决定时卡片是 {info['card']}")
            self.stats[f"排队原因 {why}"] += 1
        # ---- 3. 通知：每一段卡住中断、失联各最多一条；说过卡住的最后一定说「已恢复」，一次最多 4 条 ----
        notices = w.notices()
        new_notices = notices[self.seen_notices:]
        for n in new_notices:
            what, text = n["what"], n["text"]
            tid = text.split("topic `", 1)[1].split("`", 1)[0]
            cid = w.sessions[tid].cid
            if what == "羊群看板通知":
                kind = "中断" if "会话中断了" in text.splitlines()[0] else "失联"
                s = w.sessions[tid]
                pend = store.get_topic(tid)["pending_batch_id"]
                if kind == "中断" and pend and pend in self.beh[tid].handled:
                    self.stats["中断通知里：会话其实做完了、只是没交回执（adopted 回执可选）"] += 1
                start = max([x for x in (s.last_action, self.reset_at.get(tid)) if x is not None], default=None)
                self.segments[(tid, kind, start)] += 1
                if self.segments[(tid, kind, start)] > 1:
                    self.fail(f"不变量 3：{tid} 同一段卡住（从 {start} 起）第 {self.segments[(tid, kind, start)]} 条{kind}通知")
                if tid == BUS_TOPIC_ID and "暂不投递" in text:
                    self.fail("不变量 3：总线的中断通知不该写「暂不投递」")
                self.owed[tid] = self.owed.get(tid, 0) + 1
                self.owed_at[tid] = now
                self.stats[f"通知 {kind}"] += 1
            elif what == "attention 通知":
                self.attn_notices[tid] += 1
                if self.owed.get(tid) and "仍卡着" not in text.splitlines()[0]:
                    self.fail(f"不变量 3：{tid} 这一段已经说过卡住，attention 通知应该只说「仍卡着」")
                self.owed[tid] = self.owed.get(tid, 0) + 1
                self.owed_at[tid] = now
                self.stats["通知 attention"] += 1
            elif what == "恢复通知":
                if not self.owed.get(tid):
                    self.fail(f"不变量 3：{tid} 没说过卡住，却说「已恢复」")
                elif self.owed[tid] > 3:
                    self.fail(f"不变量 3：{tid} 一次卡住发了 {self.owed[tid] + 1} 条通知（上限 4 条）")
                self.stats[f"一次卡住共 {self.owed.get(tid, 0) + 1} 条通知"] += 1
                self.owed[tid] = 0
                self.stats["通知 恢复"] += 1
            elif what == "投递失败通知":
                if w.sink.streak.get(cid, 0) < 3 or self.fail_told.get(tid):
                    self.fail(f"不变量 3：{tid} 连续失败 {w.sink.streak.get(cid, 0)} 次就说投递不出去（或同一段说了两次）")
                self.fail_told[tid] = True
                self.stats["通知 投递失败"] += 1
            elif what == "投递恢复通知":
                if not self.fail_told.get(tid) or w.sink.streak.get(cid, 0) != 0:
                    self.fail(f"不变量 3：{tid} 没说过投递不出去，或者还没发出去，就说投递已恢复")
                self.fail_told[tid] = False
                self.stats["通知 投递恢复"] += 1
            else:
                self.fail(f"不变量 3：没见过的通知类型 {what}")
            self.stats["inv3 检查"] += 1
        self.seen_notices = len(notices)
        for tid in self.tids:
            t = store.get_topic(tid)
            if t["state"] == sm.ATTENTION and t["attention_at"] != self.attn_seen.get(tid):
                self.attn_entries[tid] += 1
            self.attn_seen[tid] = t["attention_at"] if t["state"] == sm.ATTENTION else None
            if self.attn_notices[tid] > self.attn_entries[tid]:
                self.fail(f"不变量 3：{tid} attention 通知 {self.attn_notices[tid]} 条，进 attention 只有 {self.attn_entries[tid]} 次")
        # ---- 4. 卡片和账本、会话记录对得上 ----
        for tid in self.tids:
            self.stats["inv4 检查"] += 1
            t, c, s = store.get_topic(tid), store.get_card(tid), w.sessions[tid]
            info = self.decisions.get(tid)
            owner_recent = s.owner_spoke is not None and now - s.owner_spoke < timedelta(
                minutes=cfg.session.human_attach_minutes)
            if info is not None:
                # 卡片是「被主人占着」⇔ 这一轮因为主人没投它（卡片和投递是同一个判断）
                for_owner = info["state"] == sm.HUMAN_ATTACHED or "主人在会话里" in info["res"].get("queued", "")
                if info["card"] == duty.OCCUPIED and info["sent"]:
                    self.fail(f"不变量 4：{tid} 决定时卡片是被主人占着（{info['state']}），这一轮却投了")
                if for_owner and info["card"] != duty.OCCUPIED:
                    self.fail(f"不变量 4：{tid} 因为主人排队（{info['state']} {info['res']}），卡片却是 {info['card']}")
                self.stats["inv4 卡片被主人占着的投递决定"] += info["card"] == duty.OCCUPIED
            if info is not None and info["sent"]:
                if info["state"] == sm.HUMAN_ATTACHED:
                    self.fail(f"不变量 4：{tid} 状态是被主人占着，这一轮却投了")
                if owner_recent:  # 按真相：主人 15 分钟内在会话里说过话（failed 也一样，等主人走了再重投）
                    self.fail(f"不变量 4：主人 {s.owner_spoke} 在 {tid} 里说过话（{info['state']}），这一轮却投了")
                if info["card"] in duty.STUCK and info["state"] != sm.FAILED:
                    self.fail(f"不变量 4：{tid} 卡片判{info['card']}，这一轮却投了")
                gt = self.untouched_since(tid)
                if gt is not None and now - gt >= stall and info["state"] != sm.FAILED:
                    self.fail(f"不变量 4：{tid} 有 {gt} 送出、会话一直没碰的批次（按真相该判中断），这一轮却投了"
                              f"（决定时卡片 {info['card']}）")
            if c["queued"] != store.count_pending_signals(tid):
                self.fail(f"不变量 4：{tid} 卡片排队 {c['queued']}，账本里 {store.count_pending_signals(tid)}")
            if owner_recent and c["run_state"] != duty.OCCUPIED:
                self.fail(f"不变量 4：主人 {s.owner_spoke} 在 {tid} 里说过话，卡片却是 {c['run_state']}")
            if c["run_state"] == duty.OCCUPIED and not owner_recent and t["state"] != sm.HUMAN_ATTACHED:
                self.fail(f"不变量 4：{tid} 卡片被主人占着，但主人没在说话")
            quiet_from = max([x for x in (s.last_action, self.reset_at.get(tid)) if x is not None], default=None)
            if c["run_state"] == duty.WORKING:
                if t["state"] != sm.RUNNING:
                    self.fail(f"不变量 4：{tid} 卡片在做，会话状态却是 {t['state']}")
                ref = self.work_clock(tid, s.last_action)
                if ref is not None and now - ref >= stall:
                    self.fail(f"不变量 4：{tid} 卡片在做，但从 {ref} 起就没有新动作（{c['detail']}）")
            if c["run_state"] in duty.STUCK and t["state"] not in (sm.FAILED, sm.ATTENTION):
                if quiet_from is not None and now - quiet_from < stall:
                    self.fail(f"不变量 4：{tid} 卡片判{c['run_state']}，会话 {quiet_from} 还动过（{c['detail']}）")
            if c["run_state"] == duty.LOST and (s.last_write is None or now - s.last_write < lost):
                self.fail(f"不变量 4：{tid} 判失联，但记录 {s.last_write} 还写过")
            if c["run_state"] in (duty.IDLE, duty.WAITING) and t["state"] == sm.RUNNING:
                self.fail(f"不变量 4：{tid} 卡片{c['run_state']}，会话却在跑批次")
        # ---- 5. 渲染 ----
        try:
            cards = duty.board_cards(store, cfg)
            duty.render_board(cards, "SIM")
            json.dumps(cards, ensure_ascii=False)
            self.stats["自报的活标了可能过期（卡片×轮）"] += sum(1 for d in cards for x in d["work"] if x["stale"])
            self.stats["手上的事里的路由事实（卡片×轮）"] += sum(len(d["routes"]) for d in cards)
            for dlv in self.round_deliveries:
                for ln in dlv["content"].splitlines():
                    if ln.startswith("🔀 撞车提醒"):
                        self.stats["撞车提醒（按路由事实）" if "的消息最近送到了" in ln else "撞车提醒（按自动补的 / 自报的活）"] += 1
                        self.stats["撞车提醒里带「可能过期」"] += duty.STALE_MARK in ln
            one = duty.board_cards(store, cfg, topic=self.rng.choice(self.tids), history=True)
            duty.render_board(one, "SIM")
            lines = duty.digest_lines([d for d in cards if d["topic_id"] != BUS_TOPIC_ID], cfg.duty.digest_max_lines)
            if len(lines) > cfg.duty.digest_max_lines:
                self.fail(f"不变量 5：摘要 {len(lines)} 行，超过上限")
            self.stats["inv5 检查"] += 1
        except Exception as e:  # noqa: BLE001
            self.fail(f"不变量 5：渲染报错 {type(e).__name__}: {e}")
        if self.w.round_no % 100 == 0:
            for argv in (("board",), ("board", "--json"), ("board", "--topic", self.rng.choice(self.tids))):
                rc, out = self.cli(*argv)
                if rc != 0:
                    self.fail(f"不变量 5：sheepdog {' '.join(argv)} 退出码 {rc}：{out[-300:]}")
            self.stats["inv5 cli 检查"] += 3
        # ---- 6. 摘要只在在岗表变了时附 ----
        info = self.decisions.get(BUS_TOPIC_ID)
        if info is not None and info["sent"]:
            text = info["sent"][-1]["content"]
            has = "FX-DIGEST" in text
            changed = info["fp"] != self.last_digest_fp
            if has != changed:
                self.fail(f"不变量 6：在岗表{'变了' if changed else '没变'}，摘要却{'附了' if has else '没附'}")
            if has:
                self.last_digest_fp = info["fp"]
                body = text.split("FX-DIGEST", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
                if len(body) > cfg.duty.digest_max_lines:
                    self.fail(f"不变量 5：总线收到的摘要 {len(body)} 行")
                self.stats["摘要附上"] += 1
            else:
                self.stats["摘要没变不附"] += 1
            self.stats["inv6 检查"] += 1
        # ---- 差分用：这一轮的决定（投了什么、给主人说了什么、每张卡片的运行状态） ----
        self.trace.append((
            self.w.round_no, now.isoformat(),
            tuple((d["cid"], d["batch"], tuple(self.canon(m) for m in json.loads(
                store.get_dispatch(d["batch"])["message_ids"] or "[]"))) for d in self.round_deliveries if d["batch"]),
            tuple((n["at"], n["what"], n["text"]) for n in new_notices),
            tuple((tid, (store.get_card(tid) or {"run_state": None})["run_state"], store.get_topic(tid)["state"])
                  for tid in self.tids)))
        # 主人在会话里说话只有一个判断源：在岗表扫会话记录写进卡片的 human_at。它和真相一致；engine 从不问 sink
        if w.sink.human_asked:
            self.fail(f"不变量 4：engine 问了 sink 主人有没有说话（{w.sink.human_asked} 次），判断源不止一个")
        for tid in self.tids:
            c = store.get_card(tid)
            got = datetime.fromisoformat(c["human_at"]) if c is not None and c["human_at"] else None
            if got != w.sessions[tid].owner_spoke:
                self.fail(f"不变量 4：{tid} 卡片上主人说话的时间 {got}，真相是 {w.sessions[tid].owner_spoke}")
        self.stats["inv4 主人说话判断源检查"] += 1

    def canon(self, mid: str) -> str:
        """消息编号在两次跑里要能对上：sheepdog 自己生成的系统消息编号带随机串，换成「原因 + 正文」的摘要。"""
        if not mid.startswith("sd_"):
            return mid
        r = self.w.store.get_message(mid)
        import hashlib
        return f"sd:{r['reason']}:{hashlib.sha1((r['content'] or '').encode()).hexdigest()[:12]}" if r is not None else mid

    def untouched_since(self, tid: str):
        """按真相算：投给这个会话、它一直没碰过（之后没有动作）、也没交回执的批次里最早送出的时间（这一轮刚送的不算）。"""
        store, s, now = self.w.store, self.w.sessions[tid], self.w.now
        floor = max([x for x in (s.last_action, self.reset_at.get(tid)) if x is not None], default=None)
        out = None
        for d in store.recent_dispatches(tid):
            ok = d["state"] in ("sent", "missed") or (d["state"] == "failed" and (d["error"] or "").startswith(
                RECEIPT_TIMEOUT))
            sent = datetime.fromisoformat(d["sent_at"])
            if not ok or sent >= now or now - sent > timedelta(hours=24) or (floor is not None and sent <= floor):
                continue
            out = sent if out is None or sent < out else out
        return out

    def work_clock(self, tid: str, last_action):
        """在做的时钟从哪算：会话最后一次动作，和「这一串没交差的批次」最早送出的时间，取晚的。
        没交差：还挂着的那一批、判回执超时退回重投的；acked / acked_late / missed 断开这一串，session-reset 也断开
        （reset 之前送的不算）。还挂着的那一批是 reset 之前送的：reset 清掉了判定，等它回执超时，不查（返回 None）。"""
        store = self.w.store
        t = store.get_topic(tid)
        reset = self.reset_at.get(tid)
        chain = None
        for d in store.recent_dispatches(tid):
            sent = datetime.fromisoformat(d["sent_at"])
            if reset is not None and sent <= reset:
                break
            if d["state"] == "sent" and d["batch_id"] == t["pending_batch_id"]:
                chain = sent
            elif d["state"] == "failed" and (d["error"] or "").startswith(RECEIPT_TIMEOUT):
                chain = sent
            elif d["state"] in ("acked", "acked_late", "missed"):
                break
        if chain is None:
            return None
        return max(x for x in (last_action, chain) if x is not None)

    # ======================= 跑 =======================
    def run(self) -> Counter:
        t0 = time.time()
        last_prune = self.w.now
        for _ in range(self.rounds):
            self.events()
            self.decisions = {}
            self.w.round(1 if not self.chance(0.02) else self.rng.randint(2, 10))
            self.check()
            if self.w.now - last_prune >= timedelta(hours=1):  # 同 sheepdog run：每小时清一次过期数据
                cfg = self.w.cfg
                pruned = self.w.store.prune(cfg.retention_days, cfg.receipts_dir, cfg.runs_dir, cfg.duty.history_days)
                self.stats["清理掉的消息"] += pruned.get("messages", 0)
                last_prune = self.w.now
            if len(self.failures) > 20:
                break
        self.drain()
        self.stats["耗时秒"] = round(time.time() - t0, 1)
        self.stats["轮数"] = self.w.round_no
        self.stats["模拟分钟"] = int((self.w.now - START).total_seconds() // 60)
        return self.stats

    def drain(self) -> None:
        """治好所有会话，排队的必须全部投出去（不变量 2 的「最终」那一半）。"""
        w = self.w
        w.sink.fail.clear()
        for tid in self.tids:
            b, s = self.beh[tid], w.sessions[tid]
            b.mode, b.owner_until = "healthy", None
            s.app_alive, s.net_down = True, False
        w.clock.advance(minutes=20)  # 主人占着的都过期
        for tid in self.tids:
            w.sessions[tid].act("Wake up")  # 每个会话都醒一下：说过卡住的，接着要说「已恢复」
        for i in range(400):
            for tid in self.tids:
                t = w.store.get_topic(tid)
                if t["state"] == sm.ATTENTION and t["attention_reason"] != "receipt_timeout":
                    self.cli("session-reset", "--topic", tid)
                    self.reset_at[tid] = w.now
                    self.owed[tid] = 0
                self.work_on_batch(tid)
                c = w.store.get_card(tid)
                if c is not None and c["run_state"] in duty.STUCK and (i % 20 == 19):
                    w.sessions[tid].act("Nudge")  # 会话自己醒一下
            self.decisions = {}
            w.round(1)
            self.check()
            pending = w.store.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE route='dispatch' AND dispatch_state='pending'").fetchone()[0]
            busy = any(w.store.get_topic(tid)["state"] == sm.RUNNING for tid in self.tids)
            if pending == 0 and not busy and i >= 2:
                break
        for tid, n in self.owed.items():
            s = w.sessions[tid]
            # 说过卡住、之后会话真的又动了（不是回执超时把 adopted 的批次收掉那种），就一定要说过「已恢复」
            if n and s.last_action is not None and s.last_action > self.owed_at[tid]:
                self.fail(f"不变量 3（最终）：{tid} 说过卡住（{n} 条），会话 {s.last_action} 又动了，也没说「已恢复」")
            elif n:
                self.stats["说过中断、之后会话没再动（adopted 回执超时收掉），不说已恢复"] += 1
        # 治好以后，每条消息都要有着落：会话交了回执（acked），或者 adopted 会话超时不交（missed）
        left = w.store.conn.execute(
            "SELECT message_id, topic_id, dispatch_state FROM messages WHERE route='dispatch' AND dispatch_state NOT IN "
            "('acked','missed')").fetchall()
        for r in left:
            self.fail(f"不变量 2（最终）：{r['message_id']}（{r['topic_id']}）最后还是 {r['dispatch_state']}")
        n = w.store.conn.execute("SELECT COUNT(*) FROM messages WHERE route='dispatch'").fetchone()[0]
        self.stats["最终投出的消息"] = n - len(left)

    def close(self) -> None:
        self.w.close()


def rounds_from_env() -> int:
    return int(os.environ.get("SHEEPDOG_LONGRUN_ROUNDS", "2000"))


def seeds_from_env() -> list[int]:
    v = os.environ.get("SHEEPDOG_LONGRUN_SEEDS", "")
    return [int(x) for x in v.split(",") if x.strip()] if v else list(DEFAULT_SEEDS)


def run_one(seed: int, rounds: int, mode: str) -> LongRun:
    sim = LongRun(seed, rounds, mode)
    try:
        sim.run()
    finally:
        sim.close()
    return sim


def compare(a: LongRun, b: LongRun) -> tuple[list[str], Counter]:
    """差分：同一个种子、一遍乱登记（a）、一遍不登记（b），每一轮的决定要完全一样。返回 (不一样的地方, 比了多少)。"""
    diffs: list[str] = []
    n: Counter = Counter()
    if len(a.trace) != len(b.trace):
        diffs.append(f"轮数不一样：{len(a.trace)} vs {len(b.trace)}")
    names = ("投递", "通知", "卡片")
    for ta, tb in zip(a.trace, b.trace):
        n["轮"] += 1
        n["投递批次"] += len(ta[2])
        n["投出的消息"] += sum(len(x[2]) for x in ta[2])
        n["通知"] += len(ta[3])
        n["卡片"] += len(ta[4])
        if ta[:2] != tb[:2]:
            diffs.append(f"第 {ta[0]} 轮时间不一样：{ta[1]} vs {tb[1]}")
        for i, name in enumerate(names, start=2):
            if ta[i] != tb[i]:
                diffs.append(f"第 {ta[0]} 轮（{ta[1]}）{name}不一样：\n  乱登记：{ta[i]}\n  不登记：{tb[i]}")
        if len(diffs) > 5:
            break
    return diffs, n


class LongRunTest(unittest.TestCase):
    def test_random_long_run_and_registration_does_not_change_decisions(self):
        """每个种子跑两遍：一遍会话随机登记、还乱登记，一遍完全不登记。两遍都查 6 条不变量；
        两遍每一轮投给每个会话的消息、给主人的每条通知（时间、类型、正文）、每张卡片的运行状态，必须一模一样。"""
        rounds = rounds_from_env()
        for seed in seeds_from_env():
            with self.subTest(seed=seed):
                a = run_one(seed, rounds, "chaos")
                b = run_one(seed, rounds, "none")
                self.assertEqual(a.failures, [], "\n".join(a.failures[:20]))
                self.assertEqual(b.failures, [], "\n".join(b.failures[:20]))
                diffs, n = compare(a, b)
                self.assertEqual(diffs, [], "\n".join(diffs))
                self.assertGreater(n["通知"], 0)
                # 长跑真的覆盖到了：卡住、通知、重投、摘要、留言暂停、乱登记、主人占着时有消息要投都出现过
                for k in ("通知 中断", "通知 恢复", "合法重投", "摘要附上", "摘要没变不附", "进入 stuck", "tell 被拒", "登记",
                          "登记被拒", "登记 done", "inv4 卡片被主人占着的投递决定"):
                    self.assertGreater(a.stats[k], 0, f"seed {seed} 没跑到「{k}」：{dict(a.stats)}")
                self.assertEqual(b.stats["登记"], 0)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    for seed in seeds_from_env():
        a = run_one(seed, rounds_from_env(), "chaos")
        b = run_one(seed, rounds_from_env(), "none")
        diffs, n = compare(a, b)
        print(f"seed {seed}: 乱登记失败 {len(a.failures)}，不登记失败 {len(b.failures)}，两遍不一样的地方 {len(diffs)}")
        for f in (a.failures + b.failures)[:20] + diffs[:5]:
            print("  ", f)
        print("   比了：", json.dumps(dict(n), ensure_ascii=False))
        print("   乱登记：", json.dumps(dict(sorted(a.stats.items())), ensure_ascii=False))
        print("   不登记：", json.dumps(dict(sorted(b.stats.items())), ensure_ascii=False))
    sys.exit(0)
