"""卡片（Card 2.0）。

- 代回卡片（7.17）：会话以主人身份回复时发一张卡片——正文 + 右下角灰色小字标记。`sheepdog reply-card` 打印卡片 JSON，
  会话再用 lark-cli 以 interactive 消息发出。正文原样放进 markdown 元素，支持卡片 markdown 与 `<at id=ou_xxx></at>`。
- 给主人的卡片（7.48）：牧羊犬发给主人的四条路（ask / report、系统通知、「待办」清单、结案那一句）都发成卡片，
  模型写的 markdown 原样渲染。第一行（「🐕 需要你定 · 标题 [tp_xxx]」这类）放进卡片 header 的标题，其余放一个 markdown 元素；
  要提醒主人时另起一个 markdown 元素放 `<at id=ou_xxx></at>`。

读回（10-16 只读核过正式账本：主人身份搜索拉到的卡片，8102 / 8109 条是这个样子）：
    <card title="标题" subtitle="副标题">     ← 有 header 时；没有 header 就是 <card>
    正文（markdown 基本原样；`<at id=ou_x></at>` 变成 `@名字(ou_x)`，<font> 标签去掉）
    </card>
不同 markdown 元素之间以换行分开；同一个元素里，标题、列表、引用、代码块前后的空行常被吃掉、和上一行连在一起。
所以第一行放 header：读回时它单独在第一行的 title 里，不会和正文连上。card_text() 把读回的卡片还原成「标题\\n正文」，
牧羊犬认「需要你定」、认会话都按还原后的文字认；不是卡片的原样返回，旧的纯文本认法一字不变。
"""

from __future__ import annotations

import json
import re


# ======================= 当面日常大白话首屏（7.70）+ 白话总结与 more 默认折叠区（7.62） =======================
MORE_PANEL_TITLE = "more"
MORE_DELIMITER = "---more---"
LEGACY_MORE_DELIMITER = "---agent-read---"
SUMMARY_MAX_LINES = 6
SUMMARY_MAX_CHARS = 220
SHORT_MESSAGE_MAX_LINES = 4
SHORT_MESSAGE_MAX_CHARS = 160
REPLY_SUMMARY_MAX_LINES = 3
REPLY_SUMMARY_MAX_CHARS = 100
REPLY_SHORT_MESSAGE_MAX_LINES = 3
REPLY_SHORT_MESSAGE_MAX_CHARS = 100
DELIVERABLE_MAX_LINES = 12
DELIVERABLE_MAX_CHARS = 520
_MORE_SPLIT_RE = re.compile(r"(?:^[ \t]*|[ \t]+)(?<!`)---(?:more|agent-read)---(?!`)(?:[ \t]*$|[ \t]+)", re.M)
_CODE_FENCE_RE = re.compile(r"```")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_DELIVERABLE_URL_RE = re.compile(r"(?:https?|vless|trojan|vmess|ss)://[A-Za-z0-9_./?=&%#:~+@!-]+", re.I)
_SINGLE_LINE_URI_FENCE_RE = re.compile(
    r"```[A-Za-z0-9_-]*\s*\n\s*((?:vless|trojan|vmess|ss)://[^\s`]+)\s*\n```",
    re.I,
)
_DELIVERABLE_STRONG_RE = re.compile(
    r"(?:临时登录密码|临时密码|初始密码|登录密码|(?:密码|Password|Token)\*{0,2}\s*[:：]|密码\s*[（(]Password[)）]"
    r"|SecretId|SecretKey|AccessKeyId|AccessKeySecret|API\s*Key|Access\s*Token"
    r"|订阅链接|订阅地址|节点链接|直连链接|子用户登录入口|登录入口|登录地址|切换\s*Prod\s*账号直连链接|一键直达"
    r"|(?:vless|trojan|vmess|ss)://\S+)",
    re.I,
)
_DELIVERABLE_KV_LINE_RE = re.compile(
    r"^\s*(?:[-*•]|\d+\.)?\s*\*{0,2}(?:"
    r"子用户名|用户名|账号|登录账号|登录邮箱|邮箱|Email|主账号(?:\s*ID)?|临时登录密码|临时密码|初始密码|登录密码|密码"
    r"|SecretId|SecretKey|AccessKeyId|AccessKeySecret|API\s*Key|Access\s*Token|Token"
    r"|(?:公网\s*|内网\s*)?Host|数据库地址|内网地址|端口|Port|Database|数据库(?:名)?|库名"
    r"|子用户登录入口|子账号登录入口|登录入口|登录地址|一键直达|切换[^：:\n]{0,16}(?:链接|直达)|订阅链接|订阅地址|节点链接|直连链接"
    r")(?:\s*[（(][^）)\n]*[)）])?\*{0,2}\s*[:：]\s*\S+",
    re.I,
)
_DELIVERABLE_STANDALONE_URI_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?`?(?:vless|trojan|vmess|ss)://[^\s`]+`?\s*$",
    re.I,
)
_LOCAL_PATH_RULE_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.-])(?:/|~/|\./)[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+"
    r"|(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+\.(?:py|go|ts|tsx|js|mjs|json|yaml|yml|toml|md|sh|sql)(?![A-Za-z0-9_.-]))"
)
_AT_TAG_RE = re.compile(
    r'<at\s+(?:id|user_id)=["\']?(?P<target>ou_[A-Za-z0-9_]+|all)["\']?\s*>.*?</at>',
    re.I | re.S,
)
_LEADING_AT_PREFIX_RE = re.compile(
    r"^\s*(?:<at\s+(?:id|user_id)=[\"']?(?:ou_[A-Za-z0-9_]+|all)[\"']?\s*>.*?</at>[ \t]*)+",
    re.I | re.S,
)
_MACHINE_SYMBOL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Cloud Run revision 编号",
        re.compile(r"(?:-[0-9]{5}-[a-z0-9]{3}(?![A-Za-z0-9])|(?<![A-Za-z0-9-])[0-9]{5}-[a-z0-9]{3}(?![A-Za-z0-9]))"),
    ),
    (
        "Git commit SHA",
        re.compile(r"(?<![A-Za-z0-9_-])(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,40}(?![A-Za-z0-9_-])"),
    ),
    (
        "URL 或文件路径",
        re.compile(
            r"(?:https?://[A-Za-z0-9_./?=&%#:~+-]+|(?<![A-Za-z0-9_.-])(?:/|~/|\./)[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+"
            r"|(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+\.(?:py|go|ts|tsx|js|mjs|json|yaml|yml|toml|md|sh|sql)(?![A-Za-z0-9_.-]))"
        ),
    ),
    ("飞书内部 ID", re.compile(r"(?<![A-Za-z0-9_])(?:om|oc|ou|omt)_[A-Za-z0-9_]{6,}(?![A-Za-z0-9_])")),
    ("反引号代码", re.compile(r"`[^`\n]+`")),
    (
        "Markdown 编号分点列表",
        re.compile(r"(?:(?:\A|\n)\s*|(?<![0-9A-Za-z.]))1\.\s+\S.*?(?:\n\s*|[；;，,\s]+)2\.\s+\S", re.S),
    ),
)
SUMMARY_REQUIRED_GUIDE = (
    '原文不需要改，只需要增加白话总结（当面日常大白话回话）：加 --summary "<1~2句当面日常大白话：reply ≤3行/≤100字，只答问题、不括技术亮点与机器编号>"'
    '（或在原文最前面写大白话并用一行 ---more--- 隔开），修订号/路径/分点清单留在原文里会自动收进 more 折叠区。\n'
    '反例 ❌：模型服务体系下的model-manager-decision-service已上线Ready(00005-5vc)，含三级容灾+无锁熔断，今晚可上生产。\n'
    '正例 ✅：我们是根据模型服务V2的代码，单独创建一个独立服务，不混在旧模型服务里。'
)
_MACHINE_TOKEN_PATTERNS = _MACHINE_SYMBOL_RULES
_PLAIN_SPEAK_GUIDANCE = SUMMARY_REQUIRED_GUIDE


def _strip_code_spans(text: str) -> str:
    """去掉围栏代码块和行内反引号代码，避免把文档/命令示例里的 `<at id=ou_xxx></at>` 当成真 @人。"""
    return _INLINE_CODE_RE.sub("", _FENCED_BLOCK_RE.sub("", text or ""))


def _at_targets(text: str) -> list[str]:
    """按出现先后提取非代码区里的 @ 目标（ou_xxx 或 all），去重保序。"""
    cleaned = _strip_code_spans(text)
    return list(dict.fromkeys(m.group("target") for m in _AT_TAG_RE.finditer(cleaned)))


def _normalize_card_at_tags(text: str) -> str:
    """把非代码区里的 `<at user_id="ou_xxx">名字</at>` 统一转成飞书 Card 2.0 认识的 `<at id=ou_xxx></at>`。"""
    s = text or ""
    code_spans: list[tuple[int, int]] = [m.span() for m in _FENCED_BLOCK_RE.finditer(s)]
    code_spans += [m.span() for m in _INLINE_CODE_RE.finditer(s)]

    def _in_code(pos: int) -> bool:
        return any(a <= pos < b for a, b in code_spans)

    def _repl(m: re.Match) -> str:
        if _in_code(m.start()):
            return m.group(0)
        return f"<at id={m.group('target')}></at>"

    return _AT_TAG_RE.sub(_repl, s)


def _hoist_at_tags(summary: str, detail: str) -> tuple[str, str]:
    """7.62：把原文（more 折叠区）里的 @人自动提到外层白话总结最前面，并剥掉原文开头紧挨着的重复 @ 标签。"""
    norm_sum = _normalize_card_at_tags(summary)
    norm_det = _normalize_card_at_tags(detail)
    existing = set(_at_targets(norm_sum))
    missing = [t for t in _at_targets(norm_det) if t not in existing]
    if missing:
        prefix = " ".join(f"<at id={t}></at>" for t in missing)
        norm_sum = f"{prefix} {norm_sum.lstrip()}"
    stripped_det = _LEADING_AT_PREFIX_RE.sub("", norm_det, count=1)
    if stripped_det.strip():
        norm_det = stripped_det
    return norm_sum, norm_det


def build_more_panel(detail: str) -> dict:
    """7.62：把原有完整回复原封不动装进飞书 Card 2.0 原生默认折叠面板（无背景框、左侧 more + 原生旋转下拉箭头）。"""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "padding": "0px",
        "header": {
            "title": {"tag": "markdown", "content": f"<font color='grey'>{MORE_PANEL_TITLE}</font>"},
            "padding": "0px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "14px 14px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "elements": [{"tag": "markdown", "content": detail.strip("\n")}],
    }


def split_more(text: str) -> tuple[str, str]:
    """按第一处 ---more---（兼容 ---agent-read---，支持独占一行或行内带空格分隔）拆成 (白话总结, 原文)；没有分隔线时返回 ("", 原文)。"""
    s = text or ""
    m = _MORE_SPLIT_RE.search(s)
    if m is None:
        return "", s.strip("\n")
    return s[:m.start()].strip(" \t\n"), s[m.end():].strip(" \t\n")


def _find_machine_symbol(text: str) -> tuple[str, str] | None:
    """7.70：先剥离合法 <at> 标签，再匹配 6 类绝对不属于人类日常口语的机器符号。"""
    cleaned = _AT_TAG_RE.sub("", text or "")
    for label, rx in _MACHINE_SYMBOL_RULES:
        if m := rx.search(cleaned):
            return label, m.group(0).strip()
    return None


def is_deliverable_payload(text: str) -> bool:
    """7.119：识别含账号、密码、临时密码、SecretId/SecretKey、API Key、Token、登录入口或节点订阅链接的核心交付物文本。"""
    unwrapped = _SINGLE_LINE_URI_FENCE_RE.sub(r"`\1`", text or "")
    s = _FENCED_BLOCK_RE.sub("", unwrapped)
    if not s.strip() or not _DELIVERABLE_STRONG_RE.search(s):
        return False
    if _INLINE_CODE_RE.search(s) or _DELIVERABLE_URL_RE.search(s):
        return True
    return any(_DELIVERABLE_KV_LINE_RE.match(ln) for ln in s.splitlines())


_BARE_CREDENTIAL_VALUE_RE = re.compile(
    r"((?:临时登录密码|临时密码|初始密码|登录密码|密码|Password|SecretId|SecretKey|AccessKeyId|AccessKeySecret|API\s*Key|Access\s*Token|Token)"
    r"(?:\s*[（(][^）)\n]*[)）])?\*{0,2}\s*[:：]\s*)`?[A-Za-z0-9!@#$%^&*_+=.~-]+`?",
    re.I,
)


def _find_deliverable_forbidden_symbol(text: str) -> tuple[str, str] | None:
    """7.119 / 1.1.1：核心交付物首屏豁免凭证值（含未加反引号的裸密码如 KaiStage2026!643d1946）、行内反引号与登录/订阅 URL，但仍拦截 Cloud Run revision、裸 Git SHA、代码文件路径与飞书内部 ID。"""
    cleaned = _AT_TAG_RE.sub("", text or "")
    cleaned = _BARE_CREDENTIAL_VALUE_RE.sub(r"\1 ", cleaned)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _DELIVERABLE_URL_RE.sub(" ", cleaned)
    for label, rx in (
        _MACHINE_SYMBOL_RULES[0],
        _MACHINE_SYMBOL_RULES[1],
        ("URL 或文件路径", _LOCAL_PATH_RULE_RE),
        _MACHINE_SYMBOL_RULES[3],
    ):
        if m := rx.search(cleaned):
            return label, m.group(0).strip()
    return None


def _strip_more_pointer_clauses(summary: str) -> str:
    """7.119：当把凭据/链接自动提回首屏后，剥除总结里『在下方展开查看 / 点开下方 more 即可复制』等冗余引导词。"""
    s = summary or ""
    s = re.sub(
        r"\s*[（(][^（）()\n]*(?:下方|下面|more|展开|点开)[^（）()\n]*(?:查看|复制|展开|点开|使用)[^（）()\n]*[）)]",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"[，,；;]\s*[^，,；;。\n]*(?:(?:下方|下面|more)[^，,；;。\n]*(?:查看|复制|展开|点开|使用)|(?:展开|点开)(?:下方|下面|more|卡片)[^，,；;。\n]*)",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"(?:已整理|整理)?在(?:下方|下面)(?:卡片)?(?:中|里)?", "如下", s)
    s = re.sub(r"[，,；;：:]+\s*([。！!])", r"\1", s)
    return s.strip()


def _norm_dedup_text(text: str) -> str:
    cleaned = _AT_TAG_RE.sub("", text or "")
    return re.sub(r"[\s，,。！!？?；;：:、~～]+", "", cleaned).lower()


def _is_redundant_more(summary: str, detail: str, *, for_reply: bool = True) -> bool:
    """7.119：识别 summary 与 detail 实质重复的假折叠（完全一致，或 detail 本身也是无凭据短句且高度重合）。"""
    s_norm = _norm_dedup_text(summary)
    d_norm = _norm_dedup_text(detail)
    if not s_norm or not d_norm:
        return False
    if s_norm == d_norm:
        return True
    if for_reply and not is_deliverable_payload(detail):
        d_stripped = (detail or "").strip()
        vis_len = len(_AT_TAG_RE.sub("", d_stripped).strip())
        if (
            not _CODE_FENCE_RE.search(d_stripped)
            and len(d_stripped.splitlines()) <= REPLY_SHORT_MESSAGE_MAX_LINES
            and vis_len <= REPLY_SHORT_MESSAGE_MAX_CHARS
            and _find_machine_symbol(d_stripped) is None
        ):
            s_set, d_set = set(s_norm), set(d_norm)
            overlap = len(s_set & d_set) / max(1, min(len(s_set), len(d_set)))
            if overlap >= 0.65:
                return True
    return False


def _hoist_deliverables_and_dedup(
    summary: str,
    detail: str,
    *,
    for_reply: bool = True,
) -> tuple[str, str]:
    """7.119：(1) 消除 summary == detail 假折叠；(2) 把被藏在 more 里的账号/密码/密钥/Token/登录或订阅链接自动提回首屏。"""
    if _is_redundant_more(summary, detail, for_reply=for_reply):
        return "", summary
    if not for_reply or is_deliverable_payload(summary) or not is_deliverable_payload(detail):
        return summary, detail
    normalized_detail = _SINGLE_LINE_URI_FENCE_RE.sub(r"`\1`", detail)
    fenced_spans = [m.span() for m in _FENCED_BLOCK_RE.finditer(normalized_detail)]

    def _in_fence(offset: int) -> bool:
        return any(a <= offset < b for a, b in fenced_spans)

    hoisted: list[str] = []
    remaining_lines: list[str] = []
    pos = 0
    for ln in normalized_detail.splitlines(keepends=True):
        raw_ln = ln.rstrip("\r\n")
        if not _in_fence(pos) and (
            _DELIVERABLE_KV_LINE_RE.match(raw_ln) or _DELIVERABLE_STANDALONE_URI_LINE_RE.match(raw_ln)
        ):
            hoisted.append(raw_ln.strip())
        else:
            remaining_lines.append(raw_ln)
        pos += len(ln)
    if not hoisted:
        return summary, detail
    cleaned_sum = _strip_more_pointer_clauses(summary)
    candidate_sum = f"{cleaned_sum}\n" + "\n".join(hoisted)
    vis_len = len(_AT_TAG_RE.sub("", candidate_sum).strip())
    if (
        len(candidate_sum.splitlines()) > DELIVERABLE_MAX_LINES
        or vis_len > DELIVERABLE_MAX_CHARS
        or _find_deliverable_forbidden_symbol(candidate_sum) is not None
    ):
        return summary, detail
    # 清理残留在 detail 里失去了下属列表项的引导空标题（如『新的登录信息如下：』『### 1. 单节点直连链接』）
    pruned_rem: list[str] = []
    for ln in remaining_lines:
        stripped = ln.strip()
        if not stripped:
            if pruned_rem and pruned_rem[-1] != "":
                pruned_rem.append("")
            continue
        if re.match(
            r"^(?:#{1,4}\s*|\d+\.\s*)?\*{0,2}[^。\n]{0,60}(?:信息如下|连接信息如下|凭证如下|节点链接|登录入口)(?:\s*[（(][^）)\n]*[)）])?\*{0,2}[：:]?$",
            stripped,
        ):
            continue
        pruned_rem.append(ln)
    rem_text = "\n".join(pruned_rem).strip()
    if not rem_text:
        return "", candidate_sum
    if not _CODE_FENCE_RE.search(rem_text) and len(rem_text.splitlines()) <= 2 and len(rem_text) <= 100:
        return "", candidate_sum
    return candidate_sum, rem_text


def is_short_message(text: str, *, for_reply: bool = False) -> bool:
    """短消息：不传 --summary 时直接平铺，无需套 more 折叠框。7.70：reply 收紧为 ≤3 行、≤100 字且禁含机器符号；7.119：核心交付物放宽至 ≤12 行、≤520 字。"""
    s = (text or "").strip()
    if not s:
        return True
    if _CODE_FENCE_RE.search(s):
        return False
    if for_reply:
        vis_len = len(_AT_TAG_RE.sub("", s).strip())
        if is_deliverable_payload(s):
            return (
                len(s.splitlines()) <= DELIVERABLE_MAX_LINES
                and vis_len <= DELIVERABLE_MAX_CHARS
                and _find_deliverable_forbidden_symbol(s) is None
            )
        return (
            len(s.splitlines()) <= REPLY_SHORT_MESSAGE_MAX_LINES
            and vis_len <= REPLY_SHORT_MESSAGE_MAX_CHARS
            and _find_machine_symbol(s) is None
        )
    return len(s.splitlines()) <= SHORT_MESSAGE_MAX_LINES and len(s) <= SHORT_MESSAGE_MAX_CHARS


_DEFERRED_LOOP_BADGE_LINE_RE = re.compile(r"^\s*⏳\s*在途收尾项（已挂入\s*Loop\b[^\n]*$", re.M)


def validate_summary(summary: str, *, for_reply: bool = True) -> str:
    """校验卡片首屏（7.70 reply 为 ≤3 行、≤100 字、禁含 6 类机器符号；7.119 交付物首屏 ≤12 行、≤520 字；ask/report 为 ≤6 行、≤220 字）。"""
    s = (summary or "").strip("\n")
    if not s.strip():
        raise ValueError(f"白话总结为空。{SUMMARY_REQUIRED_GUIDE}")
    if _CODE_FENCE_RE.search(s):
        raise ValueError(f"白话总结不能含代码块（代码块请留在原文里，会自动收进 more 折叠区）。{SUMMARY_REQUIRED_GUIDE}")
    s_for_check = _DEFERRED_LOOP_BADGE_LINE_RE.sub("", s).strip("\n") or s
    is_deliv = for_reply and is_deliverable_payload(s_for_check)
    max_lines = DELIVERABLE_MAX_LINES if is_deliv else (REPLY_SUMMARY_MAX_LINES if for_reply else SUMMARY_MAX_LINES)
    max_chars = DELIVERABLE_MAX_CHARS if is_deliv else (REPLY_SUMMARY_MAX_CHARS if for_reply else SUMMARY_MAX_CHARS)
    lines = s_for_check.splitlines()
    if len(lines) > max_lines:
        raise ValueError(f"白话总结超过 {max_lines} 行（当前 {len(lines)} 行）。{SUMMARY_REQUIRED_GUIDE}")
    visible_len = len(_AT_TAG_RE.sub("", s_for_check).strip())
    if visible_len > max_chars:
        raise ValueError(f"白话总结超过 {max_chars} 字（当前 {visible_len} 字）。{SUMMARY_REQUIRED_GUIDE}")
    if for_reply:
        hit = _find_deliverable_forbidden_symbol(s_for_check) if is_deliv else _find_machine_symbol(s_for_check)
        if hit is not None:
            raise ValueError(f"白话总结不能含{hit[0]}（{hit[1]!r} 请留在原文里，会自动收进 more 折叠区）。{SUMMARY_REQUIRED_GUIDE}")
    return s


def resolve_summary_and_body(
    body: str,
    summary: str = "",
    *,
    require_summary: bool = True,
    hoist_at: bool = True,
    for_reply: bool | None = None,
) -> tuple[str, str, str]:
    """7.62 / 7.70 / 7.119：解析并校验 (白话总结, 原文, 记账/建卡用的合并文本)。"""
    raw = (body or "").strip("\n")
    if not raw.strip():
        raise ValueError("正文为空")
    is_reply = hoist_at if for_reply is None else for_reply
    inline_sum, inline_det = split_more(raw)
    has_delim = _MORE_SPLIT_RE.search(raw) is not None
    if (summary or "").strip():
        s = validate_summary(summary, for_reply=is_reply)
        if has_delim and inline_det.strip():
            if _norm_dedup_text(inline_sum) == _norm_dedup_text(s):
                d = inline_det
            else:
                d = f"{inline_sum.strip()}\n\n{inline_det.strip()}" if inline_sum.strip() else inline_det
        else:
            d = raw
        if not d.strip():
            raise ValueError(f"more 折叠区原文为空。{SUMMARY_REQUIRED_GUIDE}")
        if hoist_at:
            s, d = _hoist_at_tags(s, d)
        s, d = _hoist_deliverables_and_dedup(s, d, for_reply=is_reply)
        if not s:
            return "", d, d
        return s, d, f"{s}\n{MORE_DELIMITER}\n{d}"
    if has_delim:
        s = validate_summary(inline_sum, for_reply=is_reply)
        if not inline_det.strip():
            raise ValueError(f"more 折叠区原文为空（---more--- 下面要有原有完整回复）。{SUMMARY_REQUIRED_GUIDE}")
        d = inline_det
        if hoist_at:
            s, d = _hoist_at_tags(s, d)
        s, d = _hoist_deliverables_and_dedup(s, d, for_reply=is_reply)
        if not s:
            return "", d, d
        return s, d, f"{s}\n{MORE_DELIMITER}\n{d}"
    if is_short_message(raw, for_reply=is_reply) or not require_summary:
        return "", body, body
    n_lines, n_chars = len(raw.splitlines()), len(raw)
    hit = _find_machine_symbol(raw) if is_reply else None
    why = "含代码块" if _CODE_FENCE_RE.search(raw) else (f"含{hit[0]} {hit[1]!r}" if hit else f"{n_lines} 行 / {n_chars} 字")
    raise ValueError(f"正文较长（{why}）但未提供白话总结。{SUMMARY_REQUIRED_GUIDE}")


def _card_body_elements(body_text: str, summary: str = "", *, hoist_at: bool = True) -> list[dict]:
    """把正文（或 summary + 正文）拆成卡片 body elements：有总结/分隔线时为 [summary_md, more_panel]，否则为单个 [body_md]。"""
    s, d, _ = resolve_summary_and_body(body_text, summary, require_summary=False, hoist_at=hoist_at)
    if s:
        return [{"tag": "markdown", "content": s}, build_more_panel(d)]
    return [{"tag": "markdown", "content": d}]


def build_reply_card(text: str, suffix: str, width_mode: str = "compact", summary: str = "") -> dict:
    if not (text or "").strip():
        raise ValueError("正文为空")
    elements = _card_body_elements(text, summary, hoist_at=True)
    if suffix:
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{suffix}</font>",
                         "text_align": "right", "text_size": "notation"})
    return {"schema": "2.0", "config": {"width_mode": width_mode}, "body": {"elements": elements}}


# ======================= 给主人的卡片（7.48） =======================
# header 的主题色：要他定的橙色，其余蓝色（飞书卡片 header.template 枚举）
TEMPLATE_DECIDE = "orange"
TEMPLATE_DEFAULT = "blue"
DECIDE_WORD = "需要你定"

# 卡片的大小上限。飞书开放平台「发送消息」接口：卡片消息的请求体不能超过 30 KB（文本消息 150 KB）。请求体里卡片 JSON 是作为字符串
# 再转义一次放进 content 的，发送方还可能把 < > & 转成 < 这类 6 个字节。这里按「转义两次后的 UTF-8 字节数 + 每个 <>& 多算
# 5 个字节」估算，超过 OWNER_CARD_MAX_BYTES 就不发卡片、退回纯文本（同一条消息、同一个防重编号；纯文本上限 150 KB）。
# 留了约 2.7 KB 给请求体里的其他字段和估算误差。实际上限要在真飞书上核实（7.48 第 4 条）。
OWNER_CARD_MAX_BYTES = 28_000


def owner_title(title: str) -> str:
    """header 标题：一行；半角双引号换成全角（读回时标题在 title="…" 里，半角双引号会和属性的引号分不清）。"""
    return " ".join((title or "").split()).replace('"', "＂")


def build_owner_card(text: str, at_open_id: str = "", summary: str = "") -> dict:
    """给主人的卡片：第一行进 header 标题，其余原样进一个 markdown 元素（7.62：有白话总结 / ---more--- 时首屏只露白话总结，
    原文进 more 默认折叠区）；at_open_id 有值时末尾另起一个 markdown 元素 @ 他。只有一行（结案那一句这类）时不放 header，
    这一行就是正文。正文为空抛 ValueError。"""
    text = (text or "").strip("\n")
    if not text.strip():
        raise ValueError("正文为空")
    first, _, rest = text.partition("\n")
    card: dict = {"schema": "2.0"}
    if rest.strip():
        card["header"] = {"title": {"tag": "plain_text", "content": owner_title(first)},
                          "template": TEMPLATE_DECIDE if DECIDE_WORD in first else TEMPLATE_DEFAULT}
        elements = _card_body_elements(rest.strip("\n"), summary, hoist_at=False)
    else:
        elements = _card_body_elements(first, summary, hoist_at=False)
    if at_open_id:
        elements.append({"tag": "markdown", "content": f"<at id={at_open_id}></at>"})
    card["body"] = {"elements": elements}
    return card


def card_request_bytes(card: dict) -> int:
    """估算这张卡片发出去时在请求体里占多少字节（见 OWNER_CARD_MAX_BYTES）。"""
    once = json.dumps(card, ensure_ascii=False)
    twice = json.dumps(once, ensure_ascii=False)
    return len(twice.encode("utf-8")) + 5 * sum(once.count(c) for c in "<>&")


def card_fits(card: dict, limit: int = OWNER_CARD_MAX_BYTES) -> bool:
    return card_request_bytes(card) <= limit


# 读回：卡片第一行。先试带副标题的（标题不贪心），再试只有标题的（标题贪心，标题里有半角引号也认得出）
_CARD_TITLE_SUB = re.compile(r'^<card title="(?P<title>.*?)" subtitle="(?P<sub>[^\n]*)">$')
_CARD_TITLE = re.compile(r'^<card title="(?P<title>.*)">$')
_CARD_BARE = "<card>"
_CARD_END = "</card>"


def card_text(content: str) -> str | None:
    """读回的卡片还原成「标题\\n正文」（没有标题就只有正文）；不是这种样子返回 None。只去掉外层，正文一字不改。"""
    s = content or ""
    if not s.startswith("<card"):
        return None
    first, _, rest = s.partition("\n")
    first = first.rstrip()
    if first == _CARD_BARE:
        title = ""
    elif (m := _CARD_TITLE_SUB.match(first)) or (m := _CARD_TITLE.match(first)):
        title = m.group("title")
    else:
        return None
    body = rest.rstrip()
    if body.endswith(_CARD_END):
        body = body[: -len(_CARD_END)].rstrip("\n")
    return f"{title}\n{body}" if title and body else (title or body)


def plain_text(content: str) -> str:
    """认「需要你定」、认会话、记账用的文字：卡片还原成「标题\\n正文」，别的原样返回（旧的纯文本一字不变）。"""
    t = card_text(content)
    return content if t is None else t
