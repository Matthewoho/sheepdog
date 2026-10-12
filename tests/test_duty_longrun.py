"""在岗表（7.20）/ attention 自动恢复（7.21）的随机长跑：8 个会话（含总线），固定随机种子，默认每个种子 2000 轮，
每一轮随机发生新消息、会话动作、回执、卡住、断网、App 死掉、回执超时、投递失败、主人在会话里说话、tell 留言、
work set / done、压缩、等待、重启、session-reset，然后检查不变量：

1. 同一条消息不会投给会话两次。合法的重投只有一种：它上一次所在的批次被判回执超时（failed，7.21 的重投），
   消息退回排队、再投；判过 acked / acked_late / missed 的批次里的消息不会再投。
2. 消息不丢：每条要投的消息要么已投出，要么在排队，而且排队有原因（会话状态不可投递、在岗表判中断 / 失联、
   这一轮发送失败）；最后把所有会话治好，排队的全部投出去。
3. 每个会话每一段卡住（从会话最后一次动作或 session-reset 算起的一段沉默），给主人的在岗表通知中断最多一条、
   失联最多一条；attention 通知每进一次 attention 最多一条，恢复通知不多于 attention 通知。
4. 卡片和账本、会话记录对得上：被主人占着那一轮一定没投它（总线 failed 时主人在说话是已知例外，单独计数）；
   在做 ⇒ 批次在跑、且从「最后一次动作 / 这一串没交差的批次最早送出」起不到中断阈值；路径判的中断 / 失联 ⇒
   会话真的沉默够久（失联还要记录文件真的不动）；排队数 = 账本里排队的信号数。
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
    """按规格独立算的在岗表指纹：空闲的只看「空闲」，不空闲的看运行状态、空转、名下的活、未读留言。"""
    data = {}
    for d in cards:
        if d["run_state"] == duty.IDLE:
            data[d["topic_id"]] = "idle"
        else:
            data[d["topic_id"]] = [d["run_state"], bool(d["spinning"]),
                                   sorted([duty.norm_key(w["key"]), w["state"]] for w in d["work"]), d["unread_mail"]]
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
    def __init__(self, seed: int, rounds: int):
        self.seed, self.rounds = seed, rounds
        self.rng = random.Random(seed)
        self.w = World(KEYS, real_human_reader=False, fast_db=True)
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
            self.quiet_until = now + self.mins(30, 90)
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
        # 登记 / 收掉活
        if not quiet and self.chance(0.05):
            tid = rng.choice(self.tids)
            open_keys = [x["key"] for x in w.store.open_work(tid) if x["declared"]]
            if open_keys and self.chance(0.4):
                rc, out = self.cli("work", "done", "--topic", tid, "--key", rng.choice(open_keys), "--result", "SIM 做完")
            else:
                state = rng.choice(("doing", "doing", "waiting_other", "waiting_owner", "done", "handed_off"))
                argv = ["work", "set", "--topic", tid, "--key", rng.choice(KEY_POOL), "--title", "SIM 活", "--state", state]
                if state == "handed_off":
                    argv += ["--to", rng.choice([x for x in self.tids if x != tid])]
                rc, out = self.cli(*argv)
            if rc != 0:
                self.fail(f"work 命令失败：{out}")
            self.stats["work"] += 1
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
                self.stats["session-reset"] += 1
        if self.chance(0.001):
            w.restart()
            self.wrap()
            self.stats["重启"] += 1

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
                    extra = {"work": {"key": rng.choice(KEY_POOL), "title": "SIM 回执里的活", "state": "doing"}} \
                        if self.chance(0.1) else {}
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
        for dlv in w.sink.deliveries[self.seen_deliveries:]:
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
            elif why == "queued" and "在岗表" in res["queued"] and info["card"] not in duty.STUCK:
                self.fail(f"不变量 2：按在岗表排队，但决定时卡片是 {info['card']}")
            self.stats[f"排队原因 {why}"] += 1
        # ---- 3. 通知：每一段卡住中断、失联各最多一条 ----
        notices = w.notices()
        for n in notices[self.seen_notices:]:
            what, text = n["what"], n["text"]
            tid = text.split("topic `", 1)[1].split("`", 1)[0]
            if what == "在岗表通知":
                kind = "中断" if "会话中断了" in text.splitlines()[0] else "失联"
                s = w.sessions[tid]
                pend = store.get_topic(tid)["pending_batch_id"]
                if kind == "中断" and pend and pend in self.beh[tid].handled:
                    self.stats["中断通知里：会话其实做完了、只是没交回执（adopted 回执可选）"] += 1
                start = max([x for x in (s.last_action, self.reset_at.get(tid)) if x is not None], default=None)
                self.segments[(tid, kind, start)] += 1
                if self.segments[(tid, kind, start)] > 1:
                    self.fail(f"不变量 3：{tid} 同一段卡住（从 {start} 起）第 {self.segments[(tid, kind, start)]} 条{kind}通知")
                self.stats[f"通知 {kind}"] += 1
            elif what == "attention 通知":
                self.attn_notices[tid] += 1
                self.stats["通知 attention"] += 1
            elif what == "恢复通知":
                self.recover_notices[tid] += 1
                self.stats["通知 恢复"] += 1
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
            if self.recover_notices[tid] > self.attn_notices[tid]:
                self.fail(f"不变量 3：{tid} 恢复通知比 attention 通知多")
        # ---- 4. 卡片和账本、会话记录对得上 ----
        for tid in self.tids:
            self.stats["inv4 检查"] += 1
            t, c, s = store.get_topic(tid), store.get_card(tid), w.sessions[tid]
            info = self.decisions.get(tid)
            owner_recent = s.owner_spoke is not None and now - s.owner_spoke < timedelta(
                minutes=cfg.session.human_attach_minutes)
            if info is not None and info["sent"]:
                if info["state"] == sm.HUMAN_ATTACHED:
                    self.fail(f"不变量 4：{tid} 状态是被主人占着，这一轮却投了")
                if owner_recent:  # 按真相：主人 15 分钟内在会话里说过话
                    if info["state"] == sm.FAILED:
                        self.stats["已知例外：failed 时主人在说话照样重投"] += 1
                    else:
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
        # 主人在会话里说话：拿真读法（AgentApiSink 整份读合成的记录）和真相对一遍；前 2000 轮每 50 轮一次，之后每 1000 轮
        if self.w.round_no % (50 if self.w.round_no <= 2000 else 1000) == 0:
            from sheepdog.sink.agentapi import AgentApiSink
            for tid in self.tids:
                s = w.sessions[tid]
                real = AgentApiSink().last_human_activity(s.cid)
                if (real is None) != (s.owner_spoke is None) or (real is not None and real != s.owner_spoke):
                    self.fail(f"主人说话的真读法和真相不一致：{tid} {real} vs {s.owner_spoke}")

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
        for i in range(400):
            for tid in self.tids:
                t = w.store.get_topic(tid)
                if t["state"] == sm.ATTENTION and t["attention_reason"] != "receipt_timeout":
                    self.cli("session-reset", "--topic", tid)
                    self.reset_at[tid] = w.now
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
            if pending == 0 and not busy:
                break
        left = w.store.conn.execute(
            "SELECT message_id, topic_id, dispatch_state FROM messages WHERE route='dispatch' AND dispatch_state NOT IN "
            "('delivered','acked','missed')").fetchall()
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


class LongRunTest(unittest.TestCase):
    def test_random_long_run(self):
        rounds = rounds_from_env()
        for seed in seeds_from_env():
            with self.subTest(seed=seed):
                sim = LongRun(seed, rounds)
                try:
                    stats = sim.run()
                finally:
                    sim.close()
                self.assertEqual(sim.failures, [], "\n".join(sim.failures[:20]))
                # 长跑真的覆盖到了：卡住、通知、重投、摘要、留言暂停都出现过
                for k in ("通知 中断", "合法重投", "摘要附上", "摘要没变不附", "进入 stuck", "tell 被拒"):
                    self.assertGreater(stats[k], 0, f"seed {seed} 没跑到「{k}」：{dict(stats)}")


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    for seed in seeds_from_env():
        sim = LongRun(seed, rounds_from_env())
        try:
            st = sim.run()
        finally:
            sim.close()
        print(f"seed {seed}: 失败 {len(sim.failures)}")
        for f in sim.failures[:20]:
            print("  ", f)
        print("  ", json.dumps(dict(sorted(st.items())), ensure_ascii=False))
    sys.exit(0)
