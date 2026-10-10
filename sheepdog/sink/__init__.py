"""Sink 接口：把信号投递到 AI Agent 会话。

每个会话有一个宿主（7.18）：antigravity 走 App 的 agentapi（同步投递、会话自己交回执）；
codex / claude 走各自的命令行，一轮在后台跑完，由 engine 每轮调 collect_finished() 收尾。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class SinkError(RuntimeError):
    pass


class Sink(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", *,
                         topic_id: str = "", batch_id: str = "") -> str: ...
    def send_message(self, conversation_id: str, content: str, *, batch_id: str = "", topic_id: str = "",
                     model: str = "", effort: str = "") -> None: ...
    def last_human_activity(self, conversation_id: str) -> datetime | None: ...


@dataclass
class RunResult:
    """命令行宿主一轮的结局（7.18 第 4–6 条），由 collect_finished() 交给 engine。"""
    batch_id: str
    topic_id: str
    host: str
    conversation_id: str
    # done | failed | timeout | locked | retry_wait（容量满，等重试）
    state: str
    # "" | capacity | locked | auth | other | timeout
    error_kind: str = ""
    error: str = ""
    answer: str = ""
    # 宿主拒绝的工具 / 审批请求名
    denials: list[str] = field(default_factory=list)


class BackgroundSink(Sink, Protocol):
    """命令行宿主（Codex / Claude）多出来的接口。"""

    host: str

    def busy(self, conversation_id: str) -> str: ...
    def run_active(self, batch_id: str) -> bool: ...
    def collect_finished(self) -> list[RunResult]: ...
    def transcript_contains(self, conversation_id: str, text: str) -> bool: ...
    def history(self, conversation_id: str, limit: int = 6) -> list[tuple[str, str, str]]: ...
    def resume_hint(self, conversation_id: str, title: str = "") -> str: ...
    def unavailable_reason(self) -> str: ...
    def record_files(self, conversation_id: str) -> list: ...
    def lock_streak_since(self, conversation_id: str) -> datetime | None: ...
