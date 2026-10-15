"""最后好用的配置（7.40 配套，第一期 B 部分发现的问题第 1 条）。

常驻进程每次成功加载配置（启动、每轮重读成功后），把 config.toml、roster.toml、security.toml 的原文存一份到
<state_dir>/last_good/（内容没变不重写；先写临时文件、校验过再改名）。会话侧命令（receipt、ask、report…，每次都是新进程，
没有「上一份」）读当前配置失败时用它，并在 stderr 写一行说明；从没存过就照旧报错。

找这份不能依赖坏掉的配置：先看 SHEEPDOG_STATE_DIR / 默认状态目录；配置里自己写了 state_dir 的，从坏掉的原文里按行
认出 state_dir = "…" 再找一次。meta.json 记着这份是哪个配置文件的（source）、存在哪个状态目录（state_dir），对不上的不用。
Stop hook 不用它：hook 出错一律不输出，不拿旧配置去拦会话。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config, ConfigError, default_state_dir, load_config
from .roster import RosterError, load_roster
from .security import SecurityError, load_security

DIR_NAME = "last_good"
META = "meta.json"
CONFIG, ROSTER, SECURITY = "config.toml", "roster.toml", "security.toml"
# 配置文件里顶层（第一个 [表] 之前）的 state_dir = "…"：配置写坏时只靠它找到自定义状态目录
_STATE_DIR_LINE = re.compile(r'^\s*state_dir\s*=\s*(["\'])(?P<v>[^"\']+)\1\s*(#.*)?$')


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dir(state_dir: Path) -> Path:
    return Path(state_dir) / DIR_NAME


def _read_meta(d: Path) -> dict:
    try:
        data = json.loads((d / META).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _valid(name: str, data: bytes, d: Path) -> bool:
    """存之前再校验一次刚读到的字节（读和存之间文件可能又被改坏了）。"""
    fd, tmp = tempfile.mkstemp(prefix=f".check.{name}.", suffix=".toml", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        p = Path(tmp)
        if name == CONFIG:
            load_config(p)
        elif name == ROSTER:
            load_roster(p, True)
        else:
            load_security(p)
        return True
    except (ConfigError, RosterError, SecurityError, OSError, ValueError):
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)


def save(cfg: Config, which: tuple[str, ...] = (CONFIG, ROSTER, SECURITY)) -> list[str]:
    """常驻进程加载成功后调用：把这几样的原文存进 <state_dir>/last_good/。内容没变不重写；源文件不存在的，删掉旧的那份
    （名册 / 规则删了就是没有，不能在回退时又冒出来）。返回这次改写了哪几样。出错只影响这一次存，不抛出。"""
    d = _dir(cfg.state_dir)
    d.mkdir(parents=True, exist_ok=True)
    meta = _read_meta(d)
    sources = {CONFIG: cfg.config_path, ROSTER: cfg.roster_file, SECURITY: cfg.security_file}
    changed: list[str] = []
    for name in which:
        src = Path(sources[name])
        dst = d / name
        try:
            data = src.read_bytes() if src.is_file() else None
        except OSError:
            continue   # 读不了：不动旧的那份
        if data is None:
            if dst.exists() or name in meta:
                dst.unlink(missing_ok=True)
                meta.pop(name, None)
                changed.append(name)
            continue
        old = meta.get(name) or {}
        same = dst.is_file() and dst.read_bytes() == data and old.get("source") == str(src)
        if same or not _valid(name, data, d):
            continue
        _atomic_write(dst, data)
        meta[name] = {"source": str(src), "saved_at": _now()}
        changed.append(name)
    if changed or meta.get("state_dir") != str(cfg.state_dir):
        meta["state_dir"] = str(cfg.state_dir)
        _atomic_write(d / META, json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    return changed


@dataclass
class Saved:
    directory: Path
    meta: dict

    def path(self, name: str) -> Path | None:
        p = self.directory / name
        return p if name in self.meta and p.is_file() else None

    def saved_at(self, name: str) -> str:
        return (self.meta.get(name) or {}).get("saved_at", "")

    def label(self, name: str) -> str:
        """「10-15 17:50:12」这样的时间（存的时候的本地时间）。"""
        at = self.saved_at(name)
        try:
            return datetime.fromisoformat(at).strftime("%m-%d %H:%M:%S")
        except ValueError:
            return at or "?"


def _state_dir_from_text(config_path: Path) -> Path | None:
    try:
        lines = config_path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.lstrip().startswith("["):
            return None
        m = _STATE_DIR_LINE.match(line)
        if m:
            return Path(m.group("v")).expanduser()
    return None


def find(config_path: Path) -> Saved | None:
    """这个配置文件的「最后好用的那份」；没有是 None。"""
    cands = [default_state_dir()]
    if (sd := _state_dir_from_text(config_path)) is not None:
        cands.append(sd)
    for sd in cands:
        d = _dir(sd)
        meta = _read_meta(d)
        if (meta.get(CONFIG) or {}).get("source") == str(config_path) and (d / CONFIG).is_file():
            return Saved(d, meta)
    return None


def find_for(cfg: Config) -> Saved | None:
    """配置本身是好的、名册或规则坏了时：按这份配置的状态目录找。"""
    d = _dir(cfg.state_dir)
    meta = _read_meta(d)
    return Saved(d, meta) if meta else None


def load_saved_config(saved: Saved, config_path: Path) -> Config:
    """读最后好用的那份配置。相对路径（名册、playbook、overlay…）照旧相对原配置文件所在目录；状态目录用存的时候那个。"""
    cfg = load_config(saved.directory / CONFIG)
    cfg.config_path = config_path
    if saved.meta.get("state_dir"):
        cfg.state_dir = Path(saved.meta["state_dir"])
    return cfg
