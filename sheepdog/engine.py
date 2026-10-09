"""引擎：Collector（拉取 + 路由 + 聊天归属 + 编辑/撤回处理）与 Dispatcher（名册同步 + 按 topic 投递 + 状态机 + 回执）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import session as sm
from .config import Config
from .models import Message
from .playbook import Playbook
from .prompts import (adopted_bootstrap, batch_prompt, bootstrap_prompt, onboarding_prompt, relay_content,
                      retirement_prompt, roster_table, watch_message)
from .roster import Roster
from .security import HOLD, NONE, SecurityConfig, quote_verified
from .router import DISPATCH, DROP, SELF, RouteContext, RouteDecision, message_bodies, route
from .sink import Sink, SinkError
from .source import Source
from .sink.agentapi import conversation_dir, transcript_path
from .store import SYSTEM_ID_PREFIX, Store, now_iso, row_to_message

log = logging.getLogger("sheepdog")

BUS_TOPIC_ID = "tp_bus"
ADOPTED = "adopted"
KNOWN_KIND = "known"
RETIRED = "retired"
# 总线职责写在 playbook 的 bus.md 里（7.6），topics 表只存指向
BUS_DUTY = "见 playbook/bus.md"
# watches 表的两个提醒时间列，按 watch.remind_minutes 的第 1、2 档使用（列名沿用规格）
WATCH_SLOTS = ("nudged_15_at", "nudged_30_at")
# 安全闸（7.7）在 messages.tags 里留的标记
SECURITY_HOLD_TAG = "security_hold"
# hold 消息凭已核对的主人原话转交后打上，投递时不再被拉回总线
SECURITY_RELEASE_TAG = "security_release:quote"
# 主人在 IM 上的回复（7.8）：发送人已由 IM 账号核实；escalation:<提问消息 id> 记它回答的是哪条「需要你定」
OWNER_VERIFIED_TAG = "owner_verified"
ESCALATION_TAG_PREFIX = "escalation:"


def open_escalations(store: Store, open_hours: float, topic_id: str | None = None) -> list:
    """未结的「需要你定」：没关闭、且提问时间在 open_hours 内（超时的即使还没被每轮检查关掉也不算）。"""
    cutoff = _now() - timedelta(hours=open_hours)
    out = []
    for e in store.open_escalations(topic_id):
        asked = _parse(e["asked_at"])
        if asked is None or asked.tzinfo is None or asked >= cutoff:
            out.append(e)
    return out


def escalation_summary(e, limit: int = 120) -> str:
    text = " ".join((e["text"] or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def escalation_note(e) -> str:
    return f"回复的问题（topic `{e['topic_id']}`，{e['asked_at']}，message_id `{e['message_id']}`）：{escalation_summary(e)}"


def predecessor_topic_id(topic_id: str) -> str:
    # 「.」不在名册 key 允许的字符里，不会和真实条目撞名
    return topic_id + ".prev"
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
    if d.route == SELF or d.reason == "self_escalation" or msg.chat_id in ignore_chat_ids:
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
    def __init__(self, cfg: Config, store: Store, source: Source, roster: Roster | None = None,
                 security: SecurityConfig | None = None, acker=None):
        self.cfg = cfg
        self.store = store
        self.source = source
        self.roster = roster
        self.security = security
        # 确认表情（7.9）：入账主人自己发的消息时撤下已点的表情
        self.acker = acker
        self._sender_cache: dict[str, str] = {}

    def _decide(self, m: Message, ctx: RouteContext):
        """路由 → 名册归属 → 等待归属。返回 (决策, topic_id, 命中的等待)；等待由调用方在安全闸之后关闭。"""
        d, topic_id = apply_ownership(m, route(m, ctx, self.cfg.routing), self.roster, self.cfg.routing.ignore_chat_ids)
        # 等待中的回复优先归给登记等待的 topic（7.4），覆盖聊天归属、总线、Inbox、免打扰；
        # 自己发的、升级私聊、ignore_chat_ids 不受影响
        if d.route == SELF or d.reason == "self_escalation" or m.chat_id in self.cfg.routing.ignore_chat_ids:
            return d, topic_id, None
        w = self.store.match_watch(m.sender_id, m.chat_id)
        if w:
            return RouteDecision(DISPATCH, d.reason, [*d.tags, f"watch:{w['id']}"]), w["topic_id"], w
        return d, topic_id, None

    def _secure(self, message_id: str) -> str:
        """安全闸（7.7）：对 dispatch 级消息跑规则，结果写进 security_tags / security_action。

        hold：改投总线（覆盖名册、等待、all_messages），tags 加 security_hold，去掉等待标记。返回动作。
        """
        if self.security is None:
            return NONE
        row = self.store.get_message(message_id)
        if row is None or row["route"] != DISPATCH:
            return NONE
        if self.cfg.self_open_id and row["sender_id"] == self.cfg.self_open_id:
            return NONE  # 主人本人的消息不走安全闸（7.8）
        names, action = self.security.evaluate(row_to_message(row))
        tags = json.loads(row["tags_json"] or "[]")
        topic_id = row["topic_id"]
        if action == HOLD:
            topic_id = BUS_TOPIC_ID
            tags = [t for t in tags if not t.startswith("watch:") and t != SECURITY_RELEASE_TAG]
            if SECURITY_HOLD_TAG not in tags:
                tags.append(SECURITY_HOLD_TAG)
        self.store.set_security(message_id, names, action, topic_id, tags)
        if action != NONE:
            log.warning("安全规则命中 %s: %s -> %s（%s）", message_id, ",".join(names), action, topic_id)
        return action

    def _close_watch(self, w, message_id: str, action: str) -> None:
        # 被 hold 的回复没到等待的会话手里：等待不关，照常提醒 / 到期
        if w is None or action == HOLD:
            return
        self.store.close_watch(w["id"], "replied")
        log.info("等待 #%s 收到回复 %s -> %s", w["id"], message_id, w["topic_id"])

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

        # 先入账「找主人」聊天里机器人的提问（同批次里主人的回复才找得到它），再入账自己发的消息
        # （保证同批次里「回复我」能判定），最后其他
        esc_chats = set(self.cfg.escalation.chat_ids)
        me = self.cfg.self_open_id
        msgs.sort(key=lambda m: (0 if m.chat_id in esc_chats and m.sender_id != me else 1 if m.sender_id == me else 2,
                                 m.create_time))
        for m in msgs:
            existing = self.store.get_message(m.message_id)
            if existing is None and me and m.sender_id == me:
                self._own_message(m)
            if existing is None and m.chat_id in esc_chats:
                d = self._escalation_message(m)
                stats["new"] += 1
                stats[d.route] = stats.get(d.route, 0) + 1
            elif existing is None:
                d, topic_id, w = self._decide(m, ctx)
                self.store.upsert_message(m, d.route, d.reason, d.tags, topic_id)
                if d.route == DISPATCH:
                    self._close_watch(w, m.message_id, self._secure(m.message_id))
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

    def _own_message(self, m: Message) -> None:
        """主人自己发的新消息（含会话以主人身份代回的）：撤下它回应了的确认表情。失败不影响入账。"""
        if self.acker is None:
            return
        try:
            self.acker.on_own_message(m)
        except Exception as e:
            log.warning("处理确认表情失败 %s: %s", m.message_id, e)

    # ---------- 「找主人」聊天（7.8） ----------
    def _escalation_message(self, m: Message) -> RouteDecision:
        """先于普通路由，不受 ignore_chat_ids 影响。三种结果：登记提问 / 主人的回复 / 其他一律 drop。"""
        esc = self.cfg.escalation
        me = self.cfg.self_open_id
        if me and m.sender_id == me:
            return self._owner_reply(m)
        if m.sender_type in ("app", "bot") and esc.pattern is not None:
            topic = next((mt.group("topic") for b in message_bodies(m.content)
                          if (mt := esc.pattern.search(b)) and mt.group("topic")), "")
            if topic:
                d = RouteDecision(DROP, "escalation_asked")
                self.store.upsert_message(m, d.route, d.reason, [f"escalation_topic:{topic}"])
                if self._escalation_target_ok(topic):
                    self.store.add_escalation(m.message_id, topic, m.chat_id, m.content, m.create_time or now_iso())
                    log.info("登记「需要你定」%s -> %s", m.message_id, topic)
                else:
                    log.warning("「需要你定」%s 的 topic %s 不在名册里（或已关闭），只记日志", m.message_id, topic)
                return d
        d = RouteDecision(DROP, "escalation_chat_other")
        self.store.upsert_message(m, d.route, d.reason, [])
        return d

    def _escalation_target_ok(self, topic_id: str) -> bool:
        # 名册里的 managed 会话，或总线自己（总线也会找主人）。看名册本身：新账本第一轮还没同步 topics 表
        if topic_id == BUS_TOPIC_ID:
            return True
        if self.roster is not None:
            s = self.roster.by_topic(topic_id)
            return bool(s and s.mode == "managed")
        t = self.store.get_topic(topic_id)
        return bool(t and t["kind"] == ADOPTED and t["state"] != sm.CLOSED)

    def _owner_reply(self, m: Message) -> RouteDecision:
        """主人在「找主人」聊天里的回复：引用了哪条就投给哪条的会话；没引用且只有一条未结就投给它；
        否则投总线并附上当前未结列表。不走安全闸（发送人已由 IM 账号核实）。"""
        opens = open_escalations(self.store, self.cfg.escalation.open_hours)
        target = self.store.get_escalation(m.reply_to) if m.reply_to else None
        if target is None and len(opens) == 1:
            target = opens[0]
        tags = [OWNER_VERIFIED_TAG]
        d = RouteDecision(DISPATCH, "matthew_reply", tags)
        if target is not None:
            tags.append(ESCALATION_TAG_PREFIX + target["message_id"])
            self.store.upsert_message(m, d.route, d.reason, tags, target["topic_id"])
            self.store.set_note(m.message_id, escalation_note(target))
            log.info("主人的回复 %s -> %s（回答 %s）", m.message_id, target["topic_id"], target["message_id"])
            return d
        self.store.upsert_message(m, d.route, d.reason, tags, BUS_TOPIC_ID)
        lines = [f"主人的回复 `{m.message_id}` 没有引用具体问题，当前未结的「需要你定」{len(opens)} 条："]
        lines += [f"- topic `{e['topic_id']}` | {e['asked_at']} | message_id `{e['message_id']}` | {escalation_summary(e)}"
                  for e in opens] or ["- （无）"]
        lines.append("判断它回答的是哪一条后，用 `sheepdog forward --topic <topic> --message-ids " + m.message_id + "` 转交。")
        self.store.add_system_message(BUS_TOPIC_ID, "escalation_list", "\n".join(lines))
        log.info("主人的回复 %s 无法确定回答哪条（未结 %d 条），投总线", m.message_id, len(opens))
        return d

    def _handle_change(self, old, m: Message, ctx: RouteContext) -> bool:
        """编辑 / 撤回：按原消息的路由级别处理（设计稿 2.2.1）。返回是否有变化。"""
        content_changed = (old["content"] or "") != (m.content or "")
        recalled = m.deleted and not old["deleted"]
        if not content_changed and not recalled:
            return False
        old_route, old_state = old["route"], old["dispatch_state"]
        if m.chat_id in self.cfg.escalation.chat_ids and old_route != DISPATCH:
            # 「找主人」聊天里没投递过的消息（提问 / 其他）编辑后只更新内容，不重新路由
            self.store.update_content(m.message_id, m)
            return True
        if old_route == DISPATCH and old_state in ("delivered", "acked", "missed"):
            # 已推送过：通知原来那个 session（topic_id 不变；撤回附原文，编辑附新内容）
            if recalled:
                m.content = f"（已撤回）原文：{old['content']}"
            else:
                m.content = f"（已编辑）旧：{old['content']}\n新：{m.content}"
            self.store.update_content(m.message_id, m, DISPATCH, "recalled" if recalled else "edited", "pending")
            self._secure(m.message_id)  # 编辑后重新判定：改成危险内容会被拉回总线
        elif old_route == DISPATCH:
            # 尚未投递：直接更新内容，并重新判定
            self.store.update_content(m.message_id, m)
            self._secure(m.message_id)
        else:
            # Inbox / drop：重新路由，若升级为直推则投递
            d, topic_id, w = self._decide(m, ctx)
            if d.route == DISPATCH and not recalled:
                self.store.update_content(m.message_id, m, DISPATCH, d.reason, "pending", d.tags, topic_id)
                self._close_watch(w, m.message_id, self._secure(m.message_id))
            else:
                self.store.update_content(m.message_id, m)
        return True

    def _tag_read_status(self) -> None:
        ids = [r["message_id"] for r in self.store.pending_dispatch()
               if not r["message_id"].startswith(SYSTEM_ID_PREFIX)] + self.store.unread_inbox_ids()
        if not ids:
            return
        try:
            self.store.set_read(self.source.read_status(ids))
        except Exception as e:  # 已读只是标签，失败不影响路由
            log.warning("查询已读状态失败: %s", e)


# ======================= Dispatcher =======================
class Dispatcher:
    def __init__(self, cfg: Config, store: Store, sink: Sink, roster: Roster | None = None,
                 security: SecurityConfig | None = None, acker=None):
        self.cfg = cfg
        self.store = store
        self.sink = sink
        # 确认表情（7.9）：投递成功后以主人身份点表情
        self.acker = acker
        self.roster = roster or Roster()
        # 只用于批次里渲染警示（规则说明）；判定在 Collector 入账时完成
        self.security = security or SecurityConfig()
        # 业务文字每次组装 prompt 时从 playbook 现读，改完下一批生效
        self.playbook = Playbook.from_config(cfg)

    @property
    def bus_title(self) -> str:
        return f"{self.cfg.session.title_prefix} {self.cfg.session.bus_title}".strip()

    def _session_title(self, t) -> str:
        """{{session_title}}：总线用不带前缀的 bus_title，其他用名册 title。"""
        return self.cfg.session.bus_title if t["topic_id"] == BUS_TOPIC_ID else t["title"]

    def ensure_bus_topic(self):
        t = self.store.get_topic(BUS_TOPIC_ID)
        if t is None:
            self.store.create_topic(BUS_TOPIC_ID, self.bus_title, "bus", BUS_DUTY, sm.ACTIVE)
            t = self.store.get_topic(BUS_TOPIC_ID)
        elif t["duty"] != BUS_DUTY:
            # 老账本里总线 duty 是写死的业务文字，改成指向 playbook
            self.store.update_topic(BUS_TOPIC_ID, duty=BUS_DUTY)
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
            if s.to_spawn:
                # 名册留空 = 由 sheepdog 新建：运行态以 topics 表为准，不回写名册；还没 spawn 就没有投递目标
                cid = t["conversation_id"] if t["spawned_at"] else None
            else:
                cid = s.conversation_id or None
            fields = {
                "title": s.display_title, "kind": s.kind, "duty": s.duty,
                "conversation_id": cid,
                "anchors_json": json.dumps(chat_ids + sorted(set(keep)), ensure_ascii=False),
            }
            if t["conversation_id"] and t["conversation_id"] != fields["conversation_id"]:
                # 换了会话：新会话没收过 onboarding，旧批次的回执也不再等
                fields.update(onboarded_at=None, pending_batch_id=None)
                if not s.to_spawn:
                    fields["spawned_at"] = None
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
            # adopted 会话由名册决定存续，总线是常驻入口：两者的回执 done 都不关会话，按 handled 处理
            if (adopted or t["topic_id"] == BUS_TOPIC_ID) and status == "done":
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
        w = self.cfg.watch
        prompt = bootstrap_prompt(self.playbook, t["title"], self.cfg.session.bus_title, t["topic_id"], self.roster,
                                  self.cfg.prompt_overlay(), w.remind_minutes, w.expire_minutes)
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
        w = self.cfg.watch
        self.sink.send_message(t["conversation_id"],
                               onboarding_prompt(self.playbook, s, bid, w.remind_minutes, w.expire_minutes))
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
        if adopted and not t["conversation_id"]:
            log.info("topic %s 等待 sheepdog spawn 新建会话，%d 条信号排队中", t["topic_id"], len(rows))
            res["waiting_spawn"] = True
            return res
        roster_update = ""
        try:
            if adopted and not t["onboarded_at"]:
                # 第一次收消息前先发 onboarding；消息等它回执或超时后下一轮再投
                res.update(onboarding=self.send_onboarding(t), state=sm.RUNNING)
                return res
            if not adopted:
                t = self._ensure_bus_conversation(t)
                if self.store.get_meta("roster_notify_bus") == "1":
                    roster_update = roster_table(self.roster)
            batch_id = self._new_batch_id("b", t["topic_id"])
            waiting = t["summary"] if t["state"] == sm.WAITING_HUMAN else ""
            # Inbox 摘要只给总线；adopted 会话只管自己的聊天
            inbox = [] if adopted else self.store.inbox_summary()
            prompt = batch_prompt(self.playbook, t["topic_id"], batch_id, rows, inbox, self._session_title(t),
                                  waiting, receipt_optional=adopted, roster_update=roster_update,
                                  security=self.security)
            self.sink.send_message(t["conversation_id"], prompt)
        except SinkError as e:
            self._fail(t, None, str(e))
            res["error"] = str(e)
            return res

        if roster_update:
            self.store.set_meta("roster_notify_bus", "0")
        ids = [r["message_id"] for r in rows]
        # 主人的回复已送到提问的会话：对应的「需要你定」标记已答
        for r in rows:
            for tag in json.loads(r["tags_json"] or "[]"):
                if tag.startswith(ESCALATION_TAG_PREFIX):
                    self.store.answer_escalation(tag.removeprefix(ESCALATION_TAG_PREFIX), r["message_id"])
        self.store.record_dispatch(batch_id, t["topic_id"], ids)
        self.store.mark_batch(ids, batch_id, t["topic_id"], "delivered")
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                pending_batch_id=batch_id, dispatched_at=now_iso())
        res.update(sent=len(ids), batch_id=batch_id, state=sm.RUNNING)
        if self.acker is not None:
            try:  # 点表情只在真正送达之后；失败不影响投递
                self.acker.on_delivered(rows)
            except Exception as e:
                log.warning("点确认表情失败（批次 %s）: %s", batch_id, e)
        return res

    # ---------- 主流程 ----------
    def dispatch_once(self) -> dict:
        self.sync_roster()
        self.ensure_bus_topic()
        tids = [BUS_TOPIC_ID] + [t["topic_id"] for t in self.store.list_topics()
                                 if t["kind"] == ADOPTED and t["state"] != sm.CLOSED]
        # 先处理回执与超时（总线超时会把批次退回待推）、前任交接回执、等待计时，再取待推消息
        for tid in tids:
            self._check_receipt(self.store.get_topic(tid))
        self._check_handovers()
        self._tick_watches(set(tids))
        self._expire_escalations()

        rows = self.store.pending_dispatch()
        groups: dict[str, list] = {}
        for r in rows:
            tid = r["topic_id"] or BUS_TOPIC_ID
            if tid not in tids:
                # 归属的会话已从名册移除或改成 known：退回总线，不丢消息
                log.warning("消息 %s 归属的 %s 已不可投递，改投总线", r["message_id"], tid)
                tid = BUS_TOPIC_ID
            elif (tid != BUS_TOPIC_ID and r["security_action"] == HOLD
                  and SECURITY_RELEASE_TAG not in json.loads(r["tags_json"] or "[]")):
                # 兜底：被 hold 的消息没有经已核对的主人原话放行，不得投给 managed 会话
                log.warning("消息 %s 被安全规则拦截，改投总线", r["message_id"])
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

    # ---------- 新建与接手（7.2） ----------
    def spawn(self, key: str) -> dict:
        """为名册里 conversation_id 留空的 managed 条目新建会话；只建一次，新 id 只写 topics 表。

        有前任且 retire_predecessor：先给前任发退休通知（前任 topic 置 closed），再新建接手会话；
        接手会话先读完前任上下文并回执，期间信号排队不丢。调用方需先 sync_roster()。
        """
        s = self.roster.by_key(key)
        if s is None:
            raise ValueError(f"名册里没有 key={key!r}")
        if not s.to_spawn:
            raise ValueError(f"{key} 不是待建条目（只有 mode = managed 且 conversation_id 留空的条目才能 spawn）")
        t = self.store.get_topic(s.topic_id)
        if t["spawned_at"]:
            return {"skipped": f"已于 {t['spawned_at']} 新建 {t['conversation_id']}"}
        title = f"{self.cfg.session.title_prefix} {s.display_title}".strip()
        report: dict = {}
        if s.predecessor_conversation_id and s.retire_predecessor:
            report["retire"] = self._retire_predecessor(s, title)
        read_bid = self._new_batch_id("h", s.topic_id) if s.predecessor_conversation_id else ""
        prompt = adopted_bootstrap(
            self.playbook, s, title, read_bid, self.cfg.watch.remind_minutes, self.cfg.watch.expire_minutes,
            str(transcript_path(s.predecessor_conversation_id)) if s.predecessor_conversation_id else "",
            str(conversation_dir(s.predecessor_conversation_id)) if s.predecessor_conversation_id else "",
        )
        cid = self.sink.new_conversation(title, prompt, self.cfg.session.model)
        ts = now_iso()
        # bootstrap 已含职责与规则，等同 onboarding
        fields = dict(conversation_id=cid, spawned_at=ts, onboarded_at=ts, pending_batch_id=None,
                      state=sm.ACTIVE, retries=0)
        if read_bid:
            # 「读完前任上下文」的回执作为一个不含消息的批次：回执前信号排队
            self.store.record_dispatch(read_bid, s.topic_id, [])
            fields.update(state=sm.RUNNING, pending_batch_id=read_bid, dispatched_at=ts)
        self.store.update_topic(s.topic_id, **fields)
        log.info("spawn %s -> %s", s.topic_id, cid)
        report.update(conversation_id=cid, title=title, read_batch=read_bid or None)
        return report

    def _retire_predecessor(self, s, successor_title: str) -> str:
        pid = predecessor_topic_id(s.topic_id)
        pt = self.store.get_topic(pid)
        if pt is not None and pt["conversation_id"] == s.predecessor_conversation_id:
            return f"已发过（{pid}）"
        if pt is None:
            self.store.create_topic(pid, f"{s.display_title}（前任）", RETIRED, "已退休，由接手会话继续", sm.CLOSED)
        bid = self._new_batch_id("r", pid)
        # 先发退休通知，失败就整个 spawn 失败，不新建接手会话
        self.sink.send_message(s.predecessor_conversation_id,
                               retirement_prompt(self.playbook, pid, f"{s.display_title}（前任）", bid,
                                                 successor_title, s.topic_id))
        self.store.record_dispatch(bid, pid, [])
        self.store.update_topic(pid, conversation_id=s.predecessor_conversation_id, kind=RETIRED, state=sm.CLOSED,
                                pending_batch_id=bid, dispatched_at=now_iso())
        log.info("已给前任 %s 发退休通知 %s", s.predecessor_conversation_id, bid)
        return f"已发送 {bid}"

    def _check_handovers(self) -> None:
        """前任交接回执到达：原样转给接手会话（作为待推消息，受接管/排队约束）。前任 topic 保持 closed。"""
        for t in self.store.list_topics():
            if t["kind"] != RETIRED or not t["pending_batch_id"]:
                continue
            p = self.receipt_path(t["topic_id"], t["pending_batch_id"])
            if not p.exists():
                continue
            raw = p.read_text(encoding="utf-8")
            successor = t["topic_id"].removesuffix(".prev")
            self.store.add_system_message(
                successor, "handover_receipt",
                f"前任「{t['title']}」（`{t['conversation_id']}`）的交接回执，原样转发：\n```json\n{raw.strip()}\n```")
            receipt = json.loads(raw)
            self.store.finish_dispatch(t["pending_batch_id"], "acked", raw)
            self.store.update_topic(t["topic_id"], pending_batch_id=None, summary=receipt.get("summary", ""))
            log.info("前任交接回执 %s 已转给 %s", t["pending_batch_id"], successor)

    # ---------- 等别人回复（7.4） ----------
    def _tick_watches(self, live_topics: set[str]) -> None:
        """每轮检查等待：到 remind_minutes 各档各提醒一次，到 expire_minutes 关闭并提示。文字来自 nudge_*.md。"""
        now = _now()
        remind, expire = self.cfg.watch.remind_minutes, self.cfg.watch.expire_minutes
        for w in self.store.open_watches():
            if w["topic_id"] not in live_topics:
                self.store.close_watch(w["id"], "topic_closed")
                continue
            started = _parse(w["started_at"])
            if not started:
                continue
            mins = (now - started).total_seconds() / 60
            title = self._session_title(self.store.get_topic(w["topic_id"]))
            if mins >= expire:
                self.store.close_watch(w["id"], "expired")
                self.store.add_system_message(w["topic_id"], "watch_expired",
                                              watch_message(self.playbook, "nudge_expire.md", w, expire, title),
                                              [f"watch:{w['id']}"])
                continue
            # 只发最近一档：进程停过一阵、一次跨过两档时只提醒一次，并把前面的档也记上
            due = [i for i, m in enumerate(remind) if mins >= m and not w[WATCH_SLOTS[i]]]
            if not due:
                continue
            i = max(due)
            self.store.add_system_message(w["topic_id"], "watch_nudge",
                                          watch_message(self.playbook, "nudge_remind.md", w, remind[i], title),
                                          [f"watch:{w['id']}"])
            ts = now_iso()
            self.store.update_watch(w["id"], **{WATCH_SLOTS[j]: ts for j in range(i + 1) if not w[WATCH_SLOTS[j]]})

    def _expire_escalations(self) -> None:
        """超过 open_hours 未答的「需要你定」自动关闭（只关闭，不提醒）。"""
        cutoff = _now() - timedelta(hours=self.cfg.escalation.open_hours)
        for e in self.store.open_escalations():
            asked = _parse(e["asked_at"])
            if asked is not None and asked.tzinfo is not None and asked < cutoff:
                self.store.expire_escalation(e["message_id"])
                log.info("「需要你定」%s（%s）超时未答，已关闭", e["message_id"], e["topic_id"])

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
        report["spawn"] = {}
        for s in self.roster.managed:
            t = self.store.get_topic(s.topic_id)
            if s.to_spawn:
                # 待建条目：init 自动 spawn（bootstrap 已含职责，不再单独 onboarding）
                try:
                    r = self.spawn(s.key)
                    report["spawn"][s.topic_id] = r.get("skipped") or (
                        f"已新建 {r['conversation_id']}" + (f"；前任退休通知 {r['retire']}" if r.get("retire") else ""))
                except SinkError as e:
                    report["spawn"][s.topic_id] = f"失败: {e}"
                continue
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


def _check_target(store: Store, topic_id: str, verb: str):
    if topic_id == BUS_TOPIC_ID:
        raise ValueError(f"不能{verb}给总线 tp_bus：只用于名册里的 managed 会话")
    t = store.get_topic(topic_id)
    if t is None:
        raise ValueError(f"topic {topic_id} 不存在：先确认名册里有它，并已同步（sheepdog init 或 run）")
    if t["kind"] == KNOWN_KIND:
        raise ValueError(f"{topic_id} 是 known 会话，只登记不投递；请在回执里建议主人去该会话处理")
    if t["kind"] != ADOPTED:
        raise ValueError(f"{topic_id} 不是名册里的 managed 会话（kind={t['kind']}）")
    if t["state"] == sm.CLOSED:
        raise ValueError(f"{topic_id} 已从名册移除（closed）")
    return t


QUOTE_NOT_FOUND = "这句话在总线里找不到主人的原文，请原样引用"


def bus_quote_verifier(store: Store, max_age_hours: float):
    """--quote 核对器（7.7 C）：读总线会话 transcript 的 USER_INPUT，确认主人在回看期内亲口说过。"""
    bus = store.get_topic(BUS_TOPIC_ID)
    cid = bus["conversation_id"] if bus else None

    def verify(quote: str) -> bool:
        return bool(cid) and quote_verified(quote, transcript_path(cid), max_age_hours)
    return verify


def forward_messages(store: Store, topic_id: str, message_ids: list[str], note: str = "", quote: str = "",
                     verify_quote=None, self_open_id: str = "", escalation_open_hours: float = 24) -> list[str]:
    """把消息 / 主人原话转交给 managed 会话（规格第 6 节、7.3、7.7）。只改归属并回到待推，真正投递由 Dispatcher
    下一轮完成，因此同样受人类接管、onboarding 约束。

    - 主人本人发的消息（sender_id == self_open_id）带「已核对」标注，并关闭目标会话最近一条未结的「需要你定」。
    - quote 必须通过 verify_quote 核对（没给核对器一律拒绝），投递时标成已核对的主人原话；note 不核对，标成总线备注。
    - 被安全规则 hold 的消息只能带已核对的 quote 转交。
    - 目标是总线、known、已移除或不存在时抛 ValueError。
    """
    _check_target(store, topic_id, "转交")
    ids = list(dict.fromkeys(i.strip() for i in message_ids if i.strip()))
    quote, note = (quote or "").strip(), (note or "").strip()
    if not ids and not quote and not note:
        raise ValueError("至少要有 --message-ids、--quote、--note 之一")
    rows = {i: store.get_message(i) for i in ids}
    missing = [i for i, r in rows.items() if r is None]
    if missing:
        raise ValueError(f"账本里没有这些消息: {missing}")
    held = [i for i, r in rows.items() if r["security_action"] == HOLD]
    if held and not quote:
        raise ValueError(f"消息 {held} 被安全规则拦截，只能凭已核对的主人原话转交：加 --quote \"<主人原话>\"")
    if quote and not (verify_quote and verify_quote(quote)):
        raise ValueError(QUOTE_NOT_FOUND)
    for i, row in rows.items():
        src = row["topic_id"] or BUS_TOPIC_ID
        tags = [x for x in json.loads(row["tags_json"] or "[]") if not x.startswith(("owner:", "forwarded_from:"))]
        tags += [f"owner:{topic_id.removeprefix('tp_')}", f"forwarded_from:{src}"]
        if i in held and SECURITY_RELEASE_TAG not in tags:
            tags.append(SECURITY_RELEASE_TAG)
        if self_open_id and row["sender_id"] == self_open_id:
            # 主人本人的消息（7.8）：带已核对标注，不需要 --quote；目标会话最近一条未结的「需要你定」标记已答
            if OWNER_VERIFIED_TAG not in tags:
                tags.append(OWNER_VERIFIED_TAG)
            opens = open_escalations(store, escalation_open_hours, topic_id)
            if opens:
                store.answer_escalation(opens[0]["message_id"], i)
                store.set_note(i, escalation_note(opens[0]))
        store.forward_message(i, topic_id, tags)
    if quote or note:
        extra = f"（随附转交的消息：{', '.join(ids)}）" if ids else ""
        store.add_system_message(topic_id, "bus_relay", relay_content(quote, note) + ("\n" + extra if extra else ""))
    log.info("转交 %d 条消息%s -> %s", len(ids), "（含转达）" if quote or note else "", topic_id)
    return ids


def add_watch(store: Store, topic_id: str, person_id: str, chat_id: str = "", note: str = "") -> int:
    """登记一条等待（7.4）。总线和 managed 会话都可以登记。"""
    if topic_id != BUS_TOPIC_ID:
        _check_target(store, topic_id, "登记等待")
    elif store.get_topic(BUS_TOPIC_ID) is None:
        raise ValueError("总线还没建：先 sheepdog init 或 run")
    if not person_id.strip():
        raise ValueError("--person 不能为空")
    return store.add_watch(topic_id, person_id.strip(), (chat_id or "").strip(), note)


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
