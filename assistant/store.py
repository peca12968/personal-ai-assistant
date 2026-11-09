from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .retrieval import rank_memories, terms
from .context import MAX_EARLIER_CORRECTIONS, is_explicit_correction
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


def allocate_memory_id(db):
    """Called inside a write transaction; keep IDs stable after deletion."""
    largest = db.execute('SELECT COALESCE(MAX(id),0) FROM memories').fetchone()[0]
    previous = db.execute('SELECT last_id FROM memory_sequence WHERE id=1').fetchone()[0]
    value = max(largest, previous) + 1
    if value > 9223372036854775807:
        raise ValueError('記憶編號已達資料庫上限。')
    db.execute('UPDATE memory_sequence SET last_id=? WHERE id=1', (value,))
    return value


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
                CREATE TABLE IF NOT EXISTS chat_drafts (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    revision INTEGER NOT NULL CHECK(revision>=1), content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_draft_submissions (
                    token TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    revision INTEGER NOT NULL CHECK(revision>=1),
                    message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id), UNIQUE(session_id,revision));
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY, content TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('preference', 'fact', 'lesson')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS memory_sequence (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_id INTEGER NOT NULL CHECK(last_id>=0));
                CREATE TABLE IF NOT EXISTS memory_aliases (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=1),
                    groups_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS memory_drafts (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL CHECK(status IN ('pending','running','ready','failed','cancelled')),
                    context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL,
                    proposals_json TEXT NOT NULL, proposals_sha256 TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS memory_acceptances (
                    draft_id TEXT NOT NULL REFERENCES memory_drafts(id), item_index INTEGER NOT NULL,
                    memory_id INTEGER NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL,
                    expires_on TEXT, reused INTEGER NOT NULL CHECK(reused IN (0,1)),
                    accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(draft_id,item_index));
                CREATE TABLE IF NOT EXISTS memory_replacements (
                    draft_id TEXT NOT NULL, item_index INTEGER NOT NULL,
                    before_json TEXT NOT NULL, before_sha256 TEXT NOT NULL,
                    PRIMARY KEY(draft_id,item_index),
                    FOREIGN KEY(draft_id,item_index) REFERENCES memory_acceptances(draft_id,item_index));
                CREATE TABLE IF NOT EXISTS feedback (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                    rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
                    correction TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS learning_examples (
                    id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                    status TEXT NOT NULL CHECK(status IN ('confirmed','withdrawn')),
                    prompt TEXT NOT NULL, answer TEXT NOT NULL, source_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS session_briefs (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    objective TEXT NOT NULL DEFAULT '', constraints_text TEXT NOT NULL DEFAULT '',
                    decisions TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS answer_contexts (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_images (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL, format TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS local_memory_messages (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS answer_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    user_message_id INTEGER NOT NULL REFERENCES messages(id),
                    answer_message_id INTEGER REFERENCES messages(id),
                    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','interrupted')),
                    error_code TEXT NOT NULL DEFAULT '', context_json TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
                    CHECK((status='succeeded' AND answer_message_id IS NOT NULL) OR
                          (status<>'succeeded' AND answer_message_id IS NULL)));
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_answer_per_session
                    ON answer_runs(session_id) WHERE status='running';
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL, source_path TEXT NOT NULL, path_key TEXT NOT NULL,
                    content TEXT NOT NULL, sha256 TEXT NOT NULL, encoding TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(session_id, path_key));
                CREATE TABLE IF NOT EXISTS answer_checks (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS answer_deliveries (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL CHECK(status IN ('planned','running','succeeded','failed','cancelled')),
                    plan_json TEXT NOT NULL, result_json TEXT, error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS autonomous_goals (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    objective TEXT NOT NULL, status TEXT NOT NULL,
                    sources_json TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
                    notice TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS autonomous_steps (
                    id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES autonomous_goals(id),
                    sequence INTEGER NOT NULL, action_json TEXT NOT NULL, observation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(goal_id,sequence));
                CREATE TABLE IF NOT EXISTS autonomous_reply_sources (
                    goal_id TEXT NOT NULL, ask_sequence INTEGER NOT NULL CHECK(ask_sequence BETWEEN 1 AND 32),
                    notes_start INTEGER NOT NULL CHECK(notes_start>=0), answer TEXT NOT NULL,
                    PRIMARY KEY(goal_id,ask_sequence),
                    FOREIGN KEY(goal_id,ask_sequence) REFERENCES autonomous_steps(goal_id,sequence));
                CREATE TABLE IF NOT EXISTS autonomous_guidance_sources (
                    goal_id TEXT NOT NULL REFERENCES autonomous_goals(id),
                    number INTEGER NOT NULL CHECK(number>=1),
                    notes_start INTEGER NOT NULL CHECK(notes_start>=0),
                    steps_seen INTEGER NOT NULL CHECK(steps_seen BETWEEN 0 AND 31),
                    stamp TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(goal_id,number));
                CREATE TABLE IF NOT EXISTS autonomous_question_withdrawals (
                    goal_id TEXT NOT NULL, ask_sequence INTEGER NOT NULL, guidance_number INTEGER NOT NULL,
                    question_sha256 TEXT NOT NULL, guidance_sha256 TEXT NOT NULL,
                    PRIMARY KEY(goal_id,ask_sequence),
                    FOREIGN KEY(goal_id,ask_sequence) REFERENCES autonomous_steps(goal_id,sequence),
                    FOREIGN KEY(goal_id,guidance_number) REFERENCES autonomous_guidance_sources(goal_id,number));
                CREATE TABLE IF NOT EXISTS autonomous_handoffs (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id),
                    parent_goal_id TEXT NOT NULL REFERENCES autonomous_goals(id),
                    context_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(goal_id != parent_goal_id));
                CREATE TABLE IF NOT EXISTS companion_profile (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=1),
                    settings_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS autonomous_delivery (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id), enabled INTEGER NOT NULL CHECK(enabled=1));
                CREATE TABLE IF NOT EXISTS autonomous_artifacts (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES autonomous_delivery(goal_id),
                    name TEXT NOT NULL, file_key TEXT NOT NULL, content TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared','verified','uncertain')),
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    verified_at TEXT, UNIQUE(goal_id,file_key));
                CREATE TABLE IF NOT EXISTS goal_reviews (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id),
                    fingerprint TEXT NOT NULL, hidden_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS review_reminders (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), daily_time TEXT NOT NULL,
                    last_day TEXT, snooze_until INTEGER, notice_token TEXT);
                CREATE TABLE IF NOT EXISTS autonomous_csv_sources (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id), config_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS autonomous_csv_plans (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES autonomous_csv_sources(goal_id),
                    spec_json TEXT NOT NULL, mean_policy_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('planned','running','succeeded','failed','cancelled')),
                    report_json TEXT, report_sha256 TEXT, error TEXT NOT NULL DEFAULT '',
                    exports_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS autonomous_continuous_runs (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES autonomous_goals(id),
                    max_steps INTEGER NOT NULL CHECK(max_steps BETWEEN 1 AND 32),
                    max_seconds INTEGER NOT NULL CHECK(max_seconds BETWEEN 60 AND 7200),
                    start_step INTEGER NOT NULL, end_step INTEGER,
                    reason TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, stopped_at TEXT);
                CREATE TABLE IF NOT EXISTS autonomous_conversations (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id),
                    context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS autonomous_conversation_history (
                    goal_id TEXT PRIMARY KEY REFERENCES autonomous_goals(id),
                    context_json TEXT NOT NULL, context_sha256 TEXT NOT NULL);
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
            db.execute('INSERT OR IGNORE INTO memory_sequence(id,last_id) VALUES(1,0)')
            db.execute('UPDATE memory_sequence SET last_id=MAX(last_id,(SELECT COALESCE(MAX(id),0) FROM memories)) WHERE id=1')
            from .feedback_order import initialize
            initialize(db)

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

    def create_task(self, sid, plan):
        task_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO task_runs(id,session_id,status,plan_json) VALUES (?, ?, ?, ?)',
                       (task_id, sid, 'planned', json.dumps(plan, ensure_ascii=False)))
        return task_id

    def tasks(self, sid):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT id,status,created_at,finished_at FROM task_runs WHERE session_id=? ORDER BY rowid DESC', (sid,))]

    def task(self, sid, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM task_runs WHERE session_id=? AND id=?', (sid, task_id)).fetchone()
        if row is None:
            raise ValueError('找不到目前對話的任務。')
        result = dict(row)
        result['plan'] = json.loads(result.pop('plan_json'))
        raw = result.pop('result_json')
        result['result'] = json.loads(raw) if raw else None
        return result

    def claim_task(self, sid, task_id):
        with self.connect() as db:
            changed = db.execute("UPDATE task_runs SET status='running' WHERE session_id=? AND id=? AND status='planned'", (sid, task_id))
            if changed.rowcount != 1:
                raise ValueError('只能執行尚未開始的任務；需重跑時請建立新計畫。')

    def finish_task(self, sid, task_id, status, result=None, error=''):
        if status not in {'succeeded', 'failed', 'cancelled'} or (status == 'succeeded' and result is None):
            raise ValueError('任務狀態或結果無效。')
        with self.connect() as db:
            changed = db.execute("UPDATE task_runs SET status=?,result_json=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE session_id=? AND id=? AND status='running'",
                                 (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                                  error[:2000], sid, task_id))
            if changed.rowcount != 1:
                raise ValueError('任務狀態已改變，未寫入完成紀錄。')

    def record_task_export(self, sid, task_id, record):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT result_json FROM task_runs WHERE session_id=? AND id=? AND status='succeeded'", (sid, task_id)).fetchone()
            if row is None:
                raise ValueError('任務尚未完成，無法記錄匯出。')
            result = json.loads(row['result_json'])
            if result['report_sha256'] != record['sha256']:
                raise ValueError('報告版本與匯出結果不一致。')
            result.setdefault('exports', []).append(record)
            result['exports'] = result['exports'][-20:]
            db.execute('UPDATE task_runs SET result_json=? WHERE id=?', (json.dumps(result, ensure_ascii=False), task_id))

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

    def messages(self, sid, limit=30, exclude_local=False, exclude_message_id=None):
        local_filter = ' AND id NOT IN (SELECT message_id FROM local_memory_messages)' if exclude_local else ''
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND id<>?" + local_filter + " ORDER BY id DESC LIMIT ?) ORDER BY id",
                (sid, exclude_message_id or -1, limit))]

    def earlier_user_messages(self, sid, before_id, limit=200, exclude_local=False, exclude_message_id=None):
        local_filter = ' AND id NOT IN (SELECT message_id FROM local_memory_messages)' if exclude_local else ''
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM (SELECT * FROM messages
                WHERE session_id=? AND role='user' AND id<? AND id<>?""" + local_filter + " ORDER BY id DESC LIMIT ?) ORDER BY id",
                (sid, before_id, exclude_message_id or -1, limit))]
            first = db.execute("SELECT * FROM messages WHERE session_id=? AND role='user' AND id<? AND id<>?" + local_filter + " ORDER BY id LIMIT 1",
                               (sid, before_id, exclude_message_id or -1)).fetchone()
            if first and (not rows or rows[0]["id"] != first["id"]):
                rows.insert(0, dict(first))
            return rows

    def earlier_correction_messages(self, sid, before_id, exclude_message_id=None):
        """Read the newest explicit user corrections beyond the recent window.

        Evaluate only bounded prefixes in SQLite; return at most seven original
        rows (six excerpts plus one overflow signal), without loading all history.
        This selection neither edits memory nor asserts the correction is true.
        """
        with self.connect() as db:
            from .sqlite_text import register_correction
            register_correction(db, is_explicit_correction)
            return [dict(row) for row in db.execute("""SELECT * FROM messages
                WHERE session_id=? AND role='user' AND id<? AND id<>?
                AND id NOT IN (SELECT message_id FROM local_memory_messages)
                AND explicit_correction_bytes(substr(CAST(content AS BLOB),1,644),length(CAST(content AS BLOB)))
                ORDER BY id DESC LIMIT ?""",
                (sid, before_id, exclude_message_id or -1, MAX_EARLIER_CORRECTIONS + 1))]

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

    def attach_document(self, sid, path, *, scope=None, expected_sha256=None, cancel=None):
        def check_cancel():
            if cancel is not None and cancel.is_set():raise ValueError('已停止文件匯入，這個檔案未保存。')
        check_cancel()
        document = read_document(path, scope=scope, **({'cancel':cancel} if cancel is not None else {}))
        check_cancel()
        if expected_sha256 is not None and document['sha256'] != expected_sha256:
            raise ValueError('文件已在搜尋後變更，請重新搜尋並確認預覽。')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_cancel()
            size = db.execute("SELECT COALESCE(SUM(size_bytes),0), COUNT(*) FROM documents WHERE session_id=? AND path_key<>?",
                              (sid, document["path_key"])).fetchone()
            if size[1] >= MAX_DOCUMENTS or size[0] + document["size_bytes"] > MAX_SESSION_BYTES:
                raise ValueError("每段對話最多 8 個文件、合計 8 MiB；請先移除不需要的參考文件。")
            db.execute("""INSERT INTO documents(session_id, name, source_path, path_key, content, sha256, encoding, size_bytes)
                VALUES (:session_id, :name, :source_path, :path_key, :content, :sha256, :encoding, :size_bytes)
                ON CONFLICT(session_id, path_key) DO UPDATE SET name=excluded.name, source_path=excluded.source_path,
                content=excluded.content, sha256=excluded.sha256, encoding=excluded.encoding,
                size_bytes=excluded.size_bytes, imported_at=CURRENT_TIMESTAMP""", {"session_id": sid, **document})
            check_cancel()
            return db.execute("SELECT id FROM documents WHERE session_id=? AND path_key=?", (sid, document["path_key"])).fetchone()[0]

    def documents(self, sid, include_content=False, *, document_ids=None):
        columns = "*" if include_content else "id, name, source_path, sha256, encoding, size_bytes, imported_at"
        sql = f"SELECT {columns} FROM documents WHERE session_id=?"
        params = (sid,)
        if document_ids is not None:
            ids = tuple(document_ids)
            if not ids:
                return []
            sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
            params += ids
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY id", params)]

    def detach_document(self, sid, document_id):
        with self.connect() as db:
            db.execute("DELETE FROM documents WHERE session_id=? AND id=?", (sid, document_id))

    def remember(self, content, kind="fact", expires_on=None, active=True):
        content = validate_memory(content, kind, expires_on)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            mid = allocate_memory_id(db)
            db.execute("""INSERT INTO memories(id, content, kind, expires_on, active, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (mid, content, kind, expires_on or None, int(bool(active))))
            return mid

    def edit_memory(self, memory_id, content, kind="fact", expires_on=None, active=True, *, expected=None):
        content = validate_memory(content, kind, expires_on)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
            if current is None:
                raise ValueError('這筆記憶已不存在；草稿未儲存，也未重新新增。')
            draft = dict(content=content, kind=kind, expires_on=expires_on or None, active=int(bool(active)))
            preserved = []
            if expected is not None:
                from .memory_edits import compare, MemoryConflict
                comparison = compare(expected, dict(current), draft)
                if comparison['conflicts']:
                    raise MemoryConflict(expected, current, draft, comparison)
                draft, preserved = comparison['merged'], comparison['preserved_fields']
            result = db.execute("""UPDATE memories SET content=?, kind=?, expires_on=?, active=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (draft['content'], draft['kind'], draft['expires_on'], draft['active'], memory_id))
            if result.rowcount != 1:
                raise ValueError("這筆記憶已不存在，請重新整理。")
            return {'memory': dict(db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()),
                    'preserved_fields': preserved}

    def set_memory_active(self, memory_id, active):
        with self.connect() as db:
            result = db.execute("UPDATE memories SET active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (int(bool(active)), memory_id))
            if result.rowcount != 1:
                raise ValueError("這筆記憶已不存在，請重新整理。")

    def memories(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories ORDER BY id DESC")]

    def forget(self, memory_id, *, expected=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected is not None:
                from .memory_edits import validate_original
                validate_original(expected, memory_id)
                current = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
                if current is None or dict(current) != expected:
                    raise ValueError('記憶已變更或刪除，未刪除任何資料；請先核對最新記憶。')
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def retrieve(self, query, limit=8, today=None):
        from . import memory_aliases
        from .retrieval import alias_terms
        today = today or date.today().isoformat()
        with self.connect() as db:
            db.execute('BEGIN')  # One read snapshot for vocabulary and memory contents.
            aliases = memory_aliases.read(db)
            memories = [dict(r) for r in db.execute('SELECT * FROM memories ORDER BY id DESC')]
        rows = [row for row in memories if row["active"] and
                (not row["expires_on"] or row["expires_on"] >= today)]
        selected = rank_memories(query, rows, limit, alias_groups=aliases['groups'])
        query_aliases = alias_terms(query, aliases['groups'])
        for row in selected:
            shared = query_aliases & alias_terms(row['content'], aliases['groups'])
            if shared:
                row['retrieval_aliases'] = {'source': 'user_saved_search_aliases', 'revision': aliases['revision'],
                    'groups': [group for i, group in enumerate(aliases['groups']) if '@user_alias:' + str(i) in shared]}
        return selected

    def rate(self, message_id, rating, correction=""):
        if rating not in (-1, 1):
            raise ValueError("評分只能是 -1 或 1。")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT role FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["role"] != "assistant":
                raise ValueError("只能評價助理回答。")
            db.execute("""INSERT INTO feedback(message_id, rating, correction) VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating,
                correction=excluded.correction, updated_at=CURRENT_TIMESTAMP""",
                (message_id, rating, correction[:4000]))
            from .feedback_order import record
            record(db, message_id)

    def feedback_summary(self):
        with self.connect() as db:
            rows = db.execute("""SELECT f.*, v.revision AS update_revision, v.order_basis,
                m.content AS answer, m.session_id FROM feedback f
                JOIN messages m ON m.id=f.message_id
                JOIN feedback_versions v ON v.message_id=f.message_id ORDER BY v.revision DESC""")
            return [dict(r) for r in rows]

    def export_session(self, sid, path):
        rows = self.messages(sid, limit=100000)
        from .answer_runs import runs
        attempts = {}
        for run in runs(self, sid):
            attempts.setdefault(run['user_message_id'], []).append(run)
        for row in rows:
            if row['id'] in attempts:
                row['answer_attempts'] = attempts[row['id']]
        Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
