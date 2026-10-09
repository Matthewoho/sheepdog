"""安全闸（7.7）：机制在代码，规则在配置。

- 规则文件 security.toml（配置键 security_path，默认相对配置目录）：每条规则 tag 或 hold，
  patterns（正则，任一命中）+ 可选条件 external_sender / sender_types（全部满足才算命中）。
  本模块不含任何具体关键词，规则全部来自配置。
- 判定只看消息本身（正文、发送方类型、发送方租户），纯函数，便于测试。
- 「主人原话」核对：读总线会话 transcript 的 USER_INPUT，确认 forward --quote 的话主人真的说过。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .models import Message

TAG = "tag"
HOLD = "hold"
NONE = "none"
# 动作严重度：多条规则命中时取最严
_SEVERITY = {NONE: 0, TAG: 1, HOLD: 2}

_TOP_KEYS = {"own_tenant_keys", "quote_max_age_hours", "rule"}
_RULE_KEYS = {"name", "note", "action", "patterns", "external_sender", "sender_types"}


class SecurityError(ValueError):
    pass


@dataclass
class SecurityRule:
    name: str
    note: str
    action: str
    patterns: list[re.Pattern] = field(default_factory=list)
    # None = 不设这个条件
    external_sender: bool | None = None
    sender_types: list[str] | None = None

    def matches(self, msg: Message, external: bool) -> bool:
        if self.external_sender is not None and self.external_sender != external:
            return False
        if self.sender_types is not None and msg.sender_type not in self.sender_types:
            return False
        if self.patterns:
            text = msg.content or ""
            return any(p.search(text) for p in self.patterns)
        return True


@dataclass
class SecurityConfig:
    rules: list[SecurityRule] = field(default_factory=list)
    own_tenant_keys: list[str] = field(default_factory=list)
    quote_max_age_hours: float = 24
    # 规则文件是否存在（不存在 = 没有规则，doctor 警告）
    loaded: bool = False

    def is_external(self, msg: Message) -> bool:
        """发送方租户不在 own_tenant_keys 里 = 外部人。没配 own_tenant_keys 或消息没带租户时按外部人处理（宁严勿松）。"""
        return not (msg.sender_tenant_key and msg.sender_tenant_key in self.own_tenant_keys)

    def evaluate(self, msg: Message) -> tuple[list[str], str]:
        """返回 (命中的规则名, 最严动作)。"""
        external = self.is_external(msg)
        hit = [r for r in self.rules if r.matches(msg, external)]
        action = max((r.action for r in hit), key=_SEVERITY.__getitem__, default=NONE)
        return [r.name for r in hit], action

    def note_of(self, name: str) -> str:
        r = next((x for x in self.rules if x.name == name), None)
        return r.note if r and r.note else name

    def counts(self) -> dict[str, int]:
        return {TAG: sum(r.action == TAG for r in self.rules), HOLD: sum(r.action == HOLD for r in self.rules)}


def parse_security(data: dict) -> SecurityConfig:
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise SecurityError(f"顶层有未知键 {sorted(unknown)}")
    keys = data.get("own_tenant_keys", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise SecurityError("own_tenant_keys 必须是字符串列表")
    age = data.get("quote_max_age_hours", 24)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age <= 0:
        raise SecurityError("quote_max_age_hours 必须是正数")
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise SecurityError("rule 必须写成 [[rule]] 数组")
    rules: list[SecurityRule] = []
    names: set[str] = set()
    for i, raw in enumerate(raw_rules):
        where = f"rule[{i}]"
        unknown = set(raw) - _RULE_KEYS
        if unknown:
            raise SecurityError(f"{where}: 未知键 {sorted(unknown)}")
        name = raw.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise SecurityError(f"{where}: name 必填")
        name = name.strip()
        where = f"rule[{i}] name={name!r}"
        if name in names:
            raise SecurityError(f"{where}: name 重复")
        names.add(name)
        action = raw.get("action", "")
        if action not in (TAG, HOLD):
            raise SecurityError(f"{where}: action 必须是 tag 或 hold，收到 {action!r}")
        note = raw.get("note", "")
        if not isinstance(note, str):
            raise SecurityError(f"{where}: note 必须是字符串")
        pats = raw.get("patterns", [])
        if not isinstance(pats, list) or not all(isinstance(p, str) and p for p in pats):
            raise SecurityError(f"{where}: patterns 必须是非空字符串列表")
        compiled = []
        for p in pats:
            try:
                compiled.append(re.compile(p))
            except re.error as e:
                raise SecurityError(f"{where}: 正则 {p!r} 无效: {e}") from e
        ext = raw.get("external_sender")
        if ext is not None and not isinstance(ext, bool):
            raise SecurityError(f"{where}: external_sender 必须是 true/false")
        types = raw.get("sender_types")
        if types is not None and (not isinstance(types, list) or not all(isinstance(t, str) for t in types)):
            raise SecurityError(f"{where}: sender_types 必须是字符串列表")
        if not compiled and ext is None and types is None:
            # 既没 patterns 也没条件 = 命中所有消息，几乎一定是写漏了
            raise SecurityError(f"{where}: 至少要有 patterns 或一个条件（external_sender / sender_types）")
        rules.append(SecurityRule(name, note.strip(), action, compiled, ext, types))
    return SecurityConfig(rules, keys, float(age), loaded=True)


def load_security(path: Path) -> SecurityConfig:
    """读规则文件。不存在 = 没有规则；格式错误抛 SecurityError（调用方报错退出）。"""
    if not path.exists():
        return SecurityConfig()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SecurityError(f"{path}: TOML 解析失败: {e}") from e
    try:
        return parse_security(data)
    except SecurityError as e:
        raise SecurityError(f"{path}: {e}") from e


# ---------- 「主人原话」核对 ----------
_USER_REQUEST = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def owner_inputs(transcript: Path, max_age_hours: float, now: datetime | None = None) -> list[str]:
    """总线 transcript 里回看期内主人亲口说的话（USER_INPUT 的 <USER_REQUEST> 正文，没有标签取全文）。

    第 0 步是 new-conversation 的开场 prompt（sheepdog 自己写的），不算。
    """
    if not transcript.exists():
        return []
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=max_age_hours)
    out: list[str] = []
    with transcript.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "USER_INPUT":
                continue
            step = d.get("step_index", i)
            if step == 0 or i == 0:
                continue
            ts = _parse_ts(d.get("created_at") or "")
            if ts is None or ts.tzinfo is None or ts < cutoff:
                continue
            content = d.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            found = _USER_REQUEST.findall(content)
            out.append(_norm(" ".join(found)) if found else _norm(content))
    return out


def quote_verified(quote: str, transcript: Path, max_age_hours: float, now: datetime | None = None) -> bool:
    """空白归一后，quote 是否是回看期内某条主人输入的子串。"""
    q = _norm(quote)
    return bool(q) and any(q in said for said in owner_inputs(transcript, max_age_hours, now))
