"""配置加载。

配置与运行数据都放在仓库之外（XDG 目录），仓库内只有 examples/ 下的示例：
- 配置：$SHEEPDOG_CONFIG 或 ~/.config/sheepdog/config.toml
- 状态：$SHEEPDOG_STATE_DIR 或 ~/.local/state/sheepdog/
"""

from __future__ import annotations

import os
import re
import shlex
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
    # 每条消息送进会话时最多带多少行正文；超过的末尾提示用 sheepdog show 看全文（7.16）
    max_message_lines: int = 200
    # 代回前缀：作为 playbook 的 {{reply_prefix}} 占位符（7.17 起改用卡片 + 后缀，保留兼容）
    reply_prefix: str = "🐕 [Agent 代回] "
    # 代回卡片右下角的标记（7.17）：playbook 的 {{reply_suffix}}，sheepdog reply-card 用它
    reply_suffix: str = "[🐕Sheepdog Reply]"
    # 代回卡片宽度：compact（400px）| default（≤600px）| fill（撑满）
    reply_card_width: str = "compact"

    def validate(self) -> None:
        if self.reply_card_width not in ("compact", "default", "fill"):
            raise ConfigError("session.reply_card_width 只能是 compact / default / fill")
        if not isinstance(self.reply_suffix, str):
            raise ConfigError("session.reply_suffix 必须是字符串")


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
    # 正文任意位置包含其一也视为会话代回（7.17：代回改成卡片，标记在右下角），与 skip_prefixes 并存
    agent_markers: list[str] = field(default_factory=list)
    # 主人对已投递消息点的表情（7.13 补充）：回看多久内投递过的消息、多久查一次（0 = 不查）
    reaction_lookback_hours: float = 24
    reaction_check_minutes: float = 5

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[owner_context] enabled 必须是 true/false")
        if isinstance(self.follow_hours, bool) or not isinstance(self.follow_hours, (int, float)) or self.follow_hours < 0:
            raise ConfigError("[owner_context] follow_hours 必须是 >= 0 的数")
        for k in ("skip_prefixes", "agent_markers"):
            v = getattr(self, k)
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(f"[owner_context] {k} 必须是非空字符串列表")
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


# Codex 配置档名：只是一个名字，拼进 ~/.codex/<名字>.config.toml，不许带路径或其他字符；
# 也不许以「-」开头
_PROFILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")

# 会话宿主（7.18）：antigravity 走 App 的 agentapi；codex / claude 走各自的命令行，在后台跑
ANTIGRAVITY = "antigravity"
CLI_HOSTS = ("codex", "claude")
HOSTS = (ANTIGRAVITY, *CLI_HOSTS)


@dataclass
class HostConfig:
    """[hosts.codex] / [hosts.claude]（7.18）。model / effort 只是默认值，名册和 new-session 可以逐个会话覆盖。

    权限不归 sheepdog 管：这里没有任何沙箱、审批、权限项，全部来自宿主自己的配置；
    settings 只是一个文件名（只给 Claude，原样传给 --settings），sheepdog 不写它的内容。
    """
    name: str = ""
    enabled: bool = False
    command: str = ""             # 可执行文件；留空 = 宿主名（codex / claude）
    model: str = ""               # 默认模型；留空 = 宿主自己的默认
    effort: str = ""              # 默认推理档位；留空 = 宿主自己的默认
    workdir: str = ""             # 会话的工作目录，启用时必填
    timeout_minutes: float = 60   # 一轮最多跑多久，超过就杀掉整个进程组
    max_concurrent: int = 2       # 这个宿主同时最多跑几轮，超过的批次排队
    settings: str = ""            # 只给 Claude
    # 只给 Codex：Codex 配置档名。sheepdog 读 ~/.codex/<profile>.config.toml，展开成 -c 参数传给自己起的 app-server
    # （app-server 不接受 -p）；权限内容全在那份文件里，主人在 Codex 应用里的会话不受影响。空 = 不用配置档。改了要重启
    profile: str = ""
    # 新开这个宿主的会话前要先问主人（7.18，19:0x）：new-session --host <它> 必须带核对过的 --quote 主人原话。
    # 只管新开；spawn（名册里主人亲手写的）和往已有会话 forward 不受影响
    ask_owner_before_new: bool = True

    @property
    def executable(self) -> str:
        return self.command or self.name

    @property
    def workdir_path(self) -> Path:
        return Path(self.workdir).expanduser()

    def validate(self) -> None:
        where = f"[hosts.{self.name}]"
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"{where} enabled 必须是 true/false")
        if not isinstance(self.ask_owner_before_new, bool):
            raise ConfigError(f"{where} ask_owner_before_new 必须是 true/false")
        for k in ("command", "model", "effort", "workdir", "settings", "profile"):
            if not isinstance(getattr(self, k), str):
                raise ConfigError(f"{where} {k} 必须是字符串")
        if self.settings and self.name != "claude":
            raise ConfigError(f"{where} settings 只给 Claude 用")
        if self.profile and self.name != "codex":
            raise ConfigError(f"{where} profile 只给 Codex 用")
        if self.profile and not _PROFILE_RE.fullmatch(self.profile):
            raise ConfigError(f"{where} profile 只能是字母、数字、下划线、减号（[A-Za-z0-9_-]+），且不能以减号开头，"
                              f"收到 {self.profile!r}")
        t = self.timeout_minutes
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            raise ConfigError(f"{where} timeout_minutes 必须是正数")
        m = self.max_concurrent
        if isinstance(m, bool) or not isinstance(m, int) or m < 1:
            raise ConfigError(f"{where} max_concurrent 必须是 >= 1 的整数")
        if self.enabled and not self.workdir.strip():
            raise ConfigError(f"{where} 已启用但 workdir 为空：会话的工作目录必填")


@dataclass
class SourceConfig:
    # false = 不拉飞书（测试环境用，7.18）：这时可以用 sheepdog inject 往账本里放合成消息
    enabled: bool = True

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[source] enabled 必须是 true/false")


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
    # 会话在自己终端里敲的 sheepdog 命令（提示词里所有「去跑 sheepdog …」的命令文字都用它）。
    # 测试环境改成一个只连测试配置的包装脚本的绝对路径，免得会话把操作写进正式账本。热加载，下一轮生效
    cli_name: str = "sheepdog"
    source: SourceConfig = field(default_factory=SourceConfig)
    hosts: dict = field(default_factory=lambda: {h: HostConfig(name=h) for h in CLI_HOSTS})
    state_dir: Path = field(default_factory=default_state_dir)
    config_path: Path = field(default_factory=default_config_path)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "sheepdog.sqlite3"

    @property
    def receipts_dir(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def runs_dir(self) -> Path:
        """命令行宿主每轮的输入、输出、错误（7.18）：runs/<batch_id>.in / .out / .err"""
        return self.state_dir / "runs"

    @property
    def cli_command(self) -> str:
        """cli_name 按 shell 规则加好引号（路径带空格时用引号括起来），直接拼进给会话看的命令里。"""
        return shlex.quote(self.cli_name)

    def host_enabled(self, host: str) -> bool:
        return host == ANTIGRAVITY or (host in self.hosts and self.hosts[host].enabled)

    def check_host(self, host: str) -> None:
        """会话要用的宿主必须存在且已启用；不行就抛 ValueError，说清是哪个宿主没开。"""
        if host not in HOSTS:
            raise ValueError(f"未知宿主 {host!r}，只能是 {' / '.join(HOSTS)}")
        if not self.host_enabled(host):
            raise ValueError(f"宿主 {host} 没开：配置里 [hosts.{host}] enabled = false")

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
        _apply(cfg.source, data.get("source", {}))
        hosts = data.get("hosts", {})
        if not isinstance(hosts, dict) or set(hosts) - set(CLI_HOSTS):
            raise ConfigError(f"[hosts] 只认 {' / '.join(f'[hosts.{h}]' for h in CLI_HOSTS)}")
        for h in CLI_HOSTS:
            _apply(cfg.hosts[h], hosts.get(h, {}))
            cfg.hosts[h].name = h
        if isinstance(data.get("ack"), dict):
            _apply(cfg.ack, {k: v for k, v in data["ack"].items() if k != "enabled"})
            cfg.ack.enabled = True
        if "state_dir" in data:
            cfg.state_dir = Path(data["state_dir"]).expanduser()
    cfg.session.validate()
    cfg.watch.validate()
    cfg.escalation.validate()
    cfg.ack.validate()
    cfg.bus.validate()
    cfg.owner_context.validate()
    cfg.loop_guard.validate()
    if not isinstance(cfg.cli_name, str) or not cfg.cli_name.strip():
        raise ConfigError("cli_name 必须是非空字符串")
    cfg.cli_name = cfg.cli_name.strip()
    cfg.source.validate()
    for h in cfg.hosts.values():
        h.validate()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    return cfg
