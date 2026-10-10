"""Codex 配置档展开（7.18，20:0x 实测后改）：`codex app-server` 不接受 `-p <profile>`，所以 sheepdog 自己读
`~/.codex/<profile>.config.toml`，把内容拍平成点号键，每个键转成一对 `-c key=<TOML 字面值>`，放在 `app-server` 前面。

- 权限内容只写在那份配置档里，由主人自己维护；sheepdog 只读、只照原样展开，不增不减，也不传任何别的沙箱、审批参数。
- 只影响 sheepdog 自己起的 app-server；主人在 Codex 应用里的会话不受影响。
- 支持的值：字符串、布尔、整数、浮点，以及由这些（或这些组成的数组）组成的数组。日期、表的数组等一律按配置档错误处理。
- 键的每一段只能是裸键（[A-Za-z0-9_-]+）：codex 的 -c 按点号切路径，带点号或其他字符的键没法原样表达，也按错误处理。
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class ProfileError(ValueError):
    """配置档读不到、解析失败，或者有没法展开成 -c 的内容。"""


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def profile_path(profile: str) -> Path:
    return codex_home() / f"{profile}.config.toml"


def toml_literal(value, where: str) -> str:
    """一个值的 TOML 字面值：字符串带引号并转义（JSON 字符串的转义在 TOML 基本字符串里都合法）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(v, where) for v in value) + "]"
    raise ProfileError(f"{where} 的值类型 {type(value).__name__} 不支持（只支持字符串、布尔、整数、浮点和它们的数组）")


def flatten(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """把配置档拍平成 (点号键, TOML 字面值)，按文件里的顺序。"""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        if not _BARE_KEY.fullmatch(k):
            raise ProfileError(f"键 {prefix + k!r} 不是裸键（只能是 [A-Za-z0-9_-]+），没法用 -c 原样传")
        key = prefix + k
        if isinstance(v, dict):
            out += flatten(v, key + ".")
        else:
            out.append((key, toml_literal(v, key)))
    return out


def profile_overrides(profile: str) -> list[str]:
    """读 ~/.codex/<profile>.config.toml，展开成 ["-c", "k=v", "-c", ...]。读不到、解析失败、内容不支持就抛 ProfileError。"""
    p = profile_path(profile)
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ProfileError(f"配置档 {p} 不存在") from e
    except OSError as e:
        raise ProfileError(f"配置档 {p} 读不了：{e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"配置档 {p} 解析失败：{e}") from e
    try:
        pairs = flatten(data)
    except ProfileError as e:
        raise ProfileError(f"配置档 {p}：{e}") from e
    argv: list[str] = []
    for key, lit in pairs:
        argv += ["-c", f"{key}={lit}"]
    return argv


def app_server_argv(codex: str, profile: str = "") -> list[str]:
    """起 app-server 的命令行：[codex, <配置档展开的 -c ...>, "app-server"]。没设配置档就是 [codex, "app-server"]。"""
    return [codex, *(profile_overrides(profile) if profile else []), "app-server"]
