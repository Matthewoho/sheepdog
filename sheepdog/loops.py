"""声明式业务循环引擎（7.120：sheepdog loop）。

将 AcmecoAI/loops 的核心工程精髓（5 格子业务契约、零 Token 前置 load 取料与幂等去重、
shadow -> confirm -> auto 三级自治水位晋级、连续失败 autopause 自动熔断、对话式 loop-intake）
与 Sheepdog 本机持久会话 / Lamb 执行器、跨周期多阶段状态机（stage）、闲忙动态变频
及 sheepdog-evolve 单循环错题本自进化（loop-improver）同构合一。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import session as sm
from .card import build_owner_card, card_fits
from .config import ANTIGRAVITY, Config
from .roster import load_roster, parse_patrol_hours
from .store import Store, now_iso

log = logging.getLogger("sheepdog")

AUTONOMY_SHADOW = "shadow"
AUTONOMY_CONFIRM = "confirm"
AUTONOMY_AUTO = "auto"
VALID_AUTONOMIES = (AUTONOMY_SHADOW, AUTONOMY_CONFIRM, AUTONOMY_AUTO)

AUTONOMY_LABEL = {
    AUTONOMY_SHADOW: "影子试跑 (shadow)",
    AUTONOMY_CONFIRM: "拍板执行 (confirm)",
    AUTONOMY_AUTO: "全自动闭环 (auto)",
}

ITEM_ACTIVE = "active"
ITEM_CLOSED = "closed"
ITEM_EXEMPTED = "exempted"
VALID_ITEM_STATUSES = (ITEM_ACTIVE, ITEM_CLOSED, ITEM_EXEMPTED)

LOOP_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS loop_state (
    loop_id              TEXT PRIMARY KEY,
    enabled              INTEGER NOT NULL DEFAULT 1,
    autonomy             TEXT NOT NULL DEFAULT 'shadow',
    paused               INTEGER NOT NULL DEFAULT 0,
    paused_reason        TEXT DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_at          TEXT DEFAULT '',
    last_status          TEXT DEFAULT '',
    last_run_id          TEXT DEFAULT '',
    total_runs           INTEGER NOT NULL DEFAULT 0,
    empty_runs           INTEGER NOT NULL DEFAULT 0,
    action_runs          INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_items (
    loop_id        TEXT NOT NULL,
    item_key       TEXT NOT NULL,
    title          TEXT DEFAULT '',
    stage          TEXT NOT NULL DEFAULT 'detected',
    status         TEXT NOT NULL DEFAULT 'active',
    payload_json   TEXT DEFAULT '{}',
    context_json   TEXT DEFAULT '{}',
    escalation_key TEXT DEFAULT '',
    last_run_id    TEXT DEFAULT '',
    next_check_at  TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (loop_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_loop_items_status ON loop_items(loop_id, status, next_check_at);

CREATE TABLE IF NOT EXISTS loop_runs (
    run_id             TEXT PRIMARY KEY,
    loop_id            TEXT NOT NULL,
    trigger_reason     TEXT NOT NULL,
    autonomy           TEXT NOT NULL,
    status             TEXT NOT NULL,
    items_count        INTEGER NOT NULL DEFAULT 0,
    active_items_count INTEGER NOT NULL DEFAULT 0,
    summary            TEXT DEFAULT '',
    evidence_json      TEXT DEFAULT '{}',
    message_id         TEXT DEFAULT '',
    human_feedback     TEXT DEFAULT '',
    error              TEXT DEFAULT '',
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL,
    finished_at        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_loop_runs_loop ON loop_runs(loop_id, started_at DESC);
"""


@dataclass
class LoopPlanStep:
    step: str
    does: str
    ask: bool = False


@dataclass
class LoopSpec:
    id: str
    title: str
    description: str = ""
    topic_id: str = "tp_bus"
    executor: str = "session"  # session | lamb:cli | lamb:subagent | acmeco-loops
    autonomy: str = AUTONOMY_SHADOW
    enabled: bool = True
    interval_minutes: float = 30.0
    active_interval_minutes: float = 5.0
    active_hours: str = ""
    load_cmd: str = ""
    load_timeout_seconds: float = 15.0
    dedup_key: str = "key"
    autopause_after_failures: int = 3
    stages: list[str] = field(default_factory=lambda: ["detected", "confirming", "verifying", "closed"])
    plan: list[LoopPlanStep] = field(default_factory=list)
    body: str = ""
    path: Path | None = None
    memory_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path) if self.path else ""
        d["memory_path"] = str(self.memory_path) if self.memory_path else ""
        return d


def ensure_loop_tables(store: Store) -> None:
    store.conn.executescript(LOOP_TABLES_SQL)
    store.conn.commit()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_scalar(val: str) -> Any:
    s = val.strip()
    if not s:
        return ""
    if s.startswith('"') and s.endswith('"'):
        try:
            return json.loads(s)
        except Exception:
            return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if s.startswith("[") and s.endswith("]"):
        try:
            return json.loads(s)
        except Exception:
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [_parse_scalar(p) for p in inner.split(",") if p.strip()]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 LOOP.md 的 YAML Frontmatter 与 Markdown 正文（零第三方依赖）。"""
    raw = text or ""
    if not raw.startswith("---\n"):
        return {}, raw
    end_idx = raw.find("\n---\n", 4)
    if end_idx < 0:
        if raw.endswith("\n---"):
            end_idx = len(raw) - 4
        else:
            return {}, raw
    fm_text = raw[4:end_idx]
    body = raw[end_idx + 5 :].lstrip("\n")

    meta: dict[str, Any] = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in line or line.startswith((" ", "\t", "-")):
            i += 1
            continue
        key, rhs = line.split(":", 1)
        key = key.strip()
        rhs_clean = rhs.strip()
        if rhs_clean:
            meta[key] = _parse_scalar(rhs_clean)
            i += 1
            continue
        # collect indented block (list of scalars or list of dicts)
        items: list[Any] = []
        current_dict: dict[str, Any] | None = None
        i += 1
        while i < len(lines):
            sub = lines[i]
            sub_s = sub.strip()
            if not sub_s or sub_s.startswith("#"):
                i += 1
                continue
            indent = len(sub) - len(sub.lstrip(" "))
            if indent == 0 and not sub_s.startswith("-"):
                break
            if sub_s.startswith("- "):
                rest = sub_s[2:].strip()
                if ":" in rest and not (rest.startswith('"') or rest.startswith("'")):
                    if current_dict is not None:
                        items.append(current_dict)
                    k2, v2 = rest.split(":", 1)
                    current_dict = {k2.strip(): _parse_scalar(v2)}
                else:
                    if current_dict is not None:
                        items.append(current_dict)
                        current_dict = None
                    items.append(_parse_scalar(rest))
            elif current_dict is not None and ":" in sub_s:
                k2, v2 = sub_s.split(":", 1)
                current_dict[k2.strip()] = _parse_scalar(v2)
            i += 1
        if current_dict is not None:
            items.append(current_dict)
        meta[key] = items
    return meta, body


def render_loop_markdown(spec: LoopSpec) -> str:
    """将 LoopSpec 序列化为标准 LOOP.md 文本。"""
    stages_json = json.dumps(spec.stages, ensure_ascii=False)
    lines = [
        "---",
        f"id: {spec.id}",
        f'title: "{spec.title}"',
        f'description: "{spec.description}"',
        f"topic_id: {spec.topic_id}",
        f"executor: {spec.executor}",
        f"autonomy: {spec.autonomy}",
        f"enabled: {'true' if spec.enabled else 'false'}",
        f"interval_minutes: {spec.interval_minutes:g}",
        f"active_interval_minutes: {spec.active_interval_minutes:g}",
        f'active_hours: "{spec.active_hours}"',
        f"load_cmd: {json.dumps(spec.load_cmd, ensure_ascii=False)}",
        f"load_timeout_seconds: {spec.load_timeout_seconds:g}",
        f"dedup_key: {spec.dedup_key}",
        f"autopause_after_failures: {spec.autopause_after_failures}",
        f"stages: {stages_json}",
        "plan:",
    ]
    for p in spec.plan:
        lines.append(f"  - step: {p.step}")
        lines.append(f"    does: {json.dumps(p.does, ensure_ascii=False)}")
        lines.append(f"    ask: {'true' if p.ask else 'false'}")
    lines.append("---")
    lines.append("")
    lines.append((spec.body or "").strip())
    lines.append("")
    return "\n".join(lines)


def load_loop_spec(loop_md_path: Path) -> LoopSpec:
    """从 LOOP.md 文件解析出 LoopSpec。"""
    text = loop_md_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    loop_id = str(meta.get("id") or loop_md_path.parent.name).strip()
    title = str(meta.get("title") or loop_id).strip()
    desc = str(meta.get("description") or "").strip()
    topic_id = str(meta.get("topic_id") or meta.get("topic") or "tp_bus").strip()
    executor = str(meta.get("executor") or "session").strip()
    autonomy = str(meta.get("autonomy") or AUTONOMY_SHADOW).strip().lower()
    if autonomy not in VALID_AUTONOMIES:
        autonomy = AUTONOMY_SHADOW
    enabled = bool(meta.get("enabled", True))
    interval = float(meta.get("interval_minutes") or 30.0)
    active_interval = float(meta.get("active_interval_minutes") or min(5.0, interval))
    active_hours = str(meta.get("active_hours") or "").strip()
    load_cmd = str(meta.get("load_cmd") or "").strip()
    if not load_cmd and (loop_md_path.parent / "probe.py").is_file():
        load_cmd = f'"{sys.executable}" "{loop_md_path.parent / "probe.py"}"'
    load_timeout = float(meta.get("load_timeout_seconds") or 15.0)
    dedup_key = str(meta.get("dedup_key") or "key").strip()
    autopause = int(meta.get("autopause_after_failures") or 3)
    raw_stages = meta.get("stages")
    if isinstance(raw_stages, list) and raw_stages:
        stages = [str(s).strip() for s in raw_stages if str(s).strip()]
    else:
        stages = ["detected", "confirming", "verifying", "closed"]
    raw_plan = meta.get("plan")
    plan_steps: list[LoopPlanStep] = []
    if isinstance(raw_plan, list):
        for item in raw_plan:
            if isinstance(item, dict) and item.get("step"):
                plan_steps.append(
                    LoopPlanStep(
                        step=str(item.get("step")).strip(),
                        does=str(item.get("does") or "").strip(),
                        ask=bool(item.get("ask", False)),
                    )
                )
    mem_path = loop_md_path.parent / "MEMORY.md"
    return LoopSpec(
        id=loop_id,
        title=title,
        description=desc,
        topic_id=topic_id,
        executor=executor,
        autonomy=autonomy,
        enabled=enabled,
        interval_minutes=interval,
        active_interval_minutes=active_interval,
        active_hours=active_hours,
        load_cmd=load_cmd,
        load_timeout_seconds=load_timeout,
        dedup_key=dedup_key,
        autopause_after_failures=autopause,
        stages=stages,
        plan=plan_steps,
        body=body,
        path=loop_md_path,
        memory_path=mem_path,
    )


# ======================= 内置 6 大高价值业务 Loop 模板 =======================

LOOP_TEMPLATES: dict[str, dict[str, Any]] = {
    "spend-steward": {
        "title": "全云资源利用率巡检与降本治理主理人",
        "description": "持续操心 GCP / 腾讯云 / 阿里云 / Cloudflare 资源低利用率与账单突增，跨周期追踪降配落地与 24h 稳定性验证，沉淀豁免潜规则。",
        "topic_id": "tp_cost-ops",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 360.0,
        "active_interval_minutes": 30.0,
        "active_hours": "9-23",
        "load_cmd": "",
        "dedup_key": "resource_id",
        "stages": ["detected", "confirming_owner", "applied_downscale", "verifying_24h", "closed"],
        "plan": [
            {"step": "load_utilization", "does": "扫描多云低利用率实例与账单突增项，过滤 MEMORY.md 已豁免资源", "ask": False},
            {"step": "diagnose_and_draft", "does": "核查归属服务、近期流量波峰与安全降配建议，计算月度可省金额", "ask": True},
            {"step": "apply_and_verify", "does": "测试环境直接降配 / 生产获批后降配，转入 verifying_24h 盯盘 24 小时确认零告警后结案", "ask": False},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 扫描范围：GCP（`acmelab-000002`、`acme-testing-env`、`acme-prod-000001` 等）、腾讯云国际/国内、阿里云及 Cloudflare 的计算/数据库/LB/无流量常驻实例。
- 幂等去重键：`resource_id`（格式如 `<cloud>:<project>:<service_or_instance>`）。已在 `MEMORY.md` 登记为保底/批处理豁免的实例直接跳过。

## 2. 跨周期多阶段状态机 (Stages)
1. `detected`：刚发现 CPU/内存/QPS 持续低于阈值或费用突增，查清服务 Owner 与最近 7 天峰值。
2. `confirming_owner`：已向 Matthew 或服务负责人发出降配/回收建议卡，等待确认。
3. `applied_downscale`：已执行缩容或关闭闲置资源，自动将巡检频率切换至 `active_interval_minutes`（30m）。
4. `verifying_24h`：持续核查降配后 24 小时内有无 OOM、5xx 或延迟毛刺，并核算真实账单下降幅度。
5. `closed`：验证稳定且降本生效，归档结案。若主人指示「保底别动」，执行 `sheepdog loop item spend-steward --key <id> --status exempted --note "..."` 记入豁免记忆。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- `acme-testing-env` 闲置无流量测试资源可在确认不打断现有联调链路后直接缩容并 `sheepdog report`；
- 生产环境任何规格/实例数变更必须走 `runtime-change-checklist` 附上完整回读与明确授权口令，通过 `sheepdog ask` 找 Matthew 拍板。

## 4. 异常分诊 (Triage)
- `access`：云账号凭证过期或权限不足 -> 通知对应专席补读权限，不刷屏报错；
- `business`：大促/压测期间临时保底扩容 -> 记入 `MEMORY.md` 并设置到期复查时间。

## 5. 验收与复利指标 (Observe)
- 每次结案更新累计月度节省金额（USD/CNY），零误杀生产保底容量，零重复追问同一豁免实例。""",
        "memory": """\
# spend-steward 领域经验与豁免账本 (MEMORY.md)

## 长期豁免规则（勿重复报警）
- 凡标注为灾备保底、合规独立保底或周末定时批处理的实例，在豁免有效期内跳过低利用率告警。

## 历史纠偏与豁免记录
""",
    },
    "service-release-guardian": {
        "title": "核心服务端上线盯盘、自动排障修代码、测服验证与发版闭环守护",
        "description": "盯防所负责服务上线后的线上 5xx/延迟/异常日志；抓到回归自动定位代码、写修复补丁、跑单测并部署 acme-testing-env 验证，随后递交生产授权卡片一键止血上线。",
        "topic_id": "tp_bus",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 15.0,
        "active_interval_minutes": 5.0,
        "active_hours": "",
        "load_cmd": "",
        "dedup_key": "incident_fingerprint",
        "stages": [
            "watching",
            "diagnosing_and_fixing",
            "testing_env_verify",
            "waiting_prod_approval",
            "releasing_and_post_check",
            "closed",
        ],
        "plan": [
            {"step": "watch_surface", "does": "零 Token 探测新上线 Revision 与线上 Error/P95 指标，按异常栈指纹去重", "ask": False},
            {"step": "diagnose_and_fix", "does": "对照刚上线 Commit Diff 定位根因，编写修复代码并跑通本地回归单测", "ask": False},
            {"step": "verify_testing_env", "does": "自动部署至 acme-testing-env 测试环境并执行端到端冒烟，获取测试通过回执", "ask": False},
            {"step": "ask_prod_release", "does": "向 Matthew 发送包含根因、PR Diff、测服回执与明确生产授权口令的拍板卡", "ask": True},
            {"step": "release_and_post_watch", "does": "获批后触发生产发版，切 5 分钟高频盯盘核实报错归零后 report 结案", "ask": False},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 监控目标服务上线后的 Cloud Run Revision 状态、5xx 错误率、P95 延迟与 Error 堆栈指纹。
- 幂等去重键：`incident_fingerprint`（`<service>:<revision_or_commit>:<error_signature>`）。无新增异常且无在途修复事项时 0 Token 静默跳过。

## 2. 跨周期多阶段状态机 (Stages)
1. `watching`：常态或发版后高频盯盘阶段。
2. `diagnosing_and_fixing`：捕获真实线上回归，自动结合最新 Git Diff 定位根因、在本地分支写出修复代码并跑通单测（含新增回归测试）。
3. `testing_env_verify`：将修复分支部署到 `acme-testing-env` 测试环境完成真机冒烟验证，拿到测试环境回执。
4. `waiting_prod_approval`：通过 `sheepdog ask` 向 Matthew 递交拍板卡（含根因、PR 链接、测试环境里验证回执及标准生产授权口令）。
5. `releasing_and_post_check`：收到 Matthew 明确点名授权后触发生产发布，自动以 5 分钟高频盯盘 15~30 分钟，确认报错归零。
6. `closed`：线上指标完全恢复，发送 `sheepdog report` 结案并将踩坑模式沉淀至 `MEMORY.md`。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- 本地写修复代码、跑单测、部署 `acme-testing-env` 测试环境里验证全部全自动执行，不拿半成品问主人；
- 生产环境合入与发布必须带齐测试回执与明确口令（如「回『同意发布 <service> <tag> 到生产』」），经 Matthew 点名批准后执行。

## 4. 异常分诊 (Triage)
- `platform`：云平台或上游第三方模型瞬时抖动 -> 记录观察，不盲目改业务代码；
- `code`：近期发版引入的代码逻辑或配置缺陷 -> 立即进入 `diagnosing_and_fixing` 闭环。

## 5. 验收与复利指标 (Observe)
- 从发现线上回归到备妥「代码修复 + 测服验证通过 + 生产待拍板卡片」端到端自动完成，每次事故留下一条可证伪回归测试。""",
        "memory": """\
# service-release-guardian 领域经验与豁免账本 (MEMORY.md)

## 已知瞬时抖动与非代码噪音（豁免规则）
- 客户端主动断开连接（`context canceled`）且无 5xx 伴随上升时，不触发代码修复流程。

## 历史事故与自愈记录
""",
    },
    "release-pilot-template": {
        "title": "Lark 生产发版审批单零 Token 探活与全流程引航",
        "description": "用毫秒级前置探活替代每 5 分钟盲目唤醒发版会话：无新审批单且无在途发版项时 0 Token 静默；有单时自动按 release-pilot-template 推进核验、催办、构建、切流回读与办结。",
        "topic_id": "tp_release",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 5.0,
        "active_interval_minutes": 3.0,
        "active_hours": "9-21",
        "load_cmd": "",
        "dedup_key": "approval_code",
        "stages": [
            "intake_verify",
            "waiting_initiator",
            "ready_to_trigger",
            "building_and_deploying",
            "traffic_and_readback",
            "closed",
        ],
        "plan": [
            {"step": "probe_approvals", "does": "零 Token 检查待办发版审批单与在途构建状态，无新单且无在途单时静默跳过", "ask": False},
            {"step": "verify_and_orchestrate", "does": "按服务契约核验 PR/Tag/DB 变更与版本倒挂风险，缺信息自动找发起人确认并挂 watch", "ask": False},
            {"step": "trigger_and_readback", "does": "常规清晰单按授权直接触发构建并核验 Cloud Run 100% 切流与回读，复杂高危单 ask 主人拍板", "ask": True},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 前置探活：检查是否有新的 Lark 版本迭代审批单或处于 `waiting_initiator / building_and_deploying / traffic_and_readback` 的在途单据。
- 若无新单且无在途单据，直接记 `empty`（0 Token 消耗），彻底消灭每 5 分钟空唤醒 `tp_release`。

## 2. 跨周期多阶段状态机 (Stages)
1. `intake_verify`：新单接入，核对服务契约、Tag/Commit、DB 变更与依赖拓扑。
2. `waiting_initiator`：单据信息不全或待发起人确认，已贴 Get 表情并挂载阶梯催办（15m/30m/1h）。
3. `ready_to_trigger`：前置校验全绿，准备触发生产 Cloud Build。
4. `building_and_deploying`：构建进行中（动态切 3 分钟高频跟进）。
5. `traffic_and_readback`：构建完成，执行 Cloud Run 原生 JSON 回读、100% 流量切换核验与后置动作（如 `demo-post-task`）。
6. `closed`：全部验证通过后点击 Lark 审批单通过、通知发起人并结案。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- 遵循主人对「简单且写清楚的发版可以直接发」的授权；
- 业务高峰禁发窗口自动排队等待；遇到复杂架构变更、版本倒挂或跨服务破坏性依赖时通过 `sheepdog ask` 请示 Matthew。

## 4. 异常分诊 (Triage)
- 构建失败或健康检查不通过时立即停止切流，保留旧 Revision 100% 流量并双通道告警。

## 5. 验收与复利指标 (Observe)
- 空闲时段 LLM 唤醒次数降为 0，单据响应延迟 <= 3 分钟，服务专属坑点自动沉淀进 `MEMORY.md`。""",
        "memory": """\
# release-pilot-template 领域经验与豁免账本 (MEMORY.md)

## 发版铁律
- 严禁在构建中途提前点击 Lark 审批通过；必须等部署、100% 切流、回读与后置动作全部完成后再办结审批单。
""",
    },
    "vps-node-and-whitelist-steward": {
        "title": "proxy-panel 专线节点健康度、优选 链路自愈与防火墙白名单对齐管家",
        "description": "持续巡检 多台家宽/专线 VPS 节点连通率、证书与配额，并在新增/变更节点时自动对齐 acme-testing-env 防火墙及通知受影响同学。",
        "topic_id": "tp_net-ops",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 60.0,
        "active_interval_minutes": 10.0,
        "active_hours": "",
        "load_cmd": "",
        "dedup_key": "node_event_key",
        "stages": ["detected", "healing_or_syncing", "verifying_route", "notified_users", "closed"],
        "plan": [
            {"step": "probe_nodes_and_whitelist", "does": "探测全部 VPS 节点端口/延迟与 Xray 状态，比对出口 IP 与测试环境防火墙白名单差异", "ask": False},
            {"step": "auto_heal_test_whitelist", "does": "测试环境白名单缺失直接幂等补齐并回读；节点故障执行 Xray/证书自愈；生产 DB 白名单发 ask 请示", "ask": True},
            {"step": "notify_and_close", "does": "链路恢复或新节点就绪后主动通知曾反馈异常的同学并结案", "ask": False},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 探测纳管的全部 proxy-panel VPS 节点（`node1 ~ nodeN` 等）连通性、丢包率、Xray 进程与 TLS 证书有效期，并核对出口 IP 是否全部在 `acme-testing-env` 的 `allow-demo-office` 白名单内。

## 2. 跨周期多阶段状态机 (Stages)
1. `detected`：发现新节点未加白、节点高丢包/断连或证书临期。
2. `healing_or_syncing`：自动修复 Xray/证书，或将新出口 IP 补齐至测试环境防火墙白名单（生产数据库白名单则汇总提给 DBA / 请示主人）。
3. `verifying_route`：连续观测链路延迟与 0 丢包恢复情况。
4. `notified_users`：若此前有同学（如 Basil Cui / Bruno 等）登记等待恢复通知，恢复确认后自动代回告知。
5. `closed`：白名单与节点健康度 100% 闭环。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- `acme-testing-env` 防火墙白名单同步与节点 Xray 服务重启可直接自治完成并 `report`；
- 生产环境数据库白名单或付费增购节点通过 `sheepdog ask` 确认。

## 4. 异常分诊 (Triage)
- 跨境骨干网晚高峰瞬时抖动先切备选节点观察，不误发全员恢复通知。

## 5. 验收与复利指标 (Observe)
- 新购 VPS 上线后自动触发测试白名单同步，零人工漏加白。""",
        "memory": """\
# vps-node-and-whitelist-steward 领域经验与豁免账本 (MEMORY.md)

## 节点与白名单基线
- 新增任何 proxy-panel 专线节点后，必须同步检查 `acme-testing-env` 防火墙 `allow-demo-office` 是否已包含其 `/32` 出口 IP。
""",
    },
    "ssl-cert-lifecycle-steward": {
        "title": "多云 SSL/TLS 证书到期预警、自动续期与 ELB 挂载核验管家",
        "description": "零 Token 巡检 GCP / 腾讯云 / 阿里云全量域名 SSL 证书剩余有效期，临期自动推进续期、挂载与真机 TLS 握手验证。",
        "topic_id": "tp_cert-ops",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 720.0,
        "active_interval_minutes": 60.0,
        "active_hours": "9-22",
        "load_cmd": "",
        "dedup_key": "domain_cert_id",
        "stages": ["expiring_detected", "renewing_cert", "waiting_mount_approval", "verifying_tls_handshake", "closed"],
        "plan": [
            {"step": "probe_cert_expiry", "does": "零 Token 扫描全网域名与云端证书剩余天数（< 21 天触发跟进）", "ask": False},
            {"step": "prepare_renewal", "does": "申请/上传新证书或核实托管证书自动续期状态，生成 ELB/CDN 替换方案", "ask": True},
            {"step": "verify_handshake", "does": "完成替换后用 openssl/curl 实测线上生效序列号与到期日，退役旧证书并结案", "ask": False},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 定期探测核心生产与预发域名（`*.site-b.example`、`*.site-c.example`、`*.site-d.example` 等）TLS 证书到期时间与云端证书台账。剩余有效期 >= 21 天时 0 Token 静默跳过。

## 2. 跨周期多阶段状态机 (Stages)
1. `expiring_detected`：发现证书剩余天数 $< 21$ 天。
2. `renewing_cert`：备妥新证书或核验托管续期状态。
3. `waiting_mount_approval`：涉及生产 ELB / CDN 证书切换的，递交含回滚边界的拍板卡。
4. `verifying_tls_handshake`：切换后实测多地域 TLS 握手到期日更新无误。
5. `closed`：清理过期旧证书并更新台账。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- 证书到期探测与测试环境更新直接自治；生产负载均衡器证书切换必走 `sheepdog ask` 确认。

## 4. 异常分诊 (Triage)
- DNS CNAME 验证未生效时自动挂载跟进检查，不重复建单。

## 5. 验收与复利指标 (Observe)
- 全网生产与测试域名 100% 零证书过期事故。""",
        "memory": """\
# ssl-cert-lifecycle-steward 领域经验与豁免账本 (MEMORY.md)

## 证书台账记忆
- 优先推进全托管自动续期证书替手工上传证书。
""",
    },
    "env-migration-daily-steward": {
        "title": "一批工作负载跨云迁移交割阻塞追踪与内部台账静默跟进循环",
        "description": "持续追踪 一批工作负载向腾讯云迁移的构建、配置中心/Polaris 配置对齐、网络探针与阻塞卡点，仅在内部台账静默跟进，不自动发起每日定时 群内长表播报。",
        "topic_id": "tp_migrate-ops",
        "executor": "session",
        "autonomy": AUTONOMY_SHADOW,
        "interval_minutes": 240.0,
        "active_interval_minutes": 60.0,
        "active_hours": "10-22",
        "load_cmd": "",
        "dedup_key": "migration_item_key",
        "stages": ["blocker_detected", "coordinating_owner", "verifying_dryrun", "ready_for_cutover", "closed"],
        "plan": [
            {"step": "scan_migration_delta", "does": "盘点当日迁移群新进展、候选分支 dry-run 状态与未解阻塞项", "ask": False},
            {"step": "nudge_or_verify", "does": "跟进阻塞项负责人或执行镜像 TCR 推送与配置对齐校验", "ask": False},
            {"step": "ledger_sync_or_escalate_blocker", "does": "更新内部台账与 Linear 单据静默收口（不自动发起每日定时 群内长表播报），仅遇高危生产切流阻塞时 ask", "ask": True},
        ],
        "body": """\
## 1. 取料与去重标准 (Load & Dedup)
- 扫描「示例迁移群」群最新消息、在途候选分支构建与 一批工作负载台账变化；无新增变化时 0 Token 静默（不生成每日定时 群内长篇日清进度表播报）。

## 2. 跨周期多阶段状态机 (Stages)
1. `blocker_detected`：识别到某微服务配置缺失、网络不通或镜像构建阻塞。
2. `coordinating_owner`：协同对应研发/SRE 专席（如 `tp_prod-topology` / `tp_region-sre`）推进解决。
3. `verifying_dryrun`：核验 TCR 镜像 Digest、Polaris 配置与 server-side dry-run 结果。
4. `ready_for_cutover`：具备上线/切流条件，更新内部台账与对应 Linear 单据。
5. `closed`：该工作负载交割闭环。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- 构建镜像、拉取配置对比与 dry-run 验证自动推进；不在飞书群里发长篇进度表（遵循 `DEMO-128` 第 6 条与主人此前的反馈），仅遇真实生产 apply 决策点时受控请示。

## 4. 异常分诊 (Triage)
- 群内同事互相讨论且无需运维动作时保持静默背景吸收，不插话刷屏。

## 5. 验收与复利指标 (Observe)
- 阻塞卡点日清日结，一批工作负载迁移进度透明可查。""",
        "memory": """\
# env-migration-daily-steward 领域经验与豁免账本 (MEMORY.md)

## 协同纪律
- 群内同步默认简洁直接，非必要不额外 `@` 高管。
""",
    },
}


DEFAULT_LOOP_INTAKE_SKILL_MD = """\
---
name: sheepdog-loop-intake
description: "对话式创建与升级 Sheepdog 业务循环（Loop）。当用户说『帮我建个 loop』『把这件事做成循环』『一直帮我操心某事』时使用。"
---

# Sheepdog 对话式建 Loop 指南（Loop Intake & Authoring）

当主人希望把某件「需要持续操心、定期巡检、出问题自动修、从反馈里越跑越聪明」的事做成 Loop 时，按以下步骤闭环：

## 1. 先查能不能直接套用内置模板
运行 `sheepdog loop recommend` 或检查 6 大内置模板：
- `spend-steward`（全云资源利用率巡检与降本闭环）
- `service-release-guardian`（单服务上线盯盘 $\\to$ 自动排障修代码 $\\to$ 测服验证 $\\to$ 拿生产授权发版闭环）
- `release-pilot-template`（Lark 发版审批单零 Token 探活与引航）
- `vps-node-and-whitelist-steward`（proxy-panel 专线节点健康度与测试白名单自动对齐）
- `ssl-cert-lifecycle-steward`（多云 SSL/TLS 证书到期预警与续期核验）
- `env-migration-daily-steward`（跨云迁移阻塞跟踪与每日进度播报）

## 2. 明确 5 个业务格子（若主人已说清则直接推断，不反复盘问）
1. **① 取料与去重（`load_cmd` & `dedup_key`）**：怎么用最轻的命令判断「现在有没有新活」？按什么唯一键防重复？
2. **② 多阶段跟进状态机（`stages`）**：一件事从发现到彻底解决要经历哪几个阶段（例如 `detected -> fixing -> testing -> waiting_approval -> closed`）？
3. **③ 闲忙动态变频（`interval_minutes` vs. `active_interval_minutes`）**：平时多久探一次？手头有活在跟进时加密到几分钟盯一次？
4. **④ 拍板边界（`autonomy: shadow | confirm | auto`）**：新建默认 `shadow`（影子试跑发预览卡，主人点按钮再升 `confirm` 或 `auto`）。
5. **⑤ 专属记忆与豁免（`MEMORY.md`）**：哪些已知情况属于豁免噪音？

## 3. 一键落盘并触发首次影子试跑
1. 初始化循环目录与 `LOOP.md` / `MEMORY.md`：
   `sheepdog loop init <loop-id> --template <模板名或 custom> --title "<标题>" --topic <负责会话>`
2. 按需编辑 `~/.config/sheepdog/loops/<loop-id>/LOOP.md` 填入具体的 `load_cmd`、阶段和规则。
3. 触发一次 `shadow` 试跑并向主人发送带 **`[🚀 升为拍板模式 (confirm)]` / `[⚡ 升为全自动 (auto)]`** 按钮的预览卡：
   `sheepdog loop run <loop-id> --force`
"""


def ensure_loop_intake_skill(cfg: Config) -> Path:
    """确保内置对话式建 Loop 技能文件存在。"""
    p = cfg.config_path.parent / "skills" / "sheepdog-loop-intake" / "SKILL.md"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_LOOP_INTAKE_SKILL_MD.rstrip() + "\n", encoding="utf-8")
    return p


def init_loop(
    cfg: Config,
    store: Store,
    loop_id: str,
    *,
    template: str = "",
    title: str = "",
    topic_id: str = "",
    autonomy: str = "",
    load_cmd: str = "",
    interval_minutes: float | None = None,
    active_interval_minutes: float | None = None,
    overwrite: bool = False,
) -> LoopSpec:
    """初始化一条声明式业务 Loop（创建 ~/.config/sheepdog/loops/<id>/LOOP.md 与 MEMORY.md 并注册状态）。"""
    clean_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", (loop_id or "").strip()).strip("-").lower()
    if not clean_id:
        raise ValueError("loop_id 不能为空")
    ensure_loop_tables(store)
    ensure_loop_intake_skill(cfg)

    tpl_key = (template or clean_id).strip().lower()
    tpl = LOOP_TEMPLATES.get(tpl_key, {})

    loop_dir = cfg.loops_dir / clean_id
    loop_md_path = loop_dir / "LOOP.md"
    mem_path = loop_dir / "MEMORY.md"

    if loop_md_path.exists() and not overwrite:
        spec = load_loop_spec(loop_md_path)
        _sync_spec_to_state(store, spec)
        return spec

    loop_dir.mkdir(parents=True, exist_ok=True)
    resolved_autonomy = (autonomy or tpl.get("autonomy") or AUTONOMY_SHADOW).strip().lower()
    if resolved_autonomy not in VALID_AUTONOMIES:
        resolved_autonomy = AUTONOMY_SHADOW

    raw_plan = tpl.get("plan") or [
        {"step": "load", "does": "零 Token 前置探测新增待办或异常事项并按唯一键去重", "ask": False},
        {"step": "execute", "does": "唤醒绑定会话或羔羊执行诊断、修复与验证", "ask": resolved_autonomy != AUTONOMY_AUTO},
        {"step": "settle", "does": "推进事项阶段直至验证闭环，沉淀经验至 MEMORY.md", "ask": False},
    ]
    plan_steps = [
        LoopPlanStep(step=str(p["step"]), does=str(p["does"]), ask=bool(p.get("ask", False)))
        for p in raw_plan
    ]

    spec = LoopSpec(
        id=clean_id,
        title=(title or tpl.get("title") or clean_id).strip(),
        description=str(tpl.get("description") or "声明式自进化业务循环").strip(),
        topic_id=(topic_id or tpl.get("topic_id") or "tp_bus").strip(),
        executor=str(tpl.get("executor") or "session").strip(),
        autonomy=resolved_autonomy,
        enabled=True,
        interval_minutes=float(interval_minutes if interval_minutes is not None else (tpl.get("interval_minutes") or 30.0)),
        active_interval_minutes=float(
            active_interval_minutes if active_interval_minutes is not None else (tpl.get("active_interval_minutes") or 5.0)
        ),
        active_hours=str(tpl.get("active_hours") or ""),
        load_cmd=(load_cmd if load_cmd else str(tpl.get("load_cmd") or "")).strip(),
        load_timeout_seconds=15.0,
        dedup_key=str(tpl.get("dedup_key") or "key"),
        autopause_after_failures=int(tpl.get("autopause_after_failures") or 3),
        stages=list(tpl.get("stages") or ["detected", "confirming", "verifying", "closed"]),
        plan=plan_steps,
        body=str(
            tpl.get("body")
            or """\
## 1. 取料与去重标准 (Load & Dedup)
- 前置探测：无新增事项且无在途未结事项时 0 Token 静默跳过。

## 2. 跨周期多阶段状态机 (Stages)
- 按 `detected -> confirming -> verifying -> closed` 逐阶段推进，未结案事项在下一轮自动接续跟进。

## 3. 拍板边界与自治规则 (Gate: shadow / confirm / auto)
- `shadow` 仅发送试跑预览卡；`confirm` 先准备证据与测试回执再通过 `sheepdog ask` 请示；`auto` 在红线内直接闭环并 `report`。

## 4. 异常分诊 (Triage)
- 连续失败达到阈值自动熔断暂停并告警。

## 5. 验收与复利指标 (Observe)
- 主人每次驳回或补充说明自动沉淀进同目录 `MEMORY.md`，由 `sheepdog-evolve` 持续调优。"""
        ),
        path=loop_md_path,
        memory_path=mem_path,
    )

    loop_md_path.write_text(render_loop_markdown(spec), encoding="utf-8")
    if not mem_path.exists() or overwrite:
        default_mem = str(
            tpl.get("memory")
            or f"# {spec.id} 领域经验与豁免账本 (MEMORY.md)\n\n## 长期豁免规则（勿重复报警）\n\n## 历史纠偏记录\n"
        )
        mem_path.write_text(default_mem.rstrip() + "\n", encoding="utf-8")

    _sync_spec_to_state(store, spec, force_autonomy=bool(autonomy))
    return spec


def _sync_spec_to_state(store: Store, spec: LoopSpec, *, force_autonomy: bool = False) -> dict[str, Any]:
    ensure_loop_tables(store)
    now = now_iso()
    row = store.conn.execute("SELECT * FROM loop_state WHERE loop_id = ?", (spec.id,)).fetchone()
    if row is None:
        store.conn.execute(
            """INSERT INTO loop_state
               (loop_id, enabled, autonomy, paused, paused_reason, consecutive_failures,
                last_run_at, last_status, last_run_id, total_runs, empty_runs, action_runs, updated_at)
               VALUES (?, ?, ?, 0, '', 0, '', '', '', 0, 0, 0, ?)""",
            (spec.id, 1 if spec.enabled else 0, spec.autonomy, now),
        )
        store.conn.commit()
    else:
        new_autonomy = spec.autonomy if force_autonomy else (row["autonomy"] or spec.autonomy)
        store.conn.execute(
            "UPDATE loop_state SET enabled = ?, autonomy = ?, updated_at = ? WHERE loop_id = ?",
            (1 if spec.enabled else 0, new_autonomy, now, spec.id),
        )
        store.conn.commit()
    return get_loop_state(store, spec.id)


def get_loop_state(store: Store, loop_id: str) -> dict[str, Any]:
    ensure_loop_tables(store)
    row = store.conn.execute("SELECT * FROM loop_state WHERE loop_id = ?", (loop_id,)).fetchone()
    if row is None:
        return {
            "loop_id": loop_id,
            "enabled": True,
            "autonomy": AUTONOMY_SHADOW,
            "paused": False,
            "paused_reason": "",
            "consecutive_failures": 0,
            "last_run_at": "",
            "last_status": "",
            "last_run_id": "",
            "total_runs": 0,
            "empty_runs": 0,
            "action_runs": 0,
            "updated_at": "",
        }
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    d["paused"] = bool(d.get("paused", 0))
    return d


def discover_loops(cfg: Config, store: Store | None = None) -> list[LoopSpec]:
    """扫描 ~/.config/sheepdog/loops/*/LOOP.md 并返回全部 LoopSpec。"""
    root = cfg.loops_dir
    specs: list[LoopSpec] = []
    if root.is_dir():
        for p in sorted(root.glob("*/LOOP.md")):
            try:
                spec = load_loop_spec(p)
                if store is not None:
                    st = _sync_spec_to_state(store, spec)
                    spec.autonomy = st.get("autonomy") or spec.autonomy
                    spec.enabled = bool(st.get("enabled", spec.enabled))
                specs.append(spec)
            except Exception as e:
                log.warning("解析 LOOP.md 失败 (%s): %s", p, e)
    return specs


_SAFE_LOOP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _validate_loop_id(loop_id: str) -> str:
    s = (loop_id or "").strip()
    if not s or not _SAFE_LOOP_ID_RE.match(s) or ".." in s:
        raise ValueError(f"非法 loop_id: {loop_id!r}")
    return s


def get_loop_spec(cfg: Config, store: Store | None, loop_id: str) -> LoopSpec | None:
    try:
        safe_id = _validate_loop_id(loop_id)
    except ValueError:
        return None
    p = cfg.loops_dir / safe_id / "LOOP.md"
    if not p.is_file() or p.is_symlink():
        return None
    spec = load_loop_spec(p)
    if store is not None:
        st = _sync_spec_to_state(store, spec)
        spec.autonomy = st.get("autonomy") or spec.autonomy
        spec.enabled = bool(st.get("enabled", spec.enabled))
    return spec


def promote_loop(cfg: Config, store: Store, loop_id: str, target_autonomy: str) -> dict[str, Any]:
    """切换 Loop 的自治水位（shadow | confirm | auto），同步回写 LOOP.md 与 SQLite loop_state。"""
    target = (target_autonomy or "").strip().lower()
    if target not in VALID_AUTONOMIES:
        raise ValueError(f"autonomy 只能是 {' / '.join(VALID_AUTONOMIES)}")
    ensure_loop_tables(store)
    spec = get_loop_spec(cfg, store, loop_id)
    if spec is None:
        raise ValueError(f"循环 {loop_id!r} 不存在")
    spec.autonomy = target
    if spec.path is not None:
        spec.path.write_text(render_loop_markdown(spec), encoding="utf-8")
    now = now_iso()
    store.conn.execute(
        """UPDATE loop_state
           SET autonomy = ?, paused = 0, paused_reason = '', consecutive_failures = 0, updated_at = ?
           WHERE loop_id = ?""",
        (target, now, loop_id),
    )
    store.conn.commit()
    return get_loop_state(store, loop_id)


def pause_loop(cfg: Config, store: Store, loop_id: str, reason: str = "人工暂停") -> dict[str, Any]:
    ensure_loop_tables(store)
    spec = get_loop_spec(cfg, store, loop_id)
    if spec is None:
        raise ValueError(f"循环 {loop_id!r} 不存在")
    now = now_iso()
    store.conn.execute(
        "UPDATE loop_state SET paused = 1, paused_reason = ?, updated_at = ? WHERE loop_id = ?",
        (reason or "人工暂停", now, loop_id),
    )
    store.conn.commit()
    return get_loop_state(store, loop_id)


def resume_loop(cfg: Config, store: Store, loop_id: str) -> dict[str, Any]:
    ensure_loop_tables(store)
    spec = get_loop_spec(cfg, store, loop_id)
    if spec is None:
        raise ValueError(f"循环 {loop_id!r} 不存在")
    now = now_iso()
    store.conn.execute(
        "UPDATE loop_state SET paused = 0, paused_reason = '', consecutive_failures = 0, updated_at = ? WHERE loop_id = ?",
        (now, loop_id),
    )
    store.conn.commit()
    return get_loop_state(store, loop_id)


# ======================= 事项多阶段状态机 (loop_items) =======================

def list_loop_items(
    store: Store,
    loop_id: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_loop_tables(store)
    if status:
        rows = store.conn.execute(
            "SELECT * FROM loop_items WHERE loop_id = ? AND status = ? ORDER BY updated_at DESC LIMIT ?",
            (loop_id, status, limit),
        ).fetchall()
    else:
        rows = store.conn.execute(
            "SELECT * FROM loop_items WHERE loop_id = ? ORDER BY updated_at DESC LIMIT ?",
            (loop_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except Exception:
            d["payload"] = {}
        try:
            d["context"] = json.loads(d.get("context_json") or "{}")
        except Exception:
            d["context"] = {}
        out.append(d)
    return out


_WAITING_STAGE_PREFIXES = ("waiting_", "confirming")
_STATE_IDENTITY_PAYLOAD_KEYS = (
    "status",
    "statusType",
    "updatedAt",
    "updated_at",
    "assignee",
    "state",
    "task_id",
    "revision",
    "digest",
    "pr_state",
    "version",
    "min_scale",
    "days_left",
)
_NON_EXTERNAL_METADATA_KEYS = {
    "title",
    "stage",
    "bucket",
    "priority_val",
    "defer_check_ready_at",
    "defer_check_output",
    "last_defer_check_rc",
    "last_defer_check_at",
    "deferred_at",
}
_VOLATILE_DEFER_KEYS = {
    "defer_check_ready_at",
    "defer_check_output",
    "last_defer_check_rc",
    "last_defer_check_at",
    "deferred_at",
}


def _is_waiting_stage(stage: str | None) -> bool:
    s = str(stage or "").strip().lower()
    return bool(s and s.startswith(_WAITING_STAGE_PREFIXES))


def _has_external_payload_changed(old_payload: Any, new_payload: Any) -> bool:
    """判断外部探针返回的事项负载是否发生了实质状态变化（如 status / updatedAt / assignee 变更）。"""
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict) or not old_payload:
        return bool(old_payload != new_payload)
    has_identity = any(k in old_payload or k in new_payload for k in _STATE_IDENTITY_PAYLOAD_KEYS)
    ignore_keys = _NON_EXTERNAL_METADATA_KEYS if has_identity else _VOLATILE_DEFER_KEYS
    old_clean = {k: v for k, v in old_payload.items() if k not in ignore_keys}
    new_clean = {k: v for k, v in new_payload.items() if k not in ignore_keys}
    return old_clean != new_clean


def update_loop_item(
    cfg: Config,
    store: Store,
    loop_id: str,
    item_key: str,
    *,
    title: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    note: str = "",
    next_minutes: float | None = None,
    payload: dict[str, Any] | None = None,
    clear_next_check: bool = False,
    keep_longer_next: bool = False,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """推进或更新单条跟进事项的阶段（stage）、状态（active|closed|exempted）及下次检查时间；豁免时自动写 MEMORY.md。"""
    loop_id = _validate_loop_id(loop_id)
    ensure_loop_tables(store)
    spec = get_loop_spec(cfg, store, loop_id)
    now_dt = now_dt or datetime.now(timezone.utc).astimezone()
    now_s = now_dt.isoformat(timespec="seconds")
    next_s = (
        (now_dt + timedelta(minutes=float(next_minutes))).isoformat(timespec="seconds")
        if next_minutes is not None and next_minutes > 0
        else ""
    )

    row = store.conn.execute(
        "SELECT * FROM loop_items WHERE loop_id = ? AND item_key = ?",
        (loop_id, item_key),
    ).fetchone()

    init_stage = stage or (spec.stages[0] if spec and spec.stages else "detected")
    resolved_status = (status or (row["status"] if row else ITEM_ACTIVE)).strip().lower()
    if resolved_status not in VALID_ITEM_STATUSES:
        raise ValueError(f"事项 status 只能是 {' / '.join(VALID_ITEM_STATUSES)}")
    if stage == "closed" and status is None:
        resolved_status = ITEM_CLOSED

    ctx: dict[str, Any] = {}
    if row is not None:
        try:
            ctx = json.loads(row["context_json"] or "{}")
        except Exception:
            ctx = {}
    history = list(ctx.get("history") or [])
    if stage or status or note:
        history.append(
            {
                "at": now_s,
                "stage": stage or (row["stage"] if row else init_stage),
                "status": resolved_status,
                "note": note,
            }
        )
    ctx["history"] = history[-20:]
    if note:
        ctx["last_note"] = note

    default_wait_m = max(float(spec.interval_minutes if spec else 30.0), 60.0)
    if row is None:
        if clear_next_check:
            init_next = ""
        elif next_s:
            init_next = next_s
        elif _is_waiting_stage(init_stage) and resolved_status == ITEM_ACTIVE:
            init_next = (now_dt + timedelta(minutes=default_wait_m)).isoformat(timespec="seconds")
        else:
            init_next = ""
        store.conn.execute(
            """INSERT INTO loop_items
               (loop_id, item_key, title, stage, status, payload_json, context_json,
                escalation_key, last_run_id, next_check_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)""",
            (
                loop_id,
                item_key,
                title or item_key,
                init_stage,
                resolved_status,
                json.dumps(payload or {}, ensure_ascii=False),
                json.dumps(ctx, ensure_ascii=False),
                init_next,
                now_s,
                now_s,
            ),
        )
    else:
        new_title = title if title is not None else row["title"]
        new_stage = stage if stage is not None else ("closed" if resolved_status == ITEM_CLOSED else row["stage"])
        new_payload = json.dumps(payload, ensure_ascii=False) if payload is not None else row["payload_json"]
        prev_next = str(row["next_check_at"] or "").strip()
        if clear_next_check:
            new_next = ""
        elif next_minutes is not None:
            new_next = prev_next if (keep_longer_next and prev_next > next_s) else next_s
        elif stage is not None and _is_waiting_stage(stage) and resolved_status == ITEM_ACTIVE and (not prev_next or prev_next <= now_s):
            new_next = (now_dt + timedelta(minutes=default_wait_m)).isoformat(timespec="seconds")
        else:
            new_next = prev_next
        store.conn.execute(
            """UPDATE loop_items
               SET title = ?, stage = ?, status = ?, payload_json = ?, context_json = ?,
                   next_check_at = ?, updated_at = ?
               WHERE loop_id = ? AND item_key = ?""",
            (new_title, new_stage, resolved_status, new_payload, json.dumps(ctx, ensure_ascii=False), new_next, now_s, loop_id, item_key),
        )
    store.conn.commit()

    # 若标记为 exempted，自动追加进该 Loop 的 MEMORY.md 长期豁免账本
    if resolved_status == ITEM_EXEMPTED and spec is not None and spec.memory_path is not None:
        _append_memory_note(
            spec.memory_path,
            f"- [{now_dt.strftime('%Y-%m-%d %H:%M')}] 豁免项 `{item_key}`: {note or '人工标记豁免，后续巡检跳过'}",
        )

    items = [x for x in list_loop_items(store, loop_id, limit=200) if x["item_key"] == item_key]
    return items[0] if items else {"loop_id": loop_id, "item_key": item_key, "status": resolved_status}


def register_deferred_report_item(
    cfg: Config,
    store: Store,
    topic_id: str,
    defer_item: str,
    *,
    defer_loop: str = "",
    defer_check_cmd: str = "",
    next_minutes: float = 5.0,
) -> dict[str, Any]:
    """REG-091：由 `sheepdog report --defer-item` 原生登记在途异步收尾事项到 `loop_items`，
    自动进入 `awaiting_async_ready` 阶段并绑定零 Token `defer_check_cmd`。
    """
    ensure_loop_tables(store)
    raw = (defer_item or "").strip()
    if not raw:
        raise ValueError("--defer-item 不能为空（格式：'<item_key>:<待收尾说明>'）")
    if ":" in raw:
        ikey_raw, _, ititle_raw = raw.partition(":")
        ikey = ikey_raw.strip()
        ititle = ititle_raw.strip() or ikey
    else:
        ikey = f"defer-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]}"
        ititle = raw
    if not ikey:
        ikey = f"defer-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]}"

    target_loop = (defer_loop or "").strip()
    specs = discover_loops(cfg, store)
    spec_ids = {s.id for s in specs}
    if not target_loop:
        for s in specs:
            if s.topic_id == topic_id:
                target_loop = s.id
                break
    if not target_loop:
        low_text = f"{topic_id} {raw} {defer_check_cmd}".lower()
        if any(w in low_text for w in ("ssl", "cert", "tls", "cname", "cdn", "dns", "证书", "解析")):
            target_loop = "ssl-cert-lifecycle-steward"
        elif "service-release-guardian" in spec_ids:
            target_loop = "service-release-guardian"
        elif specs:
            target_loop = specs[0].id
        else:
            target_loop = "service-release-guardian"
    if target_loop not in spec_ids:
        init_loop(
            cfg,
            store,
            target_loop,
            template=target_loop if target_loop in LOOP_TEMPLATES else "service-release-guardian",
            topic_id=topic_id or "tp_bus",
        )

    payload = {
        "source_topic": topic_id,
        "defer_check_cmd": (defer_check_cmd or "").strip(),
        "deferred_at": now_iso(),
    }
    updated = update_loop_item(
        cfg,
        store,
        target_loop,
        ikey,
        title=ititle,
        stage="awaiting_async_ready",
        status=ITEM_ACTIVE,
        note=f"由 {topic_id} report --defer-item 挂入自动盯盘",
        next_minutes=next_minutes,
        payload=payload,
    )
    return {
        "loop_id": target_loop,
        "item_key": ikey,
        "title": ititle,
        "stage": updated.get("stage") or "awaiting_async_ready",
        "defer_check_cmd": (defer_check_cmd or "").strip(),
    }


def _normalize_feedback_text(text: str) -> str:
    s = re.sub(r"^\[卡片回调\]\s*", "", (text or "").strip()).strip()
    s = re.sub(r"^([A-Ea-e])\s*[·:：.、]\s*", lambda m: f"{m.group(1).upper()}：", s)
    return s


def _append_memory_note(mem_path: Path, line: str) -> None:
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    existing = mem_path.read_text(encoding="utf-8") if mem_path.exists() else ""
    clean_line = line.strip()
    if clean_line in existing:
        return
    prefix_re = r"^-\s*\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\]\s*(?:主人反馈纠偏:\s*)?"
    new_core = _normalize_feedback_text(re.sub(prefix_re, "", clean_line))
    if not new_core:
        return
    lines = existing.splitlines()
    replaced = False
    for idx, ln in enumerate(lines):
        old_core = _normalize_feedback_text(re.sub(prefix_re, "", ln.strip()))
        if not old_core:
            continue
        if old_core == new_core or (len(old_core) >= len(new_core) and old_core.startswith(new_core)):
            return
        if len(new_core) > len(old_core) and new_core.startswith(old_core):
            lines[idx] = clean_line
            replaced = True
            break
    if replaced:
        mem_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return
    updated = existing.rstrip() + "\n" + clean_line + "\n"
    mem_path.write_text(updated, encoding="utf-8")


def record_loop_feedback(
    cfg: Config,
    store: Store,
    loop_id: str,
    feedback_text: str,
    *,
    run_id: str = "",
) -> None:
    """当主人在飞书上对某条 Loop 的卡片给出修改/驳回意见时，记入 loop_runs.human_feedback 与 MEMORY.md。"""
    ensure_loop_tables(store)
    clean_fb = (feedback_text or "").strip()
    if not clean_fb:
        return
    norm_fb = _normalize_feedback_text(clean_fb) or clean_fb
    now_dt = datetime.now(timezone.utc).astimezone()
    if run_id:
        store.conn.execute(
            "UPDATE loop_runs SET human_feedback = ? WHERE run_id = ?",
            (norm_fb, run_id),
        )
    else:
        row = store.conn.execute(
            """SELECT run_id, human_feedback FROM loop_runs
               WHERE loop_id = ?
               ORDER BY CASE WHEN status NOT IN ('empty', 'failed') THEN 0 WHEN status = 'empty' THEN 1 ELSE 2 END, started_at DESC
               LIMIT 1""",
            (loop_id,),
        ).fetchone()
        if row:
            prev_fb = str(row["human_feedback"] or "").strip()
            prev_parts = [_normalize_feedback_text(p) for p in prev_fb.split(" | ") if p.strip()] if prev_fb else []
            all_parts = [p for p in [*prev_parts, norm_fb] if p]
            kept: list[str] = []
            for p in all_parts:
                if any(other != p and len(other) > len(p) and other.startswith(p) for other in all_parts):
                    continue
                if p not in kept:
                    kept.append(p)
            merged_fb = " | ".join(kept) or norm_fb
            store.conn.execute(
                "UPDATE loop_runs SET human_feedback = ? WHERE run_id = ?",
                (merged_fb, row["run_id"]),
            )
    store.conn.commit()
    spec = get_loop_spec(cfg, store, loop_id)
    if spec is not None and spec.memory_path is not None:
        _append_memory_note(
            spec.memory_path,
            f"- [{now_dt.strftime('%Y-%m-%d %H:%M')}] 主人反馈纠偏: {norm_fb}",
        )


_LOOP_FEEDBACK_OVERRIDE_RE = re.compile(
    r"(?:\[其他\]|其他[:：]|暂(?:时)?不(?:同步|播报|发)|取消|不对|不要|别发|先不)",
    re.I,
)


def maybe_record_escalation_loop_feedback(
    cfg: Config,
    store: Store,
    esc_mid: str,
    topic_id: str,
    chosen_display: str,
) -> str | None:
    """REG-103 / REG-105：当主人对显式关联 Loop（--key loop-<id> 或命中 loop_items）的拍板卡给出 [其他] 或纠偏/暂缓指令时，自动写入 loop_runs.human_feedback 与 MEMORY.md。"""
    disp = (chosen_display or "").strip()
    if not disp or not _LOOP_FEEDBACK_OVERRIDE_RE.search(disp):
        return None
    ensure_loop_tables(store)
    loop_id = ""
    if esc_mid:
        s_row = store.conn.execute(
            "SELECT key FROM sends WHERE message_id = ? LIMIT 1",
            (esc_mid,),
        ).fetchone()
        if s_row and s_row["key"]:
            k = str(s_row["key"]).strip()
            if k.lower().startswith("loop-"):
                loop_id = k[5:].strip()
            else:
                li_row = store.conn.execute(
                    "SELECT loop_id FROM loop_items WHERE escalation_key = ? OR item_key = ? LIMIT 1",
                    (k, k),
                ).fetchone()
                if li_row and li_row["loop_id"]:
                    loop_id = str(li_row["loop_id"]).strip()
    if not loop_id:
        return None
    record_loop_feedback(cfg, store, loop_id, disp)
    return loop_id


# ======================= 零 Token 前置取料 (① Load) 与解析 =======================

def _run_load_command(cmd: str, timeout_seconds: float = 15.0, cwd: Path | None = None) -> tuple[int, str, str]:
    """在独立进程组运行前置取料命令 load_cmd，超时强杀整组子进程，绝不阻塞守护进程。"""
    if not (cmd or "").strip():
        return 0, "", ""
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout_seconds)))
        return proc.returncode or 0, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return 124, "", f"load_cmd 执行超时 (>{timeout_seconds:g}s)"


def parse_load_output(stdout: str, dedup_key: str = "key") -> list[dict[str, Any]]:
    """解析 load_cmd 的标准输出为规范化 Item 列表；空输出或 {'items':[]} 返回 []。"""
    s = (stdout or "").strip()
    if not s:
        return []
    raw_list: list[Any] = []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            if parsed.get("has_work") is False:
                return []
            items_val = parsed.get("items")
            if isinstance(items_val, list):
                raw_list = items_val
            elif parsed:
                raw_list = [parsed]
        elif isinstance(parsed, list):
            raw_list = parsed
    except Exception:
        # 兼容 JSONL 或按行文本输出
        for line in s.splitlines():
            ln = line.strip()
            if not ln or ln.lower() in ("[]", "{}", "null", "none"):
                continue
            try:
                obj = json.loads(ln)
                if isinstance(obj, list):
                    raw_list.extend(obj)
                    continue
                if isinstance(obj, dict):
                    raw_list.append(obj)
                    continue
            except Exception:
                pass
            raw_list.append({"title": ln[:120], "raw": ln})

    normalized: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            item = {"title": str(item)[:120], "raw": str(item)}
        else:
            # 安全加固（REG-125）：严禁外部探测脚本 stdout JSON 注入 defer_check_cmd 触发 shell=True RCE
            item = {k: v for k, v in item.items() if k != "defer_check_cmd"}
        k = str(
            item.get(dedup_key)
            or item.get("key")
            or item.get("id")
            or hashlib.sha1(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        ).strip()
        if not k:
            continue
        ttl = str(item.get("title") or item.get("name") or item.get("summary") or k).strip()
        normalized.append({"key": k, "title": ttl[:140], "payload": item})
    return normalized


# ======================= 飞书试跑预览卡片与熔断卡片构建 =======================

def build_loop_shadow_card(
    spec: LoopSpec,
    due_items: list[dict[str, Any]],
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """构建 Card 2.0『🐕 汇报 · 循环试跑预览』卡片，带一键升为 confirm / auto 与暂停按钮。"""
    plan_lines = []
    for idx, p in enumerate(spec.plan, 1):
        gate_tag = " `🙋 需拍板`" if p.ask else ""
        plan_lines.append(f"{idx}. **{p.step}**：{p.does}{gate_tag}")
    plan_md = "\n".join(plan_lines) if plan_lines else "1. **load** -> 2. **execute** -> 3. **verify**"

    item_lines = []
    for it in due_items[:6]:
        item_lines.append(f"- `{it['item_key']}` [{it.get('stage') or 'detected'}]：{it.get('title') or it['item_key']}")
    if not item_lines:
        item_lines.append("- （当前无待处理事项，已按规则完成空跑探活验证）")

    text = (
        f"🐕 汇报 · 循环试跑预览：{spec.title} [{spec.topic_id}]\n"
        f"循环 `{spec.id}`（影子试跑模式）本次捕获/跟进 **{len(due_items)}** 项待办，未执行外部写操作；确认无误可点下方按钮正式开启。\n"
        "---more---\n"
        f"- **循环 ID**：`{spec.id}` ｜ **当前水位**：`{AUTONOMY_LABEL.get(spec.autonomy, spec.autonomy)}` ｜ **调度**：闲时 `{spec.interval_minutes:g}m` / 有活 `{spec.active_interval_minutes:g}m`\n"
        f"### 本次捕获/跟进事项（{len(due_items)} 项）\n"
        + "\n".join(item_lines)
        + "\n\n"
        f"### 循环执行蓝图 (Plan)\n{plan_md}\n\n"
        f"### 阶段状态机 (Stages)\n`{' -> '.join(spec.stages)}`\n"
    )
    card = build_owner_card(text)
    card.setdefault("header", {"title": {"tag": "plain_text", "content": f"🐕 循环试跑预览 · {spec.title}"}})
    card["header"]["template"] = "wathet"

    buttons = [
        {
            "tag": "button",
            "element_id": "sd_loop_confirm",
            "text": {"tag": "plain_text", "content": "🚀 升为拍板模式 (confirm)"},
            "type": "primary",
            "size": "medium",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "sd_cb": "1",
                        "sd_action": "loop_promote",
                        "loop_id": spec.id,
                        "target_autonomy": AUTONOMY_CONFIRM,
                        "topic_id": spec.topic_id,
                        "run_id": run_id,
                        "label": "🚀 升为拍板模式 (confirm)",
                    },
                }
            ],
        },
        {
            "tag": "button",
            "element_id": "sd_loop_auto",
            "text": {"tag": "plain_text", "content": "⚡ 升为全自动 (auto)"},
            "type": "default",
            "size": "medium",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "sd_cb": "1",
                        "sd_action": "loop_promote",
                        "loop_id": spec.id,
                        "target_autonomy": AUTONOMY_AUTO,
                        "topic_id": spec.topic_id,
                        "run_id": run_id,
                        "label": "⚡ 升为全自动 (auto)",
                    },
                }
            ],
        },
        {
            "tag": "button",
            "element_id": "sd_loop_pause",
            "text": {"tag": "plain_text", "content": "⏸ 暂停此循环"},
            "type": "default",
            "size": "medium",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "sd_cb": "1",
                        "sd_action": "loop_pause",
                        "loop_id": spec.id,
                        "topic_id": spec.topic_id,
                        "run_id": run_id,
                        "label": "⏸ 暂停此循环",
                    },
                }
            ],
        },
    ]
    col_set = {
        "tag": "column_set",
        "flex_mode": "flow",
        "horizontal_spacing": "8px",
        "margin": "4px 0px 0px 0px",
        "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons],
    }
    new_card = json.loads(json.dumps(card))
    new_card.setdefault("body", {}).setdefault("elements", []).extend([{"tag": "hr"}, col_set])
    return new_card if card_fits(new_card) else card


def format_loop_execution_prompt(
    spec: LoopSpec,
    due_items: list[dict[str, Any]],
    *,
    run_id: str,
    trigger_reason: str,
) -> str:
    """组装注入给负责会话或羔羊的结构化业务 Loop 执行提示词。"""
    mem_text = ""
    if spec.memory_path and spec.memory_path.is_file():
        try:
            mem_text = spec.memory_path.read_text(encoding="utf-8").strip()
        except OSError:
            mem_text = ""

    autonomy_rule = {
        AUTONOMY_SHADOW: (
            "【当前水位：shadow（影子试跑）】只允许执行只读排查与方案推演，"
            "**严禁执行任何外部写操作或直接对同事发消息**；请将试跑结论整理汇报给主人。"
        ),
        AUTONOMY_CONFIRM: (
            "【当前水位：confirm（拍板执行）】你可以直接完成只读取证、本地写修复代码、跑单测以及 `acme-testing-env` 测试环境里验证；"
            "但在执行生产变更或对外正式发送前，必须用 `sheepdog ask --topic "
            f"{spec.topic_id} --key loop-{spec.id} ...` 递交证据与明确选项等主人拍板。"
        ),
        AUTONOMY_AUTO: (
            "【当前水位：auto（全自动闭环）】在严格遵守生产红线的前提下，直接端到端推进当前阶段动作，"
            "完成后更新事项阶段并用 `sheepdog report` 汇报。"
        ),
    }.get(spec.autonomy, "")

    lines = [
        f"🔄 **【Sheepdog 业务循环唤醒 · {spec.title} (`{spec.id}`)】**",
        f"- **运行批次**：`{run_id}` ｜ **触发原因**：`{trigger_reason}` ｜ **自治水位**：`{spec.autonomy}`",
        f"- **阶段流水线**：`{' -> '.join(spec.stages)}`",
        f"- **水位纪律**：{autonomy_rule}",
        "",
        f"### 待推进事项（共 {len(due_items)} 项）",
    ]
    for idx, it in enumerate(due_items[:15], 1):
        lines.append(
            f"{idx}. **`{it['item_key']}`** ｜ 当前阶段：`{it.get('stage') or 'detected'}` ｜ 标题：{it.get('title') or '-'}"
        )
        if it.get("payload"):
            lines.append(f"   - 数据负载：`{json.dumps(it['payload'], ensure_ascii=False)[:320]}`")
        if (it.get("context") or {}).get("last_note"):
            lines.append(f"   - 上轮备注：{(it['context'] or {}).get('last_note')}")

    lines.extend(
        [
            "",
            "### 状态机推进命令（处理完每项后必须调用以更新阶段，防下次重复报警）",
            f"- 推进到下一阶段：`sheepdog loop item {spec.id} --key <item_key> --stage <下一阶段> [--next-minutes <分钟>] --note \"进展说明\"`",
            f"- 彻底办结关单：`sheepdog loop item {spec.id} --key <item_key> --status closed --note \"结案说明\"`",
            f"- 标记长期豁免（自动写入 MEMORY.md）：`sheepdog loop item {spec.id} --key <item_key> --status exempted --note \"豁免原因\"`",
            "",
            f"### 循环执行规格 (`{spec.path}`)",
            (spec.body or "").strip(),
        ]
    )
    if mem_text:
        lines.extend(["", f"### 该循环专属经验与豁免账本 (`{spec.memory_path}`)", mem_text[:1500]])
    return "\n".join(lines)


# ======================= 单轮与批量调度执行器 =======================

def run_loop_tick(
    cfg: Config,
    store: Store,
    loop_id: str,
    *,
    dispatcher: Any = None,
    source: Any = None,
    force: bool = False,
    trigger_reason: str = "interval",
    injected_items: list[dict[str, Any]] | None = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """执行指定业务 Loop 的单次 Tick：
    1. 检查开关、暂停状态、活跃时段与闲忙动态变频（有 active 事项用 active_interval_minutes，无则用 interval_minutes）；
    2. 执行零 Token 前置 load_cmd（超时强杀），连续失败达阈值自动触发 autopause 熔断；
    3. 过滤已在 loop_items 中 closed/exempted 的幂等键；
    4. 若既无新事项、也无到期的在途 active 事项 -> 记 status='empty'（0 Token 消耗，不吵醒会话）；
    5. 若有待办事项 -> 按 shadow / confirm / auto 水位发预览卡或唤醒目标会话推进阶段状态机。
    """
    ensure_loop_tables(store)
    spec = get_loop_spec(cfg, store, loop_id)
    if spec is None:
        raise ValueError(f"循环 {loop_id!r} 不存在")
    st = get_loop_state(store, loop_id)
    now = now_dt or datetime.now(timezone.utc).astimezone()
    now_s = now.isoformat(timespec="seconds")

    if not force:
        if not spec.enabled or not st["enabled"]:
            return {"triggered": False, "loop_id": loop_id, "reason": "disabled"}
        if st["paused"]:
            return {"triggered": False, "loop_id": loop_id, "reason": "paused", "paused_reason": st["paused_reason"]}
        if spec.active_hours and now.hour not in parse_patrol_hours(spec.active_hours):
            return {"triggered": False, "loop_id": loop_id, "reason": "outside_active_hours"}

    active_items_before = list_loop_items(store, loop_id, status=ITEM_ACTIVE)
    effective_interval = (
        spec.active_interval_minutes if active_items_before else spec.interval_minutes
    )
    last_dt = _parse_iso(st.get("last_run_at"))
    if not force and last_dt is not None:
        elapsed_m = (now - last_dt).total_seconds() / 60.0
        if elapsed_m < effective_interval:
            return {
                "triggered": False,
                "loop_id": loop_id,
                "reason": "within_interval",
                "effective_interval_minutes": effective_interval,
                "elapsed_minutes": round(elapsed_m, 2),
            }

    run_id = f"lrun_{loop_id}_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    t0 = datetime.now(timezone.utc)

    # Step 1: 零 Token 前置取料 (load_cmd)
    loaded_candidates: list[dict[str, Any]] = []
    if injected_items is not None:
        loaded_candidates = parse_load_output(json.dumps(injected_items, ensure_ascii=False), spec.dedup_key)
    elif spec.load_cmd:
        rc, out, err = _run_load_command(
            spec.load_cmd,
            timeout_seconds=spec.load_timeout_seconds,
            cwd=spec.path.parent if spec.path else None,
        )
        if rc != 0:
            dur_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            fails = int(st.get("consecutive_failures") or 0) + 1
            paused = 1 if fails >= spec.autopause_after_failures else 0
            err_msg = (err or out or f"load_cmd exit {rc}").strip()[:300]
            pause_reason = f"连续失败 {fails} 次自动熔断: {err_msg}" if paused else ""
            store.conn.execute(
                """INSERT INTO loop_runs
                   (run_id, loop_id, trigger_reason, autonomy, status, items_count, active_items_count,
                    summary, error, duration_ms, started_at, finished_at)
                   VALUES (?, ?, ?, ?, 'failed', 0, ?, ?, ?, ?, ?, ?)""",
                (run_id, loop_id, trigger_reason, spec.autonomy, len(active_items_before), "前置取料失败", err_msg, dur_ms, now_s, now_s),
            )
            store.conn.execute(
                """UPDATE loop_state
                   SET consecutive_failures = ?, paused = ?, paused_reason = ?,
                       last_run_at = ?, last_status = ?, last_run_id = ?,
                       total_runs = total_runs + 1, updated_at = ?
                   WHERE loop_id = ?""",
                (fails, paused, pause_reason, now_s, "paused" if paused else "failed", run_id, now_s, loop_id),
            )
            store.conn.commit()
            return {
                "triggered": True,
                "loop_id": loop_id,
                "run_id": run_id,
                "status": "paused" if paused else "failed",
                "error": err_msg,
                "consecutive_failures": fails,
                "autopause_triggered": bool(paused),
            }
        loaded_candidates = parse_load_output(out, spec.dedup_key)

    # Step 2: 幂等去重与在途状态机对账
    mem_text = ""
    if spec.memory_path and spec.memory_path.is_file():
        try:
            mem_text = spec.memory_path.read_text(encoding="utf-8")
        except OSError:
            mem_text = ""

    existing_rows = {
        r["item_key"]: r
        for r in store.conn.execute("SELECT * FROM loop_items WHERE loop_id = ?", (loop_id,)).fetchall()
    }
    newly_added = 0
    for cand in loaded_candidates:
        ikey = cand["key"]
        if f"`{ikey}`" in mem_text and "豁免" in mem_text:
            continue
        ex = existing_rows.get(ikey)
        if ex is not None and ex["status"] in (ITEM_CLOSED, ITEM_EXEMPTED):
            continue
        if ex is None:
            update_loop_item(
                cfg,
                store,
                loop_id,
                ikey,
                title=cand["title"],
                stage=spec.stages[0] if spec.stages else "detected",
                status=ITEM_ACTIVE,
                payload=cand["payload"],
                now_dt=now,
            )
            newly_added += 1
        else:
            try:
                old_pl = json.loads(ex["payload_json"] or "{}")
            except Exception:
                old_pl = {}
            ext_changed = _has_external_payload_changed(old_pl, cand["payload"])
            update_loop_item(
                cfg,
                store,
                loop_id,
                ikey,
                title=cand["title"] or ex["title"],
                payload=cand["payload"],
                clear_next_check=ext_changed and bool(str(ex["next_check_at"] or "").strip() > now_s),
                now_dt=now,
            )

    all_active = list_loop_items(store, loop_id, status=ITEM_ACTIVE)
    raw_due_items = [
        it
        for it in all_active
        if not it.get("next_check_at") or str(it["next_check_at"]) <= now_s or force
    ]
    due_items: list[dict[str, Any]] = []
    for it in raw_due_items:
        pl = dict(it.get("payload") or {})
        d_cmd = str(pl.get("defer_check_cmd") or "").strip()
        if d_cmd and it.get("stage") == "awaiting_async_ready":
            d_rc, d_out, d_err = _run_load_command(
                d_cmd,
                timeout_seconds=spec.load_timeout_seconds,
                cwd=spec.path.parent if spec.path else None,
            )
            if d_rc == 0:
                pl["defer_check_ready_at"] = now_s
                pl["defer_check_output"] = (d_out or "").strip()[:300]
                it = update_loop_item(
                    cfg,
                    store,
                    loop_id,
                    it["item_key"],
                    stage="async_ready_to_cutover",
                    note="零 Token 异步前置条件已就绪，立即唤醒会话执行最终切换收尾",
                    payload=pl,
                )
                due_items.append(it)
            else:
                pl["last_defer_check_rc"] = d_rc
                pl["last_defer_check_at"] = now_s
                update_loop_item(
                    cfg,
                    store,
                    loop_id,
                    it["item_key"],
                    note=f"异步前置条件未就绪 (exit={d_rc})，零 Token 顺延等待",
                    next_minutes=spec.active_interval_minutes,
                    payload=pl,
                )
        else:
            due_items.append(it)
    all_active = list_loop_items(store, loop_id, status=ITEM_ACTIVE)

    # Step 3: 零 Token 空跑拦截（无 due_items 且非无脚本强制试跑时，0 Token 记 empty 结束）
    if not due_items and not (force and not spec.load_cmd):
        dur_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        store.conn.execute(
            """INSERT INTO loop_runs
               (run_id, loop_id, trigger_reason, autonomy, status, items_count, active_items_count,
                summary, duration_ms, started_at, finished_at)
               VALUES (?, ?, ?, ?, 'empty', 0, ?, '零 Token 探活：无新增或到期跟进事项', ?, ?, ?)""",
            (run_id, loop_id, trigger_reason, spec.autonomy, len(all_active), dur_ms, now_s, now_s),
        )
        store.conn.execute(
            """UPDATE loop_state
               SET consecutive_failures = 0, last_run_at = ?, last_status = 'empty', last_run_id = ?,
                   total_runs = total_runs + 1, empty_runs = empty_runs + 1, updated_at = ?
               WHERE loop_id = ?""",
            (now_s, run_id, now_s, loop_id),
        )
        store.conn.commit()
        return {
            "triggered": True,
            "loop_id": loop_id,
            "run_id": run_id,
            "status": "empty",
            "token_cost": 0,
            "newly_added": 0,
            "active_items_count": len(all_active),
        }

    # Step 4: 按自治水位触发 shadow 预览卡或向绑定会话投递
    dispatched_mid = ""
    owner_card_mid = ""
    run_status = "shadow_preview" if spec.autonomy == AUTONOMY_SHADOW else "dispatched"

    if spec.autonomy == AUTONOMY_SHADOW and source is not None and cfg.source.enabled:
        owner_chats = getattr(cfg.escalation, "chat_ids", None) or []
        target_chat = owner_chats[0] if owner_chats else (getattr(cfg.threads, "owner_chat_id", "") or "")
        if target_chat and hasattr(source, "send_to_chat"):
            try:
                card_payload = build_loop_shadow_card(spec, due_items, run_id=run_id)
                plain_text = f"🐕 汇报 · 循环试跑预览：{spec.title} [{spec.topic_id}]"
                idem_key = f"sd-loop-{run_id}"[:48]
                sent_res = source.send_to_chat(
                    target_chat,
                    plain_text,
                    idem_key,
                    "bot",
                    card=card_payload,
                )
                if isinstance(sent_res, dict):
                    owner_card_mid = str(sent_res.get("message_id") or "")
                elif isinstance(sent_res, str):
                    owner_card_mid = sent_res
                if owner_card_mid and hasattr(store, "add_bot_outbox"):
                    store.add_bot_outbox(
                        owner_card_mid,
                        spec.topic_id,
                        False,
                        plain_text,
                        now_s,
                    )
            except Exception as e:
                log.warning("发送循环试跑预览卡失败 (%s): %s", loop_id, e)

    prompt_text = format_loop_execution_prompt(
        spec,
        due_items,
        run_id=run_id,
        trigger_reason=trigger_reason,
    )
    source_topics = list(
        dict.fromkeys(
            str((it.get("payload") or {}).get("source_topic") or "").strip()
            for it in due_items
            if str((it.get("payload") or {}).get("source_topic") or "").strip()
        )
    )
    target_topics = source_topics if (source_topics and len(source_topics) == 1 and len(due_items) == sum(1 for it in due_items if (it.get("payload") or {}).get("source_topic"))) else [spec.topic_id, *[t for t in source_topics if t != spec.topic_id]]
    for target_tid in target_topics:
        if store.get_topic(target_tid) is None:
            store.create_topic(
                target_tid,
                spec.title,
                "adopted",
                spec.description or spec.title,
                sm.ACTIVE,
                host=ANTIGRAVITY,
            )
        mid_cur = store.add_system_message(
            target_tid,
            "loop_tick",
            prompt_text,
            tags=[f"loop:{spec.id}", f"loop_run:{run_id}", f"autonomy:{spec.autonomy}"],
        )
        if not dispatched_mid:
            dispatched_mid = mid_cur
    if dispatcher is not None:
        try:
            dispatcher.dispatch_once()
        except Exception as e:
            log.warning("派发循环消息异常 (%s): %s", loop_id, e)

    wait_defer_s = (now + timedelta(minutes=max(float(spec.interval_minutes), 60.0))).isoformat(timespec="seconds")
    for it in due_items:
        ikey = str(it.get("item_key") or "").strip()
        if not ikey:
            continue
        cur_next = str(it.get("next_check_at") or "").strip()
        if not force and _is_waiting_stage(it.get("stage")) and (not cur_next or cur_next <= now_s):
            store.conn.execute(
                "UPDATE loop_items SET last_run_id = ?, next_check_at = ? WHERE loop_id = ? AND item_key = ?",
                (run_id, wait_defer_s, loop_id, ikey),
            )
        else:
            store.conn.execute(
                "UPDATE loop_items SET last_run_id = ? WHERE loop_id = ? AND item_key = ?",
                (run_id, loop_id, ikey),
            )

    dur_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    summary_str = (
        f"新增 {newly_added} 项，到期跟进 {len(due_items)} 项（水位={spec.autonomy}）"
    )
    store.conn.execute(
        """INSERT INTO loop_runs
           (run_id, loop_id, trigger_reason, autonomy, status, items_count, active_items_count,
            summary, message_id, duration_ms, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            loop_id,
            trigger_reason,
            spec.autonomy,
            run_status,
            len(due_items),
            len(all_active),
            summary_str,
            owner_card_mid or dispatched_mid,
            dur_ms,
            now_s,
            now_s,
        ),
    )
    store.conn.execute(
        """UPDATE loop_state
           SET consecutive_failures = 0, last_run_at = ?, last_status = ?, last_run_id = ?,
               total_runs = total_runs + 1, action_runs = action_runs + 1, updated_at = ?
           WHERE loop_id = ?""",
        (now_s, run_status, run_id, now_s, loop_id),
    )
    store.conn.commit()
    return {
        "triggered": True,
        "loop_id": loop_id,
        "run_id": run_id,
        "autonomy": spec.autonomy,
        "status": run_status,
        "topic_id": target_topics[0] if target_topics else spec.topic_id,
        "target_topics": target_topics,
        "newly_added": newly_added,
        "due_items_count": len(due_items),
        "active_items_count": len(all_active),
        "message_id": owner_card_mid or dispatched_mid,
    }


def run_due_loops_tick(
    cfg: Config,
    store: Store,
    dispatcher: Any = None,
    source: Any = None,
    *,
    now_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """常驻守护进程主循环每轮调用：扫描全部启用且未暂停的业务 Loop，按闲忙动态变频执行。"""
    ensure_loop_tables(store)
    results: list[dict[str, Any]] = []
    for spec in discover_loops(cfg, store):
        try:
            res = run_loop_tick(
                cfg,
                store,
                spec.id,
                dispatcher=dispatcher,
                source=source,
                force=False,
                trigger_reason="daemon_tick",
                now_dt=now_dt,
            )
            if res.get("triggered"):
                results.append(res)
        except Exception as e:
            log.warning("业务循环 %s 执行异常（已隔离，不中断主循环）: %s", spec.id, e)
    return results


def list_loop_runs(store: Store, loop_id: str = "", limit: int = 30) -> list[dict[str, Any]]:
    ensure_loop_tables(store)
    if loop_id:
        rows = store.conn.execute(
            "SELECT * FROM loop_runs WHERE loop_id = ? ORDER BY started_at DESC LIMIT ?",
            (loop_id, limit),
        ).fetchall()
    else:
        rows = store.conn.execute(
            "SELECT * FROM loop_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def build_loops_overview(cfg: Config, store: Store) -> dict[str, Any]:
    """构建全量业务 Loop 概览（供 CLI、:8788 管理工作台与 sheepdog-evolve 读取）。"""
    ensure_loop_tables(store)
    specs = discover_loops(cfg, store)
    loops_out: list[dict[str, Any]] = []
    total_empty = 0
    total_action = 0
    paused_loops = 0
    for spec in specs:
        st = get_loop_state(store, spec.id)
        active_items = list_loop_items(store, spec.id, status=ITEM_ACTIVE, limit=50)
        closed_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM loop_items WHERE loop_id = ? AND status = 'closed'",
            (spec.id,),
        ).fetchone()["n"]
        exempt_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM loop_items WHERE loop_id = ? AND status = 'exempted'",
            (spec.id,),
        ).fetchone()["n"]
        recent_runs = list_loop_runs(store, spec.id, limit=8)
        total_empty += int(st.get("empty_runs") or 0)
        total_action += int(st.get("action_runs") or 0)
        if st.get("paused"):
            paused_loops += 1
        loops_out.append(
            {
                **spec.to_dict(),
                "state": st,
                "effective_interval_minutes": (
                    spec.active_interval_minutes if active_items else spec.interval_minutes
                ),
                "active_items": active_items,
                "active_items_count": len(active_items),
                "closed_items_count": int(closed_count or 0),
                "exempted_items_count": int(exempt_count or 0),
                "recent_runs": recent_runs,
            }
        )
    return {
        "loops_dir": str(cfg.loops_dir),
        "total_loops": len(loops_out),
        "paused_loops": paused_loops,
        "total_empty_runs": total_empty,
        "total_action_runs": total_action,
        "loops": loops_out,
        "recommendations": recommend_loops(cfg, store),
    }


# ======================= 智能诊断：哪些现有工作应该升级成 Loop =======================

def recommend_loops(cfg: Config, store: Store) -> list[dict[str, Any]]:
    """扫描当前名册、活跃会话、周期巡检与事项台账，识别最值得升格为声明式 Loop 的工作流。"""
    existing_ids = {s.id for s in discover_loops(cfg, store)}
    recs: list[dict[str, Any]] = [
        {
            "id": "release-pilot-template",
            "template": "release-pilot-template",
            "priority": "P0",
            "topic_id": "tp_release",
            "title": "Lark 生产发版审批单零 Token 探活与全流程引航",
            "current_pain": (
                "目前 roster.toml 里 tp_release 配置了 patrol_every_minutes = N，"
                "无论线上有没有新审批单，每隔几分钟都会盲目唤醒一次大模型会话（多次未回执），空烧 Token 且冲乱上下文。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：平时用零 Token 前置探活检查有无新审批单或未完结发版项（没单直接记 empty 0 消耗）；"
                "有单时按 6 阶段状态机（intake -> 等发起人 -> 触发构建 -> 3分钟高频盯构建 -> Cloud Run 100%切流回读 -> 办结）闭环推进。"
            ),
            "installed": "release-pilot-template" in existing_ids,
            "init_cmd": "sheepdog loop init release-pilot-template --template release-pilot-template",
        },
        {
            "id": "spend-steward",
            "template": "spend-steward",
            "priority": "P0",
            "topic_id": "tp_cost-ops",
            "title": "全云资源利用率巡检与降本治理主理人（GCP / 腾讯云 / 阿里云 / CF）",
            "current_pain": (
                "目前多云资源盘点（T-9094）挂在 tp_cost-ops 会话里靠人工追问触发，"
                "缺乏跨周期的『发现低利用率 -> 推进降配 -> 盯盘 24h 验证没报错且账单真降 -> 沉淀保底豁免进 MEMORY.md』持续控制回路。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：一直替你操心四朵云的闲置实例与账单突增；你说过『保底别动』的资源自动记入 MEMORY.md 永不重报；"
                "降配执行后自动切 30 分钟高频观察 24 小时，确认稳定且省钱才结案。"
            ),
            "installed": "spend-steward" in existing_ids,
            "init_cmd": "sheepdog loop init spend-steward --template spend-steward",
        },
        {
            "id": "service-release-guardian",
            "template": "service-release-guardian",
            "priority": "P0",
            "topic_id": "tp_svc-b-latency-check",
            "title": "核心服务端上线盯盘、自动排障修代码、测服验证与发版闭环守护",
            "current_pain": (
                "目前像 svc-alpha-decision-service、svc-catalog 或 acmeco-agent 上线后出现 P1 告警/5xx 时，"
                "要临时新开排查专席（如 tp_svc-b-latency-check），排障、改代码、测服验证和申请生产发版割裂在多处。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：发版后自动切 5 分钟高频盯盘；一旦抓到线上新报错，自动拉最近 Commit Diff 写好修复代码、"
                "跑通单测并部署 acme-testing-env 测服验证，直接把『根因 + PR + 测服通过回执 + 生产授权口令』递到你飞书拍板。"
            ),
            "installed": "service-release-guardian" in existing_ids,
            "init_cmd": "sheepdog loop init service-release-guardian --template service-release-guardian",
        },
        {
            "id": "vps-node-and-whitelist-steward",
            "template": "vps-node-and-whitelist-steward",
            "priority": "P1",
            "topic_id": "tp_net-ops",
            "title": "多台 proxy-panel 专线节点健康度、优选 恢复通知与防火墙白名单自动对齐",
            "current_pain": (
                "目前新购 VPS（如 node7）或海外优选 链路波动时，靠同学私聊反馈才被动触发 tp_net-ops 和 tp_auth 补齐 acme-testing-env 防火墙白名单。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：零 Token 探测 多台节点连通率与出口 IP，发现测试环境里 allow-demo-office 缺白名单直接幂等补齐回读；"
                "优选 链路恢复后自动通知登记等待的同学（如 Basil Cui）。"
            ),
            "installed": "vps-node-and-whitelist-steward" in existing_ids,
            "init_cmd": "sheepdog loop init vps-node-and-whitelist-steward --template vps-node-and-whitelist-steward",
        },
        {
            "id": "ssl-cert-lifecycle-steward",
            "template": "ssl-cert-lifecycle-steward",
            "priority": "P1",
            "topic_id": "tp_cert-ops",
            "title": "多云 SSL/TLS 证书到期预警、自动续期与 ELB 挂载核验管家",
            "current_pain": (
                "目前 tp_cert-ops（T-9026）依赖被动提醒才去盘点 GCP / 腾讯云 / 阿里云证书有效期与老 ELB 证书退役。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：每天零 Token 扫描全网域名 TLS 握手到期天数（>=21 天 0 Token 静默）；"
                "临期自动备妥续期方案并核验多地域握手生效。"
            ),
            "installed": "ssl-cert-lifecycle-steward" in existing_ids,
            "init_cmd": "sheepdog loop init ssl-cert-lifecycle-steward --template ssl-cert-lifecycle-steward",
        },
        {
            "id": "env-migration-daily-steward",
            "template": "env-migration-daily-steward",
            "priority": "P1",
            "topic_id": "tp_migrate-ops",
            "title": "一批工作负载腾讯云迁移交割阻塞追踪与每日定时 进度播报",
            "current_pain": (
                "目前 tp_migrate-ops 负责跟踪 一批工作负载迁移与每晚定时 播报，靠会话自己记时间或人工催促。"
            ),
            "loop_upgrade_benefit": (
                "升级为 Loop 后：按 blocker_detected -> coordinating -> verifying_dryrun -> ready_for_cutover 阶段持续追踪阻塞卡点，"
                "每晚准点汇总增量进展供你拍板播报。"
            ),
            "installed": "env-migration-daily-steward" in existing_ids,
            "init_cmd": "sheepdog loop init env-migration-daily-steward --template env-migration-daily-steward",
        },
    ]
    return recs
