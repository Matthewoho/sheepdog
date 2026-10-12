"""引擎：Collector（拉取 + 路由 + 聊天归属 + 编辑/撤回处理）与 Dispatcher（名册同步 + 按 topic 投递 + 状态机 + 回执）。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import session as sm
from .config import ANTIGRAVITY, Config, DutyConfig
from .models import Message
from . import duty
from .guard import (ACTIVITY, AUDIT, COMPACTION, DUTY, LAST_ACTIVITY_TAIL, activity_at, commands_in, first_match,
                    human_input_at, is_compaction, last_activity_at, local_hhmm, one_sentence, parse_ts, record_time, redact,
                    scan_jsonl, tool_actions)
from .playbook import REMINDERS_DIR, Playbook
from .prompts import (DEFAULT_COMPACTION_NOTE, QUOTE_LABEL, QUOTE_LABEL_IM, ROSTER_HASH_STUB, adopted_bootstrap,
                      batch_prompt, bootstrap_prompt, onboarding_prompt, with_cli,
                      quote_line, relay_content, retirement_prompt, roster_table, rules_update_message, standing_rules_bus,
                      standing_rules_managed, watch_message)
from .roster import _KEY_RE, MANAGED, Roster, RosterChat, RosterSession
from .security import HOLD, NONE, SecurityConfig, quote_verified
from .router import DISPATCH, DROP, INBOX, SELF, RouteContext, RouteDecision, message_bodies, route
from .sink import RunResult, Sink, SinkError
from .source import Source
from .sink.agentapi import conversation_dir, transcript_contains, transcript_path
from .sink.cli_host import NO_RESULT, RECEIPT_FENCE, extract_receipt
from .store import SYSTEM_CHAT, SYSTEM_ID_PREFIX, Store, now_iso, row_to_message

log = logging.getLogger("sheepdog")

BUS_TOPIC_ID = "tp_bus"
ADOPTED = "adopted"
KNOWN_KIND = "known"
RETIRED = "retired"
# 总线临时新开的会话（7.10）：只在账本里，不写名册；回执 done 即收掉
DYNAMIC = "dynamic"
# 会往里投信号的会话类型
MANAGED_KINDS = (ADOPTED, DYNAMIC)
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
# 主人本人的发言作为背景送给负责的会话（7.13）：不点表情、不要回执、不改状态
OWNER_CONTEXT = "owner_context"
ESCALATION_TAG_PREFIX = "escalation:"
# 命令审计（7.19 K3）：同一个会话 10 分钟内的命中合并成一条报
AUDIT_MERGE_MINUTES = 10


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


def is_agent_reply(content: str, oc) -> bool:
    """这条主人身份的消息是不是会话代回的（7.13 / 7.17）：正文以 skip_prefixes 之一开头，或任意位置包含
    agent_markers 之一（代回卡片的右下角标记；卡片按账本里的 content 匹配）。"""
    bodies = message_bodies(content)
    if any(b.startswith(p) for b in bodies for p in oc.skip_prefixes):
        return True
    return any(mk in b for b in [content or "", *bodies] for mk in oc.agent_markers)


def predecessor_topic_id(topic_id: str) -> str:
    # 「.」不在名册 key 允许的字符里，不会和真实条目撞名
    return topic_id + ".prev"
MUTED_REFRESH_MINUTES = 30
MAX_RETRIES = 3
# 命令行宿主连续写锁冲突多久提示总线（7.18 第 6 条）；登录失效的提醒同一原因多久发一次
LOCK_ALERT_MINUTES = 60
LOGIN_NOTICE_HOURS = 24
# 写锁满 60 分钟给主人的提醒：同一个会话 24 小时内最多一次
LOCK_NOTICE_HOURS = 24
HOST_LABEL = {"antigravity": "Antigravity", "codex": "Codex", "claude": "Claude"}
LOGIN_COMMAND = {"codex": "codex login", "claude": "claude auth login"}

# 会话卡在 attention（7.21）：topics.attention_reason 的取值。只有回执超时会自动恢复，其他要人手动 session-reset
RECEIPT_TIMEOUT = "回执超时"          # dispatches.error：判回执超时的批次以它开头，晚到的回执按它找回来
ACKED_LATE = "acked_late"             # dispatches.state：判过回执超时以后回执才到
ATTN_RECEIPT_TIMEOUT = "receipt_timeout"
ATTN_NO_RECEIPT = "no_receipt"        # 命令行会话这一轮跑完了，但没交回执
ATTN_SEND_FAILED = "send_failed"      # 投递本身失败（agentapi 发送失败、宿主没开等）
ATTN_HOST_FAILED = "host_failed"      # 命令行宿主这一轮失败
ATTN_HOST_TIMEOUT = "host_timeout"    # 命令行宿主这一轮超过 timeout_minutes
ATTN_CONFIG = "config"                # Codex 配置档读不到 / 解析失败
ATTN_CAPACITY = "capacity"            # 容量满、重试用完
ATTENTION_REASONS = {
    ATTN_RECEIPT_TIMEOUT: "回执超时", ATTN_NO_RECEIPT: "这一轮跑完了但没交回执", ATTN_SEND_FAILED: "投递失败",
    ATTN_HOST_FAILED: "宿主这一轮失败", ATTN_HOST_TIMEOUT: "宿主这一轮超时", ATTN_CONFIG: "Codex 配置档有问题",
    ATTN_CAPACITY: "容量满、重试用完",
}
AUTO_RECOVER_REASONS = {ATTN_RECEIPT_TIMEOUT}

# 在岗表（7.20）
DIGEST_META = "duty_digest_fp"           # 上一次附给总线的摘要指纹：变了才再附
DEFAULT_DIGEST_TITLE = "📋 在岗表摘要（有变化才附；不空闲的会话每个一行，空闲的合成一行）："


def duty_reset_key(topic_id: str) -> str:
    """meta 键：session-reset 的时间。在这之前送出、会话没碰过的批次不再算中断 / 失联。"""
    return f"duty_reset:{topic_id}"


def duty_notice_key(topic_id: str) -> str:
    """meta 键：这一段卡住（从会话最后一次动作或 session-reset 起）已经通知过什么：{"seg": 起点, "sent": [中断 / 失联]}，
    同一段中断、失联各只通知一次。"""
    return f"duty_notice:{topic_id}"


def attention_reason_label(reason: str | None) -> str:
    return ATTENTION_REASONS.get(reason or "", reason or "原因没记录（老账本）")


def auto_recovers(reason: str | None) -> bool:
    return (reason or "") in AUTO_RECOVER_REASONS


def attention_notice_key(topic_id: str) -> str:
    """meta 键：值是发通知时那次 attention 的 attention_at。同一次 attention 只发一次，恢复时据此决定发不发「已恢复」。"""
    return f"attention_notice:{topic_id}"


def _tz(offset: str) -> tuple[timezone, str]:
    """配置的时区（timezone_offset）和它的叫法：+08:00 叫「北京时间」。"""
    try:
        sign = -1 if offset.startswith("-") else 1
        hh, _, mm = offset.lstrip("+-").partition(":")
        tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)))
    except ValueError:
        tz, offset = timezone(timedelta(hours=8)), "+08:00"
    return tz, ("北京时间" if offset == "+08:00" else f"UTC{offset}")


def display_time(dt: datetime | None, offset: str = "+08:00") -> str:
    """给人看的时间：换到配置的时区（timezone_offset），+08:00 标「北京时间」。"""
    if dt is None:
        return ""
    tz, label = _tz(offset)
    return f"{dt.astimezone(tz).strftime('%m-%d %H:%M')}（{label}）"


def clock(dt: datetime | None, offset: str = "+08:00") -> str:
    """HH:MM（配置的时区），在岗表里一行写得下。"""
    return dt.astimezone(_tz(offset)[0]).strftime("%H:%M") if dt is not None else "-"


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None




# ======================= 聊天归属 =======================
def chat_anchor(chat_id: str, all_messages: bool) -> str:
    """dynamic 会话负责的聊天，写进 anchors：oc_x（只推 dispatch 级）或 oc_x:all（全部消息）。"""
    return f"{chat_id}:all" if all_messages else chat_id


def dynamic_owners(store: Store) -> dict[str, tuple[str, bool]]:
    """未关闭的 dynamic 会话负责的聊天：chat_id → (topic_id, all_messages)。"""
    out: dict[str, tuple[str, bool]] = {}
    for t in store.list_topics():
        if t["kind"] != DYNAMIC or t["state"] == sm.CLOSED:
            continue
        for a in json.loads(t["anchors_json"] or "[]"):
            if a.startswith("oc_"):
                cid, _, flag = a.partition(":")
                out.setdefault(cid, (t["topic_id"], flag == "all"))
    return out


def open_dynamic_topics(store: Store) -> list:
    return [t for t in store.list_topics() if t["kind"] == DYNAMIC and t["state"] != sm.CLOSED]


def apply_ownership(msg: Message, d: RouteDecision, roster: Roster | None,
                    ignore_chat_ids: list[str],
                    dynamic: dict[str, tuple[str, bool]] | None = None) -> tuple[RouteDecision, str | None]:
    """路由之后按名册判断归属（规格第 3 节），返回 (决策, topic_id)。reason 不变，归属写进 tags。

    - 自己发的、ignore_chat_ids：不变
    - managed 会话的聊天 + all_messages：推给它（覆盖 inbox 和免打扰 drop）
    - managed 会话的聊天 + dispatch 级：推给它
    - 名册里没人负责、但总线新开的 dynamic 会话负责（7.10）：同上
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
    elif dynamic and msg.chat_id in dynamic:
        topic_id, all_messages = dynamic[msg.chat_id]
        if all_messages or d.route == DISPATCH:
            return RouteDecision(DISPATCH, d.reason, [*d.tags, f"owner:{topic_id.removeprefix('tp_')}"]), topic_id
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
        self._dynamic: dict[str, tuple[str, bool]] = {}
        # 正在冷却的聊天（7.14）：chat_id → until
        self._cooling: dict[str, datetime] = {}

    def _decide(self, m: Message, ctx: RouteContext):
        """路由 → 名册归属 → 等待归属 → 防循环。返回 (决策, topic_id, 命中的等待)；等待由调用方在安全闸之后关闭。"""
        d, topic_id, w = self._decide_inner(m, ctx)
        if d.route == SELF:
            return d, topic_id, w
        if self._is_agent(m):
            d = RouteDecision(d.route, d.reason, [*d.tags, "agent_sender"])
        until = self._cooling.get(m.chat_id)
        if until and _now() < until and d.route in (DISPATCH, INBOX):
            # 疑似循环冷却中：除主人本人以外一律进 Inbox，不投给任何会话（等待也不算收到回复）
            return RouteDecision(INBOX, "loop_guard", d.tags), None, None
        return d, topic_id, w

    def _is_agent(self, m: Message) -> bool:
        lg = self.cfg.loop_guard
        if lg.treat_all_bots_as_agents and m.sender_type in ("app", "bot"):
            return True
        if m.sender_id and m.sender_id in lg.agent_sender_ids:
            return True
        name = (m.sender_name or "").lower()
        return any(n.lower() in name for n in lg.agent_sender_names)

    def _refresh_cooling(self) -> None:
        now = _now()
        self._cooling = {}
        for e in self.store.open_loop_events():
            until = _parse(e["until"])
            if until and until > now and e["chat_id"] not in self._cooling:
                self._cooling[e["chat_id"]] = until

    def _loop_check(self, m: Message) -> None:
        """会话代回（主人身份、带代回前缀或代回卡片标记）计数：window 内超过 max_agent_replies、且窗口内最近一条非主人消息
        来自 Agent 发送人 → 该聊天冷却，并提示总线一次。对方是真人时不熔断，只记 INFO。"""
        lg, me, oc = self.cfg.loop_guard, self.cfg.self_open_id, self.cfg.owner_context
        if not (oc.skip_prefixes or oc.agent_markers) or m.chat_id in self.cfg.escalation.chat_ids:
            return
        if not is_agent_reply(m.content, oc):
            return
        until = self._cooling.get(m.chat_id)
        if until and _now() < until:
            return  # 已在冷却，不重复触发
        start = _now() - timedelta(minutes=lg.window_minutes)
        n = 0
        for r in self.store.own_messages_in_chat(m.chat_id, me):
            t = _parse(r["create_time"])
            if t is None or t.tzinfo is None or t < start:
                continue
            if is_agent_reply(r["content"], oc):
                n += 1
        if n <= lg.max_agent_replies:
            return
        name = m.chat_name or m.chat_id
        # 7.14 补丁：只在对方是 Agent 时熔断——窗口内最近一条非主人消息来自 Agent 发送人；真人对话只记 INFO
        latest = None
        for r in self.store.others_in_chat(m.chat_id, me):
            t = _parse(r["create_time"])
            if t is not None and t.tzinfo is not None and t >= start:
                latest = r
                break
        if latest is None or not self._is_agent(row_to_message(latest)):
            log.info("代回频繁（对方为真人，不熔断）：%s（%s）%g 分钟内代回 %d 次", name, m.chat_id, lg.window_minutes, n)
            return
        until = _now() + timedelta(minutes=lg.cooldown_minutes)
        self.store.add_loop_event(m.chat_id, name, n, until.isoformat(timespec="seconds"))
        self._cooling[m.chat_id] = until
        log.warning("疑似循环：%s（%s）%g 分钟内代回 %d 次，暂停 %g 分钟", name, m.chat_id, lg.window_minutes, n,
                    lg.cooldown_minutes)
        self.store.add_system_message(
            BUS_TOPIC_ID, "loop_guard",
            f"⚠️ 疑似循环：{name}（`{m.chat_id}`），{lg.window_minutes:g} 分钟内代回 {n} 次，已暂停 {lg.cooldown_minutes:g} 分钟。"
            + with_cli(f"期间该聊天除主人以外的消息只进 Inbox；手动解除：`sheepdog loops --clear {m.chat_id}`",
                     self.cfg.cli_command))

    def _decide_inner(self, m: Message, ctx: RouteContext):
        d, topic_id = apply_ownership(m, route(m, ctx, self.cfg.routing), self.roster, self.cfg.routing.ignore_chat_ids,
                                      self._dynamic)
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
        """对方回复了（7.16）：等待进入 replied（候选），不关闭——回复可能只是寒暄；由会话 watch-done 确认。
        被 hold 的回复没到等待的会话手里：状态不变，照常提醒 / 到期。"""
        if w is None or action == HOLD:
            return
        self.store.mark_watch_replied(w["id"], message_id)
        log.info("等待 #%s 收到回复 %s -> %s，等会话确认", w["id"], message_id, w["topic_id"])

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

    def ingest(self, msgs: list[Message]) -> dict:
        """一批消息入账：路由 → 归属 → 安全闸 → 等待 / 主人发言 / 防循环。poll_once 和 sheepdog inject 共用。"""
        ctx = RouteContext(self.cfg.self_open_id, self._muted(), self._is_my_message)
        self._dynamic = dynamic_owners(self.store)
        self._refresh_cooling()
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
                elif d.route == SELF and me and m.sender_id == me:
                    self._loop_check(m)
                    self._owner_context(m)
                stats["new"] += 1
                stats[d.route] = stats.get(d.route, 0) + 1
            else:
                if self._handle_change(existing, m, ctx):
                    stats["changed"] += 1
                else:
                    self.store.touch(m.message_id)

        return stats

    def poll_once(self) -> dict:
        started = _now()
        wm = _parse(self.store.get_meta("watermark"))
        pending_start = _parse(self.store.get_meta("partial_start"))
        if pending_start:
            start = pending_start  # 上一轮没拉完：从原 start 重拉（按 message_id 去重）
        elif wm:
            start = wm - timedelta(seconds=self.cfg.overlap_seconds)
        else:
            start = started - timedelta(minutes=self.cfg.initial_lookback_minutes)
        # 固定窗口 [start, started]：翻页期间新来的消息留给下一轮，水位线只推进到 started（7.16）
        msgs = self.source.fetch_since(start.isoformat(timespec="seconds"), started.isoformat(timespec="seconds"))
        partial = bool(getattr(self.source, "last_fetch_partial", False))
        stats = self.ingest(msgs)

        self._tag_read_status()
        self._check_owner_reactions()
        stats["partial"] = partial
        if partial:
            self._partial_round(start)
        else:
            self.store.set_meta("partial_start", "")
            self.store.set_meta("partial_streak", "0")
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

    def _partial_round(self, start: datetime) -> None:
        """本轮没拉完（7.16）：不推进水位线，下一轮从原 start 重拉；同一缺口只提示总线一次。"""
        streak = int(self.store.get_meta("partial_streak", "0") or 0) + 1
        self.store.set_meta("partial_streak", str(streak))
        key = start.isoformat(timespec="seconds")
        log.warning("本轮消息没拉完（从 %s 起，连续 %d 轮），不推进水位线，下一轮重拉", key, streak)
        if self.store.get_meta("partial_start") != key:
            self.store.set_meta("partial_start", key)
            self.store.add_system_message(
                BUS_TOPIC_ID, "collect_partial",
                f"⚠️ 采集不完整：从 {key} 起的消息这一轮没拉完（翻页上限 max_pages 或分页中断），"
                "sheepdog 会从这个时间点重拉，期间消息可能晚到。")

    # ---------- 主人本人的发言作为背景（7.13） ----------
    def _owner_context_wanted(self, m: Message) -> bool:
        oc = self.cfg.owner_context
        if not oc.enabled:
            return False
        if m.chat_id in self.cfg.escalation.chat_ids or m.chat_id in self.cfg.routing.ignore_chat_ids:
            return False
        # 会话以主人身份代回的（带代回前缀）不送
        return not is_agent_reply(m.content, oc)

    def _owner_context_target(self, chat_id: str) -> str | None:
        """去向：名册 / 总线新开的聊天归属 → 它；否则 follow_hours 内最近处理过该聊天的会话（含总线）；都没有 → 不送。"""
        owner = self.roster.owner_of(chat_id) if self.roster else None
        if owner is not None:
            return owner[0].topic_id
        if chat_id in self._dynamic:
            return self._dynamic[chat_id][0]
        since = (_now() - timedelta(hours=self.cfg.owner_context.follow_hours)).isoformat(timespec="seconds")
        tid = self.store.last_delivered_topic(chat_id, since)
        if not tid:
            return None
        t = self.store.get_topic(tid)
        if t is None or t["state"] == sm.CLOSED or not (tid == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS):
            return None
        return tid

    def _owner_context(self, m: Message) -> None:
        if not self._owner_context_wanted(m):
            return
        topic_id = self._owner_context_target(m.chat_id)
        if not topic_id:
            return
        # 回复的是哪条由投递时统一附的「↪ 回复的是」说明（7.15）
        self.store.route_owner_context(m.message_id, topic_id, "")
        log.info("主人的发言 %s 作为背景送给 %s", m.message_id, topic_id)

    def _owner_edit(self, old, m: Message, recalled: bool) -> None:
        if not self._owner_context_wanted(m):
            return
        old_text = old["content"] or ""
        if is_agent_reply(old_text, self.cfg.owner_context):
            return  # 原文是会话代回的
        topic_id = self._owner_context_target(m.chat_id)
        if not topic_id:
            return
        where = "私聊" if m.chat_type == "p2p" else f"群「{m.chat_name or m.chat_id}」"
        if recalled:
            body = f"📝 主人撤回了他在{where}的发言（message_id `{m.message_id}`）：原文\n> {old_text}"
        else:
            body = f"📝 主人编辑了他在{where}的发言（message_id `{m.message_id}`）：\n旧：{old_text}\n新：{m.content}"
        self.store.add_system_message(topic_id, OWNER_CONTEXT, body)
        log.info("主人%s发言 %s，作为背景送给 %s", "撤回" if recalled else "编辑", m.message_id, topic_id)

    def _check_owner_reactions(self) -> None:
        """每 reaction_check_minutes 查一次主人对已投递消息点的表情（只读），新加的作为背景送给负责的会话。

        排除 sheepdog 自己以主人身份点的确认表情；已报过的不重复报；第一次检查只记基线不报。失败只记日志。
        """
        oc, me = self.cfg.owner_context, self.cfg.self_open_id
        if not oc.enabled or not oc.reaction_check_minutes or not me or not hasattr(self.source, "reactions_of"):
            return
        last = _parse(self.store.get_meta("owner_reactions_at"))
        if last and _now() - last < timedelta(minutes=oc.reaction_check_minutes):
            return
        baseline = last is None
        since = (_now() - timedelta(hours=oc.reaction_lookback_hours)).isoformat(timespec="seconds")
        rows = {r["message_id"]: r for r in self.store.delivered_since(since)}
        self.store.set_meta("owner_reactions_at", now_iso())
        if not rows:
            return
        try:
            found = self.source.reactions_of(list(rows))
        except Exception as e:
            log.warning("查主人表情失败: %s", e)
            return
        acks = self.store.ack_reaction_ids()
        for mid, items in found.items():
            r = rows.get(mid)
            if r is None:
                continue
            for it in items:
                emoji, op, rid = it.get("emoji_type", ""), it.get("operator_id", ""), it.get("reaction_id", "")
                if op != me or not emoji or (rid and rid in acks):
                    continue  # 别人点的、或 sheepdog 自己点的确认表情
                if self.store.owner_reaction_known(mid, emoji, op):
                    continue
                self.store.add_owner_reaction(mid, emoji, op, rid, reported=not baseline)
                if baseline:
                    continue
                topic_id = self._owner_reaction_target(r)
                if not topic_id:
                    continue
                text = " ".join((r["content"] or "").split())
                where = "私聊" if r["chat_type"] == "p2p" else f"群「{r['chat_name'] or r['chat_id']}」"
                self.store.add_system_message(
                    topic_id, OWNER_CONTEXT,
                    f"📝 主人对这条消息点了 {emoji}：{where} | {r['sender_name'] or '-'}（{r['sender_id'] or '-'}）："
                    f"{text[:80] + ('…' if len(text) > 80 else '')}（message_id `{mid}`）")
                log.info("主人对 %s 点了 %s，作为背景送给 %s", mid, emoji, topic_id)

    def _owner_reaction_target(self, row) -> str | None:
        """名册 / 总线新开的聊天归属优先，否则送给投递过这条消息的会话。"""
        owner = self.roster.owner_of(row["chat_id"]) if self.roster else None
        if owner is not None:
            return owner[0].topic_id
        if row["chat_id"] in self._dynamic:
            return self._dynamic[row["chat_id"]][0]
        t = self.store.get_topic(row["topic_id"]) if row["topic_id"] else None
        if t is None or t["state"] == sm.CLOSED or not (t["topic_id"] == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS):
            return None
        return t["topic_id"]

    # ---------- 「找主人」聊天（7.8） ----------
    def _escalation_message(self, m: Message) -> RouteDecision:
        """先于普通路由，不受 ignore_chat_ids 影响。三种结果：登记提问 / 主人的回复 / 其他一律 drop。"""
        esc = self.cfg.escalation
        me = self.cfg.self_open_id
        if me and m.sender_id == me:
            return self._owner_reply(m)
        if m.sender_type in ("app", "bot"):
            question = ""
            if esc.pattern is not None:
                question = next((mt.group("topic") for b in message_bodies(m.content)
                                 if (mt := esc.pattern.search(b)) and mt.group("topic")), "")
            topic = question or self._attribute(m)
            if topic:
                ok = self._escalation_target_ok(topic)
                d = RouteDecision(DROP, "escalation_asked" if question else "escalation_bot_message")
                self.store.upsert_message(m, d.route, d.reason, [f"escalation_topic:{topic}"])
                if ok:
                    # 7.15：机器人发给主人的每条消息都记来源，主人引用回复它时按这里送回
                    self.store.add_bot_outbox(m.message_id, topic, bool(question), m.content, m.create_time or now_iso())
                if question and ok:
                    self.store.add_escalation(m.message_id, topic, m.chat_id, m.content, m.create_time or now_iso())
                    log.info("登记「需要你定」%s -> %s", m.message_id, topic)
                elif not ok:
                    log.warning("机器人消息 %s 的 topic %s 不在名册里（或已关闭），只记日志", m.message_id, topic)
                return d
        d = RouteDecision(DROP, "escalation_chat_other")
        self.store.upsert_message(m, d.route, d.reason, [])
        return d

    # 机器人消息第一行里的 [..] 标签（7.15）：按「·」拆开逐段和 aliases、会话标题比
    _BRACKET = re.compile(r"\[([^\[\]]+)\]")

    def _attribute(self, m: Message) -> str | None:
        """机器人发给主人的消息来自哪个 topic：第一行先用 attribution_regex，认不出再用 aliases 和会话标题；唯一命中才算。"""
        bodies = message_bodies(m.content)
        text = (bodies[-1] if len(bodies) > 1 else bodies[0]).strip()
        first = text.splitlines()[0] if text else ""
        if not first:
            return None
        rx = self.cfg.escalation.attribution
        if rx is not None and (mt := rx.search(first)) and mt.group("topic"):
            return mt.group("topic")
        names = [part.strip() for br in self._BRACKET.findall(first) for part in br.split("·") if part.strip()]
        aliases = self.cfg.escalation.aliases or {}
        hits = {aliases[n] for n in names if n in aliases}
        if not hits:
            titles = self._titles_for_attribution()
            hits = {tid for n in names for tid, title in titles if title and (n == title or n in title or title in n)}
        return hits.pop() if len(hits) == 1 else None

    def _titles_for_attribution(self) -> list[tuple[str, str]]:
        """会发消息给主人的会话（总线、名册 managed、总线新开）及去掉 title_prefix 的标题。"""
        prefix = self.cfg.session.title_prefix
        out = [(BUS_TOPIC_ID, self.cfg.session.bus_title)]
        for t in self.store.list_topics():
            if t["kind"] in MANAGED_KINDS and t["state"] != sm.CLOSED:
                title = (t["title"] or "").strip()
                if prefix and title.startswith(prefix):
                    title = title[len(prefix):].strip()
                out.append((t["topic_id"], title))
        return out

    def _escalation_target_ok(self, topic_id: str) -> bool:
        # 名册里的 managed 会话，或总线自己（总线也会找主人）。看名册本身：新账本第一轮还没同步 topics 表
        if topic_id == BUS_TOPIC_ID:
            return True
        if self.roster is not None and (s := self.roster.by_topic(topic_id)) is not None:
            return s.mode == MANAGED
        t = self.store.get_topic(topic_id)
        if t is None or t["state"] == sm.CLOSED:
            return False
        # 总线新开的会话也会找主人；没有名册时退回看账本里的 adopted
        return t["kind"] == DYNAMIC or (self.roster is None and t["kind"] == ADOPTED)

    def _owner_reply(self, m: Message) -> RouteDecision:
        """主人在「找主人」聊天里的回复：引用了哪条就投给哪条的会话；没引用时，未结的全部属于同一个会话
        （含只有一条）就投给它、回答其中最近的一条；未结分属不同会话或没有未结时投总线并附上列表。
        不走安全闸（发送人已由 IM 账号核实）。"""
        opens = open_escalations(self.store, self.cfg.escalation.open_hours)  # 按提问时间倒序
        tags = [OWNER_VERIFIED_TAG]
        d = RouteDecision(DISPATCH, "owner_reply", tags)
        if m.reply_to:
            # 7.15：有引用就按引用走，绝不当成没引用去匹配未结问题
            target = self.store.get_escalation(m.reply_to)
            if target is None:
                out = self.store.get_bot_outbox(m.reply_to)
                if out is not None:
                    self.store.upsert_message(m, d.route, d.reason, tags, out["topic_id"])
                    text = " ".join((out["text"] or "").split())
                    self.store.set_note(m.message_id, f"回复的是 topic `{out['topic_id']}` 发给主人的消息（{out['sent_at']}）："
                                                      f"{text[:120] + ('…' if len(text) > 120 else '')}")
                    log.info("主人的回复 %s 引用了 %s 的消息 %s，送回", m.message_id, out["topic_id"], m.reply_to)
                    return d
                self.store.upsert_message(m, d.route, d.reason, tags, BUS_TOPIC_ID)
                q = self.store.get_message(m.reply_to)
                quoted = " ".join((q["content"] or "").split())[:200] if q is not None else "（原文不在账本里）"
                self.store.add_system_message(
                    BUS_TOPIC_ID, "escalation_list",
                    f"主人引用回复了一条认不出来源的消息（message_id `{m.reply_to}`）：{quoted}\n"
                    f"主人的回复是 `{m.message_id}`。判断它属于哪个会话后用 "
                    + with_cli(f"`sheepdog forward --topic <topic> --message-ids {m.message_id}` 转交；判断不了就问主人。",
                               self.cfg.cli_command))
                log.info("主人的回复 %s 引用了认不出来源的消息 %s，投总线", m.message_id, m.reply_to)
                return d
        else:
            target = opens[0] if opens and len({e["topic_id"] for e in opens}) == 1 else None
            # 没引用：未结全属同一会话（含只有一条）就投给它，关闭最近一条（与 forward 一致）
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
        lines.append(with_cli("判断它回答的是哪一条后，用 `sheepdog forward --topic <topic> --message-ids " + m.message_id
                              + "` 转交。", self.cfg.cli_command))
        self.store.add_system_message(BUS_TOPIC_ID, "escalation_list", "\n".join(lines))
        log.info("主人的回复 %s 无法确定回答哪条（未结 %d 条），投总线", m.message_id, len(opens))
        return d

    def _handle_change(self, old, m: Message, ctx: RouteContext) -> bool:
        """编辑 / 撤回：按原消息的路由级别处理（设计稿 2.2.1）。返回是否有变化。"""
        content_changed = (old["content"] or "") != (m.content or "")
        recalled = m.deleted and not old["deleted"]
        if not content_changed and not recalled:
            return False
        me = self.cfg.self_open_id
        if me and m.sender_id == me and m.chat_id not in self.cfg.escalation.chat_ids:
            # 主人编辑 / 撤回自己的发言：只更新账本，按 7.13 的去向送一条背景（7.13 补充）
            self.store.update_content(m.message_id, m)
            self._owner_edit(old, m, recalled)
            return True
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
    def __init__(self, cfg: Config, store: Store, sink: Sink | dict, roster: Roster | None = None,
                 security: SecurityConfig | None = None, acker=None, notifier=None):
        self.cfg = cfg
        self.store = store
        # 按宿主取 sink（7.18）：{antigravity: ..., codex: ..., claude: ...}；只给一个时就是 antigravity 的
        self.sinks: dict[str, Sink] = dict(sink) if isinstance(sink, dict) else {ANTIGRAVITY: sink}
        # 经机器人给主人发一条私聊（登录失效提醒等）；notifier(text)，或 [notify] file 时的 FileNotifier（notify(text, 类型)）；
        # 没有就只记日志
        self.notifier = notifier
        self._host_warned: set[str] = set()
        # 确认表情（7.9）：投递成功后以主人身份点表情
        self.acker = acker
        self.roster = roster or Roster()
        # 只用于批次里渲染警示（规则说明）；判定在 Collector 入账时完成
        self.security = security or SecurityConfig()
        # 业务文字每次组装 prompt 时从 playbook 现读，改完下一批生效
        self.playbook = Playbook.from_config(cfg)
        # 这一轮里各会话的记录文件（护栏用，每轮清空）
        self._record_cache: dict[tuple[str, str], Path | None] = {}

    @property
    def sink(self) -> Sink:
        return self.sinks[ANTIGRAVITY]

    # ---------- 宿主（7.18） ----------
    @staticmethod
    def topic_host(t) -> str:
        return (t["host"] if t is not None and "host" in t.keys() else None) or ANTIGRAVITY

    def sink_for(self, t) -> Sink:
        host = self.topic_host(t)
        sink = self.sinks.get(host)
        if sink is None:
            raise SinkError(f"宿主 {host} 没开（配置里 [hosts.{host}] enabled = false），{t['topic_id']} 的信号排队")
        return sink

    def model_effort(self, t) -> tuple[str, str]:
        """这个会话这一轮用的模型和档位：会话自己的，留空就用宿主的默认值（每轮现取配置，改了下一轮生效）。"""
        host = self.topic_host(t)
        model = (t["model"] if t is not None and "model" in t.keys() else "") or ""
        effort = (t["effort"] if t is not None and "effort" in t.keys() else "") or ""
        if host == ANTIGRAVITY:
            return model or self.cfg.session.model, ""
        h = self.cfg.hosts[host]
        return model or h.model, effort or h.effort

    def _send(self, t, content: str, batch_id: str = "") -> None:
        sink = self.sink_for(t)
        if self.topic_host(t) == ANTIGRAVITY:
            sink.send_message(t["conversation_id"], content)  # agentapi 投递行为不变
            return
        model, effort = self.model_effort(t)
        sink.send_message(t["conversation_id"], content, batch_id=batch_id, topic_id=t["topic_id"],
                          model=model, effort=effort)

    def _new_conversation(self, host: str, topic_id: str, title: str, prompt: str, model: str, effort: str,
                          batch_id: str = "") -> str:
        """新建会话。batch_id：开场这一轮属于哪个批次（命令行宿主从这一轮的回答里取回执块时用）。"""
        sink = self.sinks.get(host)
        if sink is None:
            raise SinkError(f"宿主 {host} 没开（配置里 [hosts.{host}] enabled = false）")
        if host == ANTIGRAVITY:
            cid = sink.new_conversation(title, prompt, model or self.cfg.session.model)  # agentapi 行为不变
        else:
            h = self.cfg.hosts[host]
            cid = sink.new_conversation(title, prompt, model or h.model, effort or h.effort, topic_id=topic_id,
                                        batch_id=batch_id)
        self._read_from_start(topic_id, cid)
        return cid

    def conversation_host(self, conversation_id: str) -> str:
        """一个会话编号属于哪个宿主（退休前任时用）：账本里查不到就当 Antigravity（P0.5 以来的老会话）。"""
        return self.store.host_of_conversation(conversation_id) or ANTIGRAVITY

    def _predecessor_paths(self, conversation_id: str) -> tuple[str, str]:
        """前任会话的记录文件和目录，写进接手会话的开场（successor.md）。"""
        if not conversation_id:
            return "", ""
        host = self.conversation_host(conversation_id)
        sink = self.sinks.get(host)
        if host != ANTIGRAVITY and hasattr(sink, "record_files"):
            files = sink.record_files(conversation_id)
            return (str(files[-1]), str(files[-1].parent)) if files else ("", "")
        return str(transcript_path(conversation_id)), str(conversation_dir(conversation_id))

    def _busy(self, t) -> str:
        """命令行宿主这一轮能不能开：宿主不可用、会话还在跑、并发已满时返回排队原因。"""
        sink = self.sinks.get(self.topic_host(t))
        if sink is None:
            return f"宿主 {self.topic_host(t)} 没开（[hosts.{self.topic_host(t)}] enabled = false）"
        busy = getattr(sink, "busy", None)
        return busy(t["conversation_id"] or "") if busy else ""

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
            if t is not None and t["kind"] == DYNAMIC and t["state"] != sm.CLOSED:
                log.error("名册条目 %s 与总线新开的会话同名，先 sheepdog close-session --topic %s 再登记；本轮跳过",
                          s.key, s.topic_id)
                continue
            if t is None:
                self.store.create_topic(s.topic_id, s.display_title, s.kind, s.duty, sm.ACTIVE, s.host, s.model,
                                        s.effort)
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
                "model": s.model or None, "effort": s.effort or None,
            }
            # 已有会话不能换宿主（7.18）：要换就退休旧的、新开一个，走接手流程
            host = self.topic_host(t)
            if host != s.host:
                if t["conversation_id"] and cid == t["conversation_id"]:
                    if s.topic_id not in self._host_warned:
                        self._host_warned.add(s.topic_id)
                        log.error("名册条目 %s 的 host 从 %s 改成了 %s：已有会话不能换宿主，沿用 %s；"
                                  "要换就退休旧会话、新开一个（接手流程）", s.key, host, s.host, host)
                else:
                    fields["host"] = s.host
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
        adopted = t["kind"] in MANAGED_KINDS
        p = self.receipt_path(t["topic_id"], batch)
        if p.exists():
            receipt = json.loads(p.read_text(encoding="utf-8"))
            status = receipt.get("status", "handled")
            # adopted 会话由名册决定存续，总线是常驻入口：两者的回执 done 都不关会话，按 handled 处理；
            # dynamic 会话是为一件事开的，done 就收掉（7.10）
            if (t["kind"] == ADOPTED or t["topic_id"] == BUS_TOPIC_ID) and status == "done":
                status = "handled"
            event = sm.RECEIPT_EVENTS.get(status, "receipt_handled")
            new_state = sm.transition(t["state"], event)
            self.store.finish_dispatch(batch, "acked", json.dumps(receipt, ensure_ascii=False))
            self.store.mark_batch_state(batch, "acked")
            fields = {"state": new_state, "pending_batch_id": None, "retries": 0,
                      "summary": receipt.get("summary", t["summary"])}
            self._watches_after_receipt(t["topic_id"], batch, receipt.get("anchors") or [])
            if anchors := self._receipt_anchors(t, receipt):
                fields["anchors_json"] = anchors
            self.store.update_topic(t["topic_id"], **fields)
            self._receipt_duty(t["topic_id"], batch, receipt)
            log.info("回执 %s/%s: %s -> %s", t["topic_id"], batch, status, new_state)
            if t["kind"] == DYNAMIC and new_state == sm.CLOSED:
                self._dynamic_closed(t["topic_id"], "回执 done")
            return
        sink = self.sinks.get(self.topic_host(t))
        if sink is not None and hasattr(sink, "run_active") and sink.run_active(batch):
            return  # 命令行宿主这一轮还在后台跑（或等重试）：跑完由 _finish_run 收尾，不按回执超时算
        sent = _parse(t["dispatched_at"])
        if sent and _now() - sent > timedelta(minutes=self.cfg.session.receipt_timeout_minutes):
            if adopted:
                self._miss(t, batch)
            else:
                self._fail(t, batch, RECEIPT_TIMEOUT, ATTN_RECEIPT_TIMEOUT)

    @staticmethod
    def _receipt_anchors(t, receipt: dict) -> str:
        """回执带来的锚点并进会话的锚点，返回新的 anchors_json；没有新锚点返回空串。

        回执带来的锚点只作记录；聊天归属（oc_ 开头）只能由名册 / new-session 决定，回执改不了；
        watch_done:N 是确认等待用的，不存。"""
        if not isinstance(receipt.get("anchors"), list) or not receipt["anchors"]:
            return ""
        extra = {a for a in receipt["anchors"] if isinstance(a, str) and not a.startswith(("oc_", "watch_done:"))}
        anchors = sorted(set(json.loads(t["anchors_json"] or "[]")) | extra)
        return json.dumps(anchors, ensure_ascii=False)

    def _dynamic_closed(self, topic_id: str, why: str) -> None:
        """dynamic 会话收掉：聊天归属随之释放（之后的消息回总线），总线下一批附最新名册。"""
        self.store.set_meta("roster_notify_bus", "1")
        log.info("总线新开的会话 %s 已收掉（%s），聊天归属释放", topic_id, why)

    def _watches_after_receipt(self, topic_id: str, batch_id: str, anchors: list) -> None:
        """会话回执后处理它的等待（7.16）：anchors 里 watch_done:N → 关闭（done）；
        回复随这个批次送达、但没被确认的 → 回到 waiting，从回复时间重新计时。"""
        done = set()
        for a in anchors:
            if isinstance(a, str) and a.startswith("watch_done:") and a.removeprefix("watch_done:").isdigit():
                done.add(int(a.removeprefix("watch_done:")))
        for w in self.store.open_watches():
            if w["topic_id"] != topic_id:
                continue
            if w["id"] in done:
                self.store.close_watch(w["id"], "done")
                log.info("等待 #%s 经回执确认已等到", w["id"])
            elif w["status"] == "replied" and w["last_reply_message_id"]:
                r = self.store.get_message(w["last_reply_message_id"])
                if r is not None and r["batch_id"] == batch_id:
                    self.store.reset_watch_waiting(w["id"])
                    log.info("等待 #%s 的回复没被确认，回到 waiting", w["id"])

    def _miss(self, t, batch: str) -> None:
        """adopted 会话回执超时：只记一次未回执、回到可投递，不重投（避免往大上下文里重复灌消息）。"""
        self._watches_after_receipt(t["topic_id"], batch, [])
        self.store.finish_dispatch(batch, "missed", error=f"{RECEIPT_TIMEOUT}（adopted 不重投）")
        self.store.mark_batch_state(batch, "missed")
        missed = (t["receipt_missed"] or 0) + 1
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "receipt_missed"),
                                pending_batch_id=None, receipt_missed=missed)
        log.info("topic %s 批次 %s 未回执（累计 %d 次），不重投", t["topic_id"], batch, missed)

    def _fail(self, t, batch: str | None, error: str, reason: str) -> None:
        """投递失败一次：批次退回排队，累计 MAX_RETRIES 次标 attention。reason 是这一次失败的原因（ATTN_*），
        进 attention 时记进 topics.attention_reason——连续几次原因不同时，以压垮它的最后一次为准。"""
        retries = (t["retries"] or 0) + 1
        state = sm.transition(t["state"], "error") if sm.can(t["state"], "error") else t["state"]
        if batch:
            self.store.finish_dispatch(batch, "failed", error=error)
            self.store.requeue_batch(batch)
        if retries >= MAX_RETRIES and sm.can(state, "retries_exhausted"):
            state = sm.transition(state, "retries_exhausted")
        self._set_state(t, state, reason, error, pending_batch_id=None, retries=retries)
        log.warning("topic %s 投递失败(%s)，第 %d 次，状态 %s", t["topic_id"], error, retries, state)

    @staticmethod
    def _exhaust(state: str) -> str:
        """这一轮不再重试：running → failed → attention（走得通几步走几步）。"""
        for ev in ("error", "retries_exhausted"):
            if sm.can(state, ev):
                state = sm.transition(state, ev)
        return state

    # ---------- 卡在 attention：告知主人、自动恢复（7.21） ----------
    def _set_state(self, t, state: str, reason: str, detail: str = "", **fields) -> None:
        """改会话状态。从别的状态进入 attention 时记下时间和原因（reason 是 ATTN_*），并私聊主人一次。"""
        entering = state == sm.ATTENTION and t["state"] != sm.ATTENTION
        if entering:
            fields.update(attention_at=now_iso(), attention_reason=reason)
        self.store.update_topic(t["topic_id"], state=state, **fields)
        if entering:
            nt = self.store.get_topic(t["topic_id"])
            if auto_recovers(reason):
                self._mark_activity_start(nt)
            self._attention_entered(nt, detail)

    def _last_alive(self, t) -> tuple[datetime | None, str]:
        """会话最后一次有回应的时间和依据：最近一次回执，或会话记录里最后一次动作，取晚的那个。"""
        best: tuple[datetime | None, str] = (None, "")
        d = self.store.last_receipt(t["topic_id"])
        if d is not None:
            at = None
            try:
                at = _parse(str(json.loads(d["receipt"]).get("received_at") or ""))
            except (json.JSONDecodeError, AttributeError):
                pass
            if at is None:
                p = self.receipt_path(t["topic_id"], d["batch_id"])
                try:
                    at = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
                except OSError:
                    at = None
            if at is not None and at.tzinfo is not None:
                best = (at, "最后一次回执")
        path = self._record_file(t)
        at = last_activity_at(self.topic_host(t), path) if path is not None else None
        if at is not None and (best[0] is None or at > best[0]):
            best = (at, "会话记录里最后一次动作")
        return best

    def _attention_entered(self, t, detail: str = "") -> None:
        """会话进入 attention（不论原因）：经机器人私聊主人一次。事实在这里拼，对人的说明在 playbook 的 attention_notice.md。"""
        tid = t["topic_id"]
        key = attention_notice_key(tid)
        if self.store.get_meta(key) == t["attention_at"]:
            return
        host = HOST_LABEL.get(self.topic_host(t), self.topic_host(t))
        title = self._session_title(t) or tid
        reason = attention_reason_label(t["attention_reason"])
        since_dt, basis = self._last_alive(t)
        since = display_time(since_dt, self.cfg.timezone_offset)
        queued = self.store.count_pending_signals(tid)
        auto = auto_recovers(t["attention_reason"])
        if auto:
            how = ("会自动恢复：它交来任何一批的回执（包括已经判超时的那批）"
                   + ("，或会话记录里又有新动作" if self._record_file(t) is not None
                      and self.topic_host(t) in (ANTIGRAVITY, "codex") else "")
                   + "，sheepdog 就自动解除、补送排队的信号。")
        else:
            how = f"不会自动恢复：处理好以后用 `sheepdog session-reset --topic {tid}` 复位，排队的信号随后补送。"
        detail = " ".join((detail or "").split())
        lines = [f"🐕 汇报 · 会话卡住了 [{BUS_TOPIC_ID}]",
                 f"「{title}」（topic `{tid}`，{host}）进入 attention：{reason}"
                 + (f"（累计失败 {t['retries']} 次）" if t["retries"] else "")
                 + (f"，{detail[-200:]}" if detail and detail != reason else "") + "。",
                 (f"从{since}起没回应（依据：{basis}）" if since else "没有回执或动作记录，看不出从几点起没回应")
                 + f"；排着 {queued} 条信号，暂不投递。",
                 how]
        why = self.playbook.render("attention_notice.md", session_title=title, topic_id=tid, host=host, reason=reason,
                                   since=since, queued=str(queued), auto_recover="yes" if auto else "no")
        if why:
            lines.append(why)
        log.warning("会话 %s 进入 attention（%s），%d 条信号排队", tid, t["attention_reason"], queued)
        if self._notify_owner("\n".join(lines), "attention 通知"):
            self.store.set_meta(key, t["attention_at"] or "")

    def _mark_activity_start(self, t) -> None:
        """回执超时进 attention 时，「新动作」的读取偏移从记录现在的末尾开始（之后只看新写的行）。"""
        path = self._record_file(t)
        if path is None or not t["conversation_id"]:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        self.store.set_cursor(t["topic_id"], ACTIVITY, t["conversation_id"], str(path), size)

    def _activity_since(self, t) -> datetime | None:
        """进 attention 之后会话记录里的第一个新动作（7.21）。复用护栏的增量读；时间早于 attention_at 的不算。"""
        if self.topic_host(t) not in (ANTIGRAVITY, "codex"):
            return None  # 没有能认的记录，只认晚到的回执
        since = _parse(t["attention_at"] or "")
        if since is None or since.tzinfo is None:
            return None
        found: list[datetime] = []
        host = self.topic_host(t)

        def handle(_off: int, d: dict) -> None:
            at = activity_at(host, d)
            if at is not None and at > since:
                found.append(at)

        self._follow(t, ACTIVITY, handle)
        return min(found) if found else None

    def _late_receipts(self, t) -> list[tuple[str, dict]]:
        """判过回执超时的批次后来又交了回执：回执存进那一行 dispatch、标 acked_late；那批里退回排队、还没再投出去的
        消息会话已经处理过，改记为已回执、不再重投；回执里的等待确认和锚点照常处理。返回 [(批次号, 回执)]，按发送时间。"""
        out: list[tuple[str, dict]] = []
        for d in self.store.timed_out_dispatches(t["topic_id"], RECEIPT_TIMEOUT):
            p = self.receipt_path(t["topic_id"], d["batch_id"])
            if not p.exists():
                continue
            try:
                raw = p.read_text(encoding="utf-8")
                receipt = json.loads(raw)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("晚到的回执 %s/%s 读不了，先跳过: %s", t["topic_id"], d["batch_id"], e)
                continue
            if not isinstance(receipt, dict):
                receipt = {}
            self.store.finish_dispatch(d["batch_id"], ACKED_LATE, raw, error=d["error"] or "")
            n = self.store.ack_late_messages(d["batch_id"], json.loads(d["message_ids"] or "[]"))
            anchors = receipt.get("anchors") if isinstance(receipt.get("anchors"), list) else []
            self._watches_after_receipt(t["topic_id"], d["batch_id"], anchors)
            if new := self._receipt_anchors(t, receipt):
                self.store.update_topic(t["topic_id"], anchors_json=new)
                t = self.store.get_topic(t["topic_id"])
            self._receipt_duty(t["topic_id"], d["batch_id"], receipt)
            log.info("回执晚到 %s/%s: %s（判超时以后才到；其中 %d 条消息不再重投）", t["topic_id"], d["batch_id"],
                     receipt.get("status", "-"), n)
            out.append((d["batch_id"], receipt))
        return out

    def _check_recovery(self, t) -> None:
        """会话恢复的证据（7.21）：晚到的回执，或进 attention 之后会话记录里又有新动作。

        - 因回执超时卡在 attention 的：有证据就自动解除、回到可投递，这一轮就补送排队的信号；
        - 因回执超时 failed、或已经重投在等回执的：晚到的回执清零失败计数（同一次正常回执）；
        - 其他原因的 attention 不自动解，晚到的回执照样记下。
        """
        if t is None or t["state"] == sm.CLOSED:
            return
        late = self._late_receipts(t)
        t = self.store.get_topic(t["topic_id"])
        if t["state"] == sm.ATTENTION:
            if not auto_recovers(t["attention_reason"]):
                return
            if late:
                evidence = f"晚到的回执（批次 {'、'.join(b for b, _ in late)}）"
            elif (at := self._activity_since(t)) is not None:
                evidence = f"会话记录在 {display_time(at, self.cfg.timezone_offset)} 又有新动作"
            else:
                return
            self._recover(t, evidence, late[-1][1] if late else None)
        elif late and t["kind"] not in MANAGED_KINDS:
            fields: dict = {"retries": 0}
            if t["state"] == sm.FAILED:
                fields["state"] = sm.transition(t["state"], "reset")
            if not t["pending_batch_id"]:
                fields["summary"] = late[-1][1].get("summary", t["summary"])
            self.store.update_topic(t["topic_id"], **fields)

    def _recover(self, t, evidence: str, receipt: dict | None) -> None:
        tid = t["topic_id"]
        state = sm.ACTIVE
        fields: dict = {"retries": 0, "pending_batch_id": None, "attention_at": None, "attention_reason": None}
        if receipt is not None:
            # 照回执本来会带来的状态走（handled → active，needs_decision → waiting_human …），都可投递
            status = receipt.get("status", "handled")
            if (t["kind"] == ADOPTED or tid == BUS_TOPIC_ID) and status == "done":
                status = "handled"
            target = sm.TRANSITIONS.get(sm.RECEIPT_EVENTS.get(status, "receipt_handled"), {}).get(sm.RUNNING)
            state = target if target in sm.DISPATCHABLE else sm.ACTIVE
            fields["summary"] = receipt.get("summary", t["summary"])
        self.store.update_topic(tid, state=state, **fields)
        queued = self.store.count_pending_signals(tid)
        log.warning("会话恢复，自动解除 attention（依据：%s）：%s -> %s，%d 条信号这一轮补送", evidence, tid, state, queued)
        key = attention_notice_key(tid)
        notified = bool(t["attention_at"]) and self.store.get_meta(key) == t["attention_at"]
        self.store.set_meta(key, "")
        if not notified:
            return
        host = HOST_LABEL.get(self.topic_host(t), self.topic_host(t))
        title = self._session_title(t) or tid
        text = (f"🐕 汇报 · 会话已恢复 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）已恢复，补送 {queued} 条。"
                f"依据：{evidence}。")
        why = self.playbook.render("attention_recovered.md", session_title=title, topic_id=tid, host=host,
                                   evidence=evidence, queued=str(queued))
        self._notify_owner(text + (f"\n{why}" if why else ""), "恢复通知")

    # ---------- 人类接管 ----------
    def _check_human(self, t) -> None:
        if not t["conversation_id"]:
            return
        sink = self.sinks.get(self.topic_host(t))
        if sink is None:
            return
        last = sink.last_human_activity(t["conversation_id"])
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
                                  self.cfg.prompt_overlay(), w.remind_minutes, w.expire_minutes,
                                  open_dynamic_topics(self.store))
        cid = self._new_conversation(self.topic_host(t), t["topic_id"], t["title"], prompt, "", "")
        self.store.update_topic(t["topic_id"], conversation_id=cid, rules_hash=self.rules_hash(t),
                                compacted_at=None)
        self.store.set_meta("roster_notify_bus", "0")
        log.info("新建 session %s -> %s", t["title"], cid)
        return self.store.get_topic(t["topic_id"])

    # ---------- 规则同步（7.11） ----------
    def _rules_session(self, t) -> RosterSession | None:
        if t["kind"] == ADOPTED:
            return self.roster.by_topic(t["topic_id"])
        if t["kind"] == DYNAMIC:
            chats = []
            for a in json.loads(t["anchors_json"] or "[]"):
                if a.startswith("oc_"):
                    cid, _, flag = a.partition(":")
                    chats.append(RosterChat(cid, "", flag == "all"))
            return RosterSession(key=t["topic_id"].removeprefix("tp_"), mode=MANAGED, title=t["title"],
                                 duty=t["duty"] or "", chats=chats, host=self.topic_host(t))
        return None

    def standing_rules(self, t, for_hash: bool = False) -> str:
        """该 topic 现行的常驻规则（每次现读 playbook）。总线算指纹时名册用占位，名册变化另走「名册更新」。"""
        w = self.cfg.watch
        if t["topic_id"] == BUS_TOPIC_ID:
            roster_text = ROSTER_HASH_STUB if for_hash else roster_table(self.roster, open_dynamic_topics(self.store))
            return standing_rules_bus(self.playbook, self.cfg.session.bus_title, roster_text,
                                      self.cfg.prompt_overlay(), w.remind_minutes, w.expire_minutes)
        s = self._rules_session(t)
        return standing_rules_managed(self.playbook, s, w.remind_minutes, w.expire_minutes) if s else ""

    def rules_hash(self, t) -> str:
        text = self.standing_rules(t, for_hash=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

    def rules_status(self, t) -> str:
        if t["topic_id"] != BUS_TOPIC_ID and t["kind"] not in MANAGED_KINDS:
            return ""
        if t["compacted_at"]:
            return f"上下文 {t['compacted_at']} 被压缩过（下一批带上完整规则）"
        if not t["rules_hash"]:
            return "未记录（下一批带上完整规则）"
        return "最新" if t["rules_hash"] == self.rules_hash(t) else "待更新（下一批带上完整规则）"

    def push_rules(self, topic_id: str | None = None) -> dict[str, str]:
        """不等新消息，立即给指定 / 全部会话发一次完整现行规则，并更新指纹。"""
        if topic_id:
            t = self.store.get_topic(topic_id)
            if t is None:
                raise ValueError(f"topic {topic_id} 不存在")
            targets = [t]
        else:
            targets = [t for t in self.store.list_topics() if t["topic_id"] == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS]
        report: dict[str, str] = {}
        for t in targets:
            tid = t["topic_id"]
            if tid != BUS_TOPIC_ID and t["kind"] not in MANAGED_KINDS:
                report[tid] = f"跳过：{t['kind']} 会话不投递"
            elif t["state"] == sm.CLOSED:
                report[tid] = "跳过：已关闭"
            elif not t["conversation_id"]:
                report[tid] = "跳过：还没有会话"
            elif t["kind"] == ADOPTED and not t["onboarded_at"]:
                report[tid] = "跳过：还没 onboarding（onboarding 本身含现行规则）"
            elif not (rules := self.standing_rules(t)):
                report[tid] = "跳过：没有可发的规则"
            elif busy := self._busy(t):
                report[tid] = f"排队：{busy}（下一批会带上完整规则）"
            else:
                try:
                    self._send(t, rules_update_message(rules, self._compaction_note(t)))
                except SinkError as e:
                    report[tid] = f"失败：{e}"
                    continue
                self.store.update_topic(tid, rules_hash=self.rules_hash(t), compacted_at=None)
                report[tid] = "已发送"
        return report

    def send_onboarding(self, t) -> str:
        """给 adopted 会话发登记通知（只发一次）。作为一个不含消息的批次，等回执或超时后再投消息。"""
        s = self.roster.by_topic(t["topic_id"])
        if s is None:
            raise SinkError(f"{t['topic_id']} 不在名册里，不能 onboarding")
        bid = self._new_batch_id("o", t["topic_id"])
        w = self.cfg.watch
        self._send(t, onboarding_prompt(self.playbook, s, bid, w.remind_minutes, w.expire_minutes), bid)
        ts = now_iso()
        self.store.record_dispatch(bid, t["topic_id"], [])
        self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                pending_batch_id=bid, dispatched_at=ts, onboarded_at=ts, rules_hash=self.rules_hash(t),
                                compacted_at=None)
        log.info("topic %s: 已发 onboarding %s", t["topic_id"], bid)
        return bid

    def _dispatch_topic(self, t, rows) -> dict:
        res = {"pending": len(rows), "sent": 0, "state": t["state"]}
        if not self._deliverable(t):
            log.info("topic %s 状态 %s，%d 条信号排队中", t["topic_id"], t["state"], len(rows))
            return res
        adopted = t["kind"] in MANAGED_KINDS
        if adopted and not t["conversation_id"]:
            log.info("topic %s 等待 sheepdog spawn 新建会话，%d 条信号排队中", t["topic_id"], len(rows))
            res["waiting_spawn"] = True
            return res
        card = self.store.get_card(t["topic_id"])
        if card is not None and card["run_state"] in duty.STUCK and t["state"] != sm.FAILED:
            # 在岗表判为中断 / 失联（7.20 第 6 步）：排队，主人那边已经通知；会话记录一有新动作就恢复投递。
            # failed 照旧重投（重投本身就是在试它还活着没有，连续失败由 7.21 的 attention 接手）
            why = f"在岗表判为{duty.RUN_LABEL[card['run_state']]}（{card['detail']}）"
            log.info("topic %s 排队：%s，%d 条信号", t["topic_id"], why, len(rows))
            res["queued"] = why
            return res
        busy = self._busy(t) if t["conversation_id"] else ""
        if busy:
            # 命令行宿主（7.18）：宿主不可用 / 会话还在跑上一轮 / 并发已满 → 排队，下一轮再看
            log.info("topic %s 排队：%s，%d 条信号", t["topic_id"], busy, len(rows))
            res["queued"] = busy
            return res
        roster_update = ""
        batch_id = ""
        try:
            if t["kind"] == ADOPTED and not t["onboarded_at"]:
                # 第一次收消息前先发 onboarding；消息等它回执或超时后下一轮再投
                res.update(onboarding=self.send_onboarding(t), state=sm.RUNNING)
                return res
            if not adopted:
                t = self._ensure_bus_conversation(t)
                if self.store.get_meta("roster_notify_bus") == "1":
                    roster_update = roster_table(self.roster, open_dynamic_topics(self.store))
            batch_id = self._new_batch_id("b", t["topic_id"])
            # 主人本人的发言（背景）与信号同批；只有背景时不要求回执、不改状态（7.13）
            signals = [r for r in rows if r["reason"] != OWNER_CONTEXT]
            context = [r for r in rows if r["reason"] == OWNER_CONTEXT]
            waiting = t["summary"] if t["state"] == sm.WAITING_HUMAN else ""
            # Inbox 摘要只给总线；adopted 会话只管自己的聊天
            inbox = [] if adopted else self.store.inbox_summary(self.cfg.self_open_id)
            # 规则变了（或从没记录过指纹）：这批最前面带上完整现行规则（7.11）
            new_hash = self.rules_hash(t)
            rules_update = self.standing_rules(t) if new_hash and new_hash != t["rules_hash"] else ""
            # 7.15：有引用的消息都附「↪ 回复的是」（从账本取被引用的那条）
            quotes = {r["message_id"]: quote_line(self.store.get_message(r["reply_to"]))
                      for r in rows if "reply_to" in r.keys() and r["reply_to"]}
            # 在岗表（7.20）：消息里的编号登记在别的会话名下 → 附撞车提醒；总线批次在在岗表变化时附摘要
            notes = self._collision_notes(t, rows)
            digest, digest_fp = self._duty_digest(t) if t["topic_id"] == BUS_TOPIC_ID else ("", "")
            prompt = batch_prompt(self.playbook, t["topic_id"], batch_id, rows, inbox, self._session_title(t),
                                  waiting, receipt_optional=adopted, roster_update=roster_update,
                                  security=self.security, rules_update=rules_update, context_only=not signals,
                                  quotes=quotes, max_lines=self.cfg.session.max_message_lines,
                                  cli_host=self.topic_host(t) != ANTIGRAVITY,
                                  rules_note=self._compaction_note(t) if rules_update else "",
                                  reminder=self.reminder(t), notes=notes, digest=digest)
            # 先记「正在发」再发送（7.16）：发完后若来不及记账就崩溃，下一轮能从 transcript 核对，不会重投
            self.store.record_dispatch(batch_id, t["topic_id"], [r["message_id"] for r in rows], state="sending")
            self._send(t, prompt, batch_id)
        except SinkError as e:
            if batch_id and self.store.get_dispatch(batch_id) is not None:
                self.store.set_dispatch_state(batch_id, "failed", str(e))
            self._fail(t, None, str(e), ATTN_SEND_FAILED)
            res["error"] = str(e)
            return res

        self.store.set_dispatch_state(batch_id, "sent")
        if roster_update:
            self.store.set_meta("roster_notify_bus", "0")
        if digest_fp:
            self.store.set_meta(DIGEST_META, digest_fp)
        if rules_update:
            self.store.update_topic(t["topic_id"], rules_hash=new_hash, compacted_at=None)
            log.info("topic %s: 规则已更新，随批次 %s 送达%s", t["topic_id"], batch_id,
                     "（压缩后重推）" if t["compacted_at"] else "")
        res.update(self._complete_delivery(t, batch_id, signals, context))
        return res

    def _complete_delivery(self, t, batch_id: str, signals: list, context: list, sent_at: str | None = None) -> dict:
        """批次已送达后的记账：标记消息、改会话状态、关闭被回答的「需要你定」、点确认表情。正常发送与崩溃恢复共用。"""
        ids = [r["message_id"] for r in signals]
        ctx_ids = [r["message_id"] for r in context]
        # 主人的回复已送到提问的会话：对应的「需要你定」标记已答
        for r in signals:
            for tag in json.loads(r["tags_json"] or "[]"):
                if tag.startswith(ESCALATION_TAG_PREFIX):
                    self.store.answer_escalation(tag.removeprefix(ESCALATION_TAG_PREFIX), r["message_id"])
        self.store.mark_batch(ctx_ids, batch_id, t["topic_id"], "context")
        out: dict = {"context": len(ctx_ids), "batch_id": batch_id}
        if not signals:
            # 只有背景：不要回执、不改变会话状态、不会超时重投
            self.store.finish_dispatch(batch_id, "context")
            return out
        self.store.mark_batch(ids, batch_id, t["topic_id"], "delivered")
        t = self.store.get_topic(t["topic_id"])
        if sm.can(t["state"], "dispatch"):
            self.store.update_topic(t["topic_id"], state=sm.transition(t["state"], "dispatch"),
                                    pending_batch_id=batch_id, dispatched_at=sent_at or now_iso())
        out.update(sent=len(ids), state=sm.RUNNING)
        if self.acker is not None:
            try:  # 点表情只在真正送达之后；失败不影响投递；背景消息不点
                self.acker.on_delivered(signals)
            except Exception as e:
                log.warning("点确认表情失败（批次 %s）: %s", batch_id, e)
        return out

    def _recover_sending(self) -> None:
        """上一轮「正在发」就中断的批次（7.16）：到会话 transcript 里找批次号——找到就补记已送达；
        找不到标 uncertain，消息不再自动重发，等人工 sheepdog redeliver --batch 确认。"""
        for d in self.store.dispatches_in_state("sending"):
            t = self.store.get_topic(d["topic_id"])
            ids = json.loads(d["message_ids"] or "[]")
            rows = [r for r in (self.store.get_message(i) for i in ids) if r is not None]
            sink = self.sinks.get(self.topic_host(t)) if t is not None else None
            contains = getattr(sink, "transcript_contains", None) or transcript_contains
            if t is not None and t["conversation_id"] and contains(t["conversation_id"], d["batch_id"]):
                self.store.set_dispatch_state(d["batch_id"], "sent")
                self._complete_delivery(t, d["batch_id"], [r for r in rows if r["reason"] != OWNER_CONTEXT],
                                        [r for r in rows if r["reason"] == OWNER_CONTEXT], d["sent_at"])
                log.info("批次 %s 发送中断，但 transcript 里已有，补记为已送达", d["batch_id"])
            else:
                self.store.set_dispatch_state(d["batch_id"], "uncertain", "发送中断，transcript 里找不到这个批次")
                self.store.mark_batch(ids, d["batch_id"], d["topic_id"], "uncertain")
                log.warning("批次 %s（%s）发送中断，不确定是否送达，不自动重发；确认后用 sheepdog redeliver --batch %s",
                            d["batch_id"], d["topic_id"], d["batch_id"])

    # ---------- 命令行宿主收尾（7.18） ----------
    def collect_runs(self) -> list[RunResult]:
        """收各命令行宿主已经结束的运行，按结果处理回执、写锁、登录失效、超时、失败。"""
        done: list[RunResult] = []
        for host, sink in self.sinks.items():
            collect = getattr(sink, "collect_finished", None)
            if collect is None:
                continue
            try:
                results = collect()
            except Exception as e:  # 一个宿主出错不影响别的
                log.warning("收 %s 的运行结果失败: %s", host, e)
                continue
            for r in results:
                try:
                    self._finish_run(r)
                except Exception as e:
                    log.warning("处理 %s 一轮 %s 的结果失败: %s", host, r.batch_id, e)
                done.append(r)
            # 登录失效按宿主的状态提醒，不按某一轮：新建会话时失效的那一轮不经过 _finish_run，也要提醒到
            reason = getattr(sink, "unavailable_reason", None)
            if reason is not None and (why := reason()):
                self._login_notice(host, why)
        self._check_lock_streaks()
        return done

    def _bus_hint(self, reason: str, text: str, about: str = "") -> None:
        """给总线排一条系统提示。about 是提示说的是哪个会话：说的就是总线自己、而总线已经卡在 attention 时不发——
        它收不到，信号只会排在队里（7.21）；这种情况由 _attention_entered 私聊主人。"""
        if about == BUS_TOPIC_ID and (b := self.store.get_topic(BUS_TOPIC_ID)) is not None and b["state"] == sm.ATTENTION:
            log.info("总线自己进了 attention，这条提示（%s）不发给它自己", reason)
            return
        # 提示里的 `sheepdog …` 命令换成配置的 cli_name（会话在自己终端里敲的那个）
        self.store.add_system_message(BUS_TOPIC_ID, reason, with_cli(text, self.cfg.cli_command))

    def _finish_run(self, r: RunResult) -> None:
        t = self.store.get_topic(r.topic_id) if r.topic_id else None
        label = HOST_LABEL.get(r.host, r.host)
        name = (t["title"] if t is not None else "") or r.topic_id or r.conversation_id
        if r.denials:
            log.warning("%s 会话 %s 这一轮被拒的工具 / 请求：%s", label, r.topic_id, ", ".join(r.denials))
        if r.state == "retry_wait":
            return
        d = self.store.get_dispatch(r.batch_id)
        mine = t is not None and d is not None and t["pending_batch_id"] == r.batch_id
        if r.state == "done":
            if d is None or t is None:
                return  # 新建会话的开场、push-rules：没有批次要收
            p = self.receipt_path(t["topic_id"], r.batch_id)
            if not p.exists():
                receipt, bad = extract_receipt(r.answer)
                if receipt is not None:
                    try:
                        write_receipt(self.cfg, t["topic_id"], r.batch_id, receipt)
                        log.info("从 %s 回答里取到回执块 %s/%s", label, t["topic_id"], r.batch_id)
                    except ValueError as e:
                        bad = str(e)
                if bad:
                    log.warning("%s/%s 的回执块格式不对，按没有处理：%s", t["topic_id"], r.batch_id, bad)
            if not p.exists() and mine and t["state"] == sm.RUNNING:
                # 这一轮已经跑完、会话没交回执也没附回执块：不必再等，直接走回执超时的处理
                log.info("%s/%s 这一轮结束了，但没有回执（也没有 %s 块）", t["topic_id"], r.batch_id, RECEIPT_FENCE)
                if t["kind"] in MANAGED_KINDS:
                    self._miss(t, r.batch_id)
                elif t["kind"] != RETIRED:
                    self._fail(t, r.batch_id, "命令行会话这一轮结束时没有回执", ATTN_NO_RECEIPT)
            return
        if mine and t["kind"] == RETIRED:
            # 退休通知没跑完：不会有交接回执了，记失败、提示总线补发（退休通知不自动重发）
            self.store.finish_dispatch(r.batch_id, "failed", error=f"{r.error_kind}: {r.error[-300:]}")
            self.store.update_topic(t["topic_id"], pending_batch_id=None)
            self._bus_hint("host_failed", f"⚠️ 给「{name}」的退休通知没送到（{label} {r.state}/{r.error_kind}）："
                                          f"{r.error[-200:]}；稍后用 `sheepdog retire --topic {t['topic_id']}` 补发")
            return
        if r.state in ("locked", "failed") and r.error_kind in ("locked", "auth"):
            # 写锁冲突视同主人正在用；登录失效整个宿主排队。两种都是这一轮根本没跑：批次退回排队，一律不算失败
            if mine:
                self.store.finish_dispatch(r.batch_id, "failed", error=f"{r.error_kind}：退回排队")
                self.store.requeue_batch(r.batch_id)
                fields = {"pending_batch_id": None}
                if r.batch_id.startswith("o"):
                    fields["onboarded_at"] = None  # 登记通知没送到，下次重发
                if r.error_kind == "locked" and sm.can(t["state"], "human_attach"):
                    fields["state"] = sm.transition(t["state"], "human_attach")
                elif t["state"] == sm.RUNNING:
                    fields["state"] = sm.ACTIVE
                self.store.update_topic(t["topic_id"], **fields)
            if r.error_kind != "auth":  # 登录失效的提醒在 collect_runs 里按宿主状态统一发
                log.info("%s 会话 %s 写锁冲突（有别人在用这个线程），视同主人正在用，批次 %s 退回排队",
                         label, r.topic_id, r.batch_id)
            return
        if r.state == "timeout":
            if mine:
                self.store.finish_dispatch(r.batch_id, "failed", error=r.error)
                self._set_state(t, self._exhaust(t["state"]), ATTN_HOST_TIMEOUT, r.error, pending_batch_id=None)
            self._bus_hint("host_timeout", f"⚠️ {label} 会话「{name}」（topic `{r.topic_id}`）这一轮超时：{r.error}。"
                                           f"会话已标 attention，确认后用 `sheepdog session-reset --topic {r.topic_id}` 复位。",
                           about=r.topic_id)
            return
        if r.error_kind == "config":
            # Codex 配置档读不到 / 解析失败（7.18）：这一轮根本没起 app-server，不重试；批次退回排队、会话标 attention，
            # 等主人修好配置档再 session-reset
            if mine:
                self.store.finish_dispatch(r.batch_id, "failed", error=r.error[-1000:])
                self.store.requeue_batch(r.batch_id)
                self._set_state(t, self._exhaust(t["state"]), ATTN_CONFIG, r.error, pending_batch_id=None)
            self._bus_hint("host_config", f"⚠️ {label} 会话「{name}」（topic `{r.topic_id}`）这一轮没跑起来，配置档有问题："
                                          f"{r.error[-500:]}。批次已退回排队、会话标 attention；修好配置档后用 "
                                          f"`sheepdog session-reset --topic {r.topic_id}` 复位（`sheepdog doctor` 能看到同样的问题）。",
                           about=r.topic_id)
            return
        # 容量满重试用完：批次退回排队、会话标 attention（不再自动重发，免得同一批次一轮轮撞容量），给总线提示；
        # 不自动换模型。其他失败：照普通投递失败处理（退回排队，累计 3 次标 attention）
        if mine and r.error_kind == "capacity":
            self.store.finish_dispatch(r.batch_id, "failed", error=r.error[-1000:])
            self.store.requeue_batch(r.batch_id)
            self._set_state(t, self._exhaust(t["state"]), ATTN_CAPACITY, r.error, pending_batch_id=None)
        elif mine:
            self._fail(t, r.batch_id, f"{label} {r.error_kind}: {r.error[-300:]}", ATTN_HOST_FAILED)
        self._bus_hint("host_failed", f"⚠️ {label} 会话「{name}」（topic `{r.topic_id}`）这一轮失败"
                                      f"（{r.error_kind}）：{r.error[-300:]}", about=r.topic_id)

    def _notify_owner(self, text: str, what: str) -> bool:
        """经机器人给主人发一条私聊（登录失效、写锁满 60 分钟、会话卡住）；没有发送渠道时只记日志。返回发出去没有。

        notifier 有 notify(text, what) 方法的（[notify] file 写文件的那种）连同通知类型一起交给它；
        否则只交正文（经机器人私聊、dry-run 打印、测试里的 list.append）。"""
        if self.notifier is None:
            return False
        text = with_cli(text, self.cfg.cli_command)
        try:
            notify = getattr(self.notifier, "notify", None)
            if callable(notify):
                notify(text, what)
            else:
                self.notifier(text)
        except Exception as e:
            log.warning("给主人发%s失败: %s", what, e)
            return False
        return True

    def _login_notice(self, host: str, reason: str) -> None:
        """登录失效：经机器人给主人发一条「需要你重新登录」，同一宿主 24 小时只发一次。"""
        key = f"login_notice:{host}"
        last = _parse(self.store.get_meta(key))
        if last and _now() - last < timedelta(hours=LOGIN_NOTICE_HOURS):
            return
        self.store.set_meta(key, now_iso())
        label = HOST_LABEL.get(host, host)
        text = (f"🐕 汇报 · 需要你重新登录 {label} [{BUS_TOPIC_ID}]\n"
                f"{label} 会话跑不了：{(reason or '').strip()[-200:]}\n"
                f"在终端跑 `{LOGIN_COMMAND.get(host, host + ' login')}`；之后 sheepdog 会自己再试，期间这个宿主的批次排队。")
        log.warning("需要主人重新登录 %s：%s", label, reason[-200:])
        self._notify_owner(text, "登录提醒")

    def _check_lock_streaks(self) -> None:
        """命令行会话连续写锁冲突满 60 分钟：提示总线一次，并经机器人提醒主人一次（同一段连续冲突各只一次）。

        一段连续冲突从第一次撞锁算起，到有一轮不再撞锁为止；之后再被锁重新计时。
        给主人的提醒另有上限：同一个会话 24 小时内最多一次（被上限挡住的，过了 24 小时、还锁着就补发）。
        """
        for t in self.store.list_topics():
            sink = self.sinks.get(self.topic_host(t))
            since_fn = getattr(sink, "lock_streak_since", None)
            if since_fn is None or not t["conversation_id"] or t["state"] == sm.CLOSED:
                continue
            since = since_fn(t["conversation_id"])
            if since is None or _now() - since < timedelta(minutes=LOCK_ALERT_MINUTES):
                continue
            mark = since.isoformat(timespec="seconds")
            label = HOST_LABEL.get(self.topic_host(t), self.topic_host(t))
            # 机制事实写在这里；为什么会锁、怎么放锁属于对人的说明，放 playbook 的 host_locked.md
            why = self.playbook.render("host_locked.md", host=label, session_title=t["title"], topic_id=t["topic_id"],
                                       since=mark)
            fact = (f"{label} 会话「{t['title']}」（topic `{t['topic_id']}`）从 {mark} 起一直写锁冲突"
                    f"（already has an active writer），信号在排队，一条不丢。")
            key = f"lock_alert:{t['conversation_id']}"
            if self.store.get_meta(key) != mark:
                self.store.set_meta(key, mark)
                self._bus_hint("host_locked", "⚠️ " + fact + (f"\n{why}" if why else ""))
            self._lock_notice(t, mark, f"🐕 汇报 · {label} 会话被占着 [{BUS_TOPIC_ID}]\n{fact}" + (f"\n{why}" if why else ""))

    def _lock_notice(self, t, mark: str, text: str) -> None:
        key = f"lock_notice:{t['conversation_id']}"
        try:
            last = json.loads(self.store.get_meta(key) or "{}")
        except json.JSONDecodeError:
            last = {}
        if last.get("mark") == mark:
            return  # 这一段连续冲突已经提醒过
        at = _parse(last.get("at") or "")
        if at and _now() - at < timedelta(hours=LOCK_NOTICE_HOURS):
            return  # 24 小时上限：还锁着的话，过了 24 小时再提醒
        self.store.set_meta(key, json.dumps({"mark": mark, "at": now_iso()}))
        log.warning("会话 %s 写锁冲突满 %d 分钟，提醒主人", t["topic_id"], LOCK_ALERT_MINUTES)
        self._notify_owner(text, "写锁提醒")

    # ---------- 护栏（7.19） ----------
    def _record_file(self, t) -> Path | None:
        """这个会话在宿主那边的记录文件：命令行宿主问 sink，Antigravity 是 brain/<编号>/…/transcript.jsonl。"""
        cid = t["conversation_id"]
        if not cid:
            return None
        # 同一轮里两种用途共用一次查找（Codex 找 rollout 要遍历 sessions 目录）
        key = (self.topic_host(t), cid)
        if key in self._record_cache:
            return self._record_cache[key]
        sink = self.sinks.get(self.topic_host(t))
        files_fn = getattr(sink, "record_files", None)
        if files_fn is not None:
            files = files_fn(cid)
            path = Path(files[-1]) if files else None
        else:
            path = transcript_path(cid) if self.topic_host(t) == ANTIGRAVITY else None
        self._record_cache[key] = path
        return path

    def _read_from_start(self, topic_id: str, conversation_id: str) -> None:
        """sheepdog 自己刚建的会话：各种用途都从头读（第一轮跑的命令也要审）。记录文件还没有，路径先空着。"""
        if not topic_id or not conversation_id:
            return
        for purpose in (COMPACTION, AUDIT, DUTY):
            self.store.set_cursor(topic_id, purpose, conversation_id, "", 0)

    def _follow(self, t, purpose: str, handle, backfill: int = 0) -> None:
        """增量读一个会话的记录：每个会话、每种用途记一个偏移，只读新写的完整行。

        - 没有偏移，或者偏移记的是别的会话编号（名册改指了一个已有的会话）：从现在的末尾开始，不翻旧账；
          backfill > 0 时（在岗表要知道最近一次动作）先读末尾 backfill 字节，从其中第一个完整行起；
        - sheepdog 自己建的会话（_read_from_start）、记录文件换了、文件变短了：从头读。
        """
        path = self._record_file(t)
        if path is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        tid, cid = t["topic_id"], t["conversation_id"]
        cur = self.store.get_cursor(tid, purpose)
        if cur is None or cur["conversation_id"] != cid:
            start = max(0, size - backfill) if backfill else size
            if 0 < start < size:
                try:
                    with path.open("rb") as f:  # 从中间开始读：跳到下一个完整行的行首
                        f.seek(start)
                        start += len(f.readline())
                except OSError:
                    start = size
            if start >= size:
                self.store.set_cursor(tid, purpose, cid, str(path), size)
                return
            self.store.set_cursor(tid, purpose, cid, str(path), scan_jsonl(path, start, purpose, handle))
            return
        offset = cur["byte_offset"] or 0
        if cur["path"] != str(path) or size < offset:
            offset = 0
        elif offset == size:
            return
        self.store.set_cursor(tid, purpose, cid, str(path), scan_jsonl(path, offset, purpose, handle))

    def _scan_compactions(self) -> None:
        """K2：会话记录里出现了新的压缩点，就清空它的规则指纹，下一批带上完整规则（规则前加一句为什么又发）。"""
        for t in self.store.list_topics():
            if (t["topic_id"] != BUS_TOPIC_ID and t["kind"] not in MANAGED_KINDS) or t["state"] == sm.CLOSED \
                    or not t["conversation_id"]:
                continue
            host = self.topic_host(t)
            found: list[dict] = []
            self._follow(t, COMPACTION, lambda _off, d: found.append(d) if is_compaction(host, d) else None)
            if found:
                self.store.update_topic(t["topic_id"], rules_hash=None, compacted_at=now_iso())
                log.info("topic %s: 检测到压缩（%d 处），下一批带上完整规则", t["topic_id"], len(found))

    def _compaction_note(self, t) -> str:
        if not t["compacted_at"]:
            return ""
        v = dict(session_title=self._session_title(t), topic_id=t["topic_id"])
        return self.playbook.render("rules_after_compaction.md", **v) or DEFAULT_COMPACTION_NOTE

    @staticmethod
    def reminder_key(topic_id: str) -> str:
        """红线文件的 key：总线是 bus，其他是名册 key / 总线新开时起的 key（topic_id 去掉 tp_）。"""
        key = "bus" if topic_id == BUS_TOPIC_ID else topic_id.removeprefix("tp_")
        return key if _KEY_RE.match(key) else ""

    def reminder(self, t) -> str:
        """K1：这个会话的红线（reminders/<key>.md），附在每一批的最末尾；没有文件就空。"""
        key = self.reminder_key(t["topic_id"])
        if not key:
            return ""
        return self.playbook.render(f"{REMINDERS_DIR}/{key}.md", session_title=self._session_title(t),
                                    topic_id=t["topic_id"])

    def _audit_commands(self) -> None:
        """K3：增量扫 [audit] topics 对应会话新跑的命令，命中 danger_patterns 的记下来，再按会话合并报警。"""
        a = self.cfg.audit
        patterns, ignores = a.patterns, a.ignores
        if patterns:
            for tid in a.topics:
                t = self.store.get_topic(tid)
                if t is None or t["state"] == sm.CLOSED or not t["conversation_id"]:
                    continue
                host, cid = self.topic_host(t), t["conversation_id"]

                def handle(off: int, d: dict, tid=tid, host=host, cid=cid) -> None:
                    for c in commands_in(host, d, off):
                        pat = first_match(c.text, patterns)
                        if pat and (skip := first_match(c.text, ignores)):
                            log.info("会话 %s 的命令命中 %s，但也命中忽略规则 %s，不报", tid, pat, skip)
                            continue
                        shown = redact(c.text)
                        if pat and self.store.add_audit_hit(tid, cid, c.step, shown, pat, c.at):
                            log.warning("会话 %s 跑了危险命令（命中 %s）：%s", tid, pat, shown)

                self._follow(t, AUDIT, handle)
        self._flush_audit()

    def _flush_audit(self) -> None:
        """没报过的命中按会话报：上次报警满 10 分钟（或从没报过）才报，期间新命中的攒起来合并成一条。"""
        groups: dict[str, list] = {}
        for h in self.store.pending_audit_hits():
            groups.setdefault(h["topic_id"], []).append(h)
        for tid, hits in groups.items():
            last = _parse(self.store.last_audit_notice(tid))
            if last and _now() - last < timedelta(minutes=AUDIT_MERGE_MINUTES):
                continue
            self._send_audit(tid, hits)
            self.store.mark_audit_notified([h["id"] for h in hits])

    def _send_audit(self, tid: str, hits: list) -> None:
        """经机器人私聊主人；给犯规的会话、[audit] also_notify 的会话各排一条系统提示。事实在这里，措辞在 playbook。"""
        t = self.store.get_topic(tid)
        title = (self._session_title(t) if t is not None else "") or tid
        label = HOST_LABEL.get(self.topic_host(t), self.topic_host(t)) if t is not None else ""
        # 同一条命令（脱敏后一样）只列一行，后面标次数
        seen: dict[str, list] = {}
        for h in hits:
            seen.setdefault(h["command"], []).append(h)
        lines = []
        for cmd, hs in seen.items():
            times = "、".join(dict.fromkeys(local_hhmm(h["ran_at"]) for h in hs if h["ran_at"]))
            lines.append(f"- {times + ' ' if times else ''}`{cmd}`" + (f"（{len(hs)} 次）" if len(hs) > 1 else ""))
        commands = "\n".join(lines)
        notify = [x for x in self.cfg.audit.also_notify if x != tid and self._audit_target_ok(x)]
        cli = self.cfg.cli_command
        # 1. 主人
        offender_ok = t is not None and self._audit_target_ok(tid)
        done = (["已提示这个会话停手"] if offender_ok else []) + ([f"已告诉 {'、'.join(notify)} 别重复操作"] if notify else [])
        self._notify_owner(f"🐕 汇报 · 会话跑了危险命令 [{tid}]\n「{title}」（topic `{tid}`{'，' + label if label else ''}）"
                           f"跑了下面的命令（命中 [audit] danger_patterns）：\n{commands}"
                           + (f"\n{'；'.join(done)}。" if done else ""),
                           "危险命令报警")
        # 2. 犯规的会话
        if offender_ok:
            why = self.playbook.render("audit_offender.md", session_title=title, topic_id=tid, commands=commands)
            text = f"⚠️ sheepdog 在你的会话记录里看到你跑了下面的命令（命中危险命令清单）：\n{commands}"
            self.store.add_system_message(tid, "audit_offender", with_cli(text + (f"\n{why}" if why else ""), cli))
        # 3. 要另外知会的会话
        for x in notify:
            xt = self.store.get_topic(x)
            why = self.playbook.render("audit_notify.md", session_title=self._session_title(xt), topic_id=x,
                                       commands=commands, offender_title=title, offender_topic=tid)
            text = f"⚠️ 「{title}」（topic `{tid}`）跑了下面的命令：\n{commands}"
            self.store.add_system_message(x, "audit_notify", with_cli(text + (f"\n{why}" if why else ""), cli))
        log.warning("危险命令报警：%s 共 %d 条，已知会 %s", tid, len(hits), "、".join(["主人", tid] + notify))

    def _audit_target_ok(self, topic_id: str) -> bool:
        """系统提示只排给还在投递的会话（总线、名册 managed、总线新开的），否则会被退回总线。"""
        t = self.store.get_topic(topic_id)
        return t is not None and t["state"] != sm.CLOSED and (topic_id == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS)

    # ---------- 在岗表（7.20） ----------
    def _duty_round(self, tids: list[str], autofill: bool = True) -> None:
        """第 5 步自动补活、第 4 步算各会话的运行状态并写进卡片；中断、失联的私聊主人。出错不挡投递。"""
        if autofill:
            try:
                self._autofill_work()
            except Exception as e:
                log.warning("在岗表自动补活出错: %s", e)
        for tid in tids:
            t = self.store.get_topic(tid)
            if t is None or t["state"] == sm.CLOSED:
                continue
            try:
                self._duty_scan(t)
                state, detail, since, notify = self._duty_state(t)
                card = self.store.get_card(tid)
                spinning = state == duty.WORKING and card is not None and \
                    (card["sig_repeat"] or 0) >= self.cfg.duty.spin_repeat
                self.store.upsert_card(tid, run_state=state, detail=detail, stuck_since=since or None,
                                       spinning=int(spinning), queued=self.store.count_pending_signals(tid))
                if notify:
                    self._duty_notice(self.store.get_topic(tid), state, detail, since)
            except Exception as e:
                log.warning("算会话 %s 的在岗表卡片出错: %s", tid, e)

    def _duty_scan(self, t) -> None:
        """增量读会话记录（复用护栏的 _follow，用途 duty）：最近一次工具调用、最近一次动作、空转计数、主人发言、压缩点。
        卡片上的动作（在做时叫此刻动作，其他状态叫最近动作）只取工具调用的动作说明（Antigravity 的 toolSummary /
        toolAction，Codex 的命令），不读模型的思考。"""
        host = self.topic_host(t)
        if host not in (ANTIGRAVITY, "codex") or not t["conversation_id"]:
            return
        card = self.store.get_card(t["topic_id"])
        c = dict(card) if card is not None else {}
        state = {"sig": c.get("last_sig"), "rep": c.get("sig_repeat") or 0,
                 "act": _parse(c.get("last_act_at") or ""), "upd": {}}

        def iso(dt: datetime) -> str:
            return dt.astimezone().isoformat(timespec="seconds")

        def handle(off: int, d: dict) -> None:
            upd = state["upd"]
            at = activity_at(host, d)
            for a in tool_actions(host, d, off):
                state["rep"] = state["rep"] + 1 if a.sig == state["sig"] else 1
                state["sig"] = a.sig
                when = parse_ts(a.at) or at
                text = one_sentence(a.summary) if host == ANTIGRAVITY else redact(a.summary, 80)
                upd.update(action=text, action_at=iso(when) if when else now_iso())
                if when is not None and (at is None or when > at):
                    at = when
            if at is not None and (state["act"] is None or at > state["act"]):
                state["act"] = at
            if (h := human_input_at(host, d)) is not None:
                upd["human_at"] = iso(h)
            if is_compaction(host, d):
                upd["compacted_at"] = iso(record_time(host, d) or _now())

        self._follow(t, DUTY, handle, backfill=LAST_ACTIVITY_TAIL)
        upd = state["upd"]
        if state["act"] is not None:
            upd["last_act_at"] = iso(state["act"])
        if state["sig"] != c.get("last_sig") or state["rep"] != (c.get("sig_repeat") or 0):
            upd.update(last_sig=state["sig"], sig_repeat=state["rep"])
        if upd:
            self.store.upsert_card(t["topic_id"], **upd)
            if upd.get("action") and upd["action"] != c.get("action"):
                self.store.add_history(t["topic_id"], "action", upd["action"], upd.get("action_at", ""))

    def _unanswered_since(self, t, last_act: datetime | None) -> datetime | None:
        """会话还没碰过的批次里最早送出的时间：在跑的这一批，以及回执超时、之后会话记录里再没动作的批次。
        session-reset 之前送出的不算；只看 24 小时内。"""
        floor = [x for x in (last_act, _parse(self.store.get_meta(duty_reset_key(t["topic_id"])))) if x is not None]
        floor_dt = max(floor) if floor else None
        now, out = _now(), None
        for d in self.store.recent_dispatches(t["topic_id"]):
            if d["state"] not in ("sent", "missed", "failed"):
                continue
            if d["state"] == "failed" and not (d["error"] or "").startswith(RECEIPT_TIMEOUT):
                continue  # 根本没送到（发送失败）的不算会话没碰
            sent = _parse(d["sent_at"])
            if sent is None or sent.tzinfo is None or now - sent > timedelta(hours=24):
                continue
            if floor_dt is not None and sent <= floor_dt:
                continue
            out = sent if out is None or sent < out else out
        return out

    def _quiet_since(self, t, last_act: datetime | None) -> datetime | None:
        """批次在跑、会话动过以后停下来：会话最后一次动作时手上那一批还没交差，之后送的批次也都没交差（总线回执超时会
        重投同一批），就从最后一次动作算起。不然返回 None。

        没交差：还挂在 topics 上等回执的那一批，或者判过回执超时、退回重投的（failed）。adopted / 总线新开的会话回执
        可选，回执超时记 missed，当作处理过（同 _unanswered_since）；只有背景、发送失败、不确定送没送到的批次不算。
        session-reset 在最后一次动作之后的，不从动作算（reset 清掉的是这次判定）。"""
        if last_act is None:
            return None
        reset = _parse(self.store.get_meta(duty_reset_key(t["topic_id"])))
        if reset is not None and reset >= last_act:
            return None
        before = None
        later_done = False
        for d in self.store.recent_dispatches(t["topic_id"]):  # 新的在前
            sent = _parse(d["sent_at"])
            if sent is None or sent.tzinfo is None:
                continue
            if d["state"] in ("acked", ACKED_LATE, "missed"):
                done = True
            elif d["state"] == "sent" and d["batch_id"] == t["pending_batch_id"]:
                done = False
            elif d["state"] == "failed" and (d["error"] or "").startswith(RECEIPT_TIMEOUT):
                done = False
            else:
                continue
            if sent > last_act:
                later_done = later_done or done
                continue
            before = d
            if done or later_done:
                return None
            break
        return last_act if before is not None else None

    def _silence_start(self, t) -> str:
        """这一段卡住从哪算起：会话最后一次动作和 session-reset 里晚的那个（同一段只通知一次，7.20）。都没有就是空串。"""
        card = self.store.get_card(t["topic_id"])
        at = [x for x in (_parse(card["last_act_at"] or "") if card is not None else None,
                          _parse(self.store.get_meta(duty_reset_key(t["topic_id"])))) if x is not None]
        return max(at).isoformat() if at else ""

    def _duty_state(self, t) -> tuple[str, str, str, bool]:
        """运行状态，按顺序取第一个命中：被主人占着 → 失联 → 中断 → 在做 → 等回复 → 空闲。
        返回 (状态, 判定依据, 中断 / 失联从几点算起, 要不要通知主人)。阈值在 [duty]。"""
        tid, host, now = t["topic_id"], self.topic_host(t), _now()
        dc, tz = self.cfg.duty, self.cfg.timezone_offset
        label = HOST_LABEL.get(host, host)
        card = self.store.get_card(tid)
        c = dict(card) if card is not None else {}
        last_act = _parse(c.get("last_act_at") or "")

        def hm(dt: datetime | None) -> str:
            return clock(dt, tz)

        def mins(delta: timedelta) -> int:
            return int(delta.total_seconds() // 60)

        # 1. 被主人占着：主人在 Antigravity 里亲自说话，或 Codex 应用开着它（写锁冲突）
        if t["state"] == sm.HUMAN_ATTACHED:
            return duty.OCCUPIED, "主人在会话里亲自说话，暂停投递", "", False
        human = _parse(c.get("human_at") or "")
        if human is not None and now - human < timedelta(minutes=self.cfg.session.human_attach_minutes):
            return duty.OCCUPIED, f"主人 {hm(human)} 在会话里说过话", "", False
        sink = self.sinks.get(host)
        lock = getattr(sink, "lock_streak_since", None)
        if lock is not None and t["conversation_id"] and (since := lock(t["conversation_id"])) is not None:
            return duty.OCCUPIED, f"{label} 应用开着它（{hm(since)} 起写锁冲突）", "", False
        # 2. 失联：后台进程没了还没交结果
        if host != ANTIGRAVITY and t["state"] in (sm.FAILED, sm.ATTENTION):
            run = self.store.latest_run_for_topic(tid)
            if run is not None and run["state"] == "failed" and (run["error"] or "").startswith(NO_RESULT):
                return duty.LOST, f"后台进程没了，没交结果（{hm(_parse(run['ended_at'] or ''))}）", \
                    run["ended_at"] or "", False
        # 2、3. 看会话记录：批次送出后完全不动 → 失联；批次在跑但没有新动作 → 中断
        path = self._record_file(t) if host in (ANTIGRAVITY, "codex") else None
        mtime = None
        if path is not None:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                path = None
        if path is not None:
            # since：会话没碰过的批次里最早送出的；或者批次在跑、会话动过以后停下来的那一刻（从那以后手上一直有没交差的
            # 批次——总线回执超时重投同一批，不算重新开始）
            since, untouched = self._unanswered_since(t, last_act), True
            quiet_since = self._quiet_since(t, last_act)
            if quiet_since is not None and now - quiet_since >= timedelta(minutes=dc.stall_minutes) \
                    and (since is None or quiet_since < since):
                since, untouched = quiet_since, False
            if since is not None:
                head = f"批次 {hm(since)} 送出后" if untouched else f"批次在跑，最近动作 {hm(since)}，之后"
                quiet = now - max(mtime, since)
                if quiet >= timedelta(minutes=dc.lost_minutes):
                    return duty.LOST, f"{head}会话记录 {mins(quiet)} 分钟完全不动", since.isoformat(), True
                if now - since >= timedelta(minutes=dc.stall_minutes):
                    return duty.STALLED, f"{head} {mins(now - since)} 分钟没有新动作", since.isoformat(), True
        # 3. 中断：这一轮跑失败、超时被杀（连续失败由 7.21 的 attention 通知主人，这里不重复发）
        if t["state"] == sm.ATTENTION:
            return duty.STALLED, f"卡在 attention：{attention_reason_label(t['attention_reason'])}", \
                t["attention_at"] or "", False
        if t["state"] == sm.FAILED:
            return duty.STALLED, f"这一轮失败（累计 {t['retries'] or 0} 次），下一轮重投", "", False
        # 4. 在做：批次在跑
        run_active = getattr(sink, "run_active", None)
        if t["state"] == sm.RUNNING or (run_active is not None and t["pending_batch_id"]
                                        and run_active(t["pending_batch_id"])):
            sent = _parse(t["dispatched_at"] or "")
            return duty.WORKING, f"批次 {t['pending_batch_id'] or '-'} {hm(sent)} 送出", "", False
        # 5. 等回复：最近回执说在等，或名下有等待中的活
        if t["state"] in (sm.BLOCKED, sm.WAITING_HUMAN):
            what = "等别人" if t["state"] == sm.BLOCKED else "等主人拍板"
            return duty.WAITING, f"最近回执：{what}" + (f"（{duty.short(t['summary'], 40)}）" if t["summary"] else ""), \
                "", False
        waiting = [w for w in self.store.open_work(tid) if w["state"] in duty.WAITING_WORK]
        if waiting:
            w = waiting[0]
            who = w["waiting_on"] or duty.WORK_LABEL[w["state"]]
            return duty.WAITING, f"{w['key']} 等 {who}" + (f"，到 {w['until']}" if w["until"] else "") \
                + (f"（另有 {len(waiting) - 1} 件在等）" if len(waiting) > 1 else ""), "", False
        # 6. 空闲：附上一件做完了什么
        done = self.store.closed_work(tid, 1)
        if done:
            return duty.IDLE, f"上一件：{duty.work_line(done[0])}", "", False
        r = self.store.last_receipt(tid)
        if r is not None:
            try:
                summary = json.loads(r["receipt"]).get("summary") or ""
            except (json.JSONDecodeError, AttributeError):
                summary = ""
            if summary:
                return duty.IDLE, f"上一批回执：{duty.short(summary, 60)}", "", False
        return duty.IDLE, "", "", False

    def _duty_notice(self, t, state: str, detail: str, since: str) -> None:
        """会话判为中断 / 失联（看会话记录判的）：经机器人私聊主人，同一段只发一次（复用 7.21 的告知通道）。
        已经卡在 attention 的不发：7.21 已经通知过。事实在这里拼，对人的说明在 playbook 的 duty_stuck.md。"""
        tid = t["topic_id"]
        if t["state"] == sm.ATTENTION:
            return
        # 「同一段」按会话的沉默算：从最后一次动作（或 session-reset）起，中断、失联各通知一次。重投、回执超时以后
        # 再送的批次让 since 变了、卡片在中断和失联之间来回（投递写进记录、文件又动了），都还是同一段，不重复通知；
        # 会话一动（或 reset）才是新的一段
        seg = self._silence_start(t) or since
        key = duty_notice_key(tid)
        try:
            rec = json.loads(self.store.get_meta(key) or "{}")
        except json.JSONDecodeError:
            rec = {}
        if not isinstance(rec, dict) or rec.get("seg") != seg or not isinstance(rec.get("sent"), list):
            rec = {"seg": seg, "sent": []}
        if state in rec["sent"]:
            return
        host = HOST_LABEL.get(self.topic_host(t), self.topic_host(t))
        title = self._session_title(t) or tid
        label = duty.RUN_LABEL[state]
        card = self.store.get_card(tid)
        queued = self.store.count_pending_signals(tid)
        action = ""
        if card is not None and card["action"]:
            action = f"最近一次工具调用「{card['action']}」（{display_time(_parse(card['action_at'] or ''), self.cfg.timezone_offset)}）。"
        lines = [f"🐕 汇报 · 会话{label}了 [{BUS_TOPIC_ID}]",
                 f"「{title}」（topic `{tid}`，{host}）判为{label}：{detail}。" + action,
                 f"排着 {queued} 条信号，暂不投递。会话记录里一有新动作就自动恢复投递；确认处理好了也可以用 "
                 f"`sheepdog session-reset --topic {tid}` 清掉这次判定。"]
        why = self.playbook.render("duty_stuck.md", session_title=title, topic_id=tid, host=host, state=label,
                                   detail=detail, queued=str(queued))
        if why:
            lines.append(why)
        log.warning("会话 %s 判为%s：%s（%d 条信号排队）", tid, label, detail, queued)
        if self._notify_owner("\n".join(lines), "在岗表通知"):
            rec["sent"].append(state)
            self.store.set_meta(key, json.dumps(rec, ensure_ascii=False))

    def _autofill_work(self) -> None:
        """第 5 步：watch → 等别人；「需要你定」→ 等主人（forward → 已转交在 forward_messages 里当场补）。
        会话自己申报过同一把钥匙的以申报为准，只补缺的字段；来源结束了，自动补的活跟着收掉。"""
        pats = self.cfg.duty.patterns
        expire = self.cfg.watch.expire_minutes
        for w in self.store.open_watches():
            who = self.store.person_name(w["person_id"]) or w["person_id"]
            started = _parse(w["last_reply_at"] or "") or _parse(w["started_at"] or "")
            until = clock(started + timedelta(minutes=expire), self.cfg.timezone_offset) if started else ""
            key = duty.key_for(w["note"] or "", pats, f"watch-{w['id']}")
            duty.auto_work(self.store, w["topic_id"], key, f"watch:{w['id']}", duty.SRC_WATCH, duty.WAITING_OTHER,
                           title=duty.short(w["note"] or "", 40) or f"等 {who} 回复", waiting_on=who, until=until)

        def watch_closed(ref: str) -> str | None:
            wid = ref.removeprefix("watch:")
            w = self.store.get_watch(int(wid)) if wid.isdigit() else None
            if w is None:
                return "等待记录已清理"
            return f"等待结束（{w['close_reason']}）" if w["closed_at"] else None

        duty.close_auto_work(self.store, duty.SRC_WATCH, watch_closed)
        for e in open_escalations(self.store, self.cfg.escalation.open_hours):
            key = duty.key_for(e["text"] or "", pats, f"ask-{e['message_id'][-8:]}")
            duty.auto_work(self.store, e["topic_id"], key, f"escalation:{e['message_id']}", duty.SRC_ESCALATION,
                           duty.WAITING_OWNER, title=escalation_summary(e, 40), waiting_on=duty.OWNER_NAME_FIELD)
        live = {e["message_id"] for e in open_escalations(self.store, self.cfg.escalation.open_hours)}

        def escalation_closed(ref: str) -> str | None:
            mid = ref.removeprefix("escalation:")
            if mid in live:
                return None
            e = self.store.get_escalation(mid)
            return "主人已答" if e is not None and e["answered_at"] else "「需要你定」已过期"

        duty.close_auto_work(self.store, duty.SRC_ESCALATION, escalation_closed)

    def _receipt_duty(self, topic_id: str, batch_id: str, receipt: dict) -> None:
        """回执进在岗表：work 字段同 work set；回执摘要记进履历。"""
        try:
            duty.apply_receipt_work(self.store, topic_id, receipt, log)
            summary = receipt.get("summary") if isinstance(receipt, dict) else ""
            self.store.add_history(topic_id, "receipt",
                                   f"{batch_id} {receipt.get('status', 'handled')}：{duty.short(summary or '', 80)}")
        except Exception as e:
            log.warning("回执 %s/%s 写进在岗表出错: %s", topic_id, batch_id, e)

    def _collision_notes(self, t, rows) -> dict[str, list[str]]:
        """第 3 步撞车检查：消息正文里的编号登记在别的会话名下、没完成没转交 → 给这条消息附提醒。只提醒不拦。"""
        pats = self.cfg.duty.patterns
        out: dict[str, list[str]] = {}
        for r in rows:
            if r["chat_type"] == SYSTEM_CHAT:
                continue
            lines: list[str] = []
            for key in duty.extract_keys(r["content"] or "", pats):
                for w in duty.holders(self.store, key, t["topic_id"]):
                    lines.append(self._collision_line(t, w))
            if lines:
                out[r["message_id"]] = lines
        return out

    def _collision_line(self, t, w) -> str:
        owner = self.store.get_topic(w["topic_id"])
        title = (self._session_title(owner) if owner is not None else "") or w["topic_id"]
        card = self.store.get_card(w["topic_id"])
        run = duty.RUN_LABEL.get(card["run_state"], "") if card is not None and card["run_state"] else ""
        ago = duty.ago_text(card["action_at"] or "") if card is not None else ""
        action = f"，最近动作「{card['action']}」{ago}" if card is not None and card["action"] else ""
        fact = (f"🔀 撞车提醒：{w['key']} 登记在「{title}」（topic `{w['topic_id']}`）名下，"
                f"{duty.WORK_LABEL.get(w['state'], w['state'])}" + (f"，会话{run}" if run else "") + action + "。")
        why = self.playbook.render("duty_collision.md", key=w["key"], owner_title=title, owner_topic=w["topic_id"],
                                   work_state=duty.WORK_LABEL.get(w["state"], w["state"]), run_state=run,
                                   action=card["action"] if card is not None and card["action"] else "", ago=ago,
                                   session_title=self._session_title(t), topic_id=t["topic_id"])
        return fact + (f"\n{why}" if why else "")

    def _duty_digest(self, t) -> tuple[str, str]:
        """总线摘要：在岗表变了（指纹不同）才附，最多 [duty] digest_max_lines 行。返回 (摘要, 指纹)。"""
        cards = duty.board_cards(self.store, self.cfg, exclude=(BUS_TOPIC_ID,))
        if not cards:
            return "", ""
        fp = duty.digest_fingerprint(cards)
        if self.store.get_meta(DIGEST_META) == fp:
            return "", ""
        title = self.playbook.render("duty_digest.md", session_title=self._session_title(t), topic_id=t["topic_id"],
                                     count=str(len(cards))) or DEFAULT_DIGEST_TITLE
        return "\n".join([title, *duty.digest_lines(cards, self.cfg.duty.digest_max_lines)]), fp

    # ---------- 主流程 ----------
    def dispatch_once(self) -> dict:
        # 这一轮里各会话的记录文件（自动恢复和护栏共用）
        self._record_cache = {}
        self._recover_sending()
        self.collect_runs()
        self.sync_roster()
        self.ensure_bus_topic()
        tids = [BUS_TOPIC_ID] + [t["topic_id"] for t in self.store.list_topics()
                                 if t["kind"] in MANAGED_KINDS and t["state"] != sm.CLOSED]
        # 先看卡住的会话有没有恢复（7.21：解开了这一轮就补送），再处理回执与超时（总线超时会把批次退回待推）、
        # 前任交接回执、等待计时，再取待推消息
        for tid in tids:
            try:
                self._check_recovery(self.store.get_topic(tid))
            except Exception as e:  # 自动恢复出错不能挡住投递
                log.warning("检查会话 %s 是否恢复时出错: %s", tid, e)
        for tid in tids:
            self._check_receipt(self.store.get_topic(tid))
        self._check_handovers()
        self._tick_watches(set(tids))
        self._expire_escalations()
        # 在岗表（7.20）：先自动补活（第 5 步），再算各会话的运行状态（第 4 步，等回复要看名下的活）
        self._duty_round(tids)
        # 护栏（7.19）：增量读会话记录，压缩了就清指纹（这一轮的批次就带上完整规则）；危险命令报警（系统提示这一轮就排上）
        for step in (self._scan_compactions, self._audit_commands):
            try:
                step()
            except Exception as e:  # 护栏出错不能挡住投递
                log.warning("护栏检查出错（%s）: %s", step.__name__, e)

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
                if self.store.get_topic(tid)["state"] != t["state"]:
                    # 主人刚来 / 刚走：卡片按新的会话状态重算一遍再决定投不投。不然拿的是这一轮开头算的卡片——
                    # 主人刚走的那一轮卡片还是「被主人占着」，会把本该判中断的会话放过去投递
                    self._duty_round([tid], autofill=False)
            if tid in groups:
                r = self._dispatch_topic(self.store.get_topic(tid), groups[tid])
                result["topics"][tid] = r
                result["sent"] += r["sent"]
        # 投递以后再算一遍卡片（刚送出批次的会话变成在做）；记录只读新写的行，基本没有成本
        self._duty_round(tids, autofill=False)
        result["state"] = self.store.get_topic(BUS_TOPIC_ID)["state"]
        return result

    # ---------- 总线新开会话（7.10） ----------
    def new_session(self, key: str, title: str, duty: str, chats: list[tuple[str, bool]],
                    message_ids: list[str] | None = None, note: str = "", validate_only: bool = False,
                    host: str = "", model: str = "", effort: str = "", quote: str = "") -> dict:
        """总线临时新开一个专属会话：只写账本（kind = dynamic），不写名册。

        先做全部校验（key、宿主、主人原话、聊天归属、配额、待转消息），通过后才新建会话；建好后消息与 note 按 forward
        规则排队。宿主是 codex / claude 且 [hosts.*] ask_owner_before_new = true 时，必须带核对过的主人原话 quote
        （7.18，19:0x），原话写进新会话的开场。
        """
        key, title, duty, note = key.strip(), title.strip(), duty.strip(), (note or "").strip()
        host, model, effort = (host or ANTIGRAVITY).strip(), (model or "").strip(), (effort or "").strip()
        if not _KEY_RE.match(key) or key == "bus":
            raise ValueError("--key 只允许 [a-z0-9_-]，且不能是 bus")
        # 宿主没开直接报错，说清是哪个（7.18）；额度 max_new_sessions_per_day 所有宿主共用
        self.cfg.check_host(host)
        owner_quote = self._owner_consent(host, quote)
        topic_id = "tp_" + key
        if self.roster.by_key(key) is not None:
            raise ValueError(f"key {key!r} 已在名册里")
        if self.store.get_topic(topic_id) is not None:
            raise ValueError(f"{topic_id} 在账本里已存在（含已收掉的），换一个 key")
        if not title or not duty:
            raise ValueError("--title 和 --duty 都必填")
        dyn = dynamic_owners(self.store)
        seen: set[str] = set()
        for cid, _all in chats:
            if not cid.startswith("oc_"):
                raise ValueError(f"--chat {cid!r} 不是聊天 ID")
            if cid in seen:
                raise ValueError(f"--chat {cid} 重复")
            seen.add(cid)
            owner = self.roster.owner_of(cid)
            if owner is not None:
                raise ValueError(f"聊天 {cid} 已归属名册里的 {owner[0].topic_id}")
            if cid in dyn:
                raise ValueError(f"聊天 {cid} 已归属总线新开的 {dyn[cid][0]}")
        quota = self.cfg.bus.max_new_sessions_per_day
        if quota <= 0:
            raise ValueError("配置禁止总线新开会话（[bus] max_new_sessions_per_day = 0），请找主人")
        midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        if self.store.count_topics_since(DYNAMIC, midnight) >= quota:
            raise ValueError(f"今天已新开 {quota} 个会话，达到上限，请找主人")
        ids = [i.strip() for i in (message_ids or []) if i.strip()]
        for i in ids:
            row = self.store.get_message(i)
            if row is None:
                raise ValueError(f"账本里没有消息 {i}")
            if row["security_action"] == HOLD:
                raise ValueError(f"消息 {i} 被安全规则拦截，不能随 new-session 转交；"
                                 + with_cli("先新开会话，再用 `sheepdog forward --quote \"<主人原话>\"` 转",
                                            self.cfg.cli_command))
        if validate_only:
            return {"topic_id": topic_id}

        full_title = f"{self.cfg.session.title_prefix} {title}".strip()
        s = RosterSession(key=key, mode=MANAGED, title=title, duty=duty,
                          chats=[RosterChat(cid, "", al) for cid, al in chats], host=host, model=model, effort=effort)
        prompt = adopted_bootstrap(self.playbook, s, full_title, "", self.cfg.watch.remind_minutes,
                                   self.cfg.watch.expire_minutes, owner_quote=owner_quote)
        cid = self._new_conversation(host, topic_id, full_title, prompt, model, effort)
        ts = now_iso()
        self.store.create_topic(topic_id, title, DYNAMIC, duty, sm.ACTIVE, host, model, effort)
        self.store.update_topic(topic_id, conversation_id=cid, spawned_at=ts, onboarded_at=ts, origin_note=note or None,
                                anchors_json=json.dumps([chat_anchor(c, al) for c, al in chats], ensure_ascii=False))
        self.store.update_topic(topic_id, rules_hash=self.rules_hash(self.store.get_topic(topic_id)), compacted_at=None)
        self.store.set_meta("roster_notify_bus", "1")
        if ids or note:
            forward_messages(self.store, topic_id, ids, note=note, self_open_id=self.cfg.self_open_id,
                             escalation_open_hours=self.cfg.escalation.open_hours,
                             key_patterns=self.cfg.duty.patterns)
        log.info("总线新开会话 %s（%s）-> %s（%d 条消息排队）", topic_id, host, cid, len(ids))
        return {"topic_id": topic_id, "conversation_id": cid, "title": full_title, "queued": len(ids), "host": host}

    def _owner_consent(self, host: str, quote: str) -> tuple[str, str] | None:
        """新开 Codex / Claude 会话前先问主人（7.18，19:0x）：开关打开时 quote 必须是核对过的主人原话。

        返回 (标注, 原话) 写进开场；不需要问（Antigravity，或开关关着）时返回 None；不带或核对不通过抛 ValueError。
        """
        quote = (quote or "").strip()
        if host == ANTIGRAVITY or not self.cfg.hosts[host].ask_owner_before_new:
            return None
        label = HOST_LABEL.get(host, host)
        ask = with_cli(f"新开 {label} 会话要先问主人：发『需要你定』写清为什么用、做什么，拿到主人同意后，"
                       f"用 --quote 带上他的原话再开（`sheepdog new-session ... --host {host} --quote \"<主人原话>\"`）",
                       self.cfg.cli_command)
        if not quote:
            raise ValueError(ask)
        max_age = self.security.quote_max_age_hours if self.security is not None else SecurityConfig().quote_max_age_hours
        source = owner_quote_verifier(self.store, max_age, self.cfg.self_open_id)(quote)
        if not source:
            raise ValueError(f"--quote 核对没通过：这句话在总线里和主人的飞书回复里都找不到原文。{ask}")
        return source, quote

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
        self.cfg.check_host(s.host)
        title = f"{self.cfg.session.title_prefix} {s.display_title}".strip()
        report: dict = {}
        if s.predecessor_conversation_id and s.retire_predecessor:
            report["retire"] = self._retire_predecessor(s, title)
        read_bid = self._new_batch_id("h", s.topic_id) if s.predecessor_conversation_id else ""
        prompt = adopted_bootstrap(
            self.playbook, s, title, read_bid, self.cfg.watch.remind_minutes, self.cfg.watch.expire_minutes,
            *self._predecessor_paths(s.predecessor_conversation_id),
        )
        cid = self._new_conversation(s.host, s.topic_id, title, prompt, s.model, s.effort, read_bid)
        ts = now_iso()
        # bootstrap 已含职责与规则，等同 onboarding
        fields = dict(conversation_id=cid, spawned_at=ts, onboarded_at=ts, pending_batch_id=None,
                      state=sm.ACTIVE, retries=0, rules_hash=self.rules_hash(t), compacted_at=None)
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
        # 先发退休通知，失败就整个 spawn 失败，不新建接手会话。
        # 前任按它自己的宿主发（换宿主也走这条：退休旧的、新开一个）
        bid = self._send_retirement(pid, f"{s.display_title}（前任）", s.predecessor_conversation_id,
                                    successor_title, s.topic_id, self.conversation_host(s.predecessor_conversation_id))
        return f"已发送 {bid}"

    def _send_retirement(self, record_id: str, title: str, conversation_id: str, successor_title: str,
                         successor_topic_id: str | None, host: str = ANTIGRAVITY) -> str:
        """给一个会话发退休通知（retire.md）。record_id 是记录它的 retired topic（closed，不再投递）；
        它的交接回执到达后转给 successor_topic_id，没有接手会话时转总线。host 是被退休会话的宿主。"""
        if self.store.get_topic(record_id) is None:
            self.store.create_topic(record_id, title, RETIRED, "已退休，由接手会话继续", sm.CLOSED, host)
        bid = self._new_batch_id("r", record_id)
        rec = self.store.get_topic(record_id)
        rec = {**dict(rec), "conversation_id": conversation_id, "host": host}
        if busy := self._busy(rec):
            raise SinkError(f"退休通知发不出去：{busy}")
        self._send(rec, retirement_prompt(self.playbook, record_id, title, bid, successor_title,
                                          successor_topic_id or BUS_TOPIC_ID, cli_host=host != ANTIGRAVITY), bid)
        self.store.record_dispatch(bid, record_id, [])
        self.store.update_topic(record_id, conversation_id=conversation_id, kind=RETIRED, state=sm.CLOSED,
                                pending_batch_id=bid, dispatched_at=now_iso(), host=host,
                                anchors_json=json.dumps([f"successor:{successor_topic_id or BUS_TOPIC_ID}"]))
        log.info("已给 %s 发退休通知 %s（记录 %s）", conversation_id, bid, record_id)
        return bid

    def retire(self, target: str, successor_title: str = "") -> dict:
        """sheepdog retire（7.12）：给一个会话补发 / 手动发退休通知。target 是 topic_id 或裸 conversation_id。

        - 已关闭的前任（retired topic）：照原记录补发，回执转给它的接手会话；
        - 名册里某条的 predecessor_conversation_id：记到 tp_<key>.prev，回执转给该条目；
        - 账本里某个会话的 conversation_id 或 topic_id：记到 <topic>.retired，回执转总线；dynamic 会话同时收掉；
        - 都不是：记到 tp_retired.<id 前 12 位>，回执转总线。
        """
        target = (target or "").strip()
        if not target:
            raise ValueError("--topic 不能为空")
        if target == BUS_TOPIC_ID or (self.store.get_topic(BUS_TOPIC_ID) or {"conversation_id": None})["conversation_id"] == target:
            raise ValueError("总线不能退休")
        t = self.store.get_topic(target) if target.startswith("tp_") else None
        if target.startswith("tp_") and t is None:
            raise ValueError(f"topic {target} 不存在")
        if t is None:
            s = next((x for x in self.roster.sessions if x.predecessor_conversation_id == target), None)
            if s is not None:
                record, title, cid, succ = predecessor_topic_id(s.topic_id), f"{s.display_title}（前任）", target, s.topic_id
            else:
                t = next((x for x in self.store.list_topics() if x["conversation_id"] == target), None)
                if t is None:
                    safe = "".join(ch for ch in target[:12].lower() if ch.isalnum() or ch in "-_")
                    record, title, cid, succ = f"tp_retired.{safe}", f"已退休会话 {target[:12]}", target, None
        if t is not None:
            if not t["conversation_id"]:
                raise ValueError(f"{t['topic_id']} 还没有会话，无从退休")
            cid, title = t["conversation_id"], t["title"]
            if t["kind"] == RETIRED:
                record = t["topic_id"]
                anchors = json.loads(t["anchors_json"] or "[]")
                succ = next((a.removeprefix("successor:") for a in anchors if a.startswith("successor:")),
                            t["topic_id"].removesuffix(".prev") if t["topic_id"].endswith(".prev") else None)
            else:
                record, succ = t["topic_id"] + ".retired", None
                title = f"{t['title']}（已退休）"
        st = self.store.get_topic(succ) if succ else None
        if not successor_title:
            successor_title = (f"{self.cfg.session.title_prefix} {st['title']}".strip() if st and succ != BUS_TOPIC_ID
                               else "总线（会转给合适的会话）")
        bid = self._send_retirement(record, title, cid, successor_title, succ,
                                    self.topic_host(t) if t is not None else self.conversation_host(cid))
        result = {"record_topic": record, "conversation_id": cid, "batch": bid, "successor": succ or BUS_TOPIC_ID}
        if t is not None and t["kind"] == DYNAMIC and t["state"] != sm.CLOSED:
            close_session(self.store, t["topic_id"])
            result["closed"] = t["topic_id"]
        elif t is not None and t["kind"] in (ADOPTED, KNOWN_KIND):
            result["note"] = "它还在名册里：要停止投递，请从名册删掉这一条"
        return result

    def _check_handovers(self) -> None:
        """前任交接回执到达：原样转给接手会话（作为待推消息，受接管/排队约束）。前任 topic 保持 closed。"""
        for t in self.store.list_topics():
            if t["kind"] != RETIRED or not t["pending_batch_id"]:
                continue
            p = self.receipt_path(t["topic_id"], t["pending_batch_id"])
            if not p.exists():
                continue
            raw = p.read_text(encoding="utf-8")
            anchors = json.loads(t["anchors_json"] or "[]")
            successor = next((a.removeprefix("successor:") for a in anchors if a.startswith("successor:")),
                             t["topic_id"].removesuffix(".prev") if t["topic_id"].endswith(".prev") else BUS_TOPIC_ID)
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
            if w["status"] == "replied":
                continue  # 对方回复了、等会话确认：提醒和到期都暂停（7.16）
            # 计时起点：回到 waiting 的从最后一次回复时间算，否则从登记时间算
            started = _parse(w["last_reply_at"]) or _parse(w["started_at"])
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
    if t["kind"] not in MANAGED_KINDS:
        raise ValueError(f"{topic_id} 不是名册里的 managed 会话或总线新开的会话（kind={t['kind']}）")
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


def owner_quote_verifier(store: Store, max_age_hours: float, self_open_id: str):
    """主人原话核对（new-session --quote，7.18）：先按 forward 的办法查总线 transcript 里主人亲口说的；
    再查账本里主人本人账号在飞书上的回复（发送人由 IM 账号核实，带 owner_verified 标记）。回看时长同 forward。

    返回 verify(quote) -> 标注：核对通过时是「✅ 主人原话（已核对…）」，不通过是空串。
    """
    in_bus = bus_quote_verifier(store, max_age_hours)

    def verify(quote: str) -> str:
        q = " ".join((quote or "").split())
        if not q:
            return ""
        if in_bus(quote):
            return QUOTE_LABEL
        if not self_open_id:
            return ""
        cutoff = _now() - timedelta(hours=max_age_hours)
        rows = store.conn.execute("SELECT content, create_time, tags_json FROM messages WHERE sender_id=?",
                                  (self_open_id,)).fetchall()
        for r in rows:
            if OWNER_VERIFIED_TAG not in json.loads(r["tags_json"] or "[]"):
                continue
            ts = _parse(r["create_time"] or "")
            if ts is None or ts.tzinfo is None or ts < cutoff:
                continue
            if any(q in " ".join(b.split()) for b in message_bodies(r["content"] or "")):
                return QUOTE_LABEL_IM
        return ""
    return verify


def forward_messages(store: Store, topic_id: str, message_ids: list[str], note: str = "", quote: str = "",
                     verify_quote=None, self_open_id: str = "", escalation_open_hours: float = 24,
                     key_patterns: list | None = None) -> list[str]:
    """把消息 / 主人原话转交给 managed 会话（规格第 6 节、7.3、7.7）。只改归属并回到待推，真正投递由 Dispatcher
    下一轮完成，因此同样受人类接管、onboarding 约束。

    - 主人本人发的消息（sender_id == self_open_id）带「已核对」标注，并关闭目标会话最近一条未结的「需要你定」。
    - quote 必须通过 verify_quote 核对（没给核对器一律拒绝），投递时标成已核对的主人原话；note 不核对，标成总线备注。
    - 被安全规则 hold 的消息只能带已核对的 quote 转交。
    - 目标是总线、known、已移除或不存在时抛 ValueError。
    - 在岗表（7.20）：转出去的消息记成转出方名下「已转交」的活（钥匙按 [duty] key_patterns 从正文认）。
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
    pats = key_patterns if key_patterns is not None else DutyConfig().patterns
    by_src: dict[str, list] = {}
    for i, row in rows.items():
        by_src.setdefault(row["topic_id"] or BUS_TOPIC_ID, []).append(row)
    for src, rs in by_src.items():
        if src != topic_id:
            try:
                duty.forward_work(store, src, topic_id, rs, pats)
            except Exception as e:  # 在岗表出错不挡转交
                log.warning("转交记进在岗表出错: %s", e)
    if quote or note:
        extra = f"（随附转交的消息：{', '.join(ids)}）" if ids else ""
        store.add_system_message(topic_id, "bus_relay", relay_content(quote, note) + ("\n" + extra if extra else ""))
    log.info("转交 %d 条消息%s -> %s", len(ids), "（含转达）" if quote or note else "", topic_id)
    return ids


def close_session(store: Store, topic_id: str) -> None:
    """手动收掉总线新开的会话（7.10）。名册里的会话、known、总线、前任都不能这样关。"""
    t = store.get_topic(topic_id)
    if t is None:
        raise ValueError(f"topic {topic_id} 不存在")
    if t["kind"] != DYNAMIC:
        raise ValueError(f"{topic_id} 不是总线新开的会话（kind={t['kind']}），不能用 close-session 关")
    if t["state"] == sm.CLOSED:
        raise ValueError(f"{topic_id} 已经收掉了")
    store.update_topic(topic_id, state=sm.CLOSED, pending_batch_id=None)
    store.set_meta("roster_notify_bus", "1")
    log.info("总线新开的会话 %s 已手动收掉，聊天归属释放", topic_id)


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
