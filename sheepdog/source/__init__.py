"""Source 接口与 Lark 实现（基于 lark-cli，user 身份）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Protocol

from ..models import Mention, Message


class SourceError(RuntimeError):
    pass


class Source(Protocol):
    def fetch_since(self, start_iso: str, end_iso: str | None = None) -> list[Message]: ...
    def muted_chat_ids(self) -> set[str]: ...
    def read_status(self, message_ids: list[str]) -> dict[str, bool]: ...
    def senders_of(self, message_ids: list[str]) -> dict[str, str]: ...


class ImWriter(Protocol):
    """对 IM 唯一的写操作：加 / 撤确认表情（7.9）。identity：user = 主人身份，bot = 机器人身份。"""

    def add_reaction(self, message_id: str, emoji_type: str, identity: str = "user") -> str: ...
    def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...
