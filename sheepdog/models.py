"""与具体 IM 平台无关的消息模型。Source 负责把平台数据转换成这里的结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Mention:
    id: str
    name: str = ""
    # 是否为 @所有人
    is_all: bool = False


@dataclass
class Message:
    message_id: str
    chat_id: str
    chat_name: str
    # p2p | group
    chat_type: str
    sender_id: str
    sender_name: str
    # user | app | bot | system
    sender_type: str
    content: str
    msg_type: str
    # 本地时区 ISO 字符串
    create_time: str
    mentions: list[Mention] = field(default_factory=list)
    reply_to: str = ""
    thread_id: str = ""
    link: str = ""
    deleted: bool = False
    updated: bool = False
    update_time: str = ""
    # 发送方所属租户（lark sender.tenant_key），安全规则据此判断外部人
    sender_tenant_key: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_p2p(self) -> bool:
        return self.chat_type == "p2p"

    @property
    def is_bot_sender(self) -> bool:
        return self.sender_type in ("app", "bot", "system")
