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
from datetime import datetime, timedelta, timezone

from ..models import Mention, Message
from . import SourceError

log = logging.getLogger("sheepdog")


class LarkTimeout(SourceError):
    """lark-cli 超时：写操作（发消息）超时 = 不确定发出去没有，只能带同一个防重编号重试。"""

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
        message_position=str(raw.get("message_position") or ""),
        thread_position="" if raw.get("thread_message_position") is None else str(raw.get("thread_message_position")),
        raw=raw,
    )


def _event_time(ms: str, tz: str) -> str:
    """事件里的毫秒时间戳 → 本地时区 ISO，只到分钟（和搜索结果一样：发前检查、确认表情按分钟比先后）。"""
    try:
        sec = int(str(ms).strip()) / 1000
    except (TypeError, ValueError):
        return ""
    sign = -1 if tz.startswith("-") else 1
    hh, _, mm = tz.lstrip("+-").partition(":")
    try:
        offset = timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)))
    except ValueError:
        offset = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(sec, offset).replace(second=0, microsecond=0).isoformat(timespec="seconds")


def parse_event(raw: dict, tz: str = "+08:00") -> Message:
    """机器人身份实时接收的一条消息（lark-cli event consume im.message.receive_v1，7.26）。
    事件是拍平的：没有群名、发送人名字、租户、序号；正文是渲染好的文字（卡片是原始 JSON）；
    机器人发的 sender_type 是 bot（搜索结果里是 app）。"""
    mentions = []
    for m in raw.get("mentions") or []:
        mid = m.get("id") or ""
        key = m.get("key") or ""
        mentions.append(Mention(id=mid, name=m.get("name") or "", is_all=(mid in _AT_ALL_IDS or key in _AT_ALL_IDS)))
    content = raw.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    sender_type = raw.get("sender_type") or ""
    return Message(
        message_id=raw.get("message_id") or raw.get("id") or "",
        chat_id=raw.get("chat_id") or "",
        chat_name="",
        chat_type=raw.get("chat_type") or "",
        sender_id=raw.get("sender_id") or "",
        sender_name="",
        sender_type="app" if sender_type == "bot" else sender_type,
        content=content or "",
        msg_type=raw.get("message_type") or "",
        create_time=_event_time(raw.get("create_time", ""), tz),
        mentions=mentions,
        reply_to=raw.get("reply_to") or "",
        thread_id=raw.get("thread_id") or "",
        raw=raw,
    )


class LarkCliSource:
    def __init__(self, tz: str = "+08:00", binary: str = "lark-cli", max_pages: int = 100, retries: int = 3):
        self.tz = tz
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

    def _run(self, args: list[str], identity: str = "user") -> dict:
        delay = self.backoff
        last_err = ""
        for attempt in range(self.retries):
            try:
                proc = subprocess.run([self.binary, *args, "--as", identity], capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired as e:
                raise LarkTimeout(f"lark-cli 超时 {args[:2]}（{e.timeout:g} 秒）") from e
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

    def _chat_ids(self, extra: list[str], identity: str = "user") -> set[str]:
        ids: set[str] = set()
        token = ""
        for _ in range(self.max_pages):
            args = ["im", "+chat-list", "--page-size", "100", *extra]
            data = self._run(args + (["--page-token", token] if token else []), identity)
            ids |= {c.get("chat_id") for c in data.get("chats") or [] if c.get("chat_id")}
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        return ids

    def muted_chat_ids(self) -> set[str]:
        all_ids = self._chat_ids([])
        unmuted = self._chat_ids(["--exclude-muted"])
        return all_ids - unmuted

    # ---------- 机器人身份（7.26），只读 ----------
    def bot_chat_ids(self) -> set[str]:
        """机器人在哪些群（+chat-list --as bot，翻完所有页）。"""
        return self._chat_ids([], "bot")

    def chat_name(self, chat_id: str, identity: str = "bot") -> str:
        """群名（只读：lark-cli im chats get --chat-id，GET /open-apis/im/v1/chats/:chat_id，--help 标的 Risk: read，
        user / bot 两个身份都支持，调用方要在群里）。只试一次、15 秒超时：机器人事件那条路补群名用（事件和 mget 都不带群名），
        查不到由调用方兜底，不能挡住入账。"""
        data = self._run_once(["im", "chats", "get", "--chat-id", chat_id], identity, timeout=15)
        name = self._find(data, "name")
        return name.strip() if isinstance(name, str) else ""

    def bot_open_id(self) -> str:
        """机器人自己的 open_id（bot/v3/info）。"""
        data = self._run(["api", "GET", "/open-apis/bot/v3/info"], "bot")
        oid = self._find(data, "open_id") or (data.get("bot") or {}).get("open_id")
        if not oid:
            raise SourceError(f"bot/v3/info 没返回 open_id: {json.dumps(data, ensure_ascii=False)[:300]}")
        return str(oid)

    def bot_app_id(self) -> str:
        """lark-cli 绑定的应用 app_id（lark-cli config show）：搜索结果里机器人发的消息，发送人是它。"""
        try:
            proc = subprocess.run([self.binary, "config", "show"], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            raise LarkTimeout(f"lark-cli config show 超时（{e.timeout:g} 秒）") from e
        out = proc.stdout or ""
        end = out.rfind("}")
        try:
            data = json.loads(out[out.find("{"): end + 1]) if end >= 0 else {}
        except json.JSONDecodeError:
            data = {}
        app = (data or {}).get("appId") or ""
        if not app:
            raise SourceError(f"lark-cli config show 没有 appId: {out[:200]}")
        return str(app)

    def read_status(self, message_ids: list[str]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for i in range(0, len(message_ids), 50):
            chunk = message_ids[i : i + 50]
            data = self._run(["im", "+messages-read-status", "--message-ids", ",".join(chunk)])
            for item in data.get("items") or []:
                result[item["message_id"]] = bool(item.get("is_read"))
        return result

    # ---------- 写：确认表情（7.9），默认 user 身份，只试一次不重试 ----------
    def _run_once(self, args: list[str], identity: str = "user", timeout: float = 60) -> dict:
        if identity not in ("user", "bot"):
            raise SourceError(f"未知身份 {identity!r}")
        try:
            proc = subprocess.run([self.binary, *args, "--as", identity], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise LarkTimeout(f"lark-cli 超时 {args[:3]}（{e.timeout:g} 秒）") from e
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

    # 飞书消息的防重编号最长 50 个字符；同一个编号 1 小时内只发出一条（lark-cli im +messages-send --idempotency-key）
    IDEMPOTENCY_KEY_MAX = 50

    @staticmethod
    def _body_args(text: str, card: dict | None) -> list[str]:
        """消息内容：card 不为空发卡片（--msg-type interactive --content <卡片 JSON>，同 reply_to），否则纯文本。"""
        if card is not None:
            return ["--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False)]
        return ["--text", text]

    def _idem_args(self, key: str) -> list[str]:
        if not key:
            return []
        if len(key) > self.IDEMPOTENCY_KEY_MAX:
            raise SourceError(f"防重编号超过 {self.IDEMPOTENCY_KEY_MAX} 个字符: {key!r}")
        return ["--idempotency-key", key]

    def send_text(self, user_id: str, text: str, identity: str = "bot", idempotency_key: str = "",
                  card: dict | None = None) -> None:
        """经机器人给一个人发一条私聊（给主人的通知）。只试一次：失败由调用方（给主人的通知账本）按退避重试，
        重试时带同一个 idempotency_key，飞书按它去重。card 不为空发卡片（7.48），否则纯文本。"""
        args = ["im", "+messages-send", "--user-id", user_id, *self._body_args(text, card)]
        args += self._idem_args(idempotency_key)
        self._run_once(args, identity)

    # ---------- 写：替会话发消息（7.22）。只试一次：重试由调用方带同一个防重编号重来，飞书只发一条 ----------
    def send_to_user(self, user_id: str, text: str, idem_key: str, identity: str = "bot",
                     card: dict | None = None) -> dict:
        """私聊一个人（ask / report 经机器人找主人）。返回 {message_id, chat_id, thread_id}。card 不为空发卡片（7.48）。"""
        return self._sent(self._run_once(["im", "+messages-send", "--user-id", user_id, *self._body_args(text, card),
                                          *self._idem_args(idem_key)], identity))

    def send_to_chat(self, chat_id: str, text: str, idem_key: str, identity: str = "bot",
                     card: dict | None = None) -> dict:
        """发进一个群（[threads] owner_chat_id，真实飞书测试用）。返回 {message_id, chat_id, thread_id}。
        card 不为空发卡片（7.48）。"""
        return self._sent(self._run_once(["im", "+messages-send", "--chat-id", chat_id, *self._body_args(text, card),
                                          *self._idem_args(idem_key)], identity))

    def reply_to(self, message_id: str, *, identity: str, idem_key: str, text: str = "", card: dict | None = None,
                 in_thread: bool = False) -> dict:
        """回复一条消息：in_thread = 回到它的话题里（不出现在主聊天）；card = 卡片（interactive），否则纯文本。"""
        args = ["im", "+messages-reply", "--message-id", message_id, *self._body_args(text, card)]
        if in_thread:
            args.append("--reply-in-thread")
        args += ["--idempotency-key", idem_key]
        return self._sent(self._run_once(args, identity))

    def _sent(self, data: dict) -> dict:
        mid = self._find(data, "message_id")
        if not mid:
            raise SourceError(f"发送没返回 message_id: {json.dumps(data, ensure_ascii=False)[:300]}")
        return {"message_id": str(mid), "chat_id": str(self._find(data, "chat_id") or ""),
                "thread_id": str(self._find(data, "thread_id") or "")}

    # ---------- 读：话题、发前检查（7.22），user 身份，只读 ----------
    def thread_messages(self, thread: str, order: str = "asc", max_pages: int = 10) -> list[Message]:
        """一个话题里的回复（thread 可以是 omt_ 话题编号，也可以是开头那条的 om_ 消息号）。
        实测（10-12 22:5x）：返回只有话题里的回复，不含开头那条。"""
        args = ["im", "+threads-messages-list", "--thread", thread, "--order", order, "--page-size", "50",
                "--format", "json", "--no-reactions"]
        out: list[Message] = []
        token = ""
        for _ in range(max_pages):
            data = self._run(args + (["--page-token", token] if token else []))
            out += [parse_message(m, self.tz) for m in data.get("messages") or data.get("items") or []]
            token = data.get("page_token") or ""
            if not data.get("has_more") or not token:
                break
        return out

    def messages_by_id(self, message_ids: list[str]) -> list[Message]:
        """按消息号批量取（+messages-mget，一次最多 50 条）。有话题的开头那条会带 thread_id 和 thread_replies。"""
        out: list[Message] = []
        for i in range(0, len(message_ids), 50):
            chunk = message_ids[i: i + 50]
            data = self._run(["im", "+messages-mget", "--message-ids", ",".join(chunk), "--format", "json",
                              "--no-reactions"])
            out += [parse_message(m, self.tz) for m in data.get("messages") or data.get("items") or []]
        return out

    def chat_messages_since(self, chat_id: str, sender_id: str, start_iso: str, max_pages: int = 5) -> list[Message]:
        """这个聊天里、这个人从 start 起发的消息（搜索接口，不会把消息标成已读）。没查完（翻页到上限仍有）就报错，
        不当成「没有」。"""
        args = ["im", "+messages-search", "--chat-id", chat_id, "--sender", sender_id, "--start", start_iso,
                "--page-size", "50", "--format", "json", "--no-reactions"]
        out: list[Message] = []
        token = ""
        for _ in range(max_pages):
            data = self._run(args + (["--page-token", token] if token else []))
            out += [parse_message(m, self.tz) for m in data.get("messages") or []]
            token = data.get("page_token") or ""
            more = bool(data.get("has_more"))
            if not more:
                return out
            if not token:
                break
        if out:
            return out  # 查到了就够判断；没查完的部分只会更多
        raise SourceError(f"发前检查没查完（{chat_id} 从 {start_iso} 起翻页到上限仍有更多）")

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
