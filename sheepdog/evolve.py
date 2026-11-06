"""内置自进化管家循环（7.119：sheepdog-evolve）。

把『定时/事件驱动 6 维体检 -> 按 SKILL.md 诊断根因 -> 自动修复/规则收敛 -> 独立验证 -> 带回滚按钮热升级』
内置为 Sheepdog 守护进程（`sheepdog run`）与 CLI（`sheepdog evolve` / `sheepdog init`）的原生能力。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import observe as obs_mod
from . import session as sm
from .config import ANTIGRAVITY, Config
from .store import Store, now_iso

log = logging.getLogger("sheepdog")

EVOLVE_META_KEY = "self_evolve_state"

DEFAULT_EVOLVE_SKILL_MD = """\
---
name: sheepdog-evolve
description: "Sheepdog 内置自进化管家协议。周期性或事件驱动巡检守护进程健康度、路由准确性、拍板错题本、全链路耗时与跨会话反馈，自动收敛规则与修复代码缺陷。"
---

# Sheepdog 自进化管家（Self-Evolution Steward）

你是当前 Sheepdog 实例的内置自进化管家（绑定会话 `tp_sheepdog`）。
你的目标是让 Sheepdog 越用越懂主人、越跑越稳：**零漏单、零误报、零卡死、少打扰**。

## 1. 触发时机与标准输入

每次唤醒时，Sheepdog 守护进程已为你生成一份结构化的 6 维体检摘要（可通过 `sheepdog evolve --digest` 或 `sheepdog observe --evolve-digest` 查看）：
1. **D1 · 守护进程与长连接健康度**：常驻进程 PID、原生飞书 WebSocket 状态、`sheepdog observe` 告警与自愈记录；
2. **D2 · 路由与待办闭环**：近期入账消息路由分布、挂起批次、未结拍板项与总台无引用回复；
3. **D3 · 人类拍板错题本与自治演进**：主人点击「修改/驳回」或跟进批评的提问（`overridden`）、推翻原因分类（`context_incomplete` / `premature_ask` / `wrong_routing` / `over_cautious`）及高频可自治候选（`autonomy_candidates`）；
4. **D4 · T0 -> T5 全链路耗时与慢投递**：入账 (`T1-T0`)、点表情 (`T2-T1`)、派单 (`T3-T2`)、会话执行 (`T4-T3`)、卡片送达 (`T5-T4`) 的 P50/P95 分位与超阈值慢批次；
5. **D5 · 总线 (`tp_bus`) 越权与后台羔羊 (`lambs`) 状态**：`tp_bus` 是否违例跑重活、CLI/Subagent 羔羊是否卡死或输出脏日志摘要；
6. **D6 · 跨会话同行反馈收件箱 (`tp_sheepdog` / `tp_bus`)**：其他业务会话通过 `sheepdog mail` / `tell` 转交的真实体验反馈与防呆诉求。

## 2. 四步根因思考法（先治一类，再修一个）

遇到任何报错、推翻、慢批次或体验反馈时，必须按以下四步思考并行动：
1. **从这一个到这一类**：去掉具体的人名、单号、时间后依然成立的规律，才是真正要解决的「这一类问题」。
2. **找它从哪来**：追问系统结构为什么允许它发生（哪条状态机不变量缺失、哪层门禁没拦住），绝不能用「模型粗心」敷衍。
3. **让这一类不存在**：优先在确定性代码或前置守卫层让该类错误物理上不可能发生，其次一出现就当场拦截并给出明确指引，避免只叠脆弱的口头提醒。
4. **最后修眼前这一个**：顺手把当前卡住的批次、脏摘要或未结拍板项清理干净。

## 3. 六大自进化职责（D1 ~ D6 闭环动作）

- **D1 守护与长连接自愈**：发现僵尸容器、挂起后台进程或规则指纹漂移时，立即触发自愈重置并核验恢复。
- **D2 路由与拍板总台零漏单**：发现主人消息被误丢弃（`DROP`）或拍板总台无引用跟进未命中原话题时，修补路由/关联窗口并补投漏单。
- **D3 拍板错题本与少打扰门禁**：每当出现新的 `overridden` 错题，剖析为什么不该问或没问清楚；若属常见无效打扰（如无需人类代跑命令、已有负责人、测试环境常规操作、同钥匙刚批过暂缓），在 `outbound.py` 或 `playbook/` 中固化防呆拦截。
- **D4 耗时盯盘**：定位 `T0 -> T5` 各阶段异常毛刺，区分会话内推理耗时与 IM 发送耗时，消除轮询阻塞与无效重试。
- **D5 总线减负与羔羊摘要净化**：发现 `tp_bus` 越权亲自跑业务脚本或 CLI 羔羊尾部摘要被进度行/JSON 碎片污染时，更新越权守卫与 `_summarize_cli_lamb_log` 提取器。
- **D6 卡片体验与交付直出**：收到用户对回复卡片的体验反馈（如账号、密码、密钥、Token、登录/订阅链接被藏进 `more` 折叠区，或首屏与 `more` 重复假折叠）时，优化 `card.py` 首屏直出与自动提权规则。

## 4. 验证与可回滚热更新闸门

任何代码或规则修改在上线前必须通过以下三道闸门：
1. **可证伪回归测试**：在 `tests/` 新增覆盖该类问题的单测，并在 `tests/REGRESSIONS.md` 登记；先验证去掉修复会变红，恢复修复后全量单测 `100% OK`。
2. **隐私与安全检查**：若仓库关联公开远端，运行 `python3 scripts/check_private_data.py` 确保零私有凭据或内部机密泄露。
3. **可回滚热重启**：递增 `sheepdog/__init__.py` 版本号，执行 `sheepdog upgrade --no-pull` 原地热升级常驻进程；升级汇报卡片必须写明「📋 更新了什么 / 🎯 解决了什么问题」并保留「⏪ 回滚至上一版」按钮。

## 5. 用户自定义配置（User Customization Profile）

> 用户可在下方自由修改自己的自治等级、安全红线、团队分工与卡片偏好；自进化管家每轮巡检都会读取并严格遵守。

```yaml
# 1) 自治与热更新策略
autonomy_level: "auto_fix_and_hot_upgrade_with_rollback"
patrol_interval_minutes: 60
trigger_immediately_on_override: true

# 2) 安全红线（绝不自动放宽）
safety_redlines:
  - "生产环境高危写操作、强推与删除资源必须由主人明确点名授权"
  - "禁止把动手敲命令的活倒推给主人"
  - "禁止向公开代码仓库提交任何凭据、内部群号或个人私密配置"

# 3) 团队职责与专席委派映射（示例，按需修改）
team_delegation_map:
  database_readonly_or_internal_user: "先对接 DBA / 数据库负责人，不打扰主人"
  staging_or_test_env_ops: "测试环境常规授权与排障直接自治闭环，事后用 report 报备"

# 4) 卡片与沟通偏好
ux_and_card_preferences:
  language: "zh-CN（白话、短句、结论先行）"
  reply_first_screen: "日常对话 ≤3行/≤100字；交付账号/密码/Token/密钥/链接时首屏直出（≤12行/≤520字），严禁把核心交付物藏进 more 折叠区"
  anti_fake_more: "严禁 summary 与 more 正文重复的假折叠"
```
"""


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ensure_evolve_skill_file(
    cfg: Config,
    *,
    custom_template: str | None = None,
    overwrite: bool = False,
) -> Path:
    """确保 `skills/sheepdog-evolve/SKILL.md` 存在；缺失或显式 overwrite 时写入模板。"""
    p = cfg.evolve_skill_file
    if p.exists() and not overwrite:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    content = (custom_template if custom_template is not None else DEFAULT_EVOLVE_SKILL_MD).rstrip() + "\n"
    p.write_text(content, encoding="utf-8")
    return p


def read_evolve_state(store: Store) -> dict:
    raw = store.get_meta(EVOLVE_META_KEY) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_evolve_state(store: Store, state: dict) -> None:
    store.set_meta(EVOLVE_META_KEY, json.dumps(state, ensure_ascii=False))


def build_evolve_digest(
    cfg: Config,
    store: Store,
    *,
    since_minutes: float = 65.0,
    now_dt: datetime | None = None,
) -> dict:
    """7.119：构建 6 维自进化巡检体检摘要（D1~D6）。"""
    now = now_dt or datetime.now(timezone.utc).astimezone()
    window_start = now - timedelta(minutes=max(5.0, float(since_minutes)))
    window_iso = window_start.isoformat(timespec="seconds")

    conn = store.conn
    prev_evolve_st = read_evolve_state(store)
    settled_ovr_ids: set[str] = set(str(x) for x in (prev_evolve_st.get("seen_override_ids") or []) if x)
    try:
        r_healed_ovr = conn.execute("SELECT value FROM meta WHERE key='healed_override_escalation_ids'").fetchone()
        if r_healed_ovr and r_healed_ovr["value"]:
            settled_ovr_ids.update(str(x) for x in json.loads(str(r_healed_ovr["value"])) if x)
    except Exception:
        pass

    # D1: 守护进程与 observe 告警
    obs_res = obs_mod.run_observe(
        cfg,
        db_path=cfg.db_path,
        since_minutes=float(since_minutes),
        self_heal=True,
        now_dt=now,
        check_processes=(cfg.sink != "dryrun"),
        check_log=(cfg.sink != "dryrun"),
    )
    warns = list(obs_res.get("warns") or [])
    self_healed = list(obs_res.get("self_healed") or [])

    # D2: 路由统计、挂起批次与未结拍板项
    route_rows = conn.execute(
        "SELECT route, reason, COUNT(*) AS cnt FROM messages WHERE first_seen >= ? GROUP BY route, reason ORDER BY cnt DESC",
        (window_iso,),
    ).fetchall()
    routes_in_window = [
        {"route": r["route"], "reason": r["reason"], "count": int(r["cnt"])}
        for r in route_rows
    ]
    open_escs = [
        {
            "message_id": r["message_id"],
            "topic_id": r["topic_id"],
            "asked_at": r["asked_at"],
            "excerpt": (r["text"] or "").splitlines()[0][:100] if r["text"] else "",
        }
        for r in conn.execute(
            "SELECT message_id, topic_id, asked_at, text FROM escalations WHERE closed_at IS NULL ORDER BY asked_at DESC LIMIT 20"
        ).fetchall()
    ]

    # D3 & D4: 人类拍板错题本、自治候选与 T0->T5 阶段耗时
    from .admin_web import WorkbenchService
    wb_cfg = cfg
    if not cfg.db_path.is_file() and getattr(store, "path", None) and Path(store.path).is_file():
        import dataclasses
        wb_cfg = dataclasses.replace(cfg, state_dir=Path(store.path).parent)
    wb = WorkbenchService(wb_cfg)
    overview = wb.get_overview(minutes=1440)
    dec_analytics = wb.get_decisions_analytics()
    dec_summary = dec_analytics.get("summary") or {}
    recent_decisions = dec_analytics.get("ledger") or []
    overridden_in_window = []
    latest_override_answer_id = ""
    for d in recent_decisions:
        if d.get("alignment") == "overridden":
            msg_id_str = str(d.get("message_id") or "")
            ans_id = str(d.get("answer_message_id") or msg_id_str or "")
            if msg_id_str in settled_ovr_ids or ans_id in settled_ovr_ids:
                continue
            if ans_id and not latest_override_answer_id:
                latest_override_answer_id = ans_id
            ans_at = _parse_iso(str(d.get("answered_at") or d.get("asked_at") or ""))
            if ans_at is None or ans_at >= window_start:
                overridden_in_window.append(
                    {
                        "message_id": d.get("message_id"),
                        "topic_id": d.get("topic_id"),
                        "title": d.get("title"),
                        "answered_at": d.get("answered_at"),
                        "answer_id": ans_id,
                        "answer_text": d.get("answer_text"),
                        "override_reason": d.get("override_code") or d.get("override_reason"),
                        "diagnosis": d.get("override_reason") or d.get("alignment_label"),
                    }
                )
    autonomy_candidates = list(dec_analytics.get("autonomy_candidates") or [])
    latencies = overview.get("latencies") or {}
    stage_summary = {
        "t1_ingest": latencies.get("t0_t1_ingest") or {},
        "t2_ack": latencies.get("t1_t2_ack") or {},
        "t3_dispatch": latencies.get("t1_t3_dispatch") or {},
        "t4_session": latencies.get("t3_t4_session") or {},
        "t5_deliver": latencies.get("t0_t5_e2e_reply") or {},
    }

    slow_rows = conn.execute(
        """
        SELECT batch_id, topic_id, state, sent_at, acked_at,
               ROUND((julianday(CASE
                   WHEN acked_at IS NOT NULL AND acked_at != '' THEN acked_at
                   WHEN state IN ('sent', 'sending', 'uncertain') THEN ?
                   ELSE sent_at
               END) - julianday(sent_at)) * 86400.0, 1) AS dur_s
        FROM dispatches
        WHERE sent_at >= ?
          AND state NOT IN ('context', 'priority', 'redelivered')
        ORDER BY dur_s DESC
        LIMIT 10
        """,
        (now.isoformat(timespec="seconds"), window_iso),
    ).fetchall()
    slow_dispatches = [
        {
            "batch_id": r["batch_id"],
            "topic_id": r["topic_id"],
            "state": r["state"],
            "sent_at": r["sent_at"],
            "acked_at": r["acked_at"],
            "duration_seconds": float(r["dur_s"] or 0.0),
        }
        for r in slow_rows
        if float(r["dur_s"] or 0.0) >= 120.0
    ]

    # D5: tp_bus 批次与羔羊状态
    lamb_rows = conn.execute(
        """
        SELECT lamb_id, parent_topic_id, kind, task_title, state, started_at, finished_at, result_summary
        FROM lambs
        WHERE started_at >= ? OR state = 'running'
        ORDER BY started_at DESC
        LIMIT 15
        """,
        (window_iso,),
    ).fetchall()
    recent_lambs = []
    for r in lamb_rows:
        st_val = str(r["state"] or "")
        p_tid = str(r["parent_topic_id"] or "")
        fin_at = str(r["finished_at"] or "")
        recovered_by_parent = False
        if st_val in ("failed", "timeout") and p_tid and fin_at:
            has_newer_done_lamb = conn.execute(
                "SELECT 1 FROM lambs WHERE parent_topic_id = ? AND state IN ('done', 'running') AND started_at >= ? LIMIT 1",
                (p_tid, fin_at),
            ).fetchone() is not None
            has_post_ack = conn.execute(
                "SELECT 1 FROM dispatches WHERE topic_id = ? AND state IN ('acked', 'acked_late') AND acked_at IS NOT NULL AND acked_at >= ? LIMIT 1",
                (p_tid, fin_at),
            ).fetchone() is not None
            has_open_fail = conn.execute(
                "SELECT 1 FROM dispatches WHERE topic_id = ? AND state IN ('failed', 'missed') AND sent_at >= ? LIMIT 1",
                (p_tid, fin_at),
            ).fetchone() is not None
            recovered_by_parent = bool(has_newer_done_lamb or (has_post_ack and not has_open_fail))
        recent_lambs.append(
            {
                "lamb_id": r["lamb_id"],
                "parent_topic_id": r["parent_topic_id"],
                "kind": r["kind"],
                "task_title": r["task_title"],
                "state": r["state"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "result_summary": (r["result_summary"] or "")[:160],
                "recovered_by_parent": recovered_by_parent,
            }
        )

    # D6: 跨会话同行反馈收件箱（发往 tp_sheepdog 的留言或待处理系统消息）
    evolve_tid = cfg.self_evolve.topic_id
    peer_mails = []
    latest_mail_id = 0
    last_seen_mail_id = int(prev_evolve_st.get("last_mail_id") or 0)
    if store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mailbox'").fetchone():
        for mr in conn.execute(
            """
            SELECT b.id, b.from_topic, b.to_topic, b.body, b.created_at, m.dispatch_state
            FROM mailbox b
            LEFT JOIN messages m ON m.message_id = b.message_id
            WHERE b.to_topic = ? AND (m.dispatch_state IS NULL OR m.dispatch_state != 'acked' OR b.created_at >= ?)
            ORDER BY b.id DESC
            LIMIT 10
            """,
            (evolve_tid, window_iso),
        ).fetchall():
            mid_int = int(mr["id"])
            if mid_int > latest_mail_id:
                latest_mail_id = mid_int
            is_unread = (mr["dispatch_state"] != "acked")
            if not is_unread and mid_int <= last_seen_mail_id:
                continue
            peer_mails.append(
                {
                    "id": mid_int,
                    "from_topic": mr["from_topic"],
                    "to_topic": mr["to_topic"],
                    "body": (mr["body"] or "")[:300],
                    "created_at": mr["created_at"],
                    "unread": is_unread,
                }
            )

    action_reasons: list[str] = []
    if warns:
        action_reasons.append(f"observe_warns({len(warns)})")
    if overridden_in_window:
        action_reasons.append(f"new_overrides({len(overridden_in_window)})")
    if any(m.get("unread") for m in peer_mails):
        action_reasons.append(f"unread_peer_mail({sum(1 for m in peer_mails if m.get('unread'))})")
    unpromoted = [c for c in autonomy_candidates if not c.get("promoted")]
    if unpromoted:
        action_reasons.append(f"autonomy_candidates({len(unpromoted)})")
    seen_failed_lamb_ids: set[str] = set(str(x) for x in (prev_evolve_st.get("seen_failed_lamb_ids") or []) if x)
    failed_lambs = [
        lb for lb in recent_lambs
        if lb.get("state") in ("failed", "timeout")
        and not lb.get("recovered_by_parent")
        and str(lb.get("lamb_id") or "") not in seen_failed_lamb_ids
    ]
    if failed_lambs:
        action_reasons.append(f"failed_lambs({len(failed_lambs)})")

    # D7: 声明式业务循环 (Business Loops & Loop-Improver)
    d7_loops: dict = {"total_loops": 0, "paused_loops": [], "feedback_runs": []}
    try:
        from . import loops as loops_mod
        ov_loops = loops_mod.build_loops_overview(cfg, store)
        paused_list = [
            {"id": lp["id"], "title": lp["title"], "reason": (lp.get("state") or {}).get("paused_reason", "")}
            for lp in (ov_loops.get("loops") or [])
            if (lp.get("state") or {}).get("paused")
        ]
        seen_fb_run_ids: set[str] = set(str(x) for x in (prev_evolve_st.get("seen_feedback_run_ids") or []) if x)
        fb_rows = store.conn.execute(
            """SELECT run_id, loop_id, human_feedback, started_at
               FROM loop_runs
               WHERE human_feedback != '' AND started_at >= ?
               ORDER BY started_at DESC LIMIT 10""",
            (window_iso,),
        ).fetchall()
        fb_list = [dict(r) for r in fb_rows if str(r["run_id"] or "") not in seen_fb_run_ids]
        if paused_list:
            action_reasons.append(f"paused_loops({len(paused_list)})")
        if fb_list:
            action_reasons.append(f"loop_feedback({len(fb_list)})")
        d7_loops = {
            "total_loops": int(ov_loops.get("total_loops") or 0),
            "empty_runs": int(ov_loops.get("total_empty_runs") or 0),
            "action_runs": int(ov_loops.get("total_action_runs") or 0),
            "paused_loops": paused_list,
            "feedback_runs": fb_list,
        }
    except Exception:
        pass

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_minutes": float(since_minutes),
        "ok": len(warns) == 0,
        "needs_action": bool(action_reasons),
        "action_reasons": action_reasons,
        "d1_daemon_and_observe": {
            "ok": bool(obs_res.get("ok", True)),
            "warns": warns,
            "self_healed": self_healed,
            "daemon": obs_res.get("daemon") or {},
        },
        "d2_routing_and_pending": {
            "routes_in_window": routes_in_window,
            "open_escalations_count": len(open_escs),
            "open_escalations": open_escs[:8],
        },
        "d3_decision_ledger": {
            "total_evaluated": dec_summary.get("total", 0),
            "approval_rate": round(
                float(dec_summary.get("acceptance_rate_pct", dec_summary.get("acceptance_rate", 100.0))) / 100.0,
                4,
            ),
            "overridden_count_total": dec_summary.get("overridden", 0),
            "overridden_in_window": overridden_in_window,
            "latest_override_answer_id": latest_override_answer_id,
            "autonomy_candidates": autonomy_candidates[:5],
        },
        "d4_latency_and_slow_dispatches": {
            "stage_summary": stage_summary,
            "slow_dispatches": slow_dispatches,
        },
        "d5_bus_and_lambs": {
            "recent_lambs": recent_lambs,
        },
        "d6_peer_feedback_inbox": {
            "topic_id": evolve_tid,
            "latest_mail_id": latest_mail_id,
            "peer_mails": peer_mails,
        },
        "d7_business_loops": d7_loops,
    }


def format_evolve_prompt(
    cfg: Config,
    digest: dict,
    *,
    include_skill: bool = False,
    skill_text: str = "",
    trigger_reason: str = "scheduled",
) -> str:
    """把 6 维体检摘要与（按需附带的）SKILL.md 组装成投递给自进化管家会话的 Markdown 提示词。"""
    d1 = digest.get("d1_daemon_and_observe") or {}
    d2 = digest.get("d2_routing_and_pending") or {}
    d3 = digest.get("d3_decision_ledger") or {}
    d4 = digest.get("d4_latency_and_slow_dispatches") or {}
    d5 = digest.get("d5_bus_and_lambs") or {}
    d6 = digest.get("d6_peer_feedback_inbox") or {}

    lines = [
        f"【🐕 Sheepdog 内置自进化巡检 · 触发原因：`{trigger_reason}` · 时间：`{digest.get('generated_at', '')}`】",
        f"- 巡检窗口：过去 `{int(digest.get('window_minutes', 65))}` 分钟 | 需处置信号：`{', '.join(digest.get('action_reasons') or ['无（例行巡检）'])}`",
        f"- D1 守护进程与告警：`ok={d1.get('ok')}` | `warns={len(d1.get('warns') or [])}` | `self_healed={len(d1.get('self_healed') or [])}`",
    ]
    for w in d1.get("warns") or []:
        lines.append(f"  - ⚠️ 告警：{w}")
    lines.append(
        f"- D2 路由与未结拍板：窗口内路由 `{len(d2.get('routes_in_window') or [])}` 类 | 当前未结「需要你定」`{d2.get('open_escalations_count', 0)}` 条"
    )
    lines.append(
        f"- D3 拍板错题本与自治演进：累计采纳率 `{round(float(d3.get('approval_rate', 1.0)) * 100, 1)}%` | 窗口内新增推翻/批评 `{len(d3.get('overridden_in_window') or [])}` 条 | 自治候选 `{len(d3.get('autonomy_candidates') or [])}` 类"
    )
    for ov in d3.get("overridden_in_window") or []:
        lines.append(
            f"  - ❌ 推翻案例 `{ov.get('message_id')}` [{ov.get('topic_id')}] ({ov.get('override_reason')}): 主人回复 `{ov.get('answer_text')}` | 诊断：{ov.get('diagnosis')}"
        )
    st = d4.get("stage_summary") or {}
    t1_p50 = ((st.get("t1_ingest") or {}).get("p50_ms"))
    t2_p50 = ((st.get("t2_ack") or {}).get("p50_ms"))
    t4_p50 = ((st.get("t4_session") or {}).get("p50_ms"))
    lines.append(
        f"- D4 阶段耗时与慢批次：T1 入账 P50=`{t1_p50}ms` | T2 点表情 P50=`{t2_p50}ms` | T4 会话执行 P50=`{t4_p50}ms` | 慢批次(>=120s)=`{len(d4.get('slow_dispatches') or [])}`"
    )
    lines.append(f"- D5 后台羔羊：近期/在途 `{len(d5.get('recent_lambs') or [])}` 只")
    lines.append(f"- D6 同行反馈收件箱（`{d6.get('topic_id')}`）：`{len(d6.get('peer_mails') or [])}` 条留言")
    for pm in d6.get("peer_mails") or []:
        lines.append(f"  - 📩 来自 `{pm.get('from_topic')}` (#{pm.get('id')}): {pm.get('body')}")
    d7 = digest.get("d7_business_loops") or {}
    lines.append(
        f"- D7 业务循环（Loop-Improver）：共 `{d7.get('total_loops', 0)}` 条 | 零Token空跑 `{d7.get('empty_runs', 0)}` 次 | 触发执行 `{d7.get('action_runs', 0)}` 次 | 熔断暂停 `{len(d7.get('paused_loops') or [])}` 条"
    )
    for pl in d7.get("paused_loops") or []:
        lines.append(f"  - ⏸ 熔断循环 `{pl.get('id')}` ({pl.get('title')}): {pl.get('reason')}")
    for fb in d7.get("feedback_runs") or []:
        lines.append(f"  - 🧠 循环人工反馈 `{fb.get('loop_id')}` ({fb.get('run_id')}): {fb.get('human_feedback')}")

    lines.append(
        "\n请按 `skills/sheepdog-evolve/SKILL.md` 的四步根因思考法与六大自进化职责处置上述异常；若修复了代码或规则，完成可证伪测试后执行 `sheepdog upgrade --no-pull` 原地热升级（带回滚按钮）。"
    )
    if include_skill and skill_text.strip():
        lines.append("\n---\n### 当前生效的 `SKILL.md` 配置\n" + skill_text.strip())
    return "\n".join(lines)


def ensure_evolve_topic(cfg: Config, store: Store) -> dict | None:
    """确保自进化管家会话（默认 `tp_sheepdog`）已在 `topics` 表登记且标记为常设（standing）。"""
    tid = cfg.self_evolve.topic_id
    t = store.get_topic(tid)
    host = cfg.self_evolve.host or ANTIGRAVITY
    duty_text = f"Sheepdog 内置自进化管家（配置文件：{cfg.evolve_skill_file}）"
    if t is None:
        store.create_topic(
            tid,
            cfg.self_evolve.title,
            "adopted",
            duty_text,
            sm.ACTIVE,
            host=host,
            lifecycle="standing",
        )
        t = store.get_topic(tid)
    else:
        upd_fields: dict = {}
        if t["state"] == sm.CLOSED:
            upd_fields["state"] = sm.ACTIVE
        if not t["duty"]:
            upd_fields["duty"] = duty_text
        if (t["lifecycle"] or "") != "standing":
            upd_fields["lifecycle"] = "standing"
        if upd_fields:
            store.update_topic(tid, **upd_fields)
            t = store.get_topic(tid)
    return dict(t) if t is not None else None


def run_evolve_tick(
    cfg: Config,
    store: Store,
    dispatcher=None,
    *,
    force: bool = False,
    now_dt: datetime | None = None,
) -> dict:
    """守护进程主循环每轮调用的自进化巡检勾子（7.119）。

    - 自动确保 `skills/sheepdog-evolve/SKILL.md` 存在；
    - 到达 `interval_minutes` 周期，或检测到新的拍板推翻/同行留言（且已过 `cooldown_minutes`）时自动执行 6 维体检；
    - 将最新体检状态写入 `meta['self_evolve_state']`（供 `sheepdog observe`、Web 工作台 `:8788` 与 `sheepdog evolve --status` 实时读取）；
    - 若发现需处置异常（`needs_action=True`）或 `force=True`，向 `tp_sheepdog` 写入 `self_evolve_digest` 系统消息并在会话空闲时立即投递唤醒。
    """
    if not cfg.self_evolve.enabled and not force:
        return {"triggered": False, "reason": "disabled"}
    if cfg.sink == "dryrun" and not force:
        return {"triggered": False, "reason": "dryrun_skip"}

    now = now_dt or datetime.now(timezone.utc).astimezone()
    skill_file = ensure_evolve_skill_file(cfg)
    try:
        skill_text = skill_file.read_text(encoding="utf-8")
    except OSError:
        skill_text = DEFAULT_EVOLVE_SKILL_MD
    skill_hash = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()[:16]

    prev = read_evolve_state(store)
    last_run_at = _parse_iso(prev.get("last_run_at"))
    interval_s = max(60.0, float(cfg.self_evolve.interval_minutes) * 60.0)
    cooldown_s = max(60.0, float(cfg.self_evolve.cooldown_minutes) * 60.0)
    elapsed_s = (now - last_run_at).total_seconds() if last_run_at is not None else float("inf")

    trigger_reason = ""
    cand_digest: dict | None = None
    if force:
        trigger_reason = "manual_force"
    elif last_run_at is None or elapsed_s >= interval_s:
        trigger_reason = "scheduled_interval"
    elif cfg.self_evolve.trigger_on_override and elapsed_s >= cooldown_s:
        # 轻量探测是否有未读同行留言或近期新答复的拍板项
        evolve_tid = cfg.self_evolve.topic_id
        last_mail_id = int(prev.get("last_mail_id") or 0)
        if store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mailbox'").fetchone():
            unreads = store.unread_mail(evolve_tid)
            max_unread_id = max((int(r["id"]) for r in unreads), default=0)
            if max_unread_id > last_mail_id:
                trigger_reason = "event_peer_mail"
        if not trigger_reason:
            last_esc_ts = str(prev.get("last_checked_escalation_at") or "")
            row_e = store.conn.execute(
                "SELECT MAX(answered_at) AS ats FROM escalations WHERE answered_at IS NOT NULL AND answered_at > ?",
                (last_esc_ts or (now - timedelta(minutes=60)).isoformat(timespec="seconds"),),
            ).fetchone()
            if row_e and row_e["ats"] and str(row_e["ats"]) > last_esc_ts:
                # 进一步跑一次 digest 看是否真属于 overridden
                cand_digest = build_evolve_digest(
                    cfg,
                    store,
                    since_minutes=max(65.0, float(cfg.self_evolve.interval_minutes) + 5.0),
                    now_dt=now,
                )
                prev["last_checked_escalation_at"] = str(row_e["ats"])
                new_ov_id = str((cand_digest.get("d3_decision_ledger") or {}).get("latest_override_answer_id") or "")
                seen_ovr_set = set(str(x) for x in (prev.get("seen_override_ids") or []) if x)
                if new_ov_id and new_ov_id != str(prev.get("last_override_answer_id") or "") and new_ov_id not in seen_ovr_set:
                    trigger_reason = "event_override"
                else:
                    write_evolve_state(store, prev)
                    return {"triggered": False, "reason": "within_interval_no_override"}

    if not trigger_reason:
        return {"triggered": False, "reason": "within_interval", "elapsed_seconds": round(elapsed_s, 1)}

    ensure_evolve_topic(cfg, store)
    digest = cand_digest if cand_digest is not None else build_evolve_digest(
        cfg,
        store,
        since_minutes=max(65.0, float(cfg.self_evolve.interval_minutes) + 5.0),
        now_dt=now,
    )
    iteration = int(prev.get("iteration") or 0) + 1
    include_skill = (prev.get("skill_hash") != skill_hash) or force
    dispatched_msg_id = ""
    evolve_tid = cfg.self_evolve.topic_id

    if digest.get("needs_action") or force:
        prompt_text = format_evolve_prompt(
            cfg,
            digest,
            include_skill=include_skill,
            skill_text=skill_text,
            trigger_reason=trigger_reason,
        )
        dispatched_msg_id = store.add_system_message(
            evolve_tid,
            "self_evolve_digest",
            prompt_text,
            tags=[f"evolve_iter:{iteration}", f"trigger:{trigger_reason}"],
        )
        now_str = now.isoformat(timespec="seconds")
        store.update_topic(
            evolve_tid,
            last_active=now_str,
            dispatched_at=now_str,
            summary=f"自进化巡检 #{iteration} ({trigger_reason})",
        )
        if dispatcher is not None:
            t_row = store.get_topic(evolve_tid)
            if t_row is not None and not t_row["conversation_id"] and dispatcher.topic_host(t_row) == ANTIGRAVITY:
                try:
                    cid = dispatcher._new_conversation(
                        ANTIGRAVITY,
                        evolve_tid,
                        t_row["title"] or cfg.self_evolve.title,
                        prompt_text,
                        t_row["model"] or "",
                        t_row["effort"] or "",
                    )
                    store.update_topic(evolve_tid, conversation_id=cid, spawned_at=now.isoformat(timespec="seconds"))
                    t_row = store.get_topic(evolve_tid)
                except Exception as e:
                    log.warning("自动拉起自进化管家会话 %s 失败: %s", evolve_tid, e)
            if t_row is not None and t_row["conversation_id"] and t_row["kind"] == "known":
                try:
                    bid = f"evolve-{now.strftime('%Y%m%d%H%M%S')}"
                    dispatcher._send(t_row, prompt_text, bid)
                    store.conn.execute(
                        "UPDATE messages SET dispatch_state='acked', batch_id=? WHERE message_id=?",
                        (bid, dispatched_msg_id),
                    )
                    store.conn.commit()
                except Exception as e:
                    log.warning("向 known 自进化管家会话 %s 发送巡检摘要失败: %s", evolve_tid, e)

    d3 = digest.get("d3_decision_ledger") or {}
    d5_lambs = (digest.get("d5_bus_and_lambs") or {}).get("recent_lambs") or []
    d6 = digest.get("d6_peer_feedback_inbox") or {}
    seen_ovr_list = list(dict.fromkeys([
        *(str(x) for x in (prev.get("seen_override_ids") or []) if x),
        *(str(ov.get("message_id") or "") for ov in (d3.get("overridden_in_window") or []) if ov.get("message_id")),
        *(str(ov.get("answer_id") or "") for ov in (d3.get("overridden_in_window") or []) if ov.get("answer_id")),
        *([str(d3.get("latest_override_answer_id"))] if d3.get("latest_override_answer_id") else []),
        *([str(prev.get("last_override_answer_id"))] if prev.get("last_override_answer_id") else []),
    ]))[-100:]
    d7_fbs = (digest.get("d7_business_loops") or {}).get("feedback_runs") or []
    seen_lamb_list = list(dict.fromkeys([
        *(str(x) for x in (prev.get("seen_failed_lamb_ids") or []) if x),
        *(str(lb.get("lamb_id") or "") for lb in d5_lambs if lb.get("state") in ("failed", "timeout") and lb.get("lamb_id")),
    ]))[-100:]
    seen_fb_list = list(dict.fromkeys([
        *(str(x) for x in (prev.get("seen_feedback_run_ids") or []) if x),
        *(str(fb.get("run_id") or "") for fb in d7_fbs if fb.get("run_id")),
    ]))[-100:]
    new_state = {
        "enabled": bool(cfg.self_evolve.enabled),
        "topic_id": evolve_tid,
        "skill_path": str(skill_file),
        "skill_hash": skill_hash,
        "interval_minutes": float(cfg.self_evolve.interval_minutes),
        "iteration": iteration,
        "last_run_at": now.isoformat(timespec="seconds"),
        "last_trigger_reason": trigger_reason,
        "last_status": "dispatched" if dispatched_msg_id else "all_green",
        "last_message_id": dispatched_msg_id,
        "needs_action": bool(digest.get("needs_action")),
        "action_reasons": list(digest.get("action_reasons") or []),
        "last_override_answer_id": str(d3.get("latest_override_answer_id") or prev.get("last_override_answer_id") or ""),
        "seen_override_ids": seen_ovr_list,
        "seen_failed_lamb_ids": seen_lamb_list,
        "seen_feedback_run_ids": seen_fb_list,
        "last_checked_escalation_at": now.isoformat(timespec="seconds"),
        "last_mail_id": max(int(d6.get("latest_mail_id") or 0), int(prev.get("last_mail_id") or 0)),
        "summary": {
            "ok": bool(digest.get("ok", True)),
            "warns_count": len((digest.get("d1_daemon_and_observe") or {}).get("warns") or []),
            "overridden_in_window": len(d3.get("overridden_in_window") or []),
            "open_escalations": int((digest.get("d2_routing_and_pending") or {}).get("open_escalations_count") or 0),
            "slow_dispatches": len((digest.get("d4_latency_and_slow_dispatches") or {}).get("slow_dispatches") or []),
            "peer_mails": len(d6.get("peer_mails") or []),
        },
    }
    write_evolve_state(store, new_state)
    store.add_history(
        evolve_tid,
        "self_evolve",
        f"iter #{iteration} ({trigger_reason}): {new_state['last_status']} | reasons={','.join(new_state['action_reasons']) or 'none'}",
    )
    return {
        "triggered": True,
        "iteration": iteration,
        "trigger_reason": trigger_reason,
        "status": new_state["last_status"],
        "message_id": dispatched_msg_id,
        "state": new_state,
        "digest": digest,
    }
