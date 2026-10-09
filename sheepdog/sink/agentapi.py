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
from datetime import datetime
from pathlib import Path

from . import SinkError

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
        proc = subprocess.run([exe, "agentapi", *args], capture_output=True, text=True, timeout=60)
        # language_server 会往 stderr 打启动日志，只解析 stdout 里的 JSON
        out = proc.stdout.strip()
        start = out.find("{")
        if proc.returncode != 0 or start < 0:
            raise SinkError(f"agentapi 失败 rc={proc.returncode}: {(proc.stderr or out)[-400:]}")
        try:
            return json.loads(out[start:])
        except json.JSONDecodeError as e:
            raise SinkError(f"agentapi 输出无法解析: {out[:300]}") from e

    def new_conversation(self, title: str, prompt: str, model: str = "") -> str:
        args = ["new-conversation"]
        if model:
            args.append(f"--model={model}")
        args += [f"--title={title}", "--", prompt]
        data = self._call(args)
        cid = ((data.get("response") or {}).get("newConversation") or {}).get("conversationId")
        if not cid:
            raise SinkError(f"agentapi 未返回 conversationId: {data}")
        return cid

    def send_message(self, conversation_id: str, content: str) -> None:
        data = self._call(["send-message", conversation_id, content])
        if not (data.get("response") or {}).get("sendMessage"):
            raise SinkError(f"agentapi send-message 异常: {data}")

    def last_human_activity(self, conversation_id: str) -> datetime | None:
        """读 session transcript，返回人类最后一次在 App 里发言（USER_INPUT）的时间。

        agentapi 投递的消息在 transcript 里是 SYSTEM_MESSAGE，人类输入是 USER_INPUT，以此区分。
        """
        p = transcript_path(conversation_id)
        if not p.exists():
            return None
        last: datetime | None = None
        with p.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 第 0 步是 new-conversation 的初始 prompt，不算人类介入
                if i == 0 or d.get("type") != "USER_INPUT":
                    continue
                ts = d.get("created_at")
                if ts:
                    try:
                        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        pass
        return last


class DryRunSink:
    """不真正投递，只打印，用于调试规则。"""

    name = "dryrun"

    def __init__(self):
        self._n = 0

    def available(self) -> tuple[bool, str]:
        return True, "dry-run"

    def new_conversation(self, title: str, prompt: str, model: str = "") -> str:
        self._n += 1
        cid = f"dryrun-{self._n}"
        print(f"[dryrun] new-conversation title={title!r} -> {cid}\n{prompt}\n")
        return cid

    def send_message(self, conversation_id: str, content: str) -> None:
        print(f"[dryrun] send-message -> {conversation_id}\n{content}\n")

    def last_human_activity(self, conversation_id: str) -> datetime | None:
        return None
