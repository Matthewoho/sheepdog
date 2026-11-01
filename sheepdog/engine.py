"""引擎：Collector（拉取 + 路由 + 聊天归属 + 编辑/撤回处理）与 Dispatcher（名册同步 + 按 topic 投递 + 状态机 + 回执）。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import duty, identity, media, session as sm
from .ack import clear_handled_owner_reactions
from .config import (ANTIGRAVITY, BUILTIN_REPLY_SUFFIX_RE, LEGACY_REPLY_SUFFIX, NORMAL, OWNER_SIDE, Config, DutyConfig,
                     RoutingConfig)
from .models import Message
from .card import plain_text
from .guard import (ACTIVITY, AUDIT, COMPACTION, DUTY, LAMB_DISCOVERY, LAST_ACTIVITY_TAIL, activity_at, commands_in,
                    first_match, human_input_at, is_compaction, last_activity_at, local_hhmm, one_sentence, parse_ts,
                    recent_subagent_cids_since, record_time, redact, scan_jsonl, subagent_created_cids_in,
                    subagent_invocations_in, subagent_kills_in, subagent_returns_in, subagent_reused_cids_in,
                    tool_actions, user_turn)
from .playbook import REMINDERS_DIR, Playbook
from .prompts import (DEFAULT_COMPACTION_NOTE, DEFAULT_PATROL_PROMPT, QUOTE_LABEL, QUOTE_LABEL_IM, ROSTER_HASH_STUB,
                      adopted_bootstrap, batch_prompt, bootstrap_prompt, chat_label, compaction_rules_message,
                      is_agent_proxy_content, onboarding_prompt, with_cli, quote_line, relay_content, render_group_peer_status,
                      retirement_prompt, roster_table, rules_update_message, standing_rules_bus,
                      standing_rules_managed, watch_message, priority_prompt)
from .roster import _KEY_RE, MANAGED, Roster, RosterChat, RosterSession, parse_patrol_hours
from .security import HOLD, NONE, SecurityConfig, quote_verified
from .router import DISPATCH, DROP, INBOX, SELF, RouteContext, RouteDecision, message_bodies, route, route_within_group
from .sink import RunResult, Sink, SinkError, SinkTimeout
from .source import Source
from .sink.agentapi import conversation_dir, transcript_contains, transcript_path
from .sink.cli_host import NO_RESULT, RECEIPT_FENCE, extract_receipt
from .store import (ECHO_RELAY_FIELDS, LIFECYCLES, ONESHOT, STANDING, SYSTEM_CHAT, SYSTEM_ID_PREFIX, Store,
                    infer_topic_lifecycle, is_standing_topic, now_iso, row_to_message)
from .source.lark import LarkCliSource, LarkTimeout, parse_event, parse_message as parse_thread_reply

log = logging.getLogger("sheepdog")

BUS_TOPIC_ID = "tp_bus"
ADOPTED = "adopted"
KNOWN_KIND = "known"
RETIRED = "retired"
# 总线临时新开的会话（7.10）：只在账本里，不写名册；回执 done 即收掉
DYNAMIC = "dynamic"
# 会往里投信号的会话类型
MANAGED_KINDS = (ADOPTED, DYNAMIC)
# 在岗表看会话记录判中断 / 失联的宿主（Claude 的记录格式没核实过，只按投递状态判）
RECORD_HOSTS = (ANTIGRAVITY, "codex")
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
# 主人在「找主人」聊天里的回复（_owner_reply）。会话在跑上一批时不排队，当轮单独发一批「主人插话」（7.37 第 4 件）
OWNER_REPLY = "owner_reply"
# 7.45 A 类：主人本人账号在群里 @ 机器人 = 给助理的指令。和 owner_reply 一样会插队、机器人身份点确认表情，但不当成回答
# 哪条「需要你定」。tags 里带 owner_instruction（被 forward 以后也认得出）和 owner_at:<去向依据>
OWNER_INSTRUCTION = "owner_instruction"
OWNER_INSTRUCTION_TAG = "owner_instruction"
OWNER_AT_TAG_PREFIX = "owner_at:"
OWNER_MENTION_TAG = "owner_at:mention"
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_SHEEPDOG_CALL_RE = re.compile(r"(?<![A-Za-z0-9_./-])sheepdog(?![A-Za-z0-9_/-]|\.[A-Za-z0-9_])", re.IGNORECASE)


def _has_owner_instruction_tag(r) -> bool:
    try:
        tags = json.loads(r["tags_json"] or "[]")
    except (TypeError, ValueError, KeyError, IndexError):
        tags = []
    return OWNER_INSTRUCTION_TAG in tags


def _is_pure_owner_context(r) -> bool:
    return r["reason"] == OWNER_CONTEXT and not _has_owner_instruction_tag(r)

# 7.45 C 类：别人在群里的话题回复按话题归属直达。路由器本来判 inbox / drop 的改成这个原因；本来就 dispatch 的（@主人等）
# 原因不变。tags 里带 group_thread:<依据>
THREAD_OWNER = "thread_owner"
GROUP_THREAD_TAG_PREFIX = "group_thread:"
# 归属依据（给人看的说法）
GROUP_SOURCE_LABEL = {
    "roster": "名册绑定了这个话题", "recorded": "之前认出过这个话题", "delivered": "开头那条送到过你",
    "forwarded": "开头那条（或话题里的消息）转交给了你", "sent": "开头那条是你经 sheepdog 发的",
    "quote_delivered": "引用的那条送到过你", "quote_forwarded": "引用的那条转交给了你",
    "quote_sent": "引用的那条是你经 sheepdog 发的", "quote_thread": "引用的那条所在的话题归你",
}
# 7.45 C 类不收这些发送人类型的话题回复（机器人 / 应用；事件里的 bot 入账时已经换成 app）
BOT_SENDER_TYPES = ("app", "bot")
# 找群话题开头那条时一次 mget 最多看几条候选（7.45 第二节第 3 条）
GROUP_ROOT_MGET = 50
# 主人的回复认不出回答哪条时，给总线的那条系统消息（未结列表 / 认不出来源）；正文里写着那条回复的编号
ESCALATION_LIST = "escalation_list"
# 已推送过的消息被编辑 / 撤回后回到待推的路由原因（_handle_change）
OWNER_CORRECTIONS = ("recalled", "edited")
# 主人插话批次：批次号前缀（b 普通 / o onboarding / h 读前任 / r 退休通知）和发完后 dispatches 里的终态。
# 不要回执、不改会话状态、不参与回执超时；消息直接记成已回执（acked），不会再当待推消息送一遍、也不会随超时退回重投
PRIORITY_PREFIX = "p"
PRIORITY_DISPATCH = "priority"
ESCALATION_TAG_PREFIX = "escalation:"
# sends 表里牧羊犬自己的话（读回时一律丢弃，不登记「需要你定」、不投给任何会话）：结案那一句、待办清单（7.36），
# 话题群的标题卡、索引卡、变化说明、「已交给 …」、提示、私聊旧话题里那句「已经放进话题群」（7.51）
OWN_SEND_KINDS = ("resolve", "pending", "tg_title", "tg_index", "tg_change", "tg_handoff", "tg_note", "tg_moved")
# 话题群的消息（7.51）路由原因、送给会话的系统消息
TOPIC_GROUP = "topic_group"
# 命令审计（7.19 K3）：同一个会话 10 分钟内已经报过的同一条命令不再单独报（并进那次报警）；没报过的命令马上报
AUDIT_MERGE_MINUTES = 10
# 发前再看一眼（7.22）：会话 sheepdog reply 发前检查时已经看到过的主人发言，作为背景送到时附这一句
SEEN_BEFORE_SEND_NOTE = "👀 你发送前已经看到过这条（sheepdog reply 发前检查时打印给你了），不是新的提醒。"

# 给主人的通知账本（owner_notices）：第 n 次发送失败后隔几分钟再试，之后每次隔 NOTICE_BACKOFF_CAP 分钟
NOTICE_BACKOFF_MINUTES = (1, 2, 4, 8)
NOTICE_BACKOFF_CAP = 10
# 从落账本（或人工 --retry）起这么久还没发出去：标放弃，记 ERROR，doctor / board 一直提示到有人处理
NOTICE_GIVE_UP_HOURS = 2
# 飞书同一个防重编号 1 小时内只发出一条。发送途中进程没了、不知道发出去没有：这么久以内用同一个编号补发（发出去过就被
# 去重），更久了不敢补发（怕重复），标放弃等人看
NOTICE_RESEND_SAFE_MINUTES = 50
NOTICE_PENDING, NOTICE_SENT, NOTICE_ABANDONED = "pending", "sent", "abandoned"
NOTICE_STATE_LABEL = {NOTICE_PENDING: "待发", NOTICE_SENT: "已发", NOTICE_ABANDONED: "放弃"}


def _accepts(fn, name: str) -> bool:
    """fn 收不收这个关键字参数（给主人的通知渠道：OwnerDM 能回复某一条，测试里的假渠道、写文件的不能）。"""
    import inspect
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def new_idempotency_key() -> str:
    """给主人的一条通知的防重编号：落账本时定好，每次重试都用它。lark-cli 的 --idempotency-key 最长 50 个字符。"""
    return "sd-" + uuid.uuid4().hex


def notice_backoff(attempts: int) -> timedelta:
    """第 attempts 次发送失败以后隔多久再试：1、2、4、8 分钟，之后每次 10 分钟。"""
    if 1 <= attempts <= len(NOTICE_BACKOFF_MINUTES):
        return timedelta(minutes=NOTICE_BACKOFF_MINUTES[attempts - 1])
    return timedelta(minutes=NOTICE_BACKOFF_CAP)


def unsent_notice_line(store: Store, cli: str = "sheepdog") -> str:
    """doctor / board 开头那一行：还有给主人的通知没发出去（还在重试的、放弃了没人处理的）。都发出去了是空串。"""
    rows = store.unsent_owner_notices()
    if not rows:
        return ""
    gave_up = sum(1 for r in rows if r["state"] == NOTICE_ABANDONED)
    parts = ([f"{gave_up} 条已放弃"] if gave_up else []) + \
        ([f"{len(rows) - gave_up} 条还在重试"] if len(rows) > gave_up else [])
    return with_cli(f"⚠ 有 {len(rows)} 条给主人的通知没发出去（{'，'.join(parts)}）：`sheepdog notices` 查看，"
                    f"确认没收到用 `sheepdog notices --retry` 重发，处理过了用 `sheepdog notices --ack`", cli)


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


def has_reply_suffix(content: str, suffix: str = "") -> bool:
    """正文是否包含代回卡片右下角签名（当前配置的 reply_suffix，或内置跨版本签名 🐕SheepDog vX.Y.Z / [🐕Sheepdog Reply]）。"""
    text = content or ""
    if suffix and suffix in text:
        return True
    return bool(BUILTIN_REPLY_SUFFIX_RE.search(text))


def is_agent_reply(content: str, oc) -> bool:
    """这条主人身份的消息是不是会话代回的（7.13 / 7.17 / 7.69）：正文以 skip_prefixes 之一开头，或任意位置包含
    agent_markers 之一（代回卡片的右下角标记；卡片按账本里的 content 匹配）。当 agent_markers 含有官方标记（如
    [🐕Sheepdog Reply] 或 🐕SheepDog）时，自动兼容任意版本的 🐕SheepDog vX.Y.Z 与旧版 [🐕Sheepdog Reply]。"""
    bodies = message_bodies(content)
    if any(b.startswith(p) for b in bodies for p in oc.skip_prefixes):
        return True
    haystacks = [content or "", *bodies]
    if any(mk in b for b in haystacks for mk in oc.agent_markers):
        return True
    if any(mk == LEGACY_REPLY_SUFFIX or "🐕SheepDog" in mk or "🐕Sheepdog" in mk for mk in oc.agent_markers):
        return any(bool(BUILTIN_REPLY_SUFFIX_RE.search(b)) for b in haystacks)
    return False


UPGRADE_WORDS = frozenset({
    "升级", "你来升级", "立即升级", "更新", "你来更新", "重启", "你来重启", "重启生效", "升级重启", "sheepdog upgrade",
})


def is_upgrade_command(content: str) -> bool:
    """主人在找主人聊天里发的升级快捷口令（7.69 / 7.75）：委托给 version_watch.is_upgrade_command。"""
    from . import version_watch
    return version_watch.is_upgrade_command(content)


# 话题里开头那条的序号（thread_message_position）：开头那条 "-1"、回复从 "0" 起。7.22 时（10-12 22:5x）按一个样本以为
# 话题群（chat_type = topic）开头那条是 "0"；7.45 用正式账本只读核实（10-13 19:50～10-16 16:13：5 个话题群 195 个话题，
# 54 个同时有 "-1" 和 "0" 的话题里 "-1" 都是最早那条），话题群同样是 "-1"。表留着，按聊天类型取
THREAD_ROOT_POSITION: dict[str, str] = {}
DEFAULT_THREAD_ROOT_POSITION = "-1"


def root_position(chat_type: str) -> str:
    return THREAD_ROOT_POSITION.get(chat_type or "", DEFAULT_THREAD_ROOT_POSITION)


def is_thread_root(m: Message) -> bool:
    """话题开头那条（序号是这个聊天类型的开头序号）：它自己不是「在话题里回复」。没有序号的不算开头。"""
    pos = (m.thread_position or "").strip()
    return bool(pos) and pos == root_position(m.chat_type)


def is_thread_reply(m: Message) -> bool:
    """在话题里回复（不是开头那条，7.45）：有话题编号，序号不是开头序号；没有序号时（机器人事件、mget 没补上）看事件带的
    root_id 是不是另一条。"""
    if not m.thread_id:
        return False
    pos = (m.thread_position or "").strip()
    if pos:
        return pos != root_position(m.chat_type)
    return bool(m.root_id) and m.root_id != m.message_id


def _seq(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def order_thread_items(items: list[dict], last_id: str = "") -> list[dict]:
    """话题里的消息排顺序：开头那条放最前；回复都有话题里的序号（tpos，thread_position）就按它排，否则都有聊天里的
    序号（mpos，message_position）就按它排；都没有才按时间排（飞书时间只到分钟，同一分钟里分不清先后，10-13 实测
    排反过）。last_id：按时间排时同一分钟里放最后的那条（主人刚回的这一条）。"""
    roots = [x for x in items if x["root"]]
    rest = [x for x in items if not x["root"]]
    for k in ("tpos", "mpos"):
        if rest and all(x.get(k) is not None for x in rest):
            return roots + sorted(rest, key=lambda x: (x[k], x["message_id"]))
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return roots + sorted(rest, key=lambda x: (_parse(x["at"]) or floor, x["message_id"] == last_id, x["message_id"]))


def first_line(content: str) -> str:
    """第一行。读回的卡片（7.48）按「标题\n正文」算：有 header 时第一行是标题。"""
    bodies = message_bodies(plain_text(content))
    text = (bodies[-1] if len(bodies) > 1 else bodies[0]).strip()
    return text.splitlines()[0] if text else ""


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
SEND_FAIL_NOTICE = 3                     # 同一会话连续这么多次投递失败（agentapi 发不出去等），私聊主人一次
DEFAULT_DIGEST_TITLE = "📋 羊群看板摘要（有变化才附；不空闲的会话每个一行，空闲的合成一行）："


def duty_reset_key(topic_id: str) -> str:
    """meta 键：session-reset 的时间。在这之前送出、会话没碰过的批次不再算中断 / 失联。"""
    return f"duty_reset:{topic_id}"


def duty_notice_key(topic_id: str) -> str:
    """meta 键：这一段卡住（从会话最后一次动作或 session-reset 起）已经通知过什么：{"seg": 起点, "sent": [中断 / 失联]}，
    同一段中断、失联各只通知一次。"""
    return f"duty_notice:{topic_id}"


def send_fail_key(topic_id: str) -> str:
    """meta 键：这个会话这一段连续投递失败的记录 {since, count, error, notified, covered}；发送成功就清掉。"""
    return f"send_fail:{topic_id}"


# 7.47 第 5 条：uncertain 批次每轮接着核多久；送出后多久还核不到就私聊主人（每批一次，防重记在 meta）
UNCERTAIN_RECHECK_HOURS = 24
UNCERTAIN_NOTICE_MINUTES = 30


def uncertain_notice_key(batch_id: str) -> str:
    """uncertain 批次已经私聊过主人（7.47 第 5 条）：值是发的时间。"""
    return f"uncertain_notice:{batch_id}"


# 7.47 第 6 条：插话送到后等会话发消息的记录，一个插话批次一条：priority_followup:<批次号> → JSON
LEGACY_PRIORITY_FOLLOWUP_PREFIX = "priority_followup:"   # 7.52 起收进回音账本，旧记录每轮清掉

# 7.52 回音账本
ECHO_UNDELIVERED, ECHO_QUEUED, ECHO_DELIVERED, ECHO_SETTLED = "undelivered", "queued", "delivered", "settled"
ECHO_REPLY = "reply"                                  # 报过「送到了、没回音」那一阶段（alerted 里的名字）
ECHO_DELIVERED_STATES = ("delivered", "acked", "missed")
ECHO_REQUEST_REASONS = ("owner_reply", "owner_instruction", "recalled", "edited")
ECHO_RELAY_PREFIX = "owner_relay:"                    # 这一批已经带回过：meta owner_relay:<批次号>
ECHO_UNBOUND = dict.fromkeys(ECHO_RELAY_FIELDS)       # 这句重新送了一次：绑过的回执作废（_echo_sync）
ECHO_EXCERPT_CHARS = 60
RELAY_STATUS_LABEL = {"handled": "已处理", "done": "已完成", "needs_decision": "等你定", "waiting_external": "在等别人", "waiting": "已派后台任务处理中"}


def compaction_resend_key(topic_id: str) -> str:
    """meta 键：认出压缩后立即补发过规则的那个压缩点 {cid, at, step}。同一个压缩点只补发一次（重启以后也一样）。"""
    return f"compaction_resent:{topic_id}"


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


def closed_dynamic_topics(store: Store, roster: Roster | None = None, limit: int = 30) -> list:
    """已收掉、可原地重开的动态会话（常驻类优先、再按最近活动倒序，最多 limit 条；排除与名册或当前开着的动态会话同 key 的）。"""
    active_ids = {s.topic_id for s in (roster.sessions if roster else [])}
    candidates = []
    for t in store.list_topics():
        if t["kind"] == DYNAMIC and t["state"] != sm.CLOSED:
            active_ids.add(t["topic_id"])
    for t in store.list_topics():
        if t["kind"] == DYNAMIC and t["state"] == sm.CLOSED and t["topic_id"] not in active_ids:
            candidates.append(t)
    candidates.sort(key=lambda t: 0 if is_standing_topic(t) else 1)
    return candidates[:limit]


def apply_ownership(msg: Message, d: RouteDecision, roster: Roster | None,
                    routing: RoutingConfig,
                    dynamic: dict[str, tuple[str, bool]] | None = None) -> tuple[RouteDecision, str | None]:
    """路由之后按名册判断归属（规格第 3 节），返回 (决策, topic_id)。reason 不变，归属写进 tags。

    - 自己发的、ignore 名单里的（routing.ignored，7.56）：不变
    - managed 会话的聊天 + all_messages：推给它（覆盖 inbox 和免打扰 drop）
    - managed 会话的聊天 + dispatch 级：推给它
    - 名册里没人负责、但总线新开的 dynamic 会话负责（7.10）：同上
    - 其他 dispatch 级：总线
    - 其他：不变（inbox / drop）
    """
    if d.route == SELF or d.reason == "self_escalation" or routing.ignored(msg.chat_id):
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
        # 群名查不到的群：chat_id → 上次查的时间（CHAT_NAME_RETRY_MINUTES 内不再查）
        self._chat_name_missed: dict[str, datetime] = {}
        # 机器人身份的实时接收（7.26，[identity] 开着时 sheepdog run 设）：BotEventConsumer，每轮 poll() 取事件
        self.bot_events = None
        # 机器人自己的 id（open_id、app_id），每批入账前按配置和 meta 现算；[identity] 关着是空集
        self.bot_ids: frozenset[str] = frozenset()
        # 7.45：路由时算好、入账后要写进 note 的说明（话题归属的「这是你处理过的 … 下面的话题回复」）：message_id → 文字
        self._notes: dict[str, str] = {}
        # sheepdog poll --dry-run：话题群（7.51）那几样不往飞书发（标题卡、提示）
        self.dry_run = False

    def _decide(self, m: Message, ctx: RouteContext):
        """路由 → 名册归属 → 等待归属 → 防循环。返回 (决策, topic_id, 命中的等待)；等待由调用方在安全闸之后关闭。"""
        if isinstance(m.raw, dict) and m.raw.get("_sd_card_callback") and m.reply_to:
            d = self._escalation_message(m)
            r = self.store.get_message(m.message_id)
            return d, (r["topic_id"] if r else None), None
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
        if lg.treat_all_bots_as_agents and (m.sender_type in ("app", "bot") or (m.sender_id != self.cfg.self_open_id and is_agent_proxy_content(m.content or ""))):
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
        来自 Agent 发送人 → 该聊天冷却，并提示总线一次。对方是真人时不熔断，只记 INFO。
        7.26（[identity] 开着）：会话以机器人身份代回的（机器人在群里发的每一条）也算代回，「非主人消息」也不算机器人自己的。"""
        lg, me, oc = self.cfg.loop_guard, self.cfg.self_open_id, self.cfg.owner_context
        bots = self.bot_ids
        if bots and m.sender_id in bots:
            if m.chat_id in self.cfg.owner_side_chat_ids:
                return
            self._loop_count(m, lg, me, oc, bots)
            return
        if not (oc.skip_prefixes or oc.agent_markers) or m.chat_id in self.cfg.owner_side_chat_ids:
            return
        if not is_agent_reply(m.content, oc):
            return
        self._loop_count(m, lg, me, oc, bots)

    def _loop_count(self, m: Message, lg, me: str, oc, bots: frozenset[str]) -> None:
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
        for b in sorted(bots):
            for r in self.store.own_messages_in_chat(m.chat_id, b):  # 7.26：机器人身份的代回
                t = _parse(r["create_time"])
                if t is not None and t.tzinfo is not None and t >= start:
                    n += 1
        if n <= lg.max_agent_replies:
            return
        name = m.chat_name or m.chat_id
        # 7.14 补丁：只在对方是 Agent 时熔断——窗口内最近一条非主人消息来自 Agent 发送人；真人对话只记 INFO
        latest = None
        for r in self.store.others_in_chat(m.chat_id, me):
            if bots and r["sender_id"] in bots:
                continue  # 7.26：机器人自己的不算「对方」
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
        d, topic_id = apply_ownership(m, route(m, ctx, self.cfg.routing), self.roster, self.cfg.routing,
                                      self._dynamic)
        # 等待中的回复优先归给登记等待的 topic（7.4），覆盖聊天归属、总线、Inbox、免打扰；
        # 自己发的、升级私聊、ignore 名单里的不受影响
        if d.route == SELF or d.reason == "self_escalation" or self.cfg.routing.ignored(m.chat_id):
            return d, topic_id, None
        w = self.store.match_watch(m.sender_id, m.chat_id)
        g = self._group_reply_owner(m, allow_bot=(d.route == DISPATCH))
        if g is not None:
            # 7.45 C 类：别人在群里的话题回复，话题有归属 → 直达归属会话（覆盖整群归属、Inbox、免打扰）。
            # 不抢：登记的 watch 只认归属会话自己的；别的会话的（含整群范围的）watch 不抢走这条
            tags = [*d.tags, GROUP_THREAD_TAG_PREFIX + g["source"]]
            reason = d.reason if d.route == DISPATCH else THREAD_OWNER
            self._notes[m.message_id] = self._group_note(g, m, "话题回复")
            if w is not None and w["topic_id"] == g["topic"]:
                return RouteDecision(DISPATCH, reason, [*tags, f"watch:{w['id']}"]), g["topic"], w
            if w is not None:
                log.info("消息 %s 在 %s 的话题里，等待 #%s（%s）不抢", m.message_id, g["topic"], w["id"], w["topic_id"])
            return RouteDecision(DISPATCH, reason, tags), g["topic"], None
        if w:
            return RouteDecision(DISPATCH, d.reason, [*d.tags, f"watch:{w['id']}"]), w["topic_id"], w
        if d.route == DISPATCH and topic_id and d.reason != "owner_context":
            topic_id = route_within_group(self.store, self.roster, topic_id, chat_id=m.chat_id, root_id=m.root_id,
                                          reply_to_msg_id=m.reply_to, thread_id=m.thread_id, sender_id=m.sender_id)
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
        if self.bot_ids:
            return self._is_mine_or_bots(message_id)
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

    def _is_mine_or_bots(self, message_id: str) -> bool:
        """7.26：被回复的是主人发的、或机器人发的（会话以机器人身份代回的、找主人的）。"""
        mine = {x for x in (self.cfg.self_open_id, *self.bot_ids) if x}
        if not mine or not message_id:
            return False
        if self.store.sender_in(message_id, mine):
            return True
        if message_id not in self._sender_cache:
            try:
                self._sender_cache.update(self.source.senders_of([message_id]))
            except Exception as e:
                log.warning("查询被回复消息发送人失败: %s", e)
                self._sender_cache[message_id] = ""
        return self._sender_cache.get(message_id) in mine

    def ingest(self, msgs: list[Message], received_by: str = "user") -> dict:
        """一批消息入账：路由 → 归属 → 安全闸 → 等待 / 主人发言 / 防循环。poll_once 和 sheepdog inject 共用。
        received_by（7.26）：这批是哪个身份收到的（user = 主人身份的搜索，bot = 机器人身份的实时接收）；[identity] 关着时不用。"""
        self.bot_ids = identity.bot_ids(self.cfg, self.store)
        enabled = self.cfg.identity.enabled
        rb = received_by if enabled else None
        ctx = RouteContext(self.cfg.self_open_id, self._muted(), self._is_my_message, self.bot_ids)
        self._dynamic = dynamic_owners(self.store)
        self._refresh_cooling()
        stats = {"fetched": len(msgs), "new": 0, "changed": 0, "dispatch": 0, "inbox": 0, "drop": 0, "self": 0}

        # 先入账「找主人」聊天里机器人的提问（同批次里主人的回复才找得到它），再入账自己发的消息
        # （保证同批次里「回复我」能判定），最后其他
        # 找主人那一侧（7.56：按 Config.chat_scope 一处判定）
        esc_chats = {m.chat_id for m in msgs if self.cfg.chat_scope(m.chat_id) == OWNER_SIDE}
        me = self.cfg.self_open_id
        msgs.sort(key=lambda m: (0 if m.chat_id in esc_chats and m.sender_id != me else 1 if m.sender_id == me else 2,
                                 m.create_time))
        for m in msgs:
            existing = self.store.get_message(m.message_id)
            if existing is not None and enabled and self._other_identity_copy(existing, m, received_by):
                stats["merged"] = stats.get("merged", 0) + 1
                continue
            if existing is None and me and m.sender_id == me:
                self._own_message(m)
            if existing is None and (m.chat_id in esc_chats or (isinstance(m.raw, dict) and m.raw.get("_sd_card_callback"))):
                d = self._escalation_message(m)
                if rb:
                    self.store.set_received_by(m.message_id, rb)
                if d.route == DISPATCH:
                    self._ack_ingested(m.message_id)
                stats["new"] += 1
                stats[d.route] = stats.get(d.route, 0) + 1
            elif existing is None and enabled and received_by == identity.BOT and self._is_bot_dm(m):
                self.store.upsert_message(m, DROP, "bot_dm", [], None, rb)
                self._bot_dm_reply(m)
                stats["new"] += 1
                stats[DROP] = stats.get(DROP, 0) + 1
            elif existing is None:
                d, topic_id, w = self._decide(m, ctx)
                self.store.upsert_message(m, d.route, d.reason, d.tags, topic_id, rb)
                self._write_note(m.message_id)
                if d.route == DISPATCH:
                    self._close_watch(w, m.message_id, self._secure(m.message_id))
                    self._ack_ingested(m.message_id)
                elif d.route == SELF and me and m.sender_id == me:
                    self._loop_check(m)
                    if self._owner_instruction(m):       # 7.45 A 类：@ 机器人 = 指令；否则照旧当背景（B 类）
                        self._echo_record(m, OWNER_INSTRUCTION)
                        self._ack_ingested(m.message_id)
                    else:
                        if self._owner_context(m):       # 7.67：点名 sheepdog 升格为 owner_instruction
                            self._echo_record(m, OWNER_INSTRUCTION)
                            self._ack_ingested(m.message_id)
                        elif self._owner_at_bot(m):      # 7.52：@ 了机器人却没当成指令：记账，当场报没送到
                            self._echo_record(m, OWNER_INSTRUCTION)
                    self._learn_source_thread(m)
                elif d.route == SELF and d.reason == "self_bot":
                    # 7.26：机器人自己发的（会话以机器人身份代回的）：撤下它回应了的确认表情、算进防循环；不算主人发言
                    self._own_message(m)
                    self._loop_check(m)
                stats["new"] += 1
                stats[d.route] = stats.get(d.route, 0) + 1
                if m.thread_id:
                    self.store.touch_group_thread(m.thread_id, m.create_time)   # 7.45：话题最近活动（过期按它）
            else:
                if m.chat_id in esc_chats and self._in_topic_group(m.chat_id):
                    self._tg_learn(m)      # 7.51：话题群里机器人发的开头那条后来有了话题编号
                elif m.chat_id in esc_chats:
                    self._learn_thread(m)  # 7.22：老消息后来有了话题（开头那条再被拉到时带上 thread_id）
                if self._handle_change(existing, m, ctx):
                    stats["changed"] += 1
                else:
                    self.store.touch(m.message_id)

        return stats

    # ---------- 7.26 两个身份进同一个账本 ----------
    def _other_identity_copy(self, existing, m: Message, received_by: str) -> bool:
        """账本里已经有这条，这一份是不是另一个身份收到的同一条。是就只合并（received_by 记成 both、补空着的字段），
        不当成编辑重新路由，返回 True；不是返回 False（照旧按编辑 / 撤回处理）。
        - 机器人身份收到的、账本里已经有：一律只合并（事件只报新消息，不报编辑；正文渲染也和搜索不一样）；
        - 主人身份搜到的、账本里那份只是机器人收到的：合并，正文换成搜索的这一份（不当编辑）；这时撤回了照样按撤回处理。"""
        prev = existing["received_by"] or identity.USER   # 开关打开以前入账的都是主人身份搜到的
        if received_by == identity.BOT:
            self.store.merge_identity_copy(m.message_id, m, received_by, fill_content=False)
            return True
        if prev != identity.BOT:
            return False
        self.store.merge_identity_copy(m.message_id, m, received_by, fill_content=True)
        self._reconsider_bot_owner_message(existing, m)
        if m.deleted and not existing["deleted"]:
            ctx = RouteContext(self.cfg.self_open_id, self._muted(), self._is_my_message, self.bot_ids)
            self._handle_change(self.store.get_message(m.message_id), m, ctx)
        return True

    def _reconsider_bot_owner_message(self, existing, m: Message) -> None:
        """7.48：找主人聊天里机器人发的卡片，要是机器人身份先收到（事件里卡片只有「[interactive card]」，认不出来，记成
        escalation_chat_other），主人身份搜到正文以后按搜到的正文再认一次，认出来就照常登记。
        10-16 核过正式账本：7.26 上线以来机器人自己发的 690 条全是主人身份搜到的，机器人事件收不到自己发的消息；这里是兜底。"""
        if (m.chat_id not in self.cfg.owner_chat_ids or m.sender_type not in ("app", "bot")
                or existing["reason"] != "escalation_chat_other" or m.deleted):
            return
        own = self.store.send_by_message(m.message_id)
        if own is not None and own["kind"] in OWN_SEND_KINDS:
            return   # 牧羊犬自己的话（结案那一句、待办清单、7.51 话题群那几种）照旧不登记
        hit = self._recognize_bot_owner_message(m)
        if hit is None:
            return
        d, topic, question, text = hit
        self.store.set_reason(m.message_id, d.route, d.reason, d.tags)
        self._register_bot_owner_message(m, topic, question, text)
        log.info("找主人聊天里机器人的消息 %s 搜到正文后认出来了：%s -> %s", m.message_id, d.reason, topic)

    def _is_bot_dm(self, m: Message) -> bool:
        """别人私聊机器人（7.26）：机器人身份收到的私聊，不是「找主人」聊天，不是主人本人、不是机器人自己发的。
        （机器人只收得到它自己的私聊；主人和机器人的私聊是「找主人」聊天，照 7.22 走。）"""
        me = self.cfg.self_open_id
        return (m.chat_type == "p2p" and m.chat_id not in self.cfg.owner_chat_ids
                and not (me and m.sender_id == me) and m.sender_id not in self.bot_ids)

    def _bot_dm_reply(self, m: Message) -> None:
        """机器人回一句固定的话（playbook/bot_dm.md），请对方直接私聊主人本人。同一个人 bot_dm_reply_hours 内只回一次；
        别的机器人私聊过来不回（免得两个机器人来回）。失败只记日志，下一条私聊再试。"""
        if m.sender_type in ("app", "bot") or not m.sender_id:
            return
        since = (_now() - timedelta(hours=self.cfg.identity.bot_dm_reply_hours)).isoformat(timespec="seconds")
        if self.store.bot_dm_replied_since(m.sender_id, since) is not None:
            log.info("%s 私聊机器人（%s），%g 小时内回过，不再回", m.sender_id, m.message_id, self.cfg.identity.bot_dm_reply_hours)
            return
        fn = getattr(self.source, "reply_to", None)
        if fn is None:
            return
        text = identity.bot_dm_text(Playbook.from_config(self.cfg))
        try:
            res = fn(m.message_id, identity=identity.BOT, idem_key=identity.short_key("botdm", m.sender_id, m.message_id),
                     text=text)
        except Exception as e:
            log.warning("回复私聊机器人的 %s 失败: %s", m.sender_id, e)
            return
        self.store.add_bot_dm_reply(m.sender_id, m.chat_id, m.message_id, res.get("message_id", ""))
        log.info("%s 私聊机器人（%s），已回请他直接找主人", m.sender_id, m.message_id)

    def _enrich_bot_events(self, msgs: list[Message]) -> list[Message]:
        """机器人收到的新消息：先用主人身份 mget 取一份（和搜索同一套格式：发送人名字、租户、序号都有，正文渲染一样；
        但不带群名，群名由 _fill_chat_names 补），取得到就用它，回复关系、聊天号和类型事件里有、mget 里没有的补上；
        取不到（主人不在的聊天、私聊机器人）用事件里的。只读。"""
        ids = [m.message_id for m in msgs if m.message_id and self.store.get_message(m.message_id) is None]
        if not ids:
            return msgs
        got: dict[str, Message] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            res, _ = self._mget(chunk)
            if res is None and len(chunk) > 1:
                res = []
                for one in chunk:  # 整批失败（多半是有主人不在的聊天）：逐条再试
                    r, _ = self._mget([one])
                    res += r or []
            got.update({x.message_id: x for x in res or [] if x.message_id})
        out = []
        for m in msgs:
            x = got.get(m.message_id)
            if x is None:
                out.append(m)
                continue
            if not x.reply_to and m.reply_to:
                x.reply_to = m.reply_to
            if not x.thread_id and m.thread_id:
                x.thread_id = m.thread_id
            if not x.root_id and m.root_id:
                x.root_id = m.root_id   # 7.45：话题开头那条只有事件里有
            if not x.chat_id and m.chat_id:
                x.chat_id = m.chat_id
            if not x.chat_type and m.chat_type:
                x.chat_type = m.chat_type
            out.append(x)
        return out

    # 查群名失败后多久内不再查同一个群（分钟）：飞书那边一时查不到，不在每一轮都打一次
    CHAT_NAME_RETRY_MINUTES = 60

    def _fill_chat_names(self, msgs: list[Message]) -> None:
        """机器人事件那条路进来的消息没有群名（事件不带，mget 也不带；主人身份的搜索晚一两分钟才补上，那时批次可能已经
        发出去了）。入账前补上：先用账本里这个群最近一次记过的群名；没有就向飞书只读查一次（机器人身份，不行再用主人身份），
        查到的记进账本，同一个群下次不再查。查不到留空，不挡入账和投递（拼提示时写成「群（名字没取到，oc_…）」）。
        私聊不补：消息头只写「私聊」。"""
        for m in msgs:
            if m.chat_name or not m.chat_id or m.chat_type in ("p2p", SYSTEM_CHAT):
                continue
            try:
                m.chat_name = self._chat_name(m.chat_id)
            except Exception as e:  # 账本读不了之类：留空，照常入账
                log.warning("补群名出错（%s）: %s", m.chat_id, e)

    def _chat_name(self, chat_id: str) -> str:
        name = self.store.known_chat_name(chat_id)
        if name:
            return name
        fn = getattr(self.source, "chat_name", None)
        missed = self._chat_name_missed.get(chat_id)
        if fn is None or (missed is not None and _now() - missed < timedelta(minutes=self.CHAT_NAME_RETRY_MINUTES)):
            return ""
        errors = []
        for who in (identity.BOT, identity.USER):
            try:
                name = fn(chat_id, identity=who)
            except Exception as e:
                errors.append(f"{who}: {e}")
                continue
            if name:
                self.store.remember_chat_name(chat_id, name)
                self._chat_name_missed.pop(chat_id, None)
                log.info("群 %s 的名字向飞书查到：%s（%s 身份）", chat_id, name, who)
                return name
            errors.append(f"{who}: 没返回群名")
        self._chat_name_missed[chat_id] = _now()
        log.warning("群 %s 的名字查不到，先留空（%d 分钟内不再查）：%s", chat_id, self.CHAT_NAME_RETRY_MINUTES,
                    "；".join(errors))
        return ""

    def _poll_bot_events(self) -> dict | None:
        """取机器人身份这期间收到的事件，入账（received_by = bot）。没开 / 没起接收返回 None。"""
        if self.bot_events is None or not self.cfg.identity.enabled:
            return None
        from . import card_callback
        raws = self.bot_events.poll()
        only = set(self.cfg.source.only_chat_ids)
        msgs, card_msgs = [], []
        for r in raws:
            if card_callback.is_card_action_event(r):
                m = card_callback.process_card_action_event(r, cfg=self.cfg, store=self.store, source=self.source)
                if m is not None and (not only or m.chat_id in only):
                    card_msgs.append(m)
            elif (m := parse_event(r, self.cfg.timezone_offset)).message_id and (not only or m.chat_id in only):
                msgs.append(m)
        if not msgs and not card_msgs:
            return {"fetched": 0}
        msgs = (self._enrich_bot_events(msgs) if msgs else []) + card_msgs
        self._fill_chat_names(msgs)
        return self.ingest(msgs, received_by=identity.BOT)

    def poll_bot_only(self) -> dict | None:
        """7.45 第三节：机器人实时事件到了的小轮次：只把这期间收到的事件入账（路由、归属、安全闸），不跑主人身份的搜索、
        不查已读和表情、不动水位线。没开 / 没起接收返回 None。"""
        return self._poll_bot_events()

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
        only = set(self.cfg.source.only_chat_ids)
        if only:
            # 真实飞书测试用（[source] only_chat_ids，7.22）：别的聊天的消息不入账本，所以也不会点表情、查已读
            msgs = [m for m in msgs if m.chat_id in only]
        stats = self.ingest(msgs)
        bot = self._poll_bot_events()
        if bot is not None:
            stats["bot"] = bot

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

    def _ack_ingested(self, message_id: str) -> None:
        """7.67：消息入账且 route='dispatch'、有明确 topic_id、通过安全闸（非 hold）时当场点确认表情。
        幂等由 acks 表（store.get_ack）保证，后续 Dispatcher 投递时不会重复点。"""
        if self.acker is None:
            return
        row = self.store.get_message(message_id)
        if row is None or row["route"] != DISPATCH or not row["topic_id"]:
            return
        if row["security_action"] == HOLD:
            return
        try:
            self.acker.on_delivered([row])
        except Exception as e:
            log.warning("入账点确认表情失败（%s）: %s", message_id, e)

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
        if self.cfg.chat_scope(m.chat_id) != NORMAL:   # 7.56 一处判定：找主人那一侧、ignore 的都不当背景
            return False
        # 会话以主人身份代回的（带代回前缀）不送
        return not is_agent_reply(m.content, oc)

    def _owner_context_target(self, m: Message, exclude: str = "") -> str | None:
        """去向（7.45 B 类）：①话题 / 引用归属（[threads] group_owner 开着）②名册 / 总线新开的聊天归属 ③follow_hours 内最近
        处理过该聊天的会话（含总线）④都没有 → 不送。①命中时把「在谁的话题里」写进 _notes。"""
        chat_id = m.chat_id
        g = self._group_owner(m, exclude or m.message_id)
        if g is not None:
            self._notes[m.message_id] = self._group_note(g, m, "话题里主人的发言")
            return g["topic"]
        owner = self.roster.owner_of(chat_id) if self.roster else None
        if owner is not None:
            s_own = owner[0]
            if s_own.group_id:
                aff = self.store.find_recent_thread_topic_in_group(
                    s_own.group_id, chat_id=chat_id, root_id=m.root_id, reply_to_msg_id=m.reply_to,
                    thread_id=m.thread_id, sender_id=m.sender_id)
                if aff:
                    return aff
            return s_own.topic_id
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

    def _owner_sheepdog_call(self, m: Message) -> bool:
        """7.67：主人在非找主人聊天里点名 `sheepdog`（大小写不敏感、独立词、不在代码块或行内代码里、且不是会话代回）。"""
        raw = m.content or ""
        if not raw.strip():
            return False
        if self._agent_reply_text(raw) or is_agent_reply(raw, self.cfg.owner_context):
            return False
        text = plain_text(raw)
        for body in message_bodies(text):
            cleaned = _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", body))
            if _SHEEPDOG_CALL_RE.search(cleaned):
                return True
        return False

    def _owner_context(self, m: Message) -> bool:
        if not self._owner_context_wanted(m):
            return False
        called = self._owner_sheepdog_call(m)
        topic_id = self._owner_context_target(m)
        note = self._notes.pop(m.message_id, "")
        if called:
            if not topic_id:
                topic_id = BUS_TOPIC_ID
                note = with_cli("📮 主人在没有固定归属会话的聊天里点名了 sheepdog。"
                                f"判断该谁处理后用 `sheepdog forward --topic <会话> --message-ids {m.message_id}` 转交"
                                "（没有合适的就新开）；主人明说让总线自己做才自己做。", self.cfg.cli_command)
            self.store.route_owner_instruction(m.message_id, topic_id, [OWNER_INSTRUCTION, OWNER_MENTION_TAG], note)
            log.info("主人在聊天里点名 sheepdog %s：升格为指令送给 %s", m.message_id, topic_id)
            return True
        if not topic_id:
            return False
        # 回复的是哪条由投递时统一附的「↪ 回复的是」说明（7.15）
        self.store.route_owner_context(m.message_id, topic_id, note)
        log.info("主人的发言 %s 作为背景送给 %s%s", m.message_id, topic_id, "（话题 / 引用归属）" if note else "")
        return False

    # ---------- 主人在群里 @ 机器人 = 指令（7.45 A 类） ----------
    # ---------- 回音账本（7.52）：主人需要回应的话记一笔欠账 ----------
    def _owner_at_bot(self, m: Message) -> bool:
        """主人本人账号在群里 @ 了机器人（不看 [routing] owner_at_assistant 开没开）：要回音的话。"""
        return (bool(self.bot_ids) and self._group_chat(m) and any(x.id in self.bot_ids for x in m.mentions if x.id)
                and not self._agent_reply_text(m.content))

    def _echo_record(self, m: Message, kind: str) -> None:
        """路由完了按结果记账：当成要回应的话送去某个会话（dispatch + 主人回复 / 指令 / 改口）→ queued；否则 undelivered，
        记下原因（这一轮的回音账本当场报）。出错不挡入账。"""
        try:
            r = self.store.get_message(m.message_id)
            if r is None:
                return
            if r["route"] == DISPATCH and (r["reason"] in ECHO_REQUEST_REASONS or _has_owner_instruction_tag(r)) and r["topic_id"]:
                stage, why = ECHO_QUEUED, ""
            elif r["route"] == DISPATCH and r["reason"] == OWNER_CONTEXT and r["topic_id"]:
                stage, why = ECHO_UNDELIVERED, f"当成背景送给了 {r['topic_id']}，不算交办（[routing] owner_at_assistant 关着）"
            else:
                stage, why = ECHO_UNDELIVERED, f"路由 {r['route']} / {r['reason']}，没有会话收"
            self.store.echo_record(m.message_id, m.chat_id, kind, m.create_time or r["create_time"] or "", stage,
                                   r["topic_id"] or "", why, _now().isoformat(timespec="seconds"))
        except Exception as e:
            log.warning("记回音账出错 %s: %s", m.message_id, e)

    def _owner_instruction_wanted(self, m: Message) -> bool:
        """发送人是主人本人账号（调用方已判）、群聊、@ 名单里有机器人（open_id 或 app_id）、不带代回标记、不在找主人聊天、
        不在 ignore 聊天（后两条由 _group_chat 按 Config.chat_scope 判，7.56）。开关 [routing] owner_at_assistant；
        [identity] 关着时不知道机器人是谁，一律不算。"""
        if not self.cfg.routing.owner_at_assistant or not self.bot_ids or not self._group_chat(m):
            return False
        if not any(x.id in self.bot_ids for x in m.mentions if x.id):
            return False
        return not self._agent_reply_text(m.content)

    def _owner_instruction(self, m: Message) -> bool:
        """主人在群里 @ 机器人：去向 ①话题 / 引用归属 ②名册（或总线新开的会话）负责这个群的会话 ③总线。
        改成 dispatch / owner_instruction / 待推；会话在跑时由 Dispatcher 插队。返回是否按指令处理了。"""
        if not self._owner_instruction_wanted(m):
            return False
        g = self._group_owner(m, m.message_id)
        note = ""
        if g is not None:
            topic_id, how = g["topic"], g["source"]
            note = self._group_note(g, m, "主人的指令")
        elif self.roster is not None and (owner := self.roster.owner_of(m.chat_id)) is not None:
            topic_id = route_within_group(self.store, self.roster, owner[0].topic_id, chat_id=m.chat_id,
                                          root_id=m.root_id, reply_to_msg_id=m.reply_to, thread_id=m.thread_id,
                                          sender_id=m.sender_id)
            how = "roster_chat"
        elif m.chat_id in self._dynamic:
            topic_id, how = self._dynamic[m.chat_id][0], "dynamic_chat"
        else:
            topic_id, how = BUS_TOPIC_ID, "bus"
            note = with_cli("📮 名册里没有会话负责这个群，这条也不在哪个会话处理过的话题里、没引用哪个会话处理过的消息。"
                            f"判断该谁做以后用 `sheepdog forward --topic <会话> --message-ids {m.message_id}` 转过去"
                            "（没有合适的就新开）；主人明说让总线自己做才自己做。", self.cfg.cli_command)
        self.store.route_owner_instruction(m.message_id, topic_id,
                                           [OWNER_INSTRUCTION, OWNER_AT_TAG_PREFIX + how], note)
        log.info("主人在群里 @ 机器人 %s：当成指令送给 %s（%s）", m.message_id, topic_id, how)
        return True

    # ---------- 群话题 / 引用归属（7.45 第二节，思路来自 PR #1 @Jerry030-1，issue #2） ----------
    def _group_chat(self, m: Message) -> bool:
        """群（普通群、话题群），不是找主人那一侧的聊天（找主人聊天、7.51 的话题群）、不在 ignore 名单：按 Config.chat_scope
        一处判定（7.56）。7.51 合并：话题群里主人说话一律走 _topic_group_message，7.45 的 A 类（@ 机器人 = 指令）和话题归属
        都不作用在它上面。"""
        return m.chat_type not in ("p2p", SYSTEM_CHAT, "") and self.cfg.chat_scope(m.chat_id) == NORMAL

    def _write_note(self, message_id: str) -> None:
        note = self._notes.pop(message_id, "")
        if note:
            self.store.set_note(message_id, note)

    def _live_topic(self, topic_id: str) -> bool:
        """还能投的会话：总线，或没关的 managed / 总线新开的会话（口径同 dispatch_once）。"""
        if not topic_id:
            return False
        if topic_id == BUS_TOPIC_ID:
            return True
        t = self.store.get_topic(topic_id)
        return t is not None and t["state"] != sm.CLOSED and t["kind"] in MANAGED_KINDS

    def _expired(self, last: str, m: Message) -> bool:
        """话题最近一次活动（last）离这条消息超过 group_owner_hours：不再自动归属。"""
        at, now = _parse(last), _parse(m.create_time) or _now()
        if at is None or at.tzinfo is None or now.tzinfo is None:
            return False
        return now - at > timedelta(hours=self.cfg.threads.group_owner_hours)

    def _sent_owner(self, mid: str) -> tuple[str, str, str] | None:
        for r in (self.store.send_by_message(mid), self.store.get_bot_outbox(mid), self.store.get_escalation(mid)) if mid else ():
            if r is not None and (tid := r["topic_id"]) and self._live_topic(tid):
                return tid, r["sent_at" if "sent_at" in r.keys() else "asked_at"] or "", duty.short(first_line(r["text"] or ""), 60)
        return None

    def _message_owner(self, row) -> tuple[str, str] | None:
        """一条消息（话题开头那条 / 被引用的那条）归哪个会话：送到过某个会话（后来转交了就是接手的那个，账本里的 topic_id
        跟着 forward 改）→ 它；sheepdog 替某个会话发的（ask / report / reply，sends 表）→ 那个会话。主人的背景不算。
        返回 (topic_id, delivered | forwarded | sent)；认不出或会话已经不能投返回 None。"""
        if row is None:
            return None
        if (row["route"] == DISPATCH and row["dispatch_state"] in DELIVERED_STATES and row["reason"] != OWNER_CONTEXT
                and row["topic_id"] and not (row["topic_id"] == BUS_TOPIC_ID and row["reason"] == "keyword") and self._live_topic(row["topic_id"])):
            tags = json.loads(row["tags_json"] or "[]")
            return row["topic_id"], ("forwarded" if any(t.startswith("forwarded_from:") for t in tags) else "delivered")
        return (so[0], "sent") if (so := self._sent_owner(row["message_id"])) is not None else None

    def _group_root(self, m: Message, thread_id: str):
        """群话题开头那条（账本里的行），返回 (行或 None, 怎么找到的)。依次：
        1. 账本里同一话题、开头序号的那条；
        2. 机器人事件带的 root_id，或 reply_to 指向一条账本里还没记话题的消息（开头那条入账时还没有话题）；
        3. mget 这个群里投出去过、还没记话题编号的消息（一次最多 GROUP_ROOT_MGET 条），对上 thread_id 的就是；
           mget 过、当时没有话题的记下来，这个话题已经有回复以后核过的不再核。"""
        for r in self.store.thread_messages_in_ledger(thread_id):
            if (r["thread_position"] or "") == root_position(r["chat_type"]):
                return r, "ledger"
        for ref in (m.root_id, m.reply_to):
            if not ref or ref == m.message_id:
                continue
            r = self.store.get_message(ref)
            if r is None or r["chat_id"] != m.chat_id:
                continue
            if ref == m.root_id or not r["thread_id"]:
                self.store.fill_thread_fields(ref, thread_id, root_position(r["chat_type"]))
                return self.store.get_message(ref), "event"
        since = self.store.thread_first_seen(thread_id) or now_iso()
        cands = self.store.group_root_candidates(m.chat_id, m.create_time or now_iso(), since, GROUP_ROOT_MGET)
        if not cands:
            return None, ""
        got, err = self._mget(cands)
        if got is None:
            log.warning("话题 %s：mget 开头那条的候选失败: %s", thread_id, err)
            return None, ""
        found = ""
        with_thread = set()
        for x in got:
            if x.thread_id:
                with_thread.add(x.message_id)
                self.store.fill_thread_fields(x.message_id, x.thread_id, x.thread_position)
                if x.thread_id == thread_id:
                    found = x.message_id
        self.store.mark_root_checked([c for c in cands if c not in with_thread])
        log.info("话题 %s：mget %d 条候选，%s", thread_id, len(cands), f"开头那条是 {found}" if found else "没对上")
        return (self.store.get_message(found), "mget") if found else (None, "")

    def _group_thread_owner(self, m: Message, thread_id: str = "", exclude: str = "") -> dict | None:
        """群话题归谁（7.45 / 7.87 REG-053）：1 名册绑定 → 2 专席已记归属/专席代发 → 3 开头那条或话题内业务专席（tp_bus 仅内存兜底不锁表）。"""
        thread_id = thread_id or m.thread_id
        if not thread_id:
            return None
        if self.roster is not None and (hit := self.roster.thread_owner(m.chat_id, thread_id)) is not None:
            return {"topic": hit[0].topic_id, "source": "roster", "thread": thread_id, "root": "", "summary": ""}
        rec = self.store.group_thread(thread_id)
        last = max(rec["last_activity_at"] if rec is not None else "", self.store.thread_last_activity(thread_id, exclude))
        for ref in (m.reply_to, m.root_id):
            if ref and (so := self._sent_owner(ref)) is not None and so[0] != BUS_TOPIC_ID and not self._expired(max(last, so[1]), m):
                if rec is None or rec["topic_id"] != so[0]:
                    r_mid = m.root_id or ref
                    self.store.add_group_thread(thread_id, m.chat_id, so[0], "sent", r_mid, so[2], max(last, so[1]) or m.create_time or now_iso())
                    return {"topic": so[0], "source": "sent", "thread": thread_id, "root": r_mid, "summary": so[2]}
        if rec is not None:
            if self._expired(last, m):
                log.info("话题 %s 最近活动 %s，超过 %g 小时，不再自动归属", thread_id, last, self.cfg.threads.group_owner_hours)
                return None
            if not self._live_topic(rec["topic_id"]):
                return None
            return {"topic": rec["topic_id"], "source": "recorded", "thread": thread_id,
                    "root": rec["root_message_id"] or "", "summary": rec["root_summary"] or ""}
        root, how = self._group_root(m, thread_id)
        if root is None:
            if (so := self._sent_owner(m.root_id or m.reply_to)) is None or self._expired(max(last, so[1]), m):
                return None
            r_mid = m.root_id or m.reply_to
            self.store.add_group_thread(thread_id, m.chat_id, so[0], "sent", r_mid, so[2], max(last, so[1]) or m.create_time or now_iso())
            return {"topic": so[0], "source": "sent", "thread": thread_id, "root": r_mid, "summary": so[2]}
        last = max(last, root["create_time"] or "")
        if self._expired(last, m):
            log.info("话题 %s 最近活动 %s，超过 %g 小时，不再自动归属", thread_id, last, self.cfg.threads.group_owner_hours)
            return None
        owner = self._message_owner(root)
        if owner is None or owner[0] == BUS_TOPIC_ID:
            for r in self.store.thread_messages_in_ledger(thread_id, root["message_id"]):
                if (mo := self._message_owner(r)) is not None and mo[0] != BUS_TOPIC_ID:
                    owner = mo; break
                if (so := self._sent_owner(r["message_id"])) is not None and so[0] != BUS_TOPIC_ID:
                    owner = (so[0], "sent"); break
            if owner is None or owner[0] == BUS_TOPIC_ID:
                s_row = self.store.conn.execute(
                    "SELECT topic_id FROM sends WHERE target_message_id=? AND topic_id != ? ORDER BY rowid DESC LIMIT 1",
                    (root["message_id"], BUS_TOPIC_ID)).fetchone()
                if s_row is not None and self._live_topic(s_row["topic_id"]):
                    owner = (s_row["topic_id"], "sent")
        if owner is None:
            return None
        summary = duty.short(first_line(root["content"] or ""), 60)   # first_line 先把卡片还原（7.48）
        self.store.add_group_thread(thread_id, m.chat_id, owner[0], owner[1], root["message_id"], summary,
                                    last or m.create_time or now_iso())
        log.info("话题 %s 归 %s（%s，开头那条 %s 靠 %s 找到）", thread_id, owner[0], owner[1], root["message_id"], how)
        return {"topic": owner[0], "source": owner[1], "thread": thread_id, "root": root["message_id"], "summary": summary}

    _thread_owner = _group_thread_owner

    def _quote_owner(self, m: Message, exclude: str) -> dict | None:
        """引用归属：被引用的那条在某个话题里 → 那个话题的归属；否则它送到过 / 转交给 / 是谁代发的 → 那个会话。"""
        q = self.store.get_message(m.reply_to) if m.reply_to else None
        if q is not None and q["chat_id"] != m.chat_id:
            return None
        if q is not None and q["thread_id"]:
            if (g := self._group_thread_owner(m, q["thread_id"], exclude)) is not None:
                return {**g, "source": "quote_thread" if g["source"] != "roster" else "roster"}
        if q is not None:
            if self._expired(q["create_time"] or "", m):
                return None
            if (owner := self._message_owner(q)) is not None:
                return {"topic": owner[0], "source": "quote_" + owner[1], "thread": "", "root": q["message_id"],
                        "summary": duty.short(first_line(q["content"] or ""), 60)}
        if (so := self._sent_owner(m.reply_to)) is not None and not self._expired(so[1], m):
            return {"topic": so[0], "source": "quote_sent", "thread": "", "root": m.reply_to, "summary": so[2]}
        return None

    def _group_owner(self, m: Message, exclude: str = "") -> dict | None:
        """话题 / 引用归属（A、B 类的第一步）：在话题里说的看话题；否则引用了哪条看那条。开关 [threads] group_owner。"""
        if not self.cfg.threads.group_owner or not self._group_chat(m):
            return None
        if is_thread_reply(m) and (g := self._group_thread_owner(m, m.thread_id, exclude)) is not None:
            return g
        return self._quote_owner(m, exclude) if m.reply_to else None

    def _group_reply_owner(self, m: Message, *, allow_bot: bool = False) -> dict | None:
        """C 类：别人在群里的话题回复或对本会话发出卡片的引用回复 → 归属会话（7.45 / 7.86 REG-049）。"""
        if not self.cfg.threads.group_owner or not self._group_chat(m) or is_agent_reply(m.content, self.cfg.owner_context):
            return None
        if m.sender_type in BOT_SENDER_TYPES and (not allow_bot or (is_thread_reply(m) and self._sent_owner(m.reply_to or m.root_id) is None)):
            return None
        if is_thread_reply(m):
            return self._group_thread_owner(m, m.thread_id, m.message_id)
        return self._quote_owner(m, m.message_id) if (m.reply_to and allow_bot and self._sent_owner(m.reply_to) is not None) else None

    def _group_note(self, g: dict, m: Message, what: str) -> str:
        """送达时附的说明：这是谁处理过的哪条下面的话题回复 / 引用的是哪条；话题长了照 7.22 渐进附最近几条。"""
        why = GROUP_SOURCE_LABEL.get(g["source"], g["source"])
        summary = g.get("summary") or ""
        if not g.get("thread"):
            return f"↩ 引用归属：这条{what}引用的是你处理过的「{summary}」（{why}）。"
        lines = [f"🧵 这是你处理过的「{summary or '（开头那条不在账本里）'}」下面的{what}（话题 `{g['thread']}`，{why}）。"]
        tc = self.cfg.threads
        rows = [r for r in self.store.thread_messages_in_ledger(g["thread"]) if r["message_id"] != m.message_id
                and r["message_id"] != g.get("root")]
        if len(rows) + 1 >= tc.complex_min_replies:
            me = self.cfg.self_open_id
            items = [{"message_id": r["message_id"], "at": r["create_time"] or "", "root": False,
                      "tpos": _seq(r["thread_position"]), "mpos": _seq(r["message_position"]),
                      "who": "主人" if me and r["sender_id"] == me else (r["sender_name"] or r["sender_id"] or "-"),
                      "text": plain_text(r["content"] or "")} for r in rows]   # 卡片（7.48）先还原成「标题\n正文」
            recent = order_thread_items(items)[-tc.recent_replies:]
            lines.append(f"话题里除了开头那条已有 {len(rows) + 1} 条（含这一条），最近 {len(recent)} 条：")
            for x in recent:
                at = _parse(x["at"])
                lines.append(f"- {clock(at, self.cfg.timezone_offset) if at else '-'} {x['who']}："
                             f"{duty.short(x['text'], tc.reply_chars)}")
            lines.append(with_cli(f"要全貌用 `sheepdog thread --topic {g['topic']} --thread {g['thread']}`。",
                                  self.cfg.cli_command))
        return "\n".join(lines)

    def _learn_source_thread(self, m: Message) -> None:
        """7.38：主人在群里某个话题里说话，账本里还不知道这个话题的开头是哪条时，看它是不是某条还开着的提问的来源消息：
        只读 mget 这个群里还没记话题编号的来源消息（最多 50 条），把话题编号补进账本，「可能已处理」才认得出
        「在来源消息的话题里说的」。私聊、找主人聊天不用。失败只记日志。"""
        if (not self.cfg.escalation.followup or not m.thread_id or m.chat_type == "p2p"
                or m.chat_id in self.cfg.owner_side_chat_ids):
            return   # 7.40：跟进关掉时不判可能已处理，也就不用为它 mget
        try:
            from . import pending as pend
            if pend._thread_root_id(self.store, m.thread_id):
                return
            open_ids = [e["message_id"] for e in open_escalations(self.store, self.cfg.escalation.open_hours)]
            cands = []
            for r in self.store.conn.execute(
                    "SELECT DISTINCT s.about_message_id FROM escalation_sources s JOIN messages x "
                    "ON x.message_id = s.about_message_id WHERE s.about_chat_id=? AND COALESCE(x.thread_id,'')=''",
                    (m.chat_id,)):
                cands.append(r[0])
            srcs = {r["about_message_id"] for r in self.store.conn.execute(
                "SELECT about_message_id FROM escalation_sources WHERE ask_message_id IN (%s)" % ",".join("?" * len(open_ids)),
                open_ids)} if open_ids else set()
            cands = [c for c in cands if c in srcs][:50]
            if not cands:
                return
            got, _ = self._mget(cands)
            for x in got or []:
                if x.thread_id and self.store.fill_thread_fields(x.message_id, x.thread_id, x.thread_position):
                    log.info("来源消息 %s 有了话题 %s，补进账本", x.message_id, x.thread_id)
        except Exception as e:
            log.warning("补来源消息的话题编号出错 %s: %s", m.message_id, e)

    def _owner_edit(self, old, m: Message, recalled: bool) -> None:
        if not self._owner_context_wanted(m):
            return
        old_text = old["content"] or ""
        if is_agent_reply(old_text, self.cfg.owner_context):
            return  # 原文是会话代回的
        ref = row_to_message(old)   # 话题 / 引用以账本里那份为准（编辑通知不一定带全）
        ref.chat_type = ref.chat_type or m.chat_type
        topic_id = self._owner_context_target(ref, m.message_id)
        self._notes.pop(m.message_id, None)
        if not topic_id:
            return
        where = chat_label(m.chat_type, m.chat_name, m.chat_id)
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
        acks, ack_emoji = self.store.ack_reaction_ids(), (self.cfg.ack.emoji_type or "Get").strip().lower()
        for mid, items in found.items():
            if (r := rows.get(mid)) is None: continue
            for it in items:
                emoji, op, rid = it.get("emoji_type", ""), it.get("operator_id", ""), it.get("reaction_id", "")
                if op != me or not emoji or (rid and rid in acks):
                    continue  # 别人点的、或 sheepdog 自己点的确认表情
                if not rid and ack_emoji and emoji.strip().lower() == ack_emoji and (ack := self.store.get_ack(mid)) is not None and (ack["identity"] or "user") == "user" and (ack["reaction_id"] or ""):
                    continue
                if self.store.owner_reaction_known(mid, emoji, op):
                    continue
                self.store.add_owner_reaction(mid, emoji, op, rid, reported=not baseline)
                if baseline:
                    continue
                topic_id = self._owner_reaction_target(r)
                if not topic_id:
                    continue
                text = " ".join((r["content"] or "").split())
                where = chat_label(r["chat_type"], r["chat_name"], r["chat_id"])
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
        """先于普通路由，不受 ignore_chat_ids 影响。话题群（7.51）在入口分开走 _topic_group_message。"""
        me = self.cfg.self_open_id
        if isinstance(m.raw, dict) and (cb := m.raw.get("_sd_card_callback")) and (m.reply_to or str(cb.get("kind") or "").startswith(("tg_", "version_"))):
            from . import topicgroup as tg
            cb_kind = cb.get("kind")
            if cb_kind == "tg_move":
                no = f"T-{cb['item_no']}" if cb.get("item_no") else ""
                d = RouteDecision(DROP, "tg_move", [OWNER_VERIFIED_TAG, "card_callback", *([f"item:{no}"] if no else [])])
                self.store.upsert_message(m, d.route, d.reason, d.tags, cb.get("topic_id") or None)
                return d
            if cb_kind in ("version_dismiss", "version_query", "version_upgrade", "version_rollback"):
                reason = "upgrade_command" if cb_kind == "version_upgrade" else cb_kind
                d = RouteDecision(DROP, reason, [OWNER_VERIFIED_TAG, "card_callback"])
                self.store.upsert_message(m, d.route, d.reason, d.tags, BUS_TOPIC_ID)
                if cb_kind == "version_query":
                    self._version_query_reply(m)
                elif cb_kind in ("version_upgrade", "version_rollback"):
                    self._upgrade_shortcut(m)
                return d
            d = self._owner_reply(m)
            s = self.store.send_by_message(m.reply_to)
            item = (self.store.get_item(int(cb["item_no"])) if cb.get("item_no") else None) or self.store.item_by_root(m.reply_to) or (
                self.store.item_by_root(s["root_message_id"]) if s and s["root_message_id"] else None)
            if item is not None:
                tg.touch(self.store, item["item_no"])
                tg.mark_dirty(self.store)
            self._echo_record(m, d.reason)
            return d
        if self._in_topic_group(m.chat_id):
            d = self._topic_group_message(m)
            if me and m.sender_id == me and d.route == DISPATCH and d.reason in ECHO_REQUEST_REASONS:
                self._echo_record(m, d.reason)   # 7.52：话题群里主人的话同样记一笔欠账
            return d
        if me and m.sender_id == me:
            if m.chat_id == self.cfg.threads.owner_chat_id and is_agent_reply(m.content, self.cfg.owner_context):
                d = RouteDecision(DROP, "escalation_agent_reply")
                self.store.upsert_message(m, d.route, d.reason, [])
                return d
            if self._agent_reply_text(m.content):
                d = RouteDecision(SELF, "agent_reply_in_owner_chat")
                self.store.upsert_message(m, d.route, d.reason, [])
                return d
            from . import pending as pend, topicgroup as tg, version_watch
            if tg.is_command(m.content, tg.CMD_MOVE) and tg.configured(self.cfg) and self.cfg.topic_group.enabled:
                return self._tg_move(m)
            if self.cfg.escalation.owner_shortcut and not m.reply_to and (not m.thread_id or is_thread_root(m)) and pend.is_pending_query(m.content):
                d = RouteDecision(DROP, "pending_query")
                self.store.upsert_message(m, d.route, d.reason, [OWNER_VERIFIED_TAG])
                self._pending_reply(m)
                return d
            if self.cfg.escalation.owner_shortcut and is_upgrade_command(m.content) and not self._is_answering_open_escalation(m):
                d = RouteDecision(DROP, "upgrade_command", [OWNER_VERIFIED_TAG])
                self.store.upsert_message(m, d.route, d.reason, [OWNER_VERIFIED_TAG])
                self._upgrade_shortcut(m)
                return d
            if self.cfg.escalation.owner_shortcut and version_watch.is_version_query(m.content) and not self._is_answering_open_escalation(m):
                d = RouteDecision(DROP, "version_query", [OWNER_VERIFIED_TAG])
                self.store.upsert_message(m, d.route, d.reason, [OWNER_VERIFIED_TAG])
                self._version_query_reply(m)
                return d
            d = self._owner_reply(m)
            self._echo_record(m, d.reason)
            return d
        if m.sender_type in ("app", "bot"):
            self._learn_thread(m)
            own = self.store.send_by_message(m.message_id)
            if own is not None and own["kind"] in OWN_SEND_KINDS:
                d = RouteDecision(DROP, f"sheepdog_{own['kind']}")
                self.store.upsert_message(m, d.route, d.reason, [])
                return d
            hit = self._recognize_bot_owner_message(m)
            if hit is not None:
                d, topic, question, text = hit
                self.store.upsert_message(m, d.route, d.reason, d.tags)
                self._register_bot_owner_message(m, topic, question, text)
                return d
        d = RouteDecision(DROP, "escalation_chat_other")
        self.store.upsert_message(m, d.route, d.reason, [])
        return d

    def _recognize_bot_owner_message(self, m: Message):
        """找主人聊天里机器人发的消息：是「需要你定」（header_regex）、还是哪个会话发的（attribution_regex、aliases、标题）。
        7.48：发给主人的卡片读回来是 <card title="第一行">…</card>，还原成「第一行\n正文」再认；纯文本原样，认法一字不变。
        认出来返回 (去向, topic, 是不是提问, 记账用的正文)；认不出返回 None。"""
        esc = self.cfg.escalation
        text = plain_text(m.content)
        question = ""
        if esc.pattern is not None:
            question = next((mt.group("topic") for b in message_bodies(text)
                             if (mt := esc.pattern.search(b)) and mt.group("topic")), "")
        topic = question or self._attribute(m)
        if not topic:
            return None
        d = RouteDecision(DROP, "escalation_asked" if question else "escalation_bot_message", [f"escalation_topic:{topic}"])
        return d, topic, question, text

    def _register_bot_owner_message(self, m: Message, topic: str, question: str, text: str) -> None:
        ok = self._escalation_target_ok(topic)
        if ok:
            self.store.add_bot_outbox(m.message_id, topic, bool(question), text, m.create_time or now_iso())
        if question and ok:
            if self.store.add_escalation(m.message_id, topic, m.chat_id, text, m.create_time or now_iso()):
                log.info("登记「需要你定」%s -> %s", m.message_id, topic)
            try:
                from . import pending as pend
                pend.ensure_sources(self.cfg, self.store, self.store.get_escalation(m.message_id))
            except Exception as e:
                log.warning("补「需要你定」%s 的来源出错: %s", m.message_id, e)
        elif not ok:
            log.warning("机器人消息 %s 的 topic %s 不在名册里（或已关闭），只记日志", m.message_id, topic)

    def _pending_reply(self, m: Message) -> None:
        """「待办」快捷问法（7.36）：机器人回复主人那条，正文是 sheepdog pending 的清单。"""
        from . import pending as pend
        from .outbound import send_owner
        if (fn := getattr(self.source, "reply_to", None)) is None:
            return
        try:
            plain, card = pend.owner_list(self.cfg, self.store)
        except Exception as e:
            log.warning("拼待办清单出错: %s", e)
            return
        ik = identity.short_key(pend.SEND_PENDING, pend.SEND_PENDING, m.message_id)
        if self.store.get_send(ik) is not None:
            return
        try:
            res = send_owner(lambda t, c: fn(m.message_id, identity=identity.BOT, idem_key=ik, text=t,
                                             **({"card": c} if c is not None else {})), plain, card)
        except Exception as e:
            log.warning("回主人的「待办」%s 失败: %s", m.message_id, e)
            return
        self.store.add_send(ik, pend.SEND_PENDING, "", res.get("message_id", ""), plain, target_message_id=m.message_id,
                            identity=identity.BOT if self.cfg.identity.enabled else None)
        log.info("主人发了「待办」%s，已回清单 %s", m.message_id, res.get("message_id", ""))

    def _is_answering_open_escalation(self, m: Message) -> bool:
        """主人这条消息是不是在回答某条未结的「需要你定」提问（引用了未结提问，或在含未结提问的话题里回复）。"""
        open_ids = {e["message_id"] for e in open_escalations(self.store, self.cfg.escalation.open_hours)}
        if not open_ids:
            return False
        if (m.reply_to and m.reply_to in open_ids) or (m.root_id and m.root_id in open_ids):
            return True
        if m.thread_id and not is_thread_root(m):
            thr = self.store.thread_by_thread_id(m.thread_id)
            if thr is not None and (thr["root_message_id"] in open_ids or any(s["message_id"] in open_ids for s in self.store.sends_in_thread(thr["root_message_id"]))):
                return True
            if any(r["message_id"] in open_ids for r in self.store.thread_messages_in_ledger(m.thread_id)):
                return True
        return False

    def _notify_owner(self, text: str, what: str, topic_id: str = "", reply_to: str = "",
                      in_thread: bool = False, idem_key: str = "") -> bool:
        text = with_cli(text, self.cfg.cli_command)
        ik = idem_key or identity.short_key("notice", what, reply_to or topic_id, text)
        if self.store.owner_notice_by_key(ik) is not None:
            return True
        nid = self.store.add_owner_notice(what, topic_id, text.encode("utf-8"), ik, reply_to, in_thread)
        if (fn := getattr(self.source, "reply_to", None) if reply_to else None) is None or self.dry_run:
            return True
        from .outbound import owner_content, send_owner
        plain, card = owner_content(self.cfg, text)
        try:
            self.store.owner_notice_start(nid)
            send_owner(lambda t, c: fn(reply_to, identity=identity.BOT, idem_key=ik, text=t, in_thread=in_thread,
                                       **({"card": c} if c is not None else {})), plain, card)
            self.store.owner_notice_sent(nid)
            return True
        except Exception as e:
            self.store.owner_notice_failed(nid, str(e)[:300], (_now() + notice_backoff(1)).isoformat(timespec="seconds"))
            log.warning("给主人发%s失败: %s", what, e)
            return False

    def _version_query_reply(self, m: Message) -> None:
        """主人在找主人聊天里询问当前版本 / 有没有新版本或 Beta（7.75 / 7.76）：实时查远端并回复主人。"""
        from . import version_watch
        cb = m.raw.get("_sd_card_callback") if isinstance(m.raw, dict) else None
        self._notify_owner(version_watch.format_version_query_reply(self.store, check_remote=True),
                           "版本查询", BUS_TOPIC_ID, reply_to=(m.reply_to or m.message_id) if isinstance(cb, dict) else m.message_id)

    def _upgrade_shortcut(self, m: Message) -> None:
        """主人在找主人聊天里发「你来升级 / 升级 beta / 重启生效 / 回滚」或点升级/回滚卡片按钮（7.69 / 7.75 / 7.76 / 7.93）：入队 upgrade 动作。"""
        from . import actions, version_watch
        if any(a["kind"] == "upgrade" for a in self.store.pending_actions()):
            log.info("主人发了升级口令 %s，队列里已有待执行的 upgrade 动作，不重复入队", m.message_id)
            return
        cb = m.raw.get("_sd_card_callback") if isinstance(m.raw, dict) else None
        channel = (cb.get("channel") if isinstance(cb, dict) and cb.get("channel") else None) or version_watch.parse_upgrade_request(m.content or "") or "stable"
        no_pull = bool(cb.get("no_pull")) if isinstance(cb, dict) else False
        reply_to = (m.reply_to or m.message_id) if isinstance(cb, dict) else m.message_id
        cmd_text = plain_text(m.content or "").strip()
        payload: dict = {"reply_to": reply_to, "command": cmd_text, "channel": channel, "no_pull": no_pull}
        if channel == "rollback" or (isinstance(cb, dict) and cb.get("rollback")):
            payload["rollback"] = True
            payload["no_pull"] = True
            if isinstance(cb, dict) and cb.get("target_sha"):
                payload["target_sha"] = str(cb["target_sha"])
        aid = actions.enqueue(self.store, "upgrade", payload)
        log.info("主人发了升级口令 %s（%r, channel=%s, no_pull=%s），已入队动作 #%d（upgrade）", m.message_id, cmd_text, channel, no_pull, aid)

    def _agent_reply_text(self, content: str) -> bool:
        """主人身份的消息是不是会话代回的：带 [owner_context] 的代回前缀 / 标记，或 [session] reply_suffix / 内置跨版本签名。"""
        suffix = self.cfg.session.reply_suffix
        return is_agent_reply(content, self.cfg.owner_context) or has_reply_suffix(content, suffix)

    # 机器人消息第一行里的 [..] 标签（7.15）：按「·」拆开逐段和 aliases、会话标题比
    _BRACKET = re.compile(r"\[([^\[\]]+)\]")

    def _attribute(self, m: Message) -> str | None:
        """机器人发给主人的消息来自哪个 topic：第一行先用 attribution_regex，认不出再用 aliases 和会话标题；唯一命中才算。"""
        bodies = message_bodies(plain_text(m.content))
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
                out.append((t["topic_id"], title[len(prefix):].strip() if prefix and title.startswith(prefix) else title))
        return out

    def _escalation_target_ok(self, topic_id: str) -> bool:
        if topic_id == BUS_TOPIC_ID:
            return True
        if self.roster is not None and (s := self.roster.by_topic(topic_id)) is not None:
            return s.mode == MANAGED
        t = self.store.get_topic(topic_id)
        return bool(t is not None and t["state"] != sm.CLOSED and (t["kind"] == DYNAMIC or (self.roster is None and t["kind"] == ADOPTED)))

    def _owner_reply(self, m: Message) -> RouteDecision:
        """主人在「找主人」聊天里的回复：有引用按引用投；在话题里按话题送回；没引用且不在话题里（7.53）当成主人指令投总线。"""
        opens = open_escalations(self.store, self.cfg.escalation.open_hours)  # 按提问时间倒序
        cb = m.raw.get("_sd_card_callback") if isinstance(m.raw, dict) else None
        tags = [OWNER_VERIFIED_TAG, *(["card_callback"] if cb else [])]
        if cb and m.reply_to:
            s = self.store.send_by_message(m.reply_to)
            it = (self.store.get_item(int(cb["item_no"])) if cb.get("item_no") else None) or self.store.item_by_root(m.reply_to) or (
                self.store.item_by_root(s["root_message_id"]) if s and s["root_message_id"] else None)
            if it is not None:
                tags.append(f"item:T-{it['item_no']}")
        d = RouteDecision(DISPATCH, OWNER_REPLY, tags)
        if not m.reply_to and m.thread_id and not is_thread_root(m):
            # 7.22：在话题里回复（话题里的消息没有 parent_id）：按话题找会话，不当成没引用去猜
            return self._thread_reply(m, tags, d)
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
                # 7.47 第 1 条：被引用的那条已经送到某个会话，说一句，方便总线判断（转给同一会话时 forward 也不会重发它）
                got = delivered_to(self.store, q)
                where = (f"被引用的这条{delivered_text(*got, self.cfg.timezone_offset)}。\n"
                         if got is not None else "")
                self.store.add_system_message(
                    BUS_TOPIC_ID, ESCALATION_LIST,
                    f"主人引用回复了一条认不出来源的消息（message_id `{m.reply_to}`）：{quoted}\n{where}"
                    f"主人的回复是 `{m.message_id}`。判断它属于哪个会话后用 "
                    + with_cli(f"`sheepdog forward --topic <topic> --message-ids {m.message_id}` 转交；判断不了就问主人。",
                               self.cfg.cli_command))
                log.info("主人的回复 %s 引用了认不出来源的消息 %s，投总线", m.message_id, m.reply_to)
                return d
            if target is not None:
                tags.append(ESCALATION_TAG_PREFIX + target["message_id"])
                self.store.upsert_message(m, d.route, d.reason, tags, target["topic_id"])
                self.store.set_note(m.message_id, escalation_note(target))
                log.info("主人的回复 %s -> %s（引用了提问 %s）", m.message_id, target["topic_id"], target["message_id"])
                return d
        # 7.53：没引用、不在话题里 = 主人的指令。不再按「现在恰好只有谁在等」猜成回答（10-17 02:43 发版那句被送进了 jev、
        # 还关掉了 jev 的提问）：投总线，附未结列表；不打 escalation: 标签、不关任何提问
        d = RouteDecision(DISPATCH, OWNER_INSTRUCTION, [OWNER_VERIFIED_TAG, OWNER_INSTRUCTION, OWNER_AT_TAG_PREFIX + "private"])
        self.store.upsert_message(m, d.route, d.reason, d.tags, BUS_TOPIC_ID)
        lines = [f"主人在找主人聊天里发了一条没引用的话 `{m.message_id}`：这是主人的指令；如果它其实是在回答下面某一条，用 "
                 "forward --answers。当前未结的「需要你定」" + f"{len(opens)} 条："]
        lines += [f"- topic `{e['topic_id']}` | {e['asked_at']} | message_id `{e['message_id']}` | {escalation_summary(e)}"
                  for e in opens] or ["- （无）"]
        lines.append(with_cli(f"新指令：转给负责的会话 `sheepdog forward --topic <topic> --message-ids {m.message_id}`；"
                              f"是在回答某一条：`sheepdog forward --topic <那条的 topic> --message-ids {m.message_id} "
                              "--answers <提问的 message_id>`。", self.cfg.cli_command))
        self.store.add_system_message(BUS_TOPIC_ID, ESCALATION_LIST, "\n".join(lines))
        log.info("主人在找主人聊天里没引用的话 %s：当成指令投总线（未结 %d 条，不猜、不关）", m.message_id, len(opens))
        return d

    # ---------- 话题群（7.51）：群里的消息走单独一条分支 ----------
    def _in_topic_group(self, chat_id: str) -> bool:
        tgid = self.cfg.topic_group.chat_id
        return bool(tgid) and chat_id == tgid

    def _tg_learn(self, m: Message) -> None:
        """话题群里的消息带着话题编号：事项的开头那条（标题卡 / 主人开的那条）、索引卡、会话发进话题里的，补上事项的话题编号。"""
        from . import topicgroup as tg
        if not m.thread_id:
            return
        item = self.store.item_by_root(m.message_id)
        if item is None:
            s = self.store.send_by_message(m.message_id)
            if s is not None and s["root_message_id"]:
                item = self.store.item_by_root(s["root_message_id"])
        if item is not None and not item["thread_id"]:
            self.store.update_item(item["item_no"], thread_id=m.thread_id)
        info = tg.index_info(self.store)
        if info.get("message_id") == m.message_id and not info.get("thread_id"):
            info["thread_id"] = m.thread_id
            self.store.set_meta(tg.META_INDEX, json.dumps(info, ensure_ascii=False))

    def _topic_group_message(self, m: Message) -> RouteDecision:
        """话题群里的一条新消息（7.51 第 2 条）：不走私聊那套「没引用就猜」。
        - 牧羊犬自己发的（标题卡、索引卡、变化说明、「已交给 …」、提示）一律丢弃；会话经 ask / report 发进来的照旧登记；
        - 别人说的：不分派，私聊提醒主人（每人一次）；
        - 主人：整句命令（待办 / 办完了 / 关掉 / 移出群）先拦下；在某件事的话题里回复 → 送给那件事的会话（插队）；
          在索引卡的话题里回复 → 写了编号就送给那件事的负责会话；新开顶层话题 → 开一件事项，当主人的指令送总线。"""
        from . import topicgroup as tg
        from . import pending as pend
        me = self.cfg.self_open_id
        if not self.cfg.topic_group.enabled:
            d = RouteDecision(DROP, "topic_group_off")
            self.store.upsert_message(m, d.route, d.reason, [])
            return d
        if m.sender_type in ("app", "bot") or (m.sender_id and m.sender_id in self.bot_ids):
            return self._tg_bot_message(m)
        if not (me and m.sender_id == me):
            d = RouteDecision(DROP, "topic_group_stranger")
            self.store.upsert_message(m, d.route, d.reason, [])
            tg.warn_strangers(self.cfg, self.store, [m.sender_id], "在话题群里说话了")
            return d
        if self._agent_reply_text(m.content):
            d = RouteDecision(SELF, "agent_reply_in_owner_chat")
            self.store.upsert_message(m, d.route, d.reason, [])
            return d
        tags = [OWNER_VERIFIED_TAG]
        if self.cfg.escalation.owner_shortcut and tg.is_command(m.content, pend.PENDING_WORDS):
            d = RouteDecision(DROP, "pending_query", tags)
            self.store.upsert_message(m, d.route, d.reason, tags)
            self._tg_note(m, "📋 待办看置顶的索引卡（群顶上那张）")
            return d
        from . import decision_hub
        if decision_hub.is_hub_message(self.store, m):
            if decision_hub.handle_hub_text_reply(self, m):
                r = self.store.get_message(m.message_id)
                return RouteDecision(DISPATCH, OWNER_REPLY, json.loads(r["tags_json"] or "[]") if r else tags)
            d = RouteDecision(DROP, "decision_hub_hint", tags)
            self.store.upsert_message(m, d.route, d.reason, tags)
            self._tg_note(m, "请直接点击上方待拍板卡片按钮，或引用具体那张待拍板卡片回复")
            return d
        item = self._tg_find_item(m)
        if item is not None and item["root_message_id"] == m.message_id:
            item = None   # 它自己就是事项的开头（不会走到这里：开头那条入账时就开了事项）
        if item is not None:
            no = tg.label(item["item_no"])
            if tg.is_command(m.content, tg.CMD_DONE):
                d = RouteDecision(DROP, "tg_done", [*tags, f"item:{no}"])
                self.store.upsert_message(m, d.route, d.reason, d.tags)
                if tg.close(self.store, item, "owner", f"主人在话题里回「{tg.whole(m.content)}」"):
                    self._tg_tell_owner(item, f"🗂 主人在话题群把事项 {no}「{item['title']}」标成办完了（他整句回的"
                                              f"「{tg.whole(m.content)}」）。有收尾就做完，不用再往那个话题里汇报；"
                                              "同一把钥匙再发 ask / report 会把它重开。")
                return d
            if tg.is_command(m.content, tg.CMD_REMOVE):
                d = RouteDecision(DROP, "tg_remove", [*tags, f"item:{no}"])
                self.store.upsert_message(m, d.route, d.reason, d.tags)
                if tg.remove(self.store, item, "owner"):
                    self._tg_note(m, "↩️ 已移出话题群：之后这件事的提问、汇报回私聊")
                    self._tg_tell_owner(item, f"🗂 主人把事项 {no}「{item['title']}」移出了话题群。之后这件事的 ask / report "
                                              "照旧发私聊（同一把钥匙，命令不用改）。")
                return d
            return self._tg_reply(m, item)
        if self._tg_in_index(m):
            return self._tg_index_reply(m)
        if self._tg_top_level(m):
            return self._tg_new_topic(m)
        # 7.74：在话题群尚未登记为事项的话题里回复时，自动兜底建档 T-n 绑定该 thread，杜绝悬空无主话题
        thr_msgs = self.store.thread_messages_in_ledger(m.thread_id) if m.thread_id else []
        root_mid = m.root_id or m.reply_to or (thr_msgs[0]["message_id"] if thr_msgs else "") or m.message_id
        s_root = self.store.send_by_message(root_mid) or self.store.get_escalation(root_mid) or self.store.get_bot_outbox(root_mid)
        owner_tid = (s_root["topic_id"] if s_root is not None and s_root["topic_id"] else "") or BUS_TOPIC_ID
        title = tg._excerpt(first_line(plain_text(m.content)), 40) or "（主人开的话题）"
        item = tg.open_item(self.store, title, owner_tid, "owner", tg.ORIGIN_OWNER, m.chat_id, root_message_id=root_mid, thread_id=m.thread_id)
        return self._tg_reply(m, item)

    def _tg_bot_message(self, m: Message) -> RouteDecision:
        """话题群里机器人发的：会话经 ask / report 发进事项话题的照旧登记（按发出时记的正文认，事件里的卡片没有正文）；
        牧羊犬自己的话、认不出的一律丢弃（不登记「需要你定」、不进 bot_outbox，7.51 第 8 条）。"""
        from . import decision_hub, topicgroup as tg
        self._tg_learn(m)
        own = self.store.send_by_message(m.message_id)
        if own is not None and own["kind"] in ("ask", "report") and not (own["idem_key"] or "").startswith("hub_mirror:"):
            x = dataclasses.replace(m, content=own["text"] or m.content)
            hit = self._recognize_bot_owner_message(x)
            if hit is not None:
                d, topic, question, text = hit
                self.store.upsert_message(m, d.route, d.reason, d.tags)
                self._register_bot_owner_message(x, topic, question, text)
                return d
        if own is not None and own["kind"] in OWN_SEND_KINDS:
            reason = f"sheepdog_{own['kind']}"
        elif (self.store.item_by_root(m.message_id) is not None or tg.index_info(self.store).get("message_id") == m.message_id
              or decision_hub.get_hub_meta(self.store).get("root_message_id") == m.message_id
              or self.store.get_escalation(m.message_id) is not None):
            reason = "sheepdog_tg"
        else:
            reason = "topic_group_bot_other"
        d = RouteDecision(DROP, reason)
        self.store.upsert_message(m, d.route, d.reason, [])
        return d

    def _tg_find_item(self, m: Message):
        """这条消息在哪件事项的话题里：话题编号 → 开头那条（引用 / 机器人事件带的回复对象）→ 账本里同一话题的 → mget 还不知道
        话题编号的事项开头那条。找到了顺手补上事项的话题编号。"""
        st = self.store
        item = st.item_by_thread(m.thread_id) if m.thread_id else None
        if item is None and m.root_id and m.root_id != m.message_id:
            item = st.item_by_root(m.root_id)   # 7.45 起事件带 root_id = 话题开头那条
        if item is None and m.reply_to:
            item = st.item_by_root(m.reply_to)
            if item is None and (s := st.send_by_message(m.reply_to)) is not None and s["root_message_id"]:
                item = st.item_by_root(s["root_message_id"])
        if item is None and m.thread_id:
            for r in st.thread_messages_in_ledger(m.thread_id):
                item = st.item_by_root(r["message_id"])
                if item is None and (s := st.send_by_message(r["message_id"])) is not None and s["root_message_id"]:
                    item = st.item_by_root(s["root_message_id"])
                if item is not None:
                    break
        if item is None and m.thread_id:
            cands = [r["root_message_id"] for r in st.conn.execute(
                "SELECT root_message_id FROM tracked_items WHERE chat_id=? AND root_message_id IS NOT NULL "
                "AND (thread_id IS NULL OR thread_id='') ORDER BY item_no DESC LIMIT 50", (m.chat_id,))]
            if cands:
                got, _ = self._mget(cands)
                for x in got or []:
                    if x.thread_id == m.thread_id:
                        item = st.item_by_root(x.message_id)
                        break
        if item is not None and m.thread_id and not item["thread_id"]:
            st.update_item(item["item_no"], thread_id=m.thread_id)
            item = st.get_item(item["item_no"])
        return item

    def _tg_top_level(self, m: Message) -> bool:
        """主人新开的顶层话题：序号是开头序号；没有序号（机器人事件）时没有回复对象就算（事件里的话题回复带 reply_to，
        7.45 起还带 root_id = 话题开头那条）。"""
        if m.reply_to or (m.root_id and m.root_id != m.message_id):
            return False
        pos = (m.thread_position or "").strip()
        return pos == root_position(m.chat_type) if pos else True

    def _tg_in_index(self, m: Message) -> bool:
        from . import topicgroup as tg
        info = tg.index_info(self.store)
        ids = {x for x in (info.get("message_id"), info.get("previous")) if x}
        if not ids:
            return False
        if m.reply_to in ids or (m.thread_id and m.thread_id == info.get("thread_id")):
            return True
        return bool(m.thread_id) and any(r["message_id"] in ids for r in self.store.thread_messages_in_ledger(m.thread_id))

    def _tg_index_reply(self, m: Message) -> RouteDecision:
        """在索引卡的话题里说话（7.51 第 12 条「引用索引卡里的某一行」）：写了一个事项编号就按它现在的负责会话送；
        认不出是哪件就回一句提示，不分派。"""
        from . import topicgroup as tg
        text = plain_text(m.content)
        nos = {int(x) for x in re.findall(r"(?i)\bT[-_ ]?(\d+)\b", text)}
        items = [it for n in nos if (it := self.store.get_item(n)) is not None]
        if len(items) == 1:
            return self._tg_reply(m, items[0], from_index=True)
        d = RouteDecision(DROP, "tg_index_reply", [OWNER_VERIFIED_TAG])
        self.store.upsert_message(m, d.route, d.reason, d.tags)
        self._tg_note(m, "这是索引卡的话题。要回哪件事，请到那件事的话题里回复，或者写上它的编号（比如 T-12）")
        return d

    def _tg_reply(self, m: Message, item, from_index: bool = False) -> RouteDecision:
        """主人在一件事项的话题里回复（7.51 第 7 条）：引用了哪条就送给那条的会话；没引用时，话题里有几个会话的提问还没答，
        就抄送给每一个（各自关掉自己的）；一个未答的都没有，送负责会话。都当主人回复处理（插队）。办完 / 归档的会重开。"""
        from . import topicgroup as tg
        if item["state"] in (tg.DONE, tg.ARCHIVED):
            tg.reopen(self.store, item)
        tg.touch(self.store, item["item_no"])
        no = tg.label(item["item_no"])
        tags = [OWNER_VERIFIED_TAG, f"item:{no}", *([f"thread:{m.thread_id}"] if m.thread_id else [])]
        opens = tg.open_asks(self.cfg, self.store, item)   # 按提问时间倒序
        quoted = m.reply_to if (m.reply_to and not from_index and m.reply_to != item["root_message_id"]) else ""
        target, answered, cc = "", [], {}
        if quoted:
            e = self.store.get_escalation(quoted)
            if e is not None:
                target = e["topic_id"]
                answered = [e] if not e["closed_at"] else []
            else:
                o = self.store.get_bot_outbox(quoted) or self.store.send_by_message(quoted)
                target = (o["topic_id"] or "") if o is not None else ""
        if not target and not from_index and opens:
            target = opens[0]["topic_id"]
            for e in opens:
                if e["topic_id"] == target:
                    answered.append(e)
                else:
                    cc.setdefault(e["topic_id"], []).append(e)
        if not target:
            target = tg.owner_of(self.store, item)
        tags += [ESCALATION_TAG_PREFIX + e["message_id"] for e in answered]
        self.store.upsert_message(m, DISPATCH, OWNER_REPLY, tags, target)
        text = " ".join(plain_text(m.content).split())
        for t2, es in cc.items():
            self.store.add_system_message(
                t2, OWNER_REPLY,
                f"📋 抄送：主人在话题群事项 {no}「{item['title']}」的话题里回复了，没有引用具体哪条。这个话题里你也有没答的"
                f"「需要你定」（{'、'.join('`' + e['message_id'] + '`' for e in es)}），这句也算回答了你的：\n"
                f"主人原话（message_id `{m.message_id}`）：{text}",
                [OWNER_VERIFIED_TAG, f"item:{no}", f"cc_of:{m.message_id}", *(ESCALATION_TAG_PREFIX + e["message_id"] for e in es)])
        self.store.set_note(m.message_id, self._tg_note_text(item, m, answered, list(cc), quoted, from_index))
        log.info("主人在话题群事项 %s 的话题里回复 %s -> %s%s", no, m.message_id, target,
                 f"，抄送 {', '.join(cc)}" if cc else "")
        return RouteDecision(DISPATCH, OWNER_REPLY, tags)

    def _tg_note_text(self, item, m: Message, answered: list, cc: list, quoted: str, from_index: bool) -> str:
        from . import topicgroup as tg
        no = tg.label(item["item_no"])
        lines = [f"🗂 主人在话题群的话题里回复（事项 {no}「{item['title']}」"
                 + (f"，话题 `{m.thread_id}`" if m.thread_id else "") + "）。"
                 + ("他是在索引卡下面写了这件事的编号，按负责会话送给你。" if from_index else "")]
        if item["origin"] == tg.ORIGIN_OWNER and item["root_message_id"]:
            r = self.store.get_message(item["root_message_id"])
            if r is not None:
                lines.append(f"这件事是主人开的，开头那条：「{duty.short(plain_text(r['content']), self.cfg.threads.root_chars)}」")
        if answered:
            e = answered[0]
            lines.append(f"话题里{'他引用的' if quoted else '最近一条'}「需要你定」（message_id `{e['message_id']}`）："
                         f"{escalation_summary(e)}")
        if cc:
            lines.append(f"这句也抄送给了 {'、'.join(cc)}（它们在这个话题里也有没答的提问）。")
        lines.append(with_cli(f"找主人用 `sheepdog ask / report --key {no}`（或这件事原来的钥匙），会回到群里这个话题；"
                              f"要看全貌用 `sheepdog thread --topic <你的 topic> --thread {m.thread_id or item['root_message_id']}`。",
                              self.cfg.cli_command))
        return "\n".join(lines)

    def _tg_new_topic(self, m: Message) -> RouteDecision:
        """主人在话题群新开顶层话题（入口 3）：开一件事项（编号当钥匙），当成主人的指令送总线，编号写进批次说明。"""
        from . import topicgroup as tg
        text = plain_text(m.content)
        title = tg._excerpt(first_line(text), 40) or "（主人开的话题）"
        item = tg.open_item(self.store, title, BUS_TOPIC_ID, "owner", tg.ORIGIN_OWNER, m.chat_id,
                            root_message_id=m.message_id, thread_id=m.thread_id)
        no = tg.label(item["item_no"])
        tags = [OWNER_VERIFIED_TAG, f"item:{no}", *([f"thread:{m.thread_id}"] if m.thread_id else [])]
        self.store.upsert_message(m, DISPATCH, OWNER_REPLY, tags, BUS_TOPIC_ID)
        self.store.set_note(m.message_id, with_cli(
            f"🗂 主人在话题群新开了一个话题，这是主人的指令。牧羊犬记成事项 {no}（编号就是钥匙）。判断交给哪个会话后用 "
            f"`sheepdog forward --topic <会话> --message-ids {m.message_id}` 转交：{no} 会跟着带过去，那个会话之后用 "
            f"`--key {no}` 发 ask / report 就回到群里这个话题，话题里会自动说一句「已交给 …」。你自己处理就用 "
            f"`sheepdog report --topic tp_bus --key {no}` 回主人。", self.cfg.cli_command))
        # 当轮在这个话题里回一行（kind = tg_note，读回丢弃），主人在话题里就看得到编号；之后转交照旧补「➡️ 已交给 …」
        self._tg_note(m, f"🗂 记成事项 {no}，交给「{tg._name(self.cfg, self.store, BUS_TOPIC_ID)}」")
        log.info("主人在话题群新开话题 %s，记成事项 %s，交给总线", m.message_id, no)
        return RouteDecision(DISPATCH, OWNER_REPLY, tags)

    def _tg_tell_owner(self, item, text: str) -> None:
        from . import topicgroup as tg
        self.store.add_system_message(tg.owner_of(self.store, item), TOPIC_GROUP, text, [f"item:{tg.label(item['item_no'])}"])

    def _tg_note(self, m: Message, text: str) -> str:
        """在主人那条消息所在的话题里回一句提示（机器人，kind = tg_note）。失败只记日志。"""
        return self._bot_note(m.message_id, text, "tg_note", in_thread=True)

    def _bot_note(self, target: str, text: str, kind: str, in_thread: bool = True) -> str:
        fn = getattr(self.source, "reply_to", None)
        if fn is None or self.dry_run:
            return ""
        ik = identity.short_key(kind[3:] if kind.startswith("tg_") else kind, kind, target, text)
        if (prev := self.store.get_send(ik)) is not None:
            return prev["message_id"]
        from .outbound import owner_content, send_owner
        plain, card = owner_content(self.cfg, text)
        try:
            res = send_owner(lambda t, c: fn(target, identity=identity.BOT, idem_key=ik, text=t, in_thread=in_thread,
                                             **({"card": c} if c is not None else {})), plain, card)
        except Exception as e:
            log.warning("回提示（%s）失败 %s: %s", kind, target, e)
            return ""
        self.store.add_send(ik, kind, "", res.get("message_id", ""), text, target_message_id=target,
                            identity=identity.BOT if self.cfg.identity.enabled else None)
        return res.get("message_id", "")

    def _tg_move(self, m: Message) -> RouteDecision:
        """主人在私聊里整句回「放群里」（入口 2）：在群里新开话题（标题卡），私聊旧话题贴上链接，告诉负责会话。之后旧话题里的
        回复照 7.22 送，同一把钥匙的新消息都发进群。"""
        from . import topicgroup as tg
        from .source import SourceError
        tags = [OWNER_VERIFIED_TAG]
        d = RouteDecision(DROP, "tg_move", tags)
        self.store.upsert_message(m, d.route, d.reason, tags)
        if not tg.usable(self.cfg, self.store):
            info = tg.down_info(self.store) or {}
            self._bot_note(m.message_id, f"话题群现在不可用（{info.get('why', '')}），这件事先留在私聊。", "tg_note",
                           in_thread=bool(m.thread_id))
            return d
        thr, topic, title, dm_root = None, "", "", ""
        if m.thread_id and not is_thread_root(m):
            thr, _, _, _ = self._find_thread(m)
        if thr is None and m.reply_to:
            s = self.store.send_by_message(m.reply_to)
            root = (s["root_message_id"] if s is not None and s["root_message_id"] else "") or m.reply_to
            thr = self.store.thread_by_root(root)
            if thr is None:
                o = self.store.get_escalation(m.reply_to) or self.store.get_bot_outbox(m.reply_to)
                if o is not None:
                    topic, dm_root = o["topic_id"], m.reply_to
                    title = duty.short(first_line(plain_text(o["text"] or "")), 40)
        if thr is not None:
            topic, dm_root = thr["topic_id"], thr["root_message_id"]
            title = thr["title"] or ""
        key = (thr["key"] or "") if thr is not None else ""
        if not topic:
            self._bot_note(m.message_id, "要把哪件事放进群？在那件事的话题里整句回「放群里」，或者引用那条消息回。", "tg_note",
                           in_thread=bool(m.thread_id))
            return d
        old = tg.find_by_key(self.store, key) if key else self.store.item_by_dm_root(dm_root)
        if old is not None and old["state"] in (tg.ACTIVE, tg.DONE):
            link = tg.chat_link(self.store, self.cfg.topic_group.chat_id)   # 从私聊跳到话题群（只能打开整个群）
            self._bot_note(m.message_id, f"这件事已经在话题群里了：{tg.label(old['item_no'])}" + (f"（[打开话题群]({link})，"
                           f"搜 {tg.label(old['item_no'])}）" if link else ""), "tg_note", in_thread=bool(m.thread_id))
            return d
        title = re.sub(r"^🐕\s*(需要你定|汇报)\s*·\s*", "", title).strip() or key or "（无标题）"
        item = tg.open_item(self.store, title, topic, "owner", tg.ORIGIN_MOVE, self.cfg.topic_group.chat_id, key=key,
                            topic_id=topic, dm_root=dm_root)
        try:
            if not self.dry_run and hasattr(self.source, "send_to_chat"):
                item = tg.ensure_title(self.cfg, self.store, self.source, item, "", key)
        except SourceError as e:
            if isinstance(e, LarkTimeout):
                log.warning("「放群里」的标题卡发送超时，常驻进程下一轮用同一个防重编号补发: %s", e)
            else:
                self.store.delete_item(item["item_no"])
                tg.mark_down(self.cfg, self.store, f"往话题群发标题卡失败：{' '.join(str(e).split())[:200]}")
                self._bot_note(m.message_id, "没放进去：话题群发不进去，已退回私聊，这件事留在这里。", "tg_note",
                               in_thread=bool(m.thread_id))
                return d
        no = tg.label(item["item_no"])
        link = tg.chat_link(self.store, self.cfg.topic_group.chat_id)   # 从私聊跳到话题群（只能打开整个群，搜编号找话题）
        self._bot_note(dm_root, f"📌 这件事已经放进话题群：{no}" + (f"（[打开话题群]({link})，搜 {no}）" if link else "")
                       + "。之后同一把钥匙的消息都发进群里那个话题；这里的回复照旧送给会话。", "tg_moved", in_thread=True)
        how = (f"之后 `--key {key}` 的 ask / report 会发进群里那个话题（命令不用改）" if key
               else f"之后找主人用 `--key {no}`，会发进群里那个话题")
        self._tg_tell_owner(item, f"🗂 主人把你的「{title}」放进了话题群，事项 {no}。{how}；办完用 "
                                  + with_cli(f"`sheepdog track done --topic {topic} --key {key or no} --why \"<依据>\"`。",
                                             self.cfg.cli_command))
        log.info("主人「放群里」%s：%s（%s，钥匙 %s）", m.message_id, no, topic, key or "-")
        return d

    # ---------- 一事一串（7.22）：主人在话题里回复，按话题送回 ----------
    # 用 mget 找话题开头那条时，最多看多少条候选（每次 50 条）
    ROOT_CANDIDATES_MAX = 200

    def _learn_thread(self, m: Message) -> None:
        """「找主人」聊天里带话题编号的消息：账本里这条原来没有话题的补上；它是已记话题的开头那条、或 ask / report
        发进话题里的，就把话题编号补进话题表。"""
        if not m.thread_id:
            return
        self.store.fill_thread_fields(m.message_id, m.thread_id, m.thread_position)
        if self.store.thread_by_root(m.message_id) is not None:
            self.store.set_thread_id(m.message_id, m.thread_id)
            return
        s = self.store.send_by_message(m.message_id)
        if s is not None and s["root_message_id"]:
            self.store.set_thread_id(s["root_message_id"], m.thread_id)

    def _mget(self, ids: list[str]) -> tuple[list[Message] | None, str]:
        """只读按消息号取（mget）。失败返回 (None, 原因)，不抛。"""
        fn = getattr(self.source, "messages_by_id", None)
        if fn is None:
            return None, "消息源不支持按消息号取"
        try:
            return list(fn(ids)), ""
        except Exception as e:
            log.warning("mget %d 条失败: %s", len(ids), e)
            return None, str(e)

    def _recognize_root(self, root: Message) -> str | None:
        """话题开头那条来自哪个会话：先查「需要你定」和 bot_outbox；都没有、但它是机器人发的，再按第一行认（同 7.15）。"""
        e = self.store.get_escalation(root.message_id)
        if e is not None:
            return e["topic_id"]
        out = self.store.get_bot_outbox(root.message_id)
        if out is not None:
            return out["topic_id"]
        if root.sender_type in ("app", "bot"):
            esc = self.cfg.escalation
            topic = ""
            if esc.pattern is not None:
                topic = next((mt.group("topic") for b in message_bodies(plain_text(root.content))
                              if (mt := esc.pattern.search(b)) and mt.group("topic")), "")
            topic = topic or self._attribute(root) or ""
            if topic and self._escalation_target_ok(topic):
                return topic
        return None

    def _adopt_root(self, root: Message, thread_id: str):
        """找到了开头那条：话题表里有就补上话题编号；没有就按开头那条认会话，认出来补记（source = recognized）。
        返回话题行，认不出返回 None。"""
        thr = self.store.thread_by_root(root.message_id)
        if thr is None:
            topic = self._recognize_root(root)
            if topic is None:
                return None
            self.store.add_thread(root.message_id, topic, "", "", duty.short(first_line(root.content), 40),
                                  "recognized", thread_id)
            log.info("话题 %s 靠开头那条 %s 认出是 %s，补记进话题表", thread_id, root.message_id, topic)
        else:
            self.store.set_thread_id(root.message_id, thread_id)
        return self.store.thread_by_root(root.message_id)

    def _find_thread(self, m: Message):
        """主人这条话题回复属于哪个话题。依次：
        1. 话题表里有这个话题编号；
        2. 账本里同一话题的消息：序号是开头序号的那条就是开头；ask / report 发进话题里的，按发出时记的开头；
        3. mget 候选（sheepdog 在这个聊天里发过、还没记话题编号的开头那条、「需要你定」、发给主人的消息），每次 50 条，
           看谁的 thread_id 对得上（话题列表不含开头那条，不用它找开头）；
        都不行返回 None。返回 (话题行或 None, 开头那条 Message 或 None, mget 带回来的话题回复, 失败原因)。"""
        # 7.51 第一步先查事项表：放进话题群的那件事，私聊里原来那个话题（owner_threads 这一行不跟 7 天保留期清）
        thr = self.store.conn.execute(
            "SELECT o.* FROM owner_threads o JOIN tracked_items i ON i.dm_root_message_id = o.root_message_id "
            "WHERE o.thread_id=? LIMIT 1", (m.thread_id,)).fetchone()
        if thr is not None:
            return thr, None, [], ""
        thr = self.store.thread_by_thread_id(m.thread_id)
        if thr is not None:
            return thr, None, [], ""
        for r in self.store.thread_messages_in_ledger(m.thread_id):
            if (r["thread_position"] or "") == root_position(r["chat_type"]):
                root = row_to_message(r)
                return self._adopt_root(root, m.thread_id), root, [], ""
            s = self.store.send_by_message(r["message_id"])
            if s is not None and s["root_message_id"] and (t := self.store.thread_by_root(s["root_message_id"])):
                self.store.set_thread_id(t["root_message_id"], m.thread_id)
                return self.store.thread_by_root(t["root_message_id"]), None, [], ""
        cands = self.store.root_candidates(m.chat_id)
        if len(cands) > self.ROOT_CANDIDATES_MAX:
            log.warning("话题 %s：开头那条的候选 %d 条，只看最近 %d 条", m.thread_id, len(cands), self.ROOT_CANDIDATES_MAX)
            cands = cands[:self.ROOT_CANDIDATES_MAX]
        for i in range(0, len(cands), 50):
            got, err = self._mget(cands[i:i + 50])
            if got is None:
                return None, None, [], err
            for x in got:
                if x.thread_id and x.thread_id == m.thread_id:
                    replies = [parse_thread_reply(r, self.cfg.timezone_offset)
                               for r in (x.raw or {}).get("thread_replies") or []]
                    return self._adopt_root(x, m.thread_id), x, replies, ""
        return None, None, [], ""

    def _thread_reply(self, m: Message, tags: list[str], d: RouteDecision) -> RouteDecision:
        thr, root, extra, err = self._find_thread(m)
        if thr is None:
            # 都认不出：同 7.15「认不出来源」，交给总线
            self.store.upsert_message(m, d.route, d.reason, [*tags, f"thread:{m.thread_id}"], BUS_TOPIC_ID)
            if root is not None:
                where = f"开头那条（message_id `{root.message_id}`）：{duty.short(plain_text(root.content), 200)}"
            else:
                where = f"开头那条查不到（{err}）" if err else "开头那条查不到"
            self.store.add_system_message(
                BUS_TOPIC_ID, ESCALATION_LIST,
                f"主人在一个认不出来源的话题里回复了（话题 `{m.thread_id}`）。{where}\n"
                f"主人的回复是 `{m.message_id}`。判断它属于哪个会话后用 "
                + with_cli(f"`sheepdog forward --topic <topic> --message-ids {m.message_id}` 转交；判断不了就问主人。",
                           self.cfg.cli_command))
            log.info("主人在认不出来源的话题 %s 里回复 %s，投总线", m.thread_id, m.message_id)
            return d
        members = self._thread_members(thr, m, [*([root] if root is not None else []), *extra])
        member_ids = {x["message_id"] for x in members} | {thr["root_message_id"]}
        live = [e for e in open_escalations(self.store, self.cfg.escalation.open_hours, thr["topic_id"])
                if e["message_id"] in member_ids]
        # 话题就是一件事：主人在话题里回复，这件事里没结的「需要你定」都算答了（投递以后才标）
        tags = [*tags, f"thread:{m.thread_id}", *(ESCALATION_TAG_PREFIX + e["message_id"] for e in live)]
        self.store.upsert_message(m, d.route, d.reason, tags, thr["topic_id"])
        self.store.set_note(m.message_id, self._thread_note(thr, m, members, live))
        log.info("主人在话题 %s 里回复 %s -> %s（钥匙 %s）", m.thread_id, m.message_id, thr["topic_id"], thr["key"] or "-")
        return d

    def _thread_members(self, thr, m: Message, extra: list[Message]) -> list[dict]:
        """这个话题里的消息（含开头那条和主人这一条），合并账本、ask / report 发出的记录、mget 带回来的，按时间排。只读。"""
        root = thr["root_message_id"]
        me = self.cfg.self_open_id
        out: dict[str, dict] = {}

        def add(mid: str, at: str, who: str, text: str, tpos=None, mpos=None) -> None:
            if not mid:
                return
            if mid not in out:
                out[mid] = {"message_id": mid, "at": at or "", "who": who, "text": text or "", "root": mid == root,
                            "tpos": _seq(tpos), "mpos": _seq(mpos)}
                return
            for k, v in (("tpos", tpos), ("mpos", mpos)):   # 同一条从别处拿到了序号就补上
                if out[mid][k] is None:
                    out[mid][k] = _seq(v)

        def who_of(sender_id: str, sender_type: str) -> str:
            return "主人" if me and sender_id == me else "机器人" if sender_type in ("app", "bot") else "-"

        # 机器人发的卡片（7.48）按还原后的「标题\n正文」给会话看，不带 <card …> 外壳
        for r in self.store.thread_messages_in_ledger(thr["thread_id"] or m.thread_id, root):
            add(r["message_id"], r["create_time"], who_of(r["sender_id"], r["sender_type"]), plain_text(r["content"] or ""),
                r["thread_position"], r["message_position"])
        for x in extra:
            add(x.message_id, x.create_time, who_of(x.sender_id, x.sender_type), plain_text(x.content), x.thread_position,
                x.message_position)
        for s in self.store.sends_in_thread(root):
            add(s["message_id"], s["sent_at"], "机器人", s["text"])
        add(m.message_id, m.create_time, "主人", m.content, m.thread_position, m.message_position)
        if root not in out:
            o = self.store.get_bot_outbox(root)
            add(root, o["sent_at"] if o is not None else "", "机器人", o["text"] if o is not None else "")
        return order_thread_items(list(out.values()), m.message_id)

    def _thread_note(self, thr, m: Message, members: list[dict], live: list) -> str:
        """渐进式披露（参照 cc-connect 的 Linear 评论回复）：一般情况只附这件事开头那条；话题里除了开头那条已有
        complex_min_replies 条以上（含主人这一条），再附最近几条（每条截短），并说明要全貌去哪里取。条数判断，不让模型判断。"""
        tc = self.cfg.threads
        root = next((x for x in members if x["root"]), None)
        replies = [x for x in members if not x["root"]]
        tid, key = thr["topic_id"], thr["key"] or ""
        what = f"钥匙 `{key}`" if key else "没有钥匙（话题开在一条没带 --key 的消息下面）"
        lines = [f"🧵 主人在话题里回复（话题 `{m.thread_id}`，{what}）。"]
        if root is not None and root["text"]:
            root_text = duty.short(root["text"], tc.root_chars)
        else:
            root_text = "（原文不在账本里）" + (f"标题：{thr['title']}" if thr["title"] else "")
        lines.append(f"这件事开头那条（message_id `{thr['root_message_id']}`）：「{root_text}」")
        latest = live[0] if live else None  # 按提问时间倒序
        if latest is not None and latest["message_id"] != thr["root_message_id"]:
            lines.append(f"话题里最近一条「需要你定」（message_id `{latest['message_id']}`）：{escalation_summary(latest)}")
        if len(replies) >= tc.complex_min_replies:
            earlier = [x for x in replies if x["message_id"] != m.message_id][-tc.recent_replies:]
            lines.append(f"话题里除了开头那条已有 {len(replies)} 条（含主人这一条），最近 {len(earlier)} 条：")
            for x in earlier:
                at = _parse(x["at"])
                lines.append(f"- {clock(at, self.cfg.timezone_offset) if at else '-'} {x['who']}："
                             f"{duty.short(x['text'], tc.reply_chars)}")
            which = f"--key {shlex.quote(key)}" if key else f"--thread {m.thread_id}"
            lines.append(with_cli(f"要全貌用 `sheepdog thread --topic {tid} {which}` 取整个话题；还不够就看羊群看板里这件活"
                                  f"（`sheepdog board --topic {tid}`）和最初的来源消息。", self.cfg.cli_command))
        return "\n".join(lines)

    def _handle_change(self, old, m: Message, ctx: RouteContext) -> bool:
        """编辑 / 撤回：按原消息的路由级别处理（设计稿 2.2.1）。返回是否有变化。"""
        content_changed = (old["content"] or "") != (m.content or "")
        recalled = m.deleted and not old["deleted"]
        if not content_changed and not recalled:
            return False
        me = self.cfg.self_open_id
        owner_side = self.cfg.chat_scope(m.chat_id) == OWNER_SIDE   # 7.56 一处判定
        if me and m.sender_id == me and not owner_side:
            # 主人编辑 / 撤回自己的发言：只更新账本，按 7.13 的去向送一条背景（7.13 补充）
            self.store.update_content(m.message_id, m)
            self._owner_edit(old, m, recalled)
            return True
        old_route, old_state = old["route"], old["dispatch_state"]
        if owner_side and old_route != DISPATCH:
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
            if me and m.sender_id == me and m.chat_id in self.cfg.owner_chat_ids:
                self._echo_record(m, "recalled" if recalled else "edited")   # 7.52：主人改口 / 撤回，重新记账
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
                self._write_note(m.message_id)
                self._close_watch(w, m.message_id, self._secure(m.message_id))
                self._ack_ingested(m.message_id)
            else:
                self._notes.pop(m.message_id, None)
                self.store.update_content(m.message_id, m)
        return True

    def _tag_read_status(self) -> None:
        ids = [r["message_id"] for r in self.store.pending_dispatch()
               if not r["message_id"].startswith((SYSTEM_ID_PREFIX, "sd_card_cb_"))] + self.store.unread_inbox_ids()
        if not ids:
            return
        try:
            self.store.set_read(self.source.read_status(ids))
        except Exception as e:  # 已读只是标签，失败不影响路由
            log.warning("查询已读状态失败: %s", e)


# ======================= Dispatcher =======================
class Dispatcher:
    def __init__(self, cfg: Config, store: Store, sink: Sink | dict, roster: Roster | None = None,
                 security: SecurityConfig | None = None, acker=None, notifier=None, source=None):
        self.cfg = cfg
        self.store = store
        # 往话题群发东西（7.51：索引卡、变化说明、「已交给 …」、查群成员）用的飞书；没有（测试环境、--dry-run）就不做
        self.source = source
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
        # 消息里的图片送进会话前先下载（7.54）：下载用的 lark-cli。空 = 这个进程不下载（试运行、只组装 prompt 的命令），
        # 正文后面写明原因和会话自己去取的命令；常驻进程和 poll 由 cli 设上
        self.image_binary = ""
        self.image_off_reason = "这个进程不下载图片"
        self.restart_requested: bool = False
        self.runtime_info: dict | None = None

    def _images(self, rows) -> dict[str, dict]:
        """这一批里每条消息的图片下载结果（7.54）：message_id → {钥匙: media.ImageResult}。[media] 每批现读（热加载）。
        下载失败不挡投递：结果里带原因，渲染时原文照留、写明会话自己去取的命令。"""
        mc = self.cfg.media
        fetcher = None
        reason = self.image_off_reason
        if not mc.images:
            reason = "配置里关了图片下载（[media] images = false）"
        elif self.image_binary:
            fetcher = media.ImageFetcher(self.cfg.media_dir, self.image_binary, int(mc.max_image_mb * 1024 * 1024),
                                         mc.image_timeout_seconds)
        try:
            out = media.resolve(rows, fetcher, mc.max_images_per_batch, reason)
        except Exception as e:  # 兜底：图片出什么错都不能挡投递，当都没下载下来
            log.warning("下载图片出错，这一批的图片都按没下载处理: %s: %s", type(e).__name__, e)
            out = media.resolve(rows, None, mc.max_images_per_batch, f"下载图片出错：{type(e).__name__}: {e}")
        for mid, res in out.items():
            for r in res.values():
                if r.ok and not r.cached:
                    log.info("消息 %s 的图片 %s 已下载到 %s", mid, r.key, r.path)
                elif not r.ok and fetcher is not None:
                    log.warning("消息 %s 的图片 %s 没下载下来：%s", mid, r.key, r.error)
        return out

    @property
    def sink(self) -> Sink:
        return self.sinks[ANTIGRAVITY]

    def _sink(self, host: str = ANTIGRAVITY) -> Sink | None:
        return self.sinks.get(host)

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
                                        s.effort, group_id=s.group_id)
                t = self.store.get_topic(s.topic_id)
                report["created"].append(s.topic_id)
            chat_ids = [c.chat_id for c in s.chats]
            keep = [a for a in json.loads(t["anchors_json"] or "[]") if not a.startswith("oc_") and a not in chat_ids]
            keep_spawned = bool(t["spawned_at"] and t["conversation_id"]) and (
                s.to_spawn or (s.host == ANTIGRAVITY and bool(s.conversation_id) and not transcript_path(s.conversation_id).is_file())
            )
            cid = t["conversation_id"] if keep_spawned else (None if s.to_spawn else (s.conversation_id or None))
            fields = {
                "title": s.display_title, "kind": s.kind, "duty": s.duty, "conversation_id": cid,
                "anchors_json": json.dumps(chat_ids + sorted(set(keep)), ensure_ascii=False),
                "model": s.model or None, "effort": s.effort or None, "group_id": s.group_id,
            }
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
                fields.update(onboarded_at=None, pending_batch_id=None)
                if not s.to_spawn:
                    fields["spawned_at"] = None
                if t["state"] == sm.RUNNING:
                    fields["state"] = sm.ACTIVE
            if t["state"] == sm.CLOSED:
                fields["state"] = sm.ACTIVE
                report["reopened"].append(s.topic_id)
            changed = {k: v for k, v in fields.items() if t[k] != v}
            if changed:
                self.store.update_topic(s.topic_id, **changed)
                if s.topic_id not in report["created"]:
                    report["updated"].append(s.topic_id)
        self._sync_secondary_and_stale(live, report)
        h = self.roster.content_hash()
        prev = self.store.get_meta("roster_hash")
        if prev != h:
            self.store.set_meta("roster_hash", h)
            if prev or self.roster.sessions:
                self.store.set_meta("roster_notify_bus", "1")
        return report

    def _sync_secondary_and_stale(self, live: set[str], report: dict) -> None:
        for t in self.store.list_topics():
            tid = t["topic_id"]
            for (bid,) in self.store.conn.execute(
                "SELECT batch_id FROM dispatches WHERE topic_id=? AND state='sent'", (tid,)
            ).fetchall():
                rp = self.receipt_path(tid, bid)
                if not rp.exists():
                    rp = self.cfg.receipts_dir / f"{bid}.json"
                if rp.exists() and (t["pending_batch_id"] != bid or t["state"] != sm.RUNNING):
                    self.store.finish_dispatch(bid, "acked", rp.read_text(encoding="utf-8"))
                    self.store.mark_batch_state(bid, "acked")
                    if (card := self.store.get_card(tid)) is not None and card["run_state"] == duty.WORKING:
                        self.store.upsert_card(tid, run_state=duty.IDLE, detail="", stuck_since=None, idle_since=now_iso())
            if "#" in tid and (s_mem := self.roster.by_topic(tid)) is not None:
                live.add(tid)
                mem_fields = {"group_id": s_mem.group_id, "duty": s_mem.duty,
                              "model": s_mem.model or None, "effort": s_mem.effort or None}
                if t["state"] == sm.CLOSED:
                    mem_fields["state"] = sm.ACTIVE
                    report["reopened"].append(tid)
                    if (card := self.store.get_card(tid)) is not None and card["run_state"] == duty.WORKING:
                        self.store.upsert_card(tid, run_state=duty.IDLE, detail="", stuck_since=None, idle_since=now_iso())
                if changed_mem := {k: v for k, v in mem_fields.items() if t[k] != v}:
                    self.store.update_topic(tid, **changed_mem)
                continue
            if t["kind"] in (ADOPTED, KNOWN_KIND) and tid not in live and t["state"] != sm.CLOSED:
                if "#" in tid and t["state"] == sm.RUNNING and t["pending_batch_id"]:
                    continue
                self.store.update_topic(tid, state=sm.CLOSED, pending_batch_id=None)
                _reset_closed_topic(self.store, tid, "已从名册移除")
                report["closed"].append(tid)
                log.info("topic %s 已从名册移除，置为 closed", tid)

    # ---------- 回执与超时 ----------
    def _check_receipt(self, t) -> None:
        batch = t["pending_batch_id"]
        if not batch or t["state"] != sm.RUNNING:
            return
        from . import hook
        adopted = t["kind"] in MANAGED_KINDS
        p = self.receipt_path(t["topic_id"], batch)
        if p.exists():
            hook.clear_stop_state(self.cfg.state_dir, t["topic_id"])
            receipt = json.loads(p.read_text(encoding="utf-8"))
            status = receipt.get("status", "handled")
            if is_standing_topic(t) and status == "done": status = "handled"
            summary = receipt.get("summary", t["summary"])
            answered = ""
            if status == "needs_decision" and (answered := self._answered_by_priority(t)):
                status = "handled"
                summary = f"（回执说等主人拍板，但主人已在插话批次 `{answered}` 回复）{summary or ''}"
                log.info("回执 %s/%s 说等主人拍板，但主人已在插话批次 %s 回复、名下没有开着的提问：按 handled 收",
                         t["topic_id"], batch, answered)
            event = sm.RECEIPT_EVENTS.get(status, "receipt_handled")
            new_state = sm.transition(t["state"], event)
            if "#" in t["topic_id"] and self.roster.by_topic(t["topic_id"]) is None and new_state == sm.ACTIVE:
                new_state = sm.CLOSED
            self.store.finish_dispatch(batch, "acked", json.dumps(receipt, ensure_ascii=False))
            self.store.mark_batch_state(batch, "acked")
            fields = {"state": new_state, "pending_batch_id": None, "retries": 0, "summary": summary}
            self._watches_after_receipt(t["topic_id"], batch, receipt.get("anchors") or [])
            if anchors := self._receipt_anchors(t, receipt): fields["anchors_json"] = anchors
            self.store.update_topic(t["topic_id"], **fields)
            if status != "waiting":
                _auto_close_topic_subagents(self.store, t["topic_id"])
            if status in ("handled", "done"):
                clear_handled_owner_reactions(self.store, getattr(self.acker, "im", None) or self.source, t["topic_id"], batch)
            self._receipt_duty(t["topic_id"], batch, {**receipt, "status": status, "summary": summary} if answered else receipt)
            log.info("回执 %s/%s: %s -> %s", t["topic_id"], batch, status, new_state)
            try:  # 7.52 回执带回：出错不挡收回执
                self._echo_relay(t, batch, receipt, status, summary)
            except Exception as e:
                log.warning("回执带回出错（%s/%s）: %s", t["topic_id"], batch, e)
            if t["kind"] == DYNAMIC and new_state == sm.CLOSED:
                self._dynamic_closed(t["topic_id"], "回执 done")
            return
        sink = self.sinks.get(self.topic_host(t))
        if sink is not None and hasattr(sink, "run_active") and sink.run_active(batch):
            return  # 命令行宿主这一轮还在后台跑（或等重试）：跑完由 _finish_run 收尾，不按回执超时算
        sent = _parse(t["dispatched_at"])
        now = _now()
        if duty.fresh_active_lambs(self.store, t["topic_id"], now, self.cfg.lamb.timeout_minutes):
            return
        if self._check_stopped_in_flight(t, batch, sent, now, adopted):
            return
        vmins = self.cfg.session.verify_transcript_minutes
        if (vmins > 0 and self.topic_host(t) == ANTIGRAVITY and (batch or "").startswith("b")
                and sent is not None and now - sent >= timedelta(minutes=vmins)
                and not self._batch_in_record(t, batch)
                and not self._record_active_since(t, sent)):
            if self._is_zombie_antigravity(t, from_uncertain=True) and (
                new_t := self._rebuild_recycled_container(t, "uncertain_container_recycled", requeue_batch=batch)
            ) is not None:
                self._dispatch_topic(new_t, self.store.pending_for_topic(t["topic_id"]))
                return
            self.store.set_dispatch_state(batch, "uncertain", error="agentapi 返回成功，但会话记录里找不到这个批次")
            self.store.mark_batch_state(batch, "uncertain")
            self.store.update_topic(t["topic_id"], state=sm.ACTIVE, pending_batch_id=None)
            self._notify_uncertain_batch(t, batch, sent)
            return
        if sent and now - sent > timedelta(minutes=self.cfg.session.receipt_timeout_minutes):
            hook.clear_stop_state(self.cfg.state_dir, t["topic_id"])
            if adopted:
                self._miss(t, batch)
            else:
                self._fail(t, batch, RECEIPT_TIMEOUT, ATTN_RECEIPT_TIMEOUT)

    def _transcript_error_at(self, t, batch: str, sent: datetime | None) -> datetime | None:
        """7.64：检查会话记录末尾是否在当前批次送出后停在 ERROR_MESSAGE（或 status=ERROR）。"""
        path = self._record_file(t)
        if path is None or not path.is_file():
            return None
        try:
            with path.open("rb") as f:
                size = f.seek(0, os.SEEK_END)
                start = max(0, size - 256 * 1024)
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return None
        lines = [ln for ln in data.split(b"\n") if ln.strip()]
        if start > 0 and lines:
            lines = lines[1:]
        if not lines:
            return None
        try:
            last = json.loads(lines[-1])
        except ValueError:
            return None
        if not isinstance(last, dict):
            return None
        if last.get("type") != "ERROR_MESSAGE" and last.get("status") != "ERROR":
            return None
        at = _parse(str(last.get("created_at") or "")) or self._record_mtime(t)
        if at is not None and sent is not None and at < sent:
            return None
        return at

    def _check_stopped_in_flight(self, t, batch: str, sent: datetime | None, now: datetime, adopted: bool) -> bool:
        """7.64：会话在途因宿主退出/重登或 Stop hook 放行停机、且无活跃羔羊时：
        静止满 60 秒自动发一次断点续跑唤醒；若已自动唤醒过一次后再次放行停机满 60 秒，立即按超时解锁。"""
        if self.topic_host(t) != ANTIGRAVITY:
            return False
        if self.store.get_active_lambs(t["topic_id"]):
            return False
        from . import hook
        tid = t["topic_id"]
        sp = hook.count_path(self.cfg, tid)
        st = hook._read_stop_state(sp, batch)
        if not st:
            return False
        stopped_at = None
        if st.get("passed_at") or st.get("interrupted") is True:
            stopped_at = _parse(str(st.get("passed_at") or st.get("last_blocked_at") or ""))
        if stopped_at is None:
            stopped_at = self._transcript_error_at(t, batch, sent)
        if stopped_at is None or (now - stopped_at).total_seconds() < 60:
            return False
        meta_key = f"auto_resumed:{batch}"
        already_resumed = bool(st.get("auto_resumed") or self.store.get_meta(meta_key))
        resumed_at = _parse(str(st.get("auto_resumed_at") or self.store.get_meta(meta_key) or ""))
        if already_resumed:
            if resumed_at is not None and stopped_at < resumed_at:
                return False
            hook.clear_stop_state(self.cfg.state_dir, tid)
            self.store.set_meta(duty_reset_key(tid), now.isoformat(timespec="seconds"))
            if adopted:
                self._miss(t, batch)
            else:
                self._fail(t, batch, RECEIPT_TIMEOUT, ATTN_RECEIPT_TIMEOUT)
            log.info("topic %s 批次 %s 自动续跑后再次停机满 60 秒未交回执，立即按超时解锁", tid, batch)
            return True
        prompt = f"[sheepdog] 检测到刚才宿主重登/中断，批次 `{batch}` 尚未提交回执，请继续完成并提交回执。"
        try:
            self._send(t, prompt, batch)
        except SinkError as e:
            log.warning("topic %s 批次 %s 自动续跑唤醒发送失败（等待下一轮重试）: %s", tid, batch, e)
            return True
        now_str = now.isoformat(timespec="seconds")
        new_st = {k: v for k, v in st.items() if k not in ("passed_at", "interrupted")}
        new_st.update({"batch": batch, "count": int(st.get("count") or 0), "auto_resumed": True, "auto_resumed_at": now_str})
        try:
            hook._write_stop_state(sp, new_st)
        except OSError:
            pass
        self.store.set_meta(meta_key, now_str)
        self.store.refresh_topic_dispatched(tid, now_str)
        self.store.set_meta(duty_reset_key(tid), now_str)
        log.info("topic %s 批次 %s 检测到在途停机满 60 秒，已发送自动续跑唤醒", tid, batch)
        return True

    def _record_active_since(self, t, since: datetime) -> bool:
        """从 since 起会话记录里有没有新动作（7.50#6）。"""
        card = self.store.get_card(t["topic_id"])
        act = _parse(card["last_act_at"] or "") if card is not None else None
        if act is not None and act >= since:
            return True
        path = self._record_file(t)
        at = last_activity_at(self.topic_host(t), path) if path is not None and path.exists() else None
        return at is not None and at >= since

    def _notify_uncertain_batch(self, t, batch_id: str, sent: datetime) -> None:
        key = uncertain_notice_key(batch_id)
        if self.store.get_meta(key):
            return
        tid = t["topic_id"]
        d = self.store.get_dispatch(batch_id)
        n = len(json.loads(d["message_ids"] or "[]")) if d is not None else 0
        title = self._session_title(t) or tid
        vmins = self.cfg.session.verify_transcript_minutes
        text = (f"🐕 汇报 · 投递可能未进会话记录 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）的批次 `{batch_id}`"
                f"（{n} 条消息，{display_time(sent, self.cfg.timezone_offset)}送出）agentapi 返回成功，但 {vmins} 分钟后"
                f"会话记录里仍找不到该批次且无新动作，已转记为 uncertain 并解除阻塞。\n"
                f"确认没收到可用：`sheepdog redeliver --batch {batch_id}` 或 `sheepdog redeliver --topic {tid}`")
        log.warning("批次 %s（%s）送出 %d 分钟后会话记录里找不到且无新动作，转记为 uncertain 并解除阻塞", batch_id, tid, vmins)
        self._notify_owner(text, "投递不确定通知", tid, before_send=lambda: self.store.set_meta(key, now_iso()))

    def _answered_by_priority(self, t) -> str:
        """手上这一批送出以后，这个会话收到过主人插话批次（p 开头、终态 priority、送出时间晚于这一批的 dispatched_at），并且
        名下没有还开着的「需要你定」：返回最近那个插话批次号；否则空串（7.47 第 3 条）。"""
        since = _parse(t["dispatched_at"] or "")
        if since is None or since.tzinfo is None:
            return ""
        latest, latest_at = "", None
        for d in self.store.conn.execute(
                "SELECT batch_id, sent_at FROM dispatches WHERE topic_id=? AND state=? AND batch_id LIKE ?",
                (t["topic_id"], PRIORITY_DISPATCH, PRIORITY_PREFIX + "%")).fetchall():
            at = _parse(d["sent_at"] or "")
            if at is not None and at.tzinfo is not None and at >= since and (latest_at is None or at >= latest_at):
                latest, latest_at = d["batch_id"], at
        if not latest or open_escalations(self.store, self.cfg.escalation.open_hours, t["topic_id"]):
            return ""
        return latest

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
        _reset_closed_topic(self.store, topic_id, why)
        self.store.set_meta("roster_notify_bus", "1")
        log.info("总线新开的会话 %s 已收掉（%s），聊天归属释放", topic_id, why)

    def _watches_after_receipt(self, topic_id: str, batch_id: str, anchors: list) -> None:
        """会话回执后处理它的等待（7.16）：anchors 里 watch_done:N → 关闭（done）；
        回复随这个批次送达、但没被确认的 → 回到 waiting，从回复时间重新计时。"""
        settle_watches_after_receipt(self.store, topic_id, batch_id, anchors)

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
        if self._duty_notified(t):
            # 这一段在岗表已经说过「中断 / 失联」：同一件事，只补一句「仍卡着，已停止重投」（第一轮报告第 6 节问题 5）
            tail = ("它一交回执或会话记录又有动作，就自动恢复、补送。" if auto
                    else f"处理好以后用 `sheepdog session-reset --topic {tid}` 复位，排队的随后补送。")
            text = (f"🐕 汇报 · 会话仍卡着 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）仍卡着，已停止重投：{reason}"
                    + (f"（累计失败 {t['retries']} 次）" if t["retries"] else "") + f"，排着 {queued} 条信号；{tail}")
            log.warning("会话 %s 进入 attention（%s），%d 条信号排队（羊群看板已通知过，发短的）", tid,
                        t["attention_reason"], queued)
            self._notify_owner(text, "attention 通知", tid,
                               before_send=lambda: self.store.set_meta(key, t["attention_at"] or ""))
            return
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
        self._notify_owner("\n".join(lines), "attention 通知", tid,
                           before_send=lambda: self.store.set_meta(key, t["attention_at"] or ""))

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
            try:  # 7.52 晚到的回执同样带回
                self._echo_relay(t, d["batch_id"], receipt)
            except Exception as e:
                log.warning("回执带回出错（%s/%s）: %s", t["topic_id"], d["batch_id"], e)
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
            if is_standing_topic(t) and status == "done":
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
        # 这一段在岗表也说过「中断 / 失联」：恢复只说这一次（在岗表那边不再补「已恢复」）
        duty_told = self._duty_notified(t)
        self._duty_mark_recovered(tid)
        if not (notified or duty_told):
            return
        host = HOST_LABEL.get(self.topic_host(t), self.topic_host(t))
        title = self._session_title(t) or tid
        text = (f"🐕 汇报 · 会话已恢复 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）已恢复，补送 {queued} 条。"
                f"依据：{evidence}。")
        why = self.playbook.render("attention_recovered.md", session_title=title, topic_id=tid, host=host,
                                   evidence=evidence, queued=str(queued))
        self._notify_owner(text + (f"\n{why}" if why else ""), "恢复通知", tid)

    # ---------- 人类接管 ----------
    def _owner_typed_at(self, t, card=None) -> datetime | None:
        """主人最后一次在会话里亲自说话的时间。只有这一个判断源：在岗表卡片上的 human_at（_duty_scan 扫会话记录写的）。
        卡片的「被主人占着」和投递要不要暂停（_check_human）都看它，不会一个说占着、一个照投（黑盒 BUG-02）。"""
        if card is None:
            card = self.store.get_card(t["topic_id"])
        return _parse(card["human_at"] or "") if card is not None else None

    def _owner_recent(self, at: datetime | None) -> bool:
        return at is not None and _now() - at < timedelta(minutes=self.cfg.session.human_attach_minutes)

    def _check_human(self, t) -> None:
        """主人在不在（human_attached）。主人亲自说话看卡片上的 human_at：调这里之前，这一轮的 _duty_round 已经扫过会话
        记录（dispatch_once 先跑在岗表；init 先扫一遍），所以和卡片是同一个值。命令行宿主另外还有「会话被别处占着」
        （Codex 最近一轮撞上写锁、Claude 有交互式 claude --resume 在跑），照原来的路问宿主（occupied_at）。"""
        if not t["conversation_id"]:
            return
        sink = self.sinks.get(self.topic_host(t))
        if sink is None:
            return
        recent = self._owner_recent(self._owner_typed_at(t))
        held = getattr(sink, "occupied_at", None)
        if not recent and held is not None:
            recent = self._owner_recent(held(t["conversation_id"]))
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
                                  open_dynamic_topics(self.store), closed_dynamic_topics(self.store, self.roster))
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
            roster_text = (ROSTER_HASH_STUB if for_hash
                           else roster_table(self.roster, open_dynamic_topics(self.store),
                                             closed_dynamic_topics(self.store, self.roster)))
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

    def _group_peer_status(self, t) -> str:
        gid = (t["group_id"] if "group_id" in t.keys() else "") or ""
        if not gid:
            return ""
        g_topics = self.store.get_group_topics(gid)
        all_watches = self.store.open_watches()
        p_counts = {gt["topic_id"]: self.store.count_pending_messages_for_topic(gt["topic_id"]) for gt in g_topics}
        p_work = {gt["topic_id"]: self.store.open_work(gt["topic_id"]) for gt in g_topics}
        p_watches = {gt["topic_id"]: [w for w in all_watches if w["topic_id"] == gt["topic_id"]] for gt in g_topics}
        snap = self.store.group_context_snapshot(gid, t["topic_id"])
        try:
            out = render_group_peer_status(t["topic_id"], g_topics, p_counts, p_work, p_watches, snapshot=snap)
        except TypeError:
            out = render_group_peer_status(t["topic_id"], g_topics, p_counts, p_work, p_watches)
        if out and snap and not any(s in out for s in snap):
            out = "\n".join([*out.splitlines(), *[f"  - {s}" for s in snap]][:10])
        return out

    def _is_zombie_antigravity(self, t, from_uncertain: bool = False) -> bool:
        if (self.cfg.session.verify_transcript_minutes <= 0 or self.topic_host(t) != ANTIGRAVITY
                or t["kind"] not in MANAGED_KINDS or not (cid := (t["conversation_id"] or ""))
                or transcript_path(cid).is_file()):
            return False
        if from_uncertain:
            return True
        if not t["dispatched_at"]:
            return False
        cur = self.store.get_cursor(t["topic_id"], DUTY)
        return (conversation_dir(cid).is_dir() or (cur is not None and (cur["byte_offset"] or 0) > 0)
                or getattr(self._sink(ANTIGRAVITY), "name", "") == "agentapi")

    def _rebuild_recycled_container(self, t, why: str, requeue_batch: str = ""):
        if requeue_batch and self.store.get_meta(f"respawned_batch:{requeue_batch}"):
            return None
        s = self._rules_session(t) or RosterSession(
            key=t["topic_id"].removeprefix("tp_"), mode=MANAGED, title=t["title"] or t["topic_id"],
            duty=t["duty"] or "", chats=[], host=ANTIGRAVITY)
        full_title = f"{self.cfg.session.title_prefix} {s.display_title}".strip()
        w = self.cfg.watch
        boot = adopted_bootstrap(self.playbook, s, full_title, "", w.remind_minutes, w.expire_minutes)
        mem = [x for x in (f"- 历史职责：{t['duty']}" if t["duty"] else "",
                           f"- 上次进度摘要：{t['summary']}" if t["summary"] else "",
                           f"- 历史锚点：{t['anchors_json']}" if t["anchors_json"] and t["anchors_json"] != "[]" else "") if x]
        if mem:
            boot += "\n\n## 容器换壳恢复上下文\n" + "\n".join(mem)
        try:
            new_cid = self._new_conversation(ANTIGRAVITY, t["topic_id"], full_title, boot, t["model"] or "", t["effort"] or "")
        except Exception as e:
            log.warning("topic %s 自动换壳新建会话失败: %s", t["topic_id"], e)
            return None
        if requeue_batch:
            self.store.set_meta(f"respawned_batch:{requeue_batch}", new_cid)
            self.store.set_dispatch_state(requeue_batch, "redelivered", error=why)
            if (d := self.store.get_dispatch(requeue_batch)) is not None and (mids := json.loads(d["message_ids"] or "[]")):
                self.store.requeue_messages(mids)
            with self.store.tx() as c:
                c.execute("UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE batch_id=?", (requeue_batch,))
        ts, old_cid = now_iso(), t["conversation_id"]
        self.store.update_topic(t["topic_id"], conversation_id=new_cid, spawned_at=ts, onboarded_at=ts,
                                dispatched_at=None, pending_batch_id=None, state=sm.ACTIVE, retries=0,
                                rules_hash=self.rules_hash(t), compacted_at=None)
        if (card := self.store.get_card(t["topic_id"])) is not None and card["run_state"] in duty.STUCK:
            self.store.upsert_card(t["topic_id"], run_state=duty.IDLE, detail="", no_record=0, stuck_since=None, idle_since=ts)
        log.info("topic %s 僵尸容器 %s 已自动换壳 -> %s (%s)", t["topic_id"], old_cid, new_cid, why)
        return self.store.get_topic(t["topic_id"])

    def _dispatch_topic(self, t, rows) -> dict:
        res = {"pending": len(rows), "sent": 0, "state": t["state"]}
        if not self._deliverable(t):
            if t["state"] == sm.RUNNING and self.cfg.session.owner_priority and (urgent := self._priority_rows(rows)):
                return self._dispatch_priority(t, urgent, res)
            log.info("topic %s 状态 %s，%d 条信号排队中", t["topic_id"], t["state"], len(rows))
            return res
        if self._is_zombie_antigravity(t):
            t = self._rebuild_recycled_container(t, "pre_dispatch_container_recycled") or t
        adopted = t["kind"] in MANAGED_KINDS
        if adopted and not t["conversation_id"] and "#" not in t["topic_id"]:
            log.info("topic %s 等待 sheepdog spawn 新建会话，%d 条信号排队中", t["topic_id"], len(rows))
            res["waiting_spawn"] = True
            return res
        card = self.store.get_card(t["topic_id"])
        if card is not None and card["run_state"] in duty.STUCK and t["state"] != sm.FAILED:
            why = f"羊群看板判为{duty.RUN_LABEL[card['run_state']]}（{card['detail']}）"
            log.info("topic %s 排队：%s，%d 条信号", t["topic_id"], why, len(rows))
            res["queued"] = why
            return res
        if card is not None and card["run_state"] == duty.OCCUPIED and t["state"] == sm.FAILED:
            why = f"主人在会话里（{card['detail']}），等主人走了再重投"
            log.info("topic %s 排队：%s，%d 条信号", t["topic_id"], why, len(rows))
            res["queued"] = why
            return res
        if busy := (self._busy(t) if t["conversation_id"] else ""):
            log.info("topic %s 排队：%s，%d 条信号", t["topic_id"], busy, len(rows))
            res["queued"] = busy
            return res
        roster_update, batch_id = "", ""
        try:
            if adopted and not t["conversation_id"] and "#" in t["topic_id"]:
                if (s_mem := self.roster.by_topic(t["topic_id"])) is None:
                    log.info("topic %s 等待 sheepdog spawn 新建会话，%d 条信号排队中", t["topic_id"], len(rows))
                    res["waiting_spawn"] = True
                    return res
                w, title = self.cfg.watch, f"{self.cfg.session.title_prefix} {s_mem.display_title}".strip()
                boot = adopted_bootstrap(self.playbook, s_mem, title, "", w.remind_minutes, w.expire_minutes)
                cid, ts = self._new_conversation(self.topic_host(t), t["topic_id"], title, boot, t["model"] or "", t["effort"] or ""), now_iso()
                self.store.update_topic(t["topic_id"], conversation_id=cid, spawned_at=ts, onboarded_at=ts, rules_hash=self.rules_hash(t), compacted_at=None)
                t = self.store.get_topic(t["topic_id"])
            if t["kind"] == ADOPTED and not t["onboarded_at"]:
                res.update(onboarding=self.send_onboarding(t), state=sm.RUNNING)
                self._send_ok(t, 0)
                return res
            if not adopted:
                t = self._ensure_bus_conversation(t)
                if self.store.get_meta("roster_notify_bus") == "1":
                    roster_update = roster_table(self.roster, open_dynamic_topics(self.store), closed_dynamic_topics(self.store, self.roster))
            batch_id = self._new_batch_id("b", t["topic_id"])
            signals, context = [r for r in rows if not _is_pure_owner_context(r)], [r for r in rows if _is_pure_owner_context(r)]
            waiting, inbox = (t["summary"] if t["state"] == sm.WAITING_HUMAN else ""), ([] if adopted else self.store.inbox_summary(self.cfg.self_open_id))
            new_hash = self.rules_hash(t)
            rules_update = self.standing_rules(t) if new_hash and new_hash != t["rules_hash"] else ""
            quotes = {r["message_id"]: quote_line(self.store.get_message(r["reply_to"])) for r in rows if "reply_to" in r.keys() and r["reply_to"]}
            notes = self._collision_notes(t, rows)
            for mid in self.store.seen_before_send_ids(t["topic_id"], [r["message_id"] for r in rows]):
                notes.setdefault(mid, []).append(SEEN_BEFORE_SEND_NOTE)
            digest, digest_fp = self._duty_digest(t) if t["topic_id"] == BUS_TOPIC_ID else ("", "")
            prompt = batch_prompt(
                self.playbook, t["topic_id"], batch_id, rows, inbox, self._session_title(t), waiting, roster_update=roster_update,
                security=self.security, rules_update=rules_update, context_only=not signals, quotes=quotes,
                max_lines=self.cfg.session.max_message_lines, cli_host=self.topic_host(t) != ANTIGRAVITY,
                rules_note=self._compaction_note(t) if rules_update else "", reminder=self.reminder(t), notes=notes,
                digest=digest, images=self._images(rows), vips=self._vips(), peer_status=self._group_peer_status(t))
            self.store.record_dispatch(batch_id, t["topic_id"], [r["message_id"] for r in rows], state="sending")
            self._send(t, prompt, batch_id)
        except SinkError as e:
            if isinstance(e, SinkTimeout) and self._left_sending(t, batch_id, e, res):
                return res
            if batch_id and self.store.get_dispatch(batch_id) is not None:
                self.store.set_dispatch_state(batch_id, "failed", str(e))
            self._fail(t, None, str(e), ATTN_SEND_FAILED); self._send_failed(t, str(e))
            res["error"] = str(e)
            return res
        self.store.set_dispatch_state(batch_id, "sent")
        self._send_ok(t, len(signals))
        if roster_update: self.store.set_meta("roster_notify_bus", "0")
        if digest_fp: self.store.set_meta(DIGEST_META, digest_fp)
        if rules_update:
            self.store.update_topic(t["topic_id"], rules_hash=new_hash, compacted_at=None)
            log.info("topic %s: 规则已更新，随批次 %s 送达%s", t["topic_id"], batch_id, "（压缩后重推）" if t["compacted_at"] else "")
        res.update(self._complete_delivery(t, batch_id, signals, context))
        return res

    # ---------- VIP（7.61） ----------
    def _vips(self) -> dict[str, str]:
        """名册里的 VIP（7.61）：open_id → 名字。主人自己就算写进了 [[person]] 也不算（他的消息不盖章）。"""
        return self.roster.vips(exclude=self.cfg.self_open_id or "")

    # ---------- 主人插话（7.37 第 4 件） ----------
    def _owner_correction(self, r) -> bool:
        """主人在找主人聊天里撤回 / 编辑了自己已经送出去的那条（_handle_change 改成 recalled / edited、回到待推，7.47 第 2 条）。
        不论原来是插队送的还是随普通批次送的：主人改口是急事，同样插队。"""
        me = self.cfg.self_open_id
        return (r["reason"] in OWNER_CORRECTIONS and bool(me) and r["sender_id"] == me
                and r["chat_id"] in self.cfg.owner_side_chat_ids)

    def _priority_rows(self, rows) -> list:
        """插队的消息：主人在找主人聊天里的回复、主人在群里 @ 机器人的指令（7.45 A 类）、主人在聊天里点名 sheepdog 的指令（7.67）、
        主人在找主人聊天里撤回 / 编辑自己发过的话（7.47 第 2 条），加上引用了这些编号的
        escalation_list 系统消息（回复认不出回答哪条时给总线的未结列表 / 认不出来源的说明，7.38 第 5 条），保持原来的先后。
        没有主人的这两种消息就是空表。"""
        own = [
            r for r in rows
            if r["reason"] in (OWNER_REPLY, OWNER_INSTRUCTION) or _has_owner_instruction_tag(r) or self._owner_correction(r)
        ]
        if not own:
            return []
        keep = {r["message_id"] for r in own}
        marks = [f"`{i}`" for i in keep]
        return [r for r in rows if r["message_id"] in keep
                or (r["reason"] == ESCALATION_LIST and any(m in (r["content"] or "") for m in marks))]

    def _priority_blocked(self, t) -> str:
        """会话在跑上一批（running）时，主人插话为什么不能插队（照旧排队）；能插队返回空串。

        不插队：命令行宿主（Codex / Claude 一轮是一次运行，running 就是在跑上一轮，插不进去）；会话被羊群看板判为中断 /
        失联（照旧排队，主人那边在岗表已经通知过）；主人正在会话里（被主人占着）。failed / attention / closed 不是
        running，不走这里。"""
        if self.topic_host(t) != ANTIGRAVITY:
            busy = self._busy(t)
            return f"命令行宿主在跑上一轮（{busy}）" if busy else "命令行宿主在跑上一轮"
        if not t["conversation_id"] or not t["pending_batch_id"]:
            return "没有在跑的批次"
        card = self.store.get_card(t["topic_id"])
        if card is not None and card["run_state"] in duty.STUCK:
            return f"羊群看板判为{duty.RUN_LABEL[card['run_state']]}（{card['detail']}）"
        if (card is not None and card["run_state"] == duty.OCCUPIED) or self._owner_recent(self._owner_typed_at(t, card)):
            return "主人在会话里（被主人占着）"
        return ""

    def _dispatch_priority(self, t, urgent: list, res: dict) -> dict:
        """当轮单独发一批「主人插话」：只放主人的回复。依据 7.34 E6：agentapi send-message 在会话忙时，会在它下一次调模型时
        插进去（秒级）。不改会话状态、不动 pending_batch_id / dispatched_at（等的还是原来那批的回执，回执超时照原来那批算）。"""
        why = self._priority_blocked(t)
        if why:
            log.info("topic %s 在跑上一批，主人插话不插队（%s），%d 条信号排队", t["topic_id"], why, res["pending"])
            res["queued"] = why
            return res
        batch_id = self._new_batch_id(PRIORITY_PREFIX, t["topic_id"])
        try:
            quotes = {r["message_id"]: quote_line(self.store.get_message(r["reply_to"]))
                      for r in urgent if "reply_to" in r.keys() and r["reply_to"]}
            notes = self._collision_notes(t, urgent)
            for mid in self.store.seen_before_send_ids(t["topic_id"], [r["message_id"] for r in urgent]):
                notes.setdefault(mid, []).append(SEEN_BEFORE_SEND_NOTE)
            prompt = priority_prompt(self.playbook, t["topic_id"], batch_id, t["pending_batch_id"], urgent,
                                     self._session_title(t), security=self.security, quotes=quotes,
                                     max_lines=self.cfg.session.max_message_lines, notes=notes,
                                     reminder=self.reminder(t), images=self._images(urgent), vips=self._vips())
            # 同普通批次：先记「正在发」再发送，崩溃后由 _recover_sending 核对（7.16）
            self.store.record_dispatch(batch_id, t["topic_id"], [r["message_id"] for r in urgent], state="sending")
            self._send(t, prompt, batch_id)
        except SinkError as e:
            if isinstance(e, SinkTimeout) and self._left_sending(t, batch_id, e, res):
                return res
            # 发不出去：消息留在待推里，下一轮再试（会话还在跑就再插一次，跑完了就随普通批次走）；会话状态不动
            if self.store.get_dispatch(batch_id) is not None:
                self.store.set_dispatch_state(batch_id, "failed", str(e))
            self._send_failed(t, str(e))
            log.warning("topic %s 主人插话批次 %s 发送失败: %s", t["topic_id"], batch_id, e)
            res["error"] = str(e)
            return res
        self._send_ok(t, len(urgent))
        res.update(self._complete_priority(t, batch_id, urgent))
        log.info("topic %s 在跑 %s，主人插话 %d 条单独送到（批次 %s）", t["topic_id"], t["pending_batch_id"], len(urgent),
                 batch_id)
        return res

    def _left_sending(self, t, batch_id: str, e: SinkTimeout, res: dict) -> bool:
        """7.47 第 7 条：发送超时（agentapi send-message 超时可能其实送到了）。这一批已经记了「正在发」的：留在 sending，
        不判失败、消息不回待推（它们本来就还没标走，下一轮开头 _recover_sending 先核对，核到了补记送达，核不到标 uncertain
        交给第 5 条接着核）；会话状态不动。返回 False = 没有「正在发」的记录（超时发生在记账之前，如 onboarding、新建总线
        会话），调用方按普通 SinkError 处理。"""
        d = self.store.get_dispatch(batch_id) if batch_id else None
        if d is None or d["state"] != "sending":
            return False
        log.warning("topic %s 批次 %s 发送超时，可能已经送到：留在 sending，下一轮到会话记录里核对（%s）",
                    t["topic_id"], batch_id, e)
        res.update(timeout=batch_id, error=str(e))
        return True

    def _complete_priority(self, t, batch_id: str, urgent: list, sent_at: str | None = None) -> dict:
        """主人插话批次送达后的记账（正常发送与崩溃恢复共用）：关闭被回答的「需要你定」、消息记成已回执（不再待推、
        不随原来那批超时退回）、批次记终态 priority（不等回执、不算回执超时）、记路由、点确认表情。会话状态一概不动。"""
        ids = [r["message_id"] for r in urgent]
        for r in urgent:
            for tag in json.loads(r["tags_json"] or "[]"):
                if tag.startswith(ESCALATION_TAG_PREFIX):
                    self._answer_escalation_row(tag.removeprefix(ESCALATION_TAG_PREFIX), r)
        self.store.mark_batch(ids, batch_id, t["topic_id"], "acked")
        self.store.finish_dispatch(batch_id, PRIORITY_DISPATCH)
        try:  # 路由事实（7.20）。出错不挡投递
            duty.record_routes(self.store, t["topic_id"], urgent, self.cfg.duty.patterns, sent_at or now_iso())
        except Exception as e:
            log.warning("记路由事实出错（批次 %s）: %s", batch_id, e)
        if self.acker is not None:
            try:
                self.acker.on_delivered(urgent)
            except Exception as e:
                log.warning("点确认表情失败（批次 %s）: %s", batch_id, e)
        return {"priority": batch_id, "sent": len(ids), "state": self.store.get_topic(t["topic_id"])["state"]}

    # ---------- 主人的话一定有回音（7.52）：回音账本推进、按环节报、回执带回 ----------
    def _echo_reported(self, e, since_iso: str) -> bool:
        tgid = self.cfg.topic_group.chat_id
        if not (tgid and e["chat_id"] == tgid):
            return bool(self.store.owner_sends_since(e["topic_id"], since_iso))
        if (since := _parse(since_iso or "")) is None or since.tzinfo is None:
            return False
        tid, msg = e["topic_id"] or "", self.store.get_message(e["message_id"])
        base, tags = tid.split("#", 1)[0], (msg["tags_json"] or "") if msg is not None else ""
        for r in self.store.conn.execute("SELECT message_id, root_message_id, kind, sent_at FROM sends WHERE (topic_id=? OR topic_id=? OR topic_id LIKE ?) AND kind IN ('ask','report')", (tid, base, f"{base}#%")):
            if self.store.item_by_root(r["root_message_id"] or r["message_id"]) is None and self.store.item_by_root(r["message_id"]) is None:
                continue
            if (at := _parse(r["sent_at"] or "")) is not None and at.tzinfo is not None and (at > since or (at == since and f"escalation:{r['message_id']}" not in tags)):
                return True
        return False

    def _echo_sync(self) -> None:
        """按账本里消息的现状推进欠账：待推 → queued；送到 → delivered；送到后会话发过 ask/report → report；会话关闭 → topic_closed。"""
        live, now = set(self._live_tids()), _now().isoformat(timespec="seconds")
        for e in self.store.echo_open():
            if e["stage"] == ECHO_UNDELIVERED: continue
            if (r := self.store.get_message(e["message_id"])) is None:
                self.store.echo_settle([e["message_id"]], "gone")
                continue
            tid, st = r["topic_id"] or "", r["dispatch_state"]
            if r["route"] != DISPATCH or not tid:
                continue
            if tid != e["topic_id"]:
                self.store.echo_update(e["message_id"], topic_id=tid, alerted="", stage_at=now, **ECHO_UNBOUND)
            if st == "pending" and (e["stage"] != ECHO_QUEUED or tid != e["topic_id"]):
                self.store.echo_update(e["message_id"], stage=ECHO_QUEUED, stage_at=now, batch_id=None, delivered_at=None, **ECHO_UNBOUND)
            elif st in ECHO_DELIVERED_STATES and (e["stage"] != ECHO_DELIVERED or e["batch_id"] != r["batch_id"]):
                d = self.store.get_dispatch(r["batch_id"]) if r["batch_id"] else None
                self.store.echo_update(e["message_id"], stage=ECHO_DELIVERED, stage_at=now, batch_id=r["batch_id"],
                                       delivered_at=(d["sent_at"] if d is not None and d["sent_at"] else now),
                                       priority=int(bool(r["batch_id"]) and r["batch_id"].startswith(PRIORITY_PREFIX)),
                                       **(ECHO_UNBOUND if e["batch_id"] != r["batch_id"] else {}))
        for e in self.store.echo_open():
            if e["stage"] != ECHO_DELIVERED:
                continue
            if e["topic_id"] not in live:
                self.store.echo_settle([e["message_id"]], "topic_closed")
            elif self._echo_reported(e, e["delivered_at"] or e["stage_at"] or now):
                self.store.echo_settle([e["message_id"]], "report")
                clear_handled_owner_reactions(self.store, getattr(self.acker, "im", None) or self.source, e["topic_id"])
            elif self.cfg.session.owner_receipt_relay and not e["relay_batch"] and e["batch_id"] and not self.store.get_meta(f"owner_relay_skipped:{e['batch_id']}"):
                if (d := self.store.get_dispatch(e["batch_id"])) is not None and d["state"] in ("acked", ACKED_LATE) and d["receipt"]:
                    try:
                        rcpt = json.loads(d["receipt"])
                    except json.JSONDecodeError:
                        rcpt = {}
                    if isinstance(rcpt, dict):
                        t_obj, st_val = self.store.get_topic(e["topic_id"]), str(rcpt.get("status") or "handled")
                        if t_obj is not None and is_standing_topic(t_obj) and st_val == "done":
                            st_val = "handled"
                        self.store.echo_bind_relay(e["message_id"], e["batch_id"], str(rcpt.get("received_at") or d["acked_at"] or now), st_val, " ".join(str(rcpt.get("summary") or "").split()))

    def _echo_excerpt(self, e) -> str:
        from . import pending as pend
        r = self.store.get_message(e["message_id"])
        return pend.excerpt(plain_text(r["content"] or "") if r is not None else "", ECHO_EXCERPT_CHARS)

    def _echo_said(self, e) -> str:
        at = _parse(e["said_at"] or "")
        return f"你 {clock(at, self.cfg.timezone_offset)} 说的" if at is not None and at.tzinfo else "你说的"

    def _echo_name(self, topic_id: str) -> str:
        from . import pending as pend
        return pend.session_name(self.cfg, self.store, topic_id)

    def _echo_alerts(self) -> None:
        """按卡住的环节私聊主人，每句每个阶段最多一次；同一轮、同一个会话、同一个阶段的几句合成一条。
        开关 / 分钟数是 0 时，那一阶段直接记成报过（之后再打开也不补报，同 7.47 第 6 条）。"""
        sc = self.cfg.session
        now = _now()
        groups: dict[tuple, list] = {}
        for e in self.store.echo_open():
            done = set(filter(None, (e["alerted"] or "").split(",")))
            stage = e["stage"]
            at = _parse(e["stage_at"] or "")
            if stage == ECHO_UNDELIVERED and ECHO_UNDELIVERED not in done:
                if not sc.owner_undelivered_alert:
                    self.store.echo_mark_alerted([e["message_id"]], ECHO_UNDELIVERED)
                    continue
                groups.setdefault((ECHO_UNDELIVERED, "", 0), []).append(e)
            elif stage == ECHO_QUEUED and ECHO_QUEUED not in done:
                if sc.owner_queue_alert_minutes <= 0:
                    self.store.echo_mark_alerted([e["message_id"]], ECHO_QUEUED)
                elif at is not None and now - at >= timedelta(minutes=sc.owner_queue_alert_minutes):
                    groups.setdefault((ECHO_QUEUED, e["topic_id"], 0), []).append(e)
            elif stage == ECHO_DELIVERED and ECHO_REPLY not in done:
                minutes = sc.owner_priority_followup_minutes if e["priority"] else sc.owner_reply_alert_minutes
                if minutes <= 0:
                    self.store.echo_mark_alerted([e["message_id"]], ECHO_REPLY)
                elif at is not None and now - at >= timedelta(minutes=minutes):
                    groups.setdefault((ECHO_REPLY, e["topic_id"], int(e["priority"] or 0)), []).append(e)
        for (stage, tid, prio), rows in groups.items():
            text, what = self._echo_alert_text(stage, tid, prio, rows)
            ids = [e["message_id"] for e in rows]
            log.info("回音账本：%d 句卡在 %s（%s），私聊主人", len(ids), stage, tid or "-")
            self._notify_owner(text, what, tid,
                               before_send=lambda ids=ids, stage=stage: self.store.echo_mark_alerted(ids, stage))

    def _echo_alert_text(self, stage: str, tid: str, prio: int, rows: list) -> tuple[str, str]:
        sc = self.cfg.session
        quotes = [f"{self._echo_said(e)}「{self._echo_excerpt(e)}」" for e in rows]
        if stage == ECHO_UNDELIVERED:
            lines = [f"这句没送到任何会话：「{self._echo_excerpt(e)}」（{e['reason'] or '原因没记下'}）" for e in rows]
            return f"🐕 汇报 · 你的话没送到会话 [{BUS_TOPIC_ID}]\n" + "\n".join(lines), "回音账本·没送到"
        t = self.store.get_topic(tid)
        name = self._echo_name(tid)
        if stage == ECHO_QUEUED:
            why = f"它在跑批次 `{t['pending_batch_id']}`" if t is not None and t["state"] == sm.RUNNING \
                and t["pending_batch_id"] else f"会话状态 {t['state'] if t is not None else '-'}"
            if t is not None and t["dispatched_at"] and t["state"] == sm.RUNNING:
                why += f"，{clock(_parse(t['dispatched_at']), self.cfg.timezone_offset)} 送出"
            return (f"🐕 汇报 · 你的话还在排队 [{tid}]\n" + "\n".join(f"{q}还在排队等「{name}」（{why}）" for q in quotes),
                    "回音账本·排队")
        if prio:
            title = self._session_title(t) if t is not None else tid
            bid = rows[0]["batch_id"] or ""
            heads = [q.replace("说的", "回的", 1) for q in quotes]
            return (f"🐕 汇报 · 插话没回音 [{BUS_TOPIC_ID}]\n" + "\n".join(heads)
                    + f"\n插话送到了「{title}」（topic `{tid}`，批次 `{bid}`），{sc.owner_priority_followup_minutes} 分钟"
                    "没见它回你，可以去会话里看看，或者再说一次。", "插话没回音提醒")
        return (f"🐕 汇报 · 还没回你 [{tid}]\n" + "\n".join(quotes)
                + f"\n「{name}」在处理，{sc.owner_reply_alert_minutes} 分钟还没回你。可以去会话里看看，或者再说一次。",
                "回音账本·没回音")

    def _echo_round(self) -> None:
        """每轮：推进回音账本、按环节报。7.47 第 6 条的旧记录（meta priority_followup:*）一并清掉：已经收进这本账。"""
        for key, _ in self.store.meta_items(LEGACY_PRIORITY_FOLLOWUP_PREFIX):
            self.store.delete_meta(key)
        self._echo_sync()
        self._echo_relays()
        self._echo_alerts()

    def _echo_relay(self, t, batch_id: str, receipt: dict, status: str = "", summary: str | None = None) -> None:
        """回执带回（7.52 第 1 条）第一步：会话交了回执，把状态与 summary 绑到该由它带回的主人消息上（每句只绑一次）。"""
        if not self.cfg.session.owner_receipt_relay:
            if batch_id:
                self.store.set_meta(f"owner_relay_skipped:{batch_id}", "1")
            return
        if not batch_id:
            return
        self._echo_sync()
        bind_echo_relay(self.cfg, self.store, t["topic_id"], batch_id, receipt, status=status, summary=summary)

    def _echo_destination(self, e) -> tuple[str, bool]:
        """这句主人的话，回音该回到哪：(去处键, 是否回到话题里)。"""
        tgid = self.cfg.topic_group.chat_id
        chat = e["chat_id"] or ""
        r = self.store.get_message(e["message_id"])
        # 找主人那一侧（找主人聊天、话题群）按 7.56 的一处判定认
        if r is None or e["kind"] == "recalled" or self.cfg.chat_scope(chat) != OWNER_SIDE:
            return "new", False
        m = row_to_message(r)
        if tgid and chat == tgid:
            item = next((t for t in json.loads(r["tags_json"] or "[]") if t.startswith("item:")), "")
            return item or f"tg_thread:{r['thread_id'] or m.root_id or r['message_id']}", True
        if is_thread_reply(m):
            return f"thread:{r['thread_id']}", True
        return f"main:{chat}", False

    def _echo_relays(self) -> None:
        """回执带回第二步：等着带回、还没还上的，按去处分组并在到点（waiting 或话题群 0 延迟）后原帖盖楼或私聊带回卡片。"""
        sc = self.cfg.session
        if not sc.owner_receipt_relay: return
        now = _now()
        groups: dict[tuple, list] = {}
        dests: dict[str, bool] = {}
        ready: set[tuple] = set()
        for e in self.store.echo_open():
            if e["stage"] != ECHO_DELIVERED or not e["relay_batch"]:
                continue
            dest, in_thread = self._echo_destination(e)
            eff_wait = timedelta(0) if e["relay_status"] == "waiting" or dest.startswith(("item:", "tg_thread:")) else timedelta(minutes=sc.owner_relay_after_minutes)
            at, since = _parse(e["relay_at"] or ""), _parse(e["stage_at"] or "")
            due = max(x for x in (at, since + eff_wait if since else None) if x)
            dests[dest] = in_thread
            group = (e["topic_id"], e["relay_batch"], dest)
            groups.setdefault(group, []).append(e)
            if now >= due: ready.add(group)
        from .outbound import idem_key
        for (tid, batch_id, dest), rows in groups.items():
            if (tid, batch_id, dest) not in ready:
                continue
            key = f"{ECHO_RELAY_PREFIX}{batch_id}:{dest}"
            if self.store.get_meta(key):
                continue
            last = rows[-1]
            lines = [f"✅ {self._echo_name(tid)} 处理了你的话 [{tid}]"]
            lines += [f"{self._echo_said(e)}「{self._echo_excerpt(e)}」" for e in rows]
            lines += [last["relay_summary"] or "（回执没写说明）",
                      f"状态：{RELAY_STATUS_LABEL.get(last['relay_status'], last['relay_status'] or '已处理')}"]
            anchor = "" if dest == "new" else rows[-1]["message_id"]
            if anchor.startswith("sd_card_cb_"):
                anchor = (self.store.get_message(anchor) or {"reply_to": ""})["reply_to"] or ""
            in_thread = dests[dest] and bool(anchor)
            ids = [e["message_id"] for e in rows]

            def settle(ids=ids, key=key, tid=tid, st=last["relay_status"]) -> None:
                self.store.echo_settle(ids, "relay")
                self.store.set_meta(key, now_iso())
                if st in ("handled", "done", ""):
                    clear_handled_owner_reactions(self.store, getattr(self.acker, "im", None) or self.source, tid)

            if self._notify_owner("\n".join(lines), "回执带回", tid, before_send=settle, reply_to=anchor,
                                  in_thread=in_thread, idem_key=idem_key("relay", tid, batch_id, dest)):
                log.info("回执带回 %s/%s → %s：%d 句主人的话，%s", tid, batch_id, dest, len(ids),
                         f"回复 {anchor}" + ("（话题里）" if in_thread else "") if anchor else "找主人聊天新起一条")

    # ---------- 连续投递失败告诉主人（第一轮报告第 6 节问题 4） ----------
    def _send_failed(self, t, error: str) -> None:
        """投递失败一次（agentapi 发不出去、宿主没开……）：同一会话连续 SEND_FAIL_NOTICE 次，私聊主人一次。不扣消息
        （消息照常排队、每轮重试）。这一段里会话已经进了 attention 的，7.21 的 attention 通知说过了，不再发。"""
        tid = t["topic_id"]
        key = send_fail_key(tid)
        try:
            rec = json.loads(self.store.get_meta(key) or "{}")
        except json.JSONDecodeError:
            rec = {}
        if not isinstance(rec, dict):
            rec = {}
        rec["count"] = int(rec.get("count") or 0) + 1
        rec.setdefault("since", now_iso())
        rec["error"] = " ".join((error or "").split())[-200:]
        nt = self.store.get_topic(tid)
        if not rec.get("notified") and not rec.get("covered"):
            if nt is not None and nt["state"] == sm.ATTENTION:
                rec["covered"] = True
            elif rec["count"] >= SEND_FAIL_NOTICE:
                host = HOST_LABEL.get(self.topic_host(nt), self.topic_host(nt))
                title = self._session_title(nt) or tid
                text = (f"🐕 汇报 · 投递不出去 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`，{host}）从 "
                        f"{display_time(_parse(rec['since']), self.cfg.timezone_offset)}起连续 {rec['count']} 次投递失败，"
                        f"最近一次：{rec['error']}。\n消息照常排队、一条不丢，sheepdog 每轮重试；发出去了会再告诉你。")
                log.warning("会话 %s 连续 %d 次投递失败，私聊主人", tid, rec["count"])

                def mark() -> None:
                    rec["notified"] = True
                    self.store.set_meta(key, json.dumps(rec, ensure_ascii=False))

                self._notify_owner(text, "投递失败通知", tid, before_send=mark)
        self.store.set_meta(key, json.dumps(rec, ensure_ascii=False))

    def _send_ok(self, t, sent: int) -> None:
        """投递成功：清掉连续失败的记录；之前私聊过「投递不出去」的，再说一句已恢复。"""
        tid = t["topic_id"]
        key = send_fail_key(tid)
        raw = self.store.get_meta(key)
        if not raw:
            return
        self.store.set_meta(key, "")
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(rec, dict) or not rec.get("notified"):
            return
        title = self._session_title(t) or tid
        self._notify_owner(f"🐕 汇报 · 投递已恢复 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）投递恢复了"
                           f"（之前连续失败 {rec.get('count')} 次），这一批送出 {sent} 条。", "投递恢复通知", tid)

    def _answer_escalation_row(self, esc_mid: str, r) -> None:
        self.store.answer_escalation(esc_mid, r["message_id"], allow_update=True)
        from . import decision_hub
        is_cb = "raw_json" in r.keys() and '"_sd_card_callback"' in (r["raw_json"] or "")
        if decision_hub.hub_active(self.cfg, self.store) and not is_cb:
            decision_hub.sync_escalation_dual_cards(
                self.cfg, self.store, self.source, esc_mid,
                chosen_display=" ".join(plain_text(r["content"] or "").split())[:60] or "主人文字批复",
                answered_at=r["create_time"] or now_iso())

    def _complete_delivery(self, t, batch_id: str, signals: list, context: list, sent_at: str | None = None) -> dict:
        """批次已送达后的记账：标记消息、改会话状态、关闭被回答的「需要你定」、点确认表情。正常发送与崩溃恢复共用。"""
        ids = [r["message_id"] for r in signals]
        ctx_ids = [r["message_id"] for r in context]
        for r in signals:
            for tag in json.loads(r["tags_json"] or "[]"):
                if tag.startswith(ESCALATION_TAG_PREFIX):
                    self._answer_escalation_row(tag.removeprefix(ESCALATION_TAG_PREFIX), r)
        self.store.mark_batch(ctx_ids, batch_id, t["topic_id"], "context")
        out: dict = {"context": len(ctx_ids), "batch_id": batch_id}
        if not signals:
            # 只有背景：不要回执、不改变会话状态、不会超时重投
            self.store.finish_dispatch(batch_id, "context")
            return out
        self.store.mark_batch(ids, batch_id, t["topic_id"], "delivered")
        try:  # 路由事实（7.20）：带编号的消息送到了这个会话。出错不挡投递
            duty.record_routes(self.store, t["topic_id"], signals, self.cfg.duty.patterns, sent_at or now_iso())
        except Exception as e:
            log.warning("记路由事实出错（批次 %s）: %s", batch_id, e)
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

    def _batch_in_record(self, t, batch_id: str) -> bool:
        """会话记录里有没有这个批次号（发送中断 / 超时后核对是否已送达，7.16）。"""
        if t is None or not t["conversation_id"]:
            return False
        sink = self.sinks.get(self.topic_host(t))
        contains = getattr(sink, "transcript_contains", None) or transcript_contains
        return contains(t["conversation_id"], batch_id)

    def _found_delivered(self, t, d, rows: list) -> None:
        """核到了：批次补记已送达，消息照正常发送的记账（插话批次走 _complete_priority，不改会话状态）。"""
        self.store.set_dispatch_state(d["batch_id"], "sent")
        if d["batch_id"].startswith(PRIORITY_PREFIX):
            self._complete_priority(t, d["batch_id"], rows, d["sent_at"])
        else:
            self._complete_delivery(t, d["batch_id"], [r for r in rows if not _is_pure_owner_context(r)],
                                    [r for r in rows if _is_pure_owner_context(r)], d["sent_at"])

    def _recover_sending(self) -> None:
        """上一轮「正在发」就中断（或发送超时，7.47 第 7 条）的批次（7.16）：到会话 transcript 里找批次号——找到就补记
        已送达；找不到标 uncertain，消息不再自动重发，之后每轮接着核（_recheck_uncertain），等人工 sheepdog redeliver
        --batch 确认。"""
        for d in self.store.dispatches_in_state("sending"):
            t = self.store.get_topic(d["topic_id"])
            ids = json.loads(d["message_ids"] or "[]")
            rows = [r for r in (self.store.get_message(i) for i in ids) if r is not None]
            if self._batch_in_record(t, d["batch_id"]):
                self._found_delivered(t, d, rows)
                log.info("批次 %s 发送中断，但 transcript 里已有，补记为已送达", d["batch_id"])
            else:
                self.store.set_dispatch_state(d["batch_id"], "uncertain", "发送中断，transcript 里找不到这个批次")
                self.store.mark_batch(ids, d["batch_id"], d["topic_id"], "uncertain")
                log.warning("批次 %s（%s）发送中断，不确定是否送达，不自动重发；确认后用 sheepdog redeliver --batch %s",
                            d["batch_id"], d["topic_id"], d["batch_id"])

    def _recheck_uncertain(self) -> None:
        """7.47 第 5 条：UNCERTAIN_RECHECK_HOURS 小时内的 uncertain 批次每轮再到会话记录里找一次批次号（会话忙时 agentapi
        收下的消息要等它下一次调模型才写进记录，重启那一轮多半还找不到）：找到就补记送达（同 _recover_sending 找到的那支）；
        从送出时间起超过 UNCERTAIN_NOTICE_MINUTES 分钟还找不到，私聊主人一次（每批一次）。仍不自动重发：送到了却核不到时
        重发就是重复。普通批次和插话批次都管。"""
        now = _now()
        for d in self.store.dispatches_in_state("uncertain"):
            sent = _parse(d["sent_at"] or "")
            if sent is None or sent.tzinfo is None or now - sent > timedelta(hours=UNCERTAIN_RECHECK_HOURS):
                continue
            t = self.store.get_topic(d["topic_id"])
            if t is None:
                continue
            if self._batch_in_record(t, d["batch_id"]):
                # 只补记还挂在这一批、还是 uncertain 的消息（期间被转走 / 重发的不动）
                rows = [r for r in (self.store.get_message(i) for i in json.loads(d["message_ids"] or "[]"))
                        if r is not None and r["batch_id"] == d["batch_id"] and r["dispatch_state"] == "uncertain"]
                if not rows:   # 消息期间都被转走了：只记这一批送到过，不再记账
                    self.store.set_dispatch_state(d["batch_id"], "sent", "核到时消息已不在这一批")
                    continue
                self._found_delivered(t, d, rows)
                nt = self.store.get_topic(t["topic_id"])
                if nt is not None and nt["pending_batch_id"] == d["batch_id"]:
                    # 普通批次这时才算会话开始做它：回执超时从现在算，不然一补记就超时（总线超时会退回重投 = 重复）
                    self.store.update_topic(t["topic_id"], dispatched_at=now.isoformat(timespec="seconds"))
                log.info("uncertain 批次 %s（%s）在会话记录里核到了，补记为已送达", d["batch_id"], d["topic_id"])
                continue
            if now - sent >= timedelta(minutes=UNCERTAIN_NOTICE_MINUTES):
                self._uncertain_notice(t, d, sent)

    def _uncertain_notice(self, t, d, sent: datetime) -> None:
        key = uncertain_notice_key(d["batch_id"])
        if self.store.get_meta(key):
            return
        tid, bid = t["topic_id"], d["batch_id"]
        n = len(json.loads(d["message_ids"] or "[]"))
        kind = "主人插话批次" if bid.startswith(PRIORITY_PREFIX) else "批次"
        title = self._session_title(t) or tid
        text = (f"🐕 汇报 · 不确定送到没有 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）的{kind} `{bid}`（{n} 条消息，"
                f"{display_time(sent, self.cfg.timezone_offset)}送出）发送中断，{UNCERTAIN_NOTICE_MINUTES} 分钟了会话记录里"
                f"还找不到它，不知道送到没有。sheepdog 不自动重发（送到了却核不到时会重复）。\n"
                f"去会话里看一眼；确认没送到就重发：`sheepdog redeliver --batch {bid}`")
        log.warning("uncertain 批次 %s（%s）%d 分钟还核不到，私聊主人", bid, tid, UNCERTAIN_NOTICE_MINUTES)
        self._notify_owner(text, "投递不确定通知", tid, before_send=lambda: self.store.set_meta(key, now_iso()))

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

    def _notify_owner(self, text: str, what: str, topic_id: str = "", before_send=None, reply_to: str = "",
                      in_thread: bool = False, idem_key: str = "") -> bool:
        """给主人的通知（危险命令报警、在岗表中断 / 失联 / 已恢复、attention / 恢复、投递不出去 / 已恢复、写锁提醒、
        登录提醒）。没有发送渠道时只记日志，返回 False。

        - [notify] file（notifier.ledger 为 False）：照旧直接写文件，不落账本、不重试；写不进去返回 False；
        - 其他（经机器人私聊、dry-run 打印、测试里的假渠道）：先落给主人的通知账本（正文原样字节 + 防重编号），再发。
          发不出去留在待发，之后每一轮按退避重试，重启以后接着试（_retry_owner_notices）。落了账本就返回 True——
          这条通知一定会发出去，或者 2 小时后标放弃、在 doctor / board 上一直提示到有人处理；调用方不要再拼一条重发。

        before_send：调用方「这件事已经通知过」的记账，在落账本之后、真正发送之前做（写文件模式在写成以后做）。
        记账放在发送之前，进程在「发出去了、还没记账」之间没了，重启以后不会把同一件事再拼一条新通知（那条会带新的
        防重编号，飞书去不了重）；落了账本的那条由重试接着发、靠防重编号去重。
        topic_id：说的是哪个会话（登录提醒说的是宿主，留空）。
        reply_to / in_thread（7.52 回执带回）：回复主人的哪一条、是否回到它的话题里；渠道不支持回复（写文件、打印）时照常发。
        idem_key（7.52）：调用方定的防重编号（不给就新起一个）；账本里已经有这个编号的通知 = 这件事已经落过账，不再落、
        照常做 before_send 的记账，返回 True。"""
        if self.notifier is None:
            return False
        text = with_cli(text, self.cfg.cli_command)
        if not getattr(self.notifier, "ledger", True):
            try:
                self.notifier.notify(text, what)
            except Exception as e:
                log.warning("给主人发%s失败: %s", what, e)
                return False
            if before_send is not None:
                before_send()
            return True
        if idem_key and self.store.owner_notice_by_key(idem_key) is not None:
            if before_send is not None:
                before_send()
            return True
        nid = self.store.add_owner_notice(what, topic_id, text.encode("utf-8"), idem_key or new_idempotency_key(), reply_to,
                                          in_thread)
        if before_send is not None:
            before_send()
        self._send_notice(self.store.get_owner_notice(nid))
        return True

    def _notice_channel(self) -> bool:
        """有没有能重试的发送渠道（[notify] file 和没有渠道都不重试）。"""
        return self.notifier is not None and getattr(self.notifier, "ledger", True)

    def _send_notice(self, row) -> bool:
        from . import alert_group
        return alert_group.send_owner_notice(self, row)

    def _abandon_notice(self, row, error: str) -> None:
        from . import alert_group
        alert_group.abandon_owner_notice(self, row, error)

    def _retry_owner_notices(self) -> None:
        from . import alert_group
        alert_group.retry_owner_notices(self)

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
        self._notify_owner(text, "写锁提醒", t["topic_id"])

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

    def _record_mtime(self, t) -> datetime | None:
        """会话记录最后一次写入的时间（失联看它）；找不到记录（没有文件、读不了、Codex 找不到 rollout）就是 None。"""
        path = self._record_file(t)
        if path is None:
            return None
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except OSError:
            return None

    def _record_missing(self, t) -> bool:
        """在岗表看会话记录的宿主（Antigravity、Codex），会话有编号，却找不到它的记录。"""
        return self.topic_host(t) in RECORD_HOSTS and bool(t["conversation_id"]) and self._record_mtime(t) is None

    def _read_from_start(self, topic_id: str, conversation_id: str) -> None:
        """sheepdog 自己刚建的会话：各种用途都从头读（第一轮跑的命令也要审）。记录文件还没有，路径先空着。"""
        if not topic_id or not conversation_id:
            return
        for purpose in (COMPACTION, AUDIT, DUTY, LAMB_DISCOVERY):
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
                try:
                    self._resend_rules(self.store.get_topic(t["topic_id"]), found[-1])
                except Exception as e:  # 补发出错不挡别的：下一批照样带上完整规则
                    log.warning("topic %s: 压缩后立即补发规则出错（下一批照样带上完整规则）: %s", t["topic_id"], e)

    def _batch_in_hand(self, t) -> str:
        """手上有批次在跑（送出了、在等回执），或者有没交差的批次：返回批次号，否则空串。

        没交差 = 判过回执超时、之后还没交过回执：总线退回重投的（failed），以及 adopted / 总线新开的会话回执超时记的
        missed——发版这类会话常干超过 20 分钟的长活，回执超时以后多半还在做（架构侧 10-12 定：这里和羊群看板
        _quiet_since 的口径不同，missed 也算没交差；多补发一次规则只多花点 token）。之后交过回执（acked / acked_late）
        的不算；只有背景、发送失败、不确定送没送到的批次不算；一个批次都没有的不算。"""
        if t["pending_batch_id"] and t["state"] == sm.RUNNING:
            return t["pending_batch_id"]
        for d in self.store.recent_dispatches(t["topic_id"]):  # 新的在前
            if d["state"] in ("acked", ACKED_LATE):
                return ""
            if d["state"] in ("failed", "missed") and (d["error"] or "").startswith(RECEIPT_TIMEOUT):
                return d["batch_id"]
        return ""

    def _compaction_point(self, t, d: dict) -> dict:
        """一个压缩点的身份：会话编号、记录里的时间、步号。记录重读（文件换了、变短了）再认出来也是同一个。"""
        at = record_time(self.topic_host(t), d)
        return {"cid": t["conversation_id"] or "", "at": at.isoformat() if at is not None else "",
                "step": d.get("step_index")}

    @staticmethod
    def _point_seen(last: dict, point: dict) -> bool:
        """这个压缩点补发过了没有：和上次补发的是同一个，或者是同一个会话里更早的压缩点（记录重读又认出来的）。"""
        if not last or last.get("cid") != point["cid"]:
            return False
        if last == point:
            return True
        a, b = _parse(last.get("at") or ""), _parse(point["at"])
        return a is not None and b is not None and b <= a

    def _resend_rules(self, t, point: dict) -> None:
        """认出压缩时，Antigravity 会话手上有批次在跑（或有没交差的批次）：不等下一批，立即单独发一条系统消息，内容是
        完整现行规则（和下一批重推的一样，开头写明是因为认出了压缩才补发）。

        这条不是批次：不改 pending_batch_id、不记 dispatches，回执、回执超时、在岗表的批次口径都不受影响。发出去以后
        规则指纹记成最新，下一批不再重推。同一个压缩点只补发一次（记在 meta 里，重启以后也一样）；发送失败不重试，
        退回原来的做法（指纹已清空，下一批带上完整规则）。主人在会话里（human_attached）不插话。
        Codex / Claude 会话一轮跑着的时候送不进去，保持原来的「下一批重推」。"""
        tid = t["topic_id"]
        if self.topic_host(t) != ANTIGRAVITY or t["state"] in (sm.HUMAN_ATTACHED, sm.CLOSED):
            return
        batch = self._batch_in_hand(t)
        if not batch:
            return
        key = compaction_resend_key(tid)
        pid = self._compaction_point(t, point)
        try:
            last = json.loads(self.store.get_meta(key) or "{}")
        except json.JSONDecodeError:
            last = {}
        if isinstance(last, dict) and self._point_seen(last, pid):
            log.info("topic %s: 这个压缩点已经补发过规则，不再补发", tid)
            return
        rules = self.standing_rules(t)
        if not rules:
            return
        # 先记再发：同一个压缩点只补发一次
        self.store.set_meta(key, json.dumps(pid, ensure_ascii=False, sort_keys=True))
        try:
            self._send(t, compaction_rules_message(rules, self._compaction_note(t), batch))
        except SinkError as e:
            log.warning("topic %s: 压缩后立即补发规则没发出去（%s），下一批带上完整规则", tid, e)
            return
        self.store.update_topic(tid, rules_hash=self.rules_hash(t), compacted_at=None)
        log.info("topic %s: 检测到压缩，手上批次 %s 还没交差，已立即补发完整规则（不是批次；下一批不再重推）", tid, batch)

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

    # ---------- 羔羊（7.63） ----------
    def _bind_subagent_cids(self, tid: str, host: str, off: int, d: dict, discovered: list[str]) -> None:
        for cid in subagent_created_cids_in(host, d):
            existing_by_cid = self.store.find_lamb_by_cid(cid)
            if existing_by_cid is not None and existing_by_cid["state"] == "running":
                if existing_by_cid["parent_topic_id"] == tid:
                    dup_empty = next((lb for lb in self.store.get_active_lambs(tid)
                                      if lb["lamb_id"] != existing_by_cid["lamb_id"]
                                      and lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip()), None)
                    if dup_empty is not None:
                        merged_title = existing_by_cid["task_title"]
                        if (not merged_title or merged_title in ("subagent", existing_by_cid["lamb_id"])) and dup_empty["task_title"]:
                            merged_title = dup_empty["task_title"]
                        self.store.update_lamb(existing_by_cid["lamb_id"], task_title=merged_title)
                        with self.store.tx() as c: c.execute("DELETE FROM lambs WHERE lamb_id=?", (dup_empty["lamb_id"],))
                continue
            matched = next((lb for lb in self.store.get_active_lambs(tid)
                            if lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip()), None)
            ts = str(d.get("created_at") or d.get("timestamp") or "") or now_iso()
            if matched is not None:
                self.store.update_lamb(matched["lamb_id"], conversation_id=cid, last_act_at=ts)
                log.info("topic %s: 关联 subagent conversation_id %s -> %s (%s)", tid, cid, matched["lamb_id"], matched["task_title"])
            else:
                lid = f"lamb_{hashlib.sha1(f'{tid}:{off}:{cid}'.encode('utf-8')).hexdigest()[:8]}"
                if self.store.get_lamb(lid) is None:
                    self.store.create_lamb(lid, tid, kind="subagent", task_title="subagent", conversation_id=cid, state="running", started_at=ts, last_act_at=ts)
                    discovered.append(lid)
                    log.info("topic %s: 从 GENERIC 自动补建 subagent -> %s (%s)", tid, lid, cid)
        parent_cid = (t["conversation_id"] or "") if (t := self.store.get_topic(tid)) is not None else ""
        for cid in subagent_reused_cids_in(host, d, parent_cid=parent_cid):
            if any((lb["conversation_id"] or "").strip() == cid for lb in self.store.get_active_lambs(tid)):
                continue
            matched = next((lb for lb in self.store.get_active_lambs(tid)
                            if lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip()), None)
            if matched is not None:
                ts = str(d.get("created_at") or d.get("timestamp") or "") or now_iso()
                self.store.update_lamb(matched["lamb_id"], conversation_id=cid, last_act_at=ts)
                log.info("topic %s: 从 send_message/引用 关联复用 subagent conversation_id %s -> %s (%s)",
                         tid, cid, matched["lamb_id"], matched["task_title"])

    def _handle_lamb_line(self, tid: str, host: str, off: int, d: dict, discovered: list[str]) -> None:
        new_lids_in_line: set[str] = set()
        for inv in subagent_invocations_in(host, d, off):
            if inv.conversation_id and (ex := self.store.find_lamb_by_cid(inv.conversation_id)) is not None and ex["state"] == "running":
                continue
            active = [lb for lb in self.store.get_active_lambs(tid) if lb["lamb_id"] not in new_lids_in_line]
            raw_id = (inv.lamb_id or "").strip()
            matched = (next((lb for lb in active if raw_id and lb["lamb_id"] == raw_id), None)
                       or next((lb for lb in active if lb["task_title"] == inv.task_title and (not (lb["conversation_id"] or "").strip() or lb["conversation_id"] == inv.conversation_id)), None)
                       or next((lb for lb in active if lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip()), None))
            if matched is not None:
                new_lids_in_line.add(matched["lamb_id"])
                if inv.conversation_id and not matched["conversation_id"]:
                    self.store.update_lamb(matched["lamb_id"], conversation_id=inv.conversation_id, last_act_at=inv.at or now_iso())
                continue
            lid = raw_id if (raw_id.startswith("lamb_") and self.store.get_lamb(raw_id) is None) else f"lamb_{hashlib.sha1(f'{tid}:{off}:{inv.step}:{inv.task_title}:{inv.conversation_id}'.encode('utf-8')).hexdigest()[:8]}"
            if self.store.get_lamb(lid) is not None:
                continue
            ts = inv.at or now_iso()
            self.store.create_lamb(lid, tid, kind="subagent", task_title=inv.task_title or "subagent", conversation_id=inv.conversation_id or None, state="running", started_at=ts, last_act_at=ts)
            new_lids_in_line.add(lid); discovered.append(lid)
            log.info("topic %s: 从 transcript 自动发现 invoke_subagent -> %s (%s)", tid, lid, inv.task_title)
        self._bind_subagent_cids(tid, host, off, d, discovered)
        if host == ANTIGRAVITY and d.get("type") == "GENERIC":
            err_low = f"{d.get('content') or ''}\n{d.get('error') or ''}".lower()
            if str(d.get("status") or "").upper() == "ERROR" or "denied by pre-tool hook" in err_low or "encountered error in step execution" in err_low:
                for lb in self.store.get_active_lambs(tid):
                    if lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip():
                        complete_lamb(self.store, lb["lamb_id"], summary="子代理启动被宿主安全策略拦截或报错", state="failed", report_msg_id="subagent_spawn_error")
        for ret in subagent_returns_in(host, d):
            cands = ([lb for lb in self.store.get_active_lambs(tid) if (lb["conversation_id"] or "").strip() == ret.conversation_id]
                     or [lb for lb in self.store.list_lambs(tid, include_finished=True) if lb["state"] == "timeout" and (lb["conversation_id"] or "").strip() == ret.conversation_id])
            for matched in cands:
                lid = matched["lamb_id"]
                complete_lamb(self.store, lid, summary=ret.summary or matched["result_summary"] or f"羔羊 {lid} 已完成", state="done", report_msg_id="native_system_message")
                log.info("topic %s: 从 SYSTEM_MESSAGE 自动结案 subagent 羔羊 %s (%s)", tid, lid, ret.conversation_id)
        kill_all, killed_cids = subagent_kills_in(host, d)
        if kill_all or killed_cids:
            killed_set = set(killed_cids)
            for lb in self.store.get_active_lambs(tid):
                if lb["kind"] not in ("subagent", "native_subagent"): continue
                cid = (lb["conversation_id"] or "").strip()
                if (kill_all and lb["lamb_id"] in new_lids_in_line and cid not in killed_set) or (not kill_all and cid not in killed_set): continue
                complete_lamb(self.store, lb["lamb_id"], summary=lb["result_summary"] or "已由母羊销毁", state="done", report_msg_id=lb["report_msg_id"] or "killed_by_parent")
                log.info("topic %s: 从 manage_subagents 自动结案 subagent 羔羊 %s (%s)", tid, lb["lamb_id"], cid)

    def _scan_lambs(self) -> list[str]:
        """7.63：增量扫描母羊 transcript 中的 invoke_subagent 工具调用、GENERIC 子代理创建回写/报错与 SYSTEM_MESSAGE 子代理回传结案。"""
        from . import guard as _g
        if b"pre-tool hook" not in _g._NEEDLES[LAMB_DISCOVERY]:
            _g._NEEDLES[LAMB_DISCOVERY] = (*_g._NEEDLES[LAMB_DISCOVERY], b"pre-tool hook", b"Encountered error", b'"ERROR"')
        discovered: list[str] = []
        for t in self.store.list_topics():
            if (t["topic_id"] != BUS_TOPIC_ID and t["kind"] not in MANAGED_KINDS) or t["state"] == sm.CLOSED \
                    or not t["conversation_id"]:
                continue
            tid, host = t["topic_id"], self.topic_host(t)
            self._follow(t, LAMB_DISCOVERY, lambda off, d, tid=tid, host=host: self._handle_lamb_line(tid, host, off, d, discovered),
                         backfill=LAST_ACTIVITY_TAIL)
        return discovered

    discover_lambs_from_transcript = _scan_lambs

    def _tick_lambs(self) -> list[str]:
        """7.63：每轮扫描 invoke_subagent、检查 CLI 子进程存活、刷新活跃羔羊心跳、判定羔羊超时（timeout_minutes），并向母羊回流已结束羔羊的 lamb_report。"""
        self._scan_lambs()
        now = _now()
        timeout_delta = timedelta(minutes=self.cfg.lamb.timeout_minutes)
        for lb in self.store.get_active_lambs():
            lid, tid = lb["lamb_id"], lb["parent_topic_id"]
            if lb["kind"] in ("cli", "bash_worker") and lb["pid"]:
                if not _pid_alive(int(lb["pid"])) and lb["state"] == "running":
                    raw_log = ""
                    if lb["log_path"]:
                        try:
                            lp = Path(lb["log_path"])
                            if lp.exists():
                                raw_log = lp.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            pass
                    tail_text, has_err = _summarize_cli_lamb_log(raw_log)
                    st = "failed" if has_err else "done"
                    summary = tail_text or lb["result_summary"] or "后台进程已退出"
                    complete_lamb(self.store, lid, summary=summary, state=st)
                    log.info("CLI 羔羊 %s（母羊 %s，pid=%s）进程已退出，自动按 %s 收尾", lid, tid, lb["pid"], st)
                    continue
                act_iso = now.astimezone().isoformat(timespec="seconds")
                self.store.update_lamb(lid, last_act_at=act_iso)
                lb["last_act_at"] = act_iso
            if not lb["conversation_id"] and lb["kind"] in ("subagent", "native_subagent"):
                pt = self.store.get_topic(tid)
                if pt is not None and pt["conversation_id"]:
                    pt_tp = transcript_path(pt["conversation_id"])
                    start_dt = _parse(lb["started_at"] or "")
                    window_iso = (start_dt - timedelta(seconds=120)).isoformat() if start_dt else ""
                    ref_iso = (start_dt - timedelta(seconds=5)).isoformat() if start_dt else ""
                    busy_cids = {(x["conversation_id"] or "").strip() for x in self.store.get_active_lambs(tid)
                                 if x["lamb_id"] != lid and (x["conversation_id"] or "").strip()}
                    cands = [c for c in recent_subagent_cids_since(self.topic_host(pt), pt_tp, window_iso,
                                                                   parent_cid=pt["conversation_id"], ref_since_iso=ref_iso)
                             if c[0] not in busy_cids]
                    if cands:
                        cid, cid_ts = cands[-1]
                        lb["conversation_id"] = cid
                        self.store.update_lamb(lid, conversation_id=cid, last_act_at=cid_ts or lb["last_act_at"] or now_iso())
                        log.info("topic %s: 从母羊近期 transcript 自动补绑 subagent conversation_id %s -> %s (%s)",
                                 tid, cid, lid, lb["task_title"])
            last_dt = _parse(lb["last_act_at"] or "") or _parse(lb["started_at"] or "")
            if lb["conversation_id"]:
                tp = transcript_path(lb["conversation_id"])
                act = last_activity_at(ANTIGRAVITY, tp) if tp.exists() else None
                if act is not None and (last_dt is None or act > last_dt):
                    last_dt = act
                    self.store.update_lamb(lid, last_act_at=act.astimezone().isoformat(timespec="seconds"))
            if last_dt is not None and now - last_dt >= timeout_delta:
                summary = lb["result_summary"] or f"超过 {self.cfg.lamb.timeout_minutes} 分钟无心跳，已超时"
                complete_lamb(self.store, lid, summary=summary, state="timeout")
                log.warning("羔羊 %s（母羊 %s）超过 %d 分钟无心跳，已标为 timeout 并唤醒母羊",
                            lid, tid, self.cfg.lamb.timeout_minutes)
        reported: list[str] = []
        for lb in self.store.get_unreported_finished_lambs():
            complete_lamb(self.store, lb["lamb_id"], summary=lb["result_summary"] or "", state=lb["state"])
            reported.append(lb["lamb_id"])
        return reported

    def _audit_commands(self) -> None:
        """K3：增量扫 [audit] topics 对应会话（及名下羔羊 7.63）新跑的命令，命中 danger_patterns 的记下来，再按会话合并报警。"""
        a = self.cfg.audit
        patterns, ignores = a.patterns, a.ignores
        if patterns:
            for tid in a.topics:
                t = self.store.get_topic(tid)
                if t is None or t["state"] == sm.CLOSED:
                    continue
                host = self.topic_host(t)
                targets: list[tuple[dict, str]] = []
                if t["conversation_id"]:
                    targets.append((t, t["conversation_id"]))
                for lb in self.store.list_lambs(parent_topic_id=tid, include_finished=True):
                    if lb["conversation_id"]:
                        lt = {"topic_id": f"{tid}:lamb:{lb['lamb_id']}", "conversation_id": lb["conversation_id"],
                              "host": ANTIGRAVITY}
                        targets.append((lt, lb["conversation_id"]))
                for target_t, cid in targets:
                    thost = self.topic_host(target_t)

                    def handle(off: int, d: dict, tid=tid, host=thost, cid=cid) -> None:
                        for c in commands_in(host, d, off):
                            pat = first_match(c.text, patterns)
                            if pat and (skip := first_match(c.text, ignores)):
                                log.info("会话 %s 的命令命中 %s，但也命中忽略规则 %s，不报", tid, pat, skip)
                                continue
                            shown = redact(c.text)
                            if pat and self.store.add_audit_hit(tid, cid, c.step, shown, pat, c.at):
                                log.warning("会话 %s 跑了危险命令（命中 %s）：%s", tid, pat, shown)

                    is_lamb = str(target_t["topic_id"]).startswith(f"{tid}:lamb:")
                    self._follow(target_t, AUDIT, handle, backfill=LAST_ACTIVITY_TAIL if is_lamb else 0)
        self._flush_audit()

    def _flush_audit(self) -> None:
        """还没处理的命中按会话报。合并只合并已经报过的同一条命令：10 分钟内报过的（按整理后的命令文本比）只记一笔、
        并进那次报警，不再单独报；之前没报过的命令马上再报一次，同一轮里新出现的几条合成一条。

        每次报警都给主人发一条、给犯规的会话再排一条停手提示、照旧知会 also_notify。报不报只看命令有没有报过，
        不看给主人的上一条通知发出去没有（还在待发的那条由通知账本接着重试），所以前一条没发出去也压不住新的。"""
        groups: dict[str, list] = {}
        for h in self.store.pending_audit_hits():
            tid = h["topic_id"]
            if self.store.get_topic(tid) is None:
                lb = self.store.get_lamb(tid) or self.store.find_lamb_by_cid(h["conversation_id"] or tid)
                if lb is not None:
                    tid = lb["parent_topic_id"]
            groups.setdefault(tid, []).append(h)
        cutoff = _now() - timedelta(minutes=AUDIT_MERGE_MINUTES)
        for tid, hits in groups.items():
            reported, alerts = self._audit_window(tid, cutoff)
            repeats = [h for h in hits if h["command"] in reported]
            fresh = [h for h in hits if h["command"] not in reported]
            for h in repeats:
                self.store.mark_audit_notified([h["id"]], reported[h["command"]], merged=True)
            if repeats:
                log.info("会话 %s 又跑了 %d 分钟内报过的命令 %d 条，并进之前的报警，不再单独报", tid,
                         AUDIT_MERGE_MINUTES, len(repeats))
            if not fresh:
                continue
            self._send_audit(tid, fresh, alerts + 1,
                             mark=lambda ids=[h["id"] for h in fresh]: self.store.mark_audit_notified(ids, ids[0]))

    def _audit_window(self, tid: str, cutoff: datetime) -> tuple[dict[str, int], int]:
        """这个会话从 cutoff 起报过的命令 {整理后的命令文本: 那次报警的编号}，和报过几次警。
        并进别的报警的命中不算「报过」（同一条命令 10 分钟以后再跑会再报一次）。老账本里没有报警编号的，按报的时间认。"""
        reported: dict[str, int] = {}
        alerts: set = set()
        for h in self.store.handled_audit_hits(tid):
            at = _parse(h["notified_at"] or "")
            if at is None or at.tzinfo is None or at < cutoff or h["merged"]:
                continue
            alert = h["alert_id"] if h["alert_id"] is not None else h["notified_at"]
            alerts.add(alert)
            reported.setdefault(h["command"], h["alert_id"] if h["alert_id"] is not None else h["id"])
        return reported, len(alerts)

    def _send_audit(self, tid: str, hits: list, nth: int = 1, mark=None) -> None:
        """经机器人私聊主人；给犯规的会话、[audit] also_notify 的会话各排一条系统提示。事实在这里，措辞在 playbook。
        nth：这是这个会话 10 分钟内第几次报警（三处都写明）。mark：把这些命中记成已报（在给主人的通知落了账本之后、
        发出去之前做；没有发送渠道时也照样记）。两条系统提示先排进账本，再给主人发。"""
        if self.store.get_topic(tid) is None:
            lb_t = self.store.get_lamb(tid) or self.store.find_lamb_by_cid(tid)
            if lb_t is not None:
                tid = lb_t["parent_topic_id"]
        t = self.store.get_topic(tid)
        title = (self._session_title(t) if t is not None else "") or tid
        label = HOST_LABEL.get(self.topic_host(t), self.topic_host(t)) if t is not None else ""
        # 同一条命令（脱敏后一样）只列一行，后面标次数；7.63：由羔羊执行的命令注明（由羔羊 <lamb_id> 执行）
        seen: dict[str, list] = {}
        for h in hits:
            seen.setdefault(h["command"], []).append(h)
        lines = []
        lamb_ids: list[str] = []
        for cmd, hs in seen.items():
            times = "、".join(dict.fromkeys(local_hhmm(h["ran_at"]) for h in hs if h["ran_at"]))
            lamb_suffix = ""
            for h in hs:
                lb = self.store.find_lamb_by_cid(h["conversation_id"]) if h["conversation_id"] else None
                if lb is not None:
                    if lb["lamb_id"] not in lamb_ids:
                        lamb_ids.append(lb["lamb_id"])
                    if f"由羔羊 {lb['lamb_id']} 执行" not in cmd:
                        lamb_suffix = f"（由羔羊 {lb['lamb_id']} 执行）"
                    break
            lines.append(
                f"- {times + ' ' if times else ''}`{cmd}`{lamb_suffix}" + (f"（{len(hs)} 次）" if len(hs) > 1 else "")
            )
        commands = "\n".join(lines)
        lamb_head = f"（由羔羊 {'、'.join(lamb_ids)} 执行）" if lamb_ids else ""
        notify = [x for x in self.cfg.audit.also_notify if x != tid and self._audit_target_ok(x)]
        cli = self.cfg.cli_command
        count = f"这是这个会话 {AUDIT_MERGE_MINUTES} 分钟内第 {nth} 次报警"
        offender_ok = t is not None and self._audit_target_ok(tid)
        # 1. 犯规的会话：每次报警都再排一条停手提示
        if offender_ok:
            why = self.playbook.render("audit_offender.md", session_title=title, topic_id=tid, commands=commands,
                                       alert_count=str(nth))
            text = f"⚠️ sheepdog 在你的会话记录里看到你{lamb_head}跑了下面的命令（命中危险命令清单；{count}）：\n{commands}"
            self.store.add_system_message(tid, "audit_offender", with_cli(text + (f"\n{why}" if why else ""), cli))
        # 2. 要另外知会的会话
        for x in notify:
            xt = self.store.get_topic(x)
            why = self.playbook.render("audit_notify.md", session_title=self._session_title(xt), topic_id=x,
                                       commands=commands, offender_title=title, offender_topic=tid,
                                       alert_count=str(nth))
            text = f"⚠️ 「{title}」（topic `{tid}`）{lamb_head}跑了下面的命令（{count}）：\n{commands}"
            self.store.add_system_message(x, "audit_notify", with_cli(text + (f"\n{why}" if why else ""), cli))
        # 3. 主人（先落通知账本、记下这些命中已报，再发；没有发送渠道时只记账）
        done = (["已提示这个会话停手"] if offender_ok else []) + ([f"已告诉 {'、'.join(notify)} 别重复操作"] if notify else [])
        sent = self._notify_owner(f"🐕 汇报 · 会话跑了危险命令 [{tid}]\n「{title}」（topic `{tid}`{'，' + label if label else ''}）"
                                  f"{lamb_head}跑了下面的命令（命中 [audit] danger_patterns；{count}）：\n{commands}"
                                  + (f"\n{'；'.join(done)}。" if done else ""),
                                  "危险命令报警", tid, before_send=mark)
        if not sent and mark is not None:
            mark()
        log.warning("危险命令报警（%d 分钟内第 %d 次）：%s 共 %d 条，已知会 %s", AUDIT_MERGE_MINUTES, nth, tid, len(hits),
                    "、".join(["主人", tid] + notify))

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
                log.warning("羊群看板自动补活出错: %s", e)
        for tid in tids:
            t = self.store.get_topic(tid)
            if t is None or t["state"] == sm.CLOSED:
                if t is not None:
                    _reset_closed_topic(self.store, tid, "会话已关闭")
                continue
            try:
                self._duty_scan(t)
                state, detail, since, notify = self._duty_state(t)
                card = self.store.get_card(tid)
                spinning = state == duty.WORKING and card is not None and \
                    (card["sig_repeat"] or 0) >= self.cfg.duty.spin_repeat
                # 这一段空闲从几点算起（自报的活「可能过期」要看会话是不是一直空闲）
                idle_since = None
                if state == duty.IDLE:
                    keep = card is not None and card["run_state"] == duty.IDLE and card["idle_since"]
                    idle_since = card["idle_since"] if keep else now_iso()
                self.store.upsert_card(tid, run_state=state, detail=detail, stuck_since=since or None,
                                       spinning=int(spinning), queued=self.store.count_pending_signals(tid),
                                       idle_since=idle_since, no_record=int(self._record_missing(t)))
                if notify:
                    self._duty_notice(self.store.get_topic(tid), state, detail, since)
                else:
                    self._duty_recovered(self.store.get_topic(tid), state)
            except Exception as e:
                log.warning("算会话 %s 的羊群看板卡片出错: %s", tid, e)

    def _duty_scan(self, t) -> None:
        """增量读会话记录（复用护栏的 _follow，用途 duty）：最近一次工具调用、最近一次动作、空转计数、主人发言、压缩点。
        卡片上的动作（在做时叫此刻动作，其他状态叫最近动作）只取工具调用的动作说明（Antigravity 的 toolSummary /
        toolAction，Codex 的命令），不读模型的思考。

        主人发言写进卡片的 human_at，投递要不要暂停也只看它（_check_human）。Antigravity 在增量读里直接认 USER_INPUT；
        命令行宿主（Codex / Claude）增量读里一出现用户消息（或者第一次看这个会话），就用宿主自己的读法（7.18 第 7 条，
        human_message_at）重新认一遍。Claude 的动作格式没核实过，只认主人发言。"""
        host = self.topic_host(t)
        if host not in (ANTIGRAVITY, "codex", "claude") or not t["conversation_id"]:
            return
        tid, cid = t["topic_id"], t["conversation_id"]
        card = self.store.get_card(tid)
        c = dict(card) if card is not None else {}
        cur = self.store.get_cursor(tid, DUTY)
        state = {"sig": c.get("last_sig"), "rep": c.get("sig_repeat") or 0,
                 "act": _parse(c.get("last_act_at") or ""), "upd": {},
                 "typed": cur is None or cur["conversation_id"] != cid}

        def iso(dt: datetime) -> str:
            return dt.astimezone().isoformat(timespec="seconds")

        def handle(off: int, d: dict) -> None:
            upd = state["upd"]
            if user_turn(host, d):
                state["typed"] = True
            if host == "claude":
                return
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
        reader = getattr(self.sinks.get(host), "human_message_at", None)
        if host != ANTIGRAVITY and state["typed"] and reader is not None and self._record_file(t) is not None:
            h = reader(cid)
            upd["human_at"] = iso(h) if h is not None else None
        if state["act"] is not None:
            upd["last_act_at"] = iso(state["act"])
        if state["sig"] != c.get("last_sig") or state["rep"] != (c.get("sig_repeat") or 0):
            upd.update(last_sig=state["sig"], sig_repeat=state["rep"])
        if upd:
            self.store.upsert_card(tid, **upd)
            if upd.get("action") and upd["action"] != c.get("action"):
                self.store.add_history(tid, "action", upd["action"], upd.get("action_at", ""))

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

        # 1. 被主人占着：主人在会话里亲自说话（卡片上的 human_at，投递看的是同一个值），或 Codex 应用开着它（写锁冲突）
        if t["state"] == sm.HUMAN_ATTACHED:
            return duty.OCCUPIED, "主人在会话里亲自说话，暂停投递", "", False
        human = self._owner_typed_at(t, card)
        if self._owner_recent(human):
            return duty.OCCUPIED, f"主人 {hm(human)} 在会话里说过话", "", False
        sink = self.sinks.get(host)
        lock = getattr(sink, "lock_streak_since", None)
        if lock is not None and t["conversation_id"] and (since := lock(t["conversation_id"])) is not None:
            return duty.OCCUPIED, f"{label} 应用开着它（{hm(since)} 起写锁冲突）", "", False
        # 7.63：心跳透传（防误报失联）：名下存在 running 且未超时的羔羊时，母羊视为 waiting，不判 stalled / lost，不向主人发失联报警
        fresh_lambs = duty.fresh_active_lambs(self.store, tid, now, self.cfg.lamb.timeout_minutes)
        if fresh_lambs:
            lb0 = fresh_lambs[0]
            return duty.WAITING, f"等待羔羊 {lb0['lamb_id']} ({lb0['task_title']})", "", False
        # 2. 失联：后台进程没了还没交结果
        if host != ANTIGRAVITY and t["state"] in (sm.FAILED, sm.ATTENTION):
            run = self.store.latest_run_for_topic(tid)
            if run is not None and run["state"] == "failed" and (run["error"] or "").startswith(NO_RESULT):
                return duty.LOST, f"后台进程没了，没交结果（{hm(_parse(run['ended_at'] or ''))}）", \
                    run["ended_at"] or "", False
        # 2、3. 看会话记录：批次送出后完全不动 → 失联；批次在跑但没有新动作 → 中断
        watched = host in RECORD_HOSTS and bool(t["conversation_id"])
        mtime = self._record_mtime(t) if watched else None
        if watched and mtime is None:
            # 找不到会话记录（会话被删了、目录被挪走；Codex 找不到 rollout）：手上有没交差的批次（口径同下面），从这一批
            # 送出起满 lost_minutes 判失联。新建的会话第一步之前可能还没有记录文件，所以不用 stall_minutes
            since, untouched = self._unanswered_since(t, last_act), True
            quiet_since = self._quiet_since(t, last_act)
            if quiet_since is not None and (since is None or quiet_since < since):
                since, untouched = quiet_since, False
            if since is not None and now - since >= timedelta(minutes=dc.lost_minutes):
                head = f"批次 {hm(since)} 送出后" if untouched else f"批次在跑，最近动作 {hm(since)} 之后"
                return duty.LOST, f"找不到会话记录（{t['conversation_id']}），{head}无从判断", since.isoformat(), True
        elif mtime is not None:
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
        # 5. 等回复：最近回执说在等；或者手上有等别人回复（登记了 watch）、等主人拍板（「需要你定」还没答）的事。
        # 只看这些事实，不看会话自报的活：自报登没登、登得对不对，都不改运行状态
        if t["state"] in (sm.BLOCKED, sm.WAITING_HUMAN):
            what = "等别人" if t["state"] == sm.BLOCKED else "等主人拍板"
            return duty.WAITING, f"最近回执：{what}" + (f"（{duty.short(t['summary'], 40)}）" if t["summary"] else ""), \
                "", False
        waits = []
        expire = self.cfg.watch.expire_minutes
        for w in self.store.open_watches():
            if w["topic_id"] != tid:
                continue
            started = _parse(w["last_reply_at"] or "") or _parse(w["started_at"] or "")
            until = f"，到 {hm(started + timedelta(minutes=expire))}" if started else ""
            waits.append(f"等 {self.store.person_name(w['person_id']) or w['person_id']} 回复（watch #{w['id']}{until}）")
        for e in open_escalations(self.store, self.cfg.escalation.open_hours, tid):
            waits.append(f"等主人拍板（{escalation_summary(e, 30)}）")
        if waits:
            return duty.WAITING, waits[0] + (f"；另有 {len(waits) - 1} 件在等" if len(waits) > 1 else ""), "", False
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
        # 总线回执超时以后照样重投（failed），不写「暂不投递」
        hold = "" if tid == BUS_TOPIC_ID else "，暂不投递"
        lines = [f"🐕 汇报 · 会话{label}了 [{BUS_TOPIC_ID}]",
                 f"「{title}」（topic `{tid}`，{host}）判为{label}：{detail}。" + action,
                 f"排着 {queued} 条信号{hold}。会话记录里一有新动作就自动恢复投递；确认处理好了也可以用 "
                 f"`sheepdog session-reset --topic {tid}` 清掉这次判定。"]
        why = self.playbook.render("duty_stuck.md", session_title=title, topic_id=tid, host=host, state=label,
                                   detail=detail, queued=str(queued))
        if why:
            lines.append(why)
        log.warning("会话 %s 判为%s：%s（%d 条信号排队）", tid, label, detail, queued)

        def mark() -> None:
            rec["sent"].append(state)
            self.store.set_meta(key, json.dumps(rec, ensure_ascii=False))

        self._notify_owner("\n".join(lines), "羊群看板通知", tid, before_send=mark)

    def _duty_record(self, tid: str) -> dict:
        try:
            rec = json.loads(self.store.get_meta(duty_notice_key(tid)) or "{}")
        except json.JSONDecodeError:
            return {}
        return rec if isinstance(rec, dict) else {}

    def _duty_notified(self, t) -> bool:
        """这一段卡住（会话还没再动、没 reset）在岗表已经私聊过主人「中断 / 失联」，还没说过「已恢复」。"""
        rec = self._duty_record(t["topic_id"])
        if not rec.get("sent") or rec.get("recovered"):
            return False
        seg = self._silence_start(t)
        return not seg or rec.get("seg") == seg

    def _duty_mark_recovered(self, tid: str) -> None:
        rec = self._duty_record(tid)
        if rec.get("sent") and not rec.get("recovered"):
            rec["recovered"] = True
            self.store.set_meta(duty_notice_key(tid), json.dumps(rec, ensure_ascii=False))

    def _duty_recovered(self, t, state: str) -> None:
        """在岗表私聊过「中断 / 失联」的这一段结束了——会话又有动作，卡片不再是中断 / 失联：补一句「已恢复」，同一段只说一次。
        进过 attention 的由 7.21 的恢复通知说（_recover 会把这里记成已说）；session-reset 结束的一段（主人自己处理的）不说。"""
        if t is None or state in duty.STUCK:
            return
        tid = t["topic_id"]
        rec = self._duty_record(tid)
        if not rec.get("sent") or rec.get("recovered"):
            return
        seg = _parse(rec.get("seg") or "")
        reset = _parse(self.store.get_meta(duty_reset_key(tid)))
        if reset is not None and (seg is None or reset > seg):
            self._duty_mark_recovered(tid)
            return
        card = self.store.get_card(tid)
        last_act = _parse(card["last_act_at"] or "") if card is not None else None
        if last_act is None or (seg is not None and last_act <= seg):
            return
        self._duty_mark_recovered(tid)
        title = self._session_title(t) or tid
        queued = self.store.count_pending_signals(tid)
        log.warning("会话 %s 又动起来了（%s），羊群看板判定解除", tid, last_act.isoformat())
        self._notify_owner(f"🐕 汇报 · 会话已恢复 [{BUS_TOPIC_ID}]\n「{title}」（topic `{tid}`）"
                           f"{display_time(last_act, self.cfg.timezone_offset)}又有动作了"
                           + (f"，排着的 {queued} 条信号接着补送。" if queued else "。"), "恢复通知", tid)

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
            # sheepdog ask --key 发的（7.22）：钥匙就是会话给的那把，和在岗表的活同一套
            sent = self.store.send_by_message(e["message_id"])
            key = (sent["key"] if sent is not None and sent["key"] else
                   duty.key_for(e["text"] or "", pats, f"ask-{e['message_id'][-8:]}"))
            duty.auto_work(self.store, e["topic_id"], key, f"escalation:{e['message_id']}", duty.SRC_ESCALATION,
                           duty.WAITING_OWNER, title=escalation_summary(e, 40), waiting_on=duty.OWNER_NAME_FIELD)
        hours = self.cfg.escalation.open_hours
        live = {e["message_id"] for e in open_escalations(self.store, hours)}

        def close_text(e) -> str:
            """提问是怎么关的：主人已答 / 会话已结案：依据 / 已过期（N 小时没人答）。"""
            if e is None:
                return "「需要你定」记录已清理"
            if e["close_reason"] == "resolved":
                return f"会话已结案：{duty.short(e['resolve_note'] or '', 40)}"
            if e["close_reason"] == "answered" or e["answered_at"]:
                return "主人已答"
            return f"已过期（{hours:g} 小时没人答）"

        def escalation_closed(ref: str) -> str | None:
            mid = ref.removeprefix("escalation:")
            return None if mid in live else close_text(self.store.get_escalation(mid))

        # 自动补的（会话没申报过）：提问一关就收掉，三条路都走这里
        duty.close_auto_work(self.store, duty.SRC_ESCALATION, escalation_closed)

        def waited_on(w):
            """会话自报「等主人」的活等的是哪几条提问：自动补出来的那条（source_ref），加上同一会话里带同一把钥匙发的
            （sheepdog ask --key）。还有开着的返回 None；都关了返回最晚关的那条的 (说明, 关闭时间)。"""
            mids = set()
            if (w["source_ref"] or "").startswith("escalation:"):
                mids.add(w["source_ref"].removeprefix("escalation:"))
            mids |= {r[0] for r in self.store.conn.execute(
                "SELECT message_id FROM sends WHERE kind='ask' AND topic_id=? AND norm_key=?", (w["topic_id"], w["norm_key"]))}
            if not mids or mids & live:
                return None
            latest = None
            for mid in mids:
                e = self.store.get_escalation(mid)
                if e is None:
                    continue
                at = _parse(e["closed_at"] or "") or ((_parse(e["asked_at"] or "") or _now()) + timedelta(hours=hours))
                if latest is None or at > latest[1]:
                    latest = (close_text(e), at)
            return latest

        # 会话自报过的：活不替会话结束，但它等的提问都关了、又是关之前报的，就不再算等主人（改成进行中）
        for wid in duty.release_owner_waits(self.store, waited_on):
            log.info("羊群看板：活 #%d 等的「需要你定」已关，不再算等主人", wid)
        for wid in duty.reconcile_lamb_work(self.store):
            log.info("羊群看板：活 #%d 对应的羔羊已结束，自动收口为 done", wid)

    def _receipt_duty(self, topic_id: str, batch_id: str, receipt: dict) -> None:
        """回执进在岗表：work 字段同 work set；回执摘要记进履历。"""
        try:
            duty.apply_receipt_work(self.store, topic_id, receipt, log)
            summary = receipt.get("summary") if isinstance(receipt, dict) else ""
            self.store.add_history(topic_id, "receipt",
                                   f"{batch_id} {receipt.get('status', 'handled')}：{duty.short(summary or '', 80)}")
        except Exception as e:
            log.warning("回执 %s/%s 写进羊群看板出错: %s", topic_id, batch_id, e)

    def _collision_notes(self, t, rows) -> dict[str, list[str]]:
        """第 3 步撞车检查（只提醒不拦，送给谁不变）。以路由事实为主：消息里的编号，最近 routed_active_hours 小时内
        送到过别的会话（还没转交、没自报做完）；别的会话手上还开着的活（自动补的、会话自报的）也算，措辞写明来源。"""
        pats, now = self.cfg.duty.patterns, _now()
        hours = self.cfg.duty.routed_active_hours
        out: dict[str, list[str]] = {}
        for r in rows:
            if r["chat_type"] == SYSTEM_CHAT:
                continue
            lines: list[str] = []
            for key in duty.extract_keys(r["content"] or "", pats):
                owners: dict[str, dict] = {}
                for rt in duty.route_holders(self.store, key, t["topic_id"], now, hours):
                    owners.setdefault(rt["topic_id"], {"route": None, "work": []})["route"] = rt
                for w in duty.holders(self.store, key, t["topic_id"]):
                    owners.setdefault(w["topic_id"], {"route": None, "work": []})["work"].append(w)
                for tid, info in owners.items():
                    lines.append(self._collision_line(t, key, tid, info["route"], info["work"], now))
            if lines:
                out[r["message_id"]] = lines
        return out

    def _collision_line(self, t, key: str, owner_tid: str, route, works: list, now: datetime) -> str:
        """一条撞车提醒：先写事实（这个编号的消息最近送到了谁、几次、它现在什么状态），再写它手上的活和来源
        （自动补的 / 会话自报的；自报的可能过期也写上）。"""
        owner = self.store.get_topic(owner_tid)
        title = (self._session_title(owner) if owner is not None else "") or owner_tid
        card = self.store.get_card(owner_tid)
        run = duty.RUN_LABEL.get(card["run_state"], "") if card is not None and card["run_state"] else ""
        ago = duty.ago_text(card["action_at"] or "", now) if card is not None else ""
        action = "，" + phrase if (phrase := duty.last_action_phrase(card, now)) else ""
        now_text = (f"它现在{run}" if run else "") + (action if run else action.lstrip("，"))
        if route is not None:
            fact = (f"🔀 撞车提醒：{key} 的消息最近送到了「{title}」（topic `{owner_tid}`）：最近一次 "
                    f"{duty.ago_text(route['last_at'], now) or route['last_at']}，共 {route['count']} 条")
            basis = "路由"
        else:
            fact = f"🔀 撞车提醒：{key} 在「{title}」（topic `{owner_tid}`）手上"
            basis = "会话自报" if all(w["declared"] for w in works) else "自动"
        pieces = []
        for w in works:
            src = duty.SOURCE_LABEL[duty.DECLARED] if w["declared"] else duty.SOURCE_LABEL.get(w["source"], w["source"])
            p = f"{src}：{duty.WORK_LABEL.get(w['state'], w['state'])}"
            extra = []
            if w["waiting_on"]:
                extra.append(f"等 {w['waiting_on']}")
            upd = (w["declared_at"] if w["declared"] and w["declared_at"] else "") or w["updated_at"]
            if upd:
                extra.append(f"更新于 {duty.ago_text(upd, now)}")
            if duty.is_stale(w, card, now, self.cfg.duty.stale_hours):
                extra.append(duty.stale_label(self.cfg.duty.stale_hours))
            pieces.append(p + (f"（{'，'.join(extra)}）" if extra else ""))
        if pieces:
            fact += "；" + "；".join(pieces)
        fact += (f"；{now_text}" if now_text else "") + "。"
        work_state = duty.WORK_LABEL.get(works[0]["state"], works[0]["state"]) if works else "消息最近送到它那里"
        why = self.playbook.render("duty_collision.md", key=key, owner_title=title, owner_topic=owner_tid,
                                   work_state=work_state, run_state=run, basis=basis,
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
        # 给主人的通知：上一轮（或重启前）没发出去的，到了时间再发一次
        try:
            self._retry_owner_notices()
        except Exception as e:  # 重试出错不能挡住投递
            log.warning("重试给主人的通知出错: %s", e)
        try:  # 7.47 第 5 条：先核以前标了 uncertain 的（这一轮刚标的下一轮再核）。出错不挡投递
            self._recheck_uncertain()
        except Exception as e:
            log.warning("核对 uncertain 批次出错: %s", e)
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
        try:  # 7.63：羔羊发现、超时与完工汇报回流
            self._tick_lambs()
        except Exception as e:
            log.warning("羔羊状态检查出错: %s", e)
        self._expire_escalations()
        try:  # 7.36：开着的「需要你定」补来源、判可能已处理、排核实 / 快到期提醒。出错不挡投递
            from . import pending as pend
            pend.followup_round(self.cfg, self.store, self.playbook)   # [escalation] followup 关掉时只补来源
        except Exception as e:
            log.warning("「需要你定」跟进检查出错: %s", e)
        # 在岗表（7.20）：先自动补活（第 5 步），再算各会话的运行状态（第 4 步，等回复要看名下的活）
        self._duty_round(tids)
        # 护栏（7.19）：增量读会话记录，压缩了就清指纹（这一轮的批次就带上完整规则）；危险命令报警（系统提示这一轮就排上）
        for step in (self._scan_compactions, self._audit_commands):
            try:
                step()
            except Exception as e:  # 护栏出错不能挡住投递
                log.warning("护栏检查出错（%s）: %s", step.__name__, e)
        try:  # 7.57：定时巡检
            self._enqueue_patrols()
        except Exception as e:
            log.warning("定时巡检排入出错: %s", e)

        rows, groups = self._pending_groups(tids)

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
        if result["topics"]:
            self._duty_round(list(result["topics"]), autofill=False)
        # 话题群（7.51）：发提问、发通知、投递在前，改卡排在后面（一轮最多对账一次）。出错不挡投递
        try:
            from . import topicgroup as tg
            res = tg.dispatcher_round(self)
            if res:
                result["topic_group"] = res
        except Exception as e:
            log.warning("话题群这一轮出错: %s", e)
        try:  # 7.52 回音账本：投递以后推进、按环节报（含 7.47 第 6 条插话没回音）。出错不挡投递
            self._echo_round()
        except Exception as e:
            log.warning("回音账本出错: %s", e)
        result["state"] = self.store.get_topic(BUS_TOPIC_ID)["state"]
        return result

    def _enqueue_patrols(self) -> int:
        """7.57 定时巡检：给配置了 patrol_every_minutes > 0 的名册 managed 会话在空闲且无待推信号时排一条巡检系统消息。"""
        if not self.roster:
            return 0
        now = _now()
        tz, _ = _tz(self.cfg.timezone_offset)
        local_now = now.astimezone(tz)
        enqueued = 0
        for s in self.roster.managed:
            if s.patrol_every_minutes <= 0:
                continue
            if local_now.hour not in parse_patrol_hours(s.patrol_hours):
                continue
            tid = s.topic_id
            t = self.store.get_topic(tid)
            if t is None or t["state"] != sm.ACTIVE or not t["conversation_id"] or not t["onboarded_at"]:
                continue
            if t["pending_batch_id"]:
                continue
            card = self.store.get_card(tid)
            if card is not None and card["run_state"] in (duty.OCCUPIED, *duty.STUCK, duty.WORKING):
                continue
            if self._owner_recent(self._owner_typed_at(t, card)):
                continue
            if self._busy(t):
                continue
            if self.store.count_pending_signals(tid) > 0:
                continue
            meta_key = f"patrol_last:{tid}"
            last_ts = _parse(self.store.get_meta(meta_key))
            if last_ts is not None and (now - last_ts) < timedelta(minutes=s.patrol_every_minutes):
                continue
            text = (s.patrol_prompt
                    or self.playbook.render("patrol.md", session_title=s.display_title, topic_id=tid, duty=s.duty)
                    or DEFAULT_PATROL_PROMPT)
            self.store.add_system_message(tid, "patrol", text, tags=["patrol"])
            self.store.set_meta(meta_key, now.isoformat(timespec="seconds"))
            enqueued += 1
            log.info("topic %s: 已排入例行巡检消息（间隔 %d 分钟）", tid, s.patrol_every_minutes)
        return enqueued

    def _live_tids(self) -> list[str]:
        return [BUS_TOPIC_ID] + [t["topic_id"] for t in self.store.list_topics()
                                 if t["kind"] in MANAGED_KINDS and t["state"] != sm.CLOSED]

    def _pending_groups(self, tids: list[str]) -> tuple[list, dict[str, list]]:
        """待推消息按会话分组：归属的会话不能投了退回总线；被安全规则 hold 又没放行的只能给总线。"""
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
        return rows, groups

    def deliver_only(self, *, check_receipts: bool = True) -> dict:
        """7.45 第三节 / 7.86 REG-050：小轮次投递；check_receipts=True 时先对 RUNNING 会话检查已完成回执。"""
        self._record_cache = {}
        tids = self._live_tids()
        if check_receipts:
            for tid in tids:
                if (t_r := self.store.get_topic(tid)) is not None and t_r["state"] == sm.RUNNING:
                    self._check_receipt(t_r)
        sending = {d["topic_id"] for d in self.store.dispatches_in_state("sending")}
        rows, groups = self._pending_groups(tids)
        result: dict = {"pending": len(rows), "sent": 0, "topics": {}}
        for tid in tids:
            if tid not in groups or tid in sending:
                continue
            t = self.store.get_topic(tid)
            if t is None:
                continue   # 总线第一轮还没建：留给完整轮次
            self._check_human(t)
            if self.store.get_topic(tid)["state"] != t["state"]:
                self._duty_round([tid], autofill=False)
            r = self._dispatch_topic(self.store.get_topic(tid), groups[tid])
            result["topics"][tid] = r
            result["sent"] += r["sent"]
        if result["topics"]:
            self._duty_round(list(result["topics"]), autofill=False)
        return result

    # ---------- 总线新开会话（7.10 / 7.58） ----------
    def new_session(self, key: str, title: str, duty: str, chats: list[tuple[str, bool]],
                    message_ids: list[str] | None = None, note: str = "", validate_only: bool = False,
                    host: str = "", model: str = "", effort: str = "", quote: str = "",
                    lifecycle: str | None = None) -> dict:
        """总线临时新开（或原地重开已收掉的）一个专属会话：只写账本（kind = dynamic），不写名册。

        先做全部校验（key、宿主、主人原话、聊天归属、配额、待转消息），通过后才新建会话；建好后消息与 note 按 forward
        规则排队。宿主是 codex / claude 且 [hosts.*] ask_owner_before_new = true 时，必须带核对过的主人原话 quote
        （7.18，19:0x），原话写进新会话的开场。
        7.58：账本里同 key 的 dynamic 会话若已 closed，原地重开并复用原 conversation_id（不新起窗口、不扣每日新开配额），
        未显式传 lifecycle 时升格为 standing。
        """
        key, title, duty, note = key.strip(), title.strip(), duty.strip(), (note or "").strip()
        host, model, effort = (host or ANTIGRAVITY).strip(), (model or "").strip(), (effort or "").strip()
        if not _KEY_RE.match(key) or key == "bus":
            raise ValueError("--key 只允许 [a-z0-9_-]，且不能是 bus")
        resolved_lifecycle = infer_topic_lifecycle(title, lifecycle)
        # 宿主没开直接报错，说清是哪个（7.18）；额度 max_new_sessions_per_day 所有宿主共用
        self.cfg.check_host(host)
        owner_quote = self._owner_consent(host, quote)
        topic_id = "tp_" + key
        if self.roster.by_key(key) is not None:
            raise ValueError(f"key {key!r} 已在名册里")
        existing = self.store.get_topic(topic_id)
        if existing is not None:
            if existing["kind"] != DYNAMIC:
                raise ValueError(f"{topic_id} 已存在（state={existing['state']}），不能覆盖名册会话")
            if existing["state"] != sm.CLOSED:
                raise ValueError(
                    f"{topic_id} 已存在且开着（state={existing['state']}），"
                    + with_cli(f"请直接用 `sheepdog forward --topic {topic_id}` 转交", self.cfg.cli_command)
                )
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
        if existing is None:
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
        reopen_lifecycle = (infer_topic_lifecycle(title, lifecycle) if lifecycle else STANDING)
        if validate_only:
            return {"topic_id": topic_id, "reopened": existing is not None,
                    "lifecycle": reopen_lifecycle if existing is not None else resolved_lifecycle}

        full_title = f"{self.cfg.session.title_prefix} {title}".strip()
        if existing is not None:
            cid = existing["conversation_id"]
            host = self.topic_host(existing)
            self.store.update_topic(
                topic_id, title=title, duty=duty, state=sm.ACTIVE, lifecycle=reopen_lifecycle,
                pending_batch_id=None, retries=0, origin_note=note or existing["origin_note"],
                anchors_json=json.dumps([chat_anchor(c, al) for c, al in chats], ensure_ascii=False),
            )
            self.store.set_meta("roster_notify_bus", "1")
            if ids or note:
                forward_messages(self.store, topic_id, ids, note=note, self_open_id=self.cfg.self_open_id,
                                 escalation_open_hours=self.cfg.escalation.open_hours,
                                 key_patterns=self.cfg.duty.patterns, cli=self.cfg.cli_command)
            log.info("已重开动态会话 %s (%s) -> %s [lifecycle=%s]", topic_id, title, cid, reopen_lifecycle)
            return {"topic_id": topic_id, "conversation_id": cid, "title": full_title, "queued": len(ids),
                    "host": host, "reopened": True, "lifecycle": reopen_lifecycle}

        s = RosterSession(key=key, mode=MANAGED, title=title, duty=duty,
                          chats=[RosterChat(cid, "", al) for cid, al in chats], host=host, model=model, effort=effort)
        prompt = adopted_bootstrap(self.playbook, s, full_title, "", self.cfg.watch.remind_minutes,
                                   self.cfg.watch.expire_minutes, owner_quote=owner_quote)
        cid = self._new_conversation(host, topic_id, full_title, prompt, model, effort)
        ts = now_iso()
        self.store.create_topic(topic_id, title, DYNAMIC, duty, sm.ACTIVE, host, model, effort,
                                lifecycle=resolved_lifecycle)
        self.store.update_topic(topic_id, conversation_id=cid, spawned_at=ts, onboarded_at=ts, origin_note=note or None,
                                anchors_json=json.dumps([chat_anchor(c, al) for c, al in chats], ensure_ascii=False))
        self.store.update_topic(topic_id, rules_hash=self.rules_hash(self.store.get_topic(topic_id)), compacted_at=None)
        self.store.set_meta("roster_notify_bus", "1")
        if ids or note:
            forward_messages(self.store, topic_id, ids, note=note, self_open_id=self.cfg.self_open_id,
                             escalation_open_hours=self.cfg.escalation.open_hours,
                             key_patterns=self.cfg.duty.patterns, cli=self.cfg.cli_command)
        log.info("总线新开会话 %s（%s）-> %s（%d 条消息排队，lifecycle=%s）", topic_id, host, cid, len(ids), resolved_lifecycle)
        return {"topic_id": topic_id, "conversation_id": cid, "title": full_title, "queued": len(ids), "host": host,
                "reopened": False, "lifecycle": resolved_lifecycle}

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
        from . import decision_hub
        cutoff = _now() - timedelta(hours=self.cfg.escalation.open_hours)
        for e in self.store.open_escalations():
            if (asked := _parse(e["asked_at"])) is not None and asked.tzinfo is not None and asked < cutoff:
                self.store.expire_escalation(e["message_id"]); log.info("「需要你定」%s（%s）超时未答，已关闭", e["message_id"], e["topic_id"])
                if decision_hub.hub_active(self.cfg, self.store):
                    decision_hub.sync_escalation_dual_cards(self.cfg, self.store, self.source, e["message_id"], chosen_display="已超时关闭", topic_id=e["topic_id"] or "", orig_text=e["text"] or "")

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
            try:
                self._duty_scan(t)  # 主人说没说话看卡片上的 human_at：先扫一遍会话记录
            except Exception as e:
                log.warning("扫会话 %s 的记录出错: %s", s.topic_id, e)
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

    def upgrade(self, *, reply_to: str = "", command: str = "", no_pull: bool = False,
                channel: str = "stable", rollback: bool = False, target_sha: str = "",
                from_sha: str = "", from_display: str = "", notes: str = "",
                repo_dir: Path | None = None) -> dict:
        """7.69 / 7.75 / 7.93：执行常驻进程热升级或一键回滚动作——委托给 version_watch.execute_upgrade。"""
        from . import version_watch
        if channel == "stable" and not rollback:
            act = next((json.loads(a["args_json"] or "{}") for a in self.store.pending_actions() if a["kind"] == "upgrade"), {})
            channel = (version_watch.parse_upgrade_request(command) if command else None) or (
                act.get("channel") if act.get("channel") in ("stable", "beta", "rollback") else "stable"
            )
            rollback = bool(rollback or channel == "rollback" or act.get("rollback"))
            target_sha = target_sha or str(act.get("target_sha") or "")
            from_sha = from_sha or str(act.get("from_sha") or "")
            from_display = from_display or str(act.get("from_display") or "")
            notes = notes or str(act.get("notes") or "")
        return version_watch.execute_upgrade(
            self, reply_to=reply_to, command=command, no_pull=no_pull, channel=channel,
            rollback=rollback, target_sha=target_sha, from_sha=from_sha, from_display=from_display,
            notes=notes, repo_dir=repo_dir,
        )


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


# 已经送到某个会话的消息（7.47 第 1 条）：forward 到同一会话时不重发
DELIVERED_STATES = ("delivered", "acked")


def delivered_to(store: Store, row) -> tuple[str, str, str] | None:
    """这条消息已经随哪一批送到了哪个会话：(topic_id, 批次号, 送出时间 sent_at)；没送到（待推 / uncertain / missed /
    failed / context）或认不出批次返回 None。"""
    if row is None or not row["batch_id"] or not row["topic_id"] or row["dispatch_state"] not in DELIVERED_STATES:
        return None
    d = store.get_dispatch(row["batch_id"])
    return row["topic_id"], row["batch_id"], (d["sent_at"] if d is not None else "")


def delivered_text(topic_id: str, batch_id: str, sent_at: str, offset: str = "+08:00") -> str:
    """「已在 HH:MM 随批次 `…` 送到 topic `X`」（时间按配置的时区；不知道送出时间就不写）。"""
    at = _parse(sent_at or "")
    when = f"已在 {clock(at, offset)} " if at is not None and at.tzinfo is not None else "已"
    return f"{when}随批次 `{batch_id}` 送到 topic `{topic_id}`"


class ForwardResult(list):
    """forward_messages 的返回：本身是这次真正转交（回到待推）的消息编号列表；skipped 是已经送到目标会话、这次不重发的
    [(message_id, 批次号, 送出时间 sent_at)]（7.47 第 1 条）。"""

    def __init__(self, ids=(), skipped=()):
        super().__init__(ids)
        self.skipped: list[tuple[str, str, str]] = list(skipped)


def _batch_unverified_in_transcript(store: Store, topic_id: str, batch_id: str) -> bool:
    """7.50#6：批次记了 sent/delivered，但 Antigravity 会话记录里找不到这个批次号（投递黑洞）。"""
    if not topic_id or not batch_id:
        return False
    t = store.get_topic(topic_id)
    if t is None or Dispatcher.topic_host(t) != ANTIGRAVITY or not t["conversation_id"]:
        return False
    return not transcript_contains(t["conversation_id"], batch_id)


def forward_messages(store: Store, topic_id: str, message_ids: list[str], note: str = "", quote: str = "",
                     verify_quote=None, self_open_id: str = "", escalation_open_hours: float = 24,
                     key_patterns: list | None = None, cli: str = "sheepdog", answers: str = "",
                     roster: Roster | None = None) -> ForwardResult:
    """把消息 / 主人原话转交给 managed 会话（规格第 6 节、7.3、7.7）。只改归属并回到待推，真正投递由 Dispatcher
    下一轮完成，因此同样受人类接管、onboarding 约束。

    - 主人本人发的消息（sender_id == self_open_id）带「已核对」标注（群里 @ 机器人的指令照旧只标 🗣）。不再顺手关目标会话
      最近一条未结的「需要你定」（7.53：那是猜）；只有写明 answers（提问的 om_）才关那一条：它必须是目标会话名下、还开着的。
    - quote 必须通过 verify_quote 核对（没给核对器一律拒绝），投递时标成已核对的主人原话；note 不核对，标成总线备注。
    - 被安全规则 hold 的消息只能带已核对的 quote 转交。
    - 目标是总线、known、已移除或不存在时抛 ValueError。
    - 在岗表（7.20）：转出去的消息记成转出方名下「已转交」的活（钥匙按 [duty] key_patterns 从正文认）。
    - 已经随批次送到目标会话的（topic 就是目标、有批次号、delivered / acked，7.47 第 1 条）跳过：不改回待推、不关「需要
      你定」、不记看板转交，记进返回值的 skipped；uncertain / missed / failed / context 照旧可以转。quote / note 不受影响。
      7.50#6：若状态为 delivered 但 Antigravity 会话记录里找不到该批次号，允许重投。
    """
    from . import outbound
    if msg := outbound.lamb_violation_message(store, "forward", topic_id=topic_id):
        raise ValueError(msg)
    _check_target(store, topic_id, "转交")
    ids = list(dict.fromkeys(i.strip() for i in message_ids if i.strip()))
    quote, note = (quote or "").strip(), (note or "").strip()
    if not ids and not quote and not note:
        raise ValueError("至少要有 --message-ids、--quote、--note 之一")
    rows = {i: store.get_message(i) for i in ids}
    missing = [i for i, r in rows.items() if r is None]
    if missing:
        raise ValueError(f"账本里没有这些消息: {missing}")
    if "#" not in topic_id and not (answers or "").strip():
        first_row = next(iter(rows.values()), None)
        topic_id = route_within_group(
            store, roster, topic_id, chat_id=(first_row["chat_id"] or "") if first_row is not None else "",
            root_id=(first_row["root_id"] or "") if first_row is not None and "root_id" in first_row.keys() else "",
            reply_to_msg_id=(first_row["reply_to"] or "") if first_row is not None and "reply_to" in first_row.keys() else "",
            thread_id=(first_row["thread_id"] or "") if first_row is not None and "thread_id" in first_row.keys() else "",
            sender_id=(first_row["sender_id"] or "") if first_row is not None else "")
    skipped = []
    for i in list(rows):
        if (got := delivered_to(store, rows[i])) is not None and got[0] == topic_id:
            if rows[i]["dispatch_state"] == "delivered" and _batch_unverified_in_transcript(store, topic_id, got[1]):
                continue
            skipped.append((i, got[1], got[2])); del rows[i]
    target_q = None
    answers = (answers or "").strip()
    if answers:
        target_q = store.get_escalation(answers)
        if target_q is None:
            raise ValueError(f"--answers {answers} 不是登记过的「需要你定」（要给提问那条的 om_ 消息号）")
        if target_q["topic_id"] != topic_id:
            raise ValueError(f"--answers {answers} 是 {target_q['topic_id']} 的提问，不是 {topic_id} 的：转给提问的那个会话")
        if answers not in {e["message_id"] for e in open_escalations(store, escalation_open_hours, topic_id)}:
            why = "已经答过" if target_q["answered_at"] else "已经结案" if target_q["closed_at"] else "已经过期"
            raise ValueError(f"--answers {answers} 这条提问{why}，不用再关（要转交消息就去掉 --answers）")
    held = [i for i, r in rows.items() if r["security_action"] == HOLD]
    if held and not quote:
        raise ValueError(f"消息 {held} 被安全规则拦截，只能凭已核对的主人原话转交：加 --quote \"<主人原话>\"")
    if quote and not (verify_quote and verify_quote(quote)):
        raise ValueError(QUOTE_NOT_FOUND)
    src_batches: set[tuple[str, str]] = set()
    for i, row in rows.items():
        src, old_bid = row["topic_id"] or BUS_TOPIC_ID, row["batch_id"] or ""
        if old_bid: src_batches.add((src, old_bid))
        tags = [x for x in json.loads(row["tags_json"] or "[]") if not x.startswith(("owner:", "forwarded_from:"))]
        tags += [f"owner:{topic_id.removeprefix('tp_')}", f"forwarded_from:{src}"]
        if i in held and SECURITY_RELEASE_TAG not in tags: tags.append(SECURITY_RELEASE_TAG)
        if self_open_id and row["sender_id"] == self_open_id and OWNER_INSTRUCTION not in tags and OWNER_VERIFIED_TAG not in tags:
            tags.append(OWNER_VERIFIED_TAG)  # 主人本人的消息（7.8）：带已核对标注；群里 @ 机器人的指令仍只标 🗣；只认 --answers（7.53）
        if target_q is not None and (esc_tag := f"{ESCALATION_TAG_PREFIX}{answers}") not in tags: tags.append(esc_tag)
        if target_q is not None and self_open_id and row["sender_id"] == self_open_id and store.item_by_root(i) is None:
            store.set_note(i, escalation_note(target_q))   # 7.51：话题群事项的开头那条保留话题群的说明
        store.forward_message(i, topic_id, tags)
        # 7.45：转交的是群话题里的消息（或开头那条），话题原来归转出方的，改归接手的会话（之后的话题回复直接送它）
        if store.repoint_group_thread(row["thread_id"] or "", row["message_id"], {src}, topic_id):
            log.info("话题 %s 跟着转交改归 %s", row["thread_id"] or row["message_id"], topic_id)
    for src_tid, old_bid in src_batches:
        src_t = store.get_topic(src_tid)
        if src_t is not None and src_t["pending_batch_id"] == old_bid:
            rem = store.conn.execute("SELECT COUNT(*) FROM messages WHERE topic_id=? AND batch_id=? AND dispatch_state IN ('delivered','sending')", (src_tid, old_bid)).fetchone()[0]
            if rem == 0:
                store.update_topic(src_tid, state=sm.ACTIVE if src_t["state"] == sm.RUNNING else src_t["state"], pending_batch_id=None)
                store.set_meta(duty_reset_key(src_tid), now_iso())
                if (old_d := store.get_dispatch(old_bid)) is not None and old_d["state"] == "sent":
                    store.set_dispatch_state(old_bid, "redelivered" if src_tid == topic_id else "acked", error="forward redeliver" if src_tid == topic_id else f"forwarded:{topic_id}")
    pats = key_patterns if key_patterns is not None else DutyConfig().patterns
    by_src: dict[str, list] = {}
    for i, row in rows.items(): by_src.setdefault(row["topic_id"] or BUS_TOPIC_ID, []).append(row)
    for src, rs in by_src.items():
        if src != topic_id:
            try: duty.forward_work(store, src, topic_id, rs, pats)
            except Exception as e: log.warning("转交记进羊群看板出错: %s", e)  # 在岗表出错不挡转交
    try:  # 7.51：转交的是话题群里某件事项的消息：负责会话跟着改、编号带过去、话题里说一句「已交给 …」。出错不挡转交
        from . import topicgroup as tg
        tg.on_forward(store, topic_id, list(rows.values()), cli)
    except Exception as e: log.warning("转交记进话题群事项出错: %s", e)
    done, ids = [i for i, _, _ in skipped], [i for i in ids if i in rows]
    if target_q is not None:
        # 7.53：总线写明了这是在回答哪一条：关那一条（答复记成转交的第一条消息；只带 --quote / --note 时记 forward）
        by = next((i for i in [*ids, *done] if self_open_id and (store.get_message(i) or {"sender_id": ""})["sender_id"] == self_open_id), (ids or done or ["forward"])[0])
        store.answer_escalation(answers, by)
        log.info("转交时写明回答提问 %s（%s），已关", answers, topic_id)
    if quote or note:
        extra = (f"（随附转交的消息：{', '.join(ids)}）" if ids else "") + (f"（已经送到过你这里、这次不重发：{', '.join(done)}）" if done else "")
        store.add_system_message(topic_id, "bus_relay", relay_content(quote, note) + ("\n" + extra if extra else ""))
    log.info("转交 %d 条消息%s -> %s%s", len(ids), "（含转达）" if quote or note else "", topic_id, f"；{len(done)} 条已送到过、不重发" if done else "")
    touch_wakeup(store)
    return ForwardResult(ids, skipped)


def touch_wakeup(target) -> Path | None:
    sd = getattr(target, "state_dir", None) or Path(getattr(target, "db_path", None) or getattr(target, "path", "") or "").parent
    if not sd or str(sd) == ".": return None
    p = Path(sd) / ".wakeup"
    try: p.parent.mkdir(parents=True, exist_ok=True); p.touch(); return p
    except OSError: return None


def _auto_close_topic_subagents(store: Store, topic_id: str, summary: str = "随母羊本轮回执自动收口", *, force: bool = False) -> None:
    for lb in store.get_active_lambs(topic_id):
        if lb["kind"] in ("subagent", "native_subagent"):
            if not force:
                w = store.get_work(topic_id, duty.norm_key(f"lamb:{lb['lamb_id']}"))
                if w is not None and not w["closed_at"] and w["state"] == duty.WAITING_OTHER:
                    continue
            complete_lamb(store, lb["lamb_id"], summary=summary, state="done", report_msg_id="parent_receipt_auto_close")


def _reset_closed_topic(store: Store, topic_id: str, why: str = "会话已关闭") -> None:
    _auto_close_topic_subagents(store, topic_id, summary=why, force=True)
    if (card := store.get_card(topic_id)) is not None and (card["run_state"] != duty.IDLE or card["queued"] or card["spinning"]):
        store.upsert_card(topic_id, run_state=duty.IDLE, detail="", stuck_since=None, spinning=0, queued=0, idle_since=now_iso())
    if topic_id not in (BUS_TOPIC_ID, "tp_release"):
        from . import topicgroup as tg
        for w in store.open_work(topic_id): store.update_work(w["id"], state=duty.DONE, result=why, closed_at=now_iso())
        for item in store.list_items(("active",)):
            if item["owner_topic"] == topic_id: tg.close(store, item, "system", why)


def close_session(store: Store, topic_id: str) -> None:
    """手动收掉总线新开的会话（7.10）。名册里的会话、known、总线、前任都不能这样关。"""
    t = store.get_topic(topic_id)
    if t is None: raise ValueError(f"topic {topic_id} 不存在")
    if t["kind"] != DYNAMIC: raise ValueError(f"{topic_id} 不是总线新开的会话（kind={t['kind']}），不能用 close-session 关")
    if t["state"] == sm.CLOSED: raise ValueError(f"{topic_id} 已经收掉了")
    store.update_topic(topic_id, state=sm.CLOSED, pending_batch_id=None)
    _reset_closed_topic(store, topic_id, "会话已手动关闭")
    store.set_meta("roster_notify_bus", "1")
    log.info("总线新开的会话 %s 已手动收掉，聊天归属释放", topic_id)


def add_watch(store: Store, topic_id: str, person_id: str, chat_id: str = "", note: str = "") -> int:
    """登记一条等待（7.4）。总线和 managed 会话都可以登记。"""
    from . import outbound
    if msg := outbound.lamb_violation_message(store, "watch", topic_id=topic_id): raise ValueError(msg)
    if topic_id != BUS_TOPIC_ID: _check_target(store, topic_id, "登记等待")
    elif store.get_topic(BUS_TOPIC_ID) is None: raise ValueError("总线还没建：先 sheepdog init 或 run")
    if not person_id.strip(): raise ValueError("--person 不能为空")
    return store.add_watch(topic_id, person_id.strip(), (chat_id or "").strip(), note)


def settle_watches_after_receipt(store: Store, topic_id: str, batch_id: str, anchors: list) -> None:
    """会话回执后处理它的等待（7.16）：anchors 里 watch_done:N → 关闭（done）；
    回复随这个批次送达、但没被确认的 → 回到 waiting，从回复时间重新计时。"""
    done = {int(a.removeprefix("watch_done:")) for a in anchors if isinstance(a, str) and a.startswith("watch_done:") and a.removeprefix("watch_done:").isdigit()}
    for w in store.open_watches():
        if w["topic_id"] != topic_id: continue
        if w["id"] in done:
            store.close_watch(w["id"], "done"); log.info("等待 #%s 经回执确认已等到", w["id"])
        elif w["status"] == "replied" and w["last_reply_message_id"]:
            if (r := store.get_message(w["last_reply_message_id"])) is not None and r["batch_id"] == batch_id:
                store.reset_watch_waiting(w["id"]); log.info("等待 #%s 的回复没被确认，回到 waiting", w["id"])


def bind_echo_relay(cfg: Config, store: Store, topic_id: str, batch_id: str, receipt: dict,
                    status: str = "", summary: str | None = None) -> None:
    """回执带回绑定（write_receipt 与 Dispatcher._echo_relay 共用单一写入逻辑）。"""
    if not cfg.session.owner_receipt_relay:
        if batch_id: store.set_meta(f"owner_relay_skipped:{batch_id}", "1")
        return
    if not batch_id: return
    now = _now().isoformat(timespec="seconds")
    for e in store.echo_open():
        if e["stage"] == ECHO_UNDELIVERED or (r := store.get_message(e["message_id"])) is None: continue
        tid, st = r["topic_id"] or "", r["dispatch_state"]
        if r["route"] != DISPATCH or tid != topic_id: continue
        if tid != e["topic_id"]: store.echo_update(e["message_id"], topic_id=tid, alerted="", stage_at=now, **ECHO_UNBOUND)
        if st in ECHO_DELIVERED_STATES and (e["stage"] != ECHO_DELIVERED or e["batch_id"] != r["batch_id"]):
            d_row = store.get_dispatch(r["batch_id"]) if r["batch_id"] else None
            store.echo_update(e["message_id"], stage=ECHO_DELIVERED, stage_at=now, batch_id=r["batch_id"],
                              delivered_at=(d_row["sent_at"] if d_row is not None and d_row["sent_at"] else now),
                              priority=int(bool(r["batch_id"]) and r["batch_id"].startswith(PRIORITY_PREFIX)),
                              **(ECHO_UNBOUND if e["batch_id"] != r["batch_id"] else {}))
    eff_status = status or (receipt.get("status") if isinstance(receipt, dict) else "") or "handled"
    if summary is None: summary = receipt.get("summary") if isinstance(receipt, dict) else ""
    summary_clean = " ".join(str(summary or "").split())
    for e in store.echo_open():
        if e["stage"] == ECHO_DELIVERED and e["topic_id"] == topic_id and not e["relay_batch"]:
            via = e["batch_id"] or ""
            if via == batch_id or via.startswith(PRIORITY_PREFIX):
                store.echo_bind_relay(e["message_id"], batch_id, now, eff_status, summary_clean)


def write_receipt(cfg: Config, topic_id: str, batch_id: str, receipt: dict) -> Path:
    from . import hook, outbound
    store = Store(cfg.db_path) if cfg.db_path.exists() else None
    try:
        if msg := outbound.lamb_violation_message(store, "receipt", topic_id=topic_id): raise ValueError(msg)
        status = receipt.get("status")
        if status not in sm.RECEIPT_EVENTS:
            raise ValueError(f"status 必须是 {sorted(sm.RECEIPT_EVENTS)} 之一，收到 {status!r}")
        d = cfg.receipts_dir / topic_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{batch_id}.json"
        receipt = {**receipt, "received_at": now_iso()}
        p.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        hook.clear_stop_state(cfg.state_dir, topic_id)
        anchors = receipt.get("anchors") if isinstance(receipt.get("anchors"), list) else []
        has_lamb_anchor = any(isinstance(a, str) and a.startswith("lamb:") for a in anchors)
        if store is not None and status != "waiting":
            _auto_close_topic_subagents(store, topic_id)
        if store is not None and status in ("handled", "done"):
            im = LarkCliSource(tz=cfg.timezone_offset, max_pages=cfg.max_pages) if cfg.source.enabled else None
            clear_handled_owner_reactions(store, im, topic_id, batch_id)
        if store is not None and (status == "waiting" or has_lamb_anchor):
            t = store.get_topic(topic_id)
            if t is not None and t["pending_batch_id"] == batch_id and t["state"] == sm.RUNNING:
                eff_status = "handled" if is_standing_topic(t) and status == "done" else status
                new_state = sm.transition(t["state"], sm.RECEIPT_EVENTS.get(eff_status, "receipt_handled"))
                store.finish_dispatch(batch_id, "acked", json.dumps(receipt, ensure_ascii=False))
                store.mark_batch_state(batch_id, "acked")
                fields: dict = {"state": new_state, "pending_batch_id": None, "retries": 0, "summary": receipt.get("summary", t["summary"])}
                settle_watches_after_receipt(store, topic_id, batch_id, anchors)
                if merged_anchors := Dispatcher._receipt_anchors(t, receipt):
                    fields["anchors_json"] = merged_anchors
                store.update_topic(topic_id, **fields)
                duty.apply_receipt_work(store, topic_id, receipt, log)
                store.add_history(topic_id, "receipt", f"{batch_id} {status}：{duty.short(str(receipt.get('summary') or ''), 80)}")
                try:
                    bind_echo_relay(cfg, store, topic_id, batch_id, receipt, status=eff_status, summary=fields["summary"])
                except Exception as e:
                    log.warning("回执带回出错（%s/%s）: %s", topic_id, batch_id, e)
            elif t is not None and has_lamb_anchor:
                duty.apply_receipt_work(store, topic_id, receipt, log)
        touch_wakeup(cfg)
        return p
    finally:
        if store is not None:
            store.close()


# ======================= 羔羊生命周期（7.63） =======================
def _pid_alive(pid: int) -> bool:
    """检查后台子进程是否存活（若为当前进程的僵尸子进程先非阻塞回收，再用 os.kill(pid, 0) 探测）。"""
    if pid <= 0:
        return False
    try: os.waitpid(pid, os.WNOHANG)
    except OSError: pass
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def register_lamb(store: Store, parent_topic_id: str, task_title: str, kind: str = "subagent",
                  conversation_id: str | None = None, lamb_id: str | None = None,
                  pid: int | None = None, log_path: str | None = None) -> str:
    """7.63：登记一只羔羊（subagent 或 cli），返回 lamb_id。对同母羊下的 subagent 自动去重合并。"""
    parent_topic_id = (parent_topic_id or "").strip()
    if parent_topic_id and not parent_topic_id.startswith("tp_"):
        parent_topic_id = "tp_" + parent_topic_id
    if store.get_topic(parent_topic_id) is None:
        raise ValueError(f"母羊会话 {parent_topic_id} 不存在")
    if kind not in ("subagent", "cli"):
        raise ValueError(f"kind 必须是 subagent 或 cli，收到 {kind!r}")
    cid, ts = (conversation_id or "").strip() or None, now_iso()
    if cid and (existing := store.find_lamb_by_cid(cid)) is not None and existing["parent_topic_id"] == parent_topic_id and existing["state"] == "running":
        existing_id = existing["lamb_id"]
        title = (task_title or "").strip() or existing["task_title"] or existing_id
        store.update_lamb(existing_id, task_title=title, conversation_id=cid, last_act_at=ts)
        return existing_id
    if kind in ("subagent", "native_subagent"):
        active_empty = next((lb for lb in store.get_active_lambs(parent_topic_id)
                             if lb["kind"] in ("subagent", "native_subagent") and not (lb["conversation_id"] or "").strip()), None)
        if active_empty is not None:
            existing_id = active_empty["lamb_id"]
            title = (task_title or "").strip() or active_empty["task_title"] or existing_id
            store.update_lamb(existing_id, task_title=title, conversation_id=cid or active_empty["conversation_id"], last_act_at=ts)
            return existing_id
    lid = (lamb_id or "").strip() or f"lamb_{uuid.uuid4().hex[:8]}"
    if not lid.startswith("lamb_"):
        lid = "lamb_" + lid
    title = (task_title or "").strip() or lid
    store.create_lamb(lid, parent_topic_id, kind=kind, task_title=title, conversation_id=cid, pid=pid, state="running", log_path=log_path)
    return lid


def _summarize_cli_lamb_log(raw_text: str) -> tuple[str, bool]:
    """7.101（REG-069）：从 CLI 羔羊日志提取有意义的尾行或末尾 JSON 摘要，杜绝格式化多行 JSON 尾行退化为 '}\\n}\\n}\\n]\\n}'。"""
    if not raw_text:
        return "", False
    non_empty = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not non_empty:
        return "", False
    recent_window = "\n".join(non_empty[-30:])
    has_err = any(k in recent_window for k in ("Traceback", "Fatal", "Error", "ERROR"))
    stripped = raw_text.rstrip()
    if stripped.endswith("}"):
        idx = stripped.rfind("\n{")
        cand = stripped[idx + 1:] if idx != -1 else (stripped if stripped.startswith("{") else "")
        if cand:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    if obj.get("ok") is False or obj.get("status") in ("failed", "error") or obj.get("error"):
                        has_err = True
                    prefix_lines = [
                        ln.strip() for ln in stripped[:idx].splitlines()
                        if ln.strip() and not re.match(r"^[}\],;:\s]+$", ln.strip())
                    ][-2:] if idx != -1 else []
                    compact_parts: list[str] = []
                    for k, v in obj.items():
                        if isinstance(v, (str, int, float, bool)) or v is None:
                            compact_parts.append(f"{k}={v}")
                        elif isinstance(v, list):
                            compact_parts.append(f"{k}=[{len(v)} items]")
                        elif isinstance(v, dict):
                            compact_parts.append(f"{k}={{{len(v)} keys}}")
                    json_summary = ", ".join(compact_parts[:8]) or json.dumps(obj, ensure_ascii=False)[:200]
                    return ("\n".join([*prefix_lines, f"JSON({json_summary})"]) if prefix_lines else f"JSON({json_summary})"), has_err
            except Exception:
                pass
    meaningful = [ln for ln in non_empty if not re.match(r"^[}\],;:\s]+$", ln)]
    tail_lines = (meaningful or non_empty)[-5:]
    return "\n".join(tail_lines), has_err


def _terminate_lamb_process(lb: dict) -> bool:
    """7.101（REG-069）：终止 CLI 羔羊的整个后台进程组（os.killpg），防止 lamb done / cancel / timeout 后子进程残留为孤儿僵尸继续执行。"""
    import signal
    if not lb or lb.get("kind") not in ("cli", "bash_worker") or not lb.get("pid"):
        return False
    try:
        pid = int(lb["pid"])
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not _pid_alive(pid):
        return False
    killed = False
    try:
        pgid = os.getpgid(pid)
        if pgid > 1 and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGTERM)
            killed = True
    except (OSError, ValueError):
        pass
    if not killed:
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (OSError, ValueError):
            pass
    return killed


def complete_lamb(store: Store, lamb_id: str, summary: str = "", state: str = "done",
                  report_msg_id: str | None = None) -> dict:
    """7.63 / 7.101：羔羊完工 / 失败 / 超时收尾：若 CLI 进程组仍在运行则先行终止，更新 lambs 表、关闭母羊名下对应的 work_items (lamb:<id>)、向母羊排入 lamb_report 系统消息。"""
    lid = (lamb_id or "").strip()
    if (lb := store.get_lamb(lid)) is None:
        raise ValueError(f"羔羊 {lid} 不存在")
    if lb.get("state") == "running" and lb.get("pid"):
        _terminate_lamb_process(lb)
    final_state = state if state in ("done", "failed", "timeout") else "done"
    ts = now_iso()
    summary = (summary or "").strip() or (lb["result_summary"] or f"羔羊 {lid} 已结束（{final_state}）")
    already_reported = bool(lb["reported_at"]) or bool(report_msg_id)
    upd_fields = dict(state=final_state, finished_at=lb["finished_at"] or ts, last_act_at=ts,
                      result_summary=summary, reported_at=lb["reported_at"] or ts)
    if report_msg_id:
        upd_fields["report_msg_id"] = lb["report_msg_id"] or report_msg_id
    store.update_lamb(lid, **upd_fields)
    pt_id = lb["parent_topic_id"]
    close_why = "羔羊已由母羊销毁" if report_msg_id == "killed_by_parent" else "羔羊已完成"
    for wk_id in filter(None, {lid, (lb["conversation_id"] or "").strip()}):
        nk = duty.norm_key(f"lamb:{wk_id}")
        if (w := store.get_work(pt_id, nk)) is not None and not w["closed_at"]:
            store.update_work(w["id"], state=duty.DONE, result=summary, declared=1, declared_at=ts, closed_at=ts)
            store.close_route(pt_id, nk, close_why, ts)
            duty.record_closed(store, store.conn.execute("SELECT * FROM work_items WHERE id=?", (w["id"],)).fetchone())
    duty.reconcile_lamb_work(store)
    mid = ""
    if not already_reported:
        pt = store.get_topic(pt_id)
        if pt is not None and pt["state"] == sm.RUNNING and pt["pending_batch_id"] and not store.get_active_lambs(pt_id):
            old_bid = pt["pending_batch_id"]
            if (old_d := store.get_dispatch(old_bid)) is not None and old_d["state"] == "sent":
                store.finish_dispatch(old_bid, "acked", json.dumps({"status": "waiting", "summary": f"等待羔羊 {lid} 结束"}, ensure_ascii=False))
                store.mark_batch_state(old_bid, "acked")
            store.update_topic(pt_id, state=sm.ACTIVE, pending_batch_id=None, retries=0)
        log_hint = f"\n日志路径：`{lb['log_path']}`" if lb["log_path"] else ""
        text = (f"【系统提示 · 羔羊完工汇报】\n🐑 羔羊 `{lid}`（[{lb['kind']}] {lb['task_title']}）已结束，状态：`{final_state}`。\n"
                f"结果摘要：{summary}{log_hint}")
        mid = store.add_system_message(pt_id, "lamb_report", text, tags=[f"lamb:{lid}", f"lamb_state:{final_state}"])
    touch_wakeup(store)
    return {"lamb_id": lid, "parent_topic_id": pt_id, "state": final_state, "summary": summary, "message_id": mid}


def cancel_lamb(store: Store, lamb_id: str, reason: str = "") -> dict:
    """7.63 / 7.101：取消一只羔羊，若后台进程组仍在运行则整组终止并更新状态。"""
    lid = (lamb_id or "").strip()
    if (lb := store.get_lamb(lid)) is None:
        raise ValueError(f"羔羊 {lid} 不存在")
    _terminate_lamb_process(lb)
    ts, summary = now_iso(), (reason or "").strip() or "已手动取消"
    store.update_lamb(lid, state="failed", finished_at=lb["finished_at"] or ts, last_act_at=ts,
                      result_summary=summary, reported_at=lb["reported_at"] or ts)
    if (w := store.get_work(lb["parent_topic_id"], duty.norm_key(f"lamb:{lid}"))) is not None and not w["closed_at"]:
        store.update_work(w["id"], state=duty.DONE, result=summary, declared=1, declared_at=ts, closed_at=ts)
    touch_wakeup(store)
    return {"lamb_id": lid, "parent_topic_id": lb["parent_topic_id"], "state": "failed", "summary": summary}

