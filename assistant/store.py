from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .retrieval import rank_memories, terms
from .documents import MAX_DOCUMENTS, MAX_SESSION_BYTES, read_document


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
                CREATE TABLE IF NOT EXISTS session_briefs (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    objective TEXT NOT NULL DEFAULT '', constraints_text TEXT NOT NULL DEFAULT '',
                    decisions TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS answer_contexts (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL, source_path TEXT NOT NULL, path_key TEXT NOT NULL,
                    content TEXT NOT NULL, sha256 TEXT NOT NULL, encoding TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(session_id, path_key));
                CREATE TABLE IF NOT EXISTS answer_checks (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id), payload TEXT NOT NULL);
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

    def earlier_user_messages(self, sid, before_id, limit=200):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM (SELECT * FROM messages
                WHERE session_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT ?) ORDER BY id""",
                (sid, before_id, limit))]
            first = db.execute("SELECT * FROM messages WHERE session_id=? AND role='user' AND id<? ORDER BY id LIMIT 1",
                               (sid, before_id)).fetchone()
            if first and (not rows or rows[0]["id"] != first["id"]):
                rows.insert(0, dict(first))
            return rows

    def session_brief(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT objective, constraints_text, decisions, updated_at FROM session_briefs WHERE session_id=?",
                             (sid,)).fetchone()
            return dict(row) if row else {}

    def save_session_brief(self, sid, objective, constraints_text, decisions):
        values = [value.strip() for value in (objective, constraints_text, decisions)]
        if any(len(value) > 2000 for value in values):
            raise ValueError("每個欄位最多 2000 字。")
        with self.connect() as db:
            db.execute("""INSERT INTO session_briefs(session_id, objective, constraints_text, decisions)
                VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET objective=excluded.objective,
                constraints_text=excluded.constraints_text, decisions=excluded.decisions, updated_at=CURRENT_TIMESTAMP""",
                (sid, *values))

    def add_answer(self, sid, answer, context, checks=None):
        with self.connect() as db:
            mid = db.execute("INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', ?)",
                             (sid, answer)).lastrowid
            db.execute("INSERT INTO answer_contexts(message_id, payload) VALUES (?, ?)",
                       (mid, json.dumps(context, ensure_ascii=False)))
            if checks is not None:
                db.execute("INSERT INTO answer_checks(message_id, payload) VALUES (?, ?)",
                           (mid, json.dumps(checks, ensure_ascii=False)))
            return mid

    def answer_context(self, message_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM answer_contexts WHERE message_id=?", (message_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def answer_checks(self, message_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM answer_checks WHERE message_id=?", (message_id,)).fetchone()
            return json.loads(row["payload"]) if row else []

    def attach_document(self, sid, path, *, scope=None, expected_sha256=None):
        document = read_document(path, scope=scope)
        if expected_sha256 is not None and document['sha256'] != expected_sha256:
            raise ValueError('文件已在搜尋後變更，請重新搜尋並確認預覽。')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            size = db.execute("SELECT COALESCE(SUM(size_bytes),0), COUNT(*) FROM documents WHERE session_id=? AND path_key<>?",
                              (sid, document["path_key"])).fetchone()
            if size[1] >= MAX_DOCUMENTS or size[0] + document["size_bytes"] > MAX_SESSION_BYTES:
                raise ValueError("每段對話最多 8 個文件、合計 8 MiB；請先移除不需要的參考文件。")
            db.execute("""INSERT INTO documents(session_id, name, source_path, path_key, content, sha256, encoding, size_bytes)
                VALUES (:session_id, :name, :source_path, :path_key, :content, :sha256, :encoding, :size_bytes)
                ON CONFLICT(session_id, path_key) DO UPDATE SET name=excluded.name, source_path=excluded.source_path,
                content=excluded.content, sha256=excluded.sha256, encoding=excluded.encoding,
                size_bytes=excluded.size_bytes, imported_at=CURRENT_TIMESTAMP""", {"session_id": sid, **document})
            return db.execute("SELECT id FROM documents WHERE session_id=? AND path_key=?", (sid, document["path_key"])).fetchone()[0]

    def documents(self, sid, include_content=False):
        columns = "*" if include_content else "id, name, source_path, sha256, encoding, size_bytes, imported_at"
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT {columns} FROM documents WHERE session_id=? ORDER BY id", (sid,))]

    def detach_document(self, sid, document_id):
        with self.connect() as db:
            db.execute("DELETE FROM documents WHERE session_id=? AND id=?", (sid, document_id))

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
