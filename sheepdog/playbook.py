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
}

_PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


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
        })

    def read(self, name: str) -> str:
        try:
            return (self.directory / name).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return ""

    def render(self, name: str, **variables: str) -> str:
        """读 name 并替换占位符。通用占位符 session_title、topic_id 由调用方按会话传入。"""
        text = self.read(name)
        if not text.strip():
            return ""
        values = {**self.base_vars, **{k: "" if v is None else str(v) for k, v in variables.items()}}
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text).strip()

    def missing(self) -> list[str]:
        return [n for n in PLAYBOOK_FILES if not self.read(n).strip()]
