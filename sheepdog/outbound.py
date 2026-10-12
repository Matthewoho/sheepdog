"""一事一串与发前再看一眼（7.22）：会话找主人、代回，都经 sheepdog 发。

- ask / report：sheepdog 在正文前加开头一行（[threads] ask_header / report_header），以机器人身份私聊主人。
  带钥匙时，同一会话同一把钥匙第一次在主聊天里起一条（话题开头），之后回复到它的话题里（--reply-in-thread）。
- reply：以主人身份代回（卡片，右下角带代回标记）。发之前只读检查：这个聊天里、从要回复的那条之后，主人本人有没有
  新发言。检查只用搜索接口（不会把消息标成已读），不写账本里的消息、不动拉取进度、不点表情；
  主人说过就不发，把他的话原样打印给会话，记一个「发送前已看到」。
- thread：只读列出一个话题的全部消息。

每次发送都带防重编号（--idempotency-key，最长 50 个字符）：由会话、钥匙或批次、正文指纹（代回：批次 + 被回复的
消息号 + 正文指纹）算出来，固定不变；内容一样才算重复。发成功的记进 sends 表，重试时先查它，发过的不再调飞书；超时的带同一个编号重试，飞书只发一条。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import duty
from . import session as sm
from .card import build_reply_card
from .config import Config
from .engine import BUS_TOPIC_ID, MANAGED_KINDS, display_time, is_agent_reply, order_thread_items, root_position
from .models import Message
from .source import SourceError
from .source.lark import LarkTimeout
from .store import Store, now_iso

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


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


# ======================= ask / report =======================
def current_batch(store: Store, topic_id: str) -> str:
    """会话没传 --batch 时用的批次号：它当前在跑的批次（送出了、等回执的那批）；没有就用它最近一批；都没有是空串。
    同一批里重跑只发一条，隔批再说同样的话不会被当成重复，会话也不用自己记批次号。"""
    t = store.get_topic(topic_id)
    if t is not None and t["pending_batch_id"]:
        return t["pending_batch_id"]
    return store.latest_batch_id(topic_id)


def compose(cfg: Config, kind: str, topic_id: str, title: str, body: str) -> str:
    return cfg.threads.header(kind, _one_line(title), topic_id) + "\n" + body.strip()


def send_to_owner(cfg: Config, store: Store, source, kind: str, topic_id: str, title: str, body: str,
                  key: str = "", batch: str = "") -> SendResult:
    """ask / report：以机器人身份私聊主人。带钥匙：第一次起新话题，之后回到它的话题里。"""
    if kind not in (ASK, REPORT):
        raise OutboundError(EXIT_INVALID, f"未知种类 {kind!r}")
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    if not _one_line(title):
        raise OutboundError(EXIT_INVALID, "--title 不能为空：一句话说是什么事")
    if not (body or "").strip():
        raise OutboundError(EXIT_INVALID, "正文为空")
    if not cfg.self_open_id and not cfg.threads.owner_chat_id:
        raise OutboundError(EXIT_INVALID, "配置里没有 self_open_id，不知道主人是谁")
    key = _one_line(key)
    nk = duty.norm_key(key) if key else ""
    text = compose(cfg, kind, topic_id, title, body)
    ik = idem_key(kind, topic_id, nk, batch or current_batch(store, topic_id), text)
    prev = store.get_send(ik)
    if prev is not None:
        return SendResult(prev["message_id"], already=True, root_message_id=prev["root_message_id"] or "", text=text)
    root = store.thread_by_key(topic_id, nk) if nk else None
    try:
        if root is not None:
            res = source.reply_to(root["root_message_id"], identity="bot", idem_key=ik, text=text, in_thread=True)
        elif cfg.threads.owner_chat_id:
            # 真实飞书测试用：发进测试群，不私聊主人
            res = source.send_to_chat(cfg.threads.owner_chat_id, text, ik, "bot")
        else:
            res = source.send_to_user(cfg.self_open_id, text, ik, "bot")
    except LarkTimeout as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")
    except SourceError as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送失败：{e}")
    mid = res["message_id"]
    new_thread = False
    if root is not None:
        root_mid = root["root_message_id"]
        store.touch_thread(root_mid)
        if not store.set_thread_id(root_mid, res.get("thread_id") or "") and not root["thread_id"]:
            learn_thread_id(store, source, root_mid)
    elif nk:
        root_mid = mid
        new_thread = store.add_thread(mid, topic_id, key, nk, _one_line(title), ASK, res.get("thread_id") or "")
    else:
        root_mid = ""
    store.add_send(ik, kind, topic_id, mid, text, root_message_id=root_mid, key=key, norm_key=nk)
    # 7.15：发给主人的每条都记来源，主人引用回复它时按这里送回（拉取到这条时还会按第一行再认一次，重复记不变）
    store.add_bot_outbox(mid, topic_id, kind == ASK, text, now_iso())
    return SendResult(mid, root_message_id=root_mid, new_thread=new_thread, text=text)


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
        if is_agent_reply(m.content, cfg.owner_context) or (suffix and suffix in (m.content or "")):
            continue  # 会话以主人身份代回的
        seen.add(m.message_id)
        hits.append(m)
    hits.sort(key=lambda m: (m.create_time, m.message_id))
    return OwnerSpoke(hits, since)


def reply_body(cfg: Config, text: str = "", card_text: str = "") -> tuple[str, dict | None]:
    """代回的内容：卡片（沿用 reply-card：正文 + 右下角代回标记），或纯文本（末尾附同一个代回标记，别人一眼看出是代回、
    sheepdog 也靠它认出是会话代回）。返回 (记账用的正文, 卡片或 None)。"""
    suffix = cfg.session.reply_suffix
    if card_text:
        card = build_reply_card(card_text, suffix, cfg.session.reply_card_width)  # 空正文抛 ValueError
        return card_text, card
    if not (text or "").strip():
        raise ValueError("正文为空")
    plain = text.rstrip()
    if suffix and suffix not in plain:
        plain += "\n" + suffix
    return plain, None


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
    message_id: str = ""
    already: bool = False
    spoke: OwnerSpoke | None = None
    newly_seen: int = 0
    text: str = ""


def reply(cfg: Config, store: Store, source, topic_id: str, target_id: str, *, text: str = "", card_text: str = "",
          even_if_owner_replied: bool = False) -> ReplyResult:
    """代回：先查重（发过同样的就不再发），再发前检查（主人说过话就不发、记「发送前已看到」），最后以主人身份发。"""
    if not target_ok(store, topic_id):
        raise OutboundError(EXIT_INVALID, f"{topic_id} 不是在投递的会话（总线、名册 managed、总线新开的）")
    target_id = (target_id or "").strip()
    if not target_id.startswith("om_"):
        raise OutboundError(EXIT_INVALID, f"--to 要给 om_ 开头的消息号，收到 {target_id!r}")
    try:
        shown, card = reply_body(cfg, text, card_text)
    except ValueError as e:
        raise OutboundError(EXIT_INVALID, str(e))
    ik = reply_idem(store, topic_id, target_id, shown)
    prev = store.get_send(ik)
    if prev is not None:
        return ReplyResult(prev["message_id"], already=True, text=shown)  # 同样的内容已经发过（重试）
    target = store.get_message(target_id)
    if not even_if_owner_replied:
        if target is None:
            raise OutboundError(EXIT_INVALID, f"账本里没有 {target_id}，没法做发前检查（以账本里这条消息的时间为准）。"
                                              "确认消息号；是 sheepdog 送给你的消息就一定在账本里")
        spoke = owner_spoke_since(cfg, store, source, target)
        if spoke.messages:
            n = sum(store.add_seen_before_send(topic_id, m.message_id, target_id) for m in spoke.messages)
            return ReplyResult(spoke=spoke, newly_seen=n, text=shown)
    try:
        res = source.reply_to(target_id, identity="user", idem_key=ik, text="" if card is not None else shown,
                              card=card)
    except LarkTimeout as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送超时，不确定发出去没有（{e}）：用同样的参数重试，防重编号保证只发一条")
    except SourceError as e:
        raise OutboundError(EXIT_SEND_FAILED, f"发送失败：{e}")
    mid = res["message_id"]
    store.add_send(ik, REPLY, topic_id, mid, shown, target_message_id=target_id)
    store.add_bot_outbox(mid, topic_id, False, shown, now_iso())
    return ReplyResult(mid, text=shown)


def spoke_text(cfg: Config, spoke: OwnerSpoke) -> str:
    """把主人的话原样打印给会话（时间 + 内容）。"""
    lines = [f"主人已经在这个聊天里说过话（从 {display_time(_parse(spoke.since), cfg.timezone_offset)}起），这条代回没发："]
    for m in spoke.messages:
        lines.append(f"- {display_time(_parse(m.create_time), cfg.timezone_offset)}（message_id {m.message_id}）：")
        lines += ["  " + ln for ln in (m.content or "").splitlines() or [""]]
    lines.append("这些发言之后会照常作为背景送到你这里，会注明「你发送前已经看到过」，不是新的提醒。")
    lines.append("先读他的话再决定：不用回了就不回；要改就改了再发；确实还要照样发，加 --even-if-owner-replied。")
    return "\n".join(lines)


# ======================= thread：只读看整个话题 =======================
@dataclass
class ThreadView:
    thread: sqlite3.Row | None
    thread_id: str
    root_message_id: str
    items: list[dict]
    warning: str = ""


def find_thread(store: Store, topic_id: str, key: str = "", thread: str = "") -> tuple[sqlite3.Row | None, str, str]:
    """按钥匙或话题编号（omt_ / 开头那条的 om_）找话题。返回 (话题行或 None, thread_id, 开头那条)。
    钥匙找不到、或话题记在别的会话名下（总线除外）抛 OutboundError。"""
    if key:
        thr = store.thread_by_key(topic_id, duty.norm_key(key))
        if thr is None:
            raise OutboundError(EXIT_INVALID, f"{topic_id} 名下没有钥匙 {key!r} 的话题（ask / report 带这把 --key 发过才有）")
        return thr, thr["thread_id"] or "", thr["root_message_id"]
    thread = (thread or "").strip()
    if not thread.startswith(("omt_", "om_")):
        raise OutboundError(EXIT_INVALID, "--thread 要给 omt_ 开头的话题编号或开头那条的 om_ 消息号")
    thr = store.thread_by_thread_id(thread) or store.thread_by_root(thread)
    if thr is not None:
        if thr["topic_id"] != topic_id and topic_id != BUS_TOPIC_ID:
            raise OutboundError(EXIT_INVALID, f"这个话题记在 {thr['topic_id']} 名下，不是你的")
        return thr, thr["thread_id"] or "", thr["root_message_id"]
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
