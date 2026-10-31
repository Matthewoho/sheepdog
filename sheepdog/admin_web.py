"""Sheepdog 管理与可观测工作台后端（7.99 REG-067）。

基于 Python 标准库 ThreadingHTTPServer 的零外部依赖控制面与可观测服务：
1. 三级会话工作状态（Session Group -> Primary Session -> Subagent / CLI Lamb，联动 duty_cards、hooks/stop、stop-log 与 transcript.jsonl）
2. 端到端 + 会话内各事件耗时瀑布流（T0 飞书发出 -> T1 扫到 -> T2 点 Get 表情 -> T3 投递会话 -> T4 会话内思考/工具/回执 -> T5 回复飞书）
3. 三层决策数据可观测（找人决策 Escalations 采纳/推翻错题本/旁路结案/自治晋升候选 + 系统/tp_bus 路由决策 + Stop Hook/门禁拦截决策）
4. 日常管理与运维干预（VIP 花名册 CRUD、待拍板单结案、在办事项干预、Hook 阻断清理、状态目录与仓库源码全高预览）
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, default_repo_dir, duty, session as sm
from .config import Config, default_config_path, load_config
from .hook import call_log_dir, clear_stop_state, count_path
try:
    from .people_roster import list_people_roster, lookup_person, upsert_person_alias
except ImportError:  # pragma: no cover
    def list_people_roster(*a: Any, **kw: Any) -> list[dict]: return []
    def lookup_person(*a: Any, **kw: Any) -> dict | None: return None
    def upsert_person_alias(*a: Any, **kw: Any) -> dict: return {}
from .roster import load_roster
from .sink.agentapi import transcript_path
from .store import Store, is_standing_topic, now_iso


_EMOJI_STRIP_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+",
    re.UNICODE,
)
_BOILERPLATE_RE = re.compile(r"ljl/matthew|P1-P8|MER-108|护栏正常", re.I)
_OPTION_LINE_RE = re.compile(r"(?:^|\n)\s*([A-E])[\s:：.、)\]]+([^\n]{1,120})")
_RECOMMEND_RE = re.compile(
    r"(?:建议|推荐|优先|默认)[ \t]*(?:选|选择|按|走|用)?[ \t:`*「『（(]*([A-E])\b|([A-E])[ \t:：.、)\]]+[^\n]{0,60}\(\s*Recommended\s*\)",
    re.I,
)
_CALLBACK_CHOICE_RE = re.compile(r"\[卡片回调\]\s*([A-E]|其他)\s*[：:]\s*(.*)", re.S)
_ACCEPT_WORDS_RE = re.compile(
    r"^(?:好|好的|可以|行|同意|没问题|按你建议|都按你的建议|按建议|照办|批准|确认|ok|yes|收到)\b",
    re.I,
)
_OVERRIDE_WORDS_RE = re.compile(
    r"(?:忽略|不用|取消|算了|不对|不要|别|暂停|暂缓|推翻|改用|换成|测试过了吗|不用我同意|已经过时间)",
    re.I,
)


def clean_ui_text(val: Any, max_len: int = 0) -> str:
    """净化展示文本：剔除彩色 Emoji 与违规测试八股词，保持界面极简干净。"""
    s = str(val or "")
    s = _EMOJI_STRIP_RE.sub("", s)
    s = _BOILERPLATE_RE.sub("[已脱敏]", s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    if max_len > 0 and len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except ValueError:
        return None


def diff_ms(start_iso: str | None, end_iso: str | None) -> int | None:
    a, b = parse_iso(start_iso), parse_iso(end_iso)
    if a is None or b is None:
        return None
    delta = int(round((b - a).total_seconds() * 1000))
    return max(0, delta)


def quantile(values: list[int | float], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(math.ceil(q * len(s)) - 1)))
    return int(round(s[idx]))


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_backfill(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    st = Store(db_path)
    try:
        return st.backfill_dispatch_acked_at()
    finally:
        st.close()


def _load_stop_logs(state_dir: Path, limit: int = 800) -> list[dict]:
    d = call_log_dir(state_dir)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.jsonl"), reverse=True)
    rows: list[dict] = []
    for f in files[:3]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    rows.append(rec)
                    if len(rows) >= limit:
                        return rows
            except ValueError:
                continue
    return rows


def _read_stop_state_file(cfg: Config, topic_id: str) -> dict:
    p = count_path(cfg, topic_id)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_transcript_steps(
    cid: str | None,
    batch_id: str = "",
    max_steps: int = 40,
    tail_bytes: int = 384 * 1024,
) -> list[dict]:
    """从 Antigravity 会话 transcript.jsonl 提取会话内各事件步骤与耗时（思考、工具调用、派生羔羊、回执/回复命令）。"""
    if not cid or not cid.strip():
        return []
    tp = transcript_path(cid.strip())
    if not tp.is_file():
        return []
    try:
        with tp.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - tail_bytes)
            f.seek(start)
            raw_lines = f.read(size - start).split(b"\n")
        if start > 0:
            raw_lines = raw_lines[1:]
    except OSError:
        return []

    parsed: list[dict] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                parsed.append(obj)
        except ValueError:
            continue

    if not parsed:
        return []

    if batch_id:
        anchor = -1
        for i, item in enumerate(parsed):
            content = str(item.get("content") or "")
            if batch_id in content and item.get("type") in ("SYSTEM_MESSAGE", "USER_INPUT"):
                anchor = i
                break
        if anchor >= 0:
            # 截取该批次开始直到下一个批次开始之前的步骤
            sliced = [parsed[anchor]]
            for item in parsed[anchor + 1 :]:
                content = str(item.get("content") or "")
                if "[sheepdog] 新信号批次 " in content and batch_id not in content:
                    break
                sliced.append(item)
            parsed = sliced
        else:
            parsed = parsed[-max_steps:]
    else:
        parsed = parsed[-max_steps:]

    steps: list[dict] = []
    for idx, item in enumerate(parsed):
        stype = str(item.get("type") or "STEP")
        created_at = str(item.get("created_at") or "")
        next_at = str(parsed[idx + 1].get("created_at") or "") if idx + 1 < len(parsed) else ""
        dur_ms = diff_ms(created_at, next_at) if next_at else 0
        tool_calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
        content = str(item.get("content") or "")

        kind = "step"
        label = stype
        detail = ""
        if stype == "SYSTEM_MESSAGE":
            if "[sheepdog] 新信号批次" in content:
                kind = "batch_inject"
                m = re.search(r"新信号批次\s+(\S+)", content)
                label = f"注入批次 {m.group(1) if m else ''}".strip()
            elif "Stop hook" in content or "先别结束" in content:
                kind = "hook_block"
                label = "Stop Hook 拦截续跑"
            else:
                kind = "system"
                label = "系统信号注入"
            detail = clean_ui_text(content.splitlines()[0] if content else "", 100)
        elif stype == "USER_INPUT":
            kind = "user_input"
            label = "人工输入 / 唤醒"
            detail = clean_ui_text(content[:100], 100)
        elif stype == "PLANNER_RESPONSE":
            tools = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tname = str(tc.get("name") or tc.get("tool_name") or "")
                args = tc.get("arguments") or tc.get("args") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"raw": args}
                if not isinstance(args, dict):
                    args = {}
                if "run_command" in tname:
                    cmd = str(args.get("CommandLine") or args.get("toolSummary") or "")
                    if "sheepdog reply" in cmd:
                        kind = "sd_reply"
                        tools.append("sheepdog reply (代回飞书)")
                    elif "sheepdog ask" in cmd:
                        kind = "sd_ask"
                        tools.append("sheepdog ask (发起拍板)")
                    elif "sheepdog report" in cmd:
                        kind = "sd_report"
                        tools.append("sheepdog report (汇报进展)")
                    elif "sheepdog receipt" in cmd:
                        kind = "sd_receipt"
                        tools.append("sheepdog receipt (提交回执)")
                    elif "sheepdog forward" in cmd:
                        kind = "sd_forward"
                        tools.append("sheepdog forward (总线转交)")
                    elif "sheepdog lamb" in cmd:
                        kind = "sd_lamb"
                        tools.append("sheepdog lamb (羔羊派单)")
                    else:
                        kind = "tool_cmd"
                        tools.append(f"cmd: {clean_ui_text(args.get('toolSummary') or cmd, 48)}")
                elif "invoke_subagent" in tname:
                    kind = "subagent"
                    tools.append(f"Subagent: {clean_ui_text(args.get('Role') or args.get('TypeName') or 'subagent', 40)}")
                elif tname:
                    if kind == "step":
                        kind = "tool_call"
                    tools.append(clean_ui_text(tname.replace("default_api:", ""), 32))
            if tools:
                label = " · ".join(tools[:2])
                detail = clean_ui_text(content or ("调用 " + ", ".join(tools)), 110)
            else:
                kind = "thinking"
                label = "模型推理与总结"
                detail = clean_ui_text(content or str(item.get("thinking") or ""), 110)
        else:
            continue

        steps.append(
            {
                "step_index": item.get("step_index", idx),
                "kind": kind,
                "label": clean_ui_text(label, 64),
                "detail": detail,
                "at": created_at,
                "duration_ms": dur_ms if dur_ms is not None else 0,
            }
        )

    return steps[-max_steps:]


def classify_escalation(row: sqlite3.Row | dict, answer_content: str = "", ask_floor: str = "") -> dict:
    """将找人决策单归类为：accepted（采纳建议）/ overridden（推翻或改判）/ auto_resolved（旁路结案）/ pending（待拍板）。"""
    text = str(row["text"] or "")
    closed_at = row["closed_at"]
    answered_at = row["answered_at"]
    close_reason = str(row["close_reason"] or "")
    resolved_by = str(row["resolved_by"] or "")
    resolve_note = str(row["resolve_note"] or "")

    options: list[dict] = []
    for m in _OPTION_LINE_RE.finditer(text):
        code, desc = m.group(1).upper(), clean_ui_text(m.group(2), 90)
        if not any(o["code"] == code for o in options):
            options.append({"code": code, "label": desc})

    rec_match = _RECOMMEND_RE.search(text)
    recommended = ""
    if rec_match:
        recommended = (rec_match.group(1) or rec_match.group(2) or "A").upper()
    elif options:
        recommended = "A"

    # 识别找人原因分类
    category = "常规方案拍板"
    if ask_floor:
        if "cred" in ask_floor or "凭据" in ask_floor:
            category = "外发凭据底线"
        elif "destruct" in ask_floor or "删除" in ask_floor or "强推" in ask_floor:
            category = "不可逆操作底线"
        elif "vip" in ask_floor.lower():
            category = "VIP 冲突裁决"
        else:
            category = f"底线授权 ({clean_ui_text(ask_floor, 20)})"
    elif re.search(r"生产|发布|上线|切流|prr|cloud\s*run|deploy", text, re.I):
        category = "生产发布与变更授权"
    elif re.search(r"权限|iam|secret|账号|开通|write", text, re.I):
        category = "权限与安全配置审批"
    elif re.search(r"归档|关闭|清理|删除|回滚", text, re.I):
        category = "清理与生命周期确认"

    ans_clean = clean_ui_text(answer_content, 160)
    chosen_code = ""
    cb_match = _CALLBACK_CHOICE_RE.search(answer_content or "")
    if cb_match:
        chosen_code = cb_match.group(1).upper()
        ans_clean = f"[{chosen_code}] {clean_ui_text(cb_match.group(2), 140)}"

    if not closed_at and not answered_at:
        alignment = "pending"
        alignment_label = "待人类拍板"
    elif answered_at or close_reason == "answered":
        if chosen_code == "其他":
            if _OVERRIDE_WORDS_RE.search(ans_clean) or not _ACCEPT_WORDS_RE.search(ans_clean):
                alignment = "overridden"
                alignment_label = "人类改判 / 补充约束"
            else:
                alignment = "accepted"
                alignment_label = "采纳建议"
        elif chosen_code in ("A", "B", "C", "D", "E"):
            if recommended and chosen_code != recommended:
                alignment = "overridden"
                alignment_label = f"改选 {chosen_code}（原建议 {recommended}）"
            else:
                alignment = "accepted"
                alignment_label = f"采纳建议 ({chosen_code})"
        else:
            if _OVERRIDE_WORDS_RE.search(ans_clean):
                alignment = "overridden"
                alignment_label = "人类推翻 / 改判"
            else:
                alignment = "accepted"
                alignment_label = "采纳建议"
    else:
        alignment = "auto_resolved"
        alignment_label = "旁路自愈结案"
        if not ans_clean:
            ans_clean = clean_ui_text(resolve_note or close_reason or resolved_by or "会话核实后自动结案", 140)

    latency_ms = diff_ms(row["asked_at"], answered_at or closed_at)
    first_line = clean_ui_text(text.splitlines()[0] if text else row["message_id"], 96)

    return {
        "message_id": row["message_id"],
        "topic_id": row["topic_id"],
        "title": first_line,
        "text": clean_ui_text(text, 800),
        "category": category,
        "floor": clean_ui_text(ask_floor, 40),
        "options": options,
        "recommended": recommended,
        "chosen_code": chosen_code,
        "answer_text": ans_clean,
        "alignment": alignment,
        "alignment_label": alignment_label,
        "asked_at": row["asked_at"],
        "answered_at": answered_at,
        "closed_at": closed_at,
        "close_reason": close_reason,
        "resolved_by": resolved_by,
        "nudge_count": int(row["nudge_count"] or 0),
        "latency_ms": latency_ms,
    }


class WorkbenchService:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        try:
            ensure_backfill(self.cfg.db_path)
        except Exception:
            pass

    def _ro(self) -> sqlite3.Connection:
        return _connect_ro(self.cfg.db_path)

    def get_overview(self, minutes: int = 1440) -> dict:
        cutoff = (datetime.now().astimezone() - timedelta(minutes=max(5, minutes))).isoformat(timespec="seconds")
        ws_health: dict[str, Any] = {"status": "online", "conn_id": "", "last_ack_ms": 0, "restarts": 0}
        health_file = self.cfg.state_dir / "bot_ws_health.json"
        if health_file.is_file():
            try:
                raw_h = json.loads(health_file.read_text(encoding="utf-8"))
                if isinstance(raw_h, dict):
                    ws_health.update(raw_h)
            except (OSError, ValueError):
                pass

        conn = self._ro()
        try:
            # 统计各阶段耗时分位数（T0->T1 采集，T1->T2 点表情，T1->T3 投递，T3->T4 会话内执行，T0->T5 端到端回复）
            rows = conn.execute(
                """WITH page AS (
                       SELECT m.message_id, m.create_time, m.first_seen, m.route, m.reason, m.topic_id, m.batch_id
                       FROM messages m
                       WHERE m.chat_type != 'sheepdog' AND m.first_seen >= ?
                       ORDER BY m.first_seen DESC LIMIT 600
                   )
                   SELECT m.message_id, m.create_time, m.first_seen, m.route, m.reason, m.topic_id, m.batch_id,
                          a.added_at AS ack_added_at,
                          d.sent_at AS dispatch_sent_at, d.acked_at AS dispatch_acked_at,
                          (SELECT MIN(s.sent_at) FROM sends s WHERE s.target_message_id = m.message_id AND s.kind='reply') AS reply_sent_at
                   FROM page m
                   LEFT JOIN acks a ON a.message_id = m.message_id
                   LEFT JOIN dispatches d ON d.batch_id = m.batch_id""",
                (cutoff,),
            ).fetchall()

            t0_t1, t1_t2, t1_t3, t3_t4, t0_t5 = [], [], [], [], []
            route_counts = {"dispatch": 0, "inbox": 0, "drop": 0}
            for r in rows:
                rt = r["route"] or "drop"
                route_counts[rt] = route_counts.get(rt, 0) + 1
                if (v := diff_ms(r["create_time"], r["first_seen"])) is not None and v <= 300_000:
                    t0_t1.append(v)
                if (v := diff_ms(r["first_seen"], r["ack_added_at"])) is not None and v <= 300_000:
                    t1_t2.append(v)
                if (v := diff_ms(r["first_seen"], r["dispatch_sent_at"])) is not None and v <= 1_800_000:
                    t1_t3.append(v)
                if (v := diff_ms(r["dispatch_sent_at"], r["dispatch_acked_at"])) is not None and 0 < v <= 3_600_000:
                    t3_t4.append(v)
                if (v := diff_ms(r["create_time"], r["reply_sent_at"])) is not None and 0 < v <= 3_600_000:
                    t0_t5.append(v)

            def _qpack(arr: list[int], label: str) -> dict:
                return {
                    "label": label,
                    "count": len(arr),
                    "p50_ms": quantile(arr, 0.50),
                    "p95_ms": quantile(arr, 0.95),
                    "p99_ms": quantile(arr, 0.99),
                }

            active_topics = conn.execute(
                "SELECT state, COUNT(*) AS c FROM topics WHERE state != 'closed' GROUP BY state"
            ).fetchall()
            state_map = {r["state"]: r["c"] for r in active_topics}
            running_lambs = conn.execute("SELECT COUNT(*) FROM lambs WHERE state='running'").fetchone()[0]
            open_escs = conn.execute("SELECT COUNT(*) FROM escalations WHERE closed_at IS NULL").fetchone()[0]
            open_watches = conn.execute("SELECT COUNT(*) FROM watches WHERE closed_at IS NULL").fetchone()[0]
            doing_works = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE state IN ('doing','waiting_other','waiting_owner')"
            ).fetchone()[0]

            last_commit = ""
            try:
                meta_row = conn.execute("SELECT value FROM meta WHERE key='upgrade_prev_commit'").fetchone()
                if meta_row:
                    last_commit = str(meta_row["value"] or "")[:12]
                ic_row = conn.execute("SELECT value FROM meta WHERE key='identity_consumer'").fetchone()
                if ic_row and ic_row["value"]:
                    ic_obj = json.loads(ic_row["value"])
                    if isinstance(ic_obj, dict):
                        ws_health.update(ic_obj)
                        ws_health["status"] = "online" if ic_obj.get("state") == "running" else "degraded"
            except (sqlite3.OperationalError, ValueError):
                pass
        finally:
            conn.close()

        return {
            "version": __version__,
            "now": now_iso(),
            "rollback_commit": last_commit,
            "ws_health": ws_health,
            "counts": {
                "topics_running": state_map.get("running", 0),
                "topics_active": state_map.get("active", 0),
                "topics_waiting_human": state_map.get("waiting_human", 0),
                "topics_attention": state_map.get("attention", 0),
                "lambs_running": running_lambs,
                "open_escalations": open_escs,
                "open_watches": open_watches,
                "active_work_items": doing_works,
                "messages_window": len(rows),
                "routes": route_counts,
            },
            "latencies": {
                "t0_t1_ingest": _qpack(t0_t1, "T0->T1 飞书采集"),
                "t1_t2_ack": _qpack(t1_t2, "T1->T2 秒点 Get"),
                "t1_t3_dispatch": _qpack(t1_t3, "T1->T3 路由投递"),
                "t3_t4_session": _qpack(t3_t4, "T3->T4 会话执行"),
                "t0_t5_e2e_reply": _qpack(t0_t5, "T0->T5 端到端回复"),
            },
        }

    def get_sessions_tree(self) -> dict:
        """构建三级分形会话状态树：Session Group -> Primary Session -> Subagent / CLI Lamb + Hook 实时遥测。"""
        stop_logs = _load_stop_logs(self.cfg.state_dir, limit=600)
        logs_by_topic: dict[str, list[dict]] = {}
        logs_by_cid: dict[str, list[dict]] = {}
        for rec in stop_logs:
            tid = str(rec.get("topic") or "").strip()
            cid = str(rec.get("conversation_id") or "").strip()
            if tid:
                logs_by_topic.setdefault(tid, []).append(rec)
            if cid:
                logs_by_cid.setdefault(cid, []).append(rec)

        conn = self._ro()
        try:
            topics = conn.execute(
                """SELECT t.*,
                          dc.run_state AS dc_run_state, dc.spinning AS dc_spinning, dc.detail AS dc_detail,
                          dc.action AS dc_action, dc.action_at AS dc_action_at, dc.last_act_at AS dc_last_act_at,
                          dc.last_sig AS dc_last_sig, dc.queued AS dc_queued, dc.stuck_since AS dc_stuck_since,
                          (SELECT COUNT(*) FROM messages m WHERE m.topic_id=t.topic_id AND m.route='dispatch' AND m.dispatch_state='pending' AND m.reason!='owner_context') AS pending_signals,
                          (SELECT COUNT(*) FROM escalations e WHERE e.topic_id=t.topic_id AND e.closed_at IS NULL) AS open_escs,
                          (SELECT COUNT(*) FROM watches w WHERE w.topic_id=t.topic_id AND w.closed_at IS NULL) AS open_watches,
                          (SELECT COUNT(*) FROM work_items wi WHERE wi.topic_id=t.topic_id AND wi.state IN ('doing','waiting_other','waiting_owner')) AS open_works
                   FROM topics t
                   LEFT JOIN duty_cards dc ON dc.topic_id = t.topic_id
                   ORDER BY CASE WHEN t.state='running' THEN 0 WHEN t.state='attention' THEN 1 WHEN t.state='waiting_human' THEN 2 WHEN t.state='active' THEN 3 ELSE 4 END,
                            COALESCE(dc.last_act_at, t.last_active) DESC"""
            ).fetchall()

            lamb_rows = conn.execute(
                "SELECT * FROM lambs ORDER BY CASE WHEN state='running' THEN 0 ELSE 1 END, started_at DESC LIMIT 300"
            ).fetchall()
            lambs_by_parent: dict[str, list[dict]] = {}
            for lb in lamb_rows:
                lid = lb["lamb_id"]
                lcid = str(lb["conversation_id"] or "").strip()
                l_hooks = logs_by_cid.get(lcid, [])[:5] if lcid else []
                lambs_by_parent.setdefault(lb["parent_topic_id"], []).append(
                    {
                        "lamb_id": lid,
                        "parent_topic_id": lb["parent_topic_id"],
                        "kind": lb["kind"],
                        "task_title": clean_ui_text(lb["task_title"], 100),
                        "conversation_id": lcid,
                        "pid": lb["pid"],
                        "state": lb["state"],
                        "started_at": lb["started_at"],
                        "last_act_at": lb["last_act_at"] or lb["started_at"],
                        "finished_at": lb["finished_at"],
                        "result_summary": clean_ui_text(lb["result_summary"], 140),
                        "log_path": lb["log_path"] or "",
                        "hook_calls": len(l_hooks),
                        "last_hook": l_hooks[0] if l_hooks else None,
                    }
                )

            groups_map: dict[str, dict] = {}
            all_sessions: list[dict] = []

            for t in topics:
                tid = t["topic_id"]
                cid = str(t["conversation_id"] or "").strip()
                gid = str(t["group_id"] or "").strip()
                if not gid:
                    if tid == "tp_bus":
                        gid = "bus_core"
                    elif "#" in tid:
                        gid = tid.split("#", 1)[0].removeprefix("tp_")
                    elif is_standing_topic(t):
                        gid = "standing_specialists"
                    else:
                        gid = "dynamic_oneshot"

                stop_st = _read_stop_state_file(self.cfg, tid)
                t_hooks = logs_by_topic.get(tid, []) or (logs_by_cid.get(cid, []) if cid else [])
                last_hook = t_hooks[0] if t_hooks else None
                my_lambs = lambs_by_parent.get(tid, [])
                running_lambs = [x for x in my_lambs if x["state"] == "running"]

                # 综合状态判定
                visual_state = t["state"]
                if t["dc_run_state"] == "working" or t["state"] == "running" or running_lambs:
                    visual_state = "running"
                elif stop_st.get("count", 0) > 0 and not stop_st.get("passed_at"):
                    visual_state = "hook_blocked"
                elif t["state"] == "attention" or t["dc_run_state"] == "stuck":
                    visual_state = "attention"
                elif t["state"] == "waiting_human" or int(t["open_escs"] or 0) > 0:
                    visual_state = "waiting_human"

                sess_obj = {
                    "topic_id": tid,
                    "title": clean_ui_text(t["title"] or tid, 72),
                    "duty": clean_ui_text(t["duty"] or "", 120),
                    "group_id": gid,
                    "kind": t["kind"],
                    "lifecycle": t["lifecycle"] or ("standing" if is_standing_topic(t) else "oneshot"),
                    "host": t["host"] or "antigravity",
                    "model": t["model"] or "claude-opus-4-6",
                    "conversation_id": cid,
                    "state": t["state"],
                    "visual_state": visual_state,
                    "run_state": t["dc_run_state"] or ("working" if t["state"] == "running" else "idle"),
                    "spinning": bool(t["dc_spinning"]),
                    "last_action": clean_ui_text(t["dc_action"] or t["dc_detail"] or "", 110),
                    "last_sig": clean_ui_text(t["dc_last_sig"] or "", 90),
                    "last_active": t["dc_last_act_at"] or t["last_active"] or t["created_at"],
                    "pending_batch_id": t["pending_batch_id"] or "",
                    "dispatched_at": t["dispatched_at"] or "",
                    "pending_signals": int(t["pending_signals"] or 0),
                    "open_escalations": int(t["open_escs"] or 0),
                    "open_watches": int(t["open_watches"] or 0),
                    "open_works": int(t["open_works"] or 0),
                    "hook_state": {
                        "block_count": int(stop_st.get("count") or 0),
                        "batch": str(stop_st.get("batch") or ""),
                        "last_blocked_at": str(stop_st.get("last_blocked_at") or ""),
                        "interrupted": bool(stop_st.get("interrupted")),
                        "recent_calls": len(t_hooks),
                        "last_decision": last_hook.get("decision") if last_hook else "pass",
                        "last_reason": clean_ui_text(last_hook.get("reason") if last_hook else "", 90),
                        "last_at": last_hook.get("at") if last_hook else "",
                    },
                    "lambs": my_lambs[:12],
                    "running_lamb_count": len(running_lambs),
                }
                all_sessions.append(sess_obj)

                g_entry = groups_map.setdefault(
                    gid,
                    {
                        "group_id": gid,
                        "label": self._group_label(gid),
                        "sessions": [],
                        "running_count": 0,
                        "lamb_running_count": 0,
                        "attention_count": 0,
                    },
                )
                g_entry["sessions"].append(sess_obj)
                if visual_state == "running":
                    g_entry["running_count"] += 1
                if visual_state in ("attention", "hook_blocked"):
                    g_entry["attention_count"] += 1
                g_entry["lamb_running_count"] += len(running_lambs)
        finally:
            conn.close()

        groups_sorted = sorted(
            groups_map.values(),
            key=lambda g: (
                0 if g["group_id"] == "bus_core" else (1 if g["running_count"] > 0 else (2 if g["group_id"] == "standing_specialists" else 3)),
                -len(g["sessions"]),
            ),
        )
        return {
            "groups": groups_sorted,
            "sessions": all_sessions,
            "recent_hook_calls": [
                {
                    "at": r.get("at", ""),
                    "topic": r.get("topic") or "",
                    "lamb_id": r.get("lamb_id") or "",
                    "conversation_id": r.get("conversation_id") or "",
                    "batch": r.get("batch") or "",
                    "decision": r.get("decision") or "pass",
                    "reason": clean_ui_text(r.get("reason") or "", 100),
                    "ms": r.get("ms") or 0,
                }
                for r in stop_logs[:40]
            ],
        }

    @staticmethod
    def _group_label(gid: str) -> str:
        labels = {
            "bus_core": "中枢总线 (tp_bus)",
            "standing_specialists": "常驻职能与领域专席 (Standing)",
            "dynamic_oneshot": "动态专项会话 (Oneshot)",
            "laohu": "老胡并发会话组 (Session Group · laohu)",
            "sammy": "Sammy 并发会话组 (Session Group · sammy)",
            "wenbo": "Wenbo 并发会话组 (Session Group · wenbo)",
        }
        return labels.get(gid, f"并发会话组 (Session Group · {gid})")

    def get_session_detail(self, topic_id: str) -> dict:
        conn = self._ro()
        try:
            t = conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if t is None:
                return {"error": f"找不到会话 {topic_id}"}
            dc = conn.execute("SELECT * FROM duty_cards WHERE topic_id=?", (topic_id,)).fetchone()
            dispatches = conn.execute(
                "SELECT * FROM dispatches WHERE topic_id=? ORDER BY sent_at DESC LIMIT 20",
                (topic_id,),
            ).fetchall()
            history = conn.execute(
                "SELECT * FROM duty_history WHERE topic_id=? ORDER BY id DESC LIMIT 35",
                (topic_id,),
            ).fetchall()
            lambs = conn.execute(
                "SELECT * FROM lambs WHERE parent_topic_id=? ORDER BY started_at DESC LIMIT 25",
                (topic_id,),
            ).fetchall()
            works = conn.execute(
                "SELECT * FROM work_items WHERE topic_id=? ORDER BY updated_at DESC LIMIT 20",
                (topic_id,),
            ).fetchall()
            escs = conn.execute(
                "SELECT * FROM escalations WHERE topic_id=? ORDER BY asked_at DESC LIMIT 15",
                (topic_id,),
            ).fetchall()
        finally:
            conn.close()

        cid = str(t["conversation_id"] or "").strip()
        batch_id = str(t["pending_batch_id"] or (dispatches[0]["batch_id"] if dispatches else "")).strip()
        steps = parse_transcript_steps(cid, batch_id=batch_id, max_steps=35)
        stop_logs = [
            r
            for r in _load_stop_logs(self.cfg.state_dir, limit=300)
            if r.get("topic") == topic_id or (cid and r.get("conversation_id") == cid)
        ][:20]

        return {
            "topic": dict(t),
            "duty_card": dict(dc) if dc else {},
            "transcript_steps": steps,
            "dispatches": [
                {
                    "batch_id": d["batch_id"],
                    "sent_at": d["sent_at"],
                    "acked_at": d["acked_at"] if "acked_at" in d.keys() else None,
                    "duration_ms": diff_ms(d["sent_at"], d["acked_at"] if "acked_at" in d.keys() else None),
                    "state": d["state"],
                    "receipt": clean_ui_text(d["receipt"] or "", 140),
                    "error": clean_ui_text(d["error"] or "", 120),
                }
                for d in dispatches
            ],
            "duty_history": [
                {
                    "id": h["id"],
                    "kind": h["kind"],
                    "text": clean_ui_text(h["text"], 160),
                    "at": h["at"],
                }
                for h in history
            ],
            "lambs": [
                {
                    "lamb_id": lb["lamb_id"],
                    "kind": lb["kind"],
                    "task_title": clean_ui_text(lb["task_title"], 100),
                    "conversation_id": lb["conversation_id"] or "",
                    "pid": lb["pid"],
                    "state": lb["state"],
                    "started_at": lb["started_at"],
                    "last_act_at": lb["last_act_at"],
                    "finished_at": lb["finished_at"],
                    "duration_ms": diff_ms(lb["started_at"], lb["finished_at"] or lb["last_act_at"]),
                    "result_summary": clean_ui_text(lb["result_summary"] or "", 160),
                    "log_path": lb["log_path"] or "",
                }
                for lb in lambs
            ],
            "work_items": [dict(w) for w in works],
            "escalations": [classify_escalation(e) for e in escs],
            "hook_logs": stop_logs,
        }

    def get_events_waterfall(self, limit: int = 50, offset: int = 0, route: str = "", q: str = "") -> dict:
        """端到端事件耗时瀑布流列表（T0 发出 -> T1 扫到 -> T2 点 Get -> T3 投递 -> T4 会话回执 -> T5 回复飞书）。"""
        conn = self._ro()
        try:
            where = ["m.chat_type != 'sheepdog'"]
            params: list[Any] = []
            if route and route in ("dispatch", "inbox", "drop"):
                where.append("m.route = ?")
                params.append(route)
            if q and q.strip():
                needle = f"%{q.strip()}%"
                where.append("(m.content LIKE ? OR m.sender_name LIKE ? OR m.chat_name LIKE ? OR m.topic_id LIKE ? OR m.message_id LIKE ?)")
                params.extend([needle, needle, needle, needle, needle])

            where_sql = " AND ".join(where)
            total = conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {where_sql}", params).fetchone()[0]

            rows = conn.execute(
                f"""WITH page AS (
                        SELECT m.message_id, m.chat_id, m.chat_name, m.chat_type, m.sender_id, m.sender_name,
                               m.content, m.create_time, m.first_seen, m.route, m.reason, m.topic_id,
                               m.dispatch_state, m.batch_id, m.tags_json
                        FROM messages m
                        WHERE {where_sql}
                        ORDER BY m.first_seen DESC
                        LIMIT ? OFFSET ?
                    )
                    SELECT m.message_id, m.chat_id, m.chat_name, m.chat_type, m.sender_id, m.sender_name,
                           m.content, m.create_time, m.first_seen, m.route, m.reason, m.topic_id,
                           m.dispatch_state, m.batch_id, m.tags_json,
                           a.added_at AS ack_added_at, a.removed_at AS ack_removed_at, a.identity AS ack_identity,
                           d.sent_at AS dispatch_sent_at, d.acked_at AS dispatch_acked_at, d.state AS batch_state, d.receipt AS batch_receipt,
                           (SELECT MIN(s.sent_at) FROM sends s WHERE s.target_message_id = m.message_id AND s.kind='reply') AS reply_sent_at,
                           (SELECT s.message_id FROM sends s WHERE s.target_message_id = m.message_id AND s.kind='reply' ORDER BY s.sent_at ASC LIMIT 1) AS reply_msg_id
                    FROM page m
                    LEFT JOIN acks a ON a.message_id = m.message_id
                    LEFT JOIN dispatches d ON d.batch_id = m.batch_id""",
                [*params, max(1, min(120, limit)), max(0, offset)],
            ).fetchall()

            events = []
            for r in rows:
                t0 = r["create_time"]
                t1 = r["first_seen"]
                t2 = r["ack_added_at"]
                t3 = r["dispatch_sent_at"]
                t4 = r["dispatch_acked_at"]
                t5 = r["reply_sent_at"]

                # 解析 tags_json 看是否经过 tp_bus 转交（二跳投递）
                hop_from_batch = ""
                try:
                    tags = json.loads(r["tags_json"] or "[]")
                    for tg in tags:
                        if isinstance(tg, str) and tg.startswith("forwarded_from:"):
                            hop_from_batch = tg.split(":", 1)[1]
                except ValueError:
                    pass

                d_ingest = diff_ms(t0, t1)
                d_ack = diff_ms(t1, t2)
                d_queue = diff_ms(t1, t3)
                d_exec = diff_ms(t3, t4)
                d_reply = diff_ms(t0, t5)
                total_ms = d_reply or (diff_ms(t0, t4) if t4 else (diff_ms(t0, t3) if t3 else d_ingest)) or 0

                events.append(
                    {
                        "message_id": r["message_id"],
                        "chat_name": clean_ui_text(r["chat_name"] or r["chat_id"] or "私聊", 36),
                        "chat_type": r["chat_type"] or "p2p",
                        "sender_name": clean_ui_text(r["sender_name"] or r["sender_id"] or "未知", 28),
                        "content_preview": clean_ui_text((r["content"] or "").replace("\n", " "), 72),
                        "route": r["route"] or "drop",
                        "reason": r["reason"] or "",
                        "topic_id": r["topic_id"] or "",
                        "dispatch_state": r["dispatch_state"] or "",
                        "batch_id": r["batch_id"] or "",
                        "hop_from_batch": hop_from_batch,
                        "timestamps": {
                            "t0_created": t0,
                            "t1_scanned": t1,
                            "t2_ack_added": t2,
                            "t2_ack_removed": r["ack_removed_at"],
                            "t3_dispatched": t3,
                            "t4_session_acked": t4,
                            "t5_replied": t5,
                        },
                        "durations_ms": {
                            "ingest": d_ingest,
                            "ack": d_ack,
                            "queue": d_queue,
                            "session_exec": d_exec,
                            "e2e_reply": d_reply,
                            "total": total_ms,
                        },
                        "reply_msg_id": r["reply_msg_id"] or "",
                    }
                )
        finally:
            conn.close()

        return {"total": total, "offset": offset, "limit": limit, "events": events}

    def get_event_trace(self, message_id: str) -> dict:
        """单条飞书消息的完整 6 阶段时间轴 + 会话内 transcript.jsonl 步进瀑布流 + 关联决策单。"""
        conn = self._ro()
        try:
            m = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if m is None:
                return {"error": f"找不到消息 {message_id}"}
            ack = conn.execute("SELECT * FROM acks WHERE message_id=?", (message_id,)).fetchone()
            batch_id = str(m["batch_id"] or "").strip()
            disp = conn.execute("SELECT * FROM dispatches WHERE batch_id=?", (batch_id,)).fetchone() if batch_id else None
            topic = conn.execute("SELECT * FROM topics WHERE topic_id=?", (m["topic_id"],)).fetchone() if m["topic_id"] else None

            # 检查是否经历过 tp_bus 一跳转交
            bus_disp = None
            try:
                tags = json.loads(m["tags_json"] or "[]")
                for tg in tags:
                    if isinstance(tg, str) and tg.startswith("forwarded_from:"):
                        fb = tg.split(":", 1)[1].strip()
                        if fb:
                            bus_disp = conn.execute("SELECT * FROM dispatches WHERE batch_id=?", (fb,)).fetchone()
            except ValueError:
                pass

            sends = conn.execute(
                """SELECT * FROM sends
                   WHERE target_message_id=? OR (topic_id=? AND sent_at >= ? AND sent_at <= COALESCE(?, datetime(?, '+30 minutes')))
                   ORDER BY sent_at ASC LIMIT 10""",
                (
                    message_id,
                    m["topic_id"] or "",
                    m["first_seen"] or m["create_time"],
                    disp["acked_at"] if disp and "acked_at" in disp.keys() and disp["acked_at"] else None,
                    m["first_seen"] or m["create_time"],
                ),
            ).fetchall()

            escs = conn.execute(
                """SELECT e.* FROM escalations e
                   JOIN escalation_sources es ON es.ask_message_id = e.message_id
                   WHERE es.about_message_id = ?""",
                (message_id,),
            ).fetchall()
        finally:
            conn.close()

        cid = str(topic["conversation_id"] or "").strip() if topic else ""
        session_steps = parse_transcript_steps(cid, batch_id=batch_id, max_steps=35) if cid and batch_id else []

        t0 = m["create_time"]
        t1 = m["first_seen"]
        t2 = ack["added_at"] if ack else None
        t3_bus = bus_disp["sent_at"] if bus_disp else None
        t3 = disp["sent_at"] if disp else None
        t4 = disp["acked_at"] if disp and "acked_at" in disp.keys() else None
        reply_rows = [s for s in sends if s["kind"] == "reply" and s["target_message_id"] == message_id] or list(sends)
        t5 = reply_rows[0]["sent_at"] if reply_rows else None

        stages = [
            {"code": "T0", "name": "Lark 消息发出", "at": t0, "delta_ms": 0, "detail": f"发送人：{clean_ui_text(m['sender_name'] or m['sender_id'], 32)}"},
            {"code": "T1", "name": "Sheepdog 扫到入账", "at": t1, "delta_ms": diff_ms(t0, t1), "detail": f"路由：{m['route']} · 原因：{m['reason'] or '-'}"},
            {"code": "T2", "name": "点 Get 确认表情", "at": t2, "delta_ms": diff_ms(t1, t2), "detail": f"身份：{ack['identity'] if ack else '-'} · 撤下：{ack['removed_at'] if ack and ack['removed_at'] else '未撤'}"},
        ]
        if t3_bus:
            stages.append(
                {
                    "code": "T3a",
                    "name": "一跳投递中枢 tp_bus",
                    "at": t3_bus,
                    "delta_ms": diff_ms(t1, t3_bus),
                    "detail": f"批次：{bus_disp['batch_id']} · 回执：{clean_ui_text(bus_disp['receipt'] or '', 60)}",
                }
            )
        stages.append(
            {
                "code": "T3",
                "name": f"投递目标会话 ({m['topic_id'] or '-'})",
                "at": t3,
                "delta_ms": diff_ms(t3_bus or t1, t3),
                "detail": f"批次：{batch_id or '-'} · 状态：{disp['state'] if disp else m['dispatch_state'] or '-'}",
            }
        )
        stages.append(
            {
                "code": "T4",
                "name": "会话完成处理并交回执",
                "at": t4,
                "delta_ms": diff_ms(t3, t4),
                "detail": clean_ui_text(disp["receipt"] if disp and disp["receipt"] else "未交回执或仍在执行", 120),
            }
        )
        stages.append(
            {
                "code": "T5",
                "name": "回复卡片送达 Lark",
                "at": t5,
                "delta_ms": diff_ms(t0, t5),
                "detail": clean_ui_text(reply_rows[0]["text"] if reply_rows else "无需代回或由主人直接回复", 120),
            }
        )

        return {
            "message": {
                "message_id": m["message_id"],
                "chat_id": m["chat_id"],
                "chat_name": clean_ui_text(m["chat_name"] or m["chat_id"], 48),
                "sender_name": clean_ui_text(m["sender_name"] or m["sender_id"], 36),
                "content": clean_ui_text(m["content"], 1000),
                "route": m["route"],
                "reason": m["reason"],
                "topic_id": m["topic_id"] or "",
                "batch_id": batch_id,
            },
            "stages": stages,
            "session_steps": session_steps,
            "sends": [
                {
                    "kind": s["kind"],
                    "message_id": s["message_id"],
                    "sent_at": s["sent_at"],
                    "text": clean_ui_text(s["text"], 160),
                }
                for s in sends
            ],
            "escalations": [classify_escalation(e) for e in escs],
        }

    def get_decisions_analytics(self) -> dict:
        """三层决策数据可观测：1) 找人拍板与人类决定账本；2) 系统/tp_bus 路由决策；3) Hook/安全门禁拦截决策。"""
        conn = self._ro()
        try:
            esc_rows = conn.execute(
                """SELECT e.*,
                          am.content AS answer_content,
                          pm.content AS possible_content,
                          (SELECT s.floor FROM sends s WHERE s.message_id = e.message_id LIMIT 1) AS ask_floor
                   FROM escalations e
                   LEFT JOIN messages am ON am.message_id = e.answer_message_id
                   LEFT JOIN messages pm ON pm.message_id = e.possibly_handled_message_id
                   ORDER BY e.asked_at DESC LIMIT 200"""
            ).fetchall()

            ledger = []
            counts = {"total": len(esc_rows), "accepted": 0, "overridden": 0, "auto_resolved": 0, "pending": 0}
            latencies: list[int] = []
            by_cat_topic: dict[tuple[str, str], dict] = {}
            by_category: dict[str, dict] = {}

            for r in esc_rows:
                ans = str(r["answer_content"] or r["possible_content"] or "")
                item = classify_escalation(r, answer_content=ans, ask_floor=str(r["ask_floor"] or ""))
                ledger.append(item)
                al = item["alignment"]
                counts[al] = counts.get(al, 0) + 1
                if item["latency_ms"] is not None and al in ("accepted", "overridden"):
                    latencies.append(item["latency_ms"])

                cat = item["category"]
                c_stat = by_category.setdefault(cat, {"category": cat, "total": 0, "accepted": 0, "overridden": 0, "auto_resolved": 0, "pending": 0})
                c_stat["total"] += 1
                c_stat[al] = c_stat.get(al, 0) + 1

                key = (item["topic_id"], cat)
                g_stat = by_cat_topic.setdefault(
                    key,
                    {
                        "topic_id": item["topic_id"],
                        "category": cat,
                        "total": 0,
                        "accepted": 0,
                        "overridden": 0,
                        "examples": [],
                    },
                )
                g_stat["total"] += 1
                if al == "accepted":
                    g_stat["accepted"] += 1
                elif al == "overridden":
                    g_stat["overridden"] += 1
                if len(g_stat["examples"]) < 2:
                    g_stat["examples"].append(item["title"])

            human_decided = counts["accepted"] + counts["overridden"]
            acceptance_rate = round(counts["accepted"] * 100.0 / human_decided, 1) if human_decided > 0 else 100.0

            # 自动提炼「免问自治规则晋升候选」：某会话在同一类别下连续 >=3 次被人类采纳且 0 次推翻
            autonomy_candidates = []
            for (tid, cat), st in sorted(by_cat_topic.items(), key=lambda kv: -kv[1]["accepted"]):
                if st["accepted"] >= 3 and st["overridden"] == 0 and "底线" not in cat:
                    autonomy_candidates.append(
                        {
                            "topic_id": tid,
                            "category": cat,
                            "accepted_streak": st["accepted"],
                            "examples": st["examples"],
                            "suggestion": f"会话 {tid} 的「{cat}」已连续 {st['accepted']} 次获人类 100% 采纳（0 改判），建议降级为事后 report 免问自治。",
                        }
                    )

            # Layer 2: 系统路由与 tp_bus 分诊决策统计
            route_reasons = conn.execute(
                """SELECT route, COALESCE(reason, 'unspecified') AS reason, COUNT(*) AS c
                   FROM messages
                   WHERE chat_type != 'sheepdog'
                   GROUP BY route, reason
                   ORDER BY c DESC LIMIT 25"""
            ).fetchall()

            bus_receipts = conn.execute(
                """SELECT receipt FROM dispatches WHERE topic_id='tp_bus' AND state IN ('acked','acked_late')
                   ORDER BY sent_at DESC LIMIT 300"""
            ).fetchall()
            bus_stats = {"forwarded": 0, "handled_direct": 0, "new_session": 0, "ignored_context": 0}
            for br in bus_receipts:
                txt = str(br["receipt"] or "")
                if "转交" in txt or "forward" in txt.lower():
                    bus_stats["forwarded"] += 1
                elif "新开" in txt or "new-session" in txt.lower() or "spawn" in txt.lower():
                    bus_stats["new_session"] += 1
                elif "背景" in txt or "无需" in txt or "知悉" in txt:
                    bus_stats["ignored_context"] += 1
                else:
                    bus_stats["handled_direct"] += 1

            # Layer 3: Hook 拦截与安全护栏记录
            audit_rows = conn.execute(
                "SELECT * FROM audit_hits ORDER BY detected_at DESC LIMIT 15"
            ).fetchall()
        finally:
            conn.close()

        stop_logs = _load_stop_logs(self.cfg.state_dir, limit=500)
        hook_blocks = [
            {
                "at": r.get("at", ""),
                "topic": r.get("topic") or "",
                "batch": r.get("batch") or "",
                "reason": clean_ui_text(r.get("reason") or "", 110),
                "ms": r.get("ms") or 0,
            }
            for r in stop_logs
            if r.get("decision") == "block"
        ]

        return {
            "summary": {
                **counts,
                "human_decided": human_decided,
                "acceptance_rate_pct": acceptance_rate,
                "median_latency_ms": quantile(latencies, 0.50),
                "p90_latency_ms": quantile(latencies, 0.90),
                "hook_block_count": len(hook_blocks),
                "autonomy_candidate_count": len(autonomy_candidates),
            },
            "categories": sorted(by_category.values(), key=lambda x: -x["total"]),
            "autonomy_candidates": autonomy_candidates[:8],
            "ledger": ledger[:80],
            "overridden_book": [x for x in ledger if x["alignment"] == "overridden"][:25],
            "routing_breakdown": [
                {"route": r["route"], "reason": r["reason"], "count": r["c"]} for r in route_reasons
            ],
            "bus_triage": bus_stats,
            "hook_blocks": hook_blocks[:30],
            "audit_hits": [
                {
                    "id": a["id"],
                    "topic_id": a["topic_id"],
                    "command": clean_ui_text(a["command"], 100),
                    "pattern": a["pattern"] or "",
                    "detected_at": a["detected_at"],
                }
                for a in audit_rows
            ],
        }

    def get_admin_state(self) -> dict:
        """获取管理后台状态：VIP 与联系人花名册、静态路由表、在办事项与等待清单。"""
        people_list = []
        try:
            for rec in list_people_roster(self.cfg.db_path):
                rh = str(rec.role_hint or "")
                is_vip = rh.lower().startswith(("vip", "owner"))
                role_clean = rh.split(":", 1)[1] if ":" in rh else rh
                default_tp = next((a for a in rec.aliases if a.startswith("tp_")), "")
                people_list.append(
                    {
                        "person_key": rec.person_key,
                        "name": clean_ui_text(rec.feishu_name or rec.preferred_name or rec.person_key, 40),
                        "preferred_name": clean_ui_text(rec.preferred_name, 30),
                        "aliases": [clean_ui_text(a, 30) for a in rec.aliases],
                        "open_ids": [rec.open_id] if rec.open_id else [],
                        "role": clean_ui_text(role_clean, 60),
                        "team": "VIP" if is_vip else "Colleague",
                        "vip": is_vip,
                        "default_topic": default_tp,
                        "notes": clean_ui_text(rh, 120),
                    }
                )
        except Exception:
            pass
        people_list.sort(key=lambda x: (0 if x["vip"] else 1, x["name"]))

        static_topics = []
        try:
            roster = load_roster(self.cfg.roster_file, False)
            for tid, tspec in roster.topics.items():
                static_topics.append(
                    {
                        "topic_id": tid,
                        "title": clean_ui_text(tspec.title, 60),
                        "duty": clean_ui_text(tspec.duty, 100),
                        "group_id": tspec.group_id or "",
                        "chat_count": len(tspec.chats),
                        "keyword_count": len(tspec.keywords),
                    }
                )
        except Exception:
            pass

        conn = self._ro()
        try:
            works = conn.execute(
                """SELECT topic_id, key, title, state, waiting_on, until, expect, result, updated_at
                   FROM work_items
                   WHERE state IN ('doing','waiting_other','waiting_owner')
                   ORDER BY updated_at DESC LIMIT 60"""
            ).fetchall()
            watches = conn.execute(
                """SELECT id, topic_id, person_id, chat_id, note, started_at, status
                   FROM watches WHERE closed_at IS NULL ORDER BY started_at DESC LIMIT 40"""
            ).fetchall()
            tracked = conn.execute(
                """SELECT item_no, title, owner_topic, state, status, opened_at, last_activity_at
                   FROM tracked_items WHERE state='active' ORDER BY last_activity_at DESC LIMIT 40"""
            ).fetchall()
        finally:
            conn.close()

        return {
            "people": people_list,
            "static_topics": static_topics,
            "work_items": [
                {
                    "topic_id": w["topic_id"],
                    "key": w["key"],
                    "title": clean_ui_text(w["title"], 90),
                    "state": w["state"],
                    "waiting_on": clean_ui_text(w["waiting_on"] or "", 40),
                    "expect": clean_ui_text(w["expect"] or "", 80),
                    "updated_at": w["updated_at"],
                }
                for w in works
            ],
            "watches": [dict(w) for w in watches],
            "tracked_items": [
                {
                    "item_no": f"T-{t['item_no']}",
                    "title": clean_ui_text(t["title"], 80),
                    "owner_topic": t["owner_topic"] or "",
                    "status": clean_ui_text(t["status"] or t["state"], 60),
                    "last_activity_at": t["last_activity_at"] or t["opened_at"],
                }
                for t in tracked
            ],
        }

    def get_workspace_files(self) -> dict:
        """提供右栏 `agy app` 同款全高左右分栏的统一目录树数据（Active Realm 状态与配置 ⊥ Whole Drive 仓库源码）。"""
        active_realm_files = [
            {
                "path": "config/config.toml",
                "name": "config.toml",
                "badge": "TOML",
                "group": "active_realm",
                "real_path": str(default_config_path()),
            },
            {
                "path": "config/roster.toml",
                "name": "roster.toml",
                "badge": "TOML",
                "group": "active_realm",
                "real_path": str(self.cfg.roster_file),
            },
            {
                "path": "config/security.toml",
                "name": "security.toml",
                "badge": "TOML",
                "group": "active_realm",
                "real_path": str(self.cfg.security_file),
            },
            {
                "path": "state/bot_ws_health.json",
                "name": "bot_ws_health.json",
                "badge": "{}",
                "group": "active_realm",
                "real_path": str(self.cfg.state_dir / "bot_ws_health.json"),
            },
        ]
        # 加入最新 stop-log
        log_dir = call_log_dir(self.cfg.state_dir)
        if log_dir.is_dir():
            for lf in sorted(log_dir.glob("*.jsonl"), reverse=True)[:2]:
                active_realm_files.append(
                    {
                        "path": f"hooks/stop-log/{lf.name}",
                        "name": lf.name,
                        "badge": "LOG",
                        "group": "active_realm",
                        "real_path": str(lf),
                    }
                )

        repo_dir = default_repo_dir()
        whole_drive_files = []
        for rel, badge in (
            ("sheepdog/engine.py", "PY"),
            ("sheepdog/hook.py", "PY"),
            ("sheepdog/decision_hub.py", "PY"),
            ("sheepdog/store.py", "PY"),
            ("sheepdog/admin_web.py", "PY"),
            ("tests/REGRESSIONS.md", "MD"),
            ("README.md", "MD"),
        ):
            p = repo_dir / rel
            if p.is_file():
                whole_drive_files.append(
                    {
                        "path": rel,
                        "name": p.name,
                        "badge": badge,
                        "group": "whole_drive",
                        "real_path": str(p),
                    }
                )
        return {"active_realm": active_realm_files, "whole_drive": whole_drive_files}

    def read_workspace_file(self, virtual_path: str) -> dict:
        tree = self.get_workspace_files()
        all_items = tree["active_realm"] + tree["whole_drive"]
        target = next((x for x in all_items if x["path"] == virtual_path), None)
        if target is None:
            target = all_items[0] if all_items else None
        if target is None:
            return {"path": virtual_path, "lines": ["# 暂无可读文件"]}
        rp = Path(target["real_path"])
        if not rp.is_file():
            return {"path": target["path"], "lines": [f"# 文件不存在: {rp}"]}
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[-180:] if target["badge"] == "LOG" else text.splitlines()[:220]
            return {
                "path": target["path"],
                "name": target["name"],
                "badge": target["badge"],
                "lines": [clean_ui_text(line, 240) for line in lines],
            }
        except OSError as e:
            return {"path": target["path"], "lines": [f"# 读取失败: {e}"]}

    def execute_action(self, payload: dict) -> dict:
        """执行管理后台写操作，保持响应精简（< 150 KB）。"""
        action = str(payload.get("action") or "").strip()
        if not action:
            return {"ok": False, "error": "缺少 action 参数"}

        if action == "toggle_vip":
            name = str(payload.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "缺少联系人名称"}
            cur = lookup_person(name, self.cfg.db_path)
            if cur is None:
                rec = upsert_person_alias(self.cfg.db_path, target=name, feishu_name=name, role_hint=f"vip:{name}")
                return {"ok": True, "action": action, "name": name, "vip": True, "role_hint": rec.role_hint}
            rh = str(cur.role_hint or "")
            desc = rh.split(":", 1)[1] if ":" in rh else (rh or cur.preferred_name)
            was_vip = rh.lower().startswith(("vip", "owner"))
            new_rh = f"colleague:{desc}" if was_vip else f"vip:{desc}"
            rec = upsert_person_alias(self.cfg.db_path, target=cur.person_key, role_hint=new_rh)
            return {"ok": True, "action": action, "name": name, "vip": not was_vip, "role_hint": rec.role_hint}

        if action == "clear_hook_block":
            topic_id = str(payload.get("topic_id") or "").strip()
            if not topic_id:
                return {"ok": False, "error": "缺少 topic_id"}
            clear_stop_state(self.cfg.state_dir, topic_id)
            return {"ok": True, "action": action, "topic_id": topic_id}

        if action == "set_topic_state":
            topic_id = str(payload.get("topic_id") or "").strip()
            new_state = str(payload.get("state") or "active").strip()
            if new_state not in ("active", "running", "waiting_human", "attention", "closed"):
                return {"ok": False, "error": f"非法状态 {new_state}"}
            st = Store(self.cfg.db_path)
            try:
                st.update_topic(topic_id, state=new_state, attention_at=None, attention_reason=None)
            finally:
                st.close()
            return {"ok": True, "action": action, "topic_id": topic_id, "state": new_state}

        if action == "resolve_escalation":
            msg_id = str(payload.get("message_id") or "").strip()
            why = str(payload.get("why") or "在管理后台人工确认结案").strip()
            if not msg_id:
                return {"ok": False, "error": "缺少 message_id"}
            st = Store(self.cfg.db_path)
            try:
                with st.tx() as c:
                    c.execute(
                        """UPDATE escalations
                           SET closed_at=COALESCE(closed_at, ?), close_reason='admin_web_resolved',
                               resolved_by='admin_web', resolve_note=?
                           WHERE message_id=?""",
                        (now_iso(), why, msg_id),
                    )
            finally:
                st.close()
            return {"ok": True, "action": action, "message_id": msg_id}

        if action == "complete_work_item":
            topic_id = str(payload.get("topic_id") or "").strip()
            key = str(payload.get("key") or "").strip()
            result = str(payload.get("result") or "管理后台人工标记完成").strip()
            if not topic_id or not key:
                return {"ok": False, "error": "缺少 topic_id 或 key"}
            st = Store(self.cfg.db_path)
            try:
                duty.done_work(st, topic_id, key, result)
            finally:
                st.close()
            return {"ok": True, "action": action, "topic_id": topic_id, "key": key}

        if action == "close_watch":
            watch_id = int(payload.get("id") or 0)
            if watch_id <= 0:
                return {"ok": False, "error": "缺少有效的 watch id"}
            st = Store(self.cfg.db_path)
            try:
                st.close_watch(watch_id, "admin_closed")
            finally:
                st.close()
            return {"ok": True, "action": action, "id": watch_id}

        return {"ok": False, "error": f"未知动作: {action}"}


def _load_ui_html() -> bytes:
    ui_path = Path(__file__).with_name("admin_ui.html")
    if ui_path.is_file():
        return ui_path.read_bytes()
    return b"<!doctype html><html><body><h1>Sheepdog Workbench UI missing</h1></body></html>"


def make_handler(service: WorkbenchService):
    class WorkbenchHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

            if path in ("/", "/index.html"):
                body = _load_ui_html()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                if path == "/api/overview":
                    mins = int((qs.get("minutes") or ["1440"])[0])
                    self._send_json(service.get_overview(minutes=mins))
                elif path == "/api/sessions":
                    self._send_json(service.get_sessions_tree())
                elif path == "/api/sessions/detail":
                    tid = (qs.get("topic_id") or ["tp_bus"])[0]
                    self._send_json(service.get_session_detail(tid))
                elif path == "/api/events/waterfall":
                    limit = int((qs.get("limit") or ["50"])[0])
                    offset = int((qs.get("offset") or ["0"])[0])
                    route = (qs.get("route") or [""])[0]
                    q = (qs.get("q") or [""])[0]
                    self._send_json(service.get_events_waterfall(limit=limit, offset=offset, route=route, q=q))
                elif path == "/api/events/trace":
                    mid = (qs.get("message_id") or [""])[0]
                    self._send_json(service.get_event_trace(mid))
                elif path == "/api/decisions":
                    self._send_json(service.get_decisions_analytics())
                elif path == "/api/admin/state":
                    self._send_json(service.get_admin_state())
                elif path == "/api/files":
                    self._send_json(service.get_workspace_files())
                elif path == "/api/file-content":
                    vpath = (qs.get("path") or ["config/config.toml"])[0]
                    self._send_json(service.read_workspace_file(vpath))
                else:
                    self._send_json({"error": f"Not found: {path}"}, status=HTTPStatus.NOT_FOUND)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/action":
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(min(length, 65536)).decode("utf-8", errors="replace") if length > 0 else "{}"
                payload = json.loads(raw) if raw.strip() else {}
                res = service.execute_action(payload if isinstance(payload, dict) else {})
                self._send_json(res, status=HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST)
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=HTTPStatus.BAD_REQUEST)

    return WorkbenchHandler


def create_server(host: str = "127.0.0.1", port: int = 8788, cfg: Config | None = None) -> ThreadingHTTPServer:
    svc = WorkbenchService(cfg=cfg)
    return ThreadingHTTPServer((host, port), make_handler(svc))


def run_web_server(host: str = "127.0.0.1", port: int = 8788, cfg: Config | None = None) -> int:
    srv = create_server(host=host, port=port, cfg=cfg)
    actual_host, actual_port = srv.server_address[:2]
    print(f"Sheepdog 管理与可观测工作台已启动: http://{actual_host}:{actual_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
