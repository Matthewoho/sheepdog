"""一事一串与发前再看一眼（7.22）：会话找主人（ask/report，私聊或话题群，带防重编号）、代回（reply，发前检查主人发言并带代回标记）、查看话题（thread）。"""

from __future__ import annotations

import hashlib, logging, os, re, sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import duty, identity as ident, pending as pend, session as sm
from .ack import clear_handled_owner_reactions
from .card import build_owner_card, build_reply_card, card_fits, card_request_bytes, resolve_summary_and_body
from .config import Config
from .engine import (BUS_TOPIC_ID, MANAGED_KINDS, PRIORITY_PREFIX, display_time, has_reply_suffix, is_agent_reply,
                     open_escalations, order_thread_items, root_position)
from .models import Message
from .source import SourceError
from .source.lark import LarkTimeout
from .source.tat_cache import is_transient_lark_error
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
    item: str = ""                  # 7.51：发进了话题群里哪件事项（T-12）；空 = 私聊
    new_item: bool = False          # 这次新开了这件事项（发了标题卡）
    reopened: bool = False          # 这件事办完 / 归档了，这次重开
    note: str = ""                  # 给会话看的一句提示（--track 没生效、群不可用退回私聊等）


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
    """能经 sheepdog 发消息的会话：总线、名册 managed、总线新开的（没收掉的）。"""
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
    """会话没传 --batch 时用的批次号：支持 SessionGroup 槽位族（tp_laohu / tp_laohu#2）与插队批次（p…）。"""
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


def check_group_reply_allowed(
    cfg: Config,
    store: Store,
    topic_id: str,
    target: sqlite3.Row | None,
    *,
    even_if_owner_replied: bool = False,
) -> None:
    """7.103（REG-071）：多人大群代回门禁：
    1. 非总线会话严禁对停留在 tp_bus 未 forward 转交的被动群消息（keyword/group/watched_chat/vip_sender 或仅 @ 他人）调用 reply 越权抢答；
    2. 严禁在多人大群里直接对仅 @ 其他同事（未 @ 主人/机器人/所有人且未 forward 转交）的消息调用 reply；
    3. 严禁在多人大群里直接 reply 同事个人账号代发的 AI 助理消息（防跨 Agent 群聊乒乓死循环；主人确需回复时可加 --even-if-owner-replied）。"""
    if target is None:
        return
    from .prompts import is_agent_proxy_content, is_group_directed_to_others
    from .store import SYSTEM_CHAT

    keys = target.keys() if hasattr(target, "keys") else ()
    chat_type = ((target["chat_type"] if "chat_type" in keys else "") or "").strip()
    chat_id = ((target["chat_id"] if "chat_id" in keys else "") or "").strip()
    if chat_type in ("p2p", SYSTEM_CHAT, "") or chat_id in cfg.owner_side_chat_ids:
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


def send_to_owner(cfg: Config, store: Store, source, kind: str, topic_id: str, title: str, body: str, key: str = "",
                  batch: str = "", about: list[str] | None = None, track: bool = False, floor: str = "",
                  vips: dict | None = None, summary: str = "", floor_type: str | None = None, options: str = "", ball: str = "", fyi: bool = False) -> SendResult:
    """ask / report：以机器人身份私聊主人（或发进话题群），ask 发前核 VIP 底线并当场记来源与 floor。"""
    from . import decision_hub, topicgroup as tg
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
    if kind == ASK: check_vip_floor(cfg, store, topic_id, about, vips, raw_floor, batch, floor_type=floor_type)
    else: ball = decision_hub.enforce_report_contract(cfg, store, topic_id, title, body, key=key, ball=ball, fyi=fyi)
    try: _, _, resolved_body = resolve_summary_and_body(body, summary, require_summary=cfg.notify.require_summary, hoist_at=False)
    except ValueError as e: raise OutboundError(EXIT_INVALID, str(e))
    text, eff_batch = compose(cfg, kind, topic_id, title, resolved_body), (batch or current_batch(store, topic_id))
    ik = idem_key(kind, topic_id, nk, eff_batch, text)
    origin, srcs, n_chats = ask_sources(cfg, store, topic_id, about, batch) if kind == ASK else ("", [], 0)
    if (prev := store.get_send(ik)) is not None:
        res = SendResult(prev["message_id"], already=True, root_message_id=prev["root_message_id"] or "", text=text, batch_chats=n_chats)
        it = store.item_by_root(prev["root_message_id"]) if prev["root_message_id"] else None
        res.item = tg.label(it["item_no"]) if it is not None else ""
        return _record_ask_sources(cfg, store, res, topic_id, origin, srcs, floor, source=source)
    if kind == ASK and nk and decision_hub.hub_active(cfg, store):
        _, ask_card = owner_content(cfg, text, at=True, kind=ASK, topic_id=topic_id, key=key, title=title, options=options)
        if (patched := decision_hub.try_patch_open_ask(cfg, store, source, topic_id, key, nk, title, text, ask_card, options_spec=options)) is not None:
            res = SendResult(patched["message_id"], already=True, root_message_id=patched["root_message_id"], text=text, batch_chats=n_chats)
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
            mine = [r for r in open_rows if r["message_id"] in in_thread]
            if mine or not roots:
                return mine, root, root
        if not roots:
            raise OutboundError(EXIT_INVALID, f"{topic_id} 名下没有钥匙 {key!r} 的话题（ask 带这把 --key 发过才有）")
        in_thread = set(roots) | {s["message_id"] for r in roots for s in store.sends_in_thread(r)}
        return [r for r in open_rows if r["message_id"] in in_thread], roots[0], roots[0]
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


# ======================= reply：发前再看一眼 =======================
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
    """10-13 热修：要回复的那条在「找主人」聊天里（[escalation] chat_ids、[threads] owner_chat_id）就拒绝。
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
    7.62：长消息必须带四刀法白话总结（summary 或正文内 ---more---），原文收进 more 默认折叠区；--text 与 --card-text 等价，一律发成卡片。"""
    suffix = cfg.session.reply_suffix
    raw = card_text if card_text else text
    if not (raw or "").strip():
        raise ValueError("正文为空")
    _, _, combined = resolve_summary_and_body(raw, summary, require_summary=cfg.notify.require_summary)
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
    newly_seen: int = 0; text: str = ""; identity: str = ""; why: str = ""


def _send_reply(source, target_id: str, ik: str, shown: str, card: dict | None, identity: str) -> dict:
    txt = "" if card is not None else shown
    try:
        if target_id.startswith("oc_"):
            return (source.post_to(target_id, identity=identity, idem_key=ik, text=txt, card=card) if hasattr(source, "post_to")
                    else source.send_to_chat(target_id, txt, ik, identity, **_card_kw(card)))
        return source.reply_to(target_id, identity=identity, idem_key=ik, text=txt, card=card)
    except LarkTimeout as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")


def reply(cfg: Config, store: Store, source, topic_id: str, target_id: str, *, text: str = "", card_text: str = "",
          summary: str = "", even_if_owner_replied: bool = False) -> ReplyResult:
    """代回：先查重，再发前检查（om_ 消息号），支持 om_ 消息回复与 oc_ 群顶层主动发话（Bot 优先，230002 自动退回 user）。"""
    check_not_lamb(store, "reply", topic_id=topic_id)
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    target_id = (target_id or "").strip()
    is_chat = target_id.startswith("oc_")
    if not (target_id.startswith("om_") or is_chat):
        raise OutboundError(EXIT_INVALID, f"--to 要给 om_ 开头的消息号或 oc_ 开头的群 chat_id，收到 {target_id!r}")
    check_bus_not_forwarded_reply(store, topic_id, target_id)
    enabled, target = cfg.identity.enabled, (None if is_chat else store.get_message(target_id))
    chat_id = target_id if is_chat else (target["chat_id"] if target is not None else "")
    if chat_id and ((text or "").strip() or (card_text or "").strip()):
        reject_owner_chat(cfg, topic_id, chat_id)
    if not is_chat and target is not None:
        check_group_reply_allowed(cfg, store, topic_id, target, even_if_owner_replied=even_if_owner_replied)
    try: shown, card = reply_body(cfg, text, card_text, summary=summary)
    except ValueError as e: raise OutboundError(EXIT_INVALID, str(e))
    ik = reply_idem(store, topic_id, target_id, shown)
    if (prev := store.get_send(ik)) is not None:
        return ReplyResult(prev["message_id"], already=True, text=shown, identity=(prev["identity"] or ident.USER) if enabled else ident.USER)
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
    try: res = _send_reply(source, target_id, ik, shown, card, who)
    except SourceError as e:
        if not (enabled and who == ident.BOT and ident.is_not_in_chat_error(e)):
            raise OutboundError(EXIT_SEND_FAILED, f"发送失败：{e}")
        ident.invalidate_bot_chats(store)
        if chat_id: store.note_bot_absent(chat_id, "" if is_chat else (target["chat_name"] or ""), ident.WHY_230002)
        who, why = ident.USER, ident.WHY_230002
        try: res = _send_reply(source, target_id, ik, shown, card, who)
        except SourceError as e2:
            raise OutboundError(EXIT_SEND_FAILED, f"发送失败（机器人不在群里，退回主人本人也没发出去）：{e2}")
    mid = res["message_id"]
    store.add_send(ik, REPLY, topic_id, mid, shown, target_message_id=target_id, identity=who if enabled else None)
    store.add_bot_outbox(mid, topic_id, False, shown, now_iso()); clear_handled_owner_reactions(store, source, topic_id)
    if topic_id != BUS_TOPIC_ID and (thr_id := (res.get("thread_id") or (target["thread_id"] if target is not None and "thread_id" in target.keys() else "") or "")):
        store.add_group_thread(thr_id, chat_id, topic_id, "sent", mid, now_iso())
    return ReplyResult(mid, text=shown, identity=who, why=why)


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
