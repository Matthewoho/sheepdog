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


def build_reply_card(text: str, suffix: str, width_mode: str = "compact") -> dict:
    if not (text or "").strip():
        raise ValueError("正文为空")
    elements: list[dict] = [{"tag": "markdown", "content": text}]
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


def build_owner_card(text: str, at_open_id: str = "") -> dict:
    """给主人的卡片：第一行进 header 标题，其余原样进一个 markdown 元素；at_open_id 有值时末尾另起一个 markdown 元素 @ 他。
    只有一行（结案那一句这类）时不放 header，这一行就是正文。正文为空抛 ValueError。"""
    text = (text or "").strip("\n")
    if not text.strip():
        raise ValueError("正文为空")
    first, _, rest = text.partition("\n")
    card: dict = {"schema": "2.0"}
    elements: list[dict] = []
    if rest.strip():
        card["header"] = {"title": {"tag": "plain_text", "content": owner_title(first)},
                          "template": TEMPLATE_DECIDE if DECIDE_WORD in first else TEMPLATE_DEFAULT}
        elements.append({"tag": "markdown", "content": rest.strip("\n")})
    else:
        elements.append({"tag": "markdown", "content": first})
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
