"""SQLite 账本：消息、元数据（水位线等）、话题/session 注册表、投递记录。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import Mention, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id     TEXT PRIMARY KEY,
    chat_id        TEXT NOT NULL,
    chat_name      TEXT,
    chat_type      TEXT,
    sender_id      TEXT,
    sender_name    TEXT,
    sender_type    TEXT,
    sender_tenant_key TEXT,
    content        TEXT,
    msg_type       TEXT,
    create_time    TEXT,
    mentions_json  TEXT,
    reply_to       TEXT,
    thread_id      TEXT,
    link           TEXT,
    deleted        INTEGER DEFAULT 0,
    updated        INTEGER DEFAULT 0,
    update_time    TEXT,
    is_read        INTEGER,            -- NULL = 未查询
    route          TEXT,               -- self | drop | inbox | dispatch
    reason         TEXT,
    tags_json      TEXT,
    topic_id       TEXT,
    dispatch_state TEXT,               -- pending | delivered | acked | failed | missed（adopted 回执超时）
    batch_id       TEXT,
    inbox_cleared  INTEGER DEFAULT 0,
    note           TEXT,               -- 未用（早期转交说明，转达改为 sheepdog 消息）
    security_tags  TEXT,               -- 安全规则命中的 rule name 列表（JSON）
    security_action TEXT,              -- none | tag | hold（取最严）
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_route ON messages(route, dispatch_state);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, create_time);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    topic_id         TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    kind             TEXT,             -- bus | adopted（名册 managed）| known（名册 known，不投递）| retired（被接手的前任）
                                       -- | dynamic（总线临时新开，只在账本里，不写名册）
    duty             TEXT,             -- 一句话职责 + 边界，纠偏时修改
    conversation_id  TEXT,
    state            TEXT NOT NULL,
    anchors_json     TEXT DEFAULT '[]',
    summary          TEXT DEFAULT '',
    pending_batch_id TEXT,
    dispatched_at    TEXT,
    retries          INTEGER DEFAULT 0,
    onboarded_at     TEXT,             -- adopted 会话收到 onboarding 的时间，只发一次
    receipt_missed   INTEGER DEFAULT 0,  -- adopted 会话回执超时次数（不重投）
    spawned_at       TEXT,             -- 由 sheepdog spawn 新建的时间；名册 conversation_id 留空时以此为准
    origin_note      TEXT,             -- dynamic 会话的创建原因（new-session 的 --note）
    rules_hash       TEXT,             -- 上次送达的常驻规则指纹（7.11）；为空 = 没记录，下一批带上完整规则
    created_at       TEXT NOT NULL,
    last_active      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatches (
    batch_id    TEXT PRIMARY KEY,
    topic_id    TEXT NOT NULL,
    message_ids TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    state       TEXT NOT NULL,         -- sent | acked | failed | missed
    receipt     TEXT,
    error       TEXT
);

-- 等别人回复（7.4）：对方回复优先归给登记的 topic；15/30 分钟提醒，60 分钟到期
CREATE TABLE IF NOT EXISTS watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id     TEXT NOT NULL,
    person_id    TEXT NOT NULL,
    chat_id      TEXT,                 -- 空 = 对方在任何聊天里回复都算
    note         TEXT,
    started_at   TEXT NOT NULL,
    nudged_15_at TEXT,
    nudged_30_at TEXT,
    closed_at    TEXT,
    close_reason TEXT                  -- replied | expired | cancelled | topic_closed
);
CREATE INDEX IF NOT EXISTS idx_watch_person ON watches(person_id, closed_at);

-- 「需要你定」（7.8）：会话以 bot 身份找主人的提问；主人在 IM 上的回复按它投回对应 topic
CREATE TABLE IF NOT EXISTS escalations (
    message_id        TEXT PRIMARY KEY,  -- 机器人那条提问
    topic_id          TEXT NOT NULL,
    chat_id           TEXT,
    text              TEXT,
    asked_at          TEXT NOT NULL,
    answered_at       TEXT,
    answer_message_id TEXT,
    closed_at         TEXT,              -- 已答或超过 open_hours 时写入；为空 = 未结
    close_reason      TEXT               -- answered | expired
);

-- 确认表情（7.9）：消息送达会话后以主人身份点的表情；主人回复后撤下。每条消息只点一次，失败不重试
CREATE TABLE IF NOT EXISTS acks (
    message_id   TEXT PRIMARY KEY,
    chat_id      TEXT,
    chat_type    TEXT,
    sender_id    TEXT,
    reason       TEXT,
    identity     TEXT,                 -- user（以主人身份点，回复后撤）| bot（以机器人身份点，永不撤）
    reaction_id  TEXT,                 -- 点失败时为空
    added_at     TEXT,
    removed_at   TEXT,
    error        TEXT                  -- 点或撤失败的原因
);
CREATE INDEX IF NOT EXISTS idx_ack_chat ON acks(chat_id, removed_at);

-- 会话发起、要调 agentapi 的动作（7.12）：只有常驻进程能跨项目投递，其他进程只入队，由 run 每轮执行
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,        -- init | spawn | new_session | push_rules | retire
    args_json    TEXT NOT NULL,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    done_at      TEXT,                 -- 执行完（成功或失败）；为空 = 排队中
    result       TEXT,
    error        TEXT
);
"""

# sheepdog 自己生成、投给会话的消息（交接回执、等待提醒、总线转达）用这个 chat_type 和 message_id 前缀
SYSTEM_CHAT = "sheepdog"
SYSTEM_ID_PREFIX = "sd_"


# 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER（只加不删）
MIGRATIONS = {
    "messages": [("note", "TEXT"), ("sender_tenant_key", "TEXT"), ("security_tags", "TEXT"),
                 ("security_action", "TEXT")],
    "topics": [("onboarded_at", "TEXT"), ("receipt_missed", "INTEGER DEFAULT 0"), ("spawned_at", "TEXT"),
               ("origin_note", "TEXT"), ("rules_hash", "TEXT")],
    "acks": [("identity", "TEXT")],
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    @classmethod
    def snapshot(cls, path: Path) -> "Store":
        """--dry-run 用：把账本复制进内存，之后的写入都不落盘。

        否则 dry-run 会把假的 conversation_id、onboarded_at、水位线写进真实账本，正式运行时漏发 onboarding。
        """
        mem = cls(":memory:")
        if Path(path).exists():
            src = sqlite3.connect(str(path))
            try:
                src.backup(mem.conn)
            finally:
                src.close()
            mem._migrate()
        return mem

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- meta ----------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- messages ----------
    def get_message(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()

    def is_my_message(self, message_id: str, self_open_id: str) -> bool:
        row = self.conn.execute("SELECT sender_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return bool(row and self_open_id and row["sender_id"] == self_open_id)

    def upsert_message(self, m: Message, route: str, reason: str, tags: list[str], topic_id: str | None = None) -> None:
        ts = now_iso()
        mentions = json.dumps([asdict(x) for x in m.mentions], ensure_ascii=False)
        dispatch_state = "pending" if route == "dispatch" else None
        with self.tx() as c:
            c.execute(
                """INSERT INTO messages(message_id,chat_id,chat_name,chat_type,sender_id,sender_name,sender_type,
                       sender_tenant_key,content,msg_type,create_time,mentions_json,reply_to,thread_id,link,deleted,
                       updated,update_time,route,reason,tags_json,topic_id,dispatch_state,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m.message_id, m.chat_id, m.chat_name, m.chat_type, m.sender_id, m.sender_name, m.sender_type,
                 m.sender_tenant_key,
                 m.content, m.msg_type, m.create_time, mentions, m.reply_to, m.thread_id, m.link,
                 int(m.deleted), int(m.updated), m.update_time, route, reason,
                 json.dumps(tags, ensure_ascii=False), topic_id, dispatch_state, ts, ts),
            )

    def update_content(self, message_id: str, m: Message, route: str | None = None,
                       reason: str | None = None, dispatch_state: str | None = None,
                       tags: list[str] | None = None, topic_id: str | None = None) -> None:
        sets = ["content=?", "updated=?", "update_time=?", "deleted=?", "mentions_json=?", "last_seen=?"]
        vals: list = [m.content, int(m.updated), m.update_time, int(m.deleted),
                      json.dumps([asdict(x) for x in m.mentions], ensure_ascii=False), now_iso()]
        if route is not None:
            sets += ["route=?", "reason=?"]
            vals += [route, reason]
        if dispatch_state is not None:
            sets.append("dispatch_state=?")
            vals.append(dispatch_state)
        if tags is not None:
            sets.append("tags_json=?")
            vals.append(json.dumps(tags, ensure_ascii=False))
        if topic_id is not None:
            sets.append("topic_id=?")
            vals.append(topic_id)
        vals.append(message_id)
        with self.tx() as c:
            c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE message_id=?", vals)

    def set_security(self, message_id: str, rule_names: list[str], action: str,
                     topic_id: str | None, tags: list[str]) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET security_tags=?, security_action=?, topic_id=?, tags_json=? WHERE message_id=?",
                      (json.dumps(rule_names, ensure_ascii=False), action, topic_id,
                       json.dumps(tags, ensure_ascii=False), message_id))

    def security_log(self, since_iso: str, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM messages WHERE security_action IN ('tag','hold') AND first_seen >= ?
               ORDER BY first_seen DESC LIMIT ?""",
            (since_iso, limit),
        ).fetchall()

    def touch(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET last_seen=? WHERE message_id=?", (now_iso(), message_id))

    def set_read(self, status: dict[str, bool]) -> None:
        with self.tx() as c:
            for mid, r in status.items():
                c.execute("UPDATE messages SET is_read=? WHERE message_id=?", (int(r), mid))

    def pending_dispatch(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE route='dispatch' AND dispatch_state='pending' ORDER BY create_time"
        ).fetchall()

    def mark_batch(self, message_ids: list[str], batch_id: str, topic_id: str, state: str) -> None:
        with self.tx() as c:
            c.executemany(
                "UPDATE messages SET dispatch_state=?, batch_id=?, topic_id=? WHERE message_id=?",
                [(state, batch_id, topic_id, mid) for mid in message_ids],
            )

    def mark_batch_state(self, batch_id: str, state: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET dispatch_state=? WHERE batch_id=?", (state, batch_id))

    def forward_message(self, message_id: str, topic_id: str, tags: list[str]) -> None:
        """转交：改归属并回到待推，由 Dispatcher 下一轮按目标会话的状态投递（同样受人类接管约束）。"""
        with self.tx() as c:
            c.execute(
                """UPDATE messages SET route='dispatch', dispatch_state='pending', batch_id=NULL,
                       topic_id=?, tags_json=? WHERE message_id=?""",
                (topic_id, json.dumps(tags, ensure_ascii=False), message_id),
            )

    def add_system_message(self, topic_id: str, reason: str, content: str, tags: list[str] | None = None) -> str:
        """sheepdog 自己生成的待推消息，和 IM 消息一起按 topic 成批投递（同样受人类接管、排队约束）。"""
        ts = datetime.now().astimezone()
        mid = f"{SYSTEM_ID_PREFIX}{reason}_{uuid.uuid4().hex[:12]}"
        m = Message(message_id=mid, chat_id=SYSTEM_CHAT, chat_name=SYSTEM_CHAT, chat_type=SYSTEM_CHAT,
                    sender_id=SYSTEM_CHAT, sender_name=SYSTEM_CHAT, sender_type="system", content=content,
                    msg_type="text", create_time=ts.isoformat(timespec="seconds"))
        self.upsert_message(m, "dispatch", reason, tags or [], topic_id)
        return mid

    def requeue_batch(self, batch_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE batch_id=?", (batch_id,))

    # ---------- inbox ----------
    def inbox_summary(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT chat_id, chat_name, reason,
                      COUNT(*) AS total,
                      SUM(CASE WHEN is_read IS NULL OR is_read=0 THEN 1 ELSE 0 END) AS unread,
                      MAX(create_time) AS last_time
               FROM messages WHERE route='inbox' AND inbox_cleared=0 AND deleted=0
               GROUP BY chat_id ORDER BY last_time DESC"""
        ).fetchall()

    def inbox_messages(self, chat_query: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM messages WHERE route='inbox' AND inbox_cleared=0 AND deleted=0
                 AND (chat_id=? OR chat_name LIKE ?) ORDER BY create_time DESC LIMIT ?""",
            (chat_query, f"%{chat_query}%", limit),
        ).fetchall()

    def inbox_clear(self, chat_query: str | None) -> int:
        with self.tx() as c:
            if chat_query is None:
                cur = c.execute("UPDATE messages SET inbox_cleared=1 WHERE route='inbox' AND inbox_cleared=0")
            else:
                cur = c.execute(
                    "UPDATE messages SET inbox_cleared=1 WHERE route='inbox' AND inbox_cleared=0 AND (chat_id=? OR chat_name LIKE ?)",
                    (chat_query, f"%{chat_query}%"),
                )
            return cur.rowcount

    def unread_inbox_ids(self, limit: int = 500) -> list[str]:
        rows = self.conn.execute(
            "SELECT message_id FROM messages WHERE route='inbox' AND inbox_cleared=0 AND (is_read IS NULL OR is_read=0) ORDER BY create_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["message_id"] for r in rows]

    # ---------- topics ----------
    def get_topic(self, topic_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()

    def list_topics(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM topics ORDER BY last_active DESC").fetchall()

    def create_topic(self, topic_id: str, title: str, kind: str, duty: str, state: str) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO topics(topic_id,title,kind,duty,state,created_at,last_active) VALUES(?,?,?,?,?,?,?)",
                (topic_id, title, kind, duty, state, ts, ts),
            )

    def count_topics_since(self, kind: str, since_iso: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM topics WHERE kind=? AND created_at >= ?",
                                 (kind, since_iso)).fetchone()[0]

    def update_topic(self, topic_id: str, **fields) -> None:
        if not fields:
            return
        fields.setdefault("last_active", now_iso())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE topics SET {cols} WHERE topic_id=?", (*fields.values(), topic_id))

    # ---------- dispatches ----------
    def dispatch_exists(self, batch_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM dispatches WHERE batch_id=?", (batch_id,)).fetchone() is not None

    def record_dispatch(self, batch_id: str, topic_id: str, message_ids: list[str]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                (batch_id, topic_id, json.dumps(message_ids), now_iso(), "sent"),
            )

    def finish_dispatch(self, batch_id: str, state: str, receipt: str = "", error: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE dispatches SET state=?, receipt=?, error=? WHERE batch_id=?", (state, receipt, error, batch_id))

    def set_note(self, message_id: str, note: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET note=? WHERE message_id=?", (note, message_id))

    # ---------- escalations ----------
    def add_escalation(self, message_id: str, topic_id: str, chat_id: str, text: str, asked_at: str) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO escalations(message_id,topic_id,chat_id,text,asked_at)
                         VALUES(?,?,?,?,?)""", (message_id, topic_id, chat_id, text, asked_at))

    def get_escalation(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM escalations WHERE message_id=?", (message_id,)).fetchone()

    def open_escalations(self, topic_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM escalations WHERE closed_at IS NULL", []
        if topic_id:
            sql += " AND topic_id=?"
            args.append(topic_id)
        return self.conn.execute(sql + " ORDER BY asked_at DESC", args).fetchall()

    def list_escalations(self, include_closed: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        where = "" if include_closed else "WHERE closed_at IS NULL"
        return self.conn.execute(f"SELECT * FROM escalations {where} ORDER BY asked_at DESC LIMIT ?", (limit,)).fetchall()

    def answer_escalation(self, message_id: str, answer_message_id: str) -> bool:
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute("""UPDATE escalations SET answered_at=?, answer_message_id=?, closed_at=?, close_reason='answered'
                               WHERE message_id=? AND closed_at IS NULL""", (ts, answer_message_id, ts, message_id))
            return cur.rowcount > 0

    def expire_escalation(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE escalations SET closed_at=?, close_reason='expired' WHERE message_id=? AND closed_at IS NULL",
                      (now_iso(), message_id))

    # ---------- actions ----------
    def add_action(self, kind: str, args: dict, requested_by: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO actions(kind,args_json,requested_by,requested_at) VALUES(?,?,?,?)",
                            (kind, json.dumps(args, ensure_ascii=False), requested_by, now_iso()))
            return int(cur.lastrowid)

    def pending_actions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM actions WHERE done_at IS NULL ORDER BY id").fetchall()

    def finish_action(self, action_id: int, result: str = "", error: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE actions SET done_at=?, result=?, error=? WHERE id=?",
                      (now_iso(), result or None, error or None, action_id))

    def list_actions(self, include_done: bool = False, recent_done: int = 10) -> list[sqlite3.Row]:
        if include_done:
            return self.conn.execute("SELECT * FROM actions ORDER BY id DESC").fetchall()
        pending = self.conn.execute("SELECT * FROM actions WHERE done_at IS NULL ORDER BY id DESC").fetchall()
        done = self.conn.execute("SELECT * FROM actions WHERE done_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                                 (recent_done,)).fetchall()
        return list(pending) + list(done)

    # ---------- acks ----------
    def get_ack(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM acks WHERE message_id=?", (message_id,)).fetchone()

    def add_ack(self, message_id: str, chat_id: str, chat_type: str, sender_id: str, reason: str,
                reaction_id: str | None, error: str | None, identity: str = "user") -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO acks(message_id,chat_id,chat_type,sender_id,reason,identity,reaction_id,
                                                    added_at,error)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (message_id, chat_id, chat_type, sender_id, reason, identity, reaction_id, now_iso(), error))

    def removable_acks(self, chat_id: str) -> list[sqlite3.Row]:
        """同一聊天里点成功、还没撤、撤的时候也没失败过的表情（失败过的不再重试）。"""
        return self.conn.execute(
            """SELECT * FROM acks WHERE chat_id=? AND reaction_id IS NOT NULL AND removed_at IS NULL AND error IS NULL
                 AND (identity IS NULL OR identity='user')
               ORDER BY added_at""", (chat_id,)).fetchall()

    def mark_ack_removed(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE acks SET removed_at=? WHERE message_id=?", (now_iso(), message_id))

    def set_ack_error(self, message_id: str, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE acks SET error=? WHERE message_id=?", (error, message_id))

    def list_acks(self, open_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        where = "WHERE a.reaction_id IS NOT NULL AND a.removed_at IS NULL" if open_only else ""
        return self.conn.execute(
            f"""SELECT a.*, m.chat_name, m.sender_name, m.content FROM acks a
                LEFT JOIN messages m ON m.message_id = a.message_id {where}
                ORDER BY a.added_at DESC LIMIT ?""", (limit,)).fetchall()

    # ---------- watches ----------
    def add_watch(self, topic_id: str, person_id: str, chat_id: str, note: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO watches(topic_id,person_id,chat_id,note,started_at) VALUES(?,?,?,?,?)",
                            (topic_id, person_id, chat_id or None, note, now_iso()))
            return int(cur.lastrowid)

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()

    def open_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM watches WHERE closed_at IS NULL ORDER BY started_at, id").fetchall()

    def list_watches(self, include_closed: bool = False, limit: int = 100) -> list[sqlite3.Row]:
        where = "" if include_closed else "WHERE closed_at IS NULL"
        return self.conn.execute(f"SELECT * FROM watches {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def match_watch(self, person_id: str, chat_id: str) -> sqlite3.Row | None:
        """同一人有多条有效等待时取最新的一条。"""
        if not person_id:
            return None
        return self.conn.execute(
            """SELECT * FROM watches WHERE closed_at IS NULL AND person_id=? AND (chat_id IS NULL OR chat_id=?)
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (person_id, chat_id),
        ).fetchone()

    def update_watch(self, watch_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))

    def close_watch(self, watch_id: int, reason: str) -> None:
        self.update_watch(watch_id, closed_at=now_iso(), close_reason=reason)

    # ---------- 保留期清理 ----------
    def prune(self, retention_days: int) -> int:
        cutoff = (datetime.now().astimezone() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM messages WHERE first_seen < ? AND (route != 'dispatch' OR dispatch_state IN ('acked','failed','missed'))",
                (cutoff,),
            )
            return cur.rowcount


def row_to_message(row: sqlite3.Row) -> Message:
    mentions = [Mention(**x) for x in json.loads(row["mentions_json"] or "[]")]
    return Message(
        message_id=row["message_id"], chat_id=row["chat_id"], chat_name=row["chat_name"] or "",
        chat_type=row["chat_type"] or "", sender_id=row["sender_id"] or "", sender_name=row["sender_name"] or "",
        sender_type=row["sender_type"] or "", sender_tenant_key=row["sender_tenant_key"] or "",
        content=row["content"] or "", msg_type=row["msg_type"] or "",
        create_time=row["create_time"] or "", mentions=mentions, reply_to=row["reply_to"] or "",
        thread_id=row["thread_id"] or "", link=row["link"] or "", deleted=bool(row["deleted"]),
        updated=bool(row["updated"]), update_time=row["update_time"] or "",
    )
