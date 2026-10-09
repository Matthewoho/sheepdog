"""Lark/飞书消息源：通过 lark-cli（user 身份）增量拉取全部会话消息。

- 拉取：`im +messages-search`，空 query + 时间范围 = 一次覆盖所有会话
- 已读：`im +messages-read-status`（仅标注，不影响路由）
- 免打扰：`im +chat-list` 全量 与 `--exclude-muted` 的差集
- 回复我：`im +messages-mget` 查被回复消息的发送人
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from ..models import Mention, Message
from . import SourceError

# 飞书 @所有人 在 mentions 里的标识
_AT_ALL_IDS = {"all", "@_all"}


def _norm_time(t: str, tz: str) -> str:
    """lark-cli 返回 'YYYY-MM-DD HH:MM'（本地时区）→ ISO8601。"""
    t = (t or "").strip()
    if not t:
        return ""
    if "T" in t:
        return t
    if len(t) == 16:
        t += ":00"
    return t.replace(" ", "T") + tz


def parse_message(raw: dict, tz: str = "+08:00") -> Message:
    sender = raw.get("sender") or {}
    mentions = []
    for m in raw.get("mentions") or []:
        mid = m.get("id") or ""
        key = m.get("key") or ""
        mentions.append(Mention(id=mid, name=m.get("name") or "", is_all=(mid in _AT_ALL_IDS or key in _AT_ALL_IDS)))
    content = raw.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return Message(
        message_id=raw.get("message_id", ""),
        chat_id=raw.get("chat_id", ""),
        chat_name=raw.get("chat_name") or "",
        chat_type=raw.get("chat_type") or "",
        sender_id=sender.get("id") or "",
        sender_name=sender.get("name") or "",
        sender_type=sender.get("sender_type") or "",
        content=content or "",
        msg_type=raw.get("msg_type") or "",
        create_time=_norm_time(raw.get("create_time", ""), tz),
        mentions=mentions,
        reply_to=raw.get("reply_to") or raw.get("parent_id") or "",
        thread_id=raw.get("thread_id") or "",
        link=raw.get("message_app_link") or "",
        deleted=bool(raw.get("deleted")),
        updated=bool(raw.get("updated")),
        update_time=_norm_time(raw.get("update_time", ""), tz),
        raw=raw,
    )


class LarkCliSource:
    def __init__(self, tz: str = "+08:00", binary: str = "lark-cli", max_pages: int = 20, retries: int = 3):
        self.tz = tz
        self.binary = shutil.which(binary) or binary
        self.max_pages = max_pages
        self.retries = retries

    # ---------- 底层调用 ----------
    def _run(self, args: list[str]) -> dict:
        delay = 2.0
        last_err = ""
        for attempt in range(self.retries):
            proc = subprocess.run([self.binary, *args, "--as", "user"], capture_output=True, text=True, timeout=120)
            out = proc.stdout.strip() or proc.stderr.strip()
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                raise SourceError(f"lark-cli 输出无法解析: {out[:300]}")
            if data.get("ok"):
                return data.get("data") or {}
            err = data.get("error") or {}
            last_err = f"{err.get('type')}/{err.get('subtype')}: {err.get('message')}"
            # 仅对可重试错误退避（如 429）
            if err.get("retryable") or err.get("subtype") == "rate_limit":
                time.sleep(delay)
                delay *= 2
                continue
            break
        raise SourceError(f"lark-cli 调用失败 {args[:2]}: {last_err}")

    # ---------- Source 接口 ----------
    def fetch_since(self, start_iso: str, end_iso: str | None = None) -> list[Message]:
        args = ["im", "+messages-search", "--start", start_iso, "--page-size", "50", "--format", "json", "--no-reactions"]
        if end_iso:
            args += ["--end", end_iso]
        out: list[Message] = []
        token = ""
        for _ in range(self.max_pages):
            data = self._run(args + (["--page-token", token] if token else []))
            out += [parse_message(m, self.tz) for m in data.get("messages") or []]
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        return out

    def _chat_ids(self, extra: list[str]) -> set[str]:
        ids: set[str] = set()
        token = ""
        for _ in range(self.max_pages):
            args = ["im", "+chat-list", "--page-size", "100", *extra]
            data = self._run(args + (["--page-token", token] if token else []))
            ids |= {c.get("chat_id") for c in data.get("chats") or [] if c.get("chat_id")}
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        return ids

    def muted_chat_ids(self) -> set[str]:
        all_ids = self._chat_ids([])
        unmuted = self._chat_ids(["--exclude-muted"])
        return all_ids - unmuted

    def read_status(self, message_ids: list[str]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for i in range(0, len(message_ids), 50):
            chunk = message_ids[i : i + 50]
            data = self._run(["im", "+messages-read-status", "--message-ids", ",".join(chunk)])
            for item in data.get("items") or []:
                result[item["message_id"]] = bool(item.get("is_read"))
        return result

    def senders_of(self, message_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for i in range(0, len(message_ids), 50):
            chunk = message_ids[i : i + 50]
            data = self._run(["im", "+messages-mget", "--message-ids", ",".join(chunk), "--format", "json"])
            for m in data.get("messages") or data.get("items") or []:
                result[m.get("message_id", "")] = (m.get("sender") or {}).get("id", "")
        return result
