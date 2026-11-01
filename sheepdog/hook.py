"""Antigravity 生命周期 hook（7.34 实测、7.37 第 5 件）。

`sheepdog hook stop`：Antigravity 在一轮要结束时（Stop）把入参 JSON 从 stdin 给过来。这个会话是牧羊犬管的、手上那批还没交
回执，就往 stdout 写 {"decision": "continue", "reason": "…"}：Antigravity 不让它停，reason 作为系统消息给它（7.34 E3）。
其他情况一律什么都不输出、退出码 0。

约束（7.34 实测带来的改动）：
- 只在确定要拦时输出；任何异常（入参不是 JSON、配置坏了、账本打不开、计数文件写不了……）都兜成不输出。
- 只在 fullyIdle 为 true 时检查：后台任务没跑完时 Stop 也会触发（E6）。
- 主人亲自在会话里打过字（会话记录里这一批之后有 USER_EXPLICIT 的输入）不拦；只读记录末尾 2MB（7.38 第 6 条）。
- 账本只读打开（sqlite mode=ro：不建表、不迁移、不写一个字节）；这一批拦过几次记在状态目录下的小文件里。
- 不导入 engine / store / cli 之外的重模块：每一轮结束都跑这条路径，要快。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ANTIGRAVITY, Config, default_state_dir, load_config
from .playbook import Playbook
from . import session as sm

# 以下几个值和 engine / duty 里的同名常量一致（测试核对）。这里不导入 engine：太重
BUS_TOPIC_ID = "tp_bus"
MANAGED_KINDS = ("adopted", "dynamic")
OCCUPIED = "occupied"
# 同一批最多拦几次（7.37）：拦两次还不交，就放它停，交给回执超时
STOP_MAX_BLOCKS = 2
# 两次 Stop 触发间隔小于这么多秒（7.64）：视为宿主退出/断连瞬时连环报错，不累加正常拦截次数，标记 interrupted 并放行
STOP_RAPID_SECONDS = 10
# 拦过几次的计数：<状态目录>/hooks/stop/<topic>.json，{"batch": 批次号, "count": 次数}；换了批次从 0 算
STOP_COUNT_DIR = ("hooks", "stop")
STOP_TEMPLATE = "stop_receipt.md"
DEFAULT_STOP_REASON = (
    "[sheepdog] 先别结束：批次 `{{batch_id}}` 还没交回执。\n"
    "- 处理完了：`{{receipt_command}}`\n"
    "- 还在等别人：`{{waiting_command}}`，summary 写清等谁、等什么。\n"
    "交完回执再结束。")
# 7.45 三之二：每次被调用记一行（时间、conversationId、是不是牧羊犬的会话、拦 / 不拦及原因、耗时），用来确认 Antigravity 真的
# 在调它、量调用次数和拦截率。<状态目录>/hooks/stop-log/<YYYY-MM-DD>.jsonl，按天一个文件，只留最近 CALL_LOG_DAYS 天；
# 写不了不影响判断和输出
CALL_LOG_DIR = ("hooks", "stop-log")
CALL_LOG_DAYS = 7
# 连账本时最多等几秒（WAL 下读不被写挡，这只是兜底）
DB_TIMEOUT_SECONDS = 1.0
# 会话记录只读末尾这么多字节（总线的记录很大，不全读）
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
# 批次送进会话的那一行：agentapi send-message 记成 SYSTEM_MESSAGE；新建会话的开场是第 0 步 USER_INPUT
DELIVERY_TYPES = ("SYSTEM_MESSAGE", "USER_INPUT")
# 开启一批（占用 pending_batch_id）的那条消息，sheepdog 正文的头一行（7.47 第 4 条；返工后都挪到了第一行）：
# {t} = topic，{b} = 批次号。和 prompts.py 的 batch_header / onboarding_header / successor_header / retirement_prompt 一致
# （测试拿真的 prompt 核对）。只认正文开头：Antigravity 写会话记录时长消息只留首尾各约 2KB、中间换成
# `<truncated N bytes>`，开头一定还在；别的消息里也会写着批次号（主人插话批次的「手上那批」、压缩后补发规则、
# Stop hook 自己的提示……），但都不在正文开头，不算。
BATCH_OPENINGS = (
    "[sheepdog] 新信号批次 `{b}`",                      # b：普通批次（带不带完整规则都是第一行）
    "[sheepdog] 登记通知（topic `{t}`，批次 `{b}`）",   # o：onboarding
    "[sheepdog] 接手开场（topic `{t}`，批次 `{b}`）",   # h：spawn 接手会话、读前任（新建会话的第 0 步 USER_INPUT）
    "[sheepdog] 退休通知（topic `{t}`，批次 `{b}`；",   # r：退休通知（记在 retired 的 topic 上）
)
# 会话记录里 sheepdog 正文前面的包装：agentapi send-message 送的是 SYSTEM_MESSAGE，正文在「[Message] … content=」之后；
# 新建会话的开场是 USER_INPUT，正文在「<USER_REQUEST>\n」之后（10-16 只读核过正式会话记录的格式）
_SYSTEM_BODY_RE = re.compile(r"^\[Message\] [^\n]*?\bcontent=", re.M)
_USER_REQUEST = "<USER_REQUEST>\n"
_PENDING_DECISION_RE = re.compile(
    r"(?:^|\n)\s*(?:[-*•+]|\d+[.)、]|#{1,4})?\s*(?:\*\*|__)?(?:等(?:你|您|主人|他)定的事(?:和回法)?|需要(?:你|主人)拍板的事)"
    r"(?:[^:\n：]*[:：]\s*([^\n]*)|(?:\*\*|__)?\s*(?=\n))(?:\n((?:\s*(?:[-*•+]|\d+[.)、]|[A-E][:：.、])\s*\S[^\n]*\n?)*))?",
    re.I,
)
_NONE_DECISION_RE = re.compile(
    r"^\s*(?:[`*_~>-]*\s*)*(?:无|没有|暂无|无需|目前无|目前没有|暂时没有|0\s*项|none\b)", re.I
)
_ASK_CMD_RE = re.compile(r"\bsheepdog\s+ask\b")


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def count_path(cfg: Config, topic_id: str) -> Path:
    return cfg.state_dir.joinpath(*STOP_COUNT_DIR, f"{topic_id}.json")


def clear_stop_state(state_dir: Path, topic_id: str) -> None:
    """7.64：清掉这个会话的 Stop hook 状态文件（回执已交或人工复位时调用）。"""
    try:
        Path(state_dir).joinpath(*STOP_COUNT_DIR, f"{topic_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _read_stop_state(p: Path, batch_id: str) -> dict:
    """读取当前批次的 Stop hook 状态记录；文件不在、坏了、或记的是别的批次则返回空字典。"""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("batch") != batch_id:
        return {}
    return d


def _blocked_count(p: Path, batch_id: str) -> int:
    """这一批已经拦过几次。文件不在、坏了、记的是别的批次：0。"""
    d = _read_stop_state(p, batch_id)
    n = d.get("count")
    return n if isinstance(n, int) and n > 0 else 0


def _write_stop_state(p: Path, payload: dict) -> None:
    """先写临时文件再改名：Antigravity 超时杀掉进程时不会留下半个文件。写不了就抛错（调用方兜成不拦）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_count(p: Path, batch_id: str, count: int, **extra) -> None:
    """写当前批次的拦截次数与附加时间戳。"""
    payload = {"batch": batch_id, "count": count}
    payload.update(extra)
    _write_stop_state(p, payload)


def _json_line(line: bytes):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def message_body(content: str) -> str:
    """会话记录一行的 content 去掉 Antigravity 的包装，剩下 sheepdog 发的正文（没有包装就是原样）。"""
    if content.startswith(_USER_REQUEST):
        return content[len(_USER_REQUEST):]
    if content.startswith("The following is a <SYSTEM_MESSAGE>") and (m := _SYSTEM_BODY_RE.search(content, 0, 2048)):
        return content[m.end():]
    return content


def opens_batch(content, batch_id: str, topic_id: str = "") -> bool:
    """这条消息是不是开启这一批的那条：sheepdog 正文以 BATCH_OPENINGS 里的某一行开头。不知道 topic（topic_id 为空）时，
    {t} 认任意一个不含反引号的 topic 编号。"""
    if not isinstance(content, str) or not batch_id:
        return False
    body = message_body(content)
    for mark in BATCH_OPENINGS:
        if topic_id or "{t}" not in mark:
            if body.startswith(mark.format(b=batch_id, t=topic_id)):
                return True
        elif re.match(re.escape(mark.format(b=batch_id, t="\x00")).replace("\x00", "[^`\n]*"), body):
            return True
    return False


def _tail_lines(path: Path, tail: int) -> list[bytes]:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - tail)
        f.seek(start)
        lines = f.read(size - start).split(b"\n")
    return lines[1:] if start > 0 else lines


def owner_typed_after_batch(path: Path, batch_id: str, tail: int = TRANSCRIPT_TAIL_BYTES, topic_id: str = "") -> bool | None:
    """读会话记录末尾 tail 字节：这一批送达以后，主人有没有亲自在会话里打过字（True/False/None）。"""
    needle, found = batch_id.encode(), False
    for line in _tail_lines(path, tail):
        if not found:
            if needle in line and (d := _json_line(line)) is not None and d.get("type") in DELIVERY_TYPES and opens_batch(d.get("content"), batch_id, topic_id):
                found = True
        elif b"USER_EXPLICIT" in line and (d := _json_line(line)) is not None and d.get("type") == "USER_INPUT" and d.get("source") == "USER_EXPLICIT":
            return True
    return False if found else None


def _unsent_ask_turn(path: Path, tail: int = TRANSCRIPT_TAIL_BYTES) -> tuple[str, str] | None:
    """REG-044：读会话记录末尾最后一段真实输入后的本轮轨迹：最后一条 PLANNER_RESPONSE 写了「等你定的事」且本轮未调 sheepdog ask 时返回 (key, snippet)。"""
    steps = [d for line in _tail_lines(path, tail) if line and (d := _json_line(line)) is not None]
    anchor_idx, anchor_step = 0, 0
    for i, d in enumerate(steps):
        tp, content = d.get("type"), str(d.get("content") or "")
        if (tp == "USER_INPUT" and d.get("source") == "USER_EXPLICIT") or (
            tp == "SYSTEM_MESSAGE" and "[sheepdog] 先别结束" not in content and "Stop hook blocked termination" not in content
        ):
            anchor_idx, anchor_step = i, d.get("step_index", i)
    tail_steps = steps[anchor_idx:]
    if any(_ASK_CMD_RE.search(json.dumps(d, ensure_ascii=False)) for d in tail_steps if d.get("type") != "USER_INPUT"):
        return None
    planners = [d for d in tail_steps if d.get("type") == "PLANNER_RESPONSE"]
    if not planners or not (m := _PENDING_DECISION_RE.search(str(planners[-1].get("content") or ""))):
        return None
    body = f"{m.group(1) or ''}\n{m.group(2) or ''}".strip()
    return (f"ask:{anchor_step}", re.sub(r"\s+", " ", body)[:80]) if body and not _NONE_DECISION_RE.match(body) else None


def _target(conn: sqlite3.Connection, conversation_id: str):
    """conversationId 对应的、牧羊犬在投递的 Antigravity 会话（总线、名册 managed、总线新开的）；对不上或对上不止一个：None。"""
    rows = conn.execute(
        "SELECT * FROM topics WHERE conversation_id=? AND state != ? AND (kind IN (?, ?) OR topic_id=?)",
        (conversation_id, sm.CLOSED, *MANAGED_KINDS, BUS_TOPIC_ID)).fetchall()
    return rows[0] if len(rows) == 1 and (rows[0]["host"] or ANTIGRAVITY) == ANTIGRAVITY else None


def _find_lamb(conn: sqlite3.Connection, conversation_id: str):
    """7.63：conversationId 不在 topics 表时，查是不是登记在 lambs 表的羔羊；命中则可回溯到 parent_topic_id。"""
    try:
        return conn.execute("SELECT * FROM lambs WHERE conversation_id=? ORDER BY (state='running') DESC, started_at DESC LIMIT 1", (conversation_id,)).fetchone()
    except sqlite3.OperationalError:
        return None


def resolve_lamb_attribution(conn_or_store, conversation_id: str) -> tuple[str, str, str] | None:
    """7.63：按 conversation_id 查 lambs 表回溯母羊（parent_topic_id, lamb_id, '（由羔羊 <lamb_id> 执行）'）。"""
    if not (cid := (conversation_id or "").strip()):
        return None
    lb = conn_or_store.find_lamb_by_cid(cid) if hasattr(conn_or_store, "find_lamb_by_cid") else (dict(r) if (r := _find_lamb(conn_or_store, cid)) is not None else None)
    return (lb["parent_topic_id"], lb["lamb_id"], f"（由羔羊 {lb['lamb_id']} 执行）") if lb else None


def _occupied(card, cfg: Config, now: datetime) -> bool:
    """被主人占着：口径同羊群看板（卡片判为被主人占着，或主人 human_attach_minutes 分钟内在会话里亲自说过话）。"""
    if card is None:
        return False
    return card["run_state"] == OCCUPIED or ((at := _parse(card["human_at"] or "")) is not None and now - at < timedelta(minutes=cfg.session.human_attach_minutes))


def _record_stop_block(cfg: Config, data: dict, tid: str, key: str, now: datetime, info: dict, why, label: str) -> int | None:
    p, now_iso, exec_num = count_path(cfg, tid), now.isoformat(timespec="seconds"), data.get("executionNum")
    st = _read_stop_state(p, key)
    prev, last_blocked = (st.get("count") if isinstance(st.get("count"), int) and st.get("count") > 0 else 0), _parse(str(st.get("last_blocked_at") or ""))
    if isinstance(exec_num, int) and not isinstance(exec_num, bool) and exec_num >= 1 and prev > 0 and last_blocked is not None and 0 <= (now - last_blocked).total_seconds() < STOP_RAPID_SECONDS:
        try: _write_stop_state(p, {**st, "batch": key, "count": prev, "interrupted": True, "passed_at": now_iso})
        except OSError: pass
        return why("宿主断连/重登瞬时连续触发（间隔 <10s），标记 interrupted 并放行等待自动续跑")
    if (n := prev + 1) > STOP_MAX_BLOCKS:
        try: _write_stop_state(p, {**st, "batch": key, "count": prev, "passed_at": st.get("passed_at") or now_iso})
        except OSError: pass
        return why(f"这一批已经拦过 {STOP_MAX_BLOCKS} 次")
    _write_count(p, key, n, last_blocked_at=now_iso, **{k: st[k] for k in ("auto_resumed", "auto_resumed_at") if k in st})
    info["reason"] = f"{label}，第 {n} 次拦"
    return n


def stop_check(raw: str, cfg: Config | None = None, now: datetime | None = None, info: dict | None = None) -> dict | None:
    """要拦就返回要写到 stdout 的 dict，不拦返回 None。出错一律当不拦（异常向外抛，由 stop_main 兜住）。"""
    info = info if info is not None else {}
    why = lambda reason: (info.__setitem__("reason", reason), None)[1]
    if not isinstance(data := json.loads(raw), dict): return why("入参不是 JSON 对象")
    if isinstance(cid := data.get("conversationId"), str): info["conversation_id"] = cid.strip()
    if data.get("fullyIdle") is not True: return why("fullyIdle 不是 true（后台任务还在跑）")
    if not isinstance(cid, str) or not cid.strip(): return why("没有 conversationId")
    cfg, now = cfg or load_config(), now or datetime.now().astimezone()
    info["state_dir"] = str(cfg.state_dir)
    if not cfg.hooks.stop: return why("[hooks] stop 关着")
    if not cfg.db_path.is_file(): return why("账本不在")
    conn = _connect_ro(cfg.db_path)
    need_receipt, batch_reason, open_escs = False, "", 0
    try:
        info["managed"] = (t := _target(conn, cid.strip())) is not None
        if t is None:
            if (lb := _find_lamb(conn, cid.strip())) is not None:
                lid, ptid = lb["lamb_id"], lb["parent_topic_id"]
                info.update(topic=ptid, lamb_id=lid, parent_topic_id=ptid, note=f"（由羔羊 {lid} 执行）")
                return why(f"羔羊 {lid}（隶属 {ptid}）（由羔羊 {lid} 执行），不由 Stop hook 拦回执")
            return why("不是牧羊犬管的 Antigravity 会话")
        tid, batch = t["topic_id"], t["pending_batch_id"] or ""
        info["topic"] = tid
        if t["state"] != sm.RUNNING or not batch:
            batch_reason = f"会话 {t['state']}，手上没有在等回执的批次"
        else:
            info["batch"] = batch
            if (cfg.receipts_dir / tid / f"{batch}.json").exists():
                if _read_stop_state(count_path(cfg, tid), batch): clear_stop_state(cfg.state_dir, tid)
                batch_reason = "回执已交"
            else:
                d = conn.execute("SELECT state FROM dispatches WHERE batch_id=?", (batch,)).fetchone()
                if d is None or d["state"] != "sent":
                    if d is not None and d["state"] in ("acked", "acked_late", "missed", "failed") and _read_stop_state(count_path(cfg, tid), batch):
                        clear_stop_state(cfg.state_dir, tid)
                    batch_reason = f"批次状态 {d['state'] if d is not None else '查不到'}"
                else:
                    if _occupied(conn.execute("SELECT * FROM duty_cards WHERE topic_id=?", (tid,)).fetchone(), cfg, now):
                        return why("被主人占着")
                    need_receipt = True
        if tid != BUS_TOPIC_ID:
            try:
                r = conn.execute("SELECT COUNT(*) AS c FROM escalations WHERE (topic_id=? OR topic_id=?) AND closed_at IS NULL", (tid, tid.split("#", 1)[0])).fetchone()
                open_escs = int(r["c"]) if r is not None else 0
            except sqlite3.OperationalError:
                open_escs = 0
    finally:
        conn.close()
    tp = data.get("transcriptPath")
    if need_receipt:
        if not isinstance(tp, str) or not tp.strip(): return why("没有 transcriptPath")
        if (typed := owner_typed_after_batch(Path(tp), batch, TRANSCRIPT_TAIL_BYTES, tid)) is None:
            batch_reason = "会话记录末尾找不到这一批的开头"
        elif typed:
            batch_reason = "这批送达以后主人亲自打过字"
        else:
            if (n := _record_stop_block(cfg, data, tid, batch, now, info, why, "没交回执")) is None: return None
            cli, pb = cfg.cli_command, Playbook.from_config(cfg)
            pb.strict = True
            receipt = f"{cli} receipt --topic {tid} --batch {batch} --json '{{\"status\": \"handled\", \"summary\": \"一句话：做了什么\"}}'"
            waiting = f"{cli} receipt --topic {tid} --batch {batch} --json '{{\"status\": \"waiting_external\", \"summary\": \"等谁、等什么\"}}'"
            return {"decision": "continue", "reason": pb.render_or(STOP_TEMPLATE, DEFAULT_STOP_REASON, topic_id=tid, batch_id=batch, receipt_command=receipt, waiting_command=waiting, blocked=str(n))}
    if tid != BUS_TOPIC_ID and open_escs == 0 and isinstance(tp, str) and tp.strip() and Path(tp).is_file():
        if (unsent := _unsent_ask_turn(Path(tp), TRANSCRIPT_TAIL_BYTES)) is not None:
            ask_key, ask_snippet = unsent
            if _record_stop_block(cfg, data, tid, ask_key, now, info, why, "等你定的事未发 ask") is None: return None
            return {"decision": "continue", "reason": f"[sheepdog] 先别结束：你在回复中列出了「等你定的事」（{ask_snippet}），但账本中尚无未决飞书待拍板卡片（open_escalations=0）。请立即调用 `{cfg.cli_command} ask --topic {tid} ...` 发送到飞书后再结束。"}
    return why(batch_reason)


def call_log_dir(state_dir: Path) -> Path:
    return Path(state_dir).joinpath(*CALL_LOG_DIR)


def log_call(state_dir: Path, rec: dict, now: datetime | None = None) -> None:
    """调用日志记一行（追加写当天的文件），顺手删掉 CALL_LOG_DAYS 天以前的文件。出错向外抛，由调用方吞掉。"""
    now = now or datetime.now().astimezone()
    if not Path(state_dir).is_dir():
        return   # 状态目录都没有（没装好、指错了）：不替它建目录，这次不记
    d = call_log_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    keep = (now - timedelta(days=CALL_LOG_DAYS - 1)).strftime("%Y-%m-%d")
    for old in d.glob("*.jsonl"):
        if old.stem < keep:
            try:
                old.unlink()
            except OSError:
                pass


def stop_main(stdin, stdout, cfg: Config | None = None, now: datetime | None = None) -> int:
    """`sheepdog hook stop` 的全部逻辑：任何异常都不输出、退出码 0（Stop 出错 Antigravity 照常结束，7.34 E7；
    但多写一个字就可能被当成要拦，所以出错时一个字也不写）。输出写完以后记一行调用日志（7.45 三之二），记不下就算了。"""
    started = time.monotonic()
    info: dict = {}
    out = None
    try:
        raw = stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        out = stop_check(raw, cfg, now, info)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False))
            stdout.flush()
    except Exception as e:
        info["reason"] = f"出错不拦：{type(e).__name__}: {e}"[:300]
        out = None
    try:
        at = now or datetime.now().astimezone()
        state_dir = Path(info.get("state_dir") or (cfg.state_dir if cfg is not None else default_state_dir()))
        log_call(state_dir, {
            "at": at.isoformat(timespec="seconds"), "conversation_id": info.get("conversation_id", ""),
            "managed": info.get("managed"), "topic": info.get("topic", ""), "batch": info.get("batch", ""),
            "lamb_id": info.get("lamb_id", ""), "decision": "block" if out is not None else "pass",
            "reason": info.get("reason", ""), "ms": round((time.monotonic() - started) * 1000, 1)}, at)
    except Exception:
        pass
    return 0
