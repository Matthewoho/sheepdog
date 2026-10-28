"""全局待拍板聚合台（Decision Hub）与双向镜像卡片同步、球权契约、结案门禁与三阶催办（7.81）。"""
from __future__ import annotations
import copy, hashlib, json, logging, re
from datetime import datetime, timedelta, timezone
from . import duty
from .card import build_owner_card, card_fits, plain_text
from .card_callback import attach_ask_interactions, build_answered_owner_card
from .store import now_iso

log = logging.getLogger("sheepdog")
DECISION_HUB_TITLE, META_DECISION_HUB = "📌 待我拍板 · 全局聚合台", "decision_hub"
VALID_REPORT_BALLS, CST = ("done", "doing", "other"), timezone(timedelta(hours=8))
MULTI_ITEM_TOPICS = frozenset({"tp_bus", "tp_release"})
_LEGACY_SHARED_KEYS = frozenset({"REL1", "RES1", "PQ1", "G4"})
_LEGACY_MULTI_KEYS = frozenset({"OTHER", "DOING", "WAIT", "SLEEP", "EXPIRED", "DONE", "LN2", "LN3"})

def base_topic_id(topic_id: str) -> str: return (topic_id or "").strip().split("#", 1)[0]

def canonical_group_mode(cfg, store=None) -> bool:
    tg = getattr(cfg, "topic_group", None) if cfg is not None else None
    if not (tg and tg.enabled and tg.chat_id): return False
    if getattr(tg, "force_group", None) is not None: return bool(tg.force_group)
    if getattr(tg, "decision_hub", False) or (store is not None and hub_active(cfg, store)): return True
    if getattr(getattr(cfg, "threads", None), "owner_chat_id", ""): return False
    return "oc_test_esc" not in (getattr(getattr(cfg, "escalation", None), "chat_ids", None) or ())

def find_canonical_session_item(store, topic_id: str):
    if store is None or not (base := base_topic_id(topic_id)): return None
    rows = store.conn.execute("SELECT * FROM tracked_items WHERE (owner_topic=? OR owner_topic=? OR owner_topic LIKE ?) AND state IN ('active', 'done') ORDER BY (state='active') DESC, last_activity_at DESC, item_no DESC", (topic_id, base, f"{base}#%")).fetchall()
    return rows[0] if rows else None

_FENCED_RE, _MORE_LINE_RE = re.compile(r"```.*?```", re.S), re.compile(r"^[ \t]*---(?:more|agent-read)---[ \t]*$", re.M)
_OPTION_LINE_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)、])?\s*(?:\*\*|__)?(?:选项\s*|方案\s*)?[\(\[（【]?([A-E])[\)\]）】]?\s*[:：.、=·\-—]\s*\S+")
_REPLY_PROMPT_RE = re.compile(r"(?:^\s*(?:[-*•>]\s*)?|[，。；：:\s])(?:请\s*(?:[^，。\n]{0,14})?(?:回(?:复)?|发(?:送)?)|回(?:复)?口令\s*[:：])\s*[「『“\"'`]\s*(?:同意|确认|批准|放行|选\s*[A-E])")
_OWNER_WAIT_RE = re.compile(r"^\s*(?:主人|matthew(?:\s+hong)?)(?:\s|$|[（(/:：,，-])", re.I)
_AT_ME_ACTION_RE = re.compile(r"(走查|提测|来测试|测试一下|验证|确认一下|请确认|看看|看下|请回复|反馈一下|审批|放行|核对|review|preview)", re.I)

def _outbound_err(msg: str):
    from .outbound import EXIT_INVALID, OutboundError
    return OutboundError(EXIT_INVALID, msg)

def purge_self_ack_owner_reactions(cfg, store, *, dry_run: bool = False) -> dict:
    """REG-045：清理 owner_reactions 中因飞书 batch_query 不返回 reaction_id 而误记的自动 Get 确认表情。"""
    emoji = (getattr(getattr(cfg, "ack", None), "emoji_type", "Get") or "Get").strip()
    sql = (" FROM owner_reactions WHERE COALESCE(reaction_id, '') = '' AND LOWER(emoji_type) = LOWER(?) "
           "AND message_id IN (SELECT message_id FROM acks WHERE COALESCE(identity, 'user') = 'user' AND COALESCE(reaction_id, '') != '')")
    mids = [r[0] for r in store.conn.execute("SELECT message_id" + sql + " ORDER BY seen_at ASC, rowid ASC", (emoji,)).fetchall()]
    if mids and not dry_run:
        store.conn.execute("DELETE" + sql, (emoji,)); store.conn.commit()
    return {"purged_count": len(mids), "message_ids": mids}

def verify_bus_at_me_no_silent_drop(cfg, store, topic_id: str, batch_id: str = "", receipt: dict | None = None) -> str | None:
    """REG-046：总线 @Matthew (at_me) 行动/走查诉求防静默吞没门禁（仅 tell 旁人事项或交 noop 不算路由主人待办）。"""
    tid = ("tp_" + topic_id.strip() if (topic_id or "").strip() and not (topic_id or "").strip().startswith("tp_") else (topic_id or "").strip())
    if store is None or base_topic_id(tid) != "tp_bus": return None
    if isinstance(receipt, dict) and receipt.get("status") not in (None, "", "handled", "done"): return None
    eff_bid = (batch_id or "").strip() or ((t := store.get_topic("tp_bus")) is not None and (t["pending_batch_id"] or "") or "")
    if not eff_bid: return None
    rows_by_id = {r["message_id"]: r for r in store.conn.execute("SELECT * FROM messages WHERE batch_id=?", (eff_bid,)).fetchall()}
    if (d := store.conn.execute("SELECT message_ids, sent_at FROM dispatches WHERE batch_id=?", (eff_bid,)).fetchone()) is not None:
        for mid in json.loads(d["message_ids"] or "[]"):
            if mid and mid not in rows_by_id and (r := store.get_message(mid)) is not None: rows_by_id[mid] = r
    at_me_rows = [r for r in rows_by_id.values() if r["reason"] == "at_me" and not (r["message_id"] or "").startswith("sd_") and _AT_ME_ACTION_RE.search(r["content"] or "")]
    if not at_me_rows: return None
    if store.forwarded_message_targets(list(rows_by_id)) or any((r["topic_id"] or "") not in ("", "tp_bus") or ("forwarded_to" in r.keys() and (r["forwarded_to"] or "")) for r in rows_by_id.values()):
        return None
    r_acts = (receipt.get("actions") or []) if isinstance(receipt, dict) else []
    if any((str(a.get("type") or a.get("kind") or "").strip().lower() if isinstance(a, dict) else (str(a).strip().split()[0].lower() if str(a).strip() else "")) in ("forward", "new-session", "new_session", "spawn", "ask", "report", "reply") for a in r_acts):
        return None
    since_ts = (d["sent_at"] if d is not None and d["sent_at"] else "") or min((r["create_time"] or "" for r in at_me_rows if r["create_time"]), default="")
    if store.conn.execute("SELECT 1 FROM sends WHERE topic_id='tp_bus' AND kind IN ('ask','report','reply') AND (? = '' OR sent_at >= ?) LIMIT 1", (since_ts, since_ts)).fetchone(): return None
    if store.conn.execute("SELECT 1 FROM actions WHERE kind IN ('new-session','new_session','spawn') AND (? = '' OR requested_at >= ?) LIMIT 1", (since_ts, since_ts)).fetchone(): return None
    from .engine import has_reply_suffix, is_agent_reply
    me, oc, sfx = getattr(cfg, "self_open_id", "") or "", getattr(cfg, "owner_context", None), getattr(getattr(cfg, "session", None), "reply_suffix", "")
    if me and any(any(not (oc and is_agent_reply(c[0] or "", oc)) and not (sfx and has_reply_suffix(c[0] or "", sfx)) for c in store.conn.execute("SELECT content FROM messages WHERE chat_id=? AND sender_id=? AND create_time >= ? AND message_id != ? AND message_id NOT LIKE 'sd_%' AND COALESCE(reason,'') != 'agent_reply_in_owner_chat'", (r["chat_id"], me, r["create_time"] or "", r["message_id"])).fetchall()) for r in at_me_rows):
        return None
    return f"拒绝交账（REG-046 总线 @Matthew 点名诉求防吞没门禁）：批次 {eff_bid} 含显式 @Matthew 的行动/走查诉求（{at_me_rows[0]['message_id']}），但 tp_bus 既未 forward 转交专席、未 new-session 新开专席、也未向主人发 ask/report（仅 tell 旁人事项或交 noop 不算路由主人待办）。请立即用 sheepdog forward 转交或 sheepdog new-session 新开专席承接后再交回执！"

def enforce_report_contract(cfg, store, topic_id: str, title: str, text: str, *, key: str = "", ball: str = "", fyi: bool = False) -> str:
    """7.81 报告球权契约：拦截 --ball owner 及正文末尾伪装成 report 的交互选项块/祈使回执口令（零普通词黑名单）。"""
    b = (ball or "").strip().lower()
    if b in ("owner", "waiting_owner", "等主人"): raise _outbound_err("report 严禁使用 --ball owner（球在主人脚下必须调用 sheepdog ask 发起待拍板卡片，严禁把待拍板伪装成 report）")
    if b and b not in VALID_REPORT_BALLS: raise _outbound_err("report --ball 仅支持 done | doing | other")
    if not fyi and (b or hub_active(cfg, store)):
        cleaned = _MORE_LINE_RE.sub("", _FENCED_RE.sub("", text or ""))
        non_empty = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        opt_keys = {m.group(1) for ln in non_empty[-8:] if (m := _OPTION_LINE_RE.match(ln))}
        if len(opt_keys) >= 2 or any(_REPLY_PROMPT_RE.search(ln) for ln in non_empty[-4:]):
            raise _outbound_err("report 正文末尾检测到交互选项块或祈使回执口令（如 A/B 选项或『请回「同意…」』）：若需主人拍板请改用 sheepdog ask；若仅为历史转述/纯知会请加 --fyi")
    return b

def verify_receipt_no_dead_ball(cfg, store, topic_id: str, batch_id: str, receipt: dict) -> str | None:
    """REG-044：消灭『死球真空』——纯账本状态守恒校验（无 ask 禁交 needs_decision，无 ask 禁留 waiting_owner / waiting_on=主人）。"""
    tid = ("tp_" + topic_id.strip() if (topic_id or "").strip() and not (topic_id or "").strip().startswith("tp_") else (topic_id or "").strip())
    base = base_topic_id(tid)
    if not isinstance(receipt, dict) or not tid or base == "tp_bus" or store is None: return None
    if not (canonical_group_mode(cfg, store) or hub_active(cfg, store) or getattr(store, "enforce_receipt_ball_gate", False)): return None
    status = str(receipt.get("status") or "").strip()
    open_escs = store.conn.execute("SELECT message_id FROM escalations WHERE (topic_id=? OR topic_id=?) AND closed_at IS NULL", (tid, base)).fetchall()
    if status == "needs_decision" and not open_escs:
        return 'receipt 没收: status="needs_decision" 但当前会话在飞书没有任何未决的 sheepdog ask 待拍板卡片（open_escalations=0）！必须先调用 sheepdog ask 把决策卡发到飞书，再交回执。'
    if status in ("handled", "waiting") and not open_escs:
        r_works = [w for w in (receipt.get("work") or []) if isinstance(w, dict)]
        cleared = {duty.norm_key(str(w.get("key") or "")) for w in r_works if w.get("key") and str(w.get("state") or "doing").strip().lower() in ("done", "handed_off", "waiting_other", "doing") and not _OWNER_WAIT_RE.search(str(w.get("waiting_on") or ""))}
        work_dead = any(str(w.get("state") or "").strip().lower() == "waiting_owner" or _OWNER_WAIT_RE.search(str(w.get("waiting_on") or "")) for w in r_works)
        if not work_dead:
            db_works = {w["id"]: w for t in dict.fromkeys([tid, base]) for w in store.open_work(t)}.values()
            work_dead = any(w["norm_key"] not in cleared and (w["state"] == "waiting_owner" or _OWNER_WAIT_RE.search(w["waiting_on"] or "")) for w in db_works)
        if work_dead:
            return "receipt 没收: 检测到『死球真空』（当前飞书无未决 sheepdog ask，但账本 work_items 或回执 work 显示事项仍在等主人拍板 waiting_owner / waiting_on=主人）！若需主人拍板，必须先调用 sheepdog ask 发飞书决策卡；若已不再等主人，请先用 sheepdog work set --state doing/done（或 report --ball doing|done）更新状态后再交回执。"
    return None

def apply_report_ball_state(store, topic_id: str, key: str, ball: str) -> None:
    """7.81 report --ball 联动更新 work_items 状态。"""
    b = (ball or "").strip().lower()
    if b not in VALID_REPORT_BALLS or not key or store.get_work(topic_id, duty.norm_key(key)) is None: return
    mapped = {"done": duty.DONE, "doing": duty.DOING, "other": duty.WAITING_OTHER}[b]
    try: duty.set_work(store, topic_id, key, state=mapped, result=f"report --ball {b}")
    except TypeError: duty.set_work(store, topic_id, key, mapped, note=f"report --ball {b}")

def _ts_not_before(msg_ts: str, asked_ts: str) -> bool:
    if not asked_ts or not msg_ts: return True
    try: return datetime.fromisoformat(msg_ts) >= datetime.fromisoformat(asked_ts)
    except ValueError: return msg_ts >= asked_ts

def verify_resolve_allowed(cfg, store, targets: list, *, quote: str = "", superseded_by: str = "") -> None:
    """7.81 严禁自关未答提问硬门禁：answered_at 为空时必须提供可验真的 --quote 或 --superseded-by。"""
    q, sup_id, me = (quote or "").strip(), (superseded_by or "").strip(), getattr(cfg, "self_open_id", "") or ""
    for e in targets:
        if e["answered_at"]: continue
        if not q and not sup_id:
            raise _outbound_err(f"禁止自关未答提问（{e['message_id']}）：主人尚未拍板（answered_at IS NULL）。若主人已在别处批复请传 --quote <主人消息ID或原话>；若已被新 ask 替代请传 --superseded-by <新ask消息ID>")
        if q:
            ok = bool(q.startswith("om_") and (m := store.get_message(q)) is not None and ((me and m["sender_id"] == me) or ("owner_verified" in (m["tags_json"] or ""))) and _ts_not_before(m["create_time"] or "", e["asked_at"] or ""))
            if not ok:
                rows = store.conn.execute("SELECT message_id, create_time, content, sender_id, tags_json FROM messages WHERE (sender_id=? OR tags_json LIKE '%owner_verified%') AND create_time >= ? ORDER BY create_time DESC LIMIT 50", (me, e["asked_at"] or "")).fetchall()
                ok = any(_ts_not_before(r["create_time"] or "", e["asked_at"] or "") and (q == r["message_id"] or q in (r["content"] or "") or q in plain_text(r["content"] or "")) for r in rows)
            if not ok: raise _outbound_err(f"resolve --quote 验真失败：账本中未找到主人在 {e['asked_at']} 之后发送的匹配消息（{q}）")
        if sup_id and ((sup := store.get_escalation(sup_id) or store.send_by_message(sup_id)) is None or sup["message_id"] == e["message_id"]):
            raise _outbound_err(f"resolve --superseded-by 验真失败：未找到替代提问 {sup_id}")

def get_hub_meta(store) -> dict:
    raw = store.get_meta(META_DECISION_HUB) if hasattr(store, "get_meta") else ""
    try: return d if isinstance(d := (json.loads(raw) if raw else {}), dict) else {}
    except json.JSONDecodeError: return {}

def hub_active(cfg, store) -> bool:
    return bool(get_hub_meta(store).get("root_message_id") or getattr(getattr(cfg, "topic_group", None), "decision_hub", False))

def _open_escalation_rows(store) -> list:
    return store.conn.execute("SELECT * FROM escalations WHERE closed_at IS NULL AND answered_at IS NULL ORDER BY asked_at ASC, rowid ASC").fetchall()

def build_hub_root_card(cfg, store) -> tuple[str, dict]:
    """构造全局聚合台 1F 置顶动态汇总卡。"""
    rows = _open_escalation_rows(store)
    n, sub = len(rows), (f"当前 {len(rows)} 项待拍板" if rows else "🟢 当前 0 项待拍板")
    lines = [f"{DECISION_HUB_TITLE} · {sub}"]
    if not rows:
        lines.append("🟢 当前 0 项待拍板")
        els = [{"tag": "markdown", "content": "🟢 **当前 0 项待拍板**\n所有提问均已拍板或结案。"}]
    else:
        item_lines: list[str] = []
        for idx, e in enumerate(rows, 1):
            s = store.send_by_message(e["message_id"]) if hasattr(store, "send_by_message") else None
            rkeys, sk = set(e.keys()), (set(s.keys()) if s is not None else set())
            key = (s["key"] if "key" in sk and s["key"] else "") or (s["item_key"] if "item_key" in sk and s["item_key"] else "") or (s["norm_key"] if "norm_key" in sk and s["norm_key"] else "")
            root_mid = (e["origin_root_id"] if "origin_root_id" in rkeys and e["origin_root_id"] else "") or (s["root_message_id"] if "root_message_id" in sk and s["root_message_id"] else "")
            it = store.item_by_root(root_mid) if root_mid and hasattr(store, "item_by_root") else None
            t_label, k_label = (f"T-{it['item_no']} · " if it is not None else ""), (f" · 钥匙 {key}" if key else "")
            first = ((e["text"] or "").strip().splitlines() or [e["message_id"]])[0]
            nudge_n = int(e["nudge_count"] or 0) if "nudge_count" in rkeys else 0
            nudge_str = f" · ⏰ 已催 {nudge_n} 次" if nudge_n > 0 else ""
            entry = f"{idx}. **[{e['topic_id']}]** {t_label}{first}{k_label}（`{e['message_id']}` · {e['asked_at']}{nudge_str}）"
            item_lines.append(entry); lines.append(entry)
        els = [{"tag": "markdown", "content": "\n".join(item_lines)}]
    card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": DECISION_HUB_TITLE}, "subtitle": {"tag": "plain_text", "content": sub}, "template": "orange" if n > 0 else "green"}, "body": {"elements": els}}
    return "\n".join(lines), card

def ensure_hub_topic(cfg, store, source) -> dict:
    """确保话题群中已创建并置顶『📌 待我拍板 · 全局聚合台』根话题。"""
    tg_cfg = getattr(cfg, "topic_group", None)
    if not (tg_cfg and tg_cfg.enabled and tg_cfg.chat_id and source is not None and hasattr(source, "send_to_chat")): return {}
    meta = get_hub_meta(store)
    if meta.get("chat_id") == tg_cfg.chat_id and meta.get("root_message_id"): return meta
    plain, card = build_hub_root_card(cfg, store)
    res = source.send_to_chat(tg_cfg.chat_id, plain, f"sdh-hub-root-{tg_cfg.chat_id}"[:50], "bot", card=card)
    root_mid, h = res["message_id"], hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    meta = {"chat_id": tg_cfg.chat_id, "root_message_id": root_mid, "thread_id": res.get("thread_id") or "", "card_hash": h}
    store.set_meta(META_DECISION_HUB, json.dumps(meta, ensure_ascii=False)); store.add_bot_outbox(root_mid, "tp_bus", False, plain, now_iso())
    if hasattr(source, "pin"):
        try: source.pin(root_mid)
        except Exception: pass
    return meta

def refresh_hub_root_card(cfg, store, source) -> str:
    """刷新『📌 待我拍板 · 全局聚合台』1F 根卡。"""
    _, card = build_hub_root_card(cfg, store); meta = get_hub_meta(store); root_mid = meta.get("root_message_id") or ""
    if not root_mid or source is None or not hasattr(source, "patch_card"): return ""
    h = hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    if meta.get("card_hash") == h: return "same"
    try: source.patch_card(root_mid, card, identity="bot")
    except Exception as e: log.warning("刷新待拍板聚合台根卡失败（%s）: %s", root_mid, e); return ""
    meta["card_hash"] = h; store.set_meta(META_DECISION_HUB, json.dumps(meta, ensure_ascii=False)); return "patched"

def find_open_escalation_for_key(store, topic_id: str, norm_key: str):
    """查找同一 (topic_id, norm_key) 下未结案、未答复的提问。"""
    if not (nk := duty.norm_key(norm_key) if norm_key else ""): return None
    return store.conn.execute("SELECT e.* FROM escalations e JOIN sends s ON s.message_id = e.message_id WHERE e.topic_id = ? AND (s.item_key = ? OR s.norm_key = ? OR s.key = ?) AND e.closed_at IS NULL AND e.answered_at IS NULL ORDER BY e.asked_at DESC LIMIT 1", (topic_id, nk, nk, norm_key)).fetchone()

def _truncate_hub_more(shown: str, max_detail_chars: int = 1600) -> str:
    s = shown or ""
    if "\n---more---\n" in s:
        head, sep, tail = s.partition("\n---more---\n")
        if len(tail) > max_detail_chars or len(s) > 3500:
            return f"{head}{sep}{tail[:max_detail_chars].rstrip()}\n…（详情已截断，完整原文见来源业务话题）"
    elif len(s) > 3500:
        return f"{s[:max_detail_chars].rstrip()}\n…（详情已截断，完整原文见来源业务话题）"
    return s

def _build_hub_mirror_card(cfg, shown: str, *, origin_mid: str, topic_id: str, key: str, title: str, options_spec: str = "auto", item_no: int | None = None) -> tuple[str, dict | None]:
    shown = _truncate_hub_more(shown)
    badge = f"📍 来源话题：{f'T-{item_no} · ' if item_no else ''}[{topic_id}]{f' · 钥匙 {key}' if key else ''}"
    mirror_text = f"{shown.rstrip()}\n{badge}"
    if not getattr(getattr(cfg, "notify", None), "owner_card", True): return mirror_text, None
    try: base_card = build_owner_card(shown, getattr(cfg, "self_open_id", "") or "")
    except ValueError: return mirror_text, None
    base_card.setdefault("body", {}).setdefault("elements", []).insert(0, {"tag": "markdown", "content": badge})
    card = attach_ask_interactions(base_card, text=shown, options_spec=options_spec or "auto", topic_id=topic_id, key=key, title=title, interactive=True)
    for el in card.get("body", {}).get("elements", []):
        cols = el.get("columns", []) if el.get("tag") == "column_set" else [c for r in el.get("elements", []) for c in r.get("columns", [])] if el.get("tag") == "form" else []
        for sub_el in (s for col in cols for s in col.get("elements", [])):
            if isinstance(sub_el.get("value"), dict): sub_el["value"]["message_id"] = origin_mid
            for beh in sub_el.get("behaviors", []):
                if isinstance(beh.get("value"), dict): beh["value"]["message_id"] = origin_mid
    return mirror_text, (card if card_fits(card) else None)

def try_patch_open_ask(cfg, store, source, topic_id: str, key: str, norm_key: str, title: str, shown: str, card: dict | None, *, options_spec: str = "auto") -> dict | None:
    """同一 (topic_id, key) 已有未结提问时，原地 PATCH 更新业务话题卡与聚合台镜像卡，不刷第 2 张卡。"""
    if not (nk := norm_key or (duty.norm_key(key) if key else "")) or (esc := find_open_escalation_for_key(store, topic_id, nk)) is None: return None
    canon_mid = esc["message_id"]
    with store.tx() as c:
        c.execute("UPDATE escalations SET text=? WHERE message_id=?", (shown, canon_mid)); c.execute("UPDATE sends SET text=?, title=? WHERE message_id=?", (shown, title, canon_mid))
    if source is not None and hasattr(source, "patch_card"):
        if card is None and getattr(getattr(cfg, "notify", None), "owner_card", True):
            try: card = attach_ask_interactions(build_owner_card(shown, getattr(cfg, "self_open_id", "") or ""), text=shown, options_spec=options_spec or "auto", topic_id=topic_id, key=key, title=title, interactive=True)
            except Exception: card = None
        if card is not None:
            try: source.patch_card(canon_mid, card, identity="bot")
            except Exception as e: log.warning("原地更新提问卡片失败（%s）: %s", canon_mid, e)
        if hub_mid := (esc["hub_message_id"] if "hub_message_id" in esc.keys() and esc["hub_message_id"] else ""):
            _, hub_card = _build_hub_mirror_card(cfg, shown, origin_mid=canon_mid, topic_id=topic_id, key=key or nk, title=title, options_spec=options_spec)
            if hub_card is not None:
                try: source.patch_card(hub_mid, hub_card, identity="bot")
                except Exception as e: log.warning("原地更新聚合台镜像卡片失败（%s）: %s", hub_mid, e)
    refresh_hub_root_card(cfg, store, source)
    origin_root = (esc["origin_root_id"] if "origin_root_id" in esc.keys() and esc["origin_root_id"] else "") or canon_mid
    return {"message_id": canon_mid, "root_message_id": origin_root, "text": shown, "patched_in_place": True}

def mirror_ask_to_hub(cfg, store, source, *, origin_mid: str, origin_root_id: str, topic_id: str, key: str, title: str, shown: str, options_spec: str = "auto", item_no: int | None = None) -> str:
    """将新发出的 ask 双向镜像投射到『📌 待我拍板 · 全局聚合台』话题内，并绑定 escalations.hub_message_id。"""
    tg_cfg = getattr(cfg, "topic_group", None)
    if not (tg_cfg and tg_cfg.enabled and tg_cfg.chat_id and source is not None and hasattr(source, "reply_to") and hub_active(cfg, store)):
        store.set_escalation_hub(origin_mid, "", origin_root_id); return ""
    if store.get_escalation(origin_mid) is None: store.add_escalation(origin_mid, topic_id, tg_cfg.chat_id, shown, now_iso())
    try: meta = ensure_hub_topic(cfg, store, source); hub_root = meta.get("root_message_id") or ""
    except Exception as e: log.warning("初始化待拍板聚合台失败: %s", e); meta, hub_root = {}, ""
    if not hub_root: store.set_escalation_hub(origin_mid, "", origin_root_id); return ""
    mirror_text, hub_card = _build_hub_mirror_card(cfg, _truncate_hub_more(shown), origin_mid=origin_mid, topic_id=topic_id, key=key, title=title, options_spec=options_spec, item_no=item_no)
    ik, res = f"hub_mirror:{origin_mid}"[:50], {}
    try: res = source.reply_to(hub_root, identity="bot", idem_key=ik, text="" if hub_card else mirror_text, in_thread=True, **({"card": hub_card} if hub_card else {}))
    except Exception as e: log.warning("镜像提问卡片到聚合台失败（%s），将退回纯文本重试: %s", origin_mid, e); res = {}
    if not (res or {}).get("message_id") and hub_card is not None:
        try: res = source.reply_to(hub_root, identity="bot", idem_key=f"{ik}:txt"[:50], text=mirror_text, in_thread=True)
        except Exception as e: log.warning("镜像提问纯文本重试失败（%s）: %s", origin_mid, e); store.set_escalation_hub(origin_mid, "", origin_root_id); return ""
    if isinstance(res, dict) and res.get("thread_id") and not meta.get("thread_id"):
        meta["thread_id"] = res["thread_id"]; store.set_meta(META_DECISION_HUB, json.dumps(meta, ensure_ascii=False))
    hub_mid = (res or {}).get("message_id") or ""; store.set_escalation_hub(origin_mid, hub_mid, origin_root_id)
    if hub_mid: store.add_bot_outbox(hub_mid, topic_id, False, mirror_text, now_iso())
    refresh_hub_root_card(cfg, store, source); return hub_mid

def sync_escalation_dual_cards(cfg, store, source, any_mid: str, *, chosen_display: str, topic_id: str = "", answered_at: str = "", orig_text: str = "", answer_mid: str = "") -> str:
    esc = store.get_escalation(any_mid) if any_mid and hasattr(store, "get_escalation") else None
    canon_mid = str(esc["message_id"]) if esc is not None else str(any_mid or "")
    if answer_mid and hasattr(store, "answer_escalation"): store.answer_escalation(canon_mid, answer_mid, allow_update=True)
    rkeys = set(esc.keys()) if esc is not None else set()
    hub_mid = str(esc["hub_message_id"] or "") if "hub_message_id" in rkeys else ""
    text_to_use, tid = _truncate_hub_more(orig_text or (esc["text"] if esc is not None and esc["text"] else "")), topic_id or (esc["topic_id"] if esc is not None and esc["topic_id"] else "")
    green_card = build_answered_owner_card(text_to_use, chosen_display=chosen_display, topic_id=tid, answered_at=answered_at or now_iso())
    if source is not None and hasattr(source, "patch_card"):
        for mid in dict.fromkeys([m for m in (canon_mid, hub_mid, any_mid) if m]):
            try: source.patch_card(mid, copy.deepcopy(green_card), identity="bot")
            except Exception as e: log.warning("双端同步绿卡失败（%s）: %s", mid, e)
    refresh_hub_root_card(cfg, store, source); return canon_mid

settle_escalation_views = sync_escalation_dual_cards

def on_escalations_resolved(cfg, store, source, targets: list, *, why: str = "") -> None:
    for e in targets: sync_escalation_dual_cards(cfg, store, source, e["message_id"], chosen_display=f"已结案（{why or 'resolved'}）", topic_id=e["topic_id"], orig_text=e["text"] or "")

def is_hub_message(store, m) -> bool:
    if m is None: return False
    meta = get_hub_meta(store)
    root_mid, thr_id = meta.get("root_message_id") or "", meta.get("thread_id") or ""
    reply_to, root_id, m_thr = getattr(m, "reply_to", "") or "", getattr(m, "root_id", "") or "", getattr(m, "thread_id", "") or ""
    if root_mid and (reply_to == root_mid or root_id == root_mid or getattr(m, "message_id", "") == root_mid): return True
    if thr_id and m_thr == thr_id: return True
    return bool(reply_to and (esc := store.get_escalation(reply_to)) is not None and "hub_message_id" in esc.keys() and esc["hub_message_id"] == reply_to)

def handle_hub_text_reply(collector, m) -> bool:
    store, cfg, source = collector.store, collector.cfg, getattr(collector, "source", None)
    if not is_hub_message(store, m): return False
    reply_to = getattr(m, "reply_to", "") or ""
    esc = store.get_escalation(reply_to) if reply_to else None
    if esc is None and len(opens := _open_escalation_rows(store)) == 1: esc = opens[0]
    if esc is None: return False
    canon_mid = str(esc["message_id"])
    store.answer_escalation(canon_mid, m.message_id)
    chosen = " ".join(plain_text(getattr(m, "content", "") or "").split())[:60] or "主人文字批复"
    sync_escalation_dual_cards(cfg, store, source, canon_mid, chosen_display=chosen, topic_id=esc["topic_id"], answered_at=m.create_time or now_iso(), orig_text=esc["text"] or "")
    store.upsert_message(m, "dispatch", "owner_reply", ["owner_verified", f"escalation:{canon_mid}"], topic_id=esc["topic_id"])
    return True

def defer_quiet_hours(dt: datetime) -> datetime:
    cst = dt.astimezone(CST); return cst.replace(hour=10, minute=0, second=0, microsecond=0) if 0 <= cst.hour < 9 else cst

def next_nudge_due_at(asked_at: datetime, nudge_count: int, last_nudged_at: datetime | None = None) -> datetime | None:
    if nudge_count <= 0: return defer_quiet_hours(asked_at.astimezone(CST) + timedelta(minutes=30))
    if nudge_count == 1:
        base = asked_at.astimezone(CST) + timedelta(hours=2)
        return defer_quiet_hours(max(base, last_nudged_at.astimezone(CST) + timedelta(minutes=90)) if last_nudged_at else base)
    if nudge_count == 2:
        nd = max(asked_at.astimezone(CST), last_nudged_at.astimezone(CST) if last_nudged_at else asked_at.astimezone(CST)).date() + timedelta(days=1)
        return datetime(nd.year, nd.month, nd.day, 10, 0, 0, tzinfo=CST)
    return None

def nudge_open_escalations(cfg, store, source, *, now: datetime | None = None) -> list[dict]:
    """扫描未答提问并按三阶阶梯在聚合台（或原话题）发送催办提醒，00:00~09:00 CST 静默。"""
    if 0 <= (ref_now := (now or datetime.now(CST)).astimezone(CST)).hour < 9: return []
    rows = store.conn.execute("SELECT * FROM escalations WHERE closed_at IS NULL AND answered_at IS NULL AND COALESCE(nudge_count, 0) < 3 ORDER BY asked_at ASC").fetchall()
    nudged, hub_root, me = [], (get_hub_meta(store).get("root_message_id") or ""), (getattr(cfg, "self_open_id", "") or "")
    at_tag = f" <at id={me}></at>" if me else ""
    for e in rows:
        try: asked_dt, last_dt = datetime.fromisoformat(e["asked_at"]).astimezone(CST), (datetime.fromisoformat(e["last_nudged_at"]).astimezone(CST) if e["last_nudged_at"] else None)
        except Exception: continue
        if (due := next_nudge_due_at(asked_dt, cur_count := int(e["nudge_count"] or 0), last_dt)) is None or ref_now < due: continue
        target_stage = cur_count + 1
        while target_stage < 3 and (nxt := next_nudge_due_at(asked_dt, target_stage, ref_now)) is not None and ref_now >= nxt: target_stage += 1
        with store.tx() as c: c.execute("UPDATE escalations SET nudge_count=?, last_nudged_at=? WHERE message_id=? AND closed_at IS NULL", (target_stage, ref_now.isoformat(timespec="seconds"), e["message_id"]))
        reply_root, ik = (hub_root or (e["origin_root_id"] if "origin_root_id" in e.keys() and e["origin_root_id"] else "") or e["message_id"]), f"ask_nudge:{e['message_id']}:{target_stage}"[:50]
        nudge_text = f"⏰ 待拍板催办（第 {target_stage}/3 次）：[{e['topic_id']}] {((e['text'] or '').strip().splitlines() or [e['message_id']])[0]}（`{e['message_id']}`）{at_tag}"
        if source is not None and hasattr(source, "reply_to"):
            try: source.reply_to(reply_root, identity="bot", idem_key=ik, text=nudge_text, in_thread=True)
            except Exception as exc: log.warning("发送提问催办失败（%s）: %s", e["message_id"], exc)
        nudged.append({"message_id": e["message_id"], "topic_id": e["topic_id"], "nudge_count": target_stage, "idem_key": ik})
    if nudged: refresh_hub_root_card(cfg, store, source)
    return nudged

def flush_pending_signposts(cfg, store, source) -> int:
    """REG-039：在源话题 T-A（或私聊原消息/临时总线话题）内贴出单行转交路牌并清空 pending_signposts。"""
    from . import topicgroup as tg
    sent, link = 0, (tg.chat_link(store, cfg.topic_group.chat_id) if tg.configured(cfg) else "")
    for it in store.list_items((tg.ACTIVE, tg.DONE, tg.ARCHIVED, tg.REMOVED)):
        if not (sps := list((ex := tg._item_extra(store, it)).get("pending_signposts") or [])): continue
        for sp in sps:
            to_no, to_topic, rep_mid = int(sp["to_item"]), sp.get("to_topic") or "", sp.get("reply_to") or ""
            target, m_sp, root = store.get_item(to_no), (store.get_message(rep_mid) if rep_mid else None), (it["root_message_id"] or rep_mid)
            if target is None: continue
            text = f"↪️ 这件事归属「{tg._name(cfg, store, to_topic)}」，已转入专属话题 {tg.label(to_no)}「{target['title']}」处理" + (f" 👉 [去群里看]({link})" if link else "")
            if m_sp is not None and m_sp["chat_id"] != cfg.topic_group.chat_id:
                if store.get_send(ik := tg.idem(tg.KIND_HANDOFF, rep_mid, str(to_no))) is None and source is not None and hasattr(source, "reply_to"):
                    res = source.reply_to(rep_mid, identity="bot", idem_key=ik, text=text, in_thread=bool(m_sp["thread_id"]))
                    store.add_send(ik, tg.KIND_HANDOFF, to_topic, (res or {}).get("message_id") or "", text, root_message_id=rep_mid); sent += 1
            elif root and store.get_send(tg.idem(tg.KIND_HANDOFF, rep_mid or root, str(to_no))) is None and store.get_send(ik := tg.idem(tg.KIND_HANDOFF, str(it["item_no"]), str(to_no), rep_mid or root)) is None:
                tg.post_in_thread(cfg, store, source, root, tg.KIND_HANDOFF, text, ik, topic_id=to_topic); sent += 1
        ex["pending_signposts"] = []
        store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex, ensure_ascii=False), it["item_no"])); store.conn.commit()
    return sent

def heal_canonical_topics(cfg, store, source=None, *, dry_run: bool = False) -> dict:
    """REG-041：存量分裂与串味自愈（剥离跨会话污染钥匙、归并单职责会话重复活跃话题、收编残留私聊话题）。"""
    from . import topicgroup as tg
    tg._ensure_extra_col(store)
    merged_items, rebound_keys, migrated_dm = [], [], []
    for kr in store.conn.execute("SELECT item_no, key, norm_key, kind, topic_id FROM tracked_keys WHERE kind != 'number'").fetchall():
        ktid, k_base = (kr["topic_id"] or "").strip(), base_topic_id(kr["topic_id"] or "")
        it = store.get_item(kr["item_no"])
        owner_base = base_topic_id((it["owner_topic"] if it else "") or "")
        if not k_base or k_base in MULTI_ITEM_TOPICS or it is None or not owner_base or k_base == owner_base: continue
        canon = find_canonical_session_item(store, ktid)
        rebound_keys.append({"key": kr["key"], "norm_key": kr["norm_key"], "from_item": kr["item_no"], "topic_id": ktid, "to_item": canon["item_no"] if canon else None})
        if not dry_run:
            store.conn.execute("DELETE FROM tracked_keys WHERE item_no=? AND norm_key=? AND topic_id=?", (kr["item_no"], kr["norm_key"], kr["topic_id"]))
            ex = tg._item_extra(store, it)
            if "alias_keys" in ex:
                ex["alias_keys"] = [a for a in ex["alias_keys"] if tg.track_norm(a) != kr["norm_key"]]
                store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex, ensure_ascii=False), it["item_no"]))
            store.conn.commit()
            if canon is None and tg.configured(cfg):
                canon = tg.open_item(store, ktid, ktid, ktid, tg.ORIGIN_TRACK, cfg.topic_group.chat_id, key=kr["key"], topic_id=ktid)
            elif canon is not None:
                tg.bind_alias_key(store, canon["item_no"], kr["key"], ktid)
            rebound_keys[-1]["to_item"] = canon["item_no"] if canon else None
    by_base: dict[str, list] = {}
    for it in store.list_items((tg.ACTIVE,)):
        if (b := base_topic_id(it["owner_topic"] or "")) and b not in MULTI_ITEM_TOPICS:
            by_base.setdefault(b, []).append(it)
    for b, items in by_base.items():
        if len(items) <= 1: continue
        items.sort(key=lambda r: (bool(r["root_message_id"]), r["last_activity_at"] or r["opened_at"] or "", r["item_no"]), reverse=True)
        canon = items[0]
        for dup in items[1:]:
            merged_items.append({"from_item": dup["item_no"], "to_item": canon["item_no"], "owner_topic": dup["owner_topic"]})
            if not dry_run:
                dup_keys = [kr for kr in store.item_keys(dup["item_no"]) if kr["kind"] != "number"]
                dup_aliases = list(tg._item_extra(store, dup).get("alias_keys") or [])
                store.conn.execute("DELETE FROM tracked_keys WHERE item_no=? AND kind != 'number'", (dup["item_no"],))
                ex_d = tg._item_extra(store, dup); ex_d["alias_keys"] = []
                store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex_d, ensure_ascii=False), dup["item_no"]))
                store.conn.commit()
                for kr in dup_keys: tg.bind_alias_key(store, canon["item_no"], kr["key"], kr["topic_id"] or canon["owner_topic"] or b)
                for a in dup_aliases: tg.bind_alias_key(store, canon["item_no"], a, canon["owner_topic"] or b)
                store.update_item(dup["item_no"], state=tg.ARCHIVED, archived_at=now_iso(), status=tg.S_ARCHIVED, related_item=canon["item_no"], done_why=f"已并入专属话题 {tg.label(canon['item_no'])}")
    linked_dm = {r[0] for r in store.conn.execute("SELECT dm_root_message_id FROM tracked_items WHERE dm_root_message_id IS NOT NULL AND dm_root_message_id != ''")}
    for thr in store.conn.execute("SELECT * FROM owner_threads ORDER BY last_post_at DESC").fetchall():
        tid, b, dm_root = thr["topic_id"] or "", base_topic_id(thr["topic_id"] or ""), thr["root_message_id"] or ""
        if not b or b in MULTI_ITEM_TOPICS or not dm_root or dm_root in linked_dm: continue
        canon = find_canonical_session_item(store, tid)
        if canon is not None and dm_root in (tg._item_extra(store, canon).get("migrated_dm_roots") or []): continue
        migrated_dm.append({"root_message_id": dm_root, "topic_id": tid, "key": thr["key"] or "", "to_item": canon["item_no"] if canon else None})
        if not dry_run:
            if canon is None and tg.configured(cfg):
                canon = tg.open_item(store, thr["title"] or thr["key"] or tid, tid, tid, tg.ORIGIN_MOVE, cfg.topic_group.chat_id, key=thr["key"] or "", topic_id=tid, dm_root=dm_root)
            elif canon is not None:
                if not canon["dm_root_message_id"]: store.update_item(canon["item_no"], dm_root_message_id=dm_root)
                if thr["key"]: tg.bind_alias_key(store, canon["item_no"], thr["key"], tid)
                ex = tg._item_extra(store, store.get_item(canon["item_no"]))
                ex["migrated_dm_roots"] = list(dict.fromkeys([*(ex.get("migrated_dm_roots") or []), dm_root]))
                store.conn.execute("UPDATE tracked_items SET extra=? WHERE item_no=?", (json.dumps(ex, ensure_ascii=False), canon["item_no"]))
                store.conn.commit()
            migrated_dm[-1]["to_item"] = canon["item_no"] if canon else None
            linked_dm.add(dm_root)
    if not dry_run and (merged_items or rebound_keys or migrated_dm): tg.mark_dirty(store)
    return {"merged_items": merged_items, "rebound_keys": rebound_keys, "migrated_dm_threads": migrated_dm}
