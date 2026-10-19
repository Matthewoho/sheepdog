"""名册：把现有的 Agent 会话登记给 sheepdog（P0.5）。

名册是个人配置（默认 ~/.config/sheepdog/roster.toml），不进仓库；仓库只有 examples/roster.example.toml。
两种会话：
- managed：sheepdog 按聊天归属往里推消息（topic kind = adopted），永远不新建会话；
- known：只登记给总线看，从不投递。

managed 的 conversation_id 写成空字符串 = 还没建，由 `sheepdog spawn` / `init` 新建（7.2）；
新建出来的 id 只记在 topics 表，不回写名册文件。可以带 predecessor_conversation_id 接手旧会话。

顶层还可以有 [[person]]（7.61）：名册里的「人」。tier = vip 的人发来的消息投递时盖章、批次末尾附 vip.md，
他们的请求 `sheepdog ask` 要带 --floor。授权跟着人走，不改路由。

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
# 会话宿主（7.18），和 config.HOSTS 一致；默认 antigravity
HOSTS = ("antigravity", "codex", "claude")
DEFAULT_HOST = "antigravity"

_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
_SESSION_KEYS = {"key", "mode", "conversation_id", "title", "duty", "authority",
                 "self_polling", "retire_self_polling", "predecessor_conversation_id", "retire_predecessor", "chats",
                 "host", "model", "effort", "patrol_every_minutes", "patrol_hours", "patrol_prompt"}
_CHAT_KEYS = {"chat_id", "name", "all_messages", "thread_id"}
# 名册里的「人」（7.61）：授权跟着人走，不跟着会话走。目前只有 vip 一档
_PERSON_KEYS = {"open_id", "name", "tier", "authority"}
VIP = "vip"
PERSON_TIERS = (VIP,)
# authority 里要看得出主人哪天说的：至少有一个 MM-DD（YYYY-MM-DD 里也含一个）
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")


class RosterError(ValueError):
    pass


def parse_patrol_hours(spec: str) -> frozenset[int]:
    """解析巡检时段（7.57），例如 '9-23' 或 '0-2,9-18'。空串返回 0..23 全集。非法格式抛 RosterError。"""
    s = (spec or "").strip()
    if not s:
        return frozenset(range(24))
    hours: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            raise RosterError(f"patrol_hours 格式无效: {spec!r}")
        if "-" in part:
             pieces = part.split("-")
             if len(pieces) != 2 or not pieces[0].strip().isdigit() or not pieces[1].strip().isdigit():
                 raise RosterError(f"patrol_hours 区间格式无效: {part!r}（应如 9-23）")
             lo, hi = int(pieces[0].strip()), int(pieces[1].strip())
             if not (0 <= lo <= 23 and 0 <= hi <= 23 and lo <= hi):
                 raise RosterError(f"patrol_hours 区间必须在 0-23 且起<=止: {part!r}")
             hours.update(range(lo, hi + 1))
        else:
             if not part.isdigit():
                 raise RosterError(f"patrol_hours 小时必须是 0-23 整数: {part!r}")
             h = int(part)
             if not (0 <= h <= 23):
                 raise RosterError(f"patrol_hours 小时必须在 0-23: {part!r}")
             hours.add(h)
    return frozenset(hours)


@dataclass
class RosterChat:
    chat_id: str
    name: str = ""
    # true：该聊天里除自己发的以外全部推给本会话；false：只推 dispatch 级消息
    all_messages: bool = False
    # 群话题（7.45，思路来自 PR #1 @Jerry030-1，issue #2）：非空 = 只负责这个聊天里的这个话题（omt_…），话题里的回复一律送本会话，
    # 覆盖自动归属、不过期；空 = 负责整个聊天
    thread_id: str = ""


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
    # 有值 = 接手这个旧会话（7.2）；retire_predecessor = true 时先给旧会话发退休通知
    predecessor_conversation_id: str = ""
    retire_predecessor: bool = False
    chats: list[RosterChat] = field(default_factory=list)
    # 会话宿主（7.18）：antigravity | codex | claude；model / effort 留空 = 这个宿主的默认值
    host: str = DEFAULT_HOST
    model: str = ""
    effort: str = ""
    # 定时巡检（7.57）：0 = 关；>0 = 每隔这么多分钟在空闲且无待推信号时投一条例行巡检消息
    patrol_every_minutes: int = 0
    patrol_hours: str = ""
    patrol_prompt: str = ""

    @property
    def is_cli_host(self) -> bool:
        return self.host != DEFAULT_HOST

    @property
    def to_spawn(self) -> bool:
        """名册里 conversation_id 留空的 managed 条目：由 sheepdog 新建会话。"""
        return self.mode == MANAGED and not self.conversation_id

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
class RosterPerson:
    """名册里的一个人（7.61）。open_id 是飞书 ou_…；tier 目前只有 vip；authority 是主人原话（带日期），只转述，不扩大。
    和 config 的 routing.vip_sender_ids 不是一回事：那个只管「关键人发言进会话」（路由），这里管「对他放多大权」（授权）。"""
    open_id: str
    tier: str = VIP
    name: str = ""
    authority: str = ""


@dataclass
class Roster:
    sessions: list[RosterSession] = field(default_factory=list)
    persons: list[RosterPerson] = field(default_factory=list)

    def __post_init__(self):
        # (chat_id, thread_id) → (会话, 聊天)，只收 managed 会话；thread_id 为空 = 整个聊天
        self._owner: dict[tuple[str, str], tuple[RosterSession, RosterChat]] = {}
        for s in self.sessions:
            if s.mode == MANAGED:
                for c in s.chats:
                    self._owner[(c.chat_id, c.thread_id)] = (s, c)
        self._persons: dict[str, RosterPerson] = {p.open_id: p for p in self.persons}

    def owner_of(self, chat_id: str) -> tuple[RosterSession, RosterChat] | None:
        """整个聊天归谁（只看不带话题编号的条目；话题绑定见 thread_owner）。"""
        return self._owner.get((chat_id, ""))

    def thread_owner(self, chat_id: str, thread_id: str) -> tuple[RosterSession, RosterChat] | None:
        """群话题的显式绑定（7.45，PR #1 的格式）：只看 (chat_id, thread_id)，不退回整个聊天的归属。"""
        return self._owner.get((chat_id, thread_id)) if thread_id else None

    def by_key(self, key: str) -> RosterSession | None:
        return next((s for s in self.sessions if s.key == key), None)

    def by_topic(self, topic_id: str) -> RosterSession | None:
        return next((s for s in self.sessions if s.topic_id == topic_id), None)

    def person(self, open_id: str) -> RosterPerson | None:
        """按 open_id 查名册里的人（7.61）；不在名册里是 None。"""
        return self._persons.get((open_id or "").strip())

    def vips(self, exclude: str = "") -> dict[str, str]:
        """名册里 tier = vip 的人：open_id → 名字（没写名字是空串）。exclude：排除这个 open_id（主人自己不算 VIP）。"""
        return {p.open_id: p.name for p in self.persons if p.tier == VIP and p.open_id != exclude}

    @property
    def managed(self) -> list[RosterSession]:
        return [s for s in self.sessions if s.mode == MANAGED]

    def content_hash(self) -> str:
        """按解析后的内容算 hash：改注释、空白不算名册变化。"""
        rows = []
        for s in self.sessions:
            d = asdict(s)
            # 7.18 新增的宿主字段、7.57 新增的巡检字段取默认值时不进 hash：老名册升级后不算「名册更新」
            for k, default in (("host", DEFAULT_HOST), ("model", ""), ("effort", ""),
                               ("patrol_every_minutes", 0), ("patrol_hours", ""), ("patrol_prompt", "")):
                if d.get(k) == default:
                    d.pop(k, None)
            for c in d.get("chats") or []:
                # 7.45 新增的话题绑定为空时同样不进 hash：老名册升级后不算「名册更新」
                if not c.get("thread_id"):
                    c.pop("thread_id", None)
            rows.append(d)
        # 7.61 的 [[person]]：没有时 hash 和以前一字不差（老名册升级后不算「名册更新」）；有时一起算
        data = rows if not self.persons else {"session": rows, "person": [asdict(p) for p in self.persons]}
        blob = json.dumps(data, ensure_ascii=False, sort_keys=True)
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
    unknown_top = set(data) - {"session", "person"}
    if unknown_top:
        raise RosterError(f"名册顶层有未知键 {sorted(unknown_top)}（只允许 [[session]] 和 [[person]]）")
    raw_sessions = data.get("session", [])
    if not isinstance(raw_sessions, list):
        raise RosterError("session 必须写成 [[session]] 数组")

    sessions: list[RosterSession] = []
    keys: set[str] = set()
    # (chat_id, thread_id) → key：同一个聊天（或同一个话题）只能属于一个 managed 会话
    chat_owner: dict[tuple[str, str], str] = {}
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
        patrol_every = raw.get("patrol_every_minutes", 0)
        if isinstance(patrol_every, bool) or not isinstance(patrol_every, int) or patrol_every < 0:
            raise RosterError(f"{where}: patrol_every_minutes 必须是不小于 0 的整数（0 = 关）")
        patrol_hours = _str(raw, "patrol_hours", where)
        patrol_prompt = _str(raw, "patrol_prompt", where)
        if patrol_every > 0 and mode != MANAGED:
            raise RosterError(f"{where}: 只有 mode = managed 的会话才能设 patrol_every_minutes")
        if patrol_every == 0 and (patrol_hours or patrol_prompt):
            raise RosterError(f"{where}: 设了 patrol_hours / patrol_prompt 必须同时设 patrol_every_minutes > 0")
        if patrol_hours:
            try:
                parse_patrol_hours(patrol_hours)
            except RosterError as e:
                raise RosterError(f"{where}: {e}") from e
        s = RosterSession(
            key=key, mode=mode,
            conversation_id=_str(raw, "conversation_id", where),
            title=_str(raw, "title", where),
            duty=_str(raw, "duty", where),
            authority=_str(raw, "authority", where),
            self_polling=_str(raw, "self_polling", where),
            retire_self_polling=_bool(raw, "retire_self_polling", where),
            predecessor_conversation_id=_str(raw, "predecessor_conversation_id", where),
            retire_predecessor=_bool(raw, "retire_predecessor", where),
            host=_str(raw, "host", where) or DEFAULT_HOST,
            model=_str(raw, "model", where),
            effort=_str(raw, "effort", where),
            patrol_every_minutes=patrol_every,
            patrol_hours=patrol_hours,
            patrol_prompt=patrol_prompt,
        )
        if s.host not in HOSTS:
            raise RosterError(f"{where}: host 只能是 {' / '.join(HOSTS)}，收到 {s.host!r}")
        # 必须显式写出 conversation_id：漏写是笔误，报错；写成 "" 才表示「由 sheepdog 新建」
        if mode == MANAGED and "conversation_id" not in raw:
            raise RosterError(f"{where}: managed 会话必须有 conversation_id（要 sheepdog 新建就写 conversation_id = \"\"）")
        if mode == KNOWN and (s.predecessor_conversation_id or s.retire_predecessor):
            raise RosterError(f"{where}: known 会话不能有 predecessor_conversation_id / retire_predecessor")
        if s.retire_predecessor and not s.predecessor_conversation_id:
            raise RosterError(f"{where}: retire_predecessor = true 需要 predecessor_conversation_id")
        if s.predecessor_conversation_id and s.conversation_id:
            raise RosterError(f"{where}: 接手旧会话时 conversation_id 必须留空（由 sheepdog 新建接手会话）")
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
                              all_messages=_bool(rc, "all_messages", cw), thread_id=_str(rc, "thread_id", cw))
            if not chat.chat_id:
                raise RosterError(f"{cw}: chat_id 不能为空")
            if "thread_id" in rc and not chat.thread_id:
                raise RosterError(f"{cw}: thread_id 不能为空；负责整个聊天就不要写这个字段")
            if chat.thread_id and not chat.thread_id.startswith("omt_"):
                raise RosterError(f"{cw}: thread_id 要写 omt_ 开头的话题编号，收到 {chat.thread_id!r}")
            scope = (chat.chat_id, chat.thread_id)
            if scope in chat_owner:
                what = f"话题 {chat.thread_id}（聊天 {chat.chat_id}）" if chat.thread_id else f"chat_id {chat.chat_id}"
                raise RosterError(f"{cw}: {what} 已属于 {chat_owner[scope]}，"
                                  "同一个聊天（或同一个话题）只能属于一个 managed 会话")
            chat_owner[scope] = key
            s.chats.append(chat)
        sessions.append(s)
    # 前任不能同时还是名册里某个会话，否则既要退休又要投递
    live_cids = {x.conversation_id: x.key for x in sessions if x.conversation_id}
    for x in sessions:
        if x.predecessor_conversation_id in live_cids:
            raise RosterError(f"session key={x.key!r}: predecessor_conversation_id 仍登记在 "
                              f"{live_cids[x.predecessor_conversation_id]!r}，先把旧条目删掉")
    return Roster(sessions, _parse_persons(data.get("person", [])))


def _parse_persons(raw_persons) -> list[RosterPerson]:
    """名册顶层的 [[person]]（7.61）：open_id 必填（ou_ 开头、不重复）；tier 目前只认 vip；vip 必须写 authority（主人原话，
    要看得出哪天说的：至少有一个 MM-DD 或 YYYY-MM-DD）。"""
    if not isinstance(raw_persons, list):
        raise RosterError("person 必须写成 [[person]] 数组")
    persons: list[RosterPerson] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(raw_persons):
        where = f"person[{i}]"
        if not isinstance(raw, dict):
            raise RosterError(f"{where}: 必须写成 [[person]] 表")
        unknown = set(raw) - _PERSON_KEYS
        if unknown:
            raise RosterError(f"{where}: 未知键 {sorted(unknown)}（只允许 {sorted(_PERSON_KEYS)}，防止拼错的字段被静默忽略）")
        open_id = _str(raw, "open_id", where)
        where = f"person[{i}] open_id={open_id!r}"
        if not open_id.startswith("ou_") or len(open_id) <= 3:
            raise RosterError(f"{where}: open_id 必填，要写 ou_ 开头的飞书 open_id")
        if open_id in seen:
            raise RosterError(f"{where}: open_id 重复（和 person[{seen[open_id]}] 是同一个人）")
        seen[open_id] = i
        tier = _str(raw, "tier", where)
        if tier not in PERSON_TIERS:
            raise RosterError(f"{where}: tier 只能是 {' / '.join(PERSON_TIERS)}，收到 {tier!r}")
        p = RosterPerson(open_id=open_id, tier=tier, name=_str(raw, "name", where), authority=_str(raw, "authority", where))
        if tier == VIP:
            if not p.authority:
                raise RosterError(f"{where}: tier = vip 必须写 authority（主人原话，带日期）")
            if not _has_date(p.authority):
                raise RosterError(f"{where}: authority 要看得出主人哪天说的，写上日期（如 2026-10-18 或 10-18）")
        persons.append(p)
    return persons


def _has_date(text: str) -> bool:
    """text 里有没有一个像样的 MM-DD（月 1–12、日 1–31）。"""
    return any(1 <= int(m) <= 12 and 1 <= int(d) <= 31 for m, d in _DATE_RE.findall(text))


def check_hosts(roster: Roster, enabled) -> None:
    """managed 会话引用的宿主必须已启用（7.18）；enabled(host) -> bool。没开的直接报错，说清是哪个宿主。"""
    for s in roster.managed:
        if not enabled(s.host):
            raise RosterError(f"session key={s.key!r}: 宿主 {s.host} 没开（配置里 [hosts.{s.host}] enabled = false）")


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
    except (OSError, UnicodeDecodeError) as e:   # 读不了 / 不是 UTF-8：同样当名册无效（常驻进程沿用上一份）
        raise RosterError(f"名册读不了 {path}: {e}") from e
    try:
        return parse_roster(data)
    except RosterError as e:
        raise RosterError(f"{path}: {e}") from e
