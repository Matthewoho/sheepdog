"""Sink 接口：把信号投递到 AI Agent session。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class SinkError(RuntimeError):
    pass


class Sink(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def new_conversation(self, title: str, prompt: str, model: str = "") -> str: ...
    def send_message(self, conversation_id: str, content: str) -> None: ...
    def last_human_activity(self, conversation_id: str) -> datetime | None: ...
