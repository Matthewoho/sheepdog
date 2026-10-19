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


# ======================= 四刀法白话总结 + more 默认折叠区（7.62） =======================
MORE_PANEL_TITLE = "more"
MORE_DELIMITER = "---more---"
LEGACY_MORE_DELIMITER = "---agent-read---"
SUMMARY_MAX_LINES = 6
SUMMARY_MAX_CHARS = 220
SHORT_MESSAGE_MAX_LINES = 4
SHORT_MESSAGE_MAX_CHARS = 160
_MORE_SPLIT_RE = re.compile(r"^[ \t]*---(?:more|agent-read)---[ \t]*$", re.M)
_CODE_FENCE_RE = re.compile(r"```")
SUMMARY_REQUIRED_GUIDE = (
    '原文不需要改，只需要增加白话总结：加 --summary "<四刀法白话总结：≤6行、≤220字、不含代码块>"'
    '（或在原文最前面写白话总结并用一行 ---more--- 隔开），原有回复会原封不动收进 more 折叠区。'
    '四刀法：①终态与边界（做成什么、哪没动）②异动/意外 ③决策或下一步开关 ④证据锚点'
)


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
    """按第一行 ---more---（兼容 ---agent-read---）拆成 (白话总结, 原文)；没有分隔线时返回 ("", 原文)。"""
    s = text or ""
    m = _MORE_SPLIT_RE.search(s)
    if m is None:
        return "", s.strip("\n")
    return s[:m.start()].strip("\n"), s[m.end():].strip("\n")


def is_short_message(text: str) -> bool:
    """短消息（≤4 行、≤160 字、不含 ``` 代码块）：不传 --summary 时直接平铺，无需套 more 折叠框。"""
    s = (text or "").strip()
    if not s:
        return True
    if _CODE_FENCE_RE.search(s):
        return False
    return len(s.splitlines()) <= SHORT_MESSAGE_MAX_LINES and len(s) <= SHORT_MESSAGE_MAX_CHARS


def validate_summary(summary: str) -> str:
    """校验四刀法白话总结：非空、≤6 行、≤220 字、不含 ``` 代码块。不合格抛 ValueError（带明确指引）。"""
    s = (summary or "").strip("\n")
    if not s.strip():
        raise ValueError(f"白话总结为空。{SUMMARY_REQUIRED_GUIDE}")
    if _CODE_FENCE_RE.search(s):
        raise ValueError(f"白话总结不能含代码块（代码块请留在原文里，会自动收进 more 折叠区）。{SUMMARY_REQUIRED_GUIDE}")
    lines = s.splitlines()
    if len(lines) > SUMMARY_MAX_LINES:
        raise ValueError(f"白话总结超过 {SUMMARY_MAX_LINES} 行（当前 {len(lines)} 行）。{SUMMARY_REQUIRED_GUIDE}")
    if len(s) > SUMMARY_MAX_CHARS:
        raise ValueError(f"白话总结超过 {SUMMARY_MAX_CHARS} 字（当前 {len(s)} 字）。{SUMMARY_REQUIRED_GUIDE}")
    return s


def resolve_summary_and_body(body: str, summary: str = "", *, require_summary: bool = True) -> tuple[str, str, str]:
    """7.62：解析并校验 (白话总结, 原文, 记账/建卡用的合并文本)。
    - 传了 --summary 或正文含 ---more--- 分隔线：校验白话总结，返回 (summary, detail, "summary\\n---more---\\ndetail")；
    - 未传且是短消息（≤4 行、≤160 字、无代码块）：直接平铺，返回 ("", detail, detail)；
    - 未传且是长消息：require_summary=True 时抛 ValueError 拒绝并提示「原文不需要改，只需要增加白话总结」。"""
    raw = (body or "").strip("\n")
    if not raw.strip():
        raise ValueError("正文为空")
    inline_sum, inline_det = split_more(raw)
    has_delim = _MORE_SPLIT_RE.search(raw) is not None
    if (summary or "").strip():
        s = validate_summary(summary)
        d = inline_det if has_delim and inline_det.strip() else raw
        if not d.strip():
            raise ValueError(f"more 折叠区原文为空。{SUMMARY_REQUIRED_GUIDE}")
        return s, d, f"{s}\n{MORE_DELIMITER}\n{d}"
    if has_delim:
        s = validate_summary(inline_sum)
        if not inline_det.strip():
            raise ValueError(f"more 折叠区原文为空（---more--- 下面要有原有完整回复）。{SUMMARY_REQUIRED_GUIDE}")
        return s, inline_det, f"{s}\n{MORE_DELIMITER}\n{inline_det}"
    if is_short_message(raw) or not require_summary:
        return "", body, body
    n_lines = len(raw.splitlines())
    n_chars = len(raw)
    why = "含代码块" if _CODE_FENCE_RE.search(raw) else f"{n_lines} 行 / {n_chars} 字"
    raise ValueError(f"正文较长（{why}）但未提供白话总结。{SUMMARY_REQUIRED_GUIDE}")


def _card_body_elements(body_text: str, summary: str = "") -> list[dict]:
    """把正文（或 summary + 正文）拆成卡片 body elements：有总结/分隔线时为 [summary_md, more_panel]，否则为单个 [body_md]。"""
    s, d, _ = resolve_summary_and_body(body_text, summary, require_summary=False)
    if s:
        return [{"tag": "markdown", "content": s}, build_more_panel(d)]
    return [{"tag": "markdown", "content": body_text}]


def build_reply_card(text: str, suffix: str, width_mode: str = "compact", summary: str = "") -> dict:
    if not (text or "").strip():
        raise ValueError("正文为空")
    s, d, _ = resolve_summary_and_body(text, summary, require_summary=False)
    if s and suffix:
        elements = [
            {"tag": "markdown", "content": s},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [build_more_panel(d)],
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": f"<font color='grey'>{suffix}</font>",
                                "text_align": "right",
                                "text_size": "notation",
                            }
                        ],
                    },
                ],
            },
        ]
    else:
        elements = _card_body_elements(text, summary)
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
        elements = _card_body_elements(rest.strip("\n"), summary)
    else:
        elements = _card_body_elements(first, summary)
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
