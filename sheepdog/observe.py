"""系统级看门狗与深度水位巡检（规格 7.68：sheepdog observe / Watchdog 2.0）。

四层深度防御（L1–L4）：
- L1：进程唯一性（sheepdog run）、lark-cli event consume 活性、常驻进程内存版本（daemon_runtime）与磁盘版本一致性（超 5 分钟告警）；
- L2：四阶段分钟级水流 SLA（Ingest Ack 90s、Priority 插话 90s、Queue Head-of-Line 普通排队 5m、Owner Echo 15m）；
- L3：宿主 Transcript 深度探针（90s Prompt Filter / 投递黑洞探针、180s+90s 假 running 僵尸锁探针与 --self-heal 写 stop hook 自愈标记、tp_bus 150s 超时与越权工具/命令探针）；
- L4：核心不变量守卫（7.53/7.56/7.61/7.65/7.63）与 ask_jumps 噪音剃刀（仅统计未结案或超时异常关闭的提问）及告警指纹秒数归一化。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import default_repo_dir, version_info, version_watch
from .config import Config, load_config
from .guard import decode_command_line, normalize
from .sink import agentapi

TZ = timezone(timedelta(hours=8))
HOME = Path.home()
DEFAULT_DB = HOME / ".local/state/sheepdog/sheepdog.sqlite3"
DEFAULT_STATE = HOME / ".local/state/sheepdog/watchdog-cursor.json"
DEFAULT_LOG = HOME / ".gemini/antigravity/sidecar_data/sheepdog/logs/sidecar.log"
DEFAULT_MEDIA = HOME / ".local/state/sheepdog/media"
DEFAULT_ROSTER = HOME / ".config/sheepdog/roster.toml"
DEFAULT_OWNER = ""
DEFAULT_OWNER_CHAT = ""
GOLIVE = "2026-10-17T04:42:28+08:00"

TRANSCRIPT_TAIL_BYTES = 256 * 1024

_BUS_FORBIDDEN_TOOLS = frozenset({"write_file", "edit_file", "notebook_edit"})
_BUS_FORBIDDEN_CMD_RE = re.compile(
    r"\b(?:gcloud|terraform|kubectl|context_memory\.py|pkm\.py|linear_task_memory\.py)\b"
    r"|\blark-cli\s+(?:docs|drive|wiki|bitable|sheets)\b"
)
_QUOTED_STR_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_SHEEPDOG_SUBCMDS = frozenset({
    "forward", "spawn", "new-session", "watch", "watches", "watch-done", "unwatch",
    "receipt", "ask", "report", "reply", "reply-card", "show", "sessions", "work",
    "thread", "board", "escalations", "pending", "resolve", "tell", "track",
    "topic-group", "lamb", "inbox", "inbox-clear", "doctor", "version", "upgrade", "observe",
    "acks", "notices", "actions", "loops", "redeliver", "push-rules", "retire",
    "close-session", "session-reset", "security-log", "poll", "run", "init", "inject", "hook",
})
_SHEEPDOG_CMD_SEGMENT_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:(?:\S+/)?sheepdog(?:\.cli)?|(?:\S+/)?python[\d.]*\s+-m\s+sheepdog(?:\.cli)?)"
    r"(?:\s+(?P<subcmd>[A-Za-z0-9_-]+))?\b"
)

_DEFAULT_ACK_REASONS = frozenset({
    "p2p", "dm",
    "at_me", "at_bot", "mention_me",
    "at_all", "mention_all",
    "owner_reply", "owner_instruction",
    "reply_to_me",
})
_ACK_REASON_EQUIV: dict[str, tuple[str, ...]] = {
    "p2p": ("p2p", "dm"),
    "dm": ("dm", "p2p"),
    "at_me": ("at_me", "mention_me"),
    "mention_me": ("mention_me", "at_me"),
    "at_bot": ("at_bot", "at_me", "mention_me"),
    "at_all": ("at_all", "mention_all"),
    "mention_all": ("mention_all", "at_all"),
    "owner_reply": ("owner_reply", "owner_instruction"),
    "owner_instruction": ("owner_instruction", "owner_reply"),
}

_FP_PATTERNS = (
    (re.compile(r"\d+\s*分钟"), "N分钟"),
    (re.compile(r"\(\d+s\)"), "(Ns)"),
    (re.compile(r"已送出\s*\d+s"), "已送出Ns"),
    (re.compile(r"静默\s*\d+s"), "静默Ns"),
    (re.compile(r"running\s*\d+s"), "running Ns"),
    (re.compile(r"\b\d+s\b"), "Ns"),
)


def now_tz() -> datetime:
    return datetime.now(TZ)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def age_seconds(s: str | None, ref: datetime) -> float | None:
    dt = parse_iso(s)
    if dt is None:
        return None
    return (ref - dt).total_seconds()


def age_min(s: str | None, ref: datetime | None = None) -> float | None:
    sec = age_seconds(s, ref or now_tz())
    return (sec / 60.0) if sec is not None else None


def _normalize_warn_for_fp(w: str) -> str:
    out = w
    for pat, repl in _FP_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _fingerprint_warns(warns: list[str]) -> str:
    """将随时间递增的秒/分钟数字归一化后计算稳定 SHA-256 指纹，避免持续故障穿透冷却。"""
    if not warns:
        return ""
    normalized = sorted(_normalize_warn_for_fp(w) for w in warns)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()[:16]


def _is_bus_forbidden_command(cmd_str: str) -> bool:
    """判断 tp_bus 执行的 run_command 是否越权：
    1. 先剥离单双引号内的字符串字面量（如 --note "..." / --text "..."），避免转发备注提及 gcloud 等词被误判；
    2. 拆分子命令段，若子命令段是合法的 sheepdog 自身 CLI 调用则直接放行；
    3. 对非 sheepdog 子命令段的去引号文本匹配 _BUS_FORBIDDEN_CMD_RE。
    """
    if not cmd_str:
        return False
    unquoted = _QUOTED_STR_RE.sub('""', cmd_str)
    for seg in re.split(r"(?:&&|\|\||[;|\n])+", unquoted):
        seg_s = seg.strip()
        if not seg_s:
            continue
        m = _SHEEPDOG_CMD_SEGMENT_RE.match(seg_s)
        if m is not None:
            subcmd = (m.group("subcmd") or "").strip()
            if not subcmd or subcmd.startswith("-") or subcmd in _SHEEPDOG_SUBCMDS:
                continue
        if _BUS_FORBIDDEN_CMD_RE.search(seg_s):
            return True
    return False


def _should_expect_ack(row: sqlite3.Row | dict, ack_reasons: set[str]) -> bool:
    """对齐 sheepdog/ack.py::Acker.identity_for 判断一条已分派消息是否应当点确认表情。"""
    r_dict = dict(row)
    if r_dict.get("chat_type") == "system":
        return False
    reason = str(r_dict.get("reason") or "").strip()
    if not reason or reason == "owner_context":
        return False
    raw_tags = r_dict.get("tags_json")
    tags: list[str] = []
    if raw_tags:
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, list):
                tags = [str(x) for x in parsed]
        except (TypeError, ValueError):
            tags = []
    if "owner_verified" in tags or reason == "owner_instruction" or "owner_instruction" in tags:
        reason = "owner_reply"
    candidates = _ACK_REASON_EQUIV.get(reason, (reason,))
    return any(c in ack_reasons for c in candidates)


def _send_lark_card(chat_id: str, title: str, template: str, body_md: str) -> bool:
    """用 lark-cli --as bot 直发飞书 Card 2.0 卡片；失败时自动回退纯文本。"""
    lark_bin = "/opt/homebrew/bin/lark-cli" if Path("/opt/homebrew/bin/lark-cli").exists() else "lark-cli"
    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": " ".join(title.split()).replace('"', "＂")},
            "template": template,
        },
        "body": {"elements": [{"tag": "markdown", "content": body_md}]},
    }
    cmd_card = [
        lark_bin, "im", "+messages-send",
        "--as", "bot",
        "--chat-id", chat_id,
        "--msg-type", "interactive",
        "--content", json.dumps(card, ensure_ascii=False),
    ]
    try:
        r = subprocess.run(cmd_card, capture_output=True, text=True, timeout=20, check=False)
        if r.returncode == 0:
            return True
        err1 = (r.stderr or r.stdout or f"rc={r.returncode}").strip()
    except Exception as e:
        err1 = str(e)
    cmd_text = [
        lark_bin, "im", "+messages-send",
        "--as", "bot",
        "--chat-id", chat_id,
        "--text", f"{title}\n{body_md}",
    ]
    try:
        r2 = subprocess.run(cmd_text, capture_output=True, text=True, timeout=20, check=False)
        if r2.returncode == 0:
            return True
        err2 = (r2.stderr or r2.stdout or f"rc={r2.returncode}").strip()
    except Exception as e:
        err2 = str(e)
    print(f"看门狗飞书发送失败（card: {err1}; text: {err2}）", file=sys.stderr)
    return False


def ask_jumps(c: sqlite3.Connection, at: datetime) -> list[tuple[str, str]]:
    """L4 噪音剃刀版 ask_jumps：仅统计最近 24h 内未结案或超时/异常关闭的提问。

    正常已答复结案（answered / resolved / closed_by_owner）的多轮密集对话不再误报。
    当 24h 未结案提问数 d1_unsettled >= 4 且超过历史日均的 2 倍（无历史基线时 >= 5）触发告警。
    """
    out: list[tuple[str, str]] = []
    esc_cols = {r[1] for r in c.execute("PRAGMA table_info(escalations)")}
    if not esc_cols:
        return out
    if "asked_at" in esc_cols and "created_at" in esc_cols:
        ts_col = "COALESCE(asked_at, created_at)"
    elif "created_at" in esc_cols:
        ts_col = "created_at"
    else:
        ts_col = "asked_at"

    if "close_reason" in esc_cols and "status" in esc_cols:
        status_col = "COALESCE(close_reason, status, 'open')"
    elif "status" in esc_cols:
        status_col = "COALESCE(status, 'open')"
    else:
        status_col = "COALESCE(close_reason, 'open')"

    cutoff_24h = iso(at - timedelta(days=1))
    unsettled_cond = f"(closed_at IS NULL OR {status_col} NOT IN ('answered', 'resolved', 'closed_by_owner'))"
    sql = f"""
        SELECT topic_id,
               SUM(CASE WHEN {ts_col} >= ? AND {unsettled_cond} THEN 1 ELSE 0 END) AS d1_unsettled,
               SUM(CASE WHEN {ts_col} <  ? AND {unsettled_cond} THEN 1 ELSE 0 END) AS prev_unsettled,
               MIN({ts_col}) AS first_at
        FROM escalations
        GROUP BY topic_id
    """
    rows = c.execute(sql, (cutoff_24h, cutoff_24h)).fetchall()
    cutoff_dt = at - timedelta(days=1)
    for r in rows:
        tid = r["topic_id"]
        d1 = int(r["d1_unsettled"] or 0)
        prev = int(r["prev_unsettled"] or 0)
        if d1 < 4:
            continue
        first_dt = parse_iso(r["first_at"])
        if prev > 0 and first_dt is not None and first_dt < cutoff_dt:
            hist_days = max(1.0, (cutoff_dt - first_dt).total_seconds() / 86400.0)
            daily_avg = prev / hist_days
            if d1 >= 2.0 * daily_avg:
                out.append(
                    (
                        tid,
                        f"{tid} 最近 24 小时未结案/超时提问激增（ask_jumps）：{d1} 次（历史未结案日均 {daily_avg:.1f} 次）",
                    )
                )
        else:
            if d1 >= 5:
                out.append(
                    (
                        tid,
                        f"{tid} 最近 24 小时未结案/超时提问激增（ask_jumps）：{d1} 次未结案提问积压",
                    )
                )
    return out


def _resolve_transcript_path(conversation_id: str, antigravity_brain_dir: Path | None = None) -> Path:
    brain_env = os.environ.get("SHEEPDOG_ANTIGRAVITY_BRAIN_DIR")
    base = antigravity_brain_dir or (Path(brain_env) if brain_env else None)
    if base is not None:
        p1 = Path(base) / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        if p1.exists():
            return p1
        p2 = Path(base) / conversation_id / "transcript.jsonl"
        if p2.exists():
            return p2
        return p1
    return agentapi.transcript_path(conversation_id)


def _read_tail_steps(path: Path, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> list[dict]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as f:
            if start > 0:
                f.seek(start)
                f.readline()  # 丢弃可能截断的首行
            raw = f.read()
    except OSError:
        return []
    steps: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict):
            steps.append(d)
    return steps


def _extract_post_dispatch_steps(
    steps: list[dict],
    pending_batch_id: str,
    dispatched_at: str | None,
) -> list[dict]:
    """定位当前批次投递后的所有 transcript steps。"""
    if not steps:
        return []
    if pending_batch_id:
        for idx in range(len(steps) - 1, -1, -1):
            st = steps[idx]
            content = str(st.get("content") or "")
            if pending_batch_id in content:
                return steps[idx:]
    disp_dt = parse_iso(dispatched_at)
    if disp_dt is None:
        return steps
    threshold = disp_dt - timedelta(seconds=2)
    post: list[dict] = []
    for st in steps:
        st_dt = parse_iso(str(st.get("created_at") or st.get("timestamp") or ""))
        if st_dt is not None and st_dt >= threshold:
            post.append(st)
        elif st_dt is None and (
            "The prompt could not be submitted" in str(st.get("content") or "")
            or "The prompt could not be submitted" in str(st.get("error") or "")
        ):
            post.append(st)
    return post


def _write_self_heal_stop_marker(
    state_dir: Path,
    topic_id: str,
    pending_batch_id: str,
    now: datetime,
) -> Path:
    """向 state_dir/hooks/stop/<topic_id>.json 写入轻自愈标记，交由常驻进程 7.64 状态机催交或解锁。"""
    stop_dir = state_dir / "hooks" / "stop"
    stop_dir.mkdir(parents=True, exist_ok=True)
    stop_file = stop_dir / f"{topic_id}.json"
    existing: dict = {}
    if stop_file.exists():
        try:
            loaded = json.loads(stop_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("batch") == pending_batch_id:
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    passed_dt = now - timedelta(seconds=120)
    payload = {
        **existing,
        "batch": pending_batch_id,
        "count": max(int(existing.get("count") or 0), 2),
        "passed_at": iso(passed_dt),
        "self_heal_by": "sheepdog.observe",
        "self_heal_at": iso(now),
    }
    tmp = stop_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(stop_file)
    return stop_file


def run_observe(
    cfg: Config | None = None,
    *,
    db_path: Path | None = None,
    state_path: Path | None = None,
    since_minutes: float = 20.0,
    notify_lark: bool = False,
    notify_chat_id: str = DEFAULT_OWNER_CHAT,
    alert_cooldown_minutes: float = 60.0,
    simulate_warn: list[str] | None = None,
    self_heal: bool = False,
    now_dt: datetime | None = None,
    antigravity_brain_dir: Path | None = None,
    repo_dir: Path | None = None,
    check_processes: bool = True,
    check_log: bool = True,
    check_release_bot: bool = False,
    ps_output: str | None = None,
    log_path: Path | None = None,
    roster_path: Path | None = None,
) -> dict:
    """执行 L1–L4 深度巡检并返回结构化字典。"""
    ref_now = now_dt or now_tz()
    state_dir = db_path.parent if db_path is not None else (cfg.state_dir if cfg is not None else DEFAULT_DB.parent)
    db = db_path or (cfg.db_path if cfg is not None else DEFAULT_DB)
    st_path = state_path or (state_dir / "watchdog-cursor.json")
    log_file = log_path or DEFAULT_LOG
    roster_file = roster_path or (
        cfg.roster_file if (cfg is not None and getattr(cfg, "roster_file", None)) else DEFAULT_ROSTER
    )
    media_dir = state_dir / "media" if state_dir else DEFAULT_MEDIA
    owner_id = (
        cfg.self_open_id if (cfg is not None and getattr(cfg, "self_open_id", None)) else DEFAULT_OWNER
    )
    if not notify_chat_id and cfg is not None and getattr(cfg, "owner_chat_ids", None):
        notify_chat_id = cfg.owner_chat_ids[0]
    bus_topic_id = "tp_bus"
    default_ack_reasons = set(_DEFAULT_ACK_REASONS)
    if cfg is not None and cfg.ack.enabled:
        ack_enabled = True
        ack_reasons = (set(cfg.ack.reasons or ()) | set(cfg.ack.bot_reasons or ())) or default_ack_reasons
    elif cfg is not None and getattr(cfg, "config_path", None) and cfg.config_path.exists():
        ack_enabled = False
        ack_reasons = default_ack_reasons
    else:
        ack_enabled = True
        ack_reasons = default_ack_reasons

    st: dict = {}
    if st_path.exists():
        try:
            st = json.loads(st_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            st = {}
    since = st.get("since") or iso(ref_now - timedelta(minutes=since_minutes))
    if not now_dt:
        since = max(since, GOLIVE)

    warn: list[str] = []
    info: list[str] = []
    self_healed: list[str] = []

    disk_ver_info = version_info(repo_dir or default_repo_dir(), prefer_disk_version=True)

    # ==================== L1: 进程与版本一致性 ====================
    if check_processes:
        if ps_output is None:
            try:
                ps_output = subprocess.run(
                    ["ps", "-axo", "pid,lstart,command"],
                    capture_output=True, text=True, timeout=10, check=False,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                ps_output = ""
        run_procs = [
            l for l in ps_output.splitlines()
            if re.search(r"(?:/sheepdog|sheepdog(?:\.cli)?)\s+run(?:\s|$)", l) and "grep" not in l
        ]
        consumer_procs = [
            l for l in ps_output.splitlines()
            if "lark-cli event consume" in l and "grep" not in l
        ]
        if len(run_procs) != 1:
            if not run_procs:
                warn.append("正式常驻进程数量=0（应为 1）：`sheepdog run` 不在")
            else:
                pids = ", ".join(r.split()[0] for r in run_procs)
                warn.append(f"正式常驻进程数量={len(run_procs)}（应为 1，PIDs: {pids}）")
        else:
            parts = run_procs[0].split()
            info.append(f"常驻进程 {parts[0]}（起于 {' '.join(parts[1:6])}）")
        if not consumer_procs:
            warn.append("lark-cli event consume 没在跑")

    if check_log:
        try:
            lines = log_file.read_text(errors="ignore").splitlines()
        except OSError as e:
            lines = []
            warn.append(f"读不到日志 {log_file}：{e}")
        if lines:
            last_dispatch = next((l for l in reversed(lines) if " INFO dispatch:" in l), "")
            if last_dispatch:
                try:
                    t = datetime.strptime(last_dispatch[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    m = (ref_now - t).total_seconds() / 60.0
                    (warn if m > 5 else info).append(f"最近一轮在 {m:.0f} 分钟前（{last_dispatch[11:19]}）")
                except ValueError:
                    pass
            else:
                warn.append("日志里找不到任何一轮 dispatch")
            since_log = since[:19].replace("T", " ")
            recent, cur = [], ""
            for l in lines:
                if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", l):
                    cur = l[:19]
                if cur >= since_log:
                    recent.append(l)
            errs = [l for l in recent if " ERROR " in l or "Traceback" in l or " CRITICAL " in l]
            if errs:
                warn.append(f"日志里有 {len(errs)} 行 ERROR/Traceback，最近一行：{errs[-1][:200]}")
            media_fail = [l for l in recent if "图片" in l and ("失败" in l or "超限" in l)]
            if media_fail:
                warn.append(f"图片下载失败 {len(media_fail)} 次，最近：{media_fail[-1][:200]}")

    version_mismatch_since = st.get("version_mismatch_since", "")
    seen_ask_jump = dict(st.get("ask_jump") or {})

    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            tabs = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            # L1.3: daemon_runtime vs 磁盘版本一致性 + 远端可用版本提示
            d_warns, d_infos, version_mismatch_since = version_watch.check_version_drift(
                c, disk_ver_info, version_mismatch_since, ref_now
            )
            warn.extend(d_warns)
            info.extend(d_infos)

            # 卡住的 attention / lost 会话
            if "topics" in tabs:
                for r in c.execute(
                    "SELECT topic_id, title, state, attention_at, attention_reason FROM topics "
                    "WHERE state IN ('attention','lost') AND COALESCE(kind, '') != 'known'"
                ):
                    m = age_min(r["attention_at"], ref_now) if r["attention_at"] else None
                    txt = (
                        f"会话 {r['topic_id']}「{r['title']}」{r['state']}（{r['attention_reason'] or '原因没记'}）"
                        + (f"，已 {m:.0f} 分钟" if m is not None else "")
                    )
                    (warn if (m is None or m >= 30) else info).append(txt)

            # ==================== L2: 端到端四阶段水流 SLA ====================
            # 阶段 1: Ingest Ack SLA (90s) —— 固定 30 分钟滚动窗口，不受 since 推进截断
            if ack_enabled and "messages" in tabs and "acks" in tabs:
                ack_window_start = iso(ref_now - timedelta(minutes=30))
                if iso(ref_now) >= GOLIVE:
                    ack_window_start = max(GOLIVE, ack_window_start)
                ack_candidates = c.execute(
                    """
                    SELECT m.message_id, m.topic_id, m.chat_type, m.reason, m.tags_json, m.create_time, m.update_time
                    FROM messages m
                    LEFT JOIN acks a ON a.message_id = m.message_id
                    WHERE m.create_time >= ?
                      AND m.route = 'dispatch'
                      AND COALESCE(m.chat_type, '') != 'system'
                      AND COALESCE(m.security_action, 'none') != 'hold'
                      AND COALESCE(m.deleted, 0) = 0
                      AND m.dispatch_state IN ('pending', 'delivered', 'acked')
                      AND a.message_id IS NULL
                    """,
                    (ack_window_start,),
                ).fetchall()
                bad_acks: list[tuple[sqlite3.Row, float]] = []
                for r in ack_candidates:
                    if not _should_expect_ack(r, ack_reasons):
                        continue
                    age_s = age_seconds(r["create_time"], ref_now)
                    if age_s is not None and age_s >= 90:
                        bad_acks.append((r, age_s))
                if bad_acks:
                    ex_row, ex_age = bad_acks[0]
                    warn.append(
                        f"入账超 90s 未点 [Get] 表情（7.67 Ingest Ack SLA）：{len(bad_acks)} 条，"
                        f"例 {ex_row['topic_id']} {ex_row['message_id'][-8:]} ({int(ex_age)}s)"
                    )

            # 阶段 2: Priority SLA (90s)
            priority_msg_ids: set[str] = set()
            if "messages" in tabs:
                prio_rows = c.execute(
                    """
                    SELECT message_id, topic_id, reason, tags_json, create_time
                    FROM messages
                    WHERE route = 'dispatch'
                      AND dispatch_state = 'pending'
                      AND COALESCE(security_action, 'none') != 'hold'
                      AND COALESCE(deleted, 0) = 0
                      AND (
                          reason IN ('owner_instruction', 'owner_reply')
                          OR COALESCE(tags_json, '') LIKE '%"owner_instruction"%'
                      )
                    """
                ).fetchall()
                for r in prio_rows:
                    priority_msg_ids.add(r["message_id"])
                    age_s = age_seconds(r["create_time"], ref_now)
                    if age_s is not None and age_s >= 90:
                        warn.append(
                            f"主人指令/回复 pending 超 90s 未插话送达（Priority SLA）："
                            f"{r['topic_id']} {r['message_id'][-8:]} reason={r['reason']} ({int(age_s)}s)"
                        )

            # 阶段 3: Queue Head-of-Line SLA (5 分钟 = 300s)
            if "messages" in tabs:
                pending_rows = c.execute(
                    """
                    SELECT message_id, topic_id, reason, tags_json, create_time
                    FROM messages
                    WHERE route = 'dispatch'
                      AND dispatch_state = 'pending'
                      AND COALESCE(security_action, 'none') != 'hold'
                      AND COALESCE(deleted, 0) = 0
                    """
                ).fetchall()
                by_topic: dict[str, list[float]] = {}
                for r in pending_rows:
                    if r["message_id"] in priority_msg_ids:
                        continue
                    age_s = age_seconds(r["create_time"], ref_now)
                    if age_s is None:
                        continue
                    by_topic.setdefault(r["topic_id"] or "-", []).append(age_s)
                for tid, ages in by_topic.items():
                    max_age_s = max(ages)
                    if max_age_s >= 300:
                        t_row = (
                            c.execute(
                                "SELECT state, pending_batch_id, dispatched_at FROM topics WHERE topic_id = ?",
                                (tid,),
                            ).fetchone()
                            if "topics" in tabs
                            else None
                        )
                        pending_batch = (t_row["pending_batch_id"] if t_row else None) or "无"
                        topic_state = (t_row["state"] if t_row else None) or "unknown"
                        warn.append(
                            f"消息排队超 5 分钟（Queue Head-of-Line SLA 队头阻塞）：{tid} {len(ages)} 条 "
                            f"最久 {int(max_age_s // 60)} 分钟（阻塞批次={pending_batch} state={topic_state}）"
                        )

            # 阶段 4: Owner Echo SLA (15 分钟 = 900s)
            if "owner_echoes" in tabs:
                opens = c.execute(
                    "SELECT message_id, stage, topic_id, said_at FROM owner_echoes WHERE settled_at IS NULL"
                ).fetchall()
                for r in opens:
                    age_s = age_seconds(r["said_at"], ref_now)
                    if age_s is not None and age_s >= 900:
                        warn.append(
                            f"主人说话回音超 15 分钟未结清（Owner Echo SLA）："
                            f"{r['topic_id'] or '-'} {r['message_id'][-8:]} 已 {int(age_s // 60)} 分钟（stage={r['stage']}）"
                        )
                for r in c.execute(
                    "SELECT message_id, batch_id, relay_batch, settled_at FROM owner_echoes "
                    "WHERE settled_by='relay' AND settled_at >= ? AND batch_id IS NOT NULL "
                    "AND relay_batch IS NOT NULL AND batch_id != relay_batch AND batch_id NOT LIKE 'p%'",
                    (since,),
                ):
                    warn.append(
                        f"回音卡片贴错了回执：{r['message_id'][-8:]} 是批次 {r['batch_id']} 送的，"
                        f"卡片用的是 {r['relay_batch']} 的 summary（{r['settled_at'][11:16]}）"
                    )
                relays = c.execute("SELECT count(*) FROM owner_echoes WHERE relay_at >= ?", (since,)).fetchone()[0]
                settled = c.execute(
                    "SELECT settled_by, count(*) FROM owner_echoes WHERE settled_at >= ? GROUP BY settled_by",
                    (since,),
                ).fetchall()
                info.append(
                    f"回音：在欠 {len(opens)} 句；这段时间带回卡片 {relays} 句；"
                    f"结清 {dict((x[0], x[1]) for x in settled) or 0}"
                )

            # ==================== L3: 宿主 Transcript 深度探针 ====================
            if "topics" in tabs:
                running_topics = c.execute(
                    "SELECT topic_id, title, conversation_id, host, state, pending_batch_id, dispatched_at "
                    "FROM topics WHERE state = 'running'"
                ).fetchall()
                for t in running_topics:
                    tid = t["topic_id"]
                    cid = (t["conversation_id"] or "").strip()
                    host = (t["host"] or "antigravity").strip().lower()
                    batch_id = (t["pending_batch_id"] or "").strip()
                    disp_at = t["dispatched_at"]
                    batch_age_s = age_seconds(disp_at, ref_now)

                    # 探针 3a: tp_bus 运行时长越界 (150s)
                    if tid == bus_topic_id and batch_age_s is not None and batch_age_s >= 150:
                        warn.append(
                            f"总线超时运行（tp_bus Boundary Guard）：tp_bus 批次 {batch_id} "
                            f"已 running {int(batch_age_s)}s（超过 150s 上限，疑似代替业务会话干重活）"
                        )

                    if host in ("", "antigravity") and cid and batch_age_s is not None:
                        t_path = _resolve_transcript_path(cid, antigravity_brain_dir)
                        steps = _read_tail_steps(t_path)
                        post_steps = _extract_post_dispatch_steps(steps, batch_id, disp_at)

                        running_lambs = 0
                        if "lambs" in tabs:
                            running_lambs = c.execute(
                                "SELECT COUNT(*) FROM lambs WHERE parent_topic_id = ? AND state = 'running'",
                                (tid,),
                            ).fetchone()[0]

                        # 探针 1: Prompt Filter 拦截 / 90s 投递黑洞
                        if batch_age_s >= 90 and t_path.exists():
                            has_prompt_filter = any(
                                "The prompt could not be submitted" in str(s.get("content") or "")
                                or "The prompt could not be submitted" in str(s.get("error") or "")
                                for s in post_steps
                            )
                            if has_prompt_filter:
                                warn.append(
                                    f"宿主 Prompt Filter 拦截：{tid} 批次 {batch_id} 出现 'The prompt could not be submitted'"
                                )
                            elif steps and not post_steps and running_lambs == 0:
                                warn.append(
                                    f"投递黑洞：{tid} 批次 {batch_id} 已送出 {int(batch_age_s)}s，但宿主 transcript 无任何新 step"
                                )

                        # 探针 2: 假 running 僵尸锁 (batch_age >= 180s & PLANNER_RESPONSE DONE 静默 >= 90s)
                        if batch_age_s >= 180 and running_lambs == 0 and post_steps:
                            has_receipt = False
                            if "dispatches" in tabs and batch_id:
                                d_row = c.execute(
                                    "SELECT receipt FROM dispatches WHERE batch_id = ?",
                                    (batch_id,),
                                ).fetchone()
                                if d_row and d_row["receipt"]:
                                    has_receipt = True
                            receipt_file = state_dir / "receipts" / f"{tid}.json"
                            if not has_receipt and receipt_file.exists():
                                try:
                                    rf_data = json.loads(receipt_file.read_text(encoding="utf-8"))
                                    if not batch_id or rf_data.get("batch_id") in (None, "", batch_id):
                                        has_receipt = True
                                except (OSError, json.JSONDecodeError):
                                    pass
                            if not has_receipt:
                                last_step = post_steps[-1]
                                if (
                                    last_step.get("type") == "PLANNER_RESPONSE"
                                    and last_step.get("status") == "DONE"
                                    and not last_step.get("tool_calls")
                                ):
                                    silent_s = age_seconds(str(last_step.get("created_at") or ""), ref_now)
                                    if silent_s is not None and silent_s >= 90:
                                        warn.append(
                                            f"假 running 僵尸锁：{tid} 批次 {batch_id} 已跑 {int(batch_age_s // 60)} 分钟，"
                                            f"transcript 最后一步 PLANNER_RESPONSE 已 DONE 静默 {int(silent_s)}s 但未交回执"
                                        )
                                        if self_heal and state_dir is not None:
                                            _write_self_heal_stop_marker(state_dir, tid, batch_id, ref_now)
                                            self_healed.append(f"{tid}:{batch_id}")
                                            info.append(f"已对 {tid} ({batch_id}) 写入 stop 自愈标记")

                # 探针 3b: tp_bus 工具/命令越权扫描（仅检查当前在途批次或最近 since_minutes 窗口内）
                bus_row = c.execute(
                    "SELECT topic_id, conversation_id, host, state, pending_batch_id, dispatched_at "
                    "FROM topics WHERE topic_id = ?",
                    (bus_topic_id,),
                ).fetchone()
                if bus_row and (bus_row["host"] or "antigravity").strip().lower() in ("", "antigravity"):
                    bus_cid = (bus_row["conversation_id"] or "").strip()
                    if bus_cid:
                        bus_t_path = _resolve_transcript_path(bus_cid, antigravity_brain_dir)
                        bus_steps = _read_tail_steps(bus_t_path)
                        bus_window_dt = ref_now - timedelta(minutes=since_minutes)
                        in_flight_steps: set[int] = set()
                        if (bus_row["state"] or "") == "running" and (bus_row["pending_batch_id"] or "").strip():
                            for st_item in _extract_post_dispatch_steps(
                                bus_steps,
                                (bus_row["pending_batch_id"] or "").strip(),
                                bus_row["dispatched_at"],
                            ):
                                in_flight_steps.add(id(st_item))
                        bad_calls: list[str] = []
                        for s in bus_steps:
                            if s.get("type") != "PLANNER_RESPONSE":
                                continue
                            s_dt = parse_iso(str(s.get("created_at") or ""))
                            if id(s) not in in_flight_steps and (s_dt is None or s_dt < bus_window_dt):
                                continue
                            for tc in s.get("tool_calls") or []:
                                if not isinstance(tc, dict):
                                    continue
                                t_name = str(tc.get("name") or tc.get("tool_name") or "")
                                t_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                                if t_name in _BUS_FORBIDDEN_TOOLS:
                                    bad_calls.append(t_name)
                                elif t_name == "run_command":
                                    cmd_str = normalize(
                                        decode_command_line(t_args.get("CommandLine") or t_args.get("command") or "")
                                    )
                                    if _is_bus_forbidden_command(cmd_str):
                                        bad_calls.append(f"run_command({cmd_str[:60]})")
                        if bad_calls:
                            warn.append(
                                f"总线越权干活（tp_bus Boundary Guard）：检测到 tp_bus 调用越界工具/命令 {', '.join(bad_calls)}"
                            )

            # ==================== L4: 噪音剃刀与核心不变量 ====================
            # L4.2: ask_jumps 噪音剃刀（仅统计未结案/超时异常提问；持续入 warn 由指纹冷却防重复飞书通知与防假恢复）
            if "escalations" in tabs:
                today = ref_now.strftime("%Y-%m-%d")
                for tid, w in ask_jumps(c, ref_now):
                    warn.append(w)
                    seen_ask_jump[tid] = today

            # owner_notices 失败检查
            if "owner_notices" in tabs:
                bad = c.execute(
                    "SELECT id, kind, state, attempts, last_error, created_at FROM owner_notices "
                    "WHERE state NOT IN ('sent','acked','closed') AND created_at < ?",
                    (iso(ref_now - timedelta(minutes=5)),),
                ).fetchall()
                for r in bad:
                    warn.append(
                        f"给你的通知 #{r['id']}（{r['kind']}）{r['state']}，试了 {r['attempts']} 次："
                        f"{(r['last_error'] or '')[:120]}"
                    )
                sent = c.execute(
                    "SELECT kind, count(*) FROM owner_notices WHERE sent_at >= ? GROUP BY kind",
                    (since,),
                ).fetchall()
                if sent:
                    info.append("这段时间发给你的通知：" + "，".join(f"{k} {n}" for k, n in sent))

            # 7.53: 主人私聊无引用消息必须落为 owner_instruction 或 pending_query
            if "messages" in tabs:
                rows = c.execute(
                    """
                    SELECT message_id, create_time, reason, tags_json, substr(content,1,30) t FROM messages
                    WHERE sender_id=? AND chat_id=? AND create_time>=?
                      AND (reply_to IS NULL OR reply_to='') AND (thread_id IS NULL OR thread_id='')
                      AND content NOT IN ('待办') AND COALESCE(msg_type,'') != 'interactive'
                      AND COALESCE(reason, '') != 'agent_reply_in_owner_chat'
                    """,
                    (owner_id, notify_chat_id, since),
                ).fetchall()
                for r in rows:
                    if r["reason"] not in ("owner_instruction", "pending_query"):
                        warn.append(
                            f"私聊里没引用的「{r['t']}」({r['create_time'][11:16]}) 走成了 {r['reason']}，不是指令（7.53 回归？）"
                        )
                if rows and not any(r["reason"] not in ("owner_instruction", "pending_query") for r in rows):
                    info.append(f"私聊里没引用的话 {len(rows)} 句，都按指令走")

                # 7.56: 测试群不该进正式会话
                leak = c.execute(
                    """
                    SELECT chat_name, count(*) FROM messages
                    WHERE chat_name LIKE '🧪%' AND create_time>=? AND route='dispatch'
                    GROUP BY chat_name
                    """,
                    (since,),
                ).fetchall()
                for name, n in leak:
                    warn.append(f"测试群「{name}」有 {n} 条又送进了正式会话（7.56 回归？）")

            # 7.54: 图片统计
            n_media = sum(1 for p in media_dir.rglob("*") if p.is_file()) if media_dir.exists() else 0
            info.append(f"本机已存图片 {n_media} 张")

            # 7.61 & 7.65: 名册 VIP 请求底线检查
            vips: dict[str, str] = {}
            if roster_file and roster_file.exists():
                try:
                    import tomllib
                    roster_data = tomllib.loads(roster_file.read_text(encoding="utf-8"))
                    vips = {
                        p["open_id"]: p.get("name") or p["open_id"]
                        for p in roster_data.get("person", [])
                        if p.get("tier") == "vip" and p.get("open_id")
                    }
                except (OSError, ValueError, KeyError) as e:
                    warn.append(f"读不了名册里的 VIP：{e}")
            if vips and "escalation_sources" in tabs and "escalations" in tabs and "sends" in tabs:
                qs = ",".join("?" * len(vips))
                sends_cols = {r[1] for r in c.execute("PRAGMA table_info(sends)")}
                floor_sql = "COALESCE(s.floor,'')" if "floor" in sends_cols else "''"
                nofloor = c.execute(
                    f"""
                    SELECT DISTINCT e.message_id, e.topic_id, e.asked_at, m.sender_id
                    FROM escalations e
                    JOIN escalation_sources es ON es.ask_message_id = e.message_id
                    JOIN messages m ON m.message_id = es.about_message_id
                    LEFT JOIN sends s ON s.message_id = e.message_id AND s.kind = 'ask'
                    WHERE e.asked_at >= ? AND m.sender_id IN ({qs}) AND {floor_sql} = ''
                    """,
                    (since, *vips),
                ).fetchall()
                for r in nofloor:
                    warn.append(
                        f"VIP（{vips.get(r['sender_id'])}）的请求又去找你批了，没写碰到哪条底线："
                        f"{r['topic_id']} {r['asked_at'][11:16]}（7.61 回归？）"
                    )

            # 7.63: 代回卡片与 CLI 羔羊不变量
            if "sends" in tabs and "messages" in tabs:
                non_card = c.execute(
                    """
                    SELECT s.message_id, s.topic_id, s.sent_at, m.msg_type FROM sends s
                    JOIN messages m ON m.message_id = s.message_id
                    WHERE s.kind = 'reply' AND s.sent_at >= ?
                      AND COALESCE(m.msg_type, '') != 'interactive'
                      AND COALESCE(m.content, '') NOT LIKE '<card%'
                    """,
                    (since,),
                ).fetchall()
                for r in non_card:
                    warn.append(
                        f"代回没用卡片：{r['topic_id']} {r['sent_at'][11:16]}（{r['message_id'][-8:]}，msg_type={r['msg_type'] or '-'}）"
                    )
            if "lambs" in tabs:
                dup_cids = c.execute(
                    """
                    SELECT parent_topic_id, conversation_id, count(*) n FROM lambs
                    WHERE COALESCE(conversation_id, '') != ''
                    GROUP BY parent_topic_id, conversation_id
                    HAVING count(*) > 1 AND max(started_at) >= ?
                    """,
                    (since,),
                ).fetchall()
                for r in dup_cids:
                    warn.append(
                        f"羔羊同一 conversation_id 重复登记 {r['n']} 行：{r['parent_topic_id']} cid={r['conversation_id'][:8]}"
                    )
                for r in c.execute(
                    """
                    SELECT lamb_id, parent_topic_id, pid, started_at FROM lambs
                    WHERE state = 'running' AND kind IN ('cli', 'bash_worker') AND pid IS NOT NULL AND started_at < ?
                    """,
                    (iso(ref_now - timedelta(minutes=2)),),
                ).fetchall():
                    try:
                        os.kill(int(r["pid"]), 0)
                    except ProcessLookupError:
                        warn.append(
                            f"CLI 羔羊 {r['lamb_id']}（{r['parent_topic_id']}，pid={r['pid']}）进程已退出但状态仍为 running"
                        )
                    except PermissionError:
                        pass
        finally:
            c.close()

    if check_release_bot:
        rel_env = os.environ.get("SHEEPDOG_RELEASE_TRANSCRIPT")
        rel_path = Path(rel_env) if rel_env else None
        h = ref_now.hour
        if rel_path is not None and (0 <= h <= 3 or 8 <= h <= 23):
            try:
                m = (ref_now.timestamp() - rel_path.stat().st_mtime) / 60.0
                if m > 12:
                    warn.append(f"发版bot 已经 {m:.0f} 分钟没动静（巡检每 5 分钟一次，可能又停了）")
            except OSError:
                warn.append("读不到发版bot 的会话记录")

    if simulate_warn:
        warn.extend(simulate_warn)

    prev_fp = st.get("active_alert_fp", "")
    prev_at = st.get("last_alert_at", "")
    prev_warns = st.get("active_alert_warns", [])
    active_alert_fp = prev_fp
    last_alert_at = prev_at
    active_alert_warns = prev_warns
    notify_line = ""

    if notify_lark:
        fp = _fingerprint_warns(warn)
        if fp != "":
            need_send = (
                (fp != prev_fp)
                or (prev_at == "")
                or ((age_min(prev_at, ref_now) or 999999) >= alert_cooldown_minutes)
            )
            if need_send:
                title = f"🐕 看门狗告警 · 牧羊犬体检发现 {len(warn)} 个问题 [{bus_topic_id}]"
                warn_lines = "\n".join(f"⚠ {w}" for w in warn)
                info_lines = "\n".join(f"· {i}" for i in info if i)
                body_parts = [
                    f"**时间**：{ref_now.strftime('%Y-%m-%d %H:%M:%S')}",
                    warn_lines,
                ]
                if info_lines:
                    body_parts.append(f"**运行摘要**：\n{info_lines}")
                body_parts.append(f"（同指纹告警冷却 {int(alert_cooldown_minutes)} 分钟；恢复正常后将自动通知）")
                body_md = "\n\n".join(body_parts)
                if _send_lark_card(notify_chat_id, title, "orange", body_md):
                    active_alert_fp = fp
                    last_alert_at = iso(ref_now)
                    active_alert_warns = list(warn)
                    notify_line = f"已发送飞书看门狗告警（fp={fp}）"
            else:
                active_alert_fp = prev_fp
                last_alert_at = prev_at
                active_alert_warns = list(warn)
                notify_line = f"看门狗告警冷却中（上次发送于 {prev_at[11:16]}，fp={fp}）"
        else:
            if prev_fp != "":
                title = f"🐕 看门狗恢复 · 牧羊犬体检已恢复正常 [{bus_topic_id}]"
                prev_warn_lines = "\n".join(f"- {w}" for w in (prev_warns or ["（上一轮告警）"]))
                info_lines = "\n".join(f"- · {i}" for i in info if i) or "- · 正常"
                body_md = (
                    f"**已解除的告警**：\n{prev_warn_lines}\n\n"
                    f"**当前状态**：\n{info_lines}"
                )
                if _send_lark_card(notify_chat_id, title, "green", body_md):
                    active_alert_fp = ""
                    last_alert_at = ""
                    active_alert_warns = []
                    notify_line = "已发送飞书看门狗恢复通知"

    if notify_line:
        info.append(notify_line)

    st_path.parent.mkdir(parents=True, exist_ok=True)
    st_path.write_text(
        json.dumps(
            {
                "since": iso(ref_now),
                "checked_at": iso(ref_now),
                "ask_jump": seen_ask_jump,
                "version_mismatch_since": version_mismatch_since,
                "active_alert_fp": active_alert_fp,
                "last_alert_at": last_alert_at,
                "active_alert_warns": active_alert_warns,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return {
        "ok": len(warn) == 0,
        "checked_at": iso(ref_now),
        "version": disk_ver_info,
        "warns": warn,
        "infos": [i for i in info if i],
        "self_healed": self_healed,
    }


def add_observe_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--db", default=None, help="指定 SQLite 账本路径（默认读配置或正式库）")
    ap.add_argument("--state", default=None, help="游标状态文件路径（默认 state_dir/watchdog-cursor.json）")
    ap.add_argument("--since-minutes", type=float, default=20.0, help="首次无游标时往前看多少分钟（默认 20）")
    ap.add_argument("--notify-lark", action="store_true", help="开启飞书直发告警与恢复通知")
    ap.add_argument("--notify-chat-id", default=DEFAULT_OWNER_CHAT, help="飞书通知目标 chat_id")
    ap.add_argument("--alert-cooldown-minutes", type=float, default=60.0, help="相同指纹告警冷却分钟数")
    ap.add_argument("--simulate-warn", action="append", default=[], help="注入模拟告警字符串（用于自测）")
    ap.add_argument("--json", action="store_true", help="以 JSON 格式输出体检结果")
    ap.add_argument("--self-heal", action="store_true", help="对做完活忘交回执的假 running 会话写入 stop hook 自愈标记")


def render_observe_output(res: dict, *, as_json: bool = False, now_dt: datetime | None = None) -> str:
    if as_json:
        return json.dumps(res, ensure_ascii=False, indent=2)
    ref = now_dt or now_tz()
    warns = res.get("warns") or []
    infos = res.get("infos") or []
    lines = [f"{ref.strftime('%m-%d %H:%M')} 牧羊犬体检：" + ("正常" if not warns else f"{len(warns)} 个问题")]
    for w in warns:
        lines.append(f"⚠ {w}")
    for i in infos:
        if i:
            lines.append(f"· {i}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="正式牧羊犬深度水位巡检（Watchdog 2.0）")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径")
    add_observe_arguments(ap)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception:
        cfg = None
    db_path = Path(args.db).expanduser() if getattr(args, "db", None) else None
    state_path = Path(args.state).expanduser() if args.state else None
    res = run_observe(
        cfg,
        db_path=db_path,
        state_path=state_path,
        since_minutes=args.since_minutes,
        notify_lark=args.notify_lark,
        notify_chat_id=args.notify_chat_id,
        alert_cooldown_minutes=args.alert_cooldown_minutes,
        simulate_warn=args.simulate_warn,
        self_heal=args.self_heal,
    )
    print(render_observe_output(res, as_json=args.json))
    return 1 if res["warns"] else 0


if __name__ == "__main__":
    sys.exit(main())
