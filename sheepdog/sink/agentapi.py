"""Antigravity App 投递：通过 App 内置的 agentapi 新建 / 续投 session。

依赖 App 注入的环境变量（App 托管的 sidecar 与 App 内终端都会自动带上）：
  ANTIGRAVITY_AGENTAPI_EXE、ANTIGRAVITY_LS_ADDRESS、ANTIGRAVITY_CSRF_TOKEN

agentapi 命令：
  agentapi new-conversation [--model=...] [--title=...] -- <prompt>
  agentapi send-message <conversation_id> <content>      （异步投递，不返回回答）
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import SinkError, SinkTimeout

# agentapi 一次调用最多等多少秒
AGENTAPI_TIMEOUT = 60

_REQUIRED_ENV = ("ANTIGRAVITY_AGENTAPI_EXE", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")


def _app_data_dir() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or Path.home() / ".gemini" / "antigravity")


def conversation_dir(conversation_id: str) -> Path:
    """会话目录：transcript 和会话产出的文档都在这里。"""
    return _app_data_dir() / "brain" / conversation_id


def transcript_path(conversation_id: str) -> Path:
    return conversation_dir(conversation_id) / ".system_generated" / "logs" / "transcript.jsonl"


def transcript_contains(conversation_id: str, text: str) -> bool:
    """会话 transcript 里有没有出现过 text（用于发送中断后核对批次是否已送达，7.16）。"""
    p = transcript_path(conversation_id)
    if not text or not p.exists():
        return False
    with p.open(encoding="utf-8", errors="replace") as f:
        return any(text in line for line in f)


class AgentApiSink:
    name = "agentapi"
    host = "antigravity"

    def available(self) -> tuple[bool, str]:
        missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            return False, f"缺少环境变量 {missing}：需在 Antigravity App 托管的 sidecar 或 App 内终端中运行"
        return True, "ok"

    def _call(self, args: list[str]) -> dict:
        ok, why = self.available()
        if not ok:
            raise SinkError(why)
        exe = os.environ["ANTIGRAVITY_AGENTAPI_EXE"]
        try:
            proc = subprocess.run([exe, "agentapi", *args], capture_output=True, text=True, timeout=AGENTAPI_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            # 7.47 第 7 条：不再冲出 dispatch_once 中断整轮。send-message 超时可能其实送到了，由调用方按 SinkTimeout 处理
            raise SinkTimeout(f"agentapi {args[0] if args else ''} 超过 {AGENTAPI_TIMEOUT} 秒没返回（可能已经送到）") from e
        # language_server 会往 stderr 打启动日志，只解析 stdout 里的 JSON
        out = proc.stdout.strip()
        start = out.find("{")
        if proc.returncode != 0 or start < 0:
            raise SinkError(f"agentapi 失败 rc={proc.returncode}: {(proc.stderr or out)[-400:]}")
        try:
            return json.loads(out[start:])
        except json.JSONDecodeError as e:
            raise SinkError(f"agentapi 输出无法解析: {out[:300]}") from e

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", **_ctx) -> str:
        # agentapi 没有推理档位参数，effort 忽略
        args = ["new-conversation"]
        if model:
            args.append(f"--model={model}")
        args += [f"--title={title}", "--", prompt]
        data = self._call(args)
        cid = ((data.get("response") or {}).get("newConversation") or {}).get("conversationId")
        if not cid:
            raise SinkError(f"agentapi 未返回 conversationId: {data}")
        return cid

    def send_message(self, conversation_id: str, content: str, **_ctx) -> None:
        data = self._call(["send-message", conversation_id, content])
        if not (data.get("response") or {}).get("sendMessage"):
            raise SinkError(f"agentapi send-message 异常: {data}")

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        return transcript_contains(conversation_id, text)

    def history(self, conversation_id: str, limit: int = 6) -> list[tuple[str, str, str]]:
        """transcript 里最近几条 USER_INPUT / PLANNER_RESPONSE：(角色, 正文, 时间)。"""
        p = transcript_path(conversation_id)
        if not p.exists():
            return []
        out: list[tuple[str, str, str]] = []
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = d.get("type") if isinstance(d, dict) else None
                if kind not in ("USER_INPUT", "PLANNER_RESPONSE"):
                    continue
                text = d.get("content")
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
                out.append(("问" if kind == "USER_INPUT" else "答", text, d.get("created_at") or ""))
        return out[-limit:]

    def resume_hint(self, conversation_id: str, title: str = "") -> str:
        return f"在 Antigravity App 里打开会话 {conversation_id}"


class DryRunSink:
    """不真正投递，只打印，用于调试规则。每个宿主一个，打印时带上宿主名（7.18）。"""

    name = "dryrun"

    def __init__(self, host: str = "antigravity"):
        self.host = host
        self._n = 0

    @property
    def _tag(self) -> str:
        return "[dryrun]" if self.host == "antigravity" else f"[dryrun:{self.host}]"

    def available(self) -> tuple[bool, str]:
        return True, "dry-run"

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", **_ctx) -> str:
        self._n += 1
        cid = f"dryrun-{self._n}" if self.host == "antigravity" else f"dryrun-{self.host}-{self._n}"
        extra = "".join(f" {k}={v}" for k, v in (("model", model), ("effort", effort)) if v)
        print(f"{self._tag} new-conversation title={title!r}{extra} -> {cid}\n{prompt}\n")
        return cid

    def send_message(self, conversation_id: str, content: str, **_ctx) -> None:
        extra = "".join(f" {k}={_ctx[k]}" for k in ("model", "effort") if _ctx.get(k))
        print(f"{self._tag} send-message -> {conversation_id}{extra}\n{content}\n")

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        return False
