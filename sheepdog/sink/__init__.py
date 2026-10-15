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


class SinkTimeout(SinkError):
    """调用宿主超时（7.47 第 7 条）：agentapi send-message 超时时消息可能其实已经送到了。有「正在发」记录的投递路径
    （普通批次、插话批次）遇到它不判失败：批次留在 sending，下一轮由 _recover_sending 到会话记录里核对；其他调用按普通
    SinkError 处理。"""


class Sink(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", *,
                         topic_id: str = "", batch_id: str = "") -> str: ...
    def send_message(self, conversation_id: str, content: str, *, batch_id: str = "", topic_id: str = "",
                     model: str = "", effort: str = "") -> None: ...
    # 主人在会话里说没说话不问 sink：engine 只看在岗表卡片上的 human_at（每轮扫会话记录写的），卡片和投递是同一个判断


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
    # 主人最后一次在会话里说话（7.18 第 7 条的读法）：只给在岗表写卡片上的 human_at 用
    def human_message_at(self, conversation_id: str) -> datetime | None: ...
    # 会话被别处占着（最近一轮撞上写锁；Claude 还有交互式 claude --resume 在跑）：engine 视同主人正在用
    def occupied_at(self, conversation_id: str) -> datetime | None: ...
