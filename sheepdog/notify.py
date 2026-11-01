"""给主人的通知写进文件（[notify] file，测试环境用）。

测试 sidecar 不拉飞书（[source] enabled = false）时没有发私聊的渠道，原来给主人的通知（羊群看板判中断 / 失联、
attention 和恢复、危险命令报警、写锁提醒、登录失效）只记日志，测试时没法检查。设了 [notify] file，
这些通知一律追加写进这个文件，不走飞书；没设时行为不变。

文件格式（人和程序都能读）：每条一段，头一行是时间（配置的时区，+08:00 标「北京时间」）和通知类型，
下面是原文，最后空一行：

    ### 2026-10-12 14:03:05（北京时间）· 羊群看板通知
    🐕 汇报 · 会话中断了 [tp_bus]
    ...

read_notices() 按这个格式读回来。

文件模式不走「给主人的通知账本」（owner_notices，不重试）：写文件是本机操作，失败了记日志就是。
正式环境经机器人私聊用 OwnerDM：带防重编号发，发不出去由 Dispatcher 的通知账本按退避重试；7.48 起发成卡片
（[notify] owner_card），测试环境（[threads] owner_chat_id）发进测试群。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .engine import _tz

HEADER = "### "
_HEADER_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})（([^）]*)）· (.*)$")


class FileNotifier:
    """Dispatcher 的 notifier：notify(text, what) 追加一条到文件。写不进去抛 OSError（由调用方记日志）。"""

    # 不走给主人的通知账本、不重试（[notify] file 照旧）
    ledger = False

    def __init__(self, path: Path, tz_offset: str = "+08:00"):
        self.path = Path(path)
        self.tz_offset = tz_offset

    def notify(self, text: str, what: str = "") -> None:
        tz, label = _tz(self.tz_offset)
        at = datetime.now().astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
        body = (text or "").rstrip("\n")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"{HEADER}{at}（{label}）· {what or '通知'}\n{body}\n\n")
            f.flush()

    def __call__(self, text: str) -> None:
        self.notify(text, "")

    def __repr__(self) -> str:
        return f"FileNotifier({str(self.path)!r})"


def read_notices(path: Path) -> list[dict]:
    """读回通知文件：[{at（字符串）, tz（时区叫法）, what（通知类型）, text（原文）}]，按写入顺序。文件不存在就是空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    cur: dict | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if cur is not None:
                out.append(cur)
            cur = {"at": m.group(1), "tz": m.group(2), "what": m.group(3), "lines": []}
        elif cur is not None:
            cur["lines"].append(line)
    if cur is not None:
        out.append(cur)
    for n in out:
        lines = n.pop("lines")
        while lines and not lines[-1]:
            lines.pop()
        n["text"] = "\n".join(lines)
    return out


class OwnerDM:
    """经机器人发给主人（没设 [notify] file 时）。send(text, idempotency_key)：发一次，失败抛异常，由 Dispatcher 的
    通知账本按退避重试；每次重试带同一个防重编号（飞书 1 小时内同一个编号只发出一条）。

    7.48：
    - cfg 的 [notify] owner_card 开着就发成卡片（第一行进 header，其余 markdown 原样渲染），关着照旧纯文本；
      通知账本里存的正文仍是纯文本，每次发送现拼卡片。卡片太大、飞书拒了卡片，同一个编号改发纯文本（outbound.send_owner）。
    - 测试环境（设了 [threads] owner_chat_id）发进那个测试群，不私聊主人（和 ask / report 的测试做法一致），
      免得测试牧羊犬的通知进了主人和机器人的真私聊、被正式牧羊犬读到。
    配置热加载：常驻进程每轮按新配置重建 OwnerDM（cli._reload_config）。"""

    def __init__(self, source, open_id: str, cfg=None, store=None):
        self.source = source
        self.open_id = open_id
        self.cfg = cfg
        self.store = store

    @property
    def chat_id(self) -> str:
        from . import alert_group as ag
        if self.cfg is None:
            return ""
        return ag.resolve_alert_chat_id(self.cfg, self.store) or (self.cfg.threads.owner_chat_id or "")

    def send(self, text: str, idempotency_key: str = "", reply_to: str = "", in_thread: bool = False) -> None:
        """reply_to（7.52 回执带回）：回复主人的那一条（in_thread = 回到它的话题里），不新起一条。"""
        from . import alert_group as ag
        from .outbound import owner_content, send_owner
        if self.cfg is not None:
            plain, card = owner_content(self.cfg, text)
        else:
            plain, card = text, None
        target_chat = ag.ensure_alert_group(self.cfg, self.store, self.source) or self.chat_id

        def one(t: str, c: dict | None):
            kw = {"card": c} if c is not None else {}
            if reply_to:
                return self.source.reply_to(reply_to, identity="bot", idem_key=idempotency_key, text=t,
                                            in_thread=in_thread, **kw)
            if target_chat:
                return self.source.send_to_chat(target_chat, t, idempotency_key, "bot", **kw)
            if idempotency_key:
                return self.source.send_text(self.open_id, t, "bot", idempotency_key=idempotency_key, **kw)
            return self.source.send_text(self.open_id, t, "bot", **kw)

        send_owner(one, plain, card)

    def __call__(self, text: str) -> None:
        self.send(text)

    def __repr__(self) -> str:
        return f"OwnerDM(经机器人发进测试群 {self.chat_id})" if self.chat_id else "OwnerDM(经机器人私聊主人)"
