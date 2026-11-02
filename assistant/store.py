from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def terms(text: str) -> set[str]:
    """English tokens and Chinese bigrams, without external dependencies."""
    tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(block[i:i + 2] for i in range(len(block) - 1))
        if len(block) == 1:
            tokens.add(block)
    return tokens


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def remember(self, content, kind="fact"):
        content = content.strip()
        if not content or len(content) > 4000:
            raise ValueError("記憶需為 1–4000 字。")
        with self.connect() as db:
            return db.execute("INSERT INTO memories(content, kind) VALUES (?, ?)", (content, kind)).lastrowid

    def memories(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories ORDER BY id DESC")]

    def forget(self, memory_id):
        with self.connect() as db:
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def retrieve(self, query, limit=8):
        query_terms = terms(query)
        ranked = []
        for row in self.memories():
            overlap = len(query_terms & terms(row["content"]))
            if row["kind"] == "preference" or overlap:
                score = overlap + (0.5 if row["kind"] == "preference" else 0)
                ranked.append((score, row["id"], row))
        return [r[2] for r in sorted(ranked, key=lambda r: (r[0], r[1]), reverse=True)[:limit]]

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
