"""名册：把现有的 Agent 会话登记给 sheepdog（P0.5）。

名册是个人配置（默认 ~/.config/sheepdog/roster.toml），不进仓库；仓库只有 examples/roster.example.toml。
两种会话：
- managed：sheepdog 按聊天归属往里推消息（topic kind = adopted），永远不新建会话；
- known：只登记给总线看，从不投递。

校验失败抛 RosterError，调用方直接报错退出，不静默降级。
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANAGED = "managed"
KNOWN = "known"

_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
_SESSION_KEYS = {"key", "mode", "conversation_id", "title", "duty", "authority",
                 "self_polling", "retire_self_polling", "chats"}
_CHAT_KEYS = {"chat_id", "name", "all_messages"}


class RosterError(ValueError):
    pass


@dataclass
class RosterChat:
    chat_id: str
    name: str = ""
    # true：该聊天里除自己发的以外全部推给本会话；false：只推 dispatch 级消息
    all_messages: bool = False


@dataclass
class RosterSession:
    key: str
    mode: str
    conversation_id: str = ""
    # 现有标题，仅展示；sheepdog 不改标题
    title: str = ""
    duty: str = ""
    # 主人在该会话里给过的授权原话：只转述，不扩大
    authority: str = ""
    self_polling: str = ""
    retire_self_polling: bool = False
    chats: list[RosterChat] = field(default_factory=list)

    @property
    def topic_id(self) -> str:
        return "tp_" + self.key

    @property
    def kind(self) -> str:
        return "adopted" if self.mode == MANAGED else "known"

    @property
    def display_title(self) -> str:
        return self.title or self.key


@dataclass
class Roster:
    sessions: list[RosterSession] = field(default_factory=list)

    def __post_init__(self):
        # chat_id → (会话, 聊天)，只收 managed 会话
        self._owner: dict[str, tuple[RosterSession, RosterChat]] = {}
        for s in self.sessions:
            if s.mode == MANAGED:
                for c in s.chats:
                    self._owner[c.chat_id] = (s, c)

    def owner_of(self, chat_id: str) -> tuple[RosterSession, RosterChat] | None:
        return self._owner.get(chat_id)

    def by_topic(self, topic_id: str) -> RosterSession | None:
        return next((s for s in self.sessions if s.topic_id == topic_id), None)

    @property
    def managed(self) -> list[RosterSession]:
        return [s for s in self.sessions if s.mode == MANAGED]

    def content_hash(self) -> str:
        """按解析后的内容算 hash：改注释、空白不算名册变化。"""
        blob = json.dumps([asdict(s) for s in self.sessions], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str(d: dict, k: str, where: str) -> str:
    v = d.get(k, "")
    if not isinstance(v, str):
        raise RosterError(f"{where}: {k} 必须是字符串")
    return v.strip()


def _bool(d: dict, k: str, where: str) -> bool:
    v = d.get(k, False)
    if not isinstance(v, bool):
        raise RosterError(f"{where}: {k} 必须是 true/false")
    return v


def parse_roster(data: dict) -> Roster:
    """校验并解析名册 dict（tomllib 的输出）。"""
    unknown_top = set(data) - {"session"}
    if unknown_top:
        raise RosterError(f"名册顶层有未知键 {sorted(unknown_top)}（只允许 [[session]]）")
    raw_sessions = data.get("session", [])
    if not isinstance(raw_sessions, list):
        raise RosterError("session 必须写成 [[session]] 数组")

    sessions: list[RosterSession] = []
    keys: set[str] = set()
    chat_owner: dict[str, str] = {}
    for i, raw in enumerate(raw_sessions):
        where = f"session[{i}]"
        unknown = set(raw) - _SESSION_KEYS
        if unknown:
            raise RosterError(f"{where}: 未知键 {sorted(unknown)}（防止拼错的字段被静默忽略）")
        key = _str(raw, "key", where)
        where = f"session[{i}] key={key!r}"
        if not _KEY_RE.match(key):
            raise RosterError(f"{where}: key 只允许 [a-z0-9_-] 且不能为空")
        if key == "bus":
            raise RosterError(f"{where}: key 不能是 bus（tp_bus 是总线保留）")
        if key in keys:
            raise RosterError(f"{where}: key 重复")
        keys.add(key)
        mode = _str(raw, "mode", where)
        if mode not in (MANAGED, KNOWN):
            raise RosterError(f"{where}: mode 必须是 managed 或 known，收到 {mode!r}")
        s = RosterSession(
            key=key, mode=mode,
            conversation_id=_str(raw, "conversation_id", where),
            title=_str(raw, "title", where),
            duty=_str(raw, "duty", where),
            authority=_str(raw, "authority", where),
            self_polling=_str(raw, "self_polling", where),
            retire_self_polling=_bool(raw, "retire_self_polling", where),
        )
        if mode == MANAGED and not s.conversation_id:
            raise RosterError(f"{where}: managed 会话必须有 conversation_id")
        raw_chats = raw.get("chats", [])
        if not isinstance(raw_chats, list):
            raise RosterError(f"{where}: chats 必须写成 [[session.chats]] 数组")
        if mode == KNOWN and raw_chats:
            raise RosterError(f"{where}: known 会话不能有 chats（known 从不投递）")
        for j, rc in enumerate(raw_chats):
            cw = f"{where} chats[{j}]"
            unknown = set(rc) - _CHAT_KEYS
            if unknown:
                raise RosterError(f"{cw}: 未知键 {sorted(unknown)}")
            chat = RosterChat(chat_id=_str(rc, "chat_id", cw), name=_str(rc, "name", cw),
                              all_messages=_bool(rc, "all_messages", cw))
            if not chat.chat_id:
                raise RosterError(f"{cw}: chat_id 不能为空")
            if chat.chat_id in chat_owner:
                raise RosterError(f"{cw}: chat_id {chat.chat_id} 已属于 {chat_owner[chat.chat_id]}，"
                                  "同一个聊天只能属于一个 managed 会话")
            chat_owner[chat.chat_id] = key
            s.chats.append(chat)
        sessions.append(s)
    return Roster(sessions)


def load_roster(path: Path, required: bool) -> Roster:
    """读名册。文件不存在时：显式配置了 roster_path 就报错，用默认路径则视为空名册（P0 行为）。"""
    if not path.exists():
        if required:
            raise RosterError(f"名册文件不存在: {path}")
        return Roster()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RosterError(f"名册 TOML 解析失败 {path}: {e}") from e
    try:
        return parse_roster(data)
    except RosterError as e:
        raise RosterError(f"{path}: {e}") from e
