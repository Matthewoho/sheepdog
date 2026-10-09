"""代回卡片（7.17）：会话以主人身份回复时发一张 Card 2.0 卡片——正文 + 右下角灰色小字标记。

`sheepdog reply-card` 打印卡片 JSON，会话再用 lark-cli 以 interactive 消息发出。正文原样放进 markdown 元素，
支持卡片 markdown 与 `<at id=ou_xxx></at>`。
"""

from __future__ import annotations


def build_reply_card(text: str, suffix: str, width_mode: str = "compact") -> dict:
    if not (text or "").strip():
        raise ValueError("正文为空")
    elements: list[dict] = [{"tag": "markdown", "content": text}]
    if suffix:
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{suffix}</font>",
                         "text_align": "right", "text_size": "notation"})
    return {"schema": "2.0", "config": {"width_mode": width_mode}, "body": {"elements": elements}}
