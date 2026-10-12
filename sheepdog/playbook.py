"""业务规则 playbook（7.6）：代码只放机制，业务文字全部写在个人配置目录的 md 文件里。

- 目录：配置键 playbook_dir（默认 playbook/，相对配置文件目录）
- 占位符写成 {{名字}}，一遍正则替换；未知占位符原样保留；替换进去的值里即使含 {{...}} 也不会再被替换
- 每次组装 prompt 时现读文件：改完下一批生效，不用重启
- 文件缺失或为空时该段为空，`sheepdog doctor` 列出缺哪些
"""

from __future__ import annotations

import re
from pathlib import Path

# 文件名 → 用在哪（文件名是和架构侧的约定，不要改）
PLAYBOOK_FILES: dict[str, str] = {
    "common.md": "所有会话（总线、新建、接管、接手）的业务规则",
    "bus.md": "总线 bootstrap，可用 {{roster}}",
    "onboarding.md": "接管现有会话（sheepdog 新建的会话也用它），可用 {{duty}} {{chats}} {{authority}} {{self_polling_section}}",
    "retire_self_polling.md": "retire_self_polling = true 时渲染进 {{self_polling_section}}",
    "authority_default.md": "authority 为空时代替 {{authority}}",
    "retire.md": "给前任的退休通知，可用 {{successor_title}} {{batch_id}}",
    "successor.md": "接手会话开场附加，可用 {{predecessor_id}} {{predecessor_transcript}} {{predecessor_dir}}",
    "nudge_remind.md": "watch 提醒，可用 {{note}} {{person}} {{minutes}}",
    "nudge_expire.md": "watch 到期，可用 {{note}} {{person}} {{minutes}}",
    "batch_footer.md": "每批信号末尾",
    # 安全（7.7）
    "security.md": "所有开场的最前面（排在 common.md 之前）",
    "security_banner.md": "被安全规则标记的消息正文前的警示，可用 {{tags}} {{notes}} {{action}}",
    "security_footer.md": "每批信号末尾，batch_footer 之前",
    # 命令行宿主（7.18）
    "cli_host.md": "只发给 Codex / Claude 会话（排在 common.md 之后）：回执块怎么写、发不了 IM 时怎么办，可用 {{host}}",
    "host_locked.md": "命令行会话连续写锁冲突满 60 分钟时，给总线的提示里附的说明，可用 {{host}} {{since}}",
    # 护栏（7.19）
    "rules_after_compaction.md": "检测到宿主压缩、下一批重推完整规则时，规则前的那句话（缺了用代码里的一句默认话）",
    "audit_offender.md": "会话跑了危险命令时，给它的系统提示里附的话（每次报警都发），可用 {{commands}} "
                         "{{alert_count}}（这个会话 10 分钟内第几次报警）",
    "audit_notify.md": "会话跑了危险命令时，给 [audit] also_notify 会话的提示里附的话，"
                       "可用 {{commands}} {{offender_title}} {{offender_topic}} {{alert_count}}",
}

# 可选文件：缺了用代码里的默认值，不算缺（doctor 不报）
OPTIONAL_PLAYBOOK_FILES: dict[str, str] = {
    "agent_sender_label.md": "机器人 / Agent 发来的消息上的标注（回不回、怎么回）；缺了用代码里的默认值「🤖 来自机器人 / Agent」",
    # 会话卡在 attention（7.21）：事实由代码拼，这里只放对人的说明；缺了只发事实
    "attention_notice.md": "会话进入 attention 时私聊主人的通知末尾附的话，可用 {{host}} {{reason}} {{since}} {{queued}} "
                           "{{auto_recover}}（yes / no）",
    "attention_recovered.md": "会话自动恢复时私聊主人的通知末尾附的话，可用 {{host}} {{evidence}} {{queued}}",
    # 在岗表（7.20）：事实由代码拼，这里只放对会话、对人的说明；缺了只发事实（摘要标题缺了用代码里的一句）
    "duty_collision.md": "撞车提醒（消息里的编号最近送到过别的会话，或在别的会话手上）后面附的话，可用 {{key}} {{owner_title}} "
                         "{{owner_topic}} {{work_state}} {{run_state}} {{action}} {{ago}} {{basis}}（路由 / 会话自报 / 自动）",
    "duty_mail.md": "会话留言（sheepdog tell）后面附的话，可用 {{from_topic}} {{from_title}} {{key}}",
    "duty_digest.md": "总线批次里在岗表摘要的标题（在岗表有变化才附），可用 {{count}}",
    "duty_stuck.md": "会话判为中断 / 失联时私聊主人的通知末尾附的话，可用 {{host}} {{state}} {{detail}} {{queued}}",
}

# 每批末尾附的红线（7.19 K1）：reminders/<key>.md，key 是名册的 key，总线是 bus，总线新开的会话用它自己的 key。
# 可选，不在 PLAYBOOK_FILES 里（缺了不算缺），doctor 单独列出哪些会话有
REMINDERS_DIR = "reminders"

_PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
# <!-- ... --> 是写给维护者看的注释（比如某个数字来自哪个版本），渲染时去掉，不发给会话和主人
_COMMENT = re.compile(r"<!--.*?-->", re.S)


class Playbook:
    def __init__(self, directory: Path, base_vars: dict[str, str] | None = None):
        self.directory = directory
        # 通用占位符里与具体会话无关的部分（代回前缀、等待分钟数），由配置决定
        self.base_vars = dict(base_vars or {})

    @classmethod
    def from_config(cls, cfg) -> "Playbook":
        w = cfg.watch
        return cls(cfg.playbook_path, {
            "reply_prefix": cfg.session.reply_prefix or "",
            "reply_suffix": cfg.session.reply_suffix or "",
            "watch_remind_minutes": "/".join(str(m) for m in w.remind_minutes),
            "watch_expire_minutes": str(w.expire_minutes),
            # 会话在终端里敲的 sheepdog 命令（cli_name，已加好 shell 引号）；playbook 里写 {{cli}}
            "cli": cfg.cli_command,
        })

    @property
    def cli(self) -> str:
        """给会话看的命令名：默认 sheepdog，测试环境可以是一个只连测试配置的包装脚本。"""
        return self.base_vars.get("cli") or "sheepdog"

    def read(self, name: str) -> str:
        try:
            return (self.directory / name).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return ""

    def render(self, name: str, **variables: str) -> str:
        """读 name 并替换占位符。通用占位符 session_title、topic_id 由调用方按会话传入。"""
        text = _COMMENT.sub("", self.read(name))
        if not text.strip():
            return ""
        values = {**self.base_vars, **{k: "" if v is None else str(v) for k, v in variables.items()}}
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text).strip()

    def reminder_keys(self) -> list[str]:
        """reminders/ 下有内容的红线文件对应的 key。"""
        d = self.directory / REMINDERS_DIR
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.md") if p.is_file() and _COMMENT.sub("", self.read(
            f"{REMINDERS_DIR}/{p.name}")).strip())

    def missing(self) -> list[str]:
        return [n for n in PLAYBOOK_FILES if not self.read(n).strip()]
