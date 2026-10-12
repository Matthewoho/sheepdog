"""给主人的通知写进文件（[notify] file，测试环境用）。

测试 sidecar 不拉飞书（[source] enabled = false）时没有发私聊的渠道，原来给主人的通知（在岗表中断 / 失联、
attention 和恢复、危险命令报警、写锁提醒、登录失效）只记日志，测试时没法检查。设了 [notify] file，
这些通知一律追加写进这个文件，不走飞书；没设时行为不变。

文件格式（人和程序都能读）：每条一段，头一行是时间（配置的时区，+08:00 标「北京时间」）和通知类型，
下面是原文，最后空一行：

    ### 2026-10-12 14:03:05（北京时间）· 在岗表通知
    🐕 汇报 · 会话中断了 [tp_bus]
    ...

read_notices() 按这个格式读回来。

文件模式不走「给主人的通知账本」（owner_notices，不重试）：写文件是本机操作，失败了记日志就是。
正式环境经机器人私聊用 OwnerDM：带防重编号发，发不出去由 Dispatcher 的通知账本按退避重试。
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
    """经机器人私聊主人（没设 [notify] file 时）。send(text, idempotency_key)：发一次，失败抛异常，由 Dispatcher 的
    通知账本按退避重试；每次重试带同一个防重编号（飞书 1 小时内同一个编号只发出一条）。"""

    def __init__(self, source, open_id: str):
        self.source = source
        self.open_id = open_id

    def send(self, text: str, idempotency_key: str = "") -> None:
        if idempotency_key:
            self.source.send_text(self.open_id, text, "bot", idempotency_key=idempotency_key)
        else:
            self.source.send_text(self.open_id, text, "bot")

    def __call__(self, text: str) -> None:
        self.send(text)

    def __repr__(self) -> str:
        return "OwnerDM(经机器人私聊主人)"
