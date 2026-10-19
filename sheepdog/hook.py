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


def _blocked_count(p: Path, batch_id: str) -> int:
    """这一批已经拦过几次。文件不在、坏了、记的是别的批次：0。"""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(d, dict) or d.get("batch") != batch_id:
        return 0
    n = d.get("count")
    return n if isinstance(n, int) and n > 0 else 0


def _write_count(p: Path, batch_id: str, count: int) -> None:
    """先写临时文件再改名：Antigravity 超时杀掉进程时不会留下半个文件。写不了就抛错（调用方兜成不拦）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"batch": batch_id, "count": count}, f)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def owner_typed_after_batch(path: Path, batch_id: str, tail: int = TRANSCRIPT_TAIL_BYTES,
                            topic_id: str = "") -> bool | None:
    """读会话记录末尾 tail 字节：这一批送达以后，主人有没有亲自在会话里打过字（type USER_INPUT、source USER_EXPLICIT；
    agentapi 送进去的是 SYSTEM_MESSAGE，hook 插的是 SYSTEM_SDK 来源的 USER_INPUT，都不算）。

    返回 True：打过；False：没打过；None：末尾这一段里找不到这一批送达的那一行（找不到就不拦）。
    「送达的那一行」= 末尾这一段里第一条开启这一批的 SYSTEM_MESSAGE / USER_INPUT：正文里有这一批的开头（BATCH_OPENINGS，
    7.47 第 4 条）。只是提到批次号的（主人插话批次写着「手上那批」、压缩后重推规则、Stop hook 的提示）不算。
    读不了就抛错（调用方兜成不拦）。"""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - tail)
        f.seek(start)
        data = f.read(size - start)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # 第一行是从中间截开的
    needle = batch_id.encode()   # 先按字节粗筛（便宜），再解析 JSON、按批次开头认（7.47 第 4 条）
    found = False
    for line in lines:
        if not found:
            if needle in line and (d := _json_line(line)) is not None and d.get("type") in DELIVERY_TYPES \
                    and opens_batch(d.get("content"), batch_id, topic_id):
                found = True
            continue
        if b"USER_EXPLICIT" in line and (d := _json_line(line)) is not None \
                and d.get("type") == "USER_INPUT" and d.get("source") == "USER_EXPLICIT":
            return True
    return False if found else None


def _target(conn: sqlite3.Connection, conversation_id: str):
    """conversationId 对应的、牧羊犬在投递的 Antigravity 会话（总线、名册 managed、总线新开的）；对不上或对上不止一个：None。"""
    rows = conn.execute(
        "SELECT * FROM topics WHERE conversation_id=? AND state != ? AND (kind IN (?, ?) OR topic_id=?)",
        (conversation_id, sm.CLOSED, *MANAGED_KINDS, BUS_TOPIC_ID)).fetchall()
    if len(rows) != 1:
        return None
    t = rows[0]
    return t if (t["host"] or ANTIGRAVITY) == ANTIGRAVITY else None


def _find_lamb(conn: sqlite3.Connection, conversation_id: str):
    """7.63：conversationId 不在 topics 表时，查是不是登记在 lambs 表的羔羊；命中则可回溯到 parent_topic_id。"""
    try:
        return conn.execute(
            "SELECT * FROM lambs WHERE conversation_id=? ORDER BY (state='running') DESC, started_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def resolve_lamb_attribution(conn_or_store, conversation_id: str) -> tuple[str, str, str] | None:
    """7.63：按 conversation_id 查 lambs 表回溯母羊（parent_topic_id, lamb_id, '（由羔羊 <lamb_id> 执行）'）。"""
    cid = (conversation_id or "").strip()
    if not cid:
        return None
    if hasattr(conn_or_store, "find_lamb_by_cid"):
        lb = conn_or_store.find_lamb_by_cid(cid)
    else:
        row = _find_lamb(conn_or_store, cid)
        lb = dict(row) if row is not None else None
    if not lb:
        return None
    lid = lb["lamb_id"]
    ptid = lb["parent_topic_id"]
    return ptid, lid, f"（由羔羊 {lid} 执行）"


def _occupied(card, cfg: Config, now: datetime) -> bool:
    """被主人占着：口径同羊群看板（卡片判为被主人占着，或主人 human_attach_minutes 分钟内在会话里亲自说过话）。"""
    if card is None:
        return False
    if card["run_state"] == OCCUPIED:
        return True
    at = _parse(card["human_at"] or "")
    return at is not None and now - at < timedelta(minutes=cfg.session.human_attach_minutes)


def stop_check(raw: str, cfg: Config | None = None, now: datetime | None = None, info: dict | None = None) -> dict | None:
    """要拦就返回要写到 stdout 的 dict，不拦返回 None。出错一律当不拦（异常向外抛，由 stop_main 兜住）。
    info（7.45 三之二）：传进来就把这次调用的情况写进去（conversation_id、managed、topic、batch、reason），给调用日志用。

    拦的条件（全部满足，7.37 / 7.38 第 6 条）：conversationId 是牧羊犬管的 Antigravity 会话；fullyIdle 为 true；会话 running、
    手上那批的回执文件不在、账本里那批还是 sent（没收到回执）；这一批拦过的次数 < STOP_MAX_BLOCKS；会话没被主人占着（羊群
    看板的口径）；入参 transcriptPath 末尾找得到这一批，且送达以后主人没有亲自在会话里打过字。"""
    info = info if info is not None else {}

    def why(reason: str) -> None:
        info["reason"] = reason
        return None

    data = json.loads(raw)
    if not isinstance(data, dict):
        return why("入参不是 JSON 对象")
    cid = data.get("conversationId")
    if isinstance(cid, str):
        info["conversation_id"] = cid.strip()
    if data.get("fullyIdle") is not True:
        return why("fullyIdle 不是 true（后台任务还在跑）")
    if not isinstance(cid, str) or not cid.strip():
        return why("没有 conversationId")
    cfg = cfg or load_config()   # 每次调用都是新进程、现读配置：[hooks] stop 改完下一次收工就生效
    info["state_dir"] = str(cfg.state_dir)
    if not cfg.hooks.stop:
        return why("[hooks] stop 关着")   # 7.40 开关关掉：什么都不输出，会话照常结束
    now = now or datetime.now().astimezone()
    if not cfg.db_path.is_file():
        return why("账本不在")
    conn = _connect_ro(cfg.db_path)
    try:
        t = _target(conn, cid.strip())
        info["managed"] = t is not None
        if t is None:
            lb = _find_lamb(conn, cid.strip())
            if lb is not None:
                lid, ptid = lb["lamb_id"], lb["parent_topic_id"]
                info["topic"] = ptid
                info["lamb_id"] = lid
                info["parent_topic_id"] = ptid
                info["note"] = f"（由羔羊 {lid} 执行）"
                return why(f"羔羊 {lid}（隶属 {ptid}）（由羔羊 {lid} 执行），不由 Stop hook 拦回执")
            return why("不是牧羊犬管的 Antigravity 会话")
        info["topic"] = t["topic_id"]
        if t["state"] != sm.RUNNING or not t["pending_batch_id"]:
            return why(f"会话 {t['state']}，手上没有在等回执的批次")
        tid, batch = t["topic_id"], t["pending_batch_id"]
        info["batch"] = batch
        if (cfg.receipts_dir / tid / f"{batch}.json").exists():
            return why("回执已交")  # 回执交了，下一轮 sheepdog 收
        d = conn.execute("SELECT state FROM dispatches WHERE batch_id=?", (batch,)).fetchone()
        if d is None or d["state"] != "sent":
            return why(f"批次状态 {d['state'] if d is not None else '查不到'}")
        card = conn.execute("SELECT * FROM duty_cards WHERE topic_id=?", (tid,)).fetchone()
        if _occupied(card, cfg, now):
            return why("被主人占着")
    finally:
        conn.close()
    # 被主人占着的另一个口径（正式配置 human_attach_minutes = 0，上面那条永远不成立）：读会话记录，这一批送达以后主人亲自
    # 打过字就不拦；末尾找不到这一批（记录不在、太长、送达那行早被挤出末尾这一段）也不拦
    tp = data.get("transcriptPath")
    if not isinstance(tp, str) or not tp.strip():
        return why("没有 transcriptPath")
    typed = owner_typed_after_batch(Path(tp), batch, TRANSCRIPT_TAIL_BYTES, tid)
    if typed is None:
        return why("会话记录末尾找不到这一批的开头")
    if typed:
        return why("这批送达以后主人亲自打过字")
    p = count_path(cfg, tid)
    n = _blocked_count(p, batch) + 1
    if n > STOP_MAX_BLOCKS:
        return why(f"这一批已经拦过 {STOP_MAX_BLOCKS} 次")
    _write_count(p, batch, n)  # 记不下次数就不拦：宁可放它停，也不能拦个没完
    info["reason"] = f"没交回执，第 {n} 次拦"
    cli = cfg.cli_command
    receipt = f"{cli} receipt --topic {tid} --batch {batch} --json '{{\"status\": \"handled\", \"summary\": \"一句话：做了什么\"}}'"
    waiting = (f"{cli} receipt --topic {tid} --batch {batch} "
               f"--json '{{\"status\": \"waiting_external\", \"summary\": \"等谁、等什么\"}}'")
    pb = Playbook.from_config(cfg)
    pb.strict = True   # 模板读不了就抛出去、由 stop_main 兜成不输出（出错一律当不拦），不拿默认话去拦
    reason = pb.render_or(
        STOP_TEMPLATE, DEFAULT_STOP_REASON, topic_id=tid, batch_id=batch, receipt_command=receipt,
        waiting_command=waiting, blocked=str(n))
    return {"decision": "continue", "reason": reason}


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
            "decision": "block" if out is not None else "pass", "reason": info.get("reason", ""),
            "ms": round((time.monotonic() - started) * 1000, 1)}, at)
    except Exception:
        pass
    return 0
