"""第一期「问得准、答得快」第 1–3 件（7.36）：「需要你定」的来源、可能已处理、快到期提醒、待办清单。

- 来源（escalation_sources）：ask 发成功后当场记——`--about` 指定的消息（origin = about），不给就记这个会话最近一批
  送到的消息（origin = batch，批次号同 outbound.current_batch）。老提问没记来源的，按提问时间之前送到这个会话的最近一批补
  （origin = backfill）。sheepdog 自己生成的消息、「找主人」聊天里的消息不算来源。
- 可能已处理：提问还开着，并且（按先后取第一种）
  1. 主人本人后来在来源聊天里说过话（不是代回、不在找主人聊天）；
  2. 主人对来源消息点了表情（_check_owner_reactions 记下的，sheepdog 自己点的确认表情不算）；
  3. 同来源的另一条提问被主人答复或被会话结案（两条提问不在同一个会话，或都是 --about 指定的来源）。
  标上以后给负责会话排一条系统消息请它核实，每条只提醒一次。不替会话结案：误判会把没办的事当成办完。
- 快到期：离 open_hours 还剩 [escalation] remind_before_hours 小时还开着，提醒负责会话核实一次；到期照旧关闭。
- 待办清单（sheepdog pending / 主人在找主人聊天里发「待办」）：只读，从账本拼。

对会话、对人的说明放 playbook（escalation_check.md、escalation_expiring.md、resolve_notice.md，可选；缺了用这里的默认话），
这里只拼事实。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import duty
from . import engine as eng
from . import session as sm
from .router import message_bodies
from .store import SYSTEM_ID_PREFIX, Store, now_iso

ORIGIN_ABOUT, ORIGIN_BATCH, ORIGIN_BACKFILL = "about", "batch", "backfill"
ORIGIN_LABEL = {ORIGIN_ABOUT: "会话指定", ORIGIN_BATCH: "提问时那一批", ORIGIN_BACKFILL: "补记"}

PH_SPOKE = "owner_spoke"
PH_REACTION = "owner_reaction"
PH_SIBLING_ANSWERED = "sibling_answered"
PH_SIBLING_RESOLVED = "sibling_resolved"

# sends 表里这两种是牧羊犬自己的话（7.36）：拉回来时一律丢弃，不登记「需要你定」、不投给任何会话
SEND_RESOLVE = "resolve"
SEND_PENDING = "pending"
OWN_SEND_KINDS = (SEND_RESOLVE, SEND_PENDING)

# 主人在找主人聊天里单独发这句（顶层消息），牧羊犬直接回待办清单
PENDING_WORDS = ("待办",)

# 系统消息的 reason（批次里的标签在 prompts.REASON_LABEL）
CHECK_REASON = "escalation_check"
EXPIRING_REASON = "escalation_expiring"

# 系统消息里引用主人原话最多多少个字
OWNER_TEXT_CHARS = 200
# 跳过的批次状态：没送到的
_UNDELIVERED = ("failed", "sending")

DEFAULT_CHECK_GUIDE = ("请核实：已经办完就用 `{{cli}} resolve --topic {{topic_id}} --message-id {{ask_message_id}} --why \"<依据>\"` "
                       "结案（同一件事带钥匙的也可以 `--key <钥匙>` 一次结掉这个话题里所有开着的）；还要等主人定就不用管。")
DEFAULT_EXPIRING_GUIDE = ("请核实主人是不是已经在别处处理了：办完了就用 `{{cli}} resolve --topic {{topic_id}} --message-id "
                          "{{ask_message_id}} --why \"<依据>\"` 结案；还要他定，就用 `{{cli}} ask` 带同一把 `--key` 在原话题里再提醒他一次。")
DEFAULT_RESOLVE_NOTICE = "✅ 已结案：{{why}}（{{session}}）"


def _parse(ts: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def _short(text: str, limit: int) -> str:
    return duty.short(text or "", limit)


# ======================= 来源 =======================
def batch_sources(cfg, store: Store, batch_id: str) -> list[tuple[str, str]]:
    """这一批送到的消息里能当来源的：(消息号, 聊天)。去掉 sheepdog 自己生成的、「找主人」聊天里的。"""
    if not batch_id:
        return []
    d = store.get_dispatch(batch_id)
    ids = json.loads(d["message_ids"] or "[]") if d is not None else [
        r[0] for r in store.conn.execute("SELECT message_id FROM messages WHERE batch_id=? ORDER BY rowid", (batch_id,))]
    owner_chats = set(cfg.owner_chat_ids)
    out: list[tuple[str, str]] = []
    for mid in ids:
        if not mid or mid.startswith(SYSTEM_ID_PREFIX):
            continue
        r = store.get_message(mid)
        if r is None or r["chat_id"] in owner_chats:
            continue
        out.append((mid, r["chat_id"]))
    return out


def backfill_batch(store: Store, topic_id: str, asked_at: str) -> str:
    """老提问补来源：提问时间之前送到这个会话的最近一批（没送到的、不带消息的不算）。没有是空串。"""
    asked = _parse(asked_at)
    if asked is None:
        return ""
    rows = store.conn.execute("SELECT * FROM dispatches WHERE topic_id=? ORDER BY sent_at DESC, rowid DESC LIMIT 500",
                              (topic_id,)).fetchall()
    for d in rows:
        if d["state"] in _UNDELIVERED or not json.loads(d["message_ids"] or "[]"):
            continue  # 没送到的、不带消息的（onboarding、规则推送）不算一批
        sent = _parse(d["sent_at"])
        if sent is not None and sent <= asked:
            return d["batch_id"]
    return ""


def about_sources(cfg, store: Store, about_ids: list[str]) -> list[tuple[str, str]]:
    """--about 指定的来源：要是账本里有的消息（才知道在哪个聊天）。不对抛 ValueError。"""
    out: list[tuple[str, str]] = []
    for raw in about_ids:
        for mid in (x.strip() for x in (raw or "").split(",")):
            if not mid:
                continue
            if not mid.startswith("om_"):
                raise ValueError(f"--about 要给 om_ 开头的消息号，收到 {mid!r}")
            r = store.get_message(mid)
            if r is None:
                raise ValueError(f"账本里没有 {mid}（--about 要给 sheepdog 送到过的消息，才知道它在哪个聊天）")
            if (mid, r["chat_id"]) not in out:
                out.append((mid, r["chat_id"]))
    return out


def ensure_sources(cfg, store: Store, e) -> list:
    """这条提问的来源；没记过就按 backfill 补一次（补不出来就是空的，下一轮再试）。"""
    rows = store.escalation_sources(e["message_id"])
    if rows:
        return rows
    srcs = batch_sources(cfg, store, backfill_batch(store, e["topic_id"], e["asked_at"]))
    if srcs:
        store.add_escalation_sources(e["message_id"], e["topic_id"], srcs, ORIGIN_BACKFILL)
        rows = store.escalation_sources(e["message_id"])
    return rows


def _ask_open(cfg, store: Store, ask_mid: str) -> tuple[bool, str]:
    """一条提问还开着没有、几点问的。还没拉回来登记的（刚发出去），按 sends 里的发送时间算。"""
    e = store.get_escalation(ask_mid)
    cutoff = eng._now() - timedelta(hours=cfg.escalation.open_hours)
    if e is not None:
        asked = _parse(e["asked_at"])
        return e["closed_at"] is None and (asked is None or asked >= cutoff), e["asked_at"]
    s = store.send_by_message(ask_mid)
    if s is None or s["kind"] != "ask":
        return False, ""
    sent = _parse(s["sent_at"])
    return sent is not None and sent >= cutoff, s["sent_at"]


def duplicate_asks(cfg, store: Store, topic_id: str, sources: list[tuple[str, str]], exclude: str = "") -> list[dict]:
    """同一来源消息已有别的会话在问（还开着）：[{topic_id, asked_at, message_id}]，按提问时间排。"""
    seen: dict[str, dict] = {}
    for r in store.sources_about([m for m, _ in sources]):
        ask = r["ask_message_id"]
        if ask == exclude or ask in seen or not r["topic_id"] or r["topic_id"] == topic_id:
            continue
        ok, at = _ask_open(cfg, store, ask)
        if ok:
            seen[ask] = {"topic_id": r["topic_id"], "asked_at": at, "message_id": ask}
    return sorted(seen.values(), key=lambda x: x["asked_at"] or "")


def same_matter(topic_a: str, srcs_a, topic_b: str, srcs_b) -> bool:
    """两条提问是不是为同一件事问的：共用一条来源消息，并且不在同一个会话；同一个会话的，要两边都是 --about 指定的
    那条（按批次记的来源一批里常有好几件事，同一个会话在一批里问好几件很常见，不算同一件）。"""
    a = {s["about_message_id"]: s["origin"] for s in srcs_a}
    b = {s["about_message_id"]: s["origin"] for s in srcs_b}
    shared = set(a) & set(b)
    if not shared:
        return False
    if topic_a != topic_b:
        return True
    return any(a[m] == ORIGIN_ABOUT and b[m] == ORIGIN_ABOUT for m in shared)


# ======================= 可能已处理 =======================
@dataclass
class Hit:
    reason: str
    message_id: str          # 主人的发言 / 被点表情的来源消息 / 同来源的另一条提问
    at: str                  # 主人发言时间 / 看到表情的时间 / 另一条提问关闭的时间
    chat_id: str = ""
    chat_name: str = ""
    chat_type: str = ""
    text: str = ""           # 主人的话（前 200 字）/ 被点表情的那条消息 / 另一条提问的摘要
    emoji: str = ""
    other_topic: str = ""    # 同来源的另一条提问是谁问的
    note: str = ""           # 另一条提问的结案依据

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def is_agent_text(cfg, content: str) -> bool:
    suffix = cfg.session.reply_suffix
    return eng.is_agent_reply(content, cfg.owner_context) or bool(suffix and suffix in (content or ""))


def _before(dt: datetime | None, until: datetime | None) -> bool:
    return dt is not None and (until is None or dt <= until)


def detect(cfg, store: Store, e, until: datetime | None = None, sources: list | None = None) -> Hit | None:
    """只读：这条提问是不是「可能已处理」。依次看主人后来在来源聊天里的发言、对来源消息点的表情、同来源的另一条提问。
    until：只看这个时间以前的（回放用）。"""
    asked = _parse(e["asked_at"])
    if asked is None:
        return None
    srcs = sources if sources is not None else store.escalation_sources(e["message_id"])
    if not srcs:
        return None
    me = cfg.self_open_id
    owner_chats = set(cfg.owner_chat_ids)
    chats = sorted({s["about_chat_id"] for s in srcs if s["about_chat_id"] and s["about_chat_id"] not in owner_chats})
    about = [s["about_message_id"] for s in srcs]
    # 1. 主人本人后来在来源聊天里说过话
    if me and chats:
        marks = ",".join("?" * len(chats))
        rows = store.conn.execute(
            f"SELECT * FROM messages WHERE sender_id=? AND chat_id IN ({marks}) AND COALESCE(deleted,0)=0",
            (me, *chats)).fetchall()
        best = None
        for r in rows:
            at = _parse(r["create_time"])
            if at is None or at <= asked or not _before(at, until) or is_agent_text(cfg, r["content"]):
                continue
            if best is None or (at, r["message_id"]) < best[0]:
                best = ((at, r["message_id"]), r)
        if best is not None:
            r = best[1]
            return Hit(PH_SPOKE, r["message_id"], r["create_time"], r["chat_id"], r["chat_name"] or "",
                       r["chat_type"] or "", _short(r["content"], OWNER_TEXT_CHARS))
    # 2. 主人对来源消息点了表情（第一次检查只记基线的不算：reported = 0）
    if me and about:
        marks = ",".join("?" * len(about))
        rows = store.conn.execute(
            f"SELECT * FROM owner_reactions WHERE operator_id=? AND reported=1 AND message_id IN ({marks}) ORDER BY seen_at",
            (me, *about)).fetchall()
        for r in rows:
            at = _parse(r["seen_at"])
            if at is None or at <= asked or not _before(at, until):
                continue
            m = store.get_message(r["message_id"])
            return Hit(PH_REACTION, r["message_id"], r["seen_at"], m["chat_id"] if m is not None else "",
                       (m["chat_name"] or "") if m is not None else "", (m["chat_type"] or "") if m is not None else "",
                       _short(m["content"] if m is not None else "", 80), emoji=r["emoji_type"] or "")
    # 3. 同来源的另一条提问被主人答复或被会话结案
    for other in dict.fromkeys(r["ask_message_id"] for r in store.sources_about(about)):
        if other == e["message_id"]:
            continue
        o = store.get_escalation(other)
        if o is None or o["close_reason"] not in ("answered", "resolved"):
            continue
        if not same_matter(e["topic_id"], srcs, o["topic_id"], store.escalation_sources(other)):
            continue
        at = _parse(o["closed_at"])
        if at is None or at <= asked or not _before(at, until):
            continue
        return Hit(PH_SIBLING_RESOLVED if o["close_reason"] == "resolved" else PH_SIBLING_ANSWERED, other,
                   o["closed_at"], text=ask_title(cfg, o["text"]), other_topic=o["topic_id"],
                   note=o["resolve_note"] or "")
    return None


def hit_of(cfg, store: Store, e) -> Hit | None:
    """已经标上的「可能已处理」按记下的那条还原（给清单和提醒用）；标的那条找不到了再按规则重新判一次。"""
    if not e["possibly_handled_at"]:
        return None
    reason, mid = e["possibly_handled_reason"] or "", e["possibly_handled_message_id"] or ""
    if reason == PH_SPOKE and (m := store.get_message(mid)) is not None:
        return Hit(PH_SPOKE, mid, m["create_time"] or "", m["chat_id"], m["chat_name"] or "", m["chat_type"] or "",
                   _short(m["content"], OWNER_TEXT_CHARS))
    if reason == PH_REACTION:
        r = store.conn.execute("SELECT * FROM owner_reactions WHERE message_id=? AND operator_id=? ORDER BY seen_at LIMIT 1",
                               (mid, cfg.self_open_id)).fetchone()
        m = store.get_message(mid)
        return Hit(PH_REACTION, mid, r["seen_at"] if r is not None else e["possibly_handled_at"],
                   m["chat_id"] if m is not None else "", (m["chat_name"] or "") if m is not None else "",
                   (m["chat_type"] or "") if m is not None else "", _short(m["content"] if m is not None else "", 80),
                   emoji=r["emoji_type"] if r is not None else "")
    if reason in (PH_SIBLING_ANSWERED, PH_SIBLING_RESOLVED) and (o := store.get_escalation(mid)) is not None:
        return Hit(reason, mid, o["closed_at"] or "", text=ask_title(cfg, o["text"]), other_topic=o["topic_id"],
                   note=o["resolve_note"] or "")
    return detect(cfg, store, e) or Hit(reason, mid, e["possibly_handled_at"])


# ======================= 文字 =======================
_ASK_TITLE = re.compile(r"^🐕\s*需要你定\s*·\s*(?P<title>.*?)\s*\[[^\[\]]*\]\s*$")


def ask_title(cfg, text: str) -> str:
    """提问的短标题：sheepdog ask 加的开头一行里 · 和 [..] 之间那段；认不出就是第一行前 40 字。"""
    bodies = message_bodies(text or "")
    body = (bodies[-1] if len(bodies) > 1 else bodies[0]).strip()
    first = body.splitlines()[0].strip() if body else ""
    m = _ASK_TITLE.match(first)
    if m and m.group("title"):
        return m.group("title")
    return _short(first, 40)


def ask_body(text: str, limit: int = 60) -> str:
    """提问正文（去掉开头一行）的前几十个字。"""
    bodies = message_bodies(text or "")
    body = (bodies[-1] if len(bodies) > 1 else bodies[0]).strip()
    lines = body.splitlines()
    return _short(" ".join(lines[1:]) if len(lines) > 1 else "", limit)


def where(chat_type: str, chat_name: str, chat_id: str) -> str:
    if chat_type == "p2p":
        return f"私聊「{chat_name}」" if chat_name else "私聊"
    return f"群「{chat_name or chat_id}」"


def session_label(store: Store, topic_id: str) -> str:
    t = store.get_topic(topic_id)
    title = (t["title"] or "").strip() if t is not None else ""
    return f"{topic_id}「{title}」" if title else topic_id


def short_time(dt: datetime | None, offset: str) -> str:
    """清单里用的短时间（月-日 时:分，时区写在清单开头一次）。"""
    return dt.astimezone(eng._tz(offset)[0]).strftime("%m-%d %H:%M") if dt is not None else "-"


def hit_line(cfg, store: Store, hit: Hit, me_word: str = "主人", short: bool = False) -> str:
    off = cfg.timezone_offset
    dt = _parse(hit.at)
    at = (short_time(dt, off) + " ") if short and dt else eng.display_time(dt, off) if dt else "- "
    mid = hit.message_id if short else f"`{hit.message_id}`"   # 给主人看的清单不带反引号（飞书纯文本会原样显示）
    if hit.reason == PH_SPOKE:
        return (f"{me_word} {at}在{chat_label(cfg, store, hit.chat_id, hit.chat_type, hit.chat_name)}说过：「{hit.text}」"
                f"（message_id {mid}）")
    if hit.reason == PH_REACTION:
        return (f"{me_word}对来源消息点了 {hit.emoji or '表情'}（{at}看到）：{chat_label(cfg, store, hit.chat_id, hit.chat_type, hit.chat_name)}"
                f"「{hit.text}」（message_id {mid}）")
    who = session_label(store, hit.other_topic) if hit.other_topic else "别的会话"
    if hit.reason == PH_SIBLING_RESOLVED:
        return (f"同来源的另一条提问（{who} 问的「{hit.text}」）{at}已被结案：{hit.note or '-'}"
                f"（message_id {mid}）")
    if hit.reason == PH_SIBLING_ANSWERED:
        return f"同来源的另一条提问（{who} 问的「{hit.text}」）{me_word} {at}已经答复（message_id {mid}）"
    return f"标为可能已处理（{hit.reason or '-'}，{hit.message_id or '-'}）"


_PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


def _guide(playbook, name: str, default: str, **values: str) -> str:
    """playbook 里有这个文件就用它，没有就用默认话（同一套占位符）。"""
    text = playbook.render(name, **values) if playbook is not None else ""
    if text:
        return text
    vals = {"cli": "sheepdog", **(getattr(playbook, "base_vars", None) or {}), **values}
    return _PLACEHOLDER.sub(lambda m: vals.get(m.group(1), m.group(0)), default)


def check_message(cfg, store: Store, playbook, e, hit: Hit) -> str:
    off = cfg.timezone_offset
    asked = eng.display_time(_parse(e["asked_at"]), off) if _parse(e["asked_at"]) else e["asked_at"]
    sent = store.send_by_message(e["message_id"])
    key = sent["key"] if sent is not None and sent["key"] else ""
    lines = [f"🔎 你的「需要你定」可能已经处理了：你 {asked}问的「{ask_title(cfg, e['text'])}」"
             f"（message_id `{e['message_id']}`" + (f"，钥匙 {key}" if key else "") + "）。",
             hit_line(cfg, store, hit) + "。",
             _guide(playbook, "escalation_check.md", DEFAULT_CHECK_GUIDE, topic_id=e["topic_id"],
                    ask_message_id=e["message_id"], key=key, title=ask_title(cfg, e["text"]))]
    return "\n".join(lines)


def expiring_message(cfg, store: Store, playbook, e, until: datetime) -> str:
    off = cfg.timezone_offset
    asked = eng.display_time(_parse(e["asked_at"]), off) if _parse(e["asked_at"]) else e["asked_at"]
    left = max(0.0, (until - eng._now()).total_seconds() / 3600)
    sent = store.send_by_message(e["message_id"])
    key = sent["key"] if sent is not None and sent["key"] else ""
    lines = [f"⏰ 你的「需要你定」快到期了：你 {asked}问的「{ask_title(cfg, e['text'])}」（message_id `{e['message_id']}`"
             + (f"，钥匙 {key}" if key else "") + f"）还开着，约 {left:.1f} 小时后（{eng.display_time(until, off)}）自动关闭。",
             _guide(playbook, "escalation_expiring.md", DEFAULT_EXPIRING_GUIDE, topic_id=e["topic_id"],
                    ask_message_id=e["message_id"], key=key, title=ask_title(cfg, e["text"]),
                    hours_left=f"{left:.1f}")]
    return "\n".join(lines)


def resolve_notice(cfg, playbook, why: str, session: str) -> str:
    """结案后机器人在话题里补的那一句。只取一段（多行合成一行），开头不会是「🐕 需要你定」。"""
    text = _guide(playbook, "resolve_notice.md", DEFAULT_RESOLVE_NOTICE, why=" ".join(why.split()), session=session)
    return " ".join(text.split())


# ======================= 每轮：补来源、标可能已处理、排提醒 =======================
def _deliverable_topic(store: Store, topic_id: str) -> bool:
    t = store.get_topic(topic_id)
    return t is not None and t["state"] != sm.CLOSED


def followup_round(cfg, store: Store, playbook) -> dict:
    """Dispatcher 每轮（_expire_escalations 之后）：开着的提问补来源、判可能已处理、排核实提醒和快到期提醒。"""
    out = {"backfilled": 0, "marked": 0, "checked": 0, "expiring": 0}
    now = eng._now()
    esc = cfg.escalation
    for e in eng.open_escalations(store, esc.open_hours):
        mid = e["message_id"]
        had = bool(store.escalation_sources(mid))
        srcs = ensure_sources(cfg, store, e)
        if srcs and not had:
            out["backfilled"] += 1
        if not e["possibly_handled_at"]:
            hit = detect(cfg, store, e, sources=srcs)
            if hit is not None and store.mark_possibly_handled(mid, hit.message_id, hit.reason):
                out["marked"] += 1
        e = store.get_escalation(mid)
        if not _deliverable_topic(store, e["topic_id"]):
            continue
        if e["possibly_handled_at"] and not e["possibly_handled_notified_at"]:
            hit = hit_of(cfg, store, e)
            if hit is not None and store.set_escalation_mark(mid, "possibly_handled_notified_at"):
                store.add_system_message(e["topic_id"], CHECK_REASON, check_message(cfg, store, playbook, e, hit),
                                         [f"escalation_check:{mid}"])
                out["checked"] += 1
        before = esc.remind_before_hours
        asked = _parse(e["asked_at"])
        if before and before < esc.open_hours and asked is not None and not e["expiry_reminded_at"]:
            until = asked + timedelta(hours=esc.open_hours)
            if now >= until - timedelta(hours=before) and store.set_escalation_mark(mid, "expiry_reminded_at"):
                store.add_system_message(e["topic_id"], EXPIRING_REASON,
                                         expiring_message(cfg, store, playbook, e, until),
                                         [f"escalation_expiring:{mid}"])
                out["expiring"] += 1
    return out


# ======================= 待办清单 =======================
def _minutes(a: datetime | None, b: datetime) -> int | None:
    return None if a is None else max(0, int((b - a).total_seconds() // 60))


def waited(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    if minutes < 60:
        return f"{minutes} 分钟"
    h, m = divmod(minutes, 60)
    return f"{h} 小时" + (f" {m} 分" if m else "")


def chat_label(cfg, store: Store, chat_id: str, chat_type: str = "", chat_name: str = "") -> str:
    """聊天的叫法：群用群名；私聊（账本里没有名字）用对方最近一次发言的名字。"""
    if chat_name and chat_type != "p2p":
        return where(chat_type, chat_name, chat_id)
    r = store.conn.execute("SELECT chat_name, chat_type FROM messages WHERE chat_id=? ORDER BY last_seen DESC LIMIT 1",
                           (chat_id,)).fetchone()
    ctype = chat_type or (r["chat_type"] if r is not None else "") or ""
    name = chat_name or (r["chat_name"] if r is not None else "") or ""
    if ctype == "p2p":
        peer = store.conn.execute(
            "SELECT sender_name FROM messages WHERE chat_id=? AND sender_id != ? AND COALESCE(sender_name,'') != '' "
            "AND COALESCE(sender_type,'') NOT IN ('app','bot','system') ORDER BY last_seen DESC LIMIT 1",
            (chat_id, cfg.self_open_id or "")).fetchone()
        return f"和 {peer[0]} 的私聊" if peer is not None else "私聊"
    if r is None and not name:
        return f"聊天 {chat_id}"
    return where(ctype, name, chat_id)


def build(cfg, store: Store, now: datetime | None = None) -> dict:
    """只读：要你定的（同来源合成一组）→ 可能已处理 → 各会话在等你。"""
    now = now or eng._now()
    off = cfg.timezone_offset
    opens = eng.open_escalations(store, cfg.escalation.open_hours)
    items: dict[str, dict] = {}
    about_of: dict[str, tuple] = {}
    for e in opens:
        srcs = store.escalation_sources(e["message_id"])
        if not srcs:  # 只读：不写账本，按补记的规则现算
            b = backfill_batch(store, e["topic_id"], e["asked_at"])
            srcs = [{"about_message_id": m, "about_chat_id": c, "origin": ORIGIN_BACKFILL}
                    for m, c in batch_sources(cfg, store, b)]
        hit = hit_of(cfg, store, e) if e["possibly_handled_at"] else detect(cfg, store, e, sources=srcs)
        sent = store.send_by_message(e["message_id"])
        asked = _parse(e["asked_at"])
        chats = list(dict.fromkeys(s["about_chat_id"] for s in srcs if s["about_chat_id"]))
        items[e["message_id"]] = {
            "message_id": e["message_id"], "topic_id": e["topic_id"], "session": session_label(store, e["topic_id"]),
            "title": ask_title(cfg, e["text"]), "body": ask_body(e["text"]),
            "key": sent["key"] if sent is not None and sent["key"] else "",
            "asked_at": e["asked_at"], "asked_label": short_time(asked, off) if asked else e["asked_at"],
            "waited_minutes": _minutes(asked, now),
            "source_origin": srcs[0]["origin"] if srcs else "",
            "source_chats": [{"chat_id": c, "label": chat_label(cfg, store, c)} for c in chats],
            "possibly_handled": None if hit is None else {**hit.as_dict(), "line": hit_line(cfg, store, hit, "你", short=True),
                                                          "marked": bool(e["possibly_handled_at"])},
        }
        about_of[e["message_id"]] = (e["topic_id"], srcs)
    # 同一件事合成一组（same_matter：共用来源消息、不在同一个会话，或同一个会话都 --about 指定了它）
    groups: list[list[str]] = []
    for mid in items:
        joined = [g for g in groups if any(same_matter(*about_of[mid], *about_of[x]) for x in g)]
        merged = [mid] + [x for g in joined for x in g]
        groups = [g for g in groups if g not in joined] + [merged]
    decide, handled = [], []
    for g in groups:
        asks = sorted((items[x] for x in g), key=lambda x: x["asked_at"] or "")
        grp = {"asks": asks, "same_source": len(asks) > 1}
        (handled if any(a["possibly_handled"] for a in asks) else decide).append(grp)
    decide.sort(key=lambda g: g["asks"][0]["asked_at"] or "")
    handled.sort(key=lambda g: g["asks"][0]["asked_at"] or "")
    # 各会话在等你：看板上「等主人」的活。自动补的「需要你定」就是上面那些，不重复列；
    # 会话自报的、钥匙和上面某条提问相同的也不重复列
    asked_keys = {(i["topic_id"], duty.norm_key(i["key"])) for i in items.values() if i["key"]}
    waiting = []
    stale_h = cfg.duty.stale_hours
    for w in store.open_work():
        if w["state"] != duty.WAITING_OWNER:
            continue
        if not w["declared"] and w["source"] == duty.SRC_ESCALATION:
            continue
        if (w["topic_id"], w["norm_key"]) in asked_keys:
            continue
        upd = _parse(w["declared_at"] if w["declared"] and w["declared_at"] else w["updated_at"])
        stale = bool(w["declared"]) and upd is not None and now - upd >= timedelta(hours=stale_h)
        waiting.append({"topic_id": w["topic_id"], "session": session_label(store, w["topic_id"]), "key": w["key"],
                        "title": w["title"] or "", "source": duty.source_text(w), "declared": bool(w["declared"]),
                        "updated_at": upd.isoformat(timespec="seconds") if upd else "",
                        "updated_minutes": _minutes(upd, now), "stale": stale,
                        "stale_label": duty.stale_label(stale_h) if stale else ""})
    waiting.sort(key=lambda x: (x["stale"], x["updated_at"]))
    return {"generated_at": now.isoformat(timespec="seconds"), "now_label": f"{short_time(now, off)}，{eng._tz(off)[1]}",
            "decide": decide, "possibly_handled": handled, "waiting_owner": waiting}


def _ask_lines(a: dict, n: str, indent: str) -> list[str]:
    head = f"{n}「{a['title']}」— {a['session']}，{a['asked_label']} 问的，等了 {waited(a['waited_minutes'])}"
    if a["key"]:
        head += f"，钥匙 {a['key']}"
    lines = [head]
    if a["body"]:
        lines.append(f"{indent}问的是：{a['body']}")
    if a["source_chats"]:
        lines.append(f"{indent}来源：" + "、".join(c["label"] for c in a["source_chats"][:3])
                     + (f" 等 {len(a['source_chats'])} 个聊天" if len(a["source_chats"]) > 3 else ""))
    else:
        lines.append(f"{indent}来源：没记到")
    if a["possibly_handled"]:
        lines.append(f"{indent}⚠ 可能已处理：{a['possibly_handled']['line']}")
    return lines


def _group_lines(groups: list[dict]) -> list[str]:
    lines: list[str] = []
    for i, g in enumerate(groups, 1):
        if not g["same_source"]:
            lines += _ask_lines(g["asks"][0], f"{i}. ", "   ")
            continue
        lines.append(f"{i}. 同一件事 {len(g['asks'])} 个提问（来源消息相同）：")
        for j, a in enumerate(g["asks"]):
            lines += _ask_lines(a, f"   {chr(ord('a') + j)}) ", "      ")
    return lines or ["（无）"]


def render(data: dict, header: str = "") -> str:
    d, h, w = data["decide"], data["possibly_handled"], data["waiting_owner"]
    n_d = sum(len(g["asks"]) for g in d)
    n_h = sum(len(g["asks"]) for g in h)
    head = header or "待办清单"
    lines = [f"{head}（{data['now_label']}）：要你定的 {n_d} 条，可能已处理的 {n_h} 条，各会话在等你的 {len(w)} 件"]
    lines.append("")
    lines.append(f"一、要你定的（{n_d}）")
    lines += _group_lines(d)
    lines.append("")
    lines.append(f"二、可能已经处理了，请核实（{n_h}）")
    lines += _group_lines(h)
    lines.append("")
    lines.append(f"三、各会话在等你（羊群看板「等主人」，{len(w)}）")
    for i, x in enumerate(w, 1):
        line = f"{i}. {x['session']}：{x['key']}" + (f" {duty.short(x['title'], 40)}" if x["title"] else "")
        line += f"（{x['source']}，{waited(x['updated_minutes'])}前更新）" if x["updated_minutes"] is not None \
            else f"（{x['source']}）"
        if x["stale"]:
            line += f" ⚠ {x['stale_label']}"
        lines.append(line)
    if not w:
        lines.append("（无）")
    return "\n".join(lines)


def is_pending_query(content: str) -> bool:
    return any(" ".join(b.split()) in PENDING_WORDS for b in message_bodies(content or ""))
