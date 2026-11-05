"""一事一串与发前再看一眼（7.22）：会话找主人（ask/report，私聊或话题群，带防重编号）、代回（reply，发前检查主人发言并带代回标记）、查看话题（thread）。"""

from __future__ import annotations

import hashlib, logging, os, re, sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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


_CC_PROXY_ASK_RE = re.compile(
    r"(?:申请|开通|分配|询问是否有多余).{0,18}(?:CC|Claude\s*Code|家宽节点|住宅\s*IP|防风控专线|专属网络|专线代理)",
    re.I,
)
_CC_SEAT_EVIDENCE_RE = re.compile(
    r"(?:3x-ui|tp_3x-ui-ops|lsg\d|空余席位|当前.{0,6}人数|使用人数|席位余量|已满)",
    re.I,
)
_ENV_REQUIRED_TITLE_RE = re.compile(
    r"(?:请提供|需要|申请).{0,16}(?:LiveKit.{0,8}凭据|子成员账号)",
    re.I,
)
_EXPLICIT_ENV_DECL_RE = re.compile(
    r"(?:明确是|100%是|仅用于|目标环境|所属环境)[：:\s]*(?:纯)?(?:测试|生产|Stage|预发|tap-testing-env|taplabs-453508|tpn-prod-506507)",
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
    r"(?:\bgh\s+pr\s+merge\b|(?:直接合入|同意合入)[^。\n]{0,24}(?:PR|评审单)[^。\n]{0,24}(?:打\s*(?:Tag|标签)|推送.{0,8}Tag|发布到生产|发上生产))",
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
    r"(?:tp_laohu|老胡|laohu|sheepdog\s+tell|已与.{0,8}(?:老胡|laohu|负责人)[^。\n]{0,8}(?:确认|对齐|核对))",
    re.I,
)
_TEST_APPROVAL_QA_BYPASS_ASK_RE = re.compile(
    r"测试审批[^。\n]{0,24}(?:是否可)?(?:免测|直接放行|先走\s*QA)",
    re.I,
)
_TEST_JUMPBOX_FIREWALL_ASK_RE = re.compile(
    r"(?:tap-testing-env|测试环境)[^。\n]{0,40}(?:tap-testing-dev-jumpbox|测试跳板机|office-allow-all|allow-office-connect)[^。\n]{0,40}(?:tcp:22|防火墙放行|IP\s*白名单)",
    re.I,
)
_DESTRUCTIVE_OR_PROD_FIREWALL_RE = re.compile(
    r"(?:删除|移除|(?<!非)(?<!不涉及)生产|taplabs-453508|tpn-prod-506507|0\.0\.0\.0/0)",
    re.I,
)
_ASK_CLAIMS_RECOMMENDED_OPTION_RE = re.compile(
    r"(?:(?:建议|推荐|倾向|默认)\s*[:：]?\s*(?:点)?选\s*[A-E]|请\s*(?:点)?选\s*[A-E])(?![A-Za-z0-9])",
    re.I,
)
_TEST_ENV_IAM_GRANT_ASK_RE = re.compile(
    r"(?:tap-testing-env|测试环境|测试服务账号)[\s\S]{0,260}"
    r"(?:roles/iam\.serviceAccountTokenCreator|serviceAccountTokenCreator|Token\s*签发权限|TokenCreator|"
    r"roles/run\.(?:developer|viewer|invoker)|roles/secretmanager\.(?:secretAccessor|viewer|secretVersionAdder)|"
    r"add-iam-policy-binding)",
    re.I,
)
_PROD_OR_DESTRUCTIVE_IAM_RE = re.compile(
    r"(?:taplabs-453508|tpn-prod-506507|remove-iam-policy-binding|delete|删除|移除|"
    r"roles/owner|roles/editor|roles/resourcemanager\.projectIamAdmin|"
    r"(?<!非)(?<!不涉及)(?<!登记)(?<!测试与)生产(?:环境|项目|库|集群|\s*IAM|\s*Secret|\s*权限))",
    re.I,
)
_TENCENT_MAIN_ACCOUNT_CONSOLE_ASK_RE = re.compile(
    r"(?:腾讯云|AGS|ags:|cam:|UIN)[\s\S]{0,280}"
    r"(?:UnauthorizedOperation|子账号.{0,16}(?:无|缺|没有).{0,16}权限|主账号控制台)"
    r"[\s\S]{0,220}"
    r"(?:(?:Matthew|主人|您)[^。\n]{0,20}(?:登录|去|在)[^。\n]{0,16}控制台[^。\n]{0,20}(?:创建|建|生成|开通|申请)|"
    r"登录(?:主账号)?控制台[^。\n]{0,20}(?:创建|建|生成|开通))",
    re.I,
)
_LOCAL_TCCLI_CHECK_EVIDENCE_RE = re.compile(
    r"(?:~/\.tccli|\.tccli/|tencent-cn\.credential|tccli\s+凭据|本机\s*(?:tccli|腾讯云主账号)\s*凭据)",
    re.I,
)
_VPS_DB_WHITELIST_SINGLE_IP_ASK_RE = re.compile(
    r"(?:(?:专线|本机出口\s*IP|出口\s*IP|lsg\d+|Titan|家宽)[\s\S]{0,220}"
    r"(?:Cloud\s*SQL|AlloyDB|ADB|RDS|数据库[^\n。]{0,16}白名单|授权网络|authorized\s*networks)"
    r"|(?:Cloud\s*SQL|AlloyDB|ADB|RDS|数据库[^\n。]{0,16}白名单)[\s\S]{0,220}"
    r"(?:专线|本机出口\s*IP|出口\s*IP|lsg\d+|Titan|家宽))",
    re.I,
)
_VPS_FLEET_WHITELIST_EVIDENCE_RE = re.compile(
    r"(?:tp_3x-ui-ops|3x-ui[\s\S]{0,36}(?:全量|全部|全套|14\s*台|清单)|"
    r"(?:全量|全部|全套)\s*(?:需要加白的\s*)?(?:专线|VPS)\s*(?:出口\s*)?IP\s*清单|"
    r"Nicky|tp_nicky)",
    re.I,
)
_LAOHU_PROXY_OR_CONFIRMED_ASK_RE = re.compile(
    r"(?:老胡|laohu|Andy\s*Hu)[\s\S]{0,260}"
    r"(?:助理代发|Claude\s*代[^\n。]{0,16}发|在老胡本人确认后|待老胡本人确认|老胡本人确认)",
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
    r"\belaine_readonly\b)",
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
    r"(?:已查(?:库|验)|已核查\s*auth\.users|GoTrue\s*返回|登录日志显示|报错日志为)",
    re.I,
)
_NEW_VPS_PURCHASE_FULL_ASK_RE = re.compile(
    r"(?:(?:新购|采买)[^\n。]{0,20}(?:专属\s*)?(?:住宅\s*)?VPS[\s\S]{0,180}(?:满编|0\s*空闲|无空闲)|"
    r"(?:满编|0\s*空闲|无空闲)[\s\S]{0,180}(?:新购|采买)[^\n。]{0,20}(?:专属\s*)?(?:住宅\s*)?VPS)",
    re.I,
)
_OVERALL_VPS_ROSTER_EVIDENCE_RE = re.compile(
    r"(?:(?:总体|全网|全部|14\s*台|各节点)[^\n。]{0,20}(?:在用|使用|占用)[^\n。]{0,12}(?:人员)?名单|"
    r"(?=[\s\S]*\blsg1\b)(?=[\s\S]*\blsg2\b)(?=[\s\S]*\blsg3\b)(?=[\s\S]*\blsg4\b))",
    re.I,
)
_US_PROD_GCP_IAM_ASK_RE = re.compile(
    r"(?=[\s\S]*\btpn-prod-506507\b)(?=[\s\S]*(?:开通|授予|赋权|申请)[^\n。]{0,36}(?:日志|只读|IAM|权限|roles/logging\.viewer|viewer))",
    re.I,
)
_SAMMY_US_COMPLIANCE_CONFIRMED_RE = re.compile(
    r"(?:已(?:向|找|经|同)\s*Sammy(?:\s*Shi)?\s*(?:确认|核实|对齐)|Sammy(?:\s*Shi)?\s*(?:已确认|已同意|已批复|回复可以))",
    re.I,
)
_STAGE_SUBACCOUNT_MINIMAL_IAM_ASK_RE = re.compile(
    r"(?=[\s\S]*(?:\b200053747813\b|Stage\s*(?:独立\s*)?成员账号))(?:[\s\S]*(?:最小受限策略|最小策略|受限权限))",
    re.I,
)
_STAGE_SUBACCOUNT_ADMIN_OPTION_RE = re.compile(
    r"(?:AdministratorAccess|QcloudAdministratorAccess|\badmin\b\s*权限|配\s*admin)",
    re.I,
)
_PREMATURE_READONLY_INVESTIGATION_ASK_RE = re.compile(
    r"(?:A\s*[.、:：]\s*(?:仅做|先做|先进行|先由专席)?只读[^\n]{0,24}(?:排查|核实|核查|检查)"
    r"|建议选\s*A[^\n]{0,20}先(?:由专席)?只读(?:核实|排查|核查)具体是哪个"
    r"|[AB12]\s*[.、:：]\s*新开专席(?:排查|核查|摸底)?[^\n]{0,48}(?:排查|核实|摸底|定位)[^\n]{0,32}(?:归属|负责人|代码仓|具体是哪个)"
    r"|A\s*[.、:：]\s*指定负责人\s*[（(][^\n）)]{0,24}直接告知负责人)",
    re.I,
)
_NAT_PORT_DOWNSIZE_ASK_RE = re.compile(
    r"(?:minPortsPerVm|--min-ports-per-vm)[\s\S]{0,80}?(?:调小|下调|降为|改为\s*(?:64|128|256|512)|=\s*(?:64|128|256|512))",
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
    r"(?:(?:同意|批准|按申请|直接)?[^\n]{0,16}(?:给他|为其|向其|给\s*\S+)?(?:授权|开通|开放|授予|下放)[^\n]{0,24}(?:修改权限|写权限|读写权限|所申请权限|全部权限)|^(?:[A-E]\s*[.、:：]\s*)?(?:同意|批准)(?:授权|开通))",
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
    r"(?:ci/?cd|流水线|cloud\s*build|github\s*actions|cnb)[^\n]{0,40}(?:通的|打通|可(?:以)?部署到\s*prod|自动部署)",
    re.I,
)
_EXISTING_VPS_SLOT_AVAILABLE_ASK_RE = re.compile(
    r"(?=[\s\S]*申请[^\n。]{0,24}(?:Claude|VPS|CC|家宽)?[^\n。]{0,12}(?:专线|节点))(?:[\s\S]*(?:余\s*[1-9]\d*\s*(?:空额|空位|席)|空余\s*[1-9]\d*\s*(?:空额|空位|席)))",
    re.I,
)
_REGION_HARDCODE_OR_SUBFILTER_HOTFIX_ASK_RE = re.compile(
    r"(?:Nginx\s+sub_filter[\s\S]{0,120}(?:热修|强制|清空|禁用)"
    r"|(?:热修前端|热修线上)[\s\S]{0,120}(?:彻底禁掉|强制使用|清空\s*Sentry\s*DSN|禁用\s*(?:CF|Cloudflare|Turnstile|Sentry))"
    r"|(?:彻底禁掉|强制使用)[\s\S]{0,120}(?:CF\s*Turnstile|Turnstile|阿里云验证码)[\s\S]{0,120}(?:Sentry|清空\s*Sentry\s*DSN))",
    re.I,
)
_CONFIG_SWITCH_ISOLATION_EVIDENCE_RE = re.compile(
    r"(?:配置开关|环境变量开关|开关(?:来)?控制|feature\s*flag|什么配置下只给国内|按(?:环境|配置)(?:开关|变量)?(?:控制|区分|隔离))",
    re.I,
)
_BROADCAST_SYNC_ASK_RE = re.compile(
    r"(?:日清播报|进度播报|战报播报|群内播报|每日进度[^\n]{0,20}播报|正式同步至群)",
    re.I,
)
_SKIP_OR_CANCEL_SYNC_OPT_RE = re.compile(
    r"(?:^[A-E][\.\、\:：\)\s]\s*[^\n]*(?:暂不同步|暂时不同步|取消(?:本次|今晚|今日)?播报|本次不(?:发|播报|同步)|不播报|不同步))",
    re.I | re.M,
)
_CN_SITE_TENCENT_MAINLAND_VS_INTL_ASK_RE = re.compile(
    r"(?:是用腾讯云国内站还是(?:先用)?国际站"
    r"|在腾讯云国内站账号下开辟"
    r"|对外前再对齐国内站"
    r"|若国内站账号权限已就绪建议\s*[A-E])",
    re.I,
)
_TENCENT_INTL_SUBACCOUNT_POSTURE_RE = re.compile(
    r"(?:跟\s*tapnow\.tv\s*一样[\s\S]{0,40}腾讯云国际站[\s\S]{0,40}子成员账号"
    r"|统一(?:放在|使用)腾讯云国际站[\s\S]{0,40}(?:子成员账号|成员账号|200053747873|200053747813))",
    re.I,
)
_TENCENT_EDGEONE_RESOURCE_CHANGE_ASK_RE = re.compile(
    r"(?:(?:在|于)?(?:生产|预发|Stage|Prod)?\s*EdgeOne[^\n。]{0,36}(?:新增|添加|开通|创建|配置|修改|绑定)[^\n。]{0,36}(?:加速域名|子域名|域名|站点|回源|[a-z0-9.-]+\.tapnow\.tv)"
    r"|EdgeOne\s*[（(]?\s*tapnow\.tv\s*站点[）)]?\s*(?:新增|添加|开通|创建|配置)"
    r"|(?:配置|添加|开通)\s+[a-z0-9.-]+\.tapnow\.tv\s+(?:加速域名|子域名))",
    re.I,
)
_TENCENT_SUBACCOUNT_OR_UIN_EVIDENCE_RE = re.compile(
    r"(?:\b2000\d{8}\b|(?:子成员账号|成员子账号|子账号|主账号)\s*(?:[:：]?\s*)?(?:2000\d{8}|tapnow-(?:prod|stage)))",
    re.I,
)
_TENCENT_MAINLAND_FALLBACK_ON_LIMIT_ASK_RE = re.compile(
    r"(?=[\s\S]*(?:境内主账号|国内腾讯云|100048360698|境内账号)[^\n。]{0,40}(?:建|创建|新建|开辟|申请)[^\n。]{0,30}(?:子账号|账号|凭据|密钥|TMS|IMS|内容安全))"
    r"(?=[\s\S]*(?:UnsupportedRegion|地域不支持|国际账号[^\n。]{0,24}(?:不支持|报错|受限)|TMS|IMS|内容安全))",
    re.I,
)
_URGENCY_AND_TENCENT_CONTACT_EVIDENCE_RE = re.compile(
    r"(?=[\s\S]*(?:已(?:向|跟|与)?[^\n。]{0,16}确认[^\n。]{0,16}(?:着急|急需|紧急)|先问(?:一下)?[^\n。]{0,16}(?:是否)?着急|询问[^\n。]{0,16}是否着急))"
    r"(?=[\s\S]*(?:问一下腾讯云|咨询腾讯云|联系腾讯云|找腾讯云(?:的人|接口人|架构师|客服)?))",
    re.I,
)
_NEW_DOMESTIC_DOMAIN_ASK_RE = re.compile(
    r"(?:(?:是否)?(?:新增|新建|开通|配置|给一个)[^\n。]{0,20}国内(?:跳转|直连|可达)?域名"
    r"|国内(?:跳转|直连|可达)?域名[^\n。]{0,20}(?:是否)?(?:新增|新建|开通|配置)"
    r"|(?:新增|新建|开通)[^\n。]{0,20}\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.tapnow\.media\b[^\n。]{0,20}(?:国内|直连|跳转))",
    re.I,
)
_DOMESTIC_DOMAIN_AUDIENCE_EVIDENCE_RE = re.compile(
    r"(?:给谁访问|访问(?:受众|对象|群体|人群)|受众(?:群体|对象|是谁)|目标(?:受众|访问群体|用户群体)"
    r"|已(?:向|跟)[^\n。]{0,16}确认[^\n。]{0,20}(?:给.*访问|访问对象|受众)"
    r"|调用链\s*[:：]\s*国内用户\s*->|面向国内(?:站)?(?:C\s*端)?(?:用户|客户))",
    re.I,
)
_NEW_TAPNOW_MEDIA_DOMESTIC_SUBDOMAIN_ASK_RE = re.compile(
    r"(?:(?:是否|同意)?(?:新增|新建|开通|配置)[^\n。]{0,24}\b([a-z0-9-]+)\.tapnow\.media\b"
    r"|\b([a-z0-9-]+)\.tapnow\.media\b[^\n。]{0,24}(?:是否|同意)?(?:新增|新建|开通|配置))",
    re.I,
)
_TAPNOW_TV_REUSE_EVIDENCE_RE = re.compile(
    r"(?:\b[a-z0-9-]+\.tapnow\.tv\b|(?:能不能|是否可(?:以)?|改用|复用|优先用)\s*(?:[a-z0-9-]+\.)?tapnow\.tv\b)",
    re.I,
)
_TEST_ENV_NACOS_ACCOUNT_ASK_RE = re.compile(
    r"(?=[\s\S]*(?:测试环境|tap-testing-env|测试\s*Nacos|tap-testing-nacos))"
    r"(?=[\s\S]*(?:申请|创建|开通)[^\n。]{0,28}Nacos[^\n。]{0,20}账号)"
    r"(?![\s\S]*(?:生产环境|taplabs-453508|tpn-prod|生产\s*Nacos))",
    re.I,
)
_DOMESTIC_TV_TO_OVERSEAS_GCP_ASK_RE = re.compile(
    r"(?=[\s\S]*\b[a-z0-9-]+\.tapnow\.tv\b)"
    r"(?=[\s\S]*(?:GCP\s*生产|taplabs-453508|8\.233\.219\.66|sgtm-https-proxy|GCP\s*代理))",
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
    r"(?=[\s\S]*(?:COMMUNITY_API_KEY|API_KEY)[^\n。]{0,28}(?:双\s*Key\s*兼容|兼容恢复|兼容放行))"
    r"(?=[\s\S]*(?:发版单|硬切为|硬换成))",
    re.I,
)
_GROUP_HISTORY_EXPLAINED_EVIDENCE_RE = re.compile(
    r"(?:已在群(?:里|内)[^\n。]{0,20}(?:讲清|说明|同步|回复|汇报)[^\n。]{0,20}(?:来龙去脉|前因后果|相关版本)"
    r"|来龙去脉[^\n。]{0,20}已在群(?:里|内)(?:讲清|说明|同步|回复))",
    re.I,
)
_LSG14_SOLO_VIP_NODE_ASK_RE = re.compile(
    r"(?:(?:lsg14|192\.220\.48\.237)(?:(?!lsg(?!14\b)\d+)[^\n。；]){0,45}?(?:尚余|空余|余\s*[1-9]\d*\s*[席名个位]|有空位|空余\s*[1-9]|共用|(?<![不未禁可])开通|(?<![不未禁可])分配)"
    r"|(?:在\s*lsg14|选\s*[A-E][^\n。；]{0,16}lsg14|分配至\s*lsg14)[^\n。；]{0,40}(?:开通|分配|席位|专线))",
    re.I,
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
_SAMMY_PARITY_AND_ENTRYPOINT_ASK_RE = re.compile(
    r"(?=(?:[\s\S]*\b(?:Sammy(?:\s*Shi)?|Sammi)\b))"
    r"(?:[\s\S]*(?:能否提供现有[^\n。]{0,24}(?:跳板机|TKE|集群|入口)"
    r"|临时开启[^\n。]{0,24}(?:cls-[a-z0-9]+|TKE|集群)[^\n。]{0,16}公网访问"
    r"|在[^\n。]{0,24}(?:cls-[a-z0-9]+|tapnowprod|TKE)[^\n。]{0,24}新建\s*Secret[\s\S]{0,60}入口方案"
    r"|给\s*(?:Sammy|Sammi)[^\n。]{0,24}(?:开通|开放|提供)[^\n。]{0,24}(?:权限|跳板机|入口)))",
    re.I,
)
_VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE = re.compile(
    r"(?=[\s\S]*(?:谁来跟(?:完|进|到底)?|几点前完成|\.cnb\.yml|PR\s*构建修复))"
    r"(?=[\s\S]*由\s*(?:Sammy(?:\s*Shi)?|Sammi|老胡|Andy\s*Hu)\s*自己来(?:跟|合入|做|处理|接手))",
    re.I,
)
_MULTI_COMPONENT_CAPACITY_PLAN_ASK_RE = re.compile(
    r"(?=[\s\S]*(?:压测|全栈|生产|容量|扩容|升配)[^\n。]{0,24}(?:升配|扩容|容量|规格)[^\n。]{0,16}方案)"
    r"(?=[\s\S]*\b(?:PG|PostgreSQL)\b)"
    r"(?=[\s\S]*\b(?:MySQL|ADB|AnalyticDB)\b)"
    r"(?=[\s\S]*\bRedis\b)",
    re.I,
)
_LARK_DOC_LINK_EVIDENCE_RE = re.compile(
    r"(?:(?:larksuite\.com|feishu\.cn)/(?:docx|docs|wiki)/[A-Za-z0-9]+|飞书(?:云)?文档|Lark\s*文档)",
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
) -> None:
    """7.108 / 7.110 / 7.113 / 7.114 / 7.115 / 7.116 / 7.117 / 7.118（REG-076 ~ REG-086）：从人类改判错题本自进化提炼的 ask 发前防打扰门禁：
    0. 发起待拍板 ask 卡片（含写明『建议选 A』或启用聚合台/白话总结折叠模式）时，必须提供至少 2 个可解析的 A/B 互斥选项，严禁发无选项按钮的空头决策卡；
    1. 申请 CC 专线 / 家宽节点 / 住宅 IP 前，必须先向 tp_3x-ui-ops 核实各线路当前人数与空余席位并写入正文；
    2. 申请 LiveKit 凭据或子成员账号时，必须显式标明『明确是测试环境 / 明确是生产环境 / 目标环境：...』；
    3. 同一 (topic_id, key) 在途未消费主人刚给出的批复、主人已批复『这个不动/忽略』、或近 2 小时已点击选项 A 批准由专席执行时，拦截重复 ask；
    4. 请示执行 gh pr merge 或直接合入 PR 打 Tag 发布生产前，必须显式写明『人工 Code Review 状态』（如 reviewDecision: APPROVED 或人工 Review 0/N 未过）；
    5. 请示确认 CORS / Origin 域名白名单或『是否有其他前端域名调用』前，必须先扫描云上真实流量日志 + 本地记录并与技术负责人（如 tp_laohu）核对；
    6. 请示发版单『测试审批是否可免测直接放行还是先走 QA』前，必须先与该单技术负责人（如 tp_laohu）核对；
    7. 测试环境（tap-testing-env）跳板机 / office-allow-all 白名单追加属于预授权非破坏性测试环境操作，拦截向主人 ask 请示；
    8. 测试环境（tap-testing-env）为内部成员或服务账号授予非破坏性 IAM 角色（如 roles/iam.serviceAccountTokenCreator、secretAccessor）属于预授权测试环境操作，拦截向主人 ask 请示；
    9. 因腾讯云子账号权限不足（UnauthorizedOperation）请示主人登录控制台创建 API Key / 资源前，必须先核查本机 ~/.tccli/ 下的主账号凭据（如 tencent-cn.credential）；
    10. 内部同学因切换办公专线 / VPS 出口 IP 申请数据库白名单（Cloud SQL / AlloyDB / ADB / RDS）前，必须先向 tp_3x-ui-ops 拉齐整套 VPS 出口 IP 清单并协调 Nicky 老师（tp_nicky）全量数据库一次性加白，严禁按单人单 IP 散装打扰；
    11. 老胡（laohu / Andy Hu）的需求（含助理代发待老胡本人确认）属于 VIP 预授权范围（『laohu 的需求 只要经过他本人确认 你就给他弄』），先向老胡本人确认后直接执行并 report，严禁未触及 3 条底线就向主人 ask；
    12. 常规发版排期/窗口报备（PR 仍在等待他人 Review 合入、无架构/封网冲突，选项 B 为『调整排期时间或有其他要求』）无实质决策点（『这玩意有啥决策点吗 还需要问我』），拦截象征性 ask；
    13. 申请数据库内部 User / 只读账号密码与连接串（含尚未入 DBA 台账的新库）统一归口 Nicky 老师（tp_nicky）纳管发放（『如果是数据库里面的 User 账号密码什么的，那就找 Nicky』），拦截向主人 ask。"""
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
                "sheepdog ask 缺少可点击的 A/B 决策选项（主人批评：『你没有给我提供选项啊』，REG-083 防缺选项门禁）："
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
    if floor or floor_type:
        return
    if _SYMBOLIC_RELEASE_SCHEDULE_ASK_RE.search(full_text) and not _GENUINE_SCHEDULE_CONFLICT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "常规发版窗口排期报备（PR 尚在等待同行 Code Review 合入、无封网/架构冲突，选项 B 仅为『调整排期时间或有其他要求』）无实质决策点"
            "（主人 2026-11-03 20:13:53 明确批评：『这玩意有啥决策点吗 还需要问我 是哪里不合理吗 还是说你只是象征性问问』）："
            "请直接在群内或私聊配合对齐，并用 `sheepdog report`（`--ball other` 或 `doing`）报备进展；"
            "待 PR 完成人工 Code Review 合入、真正进入生产触发/切流授权或遇到真实架构冲突时再发起请示（REG-086 防象征性发问门禁，没有发送）",
        )
    if topic_id != "tp_nicky" and _DB_USER_ACCOUNT_ASK_RE.search(full_text) and not _REASK_BLOCKER_EXEMPT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "申请数据库内部 User 账号密码 / 只读账号（如 elaine_readonly）或数据库连接串属于 DBA 统一纳管职责"
            "（主人 2026-11-03 19:26:17 与 20:13:12 明确指示：『如果是 IAM 权限，那就给他。如果是数据库里面的 User 账号密码什么的，那就找 Nicky』『仍交由 Nicky 老师纳入 DBA 资产台账后再统一开通发放』）："
            "① 若在总线 `tp_bus`，请直接 `sheepdog forward` 转交对应专席处理，严禁在总线直接向主人 ask；"
            "② 若涉及云平台 IAM 角色权限，请按最小权限直接授权并回读后 `sheepdog report`；"
            "③ 若涉及数据库内部 User 账密 / 只读账号（含尚未入 DBA 台账的新库），请直接通过 `sheepdog tell --to tp_nicky` 交由 Nicky 老师纳入 DBA 资产台账后统一开通发放，严禁绕过 DBA 用 migrator 凭据代建或向主人 ask（REG-086 防打扰门禁，没有发送）",
        )
    if _LOGIN_PWD_RESET_ASK_RE.search(full_text) and not _LOGIN_DB_LOG_VERIFIED_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因用户反馈登录提示密码不正确请示重设临时密码前，必须先执行只读库表与日志排查（核查 `auth.users` 账号是否存在、最近登录时间、GoTrue 真实报错日志是否仅输错账号/密码），"
            "并在正文中写明目标站点域名（如 `cinemap-stage.tapnow.tv`）、只读查库结论与申请人全名，严禁未查库核实前因后果就发模糊 ask 让主人反问『啥意思』（REG-086 防打扰门禁，没有发送）",
        )
    if _NEW_VPS_PURCHASE_FULL_ASK_RE.search(full_text) and not _OVERALL_VPS_ROSTER_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因现有专线/住宅节点满编（0空闲）请示新购/采买 VPS 时，必须在 `---more---` 中一并附上『目前总体 VPS 在用人员名单（各节点当前占用人员明细）』"
            "（主人 2026-11-03 22:19:26 明确要求：『那我去买新的 你可以再给我一次目前总体的vps在使用人员名单』）："
            "请先从 `tp_3x-ui-ops` 或 VPS 台账拉齐当前全网各节点（如 lsg1~lsg6、res1 等）在用人员名单并附在 `---more---` 中，严禁只写『全部满编』却不附总体使用人员名单（REG-087 防打扰门禁，没有发送）",
        )
    if _US_PROD_GCP_IAM_ASK_RE.search(full_text) and not _SAMMY_US_COMPLIANCE_CONFIRMED_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "美区生产 GCP 项目（tpn-prod-506507）受物理跨境合规约束，默认禁止向物理上不在美国的同事个人账号直接开通访问权限"
            "（主人 2026-11-03 23:59:27 明确指示：『这个东西得问一下 Sammy 看一下他那个只读权限可不可以给别人开，就是美区，原本定的是美区的 Project 不能用我们任何一个物理上不在美国的同事的账号去访问』）："
            "请先通过 `sheepdog tell --to tp_sammy` 或在原群向 Sammy Shi 确认美区只读权限合规口径（或由专席代拉所需时间窗脱敏日志包交付），"
            "严禁未向 Sammy 核实合规口径前发 ask 让主人拍板或写『由主人操作』（REG-088 防打扰门禁，没有发送）",
        )
    if _STAGE_SUBACCOUNT_MINIMAL_IAM_ASK_RE.search(full_text) and not _STAGE_SUBACCOUNT_ADMIN_OPTION_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "在物理隔离的腾讯云 Stage 独立成员账号（200053747813）为内部研发同学开通联调/部署权限时，首选建议应直接授予该 Stage 成员子账号的 `AdministratorAccess`（admin 权限，严格隔离主账号与生产）"
            "（主人 2026-11-04 03:05:29 明确改判：『给他在stage配admin』）："
            "请在选项 A 中直接提供或建议在 Stage 独立成员账号（200053747813）授予 `AdministratorAccess`（admin），避免在已隔离的 Stage 子账号里抠碎片最小策略导致联调反复卡壳（REG-090 防打扰门禁，没有发送）",
        )
    if _PREMATURE_READONLY_INVESTIGATION_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "只读指标/实例/日志排查（Lane D）与转交专席核实属于 Agent 自治职责，严禁在尚未查清具体服务名（如『具体是哪个 gateway』）与实时指标前发 `sheepdog ask` 让主人拍板『A. 仅做只读指标与实例排查』"
            "（因未先只读核查而发 ask 等批，会导致对方等待后自行让其 Agent 操作，主人批复『他说他的agent操作了 你可以帮忙盯着点』）："
            "请立即直接 `sheepdog forward` 转交对应专席（或由当前专席直接执行只读核查），拿到真实服务名、当前实例数/配额与明确目标参数后，若确实需要执行生产变更，再由专席带齐实测数据发起一次精准 `sheepdog ask`（REG-094 防打扰门禁，没有发送）",
        )
    if _NAT_PORT_DOWNSIZE_ASK_RE.search(full_text) and not _NAT_PEAK_PER_VM_PORT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示调小/下调 Cloud NAT 网关 `minPortsPerVm`（`--min-ports-per-vm`）前，必须先通过 Cloud Monitoring 只读核查当前单实例（per-VM）最高端口使用数 / 已分配端口峰值及动态端口分配上限（`maxPortsPerVm`）"
            "（主人反问：『那会不会有什么影响呢 有办法看最高端口使用数的一个实例用了多少个端口吗』）："
            "请先查清当前单实例最高端口使用数（如 `nat/port_usage` / `allocated_ports` 单 VM 最大值）与调小后的实际影响评估并写入正文，严禁未附单实例峰值端口实测数据就发 ask（REG-096 防打扰门禁，没有发送）",
        )
    if _UNINVESTIGATED_STATUS_ASK_RE.search(full_text) and not _CONCRETE_STATUS_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示确认『渠道/配置/路由现状』（如『需确认...现状，以便...』）前，必须先由当前专席只读排查目标集群/环境的真实配置、路由与缺失项，"
            "弄清楚对方具体缺什么参数并整理出具体可执行的方案后再请主人决策（主人批评：『你先排查一下他需要什么弄清楚给我执行方案让我决策』，REG-099 防未排查甩锅门禁，没有发送）",
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
            "生产服务因跑着旧镜像且关联服务留有 Stage 环境变量导致报错（如 404/500）时，严禁建议『只手改部分配置、把旧镜像留着不切』且不核查 CI/CD 部署链路"
            "（主人纠偏：『直接跑新镜像呗 并且保证相关服务的cicd是通的 可以部署到prod』，REG-100 防半拉子手改门禁）："
            "请先核实相关服务部署到 prod 的 CI/CD 流水线是否畅通，并在正文中写明 CI/CD 状态及直接推进新镜像上线 + 修正生产配置的完整闭环方案后再发起 ask（没有发送）",
        )
    if _EXISTING_VPS_SLOT_AVAILABLE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部同学申请 VPS / Claude 专线且现有节点仍有空位（如『余 2 空额』）时，属于预授权自治范围"
            "（主人 2026-11-04 17:18 明确指示：『有空位就给他』，REG-100 防打扰门禁）："
            "请直接在有空位的节点为其开通专属账号并交付，完成后调用 `sheepdog report` 报备；"
            "仅在全网现有节点全部满编（0 空位）需新购 VPS 时才附全量人员名单发起 `sheepdog ask`（没有发送）",
        )
    if _REGION_HARDCODE_OR_SUBFILTER_HOTFIX_ASK_RE.search(full_text) and (
        "sub_filter" in full_text.lower() or not _CONFIG_SWITCH_ISOLATION_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "针对国内外共享前端/服务（如 app-tapnow）处理区域专属三方组件（如 CF Turnstile / 阿里云验证码 / Sentry DSN）时，"
            "严禁提出『Nginx sub_filter 线上脏热修』或『无开关写死彻底禁掉/强制替换』"
            "（主人纠偏：『这个修法你需要保证可以有个开关来控制 什么配置下只给国内的就行』，REG-101 防无开关硬编码与脏热修门禁）："
            "必须设计可由环境变量/配置项控制的开关方案（明确什么配置下只对国内生效、不影响海外环境，且不采用 Nginx sub_filter 篡改静态产物）后再发起 ask（没有发送）",
        )
    if _BROADCAST_SYNC_ASK_RE.search(full_text) and not _SKIP_OR_CANCEL_SYNC_OPT_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "就群内日清播报 / 进度播报 / 阶段战报发起 `sheepdog ask` 请示发布时，选项严禁只提供『同意发布 / 先做修改或补充内容』而缺少一键取消/暂不同步选项"
            "（主人 2026-11-04 20:04 在 `[其他]` 批复：『暂时不同步了』，REG-103 播报类请示选项完备性门禁）："
            "必须在选项中显式提供『暂不同步 / 取消本次播报（本次不发群）』的一键按钮选项，避免主人被迫手打『暂时不同步了』（没有发送）",
        )
    if _CN_SITE_TENCENT_MAINLAND_VS_INTL_ASK_RE.search(full_text) and not _TENCENT_INTL_SUBACCOUNT_POSTURE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对 `.tv` / Cinemap 中国站生产或预发资源开辟，严禁将『在腾讯云国内站账号开辟 vs 先用国际站临时过渡、对外前再对齐国内站』作为二选一请示抛给主人"
            "（主人 2026-11-04 20:29 在 `[其他]` 明确纠偏：『直接就跟tapnow.tv一样 都放在腾讯云国际站 的子成员账号环境里』，REG-104 腾讯云国际子成员账号基线门禁）："
            "TapNow 与 Cinemap 国内站（`.tv`，`ap-nanjing`）统一承载在腾讯云国际站（主账号 `200045686254` 组织集团）下的独立子成员账号环境（Stage: `200053747813`，Prod: `200053747873`），"
            "不开辟独立的腾讯云国内站账号。请直接按腾讯云国际站子成员账号环境整理资源清单与权限方案后再汇报或请示（没有发送）",
        )
    if _TENCENT_EDGEONE_RESOURCE_CHANGE_ASK_RE.search(full_text) and not _TENCENT_SUBACCOUNT_OR_UIN_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示在 EdgeOne（如 `tapnow.tv` 站点 / `*.tapnow.tv` 加速域名）新增、配置或修改加速域名与回源 CLB 前，必须在正文中显式写明目标所在的腾讯云『子成员账号 ID / UIN』（如生产子账号 `200053747873`、Stage 子账号 `200053747813` 或主账号 `200045686254`）及对应 Zone ID / CLB IP"
            "（主人 2026-11-05 01:40 在 `[其他]` 反问：『哪个子成员环境』，REG-108 腾讯云子成员账号环境显式标注门禁）："
            "请先核实并在正文中明确标注目标 EdgeOne 站点与回源 CLB 所属的子成员账号 ID（如 `生产子账号 200053747873`）后再发起 `sheepdog ask`（没有发送）",
        )
    if _TENCENT_MAINLAND_FALLBACK_ON_LIMIT_ASK_RE.search(full_text) and not _URGENCY_AND_TENCENT_CONTACT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因腾讯云国际站子账号调接口报 `UnsupportedRegion` / 地域或功能受限（如 TMS/IMS 内容安全）而请示回退到『境内主账号（100048360698）/ 国内腾讯云』新建子账号前，"
            "必须先向需求方确认『这件事是否着急（现有本地词表/兜底是否够用）』，并在方案中提供『不着急则先联系/问一下腾讯云的人（开通国际站权限或支持地域）』的收束选项"
            "（主人 2026-11-05 12:38 在 `[其他]` 纠偏：『问一下这个着急吗 不着急的话我可以先问一下腾讯云的人』，REG-115 防遇限盲目回退国内腾讯云主账号门禁）："
            "请先向需求方询问是否着急，并在正文中写明需求方关于紧急度的明确答复及咨询腾讯云接口人的选项后再发起 `sheepdog ask`（没有发送）",
        )
    if _NEW_DOMESTIC_DOMAIN_ASK_RE.search(full_text) and not _DOMESTIC_DOMAIN_AUDIENCE_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示新增/开通『国内直连/跳转域名』（如 `edm.tapnow.media` 等）前，必须先向申请人核实并在正文中写明『该国内域名具体是给谁访问的（目标访问受众是谁：真实国内 C 端客户、海外客户还是内部运营不挂梯子测试）』"
            "（主人 2026-11-05 12:40 在 `[其他]` 反问：『问一下国内域名是给谁访问的』，REG-115 新增国内域名必核访问受众门禁）："
            "请先向申请人问清该国内域名的真实访问受众（`给谁访问 / 访问受众`）并写入正文后再发起 `sheepdog ask`（没有发送）",
        )
    if (m_media_sub := _NEW_TAPNOW_MEDIA_DOMESTIC_SUBDOMAIN_ASK_RE.search(full_text)) and not _TAPNOW_TV_REUSE_EVIDENCE_RE.search(full_text):
        sub_prefix = (m_media_sub.group(1) or m_media_sub.group(2) or "edm").lower()
        raise OutboundError(
            EXIT_INVALID,
            f"请示新增/配置『{sub_prefix}.tapnow.media』国内子域名前，必须先向需求方询问确认『能不能直接用 {sub_prefix}.tapnow.tv（直接复用腾讯云国际站生产子账号 200053747873 下现有的 tapnow.tv 正式 EdgeOne 站点）』，"
            f"并在正文中写明需求方对使用 `{sub_prefix}.tapnow.tv` 的确认结论或将其列为推荐选项 A"
            f"（主人 2026-11-05 13:33 在 `[其他]` 纠偏：『问一下能不能用 edm.tapnow.tv』，REG-116 国内新子域名优先收敛复用 tapnow.tv 门禁，没有发送）",
        )
    if _TEST_ENV_NACOS_ACCOUNT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部研发/自动化流程（如 Loops）申请在测试环境（`tap-testing-env` / 测试 Nacos）创建专用或受限 Nacos 账号及凭据属于测试环境预授权范围"
            "（主人 2026-11-05 14:42 在 `[其他]` 明确指示：『给他账号密码』，REG-117 测试环境 Nacos 账号预授权自治门禁）："
            "请直接在测试环境 Nacos 为其创建对应测试命名空间的受限账号并把账号密码交付给申请人，完成后调用 `sheepdog report` 报备，严禁发 `sheepdog ask` 打扰主人（没有发送）",
        )
    if _DOMESTIC_TV_TO_OVERSEAS_GCP_ASK_RE.search(full_text) and not _DATA_CROSS_BORDER_COMPLIANCE_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示将国内站点子域名（`*.tapnow.tv`，如 `track.tapnow.tv`）解析指向海外 GCP 生产代理/负载均衡（如 `taplabs-453508` / `8.233.219.66` / `tapnow-ga4-sgtm-https-proxy`）前，"
            "必须先确认是否存在国内用户数据出境合规风险，并评估是否应按 `tapnow.tv` 统一基线收束部署在腾讯云国际站生产子账号（`200053747873`）境内节点"
            "（主人 2026-11-05 15:23 在 `[其他]` 明确纠偏：『这个有没有合规问题 要确认一下，原则上我理解数据是不能出境的 得看是不是要不是再腾讯云国际这边』，REG-117 国内 .tapnow.tv 域名数据出境合规与腾讯云国际收束门禁）："
            "请先核实数据出境合规边界及腾讯云国际站承接方案并写入正文后再发起 `sheepdog ask`（没有发送）",
        )
    if _LSG14_SOLO_VIP_NODE_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "`lsg14`（`192.220.48.237`）已由主人明确锁定为 VIP Sammy Shi 100% 单人独占节点（`1/1 独占锁定，禁止加人`），严禁将 `lsg14` 当作尚有空余/共用席位的候选节点向主人发起 `sheepdog ask`"
            "（主人 2026-11-05 17:18 在 `tp_sherwin` 误报 `lsg14 空余 1 席` 的旧卡上被迫点击 `[其他] 我去新买一个vps吧`，随后在 `tp_3x-ui-ops` 实据更正卡上改选 `A. 将 Carl 分配至 lsg4 唯一空位`，REG-118 `lsg14` 独占锁定与专线席位必先向 `tp_3x-ui-ops` 核实门禁）："
            "请先通过 `sheepdog tell --to tp_3x-ui-ops` 核实全网节点的最新真实空余席位（如 `lsg4` 等），严禁凭旧记忆提议分配 `lsg14`（没有发送）",
        )
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
                "（主人 2026-11-05 16:32 在 `[其他]` 明确批评：『还不能动 把来龙去脉在群里讲清楚先』，REG-117 先在群内讲清来龙去脉再请示变更门禁）："
                "请先用 `sheepdog reply` 在群内完整回复版本改动来龙去脉，并在正文中写明『已在群内讲清来龙去脉』后再发起 `sheepdog ask`（没有发送）",
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
                            "（主人 2026-11-05 16:36 在 `[其他]` 明确纠偏：『这个已经在…群里在讨论 这和…讨论的事情是同一个 统一转交给同一个session处理』，REG-117 跨会话同根因事件防撞车门禁）："
                            f"请先通过 `sheepdog tell --to {other_tid}` 或 `sheepdog forward --topic {other_tid}` 将线索统一归口到 `{other_tid}` 合并处理，"
                            f"若确需独立请示请在正文中显式说明已与 `{other_tid}` 对齐边界（没有发送）",
                        )
            except OutboundError:
                raise
            except Exception:
                pass
    if _SAMMY_PARITY_AND_ENTRYPOINT_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "Sammy Shi（👑 VIP，含其 AI 助理 Sammi）拥有与主人完全对等的最高权限与运维入口"
            "（主人 2026-11-04 20:50 明确立规：『我们咋做你就给sammy咋做 sammy的权限至少要跟我一摸一样』，REG-105 VIP Sammy 权限与入口平权门禁）："
            "凡未触及 3 条硬底线（`--floor-type credentials|destructive|vip_conflict`），我们平时用什么入口/方式操作"
            "（如南京生产 TKE `cls-gnui5plk` 跳板机 `ins-cey0hf6s` TAT/SSH、限单 IP 临时白名单、新建 K8s Secret 或对齐同级云权限），"
            "请直接给 Sammy / Sammi 提供完全一致的入口与同级权限并协助办结，完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（没有发送）",
        )
    if _VIP_OWN_PR_FOLLOWTHROUGH_SHIRK_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "针对我方已修改/推送的 PR（如 `.cnb.yml` 构建修复等）或 VIP（Sammy / Sammi / 老胡）在群内询问『谁来跟完、几点前完成』的收尾工作，"
            "严禁把『A. 由我们跟到底 vs B. 由 Sammy 自己来跟合入与验证』当作选择题发 `sheepdog ask` 打扰主人"
            "（主人 2026-11-05 02:35 在群内直接指示：『你直接起 subagent review 完没问题直接合 合完看看有没有解决问题就行』，并在拍板卡批复『我在群里回了呀』，REG-110 我方在途 PR 闭环与禁甩锅 VIP 门禁）："
            "自己经手的 PR 必须主动闭环跟到底！请直接派生 Subagent 完成 PR 审查、构建验证与合入，并在群内回复进展与明确完成时间，严禁发 `sheepdog ask` 询问要不要甩给 VIP 自己做（没有发送）",
        )
    if _MULTI_COMPONENT_CAPACITY_PLAN_ASK_RE.search(full_text) and not _LARK_DOC_LINK_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "涉及多组件（PG + MySQL/ADB + Redis 等）生产/全栈压测升配与容量规划方案请示时，严禁只塞在飞书卡片正文里让主人盲批"
            "（主人 2026-11-04 21:29 在 `[其他]` 明确要求：『给我完整的方案吧 用lark文档 我要仔细看看』，REG-106 多组件容量方案必附飞书文档门禁）："
            "请先将完整的全栈容量对比、连接数/IOPS/内存水位测算、各组件目标规格与回缩预案整理成飞书云文档（Lark Docx）并授权主人可访问，"
            "在 `sheepdog ask` 正文中附上飞书文档链接（`https://.../docx/...`）后再发起请示（没有发送）",
        )
    if _MAIL_ASSISTANT_ONLY_ACCESS_ASK_RE.search(full_text) and not _FEISHU_DIRECT_REQUEST_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "严禁针对仅由邮件助手（Mail Assistant / 飞书邮箱）推送的自动权限或预览访问申请邮件（如 Vercel Access Request 等）且当事人未在飞书主动找过来的情况发起 `sheepdog ask`"
            "（主人 2026-11-05 00:50 在 `[其他]` 明确立规：『邮箱来的消息吗 暂时不管』，REG-107 邮件助手自动权限申请静默门禁）："
            "凡仅从邮箱/邮件助手收到的自动申请通知、申请人未在飞书私聊或群内主动提出诉求的，请直接静默归档并交 `handled` 回执，严禁发 `sheepdog ask` 打扰主人或主动私聊当事人（没有发送）",
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
    if topic_id in (BUS_TOPIC_ID, "tp_3x-ui-ops"):
        return
    if _VPS_DB_WHITELIST_SINGLE_IP_ASK_RE.search(full_text) and not _VPS_FLEET_WHITELIST_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "内部同学切换办公专线 / VPS 出口 IP 申请数据库白名单（Cloud SQL / AlloyDB / 阿里云 ADB / RDS）时，严禁按单人单 IP 散装发起 ask"
            "（主人 2026-11-03 18:16:54 明确指示：『你可以整理一全套的需要加白的ip的vps清单让nicky老师所有数据库都一次性加上 别动不动烦别人』）："
            "请先通过 `sheepdog tell --to tp_3x-ui-ops` 拉齐当前全套需要加白的办公专线 VPS 出口 IP 清单（14 台节点），"
            "整理成完整清单后请 Nicky 老师（`tp_nicky`）所有数据库一次性加上，并在完成后用 `sheepdog report` 报备（REG-085 防打扰门禁，没有发送）",
        )
    if _LAOHU_PROXY_OR_CONFIRMED_ASK_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "老胡（laohu / Andy Hu）的需求属于 VIP 预授权范围"
            "（主人 2026-11-03 18:18:00 明确指示：『laohu 的需求 只要经过他本人确认 你就给他弄』）："
            "① 若消息由老胡的 AI 助理代发（如 Claude 代发 / ✦ 前缀）尚未经老胡本人确认，请先直接用 `sheepdog reply` 在原会话向老胡本人确认是否执行，"
            "严禁把『助理代发 / 待老胡本人确认』推给主人拍板；"
            "② 一旦经老胡本人确认，除触及 3 条硬底线（`--floor-type credentials|destructive|vip_conflict`）外，"
            "请直接按需求闭环执行并在完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（REG-085 防打扰门禁，没有发送）",
        )
    if _TENCENT_MAIN_ACCOUNT_CONSOLE_ASK_RE.search(full_text) and not _LOCAL_TCCLI_CHECK_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "因腾讯云子账号权限不足（UnauthorizedOperation）请示主人登录控制台创建 API Key / 云资源前，"
            "必须先核查本机 `~/.tccli/` 下的主账号凭据（如 `~/.tccli/tencent-cn.credential` / `default.credential`）："
            "若本机已有主账号 AK/SK 且属于低风险常规研发/测试沙箱资源，请直接或请示授权后用本机 `~/.tccli/` 凭据调 API/SDK 代建并回读，"
            "或把『由专席使用本机 ~/.tccli/ 主账号凭据代建』列为推荐选项 A，并在正文中写明本机 `~/.tccli/` 凭据核查结果，严禁未查本机凭据就把手工登录控制台活推给主人（REG-084 防打扰门禁，没有发送）",
        )
    if _TEST_ENV_IAM_GRANT_ASK_RE.search(full_text) and not _PROD_OR_DESTRUCTIVE_IAM_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "测试环境（tap-testing-env）为内部成员或服务账号授予非破坏性 IAM 角色（如 roles/iam.serviceAccountTokenCreator、Cloud Run、Secret Manager 权限）"
            "属于预授权测试环境操作（『测试环境可以直接动。做完马上回读，如实报结果』）："
            "请直接带 `--account=matthewohmygosh@gmail.com --project=tap-testing-env` 执行 IAM 绑定并 `get-iam-policy` 独立回读核验，"
            "完成后调用 `sheepdog report` 报备，严禁调用 `sheepdog ask` 打扰主人（REG-083 防打扰门禁，没有发送）",
        )
    if _CC_PROXY_ASK_RE.search(full_text) and not _CC_SEAT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "申请开通 CC 专线 / 家宽节点 / 住宅 IP 前，必须先通过 `sheepdog tell --to tp_3x-ui-ops` 核实当前各线路使用人数与空余席位余量，"
            "并在正文中写明各线路当前人数与空余席位实据后再发起 ask（REG-076 防打扰门禁，没有发送）",
        )
    if _ENV_REQUIRED_TITLE_RE.search(title) and not _EXPLICIT_ENV_DECL_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示 LiveKit 凭据或子成员账号时，必须在标题或正文中显式声明『明确是测试环境（tap-testing-env）』或『明确是生产环境（taplabs-453508）』"
            "（或『目标环境：Stage/测试/生产』），严禁让主人反问『他是测试还是生产呀 / 哪个环境的子成员账号』（REG-076 防打扰门禁，没有发送）",
        )
    if _PR_MERGE_EXEC_ASK_RE.search(full_text) and not _PR_HUMAN_REVIEW_STATUS_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示执行 `gh pr merge` 或直接合入 PR 打 Tag 发布生产前，必须先核查并在正文中显式写明『人工 Code Review 状态』"
            "（如 GitHub `reviewDecision: APPROVED`、已按 `tap-pr-review-submit` 过人工评审，或明确声明『人工 Review：0/N 未过』），"
            "严禁只写 CI/Greptile 机器人审查就请主人拍板合入发生产，让主人反问『review 过了吗』（REG-081 防打扰门禁，没有发送）",
        )
    if _CORS_OR_CALLER_DOMAIN_ASK_RE.search(full_text) and not (
        _CORS_TRAFFIC_SCAN_EVIDENCE_RE.search(full_text) and _PEER_OWNER_CONSULT_EVIDENCE_RE.search(full_text)
    ):
        raise OutboundError(
            EXIT_INVALID,
            "请示确认 CORS / Origin 域名白名单或『是否有其他前端域名调用』前，必须先：① 扫描近 48h 真实云上流量日志（gcloud logging Referer/Origin）与本地记录信息；"
            "② 通过 `sheepdog tell` 与技术负责人（如 `tp_laohu`）核对确认，并在正文中写明真实云上流量扫描与负责人核对实据后再发起 ask，"
            "严禁未经自查与负责人对齐直接反问主人『是否有其他前端域名会调用』（REG-082 防打扰门禁，没有发送）",
        )
    if _TEST_APPROVAL_QA_BYPASS_ASK_RE.search(full_text) and not _PEER_OWNER_CONSULT_EVIDENCE_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "请示发版单『测试审批是否可免测直接放行还是先走 QA』前，必须先通过 `sheepdog tell` 与该单技术负责人（如 `tp_laohu`）核对确认（或引导提问方找 `laohu` 确认），"
            "并在正文中写明核对结论后再发起 ask，严禁未经内部对齐直接把测试审批放行问题抛给主人（REG-082 防打扰门禁，没有发送）",
        )
    if _TEST_JUMPBOX_FIREWALL_ASK_RE.search(full_text) and not _DESTRUCTIVE_OR_PROD_FIREWALL_RE.search(full_text):
        raise OutboundError(
            EXIT_INVALID,
            "测试环境（tap-testing-env）跳板机 / office-allow-all 白名单追加属于预授权非破坏性测试环境操作（『测试环境可以直接动』）："
            "请直接优先将申请人出口 IP 加入 `office-allow-all`，同时通过 `sheepdog tell --to tp_3x-ui-ops` 核实全部 VPS 出口 IP 一并补齐进该白名单，"
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
    r"(?:暂将|先把|先将|临时将|临时把|先用|暂用)[^\n。；]{0,60}(?:临时指向|指向|切到|设为|用)?[^\n。；]{0,30}(?:A\s*记录|Ingress|CLB|直连|直通)"
    r"|临时\s*(?:A\s*记录|指向|直连|直通|绕过|过渡|顶上|兜底)"
    r"|(?:待|等)\s*(?:EO|EdgeOne|边缘节点|证书|SSL|TLS|CNAME|加速域名)[^\n。；]{0,80}(?:从\s*[`'\"]?process\w*[`'\"]?\s*变|部署完成|签发完成|生效|online|deployed)[^\n。；]{0,40}(?:后|再)?[^\n。；]{0,20}(?:可|一键|平滑|正式)?\s*切(?:换|回|到)?\s*CNAME"
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
            "但飞书卡片首屏（--title / --summary）只报喜未披露该临时过渡态，也未通过 --defer-item 挂入 Loop 自动盯盘（主人批评：『这个东西以后要报啊 不能啥也不说』）！\n"
            "请：① 在 --summary 首屏如实写明当前临时过渡态与待切换条件；"
            "② 强烈建议同时传 `--defer-item \"<key>:<待收尾动作说明>\" [--defer-check-cmd \"<零Token探活命令>\"]`，由系统自动挂入 Loop 盯盘并在首屏展示在途收尾项（没有发送）。",
        )
    if (ball or "").strip().lower() == "done" and not has_defer:
        raise OutboundError(
            EXIT_INVALID,
            f"report 拒绝（REG-091 临时过渡态防假结案门禁）：检测到当前存在未收尾的临时过渡方案（『{matched_snippet}』），"
            "在最终切换（如切回 CNAME）完成前严禁传 `--ball done` 假结案！请传 `--defer-item \"<key>:<待收尾说明>\"` 挂入 Loop 自动盯盘收尾，或改用 `--ball doing`（没有发送）。",
        )


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
        check_ask_pre_flight_guard(cfg, store, topic_id, title, combined_for_check, key=key, floor=raw_floor, floor_type=floor_type, options=options)
    else:
        ball = decision_hub.enforce_report_contract(cfg, store, topic_id, title, combined_for_check, key=key, ball=ball, fyi=fyi)
        check_transitional_state_report_guard(
            cfg, store, topic_id, title, summary, body,
            defer_item=defer_item, ball=ball, fyi=fyi, transcript_text=transcript_text,
        )
    try: _, _, resolved_body = resolve_summary_and_body(body, summary, require_summary=cfg.notify.require_summary, hoist_at=False)
    except ValueError as e: raise OutboundError(EXIT_INVALID, str(e))
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
    check_unverified_ticket_promise_guard(shown)
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
