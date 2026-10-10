"""会话记录的增量读：认出宿主压缩点（7.19 K2）和会话跑过的命令（7.19 K3）。

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

AG_COMPACTION_PREFIX = "# Resuming from a compaction"

# 先按字节粗筛再 json.loads：记录里大多是很长的工具输出，没必要逐行解析
_NEEDLES = {
    COMPACTION: (b"CHECKPOINT", b"compaction", b"compacted", b"compact_boundary"),
    AUDIT: (b"run_command", b"CommandExecution", b"custom_tool_call", b"tool_use"),
}


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
