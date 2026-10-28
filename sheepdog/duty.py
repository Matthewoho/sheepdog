"""在岗表（7.20）：会话之间实时感知、互相沟通（显示名「羊群看板」）。负责活（work）的登记与自动补、信箱（tell）的校验与限流、编号认取、卡片和摘要的数据拼装。"""
from __future__ import annotations
import json, re, shlex
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
# 总线摘要行数超了先砍谁：卡住的最后砍（显示仍按 RUN_ORDER）
KEEP_ORDER = (LOST, STALLED, OCCUPIED, WORKING, WAITING, IDLE)
# 卡片上最近一次工具调用的叫法：只有在做时它才是「此刻」的动作，其他状态下是过去的事
ACTION_NOW = "此刻动作"
ACTION_LAST = "最近动作"
# 找不到会话记录时，卡片上的动作写这句（不加引号：它不是会话的动作说明）
NO_RECORD_ACTION = "（找不到会话记录）"
# 总线摘要里空闲会话合成的那一行：每个标题最多几个字、整行最多几个字（超了用「等」收尾）
IDLE_TITLE_LIMIT = 16
IDLE_LINE_LIMIT = 120

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

# 活的来源。会话自报（declared）以外都是牧羊犬看事实补的（自动）；路由事实另有一张表（duty_routes），不进 work_items
DECLARED = "declared"
SRC_WATCH = "watch"
SRC_ESCALATION = "escalation"
SRC_FORWARD = "forward"
SRC_ROUTED = "routed"
SOURCE_LABEL = {DECLARED: "会话自报", SRC_WATCH: "自动·等人回复", SRC_ESCALATION: "自动·需要你定",
                SRC_FORWARD: "自动·转交", SRC_ROUTED: "自动·路由"}
STALE_MARK = "可能过期"

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


def _is_owner_wait(val: str) -> bool:
    return bool(re.search(r"^\s*(?:主人|matthew(?:\s+hong)?)(?:\s|$|[（(/:：,，-])", val or "", re.I))


def set_work(store: Store, topic_id: str, key: str, *, title: str = "", state: str = DOING, waiting_on: str = "",
             until: str = "", expect: str = "", to: str = "", result: str = "") -> tuple[int, list]:
    """会话申报一件活（sheepdog work set / 回执的 work 字段）：立即写入，已有则更新，没有则新建。"""
    key = " ".join((key or "").split())
    if not key:
        raise DutyError("--key 不能为空：有外部编号用外部编号，没有就起个短名")
    state = parse_work_state(state)
    from . import decision_hub
    if (state == WAITING_OWNER or _is_owner_wait(waiting_on)) and (decision_hub.hub_active(None, store) or getattr(store, "enforce_waiting_owner_ask", False)):
        if store.conn.execute("SELECT 1 FROM escalations WHERE (topic_id=? OR topic_id=?) AND closed_at IS NULL LIMIT 1",
                              (topic_id, decision_hub.base_topic_id(topic_id))).fetchone() is None:
            raise DutyError("当前会话没有未决的 ask 待拍板卡片，禁止将 work 标为等主人（waiting_owner / waiting_on=主人）；请先调用 sheepdog ask 发起待拍板卡片")
    if state == HANDED_OFF and not to:
        raise DutyError("已转交要写 --to <会话>")
    if to and not to.startswith("tp_"):
        to = "tp_" + to
    nk, others, ts = norm_key(key), holders(store, key, topic_id), now_iso()
    given = {k: v for k, v in dict(title=title, waiting_on=waiting_on, until=until, expect=expect, handed_to=to, result=result).items() if v}
    w = store.get_work(topic_id, nk)
    if w is None:
        wid = store.add_work(topic_id, key, nk, state, DECLARED, True, declared_at=ts, **given, **_close_fields(state))
    else:
        wid = w["id"]
        if state in (DOING, WAITING_OTHER, DONE, HANDED_OFF) and not waiting_on and _is_owner_wait(w["waiting_on"] or ""):
            given.setdefault("waiting_on", "")
            given.setdefault("expect", None)
        store.update_work(wid, key=key, state=state, declared=1, declared_at=ts, **given, **_close_fields(state))
    if state in CLOSED_WORK:
        # 会话自报做完 / 转交：这之前送到它的同一编号的消息不再算它手上的事（之后又送到就重新算）
        store.close_route(topic_id, nk, "会话自报已转交" if state == HANDED_OFF else "会话自报已完成", ts)
        if w is None or w["closed_at"] is None:
            record_closed(store, store.conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone())
    return wid, others


def done_work(store: Store, topic_id: str, key: str, result: str) -> int:
    """sheepdog work done：收掉名下的一件活。只有路由事实、没登记过的编号也可以 done（等于登记一件已完成的活）。"""
    w = store.get_work(topic_id, norm_key(key))
    if w is None and store.active_routes("", topic_id, norm_key(key)):
        if not (result or "").strip():
            raise DutyError("--result 不能为空：一句话写做成了什么")
        return set_work(store, topic_id, key, state=DONE, result=result.strip())[0]
    if w is None:
        raise DutyError(f"{topic_id} 名下没有钥匙 {key!r} 的活：先 work set，或者直接 work set --state done")
    if not (result or "").strip():
        raise DutyError("--result 不能为空：一句话写做成了什么")
    ts = now_iso()
    store.update_work(w["id"], state=DONE, result=result.strip(), declared=1, declared_at=ts,
                      closed_at=w["closed_at"] or ts)
    store.close_route(topic_id, norm_key(key), "会话自报已完成", ts)
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
    7.63：回执里的 anchors 带有 lamb:<id> 时，自动登记/更新母羊名下 key="lamb:<id>"、state="waiting_other" 的活。
    返回登记了哪些钥匙；不合格的条目跳过、记日志。"""
    if not isinstance(receipt, dict):
        return []
    done: list[str] = []
    items = receipt.get("work")
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
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
    anchors = receipt.get("anchors")
    if isinstance(anchors, list):
        for a in anchors:
            if isinstance(a, str) and a.startswith("lamb:"):
                lid = a.removeprefix("lamb:").strip()
                if not lid:
                    continue
                lb = store.get_lamb(lid)
                if lb is None:
                    lb = store.find_lamb_by_cid(lid)
                    if lb is not None:
                        lid = lb["lamb_id"]
                ttitle = (lb["task_title"] if lb else "") or str(receipt.get("summary") or lid)
                waiting_on = f"羔羊 {lid} ({ttitle})"
                try:
                    set_work(store, topic_id, f"lamb:{lid}", title=ttitle, state=WAITING_OTHER, waiting_on=waiting_on)
                    done.append(f"lamb:{lid}")
                except DutyError as e:
                    if log is not None:
                        log.warning("会话 %s 回执里的 lamb anchor %s 写进看板出错：%s", topic_id, a, e)
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


def release_owner_waits(store: Store, basis: Callable[[object], tuple[str, datetime] | None]) -> list[int]:
    """会话自报过「等主人」的活（declared，同一把钥匙上自动补的那部分已经被会话的申报接管），它等的那条「需要你定」已经关了
    （主人已答 / 会话已结案 / 过期），并且会话是在关闭之前申报的：这件活不再算等主人，改成进行中（不替会话结束它——会话自报的
    活由会话自己 done），履历记一笔。关闭以后会话又申报了「等主人」的，以新申报为准，不动。
    basis(w) 返回 (关闭说明, 关闭时间)；它等的提问还开着、或者找不到它等的是哪条提问，返回 None。返回改了哪些活。"""
    changed: list[int] = []
    for w in store.conn.execute("SELECT * FROM work_items WHERE closed_at IS NULL AND declared=1 AND state=? ORDER BY id",
                                (WAITING_OWNER,)).fetchall():
        b = basis(w)
        if b is None:
            continue
        why, closed_at = b
        declared_at = _parse_ts(w["declared_at"] or w["updated_at"] or "")
        if declared_at is not None and closed_at is not None and declared_at > closed_at:
            continue
        store.update_work(w["id"], state=DOING, waiting_on=None)
        store.add_history(w["topic_id"], "work", f"{w['key']} 不再算等主人：{why}（会话自报的活没结束，改成进行中）")
        changed.append(w["id"])
    return changed


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


def key_for(text: str, patterns: Iterable[re.Pattern], fallback: str) -> str:
    keys = extract_keys(text, patterns)
    return keys[0] if keys else fallback


def forward_work(store: Store, src_topic: str, dst_topic: str, rows: list, patterns: Iterable[re.Pattern]) -> None:
    """forward → 已转交（7.20 第 5 步）：转交出去的消息记成转出方名下「已转交」的活。"""
    patterns = list(patterns)
    ts = now_iso()
    for r in rows:
        mid = r["message_id"]
        key = key_for(r["content"] or "", patterns, f"fwd-{mid[-8:]}")
        auto_work(store, src_topic, key, f"forward:{mid}", SRC_FORWARD, HANDED_OFF,
                  title=short(r["content"] or "", 40), handed_to=dst_topic)
        # 转出方手上的这件事（路由事实）收掉：之后撞车提醒指向接手的会话
        for k in extract_keys(r["content"] or "", patterns):
            store.close_route(src_topic, norm_key(k), f"已转交给 {dst_topic}", ts)


# ======================= 路由事实 =======================
def record_routes(store: Store, topic_id: str, rows: list, patterns: Iterable[re.Pattern], at: str = "") -> int:
    """消息投给了这个会话：正文里认出的编号，按 (会话, 编号) 记一条路由事实（几点、第几条）。
    sheepdog 自己生成的消息（留言、提醒）不算。返回记了几条。"""
    from .store import SYSTEM_CHAT
    patterns = list(patterns)
    at = at or now_iso()
    n = 0
    for r in rows:
        if r["chat_type"] == SYSTEM_CHAT:
            continue
        for k in extract_keys(r["content"] or "", patterns):
            n += store.add_route(topic_id, k, norm_key(k), short(r["content"] or "", 40), r["message_id"], at)
    return n


def routes_since(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).isoformat(timespec="seconds")


def route_holders(store: Store, key: str, exclude_topic: str, now: datetime, hours: float) -> list:
    """这把钥匙的消息最近（hours 小时内）送到过的别的会话（路由事实，还没转交、没自报做完）。"""
    return [r for r in store.active_routes(routes_since(now, hours), norm_key=norm_key(key))
            if r["topic_id"] != exclude_topic]


def is_stale(w, card, now: datetime, hours: float) -> bool:
    """会话自报、还开着的活「可能过期」：hours 小时没更新，并且这段时间会话一直空闲。只是标出来，不替它关。"""
    if not w["declared"] or w["closed_at"]:
        return False
    keys = w.keys() if hasattr(w, "keys") else w
    upd = _dt(w["declared_at"] if "declared_at" in keys and w["declared_at"] else w["updated_at"])
    if upd is None or now - upd < timedelta(hours=hours):
        return False
    if card is None or card["run_state"] != IDLE:
        return False
    idle = _dt(card["idle_since"] if "idle_since" in (card.keys() if hasattr(card, "keys") else card) else "")
    return idle is not None and now - idle >= timedelta(hours=hours)


def _dt(ts) -> datetime | None:
    try: dt = datetime.fromisoformat(ts) if ts else None
    except ValueError: return None
    return dt if dt is not None and dt.tzinfo is not None else None


# ======================= 信箱 =======================
def pair_key(a: str, b: str) -> str: return "mail_pause:" + "|".join(sorted((a, b)))


def paused_until(store: Store, a: str, b: str) -> datetime | None:
    dt = _dt(store.get_meta(pair_key(a, b)))
    return dt if dt and dt > datetime.now().astimezone() else None


def mail_label(store: Store, from_topic: str) -> str:
    """留言抬头（事实，代码写死，不随 playbook 改）：谁留的言、它不是主人的指令。"""
    return f"💬 会话留言（不是主人的指令）· 来自{topic_label(store, from_topic)}"


_POLITE_ACK_RE = re.compile(r"^\s*(?:【[^】]*】\s*)?(?:已?收到|知悉|已阅|已获悉|明白|了解|感谢|收到同步|ack\b|roger\b)", re.I)
_BUS_ASK_ACT_RE = re.compile(r"请转交|forward|new-session|\?|？", re.I)


def send_mail(store: Store, cfg, from_topic: str, to_topic: str, text: str, key: str = "", quote: str = "",
              verify_quote=None, playbook=None, is_target: Callable[[str], bool] | None = None) -> int:
    """sheepdog tell：存进信箱，并作为一条系统消息排给收件会话（下一批送到；对方排队、离线都不丢）。"""
    from .engine import BUS_TOPIC_ID  # 避免循环引用
    from .prompts import QUOTE_LABEL, with_cli

    dc = cfg.duty
    from_topic, to_topic = _tid(from_topic), _tid(to_topic)
    text, key, quote = (text or "").strip(), " ".join((key or "").split()), (quote or "").strip()
    if not text: raise DutyError("留言不能为空")
    if from_topic == to_topic: raise DutyError("不能给自己留言")
    if to_topic == BUS_TOPIC_ID and from_topic != BUS_TOPIC_ID and _POLITE_ACK_RE.match(text) and not _BUS_ASK_ACT_RE.search(text):
        raise DutyError("纯知会确认无需回传 tell 给总线，请直接推进并在回执 summary 中说明")
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
        if label is True: label = QUOTE_LABEL
        if not label:
            raise DutyError("--quote 核对没通过：这句话在总线里和主人的飞书回复里都找不到原文，请原样引用")
    now = datetime.now().astimezone()
    if (until := paused_until(store, from_topic, to_topic)) is not None:
        raise DutyError(f"{from_topic} 和 {to_topic} 之间的留言已暂停到 {until.strftime('%H:%M')}（互发太频繁）；有急事找总线或主人")
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
    reply_hint = with_cli(f"回它：`sheepdog tell --topic {to_topic} --to {from_topic} \"...\""
                          + (f" --key {shlex.quote(key)}" if key else "") + "`", cfg.cli_command)
    if from_topic == BUS_TOPIC_ID:
        reply_hint += "（仅需对方执行动作或回答问题时再回；纯收到/知会勿回 tell）"
    lines.append(reply_hint)
    mid = store.add_system_message(to_topic, MAIL_REASON, "\n".join(lines), [f"mail:{mail_id}"])
    store.set_mail_message(mail_id, mid)
    if state_dir := (getattr(getattr(cfg, "paths", None), "state_dir", None) or getattr(cfg, "state_dir", None)):
        try: (state_dir / ".wakeup").write_text(now_iso(), encoding="utf-8")
        except OSError: pass
    return mail_id


send_duty_mail = send_mail


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


def last_action_phrase(card, now: datetime | None = None) -> str:
    """撞车提醒、work set 的提示里「最近动作」那半句：最近动作「…」N 分钟前；找不到会话记录时写那句话；没有就空串。"""
    if card is None:
        return ""
    if "no_record" in card.keys() and card["no_record"]:
        return f"{ACTION_LAST}{NO_RECORD_ACTION}"
    if not card["action"]:
        return ""
    return f"{ACTION_LAST}「{card['action']}」{ago_text(card['action_at'] or '', now)}"


def ago_text(ts: str, now: datetime | None = None) -> str:
    m = minutes_ago(ts, now)
    if m is None:
        return ""
    if m < 1:
        return "刚刚"
    if m < 120:
        return f"{m} 分钟前"
    return f"{m // 60} 小时前"


def source_text(w) -> str:
    """这件活从哪来：会话自报，或者牧羊犬看事实自动补的（等人回复、需要你定、转交）。自报过、又有自动来源的两个都写。"""
    src = w["source"] or DECLARED
    if w["declared"]:
        return SOURCE_LABEL[DECLARED] + (f" + {SOURCE_LABEL.get(src, src)}" if src != DECLARED else "")
    return SOURCE_LABEL.get(src, src)


def stale_label(hours: float) -> str:
    return f"{STALE_MARK}（{hours:g} 小时没更新）"


def work_line(w, with_owner: bool = False, route=None, now: datetime | None = None) -> str:
    """一件活一行：钥匙、标题、状态，括号里是细节和来源。w 里有 stale（可能过期的说明）就放在最前面。
    route：同一把钥匙的路由事实（消息最近送到这里），有就一起写。"""
    parts = [w["key"]]
    if w["title"]:
        parts.append(short(w["title"], 40))
    head = " ".join(parts) + f" · {WORK_LABEL.get(w['state'], w['state'])}"
    extra = []
    stale = w.get("stale") if isinstance(w, dict) else ""
    if stale:
        extra.append(f"⚠ {stale}")
    extra.append(source_text(w))
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
    if with_owner:
        extra.append(f"会话 {w['topic_id']}")
    keys = w.keys() if hasattr(w, "keys") else w
    upd = (w["declared_at"] if "declared_at" in keys and w["declared_at"] and w["declared"] else "") or w["updated_at"]
    if not w["closed_at"] and (ago := ago_text(upd or "", now)):
        extra.append(f"更新于 {ago}")
    if route is not None:
        extra.append(f"{SOURCE_LABEL[SRC_ROUTED]}：消息最近一次 {ago_text(route['last_at'], now) or route['last_at']}"
                     f"送到这里，共 {route['count']} 条")
    return head + "（" + "；".join(extra) + "）"


def route_line(r, now: datetime | None = None) -> str:
    """只有路由事实、会话没登记过的编号：「这个编号的消息最近送到了这里」。"""
    return (f"{r['key']} · 消息最近送到这里（{SOURCE_LABEL[SRC_ROUTED]}：最近一次 "
            f"{ago_text(r['last_at'], now) or r['last_at']}，共 {r['count']} 条；最近一条：{short(r['title'] or '', 30)}）")


def on_hand(work: list[dict], routes: list[dict]) -> list[dict]:
    """「手上的事」：还开着的活（自报 + 自动）和还算数的路由事实，按钥匙合并。"""
    items: list[dict] = []
    by_key: dict[str, dict] = {}
    for w in work:
        item = {"key": w["key"], "norm_key": w["norm_key"], "state": w["state"], "declared": bool(w["declared"]),
                "stale": bool(w.get("stale")), "work": w, "route": None}
        items.append(item)
        by_key[w["norm_key"]] = item
    for r in routes:
        if r["norm_key"] in by_key:
            by_key[r["norm_key"]]["route"] = r
        else:
            items.append({"key": r["key"], "norm_key": r["norm_key"], "state": "", "declared": False, "stale": False,
                          "work": None, "route": r})
    return items


def item_line(item: dict, now: datetime | None = None) -> str:
    return work_line(item["work"], route=item["route"], now=now) if item["work"] is not None \
        else route_line(item["route"], now)


def item_tag(item: dict) -> str:
    """摘要里一件事的简写：状态·来源（自报 / 自动 / 路由），可能过期的加一句。"""
    if item["work"] is None:
        return "路由"
    tags = [WORK_LABEL.get(item["state"], item["state"]), "自报" if item["declared"] else "自动"]
    if item["route"] is not None:
        tags.append("路由")
    if item["stale"]:
        tags.append(STALE_MARK)
    return "·".join(tags)


def action_label(run_state: str) -> str:
    """最近一次工具调用叫什么：在做时是「此刻动作」，其他状态（空闲、等回复、中断……）都是「最近动作」。"""
    return ACTION_NOW if run_state == WORKING else ACTION_LAST


def fresh_active_lambs(store: Store, topic_id: str, now: datetime | None = None,
                       timeout_minutes: float = 20) -> list[dict]:
    """母羊名下 state='running' 且最近一次心跳未超时的活跃羔羊（7.63）。"""
    if not hasattr(store, "get_active_lambs"):
        return []
    now = now or datetime.now().astimezone()
    active = store.get_active_lambs(topic_id)
    out: list[dict] = []
    for lb in active:
        dts = [x for x in (_dt(lb.get("started_at") or ""), _dt(lb.get("last_act_at") or "")) if x is not None]
        ref = max(dts) if dts else None
        if ref is None or (now - ref) <= timedelta(minutes=timeout_minutes):
            out.append(lb)
    return out


def card_data(store: Store, t, card, now: datetime | None = None, stale_hours: float = 4,
              routed_hours: float = 24, lamb_timeout_minutes: float = 20) -> dict:
    """一张会话卡（board / 摘要共用）：职责、运行状态、此刻 / 最近动作、手上的事、最近做完的 3 件、未读留言数。
    action 字段名不分此刻还是最近，action_label 说明是哪一种。

    手上的事 = 还开着的活（work：会话自报的、牧羊犬自动补的，各自标来源）+ 还算数的路由事实（routes：带这个编号的
    消息最近 routed_hours 小时内送到了这里）。会话自报、stale_hours 小时没更新、这段时间一直空闲的，标「可能过期」。"""
    now = now or datetime.now().astimezone()
    tid = t["topic_id"]
    unread = store.unread_mail(tid)
    c = dict(card) if card is not None else {}
    state = c.get("run_state") or ""
    detail = c.get("detail") or ""
    active_lambs = store.get_active_lambs(tid) if hasattr(store, "get_active_lambs") else []
    fresh_lambs = fresh_active_lambs(store, tid, now, lamb_timeout_minutes) if active_lambs else []
    if fresh_lambs:
        l0 = fresh_lambs[0]
        lamb_wait = f"等待羔羊 {l0['lamb_id']} ({l0['task_title']})"
        if state in (*STUCK, "", IDLE):
            state = WAITING
            detail = lamb_wait
        elif state == WAITING and not detail:
            detail = lamb_wait
    work = []
    for w in store.open_work(tid):
        d = dict(w)
        d["source_label"] = source_text(w)
        d["stale"] = stale_label(stale_hours) if is_stale(w, card, now, stale_hours) else ""
        work.append(d)
    closed = []
    for w in store.closed_work(tid, 3):
        d = dict(w)
        d["source_label"] = source_text(w)
        closed.append(d)
    routes = []
    for r in store.active_routes(routes_since(now, routed_hours), topic_id=tid):
        d = dict(r)
        d["source_label"] = SOURCE_LABEL[SRC_ROUTED]
        d["ago"] = ago_text(r["last_at"], now)
        routes.append(d)
    res = {
        "topic_id": tid,
        "title": t["title"],
        "host": (t["host"] if "host" in t.keys() else None) or "antigravity",
        "group_id": (t["group_id"] if "group_id" in t.keys() else "") or "",
        "duty": t["duty"] or "",
        "session_state": t["state"],
        "run_state": state,
        "run_label": RUN_LABEL.get(state, "（还没算，等下一轮）"),
        "spinning": bool(c.get("spinning")),
        "detail": detail,
        # 找不到会话记录：动作写那句话，不带时间（卡片里留着的是记录消失以前的最后一次工具调用，不再当「最近」）
        "no_record": bool(c.get("no_record")),
        "action": NO_RECORD_ACTION if c.get("no_record") else (c.get("action") or ""),
        "action_label": action_label(state),
        "action_at": "" if c.get("no_record") else (c.get("action_at") or ""),
        "action_ago": "" if c.get("no_record") else ago_text(c.get("action_at") or "", now),
        "action_minutes": None if c.get("no_record") else minutes_ago(c.get("action_at") or "", now),
        "compacted_at": c.get("compacted_at") or "",
        "queued": c.get("queued") or 0,
        "updated_at": c.get("updated_at") or "",
        "work": work,
        "routes": routes,
        "on_hand": on_hand(work, routes),
        "recent_done": closed,
        "unread_mail": len(unread),
    }
    if active_lambs:
        res["lambs"] = active_lambs
    return res


def state_text(d: dict) -> str:
    s = d["run_label"] + ("（空转）" if d["spinning"] else "")
    return s + (f"：{d['detail']}" if d["detail"] else "")


def action_text(d: dict) -> str:
    if d.get("no_record"):
        return NO_RECORD_ACTION
    if not d["action"]:
        return "—"
    return f"「{d['action']}」" + (f"（{d['action_ago']}）" if d["action_ago"] else "")


def render_board(cards: list[dict], now_label: str, compact: bool = False) -> str:
    lines = [f"羊群看板 · {now_label}"]
    if not cards:
        return lines[0] + "\n（没有在投递的会话）"
    group_counts: dict[str, int] = {}
    for c in cards:
        gid = c.get("group_id") or ""
        if gid:
            group_counts[gid] = group_counts.get(gid, 0) + 1
    for d in cards:
        gid = d.get("group_id") or ""
        group_badge = (
            f" [羊圈: {gid} · {group_counts[gid]}会话]"
            if gid and group_counts.get(gid, 0) >= 2 and "#" not in d["topic_id"]
            else ""
        )
        lines.append(f"━ 「{d['title']}」 {d['topic_id']} · {d['host']}{group_badge}")
        lines.append(f"  职责：{short(d['duty'], 80) or '-'}")
        lines.append(f"  运行状态：{state_text(d)}" + (f"；排队 {d['queued']} 条" if d["queued"] else ""))
        lines.append(f"  {d['action_label']}：{action_text(d)}"
                     + (f"；{d['compacted_label']}" if d.get("compacted_label") else ""))
        if d["on_hand"]:
            lines.append("  手上的事：")
            lines += [f"    - {item_line(it)}" for it in d["on_hand"]]
        else:
            lines.append("  手上的事：（无）")
        if d["recent_done"]:
            lines.append("  最近做完：")
            lines += [f"    - {work_line(w)}" for w in d["recent_done"]]
        else:
            lines.append("  最近做完：（无）")
        lines.append(f"  未读留言：{d['unread_mail']}")
        if d.get("lambs"):
            lines.append("  活跃羔羊：")
            for lb in d["lambs"]:
                hb = lb.get("last_act_at") or lb.get("started_at") or "-"
                hb_ago = ago_text(hb)
                hb_str = f"{hb}（{hb_ago}）" if hb_ago else hb
                lines.append(
                    f"    ↳ 🐑 {lb['lamb_id']} [{lb['kind']}] {lb['task_title']} · {lb['state']} · 心跳 {hb_str}"
                )
        for k, label in (("recent_receipts", "最近 3 条回执"), ("recent_actions", "最近 5 个动作")):
            if d.get(k):
                lines.append(f"  {label}：")
                lines += [f"    - {x}" for x in d[k]]
    return "\n".join(lines)


def digest_line(d: dict) -> str:
    """总线摘要里一个不空闲的会话一行（事实）。"""
    parts = [f"「{d['title']}」{d['topic_id']}", d["run_label"] + ("（空转）" if d["spinning"] else "")]
    if d["run_state"] in STUCK and d["detail"]:
        parts[-1] += f"（{short(d['detail'], 50)}）"
    if d.get("no_record"):
        parts.append(f"{action_label(d['run_state'])}{NO_RECORD_ACTION}")
    elif d["action"]:
        parts.append(f"{action_label(d['run_state'])}「{short(d['action'], 40)}」{d['action_ago']}")
    items = d.get("on_hand") or []
    if items:
        ws = [f"{it['key']}（{item_tag(it)}）" for it in items[:3]]
        parts.append("手上 " + "、".join(ws) + (f" 等 {len(items)} 件" if len(items) > 3 else ""))
    if d["unread_mail"]:
        parts.append(f"未读留言 {d['unread_mail']}")
    return "- " + " · ".join(parts)


def idle_line(cards: list[dict]) -> str:
    """空闲的会话合成一行：「- 空闲 N 个：标题1、标题2……」。标题太长截短；整行超过 IDLE_LINE_LIMIT 字就用「等」收尾。"""
    head = f"- 空闲 {len(cards)} 个："
    names = [short(d["title"] or d["topic_id"], IDLE_TITLE_LIMIT) for d in cards]
    shown: list[str] = []
    for i, n in enumerate(names):
        more = i + 1 < len(names)
        if shown and len(head + "、".join([*shown, n]) + ("等" if more else "")) > IDLE_LINE_LIMIT:
            break
        shown.append(n)
    return head + "、".join(shown) + ("等" if len(shown) < len(names) else "")


def digest_fingerprint(cards: list[dict]) -> str:
    """摘要只在在岗表变化时附。空闲的会话只看「它空闲」这一件事；不空闲的看运行状态、空转、手上的事（钥匙、状态、
    自报还是自动、有没有路由事实、可能过期）、未读留言。动作、几分钟前、判定依据、路由的次数这类每轮都可能变的不算。"""
    data = []
    for d in sorted(cards, key=lambda d: d["topic_id"]):
        if d["run_state"] == IDLE:
            data.append((d["topic_id"], IDLE))
        else:
            data.append((d["topic_id"], d["run_state"], bool(d["spinning"]),
                         sorted([it["norm_key"], it["state"], it["declared"], it["stale"], it["route"] is not None]
                                for it in (d.get("on_hand") or [])), d["unread_mail"]))
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def digest_lines(cards: list[dict], max_lines: int) -> list[str]:
    """不空闲的会话每个一行，按 RUN_ORDER 排（被占 / 失联 / 中断 / 在做 / 等回复）；空闲的合成最后一行。
    最多 max_lines 行：超了按 KEEP_ORDER 留最要紧的（卡住的先留），砍掉的合成最后一行，写明省略了几个、各是什么状态。"""
    order = {s: i for i, s in enumerate(RUN_ORDER)}
    keep_rank = {s: i for i, s in enumerate(KEEP_ORDER)}
    busy = [d for d in cards if d["run_state"] != IDLE]
    idle = sorted((d for d in cards if d["run_state"] == IDLE), key=lambda d: d["topic_id"])
    # 每一项是 (保留优先级, 显示顺序, 这一行, 它代表的会话)；空闲那一行最先被砍
    entries = [(keep_rank.get(d["run_state"], len(KEEP_ORDER) - 1), order.get(d["run_state"], len(order)),
                d["topic_id"], digest_line(d), [d]) for d in busy]
    if idle:
        entries.append((len(KEEP_ORDER), len(order) + 1, "", idle_line(idle), idle))
    if len(entries) > max_lines:
        entries.sort(key=lambda e: (e[0], e[2]))
        kept, dropped = entries[:max_lines - 1], entries[max_lines - 1:]
    else:
        kept, dropped = entries, []
    kept.sort(key=lambda e: (e[1], e[2]))
    lines = [e[3] for e in kept]
    if dropped:
        gone = [d for e in dropped for d in e[4]]
        counts: dict[str, int] = {}
        for d in gone:
            counts[d["run_state"]] = counts.get(d["run_state"], 0) + 1
        what = "、".join(f"{RUN_LABEL.get(s, '还没算')} {counts[s]}" for s in sorted(counts, key=lambda s: order.get(s, len(order))))
        lines.append(f"- …另有 {len(gone)} 个会话没列出（{what}），`sheepdog board` 看全部")
    return lines


def live_topics(store: Store) -> list:
    """在岗表列哪些会话：总线，加上名册 managed、总线新开的（没收掉的）。"""
    from .engine import BUS_TOPIC_ID, MANAGED_KINDS  # 避免循环引用
    bus = store.get_topic(BUS_TOPIC_ID)
    rest = sorted((t for t in store.list_topics() if t["kind"] in MANAGED_KINDS and t["state"] != "closed"), key=lambda t: t["topic_id"])
    return ([bus] if bus is not None else []) + rest


def board_cards(store: Store, cfg, topic: str = "", exclude: Iterable[str] = (), history: bool = False) -> list[dict]:
    """sheepdog board 和总线摘要用的卡片。topic 给了就只看它（不在投递的会话也能看）。"""
    now = datetime.now().astimezone()
    topics = ([t] if (t := store.get_topic(_tid(topic))) is not None else []) if topic else live_topics(store)
    out = []
    lamb_timeout = getattr(getattr(cfg, "lamb", None), "timeout_minutes", 20)
    for t in topics:
        if t["topic_id"] in exclude:
            continue
        d = card_data(store, t, store.get_card(t["topic_id"]), now, cfg.duty.stale_hours, cfg.duty.routed_active_hours,
                      lamb_timeout_minutes=lamb_timeout)
        m = minutes_ago(d["compacted_at"], now)
        if m is not None and m <= cfg.duty.compacted_recent_minutes:
            d["compacted_label"] = f"刚被压缩（{ago_text(d['compacted_at'], now)}）"
        if history:
            d["recent_receipts"] = [f"{h['at']} {h['text']}" for h in store.history(t["topic_id"], "receipt", 3)]
            d["recent_actions"] = [f"{h['at']} {h['text']}" for h in store.history(t["topic_id"], "action", 5)]
        out.append(d)
    return out
