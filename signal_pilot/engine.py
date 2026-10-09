"""引擎：Collector（拉取 + 路由 + 编辑/撤回处理）与 Dispatcher（话题 session + 状态机 + 回执）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import session as sm
from .config import Config
from .models import Message
from .prompts import batch_prompt, bootstrap_prompt
from .router import DISPATCH, RouteContext, route
from .sink import Sink, SinkError
from .source import Source
from .store import Store, now_iso

log = logging.getLogger("signal-pilot")

BUS_TOPIC_ID = "tp_bus"
MUTED_REFRESH_MINUTES = 30
MAX_RETRIES = 3


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


# ======================= Collector =======================
class Collector:
    def __init__(self, cfg: Config, store: Store, source: Source):
        self.cfg = cfg
        self.store = store
        self.source = source
        self._sender_cache: dict[str, str] = {}

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
                d = route(m, ctx, self.cfg.routing)
                self.store.upsert_message(m, d.route, d.reason, d.tags)
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
        if old_route == DISPATCH and old_state in ("delivered", "acked"):
            # 已推送过：通知 session（撤回附原文，编辑附新内容）
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
            d = route(m, ctx, self.cfg.routing)
            if d.route == DISPATCH and not recalled:
                self.store.update_content(m.message_id, m, DISPATCH, d.reason, "pending")
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
    def __init__(self, cfg: Config, store: Store, sink: Sink):
        self.cfg = cfg
        self.store = store
        self.sink = sink

    @property
    def bus_title(self) -> str:
        return f"{self.cfg.session.title_prefix} {self.cfg.session.bus_title}".strip()

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

    # ---------- 回执与超时 ----------
    def _check_receipt(self, t) -> None:
        batch = t["pending_batch_id"]
        if not batch or t["state"] != sm.RUNNING:
            return
        p = self.receipt_path(t["topic_id"], batch)
        if p.exists():
            receipt = json.loads(p.read_text(encoding="utf-8"))
            status = receipt.get("status", "handled")
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
            self._fail(t, batch, "回执超时")

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

    # ---------- 主流程 ----------
    def dispatch_once(self) -> dict:
        t = self.ensure_bus_topic()
        self._check_receipt(t)
        t = self.store.get_topic(BUS_TOPIC_ID)
        self._check_human(t)
        t = self.store.get_topic(BUS_TOPIC_ID)

        rows = self.store.pending_dispatch()
        result = {"pending": len(rows), "sent": 0, "state": t["state"]}
        if not rows:
            return result
        if not (t["state"] in sm.DISPATCHABLE or t["state"] == sm.FAILED):
            log.info("topic %s 状态 %s，%d 条信号排队中", t["topic_id"], t["state"], len(rows))
            return result

        try:
            if not t["conversation_id"]:
                cid = self.sink.new_conversation(
                    t["title"], bootstrap_prompt(t["title"], t["duty"], t["topic_id"], self.cfg.prompt_overlay()),
                    self.cfg.session.model,
                )
                self.store.update_topic(t["topic_id"], conversation_id=cid)
                t = self.store.get_topic(t["topic_id"])
                log.info("新建 session %s -> %s", t["title"], cid)

            batch_id = "b" + _now().strftime("%Y%m%d%H%M%S")
            waiting = t["summary"] if t["state"] == sm.WAITING_HUMAN else ""
            prompt = batch_prompt(t["topic_id"], batch_id, rows, self.store.inbox_summary(), waiting)
            self.sink.send_message(t["conversation_id"], prompt)
        except SinkError as e:
            self._fail(t, None, str(e))
            result["error"] = str(e)
            return result

        ids = [r["message_id"] for r in rows]
        self.store.record_dispatch(batch_id, t["topic_id"], ids)
        self.store.mark_batch(ids, batch_id, t["topic_id"], "delivered")
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                pending_batch_id=batch_id, dispatched_at=now_iso())
        result.update(sent=len(ids), batch_id=batch_id, state=sm.RUNNING)
        return result


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
