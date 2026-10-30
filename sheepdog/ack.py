"""确认表情（7.9）：消息送达会话后以主人身份点表情，主人回复后撤下。

这是 sheepdog 唯一的 IM 写操作，只限加 / 撤这个表情。规则：
- 点：消息实际投递给会话成功之后，投递原因在 [ack] reasons 里（以主人身份）或 bot_reasons 里（以 bot 身份，
  两者都命中以 bot 为准）；每条消息只点一次（acks 表里有记录就不再点）；被安全规则 hold 的不点（tag 的照常点）。
- bot 身份点的表情永不撤（主人自己的回复，点给主人看「会话已收到」）。
- 撤：入账一条主人自己发的消息时
  - 私聊：同一聊天里、点表情时间早于这条消息的未撤表情全部撤下；
  - 群：reply_to 指向某条已点的消息，或 @ 了它的发送人 → 撤下对应表情；
  - 只撤 reason 在 remove_on_reply_reasons 里的。
- 失败只记日志和 error 列，不阻塞投递和路由，也不重试。
- dry-run：不调接口、不写账本，只打印将要做什么。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .config import AckConfig
from .models import Message
from .security import HOLD
from .store import SYSTEM_CHAT, Store, now_iso

log = logging.getLogger("sheepdog")

# 飞书搜索结果的 create_time 只精确到分钟：比较「点表情早于这条消息」时放宽一分钟
_MINUTE = timedelta(seconds=60)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return d if d is None or d.tzinfo else d.astimezone()


def _alias(reason: str, listed: list[str]) -> str:
    """7.26：@机器人（at_bot）和 @主人（at_me）同等对待——配置里没单独写 at_bot 时按 at_me 算。"""
    if reason == "at_bot" and reason not in listed:
        return "at_me"
    return reason


class Acker:
    def __init__(self, cfg: AckConfig, store: Store, im, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.im = im
        self.dry_run = dry_run

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.emoji_type)

    # ---------- 点 ----------
    def on_delivered(self, rows) -> None:
        """一批消息投递成功后调用。"""
        if not self.enabled:
            return
        absent_chats = (
            {x["chat_id"] for x in self.store.bot_absent_chats()}
            if hasattr(self.store, "bot_absent_chats")
            else set()
        )
        for r in rows:
            mid = r["message_id"]
            if r["chat_type"] == SYSTEM_CHAT or not str(mid or "").startswith("om_"):
                continue
            identity = self.identity_for(r)
            if identity is None:
                continue
            if r["security_action"] == HOLD:
                continue  # 被安全规则拦截的消息不向发送方示意「已看到」
            prev_ack = self.store.get_ack(mid)
            if prev_ack is not None:
                # REG-059：若首次以 bot 点表情撞上 231002/230002（机器人不在会话），允许自动降级 user 重试一次
                prev_err = str(prev_ack["error"] or "")
                can_retry_user = (
                    prev_ack["reaction_id"] is None
                    and prev_ack["identity"] == "bot"
                    and any(tok in prev_err for tok in ("231002", "230002", "Bot has NO permission", "not in chat"))
                )
                if not can_retry_user:
                    continue  # 每条只点一次（含之前点失败的，不重试）
            if identity == "bot" and (r["chat_id"] in absent_chats or prev_ack is not None):
                identity = "user"
            if self.dry_run:
                print(f"[dryrun] 点表情 {self.cfg.emoji_type}（{identity}）-> {mid}（{r['reason']}）")
                continue
            try:
                rid, err = self.im.add_reaction(mid, self.cfg.emoji_type, identity), None
            except Exception as e:  # 失败只记录，不影响投递
                err_s = str(e)
                if identity == "bot" and any(
                    tok in err_s for tok in ("231002", "230002", "Bot has NO permission", "not in chat")
                ):
                    if r["chat_id"] and hasattr(self.store, "note_bot_absent"):
                        self.store.note_bot_absent(r["chat_id"], "", "231002")
                        absent_chats.add(r["chat_id"])
                    identity = "user"
                    try:
                        rid, err = self.im.add_reaction(mid, self.cfg.emoji_type, identity), None
                    except Exception as e2:
                        rid, err = None, str(e2)[:500]
                        log.warning("点表情失败 %s（bot 不在会话退回 user 仍失败）: %s", mid, err)
                else:
                    rid, err = None, err_s[:500]
                    log.warning("点表情失败 %s: %s", mid, err)
            self.store.add_ack(mid, r["chat_id"], r["chat_type"], r["sender_id"], r["reason"], rid, err,
                               identity)

    def identity_for(self, r) -> str | None:
        """以谁的身份点：bot_reasons 优先（bot，永不撤），其次 reasons（user，主人回复后撤），都不在就不点。

        主人本人的消息（带 owner_verified 标记）按 owner_reply 认：包括被 forward 转交的，以及改名前的旧 reason。
        """
        reason = r["reason"]
        if reason == "owner_context":
            return None  # 主人本人的发言只是背景，不点（7.13）
        tags = json.loads(r["tags_json"] or "[]")
        if "owner_verified" in tags or reason == "owner_instruction" or "owner_instruction" in tags:
            # 7.45 A 类：主人在群里 @ 机器人的指令，确认表情和主人在找主人聊天里的回复一样（机器人身份点、永不撤）
            reason = "owner_reply"
        if _alias(reason, self.cfg.bot_reasons) in self.cfg.bot_reasons:
            return "bot"
        if _alias(reason, self.cfg.reasons) in self.cfg.reasons:
            return "user"
        return None

    # ---------- 撤 ----------
    def candidates(self, m: Message) -> list:
        """主人自己发的消息 m 应当撤下哪些表情。"""
        sent = _parse(m.create_time)
        out = []
        mentioned = {x.id for x in m.mentions if x.id}
        for a in self.store.removable_acks(m.chat_id):
            if _alias(a["reason"] or "", self.cfg.remove_on_reply_reasons) not in self.cfg.remove_on_reply_reasons:
                continue
            if a["chat_type"] == "p2p":
                # 私聊：只撤点表情时间早于这条消息的
                added = _parse(a["added_at"])
                if sent is None or added is None or added <= sent + _MINUTE:
                    out.append(a)
            elif a["message_id"] == m.reply_to or (a["sender_id"] and a["sender_id"] in mentioned):
                # 群：回复或 @ 已经明确指向那条消息 / 那个人
                out.append(a)
        return out

    def on_own_message(self, m: Message) -> None:
        """入账一条主人自己发的消息（7.26 起也包括机器人自己发的，即会话以机器人身份代回的）时调。"""
        if not self.enabled:
            return
        for a in self.candidates(m):
            if self.dry_run:
                print(f"[dryrun] 撤表情 {a['reaction_id']} <- {a['message_id']}（主人发了 {m.message_id}）")
                continue
            try:
                try:
                    self.im.remove_reaction(a["message_id"], a["reaction_id"], a["identity"] or "user")
                except TypeError:
                    self.im.remove_reaction(a["message_id"], a["reaction_id"])
                self.store.mark_ack_removed(a["message_id"])
            except Exception as e:  # 失败只记录，不重试
                self.store.set_ack_error(a["message_id"], f"撤表情失败: {str(e)[:500]}")
                log.warning("撤表情失败 %s: %s", a["message_id"], e)


def clear_handled_owner_reactions(store: Store, im, topic_id: str, batch_id: str = "") -> list[str]:
    """REG-058：会话通过 report/ask/reply/resolve 或提交 handled/done 回执处理完毕时，撤下名下已投递/已签收的主人消息确认表情。"""
    raw = (topic_id or "").strip()
    if not raw:
        return []
    tid = raw if raw.startswith("tp_") else f"tp_{raw}"
    base = tid.split("#", 1)[0]
    rows = store.conn.execute(
        """SELECT a.message_id, a.reaction_id, COALESCE(NULLIF(a.identity, ''), 'bot') AS identity
           FROM acks a
           JOIN messages m ON m.message_id = a.message_id
           WHERE a.reaction_id IS NOT NULL AND a.removed_at IS NULL AND a.error IS NULL
             AND (a.reason IN ('owner_reply', 'owner_instruction')
                  OR m.reason IN ('owner_reply', 'owner_instruction')
                  OR COALESCE(m.tags_json, '') LIKE '%"owner_verified"%'
                  OR COALESCE(m.tags_json, '') LIKE '%"owner_instruction"%')
             AND (m.topic_id = ? OR m.topic_id = ? OR m.topic_id LIKE ?)
             AND m.dispatch_state IN ('delivered', 'acked')
           ORDER BY a.added_at""",
        (tid, base, f"{base}#%"),
    ).fetchall()
    cleared: list[str] = []
    for a in rows:
        mid, rid, ident = a["message_id"], a["reaction_id"], a["identity"] or "bot"
        try:
            if im is not None and hasattr(im, "remove_reaction"):
                try:
                    im.remove_reaction(mid, rid, ident)
                except TypeError:
                    im.remove_reaction(mid, rid)
            store.mark_ack_removed(mid)
            cleared.append(mid)
        except Exception as e:
            store.set_ack_error(mid, f"撤表情失败: {str(e)[:500]}")
            log.warning("撤表情失败 %s: %s", mid, e)
    # REG-059：清理该会话名下早期因 bot 231002/230002 未点上表情但已办结的存量 error 行
    stale_rows = store.conn.execute(
        """SELECT a.message_id
           FROM acks a
           JOIN messages m ON m.message_id = a.message_id
           WHERE a.reaction_id IS NULL AND a.removed_at IS NULL
             AND (a.error LIKE '%231002%' OR a.error LIKE '%230002%' OR a.error LIKE '%Bot has NO permission%')
             AND (m.topic_id = ? OR m.topic_id = ? OR m.topic_id LIKE ?)
             AND m.dispatch_state IN ('delivered', 'acked')""",
        (tid, base, f"{base}#%"),
    ).fetchall()
    for sr in stale_rows:
        smid = sr["message_id"]
        with store.tx() as c:
            c.execute("UPDATE acks SET error=NULL, removed_at=? WHERE message_id=?", (now_iso(), smid))
    return cleared


_clear_handled_owner_reactions = clear_handled_owner_reactions
