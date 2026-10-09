"""配置加载。

配置与运行数据都放在仓库之外（XDG 目录），仓库内只有 examples/ 下的示例：
- 配置：$SHEEPDOG_CONFIG 或 ~/.config/sheepdog/config.toml
- 状态：$SHEEPDOG_STATE_DIR 或 ~/.local/state/sheepdog/
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "sheepdog"


def default_state_dir() -> Path:
    env = os.environ.get("SHEEPDOG_STATE_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sheepdog"


def default_config_path() -> Path:
    env = os.environ.get("SHEEPDOG_CONFIG")
    return Path(env).expanduser() if env else default_config_dir() / "config.toml"


@dataclass
class RoutingConfig:
    # 命中即升级为直推 session 的关键词（大小写不敏感）
    keywords: list[str] = field(default_factory=list)
    # 关键人 open_id：群里发言不 @ 也直推
    vip_sender_ids: list[str] = field(default_factory=list)
    # 免打扰群是否完全忽略（@我 / @所有人 仍直推）
    ignore_muted_chats: bool = True
    # @所有人 是否直推
    dispatch_at_all: bool = True
    # bot/应用私聊是否直推（默认进 Inbox 的 bot 分区）
    dispatch_bot_p2p: bool = False
    # 额外强制忽略 / 强制关注的 chat_id
    ignore_chat_ids: list[str] = field(default_factory=list)
    watch_chat_ids: list[str] = field(default_factory=list)
    # 群里机器人/应用/系统发的消息不参与关键词匹配（机器人私聊的 bot_p2p_keyword 不受影响）
    keyword_skip_bot_senders: bool = True


@dataclass
class SessionConfig:
    # session 标题前缀：只有带此前缀且在注册表内的 session 才会被投递
    title_prefix: str = "[managed]"
    # 新建 session 使用的模型档位：flash_lite | flash | pro（空 = App 默认）
    model: str = ""
    # P0：所有信号投递到同一个总线 session
    bus_title: str = "Lark 信号·总线"
    # 你在 session 里发言后，暂停投递的分钟数
    human_attach_minutes: int = 15
    # 回执超时（分钟），超时判为 failed
    receipt_timeout_minutes: int = 20
    # 代回前缀：session 以主人身份对外发 IM 消息时，正文开头必须加它；空字符串 = 不要求
    reply_prefix: str = "🐕 [Agent 代回] "


@dataclass
class Config:
    # 「我」的 open_id，用于 @我 / 自己发的 / 回复我 的判定
    self_open_id: str = ""
    # 轮询间隔与回看窗口
    poll_interval_seconds: int = 60
    overlap_seconds: int = 180
    initial_lookback_minutes: int = 10
    timezone_offset: str = "+08:00"
    # 投递目标：agentapi（Antigravity App）| dryrun
    sink: str = "agentapi"
    # 本地数据保留天数
    retention_days: int = 7
    # Prompt 覆盖层文件（个人化指令，例如指向你的知识库协议），不进仓库
    prompt_overlay_path: str = ""
    # 名册文件（相对本配置文件所在目录）；留空 = 默认 roster.toml，不存在时视为空名册
    roster_path: str = ""
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    state_dir: Path = field(default_factory=default_state_dir)
    config_path: Path = field(default_factory=default_config_path)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "sheepdog.sqlite3"

    @property
    def receipts_dir(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def roster_file(self) -> Path:
        p = Path(self.roster_path or "roster.toml").expanduser()
        return p if p.is_absolute() else self.config_path.parent / p

    @property
    def roster_required(self) -> bool:
        # 显式配置了 roster_path 时文件必须存在，避免路径写错后静默退回「无名册」
        return bool(self.roster_path)

    def prompt_overlay(self) -> str:
        if not self.prompt_overlay_path:
            return ""
        p = Path(self.prompt_overlay_path).expanduser()
        if not p.is_absolute():
            p = self.config_path.parent / p
        return p.read_text(encoding="utf-8") if p.exists() else ""


def _apply(dc, data: dict) -> None:
    """把 dict 浅层写入 dataclass，忽略未知键。"""
    for k, v in data.items():
        if hasattr(dc, k) and not isinstance(v, dict):
            setattr(dc, k, v)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or default_config_path()
    cfg.config_path = path
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
        _apply(cfg, data)
        _apply(cfg.routing, data.get("routing", {}))
        _apply(cfg.session, data.get("session", {}))
        if "state_dir" in data:
            cfg.state_dir = Path(data["state_dir"]).expanduser()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    return cfg
