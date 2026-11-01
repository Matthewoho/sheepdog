"""长期跟踪的话题放进专门的话题群（7.51 / 7.74）。"""

from __future__ import annotations

import hashlib, json, logging, re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import decision_hub, duty, engine as eng
from .card import build_owner_card, card_fits, owner_title, plain_text
from .decision_hub import (MULTI_ITEM_TOPICS, _LEGACY_MULTI_KEYS, _LEGACY_SHARED_KEYS, base_topic_id,
                           canonical_group_mode, find_canonical_session_item, heal_canonical_topics)
from .router import message_bodies
from .source.tat_cache import is_transient_lark_error
from .store import Store, now_iso

log = logging.getLogger("sheepdog")
ITEM_PREFIX = "T-"
KIND_TITLE, KIND_INDEX, KIND_CHANGE, KIND_HANDOFF, KIND_NOTE, KIND_MOVED = (
    "tg_title", "tg_index", "tg_change", "tg_handoff", "tg_note", "tg_moved")
OWN_KINDS = (KIND_TITLE, KIND_INDEX, KIND_CHANGE, KIND_HANDOFF, KIND_NOTE, KIND_MOVED)
CMD_MOVE, CMD_DONE, CMD_REMOVE = ("放群里",), ("办完了", "关掉"), ("移出群",)
ACTIVE, DONE, ARCHIVED, REMOVED = "active", "done", "archived", "removed"
ORIGIN_TRACK, ORIGIN_MOVE, ORIGIN_OWNER = "track", "owner_move", "owner_topic"
ORIGIN_LABEL = {ORIGIN_TRACK: "会话 --track", ORIGIN_MOVE: "主人「放群里」", ORIGIN_OWNER: "主人在群里新开"}
S_DECIDE, S_EXPIRED, S_DOING, S_WAITING, S_SLEEPING, S_DONE, S_ARCHIVED = (
    "decide", "expired", "doing", "waiting_other", "sleeping", "done", "archived")
STATUS_LABEL = {S_DECIDE: "等你定", S_EXPIRED: "过期未定", S_DOING: "进行中", S_WAITING: "等别人", S_SLEEPING: "沉睡",
                S_DONE: "最近办完", S_ARCHIVED: "已归档"}
INDEX_ORDER = (S_DECIDE, S_EXPIRED, S_DOING, S_WAITING, S_SLEEPING, S_DONE)
SUGGEST = "suggest"
REOPEN_WITHIN_DAYS, PURGE_AFTER_DAYS, INDEX_RENEW_DAYS, MEMBER_CHECK_MINUTES, INDEX_ROWS = 14, 30, 13, 10, 12
META_DOWN, META_INDEX, META_INDEX_PENDING = "tg_down", "tg_index", "tg_index_pending"
META_DIRTY, META_REFRESHED, META_MEMBERS_AT, META_STRANGERS = "tg_dirty", "tg_index_refreshed_at", "tg_members_at", "tg_strangers"
NOTICE_KIND, DEFAULT_LINK_HOST = "话题群通知", "https://applink.larksuite.com"


def track_norm(key: str) -> str: return re.sub(r"[\s_\-]+", "", key or "").upper()
def normalize_key(key: str) -> str: return re.sub(r"[\s_\-]+", "-", (key or "").strip().lower()).strip("-")
def _cfg_store(a, b=None): return (b, a) if isinstance(a, Store) else (a, b)

def _excerpt(text: str, limit: int) -> str:
    from .pending import excerpt
    return excerpt(text or "", limit)

def label(item_no: int) -> str: return f"{ITEM_PREFIX}{item_no}"

def parse_item_no(key: str) -> int | None:
    return int(m.group(1)) if (m := re.fullmatch(r"T(\d+)", track_norm(key))) else None

def whole(content: str) -> str:
    bodies = message_bodies(plain_text(content or ""))
    return "".join((bodies[-1] if bodies else "").split())

def is_command(content: str, words: tuple[str, ...]) -> bool: return whole(content) in words

def _parse(ts: str | None) -> datetime | None:
    try: dt = datetime.fromisoformat(ts) if ts else None
    except ValueError: return None
    return dt if dt is not None and dt.tzinfo is not None else None

def _hash(text: str) -> str: return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]

def idem(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"sd{kind[3] if kind.startswith('tg_') else kind[0]}-{digest}"

def configured(cfg) -> bool: return bool(cfg and getattr(cfg, "topic_group", None) and cfg.topic_group.chat_id)

def down_info(store: Store) -> dict | None:
    if not (raw := store.get_meta(META_DOWN)): return None
    try: return json.loads(raw)
    except json.JSONDecodeError: return {"since": "", "why": raw}

def is_down(store: Store) -> bool: return down_info(store) is not None
def usable(cfg, store: Store) -> bool: return bool(cfg and cfg.topic_group.active) and down_info(store) is None


def notify_owner(cfg, store: Store, text: str, kind: str = NOTICE_KIND) -> None:
    path = getattr(cfg, "notify_path", None) if cfg is not None else None
    try:
        if path is not None:
            from .notify import FileNotifier
            FileNotifier(path, cfg.timezone_offset).notify(text, kind); return
        store.add_owner_notice(kind, "", text.encode("utf-8"), eng.new_idempotency_key())
    except Exception as e:
        log.warning("话题群的系统通知没记上（%s）: %s", kind, e)


def mark_down(a, b=None, why: str = "") -> bool:
    cfg, store, why = (None, a, (b or why)) if isinstance(a, Store) else (a, b, why)
    if down_info(store) is not None: return False
    store.set_meta(META_DOWN, json.dumps({"since": now_iso(), "why": why}, ensure_ascii=False))
    log.warning("话题群不可用，退回私聊：%s", why)
    notify_owner(cfg, store, f"🐕 汇报 · 话题群不可用，已退回私聊 [{eng.BUS_TOPIC_ID}]\n{why}\n之后的提问、汇报照旧发进私聊；牧羊犬每 10 分钟核一次群成员，正常了自动恢复。")
    return True


def clear_down(a, b=None) -> bool:
    cfg, store = _cfg_store(a, b)
    if (info := down_info(store)) is None: return False
    store.set_meta(META_DOWN, ""); store.set_meta(META_DIRTY, "1")
    log.info("话题群恢复（之前：%s）", info.get("why", ""))
    notify_owner(cfg, store, f"🐕 汇报 · 话题群恢复了 [{eng.BUS_TOPIC_ID}]\n之前退回私聊的原因：{info.get('why', '')}。之后长期跟踪的事照旧发进话题群。")
    return True


def mark_dirty(store: Store) -> None: store.set_meta(META_DIRTY, "1")


def _ensure_extra_col(store: Store) -> None:
    if "extra" not in {r[1] for r in store.conn.execute("PRAGMA table_info(tracked_items)").fetchall()}:
        store.conn.execute("ALTER TABLE tracked_items ADD COLUMN extra TEXT DEFAULT '{}'"); store.conn.commit()

def _item_extra(store: Store, item) -> dict:
    _ensure_extra_col(store)
    raw = item["extra"] if "extra" in item.keys() else None
    if raw is None:
        r = store.conn.execute("SELECT extra FROM tracked_items WHERE item_no=?", (item["item_no"],)).fetchone()
        raw = r[0] if r else "{}"
    try: return data if isinstance(data := json.loads(raw or "{}"), dict) else {}
    except json.JSONDecodeError: return {}

def bind_alias_key(store: Store, item_no: int, key: str, topic_id: str = "") -> list[str]:
    _ensure_extra_col(store)
    clean, item = " ".join((key or "").split()), store.get_item(item_no)
    if item is None: return []
    extra = _item_extra(store, item)
    aliases, nk = list(extra.get("alias_keys") or []), track_norm(clean)
    if not nk or parse_item_no(clean) == item_no: return aliases
    store.add_item_key(item_no, clean, nk, "alias", topic_id or item["owner_topic"] or "")
    if nk not in {track_norm(x) for x in aliases}:
        aliases = (aliases + [normalize_key(clean) or clean])[-12:]
        extra["alias_keys"] = aliases
        store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(extra, ensure_ascii=False), item_no))
        store.conn.commit()
    return aliases

def find_by_key(store: Store, key: str, topic_id: str = "", *, allow_cross_session: bool = False):
    if not (nk := track_norm(key)): return None
    if (no := parse_item_no(key)) is not None and (row := store.get_item(no)) is not None: return row
    _ensure_extra_col(store)
    cands = list(store.items_by_norm_key(nk))
    seen = {r["item_no"] for r in cands}
    for row in store.conn.execute("SELECT * FROM tracked_items ORDER BY item_no DESC").fetchall():
        if row["item_no"] not in seen and any(track_norm(a) == nk for a in (_item_extra(store, row).get("alias_keys") or [])):
            cands.append(row); seen.add(row["item_no"])
    if not cands or not topic_id: return cands[0] if cands else None
    base = base_topic_id(topic_id)
    for r in cands:
        if base_topic_id(r["owner_topic"] or "") in (base, ""): return r
        if store.conn.execute("SELECT 1 FROM tracked_keys WHERE item_no=? AND norm_key=? AND (topic_id=? OR topic_id=? OR topic_id LIKE ?)",
                              (r["item_no"], nk, topic_id, base, f"{base}#%")).fetchone():
            return r
    return cands[0] if allow_cross_session else None

def item_keys(store: Store, item_no: int) -> list[str]:
    out = [r["key"] for r in store.item_keys(item_no) if r["kind"] != "number"]
    seen = {track_norm(k) for k in out}
    if (it := store.get_item(item_no)) is not None:
        for a in _item_extra(store, it).get("alias_keys") or []:
            if track_norm(a) not in seen:
                out.append(a); seen.add(track_norm(a))
    return out

def open_item(store: Store, title: str, owner_topic: str, opened_by: str, origin: str, chat_id: str, *,
              key: str = "", topic_id: str = "", dm_root: str = "", related: int | None = None,
              root_message_id: str = "", thread_id: str = ""):
    no = store.add_item(title=_excerpt(title, 60) or "（无标题）", owner_topic=owner_topic, opened_by=opened_by,
                        origin=origin, chat_id=chat_id, dm_root_message_id=dm_root, related_item=related,
                        root_message_id=root_message_id, thread_id=thread_id)
    store.add_item_key(no, label(no), track_norm(label(no)), "number", "")
    if key: bind_alias_key(store, no, key, topic_id or owner_topic)
    mark_dirty(store)
    log.info("开了事项 %s「%s」（%s，负责 %s）", label(no), title, ORIGIN_LABEL.get(origin, origin), owner_topic or "-")
    return store.get_item(no)

def touch(store: Store, item_no: int) -> None: store.update_item(item_no, last_activity_at=now_iso())

def reopen(store: Store, item) -> bool:
    if item["state"] not in (DONE, ARCHIVED): return False
    store.update_item(item["item_no"], state=ACTIVE, reopened_at=now_iso(), archived_at=None)
    mark_dirty(store); log.info("事项 %s 重开", label(item["item_no"])); return True

def close(store: Store, item, by: str, why: str) -> bool:
    if item["state"] not in (ACTIVE,): return False
    store.update_item(item["item_no"], state=DONE, done_at=now_iso(), done_by=by, done_why=why, last_activity_at=now_iso())
    mark_dirty(store); log.info("事项 %s 办完（%s）：%s", label(item["item_no"]), by, why); return True

def remove(store: Store, item, by: str) -> bool:
    if item["state"] == REMOVED: return False
    store.update_item(item["item_no"], state=REMOVED, removed_at=now_iso(), done_by=by)
    mark_dirty(store); return True

def session_title(store: Store, topic_id: str) -> str:
    if not topic_id: return "-"
    t = store.get_topic(topic_id)
    if topic_id == eng.BUS_TOPIC_ID:
        return (t["title"] or "总线").strip() if t is not None else "总线"
    return ((t["title"] or "").strip() or topic_id) if t is not None else topic_id

def deliverable(store: Store, topic_id: str) -> bool:
    t = store.get_topic(topic_id) if topic_id else None
    return t is not None and t["state"] != "closed" and (topic_id == eng.BUS_TOPIC_ID or t["kind"] in eng.MANAGED_KINDS)

def owner_of(store: Store, item) -> str:
    return tid if deliverable(store, tid := item["owner_topic"] or "") else eng.BUS_TOPIC_ID

def _link_host(store: Store) -> str:
    if known := store.get_meta("tg_link_host"): return known
    r = store.conn.execute("SELECT link FROM messages WHERE link LIKE 'https://%/client/%' LIMIT 1").fetchone()
    if (m := re.match(r"^(https://[^/]+)/client/", r[0]) if r is not None else None) is None: return DEFAULT_LINK_HOST
    store.set_meta("tg_link_host", m.group(1)); return m.group(1)

def chat_link(store: Store, chat_id: str) -> str:
    return f"{_link_host(store)}/client/chat/open?openChatId={chat_id}" if chat_id else ""


# ======================= 进群：因果路由与发送这一侧（REG-020 ~ REG-022） =======================
@dataclass
class SendRoute:
    """ask / report 去哪：group = 进群里的事项话题（item 为空表示要新开）；否则私聊。"""
    group: bool = False
    item: object = None
    item_no: int | None = None
    root_message_id: str = ""
    related: int | None = None
    note: str = ""


def _msg_to_item(store: Store, m):
    if m is None:
        return None
    for tag in json.loads(m["tags_json"] or "[]"):
        if tag.startswith("item:") and (no := parse_item_no(tag.split(":", 1)[1])) is not None:
            if (it := store.get_item(no)) is not None and it["state"] != REMOVED:
                return it
    rkeys = set(m.keys())
    for cid in ([m["root_id"]] if "root_id" in rkeys and m["root_id"] else []) + [m["reply_to"], m["message_id"]]:
        if not cid:
            continue
        it = store.item_by_root(cid)
        if it is None and (s := store.send_by_message(cid)) is not None and s["root_message_id"]:
            it = store.item_by_root(s["root_message_id"])
        if it is not None and it["state"] != REMOVED:
            return it
    if m["thread_id"]:
        if (it := store.item_by_thread(m["thread_id"])) is not None and it["state"] != REMOVED:
            return it
        for r in store.thread_messages_in_ledger(m["thread_id"]):
            it = store.item_by_root(r["message_id"]) or (
                store.item_by_root(s["root_message_id"]) if (s := store.send_by_message(r["message_id"])) and s["root_message_id"] else None)
            if it is not None and it["state"] != REMOVED:
                return it
    return None


def resolve_inbound_item(a, b, topic_id: str = "", batch_id: str = ""):
    """按入站消息来源因果反查话题群事项（REG-020）：当前批次（含 p...）→ 未结清 owner_echoes → 30 分钟内入站话题群消息。"""
    from .outbound import current_batch
    cfg, store = _cfg_store(a, b)
    if not configured(cfg) or not cfg.topic_group.enabled or not topic_id:
        return None
    tg_chat = cfg.topic_group.chat_id
    base = topic_id.split("#", 1)[0]
    tids = list(dict.fromkeys([topic_id, base, *(t["topic_id"] for t in store.list_topics() if t["topic_id"].split("#", 1)[0] == base)]))
    marks = ",".join("?" * len(tids))
    bids: list[str] = []
    for cand in (batch_id, current_batch(store, topic_id, include_priority=True),
                 *(t["pending_batch_id"] for tid in tids if (t := store.get_topic(tid)) and t["pending_batch_id"])):
        if cand and cand not in bids:
            bids.append(cand)
    for (bid,) in store.conn.execute(
            f"SELECT batch_id FROM dispatches WHERE topic_id IN ({marks}) AND state IN ('sending','sent','priority') "
            "ORDER BY rowid DESC LIMIT 6", tuple(tids)):
        if bid and bid not in bids:
            bids.append(bid)
    for bid in bids:
        rows = list(store.conn.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY rowid DESC", (bid,)).fetchall())
        if not rows and (d := store.conn.execute("SELECT message_ids FROM dispatches WHERE batch_id=?", (bid,)).fetchone()) is not None:
            rows = [r for mid in json.loads(d[0] or "[]") if (r := store.get_message(mid)) is not None]
        for m in rows:
            if (m["chat_id"] == tg_chat or '"item:T-' in (m["tags_json"] or "")) and (it := _msg_to_item(store, m)) is not None:
                return it
    for (emid,) in store.conn.execute(
            f"SELECT message_id FROM owner_echoes WHERE chat_id=? AND topic_id IN ({marks}) "
            "AND settled_at IS NULL AND stage != 'settled' ORDER BY rowid DESC", (tg_chat, *tids)):
        if (it := _msg_to_item(store, store.get_message(emid)) or store.item_by_root(emid)) is not None and it["state"] != REMOVED:
            return it
    cutoff = (eng._now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    for m in store.conn.execute(
            f"SELECT * FROM messages WHERE chat_id=? AND topic_id IN ({marks}) AND route!='drop' "
            "AND COALESCE(create_time, first_seen, '') >= ? ORDER BY COALESCE(create_time, first_seen) DESC, rowid DESC",
            (tg_chat, *tids, cutoff)).fetchall():
        if (it := _msg_to_item(store, m)) is not None:
            return it
    return None


def route_for_send(a, b, topic_id: str, c="", d=False, e=False, *, kind: str = "", title: str = "", batch_id: str = "") -> SendRoute:
    """进群路由（REG-020 / REG-021 / REG-038 / REG-039）：支持 (cfg, store, topic_id, key, track) 与 (store, cfg, topic_id, kind, key, track, title, batch_id)。"""
    cfg, store = _cfg_store(a, b)
    if isinstance(a, Store) and isinstance(d, str) and c in ("ask", "report", ""): kind, key, track = c, d, bool(e)
    else: key, track = (c or ""), bool(d)
    key = " ".join((key or "").split())
    if not configured(cfg) or not cfg.topic_group.enabled:
        return SendRoute(note="没配话题群（[topic_group]），--track 不生效，照旧发私聊" if track else "")
    canon_mode, multi_topic = canonical_group_mode(cfg, store), base_topic_id(topic_id) in MULTI_ITEM_TOPICS
    allow_cross = not canon_mode and not track and track_norm(key) in _LEGACY_SHARED_KEYS
    item, related = (find_by_key(store, key, topic_id=topic_id, allow_cross_session=allow_cross) if key else None), None
    if item is not None and item["state"] == REMOVED:
        return SendRoute(note=f"这件事（{label(item['item_no'])}）主人已经移出话题群，照旧发私聊" if track else "")
    if item is not None and item["state"] == ARCHIVED:
        if (at := _parse(item["archived_at"])) is not None and eng._now() - at > timedelta(days=REOPEN_WITHIN_DAYS):
            related, item = item["item_no"], None
    if item is None and (inbound := resolve_inbound_item(store, cfg, topic_id, batch_id)) is not None:
        nk = track_norm(key)
        sem_keys = [track_norm(k) for k in item_keys(store, inbound["item_no"]) if track_norm(k) and parse_item_no(k) is None and not re.fullmatch(r"TG\d+", track_norm(k))]
        if track and (not canon_mode or multi_topic) and nk and sem_keys and nk not in sem_keys and not any(nk in sk or sk in nk for sk in sem_keys):
            related = inbound["item_no"]
        else:
            item = inbound
    if item is None and not multi_topic:
        canon = find_canonical_session_item(store, topic_id)
        if canon is not None and (canon_mode or (not track and track_norm(key) not in _LEGACY_MULTI_KEYS)):
            item = canon
        elif canon is None and canon_mode:
            if down_info(store) is not None:
                return SendRoute(note="话题群暂时不可用（已退回私聊），这条发进了私聊")
            return SendRoute(group=True, related=related)
    if item is not None:
        if key: bind_alias_key(store, item["item_no"], key, topic_id)
        if item["state"] in (DONE, ARCHIVED):
            reopen(store, item); item = store.get_item(item["item_no"])
        if down_info(store) is not None and not item["root_message_id"]:
            return SendRoute(note="话题群暂时不可用（已退回私聊），这条发进了私聊")
        return SendRoute(group=True, item=item, item_no=item["item_no"], root_message_id=item["root_message_id"] or "")
    if not key or not track:
        return SendRoute()
    if down_info(store) is not None:
        return SendRoute(note="话题群暂时不可用（已退回私聊），这条发进了私聊")
    if store.thread_by_key(topic_id, duty.norm_key(key)) is not None:
        return SendRoute(note="这把钥匙在私聊里已经开过话题，--track 只在第一次用这把钥匙时有效，这条照旧发进私聊的那个话题；"
                              "要放进群请主人在那个话题里回「放群里」")
    return SendRoute(group=True, related=related)


TITLE_FOOTER = "在这里回复就是回给负责的会话；整句回「办完了」表示办完，「移出群」退回私聊"
INDEX_TITLE = "📌 话题群索引"
INDEX_EMPTY = "现在没有在跟的事"
INDEX_FOOTER = "在话题里回复就是回给负责的会话；整句回「办完了」表示办完，「移出群」退回私聊"
SUGGEST_HOW = "在私聊那件事的话题里整句回「放群里」（在私聊里搜钥匙就能找到那个话题）"


def _grey(text: str) -> str:
    return f"<font color='grey'>{text}</font>"

def _name(cfg, store: Store, topic_id: str) -> str:
    from .pending import session_name
    return session_name(cfg, store, topic_id) if (cfg is not None and topic_id) else (topic_id or "-")

def opener_phrase(cfg, store: Store, item) -> str:
    if item["origin"] == ORIGIN_MOVE:
        return "你从私聊放进群"
    if item["origin"] == ORIGIN_OWNER or item["opened_by"] == "owner":
        return "你开的"
    return f"{_name(cfg, store, item['opened_by'] or item['owner_topic'] or '')}开的"

def title_view(cfg, store: Store, item, opener: str = "", key: str = "") -> dict:
    no, opened = label(item["item_no"]), _parse(item["opened_at"])
    when = opened.astimezone(eng._tz(cfg.timezone_offset if cfg else 8)[0]).strftime("%m-%d %H:%M") if opened else ""
    keys = item_keys(store, item["item_no"])
    k0 = key or (keys[0] if keys else "")
    sub = [f"钥匙 {k0}" if k0 else "", opener or opener_phrase(cfg, store, item), when]
    related = f"可能相关：{label(item['related_item'])}，那件已经归档" if item["related_item"] else ""
    return {"title": f"🗂 {no} · {item['title']}", "subtitle": " · ".join(x for x in sub if x), "related": related,
            "keys": keys, "footer": TITLE_FOOTER}

def title_text(v: dict) -> str:
    return "\n".join(x for x in (v["title"], v["subtitle"], v["related"], v["footer"]) if x)

def title_card(v: dict, *, template: str = "blue", status_line: str = "", keys_line: str = "") -> dict:
    els = ([{"tag": "markdown", "content": status_line}] if status_line else []) + (
        [{"tag": "markdown", "content": keys_line}] if keys_line else []) + (
        [{"tag": "markdown", "content": v["related"]}] if v["related"] else []) + [
        {"tag": "markdown", "content": _grey(v["footer"]), "text_size": "notation"}]
    return {"schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": owner_title(v["title"])},
                       "subtitle": {"tag": "plain_text", "content": v["subtitle"]}, "template": template},
            "body": {"elements": els}}

def sync_title_card(a, b, source, item_no: int, latest_text: str = "", kind: str = "") -> bool:
    """REG-022：实时更新话题群首卡（动态状态灯 + 1 行最新进展摘要 + 全部主/别名钥匙列表）。失败仅记 warning，绝不熔断。"""
    cfg, store = _cfg_store(a, b)
    item = store.get_item(item_no) if store is not None else None
    if item is None or not item["root_message_id"] or source is None or not hasattr(source, "patch_card"):
        return False
    wait_owner = any(w["state"] == duty.WAITING_OWNER
                     for k in ({duty.norm_key(x) for x in item_keys(store, item_no)} | {duty.norm_key(label(item_no))})
                     for w in store.open_work_by_key(k))
    if item["state"] in (DONE, ARCHIVED) or kind == "done":
        tpl, badge = "green", "✅ 已结案"
    elif kind == "ask" or (cfg is not None and open_asks(cfg, store, item)) or wait_owner:
        tpl, badge = "orange", "⏳ 等你定"
    else:
        tpl, badge = "blue", "🔄 跟进中"
    raw = (latest_text or "").strip()
    if not raw:
        sends = [s for s in store.sends_in_thread(item["root_message_id"]) if s["kind"] in ("ask", "report")]
        raw = sends[-1]["text"] if sends else (item["done_why"] or "")
    lines = [ln.strip() for ln in plain_text(raw).splitlines() if ln.strip()]
    body_text = "\n".join(lines[1:]) if len(lines) > 1 and lines[0].startswith(("🐕", "❓", "📋")) else "\n".join(lines)
    summary_1, v = _excerpt(body_text, 120), title_view(cfg, store, item)
    v["subtitle"] = " · ".join(x for x in (badge, v["subtitle"]) if x)
    keys = v.get("keys") or [label(item_no)]
    card = title_card(v, template=tpl, status_line=f"**{badge}**" + (f" · {summary_1}" if summary_1 else ""),
                      keys_line=f"🔎 搜索钥匙：{' · '.join(keys)}")
    try:
        source.patch_card(item["root_message_id"], card)
        return True
    except Exception as e:
        log.warning("更新事项 %s 标题卡失败（不触发群熔断）: %s", label(item_no), e)
        return False

def move_from_dm(a, b, source, key: str, *, topic_id: str = "", title: str = ""):
    """REG-022：把私聊长线事项一键迁移到话题群（幂等：已在群里直接返回原 T-n）。"""
    cfg, store = _cfg_store(a, b)
    if not (clean_key := " ".join((key or "").split())):
        raise ValueError("--key 不能为空")
    if not configured(cfg) or not cfg.topic_group.enabled:
        raise ValueError("没配话题群（[topic_group]）或未开启")
    existing = find_by_key(store, clean_key)
    if existing is not None and existing["state"] in (ACTIVE, DONE) and existing["root_message_id"]:
        return existing
    nk = duty.norm_key(clean_key)
    thr = (store.thread_by_key(topic_id, nk) if topic_id else None) or store.conn.execute(
        "SELECT * FROM owner_threads WHERE norm_key=? ORDER BY last_post_at DESC LIMIT 1", (nk,)).fetchone()
    owner_tid, dm_root = topic_id or (thr["topic_id"] if thr else "") or eng.BUS_TOPIC_ID, (thr["root_message_id"] if thr else "") or ""
    raw_title = title or (thr["title"] if thr else "") or clean_key
    clean_title = re.sub(r"^🐕\s*(需要你定|汇报)\s*·\s*", "", raw_title).strip() or clean_key
    item = existing if (existing is not None and existing["state"] != REMOVED) else open_item(
        store, clean_title, owner_tid, owner_tid, ORIGIN_MOVE, cfg.topic_group.chat_id,
        key=clean_key, topic_id=owner_tid, dm_root=dm_root)
    reopen(store, item)
    if source is not None and hasattr(source, "send_to_chat"):
        item = ensure_title(cfg, store, source, item, "", clean_key)
        sync_title_card(store, cfg, source, item["item_no"], latest_text=clean_title, kind="report")
        if dm_root and hasattr(source, "reply_to"):
            no, link = label(item["item_no"]), chat_link(store, cfg.topic_group.chat_id)
            note = f"📌 这件事已经放进话题群：{no}" + (f"（[打开话题群]({link})，搜 {no}）" if link else "") + "。之后同一把钥匙的消息都发进群里那个话题。"
            try:
                post_in_thread(cfg, store, source, dm_root, KIND_MOVED, note, idem(KIND_MOVED, dm_root, no))
            except Exception as e:
                log.warning("私聊原话题回迁群提示失败 %s: %s", dm_root, e)
    return store.get_item(item["item_no"])


def post_top(cfg, store: Store, source, kind: str, text: str, ik: str, topic_id: str = "", card: dict | None = None,
             force_card: bool = False) -> dict:
    from .outbound import owner_content, send_owner
    if card is not None:
        plain, c = text, (card if force_card or (cfg.notify.owner_card and card_fits(card)) else None)
    else:
        plain, c = owner_content(cfg, text)
        c = build_owner_card(text) if force_card else c
    chat = cfg.topic_group.chat_id
    res = send_owner(lambda t, cc: source.send_to_chat(chat, t, ik, "bot", **({"card": cc} if cc is not None else {})), plain, c)
    store.add_send(ik, kind, topic_id, res["message_id"], text, root_message_id=res["message_id"])
    return res


def post_in_thread(cfg, store: Store, source, root: str, kind: str, text: str, ik: str, topic_id: str = "",
                   at: bool = False) -> dict:
    from .outbound import owner_content, send_owner
    if (prev := store.get_send(ik)) is not None:
        return {"message_id": prev["message_id"], "already": True}
    plain, c = owner_content(cfg, text, at=at)
    res = send_owner(lambda t, cc: source.reply_to(root, identity="bot", idem_key=ik, text=t, card=cc, in_thread=True), plain, c)
    store.add_send(ik, kind, topic_id, res["message_id"], text, root_message_id=root)
    return res


def ensure_title(cfg, store: Store, source, item, opener: str = "", key: str = ""):
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
    out, seen, n = [], set(), 0
    for root in (item["dm_root_message_id"], item["root_message_id"]):
        if not root:
            continue
        mids = [s["message_id"] for s in store.sends_in_thread(root) if s["kind"] == "ask"]
        if root == item["dm_root_message_id"] and root not in mids:
            mids.insert(0, root)
        for mid in mids:
            if mid not in seen:
                seen.add(mid)
                if (e := store.get_escalation(mid)) is not None:
                    out.append((e["asked_at"] or "", n, e))
                n += 1
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e for _, _, e in out]


def answered_in_flight(store: Store) -> set[str]:
    out, prefix = set(), eng.ESCALATION_TAG_PREFIX
    for (tags,) in store.conn.execute(
            "SELECT tags_json FROM messages WHERE route='dispatch' AND COALESCE(dispatch_state,'pending') "
            "IN ('pending','sending','uncertain') AND tags_json LIKE ?", (f'%"{prefix}%',)):
        out.update(t[len(prefix):] for t in json.loads(tags or "[]") if t.startswith(prefix))
    return out

def open_asks(cfg, store: Store, item) -> list:
    cutoff, flying = eng._now() - timedelta(hours=cfg.escalation.open_hours), answered_in_flight(store)
    return [e for e in item_asks(store, item) if not e["closed_at"] and e["message_id"] not in flying
            and ((a := _parse(e["asked_at"])) is None or a >= cutoff)]

def waiting_other(store: Store, item) -> bool:
    keys = {duty.norm_key(k) for k in item_keys(store, item["item_no"])} | {duty.norm_key(label(item["item_no"]))}
    return any(w["state"] == duty.WAITING_OTHER for k in keys for w in store.open_work_by_key(k))

def status(cfg, store: Store, item, now: datetime | None = None) -> str:
    now = now or eng._now()
    if item["state"] in (ARCHIVED, DONE):
        return S_ARCHIVED if item["state"] == ARCHIVED else S_DONE
    if open_asks(cfg, store, item):
        return S_DECIDE
    if (asks := item_asks(store, item)) and asks[0]["close_reason"] == "expired":
        return S_EXPIRED
    if waiting_other(store, item):
        return S_WAITING
    last = _parse(item["last_activity_at"] or item["opened_at"])
    return S_SLEEPING if (last is not None and now - last >= timedelta(days=cfg.topic_group.stale_after_days)) else S_DOING


# ======================= 建议放进群（私聊里长期在跟的） =======================
def suggestions(cfg, store: Store, now: datetime | None = None, owner_chats: list[str] | None = None) -> list[dict]:
    g, now, me = cfg.topic_group, now or eng._now(), cfg.self_open_id
    active_from = now - timedelta(hours=g.suggest_active_hours)
    chats = owner_chats if owner_chats is not None else cfg.owner_chat_ids
    rows = store.conn.execute("SELECT * FROM owner_threads WHERE norm_key IS NOT NULL AND key IS NOT NULL "
                              "AND created_at <= ?", (now.isoformat(timespec="seconds"),)).fetchall()
    moved = {r[0] for r in store.conn.execute(
        "SELECT dm_root_message_id FROM tracked_items WHERE dm_root_message_id IS NOT NULL AND state != 'removed'")}
    out: dict[str, dict] = {}
    for r in rows:
        nk = track_norm(r["key"])
        if r["root_message_id"] in moved or ((it := find_by_key(store, r["key"])) is not None and it["state"] in (ACTIVE, DONE)):
            continue
        created, last, replies = _parse(r["created_at"]), _parse(r["last_post_at"] or r["created_at"]), []
        if me and r["thread_id"] and chats:
            marks = ",".join("?" * len(chats))
            replies = store.conn.execute(
                f"SELECT create_time FROM messages WHERE thread_id=? AND sender_id=? AND chat_id IN ({marks}) "
                "AND create_time <= ?", (r["thread_id"], me, *chats, now.isoformat(timespec="seconds"))).fetchall()
            for x in replies:
                if (t := _parse(x["create_time"])) is not None and (last is None or t > last):
                    last = t
        if last is None or last < active_from:
            continue
        span = (last - created).total_seconds() / 3600 if created else 0
        why = ([f"拖了 {span:.0f} 小时"] if span >= g.suggest_after_hours else []) + (
            [f"你回过 {len(replies)} 次"] if len(replies) >= g.suggest_owner_replies else [])
        if not why:
            continue
        cand = {"key": r["key"], "norm_key": nk, "title": r["title"] or r["key"], "topic_id": r["topic_id"],
                "root_message_id": r["root_message_id"], "thread_id": r["thread_id"] or "", "why": why,
                "last_at": last.isoformat(timespec="seconds") if last else "", "span_hours": round(span, 1),
                "owner_replies": len(replies)}
        if (cur := out.get(nk)) is None or cand["last_at"] > cur["last_at"]:
            out[nk] = cand
    return sorted(out.values(), key=lambda x: x["last_at"], reverse=True)


# ======================= 索引卡 =======================
def build_index(cfg, store: Store, now: datetime | None = None) -> dict:
    from .pending import _hm, waited
    now, off = now or eng._now(), cfg.timezone_offset
    sections: dict[str, list[dict]] = {k: [] for k in INDEX_ORDER}
    per_session: dict[str, int] = {}

    def ago(dt) -> str:
        if dt is None:
            return ""
        m = max(0, int((now - dt).total_seconds() // 60))
        return f"{m // 1440} 天" if m >= 48 * 60 else waited(m)

    for it in store.list_items((ACTIVE, DONE)):
        if (st := status(cfg, store, it, now)) not in sections:
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
            when = f"{_hm(item_asks(store, it)[0]['asked_at'], now, off)} 问 · 已过期没答"
        elif st == S_DONE:
            when = f"{_hm(it['done_at'] or '', now, off)} 办完"
        else:
            when = f"最近动静 {ago(last)}前" if last else ""
        sections[st].append({"item_no": it["item_no"], "label": label(it["item_no"]), "title": it["title"],
                             "owner": _name(cfg, store, owner) if owner else "-",
                             "when": when, "last": it["last_activity_at"] or it["opened_at"] or ""})
    for k in sections:
        sections[k].sort(key=lambda x: x["last"], reverse=True)
    sugg = suggestions(cfg, store, now)
    for x in sugg:
        x["name"] = _name(cfg, store, x["topic_id"])
        root = store.get_message(x["root_message_id"])
        x["link"] = chat_link(store, (root["chat_id"] if root is not None else "") or (cfg.owner_chat_ids[0] if cfg.owner_chat_ids else ""))
        x["ago"] = ago(_parse(x["last_at"]))
    return {"generated_at": now.isoformat(timespec="seconds"), "sections": sections, "suggest": sugg,
            "per_session": {_name(cfg, store, k): v for k, v in sorted(per_session.items(), key=lambda kv: -kv[1])}}


def index_view(cfg, data: dict, rows: int = INDEX_ROWS) -> dict:
    from .pending import _hm
    now = _parse(data["generated_at"]) or eng._now()
    n_total = sum(len(v) for v in data["sections"].values())
    n_decide = len(data["sections"][S_DECIDE])
    secs = []
    for st in INDEX_ORDER:
        if not (items := data["sections"][st]):
            continue
        shown = [{"head": f"{x['label']} · {_excerpt(x['title'], 40)}", "link": "",
                  "meta": " · ".join(y for y in (x["owner"], x["when"]) if y)} for x in items[:rows]]
        secs.append({"label": f"{STATUS_LABEL[st]} · {len(items)} 件", "items": shown, "more": len(items) - len(shown)})
    sugg, cap = data["suggest"], min(rows, cfg.topic_group.suggest_max)
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
    lines = [f"{INDEX_TITLE} · {v['subtitle']}"] + (["", INDEX_EMPTY] if not v["sections"] else [])
    for sec in v["sections"]:
        lines += ["", sec["label"]]
        for x in sec["items"]:
            lines += [f"- {x['head']}" + (f"  {x['link']}" if x["link"] else ""), f"  {x['meta']}"]
        if sec["more"]: lines.append(f"- 另有 {sec['more']} 件")
    if v["suggest"]:
        lines += ["", f"建议放进群 · {v['n_suggest']} 件", SUGGEST_HOW]
        for x in v["suggest"]:
            lines += [f"- {x['head']}" + (f"  {x['link']}" if x["link"] else ""), f"  {x['meta']}"]
        if v["more_suggest"]: lines.append(f"- 另有 {v['more_suggest']} 件")
    if v["sections"]:
        lines += [""] + [x for x in (_per_session_line(v), INDEX_FOOTER) if x]
    return "\n".join(lines)

def index_card(v: dict) -> dict:
    els: list[dict] = []
    def rows_of(items: list[dict], more: int) -> None:
        for i, x in enumerate(items):
            if i: els.append({"tag": "hr"})
            head = f"**{x['head']}**" + (f" [打开]({x['link']})" if x["link"] else "")
            els.append({"tag": "markdown", "content": f"{head}\n{_grey(x['meta'])}"})
        if more: els.append({"tag": "markdown", "content": _grey(f"另有 {more} 件")})
    if not v["sections"]: els.append({"tag": "markdown", "content": INDEX_EMPTY})
    for n, sec in enumerate(v["sections"]):
        if n: els.append({"tag": "hr"})
        els.append({"tag": "markdown", "content": f"**{sec['label']}**"}); rows_of(sec["items"], sec["more"])
    if v["suggest"]:
        els += [{"tag": "hr"}, {"tag": "markdown", "content": f"**建议放进群 · {v['n_suggest']} 件**\n{_grey(SUGGEST_HOW)}"}]
        rows_of(v["suggest"], v["more_suggest"])
    if v["sections"]:
        tail = [x for x in (_per_session_line(v), INDEX_FOOTER) if x]
        els.append({"tag": "markdown", "content": "\n".join(_grey(x) for x in tail), "text_size": "notation"})
    return {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": INDEX_TITLE}, "subtitle": {"tag": "plain_text", "content": v["subtitle"]},
                                        "template": "orange" if v["n_decide"] > 0 else "blue"}, "body": {"elements": els}}

def render_index(cfg, data: dict, rows: int = INDEX_ROWS) -> str: return index_text(index_view(cfg, data, rows))


# ======================= 常驻进程每轮（Dispatcher） =======================
def dispatcher_round(disp) -> dict:
    """群成员核对、归档、「已交给 …」、话题里的变化说明、索引卡对账。索引卡异常不触发群熔断（REG-021）。"""
    cfg, store, out = disp.cfg, disp.store, {}
    if not configured(cfg) or not cfg.topic_group.enabled or (source := getattr(disp, "source", None)) is None: return out
    from .source import SourceError
    from .source.lark import LarkTimeout
    try: out["members"] = check_members(cfg, store, source)
    except Exception as e: log.warning("话题群成员核对出错: %s", e)
    if down_info(store) is not None: return out
    now = eng._now(); archive_due(cfg, store, now)
    try:
        for it in store.list_items((ACTIVE,)):
            if not it["root_message_id"] and it["origin"] != ORIGIN_OWNER:
                keys = item_keys(store, it["item_no"])
                ensure_title(cfg, store, source, it, "", keys[0] if keys else "")
        flush_pending_signposts(cfg, store, source)
        for it in store.list_items((ACTIVE, DONE, ARCHIVED, REMOVED)):
            if it["handoff_to"] and it["root_message_id"]: post_handoff(cfg, store, source, it)
            if it["state"] in (ACTIVE, DONE, ARCHIVED) and it["root_message_id"]: post_change(cfg, store, source, it, now)
        from . import decision_hub
        if decision_hub.hub_active(cfg, store): decision_hub.nudge_open_escalations(cfg, store, source, now=now)
    except LarkTimeout as e: log.warning("话题群发送超时，下一轮重试（同一个防重编号）: %s", e)
    except SourceError as e:
        if is_transient_lark_error(e):
            log.warning("topic group transient error (skipping mark_down): %s", e); return out
        mark_down(cfg, store, f"往话题群发消息失败：{' '.join(str(e).split())[:200]}"); return out
    if index_due(cfg, store, now) and (res := refresh_index(cfg, store, source, now)): out["index"] = res
    return out

def archive_due(cfg, store: Store, now: datetime) -> int:
    n, hours = 0, cfg.topic_group.archive_after_hours
    for it in store.list_items((DONE,)):
        last = max(filter(None, (_parse(it["done_at"]), _parse(it["last_activity_at"]))), default=None)
        if last is not None and now - last >= timedelta(hours=hours):
            store.update_item(it["item_no"], state=ARCHIVED, archived_at=now.isoformat(timespec="seconds"), status=S_ARCHIVED)
            mark_dirty(store); n += 1
    return n

def post_handoff(cfg, store: Store, source, item) -> None:
    to = item["handoff_to"]
    post_in_thread(cfg, store, source, item["root_message_id"], KIND_HANDOFF, f"➡️ 已交给「{_name(cfg, store, to)}」",
                   idem(KIND_HANDOFF, str(item["item_no"]), to, item["handoff_at"] or ""))
    store.update_item(item["item_no"], handoff_to=None)

def change_lines(cfg, store: Store, item, st: str) -> list[tuple[str, str]]:
    from . import decision_hub
    prev, out, hub_on = item["status"] or "", [], decision_hub.hub_active(cfg, store)
    if prev in (S_DONE, S_ARCHIVED) and st not in (S_DONE, S_ARCHIVED): out.append(("🔁 重开", "reopen:" + (item["reopened_at"] or "")))
    if not hub_on and st == S_DECIDE and prev != S_DECIDE and (e := open_asks(cfg, store, item)):
        from .pending import ask_title
        what = _excerpt(ask_title(cfg, e[0]["text"]), 40)
        out.append(("🟠 等你定" + (f"：{what}" if what else ""), "decide:" + e[0]["message_id"]))
    if st == S_DONE and prev not in (S_DONE, S_ARCHIVED):
        has_rep = bool(hub_on and item["root_message_id"] and store.conn.execute("SELECT 1 FROM sends WHERE root_message_id=? AND kind='report' LIMIT 1", (item["root_message_id"],)).fetchone())
        if not has_rep:
            who = "你" if item["done_by"] == "owner" else (_name(cfg, store, item["done_by"]) if item["done_by"] else "")
            why = item["done_why"] or ""
            out.append(("✅ 办完了" + (f"：{why}" if why else "") + (f"（{who}）" if who else ""), "done:" + (item["done_at"] or "")))
    return out

def post_change(cfg, store: Store, source, item, now: datetime) -> None:
    if (st := status(cfg, store, item, now)) == (item["status"] or ""): return
    for text, seed in change_lines(cfg, store, item, st):
        post_in_thread(cfg, store, source, item["root_message_id"], KIND_CHANGE, text, idem(KIND_CHANGE, str(item["item_no"]), seed))
    store.update_item(item["item_no"], status=st); mark_dirty(store)


def index_due(cfg, store: Store, now: datetime) -> bool:
    if store.get_meta(META_DIRTY) == "1": return True
    return (last := _parse(store.get_meta(META_REFRESHED))) is None or now - last >= timedelta(minutes=cfg.topic_group.index_refresh_minutes)

def index_info(store: Store) -> dict:
    try: return json.loads(raw) if (raw := store.get_meta(META_INDEX)) else {}
    except json.JSONDecodeError: return {}

def refresh_index(cfg, store: Store, source, now: datetime | None = None) -> str:
    """对账索引卡（REG-021）：patch_card 或新建失败只记 warning 返回空串，绝不调用 mark_down。"""
    now = now or eng._now()
    data, rows = build_index(cfg, store, now), INDEX_ROWS
    v = index_view(cfg, data, rows); text, card = index_text(v), index_card(v)
    while not card_fits(card) and rows > 1:
        rows = max(1, rows // 2); v = index_view(cfg, data, rows); text, card = index_text(v), index_card(v)
    info = index_info(store); h, sent = _hash(_strip_time(text)), _parse(info.get("sent_at"))
    def done(what: str) -> str:
        store.set_meta(META_DIRTY, ""); store.set_meta(META_REFRESHED, now.isoformat(timespec="seconds")); return what
    if info.get("message_id") and sent is not None and now - sent < timedelta(days=INDEX_RENEW_DAYS):
        if info.get("hash") == h: return done("same")
        try: source.patch_card(info["message_id"], card)
        except Exception as e:
            log.warning("索引卡 patch_card 失败（%s，不熔断话题群）: %s", info["message_id"], e); return ""
        info["hash"] = h; store.set_meta(META_INDEX, json.dumps(info, ensure_ascii=False)); return done("patched")
    ik = store.get_meta(META_INDEX_PENDING) or idem(KIND_INDEX, cfg.topic_group.chat_id, info.get("message_id") or "", now.isoformat(timespec="minutes"))
    store.set_meta(META_INDEX_PENDING, ik)
    try: res = post_top(cfg, store, source, KIND_INDEX, text, ik, card=card, force_card=True)
    except Exception as e:
        log.warning("发索引卡失败（不熔断话题群）: %s", e); return ""
    store.set_meta(META_INDEX_PENDING, ""); mid, old = res["message_id"], info.get("message_id")
    store.set_meta(META_INDEX, json.dumps({"message_id": mid, "sent_at": now.isoformat(timespec="seconds"), "hash": h,
                                           "thread_id": res.get("thread_id") or "", "previous": old or ""}, ensure_ascii=False))
    try: source.pin(mid)
    except Exception as e: log.warning("置顶索引卡 %s 失败（卡片照样在群里）: %s", mid, e)
    if old: retire_index(cfg, store, source, old, mid)
    return done("posted")

def sync_index(a, b, source, now: datetime | None = None) -> bool:
    cfg, store = _cfg_store(a, b); return bool(refresh_index(cfg, store, source, now or eng._now()))

def retire_index(cfg, store: Store, source, old: str, new: str) -> None:
    try:
        source.patch_card(old, {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": INDEX_TITLE}, "subtitle": {"tag": "plain_text", "content": "已停用"}, "template": "grey"},
                                "body": {"elements": [{"tag": "markdown", "content": _grey("这张已停用，请看群里最新置顶的那张索引卡")}]}})
    except Exception as e: log.warning("改旧索引卡 %s 失败: %s", old, e)
    if cfg.topic_group.unpin_old_index:
        try: source.unpin(old)
        except Exception as e: log.warning("取消旧索引卡 %s 的置顶失败: %s", old, e)

_REL_TIME_RE = re.compile(r"(?: · )?(?:已等 [^·\n]+|最近动静 [^·\n]+前|拖了 \d+ 小时)|（拖了 \d+ 小时）")
def _strip_time(text: str) -> str: return _REL_TIME_RE.sub("", text.partition("\n")[2])


# ======================= 群成员 =======================
def check_members(cfg, store: Store, source, now: datetime | None = None) -> str:
    now, last = now or eng._now(), _parse(store.get_meta(META_MEMBERS_AT))
    if (last is not None and now - last < timedelta(minutes=MEMBER_CHECK_MINUTES)) or (fn := getattr(source, "chat_members", None)) is None: return ""
    store.set_meta(META_MEMBERS_AT, now.isoformat(timespec="seconds")); chat = cfg.topic_group.chat_id
    try: got = fn(chat)
    except Exception as e: log.warning("查话题群成员失败（不改状态）: %s", e); return "error"
    users, bots, me = set(got.get("users") or []), set(got.get("bots") or []), cfg.self_open_id
    from . import identity as ident
    bot_ids = {x for x in (cfg.identity.bot_open_id or store.get_meta(ident.META_OPEN_ID), cfg.identity.bot_app_id or store.get_meta(ident.META_APP_ID)) if x}
    missing = (["主人"] if me and me not in users else []) + (["机器人"] if bot_ids and not (bot_ids & bots) else [])
    if others := sorted(u for u in users if u != me): warn_strangers(cfg, store, others, "进了话题群")
    if missing: mark_down(cfg, store, f"话题群（{chat}）里没有{'和'.join(missing)}"); return "down"
    clear_down(cfg, store); return "ok"

def warn_strangers(cfg, store: Store, people: list[str], what: str) -> list[str]:
    try: seen = set(json.loads(store.get_meta(META_STRANGERS) or "[]"))
    except json.JSONDecodeError: seen = set()
    if not (new := [p for p in people if p and p not in seen]): return []
    store.set_meta(META_STRANGERS, json.dumps(sorted(seen | set(new))))
    names = "、".join(f"{store.person_name(p) or p}（{p}）" for p in new)
    notify_owner(cfg, store, f"🐕 汇报 · 话题群里有别人 [{eng.BUS_TOPIC_ID}]\n{names}{what}。话题群应该只有你和机器人；他们说的话牧羊犬不分派。不是你拉的就把人移出去。")
    return new


# ======================= 转交、跨窗路标与只读列表 =======================
def _rebind_msg_item(store: Store, mid: str, item_no: int) -> None:
    if not mid or (m := store.get_message(mid)) is None: return
    tags = [t for t in json.loads(m["tags_json"] or "[]") if not t.startswith("item:")] + [f"item:{label(item_no)}"]
    store.conn.execute("UPDATE messages SET tags_json=? WHERE message_id=?", (json.dumps(tags, ensure_ascii=False), mid))
    store.conn.commit()

def _queue_signpost(store: Store, item, to_no: int, to_topic: str, reply_to: str) -> None:
    ex = _item_extra(store, item)
    ex["pending_signposts"] = [*(ex.get("pending_signposts") or []), {"to_item": to_no, "to_topic": to_topic, "reply_to": reply_to or item["root_message_id"] or ""}]
    store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex, ensure_ascii=False), item["item_no"]))
    store.conn.commit()

def flush_pending_signposts(cfg, store: Store, source) -> int:
    """REG-039 / REG-043：在源话题 T-A 或机器人私聊原消息下贴出单行转交路牌（与 post_origin_signposts 共享幂等键）。"""
    from . import outbound
    sent, link, allowed = 0, (chat_link(store, cfg.topic_group.chat_id) if configured(cfg) else ""), outbound.owner_chats(cfg)
    for it in store.list_items((ACTIVE, DONE, ARCHIVED, REMOVED)):
        ex = _item_extra(store, it)
        if not (sps := list(ex.get("pending_signposts") or [])): continue
        for sp in sps:
            to_no, to_topic, rep_mid = int(sp["to_item"]), sp.get("to_topic") or "", sp.get("reply_to") or ""
            target, m_rep = store.get_item(to_no), (store.get_message(rep_mid) if rep_mid else None)
            if target is None: continue
            no = label(to_no)
            text = f"↪️ 这件事归属「{_name(cfg, store, to_topic)}」，已转入专属话题 {no}「{target['title']}」处理" + (f" 👉 [去群里看]({link})" if link else f"（搜 {no}）")
            if m_rep is not None and configured(cfg) and m_rep["chat_id"] != cfg.topic_group.chat_id:
                if m_rep["chat_id"] not in allowed and not (m_rep["chat_type"] == "p2p" and cfg.self_open_id and m_rep["sender_id"] == cfg.self_open_id): continue
                ik = idem(KIND_HANDOFF, rep_mid, str(to_no))
                if store.get_send(ik) is None and source is not None and hasattr(source, "reply_to"):
                    try:
                        res = source.reply_to(rep_mid, identity="bot", idem_key=ik, text=text, in_thread=False)
                        store.add_send(ik, KIND_HANDOFF, to_topic, res["message_id"], text, root_message_id=rep_mid); sent += 1
                    except Exception as e: log.warning("私聊转交路标发送失败（%s）: %s", rep_mid, e)
            elif (root := it["root_message_id"] or rep_mid):
                ik = idem(KIND_HANDOFF, rep_mid or root, str(to_no))
                post_in_thread(cfg, store, source, root, KIND_HANDOFF, text, ik, topic_id=to_topic); sent += 1
        ex["pending_signposts"] = []
        store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex, ensure_ascii=False), it["item_no"]))
        store.conn.commit()
    return sent

def post_origin_signposts(cfg, store: Store, source, item, topic_id: str, batch_id: str = "") -> int:
    """REG-043：跨窗口（机器人私聊 P2P）或跨话题回复进话题群 T-n 时，在主人原入站消息原地补发单行跳转路标。"""
    from . import outbound
    if item is None or not item["root_message_id"] or source is None or not hasattr(source, "reply_to") or not configured(cfg): return 0
    base, allowed, sent = base_topic_id(topic_id), outbound.owner_chats(cfg), 0
    tids = list(dict.fromkeys([topic_id, base, *(t["topic_id"] for t in store.list_topics() if base_topic_id(t["topic_id"]) == base)])) if topic_id else []
    bid, mids = batch_id or (outbound.current_batch(store, topic_id, include_priority=True) if topic_id else ""), []
    if bid:
        mids += [r["message_id"] for r in store.conn.execute("SELECT message_id FROM messages WHERE batch_id=? ORDER BY rowid ASC", (bid,))]
        if (d := store.conn.execute("SELECT message_ids FROM dispatches WHERE batch_id=?", (bid,)).fetchone()) is not None:
            mids += [mid for mid in json.loads(d[0] or "[]") if mid]
    if tids:
        marks = ",".join("?" * len(tids))
        mids += [r[0] for r in store.conn.execute(f"SELECT message_id FROM owner_echoes WHERE topic_id IN ({marks}) AND stage IN ('recorded', 'delivered') AND settled_at IS NULL ORDER BY rowid ASC", tuple(tids))]
        if not mids:
            mids += [r[0] for r in store.conn.execute(f"SELECT message_id FROM messages WHERE topic_id IN ({marks}) AND route='dispatch' AND COALESCE(dispatch_state,'pending') IN ('pending','sending','delivered') AND tags_json LIKE '%forwarded%' ORDER BY rowid ASC", tuple(tids))]
    no, link = label(item["item_no"]), chat_link(store, cfg.topic_group.chat_id)
    for mid in dict.fromkeys(mids):
        if not mid.startswith("om_") or (m := store.get_message(mid)) is None: continue
        if m["chat_id"] not in allowed and not (m["chat_type"] == "p2p" and cfg.self_open_id and m["sender_id"] == cfg.self_open_id): continue
        same_chat = m["chat_id"] == item["chat_id"]
        in_item = same_chat and (m["message_id"] == item["root_message_id"] or (m["root_id"] or "") == item["root_message_id"] or (m["reply_to"] or "") == item["root_message_id"] or (bool(item["thread_id"]) and (m["thread_id"] or "") == item["thread_id"]) or (f"item:{no}" in json.loads(m["tags_json"] or "[]") and "forwarded_from:" not in (m["tags_json"] or "")))
        if in_item or store.get_send(ik := idem(KIND_HANDOFF, m["message_id"], str(item["item_no"]))) is not None: continue
        text = f"↪️ 这件事已在话题群专属话题 {no}「{item['title']}」回复你" + (f" 👉 [去群里看]({link})" if link else f"（搜 {no}）")
        reply_target = (m["root_id"] or m["message_id"]) if m["chat_id"] == cfg.topic_group.chat_id else m["message_id"]
        in_thr = (m["chat_id"] == cfg.topic_group.chat_id or bool(m["thread_id"]))
        try:
            res = source.reply_to(reply_target, identity="bot", idem_key=ik, text=text, in_thread=in_thr)
            store.add_send(ik, KIND_HANDOFF, topic_id, res["message_id"], text, root_message_id=reply_target); sent += 1
        except Exception as e: log.warning("跨窗口/跨话题原消息补发路标失败（%s）: %s", reply_target, e)
    return sent

def on_forward(store: Store, topic_id: str, rows: list, cli: str = "sheepdog") -> list[int]:
    from .prompts import with_cli
    done: list[int] = []
    for r in rows:
        mid = r["message_id"] if "message_id" in r.keys() else ""
        thr_id = r["thread_id"] if "thread_id" in r.keys() and r["thread_id"] else ""
        m_cur = store.get_message(mid) if mid else None
        item = _msg_to_item(store, m_cur or r) or store.item_by_root(mid) or (store.item_by_thread(thr_id) if thr_id else None)
        canon_b = None if base_topic_id(topic_id) in MULTI_ITEM_TOPICS else find_canonical_session_item(store, topic_id)
        if item is None:
            if m_cur is None or canon_b is None or canon_b["item_no"] in done or (m_cur["chat_id"] or "") == (canon_b["chat_id"] or "") or "owner_instruction" in (m_cur["tags_json"] or ""): continue
            if canon_b["state"] in (DONE, ARCHIVED): reopen(store, canon_b); canon_b = store.get_item(canon_b["item_no"])
            done.append(canon_b["item_no"]); _rebind_msg_item(store, mid, canon_b["item_no"]); _queue_signpost(store, canon_b, canon_b["item_no"], topic_id, mid); target = canon_b
        else:
            if item["item_no"] in done or item["state"] == REMOVED: continue
            done.append(item["item_no"])
            if (owner_a := item["owner_topic"] or "") == topic_id: continue
            if owner_a not in ("", eng.BUS_TOPIC_ID) and base_topic_id(owner_a) != base_topic_id(topic_id):
                if canon_b is None:
                    def_key = base_topic_id(topic_id).removeprefix("tp_") or topic_id
                    content = (r["content"] if "content" in r.keys() and r["content"] else "")
                    canon_b = open_item(store, _excerpt(plain_text(content), 60) or item["title"], topic_id, owner_a, ORIGIN_TRACK, item["chat_id"] or "", key=def_key, topic_id=topic_id, related=item["item_no"])
                elif canon_b["state"] in (DONE, ARCHIVED): reopen(store, canon_b); canon_b = store.get_item(canon_b["item_no"])
                _rebind_msg_item(store, mid, canon_b["item_no"]); _queue_signpost(store, item, canon_b["item_no"], topic_id, mid); target = canon_b
            elif canon_b is not None and canon_b["item_no"] != item["item_no"]:
                if canon_b["state"] in (DONE, ARCHIVED): reopen(store, canon_b); canon_b = store.get_item(canon_b["item_no"])
                _rebind_msg_item(store, mid, canon_b["item_no"])
                store.update_item(item["item_no"], state=DONE, status=S_DONE, done_at=now_iso(), done_by=topic_id, related_item=canon_b["item_no"], done_why=f"已并入专属话题 {label(canon_b['item_no'])}")
                _queue_signpost(store, item, canon_b["item_no"], topic_id, mid); target = canon_b
            else:
                store.update_item(item["item_no"], owner_topic=topic_id, handoff_to=topic_id, handoff_at=now_iso()); target = store.get_item(item["item_no"])
        mark_dirty(store); no, keys = label(target["item_no"]), item_keys(store, target["item_no"])
        store.add_system_message(topic_id, "topic_group", with_cli(
            f"🗂 这件事在话题群里是事项 {no}「{target['title']}」，现在由你负责。找主人时用 `sheepdog ask / report --topic {topic_id} --key {no} …`，消息会发进群里这个话题"
            + (f"（它也认钥匙 {'、'.join(keys)}）" if keys else "")
            + f"；你自己另有钥匙就用 `sheepdog track alias --topic {topic_id} --item {no} --key <你的钥匙>` 登记成别名。办完用 `sheepdog track done --topic {topic_id} --key {no} --why \"<依据>\"`。", cli), [f"item:{no}"])
    return done


def list_lines(cfg, store: Store, include_all: bool = False) -> list[str]:
    states, out, now = ((ACTIVE, DONE, ARCHIVED, REMOVED) if include_all else (ACTIVE, DONE)), [], eng._now()
    for it in store.list_items(states):
        st = status(cfg, store, it, now) if it["state"] != REMOVED else REMOVED
        keys = item_keys(store, it["item_no"])
        out.append(f"{label(it['item_no'])} [{STATUS_LABEL.get(st, '已移出')}] {it['title']} · 负责 {it['owner_topic'] or '-'}"
                   + (f" · 钥匙 {'、'.join(keys)}" if keys else "") + f" · {ORIGIN_LABEL.get(it['origin'], it['origin'])}")
    return out
decision_hub.flush_pending_signposts, decision_hub.post_origin_signposts = flush_pending_signposts, post_origin_signposts
