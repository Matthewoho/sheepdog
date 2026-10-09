"""确认表情（7.9）：消息送达会话后以主人身份点表情，主人回复后撤下。

这是 sheepdog 唯一的 IM 写操作，只限加 / 撤这个表情。规则：
- 点：消息实际投递给会话成功之后，投递原因在 [ack] reasons 里；每条消息只点一次（acks 表里有记录就不再点）。
- 撤：入账一条主人自己发的消息时
  - 私聊：同一聊天里、点表情时间早于这条消息的未撤表情全部撤下；
  - 群：reply_to 指向某条已点的消息，或 @ 了它的发送人 → 撤下对应表情；
  - 只撤 reason 在 remove_on_reply_reasons 里的。
- 失败只记日志和 error 列，不阻塞投递和路由，也不重试。
- dry-run：不调接口、不写账本，只打印将要做什么。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import AckConfig
from .models import Message
from .store import SYSTEM_CHAT, Store

log = logging.getLogger("sheepdog")

# 飞书搜索结果的 create_time 只精确到分钟：比较「点表情早于这条消息」时放宽一分钟
_MINUTE = timedelta(seconds=60)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return d if d is None or d.tzinfo else d.astimezone()


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
        for r in rows:
            if r["chat_type"] == SYSTEM_CHAT or r["reason"] not in self.cfg.reasons:
                continue
            if self.store.get_ack(r["message_id"]) is not None:
                continue  # 每条只点一次（含之前点失败的，不重试）
            if self.dry_run:
                print(f"[dryrun] 点表情 {self.cfg.emoji_type} -> {r['message_id']}（{r['reason']}）")
                continue
            try:
                rid, err = self.im.add_reaction(r["message_id"], self.cfg.emoji_type), None
            except Exception as e:  # 失败只记录，不影响投递
                rid, err = None, str(e)[:500]
                log.warning("点表情失败 %s: %s", r["message_id"], err)
            self.store.add_ack(r["message_id"], r["chat_id"], r["chat_type"], r["sender_id"], r["reason"], rid, err)

    # ---------- 撤 ----------
    def candidates(self, m: Message) -> list:
        """主人自己发的消息 m 应当撤下哪些表情。"""
        sent = _parse(m.create_time)
        out = []
        mentioned = {x.id for x in m.mentions if x.id}
        for a in self.store.removable_acks(m.chat_id):
            if a["reason"] not in self.cfg.remove_on_reply_reasons:
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
        if not self.enabled:
            return
        for a in self.candidates(m):
            if self.dry_run:
                print(f"[dryrun] 撤表情 {a['reaction_id']} <- {a['message_id']}（主人发了 {m.message_id}）")
                continue
            try:
                self.im.remove_reaction(a["message_id"], a["reaction_id"])
                self.store.mark_ack_removed(a["message_id"])
            except Exception as e:  # 失败只记录，不重试
                self.store.set_ack_error(a["message_id"], f"撤表情失败: {str(e)[:500]}")
                log.warning("撤表情失败 %s: %s", a["message_id"], e)
