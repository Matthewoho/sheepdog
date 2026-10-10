"""Lark/飞书消息源：通过 lark-cli（user 身份）增量拉取全部会话消息。

- 拉取：`im +messages-search`，空 query + 时间范围 = 一次覆盖所有会话
- 已读：`im +messages-read-status`（仅标注，不影响路由）
- 免打扰：`im +chat-list` 全量 与 `--exclude-muted` 的差集
- 回复我：`im +messages-mget` 查被回复消息的发送人
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

from ..models import Mention, Message
from . import SourceError

log = logging.getLogger("sheepdog")

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
        sender_tenant_key=sender.get("tenant_key") or "",
        raw=raw,
    )


class LarkCliSource:
    def __init__(self, tz: str = "+08:00", binary: str = "lark-cli", max_pages: int = 100, retries: int = 3, chat_ids: list[str] | None = None):
        self.tz = tz
        self.chat_ids = chat_ids
        self.binary = shutil.which(binary) or binary
        self.max_pages = max_pages
        # 可重试错误的首次退避秒数，之后每次翻倍
        self.backoff = 2.0
        # 最近一次 fetch_since 是否没拉完（7.16）：has_more 却缺 page_token，或翻到上限仍 has_more
        self.last_fetch_partial = False
        self.retries = retries

    # ---------- 底层调用 ----------
    @staticmethod
    def _retryable(err: dict) -> bool:
        """限流、标了 retryable 的，以及网络类错误（如 TLS handshake timeout）都按退避重试。"""
        subtype = str(err.get("subtype") or "").lower()
        return bool(err.get("retryable")) or subtype == "rate_limit" or err.get("type") == "network" or "timeout" in subtype

    def _run(self, args: list[str]) -> dict:
        delay = self.backoff
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
            if self._retryable(err) and attempt + 1 < self.retries:
                log.warning("lark-cli %s 第 %d 次失败（%s），%.0f 秒后重试", " ".join(args[:2]), attempt + 1, last_err, delay)
                time.sleep(delay)
                delay *= 2
                continue
            break
        raise SourceError(f"lark-cli 调用失败 {args[:2]}: {last_err}")

    # ---------- Source 接口 ----------
    def fetch_since(self, start_iso: str, end_iso: str | None = None) -> list[Message]:
        args = ["im", "+messages-search", "--start", start_iso, "--page-size", "50", "--format", "json", "--no-reactions"]
        if self.chat_ids is not None:
            if not self.chat_ids:
                self.last_fetch_partial = False
                return []
            args += ["--chat-id", ",".join(sorted(set(self.chat_ids)))]
        if end_iso:
            args += ["--end", end_iso]
        out: list[Message] = []
        token = ""
        more = False
        for _ in range(self.max_pages):
            data = self._run(args + (["--page-token", token] if token else []))
            out += [parse_message(m, self.tz) for m in data.get("messages") or []]
            token = data.get("page_token") or ""
            more = bool(data.get("has_more"))
            if not more or not token:
                break
        self.last_fetch_partial = more
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

    # ---------- 写：确认表情（7.9），默认 user 身份，只试一次不重试 ----------
    def _run_once(self, args: list[str], identity: str = "user") -> dict:
        if identity not in ("user", "bot"):
            raise SourceError(f"未知身份 {identity!r}")
        proc = subprocess.run([self.binary, *args, "--as", identity], capture_output=True, text=True, timeout=60)
        out = proc.stdout.strip() or proc.stderr.strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise SourceError(f"lark-cli 输出无法解析 rc={proc.returncode}: {out[:300]}")
        # 既接受 {"ok":..,"data":..} 外层，也接受原始 API 的 {"code":0,"data":..} 或直接返回数据
        if not isinstance(data, dict):
            raise SourceError(f"lark-cli 输出不是对象: {out[:300]}")
        if data.get("ok") is False or data.get("error") or (data.get("code") not in (None, 0)) or proc.returncode != 0:
            err = data.get("error") or data.get("msg") or out[:300]
            raise SourceError(f"lark-cli 调用失败 {args[:3]}: {err}")
        return data

    @staticmethod
    def _find(data: dict, key: str):
        cur = data
        for _ in range(3):
            if not isinstance(cur, dict):
                return None
            if key in cur:
                return cur[key]
            cur = cur.get("data")
        return None

    def add_reaction(self, message_id: str, emoji_type: str, identity: str = "user") -> str:
        data = self._run_once(["im", "reactions", "create",
                               "--params", json.dumps({"message_id": message_id}),
                               "--data", json.dumps({"reaction_type": {"emoji_type": emoji_type}})], identity)
        rid = self._find(data, "reaction_id")
        if not rid:
            raise SourceError(f"reactions create 没返回 reaction_id: {json.dumps(data, ensure_ascii=False)[:300]}")
        return str(rid)

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        self._run_once(["im", "reactions", "delete",
                        "--params", json.dumps({"message_id": message_id, "reaction_id": reaction_id})])

    # ---------- 读：消息上的表情（7.13 补充），user 身份，只读 ----------
    REACTION_BATCH = 20

    def reactions_of(self, message_ids: list[str]) -> dict[str, list[dict]]:
        """每条消息的表情（每条最多取第一页 10 个）：{message_id: [{emoji_type, operator_id, reaction_id}]}。"""
        out: dict[str, list[dict]] = {}
        for i in range(0, len(message_ids), self.REACTION_BATCH):
            chunk = message_ids[i: i + self.REACTION_BATCH]
            data = self._run_once(["im", "reactions", "batch_query",
                                   "--params", json.dumps({"user_id_type": "open_id"}),
                                   "--data", json.dumps({"queries": [{"message_id": m} for m in chunk],
                                                         "page_size_per_message": 10})])
            for d in self._find(data, "success_msg_reaction_details") or []:
                items = []
                for it in d.get("message_reaction_items") or []:
                    op = it.get("operator") or {}
                    items.append({"emoji_type": it.get("emoji_type") or "",
                                  "operator_id": op.get("operator_id") or op.get("open_id") or "",
                                  "reaction_id": it.get("reaction_id") or ""})
                out[d.get("message_id", "")] = items
        return out

    def senders_of(self, message_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for i in range(0, len(message_ids), 50):
            chunk = message_ids[i : i + 50]
            data = self._run(["im", "+messages-mget", "--message-ids", ",".join(chunk), "--format", "json"])
            for m in data.get("messages") or data.get("items") or []:
                result[m.get("message_id", "")] = (m.get("sender") or {}).get("id", "")
        return result
