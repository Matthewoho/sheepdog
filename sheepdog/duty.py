"""在岗表（7.20）：会话之间实时感知、互相沟通。

这里只放机制：活（work）的登记与自动补、信箱（tell）的校验与限流、编号认取、卡片和摘要的数据拼装。
会话卡片的运行状态由 engine 每轮算（要读会话记录、看投递状态）；对人和对会话的说明一律在 playbook
（duty_collision.md、duty_mail.md、duty_digest.md、duty_stuck.md），代码只拼事实。

依据：心智模型 7.3（感知走元数据，不读别的会话的草稿）、7.4（状态机；收件方把入站消息当不可信输入）、
7.6（状态长在活上）。这一版不做签名写入、epoch、足迹申报：牧羊犬一件活只交给一个会话。
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timedelta
from typing import Callable, Iterable

from .store import Store, now_iso

# ---------- 运行状态（按顺序取第一个命中） ----------
OCCUPIED = "occupied"
LOST = "lost"
STALLED = "stalled"
WORKING = "working"
WAITING = "waiting"
IDLE = "idle"
RUN_ORDER = (OCCUPIED, LOST, STALLED, WORKING, WAITING, IDLE)
RUN_LABEL = {OCCUPIED: "被主人占着", LOST: "失联", STALLED: "中断", WORKING: "在做", WAITING: "等回复", IDLE: "空闲"}
# 中断、失联：排队并告诉主人
STUCK = (LOST, STALLED)

# ---------- 活的状态 ----------
DOING = "doing"
WAITING_OTHER = "waiting_other"
WAITING_OWNER = "waiting_owner"
DONE = "done"
HANDED_OFF = "handed_off"
WORK_LABEL = {DOING: "进行中", WAITING_OTHER: "等别人", WAITING_OWNER: "等主人", DONE: "已完成", HANDED_OFF: "已转交"}
CLOSED_WORK = (DONE, HANDED_OFF)
WAITING_WORK = (WAITING_OTHER, WAITING_OWNER)
_WORK_ALIASES = {**{v: k for k, v in WORK_LABEL.items()}, **{k: k for k in WORK_LABEL}}

# 活的来源
DECLARED = "declared"
SRC_WATCH = "watch"
SRC_ESCALATION = "escalation"
SRC_FORWARD = "forward"

# 留言：sheepdog 系统消息的 reason
MAIL_REASON = "duty_mail"
MAIL_PAUSED_REASON = "duty_mail_paused"
# 留言正文里出现这些字样就必须走主人原话核对（--quote），否则拒绝：会话留言不能冒充主人
OWNER_CLAIM_WORDS = ("主人原话", "已核对")
OWNER_NAME_FIELD = "主人"


class DutyError(ValueError):
    pass


def norm_key(key: str) -> str:
    """钥匙比较用：去掉所有空白、转大写（PR #12 和 pr#12 是同一把钥匙）。"""
    return re.sub(r"\s+", "", key or "").upper()


def extract_keys(text: str, patterns: Iterable[re.Pattern]) -> list[str]:
    """按配置的编号规则从正文认编号：有命名分组 key 就取它，否则取整段命中；按 norm_key 去重，保持出现顺序。"""
    seen: dict[str, str] = {}
    for p in patterns:
        for m in p.finditer(text or ""):
            k = m.group("key") if "key" in p.groupindex and m.group("key") else m.group(0)
            k = " ".join(k.split())
            if k and norm_key(k) not in seen:
                seen[norm_key(k)] = k
    return list(seen.values())


def parse_work_state(s: str) -> str:
    v = _WORK_ALIASES.get((s or "").strip())
    if v is None:
        raise DutyError(f"--state 只能是 {' / '.join(f'{k}（{v}）' for k, v in WORK_LABEL.items())}，收到 {s!r}")
    return v


def short(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


# ======================= 活 =======================
def topic_label(store: Store, topic_id: str) -> str:
    t = store.get_topic(topic_id)
    return f"「{t['title']}」（{topic_id}）" if t is not None else topic_id


def holders(store: Store, key: str, exclude_topic: str = "") -> list:
    """这把钥匙登记在别的会话名下、还没结束的活。"""
    return [w for w in store.open_work_by_key(norm_key(key)) if w["topic_id"] != exclude_topic]


def _close_fields(state: str, result: str = "") -> dict:
    return {"closed_at": now_iso()} if state in CLOSED_WORK else {"closed_at": None}


def set_work(store: Store, topic_id: str, key: str, *, title: str = "", state: str = DOING, waiting_on: str = "",
             until: str = "", expect: str = "", to: str = "", result: str = "") -> tuple[int, list]:
    """会话申报一件活（sheepdog work set / 回执的 work 字段）：立即写入，不等下一轮。

    同一会话同一把钥匙已有就更新（申报覆盖自动补的；只改给了值的字段），没有就新建。
    返回 (活的 id, 这把钥匙在别的会话名下没结束的活)：撞车只提醒，仍然登记。"""
    key = " ".join((key or "").split())
    if not key:
        raise DutyError("--key 不能为空：有外部编号用外部编号，没有就起个短名")
    state = parse_work_state(state)
    if state == HANDED_OFF and not to:
        raise DutyError("已转交要写 --to <会话>")
    if to and not to.startswith("tp_"):
        to = "tp_" + to
    nk = norm_key(key)
    others = holders(store, key, topic_id)
    given = {k: v for k, v in dict(title=title, waiting_on=waiting_on, until=until, expect=expect, handed_to=to,
                                   result=result).items() if v}
    w = store.get_work(topic_id, nk)
    if w is None:
        wid = store.add_work(topic_id, key, nk, state, DECLARED, True, **given, **_close_fields(state))
    else:
        wid = w["id"]
        store.update_work(wid, key=key, state=state, declared=1, **given, **_close_fields(state))
    if state in CLOSED_WORK and (w is None or w["closed_at"] is None):
        record_closed(store, store.conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone())
    return wid, others


def done_work(store: Store, topic_id: str, key: str, result: str) -> int:
    """sheepdog work done：收掉名下的一件活。"""
    w = store.get_work(topic_id, norm_key(key))
    if w is None:
        raise DutyError(f"{topic_id} 名下没有钥匙 {key!r} 的活：先 work set，或者直接 work set --state done")
    if not (result or "").strip():
        raise DutyError("--result 不能为空：一句话写做成了什么")
    store.update_work(w["id"], state=DONE, result=result.strip(), declared=1, closed_at=w["closed_at"] or now_iso())
    if w["closed_at"] is None:
        record_closed(store, store.conn.execute("SELECT * FROM work_items WHERE id=?", (w["id"],)).fetchone())
    return w["id"]


def record_closed(store: Store, w) -> None:
    """做完 / 转交的活记进履历。"""
    if w is None:
        return
    what = WORK_LABEL.get(w["state"], w["state"])
    tail = f"→ {w['handed_to']}" if w["state"] == HANDED_OFF and w["handed_to"] else (w["result"] or "")
    store.add_history(w["topic_id"], "work", f"{w['key']} {short(w['title'] or '', 40)}（{what}）{tail}".strip())


def apply_receipt_work(store: Store, topic_id: str, receipt: dict, log=None) -> list[str]:
    """回执里的 work 字段（Codex 等命令行会话写不了账本，可以在回执块里带）：效果同 work set。
    格式：{"work": {...}} 或 {"work": [{...}, ...]}，字段 key title state waiting_on until expect to result。
    返回登记了哪些钥匙；不合格的条目跳过、记日志。"""
    items = receipt.get("work") if isinstance(receipt, dict) else None
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    done: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            set_work(store, topic_id, str(it.get("key") or ""), title=str(it.get("title") or ""),
                     state=str(it.get("state") or DOING), waiting_on=str(it.get("waiting_on") or ""),
                     until=str(it.get("until") or ""), expect=str(it.get("expect") or ""),
                     to=str(it.get("to") or ""), result=str(it.get("result") or ""))
            done.append(str(it.get("key")))
        except DutyError as e:
            if log is not None:
                log.warning("会话 %s 回执里的 work 条目不合格，跳过：%s（%s）", topic_id, e, json.dumps(it, ensure_ascii=False))
    return done


def auto_work(store: Store, topic_id: str, key: str, ref: str, source: str, state: str, **fields) -> None:
    """自动补活：同一会话已有这把钥匙的活（会话自己申报过，或者别的来源补过）就只补缺的字段，状态以已有的为准；
    没有就新建一件。fields 里空值不写。"""
    fields = {k: v for k, v in fields.items() if v}
    w = store.get_work(topic_id, norm_key(key))
    if w is None:
        store.add_work(topic_id, key, norm_key(key), state, source, False, source_ref=ref, **fields,
                       **_close_fields(state))
        if state in CLOSED_WORK:
            record_closed(store, store.get_work(topic_id, norm_key(key)))
        return
    if w["closed_at"] and not w["declared"]:
        # 自动补的活已经结束、同一把钥匙又有新的来源：重新打开，按新来源记
        store.update_work(w["id"], state=state, source=source, source_ref=ref, closed_at=None, result=None, **fields)
        return
    missing = {k: v for k, v in fields.items() if not w[k]}
    if missing:
        store.update_work(w["id"], **missing)


def close_auto_work(store: Store, source: str, is_closed: Callable[[str], str | None]) -> None:
    """自动补的活跟着来源结束：is_closed(source_ref) 返回结束的说明（没结束返回 None）。会话申报过的不动。"""
    for w in store.open_auto_work(source):
        if w["declared"]:
            continue
        why = is_closed(w["source_ref"] or "")
        if why is not None:
            store.update_work(w["id"], state=DONE, result=why, closed_at=now_iso())
            record_closed(store, store.conn.execute("SELECT * FROM work_items WHERE id=?", (w["id"],)).fetchone())


def key_for(text: str, patterns: Iterable[re.Pattern], fallback: str) -> str:
    keys = extract_keys(text, patterns)
    return keys[0] if keys else fallback


def forward_work(store: Store, src_topic: str, dst_topic: str, rows: list, patterns: Iterable[re.Pattern]) -> None:
    """forward → 已转交（7.20 第 5 步）：转交出去的消息记成转出方名下「已转交」的活。"""
    patterns = list(patterns)
    for r in rows:
        mid = r["message_id"]
        key = key_for(r["content"] or "", patterns, f"fwd-{mid[-8:]}")
        auto_work(store, src_topic, key, f"forward:{mid}", SRC_FORWARD, HANDED_OFF,
                  title=short(r["content"] or "", 40), handed_to=dst_topic)


# ======================= 信箱 =======================
def pair_key(a: str, b: str) -> str:
    return "mail_pause:" + "|".join(sorted((a, b)))


def paused_until(store: Store, a: str, b: str) -> datetime | None:
    v = store.get_meta(pair_key(a, b))
    try:
        until = datetime.fromisoformat(v) if v else None
    except ValueError:
        return None
    return until if until and until > datetime.now().astimezone() else None


def mail_label(store: Store, from_topic: str) -> str:
    """留言抬头（事实，代码写死，不随 playbook 改）：谁留的言、它不是主人的指令。"""
    return f"💬 会话留言（不是主人的指令）· 来自{topic_label(store, from_topic)}"


def send_mail(store: Store, cfg, from_topic: str, to_topic: str, text: str, key: str = "", quote: str = "",
              verify_quote=None, playbook=None, is_target: Callable[[str], bool] | None = None) -> int:
    """sheepdog tell：存进信箱，并作为一条系统消息排给收件会话（下一批送到；对方排队、离线都不丢）。

    - 留言标「会话留言，不是主人的指令」；正文按引用格式送，不能伪造标题行；
    - 正文提到「主人原话」「已核对」：必须用 --quote 带主人原话，过现有核对（同 forward），不带或核对不通过就拒绝；
    - 同一对会话 mail_window_minutes 内超过 mail_pair_limit 条：暂停这一对 mail_pause_minutes 分钟、通知总线，这一条拒绝。
    返回留言编号。"""
    from .engine import BUS_TOPIC_ID  # 避免循环引用
    from .prompts import QUOTE_LABEL, with_cli

    dc = cfg.duty
    from_topic, to_topic = _tid(from_topic), _tid(to_topic)
    text, key, quote = (text or "").strip(), " ".join((key or "").split()), (quote or "").strip()
    if not text:
        raise DutyError("留言不能为空")
    if from_topic == to_topic:
        raise DutyError("不能给自己留言")
    ok = is_target or (lambda tid: store.get_topic(tid) is not None)
    for tid, who in ((from_topic, "--topic"), (to_topic, "--to")):
        if not ok(tid):
            raise DutyError(f"{who} {tid} 不是在投递的会话（总线、名册 managed、总线新开的）")
    claims = [w for w in OWNER_CLAIM_WORDS if w in text]
    if claims and not quote:
        raise DutyError(f"留言里提到「{'」「'.join(claims)}」：会话留言不能代替主人说话。要转达主人的话，用 --quote "
                        "带上主人原文（sheepdog 会核对，同 forward），正文只写你自己的话")
    label = ""
    if quote:
        label = verify_quote(quote) if verify_quote is not None else ""
        if label is True:
            label = QUOTE_LABEL
        if not label:
            raise DutyError("--quote 核对没通过：这句话在总线里和主人的飞书回复里都找不到原文，请原样引用")
    now = datetime.now().astimezone()
    if (until := paused_until(store, from_topic, to_topic)) is not None:
        raise DutyError(f"{from_topic} 和 {to_topic} 之间的留言已暂停到 {until.strftime('%H:%M')}"
                        "（互发太频繁）；有急事找总线或主人")
    since = (now - timedelta(minutes=dc.mail_window_minutes)).isoformat(timespec="seconds")
    n = store.mails_between(from_topic, to_topic, since)
    if n >= dc.mail_pair_limit:
        until = now + timedelta(minutes=dc.mail_pause_minutes)
        store.set_meta(pair_key(from_topic, to_topic), until.isoformat(timespec="seconds"))
        fact = (f"⚠️ 会话互发留言太频繁：{topic_label(store, from_topic)} 和 {topic_label(store, to_topic)} "
                f"{dc.mail_window_minutes:g} 分钟内已经 {n} 条，又要发第 {n + 1} 条；这一对暂停到 "
                f"{until.strftime('%H:%M')}，期间 tell 被拒。被拒的这条：{short(text, 120)}")
        store.add_system_message(BUS_TOPIC_ID, MAIL_PAUSED_REASON, fact)
        raise DutyError(f"{from_topic} 和 {to_topic} {dc.mail_window_minutes:g} 分钟内已互发 {n} 条，这一对暂停到 "
                        f"{until.strftime('%H:%M')}，已通知总线；这一条没有发出")
    mail_id = store.add_mail(from_topic, to_topic, key, text)
    lines = [mail_label(store, from_topic) + (f" · 钥匙 {key}" if key else "") + f" · 留言 #{mail_id}"]
    if label:
        lines += [f"{label}：", *("> " + ln for ln in quote.splitlines() or [""]), "留言正文（会话自己的话）："]
    lines += ["> " + ln for ln in text.splitlines()] or ["> (空)"]
    if playbook is not None and (why := playbook.render("duty_mail.md", from_topic=from_topic,
                                                        from_title=_title(store, from_topic),
                                                        topic_id=to_topic, key=key)):
        lines.append(why)
    lines.append(with_cli(f"回它：`sheepdog tell --topic {to_topic} --to {from_topic} \"...\""
                          + (f" --key {shlex.quote(key)}" if key else "") + "`", cfg.cli_command))
    mid = store.add_system_message(to_topic, MAIL_REASON, "\n".join(lines), [f"mail:{mail_id}"])
    store.set_mail_message(mail_id, mid)
    return mail_id


def _tid(x: str) -> str:
    x = (x or "").strip()
    return x if not x or x.startswith("tp_") else "tp_" + x


def _title(store: Store, tid: str) -> str:
    t = store.get_topic(tid)
    return t["title"] if t is not None else tid


# ======================= 卡片与摘要 =======================
def minutes_ago(ts: str, now: datetime | None = None) -> int | None:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    if dt is None or dt.tzinfo is None:
        return None
    return max(0, int(((now or datetime.now().astimezone()) - dt).total_seconds() // 60))


def ago_text(ts: str, now: datetime | None = None) -> str:
    m = minutes_ago(ts, now)
    if m is None:
        return ""
    if m < 1:
        return "刚刚"
    if m < 120:
        return f"{m} 分钟前"
    return f"{m // 60} 小时前"


def work_line(w, with_owner: bool = False) -> str:
    parts = [w["key"]]
    if w["title"]:
        parts.append(short(w["title"], 40))
    head = " ".join(parts) + f" · {WORK_LABEL.get(w['state'], w['state'])}"
    extra = []
    if w["waiting_on"]:
        extra.append(f"等 {w['waiting_on']}")
    if w["until"]:
        extra.append(f"到 {w['until']}")
    if w["expect"]:
        extra.append(f"答应交出：{short(w['expect'], 40)}")
    if w["handed_to"]:
        extra.append(f"转给 {w['handed_to']}")
    if w["result"]:
        extra.append(f"结果：{short(w['result'], 40)}")
    if w["source"] and w["source"] != DECLARED and not w["declared"]:
        extra.append(f"牧羊犬按 {w['source']} 自动补")
    if with_owner:
        extra.append(f"会话 {w['topic_id']}")
    if not w["closed_at"] and (ago := ago_text(w["updated_at"] or "")):
        extra.append(f"更新于 {ago}")
    return head + ("（" + "；".join(extra) + "）" if extra else "")


def card_data(store: Store, t, card, now: datetime | None = None) -> dict:
    """一张会话卡（board / 摘要共用）：职责、运行状态、此刻动作、名下的活、最近做完的 3 件、未读留言数。"""
    now = now or datetime.now().astimezone()
    tid = t["topic_id"]
    work = store.open_work(tid)
    closed = store.closed_work(tid, 3)
    unread = store.unread_mail(tid)
    c = dict(card) if card is not None else {}
    state = c.get("run_state") or ""
    return {
        "topic_id": tid,
        "title": t["title"],
        "host": (t["host"] if "host" in t.keys() else None) or "antigravity",
        "duty": t["duty"] or "",
        "session_state": t["state"],
        "run_state": state,
        "run_label": RUN_LABEL.get(state, "（还没算，等下一轮）"),
        "spinning": bool(c.get("spinning")),
        "detail": c.get("detail") or "",
        "action": c.get("action") or "",
        "action_at": c.get("action_at") or "",
        "action_ago": ago_text(c.get("action_at") or "", now),
        "action_minutes": minutes_ago(c.get("action_at") or "", now),
        "compacted_at": c.get("compacted_at") or "",
        "queued": c.get("queued") or 0,
        "updated_at": c.get("updated_at") or "",
        "work": [dict(w) for w in work],
        "recent_done": [dict(w) for w in closed],
        "unread_mail": len(unread),
    }


def state_text(d: dict) -> str:
    s = d["run_label"] + ("（空转）" if d["spinning"] else "")
    return s + (f"：{d['detail']}" if d["detail"] else "")


def action_text(d: dict) -> str:
    if not d["action"]:
        return "—"
    return f"「{d['action']}」" + (f"（{d['action_ago']}）" if d["action_ago"] else "")


def render_board(cards: list[dict], now_label: str, compact: bool = False) -> str:
    lines = [f"在岗表 · {now_label}"]
    if not cards:
        return lines[0] + "\n（没有在投递的会话）"
    for d in cards:
        lines.append(f"━ 「{d['title']}」 {d['topic_id']} · {d['host']}")
        lines.append(f"  职责：{short(d['duty'], 80) or '-'}")
        lines.append(f"  运行状态：{state_text(d)}" + (f"；排队 {d['queued']} 条" if d["queued"] else ""))
        lines.append(f"  此刻动作：{action_text(d)}" + (f"；{d['compacted_label']}" if d.get("compacted_label") else ""))
        if d["work"]:
            lines.append("  名下的活：")
            lines += [f"    - {work_line(w)}" for w in d["work"]]
        else:
            lines.append("  名下的活：（无）")
        if d["recent_done"]:
            lines.append("  最近做完：")
            lines += [f"    - {work_line(w)}" for w in d["recent_done"]]
        else:
            lines.append("  最近做完：（无）")
        lines.append(f"  未读留言：{d['unread_mail']}")
        for k, label in (("recent_receipts", "最近回执"), ("recent_actions", "最近动作")):
            if d.get(k):
                lines.append(f"  {label}：")
                lines += [f"    - {x}" for x in d[k]]
    return "\n".join(lines)


def digest_line(d: dict) -> str:
    """总线摘要里一个会话一行（事实）。"""
    parts = [f"「{d['title']}」{d['topic_id']}", d["run_label"] + ("（空转）" if d["spinning"] else "")]
    if d["run_state"] in STUCK and d["detail"]:
        parts[-1] += f"（{short(d['detail'], 50)}）"
    if d["action"]:
        parts.append(f"「{short(d['action'], 40)}」{d['action_ago']}")
    if d["work"]:
        ws = [f"{w['key']}（{WORK_LABEL.get(w['state'], w['state'])}）" for w in d["work"][:3]]
        parts.append("活 " + "、".join(ws) + (f" 等 {len(d['work'])} 件" if len(d["work"]) > 3 else ""))
    if d["unread_mail"]:
        parts.append(f"未读留言 {d['unread_mail']}")
    return "- " + " · ".join(parts)


def digest_fingerprint(cards: list[dict]) -> str:
    """摘要只在在岗表变化时附：指纹不含「几分钟前」这类每轮都变的东西。"""
    data = [(d["topic_id"], d["run_state"], d["spinning"], d["action"],
             sorted((w["norm_key"], w["state"]) for w in d["work"]), d["unread_mail"]) for d in cards]
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def digest_lines(cards: list[dict], max_lines: int) -> list[str]:
    """按要紧程度排：失联 / 中断 / 被占 / 在做 / 等回复 / 空闲；最多 max_lines 行（超出的合成最后一行）。"""
    order = {s: i for i, s in enumerate(RUN_ORDER)}
    cards = sorted(cards, key=lambda d: (order.get(d["run_state"], len(order)), d["topic_id"]))
    lines = [digest_line(d) for d in cards]
    if len(lines) > max_lines:
        rest = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 1] + [f"- …还有 {rest} 个会话，`sheepdog board` 看全部"]
    return lines


def live_topics(store: Store) -> list:
    """在岗表列哪些会话：总线，加上名册 managed、总线新开的（没收掉的）。"""
    from .engine import BUS_TOPIC_ID, MANAGED_KINDS  # 避免循环引用
    bus = store.get_topic(BUS_TOPIC_ID)
    rest = sorted((t for t in store.list_topics() if t["kind"] in MANAGED_KINDS and t["state"] != "closed"),
                  key=lambda t: t["topic_id"])
    return ([bus] if bus is not None else []) + rest


def board_cards(store: Store, cfg, topic: str = "", exclude: Iterable[str] = (), history: bool = False) -> list[dict]:
    """sheepdog board 和总线摘要用的卡片。topic 给了就只看它（不在投递的会话也能看）。"""
    now = datetime.now().astimezone()
    if topic:
        t = store.get_topic(_tid(topic))
        topics = [t] if t is not None else []
    else:
        topics = live_topics(store)
    out = []
    for t in topics:
        if t["topic_id"] in exclude:
            continue
        d = card_data(store, t, store.get_card(t["topic_id"]), now)
        m = minutes_ago(d["compacted_at"], now)
        if m is not None and m <= cfg.duty.compacted_recent_minutes:
            d["compacted_label"] = f"刚被压缩（{ago_text(d['compacted_at'], now)}）"
        if history:
            d["recent_receipts"] = [f"{h['at']} {h['text']}" for h in store.history(t["topic_id"], "receipt", 3)]
            d["recent_actions"] = [f"{h['at']} {h['text']}" for h in store.history(t["topic_id"], "action", 5)]
        out.append(d)
    return out
