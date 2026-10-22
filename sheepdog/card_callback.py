"""飞书交互式拍板卡片（Card JSON 2.0 按钮 + 其他输入框）与 card.action.trigger 回调处理（7.73，REG-017 ~ REG-019）。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .card import build_owner_card, card_fits
from .models import Message
from .store import now_iso

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
    r"[\(\[（【]?([A-E])[\)\]）】]?(?:（[^）]{1,10}）|\([^)]{1,10}\))?(?:\*\*|__)?"
    r"\s*(?:[:：.、=·\-—]|(?<=[A-E])\s+)\s*(.+?)\s*$"
)
_INLINE_PREFIXED_RE = re.compile(
    r"(?:回(?:复)?|选|选项|方案)\s*[\(\[（]?([A-E])[\)\]）]?\s*(?:[:：.、=·\-—]\s*|\s+)([^，,；;\n。！!？?]{1,48})"
)
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


def _build_option_button(opt: AskOption, idx: int, *, topic_id: str, key: str, title: str) -> dict:
    val = {"sd_cb": "1", "kind": "option", "choice": opt.key, "label": opt.label,
           "reply_text": opt.reply_text, "topic_id": topic_id, "key": key, "title": title}
    return {"tag": "button", "element_id": f"sd_opt_{opt.key}",
            "text": {"tag": "plain_text", "content": f"{opt.key} · {opt.label[:24]}"},
            "type": "primary_filled" if idx == 0 else "default", "width": "fill",
            "behaviors": [{"type": "callback", "value": val}]}


def _build_other_form(*, topic_id: str, key: str, title: str) -> dict:
    inp = {"tag": "input", "name": "other_input", "element_id": "sd_inp_other",
           "placeholder": {"tag": "plain_text", "content": "其他：可输入自定义批示后点下方提交…"},
           "max_length": 1000, "width": "fill", "required": True}
    btn = {"tag": "button", "name": "other_submit", "element_id": "sd_btn_other",
           "text": {"tag": "plain_text", "content": "提交其他批示"},
           "type": "default", "width": "fill", "form_action_type": "submit",
           "value": {"sd_cb": "1", "kind": "other", "topic_id": topic_id, "key": key, "title": title}}
    return {"tag": "form", "name": "sd_other_form", "element_id": "sd_form_other",
            "direction": "vertical", "vertical_spacing": "small", "elements": [inp, btn]}


def attach_ask_interactions(
    card: dict, *, text: str, summary: str = "", options_spec: str = "",
    topic_id: str = "", key: str = "", title: str = "", interactive: bool = False,
) -> dict:
    """向 ask 卡片根节点追加选项按钮与『其他：可输入』form 容器（超 28KB 自动回退原卡）。"""
    spec = (options_spec or "").strip()
    if spec.lower() in _DISABLED_SPECS:
        return card
    opts = parse_ask_options(f"{summary}\n{text}" if summary else text, spec)
    if not (bool(spec) or (interactive and bool(opts))):
        return card
    if not opts:
        opts = [AskOption("A", "同意 / 确认", "A：同意"), AskOption("B", "暂缓 / 拒绝", "B：暂缓")]
    tid, ttl = _infer_header_meta(text, topic_id, title)
    new_card = copy.deepcopy(card)
    els = new_card.setdefault("body", {}).setdefault("elements", [])
    els.append({"tag": "hr"})
    els.extend(_build_option_button(opt, idx, topic_id=tid, key=key, title=ttl) for idx, opt in enumerate(opts))
    els.append(_build_other_form(topic_id=tid, key=key, title=ttl))
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
    els = card.setdefault("body", {}).setdefault("elements", [])
    els.extend([{"tag": "hr"}, {"tag": "markdown", "content": f"✅ **已拍板：{chosen_display}**（{ts} → `{topic_id}`）"}])
    return card


def is_card_action_event(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    hdr = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    return (
        raw.get("type") == "card.action.trigger"
        or raw.get("event_type") == "card.action.trigger"
        or hdr.get("event_type") == "card.action.trigger"
        or "action_tag" in raw
    )


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


def process_card_action_event(raw: dict, *, cfg, store, source) -> Message | None:
    """处理飞书 card.action.trigger 事件：双层幂等、主人校验、双锚点路由、羊圈槽位复活、绿卡回写与归一化 Message。"""
    if not is_card_action_event(raw):
        return None
    cb_msg_id, card_mid, chat_id, operator_id, action_tag, val, form_val, input_val, create_time = _unpack_raw_event(raw, getattr(cfg, "timezone_offset", "+08:00"))
    if store.get_message(cb_msg_id) is not None:
        return None
    esc = store.get_escalation(card_mid) if card_mid else None
    send_row = store.send_by_message(card_mid) if card_mid else None
    out_row = store.get_bot_outbox(card_mid) if card_mid else None
    orig_msg = store.get_message(card_mid) if card_mid else None
    esc_chats = getattr(getattr(cfg, "escalation", None), "chat_ids", None) or []
    resolved_chat_id = (
        chat_id or (esc["chat_id"] if esc is not None and esc["chat_id"] else "")
        or (orig_msg["chat_id"] if orig_msg is not None and orig_msg["chat_id"] else "")
        or getattr(getattr(cfg, "threads", None), "owner_chat_id", "") or (esc_chats[0] if esc_chats else "")
    )
    tg_chat = getattr(getattr(cfg, "topic_group", None), "chat_id", "")
    resolved_chat_type = (orig_msg["chat_type"] if orig_msg is not None and orig_msg["chat_type"] else "") or ("group" if resolved_chat_id and resolved_chat_id == tg_chat else "p2p")

    if cfg.self_open_id and operator_id != cfg.self_open_id:
        drop_m = Message(message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name="", chat_type=resolved_chat_type,
                         sender_id=operator_id, sender_name="", sender_type="user", content="[卡片回调] 非主人操作",
                         msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw))
        store.upsert_message(drop_m, "drop", "card_callback_non_owner", ["card_callback", f"operator:{operator_id}"], received_by="bot")
        return None
    extracted = _extract_action_choice(action_tag, val, form_val, input_val)
    if extracted is None or not card_mid:
        return None
    choice_key, reply_content, chosen_display = extracted
    target_topic_id = (
        (esc["topic_id"] if esc is not None and esc["topic_id"] else "")
        or (send_row["topic_id"] if send_row is not None and send_row["topic_id"] else "")
        or (out_row["topic_id"] if out_row is not None and out_row["topic_id"] else "")
        or str(val.get("topic_id") or "").strip()
    )
    if not target_topic_id:
        return None
    already_answered = bool(esc is not None and (esc["answered_at"] or esc["closed_at"]))
    has_dispatched = store.conn.execute("SELECT 1 FROM messages WHERE reply_to = ? AND route = 'dispatch' LIMIT 1", (card_mid,)).fetchone() is not None
    if already_answered or has_dispatched:
        dup_m = Message(message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name="", chat_type=resolved_chat_type,
                        sender_id=operator_id, sender_name="", sender_type="user", content=reply_content,
                        msg_type="text", create_time=create_time, reply_to=card_mid, raw=dict(raw))
        store.upsert_message(dup_m, "drop", "card_callback_duplicate", ["card_callback", "card_cb_dup"], topic_id=target_topic_id, received_by="bot")
        return None

    t_row = store.get_topic(target_topic_id)
    if t_row is not None and t_row["state"] == "closed":
        store.update_topic(target_topic_id, state="active")
    payload_title = str(val.get("title") or "").strip()
    orig_text = (send_row and send_row["text"]) or (esc and esc["text"]) or (out_row and out_row["text"]) or f"🐕 需要你定 · {payload_title or '卡片提问'} [{target_topic_id}]\n{reply_content}"
    if esc is None:
        esc_at = (send_row and send_row["sent_at"]) or (out_row and out_row["sent_at"]) or create_time
        if hasattr(store, "record_escalation"):
            store.record_escalation(message_id=card_mid, topic_id=target_topic_id, chat_id=resolved_chat_id, text=orig_text, created_at=esc_at)
        else:
            store.add_escalation(card_mid, target_topic_id, resolved_chat_id, orig_text, esc_at)
        if out_row is None and hasattr(store, "add_bot_outbox"):
            store.add_bot_outbox(card_mid, target_topic_id, True, orig_text, esc_at)
    if source is not None and hasattr(source, "patch_card"):
        try:
            source.patch_card(card_mid, build_answered_owner_card(orig_text, chosen_display=chosen_display, topic_id=target_topic_id, answered_at=create_time), identity="bot")
        except Exception as e:
            log.warning("回写已拍板卡片失败（%s）: %s", card_mid, e)

    resolved_key = (send_row and send_row["key"]) or str(val.get("key") or "").strip()
    resolved_thread_id = str(raw.get("thread_id") or (orig_msg and orig_msg["thread_id"]) or "").strip()
    if not resolved_thread_id and send_row is not None and send_row["root_message_id"]:
        thr = store.thread_by_root(send_row["root_message_id"])
        resolved_thread_id = thr["thread_id"] if thr is not None and thr["thread_id"] else ""
    raw_copy = dict(raw)
    raw_copy["_sd_card_callback"] = {"topic_id": target_topic_id, "key": resolved_key, "card_message_id": card_mid, "choice": choice_key, "chosen_display": chosen_display}
    return Message(
        message_id=cb_msg_id, chat_id=resolved_chat_id, chat_name=store.known_chat_name(resolved_chat_id) if resolved_chat_id else "",
        chat_type=resolved_chat_type, sender_id=operator_id, sender_name="", sender_type="user",
        content=reply_content, msg_type="text", create_time=create_time, reply_to=card_mid, thread_id=resolved_thread_id, raw=raw_copy,
    )
