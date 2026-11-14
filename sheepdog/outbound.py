"""一事一串与发出前再核一遍（7.22）：会话找主人（ask/report，私聊或话题群，带防重编号）、代回（reply，发前检查主人发言并带代回标记）、查看话题（thread）。"""

from __future__ import annotations

import hashlib, json, logging, os, re, sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import duty, identity as ident, pending as pend, session as sm
from .ack import clear_handled_owner_reactions
from .card import build_owner_card, build_reply_card, build_start_card, card_fits, card_request_bytes, resolve_summary_and_body
from .config import Config
from .engine import (BUS_TOPIC_ID, MANAGED_KINDS, PRIORITY_PREFIX, display_time, has_reply_suffix, is_agent_reply,
                     open_escalations, order_thread_items, root_position)
from .models import Message
from .source import SourceError
from .source.lark import LarkTimeout
from .source.tat_cache import is_transient_lark_error
from .skillpack import enforce_rule_read_gate
from .store import Store, now_iso

log = logging.getLogger("sheepdog")

ASK, REPORT, REPLY = "ask", "report", "reply"

# 退出码（写进接口说明，会话按它决定下一步）
EXIT_OK = 0                 # 发出去了；或者同样的内容之前已经发过（重试），不再发
EXIT_SEND_FAILED = 1        # 飞书报错或超时：没发成 / 不确定发出去没有，用同样的参数重试（防重编号保证只发一条）
EXIT_INVALID = 2            # 参数不对、会话不能发、账本里没有要回复的那条
EXIT_OWNER_REPLIED = 3      # 发前检查：主人已经在这个聊天里说过话，没发；他的话打印出来了
EXIT_CHECK_FAILED = 4       # 发前检查本身失败（接口报错、超时），没发；不当成「主人没说话」

IDEM_MAX = 50


class OutboundError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class SendResult:
    message_id: str
    already: bool = False           # 之前已经发过（重试），这次没调飞书
    root_message_id: str = ""       # 带钥匙时所在话题的开头那条
    new_thread: bool = False        # 这条是新起的话题开头
    text: str = ""
    origin: str = ""                # ask 记下的来源是怎么来的（7.36）：about | batch；空 = 没记
    sources: list = field(default_factory=list)      # [(消息号, 聊天)]
    duplicates: list = field(default_factory=list)   # 同一来源已有别的会话在问（还开着）
    batch_chats: int = 0            # 不给 --about 时这一批候选消息来自几个聊天（7.38：多于一个就不记来源）
    item: str = ""                  # 7.51：发进了话题群里哪件事项（T-9012）；空 = 私聊
    new_item: bool = False          # 这次新开了这件事项（发了标题卡）
    reopened: bool = False          # 这件事办完 / 归档了，这次重开
    note: str = ""                  # 给会话看的一句提示（--track 没生效、群不可用退回私聊等）
    patched_in_place: bool = False  # 7.116：已在原未决 ask 卡片原地更新内容与按钮（并非防重跳过未发）


@dataclass
class OwnerSpoke:
    """发前检查查到的主人发言。"""
    messages: list[Message] = field(default_factory=list)
    since: str = ""


def idem_key(kind: str, *parts: str) -> str:
    """防重编号：sd<种类>-<指纹>，固定 44 个字符（飞书上限 50）。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    key = f"sd{kind[0]}-{digest}"
    assert len(key) <= IDEM_MAX
    return key


def target_ok(store: Store, topic_id: str) -> bool:
    """能经 sheepdog 发消息的会话：总线、名册 managed、总线新开的（没收掉的）。
    REG-166: 若 dynamic 会话已有真实宿主容器（conversation_id 非空且未退休）却因竞态处于 closed，
    当该会话自身主动调用 ask/report/resolve/reply/track 时自动重开为 active。"""
    t = store.get_topic(topic_id)
    if (
        t is not None
        and t["kind"] == "dynamic"
        and t["state"] == sm.CLOSED
        and bool((t["conversation_id"] or "").strip())
        and store.get_topic(topic_id + ".retired") is None
    ):
        store.update_topic(topic_id, state=sm.ACTIVE)
        t = store.get_topic(topic_id)
    return t is not None and t["state"] != sm.CLOSED and (topic_id == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS)


def lamb_violation_message(store: Store | None, cmd: str, *, topic_id: str = "", lamb_id: str = "", cid: str = "") -> str | None:
    """7.63 羔羊越权硬拦截：判定当前调用方是否为羔羊（环境变量 SHEEPDOG_LAMB_ID / 会话 conversation_id / lambs 表匹配）。"""
    lid = (lamb_id or os.environ.get("SHEEPDOG_LAMB_ID") or "").strip()
    conv_id = (cid or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or os.environ.get("SHEEPDOG_CONVERSATION_ID")
               or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_SESSION_ID") or "").strip()
    tid, matched_lid, parent_tid = (topic_id or "").strip(), "", ""
    if lid:
        lb = store.get_lamb(lid) if store is not None and hasattr(store, "get_lamb") else None
        matched_lid, parent_tid = (lb["lamb_id"] if lb else lid), ((lb["parent_topic_id"] if lb else "") or tid or "unknown")
    elif conv_id and store is not None and hasattr(store, "find_lamb_by_cid"):
        if (lb := store.find_lamb_by_cid(conv_id)) is not None:
            matched_lid, parent_tid = lb["lamb_id"], lb["parent_topic_id"]
    if not matched_lid and tid and store is not None:
        candidates = [tid, *([tid.removeprefix("tp_")] if tid.startswith("tp_") else [])]
        for c in list(candidates):
            if c.startswith("lamb:"):
                candidates.append(c.removeprefix("lamb:"))
        for cand in candidates:
            lb = (store.get_lamb(cand) if hasattr(store, "get_lamb") else None) or (store.find_lamb_by_cid(cand) if hasattr(store, "find_lamb_by_cid") else None)
            if lb is not None:
                matched_lid, parent_tid = lb["lamb_id"], lb["parent_topic_id"]
                break
    if not matched_lid:
        return None
    return (f"[sheepdog] 禁止越权：当前执行体是羔羊（lamb_id={matched_lid}，隶属母羊 {parent_tid}），"
            f"物理禁止直接调用 sheepdog {cmd}（reply/ask/report/forward/receipt/resolve/watch）。"
            "请将核查或执行结论直接返回给母羊，由母羊统一对外回话与交回执。")


def check_not_lamb(store: Store | None, cmd: str, *, topic_id: str = "", lamb_id: str = "", cid: str = "") -> None:
    if msg := lamb_violation_message(store, cmd, topic_id=topic_id, lamb_id=lamb_id, cid=cid):
        raise OutboundError(EXIT_INVALID, msg)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


# ======================= ask / report =======================
def current_batch(store: Store, topic_id: str, *, include_priority: bool = False) -> str:
    """会话没传 --batch 时用的批次号：支持 SessionGroup 槽位族（tp_awei / tp_awei#2）与插队批次（p…）。"""
    base = (topic_id or "").split("#", 1)[0]
    tids = list(dict.fromkeys([
        topic_id, base, *(t["topic_id"] for t in store.list_topics() if t["topic_id"].split("#", 1)[0] == base),
    ]))
    if include_priority:
        for tid in tids:
            if (bid := store.latest_batch_id(tid, skip_prefix="")) and bid.startswith(PRIORITY_PREFIX):
                return bid
    for tid in tids:
        t = store.get_topic(tid)
        if t is not None and t["pending_batch_id"] and (include_priority or not t["pending_batch_id"].startswith(PRIORITY_PREFIX)):
            return t["pending_batch_id"]
    for skip in ((PRIORITY_PREFIX, "") if not include_priority else ("",)):
        for tid in tids:
            if bid := store.latest_batch_id(tid, skip_prefix=skip):
                return bid
    return ""


def compose(cfg: Config, kind: str, topic_id: str, title: str, body: str, summary: str = "") -> str:
    if (summary or "").strip():
        _, _, body = resolve_summary_and_body(body, summary, require_summary=False)
    return cfg.threads.header(kind, _one_line(title), topic_id) + "\n" + body.strip()


def ask_sources(cfg: Config, store: Store, topic_id: str, about: list[str] | None,
                batch: str = "") -> tuple[str, list, int]:
    """ask 的来源（7.36）：给了 --about 就是它们（要在账本里）；否则是这个会话最近一批送到的消息，并且只在它们都来自
    同一个聊天时才记（7.38）。返回 (origin, 来源, 这批候选消息来自几个聊天；--about 时是 0)。"""
    if about and any((a or "").strip() for a in about):
        try:
            return pend.ORIGIN_ABOUT, pend.about_sources(cfg, store, about), 0
        except ValueError as e:
            raise OutboundError(EXIT_INVALID, str(e))
    srcs, n = pend.default_sources(cfg, store, batch or current_batch(store, topic_id))
    return pend.ORIGIN_BATCH, srcs, n


VIP_FLOOR_REFUSAL = (
    "VIP 请求默认不等主人批：合理就办，不合理给方案，坚持就 report 后配合；只有底线里的事才 ask，"
    "并用 --floor 写明碰到哪条底线。"
    "Cloud Run 环境变量/配置更新与切流生成不可变新 Revision、可秒级回退，不属于 destructive 底线；"
    "若 invoke_subagent 被宿主 pre-tool hook 拦截，请直接用 run_command 执行并事后 sheepdog report，严禁调 sheepdog ask。"
)
VIP_FLOOR_TYPES: dict[str, str] = {
    "credentials": "索要主人的密钥、凭据、个人账号",
    "destructive": "删除、覆盖、强推等不可逆操作",
    "vip_conflict": "两位 VIP 的要求真冲突",
}
VIP_NON_FLOOR_PATTERNS = (
    "脱敏", "只读", "跨系统数据导出", "生产数据库访问权限", "常规部署",
    "生产逐项", "指名授权", "pre-tool hook", "安全钩子", "宿主安全", "钩子拦截", "门禁确认",
)
_VIP_REVERSIBLE_OPS = (
    "环境变量", "update-env-vars", "update-traffic", "流量切换", "切流", "配置更新", "补齐配置", "升配", "扩缩容",
)
_VIP_TRUE_DESTRUCTIVE = (
    "删除", "删掉", "删库", "删表", "清空", "销毁", "强推",
    "delete", "destroy", "drop", "truncate", "rm -rf", "force-push", "push --force",
)
_NEGATED_CREDENTIAL_RE = re.compile(
    r"(不要|不含|不用|无需|不涉及|剔除)[^，。；\n]{0,15}(密钥|凭据|密码|个人账号|私钥|secret|token)",
    re.I,
)


def _is_valid_vip_floor(floor: str, floor_type: str | None = None) -> bool:
    if floor_type is not None and floor_type.strip() not in VIP_FLOOR_TYPES:
        return False
    clean_floor = _one_line(floor)
    if not clean_floor:
        return False
    low = clean_floor.lower()
    if any(p in low for p in VIP_NON_FLOOR_PATTERNS):
        return False
    if _NEGATED_CREDENTIAL_RE.search(clean_floor):
        return False
    if (floor_type is None or floor_type.strip() == "destructive") and any(k in low for k in _VIP_REVERSIBLE_OPS):
        if not any(k in low for k in _VIP_TRUE_DESTRUCTIVE):
            return False
    return True


def vip_sources(cfg: Config, store: Store, topic_id: str, about: list[str] | None, vips: dict | None,
                batch: str = "") -> list[tuple[str, str]]:
    """ask 关联的来源消息里，VIP 本人发的那些（7.61）：[(消息号, 名字)]。来源口径：给了 --about 就是它们；不给就是这一批送到的
    全部消息（不管来自几个聊天，和只记单聊天来源的 default_sources 不同）。判 VIP 只用 prompts.vip_of（和投递盖章同一处）。
    名册没有 VIP 时直接返回空，不碰账本。"""
    if not vips:
        return []
    from .prompts import vip_of
    if about and any((a or "").strip() for a in about):
        try:
            ids = pend.about_sources(cfg, store, about)
        except ValueError as e:
            raise OutboundError(EXIT_INVALID, str(e))
    else:
        ids = pend.batch_sources(cfg, store, batch or current_batch(store, topic_id))
    out = []
    for mid, _ in ids:
        name = vip_of(store.get_message(mid), vips)
        if name:
            out.append((mid, name))
    return out


def check_bus_not_forwarded_ask(cfg: Config, store: Store, topic_id: str, about: list[str] | None,
                                batch: str = "") -> None:
    """7.65：禁止总线 tp_bus 对已 forward 转交给专属会话的消息调用 sheepdog ask（交由承接会话判断与处理）。"""
    if topic_id != BUS_TOPIC_ID:
        return
    if about and any((a or "").strip() for a in about):
        try:
            ids = [mid for mid, _ in pend.about_sources(cfg, store, about)]
        except ValueError as e:
            raise OutboundError(EXIT_INVALID, str(e))
    else:
        ids = [mid for mid, _ in pend.batch_sources(cfg, store, batch or current_batch(store, topic_id))]
    forwarded = store.forwarded_message_targets(ids)
    if forwarded:
        pairs = "、".join(f"{mid} -> {dst}" for mid, dst in forwarded.items())
        raise OutboundError(
            EXIT_INVALID,
            f"总线 tp_bus 不能对已 forward 转交的消息调用 sheepdog ask（已转交：{pairs}）："
            "请交由承接该消息的专属会话按职责与 VIP 规则判断处理，没有发送",
        )


def check_bus_not_forwarded_reply(store: Store, topic_id: str, target_id: str) -> None:
    """7.65：禁止总线 tp_bus 对已 forward 转交给专属会话的消息调用 sheepdog reply。"""
    if topic_id == BUS_TOPIC_ID and target_id in (forwarded := store.forwarded_message_targets([target_id])):
        raise OutboundError(EXIT_INVALID, f"总线 tp_bus 不能对已 forward 转交的消息调用 sheepdog reply（{target_id} 已转交给 {forwarded[target_id]}）：请交由承接该消息的专属会话处理与回复，没有发送")


def is_self_sent_message(cfg: Config, store: Store, target, target_id: str = "") -> bool:
    """7.136（REG-144）：判断目标消息是否由本机主人或本机器人发出（route=self / reason in (self, self_bot) / sends / bot_outbox）。
    专席在群里主动发卡催办同事（带 🐕SheepDog 落款且 @ 同事）后，在 watch 提醒下对自己的根消息调用 sheepdog reply --to <own_om_id> 跟进催办，
    绝不能被误判为「回复同事的 AI 助理」或「插话旁人对话」而拦截。"""
    keys = target.keys() if target is not None and hasattr(target, "keys") else ()
    mid = (target_id or ((target["message_id"] if "message_id" in keys else "") or "")).strip()
    if target is not None:
        t_route = ((target["route"] if "route" in keys else "") or "").strip()
        t_reason = ((target["reason"] if "reason" in keys else "") or "").strip()
        t_sender = ((target["sender_id"] if "sender_id" in keys else "") or "").strip()
        if t_route == "self" or t_reason in ("self", "self_bot"):
            return True
        bot_ids = ident.bot_ids(cfg, store) if hasattr(ident, "bot_ids") else frozenset()
        if t_sender and ((cfg.self_open_id and t_sender == cfg.self_open_id) or (bot_ids and t_sender in bot_ids)):
            return True
    if mid and store is not None:
        if hasattr(store, "send_by_message") and store.send_by_message(mid) is not None:
            return True
        if hasattr(store, "get_bot_outbox") and store.get_bot_outbox(mid) is not None:
            return True
    return False


def check_group_reply_allowed(
    cfg: Config,
    store: Store,
    topic_id: str,
    target: sqlite3.Row | dict | None,
    *,
    even_if_owner_replied: bool = False,
) -> None:
    """7.103 / 7.136（REG-071 / REG-144）：多人大群代回门禁：
    1. 非总线会话严禁对停留在 tp_bus 未 forward 转交的被动群消息（keyword/group/watched_chat/vip_sender 或仅 @ 他人）调用 reply 越权抢答；
    2. 严禁在多人大群里直接对仅 @ 其他同事（未 @ 主人/机器人/所有人且未 forward 转交）的消息调用 reply；
    3. 严禁在多人大群里直接 reply 同事个人账号代发的 AI 助理消息（防跨 Agent 群聊乒乓死循环；主人确需回复时可加 --even-if-owner-replied）；
    4. 豁免本机自己发出的群根消息（is_self_sent_message），允许专席在自己发出的催办/送审根卡片话题下继续 reply 跟进催办。"""
    if target is None:
        return
    from .prompts import is_agent_proxy_content, is_group_directed_to_others
    from .store import SYSTEM_CHAT

    keys = target.keys() if hasattr(target, "keys") else ()
    chat_type = ((target["chat_type"] if "chat_type" in keys else "") or "").strip()
    chat_id = ((target["chat_id"] if "chat_id" in keys else "") or "").strip()
    if chat_type in ("p2p", SYSTEM_CHAT, "") or chat_id in cfg.owner_side_chat_ids:
        return
    if is_self_sent_message(cfg, store, target):
        return
    t_topic = ((target["topic_id"] if "topic_id" in keys else "") or "").strip()
    t_reason = ((target["reason"] if "reason" in keys else "") or "").strip()
    t_tags = (target["tags_json"] if "tags_json" in keys else "") or ""
    t_content = (target["content"] if "content" in keys else "") or ""
    forwarded = "forwarded_from:" in t_tags
    bot_ids = ident.bot_ids(cfg, store) if hasattr(ident, "bot_ids") else frozenset()
    directed_other = is_group_directed_to_others(target, cfg.self_open_id, bot_ids)

    if topic_id != BUS_TOPIC_ID and t_topic == BUS_TOPIC_ID and not forwarded:
        if t_reason in ("keyword", "group", "watched_chat", "vip_sender") or directed_other:
            raise OutboundError(
                EXIT_INVALID,
                f"群消息 {target['message_id']} 停留在总线 tp_bus 且未通过 sheepdog forward 转交给 {topic_id}（reason={t_reason}）："
                "总线未转交的群消息仅作背景知会，严禁专席在群内越权代回（没有发送）",
            )
    if is_agent_proxy_content(t_content) and not even_if_owner_replied:
        raise OutboundError(
            EXIT_INVALID,
            f"群消息 {target['message_id']} 来自同事的 AI 助理代发（个人账号代理）："
            "严禁在多人大群里直接对其他 AI 助理调用 sheepdog reply（防 Agent 群内乒乓死循环）；"
            "请保持群内静默，有结论用 sheepdog report 或 sheepdog ask 找主人（没有发送）",
        )
    if directed_other and not forwarded and not even_if_owner_replied:
        raise OutboundError(
            EXIT_INVALID,
            f"群消息 {target['message_id']} 在群内明确只 @ 了其他同事（未 @ 主人或机器人，且未经 forward 交办）："
            "非呼叫我们的群内他人对话严禁主动插话代回；若需同步进展请用 sheepdog report 汇报主人（没有发送）",
        )


def check_vip_floor(cfg: Config, store: Store, topic_id: str, about: list[str] | None, vips: dict | None,
                    floor: str, batch: str = "", floor_type: str | None = None) -> list[tuple[str, str]]:
    """7.61 / 7.65：先核总线未对已转交消息越位 ask，再核 ask 关联的来源里有 VIP 本人发的消息（或显式传了 --floor / --floor-type）时，
    必须同时提供合法的 --floor-type（credentials|destructive|vip_conflict）和非空单行 --floor（且不得自相矛盾写脱敏/只读/不要密钥等），
    否则拒绝（退出码 2，什么都不发、不写账本）。只读。"""
    check_bus_not_forwarded_ask(cfg, store, topic_id, about, batch)
    hits = vip_sources(cfg, store, topic_id, about, vips, batch)
    clean_floor, clean_type = _one_line(floor), (floor_type or "").strip()
    if not hits and not (bool(clean_floor) or bool(floor) or (floor_type is not None)):
        return hits
    if not _is_valid_vip_floor(clean_floor, clean_type):
        who = ("这次关联的来源里有 VIP 本人发的消息：" + "、".join(f"{name}（{mid}）" for mid, name in hits) + "。") if hits else ""
        enum_desc = "；".join(f"{k}={v}" for k, v in VIP_FLOOR_TYPES.items())
        cur_parts = ([f"--floor-type「{clean_type}」"] if clean_type else []) + ([f"--floor「{clean_floor}」"] if clean_floor else [])
        cur_text = f"当前 {'、'.join(cur_parts)}（如生产操作、只读查询、脱敏数据导出、常规权限/配置）" if cur_parts else "当前未完整提供合法的 --floor-type 与 --floor，"
        raise OutboundError(
            EXIT_INVALID,
            f"{VIP_FLOOR_REFUSAL}。{who}VIP 事务调用 sheepdog ask（或使用 --floor）时，必须同时带上 "
            f"--floor-type <credentials|destructive|vip_conflict> 与 --floor \"<一行具体底线原因>\"。VIP 只有 3 条底线才允许 ask：{enum_desc}。"
            f"{cur_text}不属于底线，请直接按安全合理方式办理并用 sheepdog report 报备，严禁调用 sheepdog ask！没有发送",
        )
    return hits


_CC_PROXY_ASK_RE = re.compile(
    r"(?:申请|开通|分配|询问是否有多余)[^\n。；]{0,18}"
    r"(?:(?<![A-Za-z0-9_])CC(?![A-Za-z0-9_])(?!\s*(?:防护|攻击|告警|策略|规则))"
    r"|Claude\s*Code|家宽节点|住宅\s*(?<![A-Za-z0-9_])IP(?![A-Za-z0-9_])|防风控专线|专属网络|专线代理)",
    re.I,
)
_CC_SEAT_EVIDENCE_RE = re.compile(
    r"(?:proxy-panel|tp_net-ops|node\d|空余席位|当前.{0,6}人数|使用人数|席位余量|已满)",
    re.I,
)
_ENV_REQUIRED_TITLE_RE = re.compile(
    r"(?:请提供|需要|申请).{0,16}(?:实时音视频.{0,8}凭据|子成员账号)",
    re.I,
)
_EXPLICIT_ENV_DECL_RE = re.compile(
    r"(?:明确是|100%是|仅用于|目标环境|所属环境)[：:\s]*(?:纯)?(?:测试|生产|Stage|预发|acme-testing-env|acmelab-000002|acme-prod-000001)",
    re.I,
)
_REASK_MANUAL_OPTION_RE = re.compile(
    r"(?:请定是由专席.{0,24}还是(?:您|主人|Matthew)(?:本人)?(?:手动|亲自)|"
    r"[B-E][.、:：]\s*由\s*(?:Matthew|主人|您)(?:本人)?(?:手动|亲自)(?:在|去|登录|创建|配置|操作))",
    re.I,
)
_REASK_BLOCKER_EXEMPT_RE = re.compile(
    r"(?:余额不足|权限不足|报错|失败|冲突|403|PermissionDenied|无法访问|需二次拍板)",
    re.I,
)
_PR_MERGE_EXEC_ASK_RE = re.compile(
    r"(?:\bgh\s+pr\s+merge\b|(?:直接合入|同意合入)[^。\n]{0,24}(?:(?<![A-Za-z0-9_])PR(?![A-Za-z0-9_])|评审单)[^。\n]{0,24}(?:打\s*(?:Tag|标签)|推送.{0,8}Tag|发布到生产|发上生产))",
    re.I,
)
_PR_HUMAN_REVIEW_STATUS_RE = re.compile(
    r"(?:人工\s*(?:Code\s*)?Review|人工(?:代码)?评审|reviewDecision|APPROVED|人类\s*Reviewer|已(?:通过|完成)\s*(?:人工\s*)?(?:PR\s*)?Review|未(?:过|提交)\s*(?:人工\s*)?(?:PR\s*)?Review)",
    re.I,
)
_CORS_OR_CALLER_DOMAIN_ASK_RE = re.compile(
    r"(?:(?:CORS|跨域|Origin)[^。\n]{0,20}白名单[^。\n]{0,20}(?:确认|域名)|"
    r"(?:是否有|确认有无|有没有)[^。\n]{0,16}(?:其他|别的)(?:前端)?域名[^。\n]{0,16}调用)",
    re.I,
)
_CORS_TRAFFIC_SCAN_EVIDENCE_RE = re.compile(
    r"(?:云上流量|真实.{0,6}流量|gcloud\s+logging|Referer|访问日志|流量扫描|48\s*h|本地记录)",
    re.I,
)
_PEER_OWNER_CONSULT_EVIDENCE_RE = re.compile(
    r"(?:tp_awei|阿魏|awei|sheepdog\s+tell|已与.{0,8}(?:阿魏|awei|负责人)[^。\n]{0,8}(?:确认|对齐|核对))",
    re.I,
)
_TEST_APPROVAL_QA_BYPASS_ASK_RE = re.compile(
    r"测试审批[^。\n]{0,24}(?:是否可)?(?:免测|直接放行|先走\s*QA)",
    re.I,
)
_TEST_JUMPBOX_FIREWALL_ASK_RE = re.compile(
    r"(?:acme-testing-env|测试环境)[^。\n]{0,40}(?:acme-test-jumpbox|测试跳板机|allow-demo-office|allow-demo-connect)[^。\n]{0,40}(?:tcp:22|防火墙放行|IP\s*白名单)",
    re.I,
)
_DESTRUCTIVE_OR_PROD_FIREWALL_RE = re.compile(
    r"(?:删除|移除|(?<!非)(?<!不涉及)生产|acmelab-000002|acme-prod-000001|0\.0\.0\.0/0)",
    re.I,
)
_ASK_CLAIMS_RECOMMENDED_OPTION_RE = re.compile(
    r"(?:(?:建议|推荐|倾向|默认)\s*[:：]?\s*(?:点)?选\s*[A-E]|请\s*(?:点)?选\s*[A-E])(?![A-Za-z0-9])",
    re.I,
)
_TEST_ENV_IAM_GRANT_ASK_RE = re.compile(
    r"(?:acme-testing-env|测试环境|测试服务端账号)[\s\S]{0,260}"
    r"(?:roles/iam\.serviceAccountTokenCreator|serviceAccountTokenCreator|Token\s*签发权限|TokenCreator|"
    r"roles/run\.(?:developer|viewer|invoker)|roles/secretmanager\.(?:secretAccessor|viewer|secretVersionAdder)|"
    r"add-iam-policy-binding)",
    re.I,
)
_PROD_OR_DESTRUCTIVE_IAM_RE = re.compile(
    r"(?:acmelab-000002|acme-prod-000001|remove-iam-policy-binding|delete|删除|移除|"
    r"roles/owner|roles/editor|roles/resourcemanager\.projectIamAdmin|"
    r"(?<!非)(?<!不涉及)(?<!登记)(?<!测试与)生产(?:环境|项目|库|集群|\s*IAM|\s*Secret|\s*权限))",
    re.I,
)
_TENCENT_MAIN_ACCOUNT_CONSOLE_ASK_RE = re.compile(
    r"(?:腾讯云|SBX|sbx:|cam:|UIN)[\s\S]{0,280}"
    r"(?:UnauthorizedOperation|子账号.{0,16}(?:无|缺|没有).{0,16}权限|主账号控制台)"
    r"[\s\S]{0,220}"
    r"(?:(?:Matthew|主人|您)[^。\n]{0,20}(?:登录|去|在)[^。\n]{0,16}控制台[^。\n]{0,20}(?:创建|建|生成|开通|申请)|"
    r"登录(?:主账号)?控制台[^。\n]{0,20}(?:创建|建|生成|开通))",
    re.I,
)
_LOCAL_TCCLI_CHECK_EVIDENCE_RE = re.compile(
    r"(?:~/\.cloudcli|\.cloudcli/|cloud-a\.credential|cloudcli\s+凭据|本机\s*(?:cloudcli|腾讯云主账号)\s*凭据)",
    re.I,
)
_VPS_DB_WHITELIST_SINGLE_IP_ASK_RE = re.compile(
    r"(?:(?:专线|本机出口\s*IP|出口\s*IP|机器(?:的)?\s*(?:公网\s*)?IP|\b192\.0\.2\.\d+\b|node\d+|Vega|家宽)[\s\S]{0,220}"
    r"(?:Cloud\s*SQL|托管数据库|ADB|RDS|数据库[^\n。]{0,16}白名单|授权网络|authorized\s*networks)"
    r"|(?:Cloud\s*SQL|托管数据库|ADB|RDS|数据库[^\n。]{0,16}白名单)[\s\S]{0,220}"
    r"(?:专线|本机出口\s*IP|出口\s*IP|机器(?:的)?\s*(?:公网\s*)?IP|\b192\.0\.2\.\d+\b|node\d+|Vega|家宽))",
    re.I,
)
_VPS_FLEET_WHITELIST_EVIDENCE_RE = re.compile(
    r"(?:tp_net-ops|proxy-panel[\s\S]{0,36}(?:全量|全部|全套|\d+\s*台|清单)|"
    r"(?:全量|全部|全套)\s*(?:需要加白的\s*)?(?:专线|VPS)\s*(?:出口\s*)?IP\s*清单|"
    r"(?<!转交\s)(?<!转交)(?:Hazel|tp_hazel))",
    re.I,
)
_AWEI_PROXY_OR_CONFIRMED_ASK_RE = re.compile(
    r"(?:阿魏|awei|Owen\s*Wei)[\s\S]{0,260}"
    r"(?:助理代发|Claude\s*代[^\n。]{0,16}发|在阿魏本人确认后|待阿魏本人确认|阿魏本人确认)",
    re.I,
)
_SYMBOLIC_RELEASE_SCHEDULE_ASK_RE = re.compile(
    r"(?:(?:提请|提议|申请|计划)[^\n。]{0,18}排期[^\n。]{0,28}(?:发版|上线|窗口)|"
    r"同意排期[^\n。]{0,28}待[^\n。]{0,24}(?:Review|评审|合入|提测)后|"
    r"B[.、:：]\s*调整排期时间或[^\n。]{0,24}有其他要求)",
    re.I,
)
_GENUINE_SCHEDULE_CONFLICT_RE = re.compile(
    r"(?:不合理|封网冲突|资源冲突|破坏性变更|不兼容|停机维护|架构违规|高风险异常)",
    re.I,
)
_DB_USER_ACCOUNT_ASK_RE = re.compile(
    r"(?:数据库[^\n。]{0,24}(?:连接串[^\n。]{0,12}只读账号|只读(?:账号|账密|用户)|User\s*账号)|"
    r"只读(?:账号|账密|用户)[^\n。]{0,24}(?:创建|开通|发放|申请|密码)|"
    r"(?:migrator|主账号|root|postgres)[^\n。]{0,24}凭据[^\n。]{0,24}创建[^\n。]{0,16}只读(?:账号|用户)|"
    r"\bsylvia_readonly\b)",
    re.I,
)
_RECENT_OWNER_HOLD_VETO_RE = re.compile(
    r"(?:这个不动|暂时不动|先不动|不用动|暂不开|忽略吧|先忽略|不用管了|暂缓开通|暂不执行)",
    re.I,
)
_LOGIN_PWD_RESET_ASK_RE = re.compile(
    r"(?:登录[^\n。]{0,20}提示密码不正确|申请重设[^\n。]{0,20}临时密码)"
    r"[\s\S]{0,160}(?:请求查询[^\n。]{0,24}并重设|重设临时密码私发)",
    re.I,
)
_LOGIN_DB_LOG_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:已查(?:库|验)|已核查\s*auth\.users|认证服务\s*返回|登录日志显示|报错日志为)",
    re.I,
)
_NEW_VPS_PURCHASE_FULL_ASK_RE = re.compile(
    r"(?:(?:新购|采买)[^\n。]{0,20}(?:专属\s*)?(?:住宅\s*)?VPS[\s\S]{0,180}(?:满编|0\s*空闲|无空闲)|"
    r"(?:满编|0\s*空闲|无空闲)[\s\S]{0,180}(?:新购|采买)[^\n。]{0,20}(?:专属\s*)?(?:住宅\s*)?VPS)",
    re.I,
)
_OVERALL_VPS_ROSTER_EVIDENCE_RE = re.compile(
    r"(?:(?:总体|全网|全部|\d+\s*台|各节点)[^\n。]{0,20}(?:在用|使用|占用)[^\n。]{0,12}(?:人员)?名单|"
    r"\A(?=[\s\S]*\bnode1\b)(?=[\s\S]*\bnode2\b)(?=[\s\S]*\bnode3\b)(?=[\s\S]*\bnode4\b))",
    re.I,
)
_US_PROD_GCP_IAM_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\bacme-prod-000001\b)(?=[\s\S]*(?:开通|授予|赋权|申请)[^\n。]{0,36}(?:日志|只读|IAM|权限|roles/logging\.viewer|viewer))",
    re.I,
)
_DARCY_US_COMPLIANCE_CONFIRMED_RE = re.compile(
    r"(?:已(?:向|找|经|同)\s*Darcy(?:\s*Vale)?\s*(?:确认|核实|对齐)|Darcy(?:\s*Vale)?\s*(?:已确认|已同意|已批复|回复可以))",
    re.I,
)
_STAGE_SUBACCOUNT_MINIMAL_IAM_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:\b2000\d{8}\b|Stage\s*(?:独立\s*)?成员账号))(?=[\s\S]*(?:最小受限策略|最小策略|受限权限))",
    re.I,
)
_STAGE_SUBACCOUNT_ADMIN_OPTION_RE = re.compile(
    r"(?:AdministratorAccess|QcloudAdministratorAccess|\badmin\b\s*权限|配\s*admin)",
    re.I,
)
_PREMATURE_READONLY_INVESTIGATION_ASK_RE = re.compile(
    r"(?:A\s*[.、:：)\]]\s*(?:仅做|先做|先进行|先由专席|先)?只读[^\n]{0,36}(?:排查|核实|核查|检查|盘点|摸底)"
    r"|建议[：:\s]*选\s*A[^\n]{0,28}先(?:由专席)?只读(?:核实|排查|核查|盘点|摸底)(?:具体是哪个|现状[^\n]{0,20}(?:并备好|准备好)[^\n]{0,16}方案)"
    r"|[AB12]\s*[.、:：]\s*新开专席(?:排查|核查|摸底)?[^\n]{0,48}(?:排查|核实|摸底|定位)[^\n]{0,32}(?:归属|负责人|代码仓|具体是哪个)"
    r"|A\s*[.、:：]\s*指定负责人\s*[（(][^\n）)]{0,24}直接告知负责人)",
    re.I,
)
_NAT_PORT_DOWNSIZE_ASK_RE = re.compile(
    r"(?:minPortsPerInstance|--min-ports-per-instance)[\s\S]{0,80}?(?:调小|下调|降为|改为\s*(?:64|128|256|512)|=\s*(?:64|128|256|512))",
    re.I,
)
_NAT_PEAK_PER_VM_PORT_EVIDENCE_RE = re.compile(
    r"(?:(?:单实例|单机|单个实例|per[\s_-]*vm|单\s*VM)[^\n。]{0,36}?(?:最高|峰值|最大|max)[^\n。]{0,24}?端口"
    r"|(?:最高|峰值|最大)[^\n。]{0,20}?(?:端口使用数|已用端口)[^\n。]{0,24}?(?:实例|VM|机器)"
    r"|port_usage|allocated_ports|used_ports)",
    re.I,
)
_BUS_PROXY_SPECIALIST_ASK_RE = re.compile(
    r"(?:执行\s*[：:]\s*(?:新开专席\s*)?|由.{0,8}专席\s*[（(]?|建议由\s*)(tp_(?!bus\b)[a-z0-9_-]+)",
    re.I,
)
_BUS_CODEX_OR_BLOCKED_FORWARD_ASK_RE = re.compile(
    r"(?:\bcodex\b|转交[^\n。]{0,24}(?:被拒绝|失败|报错|失联|已收掉|closed))",
    re.I,
)
_WRITE_PERM_ASK_RE = re.compile(
    r"申请[^\n。]{0,40}(?:修改权限|查看和修改权限|读写权限|写权限)",
    re.I,
)
_REFUSAL_OR_RESTRICT_OPT_RE = re.compile(
    r"(?:严守红线拒绝|拒绝[^\n]{0,20}权限|不开放|严禁下放|仅开[^\n]{0,20}只读|暂缓)",
    re.I,
)
_GRANT_WRITE_PERM_OPT_RE = re.compile(
    r"(?:(?:(?:同意|批准|按申请|直接)[^\n]{0,16})?(?:给他|为其|向其|给\s*\S+)?(?:授权|开通|开放|授予|下放)[^\n]{0,24}(?:修改权限|写权限|读写权限|所申请权限|全部权限)|^(?:[A-E]\s*[.、:：]\s*)?(?:同意|批准)(?:授权|开通))",
    re.I,
)
_UNINVESTIGATED_STATUS_ASK_RE = re.compile(
    r"(?:(?:路由|配置|渠道|网络)[^\n。]{0,16}现状确认|需确认[^\n。]{0,48}现状[，,]\s*以便)",
    re.I,
)
_CONCRETE_STATUS_EVIDENCE_RE = re.compile(
    r"(?:已实测|已核查|实测现状|当前配置为|当前路由为|kubectl|secret|configmap)",
    re.I,
)
_RCA_GOV_ASK_RE = re.compile(
    r"(?:根因治理(?:决策|排期|方案)?|询问[^\n]{0,24}(?:卡顿|故障|雪崩)是否修复及治理方案|(?:卡顿|雪崩)根因后[，,\s]*询问是否修复)",
    re.I,
)
_RCA_TIMELINE_AND_IMPACT_EVIDENCE_RE = re.compile(
    r"(?:前因后果|时间线|发生时间|故障时间|影响面|当前现状|目前状态|已恢复|已止血|报错率|P99|HTTP\s*[45]\d\d|\b\d{1,2}:\d{2}\b)",
    re.I,
)
_OLD_IMAGE_PARTIAL_FIX_ASK_RE = re.compile(
    r"(?:跑(?:着|的是)旧镜像[\s\S]{0,160}(?:留着\s*Stage|指向\s*Stage)|要先修[^\n]{0,24}还是连[^\n]{0,24}一起切)",
    re.I,
)
_CICD_PROD_READY_EVIDENCE_RE = re.compile(
    r"(?:ci/?cd|流水线|cloud\s*build|github\s*actions|mirror)[^\n]{0,40}(?:通的|打通|可(?:以)?部署到\s*prod|自动部署)",
    re.I,
)
_EXISTING_VPS_SLOT_AVAILABLE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*申请[^\n。]{0,24}(?:Claude|(?<![A-Za-z0-9_])VPS(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])CC(?![A-Za-z0-9_])|家宽)?[^\n。]{0,12}(?:专线|节点))(?=[\s\S]*(?:余\s*[1-9]\d*\s*(?:空额|空位|席)|空余\s*[1-9]\d*\s*(?:空额|空位|席)))",
    re.I,
)
_REGION_HARDCODE_OR_SUBFILTER_HOTFIX_ASK_RE = re.compile(
    r"(?:Nginx\s+sub_filter[\s\S]{0,120}(?:热修|强制|清空|禁用)"
    r"|(?:热修前端|热修线上)[\s\S]{0,120}(?:彻底禁掉|强制使用|清空\s*Sentry\s*DSN|禁用\s*(?:CF|Cloudflare|Turnstile|Sentry))"
    r"|(?:彻底禁掉|强制使用)[\s\S]{0,120}(?:CF\s*Turnstile|Turnstile|云厂商验证码)[\s\S]{0,120}(?:Sentry|清空\s*Sentry\s*DSN))",
    re.I,
)
_CONFIG_SWITCH_ISOLATION_EVIDENCE_RE = re.compile(
    r"(?:配置开关|环境变量开关|开关(?:来)?控制|feature\s*flag|什么配置下只给国内|按(?:环境|配置)(?:开关|变量)?(?:控制|区分|隔离))",
    re.I,
)
_BROADCAST_SYNC_ASK_RE = re.compile(
    r"(?:日清播报|(?<!群沟通与每日)进度播报|战报播报|群内播报|(?<!群沟通与)每日进度[^\n]{0,20}播报|正式同步至群)",
    re.I,
)
_SKIP_OR_CANCEL_SYNC_OPT_RE = re.compile(
    r"(?:^[A-E][\.\、\:：\)\s]\s*[^\n]*(?:暂不同步|暂时不同步|取消(?:本次|今晚|今日)?播报|本次不(?:发|播报|同步)|不播报|不同步))",
    re.I | re.M,
)
_CN_SITE_TENCENT_MAINLAND_VS_INTL_ASK_RE = re.compile(
    r"(?:是用腾讯云甲站还是(?:先用)?乙站"
    r"|在腾讯云甲站账号下开辟"
    r"|对外前再对齐甲站"
    r"|若甲站账号权限已就绪建议\s*[A-E])",
    re.I,
)
_TENCENT_INTL_SUBACCOUNT_POSTURE_RE = re.compile(
    r"(?:跟\s*site-a\.example\s*一样[\s\S]{0,40}腾讯云乙站[\s\S]{0,40}子成员账号"
    r"|统一(?:放在|使用)腾讯云乙站[\s\S]{0,40}(?:子成员账号|成员账号|\b2000\d{8}\b))",
    re.I,
)
_TENCENT_EDGEONE_RESOURCE_CHANGE_ASK_RE = re.compile(
    r"(?:(?:在|于)?(?:生产|预发|Stage|Prod)?\s*CDN[^\n。]{0,36}(?:新增|添加|开通|创建|配置|修改|绑定)[^\n。]{0,36}(?:加速域名|子域名|域名|站点|回源|[a-z0-9.-]+\.site-a\.example)"
    r"|CDN\s*[（(]?\s*site-a\.example\s*站点[）)]?\s*(?:新增|添加|开通|创建|配置)"
    r"|(?:配置|添加|开通)\s+[a-z0-9.-]+\.site-a\.example\s+(?:加速域名|子域名))",
    re.I,
)
_TENCENT_SUBACCOUNT_OR_UIN_EVIDENCE_RE = re.compile(
    r"(?:\b2000\d{8}\b|(?:子成员账号|成员子账号|子账号|主账号)\s*(?:[:：]?\s*)?(?:2000\d{8}|acmeco-(?:prod|stage)))",
    re.I,
)
_TENCENT_MAINLAND_FALLBACK_ON_LIMIT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:境内主账号|国内腾讯云|\b1000\d{8}\b|境内账号)[^\n。]{0,40}(?:建|创建|新建|开辟|申请)[^\n。]{0,30}(?:子账号|账号|凭据|密钥|TMS|IMS|内容安全))"
    r"(?=[\s\S]*(?:UnsupportedRegion|地域不支持|国际账号[^\n。]{0,24}(?:不支持|报错|受限)|TMS|IMS|内容安全))",
    re.I,
)
_URGENCY_AND_TENCENT_CONTACT_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:已(?:向|跟|与)?[^\n。]{0,16}确认[^\n。]{0,16}(?:着急|急需|紧急)|先问(?:一下)?[^\n。]{0,16}(?:是否)?着急|询问[^\n。]{0,16}是否着急))"
    r"(?=[\s\S]*(?:问问云厂商|咨询腾讯云|联系腾讯云|找腾讯云(?:的人|接口人|架构师|客服)?))",
    re.I,
)
_NEW_DOMESTIC_DOMAIN_ASK_RE = re.compile(
    r"(?:(?:是否)?(?:新增|新建|开通|配置|给一个)[^\n。]{0,20}国内(?:跳转|直连|可达)?域名"
    r"|国内(?:跳转|直连|可达)?域名[^\n。]{0,20}(?:是否)?(?:新增|新建|开通|配置)"
    r"|(?:新增|新建|开通)[^\n。]{0,20}\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.site-c\.example\b[^\n。]{0,20}(?:国内|直连|跳转))",
    re.I,
)
_DOMESTIC_DOMAIN_AUDIENCE_EVIDENCE_RE = re.compile(
    r"(?:给谁访问|访问(?:受众|对象|群体|人群)|受众(?:群体|对象|是谁)|目标(?:受众|访问群体|用户群体)"
    r"|已(?:向|跟)[^\n。]{0,16}确认[^\n。]{0,20}(?:给.*访问|访问对象|受众)"
    r"|调用链\s*[:：]\s*国内用户\s*->|面向国内(?:站)?(?:C\s*端)?(?:用户|客户))",
    re.I,
)
_NEW_SITE_C_DOMESTIC_SUBDOMAIN_ASK_RE = re.compile(
    r"(?:(?:是否|同意)?(?:新增|新建|开通|配置)[^\n。]{0,24}\b([a-z0-9-]+)\.site-c\.example\b"
    r"|\b([a-z0-9-]+)\.site-c\.example\b[^\n。]{0,24}(?:是否|同意)?(?:新增|新建|开通|配置))",
    re.I,
)
_SITE_A_REUSE_EVIDENCE_RE = re.compile(
    r"(?:\b[a-z0-9-]+\.site-a\.example\b|(?:能不能|是否可(?:以)?|改用|复用|优先用)\s*(?:[a-z0-9-]+\.)?site-a\.example\b)",
    re.I,
)
_TEST_ENV_NACOS_ACCOUNT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:测试环境|acme-testing-env|测试\s*配置中心|acme-testing-config))"
    r"(?=[\s\S]*(?:申请|创建|开通)[^\n。]{0,28}配置中心[^\n。]{0,20}账号)"
    r"(?![\s\S]*(?:生产环境|acmelab-000002|acme-prod|生产\s*配置中心))",
    re.I,
)
_DOMESTIC_TV_TO_OVERSEAS_GCP_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\b[a-z0-9-]+\.site-a\.example\b)"
    r"(?=[\s\S]*(?:GCP\s*生产|acmelab-000002|\b192\.0\.2\.\d{1,3}\b|edge-https-proxy|GCP\s*代理))",
    re.I,
)
_DATA_CROSS_BORDER_COMPLIANCE_EVIDENCE_RE = re.compile(
    r"(?:数据(?:不能|不准|禁止|是否)?出境|跨境合规|出境合规|合规(?:问题|风险|确认|评估)|放在腾讯云国际|收束到腾讯云国际|腾讯云国际(?:站|这边))",
    re.I,
)
_OWNER_ASKED_HISTORY_EXPLANATION_RE = re.compile(
    r"(?:查一下|讲一下|说一下|看看|梳理)[^\n。]{0,28}(?:来龙去脉|前因后果|相关版本)",
    re.I,
)
_KEY_CHANGE_ROLLBACK_WITHOUT_GROUP_TIMELINE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:DEMO_API_KEY|API_KEY)[^\n。]{0,28}(?:双\s*Key\s*兼容|兼容恢复|兼容放行))"
    r"(?=[\s\S]*(?:发版单|硬切为|硬换成))",
    re.I,
)
_GROUP_HISTORY_EXPLAINED_EVIDENCE_RE = re.compile(
    r"(?:已在群(?:里|内)[^\n。]{0,20}(?:讲清|说明|同步|回复|汇报)[^\n。]{0,20}(?:来龙去脉|前因后果|相关版本)"
    r"|来龙去脉[^\n。]{0,20}已在群(?:里|内)(?:讲清|说明|同步|回复))",
    re.I,
)
_NODE7_SOLO_VIP_NODE_ASK_RE = re.compile(
    r"(?:(?:node7|198\.51\.100\.237)(?:(?!node(?!7\b)\d+)[^\n。；]){0,45}?(?:尚余|空余|余\s*[1-9]\d*\s*[席名个位]|有空位|空余\s*[1-9]|共用|(?<![不未禁可])开通|(?<![不未禁可])分配)"
    r"|(?:在\s*node7|选\s*[A-E][^\n。；]{0,16}node7|分配至\s*node7)[^\n。；]{0,40}(?:开通|分配|席位|专线))",
    re.I,
)
_INTERNAL_DEV_REPO_PROXY_EDIT_RECOMMEND_RE = re.compile(
    r"(?:(?P<proxy_opt>[A-E])\s*[\.、:：]\s*不开(?:通)?(?:仓库|仓|GitHub)?权限[^\n]*(?:由我们|我们)(?:克隆|直接)?[^\n]*(?:修改|代改|改)[\s\S]{0,160}?(?:建议|推荐)\s*[：:]?\s*(?:选\s*)?(?P=proxy_opt)(?![A-Za-z0-9])"
    r"|(?:建议|推荐)\s*[：:]?[^\n。]*(?:不开(?:通)?(?:仓库|仓|GitHub)?权限[^\n。]*由我们(?:克隆|直接)?(?:修改|代改)|由我们(?:克隆|直接)?(?:修改|代改)[^\n。]*无需(?:开放|开通|扩散)(?:仓库|仓)?权限))",
    re.I,
)
_DIRECT_MIRROR_GIT_PUSH_RE = re.compile(
    r"(?:git\s+push\s+(?:--[a-z0-9-]+\s+)*(?:mirror(?:\s|$)|https?://mirror\.build/)"
    r"|\A(?=[\s\S]*mirror\.build/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=[\s\S]*(?:添加\s*mirror\s*remote|git\s+remote\s+add\s+mirror\b|推送(?:代码|分支)[^\n。；]{0,24}至\s*MIRROR)))",
    re.I,
)
_MIRROR_MIRROR_SYNC_COMPLIANT_RE = re.compile(
    r"(?:sync-to-mirror\.ya?ml|(?:严禁|禁止|不要|不得)[^\n。；]{0,24}(?:直接)?[^\n。；]{0,16}(?:向\s*mirror|git\s+push\s+mirror|推送[^\n。；]{0,12}MIRROR)|由\s*GitHub[^\n。；]{0,20}Action\s*自动同步到\s*MIRROR)",
    re.I,
)


def check_no_direct_mirror_git_push_guard(text: str, *, scene: str = "lamb") -> None:
    """1.1.30（REG-119）：拦截向只读镜像仓 mirror.example 直接执行或派发本机 git push 的任务单/命令/请示。"""
    s = (text or "").strip()
    if not s:
        return
    if _DIRECT_MIRROR_GIT_PUSH_RE.search(s) and not _MIRROR_MIRROR_SYNC_COMPLIANT_RE.search(s):
        raise OutboundError(
            EXIT_INVALID,
            f"{scene} 拒绝（REG-119 MIRROR 只读镜像仓与 GitHub 唯一提交源铁律）："
            "检测到直接向 `mirror.example` 添加 remote 并执行本机 `git push mirror` / 推送分支至 MIRROR！\n"
            "主人明确立规：代码只往主仓库提交与 push，镜像仓库只做镜像，由主仓库的 Action 自动同步过去再跑部署。\n"
            "本机直推 `mirror.example` 不仅会被后续 `sync-to-mirror.yml` 强推覆盖导致脑裂，还会在首次推送空仓时全量上传数十万 Git 对象造成 20+ 分钟卡死。"
            "请改为：在 GitHub 源仓库提交/合入并通过 `.github/workflows/sync-to-mirror.yml` 自动同步至 `mirror.example` 触发 MIRROR 流水线。",
        )
_SHARED_CONFIG_IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}\b")
_GENERIC_CONFIG_IDENTS = frozenset(
    {
        "DEFAULT_GROUP",
        "CLOUD_RUN",
        "CLOUD_BUILD",
        "CLOUD_SQL",
        "GITHUB_ACTIONS",
        "SECRET_MANAGER",
        "SERVICE_ACCOUNT",
        "SYSTEM_MESSAGE",
        "PLANNER_RESPONSE",
        "USER_INPUT",
        "USER_EXPLICIT",
    }
)
_DARCY_PARITY_AND_ENTRYPOINT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\b(?:Darcy(?:\s*Vale)?|Darci)\b)"
    r"(?=[\s\S]*(?:能否提供现有[^\n。]{0,24}(?:跳板机|容器集群|集群|入口)"
    r"|临时开启[^\n。]{0,24}(?:cls-[a-z0-9]+|容器集群|集群)[^\n。]{0,16}公网访问"
    r"|在[^\n。]{0,24}(?:cls-[a-z0-9]+|acmecoprod|容器集群)[^\n。]{0,24}新建\s*Secret[\s\S]{0,60}入口方案"
    r"|给\s*(?:Darcy|Darci)[^\n。]{0,24}(?:开通|开放|提供)[^\n。]{0,24}(?:权限|跳板机|入口)))",
    re.I,
)
_VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:谁来跟(?:完|进|到底)?|几点前完成|\.mirror\.yml|PR\s*构建修复))"
    r"(?=[\s\S]*由\s*(?:Darcy(?:\s*Vale)?|Darci|阿魏|Owen\s*Wei)\s*自己来(?:跟|合入|做|处理|接手))",
    re.I,
)
_MULTI_COMPONENT_CAPACITY_PLAN_ASK_RE = re.compile(
    r"\A(?:"
    r"(?=[\s\S]*(?:压测|全栈|生产|容量|扩容|升配)[^\n。]{0,24}(?:升配|扩容|容量|规格)[^\n。]{0,16}方案)"
    r"(?=[\s\S]*\b(?:PG|PostgreSQL)\b)"
    r"(?=[\s\S]*\b(?:MySQL|ADB|AnalyticDB)\b)"
    r"(?=[\s\S]*\bRedis\b)"
    r"|"
    r"(?=[\s\S]*第\s*1\s*组)(?=[\s\S]*第\s*2\s*组)(?=[\s\S]*第\s*3\s*组)"
    r"(?:(?=[\s\S]*\b1A\b)(?=[\s\S]*\b2[AB]\b)(?=[\s\S]*\b3[AB]\b)|(?=[\s\S]*生产环境[^\n。]{0,24}(?:深度体检|专项核实|资源治理|降配|缩容)))"
    r"|"
    r"(?=[\s\S]*\b1\.\s*\S)(?=[\s\S]*\b2\.\s*\S)(?=[\s\S]*\b3\.\s*\S)(?=[\s\S]*\b4\.\s*\S)"
    r"(?=[\s\S]*(?:CoreDNS[\s\S]{0,120}(?:UpdateAddon|虚拟节点)|[0-9]{2,}\s*个(?:多副本)?(?:工作负载)?[^\n。]{0,16}\bPDB\b))"
    r")",
    re.I,
)
_LARK_DOC_LINK_EVIDENCE_RE = re.compile(
    r"(?:(?:larksuite\.com|feishu\.cn)/(?:docx|docs|wiki)/[A-Za-z0-9]+|飞书(?:云)?文档|Lark\s*文档|docs/[^\s`'\"<>]+\.md\b|已整理(?:成)?\s*MD\s*(?:文件|文档))",
    re.I,
)
_MIDDLEWARE_PROVISION_WITHOUT_USAGE_PROOF_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\b(?:RabbitMQ|消息队列|RocketMQ|ActiveMQ)\b)"
    r"(?=[\s\S]*(?:开通|购买|新建|采购|部署)[^\n。]{0,36}(?:托管|专享版|3\s*节点|集群|cbs\s*云盘\s*PVC))"
    r"(?=[\s\S]*(?:选项|建议\s*[：:]?\s*(?:选\s*)?[AB]))",
    re.I,
)
_MIDDLEWARE_LIVE_TRAFFIC_AND_PURPOSE_EVIDENCE_RE = re.compile(
    r"(?:(?:publish|deliver|ack)[_\s-]*(?:rate|count|吞吐|速率)|队列(?:消息|积压|消费)(?:量|数|速率)|实际(?:在用|收发|消费|生产)[^\n。]{0,20}(?:消息|队列|业务)|具体(?:业务用途|干嘛的|哪个业务功能在用)|没有人(?:在)?用|0\s*条(?:消息|队列)|建议直接(?:下线|移除|删除|关停))",
    re.I,
)
_MAIL_ASSISTANT_ONLY_ACCESS_ASK_RE = re.compile(
    r"(?:(?:邮件助手|Mail\s*Assistant|飞书邮箱|邮箱)(?:收到|推送|转来|发来)[^\n。]{0,36}(?:访问|权限|Access\s*Request|申请|请求)"
    r"|\[Access\s*Request\][^\n]{0,80}requested\s+access\s+to)",
    re.I,
)
_FEISHU_DIRECT_REQUEST_EVIDENCE_RE = re.compile(
    r"(?:当事人在飞书|本人(?:已)?在飞书|在飞书(?:私聊|群内|主动)(?:找过来|提出|确认|催办)|飞书消息\s*`?om_[A-Za-z0-9]+)",
    re.I,
)
_UNVERIFIED_TICKET_PROMISE_RE = re.compile(
    r"(?:正在跟进(?:腾讯云|阿里云|GCP|AWS|云厂商|官方|该)?[^\n。；]{0,24}工单"
    r"|(?:目前已|已经|已)提(?:工)?单咨询(?:腾讯云|阿里云|GCP|AWS|云厂商)?[^\n。；]{0,16}(?:架构师|客服|技术支持|售后)?"
    r"|拿到(?:云厂商|腾讯云|阿里云|官方|工单)[^\n。；]{0,20}(?:确切结论|结论|答复)(?:与\s*ETA|或\s*ETA)?后第一时间[^\n。；]{0,16}同步)",
    re.I,
)
_TICKET_NEGATION_OR_VERIFIED_ID_RE = re.compile(
    r"(?:(?:并未|未|没有|尚无|暂无|无需|不用|不需)[^\n。；]{0,12}(?:提(?:交)?(?:云厂商)?工单|工单号|工单结论)"
    r"|(?:工单(?:号|编号|ID)?|ticket(?:\s*id)?|case(?:\s*id)?)\s*(?:为|是|[:：#])\s*[A-Za-z0-9_-]{5,})",
    re.I,
)
_RAW_STDOUT_IN_ASK_OPTION_RE = re.compile(
    r"(?:\[(?:PROD|TEST|STAGE)\]\s*Status:\s*(?:FAILED|SUCCESS)"
    r"|,\s*Code:\s*\d+\b"
    r"|ERROR:\s+column\s+\S+\s+does\s+not\s+exist"
    r"|Traceback\s*\(most recent call last\)"
    r"|deployment\s+\"[^\"]+\"\s+successfully\s+rolled\s+out)",
    re.I,
)
_VIP_LOW_PERM_OR_PROXY_INSTEAD_OF_E2E_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:阿魏|awei|Owen\s*Wei|Darcy(?:\s*Vale)?|Darci)[^\n。；]{0,36}申请[^\n。；]{0,60}(?:子账号|CAM|权限|kubeconfig|容器集群|集群))"
    r"(?=[\s\S]*(?:不外发[^\n。；]{0,40}(?:AK/SK|凭据|密钥|kubeconfig|权限)[^\n。；]{0,40}由[^\n。；]{0,40}(?:直接|代建|代跑|部署)[\s\S]{0,260}?(?:建议|推荐)\s*[：:]?\s*(?:选\s*)?A"
    r"|仅限[^\n。；]{0,40}单命名空间权限))",
    re.I,
)
_VIP_HIGH_PERM_E2E_RECOMMEND_RE = re.compile(
    r"A\s*[\.、:：)\]]\s*[^\n]*(?:高权限|管理员|AdministratorAccess|全量|完整权限|端到端)[\s\S]{0,360}?(?:建议|推荐)\s*[：:]?\s*(?:选\s*)?A\b",
    re.I,
)
_SINGLE_API_SUBPATH_CACHE_OR_CORS_PATCH_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:CDN|CDN|缓存|NoCache|不缓存|CORS|跨域|串域))"
    r"(?=[\s\S]*/api/[a-z0-9_-]+/[a-z0-9_/*-]+)"
    r"(?=[\s\S]*(?:设为\s*(?:NoCache|不缓存)|配置\s*/api/[a-z0-9_-]+/[a-z0-9_/*-]+\s*为\s*(?:NoCache|不缓存)|刷新(?:该(?:路径|目录))?缓存))",
    re.I,
)
_CLASS_WIDE_API_CACHE_AUDIT_EVIDENCE_RE = re.compile(
    r"(?:全量\s*/api/\*|所有\s*/api/\*|全部\s*(?:API|/api)|整个\s*/api(?:/bff)?/\*|排查.{0,16}其他\s*API\s*接口|已核查全站\s*API\s*缓存规则|统一将\s*/api/\*)",
    re.I,
)
_STAGE_PROXY_RUN_INSTEAD_OF_SELF_ACCESS_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:Stage|测试环境)\s*(?:环境)?(?:机器|CVM|主机|跳板机|ins-[a-z0-9]+))"
    r"(?=[\s\S]*(?:由专席在\s*Stage\s*代跑|同意在\s*Stage\s*机器代跑|在\s*Stage\s*机器代跑))",
    re.I,
)
_STAGE_DIRECT_SELF_ACCESS_OPTION_RE = re.compile(
    r"(?:直接给[他她其]?.{0,12}(?:Stage|机器|登录|SSH|跳板机)?权限|为.{0,12}开通\s*Stage\s*(?:机器|CVM|跳板机|SSH)\s*(?:登录|访问)?权限|让[他她其]自己(?:登录|上去|在\s*Stage)?(?:跑|执行|操作|弄))",
    re.I,
)
_DOMAIN_OWNER_E2E_WITHHOLD_DNS_EDGEONE_ASK_RE = re.compile(
    r"自主(?:完成)?(?:构建、)?(?:发布|发版)(?:与|和)?回滚[\s\S]{0,680}"
    r"A\s*[\.、:：)\]]\s*[^\n]*(?:DNS 服务|CDN)[^\n]*(?:给只读|走受控变更)[\s\S]{0,240}"
    r"B\s*[\.、:：)\]]\s*[^\n]*按申请内容全部开通[^\n]*(?:CDN|DNS 服务|站点权限)",
    re.I,
)
_DOMAIN_OWNER_FULL_GRANT_RECOMMEND_A_RE = re.compile(
    r"(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选|选择)?\s*B\b|"
    r"A\s*[\.、:：)\]]\s*[^\n]*按申请内容全部开通[^\n]*(?:CDN|DNS 服务|站点权限)",
    re.I,
)
_BINARY_FULL_ACCOUNT_ADMIN_ASK_RE = re.compile(
    r"(?:直接给|能否直接给|申请)[^\n。；]{0,20}全量(?:的)?\s*`?admin`?\s*权限[\s\S]{0,680}"
    r"A\s*[\.、:：)\]]\s*不开全量\s*`?Admin`?[\s\S]{0,280}"
    r"B\s*[\.、:：)\]]\s*[^\n]*全量\s*`?AdministratorAccess`?",
    re.I,
)
_RD_SERVICE_ADMIN_VS_NETWORK_BOUNDARY_EVIDENCE_RE = re.compile(
    r"(?:已(?:向|跟).{0,12}(?:确认|核实|问清).{0,16}(?:常用的资源范围|日常资源范围|平时都做哪些)|"
    r"(?:开发|产研)(?:相关)?(?:高频)?改动.{0,20}admin.{0,30}(?:网络|VPC|安全组).{0,16}(?:不行|不开|不放开|除外))",
    re.I,
)
_LEGACY_TEMP_DOMAIN_REDIRECT_OVER_OFFLINE_ASK_RE = re.compile(
    r"(?:联调域名|过渡域名|临时域名)[\s\S]{0,420}"
    r"A\s*[\.、:：)\]]\s*[^\n]*(?:301|302|重定向|跳转)[\s\S]{0,280}"
    r"B\s*[\.、:：)\]]\s*[^\n]*(?:停用|offline)[^\n]*(?:并下线|下线)",
    re.I,
)
_LEGACY_TEMP_DOMAIN_DIRECT_OFFLINE_RECOMMEND_A_RE = re.compile(
    r"(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选|选择)?\s*B\b|"
    r"A\s*[\.、:：)\]]\s*[^\n]*(?:直接[^\n]*(?:停用|offline)[^\n]*并下线|直接下线[^\n]*(?:联调|过渡|临时)域名)",
    re.I,
)
_DB_OWNER_WITHHOLD_DB_PERM_ASK_RE = re.compile(
    r"A\s*[\.、:：)\]]\s*[^\n]*不外发[^\n]*(?:账密|密码|账号)[^\n]*由\s*[`a-z0-9_-]*\s*[^\n]*(?:执行|代执行)[\s\S]{0,280}"
    r"B\s*[\.、:：)\]]\s*[^\n]*(?:给\s*Hazel(?:\s*Yu)?\s*开通[^\n]*(?:DB|数据库|MySQL|PG)[^\n]*权限|发给[他她]?生产库连接账号)",
    re.I,
)
_DB_OWNER_GRANT_RECOMMEND_A_RE = re.compile(
    r"(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选|选择)?\s*B\b|"
    r"A\s*[\.、:：)\]]\s*[^\n]*给\s*Hazel(?:\s*Yu)?\s*开通[^\n]*(?:DB|数据库|MySQL|PG|最高)[^\n]*权限",
    re.I,
)
_P2P_LIVE_OWNER_CONVERSATION_OR_DELEGATE_CHECK_ASK_RE = re.compile(
    r"(?:(?:请求确认)?(?:你方才说到的|你说的)\s*[「『\"][^」』\"\n]{1,40}[」』\"]\s*，?\s*是否指"
    r"|A\s*[\.、:：)\]]\s*[^\n]*由我们找(?:外部渠道|账单|相关)?[^\n]*负责人\s*[（(][^）)\n]*(?:Darcy|Caleb|Hazel|阿魏)[^）)\n]*[）)][^\n]*核查[^\n]*控制台[^\n]*(?:状态|截图)[\s\S]{0,260}"
    r"B\s*[\.、:：)\]]\s*[^\n]*由你本人登录[^\n]*控制台[^\n]*(?:查看|截图)[^\n]*(?:或径直在私聊里回|私聊回复))",
    re.I,
)
_P2P_OWNER_DELEGATE_ALREADY_CHECKED_EVIDENCE_RE = re.compile(
    r"(?:已(?:通过|向|找)\s*(?:`?tp_[a-z0-9_-]+`?|Darcy|Caleb)\s*(?:核实|确认|反馈)|"
    r"负责人(?:反馈|表示|确认)(?:无权限|无法登录|需要主人)|主人在私聊(?:明确)?指示由专席)",
    re.I,
)
_DOMESTIC_ZERO_BALANCE_LOADTEST_ACTIVATE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:国内(?:站)?主账号|境内(?:站)?主账号|\b1000\d{8}\b)[^\n。]{0,48}(?:未开通\s*LOADTEST|余额为\s*0|开通\s*LOADTEST))"
    r"(?=[\s\S]*A\s*[\.、:：)\]]\s*[^\n]*(?:由你|主人|登录)[^\n]*开通\s*LOADTEST)",
    re.I,
)
_INTL_LOADTEST_EVALUATED_EVIDENCE_RE = re.compile(
    r"(?:乙站[^\n。]{0,20}(?:LOADTEST|压测)|腾讯云国际[^\n。]{0,20}(?:LOADTEST|压测)|已(?:核查|评估|确认)[^\n。]{0,20}乙站[^\n。]{0,16}(?:LOADTEST|压测))",
    re.I,
)
_COLLEAGUE_PERSONAL_CRED_ROTATE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*A\s*[\.、:：)\]]\s*[^\n]*(?:同意)?立即(?:轮换|重置)[^\n]*(?:个人账号|hazel_yu|子账号|专属账号)[^\n]*密码)"
    r"(?=[\s\S]*B\s*[\.、:：)\]]\s*[^\n]*暂不(?:轮换|重置)[^\n]*(?:先问|询问|确认)[^\n]*(?:是否正在跑|空闲再换|长耗时))",
    re.I,
)
_COLLEAGUE_IDLE_CONFIRMED_EVIDENCE_RE = re.compile(
    r"(?:已(?:向|与|找)\s*(?:Hazel|对方|本人|同事)[^\n。]{0,24}确认(?:空闲|未在跑|当前无|可以轮换)|"
    r"(?:Hazel|对方|本人)(?:已确认|反馈|回复)(?:空闲|可以轮换|没在跑|未在跑))",
    re.I,
)
_DUPLICATE_PURCHASE_OR_ORDER_PAY_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:待支付订单|请款|完成[^\n。]{0,20}订单支付|支付[^\n。]{0,20}订单))"
    r"(?=[\s\S]*(?:已开通的?(?:按量付费|资源包)|package-[a-z0-9]{6,}|已经买(?:了|好)|已购买))",
    re.I,
)
_EXISTING_PACKAGE_UNUSABLE_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:已核实[^\n。]{0,36}(?:无法使用|不能用|不可用|已耗尽|余额不足|额度不足|规格不满足)|"
    r"实测[^\n。]{0,36}(?:已开通的?按量付费|package-[a-z0-9]{6,})[^\n。]{0,24}(?:无法|不能|报错|余量不足))",
    re.I,
)
_UNVERIFIED_EXTERNAL_PROXY_CONSOLE_RECHARGE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:ResiProxy|resi-proxy\.example|住宅代理)[^\n。]{0,60}(?:配额耗尽|流量耗尽|余额|充值|续订))"
    r"(?=[\s\S]*(?:登录\s*(?:ResiProxy\s*)?控制台|确认余额[^\n。]{0,20}充值|充值续订|proxyacct\d+))",
    re.I,
)
_EXTERNAL_PROXY_OWNERSHIP_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:不在我们管的账号里|不是我们管的账号|外部团队开的(?:号|账号)?|我们这边只管了\s*acmeco|"
    r"已(?:向|与)\s*Darcy(?:\s*Vale)?\s*(?:或\s*Darci\s*)?(?:核实|确认)[^\n。]{0,24}账号归属)",
    re.I,
)
_PEER_SOURCE_HANDLED_OR_NOT_OURS_RE = re.compile(
    r"(?:不在我们管的账号里|不是我们管的|外部团队开的|我们这边登不进去|已经(?:充值|充好|处理|解决|恢复|搞定)了?)",
    re.I,
)
_STILL_ASKING_OWNER_ACTION_RE = re.compile(
    r"(?:登录[^\n。]{0,24}控制台[^\n。]{0,24}(?:充值|续订|确认余额|处理)|"
    r"仍需(?:你|主人|你或\s*Darcy)[^\n。]{0,24}(?:登录|充值|续订|处理)|"
    r"A\s*[\.、:：)\]]\s*[^\n]*(?:立即)?登录[^\n]*控制台[^\n]*(?:充值|续订))",
    re.I,
)
_DB_OWNER_WITHHOLD_WAN_OR_CRED_ASK_RE = re.compile(
    r"\A(?=[\s\S]*Hazel(?:\s*Yu|\s*老师)?[^\n。]{0,48}申请[^\n。]{0,48}(?:开通外网|开公网|公网连接|账号密码))"
    r"(?=[\s\S]*A\s*[\.、:：)\]]\s*[^\n]*(?:保持纯内网|不开放公网|不分配公网)[^\n]*(?:不开放公网|密码外发|DMC|控制台))"
    r"(?=[\s\S]*B\s*[\.、:：)\]]\s*[^\n]*(?:同意|授权)?开通外网[^\n]*(?:28\s*个|白名单|安全组|交付凭据))"
    r"(?=[\s\S]*建议\s*[：:]?\s*(?:选\s*)?A\b)",
    re.I,
)
_PREFER_MANUAL_TERMINAL_OVER_SESSION_SCRIPT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:选项\s*A|A\s*[\.、:：)\]])[^\n]*(?:你在本机终端直接跑|由你在本机[^\n]*执行)[^\n]*脚本)"
    r"(?=[\s\S]*(?:选项\s*B|B\s*[\.、:：)\]])[^\n]*(?:点名)?授权(?:父会话|专席|本会话)(?:直接)?(?:调用|执行)(?:该|收尾)?脚本)"
    r"(?=[\s\S]*(?:推荐\s*A|建议\s*[：:]?\s*(?:选\s*)?A\b|选项\s*A\s*[（(]\s*推荐\s*[）)]))",
    re.I,
)
_VPS_RECLAIM_BACKUP_SEAT_OVER_NEW_NODE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:专线|proxy-panel|node\d+|VPS))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:零采购)?[^\n]*(?:回收|腾退|清理)[^\n]*(?:node\d+|备用席位|双占|备用二线))"
    r"(?=[\s\S]*(?:选项\s*B|B)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:合并进[^\n]*待购|新购|新买|采购)[^\n]*(?:node\d+|新机|VPS))"
    r"(?=[\s\S]*(?:建议\s*[：:]?\s*(?:选\s*)?A\b|(?:选项\s*A|A\.)[^\n]*[（(]\s*推荐\s*[）)]))",
    re.I,
)
_VPS_SEAT_ALLOC_OR_PURCHASE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:开通(?:专属网络|专线)|专线开通|新专线|采购\s*node\d+|待购的?\s*node\d+))"
    r"(?=[\s\S]*(?:node\d+|proxy-panel|tp_net-ops))",
    re.I,
)
_RIGID_PROXY_HOPS_BREAKING_ONE_HOP_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:TRUSTED_PROXY_HOPS|PROXY_HOPS)\s*(?:改为|设为|=)\s*[2-9]\b)"
    r"(?=[\s\S]*(?:1\s*跳拒|仅\s*1\s*跳\s*XFF\s*请求返回\s*5\d\d|fewer\s+than\s+the\s+[2-9]\s+trusted\s+proxy\s+hops))",
    re.I,
)
_MULTI_DOMAIN_ENTRY_HOPS_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:已核实[^\n。]{0,36}(?:站点B\s*与\s*站点C|多域名入口|不同域名入口)[^\n。]{0,36}跳数|"
    r"按可信代理\s*CIDR|trusted_proxy_cidrs|兼容\s*1\s*跳与\s*2\s*跳)",
    re.I,
)
_EXPIRED_PIPELINE_TOKEN_MANUAL_RENEW_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:GITHUB_TOKEN|GitHub\s*PAT|密钥仓|流水线[^\n。]{0,20}(?:密钥|Token|PAT))[^\n。]{0,28}过期)"
    r"(?=[\s\S]*(?:网页端|控制台|后台)[^\n。]{0,24}(?:更新|粘贴|替换)[^\n。]{0,20}(?:PAT|Token|密钥))",
    re.I,
)
_LONG_TERM_NON_EXPIRING_TOKEN_REMEDY_RE = re.compile(
    r"(?:No\s+expiration|永不过期|长期有效|vendor\s*依赖|go\s+mod\s+vendor|GitHub\s*App|免\s*Token)",
    re.I,
)
_STAGE_DB_OR_TUNNEL_WITHHOLD_OVER_GRANT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:Stage|测试环境)[^\n。]{0,36}(?:Postgres|数据库|只读连接|只读账号|评测))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:不对外发连接串|不开(?:跨境)?隧道|不开放[^\n]*连接))"
    r"(?=[\s\S]*(?:选项\s*B|B)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:受限\s*SSH\s*隧道|无\s*Shell\s*端口转发)[^\n]*交付)"
    r"(?=[\s\S]*(?:建议\s*[：:]?\s*(?:选\s*)?A\b|(?:选项\s*A|A\.)[^\n]*[（(]\s*推荐\s*[）)]))",
    re.I,
)
_FE_REPO_SELF_PR_WITHOUT_OWNER_FEEDBACK_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:web-app|deploy-pages\.yml|embed(?:-dev)?\.site-a\.example)[\s\S]{0,240}(?:建单[^\n。]{0,24}提\s*PR|向[^\n。]{0,36}提\s*PR|提\s*PR\s*修复|提交\s*PR))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:建单[^\n]*提\s*PR|向[^\n]*web-app[^\n]*提\s*PR|提\s*PR\s*修复))"
    r"(?![\s\S]*(?:阿魏|awei|前端负责人)[^\n。]{0,36}(?:没空|忙|暂无法|来不及|委托我们|让我们代|已反馈))",
    re.I,
)
_BUNDLE_STAGE_CLEANUP_WITH_PROD_SCALE_ZERO_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:销毁|退订|释放)[^\n]*Stage[^\n]*(?:并将|同时将)?[^\n]*生产[^\n]*(?:缩为|缩容至|改为)\s*0\s*副本)"
    r"(?=[\s\S]*(?:选项\s*B|B)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*仅(?:销毁|退订|清理)[^\n]*Stage[^\n]*生产[^\n]*(?:暂)?维持原样不改)"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_DORMANT_ZERO_REPLICA_BATCH_MUTATE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:现网活跃[^\n。；]{0,28}已清零\s*[（(]?0\s*个|正在跑的服务[^\n。；]{0,28}已全改到生产))"
    r"(?=[\s\S]*(?<![0-9])[1-9]\d+\s*个\s*(?:0\s*副本\s*Deployment|没开机的?服务|未开机的?服务))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:同意|批准|保持\s*0\s*副本)[^\n]*(?:同步|改掉|拷进生产|切生产|搬运|生产\s*TCR|tcr-prod))"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_REPORT_TELLS_OWNER_CLICK_OBSOLETE_ASK_RE = re.compile(
    r"(?:(?:上面|此前|之前|刚才|那张)[^\n。；]{0,28}卡[^\n。；]{0,16}点(?:选)?\s*[A-E]\s*即可"
    r"|点(?:选)?\s*[A-E]\s*即可[^\n。；]{0,24}(?:不用|无需)(?:我们|再)?(?:代切|代操作|执行)"
    r"|(?:由他们自己|对方已自行|已由对方自行)(?:切|切换|执行|处理|操作)[^\n。；]{0,28}(?:不用|无需)(?:我们|你)(?:代切|代操作|执行)[了啦]?)",
    re.I,
)
_PREFER_NEW_CLOUD_VENDOR_OR_OPEN_EGRESS_OVER_WORKING_TENCENT_CN_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:IMGSEARCH_A|搜图|图搜B|IMGSEARCH_B))"
    r"(?=[\s\S]*(?:(?:腾讯云|甲站)[^\n。；]{0,36}(?:已恢复|恢复正常|200\s*OK|实测返回\s*200|可用|余额|缩略图)|(?:恢复中|继续用|继续使用)[^\n。；]{0,16}腾讯(?:云)?[^\n。；]{0,16}`?IMGSEARCH_A`?))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*推荐\s*[）)])?\s*[\.、:：)\]]\s*[^\n]*(?:同意[^\n]*(?:阿里云|IMGSEARCH_B)|改走[^\n]*(?:阿里云|IMGSEARCH_B)|在腾讯云乙站[^\n]*开通\s*IMGSEARCH_A))"
    r"(?=[\s\S]*(?:(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b|(?:选项\s*A|A)\s*[（(]\s*推荐\s*[）)]))",
    re.I,
)
_TENCENT_CN_UNUSABLE_BLOCKER_EVIDENCE_RE = re.compile(
    r"(?:腾讯云(?:甲站)?\s*IMGSEARCH_A\s*(?:实测|确认)?(?:不可用|已停服|欠费停机|无法满足)|"
    r"已确认必须切换阿里云的硬性原因)",
    re.I,
)
_HEDGED_DUAL_RECOMMENDATION_ASK_RE = re.compile(
    r"建议[ \t:：]*(?:优先|先)?选?\s*([A-E])[ \t]*(?:[（(][ \t]*(?:或(?:者)?[ \t]*选?[ \t]*([A-E])\b[^\n）)]*|[^\n）)]{0,45}?(?:就)?选[ \t]*([A-E])\b[^\n）)]*)[）)]|(?:或|或者|/)[ \t]*选?[ \t]*([A-E])(?![a-zA-Z0-9]))",
    re.I,
)
_PARTIAL_APPROVAL_OVER_ISOLATED_NAMESPACE_OWNER_ASK_RE = re.compile(
    r"\A(?=[\s\S]*负责人[^\n。；]{0,48}申请[^\n。；]{0,60}(?:全套|自主)?(?:发版|运维)[^\n。；]{0,24}权限)"
    r"(?=[\s\S]*(?:命名空间\s*RBAC|最小范围的命名空间)[^\n。；]{0,48}kubeconfig)"
    r"(?=[\s\S]*B[ \t.、:：)\]]+部分批准[^\n]*不直发[^\n。；]*kubeconfig)"
    r"(?=[\s\S]*(?:(?:建议|推荐)[ \t:：]*(?:优先)?(?:选\s*)?B\b|B[ \t.、:：)\]]+部分批准[（(][^\n）)]*推荐))",
    re.I,
)
_PROD_DB_SCALE_WITHOUT_SHARED_INVENTORY_ASK_RE = re.compile(
    r"\A(?=[\s\S]*Reelhub[^\n。；]{0,40}(?:甲站|甲城|生产))"
    r"(?=[\s\S]*(?:PG\d*|PostgreSQL|postgres-[a-z0-9]+)[^\n。；]{0,80}(?:升配|升\s*\d+\s*C|扩容|只读实例))"
    r"(?=[\s\S]*(?:site-a\.example|Acmeco))",
    re.I,
)
_PROD_DB_SHARED_ISOLATION_EVIDENCE_RE = re.compile(
    r"(?:(?:生产|现网|账号下)[^\n。；]{0,20}(?:共有|共计|全部|所有|有哪些)\s*\d*\s*(?:台|个)?\s*(?:PG|PostgreSQL|数据库)\s*实例|"
    r"(?:与|和)\s*Acmeco[^\n。；]{0,24}(?:共用|混用|同实例|物理隔离|独立实例|不共用|分开使用)|"
    r"(?:Reelhub\s*独享|Acmeco\s*主站\s*PG))",
    re.I,
)
_CONSOLE_EYEBALL_PACKAGE_BALANCE_WITHOUT_CLI_OR_DARCY_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:资源包|基础包|套餐|VUM)[^\n。；]{0,40}(?:跑完|耗尽|剩余用量|余量))"
    r"(?=[\s\S]*(?:在控制台看一眼|看一眼控制台|请核看主账号后台余量))",
    re.I,
)
_CLI_BILLING_OR_DARCY_DELEGATION_EVIDENCE_RE = re.compile(
    r"(?:QueryBalance|QueryBillDetail|QueryJobs|Darcy)",
    re.I,
)
_INVERTED_PERM_ASK_PROXY_OVER_GRANT_RE = re.compile(
    r"\A(?=[\s\S]*申请[^\n。；]{0,60}(?:权限|账号))"
    r"(?=[\s\S]*A[ \t.、:：)\]*`]+(?![^\n]*拒绝)[^\n]*(?:不开[^\n]{0,28}(?:直连|账号|权限)[^\n]*(?:由专席|专席|SRE)|(?:转由专席|由专席|SRE)[^\n]*(?:代其执行|代跑|代配|脱敏导出|免提权)|仅走[^\n]{0,24}集群\s*Secret[^\n]*(?:不发[^\n]{0,16}(?:个人|明文|连接串)|仅内存直写)))"
    r"(?=[\s\S]*[BC][ \t.、:：)\]*`]+[^\n]*(?:同意(?:先|按[^\n]{0,24}申请|分两步)?(?:开通|授权|授予)|按(?:[^\n]{0,16})?申请(?:分两步)?(?:开通|授予|授权)|(?:同时)?同意将[^\n]{0,60}只限一库读取(?:角色|账号)[^\n]{0,40}接入串单独私信给))"
    r"(?=[\s\S]*(?:建议|推荐)[ \t*:：`]*(?:优先)?(?:选[ \t*`]*)?A\b)",
    re.I,
)
_CLOUD_ACCOUNT_RECHARGE_WITHOUT_UIN_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:国内(?:站)?主账号|国际(?:站)?(?:主|母|子)?账号|主账号|母账号))"
    r"(?=[\s\S]*(?:充值[^\n。；]{0,16}平账|平账|提请支付[^\n。；]{0,20}欠费|欠费[^\n。；]{0,40}充值))",
    re.I,
)
_CLOUD_ACCOUNT_EXPLICIT_UIN_EVIDENCE_RE = re.compile(
    r"\b(?:1000|2000)\d{8}\b|(?:账号\s*ID|UIN)\s*[：:=]?\s*\d{8,14}\b",
    re.I,
)
_PREFER_RECHARGE_ARREARS_OVER_FUNDED_MAIN_ACCOUNT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:有\s*[\d.]+\s*万(?:元)?(?:现金)?余额|余额充足)[^\n]*主账号)"
    r"(?=[\s\S]*A[ \t.、:：)\]]+[^\n]*(?:直接往|继续在|维持在)[^\n]*充值[^\n]*平账)"
    r"(?=[\s\S]*B[ \t.、:：)\]]+[^\n]*(?:迁回|在)[^\n]*主账号[^\n]*(?:开通子账号|开子账号|开权限))"
    r"(?=[\s\S]*(?:建议|推荐)[ \t:：]*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_HALF_DONE_ROLLOUT_WITHOUT_STALL_REASON_AND_E2E_GOAL_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:TRUSTED_PROXY_CIDRS|IP\s*判断(?:错误)?改造))"
    r"(?=[\s\S]*(?:已上线[^\n。；]{0,28}仅剩[^\n。；]{0,36}(?:还在旧版|未发生产)|已合入\s*`?main`?[，,\s]*生产尚未发布))",
    re.I,
)
_HALF_DONE_STALL_REASON_AND_E2E_CLIENT_IP_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:之前卡在|此前卡在|未完成原因|此前因[^\n。；]{0,24}未发|卡点说明))"
    r"(?=[\s\S]*(?:站点C|\bsite-c\b)[\s\S]{0,40}(?:站点B|\bsite-b\b)[\s\S]{0,60}(?:真实(?:的)?\s*client\s*ip|真实客户端\s*IP))",
    re.I,
)
_PROD_ZERO_TRAFFIC_SERVICE_SCALE_ZERO_WITHOUT_DEV_OWNER_CONFIRM_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,48}[a-z0-9]+-[a-z0-9-]+)"
    r"(?=[\s\S]*(?:连续\s*[\d.]+\s*天\s*`?0`?\s*请求|零请求原因|连续[^\n。；]{0,20}(?:0\s*请求|零请求|零流量)))"
    r"(?=[\s\S]*(?:A\s*[\.、:：)\]]\s*[^\n]*(?:minScale|minReplicas|--min|min)\s*[^\n]*(?:改为|设为|=)\s*`?0`?|请选\s*A\s*[（(]\s*改\s*min\s*=\s*0\s*[）)]))",
    re.I,
)
_PROD_ZERO_TRAFFIC_DEV_OWNER_CONFIRMED_EVIDENCE_RE = re.compile(
    r"(?:已(?:向|找|跟|与|联系)[^\n。；]{0,40}(?:Ivor|Nora|Haoyu|浩宇|研发|负责人|Owner)[^\n。；]{0,36}(?:确认|核实|沟通|反馈)|"
    r"(?:Ivor|Nora|Haoyu|浩宇|研发负责人|服务负责人)[^\n。；]{0,28}(?:已确认|回复|表示|同意))",
    re.I,
)
_BUNDLED_MULTI_LINEAR_ISSUES_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:[3-9]|\d{2,})\s*(?:张|个)\s*[^\n。；]{0,24}(?:安全|运维|审计|上线|验收|合规)?(?:单|工单|Issue)[^\n。；]{0,24}(?:认领|推进|确认|拍板))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:将\s*(?:[3-9]|\d{2,})\s*(?:张)?单(?:全部)?认领|先认领\s*(?:[3-9]|\d{2,})\s*(?:张)?单|其余\s*[2-9]\d*\s*(?:张)?单))",
    re.I,
)
_GRANT_111_SITE_A_HOLDER_ACCOUNT_TO_OTHERS_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:\b9000\d{2}6111\b|111\s*(?:的)?账号|site-a\.example[^\n。；]{0,16}持有(?:者)?账号))"
    r"(?=[\s\S]*(?:开通子账号|新建子账号|创建子用户|分配子账号|给[^\n。；]{0,16}(?:开|开通|授权|使用)[^\n。；]{0,20}(?:\b9000\d{2}6111\b|111\s*(?:的)?账号)))"
    r"(?![\s\S]*(?:严禁|禁止|不要|不再|不给|切勿)[^\n。；]{0,20}(?:使用|用|开|开通)[^\n。；]{0,16}(?:\b9000\d{2}6111\b|111))"
    r"(?![\s\S]*(?:迁回|改在|改用|转到)[^\n。；]{0,24}(?:\b9000\d{2}0222\b|222)[^\n。；]{0,24}(?:开通子账号|新建子用户|开权限))",
    re.I,
)
_SPECIAL_111_EXCEPTION_PROMINENT_NOTICE_RE = re.compile(
    r"(?:【例外情况说明】|⚠️\s*特殊情况|特殊情况必用\s*111)",
    re.I,
)
_PROD_LB_INGRESS_UNLOCK_MOD_WITHOUT_OWNER_WINDOW_CHECK_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,36}(?:负载均衡|Ingress|负载均衡))"
    r"(?=[\s\S]*(?:modification-protection|OperateProtect|防误改(?:保护)?(?:锁|注解)|保护锁))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:临时将|设为\s*`?\"?false\"?`?|解开|解锁))",
    re.I,
)
_PREMATURE_TEST_ONLY_OR_DRAFT_DEFER_ASK_RE = re.compile(
    r"\A(?:"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:同意对接\s*DBA\s*审核并在\s*(?:TEST|测试环境|Stage)\s*执行验证|仅在\s*(?:TEST|测试环境|Stage)\s*[^\n]{0,20}执行验证)[^\n]*(?:生产(?:操作|环境)?(?:待|等)?(?:测试)?验收后(?:另报|再报|再请示|另行请示)|生产坚决不动))"
    r"(?=[\s\S]*建议\s*[：:]\s*选?\s*A)"
    r"|"
    r"(?=[\s\S]*(?:回答「现在还要吗」|PR\s*#\d+\s*目前仍是\s*Draft\s*状态))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*先暂缓[^\n]*(?:解除\s*Draft|正式提测)[^\n]*再办)"
    r"(?=[\s\S]*(?:B|选项\s*B)\s*[\.、:：)\]]\s*[^\n]*同意仅在\s*TEST\s*环境里协助执行验证[^\n]*生产坚决不动)"
    r")",
    re.I,
)
_CONTROLLED_SCRIPT_READY_RECOMMEND_DEFER_B_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:授权马上执行|同意立即执行)[^\n]*(?:python3|bash|sh|\-\-apply)[^\n]*(?:备份|回滚))"
    r"(?=[\s\S]*(?:B|选项\s*B)\s*[\.、:：)\]]\s*[^\n]*暂缓(?:写库|执行|上线|变更))"
    r"(?=[\s\S]*建议\s*[：:]\s*(?:\*\*|__)?(?:选\s*)?B\b)",
    re.I,
)
_BARE_DB_INSTANCE_WITHOUT_PURPOSE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:MySQL|PostgreSQL|PG|MariaDB|CDB|RDS)\s+`?(?:cdb|postgres|rm|pgm)-[a-z0-9]{6,}`?"
    r"[\s\S]*(?:原子提交|写库|改库|回滚|无损回滚|\-\-apply|\-\-rollback))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:\-\-apply|执行|回滚))",
    re.I,
)
_DB_SCHEMA_OR_BUSINESS_PURPOSE_EVIDENCE_RE = re.compile(
    r"(?:(?:saas|public|auth|acmeco|billing|user|canvas|agent|cms|payment|order|asset)\.[a-z][a-z0-9_]{2,40}"
    r"|(?:计费|定价|用户|订单|画布|会话|素材|权限|资产|网关|任务|额度|账单|订阅|审核)(?:主|核心|业务|规则)?(?:库|表)|价目表"
    r"|(?:该|这个)\s*(?:MySQL|PG|PostgreSQL|数据库)\s*(?:库|实例)?\s*(?:是|用于|承载|存放))",
    re.I,
)
_ROUTINE_GROUP_DAILY_DIGEST_TABLE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:迁移日清播报|每日进度与(?:阻塞)?卡点播报|每日进展与卡点播报|群晚报草稿确认))"
    r"(?=[\s\S]*(?:DEMO-128\s*共\s*\d+\s*单|只在内部台账里静默跟踪))",
    re.I,
)
_OPTION_NUMBERED_SCHEME2_CODE_CHANGE_RE = re.compile(
    r"(?:^|\n)\s*[A-E]\s*[\.、:：)\]]\s*[^\n]*方案\s*(?:2|二)\b[^\n]*(?:代码|\d+\s*行|内存池|改造|重构|并发|封顶)",
    re.I,
)
_SCHEME2_CONCRETE_BREAKDOWN_EVIDENCE_RE = re.compile(
    r"(?:方案\s*(?:2|二)[^\n]{0,80}(?:具体(?:实现)?步骤|已发在上方|已把.*具体.*拆清|怎么改|怎么做|改动点|修改文件)|"
    r"(?:代码侧|具体)?方案\s*(?:2|二)[\s\S]{0,240}(?:\b[a-z0-9_/-]+\.(?:ts|js|py|go|rs|java)\b|第\s*[12一二]\s*步))",
    re.I,
)
_STAGE_TOY_STREAM_PREMATURE_PROD_SCALE_ASK_RE = re.compile(
    r"\A(?=[\s\S]{0,1600}连接数\s*=?\s*(?:[4-9]\d|\d{3,})\b)"
    r"(?=[\s\S]{0,1600}Stage[^\n。]{0,28}跑通\s*(?:1\d|[1-9])(?!\d)\s*(?:条|流|并发))"
    r"(?=[\s\S]{0,1600}(?:上线|更新到|部署到)[^\n。]{0,16}生产[\s\S]{0,160}(?:复测|压测)[^\n。]{0,24}(?:[1-9]\d{2})\s*(?:并发|VU|流))",
    re.I,
)
_STAGE_FULL_CONCURRENCY_VERIFIED_EVIDENCE_RE = re.compile(
    r"Stage[^\n。]{0,40}(?:已跑通|已实测|已通过|完成)[^\n。]{0,28}(?:[4-9]\d|[1-9]\d{2})\s*(?:条|流|并发|VU|-Conn)",
    re.I,
)
_PEAK_HOUR_CORE_INFRA_IMMEDIATE_MUTATE_WITHOUT_URGENCY_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,60}`?(?:svc-ledger(?:-cron)?|svc-account(?:-service)?|svc-dialog|svc-alpha-server|svc-events|svc-files(?:-v2)?)`?)"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:同意现在|同意立即|现在修正生产|立即修正生产)[^\n]*(?:svc-ledger|svc-account|svc-dialog|svc-alpha-server|svc-events|svc-files|STREAM_BROKERS))",
    re.I,
)
_CORE_INFRA_URGENCY_OR_LOW_PEAK_WINDOW_EVIDENCE_RE = re.compile(
    r"(?:(?:已(?:向|跟|与)|经)\s*[^\n。；]{0,20}确认(?:非常|特别)?紧急|线上故障止血|P0\s*止血|不在下午[^\n。；]{0,16}高峰期|低谷窗口|低峰窗口|今晚[^\n。；]{0,16}(?:执行|变更|操作))",
    re.I,
)
_CORE_INFRA_MORNING_ONLY_WINDOW_WITHOUT_TONIGHT_REQUESTER_ACCEPT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,60}`?(?:svc-ledger(?:-cron)?|svc-account(?:-service)?|svc-dialog|svc-alpha-server|svc-events|svc-files(?:-v2)?)`?)"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*明早低峰时段[^\n]*(?:修正|更新|发布|执行))"
    r"(?=[\s\S]*(?:B|选项\s*B)\s*[\.、:：)\]]\s*[^\n]*统一放到明早低峰时段)"
    r"(?![\s\S]*(?:A|B|选项\s*[AB])\s*[\.、:：)\]]\s*[^\n]*(?:今晚|今天晚上)[^\n]*(?:验收|执行))",
    re.I,
)
_RELEASE_NON_SECRET_ROUTING_DIFF_ASK_OWNER_INSTEAD_OF_INITIATOR_RE = re.compile(
    r"\A(?=[\s\S]*(?:发版单|提单)\s*\*{0,2}\d{10,14}\*{0,2})"
    r"(?=[\s\S]*(?:app-config|分层\s*Secret)[^\n。；]{0,80}(?:仅(?:追加|新增)\s*\d+\s*条[^\n。；]{0,24}路由|无明文密钥改动|非敏感路由))"
    r"(?=[\s\S]*请确认能否按\s*Diff\s*更新并发布)"
    r"(?![\s\S]*(?:发起人|提单人|Darcy)[^\n。；]{0,24}(?:已确认|已核对确认)\s*(?:该\s*)?Diff)",
    re.I,
)
_BARE_CLOUD_TEAM_NEW_IDENT_WITHOUT_RESOURCE_TYPE_ASK_RE = re.compile(
    r"在\s*(?:Vercel|GCP|AWS|Cloudflare)?\s*`?[a-z0-9_-]{3,24}`?\s*新建\s*`?[a-z0-9]+-[a-z0-9-]+`?\s*(?:[（(]\s*`?\$?\d+|\s*换入)(?!\s*(?:API\s*Key|Key|密钥|Project|项目|服务|实例|证书))",
    re.I,
)
_CROSS_CARD_PREVIOUS_REPORT_REF_ASK_RE = re.compile(
    r"(?:(?:模拟卡片|文案|草稿|纯文字|预览|方案明细)[^\n。]{0,28}(?:已发在|见|详见)(?:上一条|上方|上面|另一条)(?:汇报|消息|卡片)?"
    r"|(?:上面|上一条)(?:已把|已经把)[^\n。；;]{1,45}(?:模拟卡片|文案|草稿|纯文字|预览)[^\n。；;]{0,20}(?:列好|发出|贴出|发在))",
    re.I,
)
_PROXY_SEND_MESSAGE_WITHOUT_DRAFT_PREVIEW_ASK_RE = re.compile(
    r"(?:^|\n)\s*[AB]\s*[\.、:：)\]]\s*[^\n]*(?:代发|替你发)[^\n；;。]{0,24}(?:同步卡|消息|卡片|汇报|回复|通知)给\s*[^\n，,；;。]{1,24}",
    re.I,
)
_INLINE_DRAFT_PREVIEW_EVIDENCE_RE = re.compile(
    r"(?:(?:拟发文案|模拟文案|文案预览|消息草稿|卡片首屏原话|代回卡片首屏|拟发给[^\n：:]{1,20}的文案)|「[^」\n]{15,}」)",
    re.I,
)
_FEISHU_CUSTOM_BOT_MANUAL_ADD_SHIRK_TO_OWNER_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:添加|配置)[^\n。；;]{0,28}(?:自定义(?:告警|Webhook)?\s*机器人|Webhook\s*机器人))"
    r"(?=[\s\S]*(?:同为群管理员的\s*[^\n，,。；;]{1,24}直接(?:在客户端)?添加|请\s*[^\n，,。；;]{1,24}在客户端里直接把该机器人加进来))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:你在飞书|在飞书[^\n]{1,36}添加自定义机器人[^\n]*(?:将|把)\s*Webhook|由你在群内一键添加机器人))"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_PROD_SWITCH_ENABLE_WITHOUT_FIRST_SCREEN_TEST_VERIFIED_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；;]{0,36}(?:配置中心|[a-z0-9_.-]*cache[a-z0-9_.-]*|配置)[^\n。；;]{0,36}开关[^\n。；;]{0,28}(?:仍为|还是|目前还是|是)\s*`?false`?)"
    r"(?=[\s\S]*(?:生产开启[^\n。；;]{0,16}开关|开启[^\n。；;]{0,16}开关[^\n。；;]{0,20}滚动\s*revision|需改配置并新起\s*revision))",
    re.I,
)
_FIRST_SCREEN_TEST_SWITCH_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:测试环境|test-env|\bstage\b|预发)[^\n。；;]{0,48}(?:已开启|已打开|已测过|已实测|已验证|冒烟\s*PASS|测试通过|验证通过|尚未开启|未测)",
    re.I,
)
_ALERT_TARGET_GROUP_CHOICE_DELEGATE_TO_VIP_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:告警目标群|告警[^\n。；;]{0,20}(?:投哪个群|发哪个群|选哪个群)|是按原指定投「[^」\n]{1,24}」还是改投「[^」\n]{1,24}」))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*投「[^」\n]{1,32}」)"
    r"(?=[\s\S]*(?:B|选项\s*B)\s*[\.、:：)\]]\s*[^\n]*投「[^」\n]{1,32}」)",
    re.I,
)
_HOLLOW_DOC_INDEX_FIRST_SCREEN_ASK_RE = re.compile(
    r"(?:"
    r"完整\s*\d+\s*章飞书云文档已就绪[^\n]*[（(]\s*含来龙去脉[、，,]\s*\d+\s*大病根[\s\S]{0,160}建议选\s*A\s*[：:]?\s*先在[^\n。]{0,40}跑通两步演练"
    r"|已梳理完[^\n。]{0,48}防误报且保覆盖完整方案[\s\S]{0,120}建议选\s*A\s*[：:]?\s*先在[^\n。]{0,40}把「[^」\n]+」和「[^」\n]+」两步跑通"
    r")",
    re.I,
)
_OPAQUE_PHASE_JARGON_CHAIN_OPTION_ASK_RE = re.compile(
    r"(?:^|\n)\s*(?:选项\s*)?[A-E]\s*[\.、:：)\]]\s*[^\n]*Phase\s*1\s*[（(][^\n）)]*\+[^\n）)]*\+[^\n）)]*\+[^\n）)]*[）)]\s*\+\s*Phase\s*2\b",
    re.I,
)
_FIRST_SCREEN_PLAIN_ACTION_BREAKDOWN_EVIDENCE_RE = re.compile(
    r"(?:一是[^\n]{4,80}(?:；|;|\n)\s*二是|大白话讲|只测国外网址|隔\s*\d+\s*秒重测确认)",
    re.I,
)
_VERCEL_GATEWAY_VIRTUAL_MODEL_OR_AUTORECHARGE_WITHOUT_DARCY_CHECK_ASK_RE = re.compile(
    r"\A(?=[\s\S]*Vercel[\s\S]{0,40}(?:AI\s*网关|AI\s*网关))"
    r"(?=[\s\S]*(?:Virtual\s*Model|虚拟模型|vmodel/[a-z0-9._-]+))"
    r"(?=[\s\S]*(?:自动充值|低余额告警))"
    r"(?=[\s\S]*(?:A|选项\s*A)\s*[\.、:：)\]]\s*[^\n]*(?:Vercel|AI\s*网关|vmodel/|虚拟模型|自动充值))",
    re.I,
)
_DARCY_GATEWAY_STATUS_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:(?:Darcy|tp_darcy)[^\n。；;]{0,40}(?:已(?:经)?(?:弄|配|开|确认|核实|建好|处理)|尚未|还没|未配置)|"
    r"已(?:向|找|跟|与)\s*(?:Darcy|tp_darcy)[^\n。；;]{0,36}(?:核实|确认|协调|对齐))",
    re.I,
)
_PROD_RELEASE_FIRST_SCREEN_JUMPS_TO_TEST_PASS_ASK_RE = re.compile(
    r"已在测试环境里[^\n。]{0,40}跑通[^\n。]{0,60}[，,。]\s*(?:[^\n。]{0,50}[。，,]\s*)?(?:现)?申请(?:推送)?合入[^\n。]{0,40}发布生产",
    re.I,
)
_FIRST_SCREEN_WHY_PROBLEM_EVIDENCE_RE = re.compile(
    r"(?:为(?:了)?解决|解决[^\n。]{1,30}(?:问题|痛点|误报|故障|报错|卡死|卡顿|超时)|因为|起因|针对|导致|由于|误报|痛点|背景|来龙去脉)",
    re.I,
)
_FULL_TEXT_BACKGROUND_STORY_EVIDENCE_RE = re.compile(
    r"(?:来龙去脉|前因后果|起因|背景|解决什么(?:问题|痛点)|为什么(?:要)?(?:做|搞|改))",
    re.I,
)
_PROD_CODE_RELEASE_SKIPPING_PR_REVIEW_AND_QA_FLOW_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:(?:立即|现在)?(?:推送)?合入[^\n]*(?:发布生产|打\s*Tag\s*`?v\d+\.\d+)|执行\s*`?[^\s`]*prod_release_[a-z0-9_.-]+\.(?:zsh|sh)))"
    r"(?=[\s\S]*(?:(?:选项\s*B|B)\s*[\.、:：)\]]\s*[^\n]*(?:只推(?:送)?分支(?:建|创建)\s*GitHub\s*PR|创建\s*PR\s*暂不合入)|现(?:需|申请)?(?:推送)?合入(?:\s*代码|\s*GitHub)?\s*发布生产))",
    re.I,
)
_FULL_DEV_PR_REVIEW_AND_QA_FLOW_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:转(?:为)?正式\s*PR|正式\s*PR\s*#?\d+|PR\s*#?\d+[^\n。]{0,40}(?:已通过|通过)[^\n。]{0,20}(?:Review|评审)|reviewDecision\s*[:=]\s*`?APPROVED`?|人工\s*Review\s*已通过))"
    r"(?=[\s\S]*(?:通过\s*QA\s*(?:测试|验收)|QA\s*(?:测试|验收)(?:已)?通过|QA\s*BOT[^\n。]{0,30}(?:通过|验收)))",
    re.I,
)
_COLLEAGUE_RELAY_CONSOLE_ASK_WITHOUT_ORIGINAL_QUOTE_AND_CONTEXT_RE = re.compile(
    r"\A(?=[\s\S]*(?:在「[^」]{1,40}」群反映|反映\s*Stage\s*内网\s*负载均衡))"
    r"(?=[\s\S]*(?:(?:申请|希望)(?:您|你|主人|Matthew)?用(?:腾讯云)?主账号在控制台|联系腾讯开白))",
    re.I,
)
_COLLEAGUE_ORIGINAL_QUOTE_AND_CONTEXT_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:原文|原话|原消息)\s*[：:「『\"“])"
    r"(?=[\s\S]*(?:上下文|来龙去脉|前后文)\s*[：:])",
    re.I,
)
_CACHE_TTL_INCREASE_WITHOUT_SIDE_EFFECT_ANALYSIS_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:缓存\s*TTL|cache[_.]ttl)\s*[^\n。]{0,24}(?:从\s*\d+\s*(?:s|秒)\s*)?(?:调(?:大|高|至)|升至|改为)\s*\d+\s*(?:s|秒))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:TTL|缓存)[^\n]*(?:调(?:大|高|至)|升至|改为)\s*\d+\s*(?:s|秒))",
    re.I,
)
_CACHE_TTL_SIDE_EFFECT_ANALYSIS_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:其他影响|副作用|负面影响|潜在风险|边界影响))"
    r"(?=[\s\S]*(?:Redis\s*内存|内存占用|内存增长|绕过|直接改库|主动失效失败|兜底延迟))",
    re.I,
)
_SVC_ACCOUNT_CACHE_TTL_WITHOUT_OWEN_QIAO_SYNC_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\bsvc-account-service\b)"
    r"(?=[\s\S]*(?:鉴权缓存|demo_ctx_cache\.ttl|demo_block_cache_ttl)[\s\S]{0,60}(?:调(?:大|高|至)|升至|改为)\s*\d+\s*(?:s|秒))",
    re.I,
)
_OWEN_QIAO_OPINION_SYNCED_EVIDENCE_RE = re.compile(
    r"(?:(?:Owen(?:\s*Qiao)?|乔欧文|tp_owen)[^\n。；;]{0,40}(?:同意|认可|无异议|没意见|建议|确认|已回复)|"
    r"已(?:向|找|跟|与)\s*(?:Owen(?:\s*Qiao)?|乔欧文|tp_owen)[^\n。；;]{0,40}(?:同步|征求|询问|沟通|确认))",
    re.I,
)
_STAGE_GACCEL_OWNER_CONFIRMED_DECOMMISSION_RECOMMEND_B_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\bRJ\b[^\n。]{0,30}(?:仅保留\s*github|github\s*的留着[^\n。]{0,20}其他都删))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*按\s*RJ\s*建议仅保留\s*github[^\n]*其余[^\n]*(?:全部销毁|下线|删除))"
    r"(?=[\s\S]*(?:(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?B\b|(?:选项\s*B|B)\s*[\.、:：)\]]\s*[^\n]*[（(][^\n）)]*推荐))",
    re.I,
)
_CONTROL_PLANE_OPEN_0000_RECOMMEND_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:容器集群|K8s|Kubernetes|API\s*Server|集群|生产|Prod|安全策略|安全组|白名单))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*(?:[（(]\s*(?:推荐|建议)\s*[）)])?\s*[\.、:：)\]]\s*(?![^\n]*(?:严格锁死|只放行|仅放行|不放开|不放通|无需放开|禁止放开|仅(?:甲城\s*)?Stage|Prod[^\n]*(?:维持|走|保持)固定))[^\n]*(?:(?<!不)(?<!无需)(?<!不必)(?<!禁止)(?<!严禁)(?<!避免)(?<!拒绝)(?:放通|放开|开放|设为|改为)\s*`?0\.0\.0\.0/0`?|全网放通|不锁死动态源\s*IP|不限源\s*IP))"
    r"(?=[\s\S]*(?:(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b|(?:选项\s*A|A)\s*[（(]\s*(?:推荐|建议)\s*[）)]))",
    re.I,
)
_PROD_MOUNT_TEST_BUCKET_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:目标环境\s*[：:]\s*生产|在\s*(?:GCP\s*)?生产(?:环境)?\s*(?:ELB|LB|先?创建|配置|新建)|生产\s*(?:ELB|LB|BackendBucket)))"
    r"(?=[\s\S]*(?:--gcs-bucket-name=[a-z0-9-]+-test\b|路由至\s*`?[a-z0-9-]+-test`?\b|BackendBucket[^\n]*`?[a-z0-9-]+-test`?\b))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:同意|批准)(?:执行上述生产命令|在生产))",
    re.I,
)
_PROD_BUCKET_REPLACE_TEST_WITHOUT_REQUESTER_CONFIRM_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:测试桶|[a-z0-9-]+-test\b))"
    r"(?=[\s\S]*(?:生产一直没建|一直未建桶|一直硬编码测试桶|误用[^\n]*测试桶|纠正[^\n]*测试桶))"
    r"(?=[\s\S]*(?:新建(?:正式|专属|独立)?(?:公开)?桶|创建)[^\n]{0,24}`?(?:gs://)?[a-z0-9-]+-prod`?\b)",
    re.I,
)
_REQUESTER_CONFIRMED_PROD_BUCKET_EVIDENCE_RE = re.compile(
    r"(?:已(?:在群(?:里|内))?(?:向|跟|与|同步给)[^\n。]{0,16}(?:同步|说明|告知|需求方|业务方)[^\n。]{0,40}(?:并(?:已)?确认|已确认)|"
    r"(?:Sylvia|需求方|业务方|对方)[^\n。]{0,20}已确认[^\n。]{0,36}(?:新建|[a-z0-9-]+-prod))",
    re.I,
)
_UNVERIFIED_EXISTING_CAM_SUBUSER_ASK_RE = re.compile(
    r"\A(?=[\s\S]*\b2000\d{8}\b)"
    r"(?=[\s\S]*创建\s*(?:仅编程访问的?(?:只读)?\s*|CAM\s*)?子(?:用户|账号)\s*`?[a-z0-9_-]+`?)"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*创建[^\n]*子(?:用户|账号))",
    re.I,
)
_EXISTING_CAM_SUBUSER_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:ListUsers|ListAttachedUserAllPolicies|ListAccessKeys|GetUser|"
    r"已(?:只读)?核查[^\n。]{0,28}(?:存量|现有)(?:子用户|子账号|权限|AK)|"
    r"本来就有[^\n。]{0,16}权限|已有[^\n。]{0,16}(?:Admin|子账号|子用户))",
    re.I,
)
_UNCACHED_SLOW_QUERY_SWITCH_WITHOUT_HISTORY_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:无缓存直穿|无缓存直接查库|没走缓存|未走缓存))"
    r"(?=[\s\S]*(?:配置中心|配置中心)[\s\S]{0,160}(?:enable\s*`?\s*(?:设为|改为|=)\s*`?false|关掉[^\n。]{0,24}开关|关闭[^\n。]{0,24}开关))",
    re.I,
)
_CACHE_SWITCH_HISTORY_EVIDENCE_RE = re.compile(
    r"(?:以前(?:就|本来就)(?:有|没有)缓存|从(?:上线|最初|一开始)(?:起)?就(?:没有|未加)缓存|"
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}月[0-9]{1,2}日)[^\n。]{0,36}(?:关闭|打开|开启|去掉|引入|上线)(?:了)?(?:缓存|该开关|开关)|"
    r"git\s+(?:blame|log)|commit\s+[0-9a-f]{7,40})",
    re.I,
)
_MIRROR_MIRROR_REPO_READONLY_OVER_GITHUB_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*MIRROR[^\n]*(?:授予|开通|添加)[^\n]*(?:只读|Reporter|权限))"
    r"(?=[\s\S]*(?:选项\s*B|B)\s*[\.、:：)\]]\s*[^\n]*(?:引导走\s*GitHub|代码主仓在\s*GitHub[^\n]*无需另开\s*MIRROR\s*权限))"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_PROD_PROXY_CIDR_CONVERGE_WITHOUT_OBSERVE_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:请示生产|生产环境|acmelab-000002)[\s\S]{0,160}(?:TRUSTED_PROXY_CIDRS|APP_TRUSTED_PROXY_CIDRS|可信代理\s*CIDR)[\s\S]{0,120}(?:收敛|删(?:测试|冗余|除)?|更新))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:同意现在执行|同意立即执行)[^\n]*(?:TRUSTED_PROXY_CIDRS|可信代理\s*CIDR))",
    re.I,
)
_POST_EXEC_UPSTREAM_DOWNSTREAM_OBSERVATION_EVIDENCE_RE = re.compile(
    r"(?:(?:执行完|变更后|更新后|切流后)[^\n。；]{0,24}(?:完整观察|持续观察|观察|盯盘|核查)[^\n。；]{0,36}(?:上下游|调用链|业务日志|4\d\d|5\d\d)|"
    r"(?:观察|核查|验证)[^\n。；]{0,20}上下游[^\n。；]{0,20}(?:是否受(?:到)?影响|影响|异常|报错))",
    re.I,
)
_PROD_DB_DYNAMIC_PUBLIC_IP_WHITELIST_REJECT_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,40}(?:ADB|托管数据库|PG|PostgreSQL|MySQL|数据库)[^\n。；]{0,30}白名单)"
    r"(?=[\s\S]*(?:动态公网\s*IP|动态\s*IP\s*池|Fastly|共享公网出口|会变的地址池))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*拒绝[^\n]*向动态公网\s*IP\s*开放生产数据库白名单)"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_JUMPBOX_SG_SINGLE_IP_WHITELIST_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:跳板机|bastion|ins-[a-z0-9]+|安全组|sg-[a-z0-9]+)[\s\S]{0,160}(?:加白|放行|白名单|22\s*端口))"
    r"(?=[\s\S]*(?:专线(?:新)?出口|公司专线|办公网|VPS(?:\s*出口)?\s*IP|\b192\.0\.2\.\d+\b))"
    r"(?=[\s\S]*(?:(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:增补放行|放行|加白)\s*`?\d{1,3}(?:\.\d{1,3}){3}(?:/32)?`?(?!\s*[、,，和与及])|仅放行(?:公司专线)?新出口\s*IP))",
    re.I,
)
_FULL_COMPANY_VPS_IP_FLEET_WHITELIST_EVIDENCE_RE = re.compile(
    r"(?:(?:所有|全部|全量|全套)[^\n。；]{0,20}(?:公司\s*IP|办公网\s*IP|专线(?:\s*出口)?\s*IP|VPS\s*(?:出口\s*)?IP)|"
    r"tp_net-ops|node_allowlist\.py|(?:1[0-9]|[2-9]\d)\s*(?:个|台|条)\s*(?:公司|专线|VPS)\s*(?:出口\s*)?IP)",
    re.I,
)
_CICD_PIPELINE_WITHOUT_DEVOPS_SPEC_OR_DETAIL_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:搭建[^\n。；]{0,24}(?:双端)?(?:自动)?发布流水线|申请[^\n。；]{0,24}流水线与权限))"
    r"(?=[\s\S]*(?:mirror\.build[^\n。；]{0,20}(?:镜像仓库|建仓)[^\n。；]{0,16}(?:令牌|push\s*令牌)|生产[^\n。；]{0,24}(?:改为\s*main\s*合并自动发|稳定后再(?:全量)?放开全自动)))",
    re.I,
)
_DEVOPS_FLOW_SPEC_AND_ARCH_DETAIL_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:GitHub\s*唯一提交源|sync-to-mirror|只读镜像仓|严禁直接(?:向\s*mirror|push)))"
    r"(?=[\s\S]*(?:Release\s*(?:Tag|锚定)|v\d+\.\d+|生产(?:严禁|不开放)\s*main\s*自动))",
    re.I,
)
_PROD_DB_ROLE_LIMIT_BYPASS_DB_OWNER_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:ALTER\s+(?:ROLE|USER|SYSTEM)\b|CONNECTION\s+LIMIT\s+\d+|(?:Postgres|PG|Cloud\s*SQL|数据库)[^\n。；]{0,28}角色连接(?:数)?(?:上限|限制|配额)))"
    r"(?=[\s\S]*(?:(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:登录\s*(?:Cloud\s*SQL|Postgres|PG|生产库)|直接执行)\s*[^\n]*`?ALTER\s+(?:ROLE|USER|SYSTEM)\b|"
    r"(?:选项\s*B|B)\s*[\.、:：)\]]\s*[^\n]*找\s*Hazel(?:\s*老师)?\s*执行))",
    re.I,
)
_DB_OWNER_OPS_GROUP_SYNCED_EVIDENCE_RE = re.compile(
    r"(?:已(?:在运维群|在群(?:里|内)|私聊)?(?:跟|向|与|找|@)\s*Hazel(?:\s*老师)?[^\n。；]{0,24}(?:同步|说明|确认|沟通|执行)|"
    r"(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:先)?(?:在运维群)?(?:跟|找|请|由)\s*Hazel(?:\s*老师)?)",
    re.I,
)
_VIP_RELEASE_TICKET_SCOPE_OR_SECRET_DIFF_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:单据|发版单|审批单|202\d{9}))"
    r"(?=[\s\S]*(?:Darcy|👑\s*VIP|VIP\s*提单|提单人|发起人))"
    r"(?=[\s\S]*(?:请确认\s*Secret\s*Diff|单据备注\s*`?仅发|仅发\s*`?[a-z0-9_-]+\s*(?:与|和|\+)\s*`?[a-z0-9_-]+`?\s*[，,]\s*不动|标准\s*Trigger\s*(?:`?[a-z0-9-]+`?\s*)?内置了\s*`?Deploy\s+Extra|连带更新\s*`?svc-infer))",
    re.I,
)
_SUBMITTER_SCOPE_OR_DIFF_CONFIRMED_EVIDENCE_RE = re.compile(
    r"(?:(?:已(?:在(?:发版群|群(?:里|内)|审批单|话题)|私聊)?(?:找|向|跟|与|请|@)\s*(?:Darcy|提单人|发起人)[^\n。；]{0,28}(?:确认|核实|对齐|拍板))"
    r"|(?:(?:Darcy|提单人|发起人)\s*(?:已|已经)(?:在群里|回复)?(?:确认|同意|核实|答复)))",
    re.I,
)
_ENDPOINT_DOMAIN_SWITCH_WITHOUT_CALLER_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:[A-Z0-9_]+_(?:URL|ENDPOINT|HOST|DOMAIN)(?:_[A-Z0-9_]+)?|默认(?:语音|接口|服务)?地址))"
    r"(?=[\s\S]*(?:改为|切(?:换)?(?:至|到|为)|->|→)\s*[^\n。；]{0,48}\.(?:media|tv|plus|ai)\b)"
    r"(?=[\s\S]*(?:国内|境内|大陆)[^\n。；]{0,32}(?:直连|连通性|无法直连|访问|被墙|超时))",
    re.I,
)
_ENDPOINT_CALLER_TOPOLOGY_VERIFIED_EVIDENCE_RE = re.compile(
    r"(?:(?:调用方|谁调用|发起调用)[^\n。；]{0,36}(?:前端|浏览器|客户端|用户端|后端|服务端|微服务)|"
    r"(?:前端|浏览器|客户端|用户端)[^\n。；]{0,28}(?:直连(?:调用)?|直接(?:发起)?调用|按住说话)|"
    r"不是后端(?:微服务|服务)?调用)",
    re.I,
)
_CLIENT_DIRECT_SINGLE_DOMAIN_SWITCH_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:(?:浏览器|前端|用户浏览器)[^\n。；]{0,24}直连|客户端直连))"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:环境变量|默认(?:语音|接口|服务)?地址|[A-Z0-9_]*URL[A-Z0-9_]*)[^\n]*(?:改为|切(?:换)?(?:至|到|为)|改成)\s*(?:`?https?://[a-z0-9.-]+)?`?\.(?:media|tv)\b)",
    re.I,
)
_CLIENT_DIRECT_DUAL_ORIGIN_ROUTING_EVIDENCE_RE = re.compile(
    r"(?:(?:提供(?:好)?|同时提供|保留)(?:两种|双)(?:访问)?(?:途径|域名|通道|入口)|"
    r"(?:app\.site-c\.example|\.media)\s*进来[^\n。；]{0,48}(?:site-c\.example|\.media)[^\n。；]{0,48}(?:app\.site-b\.example|\.ai)\s*进来|"
    r"(?:客户端|前端)(?:根据|按)?(?:入口域名|访问域名)?判断[^\n。；]{0,36}(?:可配置)?构建时环境变量)",
    re.I,
)
_MIRROR_CONSOLE_CREATE_REPO_ON_NOPERM_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:\bmirror\.example\b|(?<![A-Za-z0-9_])mirror(?![A-Za-z0-9_])|mirror_cli))"
    r"(?=[\s\S]*(?:NOPERM|无建仓权限|权限不足|无法新建|创建镜像仓库|新建\s*(?:MIRROR\s*)?(?:镜像)?仓库))"
    r"(?=[\s\S]*(?:(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*(?:请\s*(?:Matthew|主人|你|您)|登录\s*MIRROR\s*网页控制台|手动新建|提供[^\n]*拥有建仓权限的\s*MIRROR\s*PAT)|"
    r"无法代建\s*MIRROR\s*仓库))",
    re.I,
)
_MIRROR_OPENAPI_PAT_CHECK_EVIDENCE_RE = re.compile(
    r"(?:api\.mirror\.example/[A-Za-z0-9_-]+/-/repos|batch_create_mirror_repos\.py|mirror_repos_report\.json|repo-scope:rw[^\n。；]{0,36}(?:已实测|403|401|失效|过期))",
    re.I,
)
_ONEOFF_EXEMPTION_OVER_CLASS_WHITELIST_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:加入[^\n。；]{0,20}(?:封版|高峰期)[^\n。；]{0,16}(?:豁免)?白名单|豁免白名单))"
    r"(?=[\s\S]*(?:离线|异步|worker)[^\n。；]{0,24}不影响\s*C\s*端)"
    r"(?=[\s\S]*(?:选项\s*A|A)\s*[\.、:：)\]]\s*[^\n]*仅本次特批放行[^\n]*长期规则维持现状)"
    r"(?=[\s\S]*(?:选项\s*B|B)\s*[\.、:：)\]]\s*[^\n]*同意纳入白名单)"
    r"(?=[\s\S]*(?:建议|推荐)\s*[：:]?\s*(?:优先)?(?:选\s*)?A\b)",
    re.I,
)
_REPORT_REVISES_OPEN_ASK_OPTION_RE = re.compile(
    r"\A(?=[\s\S]*(?:选项\s*[A-E][^\n。；]{0,28}(?:已(?:被)?排除|不可行|不适用)|原请示卡片))"
    r"(?=[\s\S]*(?:(?:建议|请示|补充请示)(?:改为|改)?(?:批准|改选|选|选择)(?:原请示卡片的)?\s*(?:\*\*|__)?选项\s*[A-E]\b"
    r"|唯一[^\n。；]{0,24}(?:可执行|可行)路径为\s*(?:\*\*|__)?选项\s*[A-E]\b))",
    re.I,
)
_LOOP_BROADCAST_PAUSE_FB_RE = re.compile(
    r"(?:暂时不同步|暂不同步|取消(?:本次)?播报|本次不发群|不自动发日清播报)",
    re.I,
)


def _has_prior_loop_broadcast_pause_feedback(
    cfg: Config,
    store: Store,
    topic_id: str,
    key: str = "",
) -> bool:
    """REG-151：检查当前播报请示关联的 Loop（通过 key 或 topic_id）是否已有主人『暂时不同步 / 这次播报取消』历史反馈。"""
    loop_ids: list[str] = []
    k = (key or "").strip()
    if k.lower().startswith("loop-"):
        lid = k[5:].strip()
        if lid:
            loop_ids.append(lid)
    if topic_id == "tp_migrate-ops" and "env-migration-daily-steward" not in loop_ids:
        loop_ids.append("env-migration-daily-steward")
    if getattr(store, "conn", None) is not None and loop_ids:
        try:
            placeholders = ",".join("?" for _ in loop_ids)
            rows = store.conn.execute(
                f"SELECT human_feedback FROM loop_runs WHERE loop_id IN ({placeholders}) AND human_feedback IS NOT NULL AND human_feedback != ''",
                tuple(loop_ids),
            ).fetchall()
            for r in rows:
                if _LOOP_BROADCAST_PAUSE_FB_RE.search(str(r["human_feedback"] or "")):
                    return True
        except Exception:
            pass
    for lid in loop_ids:
        try:
            mem_file = cfg.loops_dir / lid / "MEMORY.md"
            if mem_file.is_file() and _LOOP_BROADCAST_PAUSE_FB_RE.search(mem_file.read_text(encoding="utf-8")):
                return True
        except Exception:
            pass
    return False
_PROD_LB_INGRESS_OWNER_WINDOW_CHECKED_OR_PLANNED_RE = re.compile(
    r"(?:(?:已|先)(?:向|找|跟|与|联系)[^\n。；]{0,32}(?:Rui|瑞松|业务(?:方|负责人)|研发负责人|服务负责人)[^\n。；]{0,32}(?:确认|询问)[^\n。；]{0,24}(?:能不能动|能动|可否操作|操作窗口|可动|无影响)|"
    r"(?:Rui|瑞松|业务负责人|服务负责人)[^\n。；]{0,24}(?:已确认|确认现在能动|同意当前窗口))",
    re.I,
)
_PROD_GACC_LISTENER_MODIFY_WITHOUT_FULL_FIELDS_AND_OPTION_EXPLAIN_ASK_RE = re.compile(
    r"\A(?=[\s\S]*生产[^\n。；]{0,40}(?:GACC|ga-[a-z0-9]+|监听器|lsr-[a-z0-9]+))"
    r"(?=[\s\S]*(?:UpdateListener|RealIpPassthrough))",
    re.I,
)
_PROD_GACC_LISTENER_FULL_FIELDS_AND_OPTION_EXPLAIN_EVIDENCE_RE = re.compile(
    r"\A(?=[\s\S]*(?:具体开哪个选项|七层获取源\s*IP|获取客户端真实\s*IP|控制台[^\n。；]{0,20}选项))"
    r"(?=[\s\S]*(?:--IdleTimeout|--ServerCertificates|锁定全部(?:现有)?(?:监听器)?(?:参数|原字段)|显式(?:带上|锁死)全部原字段))",
    re.I,
)
_DISSOLVED_FE_BE_PR_REVIEW_GROUP_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:示例评审群|pr-review-guide))"
    r"(?=[\s\S]*(?:pr-review-guide|4\s*行|四行|PR\s*Review\s*消息|以(?:你|主人)(?:的)?身份))"
    r"(?=[\s\S]*(?:A|选项\s*A)(?:\s*[（(][^)）]{0,12}[)）])?\s*[\.、:：)\]]\s*[^\n]*(?:发送|发往|发至|发到|提交|同步|发一条|发(?=[「『\s]*`?示例))[^\n]*(?:示例评审群|PR\s*Review\s*消息))"
    r"(?![\s\S]*(?:已解散|chat_status\s*:\s*dissolved|群已解散))",
    re.I,
)
_UNREVIEWED_PR_DIRECT_MERGE_WITHOUT_PEER_AGENT_CHECK_ASK_RE = re.compile(
    r"\A(?=[\s\S]*(?:PR\s*#\d+|pull/\d+|[a-z0-9_-]+#\d+))"
    r"(?=[\s\S]*(?:人工\s*(?:Code\s*)?Review\s*(?:状态\s*(?:为|:|：)?\s*)?`?0/1|评审群已解散|chat_status:\s*dissolved|尚未经过人工\s*Review))"
    r"(?=[\s\S]*(?:A|选项\s*A)(?:\s*[（(][^)）]{0,12}[)）])?\s*[\.、:：)\]]\s*[^\n]*(?:你点\s*A\s*(?:即)?视同|(?:直接)?作为\s*Reviewer\s*放行|(?:直接|立即)执行\s*`?gh\s+pr\s+merge|直接合入\s*#?\d+))",
    re.I,
)
_PEER_OR_AWEI_AGENT_PITFALL_CHECKED_EVIDENCE_RE = re.compile(
    r"(?:(?:已|先)(?:跟|找|向|与)[^\n。；]{0,28}(?:阿魏(?:的)?\s*agent|阿魏\s*Lark\s*cli|tp_awei|模块负责人|Owner)[^\n。；]{0,28}(?:碰一下|核对|确认|排查)[^\n。；]{0,24}(?:坑|暗雷|风险|没问题)|"
    r"(?:阿魏(?:的)?\s*agent|阿魏\s*Lark\s*cli|tp_awei)[^\n。；]{0,28}(?:已确认|回复|无暗雷|没坑))",
    re.I,
)
_REPLY_ADDRESSES_OWNER_AS_BOSS_RE = re.compile(
    r"(?:老板\s*(?:已|刚|刚才|这边|那边)?\s*(?:批准|同意|拍板|确认|回复|指示|决定|定了|批复|选了|签字)|"
    r"(?:已|正|先|去|待|等|向|找|发给|请|跟|由)\s*老板\s*(?:确认|拍板|审批|请示|定夺|定|汇报|对齐|批复|同意|批准)|"
    r"(?:收到|好的|好嘞|明白|ok)\s*[，,、!！~～]*\s*老板)",
    re.I,
)


def check_source_peer_followup_and_proxy_guard(
    cfg: Config,
    store: Store,
    topic_id: str,
    title: str,
    body_or_combined: str,
    *,
    key: str = "",
    about: list[str] | None = None,
    batch: str = "",
    kind: str = "ask",
    ball: str = "",
    fyi: bool = False,
) -> None:
    """1.1.38 (REG-127): 发送 ask 或 report(--ball owner) 前，核查：
    1) 账本里原群来源消息下是否已有群成员/VIP（如 Darcy Vale / Darci）回复『不在我们管的账号里 / 外部团队开的 / 已处理』，
       而当前外发仍无视该回复让主人去登录控制台/充值；
    2) 是否在未核实第三方代理账号（如 ResiProxy `proxyacct*`）是否属于我方纳管账号（`acmeco / acmeco_test / acmeco02 / acmeco0001`）
       的情况下，直接要求主人登录控制台充值续订。"""
    if fyi or topic_id in (BUS_TOPIC_ID, "tp_net-ops"):
        return
    full_text = f"{title or ''}\n{body_or_combined or ''}"
    is_owner_action_request = kind == ASK or (kind == REPORT and (ball or "").strip().lower() == "owner")
    if getattr(store, "conn", None) is not None and (is_owner_action_request or _STILL_ASKING_OWNER_ACTION_RE.search(full_text)):
        try:
            cand_srcs: list[tuple[str, str]] = []
            try:
                _, a_srcs, _ = ask_sources(cfg, store, topic_id, about, batch)
                for smid, schat in a_srcs:
                    if smid and schat and (smid, schat) not in cand_srcs:
                        cand_srcs.append((smid, schat))
            except Exception:
                pass
            nk = duty.norm_key(key) if key else ""
            esc_rows = store.conn.execute(
                "SELECT e.message_id FROM escalations e LEFT JOIN sends s ON s.message_id = e.message_id "
                "WHERE e.topic_id = ? AND (? = '' OR s.norm_key = ? OR s.key = ?) "
                "ORDER BY e.asked_at DESC LIMIT 5",
                (topic_id, nk, nk, key),
            ).fetchall()
            for er in esc_rows:
                for es in store.escalation_sources(er["message_id"]):
                    smid, schat = (es["about_message_id"] or "").strip(), (es["about_chat_id"] or "").strip()
                    if smid and schat and (smid, schat) not in cand_srcs:
                        cand_srcs.append((smid, schat))
            me = cfg.self_open_id
            for src_mid, src_chat in cand_srcs:
                src_row = store.get_message(src_mid)
                if src_row is None or (src_row["chat_type"] or "") == "p2p":
                    continue
                src_sender = ((src_row["sender_id"] if "sender_id" in src_row.keys() else "") or "").strip()
                tid = ((src_row["thread_id"] if "thread_id" in src_row.keys() else "") or "").strip()
                p_rows = store.conn.execute(
                    "SELECT message_id, sender_name, sender_id, create_time, content FROM messages "
                    "WHERE chat_id=? AND COALESCE(deleted,0)=0 AND message_id != ? AND sender_id != ? "
                    "AND (? = '' OR sender_id != ?) AND COALESCE(sender_type, '') NOT IN ('app', 'bot', 'system') "
                    "AND (reply_to=? OR root_id=? OR (? != '' AND thread_id=?)) "
                    "ORDER BY create_time ASC, rowid ASC LIMIT 8",
                    (src_chat, src_mid, me, src_sender, src_sender, src_mid, src_mid, tid, tid),
                ).fetchall()
                matched_lines: list[str] = []
                for pr in p_rows:
                    raw_p = str(pr["content"] or "")
                    ptxt = " ".join(raw_p.split())
                    if not ptxt or not _PEER_SOURCE_HANDLED_OR_NOT_OURS_RE.search(ptxt):
                        continue
                    if pr["message_id"] in full_text or _EXTERNAL_PROXY_OWNERSHIP_VERIFIED_EVIDENCE_RE.search(full_text):
                        continue
                    head_p = " ".join(re.split(r"▶\s*详情", raw_p, maxsplit=1)[0].split())
                    p_who = (pr["sender_name"] or pr["sender_id"] or "群内同事").strip()
                    matched_lines.append(
                        f"- {p_who}（{pr['create_time'] or '-'}，`{pr['message_id']}`）：「{(head_p or ptxt)[:180]}」"
                    )
                if matched_lines and (_STILL_ASKING_OWNER_ACTION_RE.search(full_text) or is_owner_action_request):
                    raise OutboundError(
                        EXIT_INVALID,
                        f"{kind} 拒绝（REG-127 原群来源消息已有同事跟进/澄清防盲问门禁）："
                        f"账本显示原群来源消息（`{src_mid}`）下已有同事回复：\n"
                        + "\n".join(matched_lines)
                        + "\n（主人裁定）：\n"
                        "请先读取并依据上述群内最新回复处理（如已无需主人操作则调 `sheepdog resolve` 结案或如实汇报『Darcy 已在群内确认非我方纳管账号，无需主人处理 --ball done』），"
                        "严禁无视群内已有的同源澄清继续要求主人登录控制台或充值（没有发送）",
                    )
        except OutboundError:
            raise
        except Exception:
            pass
    if (
        is_owner_action_request
        and _UNVERIFIED_EXTERNAL_PROXY_CONSOLE_RECHARGE_ASK_RE.search(full_text)
        and not _EXTERNAL_PROXY_OWNERSHIP_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            f"{kind} 拒绝（REG-127 第三方代理/控制台充值前必核账号归属与群内回复门禁）："
            "请示或要求主人/Darcy 登录第三方代理控制台（如 ResiProxy `resi-proxy.example`）确认余额并充值续订（如 `proxyacct*` 等账号）前，"
            "必须先核实该账号是否属于我方运维纳管账号（我方仅纳管 `acmeco / acmeco_test / acmeco02 / acmeco0001` 等账号，`proxyacct*` 等外部团队自开账号不在我方控制台内），"
            "并先核查原群来源消息下是否已有 Darcy / Darci 或同事回复澄清（主人裁定）；"
            "若非我方纳管账号或群内已澄清，请勿发卡让主人登录充值（没有发送）",
        )


def _has_live_p2p_owner_conversation(
    cfg: Config,
    store: Store,
    topic_id: str,
    *,
    about: list[str] | None = None,
    batch: str = "",
) -> bool:
    """REG-123：检测当前 ask 的来源私聊（p2p）中，主人本人是否已经在来源消息之后（或近 2 分钟内）直接回复了对方（owner_context）。"""
    if getattr(store, "conn", None) is None or not cfg.self_open_id:
        return False
    try:
        from . import pending as pend
        _, srcs, _ = ask_sources(cfg, store, topic_id, about, batch)
        for src_mid, src_chat in srcs:
            if not src_mid or not src_chat:
                continue
            src_row = store.get_message(src_mid)
            if src_row is None or (src_row["chat_type"] or "") != "p2p":
                continue
            src_ct = _parse(src_row["create_time"] or "")
            src_fs = _parse(src_row["first_seen"] or "") if "first_seen" in src_row.keys() else None
            rows = store.conn.execute(
                """
                SELECT message_id, create_time, first_seen, content, reply_to
                FROM messages
                WHERE chat_id = ?
                  AND sender_id = ?
                  AND reason = 'owner_context'
                  AND deleted = 0
                ORDER BY first_seen DESC, rowid DESC
                LIMIT 5
                """,
                (src_chat, cfg.self_open_id),
            ).fetchall()
            for r in rows:
                txt = str(r["content"] or "")
                if not txt.strip() or pend.is_agent_text(cfg, txt):
                    continue
                if (r["reply_to"] or "") == src_mid:
                    return True
                r_ct = _parse(r["create_time"] or "")
                r_fs = _parse(r["first_seen"] or "")
                if src_fs is not None and r_fs is not None and r_fs >= src_fs:
                    return True
                if src_ct is not None and r_ct is not None and r_ct > src_ct:
                    return True
    except Exception:
        return False
    return False


def check_unverified_ticket_promise_guard(text: str) -> None:
    """REG-099：反空头安抚支票 / 严禁无实据承诺正在跟进云厂商工单门禁。"""
    body = text or ""
    m = _UNVERIFIED_TICKET_PROMISE_RE.search(body)
    if m and not _TICKET_NEGATION_OR_VERIFIED_ID_RE.search(body):
        snippet = " ".join(m.group(0).split())[:80]
        raise OutboundError(
            EXIT_INVALID,
            f"reply 拒绝（REG-099 反空头安抚支票门禁）：检测到对外回复宣称『{snippet}』，但正文中既无真实可核查的工单号/单据 ID（如 `工单号: 2026...`），"
            "也未如实说明『实际未提云厂商工单』（曾导致同事反复追问工单进展而 Matthew 本人毫不知情，引发人机协作信任摩擦）！\n"
            "请：① 若实际未提单或未查到确切工单号，如实写明『目前未提云厂商工单 / 尚无确切工单号』，并直接给出代码侧/架构侧立即可执行的降级绕过方案；"
            "② 若已有真实工单，请显式附上真实工单号（`工单号：xxx`）；"
            "③ 若确需 Matthew 人工找云厂商提单或催办，必须同步通过 `sheepdog ask/report` 明确通知 Matthew 本人（没有发送）。",
        )


def check_ask_pre_flight_guard(
    cfg: Config,
    store: Store,
    topic_id: str,
    title: str,
    body: str,
    *,
    key: str = "",
    floor: str = "",
    floor_type: str | None = None,
    options: str = "",
    about: list[str] | None = None,
    batch: str = "",
) -> None:
    """7.108 / 7.110 / 7.113 / 7.114 / 7.115 / 7.116 / 7.117 / 7.118（REG-076 ~ REG-086）：从人类改判错题本自进化提炼的 ask 发前防打扰门禁：
    0. 发起待拍板 ask 卡片（含写明『建议选 A』或启用聚合台/白话总结折叠模式）时，必须提供至少 2 个可解析的 A/B 互斥选项，严禁发无选项按钮的空头决策卡；
    1. 申请 CC 专线 / 家宽节点 / 住宅 IP 前，必须先向 tp_net-ops 核实各线路当前人数与空余席位并写入正文；
    2. 申请 实时音视频 凭据或子成员账号时，必须显式标明『明确是测试环境 / 明确是生产环境 / 目标环境：...』；
    3. 同一 (topic_id, key) 在途未消费主人刚给出的批复、主人已批复『这个不动/忽略』、或近 2 小时已点击选项 A 批准由专席执行时，拦截重复 ask；
    4. 请示执行 gh pr merge 或直接合入 PR 打 Tag 发布生产前，必须显式写明『人工 Code Review 状态』（如 reviewDecision: APPROVED 或人工 Review 0/N 未过）；
    5. 请示确认 CORS / Origin 域名白名单或『是否有其他前端域名调用』前，必须先扫描云上真实流量日志 + 本地记录并与技术负责人（如 tp_awei）核对；
    6. 请示发版单『测试审批是否可免测直接放行还是先走 QA』前，必须先与该单技术负责人（如 tp_awei）核对；
    7. 测试环境（acme-testing-env）跳板机 / allow-demo-office 白名单追加属于预授权非破坏性测试环境操作，拦截向主人 ask 请示；
    8. 测试环境（acme-testing-env）为内部成员或服务账号授予非破坏性 IAM 角色（如 roles/iam.serviceAccountTokenCreator、secretAccessor）属于预授权测试环境操作，拦截向主人 ask 请示；
    9. 因腾讯云子账号权限不足（UnauthorizedOperation）请示主人登录控制台创建 API Key / 资源前，必须先核查本机 ~/.cloudcli/ 下的主账号凭据（如 cloud-a.credential）；
    10. 内部同学因切换办公专线 / VPS 出口 IP 申请数据库白名单（托管数据库 / 托管数据库 / ADB / RDS）前，必须先向 tp_net-ops 拉齐整套 VPS 出口 IP 清单并协调 Hazel 老师（tp_hazel）全量数据库一次性加白，严禁按单人单 IP 散装打扰；
    11. 阿魏（awei / Owen Wei）的需求（含助理代发待阿魏本人确认）属于 VIP 预授权范围（『awei 的需求 只要经过他本人确认 你就给他弄』），先向阿魏本人确认后直接执行并 report，严禁未触及 3 条底线就向主人 ask；
    12. 常规发版排期/窗口报备（PR 仍在等待他人 Review 合入、无架构/封网冲突，选项 B 为『调整排期时间或有其他要求』）没有实质要定的点（『这玩意有啥决策点吗 还需要问我』），拦截象征性 ask；
    13. 申请数据库内部 User / 只读账号密码与连接串（含尚未入 DBA 台账的新库）统一归口 Hazel 老师（tp_hazel）纳管发放（『如果是数据库里面的 User 账号密码什么的，那就找 Hazel』），拦截向主人 ask。"""
    from . import decision_hub
    from .card_callback import _DISABLED_SPECS, _MORE_LINE_RE, parse_ask_options

    full_text = f"{title}\n{body}"
    opt_spec = (options or "").strip()
    if opt_spec.lower() not in _DISABLED_SPECS:
        parsed_opts = parse_ask_options(full_text, opt_spec)
        needs_explicit_opts = bool(
            _ASK_CLAIMS_RECOMMENDED_OPTION_RE.search(full_text)
            or (decision_hub.hub_active(cfg, store) and (cfg.notify.require_summary or _MORE_LINE_RE.search(full_text)))
        )
        if needs_explicit_opts and len(parsed_opts) < 2:
            raise OutboundError(
                EXIT_INVALID,
                "sheepdog ask 缺少可点击的 A/B 决策选项（主人批评：『这里怎么没给我选项』，REG-083 防缺选项门禁）："
                "发起待拍板决策卡时，必须在正文中显式列出至少 2 个互斥选项（如 `A. 同意...\\nB. 暂缓...` 或 `选项：A. ...；B. ...`），"
                "或传 `--options \"A:方案一|B:方案二\"`，严禁只写『建议选 A：...』却不提供 A/B 选项列表导致飞书卡片无按钮！（没有发送）",
            )
        visible_head = _MORE_LINE_RE.split(full_text, maxsplit=1)[0]
        if _RAW_STDOUT_IN_ASK_OPTION_RE.search(visible_head) or any(
            _RAW_STDOUT_IN_ASK_OPTION_RE.search(f"{o.label} {o.reply_text}") for o in parsed_opts
        ):
            raise OutboundError(
                EXIT_INVALID,
                "sheepdog ask 首屏或选项 A/B/C 中严禁直接粘贴原始命令执行日志/报错输出（如 `[PROD] Status: FAILED/SUCCESS`、`ERROR: column ... does not exist`、`deployment ... successfully rolled out` 等，REG-112 选项与首屏纯净度门禁）："
                "请把现场命令输出与报错堆栈放入 `---more---` 折叠详情区，首屏与选项 A/B/C 仅保留干净、互斥的决策动作文案后再发起 ask（没有发送）",
            )
        if any(
            re.search(
                r"由\s*(?:你|您|主人|Matthew)(?:本人)?(?:亲自|手动)\s*或\s*(?:由?(?:专席|我们|我|agent)\s*)?授权"
                r"|由\s*(?:你|您|主人|Matthew)(?:本人)?(?:亲自|手动)(?:私信|发送|外发|发给|交付)",
                f"{o.label} {o.reply_text}",
                re.I,
            )
            for o in parsed_opts
        ):
            raise OutboundError(
                EXIT_INVALID,
                "sheepdog ask 选项中严禁混写『由你亲自或授权…』或把发送/交付动作推给主人亲自操作"
                "（主人裁定，Guard 11 / REG-146 选项执行边界门禁）："
                "主人拍板授权后一律由专席代为直接执行交付，请将选项改为清晰单一的『同意破例授权由专席直接私信发送…』后再发起 ask（没有发送）",
            )
        if (
            re.search(
                r"(?:由\s*(?:你|您|主人|Matthew)(?:本人)?\s*为\s*[^\n。；]{1,20}(?:添加|开通|授予)\s*[^\n。；]{1,36}仓库(?:协作者)?权限"
                r"|执行\s*[：:]\s*(?:你|您|主人|Matthew)(?:本人)?\s*(?:确认后)?在仓库后台添加协作者权限)",
                full_text,
                re.I,
            )
            and not re.search(
                r"(?:gh\s+api[^\n]{0,60}(?:403|404|Must have admin rights|无\s*Admin\s*权限)|本机\s*`?gh`?\s*(?:CLI\s*)?(?:实测)?无(?:仓库)?\s*Admin\s*权限)",
                full_text,
                re.I,
            )
        ):
            raise OutboundError(
                EXIT_INVALID,
                "sheepdog ask 严禁把开通代码仓库协作者权限的操作推给主人手工去仓库后台点（如『由 Matthew 为 Nora 添加 svc-files-v2 仓库权限 / 执行：Matthew 确认后在仓库后台添加协作者权限』）"
                "（主人裁定，Guard 11 / REG-162 仓库权限由专席直接调用 gh CLI 开通门禁）："
                "本机已登录 `gh` CLI，请直接调用 `gh api repos/<org>/<repo>/collaborators/<username> -X PUT -f permission=push` 为同事开通并在完成后 `sheepdog report` 报备；"
                "若确需事先请示，选项 A 与 `执行：` 必须写『同意开通，授权由专席直接调用 gh api 为其添加仓库权限』（除非已实测 `gh api` 返回 `403 Must have admin rights` 并在正文里注明）（没有发送）",
            )
        if _WRITE_PERM_ASK_RE.search(full_text) and len(parsed_opts) >= 2:
            opt_texts = [f"{o.key}. {o.label} {o.reply_text}" for o in parsed_opts]
            if all(_REFUSAL_OR_RESTRICT_OPT_RE.search(t) for t in opt_texts) and not any(
                _GRANT_WRITE_PERM_OPT_RE.search(t) for t in opt_texts
            ):
                raise OutboundError(
                    EXIT_INVALID,
                    "sheepdog ask 权限申请选项缺失正向授权分支（REG-099 防单向全拒绝假选择题门禁）："
                    "当前请示为同事申请修改/读写权限，但提供的选项（A/B/C）全部为『拒绝/仅开只读/暂缓』，没有任何一个选项代表『同意授权/按申请开通』，"
                    "迫使主人只能在『其他』里手动输入『给他授权』！\n"
                    "即使你建议选 A（拒绝直接开放个人写权限、由 SRE 代跑），也必须在选项 B 或 C 中显式提供一个『同意授权开通所申请权限（配齐前置快照/水位注意事项）』的正向按钮选项，"
                    "让主人能一键拍板（没有发送）。",
                )
    if _VIP_LOW_PERM_OR_PROXY_INSTEAD_OF_E2E_ASK_RE.search(full_text) and not _VIP_HIGH_PERM_E2E_RECOMMEND_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "VIP（阿魏 / Owen Wei、Darcy Vale）申请云账号 / CAM 子账号 / 容器集群 `kubeconfig` 等高权限以推进业务部署时，"
            "严禁把『A. 不外发云凭据/kubeconfig，由专席直接代建并仅交付后台只读 Key』作为首选建议，或仅提供单命名空间受限窄权限"
            "（主人裁定，REG-120 VIP 高权限端到端闭环门禁）："
            "请将『为阿魏/VIP 开通高权限（如高权限 CAM 子账号与完整集群 kubeconfig），支持其端到端自主完成部署与验证』作为首选建议（建议选 A）后再发起 `sheepdog ask`（没有发送）",
        )
    if _SINGLE_API_SUBPATH_CACHE_OR_CORS_PATCH_ASK_RE.search(full_text) and not _CLASS_WIDE_API_CACHE_AUDIT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对 CDN / CDN 出现 API 接口缓存或 CORS 串域（如未区分 `Origin` 或误缓存动态接口）发起请示时，"
            "严禁只针对报错的那单个 API 子路径（如仅将 `/api/demo/items/*` 设为 `NoCache`）打局部点状补丁而不排查同类全部 API 接口"
            "（主人裁定，REG-121 同类 API 缓存全域治理门禁）："
            "请先全量盘点该站点下所有 `/api/*`（及 `/api/bff/*`）路径的缓存与 Origin 规则，在正文中写明全站同类 API 接口排查结论，并提出覆盖整类 `/api/*` 动态接口的统一 `NoCache`/Origin 缓存修复方案后再发起 `sheepdog ask`（没有发送）",
        )
    if _STAGE_PROXY_RUN_INSTEAD_OF_SELF_ACCESS_ASK_RE.search(full_text) and not _STAGE_DIRECT_SELF_ACCESS_OPTION_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部同事申请在 Stage / 测试环境机器（如 `ins-stage001`）运行评测脚本或联调任务时，测试/Stage 环境属于预授权自治与自助开放范围，"
            "严禁只提供『由专席在 Stage 机器代跑脚本』与『拒绝执行』两个选项而不给同事开通 Stage 机器自助权限"
            "（主人裁定，REG-121 Stage 机器自助权限门禁）："
            "请直接为同事配置 Stage 机器 SSH 公钥/跳板机受控登录权限让其自行运行验证（无需发 ask，完成后 `sheepdog report` 报备），或在选项 A 中明确推荐『直接为其开通 Stage 机器登录权限让其自己运行』（没有发送）",
        )
    if _DOMAIN_OWNER_E2E_WITHHOLD_DNS_EDGEONE_ASK_RE.search(full_text) and not _DOMAIN_OWNER_FULL_GRANT_RECOMMEND_A_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "业务域负责人申请本域端到端自主构建、发布与回滚权限时，严禁将选项 A 设为『把 CDN/DNS 服务 等收紧为只读或走受控变更』而把『按申请内容全部开通（含 CDN/DNS 服务 站点权限）』贬为选项 B"
            "（主人裁定，REG-121 业务域负责人端到端发版权限门禁）："
            "请将支持业务域负责人端到端闭环的『按申请内容全部开通（含 CDN/DNS 服务 站点权限）』作为首选建议（选项 A）后再发起 `sheepdog ask`（没有发送）",
        )
    if _BINARY_FULL_ACCOUNT_ADMIN_ASK_RE.search(full_text) and not _RD_SERVICE_ADMIN_VS_NETWORK_BOUNDARY_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部研发同学笼统追问『要不直接给全量的 admin 权限』时，严禁未先向其问清日常操作的资源范围与常做改动，就直接向主人抛『A. 不开全量 Admin 维持原状 vs B. 挂载生产账号全量 AdministratorAccess』的二选一拍板卡"
            "（主人裁定，REG-121 按产研高频服务端授 Admin 与网络红线切分门禁）："
            "请先向申请人问清日常资源范围与常做改动，并按『经常需要开发产研改动的服务域可授予 Admin 权限，但底层网络（VPC/负载均衡/安全组等）不开放 Admin』切分方案后再发起请示（没有发送）",
        )
    if (
        _LEGACY_TEMP_DOMAIN_REDIRECT_OVER_OFFLINE_ASK_RE.search(full_text)
        and not _LEGACY_TEMP_DOMAIN_DIRECT_OFFLINE_RECOMMEND_A_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "收口清理已完成历史使命的临时联调域名 / 过渡域名（如 `app-edge.site-a.example`）时，正式域名上线后应遵循奥卡姆剃刀『如无必要，勿增实体，能删不留』，"
            "严禁把『A. 为临时联调域名配置 302 重定向保留平行入口』作为首选建议，而把『B. 直接停用（offline）并下线该联调域名』贬为次选"
            "（主人裁定，REG-122 临时联调域名收口直接下线门禁）："
            "请将『直接停用（offline）并下线该临时联调/过渡域名』作为首选建议（选项 A）后再发起 `sheepdog ask`（没有发送）",
        )
    if _DB_OWNER_WITHHOLD_DB_PERM_ASK_RE.search(full_text) and not _DB_OWNER_GRANT_RECOMMEND_A_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "Hazel Yu（负责人）应拥有全部库的最高管理权与管理入口，严禁在涉及数据库 SQL 执行或库表权限时把『A. 不外发生产库账密，由业务/迁移专席代执行』作为首选建议，而把『B. 给 Hazel Yu 开通数据库权限/连接账号』贬为次选"
            "（主人裁定，REG-122 DBA 所有数据库最高权限门禁）："
            "请直接将给 Hazel Yu 开通/交付对应数据库的最高权限与连接凭据作为首选建议（选项 A）后再发起请示（没有发送）",
        )
    if not _P2P_OWNER_DELEGATE_ALREADY_CHECKED_EVIDENCE_RE.search(full_text) and (
        _P2P_LIVE_OWNER_CONVERSATION_OR_DELEGATE_CHECK_ASK_RE.search(full_text)
        or (
            _has_live_p2p_owner_conversation(cfg, store, topic_id, about=about, batch=batch)
            and re.search(r"(?:私聊|你方才说到的|你说的|径直在私聊里回)", full_text, re.I)
        )
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当主人正在私聊（p2p）与对方直接实时对话（如对方追问『你说「…」是否指的是…』或主人已在私聊亲自回复对方），"
            "或仅需找内部渠道/账单负责人（如 Darcy / Caleb）只读核查控制台状态时，严禁抢发『A. 由我们找负责人核查截图 vs B. 由你本人登录控制台查看（或径直在私聊里回）』的拍板卡打断主人"
            "（主人裁定，REG-123 主人私聊实时在场观望与免请示找负责人只读核查门禁）："
            "① 若主人在私聊正与对方实时沟通，请保持静默观望并直接交 `handled` / `waiting` 回执，严禁发 `sheepdog ask` 打扰主人；"
            "② 若需内部负责人（如 `tp_darcy`）协助只读核查控制台状态，请直接通过 `sheepdog tell` 协同核实，无需事先发 `sheepdog ask` 请示（没有发送）",
        )
    if _DOMESTIC_ZERO_BALANCE_LOADTEST_ACTIVATE_ASK_RE.search(full_text) and not _INTL_LOADTEST_EVALUATED_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因历史遗留的腾讯云国内主账号余额为 0 且未开通 LOADTEST 压测服务受阻时，严禁未经核查腾讯云乙站压测能力就直接请主人手工登录国内主账号控制台点开通"
            "（主人裁定，REG-125 压测优先核查腾讯云乙站门禁）："
            "请先核实能否直接使用腾讯云乙站子成员账号的 LOADTEST / 压测服务并在正文里注明乙站压测可用性评估结论后再发起请示（没有发送）",
        )
    if _COLLEAGUE_PERSONAL_CRED_ROTATE_ASK_RE.search(full_text) and not _COLLEAGUE_IDLE_CONFIRMED_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "轮换或重置内部同事个人专属运维/数据库的账号和密码（如 `hazel_yu` 等）前，严禁尚未向账号持有人确认其手头是否正在跑长耗时 SQL/迁移任务，"
            "就直接把『A. 同意立即轮换密码』作为首选建议、而把『B. 暂不轮换（先问本人手头是否正在跑长耗时 SQL，待对方确认空闲后再更换）』贬为次选"
            "（主人裁定，REG-126 更换同事私人账号前须确认其空闲的守卫）："
            "请先通过 `sheepdog reply` 或 `sheepdog tell` 向该同事本人确认当前是否正在执行长耗时任务、确认空闲可轮换并在正文里注明确认结论后再发起请示（没有发送）",
        )
    if _DUPLICATE_PURCHASE_OR_ORDER_PAY_ASK_RE.search(full_text) and not _EXISTING_PACKAGE_UNUSABLE_VERIFIED_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当主人此前已购买/开通对应资源包或按量付费（如正文已提及『已开通的按量付费 / package-...』）时，"
            "严禁未经核实已有资源包/按量付费为何无法使用，就再次向主人发卡请款支付新订单"
            "（主人裁定，REG-126 已买资源包不得重复请款的守卫）："
            "请先核实并指引需求方直接使用已购买的资源包或已开通的按量付费；若确因规格/地域/余量限制导致已购资源无法使用，必须在正文中写明『已核实...无法使用』的具体技术原因后再发起请示（没有发送）",
        )
    check_source_peer_followup_and_proxy_guard(
        cfg,
        store,
        topic_id,
        title,
        body,
        key=key,
        about=about,
        batch=batch,
        kind=ASK,
    )
    if _DB_OWNER_WITHHOLD_WAN_OR_CRED_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "Hazel Yu（负责人）拥有所有数据库与 Redis 的最高管理权限（`REG-122`：『hazel理应拥有全部库的最高管理权 给他』），"
            "当 Hazel 老师申请为已配置 28 个受信专线/DBA 白名单安全组的数据库或 Redis 开通外网连接地址并获取凭据时，"
            "严禁在请示中把『A. 保持纯内网，不开放公网端口与密码外发』作为推荐首选（`建议：选 A`），而把『B. 同意开通外网（严格限制 28 个 IP 白名单）并交付凭据』贬为次选"
            "（主人裁定，REG-127 DBA 数据库/Redis 白名单外网与凭据推荐门禁）："
            "请将『同意为 Hazel 老师开通外网连接（严格绑定 28 个受信专线 IP 白名单安全组）并交付连接凭据』作为明确推荐首选后再发起请示（没有发送）",
        )
    if _PREFER_MANUAL_TERMINAL_OVER_SESSION_SCRIPT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当专席/父会话已备妥不手写裸凭据的合规幂等收尾脚本（如走既有 SDK / 官方 CLI）且只需主人授权即可由父会话直接执行闭环时，"
            "严禁把『选项 A（推荐）：你在本机终端直接跑一遍脚本』作为首选推给主人手工跑，而把『选项 B：授权父会话直接调用该脚本完成切换』贬为次选"
            "（主人裁定，REG-127 授权会话自动执行合规脚本优先门禁）："
            "请将『授权父会话/专席直接调用合规脚本端到端完成并回读验证』作为首选建议（选项 A）后再发起请示（没有发送）",
        )
    if _VPS_RECLAIM_BACKUP_SEAT_OVER_NEW_NODE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当全网 VPS 专线节点（3/3 编制）已满编时，主人的既定策略是统一新购 VPS 节点（或合并进 `tp_net-ops` 正在推进的新机采购池），"
            "严禁将『A. 零采购回收 node6 等节点上现有同事的双占/备用席位』作为首选推荐（`建议：选 A`），而把『B. 合并进待购新机 / 新购 VPS』贬为次选"
            "（主人裁定，REG-128 专线满编优先合并新购禁拆同事备用席位门禁）："
            "① 若 `tp_net-ops` 当前已有在途新机采购请示，请直接通过 `sheepdog tell --to tp_net-ops` 汇总新增人数由 `tp_net-ops` 统一更新采购台数，勿在业务会话重复发第二张专线拍板卡；"
            "② 若确需请示，必须将『合并进新购 VPS 节点（如 node16/node17）统一开通』作为首选建议（选项 A）（没有发送）",
        )
    if topic_id != "tp_net-ops" and _VPS_SEAT_ALLOC_OR_PURCHASE_ASK_RE.search(full_text):
        open_3x = store.conn.execute(
            "SELECT message_id, text FROM escalations WHERE topic_id='tp_net-ops' AND closed_at IS NULL AND answered_at IS NULL ORDER BY asked_at DESC LIMIT 1"
        ).fetchone()
        if open_3x is not None and re.search(r"(?:node\d+|专线|新机器|采购|购买)", str(open_3x["text"] or ""), re.I):
            raise OutboundError(
                EXIT_INVALID,
                f"专线运维专席 `tp_net-ops` 当前已挂有未决的专线采购/席位拍板卡（`{open_3x['message_id']}`），"
                "严禁在业务会话（`" + topic_id + "`）平行再发第二张专线席位/合并采购拍板卡，导致主人在两张卡片间来回核对『一共需要买几台』"
                "（主人裁定，REG-128 跨会话专线需求统一归口 `tp_net-ops` 单一事实源门禁）："
                "请直接通过 `sheepdog tell --topic " + topic_id + " --to tp_net-ops` 将新增申请人数与客户端类型同步给 `tp_net-ops`，由 `tp_net-ops` 原地更新其未决采购卡并统一向主人汇报需购台数，本会话先代回申请人『正在安排开通』并交 `handled`/`waiting` 回执（没有发送）",
            )
    if (
        _RIGID_PROXY_HOPS_BREAKING_ONE_HOP_ASK_RE.search(full_text)
        and not _MULTI_DOMAIN_ENTRY_HOPS_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示将服务固定代理跳数（如 `APP_TRUSTED_PROXY_HOPS`）直接改为 `2`（且测试呈现『1 跳拒 / 仅 1 跳 XFF 请求返回 503』）前，"
            "严禁忽略多域名入口（如 `站点C` 经 CDN+LB 为 2 跳、而 `站点B` 直连 Global ELB 仅为 1 跳）的跳数非对称性"
            "（主人裁定，REG-128 多入口代理跳数非对称防一刀切阻断门禁）："
            "请先核查所有线上入口域名（`站点B` / `站点C` / `站点A`）的真实 `X-Forwarded-For` 跳数差异，优先采用按可信代理 CIDR 剥离或兼容 1/2 跳的方案，并在正文里注明多域名入口兼容验证结论后再发起请示（没有发送）",
        )
    if (
        _EXPIRED_PIPELINE_TOKEN_MANUAL_RENEW_ASK_RE.search(full_text)
        and not _LONG_TERM_NON_EXPIRING_TOKEN_REMEDY_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当流水线或密钥仓中的构建凭据（如 `GITHUB_TOKEN` / `GitHub PAT`）过期导致拉取私有依赖失败时，"
            "严禁只给出『去网页端粘贴更新一次临时 Token』的治标选项，而不一并给出让该类过期问题不再发生的简单长期根治方案"
            "（主人裁定，REG-128 凭证过期请示必带长期免过期根治方案门禁）："
            "请在首选方案中直接写明简单长期有效的根治做法（例如签发 `No expiration / 永不过期` 的最小权限只读 PAT、接入 GitHub App 或开启 `go mod vendor` 免 Token 离线构建），避免每次过期重复人工救火（没有发送）",
        )
    if _STAGE_DB_OR_TUNNEL_WITHHOLD_OVER_GRANT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部研发或业务域负责人在 Stage / 测试环境申请数据库脱敏只读连接（且现网已具备脱敏只读角色与无 Shell 白名单端口转发隧道模式）用于自助跑评测时，"
            "严禁将『A. 不对外发连接串、不开隧道（仅限本机闭环）』作为首选建议，而把『B. 建受限 SSH 隧道并交付脱敏只读连接』贬为次选"
            "（主人裁定，REG-129 Stage 脱敏只读与受限隧道自助赋能优先门禁）："
            "请将『按最小权限脱敏只读角色 + 受限白名单 SSH 隧道开通并交付申请人自助评测』作为首选建议（选项 A）后再发起请示（没有发送）",
        )
    if _FE_REPO_SELF_PR_WITHOUT_OWNER_FEEDBACK_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "排查出前端仓库（`web-app` / `deploy-pages.yml`）代码或发布工作流缺陷时，"
            "严禁未先向前端负责人（阿魏 / `awei`）反馈问题，就直接请示主人『要不要由本会话建单认领并向 `web-app` 提 PR 修复』"
            "（主人裁定，REG-130 领域仓库缺陷先反馈领域 Owner 门禁）："
            "请先将已查明的根因与日志证据反馈给前端负责人阿魏（`awei`）由其团队处理；只有当阿魏明确表示没空处理时，再向主人发起代提 PR 请示（没有发送）",
        )
    if _INVERTED_PERM_ASK_PROXY_OVER_GRANT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对同事申请云项目/服务或最小范围数据库权限（如 `XX申请...权限`、分两步开通先只读 `SELECT` 自查再待 DML 过目及 Stage 验证后加单表 `INSERT/UPDATE` 且不开 `DELETE`，或海内外数据库只读统计申请）发起 `sheepdog ask` 时，严禁把『A. 不开直连账号/不给权限（或一刀切仅走集群 Secret 不发海外只限一库读取连接串）』放在第 1 个主按钮（选项 A）并首推选 A、而把『B/C. 同意按申请开通最小权限账号（或国内甲城库写入甲城 K8s Secret 只在内网、不出境 + 海外那套库的只限一库读取角色，接入串单独私信给提单人）』倒置在选项 B/C"
            "（主人裁定，REG-141 / REG-147 / REG-152 权限请示卡正向最小权限授权置顶门禁）："
            "请将『A. 同意按申请开通最小权限（如国内库写 K8s Secret 只在内网、不出境 + 海外那套库的只限一库读取角色，接入串单独私信给提单人，或分两步先开只读 SELECT 自查）』作为选项 A 并优先推荐选 A，把『B. 暂不开通直连账号 / 仅写集群 Secret 由专席代跑』放在选项 B/C 后再发起请示（没有发送）",
        )
    if _PREMATURE_TEST_ONLY_OR_DRAFT_DEFER_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "对接 DBA（`tp_hazel`）审阅 SQL 并在 TEST / 测试环境执行验证（『生产操作等测试验收完再单独上报』），或核实 PR 仍处于 `Draft` 未合入状态而『先缓一缓等解除 Draft 等正式提测后再办 / 仅在 TEST 环境验证、生产不动』，属于测试环境常驻免批自治与客观进展同步范畴，不涉及任何当下生产写入决策"
            "（主人裁定，REG-148 严禁将 TEST 前置验证或 Draft 暂缓包装成两段式请示门禁）："
            "① 若 PR 仍为 Draft 或尚不急需执行，请直接调用 `sheepdog report`（`--ball other` 或 `done`）汇报核查结论；"
            "② 若需对接 DBA 审阅并在 TEST 环境执行前置验证，请直接通过 `sheepdog tell --to tp_hazel` 协同并在 TEST 环境自主完成验证与回读，用 `sheepdog report` 报备进展，待 TEST 验收通过、真正准备写生产时再一次性发起生产授权 `sheepdog ask`（没有发送）",
        )
    if _CONTROLLED_SCRIPT_READY_RECOMMEND_DEFER_B_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当专席已完成 100% 只读预检、全表备份与逐行回滚脚本就绪，且把『A. 授权马上执行 <受控脚本 --apply>（备份+回滚兜底）』置于选项 A 时，"
            "严禁因周边非阻塞关联配置（如 配置中心 预估配置等已明确由业务方跟进对齐）而在结尾倒置写『建议：B（暂缓写库/暂缓执行）』"
            "（主人裁定，REG-149 受控脚本与回滚就绪禁倒置首推暂缓门禁）："
            "请将『建议：选 A（受控脚本、备份与回滚兜底已就绪，立即执行并在执行后提醒业务方同步对齐关联配置）』作为首推建议后再发起 `sheepdog ask`（没有发送）",
        )
    if (
        _BARE_DB_INSTANCE_WITHOUT_PURPOSE_ASK_RE.search(full_text)
        and not _DB_SCHEMA_OR_BUSINESS_PURPOSE_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "发起涉及数据库写库、续跑或回滚的 `sheepdog ask` 时，严禁只甩出裸数据库实例 ID（如 `MySQL cdb-xxxxxxxx`）或泛称『生产库 / 生产 MySQL』而不交代该数据库的业务用途与库表名称"
            "（主人裁定，REG-150 数据库变更请示必写业务用途与库表门禁）："
            "请在首屏或 `---more---` 中明确写清该数据库实例的业务用途（例如『甲站生产计费主库，存放各模型扣费价目表』）及具体库表名（如 `billing_db.price_table`）后再发起 `sheepdog ask`（没有发送）",
        )
    if _ROUTINE_GROUP_DAILY_DIGEST_TABLE_ASK_RE.search(full_text) or (
        _BROADCAST_SYNC_ASK_RE.search(full_text)
        and _has_prior_loop_broadcast_pause_feedback(cfg, store, topic_id, key)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "群内例行日清长表播报 / 每日定时 跨云迁移进度汇总播报已被主人明确叫停"
            "（主人裁定，REG-151 禁发群内例行长篇日清进度表请示门禁）："
            "请仅在内部台账与 Linear 单据静默跟进阻塞卡点，严禁再为每日定时 群内日清进度汇总表发起 `sheepdog ask` 请示发群（没有发送）",
        )
    parts_before_opts = re.split(r"\n\s*选项\s*[：:]", full_text, maxsplit=1)
    if (
        len(parts_before_opts) == 2
        and _OPTION_NUMBERED_SCHEME2_CODE_CHANGE_RE.search(parts_before_opts[1])
        and not _SCHEME2_CONCRETE_BREAKDOWN_EVIDENCE_RE.search(parts_before_opts[0])
    ):
        raise OutboundError(
            EXIT_INVALID,
            "sheepdog ask 在『选项』中将『方案 2 / 方案二』（含代码/行数/内存池/架构改造）列为待拍板分支时，"
            "严禁在『事情』正文中完全不交代『方案 2 具体怎么做（改哪些文件/函数、具体实现步骤与降级兜底）』就让主人盲选"
            "（主人裁定，REG-153 多方案选项必先拆清方案具体实现步骤门禁）："
            "请先在『事情』段落（或先行发送的汇报卡片并在『事情』中注明）写清『方案 2』涉及的代码文件、核心逻辑步骤与边界兜底后再发起 `sheepdog ask`（没有发送）",
        )
    if (
        _STAGE_TOY_STREAM_PREMATURE_PROD_SCALE_ASK_RE.search(full_text)
        and not _STAGE_FULL_CONCURRENCY_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "sheepdog ask 请示将按连接数高阈值（如某个连接数阈值）扩缩容规则上线生产并通知复测百级高并发（如上百并发）时，"
            "严禁仅在 Stage 跑通小流数玩具演练（如少量流，低于生产单副本阈值）就直接请示上生产把生产当压测场"
            "（主人裁定，REG-154 Stage 未按生产真实阈值与目标并发跑通前禁请示上生产门禁）："
            "请先在 Stage 环境按生产真实连接数阈值（如 threshold=N）跑通目标高并发（如上百并发）全链路扩容与缩容零断连实测，"
            "待 Stage 高并发实测通过并在正文里注明 Stage 高并发实测数据后再发起生产上线 `sheepdog ask`（没有发送）",
        )
    if _CONTROL_PLANE_OPEN_0000_RECOMMEND_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "sheepdog ask 遇到国内家宽/办公网动态公网 IP 或本地代理默认直连（DIRECT）导致静态单 IP 白名单不便维护时，"
            "严禁在选项 A 中首推将 容器集群 / K8s API Server 或生产集群公网安全策略放开为 `0.0.0.0/0`（全网敞开）"
            "（主人裁定，REG-156 控制面公网白名单禁首推 0.0.0.0/0 必优先固定出口与 proxy-url 收敛门禁）："
            "请从第一性原理出发，将『严格锁死固定 /32 白名单（跳板机 + 专属固定出口节点 IP）+ 为客户端配置 kubeconfig 原生 proxy-url 或跳板机 SSH 隧道（或仅 Stage 放通 0.0.0.0/0、Prod 严锁固定白名单）』作为首选推荐项 A，绝不首推生产控制面放通 0.0.0.0/0（没有发送）",
        )
    if _PROD_MOUNT_TEST_BUCKET_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "sheepdog ask 严禁在生产环境负载均衡/CDN/网关（如生产 `acmelab-000002` / `acme-prod-global-elb-new`）中推荐挂载或路由回源至测试环境存储桶（如 `*-test` / `acmeco-demo-bucket-test`）"
            "（主人裁定，REG-157 生产路由严禁挂载混用测试桶门禁）："
            "生产与测试存储资源必须严格隔离，请先在群内/私聊向需求方同步测试桶混用情况并确认切换至新建生产专属桶（`*-prod`）方案后，再发起生产正规化请示（没有发送）",
        )
    if (
        _PROD_BUCKET_REPLACE_TEST_WITHOUT_REQUESTER_CONFIRM_ASK_RE.search(full_text)
        and not _REQUESTER_CONFIRMED_PROD_BUCKET_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当发现需求方现有模板/配置硬编码指向测试存储桶（`*-test`）并准备改为『新建生产专属桶（`*-prod`）+ 同步素材』方案时，"
            "严禁未先在群内/私聊把这一情况与新建生产桶方案同步给需求方并获其确认，就直接向主人发起 `sheepdog ask`"
            "（主人裁定，REG-157 替换需求方存量测试桶前必先同步需求方并确认门禁）："
            "请先在群内/私聊向需求方说明测试桶混用情况与新建生产专属桶方案，登记 `sheepdog watch` 等待需求方确认并在正文里注明『需求方已确认新建生产专属桶』后再发起请示（没有发送）",
        )
    if (
        _UNVERIFIED_EXISTING_CAM_SUBUSER_ASK_RE.search(full_text)
        and not _EXISTING_CAM_SUBUSER_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "内部同事申请在腾讯云成员账号（`2000*`）建一个只供程序访问的 CAM 子用户并发放 AK/SK 时，"
            "严禁未先在目标云账号执行只读存量核查（如 `cloudcli cam ListUsers` / `ListAttachedUserAllPolicies` / `ListAccessKeys` 核对申请人是否本来就已有子账号、权限及 Active 密钥）就盲目发起 `sheepdog ask`"
            "（主人裁定，REG-157 申请新建云子账号前必先只读核查存量权限门禁）："
            "请先在目标云账号只读核查申请人是否已存在子账号/权限及现存密钥；若本来就有权限请直接引导复用，若确需新建请在正文中写明 `ListUsers` 等存量核查结论后再发起请示（没有发送）",
        )
    if (
        _UNCACHED_SLOW_QUERY_SWITCH_WITHOUT_HISTORY_ASK_RE.search(full_text)
        and not _CACHE_SWITCH_HISTORY_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "因线上接口『无缓存直穿』慢 SQL 请示关闭生产 配置中心 开关（如 `demo_feature.enable = false`）时，"
            "严禁只摆当下慢查询现象而不查清历史演进背景（须先查清该接口缓存能力的历史演进与开关的变更时间）就让主人盲目拍板"
            "（主人裁定，REG-157 生产无缓存慢查开关请示必写历史缓存与开关变更演进门禁）："
            "请先通过代码提交历史（`git log` / `git blame`）与 配置中心 配置历史查清该链路以前是否有缓存、何时被关闭或何时开启了该开关，并在正文里注明历史背景后再发起请示（没有发送）",
        )
    if _MIRROR_MIRROR_REPO_READONLY_OVER_GITHUB_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "MIRROR（`mirror.example`）仅作为只读镜像仓库与境内 CI/CD 构建底盘，所有业务代码唯一事实源与阅读/提交主仓均在 GitHub"
            "（主人裁定，REG-157 MIRROR 只读镜像库不首推另开代码权限、须引导 GitHub 主仓门禁）："
            "同事因核对接口/代码契约申请阅读源码时，请直接引导走 GitHub 主仓或將『引导走 GitHub 主仓（无需另开 MIRROR 镜像仓权限）』置于选项 A 首推，严禁首推单独开通 MIRROR 镜像仓代码权限（没有发送）",
        )
    if (
        _PROD_PROXY_CIDR_CONVERGE_WITHOUT_OBSERVE_ASK_RE.search(full_text)
        and not _POST_EXEC_UPSTREAM_DOWNSTREAM_OBSERVATION_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示生产环境可信代理网段（`TRUSTED_PROXY_CIDRS` / `APP_TRUSTED_PROXY_CIDRS`）收敛/删减更新时，"
            "严禁仅把验证承诺停留在控制面静态配置回读（『并回读全字段 JSON Diff』），而完全遗漏执行后的数据面结果完整观察与上下游影响核查方案"
            "（主人裁定，REG-158 生产代理 CIDR 收敛变更必含执行后完整结果与上下游影响观察方案门禁）："
            "请在执行方案与选项中明确写清变更执行后的完整结果观察窗口及上下游服务（网关/BFF/客户端真实 IP 解析/4xx/5xx）影响观察方案后再发起 `sheepdog ask`（没有发送）",
        )
    if _PROD_DB_DYNAMIC_PUBLIC_IP_WHITELIST_REJECT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "同事或外部 Bot 申请将动态公网 IP 池（如 Fastly 共享节点池）加入生产数据库（ADB / 托管数据库 / PG / MySQL）白名单以做只读查数时，"
            "『不向动态公网 IP 池开放生产数据库白名单、改为由专席内部代查数据或联系 DBA Hazel 走固定跳板机』属于默认安全底线与专席自治职责，"
            "严禁在未核查对方现有网段是否已能连通、且把具体 IP（如 `203.0.113.10/24`）藏在 `---more---` 的情况下发起推荐『A. 拒绝开放白名单』的象征性 `sheepdog ask`"
            "（主人裁定，REG-161 生产数据库动态公网 IP 池拒开白名单自治代查与首屏必亮具体 IP 门禁）："
            "请直接在私聊/群内向对方说明生产数据库不对动态公网 IP 池开放白名单，先核实对方现有固定网段是否已连通或直接代查所需只读数据（或通过 `sheepdog tell --to tp_hazel` 走固定跳板机），并用 `sheepdog report` 报备（没有发送）",
        )
    if (
        _JUMPBOX_SG_SINGLE_IP_WHITELIST_ASK_RE.search(full_text)
        and not _FULL_COMPANY_VPS_IP_FLEET_WHITELIST_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "内部同学因切换公司办公专线 / VPS 出口 IP（如 `192.0.2.3`）申请在跳板机（`ins-*` / `bastion`）或安全组（`sg-*`）放行 22 端口 / SSH 白名单时，"
            "严禁只针对该同学当前单个 `/32` 出口 IP 散装发起 `sheepdog ask`"
            "（主人裁定，REG-162 跳板机与安全组白名单必一次性放行全量公司 IP 与 VPS IP 门禁）："
            "请先通过 `sheepdog tell --to tp_net-ops`（或读取 `net-ops` 技能 `node_allowlist.py` / 专线台账）拉齐我们所有公司办公网 IP 与全套 VPS 出口 IP 清单，"
            "在选项 A 中提出一次性放行全部公司 IP 与 VPS IP 的完整方案后再发起 `sheepdog ask`（没有发送）",
        )
    if (
        _CICD_PIPELINE_WITHOUT_DEVOPS_SPEC_OR_DETAIL_ASK_RE.search(full_text)
        and not _DEVOPS_FLOW_SPEC_AND_ARCH_DETAIL_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示搭建服务发布流水线（如申请 `mirror.example` 镜像仓库/push 令牌、跨仓库 Secret 导入、或提议生产改为 `main` 合并自动发 / 稳定后放开生产全自动）时，"
            "严禁只用一句话带过背景且违背公司 DevOps 流规范"
            "（主人裁定，REG-162 发布流水线请示必讲清服务背景架构并严格对齐 DevOps 流规范门禁）：\n"
            "1. 具体讲清事情背景：该服务定位、当前各环境部署形态（Vercel / GCP Cloud Run / 腾讯云国际 容器集群）、为什么需要跨仓 Secret 以及各字段用途；\n"
            "2. 严格落实 DevOps 流规范：① `GitHub` 是唯一代码与 `.mirror.yml` 提交源，`mirror.example` 仅作只读镜像仓库与构建底盘（由 `GitHub Actions` 自动 `sync-to-mirror`，不对个人发放直推 MIRROR 的 push 令牌）；"
            "② `main` 分支合入仅自动部署 Stage/测试环境，生产环境严格执行 Release Tag（`vX.Y.Z`）锚定与人工审批门禁（严禁承诺『生产改为 main 合并自动发』或『稳定后放开生产全自动』）后再发起请示（没有发送）",
        )
    if (
        _PROD_DB_ROLE_LIMIT_BYPASS_DB_OWNER_ASK_RE.search(full_text)
        and not _DB_OWNER_OPS_GROUP_SYNCED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "涉及生产数据库角色连接数上限 / 系统参数 DDL 调整（如 `ALTER ROLE ... CONNECTION LIMIT ...`、`ALTER USER`、`ALTER SYSTEM`）时，"
            "DBA Hazel Yu（`Hazel 老师`）是数据库领域的直接负责人，严禁未经在运维群跟 Hazel 老师说明同步，"
            "就在选项 A 首推『直接登录生产 托管数据库 / Postgres 执行 ALTER ROLE』而把『B. 找 Hazel 老师执行该变更』贬为次选"
            "（主人裁定，REG-164 生产数据库角色连接数与参数 DDL 必先在运维群同步 DBA Hazel 门禁）："
            "请先在运维群 `@Hazel Yu` 说明故障现场与建议执行的 `ALTER ROLE ... CONNECTION LIMIT ...` 语句请其处理（并用 `sheepdog report` 报备），"
            "或在选项 A 中明确首推『先到运维群里和 Hazel 老师说明并请其执行该数据库变更』（没有发送）",
        )
    if (
        _VIP_RELEASE_TICKET_SCOPE_OR_SECRET_DIFF_ASK_RE.search(full_text)
        and not _SUBMITTER_SCOPE_OR_DIFF_CONFIRMED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "发版单（尤其是 Darcy 等 VIP 或研发提单）存在『Secret 配置 Diff 核对』或『单据注明只发其中几个子服务（如仅发其中部分子服务、不动其余子服务）与标准 Trigger 内置 Deploy Extra 连带更新不一致』时，"
            "必须先在发版话题里 `@提单人`（如 Darcy）展示 Secret Diff 与部署范围差异请提单人本人确认，"
            "严禁跳过提单人直接发 `sheepdog ask` 让 Matthew 代为确认"
            "（主人裁定，REG-165 发版单 Secret Diff 与部署范围差异必先找提单人确认门禁）："
            "请先在发版话题里 `@Darcy`（或提单人）核对确认 Secret Diff 与是否连带更新额外服务，待提单人确认后再推进或写明『提单人已确认』（没有发送）",
        )
    if (
        _ENDPOINT_DOMAIN_SWITCH_WITHOUT_CALLER_ASK_RE.search(full_text)
        and not _ENDPOINT_CALLER_TOPOLOGY_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示切换服务的环境变量中的默认接口/语音域名地址（如 `*_URL*` 从 `站点B` 切至 `站点C`）以解决国内直连连通性问题前，"
            "必须先核查代码并在正文里注明『具体是谁调用该地址（是用户浏览器前端客户端直连调用，还是后端微服务调用）』"
            "（主人裁定，REG-169 接口域名切换必先核明调用方拓扑门禁）："
            "请先查清调用链（确认是前端浏览器客户端直连还是后端调用）并在正文里注明核查结论后再发起 `sheepdog ask`（没有发送）",
        )
    if (
        _CLIENT_DIRECT_SINGLE_DOMAIN_SWITCH_ASK_RE.search(full_text)
        and not _CLIENT_DIRECT_DUAL_ORIGIN_ROUTING_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "已确认是用户浏览器/前端客户端直连调用时，严禁在选项 A 推荐将单一全局环境变量从 `站点B` 一刀切改为 `站点C`"
            "（主人裁定，REG-170 客户端直连双途径按入口域名分流门禁）："
            "请把方案改为按入口区分、由客户端以可配置方式选择访问途径，而不是一刀切修改全局变量（没有发送）",
        )
    if (
        _MIRROR_CONSOLE_CREATE_REPO_ON_NOPERM_ASK_RE.search(full_text)
        and not _MIRROR_OPENAPI_PAT_CHECK_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "因 `mirror_cli` 报 `errcode: NOPERM`（当前 Token 无建仓权限）受阻时，严禁直接发 `sheepdog ask` 让主人登录 MIRROR 网页控制台手动建仓或索要新 PAT"
            "（主人裁定，REG-171 MIRROR 建仓优先复用本机 OpenAPI repo-scope:rw 令牌门禁）："
            "本机已有具备 `repo-scope:rw` 权限的 MIRROR OpenAPI PAT 与批量建仓先例（见 `batch_create_mirror_repos.py` / `docs/mirror_repos_report.json`），"
            "请直接调用 `POST https://api.mirror.example/Acme-Group-AI/-/repos`（Header `Authorization: Bearer <token>`, `Accept: application/vnd.mirror.api+json`）创建镜像仓库并配置 GitHub Actions `sync-to-mirror.yml` 自动同步后用 `sheepdog report` 报备（没有发送）",
        )
    if _ONEOFF_EXEMPTION_OVER_CLASS_WHITELIST_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "已确认离线/异步 worker 服务（如 `svc-worker-a`）不影响 C 端在线流量且业务方申请加入 高峰封版豁免白名单时，"
            "按往上抽象的四步『由单个问题推到一类问题』，严禁在选项 A 首推『仅本次特批放行、长期规则维持现状』而把『同意纳入白名单（自动识别整类离线/worker 项目豁免封版）』贬为选项 B"
            "（主人裁定，REG-171 整类离线 worker 封版白名单优先门禁）："
            "请将『同意将该类离线/worker 项目加入封版豁免白名单并放行当前单据』作为首选建议（选项 A）后再发起 `sheepdog ask`（没有发送）",
        )
    if floor or floor_type:
        return
    if _SYMBOLIC_RELEASE_SCHEDULE_ASK_RE.search(full_text) and not _GENUINE_SCHEDULE_CONFLICT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "常规发版窗口排期报备（PR 尚在等待同行 Code Review 合入、无封网/架构冲突，选项 B 仅为『调整排期时间或有其他要求』）没有实质要定的点"
            "（主人裁定）："
            "请直接在群内或私聊配合对齐，并用 `sheepdog report`（`--ball other` 或 `doing`）报备进展；"
            "待 PR 完成人工 Code Review 合入、真正进入生产触发/切流授权或遇到真实架构冲突时再发起请示（REG-086 防象征性发问门禁，没有发送）",
        )
    if topic_id != "tp_hazel" and _DB_USER_ACCOUNT_ASK_RE.search(full_text) and not _REASK_BLOCKER_EXEMPT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "申请数据库内部 User 账号密码 / 只读账号（如 sylvia_readonly）或数据库连接串属于 DBA 统一纳管职责"
            "（主人裁定）："
            "① 若在总线 `tp_bus`，请直接 `sheepdog forward` 转交对应专席处理，严禁在总线直接向主人 ask；"
            "② 若涉及云平台 IAM 角色权限，请按最小权限直接授权并回读后 `sheepdog report`；"
            "③ 若涉及数据库内部 User 账密 / 只读账号（含尚未入 DBA 台账的新库），请直接通过 `sheepdog tell --to tp_hazel` 交由 Hazel 老师纳入 DBA 资产台账后统一开通发放，严禁绕过 DBA 用 migrator 凭据代建或向主人 ask（REG-086 防打扰门禁，没有发送）",
        )
    if _LOGIN_PWD_RESET_ASK_RE.search(full_text) and not _LOGIN_DB_LOG_VERIFIED_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因用户反馈登录提示密码不正确请示重设临时密码前，必须先执行只读库表与日志排查（核查 `auth.users` 该账号存不存在、上次登录时间、认证服务 真实报错日志是否仅输错账号/密码），"
            "并在正文里注明目标站点域名（如 `reelhub-stage.site-a.example`）、只读查库结论与申请人全名，严禁未查库核实前因后果就发模糊 ask 让主人反问『啥意思』（REG-086 防打扰门禁，没有发送）",
        )
    if _NEW_VPS_PURCHASE_FULL_ASK_RE.search(full_text) and not _OVERALL_VPS_ROSTER_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因现有专线/住宅节点满编（0空闲）请示新购/采买 VPS 时，必须在 `---more---` 中一并附上『目前总体 VPS 在用人员名单（各节点当前占用人员明细）』"
            "（主人裁定）："
            "请先从 `tp_net-ops` 或 VPS 台账拉齐当前全网各节点（如 node1~node6、node-r1 等）在用人员名单并附在 `---more---` 中，严禁只写『全部满编』却不附总体使用人员名单（REG-087 防打扰门禁，没有发送）",
        )
    if _US_PROD_GCP_IAM_ASK_RE.search(full_text) and not _DARCY_US_COMPLIANCE_CONFIRMED_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "受数据合规约束的生产项目（acme-prod-000001）默认禁止直接向同事个人账号开通访问权限"
            "（主人明确指示：这个得先问负责人只读权限可不可以给别人开）："
            "请先通过 `sheepdog tell --to tp_darcy` 或在原群向 Darcy Vale 确认只读权限合规口径（或由专席代拉所需时间窗脱敏日志包交付），"
            "严禁未向 Darcy 核实合规口径前发 ask 让主人拍板或写『由主人操作』（REG-088 防打扰门禁，没有发送）",
        )
    if _STAGE_SUBACCOUNT_MINIMAL_IAM_ASK_RE.search(full_text) and not _STAGE_SUBACCOUNT_ADMIN_OPTION_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "在物理隔开的云上 Stage 单独的成员账号（200000000002）为内部研发同学开通联调/部署权限时，首选建议应直接授予该 Stage 成员子账号的 `AdministratorAccess`（admin 权限，严格隔离主账号与生产）"
            "（主人裁定）："
            "请在选项 A 中直接提供或建议在 Stage 单独的成员账号（200000000002）授予 `AdministratorAccess`（admin），避免在已隔离的 Stage 子账号里抠碎片最小策略导致联调反复卡壳（REG-090 防打扰门禁，没有发送）",
        )
    if _PREMATURE_READONLY_INVESTIGATION_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "只读指标/实例/日志排查（Lane D）与转交专席核实属于 Agent 自治职责，严禁在尚未查清具体服务名（如『具体是哪个 gateway』）与实时指标前发 `sheepdog ask` 让主人拍板『A. 只做只读的指标和实例检查』"
            "（因未先只读核查而发 ask 等批，会导致对方等待后自行让其 Agent 操作，主人批复『他说他的agent操作了 你可以帮忙盯着点』）："
            "请立即直接 `sheepdog forward` 转交对应专席（或由当前专席直接执行只读核查），拿到真实服务名、当前实例数/配额与明确目标参数后，若确实需要执行生产变更，再由专席带齐实测数据发起一次精准 `sheepdog ask`（REG-094 防打扰门禁，没有发送）",
        )
    if _NAT_PORT_DOWNSIZE_ASK_RE.search(full_text) and not _NAT_PEAK_PER_VM_PORT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示调小/下调 NAT 网关 `minPortsPerInstance`（`--min-ports-per-instance`）前，必须先通过 监控服务 只读核查当前单实例（per-VM）端口用量峰值 / 已分配端口峰值及动态端口分配上限（`maxPortsPerInstance`）"
            "（主人反问：『这样做会带来哪些副作用 能不能查到单个实例的端口用量峰值』）："
            "请先查清当前单实例端口用量峰值（如 `nat/port_usage` / `allocated_ports` 单 VM 最大值）与调小后的实际影响评估并写入正文，严禁未附单实例峰值端口实测数据就发 ask（REG-096 防打扰门禁，没有发送）",
        )
    if _UNINVESTIGATED_STATUS_ASK_RE.search(full_text) and not _CONCRETE_STATUS_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示确认『渠道/配置/路由现状』（如『需确认...现状，以便...』）前，必须先由当前专席只读排查目标集群/环境的真实配置、路由与缺失项，"
            "弄清楚对方具体缺什么参数并整理出具体可执行的方案后再请主人决策（主人批评：『先查清对方的真实需求，再把执行方案给我来定』，REG-099 防未排查甩锅门禁，没有发送）",
        )
    if _RCA_GOV_ASK_RE.search(full_text) and not _RCA_TIMELINE_AND_IMPACT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "故障/卡顿/根因治理类 sheepdog ask 严禁在 `---more---` 中只写一句口号式摘要而不交代前因后果"
            "（主人反问：『能具体讲讲前因后果嘛』，REG-100 防缺前因后果门禁）："
            "请在 `---more---` 中完整写明：① 前因与现象（发生时间窗、谁反馈、具体报错/P99耗时/影响面）；"
            "② 完整因果链（请求怎么路由、哪一环阻塞或超时引发级联问题）；"
            "③ 当前线上现状（目前是否已恢复/临时止血、线上实时状态）后再发起 ask（没有发送）",
        )
    if _OLD_IMAGE_PARTIAL_FIX_ASK_RE.search(full_text) and not _CICD_PROD_READY_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "生产服务因跑着旧镜像且关联服务留有 Stage 环境变量引发报错（如 404/500）时，严禁建议『只手改部分配置、把旧镜像留着不切』且不核查 CI/CD 部署链路"
            "（主人纠偏：『改用新镜像直接上就行 同时确保相关服务的cicd是通的 可以部署到prod』，REG-100 防半拉子手改门禁）："
            "请先核实相关服务部署到 prod 的 CI/CD 流水线是否畅通，并在正文里注明 CI/CD 状态及直接推进新镜像上线 + 修正生产配置的完整闭环方案后再发起 ask（没有发送）",
        )
    if _EXISTING_VPS_SLOT_AVAILABLE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部同学申请 VPS / Claude 专线且现有节点仍有空位（如『余 2 空额』）时，属于预授权自治范围"
            "（主人裁定，REG-100 防打扰门禁）："
            "请直接在有空位的节点为其开通专属账号并交付，完成后调用 `sheepdog report` 报备；"
            "仅在全网现有节点全部满编（0 空位）需新购 VPS 时才附全量人员名单发起 `sheepdog ask`（没有发送）",
        )
    if _REGION_HARDCODE_OR_SUBFILTER_HOTFIX_ASK_RE.search(full_text) and (
        "sub_filter" in full_text.lower() or not _CONFIG_SWITCH_ISOLATION_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "针对国内外共享前端/服务（如 app-acmeco）处理区域专属三方组件（如 CF Turnstile / 云厂商验证码 / Sentry DSN）时，"
            "严禁提出『Nginx sub_filter 线上脏热修』或『无开关写死彻底禁掉/强制替换』"
            "（主人纠偏：『这个修法你需要保证可以有个开关来控制 什么配置下只给国内的就行』，REG-101 防无开关硬编码与脏热修门禁）："
            "必须设计可由环境变量/配置项控制的开关方案（明确什么配置下只对国内生效、不影响海外环境，且不采用 Nginx sub_filter 篡改静态产物）后再发起 ask（没有发送）",
        )
    if _BROADCAST_SYNC_ASK_RE.search(full_text) and not _SKIP_OR_CANCEL_SYNC_OPT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "就群内日清播报 / 进度播报 / 阶段战报发起 `sheepdog ask` 请示发布时，选项严禁只提供『同意发布 / 先做修改或补充内容』而缺少一键取消/暂不同步选项"
            "（主人裁定，REG-103 播报类请示选项完备性门禁）："
            "必须在选项中显式提供『暂不同步 / 这次播报取消（本次不发群）』的一键按钮选项，避免主人被迫手打『先不同步了』（没有发送）",
        )
    if _CN_SITE_TENCENT_MAINLAND_VS_INTL_ASK_RE.search(full_text) and not _TENCENT_INTL_SUBACCOUNT_POSTURE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对 `站点A` / Reelhub 甲站生产或预发资源开辟，严禁将『在腾讯云甲站账号开辟 vs 先用乙站临时过渡、对外前再对齐甲站』作为二选一请示抛给主人"
            "（主人裁定，REG-104 腾讯云国际子成员账号基线门禁）："
            "Acmeco 与 Reelhub 甲站（`站点A`，`ap-citya`）统一承载在腾讯云乙站（主账号 `200000000000` 组织集团）下的独立子成员账号所在环境（Stage: `200000000002`，Prod: `200000000001`），"
            "不开辟独立的腾讯云甲站账号。请直接按腾讯云乙站子成员账号所在环境整理资源清单与权限方案后再汇报或请示（没有发送）",
        )
    if _TENCENT_EDGEONE_RESOURCE_CHANGE_ASK_RE.search(full_text) and not _TENCENT_SUBACCOUNT_OR_UIN_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示在 CDN（如 `site-a.example` 站点 / `*.site-a.example` 加速域名）新增、配置或修改加速域名与回源 负载均衡 前，必须在正文中显式写明目标所在的腾讯云『子成员账号 ID / UIN』（如生产子账号 `200000000001`、Stage 子账号 `200000000002` 或主账号 `200000000000`）及对应 Zone ID / 负载均衡 IP"
            "（主人裁定，REG-108 腾讯云子成员账号所在环境显式标注门禁）："
            "请先核实并在正文中明确标注目标 CDN 站点与回源 负载均衡 所属的子成员账号 ID（如 `生产子账号 200000000001`）后再发起 `sheepdog ask`（没有发送）",
        )
    if _TENCENT_MAINLAND_FALLBACK_ON_LIMIT_ASK_RE.search(full_text) and not _URGENCY_AND_TENCENT_CONTACT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因腾讯云乙站子账号调接口报 `UnsupportedRegion` / 地域或功能受限（如 TMS/IMS 内容安全）而请示回退到『境内主账号（100000000001）/ 国内腾讯云』新建子账号前，"
            "必须先向需求方确认『这件事是否着急（现有本地词表/兜底是否够用）』，并在方案中提供『不着急则先联系/问问云厂商的人（开通乙站权限或支持地域）』的收束选项"
            "（主人裁定，REG-115 遇到限制时不得贸然退回国内主账号的守卫）："
            "请先向需求方询问是否着急，并在正文里注明需求方关于紧急度的明确答复及咨询腾讯云接口人的选项后再发起 `sheepdog ask`（没有发送）",
        )
    if _NEW_DOMESTIC_DOMAIN_ASK_RE.search(full_text) and not _DOMESTIC_DOMAIN_AUDIENCE_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示新增/开通『国内直连/跳转域名』（如 `edm.site-c.example` 等）前，必须先向申请人核实并在正文里注明『该国内域名具体是给谁访问的（目标访问受众是谁：真实国内 C 端客户、海外客户还是内部运营不挂梯子测试）』"
            "（主人裁定，REG-115 新增国内域名必核访问受众门禁）："
            "请先向申请人问清该国内域名的真实访问受众（`给谁访问 / 访问受众`）并写入正文后再发起 `sheepdog ask`（没有发送）",
        )
    if (m_media_sub := _NEW_SITE_C_DOMESTIC_SUBDOMAIN_ASK_RE.search(full_text)) and not _SITE_A_REUSE_EVIDENCE_RE.search(full_text):
        sub_prefix = (m_media_sub.group(1) or m_media_sub.group(2) or "edm").lower()
        raise OutboundError(
            EXIT_INVALID,
            f"请示新增/配置『{sub_prefix}.site-c.example』国内子域名前，必须先向需求方询问确认『可否直接使用 {sub_prefix}.site-a.example（直接复用腾讯云乙站生产子账号 200000000001 下现有的 site-a.example 正式 CDN 站点）』，"
            f"并在正文里注明需求方对使用 `{sub_prefix}.site-a.example` 的确认结论或将其列为推荐选项 A"
            f"（主人裁定）",
        )
    if _TEST_ENV_NACOS_ACCOUNT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部研发/自动化流程（如 Loops）申请在测试环境（`acme-testing-env` / 测试 配置中心）创建专用或受限 配置中心 账号及凭据属于测试环境预授权范围"
            "（主人裁定，REG-117 测试环境 配置中心 账号预授权自治门禁）："
            "请直接在测试环境 配置中心 为其创建对应测试命名空间的受限账号并把账号密码交付给申请人，完成后调用 `sheepdog report` 报备，严禁发 `sheepdog ask` 打扰主人（没有发送）",
        )
    if _DOMESTIC_TV_TO_OVERSEAS_GCP_ASK_RE.search(full_text) and not _DATA_CROSS_BORDER_COMPLIANCE_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示将甲站点子域名（`*.site-a.example`，如 `track.site-a.example`）解析指向海外 GCP 生产代理/负载均衡（如 `acmelab-000002` / `192.0.2.66` / `acmeco-edge-https-proxy`）前，"
            "必须先确认是否存在国内用户数据出境合规风险，并评估是否应按 `site-a.example` 统一基线收束部署在腾讯云乙站生产子账号（`200000000001`）境内节点"
            "（主人裁定，REG-117 国内 .site-a.example 域名数据出境合规与腾讯云国际收束门禁）："
            "请先核实数据出境合规边界及腾讯云乙站承接方案并写入正文后再发起 `sheepdog ask`（没有发送）",
        )
    if _NODE7_SOLO_VIP_NODE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "`node7`（`198.51.100.237`）已由主人明确锁定为 VIP Darcy Vale 100% 单人独占节点（`1/1 独占锁定，禁止加人`），严禁将 `node7` 当作尚有空余/共用席位的候选节点向主人发起 `sheepdog ask`"
            "（主人裁定，REG-118 `node7` 独占锁定与专线席位必先向 `tp_net-ops` 核实门禁）："
            "请先通过 `sheepdog tell --to tp_net-ops` 核实全网节点的最新真实空余席位（如 `node4` 等），严禁凭旧记忆提议分配 `node7`（没有发送）",
        )
    if _INTERNAL_DEV_REPO_PROXY_EDIT_RECOMMEND_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部研发同学（如业务服务/关联模块作者）申请 GitHub 业务仓库（`Acme-Group-AI/*` / `AcmecoAI/*`）权限以自行修改代码并提交 PR 时，"
            "严禁倒置工程研发权责建议『不开仓库权限，由我们（SRE/Agent）克隆仓库代改代码提 PR』"
            "（主人裁定，REG-119 内部研发仓库权限申请禁推代改门禁）："
            "业务代码变更应由研发作者在具备仓库权限后自行提交 PR 并走标准 Code Review 与发布审批闭环；请将『同意为该内部研发同学开通对应仓库写权限，由其自行提 PR 走评审发布流』作为首选建议（建议选 A）后再发起 `sheepdog ask`（没有发送）",
        )
    check_no_direct_mirror_git_push_guard(full_text, scene="sheepdog ask")
    if not _GROUP_HISTORY_EXPLAINED_EVIDENCE_RE.search(full_text):
        unexplained_owner_hist_req = False
        if _KEY_CHANGE_ROLLBACK_WITHOUT_GROUP_TIMELINE_ASK_RE.search(full_text):
            unexplained_owner_hist_req = True
        elif getattr(store, "conn", None) is not None and topic_id:
            try:
                hist_rows = store.conn.execute(
                    """
                    SELECT message_id, create_time, content
                    FROM messages
                    WHERE topic_id = ?
                      AND reason IN ('owner_instruction', 'owner_reply')
                      AND dispatch_state IN ('pending', 'delivered')
                    ORDER BY create_time DESC LIMIT 5
                    """,
                    (topic_id,),
                ).fetchall()
                for hr in hist_rows:
                    if _OWNER_ASKED_HISTORY_EXPLANATION_RE.search(str(hr["content"] or "")):
                        replied_after = store.conn.execute(
                            "SELECT 1 FROM sends WHERE topic_id = ? AND kind = 'reply' AND sent_at >= ? LIMIT 1",
                            (topic_id, str(hr["create_time"] or "")),
                        ).fetchone()
                        if not replied_after:
                            unexplained_owner_hist_req = True
                            break
            except Exception:
                pass
        if unexplained_owner_hist_req:
            raise OutboundError(
                EXIT_INVALID,
                "主人在群内明确要求『查一下…相关版本来龙去脉』时，必须先通过 `sheepdog reply` 在群内把版本变更的前因后果与来龙去脉讲清楚，"
                "严禁尚未在群里讲清来龙去脉就抢先发 `sheepdog ask` 请示修改生产配置或双 Key 兼容"
                "（主人裁定，REG-117 先在群内讲清来龙去脉再请示变更门禁）："
                "请先用 `sheepdog reply` 在群内完整回复版本改动来龙去脉，并在正文里注明『已在群内讲清来龙去脉』后再发起 `sheepdog ask`（没有发送）",
            )
    if getattr(store, "conn", None) is not None and topic_id and topic_id != BUS_TOPIC_ID:
        idents = {
            m_id.group(0)
            for m_id in _SHARED_CONFIG_IDENT_RE.finditer(f"{key}\n{full_text}")
            if len(m_id.group(0)) >= 12 and m_id.group(0) not in _GENERIC_CONFIG_IDENTS
        }
        norm_k = (key or "").strip().upper().replace("_", "-")
        if len(norm_k) >= 12 and "-" in norm_k:
            idents.add(norm_k)
        if idents:
            try:
                peer_asks = store.conn.execute(
                    """
                    SELECT topic_id, title, text, key, norm_key, sent_at
                    FROM sends
                    WHERE kind = 'ask'
                      AND topic_id IS NOT NULL
                      AND topic_id != ''
                      AND topic_id != ?
                    ORDER BY sent_at DESC LIMIT 30
                    """,
                    (topic_id,),
                ).fetchall()
                for pa in peer_asks:
                    other_tid = str(pa["topic_id"] or "")
                    if not other_tid or other_tid in full_text:
                        continue
                    pa_blob = f"{pa['key'] or ''}\n{pa['norm_key'] or ''}\n{pa['title'] or ''}\n{pa['text'] or ''}"
                    shared = sorted(x for x in idents if x in pa_blob)
                    if shared:
                        raise OutboundError(
                            EXIT_INVALID,
                            f"检测到另一专席 `{other_tid}` 近期已就同一配置/事项（`{shared[0]}`）发起过拍板请示（`{pa['title'] or ''}`）"
                            "（主人裁定，REG-117 跨会话同根因事件防撞车门禁）："
                            f"请先通过 `sheepdog tell --to {other_tid}` 或 `sheepdog forward --topic {other_tid}` 将线索统一归口到 `{other_tid}` 合并处理，"
                            f"若确需独立请示请在正文中显式说明已与 `{other_tid}` 对齐边界（没有发送）",
                        )
            except OutboundError:
                raise
            except Exception:
                pass
    if _DARCY_PARITY_AND_ENTRYPOINT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "Darcy Vale（👑 VIP，含其 AI 助理 Darci）拥有与主人完全对等的最高权限与运维入口"
            "（主人裁定，REG-105 VIP Darcy 权限与入口平权门禁）："
            "凡未触及 3 条硬底线（`--floor-type credentials|destructive|vip_conflict`），我们平时用什么入口/方式操作"
            "（如甲城生产 容器集群 `cls-prod0001` 跳板机 `ins-jump0001` TAT/SSH、限单 IP 临时白名单、新建 K8s Secret 或对齐同级云权限），"
            "请直接给 Darcy / Darci 提供完全一致的入口与同级权限并协助办结，完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（没有发送）",
        )
    if _VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对我方已修改/推送的 PR（如 `.mirror.yml` 构建修复等）或 VIP（Darcy / Darci / 阿魏）在群内询问『谁来跟完、几点前完成』的收尾工作，"
            "严禁把『A. 由我们跟到底 vs B. 由 Darcy 自己来跟合入与验证』当作选择题发 `sheepdog ask` 打扰主人"
            "（主人裁定，REG-110 我方在途 PR 闭环与禁甩锅 VIP 门禁）："
            "自己经手的 PR 必须主动闭环跟到底！请直接派生 Subagent 完成 PR 审查、构建验证与合入，并在群内回复进展与明确完成时间，严禁发 `sheepdog ask` 询问要不要甩给 VIP 自己做（没有发送）",
        )
    if _MULTI_COMPONENT_CAPACITY_PLAN_ASK_RE.search(full_text) and not _LARK_DOC_LINK_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "涉及多组件（PG + MySQL/ADB + Redis 等）容量方案、多组生产资源治理或四步以上生产集群架构加固（如 容器集群 CoreDNS 虚拟节点调度 + 批量数十个 PDB 清单等）请示时，严禁只把脚本或摘要塞在飞书卡片里让主人盲批"
            "（主人裁定，REG-106 / REG-131 / REG-134 复杂方案必附飞书文档或 MD 文件门禁）："
            "请先将完整的背景现状、技术细节清单与处置方案整理成结构化 Markdown 文档（写入知识库 `docs/`）以及飞书云文档（Lark Docx），"
            "在 `sheepdog ask` 正文中附上 `.md` 文档路径与飞书文档链接（`https://.../docx/...`）后再发起请示（没有发送）",
        )
    if (
        _MIDDLEWARE_PROVISION_WITHOUT_USAGE_PROOF_ASK_RE.search(full_text)
        and not _MIDDLEWARE_LIVE_TRAFFIC_AND_PURPOSE_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示在生产环境开通、购买或扩容消息队列中间件（如 RabbitMQ / 消息队列 等）前，严禁仅凭集群存在旧 Pod 或服务挂了环境变量就盲目建议采购多节点托管集群"
            "（主人裁定，REG-134 中间件采购必先核实真实业务用途与实测消息流量门禁）："
            "请先只读核查业务代码调用链与 Broker 实时队列/Exchange 指标，在正文中明确交代 ① 具体业务用途（哪个功能模块在发/收什么消息）与 ② 现网实际消息收发吞吐/队列消息量（publish/deliver rate）；"
            "若核实实际无业务消息收发或无人使用，应按奥卡姆剃刀建议直接清理冗余环境变量与空跑实例（没有发送）",
        )
    if _MAIL_ASSISTANT_ONLY_ACCESS_ASK_RE.search(full_text) and not _FEISHU_DIRECT_REQUEST_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "严禁针对仅由邮件助手（Mail Assistant / 飞书邮箱）推送的自动权限或预览访问申请邮件（如 Vercel Access Request 等）且当事人未在飞书主动找过来的情况发起 `sheepdog ask`"
            "（主人裁定，REG-107 邮件助手自动权限申请静默门禁）："
            "凡仅从邮箱/邮件助手收到的自动申请通知、申请人未在飞书私聊或群内主动提出诉求的，请直接静默归档并交 `handled` 回执，严禁发 `sheepdog ask` 打扰主人或主动私聊当事人（没有发送）",
        )
    if _BUNDLE_STAGE_CLEANUP_WITH_PROD_SCALE_ZERO_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "清理/退订 Stage 空跑计费实例（零生产风险）时，严禁在首选推荐项 A 中强行捆绑『将生产 Pod/Deployment 缩为 0 副本』"
            "（主人裁定，REG-138 Stage 退订禁捆绑生产缩 0 门禁）："
            "请将零生产风险的『只退掉 Stage 闲置实例，生产暂且维持原样不改』作为首选推荐（建议选 A），避免在上线冲刺期顺带改动生产运行工作负载（没有发送）",
        )
    if _DORMANT_ZERO_REPLICA_BATCH_MUTATE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当现网活跃（replicas > 0）拉取测试镜像的工作负载已 100% 清零（0 个），仅剩数十个未开机的 0 副本休眠 Deployment 与挂起 CronJob 时，"
            "严禁在首屏堆砌底层 K8s 术语或将『立即批量修改数十个未开机休眠服务』作为首选推荐项 A 打扰主人"
            "（主人裁定，REG-138 零活跃未开机休眠负载免打扰门禁）："
            "未开机的 0 副本历史遗留模板在上线冲刺期零流量零风险，请直接保持暂不处理（或将『暂不处理未开机休眠服务，待后续需要开机时再按需迁移』作为结论用 `sheepdog report --ball done` 收尾），"
            "严禁为 0 副本休眠死负载连发 ask 打扰主人（没有发送）",
        )
    if (
        _PREFER_NEW_CLOUD_VENDOR_OR_OPEN_EGRESS_OVER_WORKING_TENCENT_CN_ASK_RE.search(full_text)
        and not _TENCENT_CN_UNUSABLE_BLOCKER_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当现有腾讯云甲站境内服务（如 `ap-citya` IMGSEARCH_A 图像搜索，且腾讯云乙站尚未上线该产品）已实测可用（HTTP 200）且账户有充足余额时，"
            "严禁将『在尚未上线该功能的腾讯云乙站尝试开通』或『改接第三方云厂商（阿里云 IMGSEARCH_B）并放开 demo_egress 第三方外连白名单』列为首选推荐项 A"
            "（主人裁定，REG-139 腾讯云甲站境内可用服务优先与严控外连白名单门禁）："
            "请直接沿用已验证可用的腾讯云甲站境内服务（不放开第三方任意 URL 下载白名单），或在群内向提问方确认是否存在腾讯云甲站无法满足的硬性原因后再议（没有发送）",
        )
    if _PARTIAL_APPROVAL_OVER_ISOLATED_NAMESPACE_OWNER_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "服务负责人（Owner）为本服务单独申请一个命名空间（如最小范围的命名空间 RBAC + kubeconfig、独立迁移账号与镜像推拉）以实现端到端自主发版、减少他人代发瓶颈时，"
            "严禁保守首推『B. 部分批准（不直发 kubeconfig / 仍由他人配合）』"
            "（主人裁定，REG-140 服务负责人最小范围的命名空间闭环授权优先门禁）："
            "请将『全部批准（严格按该服务最小范围的命名空间与迁移账号范围逐项开通并下发 kubeconfig）』作为唯一首选推荐（建议：选 A），支持服务负责人在隔离命名空间内端到端闭环（没有发送）",
        )
    if (m_hedge := _HEDGED_DUAL_RECOMMENDATION_ASK_RE.search(full_text)) and (
        m_hedge.group(1).upper() != (m_hedge.group(2) or m_hedge.group(3) or m_hedge.group(4) or "").upper()
    ):
        raise OutboundError(
            EXIT_INVALID,
            f"检测到骑墙双选建议或多分支条件建议（`{m_hedge.group(0).strip()}`）："
            "`sheepdog ask` 必须给出唯一明确的首推选项（如『建议：选 A』），以便主人一键回复『按你的建议』直接拍板，"
            "严禁写『建议选 B（或选 A）』或『建议：选 A（若不想…就选 B；若想…就选 C）』等模棱两可的骑墙建议（REG-140 / REG-173 唯一明确首推建议门禁，没有发送）",
        )
    if (
        _PROD_DB_SCALE_WITHOUT_SHARED_INVENTORY_ASK_RE.search(full_text)
        and not _PROD_DB_SHARED_ISOLATION_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示对生产数据库（PG / PostgreSQL）执行升配、扩容或新建只读实例时，严禁只写单台实例 ID 而未交代全量生产 PG 实例清单及跨业务线（Reelhub vs Acmeco）共用/隔离关系"
            "（主人裁定，REG-141 生产数据库扩容必报全量实例清单与共用隔离边界门禁）："
            "请先只读盘点当前生产账号下共有哪几台 PG 实例、各自承载哪些库表/服务，并在正文中明确说明目标 PG 实例是 Reelhub 独立实例还是与 Acmeco 其他服务共用后再发起请示（没有发送）",
        )
    if (
        _CONSOLE_EYEBALL_PACKAGE_BALANCE_WITHOUT_CLI_OR_DARCY_ASK_RE.search(full_text)
        and not _CLI_BILLING_OR_DARCY_DELEGATION_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当同事怀疑云资源包/基础包/VUM 额度耗尽时，严禁在未调用本机云 CLI（如 `cloudcli --profile site-a-profile` 的 `loadtest QueryJobs`、`billing QueryBalance`、`billing QueryBillDetail`）实测查账的情况下，直接发卡让主人『抽空在控制台看一眼剩余用量』"
            "（主人裁定，REG-141 云资源包余量 CLI 实测自理与 Darcy 代购协同门禁）："
            "请先用本机 `~/.cloudcli/` 凭据实测回读真实消耗与账户余额（若无需请示即可破案则直接 `reply` + `report`），确需加购/充值时须先核实对方紧急度并提供由 Darcy 协助处理的选项（没有发送）",
        )
    if (
        _CLOUD_ACCOUNT_RECHARGE_WITHOUT_UIN_ASK_RE.search(full_text)
        and not _CLOUD_ACCOUNT_EXPLICIT_UIN_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示对云主账号/母账号执行充值平账或支付欠费订单时，严禁只笼统写『主账号 / 国内主账号』而不在 `---more---` 中写明精准的 12 位账号 ID / UIN（如 `1000xxxxxxxx`）"
            "（主人裁定，REG-141 云账号充值平账必附精准主账号 UIN 门禁）："
            "由于团队存在多个云主账号，请在 `---more---` 详情区明确写出目标主账号的 12 位账号 ID / UIN，并交代已与提问人确认其子账号/订单归属同一主账号后再发起请示（没有发送）",
        )
    if _PREFER_RECHARGE_ARREARS_OVER_FUNDED_MAIN_ACCOUNT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当公司已有余额充足的主账号（如数万元现金余额的主账号）且专席可直接通过 CLI 为同事开通子账号与权限时，"
            "严禁在选项 A 首推让主人去给另一个欠费/仅挂域名的账号手动充值平账"
            "（主人裁定，REG-141 优先复用有余额主账号免人工充值门禁）："
            "请直接首推或自治在有充足余额的公司主账号下为同事开通子账号与对应权限，避免在仅做域名持有/备案的欠费账号上继续堆业务与人工充值（没有发送）",
        )
    if (
        _HALF_DONE_ROLLOUT_WITHOUT_STALL_REASON_AND_E2E_GOAL_ASK_RE.search(full_text)
        and not _HALF_DONE_STALL_REASON_AND_E2E_CLIENT_IP_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "针对跨日/半途停滞的改造（如某项改造一半已上生产、另一半合入后长时间未发布）发起请示时，"
            "严禁只罗列内部 PR 编号与环境变量名而不交代 ① 当前阻塞点与未完成的原因、② 最终目标是否已被实测达成"
            "（主人裁定，REG-141 半途停滞事项必报卡点原因与第一性终局目标门禁）："
            "请在正文中说明停滞的具体原因，并对照最终目标给出端到端验证结论后再发起请示（没有发送）",
        )
    if (
        _PROD_ZERO_TRAFFIC_SERVICE_SCALE_ZERO_WITHOUT_DEV_OWNER_CONFIRM_ASK_RE.search(full_text)
        and not _PROD_ZERO_TRAFFIC_DEV_OWNER_CONFIRMED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "针对生产环境业务微服务（如 `acmeco-node-placer` 等）因『连续 N 天 0 请求 / 零流量』请示将常驻实例 `minScale / min` 改为 `0`（缩零或下线）前，"
            "严禁仅凭监控指标与静态代码调用链推断就直接发卡让主人拍板改 `min=0`"
            "（主人裁定，REG-142 生产零流量服务缩零必先向业务研发负责人核实门禁）："
            "请先找该业务服务对应领域的研发负责人（如 `Ivor / Nora / Haoyu` 等）核实零请求的具体情况、是否仍有兜底用途或能否缩零，拿到研发侧确认结论并写入正文后再发起请示（没有发送）",
        )
    if _BUNDLED_MULTI_LINEAR_ISSUES_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "严禁将多张（≥3 张）独立工单（如『7张.tv上线审计运维安全单认领与首波推进确认 / 将 N 张单全部认领』）打包塞进同一张 `sheepdog ask` 卡片一揽子请示"
            "（主人裁定，REG-142 多工单拆解一单一议必查清问题与上下游门禁）："
            "请把每张单拆开交由独立 Subagent 查清具体是什么问题及受波及的上下游，每张单使用独立 `--key <单号>` 单独发一张 `sheepdog ask` 卡片供主人逐个审视确认（没有发送）",
        )
    if (
        _GRANT_111_SITE_A_HOLDER_ACCOUNT_TO_OTHERS_ASK_RE.search(full_text)
        and not _SPECIAL_111_EXCEPTION_PROMINENT_NOTICE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "严禁直接为其他同事开通或分配 `site-a.example` 持有该域名的国内账号（尾号 `111`）的子账号或使用权限"
            "（主人裁定，REG-142 域名持有者 111 账号禁对他人赋权门禁）："
            "甲站确需使用腾讯云时请一律走公司国内主账号（尾号 `222`）；若确属极特殊情况必须使用 `111` 账号，必须在卡片首屏与正文加上醒目提示（如『`【例外情况说明】`』或『`⚠️ 特殊情况`』）写明非用不可的原因后再发起请示（没有发送）",
        )
    if (
        _PROD_LB_INGRESS_UNLOCK_MOD_WITHOUT_OWNER_WINDOW_CHECK_ASK_RE.search(full_text)
        and not _PROD_LB_INGRESS_OWNER_WINDOW_CHECKED_OR_PLANNED_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示临时解开生产 负载均衡 / Ingress 防止误改的保护锁（`modification-protection` / `OperateProtect`）执行线上升配或变更时，"
            "必须在正文中写明已向对应业务域负责人（如 `Rui / 瑞松` 等）确认当前时段线上能不能动（或在选项 A 中明确声明『批准后先向业务负责人确认现在能不能动、确认能动再解锁执行』）"
            "（主人裁定，REG-142 生产网关/负载均衡 变更前必核业务方当前可动窗口门禁）："
            "请先向业务负责人核实当前时段能否操作或在选项 A 中补齐『先向业务方确认当前窗口能动后再执行』说明后再发起请示（没有发送）",
        )
    if (
        _PROD_GACC_LISTENER_MODIFY_WITHOUT_FULL_FIELDS_AND_OPTION_EXPLAIN_ASK_RE.search(full_text)
        and not _PROD_GACC_LISTENER_FULL_FIELDS_AND_OPTION_EXPLAIN_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示修改生产全球应用加速（GACC）监听器配置（如 `cloudcli gacc UpdateListener` 开启 `RealIpPassthrough`）时，"
            "严禁只给仅带单个布尔参数的裸 CLI 命令而不说明 ① 控制台具体对应哪个选项名称与路径（如『七层获取源 IP 方式 / 获取客户端真实 IP』）、"
            "② 是否会导致业务中断及如何在命令中显式锁死全部现有监听器原字段（`--IdleTimeout` / `--ServerCertificates` 等防局部传参重置）"
            "（主人裁定，REG-142 生产 GACC 监听器变更必明示控制台选项与全字段防中断门禁）："
            "请在正文中写明控制台对应选项路径、业务零中断机制、带全量原字段保护的完整命令及低峰窗口长时监控方案后再发起请示（没有发送）",
        )
    if _DISSOLVED_FE_BE_PR_REVIEW_GROUP_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "原『示例评审群』话题群已解散（`chat_status: dissolved`，群成员数为 0）且旧规范文档已删除，"
            "严禁未经只读核实目标飞书群存活状态就发卡请示『以主人身份发送 4 行 PR Review 消息到「示例评审群」』"
            "（`tp_migrate-ops` 某日 先请示发往该已解散群，获批后实测才发现群已解散并连发第 2 张卡，REG-143 已解散示例评审群防盲发与阿魏 Agent 暗雷联检门禁）："
            "请勿向已解散的『示例评审群』发起发送请示；针对待合入 PR，请先自行排查潜在暗雷并直接与阿魏的 Agent（`阿魏 Lark cli` / `tp_awei`）或模块负责人碰一下确认有无坑或暗雷后再推进（没有发送）",
        )
    if (
        _UNREVIEWED_PR_DIRECT_MERGE_WITHOUT_PEER_AGENT_CHECK_ASK_RE.search(full_text)
        and not _PEER_OR_AWEI_AGENT_PITFALL_CHECKED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当待合入 PR 还没做完人工 Code Review（如『人工 Review 0/1』或『示例评审群已解散』）时，"
            "严禁未先与阿魏的 Agent（`阿魏 Lark cli` / `tp_awei`）或模块负责人碰一下排查潜在坑与暗雷，就直接发卡把『A. 由你点 A 视同已完成人工审阅，立即直接执行 gh pr merge』推给主人当人肉 Reviewer"
            "（主人裁定，REG-143 待合入 PR 先找阿魏 Agent 排雷门禁）："
            "请先自行核查改动上下游并直接跟阿魏的 Agent（`阿魏 Lark cli` / `tp_awei`）或模块负责人碰一下确认有无坑或暗雷要小心，确认无暗雷后再自治处理或附上已联检结论发起请示（没有发送）",
        )
    if (
        _PEAK_HOUR_CORE_INFRA_IMMEDIATE_MUTATE_WITHOUT_URGENCY_ASK_RE.search(full_text)
        and not _CORE_INFRA_URGENCY_OR_LOW_PEAK_WINDOW_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "针对生产核心基建服务（如 `svc-ledger`、`svc-ledger-cron`、`svc-account`、`svc-dialog`、`svc-alpha-server`、`svc-events`、`svc-files`）的非故障止血配置修改或发布，"
            "严禁未先向需求方确认是否紧急、也未安排晚间业务低谷窗口就在选项 A 提议『同意现在修正生产…』"
            "（主人裁定，REG-172 生产核心基建服务禁下午高峰期随意变更与晚间低峰执行门禁）："
            "请先向需求发起人确认是否紧急，并在请示卡片中将生产基建服务变更安排在今晚业务低峰期执行、做完由需求发起人负责验收（没有发送）",
        )
    if _CORE_INFRA_MORNING_ONLY_WINDOW_WITHOUT_TONIGHT_REQUESTER_ACCEPT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对生产核心基建服务（如 `svc-ledger` 等）的低峰期配置修改，严禁在选项 A/B 中全部强行推迟到『明早低峰时段（08:30 CST）』而不提供『今晚业务低谷期执行并由需求发起人负责验收』"
            "（主人裁定，REG-172 生产基建服务晚间低峰执行与发起人验收门禁）："
            "请在选项 A 中明确提供『今晚业务低峰期执行生产变更，做完后让需求发起人（如 Miles）负责验收』（没有发送）",
        )
    if _RELEASE_NON_SECRET_ROUTING_DIFF_ASK_OWNER_INSTEAD_OF_INITIATOR_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "发版单涉及分层配置 Secret（如 `svc-alpha-server-app-config`）仅追加非敏感业务路由键（与现有键零冲突、无明文密钥改动）时，"
            "严禁发 `sheepdog ask` 找主人确认路由 Diff"
            "（主人裁定，REG-172 发版非敏感路由配置 Diff 交由发起人确认免打扰门禁）："
            "请直接通过 `sheepdog reply` 让发版单发起人（如 Darcy 等提单人）确认该非敏感路由 Diff，发起人确认后按发版规程推进并用 `sheepdog report` 报备（没有发送）",
        )
    if _BARE_CLOUD_TEAM_NEW_IDENT_WITHOUT_RESOURCE_TYPE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "在请示选项中写『在某平台/团队（如 Vercel `acmecoai`）新建 `<名称>`』时，必须显式写清新建的具体资源类型（如『新建 AI 网关 独立 API Key `acmeco-bff-test`』或『新建独立密钥』），"
            "严禁只写裸标识符（如『在 Vercel `acmecoai` 新建 `acmeco-bff-test`（$20/月）』）导致歧义像在新建整个项目/服务"
            "（主人裁定，REG-172 新建云资源必明示具体资源类型门禁）："
            "请在选项中写清新建的具体对象类型（如『AI 网关 独立 API Key / 密钥』）后再发起请示（没有发送）",
        )
    if _CROSS_CARD_PREVIOUS_REPORT_REF_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "严禁在 `sheepdog ask` 拍板卡中写『文案/预览已发在上一条 / 上面已把…列好』让主人跨卡片翻找待审内容"
            "（因 `sheepdog ask` 会单独镜像到『📌 待我拍板 · 全局聚合台』而 `sheepdog report` 不会镜像，主人在 `tp_vendorx-decision-service` 拍板卡 `[其他]` 反问：『文案在哪里我没找到』，REG-173 决策卡单张自足的守卫）："
            "请把待审看的模拟文案、卡片首屏与正文直接内联写进本张 `sheepdog ask` 卡片内（首屏或 `---more---` 详情中），确保主人在单张拍板卡内即可直接审看拍板（没有发送）",
        )
    if (
        _PROXY_SEND_MESSAGE_WITHOUT_DRAFT_PREVIEW_ASK_RE.search(full_text)
        and not _INLINE_DRAFT_PREVIEW_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当请示选项提议『代发同步卡 / 消息 / 卡片给某人（如 Tao Noel）』时，必须在本张 `sheepdog ask` 卡片内直接附上拟发送的模拟文案预览（如『拟发文案：「…」』或『【代回卡片首屏原话】「…」』），"
            "严禁不附具体文案草稿就让主人盲批『A. 代发三点同步卡给…』"
            "（主人裁定，REG-173 代发消息请示必附内联模拟文案门禁）："
            "请先在本张 `sheepdog ask` 卡片中附上拟发送的具体文案草稿预览后再发起请示（没有发送）",
        )
    if _FEISHU_CUSTOM_BOT_MANUAL_ADD_SHIRK_TO_OWNER_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当飞书群需添加自定义 Webhook 机器人且已有其他同为群管理员的负责人/VIP 可直接在飞书客户端添加并存入密钥库时，"
            "严禁在 `sheepdog ask` 中把『让主人去飞书客户端手动添加自定义机器人并提供 Webhook/签名密钥』列为首推选项 A"
            "（主人裁定，REG-174 群自定义机器人优先由同群管理员自理防倒推门禁）："
            "请直接通过 `sheepdog reply` 请同为群管理员的负责人直接在飞书客户端添加自定义机器人并存入密钥库（或确需请示时将其列为首推选项 A）（没有发送）",
        )
    visible_first_screen = _MORE_LINE_RE.split(full_text, maxsplit=1)[0]
    if (
        _PROD_SWITCH_ENABLE_WITHOUT_FIRST_SCREEN_TEST_VERIFIED_ASK_RE.search(full_text)
        and not _FIRST_SCREEN_TEST_SWITCH_VERIFIED_EVIDENCE_RE.search(visible_first_screen)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当 `sheepdog ask` 提及生产环境配置/缓存开关依旧是 `false` 并提议在生产开启该开关时，"
            "必须在卡片首屏（`--summary` 或 `---more---` 之前）显式写明『这个开关在测试/预发环境是否已开启并完成实测验收』"
            "（主人裁定，REG-175 生产开关开启请示首屏必报测试环境开关实测结论门禁）："
            "请先完成生产配置只读盘点，并在卡片首屏明确写出测试环境开启该开关后的实测结论（如『测试环境已开启该开关并通过冒烟验证 PASS』）后再发起请示（没有发送）",
        )
    if _ALERT_TARGET_GROUP_CHOICE_DELEGATE_TO_VIP_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "当运维负责人 / VIP 询问内部告警机器人具体投递到哪个运维群（如『投 A 群还是投 B 群』）时，此类非底线告警群去向属于负责人自治决策范围，"
            "严禁发 `sheepdog ask` 让主人二选一拍板"
            "（主人裁定，REG-175 内部告警发往哪个群由运维负责人自行决定的守卫）："
            "请直接调用 `sheepdog reply` 回复负责人让其自主决定（可附带『建议优先选新群』的倾向说明），并在完成后调用 `sheepdog report` 报备（没有发送）",
        )
    if (
        (
            _HOLLOW_DOC_INDEX_FIRST_SCREEN_ASK_RE.search(visible_first_screen)
            and not _FIRST_SCREEN_PLAIN_ACTION_BREAKDOWN_EVIDENCE_RE.search(visible_first_screen)
        )
        or _OPAQUE_PHASE_JARGON_CHAIN_OPTION_ASK_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "发起技术方案或巡检改造拍板 `sheepdog ask` 时，严禁在卡片首屏（`--summary`）只写『完整 N 章飞书云文档已就绪（含来龙去脉、N大病根…）建议选 A 先跑通两步演练』等空洞目录报幕，"
            "或把选项 `A/B` 写成『按 Phase 1（A+B+C+D）+ Phase 2』等内部代号与复合黑话串"
            "（主人裁定，REG-180 方案拍板卡首屏与选项大白话直讲具体动作门禁）："
            "请在卡片首屏和选项 `A/B` 中直接用日常大白话写清具体要改哪一两件事（例如『大白话讲就改巡检两件事：一是戊城只测国外网址、甲城多跑一个直接从国内测国内网址；二是单页面偶发超时隔 15 秒重测确认再报』），"
            "把代码文件名、阶段代号与技术细节留在 `---more---` 或云文档里后再发起请示（没有发送）",
        )
    if (
        _VERCEL_GATEWAY_VIRTUAL_MODEL_OR_AUTORECHARGE_WITHOUT_DARCY_CHECK_ASK_RE.search(full_text)
        and not _DARCY_GATEWAY_STATUS_VERIFIED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "当研发同学请求在团队 `AI 网关` 配置虚拟模型（`Virtual Model` / `vmodel/...`）或确认开启账户自动充值/低余额告警时，"
            "严禁未先与平台共管人 Darcy（`tp_darcy`）核实『是否已经配置/开启』就直接发 `sheepdog ask` 找主人操作"
            "（主人裁定，REG-180 团队 AI 网关虚拟模型与自动充值必先找 Darcy 核实协调门禁）："
            "请先通过 `sheepdog tell --to tp_darcy` 或只读核查确认 Darcy 侧是否已配置好虚拟模型与自动充值；若已配好请直接代回需求方并用 `sheepdog report` 报备，若确有未配项请在正文中写明已与 Darcy 核实的具体结论后再发起请示（没有发送）",
        )
    if _PROD_RELEASE_FIRST_SCREEN_JUMPS_TO_TEST_PASS_ASK_RE.search(visible_first_screen) and (
        not _FIRST_SCREEN_WHY_PROBLEM_EVIDENCE_RE.search(visible_first_screen)
        or not _FULL_TEXT_BACKGROUND_STORY_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示发布生产变更时，严禁首屏只报『XX已在测试环境里跑通…现申请推送合入发布生产』而不交代『这个改动解决什么痛点/为什么搞这个』及完整来龙去脉"
            "（主人裁定，REG-181 生产发版请示首屏痛点原因与完整来龙去脉门禁）："
            "请在卡片首屏第一句先用大白话写清『为解决什么线上问题/痛点（如误报、卡顿、报错）』，并在 `---more---` 正文中补齐完整的『起因/来龙去脉/为什么搞这个』上下文后再发起请示（没有发送）",
        )
    if (
        _PROD_CODE_RELEASE_SKIPPING_PR_REVIEW_AND_QA_FLOW_ASK_RE.search(full_text)
        and not _FULL_DEV_PR_REVIEW_AND_QA_FLOW_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "代码开发与生产发版严禁跳过标准研发测试流程直接请示『立即推送合入 GitHub 打 Tag 发布生产』（而把『只推分支建 GitHub PR 留底、暂不合入』列为次选 B）"
            "（主人裁定，REG-181 完整开发测试与正式PR评审/QA验收门禁）："
            "请先按标准研发流程推进：① 开发完成先提草稿 PR（Draft PR）发到测试环境里验证；② 测试环境测完后转为正式 PR 发起 PR Review 并通过评审；③ 提交并通过 QA 测试（`验收机器人` / QA 验收通过）；"
            "待上述完整流程全部通过并在正文里注明正式 PR 号、Review 状态与 QA 测试通过证据后，再发起生产发版请示（没有发送）",
        )
    if (
        _COLLEAGUE_RELAY_CONSOLE_ASK_WITHOUT_ORIGINAL_QUOTE_AND_CONTEXT_RE.search(full_text)
        and not _COLLEAGUE_ORIGINAL_QUOTE_AND_CONTEXT_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "转述同事在群内或私聊反映的问题并请示主人操作控制台/拍板方案时，严禁只写概括转述而不附同事的具体原文与上下文"
            "（主人裁定，REG-181 转述同事诉求必附具体原文与上下文门禁）："
            "请在 `---more---` 正文中明确附上『原文：「...」』与『上下文：...（所在群/话题、前因后果及完整调用场景）』，且首屏直呼 `Matthew` 勿称呼『哥』后再发起请示（没有发送）",
        )
    if (
        _CACHE_TTL_INCREASE_WITHOUT_SIDE_EFFECT_ANALYSIS_ASK_RE.search(full_text)
        and not _CACHE_TTL_SIDE_EFFECT_ANALYSIS_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示调高缓存过期时间（`TTL`）时，严禁只报命中率/延迟收益而未主动评估并写清『调高后的其他副作用与边界影响』"
            "（主人裁定，REG-181 调高缓存TTL必附副作用与边界影响评估门禁）："
            "请在正文中明确列出『其他影响/潜在风险』评估（如：① Redis 内存占用增长测算；② 若遇后台/DB 直接改库绕过主动失效或主动失效失败时的最坏脏读窗口变化），再发起请示（没有发送）",
        )
    if (
        _SVC_ACCOUNT_CACHE_TTL_WITHOUT_OWEN_QIAO_SYNC_ASK_RE.search(full_text)
        and not _OWEN_QIAO_OPINION_SYNCED_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示调高 `svc-account-service` 鉴权缓存的有效期（`demo_ctx_cache.ttl` / `demo_block_cache_ttl`）时，"
            "严禁未先将完整上下文同步给后端负责人 Owen Qiao 并征求其意见就直接发 `sheepdog ask` 让主人拍板"
            "（主人裁定，REG-182 账号服务缓存TTL变更前必须先同步Owen Qiao征求意见门禁）："
            "请先将完整上下文与边界影响同步给 Owen Qiao（或 `tp_owen-qiao`）询问其意见，待拿到反馈并在正文里注明 Owen Qiao 的意见后再发起请示（没有发送）",
        )
    if _STAGE_GACCEL_OWNER_CONFIRMED_DECOMMISSION_RECOMMEND_B_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "在技术负责人 RJ 已明确回复 Stage 跨境加速通道去留（负责人已明确哪些通道保留、其余下线，且符合境内直连或替代原则）时，"
            "严禁在拍板卡中反过来首推『B. 维持 6 大分类继续找各业务同学逐一确认后再动手（推荐）』"
            "（主人裁定，REG-182 Stage跨境通道按负责人RJ确认直退优先门禁）："
            "请将『按 RJ 建议仅保留 tunnel-keep-1、其余全部销毁下线』作为唯一首推建议（`建议：选 A`）（没有发送）",
        )
    if (
        topic_id == BUS_TOPIC_ID
        and (m_spec := _BUS_PROXY_SPECIALIST_ASK_RE.search(full_text))
        and not _BUS_CODEX_OR_BLOCKED_FORWARD_ASK_RE.search(full_text)
    ):
        spec_tid = m_spec.group(1)
        raise OutboundError(
            EXIT_INVALID,
            f"总线（tp_bus）严禁在未做现场只读核查的情况下替专席（{spec_tid}）越俎代庖发『需要你定』拍板卡"
            "（因总线无 gcloud/代码/库表只读权限，代发卡片既缺实时现场核实导致盲猜建议被推翻，又让卡片回调多绕一跳）："
            f"请立即改用 `sheepdog forward --topic {spec_tid} --message-ids <原消息ID>`（若该专席尚未创建则先 `sheepdog new-session`）直接转交专席，"
            f"由 `{spec_tid}` 完成现场只读核查（如核实 0% 流量无 tag 旧修订实例、IAM vs 数据库账密等）后直接向主人发卡请示（REG-097 防总线越权代问门禁，没有发送）",
        )
    if topic_id in (BUS_TOPIC_ID, "tp_net-ops"):
        return
    if _VPS_DB_WHITELIST_SINGLE_IP_ASK_RE.search(full_text) and not _VPS_FLEET_WHITELIST_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部同学切换办公专线 / VPS 出口 IP 申请数据库白名单（托管数据库 / 托管数据库 / 阿里云 ADB / RDS）时，严禁按单人单 IP 散装发起 ask"
            "（主人裁定）："
            "请先通过 `sheepdog tell --to tp_net-ops` 拉齐当前全套需要加白的办公专线 VPS 出口 IP 清单（多台节点），"
            "整理成完整清单后请 Hazel 老师（`tp_hazel`）所有数据库一次性加上，并在完成后用 `sheepdog report` 报备（REG-085 防打扰门禁，没有发送）",
        )
    if _AWEI_PROXY_OR_CONFIRMED_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "阿魏（awei / Owen Wei）的需求属于 VIP 预授权范围"
            "（主人裁定）："
            "① 若消息由阿魏的 AI 助理代发（如 Claude 代发 / ✦ 前缀）尚未经阿魏本人确认，请先直接用 `sheepdog reply` 在原会话向阿魏本人确认是否执行，"
            "严禁把『助理代发 / 待阿魏本人确认』推给主人拍板；"
            "② 一旦经阿魏本人确认，除触及 3 条硬底线（`--floor-type credentials|destructive|vip_conflict`）外，"
            "请直接按需求闭环执行并在完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（REG-085 防打扰门禁，没有发送）",
        )
    if _TENCENT_MAIN_ACCOUNT_CONSOLE_ASK_RE.search(full_text) and not _LOCAL_TCCLI_CHECK_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因腾讯云子账号权限不足（UnauthorizedOperation）请示主人登录控制台创建 API Key / 云资源前，"
            "必须先核查本机 `~/.cloudcli/` 下的主账号凭据（如 `~/.cloudcli/cloud-a.credential` / `default.credential`）："
            "若本机已有主账号 AK/SK 且属于低风险常规研发/测试沙箱资源，请直接或请示授权后用本机 `~/.cloudcli/` 凭据调 API/SDK 代建并回读，"
            "或把『由专席使用本机 ~/.cloudcli/ 主账号凭据代建』列为推荐选项 A，并在正文里注明本机 `~/.cloudcli/` 凭据核查结果，严禁未查本机凭据就把手工登录控制台活推给主人（REG-084 防打扰门禁，没有发送）",
        )
    if _TEST_ENV_IAM_GRANT_ASK_RE.search(full_text) and not _PROD_OR_DESTRUCTIVE_IAM_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "测试环境（acme-testing-env）为内部成员或服务账号授予非破坏性 IAM 角色（如 roles/iam.serviceAccountTokenCreator、Cloud Run、Secret Manager 权限）"
            "属于预授权测试环境操作（『测试环境可以直接动。做完马上回读，如实报结果』）："
            "请直接带 `--account=<owner-account> --project=acme-testing-env` 执行 IAM 绑定并 `get-iam-policy` 单独回读核对，"
            "完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（REG-083 防打扰门禁，没有发送）",
        )
    if _CC_PROXY_ASK_RE.search(full_text) and not _CC_SEAT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "申请开通 CC 专线 / 家宽节点 / 住宅 IP 前，必须先通过 `sheepdog tell --to tp_net-ops` 核实当前各线路使用人数与空余席位余量，"
            "并在正文里注明各线路当前人数与空余席位实据后再发起 ask（REG-076 防打扰门禁，没有发送）",
        )
    if _ENV_REQUIRED_TITLE_RE.search(title) and not _EXPLICIT_ENV_DECL_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示 实时音视频 凭据或子成员账号时，必须在标题或正文中显式声明『明确是测试环境（acme-testing-env）』或『明确是生产环境（acmelab-000002）』"
            "（或『目标环境：Stage/测试/生产』），严禁让主人反问『他是测试还是生产呀 / 哪个环境的子成员账号』（REG-076 防打扰门禁，没有发送）",
        )
    if _PR_MERGE_EXEC_ASK_RE.search(full_text) and not _PR_HUMAN_REVIEW_STATUS_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示执行 `gh pr merge` 或直接合入 PR 打 Tag 发布生产前，必须先核查并在正文中显式写明『人工 Code Review 状态』"
            "（如 GitHub `reviewDecision: APPROVED`、已按 `pr-review-guide` 过人工评审，或明确声明『人工 Review：0/N 未过』），"
            "严禁只写 CI/Greptile 机器人审查就请主人拍板合入发生产，让主人反问『review 过了吗』（REG-081 防打扰门禁，没有发送）",
        )
    if _CORS_OR_CALLER_DOMAIN_ASK_RE.search(full_text) and not (
        _CORS_TRAFFIC_SCAN_EVIDENCE_RE.search(full_text) and _PEER_OWNER_CONSULT_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示确认 CORS / Origin 域名白名单或『是否有其他前端域名调用』前，必须先：① 扫描近 48h 真实云上流量日志（gcloud logging Referer/Origin）与本地记录信息；"
            "② 通过 `sheepdog tell` 与技术负责人（如 `tp_awei`）核对确认，并在正文里注明真实云上流量扫描与负责人核对实据后再发起 ask，"
            "严禁未经自查与负责人对齐直接反问主人『是否有其他前端域名会调用』（REG-082 防打扰门禁，没有发送）",
        )
    if _TEST_APPROVAL_QA_BYPASS_ASK_RE.search(full_text) and not _PEER_OWNER_CONSULT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示发版单『测试审批是否可免测直接放行还是先走 QA』前，必须先通过 `sheepdog tell` 与该单技术负责人（如 `tp_awei`）核对确认（或引导提问方找 `awei` 确认），"
            "并在正文里注明核对结论后再发起 ask，严禁未经内部对齐直接把测试审批放行问题抛给主人（REG-082 防打扰门禁，没有发送）",
        )
    if _TEST_JUMPBOX_FIREWALL_ASK_RE.search(full_text) and not _DESTRUCTIVE_OR_PROD_FIREWALL_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "测试环境（acme-testing-env）跳板机 / allow-demo-office 白名单追加属于预授权非破坏性测试环境操作（『测试环境可以直接动』）："
            "请直接优先将申请人出口 IP 加入 `allow-demo-office`，同时通过 `sheepdog tell --to tp_net-ops` 核实全部 VPS 出口 IP 一并补齐进该白名单，"
            "独立回读确认后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（REG-082 防打扰门禁，没有发送）",
        )
    nk = duty.norm_key(key) if key else ""
    if nk:
        since_iso = (datetime.now().astimezone() - timedelta(minutes=120)).isoformat(timespec="seconds")
        prior = store.conn.execute(
            """SELECT e.message_id, e.answered_at, s.title AS prior_title,
                      m.content AS ans_content, m.dispatch_state AS ans_dispatch_state, m.batch_id AS ans_batch_id
               FROM escalations e
               JOIN sends s ON s.message_id = e.message_id
               LEFT JOIN messages m ON m.message_id = e.answer_message_id
               WHERE e.topic_id = ? AND (s.norm_key = ? OR s.key = ?)
                 AND e.answered_at IS NOT NULL AND e.answered_at >= ?
               ORDER BY e.answered_at DESC LIMIT 1""",
            (topic_id, nk, key, since_iso),
        ).fetchone()
        if prior is not None:
            ans_c = (prior["ans_content"] or "").strip()
            in_flight_sent_at = ""
            cur_b = current_batch(store, topic_id)
            if cur_b:
                d_row = store.get_dispatch(cur_b)
                if d_row is not None and d_row["state"] == "sent":
                    in_flight_sent_at = str(d_row["sent_at"] or "")
            if _RECENT_OWNER_HOLD_VETO_RE.search(ans_c) and not _REASK_BLOCKER_EXEMPT_RE.search(full_text):
                raise OutboundError(
                    EXIT_INVALID,
                    f"同一钥匙 (--key {key}) 主人已于 {prior['answered_at']} 明确批复暂不操作（「{ans_c}」）："
                    "请严格遵守主人『暂不动 / 忽略』指令用 `sheepdog report` 或直接收尾，严禁就同一事项再次发起重复 ask 打扰主人！"
                    "（REG-086 防重复追问门禁，没有发送）",
                )
            if (
                _REASK_MANUAL_OPTION_RE.search(full_text)
                and not _REASK_BLOCKER_EXEMPT_RE.search(full_text)
                and ans_c.startswith("[卡片回调] A：")
                and any(w in ans_c for w in ("由专席", "授权", "同意", "执行", "挂载", "注入", "创建", "配置"))
            ):
                raise OutboundError(
                    EXIT_INVALID,
                    f"同一钥匙 (--key {key}) 主人刚于 {prior['answered_at']} 点击选项 A 批准由专席执行（「{ans_c}」）："
                    "同一事项后续子步骤请直接继承该授权闭环执行（查同项目关联 Cloud Run 服务 env/挂载卷与 Secret），"
                    "严禁再次发起『由专席执行 vs 由主人手动操作』的重复 ask 打扰主人！"
                    "（若确遇新物理权限报错或方案分叉，请在正文中写明报错详情或『需二次拍板：...』；REG-078 防打扰门禁，没有发送）",
                )
            same_ask_title = bool(
                (prior["prior_title"] or "").strip()
                and (prior["prior_title"] or "").strip() == (title or "").strip()
            )
            if (
                ans_c
                and in_flight_sent_at
                and str(prior["answered_at"] or "") >= in_flight_sent_at
                and (
                    (prior["ans_dispatch_state"] or "") == "pending"
                    or (cur_b and (prior["ans_batch_id"] or "") != cur_b)
                    or same_ask_title
                )
            ):
                raise OutboundError(
                    EXIT_INVALID,
                    f"同一钥匙 (--key {key}) 主人刚于 {prior['answered_at']} 拍板批复了上一张卡（{prior['message_id']}，「{ans_c}」），"
                    "当前会话在途批次尚未消费该批复，严禁在途并发重发第 2 张重复 ask 卡片打扰主人！"
                    "请先按主人上述批复直接收尾或在本轮交回执后消费最新批复（REG-086 防并发重发门禁，没有发送）",
                )


_TRANSITIONAL_WORKAROUND_RE = re.compile(
    r"(?:"
    r"(?:暂将|先把|先将|临时将|临时把|先用|暂用)[^\n。；]{0,60}(?:临时指向|指向|切到|设为|用)?[^\n。；]{0,30}(?:A\s*记录|Ingress|负载均衡|直连|直通)"
    r"|临时\s*(?:A\s*记录|指向|直连|直通|绕过|过渡|顶上|兜底)"
    r"|(?:待|等)\s*(?:CDN|CDN|边缘节点|证书|SSL|TLS|CNAME|加速域名)[^\n。；]{0,80}(?:从\s*[`'\"]?process\w*[`'\"]?\s*变|部署完成|签发完成|生效|online|deployed)[^\n。；]{0,40}(?:后|再)?[^\n。；]{0,20}(?:可|一键|平滑|正式)?\s*切(?:换|回|到)?\s*CNAME"
    r"|(?:证书|边缘节点|加速域名|DomainStatus)[^\n。；]{0,40}(?:还在|处于|仍为|正在)\s*(?:最后节点同步\s*)?[`'\"]?(?:部署中|签发中|processing|process(?:\s*->\s*online)?)[`'\"]?"
    r")",
    re.I,
)

_TRANSITIONAL_RESOLVED_RE = re.compile(
    r"(?:"
    r"(?:已将|已把|现已将|已经把)[^\n。；]{0,80}(?:从\s*A\s*记录)?[^\n。；]{0,30}(?:正式)?切(?:换|回)为?\s*[`'\"]?CNAME"
    r"|(?:证书|边缘节点)已部署完成[^\n。；]{0,60}切(?:换|回)为?\s*[`'\"]?CNAME"
    r"|RecordType\s*=\s*[\"'`]?CNAME[\"'`]?"
    r")",
    re.I,
)

_FIRST_SCREEN_TRANSITIONAL_DISCLOSURE_RE = re.compile(
    r"(?:临时|过渡态?|暂用|暂指|待切|待收尾|待切换|还在部署中|部署中|签发中|processing|process\s*->\s*online|⏳\s*在途收尾项)",
    re.I,
)


def _read_current_turn_transcript_text(
    store: Store,
    topic_id: str,
    transcript_file: Path | None = None,
) -> str:
    """REG-091：读取当前会话本轮次（自上一轮结束或上一次成功外发以来）的 transcript.jsonl 文本（含 Subagent SYSTEM_MESSAGE 回报）。"""
    p = transcript_file
    if p is None:
        if store is None or not hasattr(store, "get_topic"):
            return ""
        t = store.get_topic(topic_id)
        cid = (t["conversation_id"] or "").strip() if t is not None and "conversation_id" in t.keys() else ""
        if not cid:
            return ""
        from .sink.agentapi import transcript_path

        p = transcript_path(cid)
    if p is None or not p.is_file():
        return ""
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > 262_144:
                f.seek(size - 262_144)
                raw_bytes = f.read()
                nl = raw_bytes.find(b"\n")
                if nl >= 0:
                    raw_bytes = raw_bytes[nl + 1 :]
            else:
                raw_bytes = f.read()
        lines = raw_bytes.decode("utf-8", errors="replace").split("\n")
    except OSError:
        return ""

    parsed_steps: list[dict] = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
            if isinstance(d, dict):
                parsed_steps.append(d)
        except Exception:
            continue

    collected: list[str] = []
    for step in reversed(parsed_steps[-30:]):
        stype = str(step.get("type") or "")
        content = step.get("content")
        text = content if isinstance(content, str) else ""
        tcs = step.get("tool_calls") or []
        if stype == "PLANNER_RESPONSE" and not tcs and text.strip():
            break
        if stype == "GENERIC" and any(
            marker in text for marker in ("已发到钥匙", "已发给主人", "新开了事项", "发到了话题群里的事项")
        ):
            break
        if stype in ("USER_INPUT", "SYSTEM_MESSAGE", "PLANNER_RESPONSE") and text.strip():
            collected.append(text)
    collected.reverse()
    return "\n".join(collected)


def check_transitional_state_report_guard(
    cfg: Config,
    store: Store,
    topic_id: str,
    title: str,
    summary: str,
    body: str,
    *,
    defer_item: str = "",
    ball: str = "",
    fyi: bool = False,
    transcript_text: str | None = None,
    transcript_file: Path | None = None,
) -> None:
    """7.121（REG-091）：跨源防隐瞒与防静默蒸发门禁。
    当会话在本轮 transcript（含 Subagent 汇报）或 report 正文中采用了『临时 A 记录 / 待证书或节点就绪后再切 CNAME』等过渡态方案，
    却在飞书卡片首屏（--title / --summary）隐瞒不报、或未挂入 --defer-item 就直接传 --ball done 假结案时，当场拦截。
    """
    if fyi:
        return
    combined_report = f"{title or ''}\n{summary or ''}\n{body or ''}"
    if _TRANSITIONAL_RESOLVED_RE.search(combined_report):
        return
    turn_txt = (
        transcript_text
        if transcript_text is not None
        else _read_current_turn_transcript_text(store, topic_id, transcript_file=transcript_file)
    )
    scan_source = f"{body or ''}\n{turn_txt}"
    m = _TRANSITIONAL_WORKAROUND_RE.search(scan_source)
    if m is None:
        return
    matched_snippet = " ".join(m.group(0).split())[:100]
    if (summary or "").strip():
        first_screen = f"{title or ''}\n{summary.strip()}"
    elif "\n---more---\n" in (body or ""):
        first_screen = f"{title or ''}\n{(body or '').partition('---more---')[0]}"
    else:
        first_screen = f"{title or ''}\n{body or ''}"

    has_disclosure = bool(_FIRST_SCREEN_TRANSITIONAL_DISCLOSURE_RE.search(first_screen))
    has_defer = bool((defer_item or "").strip())
    if not has_disclosure and not has_defer:
        raise OutboundError(
            EXIT_INVALID,
            f"report 拒绝（REG-091 临时过渡态防隐瞒与防漏收尾门禁）：检测到当前轮次或正文中存在未收尾的临时过渡方案（『{matched_snippet}』），"
            "但飞书卡片首屏（--title / --summary）只报喜未披露该临时过渡态，也未通过 --defer-item 挂入 Loop 自动盯盘（主人批评：『以后这类情况要汇报 不能一声不吭』）！\n"
            "请：① 在 --summary 首屏如实写明当前临时过渡态与待切换条件；"
            "② 强烈建议同时传 `--defer-item \"<key>:<待收尾动作说明>\" [--defer-check-cmd \"<零Token探活命令>\"]`，由系统自动挂入 Loop 盯盘并在首屏展示在途收尾项（没有发送）。",
        )
    if (ball or "").strip().lower() == "done" and not has_defer:
        raise OutboundError(
            EXIT_INVALID,
            f"report 拒绝（REG-091 临时过渡态防假结案门禁）：检测到当前存在未收尾的临时过渡方案（『{matched_snippet}』），"
            "在最终切换（如切回 CNAME）完成前严禁传 `--ball done` 假结案！请传 `--defer-item \"<key>:<待收尾说明>\"` 挂入 Loop 自动盯盘收尾，或改用 `--ball doing`（没有发送）。",
        )


def check_skill_read_or_raise(
    cfg: Config,
    store: Store,
    topic_id: str,
    action: str,
    *,
    receipt_status: str = "",
) -> None:
    """DEMO-144（4.3）：发前检查会话是否已完整读取当前版本的 sheepdog-worker SKILL.md（未读抛退出码 4）。"""
    pb_dir = cfg.playbook_path if getattr(cfg, "playbook_dir", "") else None
    res = enforce_rule_read_gate(
        store,
        topic=topic_id,
        action=action,
        receipt_status=receipt_status,
        playbook_dir=pb_dir,
    )
    if not res.allowed:
        raise OutboundError(res.exit_code or EXIT_CHECK_FAILED, res.reason)


def send_to_owner(cfg: Config, store: Store, source, kind: str, topic_id: str, title: str, body: str, key: str = "",
                  batch: str = "", about: list[str] | None = None, track: bool = False, floor: str = "",
                  vips: dict | None = None, summary: str = "", floor_type: str | None = None, options: str = "",
                  ball: str = "", fyi: bool = False, defer_loop: str = "", defer_item: str = "",
                  defer_check_cmd: str = "", transcript_text: str | None = None) -> SendResult:
    """ask / report：以机器人身份私聊主人（或发进话题群），ask 发前核 VIP 底线并当场记来源与 floor。"""
    from . import decision_hub, loops as lp, topicgroup as tg
    check_not_lamb(store, kind, topic_id=topic_id)
    canon_mode = decision_hub.canonical_group_mode(cfg, store)
    if track and not _one_line(key) and not canon_mode:
        raise OutboundError(EXIT_INVALID, "--track 要和 --key 一起用：长期跟踪的事要有钥匙")
    if kind not in (ASK, REPORT): raise OutboundError(EXIT_INVALID, f"未知种类 {kind!r}")
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    if not _one_line(title): raise OutboundError(EXIT_INVALID, "--title 不能为空：一句话说是什么事")
    if not (body or "").strip(): raise OutboundError(EXIT_INVALID, "正文为空")
    if not cfg.self_open_id and not cfg.threads.owner_chat_id:
        raise OutboundError(EXIT_INVALID, "配置里没有 self_open_id，不知道主人是谁")
    key = _one_line(key)
    if not key and (canon_mode or (not track and tg.configured(cfg) and cfg.topic_group.enabled and decision_hub.find_canonical_session_item(store, topic_id) is not None)):
        key = decision_hub.base_topic_id(topic_id).removeprefix("tp_") or topic_id
    nk = duty.norm_key(key) if key else ""
    raw_floor, floor = (floor if kind == ASK else ""), (_one_line(floor) if kind == ASK else "")
    combined_for_check = f"{summary.strip()}\n---more---\n{body}" if (summary or "").strip() else body
    if kind == ASK:
        check_vip_floor(cfg, store, topic_id, about, vips, raw_floor, batch, floor_type=floor_type)
        check_ask_pre_flight_guard(
            cfg, store, topic_id, title, combined_for_check,
            key=key, floor=raw_floor, floor_type=floor_type, options=options, about=about, batch=batch,
        )
    else:
        ball = decision_hub.enforce_report_contract(cfg, store, topic_id, title, combined_for_check, key=key, ball=ball, fyi=fyi)
        check_source_peer_followup_and_proxy_guard(
            cfg, store, topic_id, title, combined_for_check,
            key=key, about=about, batch=batch, kind=REPORT, ball=ball, fyi=fyi,
        )
        check_transitional_state_report_guard(
            cfg, store, topic_id, title, summary, body,
            defer_item=defer_item, ball=ball, fyi=fyi, transcript_text=transcript_text,
        )
        if not fyi and getattr(store, "conn", None) is not None and _REPORT_TELLS_OWNER_CLICK_OBSOLETE_ASK_RE.search(f"{title or ''}\n{combined_for_check}"):
            open_for_key = _filter_by_submatter_key(
                store, open_escalations(store, cfg.escalation.open_hours, topic_id), key, topic_id
            )
            if open_for_key:
                mids_str = ", ".join(r["message_id"] for r in open_for_key[:3])
                raise OutboundError(
                    EXIT_INVALID,
                    f"report 拒绝（REG-138 事项已由对方自行处理/无需代操作时必调 resolve 关单门禁）："
                    f"检测到对方已确认自行处理/无需我们代操作，但当前名下仍有未结『需要你定』卡片（{mids_str}），"
                    "严禁在 report 里让主人手动去点旧卡片『点 B 即可』（主人裁定）！\n"
                    f"请先调用 `sheepdog resolve --topic {topic_id} --key {key or '<key>'} --why \"对方已确认自行处理，无需我方代操作\"`（或传 `--quote <对方消息ID>`）将旧提问卡自动结案变绿，再发送汇报（没有发送）。",
                )
        if not fyi and getattr(store, "conn", None) is not None and _REPORT_REVISES_OPEN_ASK_OPTION_RE.search(f"{title or ''}\n{combined_for_check}"):
            raw_open = open_escalations(store, cfg.escalation.open_hours, topic_id)
            open_for_key = _filter_by_submatter_key(store, raw_open, key, topic_id) or raw_open
            if open_for_key:
                mids_str = ", ".join(r["message_id"] for r in open_for_key[:3])
                s0 = store.send_by_message(open_for_key[0]["message_id"])
                orig_key = ((s0["key"] or s0["norm_key"] or "") if s0 is not None else "").strip() or key or "<原key>"
                raise OutboundError(
                    EXIT_INVALID,
                    f"report 拒绝（REG-162 在途未结 ask 选项排除/改推必调 ask 原地更新卡片门禁）："
                    f"检测到当前名下仍有未结『需要你定』卡片（{mids_str}），且本次汇报正试图排除原选项或改推其他选项（如『选项 A 已排除；建议批准选项 B』）！\n"
                    "用 `sheepdog report` 另发汇报不会更新原决策卡片上的『建议：选 A』，会导致主人在原卡点 B 后被错题本误判为推翻（`tp_yichuan-xia` 某日 案例）。\n"
                    f"请直接调用 `sheepdog ask --topic {topic_id} --key {orig_key}`（带更新后的 `--summary`、选项与『建议：选 B』）原地 PATCH 更新未结提问卡（REG-119），勿用 `report` 旁路改推选项（没有发送）。",
                )
    try: _, _, resolved_body = resolve_summary_and_body(body, summary, require_summary=cfg.notify.require_summary, hoist_at=False)
    except ValueError as e: raise OutboundError(EXIT_INVALID, str(e))
    check_skill_read_or_raise(cfg, store, topic_id, kind)
    if kind == REPORT and (defer_item or "").strip():
        try:
            def_info = lp.register_deferred_report_item(
                cfg, store, topic_id, defer_item,
                defer_loop=defer_loop, defer_check_cmd=defer_check_cmd,
            )
        except ValueError as e:
            raise OutboundError(EXIT_INVALID, str(e))
        badge_line = f"⏳ 在途收尾项（已挂入 Loop `{def_info['loop_id']}` 自动盯盘）：{def_info['title']}"
        if badge_line not in resolved_body:
            if "\n---more---\n" in resolved_body:
                head_part, sep_part, tail_part = resolved_body.partition("\n---more---\n")
                resolved_body = f"{head_part.rstrip()}\n{badge_line}{sep_part}{tail_part}"
            else:
                resolved_body = f"{resolved_body.rstrip()}\n{badge_line}"
    text, eff_batch = compose(cfg, kind, topic_id, title, resolved_body), (batch or current_batch(store, topic_id))
    ik = idem_key(kind, topic_id, nk, eff_batch, text)
    origin, srcs, n_chats = ask_sources(cfg, store, topic_id, about, batch) if kind == ASK else ("", [], 0)
    if (prev := store.get_send(ik)) is not None:
        res = SendResult(prev["message_id"], already=True, root_message_id=prev["root_message_id"] or "", text=text, batch_chats=n_chats)
        it = store.item_by_root(prev["root_message_id"]) if prev["root_message_id"] else None
        res.item = tg.label(it["item_no"]) if it is not None else ""
        return _record_ask_sources(cfg, store, res, topic_id, origin, srcs, floor, source=source)
    if kind == ASK and decision_hub.hub_active(cfg, store):
        _, ask_card = owner_content(cfg, text, at=True, kind=ASK, topic_id=topic_id, key=key, title=title, options=options)
        about_ids = [m for m, _ in srcs] if origin == pend.ORIGIN_ABOUT else []
        if (patched := decision_hub.try_patch_open_ask(cfg, store, source, topic_id, key, nk, title, text, ask_card, options_spec=options, about_ids=about_ids)) is not None:
            res = SendResult(patched["message_id"], already=True, patched_in_place=True, root_message_id=patched["root_message_id"], text=text, batch_chats=n_chats)
            return _record_ask_sources(cfg, store, res, topic_id, origin, srcs, floor, source=source)
    route = tg.route_for_send(cfg, store, topic_id, key, track, kind=kind, title=title, batch_id=batch)
    if route.group:
        try:
            res = _send_to_group(cfg, store, source, kind, topic_id, title, key, nk, text, ik, route, options=options, batch_id=eff_batch)
            res.batch_chats = n_chats
            if kind == REPORT: decision_hub.apply_report_ball_state(store, topic_id, key or (route.item and tg.label(route.item["item_no"])) or "", ball)
            return _record_ask_sources(cfg, store, res, topic_id, origin, srcs, floor, source=source)
        except _GroupDown as e:
            route = tg.SendRoute(note=f"话题群发不进去（{e}），已退回私聊，这条发进了私聊")
    res = _send_dm(cfg, store, source, kind, topic_id, title, key, nk, text, ik, options=options)
    res.batch_chats, res.note = n_chats, route.note
    if kind == REPORT: decision_hub.apply_report_ball_state(store, topic_id, key, ball)
    return _record_ask_sources(cfg, store, res, topic_id, origin, srcs, floor, source=source)


class _GroupDown(Exception):
    """往话题群发失败（飞书报错，不是超时）：已标记退回私聊，这一条改发私聊。"""


def _send_to_group(cfg: Config, store: Store, source, kind: str, topic_id: str, title: str, key: str, nk: str,
                   text: str, ik: str, route, *, options: str = "", batch_id: str = "") -> SendResult:
    """7.51 / 7.74：发进话题群里那件事的话题。已有 root_message_id 的老事项不受 tg_down 阻断，回复成功自动清除 down。"""
    from . import decision_hub, topicgroup as tg
    item, created = route.item, False
    if item is None:
        item, created = tg.open_item(store, title, topic_id, topic_id, tg.ORIGIN_TRACK, cfg.topic_group.chat_id, key=key,
                                     topic_id=topic_id, related=route.related), True
    elif key:
        tg.bind_alias_key(store, item["item_no"], key, topic_id)
    try:
        item = tg.ensure_title(cfg, store, source, item, "", key)
        plain, card = owner_content(cfg, text, at=kind == ASK, kind=kind, topic_id=topic_id, key=key, title=title, options=options)
        res = send_owner(
            lambda tx, c: (source.reply_to(item["root_message_id"], identity="bot", idem_key=ik, text=tx, card=c, in_thread=True)
                           if hasattr(source, "reply_to") or source is None
                           else source.reply_in_topic(item["root_message_id"], tx, card=c, idem_key=ik)),
            plain, card)
    except LarkTimeout as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")
    except (SourceError, RuntimeError) as e:
        if created and not store.get_item(item["item_no"])["root_message_id"]: store.delete_item(item["item_no"])
        if is_transient_lark_error(e):
            raise OutboundError(EXIT_SEND_FAILED, f"发送受限流或网络抖动暂败（{e}）：请用同样参数重试，防重编号保证只发一条") from e
        why = " ".join(str(e).split())[:200]
        tg.mark_down(cfg, store, f"往话题群发消息失败：{why}"); raise _GroupDown(why)
    tg.clear_down(cfg, store); mid = res["message_id"]
    reopened = tg.reopen(store, item); tg.touch(store, item["item_no"]); tg.mark_dirty(store)
    if not item["thread_id"] and res.get("thread_id"):
        store.update_item(item["item_no"], thread_id=res["thread_id"]); item = store.get_item(item["item_no"])
    store.add_send(ik, kind, topic_id, mid, text, root_message_id=item["root_message_id"], key=key, norm_key=nk,
                   title=title, identity=ident.BOT if cfg.identity.enabled else None)
    store.add_bot_outbox(mid, topic_id, kind == ASK, text, now_iso())
    if kind == ASK:
        decision_hub.mirror_ask_to_hub(cfg, store, source, origin_mid=mid, origin_root_id=item["root_message_id"],
                                       topic_id=topic_id, key=key, title=title, shown=text, options_spec=options, item_no=item["item_no"])
    if not (created and text.endswith("\n?")):
        tg.sync_title_card(store, cfg, source, item["item_no"], latest_text=text, kind=kind)
    tg.post_origin_signposts(cfg, store, source, item, topic_id, batch_id)
    return SendResult(mid, root_message_id=item["root_message_id"], new_thread=created, text=text,
                      item=tg.label(item["item_no"]), new_item=created, reopened=reopened)


def _send_dm(cfg: Config, store: Store, source, kind: str, topic_id: str, title: str, key: str, nk: str, text: str,
             ik: str, *, options: str = "") -> SendResult:
    """私聊主人（7.22 一事一串，原来的路）。"""
    from . import decision_hub
    root = store.thread_by_key(topic_id, nk) if nk else None
    plain, card = owner_content(cfg, text, at=root is not None, kind=kind, topic_id=topic_id, key=key, title=title, options=options)
    if source is None:
        res = {"message_id": f"om_test_{ik[-12:]}", "thread_id": ""}
    else:
        try:
            if root is not None:
                res = send_owner(lambda t, c: source.reply_to(root["root_message_id"], identity="bot", idem_key=ik, text=t, card=c, in_thread=True), plain, card)
            elif cfg.threads.owner_chat_id:
                res = send_owner(lambda t, c: source.send_to_chat(cfg.threads.owner_chat_id, t, ik, "bot", **_card_kw(c)), plain, card)
            else:
                res = send_owner(lambda t, c: source.send_to_user(cfg.self_open_id, t, ik, "bot", **_card_kw(c)), plain, card)
        except LarkTimeout as e:
            raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")
        except SourceError as e:
            raise OutboundError(EXIT_SEND_FAILED, f"发送失败：{e}")
    mid, new_thread = res["message_id"], False
    if root is not None:
        root_mid = root["root_message_id"]
        store.touch_thread(root_mid)
        if not store.set_thread_id(root_mid, res.get("thread_id") or "") and not root["thread_id"]:
            learn_thread_id(store, source, root_mid)
    elif nk:
        root_mid, new_thread = mid, store.add_thread(mid, topic_id, key, nk, _one_line(title), ASK, res.get("thread_id") or "")
    else:
        root_mid = ""
    store.add_send(ik, kind, topic_id, mid, text, root_message_id=root_mid, key=key, norm_key=nk,
                   title=title, identity=ident.BOT if cfg.identity.enabled else None)
    store.add_bot_outbox(mid, topic_id, kind == ASK, text, now_iso())
    if kind == ASK:
        decision_hub.mirror_ask_to_hub(cfg, store, source, origin_mid=mid, origin_root_id=root_mid,
                                       topic_id=topic_id, key=key, title=title, shown=text, options_spec=options)
    return SendResult(mid, root_message_id=root_mid, new_thread=new_thread, text=text)


def _record_ask_sources(cfg: Config, store: Store, res: SendResult, topic_id: str, origin: str, srcs: list, floor: str = "", *, source=None) -> SendResult:
    """ask 发出去了（或之前已经发过）：记来源，查同一来源有没有别的会话在问；带了 --floor 记进 sends.floor（7.61）。
    report 不记。"""
    clear_handled_owner_reactions(store, source, topic_id)
    if not origin: return res
    if srcs: store.add_escalation_sources(res.message_id, topic_id, srcs, origin)
    # 来源这就定了（没记也是定了：一批来自多个聊天故意不记）；之后每轮补来源不再补它
    store.set_send_sources_origin(res.message_id, origin)
    store.set_send_floor(res.message_id, floor)
    res.origin, res.sources = origin, srcs
    res.duplicates = pend.duplicate_asks(cfg, store, topic_id, srcs, exclude=res.message_id)
    return res


def source_hint(res: SendResult) -> str:
    """不给 --about、这一批消息来自好几个聊天：没记来源，提示会话（7.38）。"""
    if res.origin == pend.ORIGIN_BATCH and res.batch_chats > 1:
        return (f"⚠ 这批消息来自 {res.batch_chats} 个聊天，用 --about 指明为哪条问的，否则不会有来源提醒"
                "（主人在那个聊天里处理了，sheepdog 不会提醒你核实）")
    return ""


def duplicate_text(store: Store, cfg: Config, dups: list) -> list[str]:
    """命令输出里的重复提示：这件事 <会话> <时间> 已经在问。"""
    out = []
    for d in dups:
        at = pend._parse(d["asked_at"])
        out.append(f"⚠ 这件事 {pend.session_label(store, d['topic_id'])} "
                   f"{display_time(at, cfg.timezone_offset) if at else d['asked_at']}已经在问（message_id {d['message_id']}）。"
                   "这次照样发了；同一件事不要两个会话各问一次，可以用 tell 跟它对一下")
    return out


# ======================= resolve：会话结案自己的「需要你定」（7.36） =======================
@dataclass
class ResolveResult:
    closed: list = field(default_factory=list)   # 这次结掉的提问消息号
    notice_message_id: str = ""
    already: bool = False                       # 之前已经结过、那句话也发过（重试）
    notice_error: str = ""                      # 测试环境没发那句话的原因
    text: str = ""


def _filter_by_submatter_key(store: Store, rows: list, key: str, topic_id: str) -> list:
    """1.1.49 (REG-138): 在单会话共享主帖（canon_mode）下，按子事项 --key 结案或匹配时，
    不得将同主帖下显式带有其他子事项 norm_key 的独立未决提问一并卷入结案。"""
    if not rows or getattr(store, "conn", None) is None:
        return rows
    from . import decision_hub as _dh, topicgroup as _tg
    nk = duty.norm_key(key) if key else ""
    if not nk or _tg.parse_item_no(key) is not None:
        return rows
    canon_nk = duty.norm_key(_dh.base_topic_id(topic_id).removeprefix("tp_") or topic_id)
    if nk == canon_nk:
        return rows
    exact: list = []
    compatible: list = []
    for r in rows:
        s_row = store.send_by_message(r["message_id"])
        raw_s_nk = ((s_row["norm_key"] or s_row["key"] or "") if s_row is not None else "").strip()
        s_nk = duty.norm_key(raw_s_nk) if raw_s_nk else ""
        if s_nk == nk:
            exact.append(r)
            compatible.append(r)
        elif not s_nk or _tg.parse_item_no(s_nk) is not None or s_nk == canon_nk:
            compatible.append(r)
    if exact:
        return exact
    has_prior_ask_for_nk = (
        store.conn.execute(
            "SELECT 1 FROM sends WHERE kind='ask' AND (UPPER(norm_key)=? OR UPPER(key)=?) LIMIT 1",
            (nk, nk),
        ).fetchone()
        is not None
    )
    return [] if has_prior_ask_for_nk else compatible


def _resolve_targets(cfg: Config, store: Store, topic_id: str, message_id: str, key: str) -> tuple[list, str, str]:
    """要结的提问（这个会话或同羊圈兄弟槽位名下、还开着的）、那句话回到哪条下面、话题开头那条。"""
    from .roster import primary_topic_id
    p_tid = primary_topic_id(topic_id)
    sibling_tids = list(dict.fromkeys([
        topic_id, p_tid, *(t["topic_id"] for t in store.list_topics() if primary_topic_id(t["topic_id"]) == p_tid),
    ]))
    open_rows = [e for stid in sibling_tids for e in open_escalations(store, cfg.escalation.open_hours, stid)]
    if key:
        nk = duty.norm_key(key)
        roots = list(dict.fromkeys(
            t["root_message_id"] for stid in [*sibling_tids, BUS_TOPIC_ID]
            if (t := store.thread_by_key(stid, nk)) is not None and t["root_message_id"]
        ))
        if not roots:
            rows = store.conn.execute(
                f"SELECT COALESCE(NULLIF(root_message_id,''), message_id) AS root_message_id FROM sends "
                f"WHERE topic_id IN ({','.join('?' * len(sibling_tids))}) AND (norm_key=? OR key=?) ORDER BY sent_at",
                (*sibling_tids, nk, key),
            ).fetchall()
            roots = list(dict.fromkeys(r["root_message_id"] for r in rows if r["root_message_id"]))
        from . import topicgroup as tg
        item = tg.find_by_key(store, key, topic_id=topic_id, allow_cross_session=True) if cfg.topic_group.chat_id else None
        if item is not None and item["root_message_id"]:
            root = item["root_message_id"]
            in_thread = {s["message_id"] for s in store.sends_in_thread(root)} | {root}
            mine = _filter_by_submatter_key(
                store, [r for r in open_rows if r["message_id"] in in_thread], key, topic_id
            )
            if mine or not roots:
                return mine, root, root
        if not roots:
            raise OutboundError(EXIT_INVALID, f"{topic_id} 名下没有钥匙 {key!r} 的话题（ask 带这把 --key 发过才有）")
        in_thread = set(roots) | {s["message_id"] for r in roots for s in store.sends_in_thread(r)}
        mine = _filter_by_submatter_key(
            store, [r for r in open_rows if r["message_id"] in in_thread], key, topic_id
        )
        return mine, roots[0], roots[0]
    message_id = (message_id or "").strip()
    if not message_id.startswith("om_"):
        raise OutboundError(EXIT_INVALID, f"--message-id 要给 om_ 开头的提问消息号，收到 {message_id!r}")
    e = store.get_escalation(message_id)
    if e is None:
        raise OutboundError(EXIT_INVALID, f"账本里没有 {message_id} 这条「需要你定」（`sheepdog escalations` 看还开着的）")
    if e["topic_id"] != topic_id:
        same_group = primary_topic_id(e["topic_id"]) == p_tid
        orphan = topic_id == BUS_TOPIC_ID and pend.follower_of(store, e) == BUS_TOPIC_ID
        if not (same_group or orphan):
            raise OutboundError(EXIT_INVALID, f"{message_id} 是 {e['topic_id']} 问的，不是你的；只能结自己名下的"
                                + ("（总线只能结原会话已收掉的）" if topic_id == BUS_TOPIC_ID else ""))
        open_rows = open_escalations(store, cfg.escalation.open_hours, e["topic_id"])
    s = store.send_by_message(message_id)
    root = s["root_message_id"] if s is not None and s["root_message_id"] else ""
    return [x for x in open_rows if x["message_id"] == message_id], root or message_id, root


def resolve(cfg: Config, store: Store, source, topic_id: str, why: str, *, message_id: str = "", key: str = "",
            playbook=None, quote: str = "", superseded_by: str = "", vips: dict | None = None) -> ResolveResult:
    """会话结案：只结自己名下、还开着的；结案后机器人在那条提问的话题里补一句「已结案：依据（会话）」。"""
    from . import decision_hub
    check_not_lamb(store, "resolve", topic_id=topic_id)
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    why = _one_line(why)
    if not why:
        raise OutboundError(EXIT_INVALID, "--why 不能为空：一句话写依据（主人在哪说了什么 / 你核实到什么）")
    if bool(message_id) == bool(key):
        raise OutboundError(EXIT_INVALID, "--message-id 和 --key 给一个")
    targets, notice_to, root = _resolve_targets(cfg, store, topic_id, message_id, _one_line(key))
    t = store.get_topic(topic_id)
    session = (t["title"] or "").strip() or topic_id
    text = pend.resolve_notice(cfg, playbook, why, session)
    ik = idem_key(pend.SEND_RESOLVE, pend.SEND_RESOLVE, topic_id, notice_to, text)
    prev = store.get_send(ik)
    if not targets:
        if prev is not None:
            return ResolveResult([], prev["message_id"], already=True, text=text)
        raise OutboundError(EXIT_INVALID, "没有还开着的「需要你定」可结（已答、已结或已过期的不用再结）"
                            + (f"：钥匙 {key} 的话题里没有开着的" if key else ""))
    if quote or superseded_by or decision_hub.hub_active(cfg, store) or any((e["hub_message_id"] if "hub_message_id" in e.keys() else "") for e in targets):
        decision_hub.verify_resolve_allowed(cfg, store, targets, quote=quote, superseded_by=superseded_by, vips=vips)
    shown, card = owner_content(cfg, text, at=cfg.escalation.resolve_notice_at)
    mid, err = "", ""
    if source is None:
        err = "测试环境（[source] enabled = false），没发飞书"
    elif prev is not None:
        mid = prev["message_id"]
    else:
        try:
            res = send_owner(lambda t, c: source.reply_to(notice_to, identity="bot", idem_key=ik, text=t, card=c, in_thread=True), shown, card)
        except LarkTimeout as e:
            raise OutboundError(EXIT_SEND_FAILED, f"「已结案」那句话发送超时，不确定发出去没有（{e}），还没结案：用同样的参数重试，防重编号保证只发一条")
        except SourceError as e:
            raise OutboundError(EXIT_SEND_FAILED, f"「已结案」那句话没发出去，还没结案：{e}")
        mid = res["message_id"]
        if root and store.thread_by_root(root) is not None:
            store.touch_thread(root)
            if not store.set_thread_id(root, res.get("thread_id") or "") and not store.thread_by_root(root)["thread_id"]:
                learn_thread_id(store, source, root)
        store.add_send(ik, pend.SEND_RESOLVE, topic_id, mid, text, root_message_id=root or notice_to,
                       target_message_id=notice_to, identity=ident.BOT if cfg.identity.enabled else None)
        store.add_bot_outbox(mid, topic_id, False, text, now_iso())
    closed = [e["message_id"] for e in targets if store.resolve_escalation(e["message_id"], e["topic_id"], why, resolved_by=topic_id)]
    if closed: decision_hub.on_escalations_resolved(cfg, store, source, targets, why=why)
    clear_handled_owner_reactions(store, source, topic_id)
    return ResolveResult(closed, mid, notice_error=err, text=text)


def owner_at(open_id: str) -> str:
    return f'<at user_id="{open_id}"></at>'


def with_owner_at(cfg: Config, text: str) -> str:
    """7.29：往找主人聊天的话题里发的 ask / report，正文末尾另起一行 @ 主人（记账与防重不含该行）。"""
    if not cfg.self_open_id:
        return text
    return text.rstrip("\n") + "\n" + owner_at(cfg.self_open_id)


# ======================= 发给主人的卡片（7.48） =======================
CARD_REJECT_CODES = ("230025", "230099")
_CARD_REJECT = re.compile(r"(?<!\d)(" + "|".join(CARD_REJECT_CODES) + r")(?!\d)")


def owner_content(cfg: Config, text: str, at: bool = False, *, summary: str = "", kind: str = "",
                  topic_id: str = "", key: str = "", title: str = "", options: str = "") -> tuple[str, dict | None]:
    """发给主人的这一条：(纯文本那一份, 卡片或 None)。超上限或关着 owner_card 时返回 None。"""
    plain = with_owner_at(cfg, text) if at else text
    if not cfg.notify.owner_card:
        return plain, None
    try:
        card = build_owner_card(text, cfg.self_open_id if at else "", summary=summary)
    except ValueError:
        return plain, None
    first_line = (text or "").strip("\n").partition("\n")[0]
    if kind == ASK or first_line.startswith(("🐕 需要你定", "🐕 汇报 · 新版提醒", "🐕 汇报 · 版本与更新状态", "🐕 汇报 · 升级完成")):
        from .card_callback import attach_ask_interactions
        card = attach_ask_interactions(card, text=text, summary=summary, options_spec=options,
                                       topic_id=topic_id, key=key, title=title, interactive=bool(cfg.identity.enabled))
    if not card_fits(card):
        log.warning("发给主人的这条卡片估算 %d 字节，超过上限，改发纯文本：%s", card_request_bytes(card), _one_line(text)[:80])
        return plain, None
    return plain, card


def card_rejected(err: Exception) -> bool:
    """飞书拒了这张卡片（内容不行、太长）：错误信息里有 CARD_REJECT_CODES 之一。超时不算（不知道发出去没有）。"""
    return not isinstance(err, LarkTimeout) and bool(_CARD_REJECT.search(str(err)))


def send_owner(send, plain: str, card: dict | None):
    """发一条给主人：send(正文, 卡片或 None) 调一次飞书。有卡片先发卡片；飞书拒了卡片（card_rejected）就用同一个防重编号
    改发纯文本（卡片没发出去，编号还没用掉）。其他失败、超时原样抛给调用方。"""
    if card is None:
        return send(plain, None)
    try:
        return send(plain, card)
    except SourceError as e:
        if not card_rejected(e):
            raise
        log.warning("飞书拒了发给主人的卡片（%s），同一条改发纯文本", " ".join(str(e).split())[:200])
        return send(plain, None)


def _card_kw(card: dict | None) -> dict:
    """只有发卡片时才带 card 参数（纯文本照旧的调用方式）。"""
    return {"card": card} if card is not None else {}


def learn_thread_id(store: Store, source, root_mid: str) -> str:
    """往话题里发完一条，返回里没有 thread_id（实测 +messages-reply --reply-in-thread 只回 chat_id、create_time、
    message_id）：只读 mget 一次开头那条，把话题编号补进话题表。失败不影响发送结果，之后从拉取里补。"""
    fn = getattr(source, "messages_by_id", None)
    if fn is None:
        return ""
    try:
        got = fn([root_mid])
    except Exception:
        return ""
    tid = next((m.thread_id for m in got if m.message_id == root_mid and m.thread_id), "")
    if tid:
        store.set_thread_id(root_mid, tid)
    return tid


# ======================= reply：发出前再核一遍 =======================
def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _position(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class CheckStart:
    """发前检查的起点：要回复的那条（它所在批次里同一聊天还有更晚的，以最晚那条为准）。"""
    at: datetime | None                      # 时间（只到分钟），搜索从这里查起；没有序号时按它比先后
    position: int | None                     # 聊天里的序号（message_position）；这几条都有序号时才有，用它比先后
    batch_ids: set[str] = field(default_factory=set)   # 这一批里同一聊天的消息：会话已经一起收到了，不算新发言


def check_start(store: Store, target: sqlite3.Row) -> CheckStart:
    rows = [target]
    if target["batch_id"]:
        rows += store.conn.execute("SELECT * FROM messages WHERE batch_id=? AND chat_id=? AND message_id != ?",
                                   (target["batch_id"], target["chat_id"], target["message_id"])).fetchall()
    times = [t for t in (_parse(r["create_time"] or "") for r in rows) if t is not None]
    positions = [_position(r["message_position"]) for r in rows]
    return CheckStart(max(times) if times else None,
                      max(positions) if positions and None not in positions else None,  # type: ignore[type-var]
                      {r["message_id"] for r in rows})


def owner_spoke_since(cfg: Config, store: Store, source, target: sqlite3.Row) -> OwnerSpoke:
    """只读检查：这个聊天里、在起点之后，主人本人有没有新发言（排除带代回标记的会话代回、这一批里已经有的）。
    先后优先按聊天里的序号（message_position）比，有一边没有序号时才按时间（分钟）比。
    只调搜索接口；不写账本、不动拉取进度、不点表情、不改已读。查询失败抛 OutboundError(EXIT_CHECK_FAILED)。"""
    base = check_start(store, target)
    start, batch_ids = base.at, base.batch_ids
    if start is None or start.tzinfo is None:
        raise OutboundError(EXIT_CHECK_FAILED, f"账本里这条消息没有可用的时间（{target['create_time']!r}），没法做发前检查")
    since = start.isoformat(timespec="seconds")
    fn = getattr(source, "chat_messages_since", None)
    if fn is None:
        raise OutboundError(EXIT_CHECK_FAILED, "消息源不支持发前检查")
    try:
        found = fn(target["chat_id"], cfg.self_open_id, since)
    except LarkTimeout as e:
        raise OutboundError(EXIT_CHECK_FAILED, f"发前检查超时，没发：{e}")
    except SourceError as e:
        raise OutboundError(EXIT_CHECK_FAILED, f"发前检查失败，没发：{e}")
    suffix = cfg.session.reply_suffix
    hits: list[Message] = []
    seen: set[str] = set()
    for m in found:
        if m.message_id in seen or m.message_id in batch_ids:
            continue
        if m.sender_id != cfg.self_open_id or (m.chat_id and m.chat_id != target["chat_id"]) or m.deleted:
            continue
        pos = _position(m.message_position)
        if base.position is not None and pos is not None:
            if pos <= base.position:
                continue  # 聊天里的序号不比要回复的那条大：在它之前（同一分钟里也分得清）
        else:
            at = _parse(m.create_time)
            if at is None or at.tzinfo is None or at < start:
                continue  # 没有序号才按时间比：只到分钟，同一分钟里的一律算「之后」（偏保守）
        if is_agent_reply(m.content, cfg.owner_context) or has_reply_suffix(m.content, suffix):
            continue  # 会话以主人身份代回的
        seen.add(m.message_id)
        hits.append(m)
    hits.sort(key=lambda m: (m.create_time, m.message_id))
    return OwnerSpoke(hits, since)


def owner_chats(cfg: Config) -> set[str]:
    return {c for c in [*(getattr(cfg, "owner_side_chat_ids", None) or []), *(getattr(cfg, "owner_chat_ids", None) or []), getattr(getattr(cfg, "topic_group", None), "chat_id", "")] if c}

def reject_owner_chat(cfg: Config, topic_id: str, chat_id: str) -> None:
    """热修：要回复的那条在「找主人」聊天里（[escalation] chat_ids、[threads] owner_chat_id）就拒绝。
    sheepdog reply 是以主人身份回别人的；在找主人聊天里用它，等于以主人身份回主人自己，还会被当成主人亲自回复投回会话。"""
    if chat_id and chat_id in cfg.owner_side_chat_ids:
        raise OutboundError(EXIT_INVALID,
                            "要回复的那条在「找主人」聊天里（主人和机器人的私聊），sheepdog reply 只用来以主人身份回别人，"
                            "不能在这里用。回主人请用 "
                            f"`sheepdog report --topic {topic_id} --key <同一件事的钥匙> --title \"<短标题>\" --text \"<正文>\"`"
                            "（要他定的事用 sheepdog ask，参数相同），会经机器人发给他，同一把钥匙回到同一个话题里。没有发送。")


def reply_body(cfg: Config, text: str = "", card_text: str = "", summary: str = "") -> tuple[str, dict | None]:
    """代回的内容：一律发成卡片（沿用 reply-card：正文 + 右下角代回标记）。返回 (记账用的正文, 卡片)。
    7.26：主人本人、机器人两个身份发的都一样带标记。
    7.62：长消息须带四刀法白话总结（summary 或正文内 ---more---），原文收进 more 默认折叠区；--text 与 --card-text 等价，一律发成卡片。"""
    suffix = cfg.session.reply_suffix
    raw = card_text if card_text else text
    if not (raw or "").strip():
        raise ValueError("正文为空")
    _, _, combined = resolve_summary_and_body(raw, summary, require_summary=cfg.notify.require_summary)
    if _REPLY_ADDRESSES_OWNER_AS_BOSS_RE.search(combined):
        raise ValueError(
            "代回正文中请直接称呼名字 `Matthew`，严禁称呼或指代为『老板』"
            "（主人裁定）"
        )
    card = build_reply_card(combined, suffix, cfg.session.reply_card_width)  # 空正文抛 ValueError
    return combined, card


def text_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def reply_idem(store: Store, topic_id: str, target_id: str, shown: str) -> str:
    """代回的防重编号：批次号 + 被回复的消息号 + 正文指纹（拿不到批次号时用会话 + 消息号 + 正文指纹）。
    内容一样才算重复：同一条先回「ok，稍等」、处理完再回结论，两条都发；同样的内容重试只发一条。
    代价：发送超时以后改了措辞再重试，可能多发一条。"""
    row = store.get_message(target_id)
    base = [row["batch_id"], target_id] if row is not None and row["batch_id"] else [topic_id, target_id]
    return idem_key(REPLY, *base, text_fingerprint(shown))


@dataclass
class ReplyResult:
    message_id: str = ""; already: bool = False; spoke: OwnerSpoke | None = None
    newly_seen: int = 0; text: str = ""; identity: str = ""; why: str = ""; work_id: int = 0


def _send_reply(source, target_id: str, ik: str, shown: str, card: dict | None, identity: str) -> dict:
    txt = "" if card is not None else shown
    try:
        if target_id.startswith("oc_"):
            return (source.post_to(target_id, identity=identity, idem_key=ik, text=txt, card=card) if hasattr(source, "post_to")
                    else source.send_to_chat(target_id, txt, ik, identity, **_card_kw(card)))
        return source.reply_to(target_id, identity=identity, idem_key=ik, text=txt, card=card)
    except LarkTimeout as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")


def _resolve_reply_target_row(store: Store, target_id: str):
    """7.136（REG-144）：优先从 messages 表取要回复的目标行；若专席刚通过 sheepdog reply 发出群根卡、守护进程尚未回采入 messages，
    则从 sends 表回填合成目标上下文，确保对本机刚发出的 om_ 根消息跟进催办不因尚未回采而报「账本里没有」。"""
    if not target_id or target_id.startswith("oc_"):
        return None
    row = store.get_message(target_id)
    if row is not None or not hasattr(store, "send_by_message"):
        return row
    s_row = store.send_by_message(target_id)
    if s_row is None:
        return None
    s_keys = s_row.keys() if hasattr(s_row, "keys") else ()
    t_mid = ((s_row["target_message_id"] if "target_message_id" in s_keys else "") or "").strip()
    chat_id = t_mid if t_mid.startswith("oc_") else ""
    if not chat_id and t_mid.startswith("om_"):
        parent = store.get_message(t_mid)
        chat_id = ((parent["chat_id"] if parent is not None else "") or "").strip()
    if not chat_id:
        return None
    ident_val = ((s_row["identity"] if "identity" in s_keys else "") or "").strip()
    is_bot = ident_val == "bot"
    return {
        "message_id": target_id,
        "chat_id": chat_id,
        "chat_name": store.known_chat_name(chat_id) if hasattr(store, "known_chat_name") else "",
        "chat_type": "group",
        "sender_id": "",
        "sender_name": "",
        "sender_type": "app" if is_bot else "user",
        "content": (s_row["text"] if "text" in s_keys else "") or "",
        "create_time": (s_row["sent_at"] if "sent_at" in s_keys else "") or "",
        "message_position": None,
        "batch_id": None,
        "route": "self",
        "reason": "self_bot" if is_bot else "self",
        "tags_json": "[]",
        "mentions_json": "[]",
        "topic_id": (s_row["topic_id"] if "topic_id" in s_keys else "") or "",
    }


def reply(cfg: Config, store: Store, source, topic_id: str, target_id: str, *, text: str = "", card_text: str = "",
          summary: str = "", even_if_owner_replied: bool = False, start: bool = False) -> ReplyResult:
    """代回：先查重，再发前检查（om_ 消息号），支持 om_ 消息回复与 oc_ 群顶层主动发话（Bot 优先，230002 自动退回 user）。
    start=True（DEMO-145）：发开工卡（带「⏹ 终止」按钮，私聊或主人身份时附「回复『停』可以终止」），并登记 work_items doing 行。"""
    check_not_lamb(store, "reply", topic_id=topic_id)
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    target_id = (target_id or "").strip()
    is_chat = target_id.startswith("oc_")
    if not (target_id.startswith("om_") or is_chat):
        raise OutboundError(EXIT_INVALID, f"--to 要给 om_ 开头的消息号或 oc_ 开头的群 chat_id，收到 {target_id!r}")
    check_bus_not_forwarded_reply(store, topic_id, target_id)
    enabled, target = cfg.identity.enabled, (None if is_chat else _resolve_reply_target_row(store, target_id))
    chat_id = target_id if is_chat else (target["chat_id"] if target is not None else "")
    if chat_id and ((text or "").strip() or (card_text or "").strip()):
        reject_owner_chat(cfg, topic_id, chat_id)
    if not is_chat and target is not None:
        check_group_reply_allowed(cfg, store, topic_id, target, even_if_owner_replied=even_if_owner_replied)
    try: shown, card = reply_body(cfg, text, card_text, summary=summary)
    except ValueError as e: raise OutboundError(EXIT_INVALID, str(e))
    check_unverified_ticket_promise_guard(shown)
    ik = reply_idem(store, topic_id, target_id, f"[start]\n{shown}" if start else shown)
    if (prev := store.get_send(ik)) is not None:
        prev_w = store.get_work_by_start_card(prev["message_id"]) if start else None
        return ReplyResult(
            prev["message_id"],
            already=True,
            text=shown,
            identity=(prev["identity"] or ident.USER) if enabled else ident.USER,
            work_id=int(prev_w["id"]) if prev_w is not None else 0,
        )
    check_skill_read_or_raise(cfg, store, topic_id, "reply")
    if not is_chat and not even_if_owner_replied:
        if target is None:
            raise OutboundError(EXIT_INVALID, f"账本里没有 {target_id}，没法做发前检查（以账本里这条消息的时间为准）。确认消息号；是 sheepdog 送给你的消息就一定在账本里")
        if (spoke := owner_spoke_since(cfg, store, source, target)).messages:
            return ReplyResult(spoke=spoke, newly_seen=sum(store.add_seen_before_send(topic_id, m.message_id, target_id) for m in spoke.messages), text=shown)
    if is_chat:
        if not enabled: who, why = ident.USER, ident.WHY_DISABLED
        else:
            ok, why = ident.bot_in_chat(cfg, store, source, target_id)
            if not ok and why == ident.WHY_LIST_FAILED and getattr(source, "bot_chat_ids", None) is None:
                ok, why = True, ident.WHY_IN_CHAT
            who = ident.BOT if ok else ident.USER
            if not ok and why == ident.WHY_NOT_IN_CHAT: store.note_bot_absent(target_id, "", why)
    else:
        who, why = ident.choose_reply_identity(cfg, store, source, target)
        if enabled and why == ident.WHY_NOT_IN_CHAT and target is not None:
            store.note_bot_absent(target["chat_id"], target["chat_name"] or "", why)
    work_id = 0
    if start:
        target_keys = target.keys() if target is not None and hasattr(target, "keys") else ()
        t_batch = (str(target["batch_id"] or "") if "batch_id" in target_keys else "") or current_batch(store, topic_id)
        req_id = str(target["sender_id"] or "") if "sender_id" in target_keys else ""
        req_name = str(target["sender_name"] or "") if "sender_name" in target_keys else ""
        t_chat_type = str(target["chat_type"] or "") if "chat_type" in target_keys else ("group" if is_chat else "")
        w_title = duty.short(_one_line( str(target["content"] or "") if "content" in target_keys and target["content"] else shown ), 60)
        work_id = store.create_start_work(
            topic_id,
            target_id,
            w_title,
            batch_id=t_batch,
            requester_id=req_id,
            requester_name=req_name,
            chat_id=chat_id,
            card_identity=who,
        )
        eff_p2p_mode = (store.get_meta("p2p_stop_mode") or getattr(getattr(cfg, "session", None), "p2p_stop_mode", "") or "card").strip().lower()
        p2p_text_hint = eff_p2p_mode == "text" and (t_chat_type == "p2p" or who == ident.USER)
        card = build_start_card(
            shown,
            work_id=work_id,
            topic_id=topic_id,
            target_mid=target_id,
            p2p_hint=p2p_text_hint,
            suffix=cfg.session.reply_suffix,
        )
    try: res = _send_reply(source, target_id, ik, shown, card, who)
    except SourceError as e:
        if not (enabled and who == ident.BOT and ident.is_not_in_chat_error(e)):
            raise OutboundError(EXIT_SEND_FAILED, f"发送失败：{e}")
        ident.invalidate_bot_chats(store)
        if chat_id: store.note_bot_absent(chat_id, "" if is_chat else (target["chat_name"] or ""), ident.WHY_230002)
        who, why = ident.USER, ident.WHY_230002
        if start and work_id:
            eff_p2p_mode = (store.get_meta("p2p_stop_mode") or getattr(getattr(cfg, "session", None), "p2p_stop_mode", "") or "card").strip().lower()
            p2p_fallback_hint = eff_p2p_mode == "text"
            card = build_start_card(shown, work_id=work_id, topic_id=topic_id, target_mid=target_id, p2p_hint=p2p_fallback_hint, suffix=cfg.session.reply_suffix)
        try: res = _send_reply(source, target_id, ik, shown, card, who)
        except SourceError as e2:
            raise OutboundError(EXIT_SEND_FAILED, f"发送失败（机器人不在群里，退回主人本人也没发出去）：{e2}")
    mid = res["message_id"]
    if start and work_id:
        store.update_work(work_id, start_card_mid=mid, card_identity=who)
    store.add_send(ik, REPLY, topic_id, mid, shown, target_message_id=target_id, identity=who if enabled else None)
    store.add_bot_outbox(mid, topic_id, False, shown, now_iso()); clear_handled_owner_reactions(store, source, topic_id)
    if topic_id != BUS_TOPIC_ID and (thr_id := (res.get("thread_id") or (target["thread_id"] if target is not None and "thread_id" in target.keys() else "") or "")):
        store.add_group_thread(thr_id, chat_id, topic_id, "sent", mid, now_iso())
    return ReplyResult(mid, text=shown, identity=who, why=why, work_id=work_id)


def spoke_text(cfg: Config, spoke: OwnerSpoke) -> str:
    """把主人的话原样打印给会话（时间 + 内容）。"""
    lines = [f"主人已经在这个聊天里说过话（从 {display_time(_parse(spoke.since), cfg.timezone_offset)}起），这条代回没发："]
    for m in spoke.messages:
        lines += [f"- {display_time(_parse(m.create_time), cfg.timezone_offset)}（message_id {m.message_id}）：", *("  " + ln for ln in (m.content or "").splitlines() or [""])]
    lines += ["这些发言之后会照常作为背景送到你这里，会注明「你发送前已经看到过」，不是新的提醒。",
              "先读他的话再决定：不用回了就不回；要改就改了再发；确实还要照样发，加 --even-if-owner-replied。"]
    return "\n".join(lines)


# ======================= thread：只读看整个话题 =======================
@dataclass
class ThreadView:
    thread: sqlite3.Row | None; thread_id: str; root_message_id: str; items: list[dict]; warning: str = ""


def find_thread(store: Store, topic_id: str, key: str = "", thread: str = "") -> tuple[sqlite3.Row | None, str, str]:
    """按钥匙或话题编号（omt_ / 开头那条的 om_）找话题。返回 (话题行或 None, thread_id, 开头那条)。
    钥匙找不到、或话题记在别的会话名下（总线除外）抛 OutboundError。
    7.51：话题群里的事项谁都能看（一件事跨会话），按钥匙（含 T-编号）或话题编号找得到就返回 (None, 话题编号, 开头那条)。"""
    item = None
    if key:
        thr = store.thread_by_key(topic_id, duty.norm_key(key))
        from . import topicgroup as tg
        item = tg.find_by_key(store, key, topic_id=topic_id, allow_cross_session=True) if thr is None else None
        if item is not None and item["root_message_id"]:
            return None, item["thread_id"] or "", item["root_message_id"]
        if thr is None:
            raise OutboundError(EXIT_INVALID, f"{topic_id} 名下没有钥匙 {key!r} 的话题（ask / report 带这把 --key 发过才有）")
        return thr, thr["thread_id"] or "", thr["root_message_id"]
    thread = (thread or "").strip()
    if not thread.startswith(("omt_", "om_")):
        raise OutboundError(EXIT_INVALID, "--thread 要给 omt_ 开头的话题编号或开头那条的 om_ 消息号")
    item = store.item_by_thread(thread) or store.item_by_root(thread)
    if item is not None and item["root_message_id"]:
        return None, item["thread_id"] or "", item["root_message_id"]
    thr = store.thread_by_thread_id(thread) or store.thread_by_root(thread)
    if thr is not None:
        if thr["topic_id"] != topic_id and topic_id != BUS_TOPIC_ID:
            raise OutboundError(EXIT_INVALID, f"这个话题记在 {thr['topic_id']} 名下，不是你的")
        return thr, thr["thread_id"] or "", thr["root_message_id"]
    g = store.group_thread(thread) if thread.startswith("omt_") else None
    if g is not None and (g["topic_id"] == topic_id or topic_id == BUS_TOPIC_ID):
        return None, thread, g["root_message_id"] or ""   # 7.45：群话题归属记在这个会话名下
    if topic_id != BUS_TOPIC_ID:
        raise OutboundError(EXIT_INVALID, "话题表里没有这个话题：只有总线能看没记过的话题")
    return None, thread if thread.startswith("omt_") else "", thread if thread.startswith("om_") else ""


def view_thread(cfg: Config, store: Store, source, topic_id: str, key: str = "", thread: str = "",
                fetch: bool = True) -> ThreadView:
    """整个话题：开头那条从账本、sends 或 mget 取，回复从账本和话题列表取（话题列表不含开头那条），合在一起列出。
    只读：不写账本里的消息、不动拉取进度、不点表情。"""
    thr, thread_id, root = find_thread(store, topic_id, key, thread)
    me = cfg.self_open_id
    items: dict[str, dict] = {}

    def who(sender_id: str, sender_type: str) -> str:
        return "主人" if me and sender_id == me else "机器人" if sender_type in ("app", "bot") else (sender_id or "-")

    def add(mid: str, at: str, w: str, text: str, src: str, tpos=None, mpos=None) -> None:
        if not mid:
            return
        if mid not in items:
            items[mid] = {"message_id": mid, "at": at or "", "who": w, "text": text or "", "source": src, "root": False,
                          "tpos": _position(tpos), "mpos": _position(mpos)}
            return
        for k, v in (("tpos", tpos), ("mpos", mpos)):   # 同一条从别处（话题列表）拿到了序号就补上
            if items[mid][k] is None:
                items[mid][k] = _position(v)

    rows = store.thread_messages_in_ledger(thread_id, root)
    if not root:  # 只给了话题编号：账本里序号是开头序号的那条就是开头
        root = next((r["message_id"] for r in rows if (r["thread_position"] or "") == root_position(r["chat_type"])), "")
    for r in rows:
        add(r["message_id"], r["create_time"], who(r["sender_id"], r["sender_type"]), r["content"], "账本",
            r["thread_position"], r["message_position"])
    for s in store.sends_in_thread(root) if root else []:
        add(s["message_id"], s["sent_at"], "机器人", s["text"], "sheepdog 发出")
    warnings: list[str] = []
    if fetch and root and root not in items:
        fn = getattr(source, "messages_by_id", None)
        try:
            for m in (fn([root]) if fn is not None else []):
                if m.message_id == root:
                    add(m.message_id, m.create_time, who(m.sender_id, m.sender_type), m.content, "飞书",
                        m.thread_position, m.message_position)
                    thread_id = thread_id or m.thread_id
        except SourceError as e:
            warnings.append(f"取开头那条失败：{e}")
    fn = getattr(source, "thread_messages", None)
    if fetch and fn is not None and (root or thread_id):
        try:
            for m in fn(root or thread_id, "asc"):
                add(m.message_id, m.create_time, who(m.sender_id, m.sender_type), m.content, "飞书",
                    m.thread_position, m.message_position)
        except SourceError as e:
            warnings.append(f"查飞书话题列表失败，只列出账本里有的：{e}")
    elif fetch and fn is None:
        warnings.append("消息源不支持查话题（测试环境），只列出账本里有的")
    if root in items:
        items[root]["root"] = True
    elif root:
        warnings.append(f"开头那条（{root}）取不到")
    # 回复按话题里的序号排（没有再按聊天里的序号），都没有序号才按时间：同一分钟里按时间会排反
    return ThreadView(thr, thread_id, root, order_thread_items(list(items.values())), "；".join(warnings))


def render_thread(cfg: Config, v: ThreadView) -> str:
    head = "话题"
    if v.thread is not None:
        head += f"（topic {v.thread['topic_id']}，钥匙 {v.thread['key'] or '-'}"
        head += f"，话题编号 {v.thread_id or '还不知道'}，开头那条 {v.root_message_id}）"
    else:
        head += f"（话题编号 {v.thread_id or '-'}，开头那条 {v.root_message_id or '-'}，话题表里没有）"
    lines = [head + f"，共 {len(v.items)} 条："]
    for x in v.items:
        at = _parse(x["at"])
        mark = "【开头】" if x["root"] else ""
        lines.append(f"- {mark}{display_time(at, cfg.timezone_offset) if at else '-'} {x['who']}"
                     f"（message_id {x['message_id']}，{x['source']}）：")
        lines += ["  " + ln for ln in (x["text"] or "").splitlines() or [""]]
    if v.warning:
        lines.append("⚠ " + v.warning)
    return "\n".join(lines)
