"""长期跟踪的话题放进专门的话题群（7.51 改版 v1）。

一件事一个话题（事项，编号 T-<数字>），按钥匙认、跨会话合并。进群只有三个入口（不中途搬家）：
1. 会话第一次用一把钥匙发 ask / report 时带 --track（outbound.send_to_owner）；
2. 主人在私聊的某个话题里、或引用某条消息，整句只回「放群里」（engine.Collector._tg_move）；
3. 主人在话题群里新开一个顶层话题（engine.Collector._topic_group_message）。

话题开头那条是静态的标题卡（标题、编号、谁开的），之后永远不改。状态集中在群里置顶的一张索引卡：每
[topic_group] index_refresh_minutes 分钟按证据对账一次，整张覆盖（patch）；发出满 13 天重发一张。状态由证据算：
等你定 = 名下有开着的「需要你定」；过期未定 = 最近一条提问过期了还没答；等别人 = 羊群看板上这把钥匙的活在等别人；
沉睡 = stale_after_days 天没动静；办完只认明确的命令（sheepdog track done、主人在话题里整句回「办完了」「关掉」）。
每个话题里只在三种变化时发一行：等你定、办完、重开。办完 archive_after_hours 小时没动静就归档，归档满 30 天删掉。

牧羊犬自己在群里发的（标题卡、索引卡、变化说明、「已交给 …」、提示）都记进 sends（kind = tg_*），读回时一律丢弃，
不登记「需要你定」、不进 bot_outbox。会话经 sheepdog ask / report 发进群里的照旧登记。

群发不进去（飞书报错），或者每 10 分钟查一次群成员发现机器人或主人不在群里：退回私聊（ask / report 照旧进私聊话题），
发一条系统通知；成员核对正常以后恢复。有别人进群、别人在群里说话：私聊提醒主人，那人说的话不分派。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import duty
from . import engine as eng
from .card import build_owner_card, card_fits, owner_title, plain_text
from .router import message_bodies
from .store import Store, now_iso

log = logging.getLogger("sheepdog")

ITEM_PREFIX = "T-"

# sends.kind：牧羊犬自己在群里（和私聊旧话题里）发的话。读回时一律丢弃
KIND_TITLE = "tg_title"        # 标题卡（话题开头那条）
KIND_INDEX = "tg_index"        # 索引卡
KIND_CHANGE = "tg_change"      # 话题里的变化说明（等你定 / 办完 / 重开）
KIND_HANDOFF = "tg_handoff"    # 「已交给 …」
KIND_NOTE = "tg_note"          # 提示（「看置顶的索引卡」、移出群、认不出是哪件事）
KIND_MOVED = "tg_moved"        # 私聊旧话题里那句「这件事已经放进话题群」
OWN_KINDS = (KIND_TITLE, KIND_INDEX, KIND_CHANGE, KIND_HANDOFF, KIND_NOTE, KIND_MOVED)

# 整句命令（整句完全一致才算，空白不算；在路由之前拦下）
CMD_MOVE = ("放群里",)
CMD_DONE = ("办完了", "关掉")
CMD_REMOVE = ("移出群",)

# 事项的生命周期（明确的事件才改）
ACTIVE, DONE, ARCHIVED, REMOVED = "active", "done", "archived", "removed"
# 事项从哪个入口进来
ORIGIN_TRACK, ORIGIN_MOVE, ORIGIN_OWNER = "track", "owner_move", "owner_topic"
ORIGIN_LABEL = {ORIGIN_TRACK: "会话 --track", ORIGIN_MOVE: "主人「放群里」", ORIGIN_OWNER: "主人在群里新开"}

# 索引卡上的状态（每次按证据现算）
S_DECIDE, S_EXPIRED, S_DOING, S_WAITING, S_SLEEPING, S_DONE, S_ARCHIVED = (
    "decide", "expired", "doing", "waiting_other", "sleeping", "done", "archived")
STATUS_LABEL = {S_DECIDE: "等你定", S_EXPIRED: "过期未定", S_DOING: "进行中", S_WAITING: "等别人", S_SLEEPING: "沉睡",
                S_DONE: "最近办完", S_ARCHIVED: "已归档"}
INDEX_ORDER = (S_DECIDE, S_EXPIRED, S_DOING, S_WAITING, S_SLEEPING, S_DONE)
SUGGEST = "suggest"

# 归档超过这么多天的钥匙再被用到，算一件新事（只附一句「可能相关」）
REOPEN_WITHIN_DAYS = 14
# 归档满这么多天删掉（事项表、钥匙、它指向的私聊话题都不跟 7 天保留期走）
PURGE_AFTER_DAYS = 30
# 索引卡发出满这么多天就重发一张（改卡只能改 14 天内发的）
INDEX_RENEW_DAYS = 13
# 多久查一次群成员
MEMBER_CHECK_MINUTES = 10
# 「建议放进群」的门槛在配置 [topic_group] suggest_*（10-16 18:0x 架构侧收紧：只看拖过几小时、主人回过几次，
# 最近 suggest_active_hours 小时有动静，最多列 suggest_max 件）
# 索引卡每栏最多列几件（卡片有 30KB 上限）
INDEX_ROWS = 12

META_DOWN = "tg_down"                  # 群不可用（退回私聊）：{"since", "why"}；空 = 正常
META_INDEX = "tg_index"                # 当前索引卡：{"message_id", "sent_at", "hash", "thread_id", "previous"}
META_INDEX_PENDING = "tg_index_pending"  # 正在发的新索引卡的防重编号（发成了清空）
META_DIRTY = "tg_dirty"                # 有事项变化，下一轮就对账
META_REFRESHED = "tg_index_refreshed_at"
META_MEMBERS_AT = "tg_members_at"
META_STRANGERS = "tg_strangers"        # 已经提醒过的别人（open_id 列表）
NOTICE_KIND = "话题群通知"

DEFAULT_LINK_HOST = "https://applink.larksuite.com"


# ======================= 钥匙、编号 =======================
def track_norm(key: str) -> str:
    """事项的钥匙比较用：`-`、`_`、空白都当成一样（全部去掉），再转大写。SSL-CERT、ssl_cert、SSL CERT 是同一把。"""
    return re.sub(r"[\s_\-]+", "", key or "").upper()


def _excerpt(text: str, limit: int) -> str:
    """给主人看的摘录（标题卡、索引卡、话题里那一行）：用「待办」那套去 markdown、合成一行、在句子边界截断（pending.excerpt）。"""
    from .pending import excerpt
    return excerpt(text or "", limit)


def label(item_no: int) -> str:
    return f"{ITEM_PREFIX}{item_no}"


def parse_item_no(key: str) -> int | None:
    """T-12 / t12 / T_12 → 12；不是事项编号返回 None。"""
    m = re.fullmatch(r"T(\d+)", track_norm(key))
    return int(m.group(1)) if m else None


def whole(content: str) -> str:
    """整句命令比较用：卡片还原成文字，正文（去掉引用外壳）去掉所有空白。"""
    bodies = message_bodies(plain_text(content or ""))
    return "".join((bodies[-1] if bodies else "").split())


def is_command(content: str, words: tuple[str, ...]) -> bool:
    return whole(content) in words


def _parse(ts: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def idem(kind: str, *parts: str) -> str:
    """防重编号（≤ 50 个字符），同 outbound.idem_key 的口径：sd<种类第 3 个字>-<指纹>。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"sd{kind[3] if kind.startswith('tg_') else kind[0]}-{digest}"


# ======================= 开关、群状态 =======================
def configured(cfg) -> bool:
    return bool(cfg.topic_group.chat_id)


def down_info(store: Store) -> dict | None:
    raw = store.get_meta(META_DOWN)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"since": "", "why": raw}


def usable(cfg, store: Store) -> bool:
    """配了话题群、开着、没有退回私聊。"""
    return cfg.topic_group.active and down_info(store) is None


def notify_owner(cfg, store: Store, text: str, kind: str = NOTICE_KIND) -> None:
    """系统通知（照旧私聊主人）：设了 [notify] file 写进文件；否则落给主人的通知账本，由常驻进程下一轮发（发不出去按退避重试）。"""
    path = cfg.notify_path
    try:
        if path is not None:
            from .notify import FileNotifier
            FileNotifier(path, cfg.timezone_offset).notify(text, kind)
            return
        store.add_owner_notice(kind, "", text.encode("utf-8"), eng.new_idempotency_key())
    except Exception as e:   # 通知出错不挡主流程
        log.warning("话题群的系统通知没记上（%s）: %s", kind, e)


def mark_down(cfg, store: Store, why: str) -> bool:
    """群不可用：退回私聊，发一条系统通知（已经退回的不重复）。返回这次是不是刚退回。"""
    if down_info(store) is not None:
        return False
    store.set_meta(META_DOWN, json.dumps({"since": now_iso(), "why": why}, ensure_ascii=False))
    log.warning("话题群不可用，退回私聊：%s", why)
    notify_owner(cfg, store, f"🐕 汇报 · 话题群不可用，已退回私聊 [{eng.BUS_TOPIC_ID}]\n{why}\n"
                             "之后的提问、汇报照旧发进私聊；牧羊犬每 10 分钟核一次群成员，正常了自动恢复。")
    return True


def clear_down(cfg, store: Store) -> bool:
    info = down_info(store)
    if info is None:
        return False
    store.set_meta(META_DOWN, "")
    store.set_meta(META_DIRTY, "1")
    log.info("话题群恢复（之前：%s）", info.get("why", ""))
    notify_owner(cfg, store, f"🐕 汇报 · 话题群恢复了 [{eng.BUS_TOPIC_ID}]\n之前退回私聊的原因：{info.get('why', '')}。"
                             "之后长期跟踪的事照旧发进话题群。")
    return True


def mark_dirty(store: Store) -> None:
    store.set_meta(META_DIRTY, "1")


# ======================= 事项 =======================
def find_by_key(store: Store, key: str):
    """按钥匙找事项：事项编号（T-12）直接找；否则按 track_norm 找最近的那件（含已移出、已归档，由调用方判断）。"""
    no = parse_item_no(key)
    if no is not None:
        row = store.get_item(no)
        if row is not None:
            return row
    rows = store.items_by_norm_key(track_norm(key))
    return rows[0] if rows else None


def item_keys(store: Store, item_no: int) -> list[str]:
    return [r["key"] for r in store.item_keys(item_no) if r["kind"] != "number"]


def open_item(store: Store, title: str, owner_topic: str, opened_by: str, origin: str, chat_id: str, *,
              key: str = "", topic_id: str = "", dm_root: str = "", related: int | None = None,
              root_message_id: str = "", thread_id: str = ""):
    """开一件事项：编号也登记成钥匙（T-12），会话的钥匙登记成钥匙。返回事项行。"""
    no = store.add_item(title=_excerpt(title, 60) or "（无标题）", owner_topic=owner_topic, opened_by=opened_by,
                        origin=origin, chat_id=chat_id, dm_root_message_id=dm_root, related_item=related,
                        root_message_id=root_message_id, thread_id=thread_id)
    store.add_item_key(no, label(no), track_norm(label(no)), "number", "")
    if key:
        store.add_item_key(no, key, track_norm(key), "key", topic_id or owner_topic)
    mark_dirty(store)
    log.info("开了事项 %s「%s」（%s，负责 %s）", label(no), title, ORIGIN_LABEL.get(origin, origin), owner_topic or "-")
    return store.get_item(no)


def touch(store: Store, item_no: int) -> None:
    store.update_item(item_no, last_activity_at=now_iso())


def reopen(store: Store, item) -> bool:
    """办完 / 归档的事项又有动静：重开（索引卡和话题里的「重开」那一行由常驻进程下一轮补）。"""
    if item["state"] not in (DONE, ARCHIVED):
        return False
    store.update_item(item["item_no"], state=ACTIVE, reopened_at=now_iso(), archived_at=None)
    mark_dirty(store)
    log.info("事项 %s 重开", label(item["item_no"]))
    return True


def close(store: Store, item, by: str, why: str) -> bool:
    """办完（只认明确的命令）。"""
    if item["state"] not in (ACTIVE,):
        return False
    store.update_item(item["item_no"], state=DONE, done_at=now_iso(), done_by=by, done_why=why, last_activity_at=now_iso())
    mark_dirty(store)
    log.info("事项 %s 办完（%s）：%s", label(item["item_no"]), by, why)
    return True


def remove(store: Store, item, by: str) -> bool:
    """移出群：之后这把钥匙的消息回私聊。"""
    if item["state"] == REMOVED:
        return False
    store.update_item(item["item_no"], state=REMOVED, removed_at=now_iso(), done_by=by)
    mark_dirty(store)
    return True


def session_title(store: Store, topic_id: str) -> str:
    if not topic_id:
        return "-"
    if topic_id == eng.BUS_TOPIC_ID:
        t = store.get_topic(topic_id)
        return (t["title"] or "总线").strip() if t is not None else "总线"
    t = store.get_topic(topic_id)
    return ((t["title"] or "").strip() or topic_id) if t is not None else topic_id


def deliverable(store: Store, topic_id: str) -> bool:
    t = store.get_topic(topic_id) if topic_id else None
    return t is not None and t["state"] != "closed" and (topic_id == eng.BUS_TOPIC_ID or t["kind"] in eng.MANAGED_KINDS)


def owner_of(store: Store, item) -> str:
    """事项现在的负责会话；它收掉了就是总线。"""
    tid = item["owner_topic"] or ""
    return tid if deliverable(store, tid) else eng.BUS_TOPIC_ID


# ======================= 链接 =======================
def _link_host(store: Store) -> str:
    """链接的域名（applink.larksuite.com / applink.feishu.cn）：按账本里已有的 message_app_link 取，记进 meta 不再查。"""
    known = store.get_meta("tg_link_host")
    if known:
        return known
    r = store.conn.execute("SELECT link FROM messages WHERE link LIKE 'https://%/client/%' LIMIT 1").fetchone()
    m = re.match(r"^(https://[^/]+)/client/", r[0]) if r is not None else None
    if m is None:
        return DEFAULT_LINK_HOST
    store.set_meta("tg_link_host", m.group(1))
    return m.group(1)


def thread_link(store: Store, chat_id: str, thread_id: str, root_message_id: str = "") -> str:
    """话题的链接：账本里开头那条的 message_app_link 是话题链接（client/thread/open）就用它；否则按聊天和话题编号拼
    （同一个格式：open_chat_id + open_thread_id，10-16 正式账本核过）；话题编号还不知道就是空串。"""
    if root_message_id:
        row = store.get_message(root_message_id)
        if row is not None and "/client/thread/open" in (row["link"] or ""):
            return row["link"]
    if not (chat_id and thread_id):
        return ""
    return (f"{_link_host(store)}/client/thread/open?open_chat_id={chat_id}&open_thread_id={thread_id}"
            f"&openchatid={chat_id}&openthreadid={thread_id}")


def item_link(store: Store, item) -> str:
    return thread_link(store, item["chat_id"] or "", item["thread_id"] or "", item["root_message_id"] or "")


def dm_thread_link(store: Store, dm_root: str) -> str:
    if not dm_root:
        return ""
    thr = store.thread_by_root(dm_root)
    row = store.get_message(dm_root)
    chat = row["chat_id"] if row is not None else ""
    link = thread_link(store, chat, (thr["thread_id"] if thr is not None else "") or
                       (row["thread_id"] if row is not None else "") or "", dm_root)
    # 话题编号还不知道：退一步用那条消息自己的链接（打开私聊、定位到那条）
    return link or ((row["link"] or "") if row is not None else "")


# ======================= 进群：发送这一侧 =======================
@dataclass
class SendRoute:
    """ask / report 带钥匙时去哪：group = 进群里的事项话题（item 为空表示要新开）；否则私聊。note 是给会话看的一句提示。"""
    group: bool = False
    item: object = None
    related: int | None = None
    note: str = ""


def route_for_send(cfg, store: Store, topic_id: str, key: str, track: bool) -> SendRoute:
    """进群入口 1（--track）和「同一把钥匙的新消息都发进群」。"""
    if not key:
        return SendRoute()
    if not configured(cfg) or not cfg.topic_group.enabled:
        return SendRoute(note="没配话题群（[topic_group]），--track 不生效，照旧发私聊" if track else "")
    if down_info(store) is not None:
        item = find_by_key(store, key)
        if track or (item is not None and item["state"] != REMOVED):
            return SendRoute(note="话题群暂时不可用（已退回私聊），这条发进了私聊")
        return SendRoute()
    item = find_by_key(store, key)
    related = None
    if item is not None and item["state"] == REMOVED:
        return SendRoute(note=f"这件事（{label(item['item_no'])}）主人已经移出话题群，照旧发私聊" if track else "")
    if item is not None and item["state"] == ARCHIVED:
        at = _parse(item["archived_at"])
        if at is not None and eng._now() - at > timedelta(days=REOPEN_WITHIN_DAYS):
            related, item = item["item_no"], None
    if item is not None:
        return SendRoute(group=True, item=item)
    if not track:
        return SendRoute()
    if store.thread_by_key(topic_id, duty.norm_key(key)) is not None:
        return SendRoute(note="这把钥匙在私聊里已经开过话题，--track 只在第一次用这把钥匙时有效，这条照旧发进私聊的那个话题；"
                              "要放进群请主人在那个话题里回「放群里」")
    return SendRoute(group=True, related=related)


TITLE_FOOTER = "在这里回复就是回给负责的会话；整句回「办完了」表示办完，「移出群」退回私聊"
INDEX_TITLE = "📌 话题群索引"
INDEX_EMPTY = "现在没有在跟的事"
INDEX_FOOTER = "在话题里回复就是回给负责的会话；整句回「办完了」表示办完，「移出群」退回私聊"
SUGGEST_HOW = "在私聊那件事的话题里整句回「放群里」"


def _grey(text: str) -> str:
    return f"<font color='grey'>{text}</font>"


def _name(cfg, store: Store, topic_id: str) -> str:
    """会话显示名：和「待办」卡片同一套（pending.session_name，去掉 [managed] 这类前缀）。"""
    from .pending import session_name
    return session_name(cfg, store, topic_id) if topic_id else "-"


def opener_phrase(cfg, store: Store, item) -> str:
    """标题卡副标题里「谁开的」：会话开的写「<显示名>开的」，主人从私聊搬进来的写「你从私聊放进群」。"""
    if item["origin"] == ORIGIN_MOVE:
        return "你从私聊放进群"
    if item["origin"] == ORIGIN_OWNER or item["opened_by"] == "owner":
        return "你开的"
    return f"{_name(cfg, store, item['opened_by'] or item['owner_topic'] or '')}开的"


def title_view(cfg, store: Store, item, opener: str = "", key: str = "") -> dict:
    """标题卡（话题开头那条，之后永远不改）的内容，卡片和纯文本共用：{title, subtitle, related, footer}。
    第一行（标题）不会被认成「需要你定」或哪个会话的消息（读回时按 sends 的种类一律丢弃）。"""
    no = label(item["item_no"])
    opened = _parse(item["opened_at"])
    when = opened.astimezone(eng._tz(cfg.timezone_offset)[0]).strftime("%m-%d %H:%M") if opened else ""
    sub = [f"钥匙 {key}" if key else "", opener or opener_phrase(cfg, store, item), when]
    related = ""
    if item["related_item"]:
        old = store.get_item(item["related_item"])
        link = item_link(store, old) if old is not None else ""
        related = f"可能相关：{label(item['related_item'])}" + (f" [打开]({link})" if link else "") + "，那件已经归档"
    return {"title": f"🗂 {no} · {item['title']}", "subtitle": " · ".join(x for x in sub if x), "related": related,
            "footer": TITLE_FOOTER}


def title_text(v: dict) -> str:
    """标题卡的纯文本版（卡片被拒、[notify] owner_card 关着时发它；sends 也记它）。"""
    return "\n".join(x for x in (v["title"], v["subtitle"], v["related"], v["footer"]) if x)


def title_card(v: dict) -> dict:
    """标题卡（Card 2.0）：header 是标题和副标题（钥匙 · 谁开的 · 时间），蓝色；正文只有灰色小字一行（前面可能有「可能相关」）。"""
    els = []
    if v["related"]:
        els.append({"tag": "markdown", "content": v["related"]})
    els.append({"tag": "markdown", "content": _grey(v["footer"]), "text_size": "notation"})
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": owner_title(v["title"])},
                       "subtitle": {"tag": "plain_text", "content": v["subtitle"]}, "template": "blue"},
            "body": {"elements": els}}


def post_top(cfg, store: Store, source, kind: str, text: str, ik: str, topic_id: str = "", card: dict | None = None,
             force_card: bool = False) -> dict:
    """在话题群里发一条顶层消息（就是一个新话题）。返回飞书的 {message_id, chat_id, thread_id}。失败原样抛。"""
    from .outbound import owner_content, send_owner
    if card is not None:
        # 调用方拼好的卡片：索引卡一律发卡片（force_card，改卡只能改卡片）；标题卡照 [notify] owner_card，太大退回纯文本
        plain, c = text, (card if force_card or (cfg.notify.owner_card and card_fits(card)) else None)
    else:
        plain, c = owner_content(cfg, text)
        if force_card:
            c = build_owner_card(text)
    chat = cfg.topic_group.chat_id
    res = send_owner(lambda t, cc: source.send_to_chat(chat, t, ik, "bot", **({"card": cc} if cc is not None else {})),
                     plain, c)
    store.add_send(ik, kind, topic_id, res["message_id"], text, root_message_id=res["message_id"])
    return res


def post_in_thread(cfg, store: Store, source, root: str, kind: str, text: str, ik: str, topic_id: str = "",
                   at: bool = False) -> dict:
    """在一个话题里回一条（--reply-in-thread）。"""
    from .outbound import owner_content, send_owner
    prev = store.get_send(ik)
    if prev is not None:
        return {"message_id": prev["message_id"], "already": True}
    plain, c = owner_content(cfg, text, at=at)
    res = send_owner(lambda t, cc: source.reply_to(root, identity="bot", idem_key=ik, text=t, card=cc, in_thread=True),
                     plain, c)
    store.add_send(ik, kind, topic_id, res["message_id"], text, root_message_id=root)
    return res


def ensure_title(cfg, store: Store, source, item, opener: str = "", key: str = ""):
    """事项还没有标题卡（刚开、或上次发送超时）：发一张（同一件事防重编号固定，超时重试只发一条）。返回最新的事项行。
    opener 是副标题里「谁开的」那一段；空 = 按事项的来路现算（opener_phrase）。"""
    if item["root_message_id"]:
        return item
    ik = idem(KIND_TITLE, cfg.topic_group.chat_id, str(item["item_no"]), item["opened_at"] or "")
    v = title_view(cfg, store, item, opener, key)
    res = post_top(cfg, store, source, KIND_TITLE, title_text(v), ik, card=title_card(v))
    store.update_item(item["item_no"], root_message_id=res["message_id"], thread_id=res.get("thread_id") or None,
                      chat_id=cfg.topic_group.chat_id)
    if not res.get("thread_id"):
        learn_thread_id(store, source, item["item_no"], res["message_id"])
    return store.get_item(item["item_no"])


def learn_thread_id(store: Store, source, item_no: int, root: str) -> str:
    """发完不知道话题编号（返回里没有）：只读 mget 一次开头那条补上。失败不影响，之后从拉取里补。"""
    fn = getattr(source, "messages_by_id", None)
    if fn is None:
        return ""
    try:
        got = fn([root])
    except Exception:
        return ""
    tid = next((m.thread_id for m in got if m.message_id == root and m.thread_id), "")
    if tid:
        store.update_item(item_no, thread_id=tid)
    return tid


# ======================= 状态（按证据现算） =======================
def item_asks(store: Store, item) -> list:
    """这件事名下会话发过的「需要你定」（escalations 行），按提问时间倒序：群里这个话题里的，加上「放群里」搬进来的事项
    私聊原话题（dm_root_message_id）里的——私聊里那条提问还开着，这件事就是在等主人定（10-17 01:57 P1C：DEMO-G7 搬进
    群以后状态算成进行中，主人在群里回的话也认不出是在答它）。"""
    out = []
    seen: set[str] = set()
    n = 0
    for root in (item["dm_root_message_id"], item["root_message_id"]):
        if not root:
            continue
        mids = [s["message_id"] for s in store.sends_in_thread(root) if s["kind"] == "ask"]
        if root == item["dm_root_message_id"] and root not in mids:
            mids.insert(0, root)   # 私聊原话题开头那条本身就是提问（不是经 sheepdog ask 发的、sends 里没有）
        for mid in mids:
            if mid in seen:
                continue
            seen.add(mid)
            e = store.get_escalation(mid)
            if e is not None:
                out.append((e["asked_at"] or "", n, e))
            n += 1
    # 提问时间只到分钟（搜索结果的 create_time）：同一分钟里按发出先后（私聊在前、群里在后，各自按 sends 的顺序）
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e for _, _, e in out]


def answered_in_flight(store: Store) -> set[str]:
    """已经有主人的回复（或抄送）带着 escalation:<提问> 排在路上、还没送到会话的提问。它们在送到的那一刻才标「已答」，
    但在主人眼里已经答了（10-17 00:37 P1C：同一轮读回两条 ask、又拉到主人没引用的回复，话题里多发了一行过时的「等你定」）。"""
    out: set[str] = set()
    prefix = eng.ESCALATION_TAG_PREFIX
    for (tags,) in store.conn.execute(
            "SELECT tags_json FROM messages WHERE route='dispatch' AND COALESCE(dispatch_state,'pending') "
            "IN ('pending','sending','uncertain') AND tags_json LIKE ?", (f'%"{prefix}%',)):
        for t in json.loads(tags or "[]"):
            if t.startswith(prefix):
                out.add(t[len(prefix):])
    return out


def open_asks(cfg, store: Store, item) -> list:
    """这件事里还开着的「需要你定」：没结、没过期，也没有主人的回复正排着要送过去（按这一刻的最终状态算）。"""
    cutoff = eng._now() - timedelta(hours=cfg.escalation.open_hours)
    flying = answered_in_flight(store)
    return [e for e in item_asks(store, item) if not e["closed_at"] and e["message_id"] not in flying
            and ((a := _parse(e["asked_at"])) is None or a >= cutoff)]


def waiting_other(store: Store, item) -> bool:
    keys = {duty.norm_key(k) for k in item_keys(store, item["item_no"])} | {duty.norm_key(label(item["item_no"]))}
    for k in keys:
        for w in store.open_work_by_key(k):
            if w["state"] == duty.WAITING_OTHER:
                return True
    return False


def status(cfg, store: Store, item, now: datetime | None = None) -> str:
    now = now or eng._now()
    if item["state"] == ARCHIVED:
        return S_ARCHIVED
    if item["state"] == DONE:
        return S_DONE
    if open_asks(cfg, store, item):
        return S_DECIDE
    asks = item_asks(store, item)
    if asks and asks[0]["close_reason"] == "expired":
        return S_EXPIRED
    if waiting_other(store, item):
        return S_WAITING
    last = _parse(item["last_activity_at"] or item["opened_at"])
    if last is not None and now - last >= timedelta(days=cfg.topic_group.stale_after_days):
        return S_SLEEPING
    return S_DOING


# ======================= 建议放进群（私聊里长期在跟的） =======================
def suggestions(cfg, store: Store, now: datetime | None = None, owner_chats: list[str] | None = None) -> list[dict]:
    """私聊话题（7.22 一事一串）里还没进群、最近 [topic_group] suggest_active_hours 小时内有动静（会话往里发，或主人在里面
    回复）、并且满足任一条的：从开出来到最近一次动静超过 suggest_after_hours 小时；主人在里面回过 suggest_owner_replies 次
    以上。同一把钥匙（track_norm）只列最近那个话题。按最近动静从新到旧排，全部返回（列几件由 render_index 按 suggest_max 截）。
    只读。"""
    g = cfg.topic_group
    now = now or eng._now()
    me = cfg.self_open_id
    active_from = now - timedelta(hours=g.suggest_active_hours)
    chats = owner_chats if owner_chats is not None else cfg.owner_chat_ids
    # 最近一次动静要算上主人的回复（owner_threads.last_post_at 只记会话往里发的），所以先不按时间筛，逐个话题算
    rows = store.conn.execute("SELECT * FROM owner_threads WHERE norm_key IS NOT NULL AND key IS NOT NULL "
                              "AND created_at <= ?", (now.isoformat(timespec="seconds"),)).fetchall()
    moved = {r[0] for r in store.conn.execute(
        "SELECT dm_root_message_id FROM tracked_items WHERE dm_root_message_id IS NOT NULL AND state != 'removed'")}
    out: dict[str, dict] = {}
    for r in rows:
        nk = track_norm(r["key"])
        if r["root_message_id"] in moved:
            continue
        it = find_by_key(store, r["key"])
        if it is not None and it["state"] in (ACTIVE, DONE):
            continue
        created = _parse(r["created_at"])
        last = _parse(r["last_post_at"] or r["created_at"])
        replies: list = []
        if me and r["thread_id"] and chats:
            marks = ",".join("?" * len(chats))
            replies = store.conn.execute(
                f"SELECT create_time FROM messages WHERE thread_id=? AND sender_id=? AND chat_id IN ({marks}) "
                "AND create_time <= ?", (r["thread_id"], me, *chats, now.isoformat(timespec="seconds"))).fetchall()
            for x in replies:
                t = _parse(x["create_time"])
                if t is not None and (last is None or t > last):
                    last = t
        if last is None or last < active_from:
            continue   # 最近 suggest_active_hours 小时没动静：不列
        span = (last - created).total_seconds() / 3600 if created else 0
        why = []
        if span >= g.suggest_after_hours:
            why.append(f"拖了 {span:.0f} 小时")
        if len(replies) >= g.suggest_owner_replies:
            why.append(f"你回过 {len(replies)} 次")
        if not why:
            continue
        cur = out.get(nk)
        cand = {"key": r["key"], "norm_key": nk, "title": r["title"] or r["key"], "topic_id": r["topic_id"],
                "root_message_id": r["root_message_id"], "thread_id": r["thread_id"] or "", "why": why,
                "last_at": last.isoformat(timespec="seconds") if last else "", "span_hours": round(span, 1),
                "owner_replies": len(replies)}
        if cur is None or cand["last_at"] > cur["last_at"]:
            out[nk] = cand
    return sorted(out.values(), key=lambda x: x["last_at"], reverse=True)


# ======================= 索引卡 =======================
def build_index(cfg, store: Store, now: datetime | None = None) -> dict:
    """只读：索引卡的内容（各栏的事项，每件带会话显示名和状态相关的时间）。"""
    from .pending import _hm, waited
    now = now or eng._now()
    off = cfg.timezone_offset
    sections: dict[str, list[dict]] = {k: [] for k in INDEX_ORDER}
    per_session: dict[str, int] = {}

    def ago(dt) -> str:
        if dt is None:
            return ""
        m = max(0, int((now - dt).total_seconds() // 60))
        return f"{m // 1440} 天" if m >= 48 * 60 else waited(m)   # 两天以上按天

    for it in store.list_items((ACTIVE, DONE)):
        st = status(cfg, store, it, now)
        if st not in sections:
            continue
        owner = it["owner_topic"] or ""
        if it["state"] == ACTIVE and owner:
            per_session[owner] = per_session.get(owner, 0) + 1
        last = _parse(it["last_activity_at"] or it["opened_at"])
        if st == S_DECIDE:
            e = open_asks(cfg, store, it)[0]
            asked = _parse(e["asked_at"])
            when = f"{_hm(e['asked_at'], now, off)} 问 · 已等 {ago(asked)}" if asked else ""
        elif st == S_EXPIRED:
            e = item_asks(store, it)[0]
            when = f"{_hm(e['asked_at'], now, off)} 问 · 已过期没答"
        elif st == S_DONE:
            when = f"{_hm(it['done_at'] or '', now, off)} 办完"
        else:   # 进行中、等别人、沉睡
            when = f"最近动静 {ago(last)}前" if last else ""
        sections[st].append({"item_no": it["item_no"], "label": label(it["item_no"]), "title": it["title"],
                             "owner": _name(cfg, store, owner) if owner else "-", "link": item_link(store, it),
                             "when": when, "last": it["last_activity_at"] or it["opened_at"] or ""})
    for k in sections:
        sections[k].sort(key=lambda x: x["last"], reverse=True)
    sugg = suggestions(cfg, store, now)
    for x in sugg:
        x["name"] = _name(cfg, store, x["topic_id"])
        x["link"] = dm_thread_link(store, x["root_message_id"])
        x["ago"] = ago(_parse(x["last_at"]))
    return {"generated_at": now.isoformat(timespec="seconds"), "sections": sections, "suggest": sugg,
            "per_session": {_name(cfg, store, k): v for k, v in sorted(per_session.items(), key=lambda kv: -kv[1])}}


def index_view(cfg, data: dict, rows: int = INDEX_ROWS) -> dict:
    """给主人看的索引卡，卡片和纯文本共用：{subtitle, n_total, n_decide, sections: [{label, items}], suggest, more_suggest,
    per_session}。空栏不列。每件 {head: 「T-1 · 标题」, link, meta: 「会话显示名 · 时间」}。"""
    from .pending import _hm
    now = _parse(data["generated_at"]) or eng._now()
    n_total = sum(len(v) for v in data["sections"].values())
    n_decide = len(data["sections"][S_DECIDE])
    secs = []
    for st in INDEX_ORDER:
        items = data["sections"][st]
        if not items:
            continue
        shown = [{"head": f"{x['label']} · {_excerpt(x['title'], 40)}", "link": x["link"],
                  "meta": " · ".join(y for y in (x["owner"], x["when"]) if y)} for x in items[:rows]]
        secs.append({"label": f"{STATUS_LABEL[st]} · {len(items)} 件", "items": shown, "more": len(items) - len(shown)})
    sugg = data["suggest"]
    cap = min(rows, cfg.topic_group.suggest_max)
    sug = [{"head": _excerpt(s["title"], 40), "link": s.get("link") or "",
            "meta": " · ".join(y for y in (s.get("name") or s["topic_id"].removeprefix("tp_"), f"钥匙 {s['key']}",
                                            "、".join(s["why"]), f"最近动静 {s['ago']}前" if s.get("ago") else "") if y)}
           for s in sugg[:cap]]
    return {"subtitle": f"{_hm(data['generated_at'], now, cfg.timezone_offset)} 更新 · 跟踪 {n_total} 件 · 等你定 {n_decide}",
            "n_total": n_total, "n_decide": n_decide, "sections": secs, "suggest": sug, "n_suggest": len(sugg),
            "more_suggest": max(0, len(sugg) - cap), "per_session": data["per_session"]}


def _per_session_line(v: dict) -> str:
    return "各会话跟踪：" + " · ".join(f"{k} {n}" for k, n in v["per_session"].items()) if v["per_session"] else ""


def index_text(v: dict) -> str:
    """索引卡的纯文本版（sends 记它，对账也按它比）。顺序同卡片。"""
    lines = [f"{INDEX_TITLE} · {v['subtitle']}"]
    if not v["sections"]:
        lines += ["", INDEX_EMPTY]
    for sec in v["sections"]:
        lines += ["", sec["label"]]
        for x in sec["items"]:
            lines.append(f"- {x['head']}" + (f"  {x['link']}" if x["link"] else ""))
            lines.append(f"  {x['meta']}")
        if sec["more"]:
            lines.append(f"- 另有 {sec['more']} 件")
    if v["suggest"]:
        lines += ["", f"建议放进群 · {v['n_suggest']} 件", SUGGEST_HOW]
        for x in v["suggest"]:
            lines += [f"- {x['head']}" + (f"  {x['link']}" if x["link"] else ""), f"  {x['meta']}"]
        if v["more_suggest"]:
            lines.append(f"- 另有 {v['more_suggest']} 件")
    if v["sections"]:
        lines += [""] + [x for x in (_per_session_line(v), INDEX_FOOTER) if x]
    return "\n".join(lines)


def index_card(v: dict) -> dict:
    """索引卡（Card 2.0），排版同「待办」卡片：header「📌 话题群索引」+ 副标题（几点更新 · 跟踪几件 · 等你定几件），
    等你定多于 0 件橙色，否则蓝色；只列非空的栏，栏标题加粗；每件两行：加粗的「T-1 · 标题」跟「[打开](链接)」，灰色小字
    「会话显示名 · 时间」；件与件、栏与栏之间分隔线。全空只写一行「现在没有在跟的事」。「建议放进群」有内容才出现，
    第一行灰色小字说怎么放。末尾灰色小字说怎么回。"""
    els: list[dict] = []

    def rows_of(items: list[dict], more: int) -> None:
        for i, x in enumerate(items):
            if i:
                els.append({"tag": "hr"})
            head = f"**{x['head']}**" + (f" [打开]({x['link']})" if x["link"] else "")
            els.append({"tag": "markdown", "content": f"{head}\n{_grey(x['meta'])}"})
        if more:
            els.append({"tag": "markdown", "content": _grey(f"另有 {more} 件")})

    if not v["sections"]:
        els.append({"tag": "markdown", "content": INDEX_EMPTY})
    for n, sec in enumerate(v["sections"]):
        if n:
            els.append({"tag": "hr"})
        els.append({"tag": "markdown", "content": f"**{sec['label']}**"})
        rows_of(sec["items"], sec["more"])
    if v["suggest"]:
        els.append({"tag": "hr"})
        els.append({"tag": "markdown", "content": f"**建议放进群 · {v['n_suggest']} 件**\n{_grey(SUGGEST_HOW)}"})
        rows_of(v["suggest"], v["more_suggest"])
    if v["sections"]:
        tail = [x for x in (_per_session_line(v), INDEX_FOOTER) if x]
        els.append({"tag": "markdown", "content": "\n".join(_grey(x) for x in tail), "text_size": "notation"})
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": INDEX_TITLE},
                       "subtitle": {"tag": "plain_text", "content": v["subtitle"]},
                       "template": "orange" if v["n_decide"] > 0 else "blue"},
            "body": {"elements": els}}


def render_index(cfg, data: dict, rows: int = INDEX_ROWS) -> str:
    """索引卡的纯文本版（兼容老调用）。"""
    return index_text(index_view(cfg, data, rows))


# ======================= 常驻进程每轮（Dispatcher） =======================
def dispatcher_round(disp) -> dict:
    """群成员核对、归档、「已交给 …」、话题里的变化说明、索引卡对账。每一步出错只记日志，不挡投递。"""
    cfg, store = disp.cfg, disp.store
    out: dict = {}
    if not configured(cfg) or not cfg.topic_group.enabled:
        return out
    source = getattr(disp, "source", None)
    if source is None:
        return out
    from .source import SourceError
    from .source.lark import LarkTimeout
    try:
        out["members"] = check_members(cfg, store, source)
    except Exception as e:
        log.warning("话题群成员核对出错: %s", e)
    if down_info(store) is not None:
        return out
    now = eng._now()
    archive_due(cfg, store, now)
    try:
        for it in store.list_items((ACTIVE,)):
            if not it["root_message_id"] and it["origin"] != ORIGIN_OWNER:
                # 标题卡上次发送超时（或「放群里」时超时）：同一个防重编号补发，飞书只发一条
                keys = item_keys(store, it["item_no"])
                ensure_title(cfg, store, source, it, "", keys[0] if keys else "")
        for it in store.list_items((ACTIVE, DONE, ARCHIVED, REMOVED)):
            if it["handoff_to"] and it["root_message_id"]:
                post_handoff(cfg, store, source, it)
            if it["state"] in (ACTIVE, DONE, ARCHIVED) and it["root_message_id"]:
                post_change(cfg, store, source, it, now)
        if index_due(cfg, store, now):
            out["index"] = refresh_index(cfg, store, source, now)
    except LarkTimeout as e:
        log.warning("话题群发送超时，下一轮重试（同一个防重编号）: %s", e)
    except SourceError as e:
        mark_down(cfg, store, f"往话题群发消息失败：{' '.join(str(e).split())[:200]}")
    return out


def archive_due(cfg, store: Store, now: datetime) -> int:
    n = 0
    hours = cfg.topic_group.archive_after_hours
    for it in store.list_items((DONE,)):
        last = max(filter(None, (_parse(it["done_at"]), _parse(it["last_activity_at"]))), default=None)
        if last is not None and now - last >= timedelta(hours=hours):
            store.update_item(it["item_no"], state=ARCHIVED, archived_at=now.isoformat(timespec="seconds"),
                              status=S_ARCHIVED)
            mark_dirty(store)
            n += 1
    return n


def post_handoff(cfg, store: Store, source, item) -> None:
    to = item["handoff_to"]
    text = f"➡️ 已交给「{_name(cfg, store, to)}」"
    ik = idem(KIND_HANDOFF, str(item["item_no"]), to, item["handoff_at"] or "")
    post_in_thread(cfg, store, source, item["root_message_id"], KIND_HANDOFF, text, ik)
    store.update_item(item["item_no"], handoff_to=None)


def change_lines(cfg, store: Store, item, st: str) -> list[tuple[str, str]]:
    """话题里的变化说明（只有三种：重开、等你定、办完）：[(正文, 防重编号的种子)]。别的变化（进行中、等别人、沉睡、过期未定、
    归档）不发。重开同时又在等你定（同一把钥匙的提问把它重开了），两行都发，先「重开」。"""
    prev = item["status"] or ""
    out = []
    if prev in (S_DONE, S_ARCHIVED) and st not in (S_DONE, S_ARCHIVED):
        out.append(("🔁 重开", "reopen:" + (item["reopened_at"] or "")))
    if st == S_DECIDE and prev != S_DECIDE:
        # 发之前再核一次：那条提问这一刻还开着（没结、没有主人的回复排在路上）才发
        e = open_asks(cfg, store, item)
        if e:
            from .pending import ask_title
            what = _excerpt(ask_title(cfg, e[0]["text"]), 40)
            out.append(("🟠 等你定" + (f"：{what}" if what else ""), "decide:" + e[0]["message_id"]))
    if st == S_DONE and prev not in (S_DONE, S_ARCHIVED):
        who = item["done_by"] or ""
        who = "你" if who == "owner" else _name(cfg, store, who) if who else ""
        why = item["done_why"] or ""
        out.append(("✅ 办完了" + (f"：{why}" if why else "") + (f"（{who}）" if who else ""),
                    "done:" + (item["done_at"] or "")))
    return out


def post_change(cfg, store: Store, source, item, now: datetime) -> None:
    st = status(cfg, store, item, now)
    if st == (item["status"] or ""):
        return
    for text, seed in change_lines(cfg, store, item, st):
        post_in_thread(cfg, store, source, item["root_message_id"], KIND_CHANGE, text,
                       idem(KIND_CHANGE, str(item["item_no"]), seed))
    store.update_item(item["item_no"], status=st)
    mark_dirty(store)


def index_due(cfg, store: Store, now: datetime) -> bool:
    if store.get_meta(META_DIRTY) == "1":
        return True
    last = _parse(store.get_meta(META_REFRESHED))
    return last is None or now - last >= timedelta(minutes=cfg.topic_group.index_refresh_minutes)


def index_info(store: Store) -> dict:
    raw = store.get_meta(META_INDEX)
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def refresh_index(cfg, store: Store, source, now: datetime) -> str:
    """对账：内容变了就整张覆盖；还没有、或发出满 13 天就重发一张并置顶。返回做了什么（posted / patched / same）。"""
    data = build_index(cfg, store, now)
    rows = INDEX_ROWS
    v = index_view(cfg, data, rows)
    text, card = index_text(v), index_card(v)
    while not card_fits(card) and rows > 1:   # 太大就每栏少列几件（卡片 30KB 上限）
        rows = max(1, rows // 2)
        v = index_view(cfg, data, rows)
        text, card = index_text(v), index_card(v)
    info = index_info(store)
    h = _hash(_strip_time(text))
    sent = _parse(info.get("sent_at"))

    def done(what: str) -> str:   # 发成了（或不用发）才清「要对账」；超时、失败下一轮再来
        store.set_meta(META_DIRTY, "")
        store.set_meta(META_REFRESHED, now.isoformat(timespec="seconds"))
        return what

    if info.get("message_id") and sent is not None and now - sent < timedelta(days=INDEX_RENEW_DAYS):
        if info.get("hash") == h:
            return done("same")
        source.patch_card(info["message_id"], card)
        info["hash"] = h
        store.set_meta(META_INDEX, json.dumps(info, ensure_ascii=False))
        log.info("索引卡改了（%s）", info["message_id"])
        return done("patched")
    # 防重编号：换下来的那张 + 第一次试的时间，发之前记进 meta；发送超时、下一轮重试用同一个编号，飞书只发一条
    ik = store.get_meta(META_INDEX_PENDING) or idem(KIND_INDEX, cfg.topic_group.chat_id, info.get("message_id") or "",
                                                     now.isoformat(timespec="minutes"))
    store.set_meta(META_INDEX_PENDING, ik)
    res = post_top(cfg, store, source, KIND_INDEX, text, ik, card=card, force_card=True)
    store.set_meta(META_INDEX_PENDING, "")
    mid = res["message_id"]
    old = info.get("message_id")
    new = {"message_id": mid, "sent_at": now.isoformat(timespec="seconds"), "hash": h,
           "thread_id": res.get("thread_id") or "", "previous": old or ""}
    store.set_meta(META_INDEX, json.dumps(new, ensure_ascii=False))
    try:
        source.pin(mid)
    except Exception as e:
        log.warning("置顶索引卡 %s 失败（卡片照样在群里）: %s", mid, e)
    if old:
        retire_index(cfg, store, source, old, mid)
    log.info("索引卡%s发了一张（%s）", "重" if old else "", mid)
    return done("posted")


def retire_index(cfg, store: Store, source, old: str, new: str) -> None:
    """换下来的旧索引卡：改成一句「已换新卡」（还在 14 天内能改）；[topic_group] unpin_old_index 开着才取消置顶。"""
    try:
        source.patch_card(old, {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": INDEX_TITLE},
                                                            "subtitle": {"tag": "plain_text", "content": "已停用"},
                                                            "template": "grey"},
                                "body": {"elements": [{"tag": "markdown", "content": _grey("这张已停用，请看群里最新置顶的那张索引卡")}]}})
    except Exception as e:
        log.warning("改旧索引卡 %s 失败: %s", old, e)
    if cfg.topic_group.unpin_old_index:
        try:
            source.unpin(old)
        except Exception as e:
            log.warning("取消旧索引卡 %s 的置顶失败: %s", old, e)


def _strip_time(text: str) -> str:
    """对账比内容时不算第一行（对账时间每次都不一样）。"""
    return text.partition("\n")[2]


# ======================= 群成员 =======================
def check_members(cfg, store: Store, source, now: datetime | None = None) -> str:
    """每 MEMBER_CHECK_MINUTES 分钟查一次群成员：机器人或主人不在 → 退回私聊；都在 → 恢复；有别人 → 私聊提醒（每人一次）。
    查询本身失败只记日志（不知道就不改状态）。"""
    now = now or eng._now()
    last = _parse(store.get_meta(META_MEMBERS_AT))
    if last is not None and now - last < timedelta(minutes=MEMBER_CHECK_MINUTES):
        return ""
    fn = getattr(source, "chat_members", None)
    if fn is None:
        return ""
    store.set_meta(META_MEMBERS_AT, now.isoformat(timespec="seconds"))
    chat = cfg.topic_group.chat_id
    try:
        got = fn(chat)
    except Exception as e:
        log.warning("查话题群成员失败（不改状态）: %s", e)
        return "error"
    users, bots = set(got.get("users") or []), set(got.get("bots") or [])
    me = cfg.self_open_id
    from . import identity as ident
    bot_ids = {x for x in (cfg.identity.bot_open_id or store.get_meta(ident.META_OPEN_ID),
                           cfg.identity.bot_app_id or store.get_meta(ident.META_APP_ID)) if x}
    missing = []
    if me and me not in users:
        missing.append("主人")
    if bot_ids and not (bot_ids & bots):
        missing.append("机器人")
    others = sorted(u for u in users if u != me)
    if others:
        warn_strangers(cfg, store, others, "进了话题群")
    if missing:
        mark_down(cfg, store, f"话题群（{chat}）里没有{'和'.join(missing)}")
        return "down"
    clear_down(cfg, store)
    return "ok"


def warn_strangers(cfg, store: Store, people: list[str], what: str) -> list[str]:
    """有别人进群 / 在群里说话：私聊提醒主人（每个人只提醒一次），那人说的话不分派。返回这次提醒了谁。"""
    try:
        seen = set(json.loads(store.get_meta(META_STRANGERS) or "[]"))
    except json.JSONDecodeError:
        seen = set()
    new = [p for p in people if p and p not in seen]
    if not new:
        return []
    store.set_meta(META_STRANGERS, json.dumps(sorted(seen | set(new))))
    names = "、".join(f"{store.person_name(p) or p}（{p}）" for p in new)
    notify_owner(cfg, store, f"🐕 汇报 · 话题群里有别人 [{eng.BUS_TOPIC_ID}]\n{names}{what}。话题群应该只有你和机器人；"
                             "他们说的话牧羊犬不分派。不是你拉的就把人移出去。")
    return new


# ======================= 转交（forward / 总线开专属会话） =======================
def on_forward(store: Store, topic_id: str, rows: list, cli: str = "sheepdog") -> list[int]:
    """转交的消息属于某件事项（话题群里那件事的开头那条、或在它的话题里）：负责会话改成目标会话，在话题里说一句「已交给 …」
    （常驻进程下一轮发），给目标会话一条说明：这件事的编号当钥匙用。返回改了哪些事项。"""
    done: list[int] = []
    for r in rows:
        item = store.item_by_root(r["message_id"]) or (store.item_by_thread(r["thread_id"]) if r["thread_id"] else None)
        if item is None or item["item_no"] in done or item["state"] == REMOVED:
            continue
        done.append(item["item_no"])
        if item["owner_topic"] == topic_id:
            continue
        store.update_item(item["item_no"], owner_topic=topic_id, handoff_to=topic_id, handoff_at=now_iso())
        mark_dirty(store)
        no = label(item["item_no"])
        keys = [k for k in item_keys(store, item["item_no"])]
        from .prompts import with_cli
        store.add_system_message(
            topic_id, "topic_group", with_cli(
            f"🗂 这件事在话题群里是事项 {no}「{item['title']}」，现在由你负责。找主人时用 `sheepdog ask / report --topic "
            f"{topic_id} --key {no} …`，消息会发进群里这个话题" + (f"（它也认钥匙 {'、'.join(keys)}）" if keys else "")
            + f"；你自己另有钥匙就用 `sheepdog track alias --topic {topic_id} --item {no} --key <你的钥匙>` 登记成别名。"
            f"办完用 `sheepdog track done --topic {topic_id} --key {no} --why \"<依据>\"`。", cli),
            [f"item:{no}"])
        log.info("事项 %s 转交给 %s", no, topic_id)
    return done


# ======================= 只读：给命令行看 =======================
def list_lines(cfg, store: Store, include_all: bool = False) -> list[str]:
    states = (ACTIVE, DONE, ARCHIVED, REMOVED) if include_all else (ACTIVE, DONE)
    out = []
    now = eng._now()
    for it in store.list_items(states):
        st = status(cfg, store, it, now) if it["state"] != REMOVED else REMOVED
        keys = item_keys(store, it["item_no"])
        out.append(f"{label(it['item_no'])} [{STATUS_LABEL.get(st, '已移出')}] {it['title']} · 负责 {it['owner_topic'] or '-'}"
                   + (f" · 钥匙 {'、'.join(keys)}" if keys else "") + f" · {ORIGIN_LABEL.get(it['origin'], it['origin'])}"
                   + (f" · {item_link(store, it)}" if item_link(store, it) else ""))
    return out
