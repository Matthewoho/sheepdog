"""会话记录的增量读：认出宿主压缩点（7.19 K2）、会话跑过的命令（7.19 K3）和会话的动作（7.21 自动恢复）。

这里只放机制：各宿主记录格式的解析、按字节偏移增量读、命令文本的整理与脱敏。
命中以后发什么话、发给谁由 engine 组装，措辞在 playbook。

各宿主的记录格式（每行一个 JSON）：
- Antigravity（2026-10-11 核过）：
  - 压缩点 `{"type": "CHECKPOINT", "content": "# Resuming from a compaction\\n...", "step_index": ..., "created_at": ...}`
  - 命令 `{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "<JSON 编码的字符串>"}}]}`
- Codex rollout（2026-10-11 核过）：
  - 压缩点 `{"type": "response_item", "payload": {"type": "compaction"}}`；06 月的老文件是顶层 `{"type": "compacted"}`
  - 命令 `{"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": [...]}}}`
  - 代码模式下模型写一段 JS 调 `tools.exec_command({cmd: "..."})`，记成
    `{"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "<JS>"}}`；
    有的线程只有这一条、没有 CommandExecution（10-10 的线程里见过），所以两种都认
- Claude（**没核实过**，有就认）：压缩点 `"subtype": "compact_boundary"`；命令是 assistant 消息里
  `{"type": "tool_use", "name": "Bash", "input": {"command": "..."}}`

会话的动作（7.21：attention 之后又有动作 = 会话恢复了）：
- Antigravity：`PLANNER_RESPONSE`（模型的回答，工具调用在它的 tool_calls 里），以及 `{"type": "GENERIC", "source": "MODEL"}`
  （工具调用的执行行）；时间看 created_at（UTC，Z 结尾）。断网时只会写 `ERROR_MESSAGE`（source 是 SYSTEM），不算
  （2026-10-12 对着 10-11 晚上卡住的总线记录核过）
- Codex：`{"type": "event_msg", "payload": {"type": "item_completed"}}`，时间看顶层 timestamp
- Claude：记录格式没核实过，不认，只认晚到的回执

在岗表（7.20）卡片上的动作（在做时叫「此刻动作」，其他状态叫「最近动作」）只取工具调用的动作说明，不读模型的思考和回答正文：
- Antigravity：`PLANNER_RESPONSE` 里 `tool_calls[].args.toolSummary`，没有就 `toolAction`（都是 JSON 编码过的字符串，
  先解一次），再没有就是工具名；主人亲自说话是 `USER_INPUT`（第 0 步是开场提示词，不算）
- Codex：跑的命令（同命令审计）
- Claude：记录格式没核实过，不认

主人亲自说话（卡片上的 human_at，投递要不要暂停也只看它）：Antigravity 在这里认 `USER_INPUT`；命令行宿主在这里只认出
「来了一条用户消息」（user_turn），是不是主人说的交给宿主自己的读法（7.18 第 7 条：去掉第一条初始提示词和带牧羊犬
批次头的）：
- Codex：`{"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage"}}}`，
  老格式 `{"type": "event_msg", "payload": {"type": "user_message"}}`
- Claude：`{"type": "user"}` 且不是工具结果（content 里没有 tool_result），isMeta / isSidechain 的不算
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .config import ANTIGRAVITY

# transcript_cursors.purpose
COMPACTION = "compaction"
AUDIT = "audit"
ACTIVITY = "activity"
DUTY = "duty"
LAMB_DISCOVERY = "lamb_discovery"

AG_COMPACTION_PREFIX = "# Resuming from a compaction"

# 先按字节粗筛再 json.loads：记录里大多是很长的工具输出，没必要逐行解析
_NEEDLES = {
    COMPACTION: (b"CHECKPOINT", b"compaction", b"compacted", b"compact_boundary"),
    AUDIT: (b"run_command", b"CommandExecution", b"custom_tool_call", b"tool_use"),
    ACTIVITY: (b"PLANNER_RESPONSE", b"GENERIC", b"item_completed"),
    DUTY: (b"PLANNER_RESPONSE", b"GENERIC", b"item_completed", b"custom_tool_call", b"USER_INPUT", b"CHECKPOINT",
           b"compaction", b"compacted", b"compact_boundary", b"user_message", b'"type":"user"', b'"type": "user"'),
    LAMB_DISCOVERY: (
        b"invoke_subagent",
        b"Created the following subagent",
        b"SYSTEM_MESSAGE",
        b"[Message]",
        b"manage_subagents",
    ),
}
# 找最后一次动作时只读记录末尾这么多字节（记录里常有很长的工具输出，整份读太慢）
LAST_ACTIVITY_TAIL = 4 * 1024 * 1024


@dataclass
class Command:
    step: str      # 去重用：同一会话里唯一
    text: str      # 整理过（续行合并、空白压成一个空格），没脱敏
    at: str        # 记录里的时间（ISO）；没有就空


def scan_jsonl(path: Path, offset: int, purpose: str, handle: Callable[[int, dict], None]) -> int:
    """从 offset 起读完整的行（最后一行没写完就留到下一轮），粗筛后解析成 dict 交给 handle(行首偏移, d)。

    返回读到的新偏移。文件读不了时原样返回 offset。
    """
    needles = _NEEDLES[purpose]
    pos = offset
    try:
        with path.open("rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break
                start, pos = pos, pos + len(line)
                if not any(n in line for n in needles):
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(d, dict):
                    handle(start, d)
    except OSError:
        return offset
    return pos


# ---------- 压缩点 ----------
def is_compaction(host: str, d: dict) -> bool:
    if host == ANTIGRAVITY:
        return d.get("type") == "CHECKPOINT" and str(d.get("content") or "").lstrip().startswith(AG_COMPACTION_PREFIX)
    if host == "codex":
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        return (d.get("type") == "response_item" and pl.get("type") == "compaction") or d.get("type") == "compacted"
    if host == "claude":
        return d.get("subtype") == "compact_boundary"  # 没核实过
    return False


# ---------- 会话的动作（7.21） ----------
def parse_ts(ts: str) -> datetime | None:
    """记录里的时间（多为 UTC 的 Z 结尾）转成带时区的 datetime；认不出或不带时区就是 None。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def activity_at(host: str, d: dict) -> datetime | None:
    """这一行是会话的动作就返回它的时间，否则 None。"""
    if host == ANTIGRAVITY:
        kind = d.get("type")
        if kind == "PLANNER_RESPONSE" or (kind == "GENERIC" and d.get("source") == "MODEL"):
            return parse_ts(str(d.get("created_at") or ""))
        return None
    if host == "codex":
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") == "event_msg" and pl.get("type") == "item_completed":
            return parse_ts(str(d.get("timestamp") or ""))
    return None  # Claude 的记录格式没核实过，不认


def last_activity_at(host: str, path: Path, tail: int = LAST_ACTIVITY_TAIL) -> datetime | None:
    """记录末尾 tail 字节里最后一次动作的时间；读不了、没有就是 None。"""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    start = max(0, size - tail)
    found: list[datetime] = []

    def handle(_off: int, d: dict) -> None:
        if (at := activity_at(host, d)) is not None:
            found.append(at)

    if start:
        # 从中间开始读：先跳到下一个完整行的行首
        try:
            with path.open("rb") as f:
                f.seek(start)
                start += len(f.readline())
        except OSError:
            return None
    scan_jsonl(path, start, ACTIVITY, handle)
    return max(found) if found else None


# ---------- 命令 ----------
_CONTINUATION = re.compile(r"\\\r?\n")
_SPACES = re.compile(r"\s+")
# JS 里 exec_command({cmd: "..."}) 的命令字符串（双引号，内部转义同 JSON）
_JS_CMD = re.compile(r"""\bcmd\s*:\s*"((?:[^"\\]|\\.)*)\"""", re.S)


def normalize(text: str) -> str:
    """续行（行尾反斜杠 + 换行）合并，所有空白压成一个空格。"""
    return _SPACES.sub(" ", _CONTINUATION.sub(" ", text or "")).strip()


def decode_command_line(raw) -> str:
    """Antigravity 的 CommandLine 是 JSON 编码过的字符串（外面多一层引号），先解一次；解不开就按原样。"""
    if not isinstance(raw, str):
        return ""
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return v if isinstance(v, str) else raw


def _argv_text(cmd) -> str:
    """Codex 的 command 是列表：`[shell, -lc, 脚本]` 取脚本，其他按空格拼。"""
    if isinstance(cmd, str):
        return cmd
    if not isinstance(cmd, list):
        return ""
    parts = [str(x) for x in cmd]
    if len(parts) >= 3 and parts[1] in ("-c", "-lc", "-ic"):
        return parts[2]
    return " ".join(parts)


def _js_commands(source: str) -> list[str]:
    out = []
    for m in _JS_CMD.finditer(source or ""):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except json.JSONDecodeError:
            out.append(m.group(1))
    return out


def commands_in(host: str, d: dict, line_offset: int) -> list[Command]:
    """一行记录里会话跑的命令。"""
    out: list[Command] = []
    if host == ANTIGRAVITY:
        if d.get("type") != "PLANNER_RESPONSE":
            return out
        step = d.get("step_index")
        base = str(step) if step is not None else f"@{line_offset}"
        for i, tc in enumerate(d.get("tool_calls") or []):
            if isinstance(tc, dict) and tc.get("name") == "run_command":
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                text = normalize(decode_command_line(args.get("CommandLine")))
                if text:
                    out.append(Command(f"{base}:{i}", text, str(d.get("created_at") or "")))
        return out
    if host == "codex":
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        ts = str(d.get("timestamp") or "")
        if d.get("type") == "event_msg" and pl.get("type") == "item_completed":
            item = pl.get("item") if isinstance(pl.get("item"), dict) else {}
            if item.get("type") == "CommandExecution":
                text = normalize(_argv_text(item.get("command")))
                if text:
                    out.append(Command(str(item.get("id") or f"@{line_offset}"), text, ts))
        elif d.get("type") == "response_item" and pl.get("type") == "custom_tool_call" and pl.get("name") == "exec":
            src = str(pl.get("input") or "")
            base = str(pl.get("call_id") or pl.get("id") or f"@{line_offset}")
            # cmd: "..." 写明的命令逐条认；其余部分（命令放在数组、变量里的写法）整段留着也去匹配，免得漏
            cmds = [(str(i), c) for i, c in enumerate(_js_commands(src))]
            cmds.append(("rest", _JS_CMD.sub('cmd: ""', src) if cmds else src))
            for i, c in cmds:
                text = normalize(c)
                if text:
                    out.append(Command(f"js:{base}:{i}", text, ts))
        return out
    if host == "claude":  # 没核实过
        if d.get("type") != "assistant":
            return out
        content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        for i, c in enumerate(content if isinstance(content, list) else []):
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                text = normalize(str(inp.get("command") or ""))
                if text:
                    out.append(Command(str(c.get("id") or f"@{line_offset}:{i}"), text, str(d.get("timestamp") or "")))
    return out


def first_match(text: str, patterns: Iterable[re.Pattern]) -> str:
    for p in patterns:
        if p.search(text):
            return p.pattern
    return ""


# ---------- 脱敏 ----------
_VALUE = r"""('[^']*'|"[^"]*"|\S+)"""
_REDACT = [
    # --data / --data-raw / --data-binary / --data-file ... 后面的值
    re.compile(r"(--data[\w-]*(?:=|\s+))" + _VALUE),
    # token= / password= / secret= / client_secret: ... 后面的值
    re.compile(r"((?:token|password|passwd|secret)[\w-]*\s*[=:]\s*)" + _VALUE, re.I),
    # --secret xxx / --client-secret xxx
    re.compile(r"(--[\w-]*secret[\w-]*\s+)" + _VALUE, re.I),
    re.compile(r"(\bbearer\s+)" + _VALUE, re.I),
]
MASK = "***"


def redact(text: str, limit: int = 200) -> str:
    """隐去疑似密钥的值，再截到 limit 字（先隐去再截，截断不会露出半截值）。"""
    for rx in _REDACT:
        text = rx.sub(lambda m: m.group(1) + MASK, text)
    return text if len(text) <= limit else text[:limit] + "…"


def local_hhmm(ts: str) -> str:
    """记录里的时间（多为 UTC 的 Z 结尾）换成本机时区的 HH:MM；认不出就原样。"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:
        return dt.strftime("%H:%M")
    return dt.astimezone().strftime("%H:%M")


# ---------- 在岗表卡片上的动作（7.20） ----------
@dataclass
class ToolAction:
    sig: str       # 空转判定用：同一个工具、同一条命令 / 同一句说明算同一个调用
    summary: str   # 给人看的动作说明（没截断、没脱敏）
    at: str        # 记录里的时间（ISO）；没有就空


def _decoded(raw) -> str:
    return decode_command_line(raw).strip() if isinstance(raw, str) else ""


def tool_actions(host: str, d: dict, line_offset: int) -> list[ToolAction]:
    """一行记录里的工具调用。只看动作说明和命令，不碰 thinking / content。"""
    out: list[ToolAction] = []
    if host == ANTIGRAVITY:
        if d.get("type") != "PLANNER_RESPONSE":
            return out
        at = str(d.get("created_at") or "")
        for tc in d.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "")
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            summary = _decoded(args.get("toolSummary")) or _decoded(args.get("toolAction")) or name
            cmd = normalize(_decoded(args.get("CommandLine"))) if name == "run_command" else ""
            if summary:
                out.append(ToolAction(f"{name}:{cmd or summary}", summary, at))
        return out
    if host == "codex":
        cmds = [c for c in commands_in(host, d, line_offset) if not c.step.endswith(":rest")]
        return [ToolAction(c.text, c.text, c.at) for c in cmds]
    return out


def human_input_at(host: str, d: dict) -> datetime | None:
    """主人在会话里亲自说话（Antigravity 的 USER_INPUT，第 0 步的开场提示词不算）；其他宿主不在这里认，返回 None
    （命令行宿主见 user_turn）。"""
    if host == ANTIGRAVITY and d.get("type") == "USER_INPUT" and d.get("step_index") not in (0, None):
        return parse_ts(str(d.get("created_at") or ""))
    return None


def user_turn(host: str, d: dict) -> bool:
    """命令行宿主（Codex / Claude）的这一行是不是一条用户消息：可能是主人说的，也可能是牧羊犬发的批次、初始提示词。
    在岗表只拿它当「该用宿主的读法重新认一遍主人说话了」的信号，不在这里判是不是主人。"""
    if host == "codex":
        if d.get("type") != "event_msg":
            return False
        pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if pl.get("type") == "user_message":
            return True
        item = pl.get("item") if isinstance(pl.get("item"), dict) else {}
        return pl.get("type") == "item_completed" and item.get("type") == "UserMessage"
    if host == "claude":
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
            return False
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        return not (isinstance(content, list)
                    and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content))
    return False


def record_time(host: str, d: dict) -> datetime | None:
    """这一行记录的时间（Antigravity 看 created_at，Codex 看 timestamp）。"""
    return parse_ts(str(d.get("created_at") or d.get("timestamp") or ""))


_SENTENCE_END = re.compile(r"[。！？!?\n]|\.(\s|$)")


def one_sentence(text: str, limit: int = 80) -> str:
    """截成一句：到第一个句号 / 问号 / 换行为止，再截到 limit 字。"""
    text = (text or "").strip()
    m = _SENTENCE_END.search(text)
    if m and m.start() > 0:
        text = text[:m.start()]
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# ---------- 自动发现 invoke_subagent（7.63） ----------
_UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)
_CREATED_SUBAGENT_HDR_RE = re.compile(r"Created the following subagents?:\s*\n")
_CREATED_SUBAGENT_CID_RE = re.compile(
    r'"conversationId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    re.I,
)
_TRUNCATED_SUBAGENT_ROLE_RE = re.compile(r'(?:\\?"Role\\?"|\\?"TaskTitle\\?")\s*:\s*\\?"([^"\\]+)')
_SUBAGENT_RETURN_BLOCK_RE = re.compile(
    r"<SYSTEM_MESSAGE>\s*\[Message\]\s+timestamp=([^\s]+)\s+"
    r"sender=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\s)"
    r"[^\n]*?\bcontent=(.*?)(?:</SYSTEM_MESSAGE>|\Z)",
    re.I | re.S,
)
_SUBAGENT_RETURN_DIRECT_RE = re.compile(
    r"\A\s*\[Message\]\s+timestamp=([^\s]+)\s+"
    r"sender=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\s)"
    r"[^\n]*?\bcontent=(.*?)\Z",
    re.I | re.S,
)


@dataclass
class SubagentInvocation:
    lamb_id: str
    conversation_id: str
    task_title: str
    step: str
    at: str


@dataclass
class SubagentReturn:
    conversation_id: str
    summary: str
    at: str


def _pick_str(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return _decoded(v) or v.strip()
    return ""


def _parse_subagents_arg(raw) -> list[dict]:
    val = raw
    for _ in range(2):
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return []
            try:
                val = json.loads(s)
            except json.JSONDecodeError:
                roles = [m.group(1).strip() for m in _TRUNCATED_SUBAGENT_ROLE_RE.finditer(s) if m.group(1).strip()]
                if roles:
                    return [{"Role": r} for r in roles]
                return []
    if isinstance(val, list):
        return [x for x in val if isinstance(x, dict)]
    return []


def _extract_subagent_fields(obj: dict, parent: dict, step_fallback: str) -> SubagentInvocation:
    args = obj.get("args") if isinstance(obj.get("args"), dict) else (obj.get("input") if isinstance(obj.get("input"), dict) else {})
    res = obj.get("result") if isinstance(obj.get("result"), dict) else (parent.get("result") if isinstance(parent.get("result"), dict) else {})

    lid = _pick_str(
        args.get("lamb_id"), args.get("LambId"),
        obj.get("lamb_id"), res.get("lamb_id"),
        parent.get("lamb_id"),
    )
    cid = _pick_str(
        args.get("conversation_id"), args.get("ConversationId"), args.get("cid"),
        args.get("subagent_id"), args.get("SubagentId"),
        obj.get("conversation_id"), obj.get("subagent_id"), obj.get("cid"),
        res.get("conversation_id"), res.get("subagent_id"), res.get("cid"),
        parent.get("subagent_conversation_id"), parent.get("subagent_id"),
    )
    if not cid:
        raw_blob = json.dumps({"res": obj.get("result"), "p_res": parent.get("result"), "content": obj.get("content") or parent.get("content")}, ensure_ascii=False)
        m = _UUID_RE.search(raw_blob)
        if m:
            cid = m.group(1)
    title = one_sentence(
        _pick_str(
            args.get("Role"), args.get("role"),
            args.get("task_title"), args.get("TaskTitle"), args.get("title"), args.get("Title"),
            args.get("toolSummary"), args.get("Task"), args.get("task"),
            args.get("Prompt"), args.get("prompt"), args.get("Name"), args.get("name"),
            args.get("toolAction"), obj.get("Role"), obj.get("role"), obj.get("task_title"), obj.get("title"),
            res.get("task_title"), res.get("title"), parent.get("task_title"), parent.get("title"),
        ),
        80,
    ) or "subagent"
    at = str(parent.get("created_at") or parent.get("timestamp") or obj.get("created_at") or "")
    return SubagentInvocation(lamb_id=lid, conversation_id=cid, task_title=title, step=step_fallback, at=at)


def _extract_subagent_invocations(obj: dict, parent: dict, step_fallback: str) -> list[SubagentInvocation]:
    args = obj.get("args") if isinstance(obj.get("args"), dict) else (obj.get("input") if isinstance(obj.get("input"), dict) else {})
    raw_subs = args.get("Subagents") if "Subagents" in args else (args.get("subagents") if "subagents" in args else obj.get("Subagents"))
    subs = _parse_subagents_arg(raw_subs)
    if not subs:
        return [_extract_subagent_fields(obj, parent, step_fallback)]
    at = str(parent.get("created_at") or parent.get("timestamp") or obj.get("created_at") or "")
    out: list[SubagentInvocation] = []
    for j, sub in enumerate(subs):
        lid = _pick_str(
            sub.get("lamb_id"), sub.get("LambId"),
            *(args.get("lamb_id"), args.get("LambId")) if len(subs) == 1 else (),
        )
        cid = _pick_str(
            sub.get("conversation_id"), sub.get("ConversationId"), sub.get("cid"),
            sub.get("subagent_id"), sub.get("SubagentId"),
        )
        title = one_sentence(
            _pick_str(
                sub.get("Role"), sub.get("role"),
                sub.get("TaskTitle"), sub.get("task_title"), sub.get("Title"), sub.get("title"),
                args.get("toolSummary"),
                sub.get("Prompt"), sub.get("prompt"),
                sub.get("TypeName"), sub.get("type_name"),
                args.get("toolAction"),
            ),
            80,
        ) or "subagent"
        out.append(SubagentInvocation(lamb_id=lid, conversation_id=cid, task_title=title, step=f"{step_fallback}:{j}", at=at))
    return out


def subagent_invocations_in(host: str, d: dict, line_offset: int) -> list[SubagentInvocation]:
    """从一行会话记录里提取 invoke_subagent 调用（7.63 自动发现）。"""
    out: list[SubagentInvocation] = []
    step = d.get("step_index")
    base = str(step) if step is not None else f"@{line_offset}"
    tcs = d.get("tool_calls")
    if isinstance(tcs, list):
        for i, tc in enumerate(tcs):
            if isinstance(tc, dict) and tc.get("name") == "invoke_subagent":
                out.extend(_extract_subagent_invocations(tc, d, f"{base}:{i}"))
    if out:
        return out
    if d.get("name") == "invoke_subagent" or d.get("tool_name") == "invoke_subagent" or d.get("tool") == "invoke_subagent" or d.get("type") == "invoke_subagent":
        out.extend(_extract_subagent_invocations(d, d, base))
        return out
    pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    if pl.get("name") == "invoke_subagent" or pl.get("tool_name") == "invoke_subagent":
        out.extend(_extract_subagent_invocations(pl, d, base))
        return out
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = msg.get("content") if isinstance(msg.get("content"), list) else (d.get("content") if isinstance(d.get("content"), list) else [])
    for i, item in enumerate(content):
        if isinstance(item, dict) and item.get("name") == "invoke_subagent":
            out.extend(_extract_subagent_invocations(item, d, f"{base}:{i}"))
    return out


def subagent_created_cids_in(host: str, d: dict) -> list[str]:
    """从 Antigravity GENERIC 'Created the following subagent(s):' 行提取创建出的子代理 conversationId 列表。"""
    if d.get("type") != "GENERIC":
        return []
    raw_content = d.get("content")
    text = _decoded(raw_content) if isinstance(raw_content, str) else ""
    if not text and isinstance(raw_content, str):
        text = raw_content
    if "Command exited with code" in text:
        return []
    m_hdr = _CREATED_SUBAGENT_HDR_RE.search(text)
    if not m_hdr:
        return []
    after = text[m_hdr.end():]
    cids = [m.group(1).strip() for m in _CREATED_SUBAGENT_CID_RE.finditer(after) if m.group(1).strip()]
    return list(dict.fromkeys(cids))


def subagent_returns_in(host: str, d: dict) -> list[SubagentReturn]:
    """从 Antigravity SYSTEM_MESSAGE '[Message] ... sender=<cid> ... content=...' 行提取子代理回传通知。"""
    if d.get("type") != "SYSTEM_MESSAGE":
        return []
    raw_content = d.get("content")
    text = _decoded(raw_content) if isinstance(raw_content, str) else ""
    if not text and isinstance(raw_content, str):
        text = raw_content
    if "[Message]" not in text:
        return []
    line_at = str(d.get("created_at") or d.get("timestamp") or "")
    matches = list(_SUBAGENT_RETURN_BLOCK_RE.finditer(text))
    if not matches:
        matches = list(_SUBAGENT_RETURN_DIRECT_RE.finditer(text))
    out: list[SubagentReturn] = []
    for m in matches:
        ts_raw = m.group(1).strip()
        sender = m.group(2).strip()
        raw_body = m.group(3).strip()
        at = line_at or ts_raw
        summary = one_sentence(raw_body, 120) or "subagent returned"
        out.append(SubagentReturn(conversation_id=sender, summary=summary, at=at))
    return out


def subagent_kills_in(host: str, d: dict) -> tuple[bool, list[str]]:
    """从 Antigravity PLANNER_RESPONSE 的 manage_subagents 工具调用提取 (kill_all, killed_cids)。"""
    if d.get("type") != "PLANNER_RESPONSE":
        return False, []
    kill_all = False
    cids: list[str] = []
    for tc in d.get("tool_calls") or []:
        if not isinstance(tc, dict) or tc.get("name") != "manage_subagents":
            continue
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        action = _pick_str(args.get("Action"), args.get("action")).lower()
        if action == "kill_all":
            kill_all = True
        elif action == "kill":
            raw_cids = args.get("ConversationIds") if "ConversationIds" in args else args.get("conversation_ids")
            if isinstance(raw_cids, list):
                raw_text = json.dumps(raw_cids, ensure_ascii=False)
            else:
                raw_text = str(raw_cids or "")
            cids.extend(m.group(1) for m in _UUID_RE.finditer(raw_text))
    if kill_all and not cids:
        return True, []
    return kill_all, list(dict.fromkeys(cids))


