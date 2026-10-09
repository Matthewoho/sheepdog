"""引擎：Collector（拉取 + 路由 + 聊天归属 + 编辑/撤回处理）与 Dispatcher（名册同步 + 按 topic 投递 + 状态机 + 回执）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import session as sm
from .config import Config
from .models import Message
from .prompts import batch_prompt, bootstrap_prompt, onboarding_prompt, roster_section
from .roster import Roster
from .router import DISPATCH, SELF, RouteContext, RouteDecision, route
from .sink import Sink, SinkError
from .source import Source
from .store import Store, now_iso

log = logging.getLogger("sheepdog")

BUS_TOPIC_ID = "tp_bus"
ADOPTED = "adopted"
KNOWN_KIND = "known"
MUTED_REFRESH_MINUTES = 30
MAX_RETRIES = 3


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


# ======================= 聊天归属 =======================
def apply_ownership(msg: Message, d: RouteDecision, roster: Roster | None,
                    ignore_chat_ids: list[str]) -> tuple[RouteDecision, str | None]:
    """路由之后按名册判断归属（规格第 3 节），返回 (决策, topic_id)。reason 不变，归属写进 tags。

    - 自己发的、ignore_chat_ids：不变
    - managed 会话的聊天 + all_messages：推给它（覆盖 inbox 和免打扰 drop）
    - managed 会话的聊天 + dispatch 级：推给它
    - 其他 dispatch 级：总线
    - 其他：不变（inbox / drop）
    """
    if d.route == SELF or msg.chat_id in ignore_chat_ids:
        return d, None
    owner = roster.owner_of(msg.chat_id) if roster else None
    if owner:
        sess, chat = owner
        if chat.all_messages or d.route == DISPATCH:
            return RouteDecision(DISPATCH, d.reason, [*d.tags, f"owner:{sess.key}"]), sess.topic_id
    if d.route == DISPATCH:
        return d, BUS_TOPIC_ID
    return d, None


# ======================= Collector =======================
class Collector:
    def __init__(self, cfg: Config, store: Store, source: Source, roster: Roster | None = None):
        self.cfg = cfg
        self.store = store
        self.source = source
        self.roster = roster
        self._sender_cache: dict[str, str] = {}

    def _decide(self, m: Message, ctx: RouteContext) -> tuple[RouteDecision, str | None]:
        return apply_ownership(m, route(m, ctx, self.cfg.routing), self.roster, self.cfg.routing.ignore_chat_ids)

    def _muted(self) -> set[str]:
        at = _parse(self.store.get_meta("muted_at"))
        if at and _now() - at < timedelta(minutes=MUTED_REFRESH_MINUTES):
            return set(json.loads(self.store.get_meta("muted_ids", "[]")))
        try:
            ids = self.source.muted_chat_ids()
        except Exception as e:  # 免打扰列表失败不阻塞主流程
            log.warning("获取免打扰群失败，沿用缓存: %s", e)
            return set(json.loads(self.store.get_meta("muted_ids", "[]")))
        self.store.set_meta("muted_ids", json.dumps(sorted(ids)))
        self.store.set_meta("muted_at", now_iso())
        return ids

    def _is_my_message(self, message_id: str) -> bool:
        me = self.cfg.self_open_id
        if not me or not message_id:
            return False
        if self.store.is_my_message(message_id, me):
            return True
        if message_id not in self._sender_cache:
            try:
                self._sender_cache.update(self.source.senders_of([message_id]))
            except Exception as e:
                log.warning("查询被回复消息发送人失败: %s", e)
                self._sender_cache[message_id] = ""
        return self._sender_cache.get(message_id) == me

    def poll_once(self) -> dict:
        started = _now()
        wm = _parse(self.store.get_meta("watermark"))
        if wm:
            start = wm - timedelta(seconds=self.cfg.overlap_seconds)
        else:
            start = started - timedelta(minutes=self.cfg.initial_lookback_minutes)
        msgs = self.source.fetch_since(start.isoformat(timespec="seconds"))
        ctx = RouteContext(self.cfg.self_open_id, self._muted(), self._is_my_message)
        stats = {"fetched": len(msgs), "new": 0, "changed": 0, "dispatch": 0, "inbox": 0, "drop": 0, "self": 0}

        # 先入账自己发的消息，保证同批次里「回复我」能判定
        msgs.sort(key=lambda m: (0 if m.sender_id == self.cfg.self_open_id else 1, m.create_time))
        for m in msgs:
            existing = self.store.get_message(m.message_id)
            if existing is None:
                d, topic_id = self._decide(m, ctx)
                self.store.upsert_message(m, d.route, d.reason, d.tags, topic_id)
                stats["new"] += 1
                stats[d.route] = stats.get(d.route, 0) + 1
            else:
                if self._handle_change(existing, m, ctx):
                    stats["changed"] += 1
                else:
                    self.store.touch(m.message_id)

        self._tag_read_status()
        self.store.set_meta("watermark", started.isoformat(timespec="seconds"))
        self.store.set_meta("last_poll", now_iso())
        return stats

    def _handle_change(self, old, m: Message, ctx: RouteContext) -> bool:
        """编辑 / 撤回：按原消息的路由级别处理（设计稿 2.2.1）。返回是否有变化。"""
        content_changed = (old["content"] or "") != (m.content or "")
        recalled = m.deleted and not old["deleted"]
        if not content_changed and not recalled:
            return False
        old_route, old_state = old["route"], old["dispatch_state"]
        if old_route == DISPATCH and old_state in ("delivered", "acked", "missed"):
            # 已推送过：通知原来那个 session（topic_id 不变；撤回附原文，编辑附新内容）
            if recalled:
                m.content = f"（已撤回）原文：{old['content']}"
            else:
                m.content = f"（已编辑）旧：{old['content']}\n新：{m.content}"
            self.store.update_content(m.message_id, m, DISPATCH, "recalled" if recalled else "edited", "pending")
        elif old_route == DISPATCH:
            # 尚未投递：直接更新内容
            self.store.update_content(m.message_id, m)
        else:
            # Inbox / drop：重新路由，若升级为直推则投递
            d, topic_id = self._decide(m, ctx)
            if d.route == DISPATCH and not recalled:
                self.store.update_content(m.message_id, m, DISPATCH, d.reason, "pending", d.tags, topic_id)
            else:
                self.store.update_content(m.message_id, m)
        return True

    def _tag_read_status(self) -> None:
        ids = [r["message_id"] for r in self.store.pending_dispatch()] + self.store.unread_inbox_ids()
        if not ids:
            return
        try:
            self.store.set_read(self.source.read_status(ids))
        except Exception as e:  # 已读只是标签，失败不影响路由
            log.warning("查询已读状态失败: %s", e)


# ======================= Dispatcher =======================
class Dispatcher:
    def __init__(self, cfg: Config, store: Store, sink: Sink, roster: Roster | None = None):
        self.cfg = cfg
        self.store = store
        self.sink = sink
        self.roster = roster or Roster()

    @property
    def bus_title(self) -> str:
        return f"{self.cfg.session.title_prefix} {self.cfg.session.bus_title}".strip()

    @property
    def reply_prefix(self) -> str:
        return self.cfg.session.reply_prefix or ""

    def ensure_bus_topic(self):
        t = self.store.get_topic(BUS_TOPIC_ID)
        if t is None:
            duty = ("接收主人在 IM 中收到的所有直推信号（私聊、@我、@所有人、回复我、关键词），"
                    "判断是否为需要跟进的工作，整理要点、起草回复、提出需要主人决策的问题。"
                    "不负责：对外发送消息、审批、生产写操作。")
            self.store.create_topic(BUS_TOPIC_ID, self.bus_title, "bus", duty, sm.ACTIVE)
            t = self.store.get_topic(BUS_TOPIC_ID)
        return t

    def receipt_path(self, topic_id: str, batch_id: str) -> Path:
        return self.cfg.receipts_dir / topic_id / f"{batch_id}.json"

    # ---------- 名册同步（规格第 2 节） ----------
    def sync_roster(self) -> dict:
        """把名册 upsert 进 topics 表；名册里删掉的条目置 closed（不删数据）；名册变化时标记通知总线。"""
        report: dict[str, list[str]] = {"created": [], "updated": [], "reopened": [], "closed": []}
        live: set[str] = set()
        for s in self.roster.sessions:
            live.add(s.topic_id)
            t = self.store.get_topic(s.topic_id)
            if t is None:
                self.store.create_topic(s.topic_id, s.display_title, s.kind, s.duty, sm.ACTIVE)
                t = self.store.get_topic(s.topic_id)
                report["created"].append(s.topic_id)
            chat_ids = [c.chat_id for c in s.chats]
            # 聊天锚点以名册为准；回执带来的其他锚点（issue:/service: 等）保留
            keep = [a for a in json.loads(t["anchors_json"] or "[]") if not a.startswith("oc_") and a not in chat_ids]
            fields = {
                "title": s.display_title, "kind": s.kind, "duty": s.duty,
                "conversation_id": s.conversation_id or None,
                "anchors_json": json.dumps(chat_ids + sorted(set(keep)), ensure_ascii=False),
            }
            if t["conversation_id"] and t["conversation_id"] != fields["conversation_id"]:
                # 换了会话：新会话没收过 onboarding，旧批次的回执也不再等
                fields.update(onboarded_at=None, pending_batch_id=None)
                if t["state"] == sm.RUNNING:
                    fields["state"] = sm.ACTIVE
            if t["state"] == sm.CLOSED:
                # 名册是 adopted/known 会话存续的唯一依据：在名册里就不是 closed
                fields["state"] = sm.ACTIVE
                report["reopened"].append(s.topic_id)
            changed = {k: v for k, v in fields.items() if t[k] != v}
            if changed:
                self.store.update_topic(s.topic_id, **changed)
                if s.topic_id not in report["created"]:
                    report["updated"].append(s.topic_id)
        for t in self.store.list_topics():
            if t["kind"] in (ADOPTED, KNOWN_KIND) and t["topic_id"] not in live and t["state"] != sm.CLOSED:
                self.store.update_topic(t["topic_id"], state=sm.CLOSED, pending_batch_id=None)
                report["closed"].append(t["topic_id"])
                log.info("topic %s 已从名册移除，置为 closed", t["topic_id"])
        h = self.roster.content_hash()
        prev = self.store.get_meta("roster_hash")
        if prev != h:
            self.store.set_meta("roster_hash", h)
            # 从没有名册到仍然没有名册不算变化
            if prev or self.roster.sessions:
                self.store.set_meta("roster_notify_bus", "1")
        return report

    # ---------- 回执与超时 ----------
    def _check_receipt(self, t) -> None:
        batch = t["pending_batch_id"]
        if not batch or t["state"] != sm.RUNNING:
            return
        adopted = t["kind"] == ADOPTED
        p = self.receipt_path(t["topic_id"], batch)
        if p.exists():
            receipt = json.loads(p.read_text(encoding="utf-8"))
            status = receipt.get("status", "handled")
            # adopted 会话由名册决定存续，回执 done 不关会话，按 handled 处理
            if adopted and status == "done":
                status = "handled"
            event = sm.RECEIPT_EVENTS.get(status, "receipt_handled")
            new_state = sm.transition(t["state"], event)
            self.store.finish_dispatch(batch, "acked", json.dumps(receipt, ensure_ascii=False))
            self.store.mark_batch_state(batch, "acked")
            fields = {"state": new_state, "pending_batch_id": None, "retries": 0,
                      "summary": receipt.get("summary", t["summary"])}
            if receipt.get("anchors"):
                anchors = sorted(set(json.loads(t["anchors_json"] or "[]")) | set(receipt["anchors"]))
                fields["anchors_json"] = json.dumps(anchors, ensure_ascii=False)
            self.store.update_topic(t["topic_id"], **fields)
            log.info("回执 %s/%s: %s -> %s", t["topic_id"], batch, status, new_state)
            return
        sent = _parse(t["dispatched_at"])
        if sent and _now() - sent > timedelta(minutes=self.cfg.session.receipt_timeout_minutes):
            if adopted:
                self._miss(t, batch)
            else:
                self._fail(t, batch, "回执超时")

    def _miss(self, t, batch: str) -> None:
        """adopted 会话回执超时：只记一次未回执、回到可投递，不重投（避免往大上下文里重复灌消息）。"""
        self.store.finish_dispatch(batch, "missed", error="回执超时（adopted 不重投）")
        self.store.mark_batch_state(batch, "missed")
        missed = (t["receipt_missed"] or 0) + 1
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "receipt_missed"),
                                pending_batch_id=None, receipt_missed=missed)
        log.info("topic %s 批次 %s 未回执（累计 %d 次），不重投", t["topic_id"], batch, missed)

    def _fail(self, t, batch: str | None, error: str) -> None:
        retries = (t["retries"] or 0) + 1
        state = sm.transition(t["state"], "error") if sm.can(t["state"], "error") else t["state"]
        if batch:
            self.store.finish_dispatch(batch, "failed", error=error)
            self.store.requeue_batch(batch)
        if retries >= MAX_RETRIES and sm.can(state, "retries_exhausted"):
            state = sm.transition(state, "retries_exhausted")
        self.store.update_topic(t["topic_id"], state=state, pending_batch_id=None, retries=retries)
        log.warning("topic %s 投递失败(%s)，第 %d 次，状态 %s", t["topic_id"], error, retries, state)

    # ---------- 人类接管 ----------
    def _check_human(self, t) -> None:
        if not t["conversation_id"]:
            return
        last = self.sink.last_human_activity(t["conversation_id"])
        recent = bool(last and _now() - last < timedelta(minutes=self.cfg.session.human_attach_minutes))
        if recent and sm.can(t["state"], "human_attach") and t["state"] != sm.RUNNING:
            self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "human_attach"))
            log.info("topic %s: 主人接管，暂停投递", t["topic_id"])
        elif not recent and t["state"] == sm.HUMAN_ATTACHED:
            self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "human_detach"))
            log.info("topic %s: 主人离开，恢复投递", t["topic_id"])

    # ---------- 投递 ----------
    def _new_batch_id(self, prefix: str, topic_id: str) -> str:
        # 同一轮会给多个 topic 发批次，批次号带上 topic key，避免 dispatches 主键冲突
        base = prefix + _now().strftime("%Y%m%d%H%M%S")
        if topic_id != BUS_TOPIC_ID:
            base += "-" + topic_id.removeprefix("tp_")
        bid, n = base, 1
        while self.store.dispatch_exists(bid):
            n += 1
            bid = f"{base}-{n}"
        return bid

    @staticmethod
    def _deliverable(t) -> bool:
        return t["state"] in sm.DISPATCHABLE or t["state"] == sm.FAILED

    def _ensure_bus_conversation(self, t):
        """总线没有会话就新建（adopted 会话永远不走这里）。bootstrap 已含完整名册，名册更新标记随之清掉。"""
        if t["conversation_id"]:
            return t
        prompt = bootstrap_prompt(t["title"], t["duty"], t["topic_id"], self.cfg.prompt_overlay(),
                                  roster_section(self.roster), self.reply_prefix)
        cid = self.sink.new_conversation(t["title"], prompt, self.cfg.session.model)
        self.store.update_topic(t["topic_id"], conversation_id=cid)
        self.store.set_meta("roster_notify_bus", "0")
        log.info("新建 session %s -> %s", t["title"], cid)
        return self.store.get_topic(t["topic_id"])

    def send_onboarding(self, t) -> str:
        """给 adopted 会话发登记通知（只发一次）。作为一个不含消息的批次，等回执或超时后再投消息。"""
        s = self.roster.by_topic(t["topic_id"])
        if s is None:
            raise SinkError(f"{t['topic_id']} 不在名册里，不能 onboarding")
        bid = self._new_batch_id("o", t["topic_id"])
        self.sink.send_message(t["conversation_id"], onboarding_prompt(s, bid, self.reply_prefix))
        ts = now_iso()
        self.store.record_dispatch(bid, t["topic_id"], [])
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                pending_batch_id=bid, dispatched_at=ts, onboarded_at=ts)
        log.info("topic %s: 已发 onboarding %s", t["topic_id"], bid)
        return bid

    def _dispatch_topic(self, t, rows) -> dict:
        res = {"pending": len(rows), "sent": 0, "state": t["state"]}
        if not self._deliverable(t):
            log.info("topic %s 状态 %s，%d 条信号排队中", t["topic_id"], t["state"], len(rows))
            return res
        adopted = t["kind"] == ADOPTED
        roster_update = ""
        try:
            if adopted and not t["onboarded_at"]:
                # 第一次收消息前先发 onboarding；消息等它回执或超时后下一轮再投
                res.update(onboarding=self.send_onboarding(t), state=sm.RUNNING)
                return res
            if not adopted:
                t = self._ensure_bus_conversation(t)
                if self.store.get_meta("roster_notify_bus") == "1":
                    roster_update = (roster_section(self.roster)
                                     or "名册已清空：当前没有登记的会话，所有信号由你自己处理。")
            batch_id = self._new_batch_id("b", t["topic_id"])
            waiting = t["summary"] if t["state"] == sm.WAITING_HUMAN else ""
            # Inbox 摘要只给总线；adopted 会话只管自己的聊天
            inbox = [] if adopted else self.store.inbox_summary()
            prompt = batch_prompt(t["topic_id"], batch_id, rows, inbox, waiting, self.reply_prefix,
                                  receipt_optional=adopted, roster_update=roster_update)
            self.sink.send_message(t["conversation_id"], prompt)
        except SinkError as e:
            self._fail(t, None, str(e))
            res["error"] = str(e)
            return res

        if roster_update:
            self.store.set_meta("roster_notify_bus", "0")
        ids = [r["message_id"] for r in rows]
        self.store.record_dispatch(batch_id, t["topic_id"], ids)
        self.store.mark_batch(ids, batch_id, t["topic_id"], "delivered")
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                pending_batch_id=batch_id, dispatched_at=now_iso())
        res.update(sent=len(ids), batch_id=batch_id, state=sm.RUNNING)
        return res

    # ---------- 主流程 ----------
    def dispatch_once(self) -> dict:
        self.sync_roster()
        self.ensure_bus_topic()
        tids = [BUS_TOPIC_ID] + [t["topic_id"] for t in self.store.list_topics()
                                 if t["kind"] == ADOPTED and t["state"] != sm.CLOSED]
        # 先处理回执与超时（总线超时会把批次退回待推），再取待推消息
        for tid in tids:
            self._check_receipt(self.store.get_topic(tid))

        rows = self.store.pending_dispatch()
        groups: dict[str, list] = {}
        for r in rows:
            tid = r["topic_id"] or BUS_TOPIC_ID
            if tid not in tids:
                # 归属的会话已从名册移除或改成 known：退回总线，不丢消息
                log.warning("消息 %s 归属的 %s 已不可投递，改投总线", r["message_id"], tid)
                tid = BUS_TOPIC_ID
            groups.setdefault(tid, []).append(r)

        result: dict = {"pending": len(rows), "sent": 0, "topics": {}}
        for tid in tids:
            t = self.store.get_topic(tid)
            # 读 transcript 有成本：adopted 会话只在有待推消息或处于接管中时检查
            if tid == BUS_TOPIC_ID or tid in groups or t["state"] == sm.HUMAN_ATTACHED:
                self._check_human(t)
            if tid in groups:
                r = self._dispatch_topic(self.store.get_topic(tid), groups[tid])
                result["topics"][tid] = r
                result["sent"] += r["sent"]
        result["state"] = self.store.get_topic(BUS_TOPIC_ID)["state"]
        return result

    def init(self) -> dict:
        """sheepdog init：同步名册；没有总线就建；给未 onboarding 的 managed 会话发 onboarding。可重复执行。"""
        report: dict = {"roster": self.sync_roster(), "onboarding": {}}
        bus = self.ensure_bus_topic()
        if bus["conversation_id"]:
            report["bus"] = f"已存在 {bus['conversation_id']}"
        else:
            try:
                bus = self._ensure_bus_conversation(bus)
                report["bus"] = f"已新建 {bus['conversation_id']}"
            except SinkError as e:
                report["bus"] = f"新建失败: {e}"
        for s in self.roster.managed:
            t = self.store.get_topic(s.topic_id)
            if t["onboarded_at"]:
                report["onboarding"][s.topic_id] = f"跳过：已于 {t['onboarded_at']} onboarding"
                continue
            self._check_human(t)
            t = self.store.get_topic(s.topic_id)
            if not self._deliverable(t):
                report["onboarding"][s.topic_id] = f"排队：状态 {t['state']}，首批消息到达时再发"
                continue
            try:
                report["onboarding"][s.topic_id] = f"已发送 {self.send_onboarding(t)}"
            except SinkError as e:
                report["onboarding"][s.topic_id] = f"发送失败: {e}"
        return report


def forward_messages(store: Store, topic_id: str, message_ids: list[str], note: str = "") -> list[str]:
    """把消息转交给 managed 会话（规格第 6 节）。只改归属并回到待推，真正投递由 Dispatcher 下一轮完成，
    因此同样受人类接管、onboarding 约束。目标是总线、known、已移除或不存在时抛 ValueError。"""
    if topic_id == BUS_TOPIC_ID:
        raise ValueError("不能转交给总线 tp_bus：forward 只用于把消息交给名册里的 managed 会话")
    t = store.get_topic(topic_id)
    if t is None:
        raise ValueError(f"topic {topic_id} 不存在：先确认名册里有它，并已同步（sheepdog init 或 run）")
    if t["kind"] == KNOWN_KIND:
        raise ValueError(f"{topic_id} 是 known 会话，只登记不投递；请在回执里建议主人去该会话处理")
    if t["kind"] != ADOPTED:
        raise ValueError(f"{topic_id} 不是名册里的 managed 会话（kind={t['kind']}）")
    if t["state"] == sm.CLOSED:
        raise ValueError(f"{topic_id} 已从名册移除（closed），不能转交")
    ids = list(dict.fromkeys(i.strip() for i in message_ids if i.strip()))
    if not ids:
        raise ValueError("没有 message_id")
    rows = {i: store.get_message(i) for i in ids}
    missing = [i for i, r in rows.items() if r is None]
    if missing:
        raise ValueError(f"账本里没有这些消息: {missing}")
    for i, row in rows.items():
        src = row["topic_id"] or BUS_TOPIC_ID
        tags = [x for x in json.loads(row["tags_json"] or "[]") if not x.startswith(("owner:", "forwarded_from:"))]
        tags += [f"owner:{topic_id.removeprefix('tp_')}", f"forwarded_from:{src}"]
        store.forward_message(i, topic_id, tags, note)
    log.info("转交 %d 条消息 -> %s", len(ids), topic_id)
    return ids


def write_receipt(cfg: Config, topic_id: str, batch_id: str, receipt: dict) -> Path:
    status = receipt.get("status")
    if status not in sm.RECEIPT_EVENTS:
        raise ValueError(f"status 必须是 {sorted(sm.RECEIPT_EVENTS)} 之一，收到 {status!r}")
    d = cfg.receipts_dir / topic_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{batch_id}.json"
    receipt = {**receipt, "received_at": now_iso()}
    p.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
