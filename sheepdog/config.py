"""配置加载。

配置与运行数据都放在仓库之外（XDG 目录），仓库内只有 examples/ 下的示例：
- 配置：$SHEEPDOG_CONFIG 或 ~/.config/sheepdog/config.toml
- 状态：$SHEEPDOG_STATE_DIR 或 ~/.local/state/sheepdog/
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger(__name__)
# prompt_overlay 文件上一次读成功的内容（常驻进程内有效）：读不了 / 不是 UTF-8 时沿用它并 WARNING，不让投递那一轮失败
_OVERLAY_LAST_GOOD: dict[Path, str] = {}


class ConfigError(ValueError):
    pass


_TOML_AT = re.compile(r"\s*\(at line (\d+), column (\d+)\)\s*$")


def toml_error_text(path: Path, e: tomllib.TOMLDecodeError) -> str:
    """TOML 语法错误写成「哪个文件 第几行第几列：原因」，日志里一眼看得出是哪一行坏了。"""
    msg = str(e)
    m = _TOML_AT.search(msg)
    if m is None:
        return f"{path} TOML 语法错误：{msg}"
    return f"{path} 第 {m.group(1)} 行第 {m.group(2)} 列 TOML 语法错误：{msg[:m.start()]}"


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
    # 7.45 A 类（热加载）：主人本人账号在群里 @ 机器人 = 给助理的指令（owner_instruction）：按话题 / 引用归属 → 名册负责的
    # 会话 → 总线送去，会话在跑就插队。关掉后照旧当背景（owner_context）
    owner_at_assistant: bool = True

    def validate(self) -> None:
        if not isinstance(self.owner_at_assistant, bool):
            raise ConfigError("[routing] owner_at_assistant 必须是 true/false")


@dataclass
class RunConfig:
    """[run]（7.45 第三节，热加载）：常驻循环怎么等下一轮。"""
    # 开着：每轮之间不再死睡 poll_interval_seconds，机器人实时事件一到（攒 2 秒）就做一次只入账 + 投递的小轮次，
    # 不跑搜索；关掉：照旧每轮睡满间隔
    wake_on_bot_event: bool = True

    def validate(self) -> None:
        if not isinstance(self.wake_on_bot_event, bool):
            raise ConfigError("[run] wake_on_bot_event 必须是 true/false")


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
    # 7.40 开关（热加载）：会话在跑上一批时，主人在找主人聊天里的回复当轮单独插队送到（7.37 第 4 件）。
    # 关掉后回到原来的排队：等手上那批交回执（或回执超时）再随下一批送
    owner_priority: bool = True
    # 7.47 第 6 条（热加载）：主人插话批次送到后这么多分钟内，那个会话经 sheepdog 一条消息都没发（ask / report / reply），
    # 私聊主人一次。0 = 关
    owner_priority_followup_minutes: int = 10

    def validate(self) -> None:
        if not isinstance(self.owner_priority, bool):
            raise ConfigError("[session] owner_priority 必须是 true/false")
        m = self.owner_priority_followup_minutes
        if isinstance(m, bool) or not isinstance(m, int) or m < 0:
            raise ConfigError("[session] owner_priority_followup_minutes 必须是不小于 0 的整数（0 = 关）")
        if self.reply_card_width not in ("compact", "default", "fill"):
            raise ConfigError("session.reply_card_width 只能是 compact / default / fill")
        if not isinstance(self.reply_suffix, str):
            raise ConfigError("session.reply_suffix 必须是字符串")


@dataclass
class HooksConfig:
    """[hooks]（7.40）：Antigravity 生命周期 hook 的开关。hook 每次被调用都现读配置，改完下一次就生效。"""
    # Stop hook（sheepdog hook stop）：这批没交回执就不让会话结束。关掉后什么都不输出，会话照常结束
    stop: bool = True

    def validate(self) -> None:
        if not isinstance(self.stop, bool):
            raise ConfigError("[hooks] stop 必须是 true/false")


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
    # 7.36：离到期（open_hours）还剩这么多小时还开着，提醒负责会话核实一次；0 = 不提醒；不小于 open_hours 也不提醒（免得一问就提醒）
    remind_before_hours: float = 2
    # 7.36：会话结案后机器人在话题里补的那句「已结案」要不要 @ 主人（话题回复默认不提醒他）。默认不 @
    resolve_notice_at: bool = False
    # 7.40 开关（热加载，常驻进程下一轮生效）：「需要你定」的跟进——判「可能已处理」、发核实提醒和快到期提醒、
    # 原会话已收掉的转给总线提醒。关掉后这些都不做；resolve、来源记录、pending 清单照常
    followup: bool = True
    # 7.40 开关：主人在找主人聊天里单独发「待办」，机器人直接回清单。关掉后不拦，照旧当普通回复处理
    owner_shortcut: bool = True

    def validate(self) -> None:
        if (isinstance(self.remind_before_hours, bool) or not isinstance(self.remind_before_hours, (int, float))
                or self.remind_before_hours < 0):
            raise ConfigError("[escalation] remind_before_hours 必须是 >= 0 的数（0 = 不提醒）")
        for k in ("resolve_notice_at", "followup", "owner_shortcut"):
            if not isinstance(getattr(self, k), bool):
                raise ConfigError(f"[escalation] {k} 必须是 true/false")
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
class ThreadsConfig:
    """[threads]（7.22 一事一串）：会话经 sheepdog ask / report 找主人，同一把钥匙的消息回到同一个话题里；
    主人在话题里回复，按话题送回会话。热加载，下一轮生效。"""
    # ask / report 加在正文前的第一行：{title} = 短标题，{topic} = topic_id。
    # ask 的那一行要能被 [escalation] header_regex 认出来（登记「需要你定」），两者都要能被 attribution_regex 认出来
    ask_header: str = "🐕 需要你定 · {title} [{topic}]"
    report_header: str = "🐕 汇报 · {title} [{topic}]"
    # 渐进式披露：话题里除了开头那条，已经有这么多条（含主人刚回的这一条）就算「复杂」，送回时再附最近几条
    complex_min_replies: int = 3
    recent_replies: int = 5
    # 开头那条、最近几条各截到多少个字
    root_chars: int = 300
    reply_chars: int = 120
    # 只给真实飞书测试用，正式环境不要设：ask / report 发进这个群（+messages-send --chat-id），不再私聊主人；
    # 系统通知（没设 [notify] file 时经机器人发的那些，7.48 第 7 条）也发进这个群；
    # 这个群同时当成「找主人」聊天（等同于加进 [escalation] chat_ids）。空 = 照旧私聊 self_open_id
    owner_chat_id: str = ""
    # 7.45 群话题归属（热加载，思路来自 PR #1 @Jerry030-1，issue #2）：群里话题回复 / 引用按话题开头那条（或被引用的那条）
    # 当初送给了谁、是谁代发的，直接送那个会话（别人的话题回复 C 类；主人 @ 机器人 A 类、主人背景 B 类的第一步）。
    # 关掉后回到以前：只按整群归属、watch、@ 和「最近处理过」分
    group_owner: bool = True
    # 话题最近一次有人说话超过这么多小时，不再自动归属（名册显式绑定的话题不受影响）。Matthew 10-15 21:3x 定 72
    group_owner_hours: float = 72

    def validate(self) -> None:
        if not isinstance(self.group_owner, bool):
            raise ConfigError("[threads] group_owner 必须是 true/false")
        if not _positive(self.group_owner_hours):
            raise ConfigError("[threads] group_owner_hours 必须是正数")
        if not isinstance(self.owner_chat_id, str) or (self.owner_chat_id and not self.owner_chat_id.startswith("oc_")):
            raise ConfigError("[threads] owner_chat_id 必须是 oc_ 开头的群 ID（测试用，正式环境不要设）")
        for k in ("ask_header", "report_header"):
            v = getattr(self, k)
            if not isinstance(v, str) or "{title}" not in v or "{topic}" not in v:
                raise ConfigError(f"[threads] {k} 必须是字符串，并且含 {{title}} 和 {{topic}}")
            if "\n" in v:
                raise ConfigError(f"[threads] {k} 只能是一行")
            try:
                v.format(title="x", topic="tp_x")
            except (KeyError, IndexError, ValueError) as e:
                raise ConfigError(f"[threads] {k} 格式不对（只认 {{title}} 和 {{topic}}）: {e}") from e
        for k in ("complex_min_replies", "recent_replies", "root_chars", "reply_chars"):
            if not _positive(getattr(self, k), integer=True):
                raise ConfigError(f"[threads] {k} 必须是正整数")

    def header(self, kind: str, title: str, topic_id: str) -> str:
        fmt = self.ask_header if kind == "ask" else self.report_header
        return fmt.format(title=title, topic=topic_id)


@dataclass
class TopicGroupConfig:
    """[topic_group]（7.51 长期跟踪的话题放进专门的话题群）。热加载，常驻进程下一轮生效，会话侧命令每次现读。

    没设 chat_id（默认）= 和以前一模一样：没有话题群，ask / report 照旧进私聊。
    设了 chat_id、enabled = true：会话第一次用一把钥匙发 ask / report 时带 --track、主人在私聊里整句回「放群里」、
    主人在群里新开顶层话题，这三种事进群（一件事一个话题，事项编号 T-<数字>）；群里置顶一张索引卡。
    设了 chat_id、enabled = false：照旧走私聊；群里的消息一律不分派（drop），也不往群里发。
    测试环境把 chat_id 设成测试用的话题群就行，一切照常发进它。"""
    enabled: bool = True
    chat_id: str = ""
    # 索引卡多久按证据对账一次（分钟）；中间有事项开出来 / 办完 / 重开时下一轮就对账
    index_refresh_minutes: float = 10
    # 没办完、这么多天没动静：索引卡「沉睡」
    stale_after_days: float = 7
    # 办完以后这么多小时没动静就归档（不再上索引卡）
    archive_after_hours: float = 72
    # 索引卡满 13 天重发一张以后，要不要取消旧卡的置顶。lark-cli 把取消置顶（im pins delete）标成 high-risk-write，
    # 要带 --yes 才执行；默认 false = 不取消，旧卡改成「已停用，请看最新置顶的那张」。打开前要主人同意（7.51 实现侧加的开关）
    unpin_old_index: bool = False

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[topic_group] enabled 必须是 true/false")
        if not isinstance(self.unpin_old_index, bool):
            raise ConfigError("[topic_group] unpin_old_index 必须是 true/false")
        if not isinstance(self.chat_id, str) or (self.chat_id and not self.chat_id.startswith("oc_")):
            raise ConfigError("[topic_group] chat_id 必须是 oc_ 开头的群 ID（sheepdog topic-group create 建的话题群）；留空 = 不用话题群")
        for k in ("index_refresh_minutes", "stale_after_days", "archive_after_hours"):
            if not _positive(getattr(self, k)):
                raise ConfigError(f"[topic_group] {k} 必须是正数")

    @property
    def active(self) -> bool:
        """配了话题群并且开着。"""
        return bool(self.enabled and self.chat_id)


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


# 会话跑了这些命令就报警（7.19 K3）：示例配置里写的也是这一份
DEFAULT_DANGER_PATTERNS = [
    r"builds triggers run",
    r"builds .*approve|:approve",
    r"add-iam-policy-binding|remove-iam-policy-binding",
    r"run services update-traffic",
    r"secrets versions (access|add)",
    r"lark-cli approval tasks (approve|reject)",
    r"gh pr merge",
    r"gh workflow run",
    r"kubectl (apply|delete)",
]

# 命中 danger_patterns 但同时命中这些的不报（7.19 K3 误报）：查帮助、知识库记录脚本、sheepdog 自己的命令
# （含 forward 的备注）、IM 收发、dry-run。对整条命令判断，不逐段拆
DEFAULT_IGNORE_PATTERNS = [
    r"(^|\s)--help(\s|$)",
    r"(^|\s)-h(\s|$)",
    r"context_memory\.py",
    r"pkm\.py",
    r"(^|[\s/])sheepdog\s",
    r"lark-cli\s+im\s",
    r"--dry-run",
]


@dataclass
class AuditConfig:
    """[audit]（7.19 K3）：每轮增量读这些会话的记录，命令命中 danger_patterns 就报警。热加载，下一轮生效。"""
    # 审计哪些会话（topic_id）
    topics: list[str] = field(default_factory=lambda: ["tp_bus"])
    # 正则列表，对整理过的命令文本做 search；空列表 = 不审计
    danger_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DANGER_PATTERNS))
    # 命中 danger_patterns 但也命中其中任一条的不报（整条命令判断）；空列表 = 不忽略
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS))
    # 命中后另外提醒这些会话「别重复操作，先核对现场」（topic_id）
    also_notify: list[str] = field(default_factory=list)

    def validate(self) -> None:
        for k in ("topics", "also_notify"):
            v = getattr(self, k)
            if not isinstance(v, list) or not all(isinstance(x, str) and x.startswith("tp_") for x in v):
                raise ConfigError(f"[audit] {k} 必须是 topic_id 列表（tp_ 开头）")
        for k in ("danger_patterns", "ignore_patterns"):
            v = getattr(self, k)
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                raise ConfigError(f"[audit] {k} 必须是非空字符串列表")
            for x in v:
                try:
                    re.compile(x)
                except re.error as e:
                    raise ConfigError(f"[audit] {k} 里的 {x!r} 不是有效正则: {e}") from e

    @property
    def patterns(self) -> list[re.Pattern]:
        return [re.compile(x) for x in self.danger_patterns]

    @property
    def ignores(self) -> list[re.Pattern]:
        return [re.compile(x) for x in self.ignore_patterns]


# 撞车检查认编号的规则（7.20）：审批单号（12 位数字）、PR #123、Linear 单号 ABC-123。示例配置里写的也是这一份
DEFAULT_KEY_PATTERNS = [
    r"(?<!\d)\d{12}(?!\d)",
    r"(?i)(?<![a-z0-9])PR\s*#\s*\d+",
    r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-\d+(?!\d)",
]


def _positive(v, integer: bool = False) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, int) and v > 0 if integer else isinstance(v, (int, float)) and v > 0


@dataclass
class DutyConfig:
    """[duty]（7.20 在岗表）：会话卡片怎么算、撞车怎么认、留言怎么限流。热加载，下一轮生效。"""
    # 从消息正文认编号的正则：有命名分组 key 就取它，否则取整段命中。认出的编号登记在别的会话名下、没结束 → 撞车提醒
    key_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_KEY_PATTERNS))
    # 批次在跑（或送出后会话没碰过）这么多分钟没有新动作 → 中断
    stall_minutes: float = 15
    # 批次送出后会话记录这么多分钟完全不动（文件没写过） → 失联
    lost_minutes: float = 30
    # 同一条命令 / 同一个工具调用连续这么多次 → 标「空转」
    spin_repeat: int = 3
    # 总线摘要最多几行（不空闲的会话每个一行，空闲的合成一行；超了先留卡住的）
    digest_max_lines: int = 15
    # 同一对会话 mail_window_minutes 内互发超过 mail_pair_limit 条 → 暂停这一对 mail_pause_minutes 分钟，并通知总线
    mail_pair_limit: int = 6
    mail_window_minutes: float = 10
    mail_pause_minutes: float = 30
    # 多久内的压缩点在卡片上标「刚被压缩」
    compacted_recent_minutes: float = 60
    # 路由事实（带编号的消息送到了哪个会话）多久内算数：撞车提醒、「手上的事」只看这么久以内送到过的
    routed_active_hours: float = 24
    # 会话自报、还开着的活，这么多小时没更新、而且这段时间会话一直空闲 → 标「可能过期」（不替它关掉）
    stale_hours: float = 4
    # 履历（回执摘要、做完的活、最近动作）、已结束的活、已读留言保留几天
    history_days: int = 7

    def validate(self) -> None:
        v = self.key_patterns
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ConfigError("[duty] key_patterns 必须是非空字符串列表")
        for x in v:
            try:
                re.compile(x)
            except re.error as e:
                raise ConfigError(f"[duty] key_patterns 里的 {x!r} 不是有效正则: {e}") from e
        for k in ("stall_minutes", "lost_minutes", "mail_window_minutes", "mail_pause_minutes",
                  "compacted_recent_minutes", "routed_active_hours", "stale_hours"):
            if not _positive(getattr(self, k)):
                raise ConfigError(f"[duty] {k} 必须是正数")
        for k in ("spin_repeat", "digest_max_lines", "mail_pair_limit", "history_days"):
            if not _positive(getattr(self, k), integer=True):
                raise ConfigError(f"[duty] {k} 必须是正整数")
        if self.lost_minutes < self.stall_minutes:
            raise ConfigError("[duty] lost_minutes 不能小于 stall_minutes（先中断、后失联）")

    @property
    def patterns(self) -> list[re.Pattern]:
        return [re.compile(x) for x in self.key_patterns]


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
class NotifyConfig:
    """[notify]：给主人的通知（在岗表中断 / 失联、attention 和恢复、危险命令报警、写锁提醒、登录失效）发到哪。

    file 留空（默认）= 经机器人私聊主人（要拉飞书、配了 self_open_id），行为不变。
    file 设了 = 一律追加写进这个文件（带时间和通知类型），不发飞书；测试环境（[source] enabled = false）用。
    相对路径相对本配置文件所在目录。下一轮生效。

    owner_card（7.48，默认开，热加载：常驻进程下一轮生效，ask / report / resolve 每次现读）：牧羊犬发给主人的消息
    （ask / report、系统通知、「待办」清单、结案那一句）发成卡片，正文的 markdown 原样渲染；关掉 = 照旧发纯文本。
    不管开没开，记账（sends、bot_outbox、给主人的通知账本）的正文都是纯文本。"""
    file: str = ""
    owner_card: bool = True

    def validate(self) -> None:
        if not isinstance(self.file, str):
            raise ConfigError("[notify] file 必须是字符串（文件路径）；留空 = 经机器人私聊主人")
        if not isinstance(self.owner_card, bool):
            raise ConfigError("[notify] owner_card 必须是 true/false（true = 发给主人的消息发成卡片）")


@dataclass
class IdentityConfig:
    """[identity]（7.26）：区分智能体（机器人）身份和主人本人身份。默认关；关着时行为和以前完全一样。

    开着时：
    - sheepdog run 另起一个常驻子进程，以机器人身份实时接收消息（lark-cli event consume im.message.receive_v1
      --as bot），和主人身份的每分钟搜索进同一个账本，按 message_id 只记一条，记下是哪个身份收到的；
    - 群里 @机器人 和 @主人 同等对待（路由原因 at_bot）；回复机器人发的消息和回复主人发的同等对待；
    - 机器人自己发的消息认作自己（self_bot）：不分派、不算主人发言；
    - 别人私聊机器人：不分派（drop，原因 bot_dm），机器人回一句 playbook/bot_dm.md 里的话，同一个人
      bot_dm_reply_hours 内只回一次；
    - sheepdog reply 按被回复的那条消息选身份：群里机器人在 → 机器人；私聊、机器人不在的群 → 主人本人。两个身份都带代回标记。
    enabled 改了要重启 sheepdog run（机器人接收是启动时起的）；sheepdog reply 每次现读配置。"""
    enabled: bool = False
    # 机器人的 open_id / app_id：留空 = sheepdog run 启动时现查（open_id 查 bot/v3/info，app_id 查 lark-cli config show），
    # 不写死。只在查不到时手填
    bot_open_id: str = ""
    bot_app_id: str = ""
    # 机器人在哪些群：用机器人身份的群列表判断，缓存这么多分钟
    bot_chats_cache_minutes: float = 10
    # 别人私聊机器人的自动回复：同一个人这么多小时内只回一次
    bot_dm_reply_hours: float = 24
    # 机器人接收子进程退出后重启的退避：从 restart_backoff_seconds 起每次翻倍，最多 restart_backoff_max_seconds
    restart_backoff_seconds: float = 5
    restart_backoff_max_seconds: float = 300

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[identity] enabled 必须是 true/false")
        if not isinstance(self.bot_open_id, str) or (self.bot_open_id and not self.bot_open_id.startswith("ou_")):
            raise ConfigError("[identity] bot_open_id 必须是 ou_ 开头的 open_id（留空 = 启动时现查）")
        if not isinstance(self.bot_app_id, str) or (self.bot_app_id and not self.bot_app_id.startswith("cli_")):
            raise ConfigError("[identity] bot_app_id 必须是 cli_ 开头的 app_id（留空 = 启动时现查）")
        for k in ("bot_chats_cache_minutes", "bot_dm_reply_hours", "restart_backoff_seconds",
                  "restart_backoff_max_seconds"):
            if not _positive(getattr(self, k)):
                raise ConfigError(f"[identity] {k} 必须是正数")
        if self.restart_backoff_max_seconds < self.restart_backoff_seconds:
            raise ConfigError("[identity] restart_backoff_max_seconds 不能小于 restart_backoff_seconds")


@dataclass
class SourceConfig:
    # false = 不拉飞书（测试环境用，7.18）：这时可以用 sheepdog inject 往账本里放合成消息
    enabled: bool = True
    # 只给真实飞书测试用，正式环境不要设（7.22）：拉到的消息只保留这些聊天的，其他聊天的消息不入账本、不点表情、
    # 不查已读。空 = 全部
    only_chat_ids: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("[source] enabled 必须是 true/false")
        v = self.only_chat_ids
        if not isinstance(v, list) or not all(isinstance(x, str) and x.startswith("oc_") for x in v):
            raise ConfigError("[source] only_chat_ids 必须是 oc_ 开头的聊天 ID 列表（测试用，正式环境不要设）")


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
    audit: AuditConfig = field(default_factory=AuditConfig)
    duty: DutyConfig = field(default_factory=DutyConfig)
    threads: ThreadsConfig = field(default_factory=ThreadsConfig)
    topic_group: TopicGroupConfig = field(default_factory=TopicGroupConfig)
    # 会话在自己终端里敲的 sheepdog 命令（提示词里所有「去跑 sheepdog …」的命令文字都用它）。
    # 测试环境改成一个只连测试配置的包装脚本的绝对路径，免得会话把操作写进正式账本。热加载，下一轮生效
    cli_name: str = "sheepdog"
    source: SourceConfig = field(default_factory=SourceConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    run: RunConfig = field(default_factory=RunConfig)
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

    @property
    def owner_chat_ids(self) -> list[str]:
        """「找主人」聊天：[escalation] chat_ids，加上 [threads] owner_chat_id（测试用的群，7.22）。"""
        out = list(self.escalation.chat_ids)
        if self.threads.owner_chat_id and self.threads.owner_chat_id not in out:
            out.append(self.threads.owner_chat_id)
        return out

    @property
    def owner_side_chat_ids(self) -> list[str]:
        """「找主人」那一侧的聊天（7.51）：找主人聊天，加上配了的话题群（[topic_group] chat_id，开没开都算）。
        这些聊天里主人说的话不当成别处的背景、不算防循环、不能当提问来源、不能用 sheepdog reply 代回；
        入账时先于普通路由处理（话题群走自己的分支）。"""
        out = self.owner_chat_ids
        tg = self.topic_group.chat_id
        if tg and tg not in out:
            out.append(tg)
        return out

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
    def notify_path(self) -> Path | None:
        """[notify] file：给主人的通知写到哪个文件；没设是 None（照旧经机器人私聊）。"""
        if not self.notify.file.strip():
            return None
        p = Path(self.notify.file.strip()).expanduser()
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
        try:
            text = p.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return ""   # 不存在 = 没有 overlay（原来的行为）
        except (OSError, UnicodeDecodeError) as e:
            # 改坏了（读不了 / 不是 UTF-8）：沿用上一份，从没读成功过就当空，不让组装 prompt 的那一轮失败（7.40）
            last = _OVERLAY_LAST_GOOD.get(p)
            log.warning("prompt overlay %s 读不了，%s: %s", p, "沿用上一份" if last is not None else "这一轮先当没有", e)
            return last or ""
        _OVERLAY_LAST_GOOD[p] = text
        return text


def _apply(dc, data: dict) -> None:
    """把 dict 浅层写入 dataclass，忽略未知键。"""
    for k, v in data.items():
        if hasattr(dc, k) and not isinstance(v, dict):
            setattr(dc, k, v)


def _apply_file(cfg: "Config", data: dict) -> None:
    """把整份配置文件（tomllib 的输出）写进 cfg；值的合法性由各节的 validate() 管。"""
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
    _apply(cfg.notify, data.get("notify", {}))
    _apply(cfg.identity, data.get("identity", {}))
    _apply(cfg.audit, data.get("audit", {}))
    _apply(cfg.duty, data.get("duty", {}))
    _apply(cfg.threads, data.get("threads", {}))
    _apply(cfg.topic_group, data.get("topic_group", {}))
    _apply(cfg.hooks, data.get("hooks", {}))
    _apply(cfg.run, data.get("run", {}))
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


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or default_config_path()
    cfg.config_path = path
    if path.exists():
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(toml_error_text(path, e)) from e
        except (OSError, UnicodeDecodeError) as e:   # 读不了 / 不是 UTF-8
            raise ConfigError(f"{path} 读不了：{e}") from e
        try:
            _apply_file(cfg, data)
        except (AttributeError, TypeError) as e:     # 某一节写成了单个值（如 session = 1）、路径写成了数字
            raise ConfigError(f"{path} 结构不对（某一节写成了单个值，或该写字符串的写成了别的）：{e}") from e
    cfg.session.validate()
    cfg.watch.validate()
    cfg.escalation.validate()
    cfg.ack.validate()
    cfg.bus.validate()
    cfg.owner_context.validate()
    cfg.loop_guard.validate()
    cfg.audit.validate()
    cfg.duty.validate()
    cfg.threads.validate()
    cfg.topic_group.validate()
    cfg.hooks.validate()
    cfg.routing.validate()
    cfg.run.validate()
    if not isinstance(cfg.cli_name, str) or not cfg.cli_name.strip():
        raise ConfigError("cli_name 必须是非空字符串")
    cfg.cli_name = cfg.cli_name.strip()
    cfg.source.validate()
    cfg.notify.validate()
    cfg.identity.validate()
    owner_chat = cfg.threads.owner_chat_id
    if owner_chat and cfg.source.only_chat_ids and owner_chat not in cfg.source.only_chat_ids:
        raise ConfigError(f"[threads] owner_chat_id {owner_chat} 不在 [source] only_chat_ids 里：找主人的群拉不到，"
                          "主人的回复送不回会话")
    tg = cfg.topic_group.chat_id
    if tg and tg in cfg.owner_chat_ids:
        raise ConfigError(f"[topic_group] chat_id {tg} 和找主人聊天是同一个：话题群要另建一个")
    if tg and cfg.source.only_chat_ids and tg not in cfg.source.only_chat_ids:
        raise ConfigError(f"[topic_group] chat_id {tg} 不在 [source] only_chat_ids 里：话题群拉不到，主人在群里说的话送不回会话")
    if owner_chat and not cfg.escalation.header_regex:
        raise ConfigError("[threads] owner_chat_id 已配置但 [escalation] header_regex 为空：ask 发的「需要你定」将无法识别")
    for h in cfg.hosts.values():
        h.validate()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
    return cfg
