"""路由器：纯规则、无 IO、无 LLM。决定一条消息是 丢弃 / 进 Inbox / 直推 session。

规则优先级（自上而下，先命中先返回）：
 1. 自己发的            → self（仅入账，供「回复我」判定）
 2. 强制忽略的会话       → drop
 3. 私聊：人 → dispatch；bot → inbox(bot_p2p)，关键词命中则 dispatch
 4. 群 @我             → dispatch
 5. 群 @所有人          → dispatch（可配置）
 6. 免打扰群            → drop（@我/@所有人 已在上面处理）
 7. 回复我的消息         → dispatch
 8. 关键人发言           → dispatch
 9. 关键词              → dispatch（群里机器人发的默认不参与，见 keyword_skip_bot_senders）
10. 强制关注的会话       → dispatch
11. 其他群消息          → inbox

按聊天归属推给名册里的会话（P0.5）是在路由之后的一步，见 engine.apply_ownership；这里保持纯规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .config import RoutingConfig
from .models import Message

DROP = "drop"
INBOX = "inbox"
DISPATCH = "dispatch"
SELF = "self"


@dataclass
class RouteContext:
    self_open_id: str
    muted_chat_ids: set[str] = field(default_factory=set)
    # 判断某条消息是否为「我」发的（用于回复我判定）；由调用方注入以便测试
    is_my_message: Callable[[str], bool] = lambda _mid: False


@dataclass
class RouteDecision:
    route: str
    reason: str
    tags: list[str] = field(default_factory=list)


def _keyword_hit(text: str, keywords: list[str]) -> str:
    low = (text or "").lower()
    for kw in keywords:
        if kw and kw.lower() in low:
            return kw
    return ""


def route(msg: Message, ctx: RouteContext, cfg: RoutingConfig) -> RouteDecision:
    # 1. 自己发的
    if ctx.self_open_id and msg.sender_id == ctx.self_open_id:
        return RouteDecision(SELF, "self")

    # 2. 强制忽略
    if msg.chat_id in cfg.ignore_chat_ids:
        return RouteDecision(DROP, "ignored_chat")

    kw = _keyword_hit(msg.content, cfg.keywords)

    # 3. 私聊
    if msg.is_p2p:
        if msg.is_bot_sender and not cfg.dispatch_bot_p2p:
            if kw:
                return RouteDecision(DISPATCH, "bot_p2p_keyword", [f"keyword:{kw}"])
            return RouteDecision(INBOX, "bot_p2p")
        return RouteDecision(DISPATCH, "p2p")

    mentioned_me = any(m.id == ctx.self_open_id for m in msg.mentions if ctx.self_open_id)
    mentioned_all = any(m.is_all for m in msg.mentions)

    # 4. @我
    if mentioned_me:
        return RouteDecision(DISPATCH, "at_me")
    # 5. @所有人
    if mentioned_all and cfg.dispatch_at_all:
        return RouteDecision(DISPATCH, "at_all")

    # 6. 免打扰群
    if cfg.ignore_muted_chats and msg.chat_id in ctx.muted_chat_ids and msg.chat_id not in cfg.watch_chat_ids:
        return RouteDecision(DROP, "muted_chat")

    # 7. 回复我
    if msg.reply_to and ctx.is_my_message(msg.reply_to):
        return RouteDecision(DISPATCH, "reply_to_me")

    # 8. 关键人
    if msg.sender_id in cfg.vip_sender_ids:
        return RouteDecision(DISPATCH, "vip_sender")

    # 9. 关键词：群里机器人的消息常带「告警」「审批」之类字样，默认不参与，避免误报
    if kw and not (cfg.keyword_skip_bot_senders and msg.is_bot_sender):
        return RouteDecision(DISPATCH, "keyword", [f"keyword:{kw}"])

    # 10. 强制关注
    if msg.chat_id in cfg.watch_chat_ids:
        return RouteDecision(DISPATCH, "watched_chat")

    # 11. 其他
    return RouteDecision(INBOX, "group")
