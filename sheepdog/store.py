"""SQLite 账本：消息、元数据（水位线等）、话题/session 注册表、投递记录。"""

from __future__ import annotations

import json
import sqlite3
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
    note           TEXT,               -- 转交说明（sheepdog forward --note）
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
    kind             TEXT,             -- bus | adopted（名册 managed）| known（名册 known，不投递）
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
"""


# 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER（只加不删）
MIGRATIONS = {
    "messages": [("note", "TEXT")],
    "topics": [("onboarded_at", "TEXT"), ("receipt_missed", "INTEGER DEFAULT 0")],
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
                       content,msg_type,create_time,mentions_json,reply_to,thread_id,link,deleted,updated,update_time,
                       route,reason,tags_json,topic_id,dispatch_state,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m.message_id, m.chat_id, m.chat_name, m.chat_type, m.sender_id, m.sender_name, m.sender_type,
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

    def forward_message(self, message_id: str, topic_id: str, tags: list[str], note: str) -> None:
        """转交：改归属并回到待推，由 Dispatcher 下一轮按目标会话的状态投递（同样受人类接管约束）。"""
        with self.tx() as c:
            c.execute(
                """UPDATE messages SET route='dispatch', dispatch_state='pending', batch_id=NULL,
                       topic_id=?, tags_json=?, note=? WHERE message_id=?""",
                (topic_id, json.dumps(tags, ensure_ascii=False), note, message_id),
            )

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
        sender_type=row["sender_type"] or "", content=row["content"] or "", msg_type=row["msg_type"] or "",
        create_time=row["create_time"] or "", mentions=mentions, reply_to=row["reply_to"] or "",
        thread_id=row["thread_id"] or "", link=row["link"] or "", deleted=bool(row["deleted"]),
        updated=bool(row["updated"]), update_time=row["update_time"] or "",
    )
