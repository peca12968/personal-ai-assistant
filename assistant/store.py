from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .retrieval import rank_memories, terms


def validate_memory(content, kind, expires_on):
    content = content.strip()
    if not content or len(content) > 4000:
        raise ValueError("記憶需為 1–4000 字。")
    if kind not in {"preference", "fact", "lesson"}:
        raise ValueError("未知的記憶類型。")
    if expires_on:
        try:
            if date.fromisoformat(expires_on).isoformat() != expires_on:
                raise ValueError()
        except ValueError:
            raise ValueError("到期日請使用 YYYY-MM-DD 格式。") from None
    return content


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY, content TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('preference', 'fact', 'lesson')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS feedback (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                    rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
                    correction TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(memories)")}
            if "active" not in columns:
                # Preserve old data before a schema change; never copy a live SQLite file.
                if existed:
                    backup_dir = self.path.parent / "backups"
                    backup_dir.mkdir(exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup_path = backup_dir / f"pre-memory-v2-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
                    backup_db = sqlite3.connect(backup_path)
                    try:
                        db.backup(backup_db)
                    finally:
                        backup_db.close()
                db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE memories ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))")
                db.execute("ALTER TABLE memories ADD COLUMN expires_on TEXT")
                db.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")
                db.execute("UPDATE memories SET updated_at=created_at")
                db.execute("PRAGMA user_version = 2")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_session(self, title="新對話"):
        sid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id, title) VALUES (?, ?)", (sid, title[:80]))
        return sid

    def sessions(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY rowid DESC")]

    def add_message(self, sid, role, content):
        with self.connect() as db:
            cur = db.execute("INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                             (sid, role, content))
            if role == "user":
                db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新對話'", (content[:50], sid))
            return cur.lastrowid

    def messages(self, sid, limit=30):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (sid, limit))]

    def remember(self, content, kind="fact", expires_on=None, active=True):
        content = validate_memory(content, kind, expires_on)
        with self.connect() as db:
            return db.execute("""INSERT INTO memories(content, kind, expires_on, active, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (content, kind, expires_on or None, int(bool(active)))).lastrowid

    def edit_memory(self, memory_id, content, kind="fact", expires_on=None, active=True):
        content = validate_memory(content, kind, expires_on)
        with self.connect() as db:
            result = db.execute("""UPDATE memories SET content=?, kind=?, expires_on=?, active=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (content, kind, expires_on or None, int(bool(active)), memory_id))
            if result.rowcount != 1:
                raise ValueError("這筆記憶已不存在，請重新整理。")

    def set_memory_active(self, memory_id, active):
        with self.connect() as db:
            result = db.execute("UPDATE memories SET active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (int(bool(active)), memory_id))
            if result.rowcount != 1:
                raise ValueError("這筆記憶已不存在，請重新整理。")

    def memories(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories ORDER BY id DESC")]

    def forget(self, memory_id):
        with self.connect() as db:
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def retrieve(self, query, limit=8, today=None):
        today = today or date.today().isoformat()
        rows = [row for row in self.memories() if row["active"] and
                (not row["expires_on"] or row["expires_on"] >= today)]
        return rank_memories(query, rows, limit)

    def rate(self, message_id, rating, correction=""):
        if rating not in (-1, 1):
            raise ValueError("評分只能是 -1 或 1。")
        with self.connect() as db:
            row = db.execute("SELECT role FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["role"] != "assistant":
                raise ValueError("只能評價助理回答。")
            db.execute("""INSERT INTO feedback(message_id, rating, correction) VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating,
                correction=excluded.correction, updated_at=CURRENT_TIMESTAMP""",
                (message_id, rating, correction[:4000]))

    def feedback_summary(self):
        with self.connect() as db:
            rows = db.execute("""SELECT f.*, m.content AS answer, m.session_id FROM feedback f
                JOIN messages m ON m.id=f.message_id ORDER BY f.updated_at DESC""")
            return [dict(r) for r in rows]

    def export_session(self, sid, path):
        rows = self.messages(sid, limit=100000)
        Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
