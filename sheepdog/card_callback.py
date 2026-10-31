"""飞书交互式拍板卡片与版本升级交互卡片（Card JSON 2.0 按钮 + 其他输入框）及 card.action.trigger 回调处理（7.73 ~ 7.76，REG-017 ~ REG-026）。"""

from __future__ import annotations

import copy, hashlib, json, logging, re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .card import build_owner_card, card_fits, plain_text
from .models import Message
from .store import now_iso
from .version_watch import attach_version_interactions, parse_version_card_actions

log = logging.getLogger("sheepdog")

_DISABLED_SPECS = frozenset({"none", "off", "false", "0"})
_AUTO_SPECS = frozenset({"auto", "default"})
_OPTION_KEYS = ("A", "B", "C", "D", "E")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.S)
_MORE_LINE_RE = re.compile(r"^[ \t]*---(?:more|agent-read)---[ \t]*$", re.M)
_HEADER_LINE_RE = re.compile(r"^🐕\s*(?:需要你定|汇报)\b[^\n]*")
_EXPLICIT_ITEM_RE = re.compile(r"^\s*[\(\[（【]?([A-Ea-e])[\)\]）】]?\s*[:：=.、·\-—)\]]\s*(.+?)\s*$")
_LINE_OPTION_RE = re.compile(
    r"^\s*(?:(?:[-*•+]|\d+[.)、])\s*)?(?:\*\*|__)?(?:选项\s*|方案\s*|回(?:复)?\s*|选\s*)?"
    r"[\(\[（【]?([A-E])[\)\]）】]?(?:（[^）]{1,10}）|\([^)]{1,10}\))?(?:\*\*|__)?\s*(?:[:：.、=·\-—]|(?<=[A-E])\s+)\s*(.+?)\s*$"
)
_INLINE_PREFIXED_RE = re.compile(r"(?:回(?:复)?|选|选项|方案)\s*[\(\[（]?([A-E])[\)\]）]?\s*(?:[:：.、=·\-—]\s*|\s+)([^，,；;\n。！!？?]{1,48})")
_INLINE_COLON_RE = re.compile(r"(?<![A-Za-z0-9_])([A-E])\s*[:：]\s*([^，,；;\n。！!？?]{1,48})")
_QUOTE_CMD_RE = re.compile(r"(?:请\s*)?(?:回(?:复)?|发)\s*[「『“\"]([^」』”\"\n]{2,60})[」』”\"]")
_TOPIC_TAG_RE = re.compile(r"\[(tp_[A-Za-z0-9_.#-]+)\]")


@dataclass(frozen=True)
class AskOption:
    key: str        # "A" .. "E"
    label: str      # 按钮短标签（<= 28 字符）
    reply_text: str # 等效主人回复文本，如 "A：同意发布"


def _clean_opt_desc(raw: str) -> str:
    s = re.sub(r"^(?:\*\*|__)+|(?:\*\*|__)+$", "", (raw or "").strip()).strip()
    return " ".join(s.strip("`'\"“”「」『』").rstrip("。；;，,！!？? ").split())


def _make_option(key: str, raw_desc: str, *, full_reply: str = "") -> AskOption | None:
    k, desc = (key or "").strip().upper(), _clean_opt_desc(raw_desc)
    if k not in _OPTION_KEYS or not desc:
        return None
    return AskOption(key=k, label=desc[:28].rstrip(), reply_text=full_reply.strip() if full_reply else f"{k}：{desc[:80]}")


def _parse_explicit_options(spec: str) -> list[AskOption]:
    out, seen = [], set()
    for idx, part in enumerate([p.strip() for p in re.split(r"[|\n；;]+", spec) if p.strip()][:5]):
        m = _EXPLICIT_ITEM_RE.match(part)
        k, desc = (m.group(1).upper(), m.group(2)) if m else (next((c for c in _OPTION_KEYS[idx:] if c not in seen), ""), part)
        if (opt := _make_option(k, desc)) and opt.key not in seen:
            seen.add(opt.key)
            out.append(opt)
    return out


def parse_ask_options(text: str, explicit_options: str = "") -> list[AskOption]:
    """解析 ask 卡片的候选项（显式 --options 优先，否则自动从正文提取 A..E 选项并防误判）。"""
    spec = (explicit_options or "").strip()
    low = spec.lower()
    if low in _DISABLED_SPECS:
        return []
    if spec and low not in _AUTO_SPECS:
        return _parse_explicit_options(spec)
    cleaned = _MORE_LINE_RE.sub("", _FENCED_BLOCK_RE.sub("", text or ""))
    by_key: dict[str, AskOption] = {}
    for line in cleaned.splitlines():
        s = line.strip()
        if not s or _HEADER_LINE_RE.match(s):
            continue
        hits = list(_INLINE_PREFIXED_RE.finditer(s))
        if len(hits) < 2:
            hits = list(_INLINE_COLON_RE.finditer(s))
        if len(hits) >= 2:
            for m in hits:
                if (opt := _make_option(m.group(1), m.group(2))) and opt.key not in by_key:
                    by_key[opt.key] = opt
            continue
        if (m_line := _LINE_OPTION_RE.match(s)) and (opt := _make_option(m_line.group(1), m_line.group(2))) and opt.key not in by_key:
            by_key[opt.key] = opt
    if len(by_key) >= 2 and "A" in by_key and "B" in by_key:
        return [by_key[k] for k in _OPTION_KEYS if k in by_key]
    if low in _AUTO_SPECS:
        if (mq := _QUOTE_CMD_RE.search(cleaned)) and (phrase := _clean_opt_desc(mq.group(1))):
            return [AskOption("A", phrase[:28], phrase), AskOption("B", "暂缓执行", "B：暂缓执行")]
        return [AskOption("A", "同意 / 确认", "A：同意"), AskOption("B", "暂缓 / 拒绝", "B：暂缓")]
    return []


def _infer_header_meta(text: str, topic_id: str, title: str) -> tuple[str, str]:
    first = (text or "").strip("\n").partition("\n")[0]
    tid = (topic_id or "").strip() or (m.group(1) if (m := _TOPIC_TAG_RE.search(first)) else "")
    ttl = (title or "").strip() or (_TOPIC_TAG_RE.sub("", first.split("·", 1)[1]).strip() if "·" in first else "")
    return tid, ttl


def _truncate_label(text: str, limit: int = 28) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _build_option_button(opt: AskOption, idx: int, *, topic_id: str, key: str, title: str) -> dict:
    val = {"sd_cb": "1", "kind": "option", "choice": opt.key, "label": opt.label, "reply_text": opt.reply_text, "topic_id": topic_id, "key": key, "title": title}
    return {"tag": "button", "element_id": f"sd_opt_{opt.key}", "text": {"tag": "plain_text", "content": _truncate_label(f"{opt.key} · {opt.label}")},
            "type": "primary" if idx == 0 else "default", "size": "medium", "width": "default", "behaviors": [{"type": "callback", "value": val}]}


def _build_options_column_set(opts: list[AskOption], *, topic_id: str, key: str, title: str) -> dict:
    return {
        "tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "margin": "4px 0px 0px 0px",
        "columns": [{"tag": "column", "width": "auto", "elements": [_build_option_button(opt, idx, topic_id=topic_id, key=key, title=title)]} for idx, opt in enumerate(opts)],
    }


def _build_other_form(*, topic_id: str, key: str, title: str) -> dict:
    submit_val = {"sd_cb": "1", "kind": "other", "topic_id": topic_id, "key": key, "title": title}
    inp = {"tag": "input", "name": "other_input", "element_id": "sd_inp_other", "placeholder": {"tag": "plain_text", "content": "补充说明或直接拍板..."}, "width": "fill", "required": True}
    btn = {"tag": "button", "name": "other_submit", "element_id": "sd_btn_other", "text": {"tag": "plain_text", "content": "提交"}, "type": "primary", "size": "medium", "width": "default", "form_action_type": "submit", "value": submit_val, "behaviors": [{"type": "callback", "value": submit_val}]}
    row = {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "vertical_align": "center",
           "columns": [{"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": [inp]}, {"tag": "column", "width": "auto", "vertical_align": "center", "elements": [btn]}]}
    return {"tag": "form", "name": "sd_other_form", "element_id": "sd_form_other", "direction": "vertical", "vertical_spacing": "small", "elements": [row]}


def _coerce_options(options, text: str = "", options_spec: str = "") -> list[AskOption]:
    if options is None:
        return parse_ask_options(text, options_spec or "auto") or [AskOption("A", "同意 / 确认", "A：同意"), AskOption("B", "暂缓 / 拒绝", "B：暂缓")]
    out: list[AskOption] = []
    for idx, item in enumerate(options):
        if isinstance(item, AskOption):
            out.append(item)
        elif isinstance(item, dict):
            k = str(item.get("key") or item.get("code") or item.get("choice") or _OPTION_KEYS[idx % len(_OPTION_KEYS)]).strip().upper()
            lbl = str(item.get("label") or item.get("text") or "").strip()
            out.append(AskOption(k, lbl[:28], str(item.get("reply_text") or f"{k}：{lbl}").strip()))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            k, lbl = str(item[0]).strip().upper(), str(item[1]).strip()
            out.append(AskOption(k, lbl[:28], str(item[2]).strip() if len(item) > 2 else f"{k}：{lbl}"))
    return out


def build_interactive_elements(
    options=None, *, topic_id: str = "", key: str = "", title: str = "", text: str = "", options_spec: str = ""
) -> list[dict]:
    """构造紧凑 Card JSON 2.0 交互元素列表：[hr, 选项流式 column_set, 同行输入与提交 form]。"""
    opts = _coerce_options(options, text=text, options_spec=options_spec)
    els: list[dict] = [{"tag": "hr"}]
    if opts:
        els.append(_build_options_column_set(opts, topic_id=topic_id, key=key, title=title))
    els.append(_build_other_form(topic_id=topic_id, key=key, title=title))
    return els


def attach_ask_interactions(
    card: dict, *, text: str, summary: str = "", options_spec: str = "",
    topic_id: str = "", key: str = "", title: str = "", interactive: bool = False,
) -> dict:
    """向 ask 卡片或版本汇报卡片追加紧凑流式 column_set 交互控件（超 28KB 自动回退原卡）。"""
    spec = (options_spec or "").strip()
    if spec.lower() in _DISABLED_SPECS:
        return card
    if (text or "").strip("\n").startswith(("🐕 汇报 · 新版提醒", "🐕 汇报 · 版本与更新状态", "🐕 汇报 · 升级完成")):
        return attach_version_interactions(card, text)
    opts = parse_ask_options(f"{summary}\n{text}" if summary else text, spec)
    if not (bool(spec) or (interactive and bool(opts))):
        return card
    opts = opts or [AskOption("A", "同意 / 确认", "A：同意"), AskOption("B", "暂缓 / 拒绝", "B：暂缓")]
    tid, ttl = _infer_header_meta(text, topic_id, title)
    new_card = copy.deepcopy(card)
    new_card.setdefault("body", {}).setdefault("elements", []).extend(build_interactive_elements(opts, topic_id=tid, key=key, title=ttl))
    return new_card if card_fits(new_card) else card


def build_answered_owner_card(
    original_text: str, *, chosen_display: str, topic_id: str = "",
    answered_at: str = "", at_open_id: str = "", summary: str = "",
) -> dict:
    """构造原地回写的绿头『✅ 已拍板』卡片（保留原 header、白话总结与 more 面板，移除全部交互控件）。"""
    base_text = (original_text or "").strip("\n") or f"🐕 需要你定 [{topic_id or 'tp_bus'}]\n（原提问卡片）"
    card = build_owner_card(base_text, at_open_id=at_open_id, summary=summary)
    card.setdefault("header", {"title": {"tag": "plain_text", "content": "🐕 需要你定"}})
    card["header"]["template"] = "green"
    ts = answered_at or now_iso()
    card.setdefault("body", {}).setdefault("elements", []).extend([{"tag": "hr"}, {"tag": "markdown", "content": f"✅ **已拍板：{chosen_display}**（{ts} → `{topic_id}`）"}])
    return card


def is_card_action_event(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    hdr = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    return raw.get("type") == "card.action.trigger" or raw.get("event_type") == "card.action.trigger" or hdr.get("event_type") == "card.action.trigger" or "action_tag" in raw


def _parse_json_dict(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _resolve_cb_time(raw_ts: str, tz: str = "+08:00") -> str:
    s = (raw_ts or "").strip()
    if not s:
        return now_iso()
    if "T" in s or "-" in s:
        return s if "T" in s else s.replace(" ", "T") + tz
    try:
        val = int(s)
        sec = val / 1000.0 if val > 10_000_000_000 else float(val)
        sign = -1 if tz.startswith("-") else 1
        hh, _, mm = tz.lstrip("+-").partition(":")
        offset = timezone(sign * timedelta(hours=int(hh or 8), minutes=int(mm or 0)))
        return datetime.fromtimestamp(sec, offset).replace(second=0, microsecond=0).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return now_iso()


def _extract_action_choice(action_tag: str, val: dict, form_val: dict, input_val) -> tuple[str, str, str] | None:
    if form_val or val.get("kind") == "other":
        other = str(form_val.get("other_input") or (input_val if input_val is not None else "") or val.get("other_input") or "").strip()
        return ("other", f"[卡片回调] 其他：{other}", f"其他：{other}") if other else None
    if action_tag == "input" or (input_val is not None and not (val.get("choice") or val.get("option") or val.get("reply_text"))):
        other = str(input_val or "").strip()
        return ("other", f"[卡片回调] 其他：{other}", f"其他：{other}") if other else None
    choice, label = str(val.get("choice") or val.get("option") or "").strip(), str(val.get("label") or "").strip()
    reply_text = str(val.get("reply_text") or (f"{choice}：{label}" if (choice and label) else choice)).strip()
    if not choice and not reply_text:
        return None
    disp = f"{choice} · {label}" if (choice and label) else (choice or reply_text)
    return (choice or "option"), f"[卡片回调] {reply_text}", disp


def _unpack_raw_event(raw: dict, tz: str) -> tuple[str, str, str, str, str, dict, dict, object, str]:
    hdr = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    ev = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    ctx = ev.get("context") if isinstance(ev.get("context"), dict) else (raw.get("context") if isinstance(raw.get("context"), dict) else {})
    op = ev.get("operator") if isinstance(ev.get("operator"), dict) else (raw.get("operator") if isinstance(raw.get("operator"), dict) else {})
    act = ev.get("action") if isinstance(ev.get("action"), dict) else (raw.get("action") if isinstance(raw.get("action"), dict) else {})
    event_id = str(raw.get("event_id") or hdr.get("event_id") or ev.get("event_id") or "").strip()
    card_mid = str(raw.get("message_id") or raw.get("open_message_id") or ctx.get("open_message_id") or ev.get("open_message_id") or ev.get("message_id") or "").strip()
    chat_id = str(raw.get("chat_id") or raw.get("open_chat_id") or ctx.get("open_chat_id") or ev.get("open_chat_id") or ev.get("chat_id") or "").strip()
    operator_id = str(raw.get("operator_id") or op.get("open_id") or op.get("operator_id") or op.get("user_id") or raw.get("sender_id") or "").strip()
    action_tag = str(raw.get("action_tag") or act.get("tag") or "").strip()
    val = _parse_json_dict(raw.get("action_value") if raw.get("action_value") is not None else act.get("value"))
    form_val = _parse_json_dict(raw.get("form_value") if raw.get("form_value") is not None else act.get("form_value"))
    input_val = raw.get("input_value") if raw.get("input_value") is not None else act.get("input_value")
    create_time = _resolve_cb_time(str(raw.get("timestamp") or raw.get("create_time") or hdr.get("create_time") or ""), tz)
    digest = event_id or hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
    return f"sd_card_cb_{digest}", card_mid, chat_id, operator_id, action_tag, val, form_val, input_val, create_time


def _parse_item_int(raw_no) -> int | None:
    m = re.search(r"(\d+)", str(raw_no)) if raw_no is not None else None
    return int(m.group(1)) if m else None


def resolve_callback_target(store, open_message_id: str = "", value: dict | None = None, *, message_id: str = "") -> dict:
    """解析卡片回调归属目标：支持私聊提问、聚合台镜像卡、sends/bot_outbox 与话题群 tracked_items（T-n）。"""
    val = _parse_json_dict(value)
    card_mid = str(open_message_id or message_id or val.get("open_message_id") or val.get("message_id") or "").strip()
    origin_mid = str(val.get("origin_mid") or val.get("message_id") or "").strip()
    esc = (store.get_escalation(origin_mid) if origin_mid and hasattr(store, "get_escalation") else None) or (
        store.get_escalation(card_mid) if card_mid and hasattr(store, "get_escalation") else None
    )
    canon_mid = str(esc["message_id"]) if esc is not None and esc["message_id"] else (origin_mid or card_mid)
    send_row = (store.send_by_message(canon_mid) or store.send_by_message(card_mid)) if card_mid and hasattr(store, "send_by_message") else None
    out_row = (store.get_bot_outbox(canon_mid) or store.get_bot_outbox(card_mid)) if card_mid and hasattr(store, "get_bot_outbox") else None
    orig_msg = (store.get_message(canon_mid) or store.get_message(card_mid)) if card_mid and hasattr(store, "get_message") else None
    item = store.item_by_root(canon_mid) if canon_mid and hasattr(store, "item_by_root") else None
    if item is None and esc is not None and "origin_root_id" in esc.keys() and esc["origin_root_id"] and hasattr(store, "item_by_root"):
        item = store.item_by_root(esc["origin_root_id"])
    if item is None and send_row is not None and send_row["root_message_id"] and hasattr(store, "item_by_root"):
        item = store.item_by_root(send_row["root_message_id"])
    if item is None and orig_msg is not None:
        item = (store.item_by_thread(orig_msg["thread_id"]) if orig_msg["thread_id"] and hasattr(store, "item_by_thread") else None) or (store.item_by_root(orig_msg["root_id"]) if orig_msg["root_id"] and hasattr(store, "item_by_root") else None)
    if item is None and (ino := _parse_item_int(val.get("item_no"))) is not None and hasattr(store, "get_item"):
        item = store.get_item(ino)
    topic_id = ((esc["topic_id"] if esc is not None and esc["topic_id"] else "") or (send_row["topic_id"] if send_row is not None and send_row["topic_id"] else "") or (out_row["topic_id"] if out_row is not None and out_row["topic_id"] else "") or str(val.get("topic_id") or "").strip() or (item["owner_topic"] if item is not None and item["owner_topic"] else ""))
    item_no = item["item_no"] if item is not None else _parse_item_int(val.get("item_no"))
    return {"topic_id": topic_id, "owner_topic_id": (item["owner_topic"] if item is not None and item["owner_topic"] else topic_id), "key": (send_row and send_row["key"]) or str(val.get("key") or "").strip(), "title": str(val.get("title") or (item["title"] if item is not None else "") or "").strip(), "open_message_id": card_mid, "card_message_id": canon_mid, "item_no": item_no, "escalation": esc, "send": send_row, "outbox": out_row, "orig_message": orig_msg, "item": item}


def record_card_callback_to_ledger(store, msg_or_event, *, target: dict | None = None, value: dict | None = None, cfg=None, source=None, route: str = "dispatch", reason: str = "owner_reply", received_by: str = "bot") -> Message | None:
    """将卡片回调归一化消息落盘 messages 表，命中话题群 T-n 时自动注入 item:T-n 标签。"""
    msg = process_card_action_event(msg_or_event, cfg=cfg, store=store, source=source) if isinstance(msg_or_event, dict) else msg_or_event
    if msg is None:
        return None
    cb = (msg.raw.get("_sd_card_callback") if isinstance(msg.raw, dict) else None) or {}
    val = _parse_json_dict(value)
    tgt = target or resolve_callback_target(store, msg.reply_to or cb.get("card_message_id", ""), val or cb)
    item_no = _parse_item_int(tgt.get("item_no") or val.get("item_no") or cb.get("item_no"))
    topic_id = str(tgt.get("topic_id") or cb.get("topic_id") or val.get("topic_id") or "").strip()
    tags = ["owner_verified", "card_callback", *([f"escalation:{tgt['escalation']['message_id']}"] if tgt.get("escalation") and tgt["escalation"]["message_id"] else []), *([f"item:T-{item_no}"] if item_no is not None else [])]
    store.upsert_message(msg, route, reason, tags, topic_id=topic_id or None, received_by=received_by)
    return msg


def _handle_tg_move_callback(val: dict, *, cb_msg_id: str, card_mid: str, chat_id: str, chat_type: str, operator_id: str, create_time: str, raw: dict, cfg, store, source) -> Message:
    from . import duty, topicgroup as tg
    key, topic = str(val.get("key") or "").strip(), str(val.get("topic_id") or "").strip()
    dm_root = str(val.get("dm_root_message_id") or val.get("root_message_id") or "").strip()
    thr = (store.thread_by_key(topic, duty.norm_key(key)) if (topic and key) else None) or (store.thread_by_root(dm_root or card_mid) if (dm_root or card_mid) else None)
    if thr is not None:
        topic, dm_root, key = topic or thr["topic_id"], dm_root or thr["root_message_id"], key or (thr["key"] or "")
    title = str(val.get("title") or (thr["title"] if thr is not None else "") or key or "（无标题）").strip()
    item = (tg.find_by_key(store, key) if key else None) or (store.item_by_dm_root(dm_root) if dm_root else None)
    tg_chat = getattr(getattr(cfg, "topic_group", None), "chat_id", "")
    if item is None or item["state"] not in (tg.ACTIVE, tg.DONE):
        item = tg.open_item(store, title, topic or "tp_bus", "owner", tg.ORIGIN_MOVE, tg_chat, key=key, topic_id=topic or "tp_bus", dm_root=dm_root)
        if source is not None and hasattr(source, "send_to_chat") and tg_chat:
            try:
                item = tg.ensure_title(cfg, store, source, item, "", key)
            except Exception as e:
                log.warning("卡片一键放进话题群发标题卡失败: %s", e)
    no = tg.label(item["item_no"])
    cb_meta = {"kind": "tg_move", "topic_id": topic or item["owner_topic"] or "tp_bus", "key": key, "item_no": item["item_no"], "card_message_id": card_mid or dm_root}
    return Message(message_id=cb_msg_id, chat_id=chat_id, chat_name=store.known_chat_name(chat_id) if chat_id else "", chat_type=chat_type, sender_id=operator_id, sender_name="", sender_type="user", content=f"[卡片回调] 放进话题群：{no}", msg_type="text", create_time=create_time, reply_to=card_mid or dm_root, raw=dict(raw, _sd_card_callback=cb_meta))


def _handle_version_callback(val: dict, *, cb_msg_id: str, card_mid: str, chat_id: str, chat_type: str, operator_id: str, create_time: str, raw: dict, tgt: dict, store, source) -> Message | None:
    act = str(val.get("sd_action") or "").strip()
    if act in ("version_upgrade", "version_dismiss", "version_rollback") and card_mid:
        if store.conn.execute("SELECT 1 FROM messages WHERE reply_to = ? AND reason IN ('upgrade_command', 'version_dismiss', 'version_rollback') LIMIT 1", (card_mid,)).fetchone() is not None:
            dup_m = Message(message_id=cb_msg_id, chat_id=chat_id, chat_name="", chat_type=chat_type, sender_id=operator_id, sender_name="", sender_type="user", content=str(val.get("label") or act), msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw))
            store.upsert_message(dup_m, "drop", "card_callback_duplicate", ["card_callback"], topic_id="tp_bus", received_by="bot")
            return None
    send_row, out_row, orig_msg = tgt["send"], tgt["outbox"], tgt["orig_message"]
    orig_text = ((send_row and send_row["text"]) or (out_row and out_row["text"]) or (orig_msg and plain_text(orig_msg["content"])) or "🐕 汇报 · 新版提醒 [tp_bus]\n（版本提醒卡片）")
    if act == "version_query":
        cb_meta = {"kind": "version_query", "topic_id": "tp_bus", "card_message_id": card_mid}
        return Message(message_id=cb_msg_id, chat_id=chat_id, chat_name="", chat_type=chat_type, sender_id=operator_id, sender_name="", sender_type="user", content="有没有 beta", msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw, _sd_card_callback=cb_meta))
    if act == "version_dismiss":
        chosen = f"{val.get('label') or '⏸ 暂不升级'}（不强制）"
        cb_meta = {"kind": "version_dismiss", "topic_id": "tp_bus", "card_message_id": card_mid, "chosen_display": chosen}
        content = chosen
    elif act == "version_rollback":
        target_sha = str(val.get("target_sha") or "").strip()
        target_ver = str(val.get("target_version") or "").strip()
        content = "回滚"
        chosen = f"{val.get('label') or (f'⏪ 回滚至 {target_ver}' if target_ver else '⏪ 回滚至上一版')}（已提交后台执行）"
        cb_meta = {"kind": "version_rollback", "topic_id": "tp_bus", "card_message_id": card_mid, "channel": "rollback", "rollback": True, "target_sha": target_sha, "target_version": target_ver, "chosen_display": chosen}
    else:
        channel = "beta" if val.get("channel") == "beta" else "stable"
        no_pull, target_ver = bool(val.get("no_pull", False)), str(val.get("target_version") or "")
        content = "重启生效" if no_pull else ("升级 beta" if channel == "beta" else "升级稳定版")
        chosen = f"{val.get('label') or content}（已提交后台执行）"
        cb_meta = {"kind": "version_upgrade", "topic_id": "tp_bus", "card_message_id": card_mid, "channel": channel, "no_pull": no_pull, "target_version": target_ver, "chosen_display": chosen}
    if source is not None and hasattr(source, "patch_card") and card_mid:
        try:
            source.patch_card(card_mid, build_answered_owner_card(orig_text, chosen_display=chosen, topic_id="tp_bus", answered_at=create_time), identity="bot")
        except Exception as e:
            log.warning("回写版本卡片失败（%s）: %s", card_mid, e)
    return Message(message_id=cb_msg_id, chat_id=chat_id, chat_name="", chat_type=chat_type, sender_id=operator_id, sender_name="", sender_type="user", content=content, msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw, _sd_card_callback=cb_meta))


def process_card_action_event(raw: dict, *, cfg, store, source) -> Message | None:
    """处理飞书 card.action.trigger 事件：双层幂等、主人校验、双锚点路由、羊圈槽位复活、绿卡回写与归一化 Message。"""
    if not is_card_action_event(raw):
        return None
    cb_msg_id, card_mid, chat_id, operator_id, action_tag, val, form_val, input_val, create_time = _unpack_raw_event(raw, getattr(cfg, "timezone_offset", "+08:00"))
    if store.get_message(cb_msg_id) is not None:
        return None
    tgt = resolve_callback_target(store, card_mid, val)
    esc, send_row, out_row, orig_msg, item = tgt["escalation"], tgt["send"], tgt["outbox"], tgt["orig_message"], tgt["item"]
    esc_chats = getattr(getattr(cfg, "escalation", None), "chat_ids", None) or []
    tg_chat = getattr(getattr(cfg, "topic_group", None), "chat_id", "")
    resolved_chat_id = (chat_id or (esc["chat_id"] if esc is not None and esc["chat_id"] else "")
                        or (orig_msg["chat_id"] if orig_msg is not None and orig_msg["chat_id"] else "")
                        or (tg_chat if item is not None and tg_chat else "")
                        or getattr(getattr(cfg, "threads", None), "owner_chat_id", "") or (esc_chats[0] if esc_chats else ""))
    resolved_chat_type = (orig_msg["chat_type"] if orig_msg is not None and orig_msg["chat_type"] else "") or ("group" if resolved_chat_id and resolved_chat_id == tg_chat else "p2p")
    if not getattr(cfg, "self_open_id", "") or operator_id != cfg.self_open_id:
        drop_m = Message(message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name="", chat_type=resolved_chat_type,
                         sender_id=operator_id, sender_name="", sender_type="user", content="[卡片回调] 非主人操作",
                         msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw))
        store.upsert_message(drop_m, "drop", "card_callback_non_owner", ["card_callback", f"operator:{operator_id}"], received_by="bot")
        return None
    if val.get("sd_action") in ("version_upgrade", "version_dismiss", "version_query", "version_rollback"):
        return _handle_version_callback(val, cb_msg_id=cb_msg_id, card_mid=card_mid, chat_id=resolved_chat_id,
                                        chat_type=resolved_chat_type, operator_id=operator_id, create_time=create_time,
                                        raw=raw, tgt=tgt, store=store, source=source)
    if (val.get("kind") or val.get("action") or val.get("cmd")) == "tg_move":
        return _handle_tg_move_callback(val, cb_msg_id=cb_msg_id, card_mid=card_mid, chat_id=resolved_chat_id,
                                        chat_type=resolved_chat_type, operator_id=operator_id, create_time=create_time,
                                        raw=raw, cfg=cfg, store=store, source=source)
    extracted = _extract_action_choice(action_tag, val, form_val, input_val)
    target_topic_id = tgt["topic_id"]
    if extracted is None or not card_mid or not target_topic_id:
        return None
    choice_key, reply_content, chosen_display = extracted
    canon_mid = str(esc["message_id"]) if esc is not None and esc["message_id"] else str(tgt.get("card_message_id") or card_mid)
    orig_text = (send_row and send_row["text"]) or (esc and esc["text"]) or (out_row and out_row["text"]) or f"🐕 需要你定 · {tgt['title'] or '卡片提问'} [{target_topic_id}]\n{reply_content}"
    prev_cb = store.conn.execute(
        "SELECT content, create_time, dispatch_state FROM messages WHERE reply_to IN (?, ?) AND route = 'dispatch' ORDER BY create_time DESC, rowid DESC LIMIT 1",
        (canon_mid, card_mid),
    ).fetchone()
    is_dup = False
    if prev_cb is not None:
        if prev_cb["content"] == reply_content:
            is_dup = True
        elif prev_cb["dispatch_state"] == "pending" and raw.get("timestamp"):
            try:
                is_dup = 0 <= (datetime.fromisoformat(create_time) - datetime.fromisoformat(prev_cb["create_time"])).total_seconds() <= 3
            except Exception:
                is_dup = False
    elif esc is not None and (esc["answered_at"] or esc["closed_at"]):
        is_dup = True
    if is_dup:
        if prev_cb is None and esc is not None:
            from . import decision_hub
            decision_hub.sync_escalation_dual_cards(cfg, store, source, canon_mid, chosen_display=chosen_display, topic_id=target_topic_id, answered_at=esc["answered_at"] or create_time, orig_text=orig_text)
        dup_m = Message(message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name="", chat_type=resolved_chat_type,
                        sender_id=operator_id, sender_name="", sender_type="user", content=reply_content,
                        msg_type="text", create_time=create_time, reply_to=canon_mid, raw=dict(raw))
        store.upsert_message(dup_m, "drop", "card_callback_duplicate", ["card_callback", "card_cb_dup"], topic_id=target_topic_id, received_by="bot")
        return None
    t_row = store.get_topic(target_topic_id)
    if t_row is not None and t_row["state"] == "closed":
        store.update_topic(target_topic_id, state="active")
    if esc is None:
        esc_at = (send_row and send_row["sent_at"]) or (out_row and out_row["sent_at"]) or create_time
        store.add_escalation(canon_mid, target_topic_id, resolved_chat_id, orig_text, esc_at)
        if out_row is None and hasattr(store, "add_bot_outbox"):
            store.add_bot_outbox(canon_mid, target_topic_id, True, orig_text, esc_at)
    from . import decision_hub
    canon_mid = decision_hub.sync_escalation_dual_cards(
        cfg, store, source, canon_mid, chosen_display=chosen_display, topic_id=target_topic_id, answered_at=create_time, orig_text=orig_text, answer_mid=cb_msg_id
    )
    green_card = build_answered_owner_card(orig_text, chosen_display=chosen_display, topic_id=target_topic_id, answered_at=create_time)
    resolved_thread_id = str(raw.get("thread_id") or (orig_msg and orig_msg["thread_id"]) or (item and item["thread_id"]) or "").strip()
    if not resolved_thread_id and send_row is not None and send_row["root_message_id"]:
        thr = store.thread_by_root(send_row["root_message_id"])
        resolved_thread_id = thr["thread_id"] if thr is not None and thr["thread_id"] else ""
    cb_meta = {"topic_id": target_topic_id, "key": tgt["key"], "card_message_id": canon_mid, "choice": choice_key, "chosen_display": chosen_display, "card": {"type": "raw", "data": green_card}}
    if tgt["item_no"] is not None:
        cb_meta["item_no"] = tgt["item_no"]
    return Message(message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name=store.known_chat_name(resolved_chat_id) if resolved_chat_id else "",
                   chat_type=resolved_chat_type, sender_id=operator_id, sender_name="", sender_type="user",
                   content=reply_content, msg_type="text", create_time=create_time, reply_to=canon_mid,
                   thread_id=resolved_thread_id, raw=dict(raw, _sd_card_callback=cb_meta))
