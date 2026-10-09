"""配置加载。

配置与运行数据都放在仓库之外（XDG 目录），仓库内只有 examples/ 下的示例：
- 配置：$SHEEPDOG_CONFIG 或 ~/.config/sheepdog/config.toml
- 状态：$SHEEPDOG_STATE_DIR 或 ~/.local/state/sheepdog/
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


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
    # 机器人/应用发的消息正文以其中任一前缀开头就 drop（reason self_escalation），
    # 用来挡住会话以 bot 身份找主人的私聊，防止回环
    drop_bot_message_prefixes: list[str] = field(default_factory=list)


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
    # 代回前缀：作为 playbook 的 {{reply_prefix}} 占位符，规则文字写在 playbook 里
    reply_prefix: str = "🐕 [Agent 代回] "


@dataclass
class WatchConfig:
    # 「等别人回复」的提醒档位（分钟，最多两档）与到期时间
    remind_minutes: list[int] = field(default_factory=lambda: [15, 30])
    expire_minutes: int = 60

    def validate(self) -> None:
        r = self.remind_minutes
        if not isinstance(r, list) or not all(isinstance(m, int) and m > 0 for m in r):
            raise ConfigError("watch.remind_minutes 必须是正整数列表")
        # watches 表只有两个提醒时间列（nudged_15_at / nudged_30_at 按第 1、2 档使用）
        if len(r) > 2:
            raise ConfigError("watch.remind_minutes 最多两档")
        if r != sorted(set(r)):
            raise ConfigError("watch.remind_minutes 必须从小到大且不重复")
        if not isinstance(self.expire_minutes, int) or any(m >= self.expire_minutes for m in r):
            raise ConfigError("watch.expire_minutes 必须是整数且大于所有提醒档位")


@dataclass
class EscalationConfig:
    # 「找主人」用的聊天（机器人与主人的私聊）：其中的消息先于普通路由处理（7.8）
    chat_ids: list[str] = field(default_factory=list)
    # 从机器人消息正文里提取 topic_id 的正则，必须有命名分组 (?P<topic>...)；具体格式写在个人配置里
    header_regex: str = ""
    # 一条「需要你定」多久内算未结
    open_hours: float = 24
    # 机器人发给主人的任何消息，从第一行认出来自哪个 topic（命名分组 topic，第一行任意位置）（7.15）
    attribution_regex: str = ""
    # 第一行 [..·名字] 里的名字 → topic_id（如 "总线" = "tp_bus"）；认不出时再和各会话标题比
    aliases: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.chat_ids, list) or not all(isinstance(c, str) and c for c in self.chat_ids):
            raise ConfigError("escalation.chat_ids 必须是非空字符串列表")
        if isinstance(self.open_hours, bool) or not isinstance(self.open_hours, (int, float)) or self.open_hours <= 0:
            raise ConfigError("escalation.open_hours 必须是正数")
        if self.chat_ids and not self.header_regex:
            raise ConfigError("escalation.chat_ids 已配置但 header_regex 为空：机器人的提问将无法识别")
        for key in ("header_regex", "attribution_regex"):
            text = getattr(self, key)
            if not text:
                continue
            try:
                rx = re.compile(text)
            except re.error as e:
                raise ConfigError(f"escalation.{key} 无效: {e}") from e
            if "topic" not in rx.groupindex:
                raise ConfigError(f"escalation.{key} 必须有命名分组 (?P<topic>...)")
        if not isinstance(self.aliases, dict) or not all(
                isinstance(k, str) and k and isinstance(v, str) and v.startswith("tp_") for k, v in self.aliases.items()):
            raise ConfigError("[escalation.aliases] 必须是「名字 = \"tp_xxx\"」")

    @property
    def pattern(self) -> re.Pattern | None:
        return re.compile(self.header_regex) if self.header_regex else None

    @property
    def attribution(self) -> re.Pattern | None:
        return re.compile(self.attribution_regex) if self.attribution_regex else None


@dataclass
class OwnerContextConfig:
    # 主人本人在聊天里的发言作为背景送给负责的会话（7.13）；默认关
    enabled: bool = False
    # 聊天没有名册 / 总线新开归属时，送给这么久内最近处理过该聊天的会话（含总线）
    follow_hours: float = 24
    # 正文以这些前缀开头的视为会话代回，不送
    skip_prefixes: list[str] = field(default_factory=list)
    # 主人对已投递消息点的表情（7.13 补充）：回看多久内投递过的消息、多久查一次（0 = 不查）
    reaction_lookback_hours: float = 24
    reaction_check_minutes: float = 5

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[owner_context] enabled 必须是 true/false")
        if isinstance(self.follow_hours, bool) or not isinstance(self.follow_hours, (int, float)) or self.follow_hours < 0:
            raise ConfigError("[owner_context] follow_hours 必须是 >= 0 的数")
        if not isinstance(self.skip_prefixes, list) or not all(isinstance(x, str) and x for x in self.skip_prefixes):
            raise ConfigError("[owner_context] skip_prefixes 必须是非空字符串列表")
        for k in ("reaction_lookback_hours", "reaction_check_minutes"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ConfigError(f"[owner_context] {k} 必须是 >= 0 的数")


@dataclass
class LoopGuardConfig:
    # 防止和 Agent 机器人来回循环（7.14）。名字 / id 的具体值只写在个人配置里
    agent_sender_names: list[str] = field(default_factory=list)   # 发送人名字包含其一（不分大小写）即视为 Agent
    agent_sender_ids: list[str] = field(default_factory=list)     # 按 open_id / app_id 精确指定
    treat_all_bots_as_agents: bool = True                         # sender_type 为 app/bot 的一律按 Agent
    max_agent_replies: int = 4        # 同一聊天 window_minutes 内会话代回超过这么多次 → 熔断
    window_minutes: float = 10
    cooldown_minutes: float = 30

    def validate(self) -> None:
        for k in ("agent_sender_names", "agent_sender_ids"):
            v = getattr(self, k)
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(f"[loop_guard] {k} 必须是非空字符串列表")
        if not isinstance(self.treat_all_bots_as_agents, bool):
            raise ConfigError("[loop_guard] treat_all_bots_as_agents 必须是 true/false")
        if isinstance(self.max_agent_replies, bool) or not isinstance(self.max_agent_replies, int) or self.max_agent_replies < 1:
            raise ConfigError("[loop_guard] max_agent_replies 必须是 >= 1 的整数")
        for k in ("window_minutes", "cooldown_minutes"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise ConfigError(f"[loop_guard] {k} 必须是正数")


@dataclass
class BusConfig:
    # 总线每天（本地日历日）最多新开几个会话（7.10）；0 = 禁止总线新开
    max_new_sessions_per_day: int = 5

    def validate(self) -> None:
        v = self.max_new_sessions_per_day
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ConfigError("[bus] max_new_sessions_per_day 必须是 >= 0 的整数")


@dataclass
class AckConfig:
    # 配置里有 [ack] 段才开启（7.9）
    enabled: bool = False
    # IM 表情类型（具体名字只写在个人配置里）
    emoji_type: str = ""
    # 投递原因在其中的消息，送达会话后以主人身份点表情
    reasons: list[str] = field(default_factory=list)
    # 这些原因的表情，在主人身份回复后撤下
    remove_on_reply_reasons: list[str] = field(default_factory=list)
    # 投递原因在其中的消息，送达后以 bot 身份点表情、永不撤（如主人在「找主人」聊天里的回复）；与 reasons 同时命中以它为准
    bot_reasons: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.enabled:
            return
        if not isinstance(self.emoji_type, str) or not self.emoji_type.strip():
            raise ConfigError("[ack] emoji_type 必填")
        for k in ("reasons", "remove_on_reply_reasons", "bot_reasons"):
            v = getattr(self, k)
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(f"[ack] {k} 必须是字符串列表")


@dataclass
class Config:
    # 「我」的 open_id，用于 @我 / 自己发的 / 回复我 的判定
    self_open_id: str = ""
    # 轮询间隔与回看窗口
    poll_interval_seconds: int = 60
    overlap_seconds: int = 180
    # 每轮拉取最多翻几页（每页 50 条）；拉不完记 partial，不推进水位线（7.16）
    max_pages: int = 100
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
    # 业务规则 md 目录（相对本配置文件所在目录）；留空 = playbook/
    playbook_dir: str = ""
    # 安全规则文件（相对本配置文件所在目录）；留空 = security.toml，不存在 = 没有规则
    security_path: str = ""
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    ack: AckConfig = field(default_factory=AckConfig)
    bus: BusConfig = field(default_factory=BusConfig)
    owner_context: OwnerContextConfig = field(default_factory=OwnerContextConfig)
    loop_guard: LoopGuardConfig = field(default_factory=LoopGuardConfig)
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
    def playbook_path(self) -> Path:
        p = Path(self.playbook_dir or "playbook").expanduser()
        return p if p.is_absolute() else self.config_path.parent / p

    @property
    def security_file(self) -> Path:
        p = Path(self.security_path or "security.toml").expanduser()
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
        _apply(cfg.watch, data.get("watch", {}))
        _apply(cfg.escalation, data.get("escalation", {}))
        if "aliases" in data.get("escalation", {}):
            cfg.escalation.aliases = data["escalation"]["aliases"]
        _apply(cfg.bus, data.get("bus", {}))
        _apply(cfg.owner_context, data.get("owner_context", {}))
        _apply(cfg.loop_guard, data.get("loop_guard", {}))
        if isinstance(data.get("ack"), dict):
            _apply(cfg.ack, {k: v for k, v in data["ack"].items() if k != "enabled"})
            cfg.ack.enabled = True
        if "state_dir" in data:
            cfg.state_dir = Path(data["state_dir"]).expanduser()
    cfg.watch.validate()
    cfg.escalation.validate()
    cfg.ack.validate()
    cfg.bus.validate()
    cfg.owner_context.validate()
    cfg.loop_guard.validate()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    return cfg
