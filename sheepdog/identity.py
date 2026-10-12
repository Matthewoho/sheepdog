"""区分智能体身份和本人身份（7.26）。[identity] enabled 开着才起作用；关着时这里的函数都不改变任何行为。

- 机器人自己的 id：open_id（事件、@ 里出现）和 app_id（搜索结果里机器人发的消息，发送人是它）。sheepdog run 启动时
  现查一次记进账本的 meta，配置里手填的优先。
- 机器人在不在一个群：用机器人身份的群列表判断，缓存 [identity] bot_chats_cache_minutes 分钟（记在 meta 里，
  sheepdog reply 在另一个进程里也能用）；发送报「不在群里」（230002）时作废缓存。
- 代回用谁的身份：按被回复的那条消息所在的聊天选（见 choose_reply_identity）。
- 别人私聊机器人：机器人回的那句话（playbook/bot_dm.md，缺了用 DEFAULT_BOT_DM）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from .store import Store, now_iso

log = logging.getLogger("sheepdog")

USER, BOT = "user", "bot"

META_OPEN_ID = "identity_bot_open_id"
META_APP_ID = "identity_bot_app_id"
META_CHATS = "identity_bot_chats"
META_CHATS_AT = "identity_bot_chats_at"
META_CONSUMER = "identity_consumer"

# 飞书：机器人不在群里，发不进去
NOT_IN_CHAT_CODE = "230002"

# 选身份的原因（写进 sheepdog reply 的输出和日志）
WHY_DISABLED = "disabled"           # [identity] 关着：一律主人本人
WHY_UNKNOWN = "unknown_target"      # 账本里没有被回复的那条：不知道在哪个聊天，用主人本人
WHY_P2P = "p2p"                     # 别人和主人本人的私聊：主人本人
WHY_IN_CHAT = "in_chat"             # 群，机器人在：机器人
WHY_NOT_IN_CHAT = "not_in_chat"     # 群，机器人不在：主人本人加标记，记下这个群
WHY_LIST_FAILED = "list_failed"     # 群列表查不到、也没有缓存：主人本人加标记（偏保守）
WHY_230002 = NOT_IN_CHAT_CODE       # 机器人发送时飞书报不在群里：这一条退回主人本人加标记

WHY_LABEL = {
    WHY_DISABLED: "没开 [identity]",
    WHY_UNKNOWN: "账本里没有被回复的那条，不知道在哪个聊天",
    WHY_P2P: "私聊",
    WHY_IN_CHAT: "机器人在这个群里",
    WHY_NOT_IN_CHAT: "机器人不在这个群里",
    WHY_LIST_FAILED: "查不到机器人在哪些群",
    WHY_230002: "机器人发送时飞书报不在群里（230002）",
}

DEFAULT_BOT_DM = "你好，我是 Matthew 的智能体助理。私聊我的消息 Matthew 本人看不到，有事请直接私聊他本人，谢谢！"


def short_key(kind: str, *parts: str) -> str:
    """防重编号（≤ 50 个字符，飞书上限），同 outbound.idem_key 的口径。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"sd{kind[0]}-{digest}"


# ======================= 机器人自己的 id =======================
def resolve_bot_ids(cfg, store: Store, source) -> tuple[str, str]:
    """sheepdog run 启动时调一次：配置里填了用配置，否则现查；查不到沿用上次记下的。记进 meta，返回 (open_id, app_id)。"""
    ic = cfg.identity
    open_id = ic.bot_open_id
    if not open_id and hasattr(source, "bot_open_id"):
        try:
            open_id = source.bot_open_id()
        except Exception as e:
            log.warning("查机器人 open_id 失败，沿用上次记下的: %s", e)
    app_id = ic.bot_app_id
    if not app_id and hasattr(source, "bot_app_id"):
        try:
            app_id = source.bot_app_id()
        except Exception as e:
            log.warning("查机器人 app_id 失败，沿用上次记下的: %s", e)
    open_id = open_id or store.get_meta(META_OPEN_ID)
    app_id = app_id or store.get_meta(META_APP_ID)
    if open_id:
        store.set_meta(META_OPEN_ID, open_id)
    if app_id:
        store.set_meta(META_APP_ID, app_id)
    if not open_id:
        log.warning("不知道机器人的 open_id：@机器人 认不出来（可在 [identity] bot_open_id 手填）")
    return open_id, app_id


def bot_ids(cfg, store: Store) -> frozenset[str]:
    """机器人自己的 id 集合（open_id、app_id）。[identity] 关着是空集。"""
    if not cfg.identity.enabled:
        return frozenset()
    ids = {cfg.identity.bot_open_id or store.get_meta(META_OPEN_ID), cfg.identity.bot_app_id or store.get_meta(META_APP_ID)}
    return frozenset(x for x in ids if x)


# ======================= 机器人在不在群里 =======================
def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def cached_bot_chats(store: Store) -> tuple[set[str] | None, datetime | None]:
    raw = store.get_meta(META_CHATS)
    if not raw:
        return None, None
    try:
        return set(json.loads(raw)), _parse(store.get_meta(META_CHATS_AT))
    except ValueError:
        return None, None


def refresh_bot_chats(store: Store, source) -> set[str]:
    """现查机器人身份的群列表，记进 meta。失败抛出。机器人已经在的群从「机器人不在」的清单里去掉。"""
    ids = set(source.bot_chat_ids())
    store.set_meta(META_CHATS, json.dumps(sorted(ids)))
    store.set_meta(META_CHATS_AT, now_iso())
    store.forget_bot_absent({r["chat_id"] for r in store.bot_absent_chats() if r["chat_id"] in ids})
    return ids


def invalidate_bot_chats(store: Store) -> None:
    store.set_meta(META_CHATS_AT, "")


def bot_in_chat(cfg, store: Store, source, chat_id: str) -> tuple[bool, str]:
    """(在不在, 原因)。缓存没过期用缓存；过期了现查，现查失败用旧缓存，连旧缓存都没有就当不在（WHY_LIST_FAILED）。"""
    ids, at = cached_bot_chats(store)
    fresh = at is not None and datetime.now().astimezone() - at < timedelta(minutes=cfg.identity.bot_chats_cache_minutes)
    if ids is None or not fresh:
        fn = getattr(source, "bot_chat_ids", None)
        try:
            if fn is None:
                raise RuntimeError("消息源不支持查机器人的群列表")
            ids = refresh_bot_chats(store, source)
        except Exception as e:
            log.warning("查机器人的群列表失败%s: %s", "，沿用旧缓存" if ids is not None else "", e)
            if ids is None:
                return False, WHY_LIST_FAILED
    return (True, WHY_IN_CHAT) if chat_id in ids else (False, WHY_NOT_IN_CHAT)


def choose_reply_identity(cfg, store: Store, source, target) -> tuple[str, str]:
    """代回用谁的身份（定稿行为第 5 条），按被回复的那条消息（账本里的行，可以是 None）：
    - 关着：主人本人；
    - 账本里没有这条：主人本人（不知道在哪个聊天）；
    - 私聊（别人和主人本人的私聊）：主人本人；
    - 群：机器人在 → 机器人；不在（或查不到）→ 主人本人。
    返回 (user | bot, 原因)。"""
    if not cfg.identity.enabled:
        return USER, WHY_DISABLED
    if target is None:
        return USER, WHY_UNKNOWN
    if (target["chat_type"] or "") == "p2p":
        return USER, WHY_P2P
    ok, why = bot_in_chat(cfg, store, source, target["chat_id"])
    return (BOT, why) if ok else (USER, why)


def is_not_in_chat_error(e: Exception) -> bool:
    return NOT_IN_CHAT_CODE in str(e)


# ======================= 别人私聊机器人 =======================
def bot_dm_text(playbook) -> str:
    text = playbook.render("bot_dm.md") if playbook is not None else ""
    return text or DEFAULT_BOT_DM


# ======================= 机器人接收子进程的状态（doctor） =======================
def consumer_state(store: Store) -> dict:
    try:
        return json.loads(store.get_meta(META_CONSUMER) or "{}")
    except ValueError:
        return {}
