"""Local, validated SQLite snapshots; restore never overwrites a data directory."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

MAX_DATABASE_BYTES = 512 * 1024 * 1024
TABLES = {
    'sessions': {'id', 'title', 'created_at'},
    'messages': {'id', 'session_id', 'role', 'content', 'created_at'},
    'memories': {'id', 'content', 'kind', 'created_at', 'active', 'expires_on', 'updated_at'},
    'memory_sequence': {'id', 'last_id'},
    'feedback': {'message_id', 'rating', 'correction', 'updated_at'},
    'session_briefs': {'session_id', 'objective', 'constraints_text', 'decisions', 'updated_at'},
    'answer_contexts': {'message_id', 'payload'},
    'local_memory_messages': {'message_id'},
    'answer_runs': {'id', 'session_id', 'user_message_id', 'answer_message_id', 'status', 'error_code', 'context_json', 'started_at', 'finished_at'},
    'documents': {'id', 'session_id', 'name', 'source_path', 'path_key', 'content', 'sha256', 'encoding', 'size_bytes', 'imported_at'},
    'answer_checks': {'message_id', 'payload'},
    'task_runs': {'id', 'session_id', 'status', 'plan_json', 'result_json', 'error', 'created_at', 'finished_at'},
    'autonomous_goals': {'id', 'session_id', 'objective', 'status', 'sources_json', 'notes', 'question', 'result', 'notice', 'created_at', 'updated_at'},
    'autonomous_steps': {'id', 'goal_id', 'sequence', 'action_json', 'observation_json', 'created_at'},
    'autonomous_handoffs': {'goal_id', 'parent_goal_id', 'context_json', 'created_at'},
    'companion_profile': {'id', 'revision', 'settings_json', 'updated_at'},
    'autonomous_delivery': {'goal_id', 'enabled'},
    'autonomous_artifacts': {'id', 'goal_id', 'name', 'file_key', 'content', 'sha256', 'size_bytes', 'status', 'error', 'created_at', 'verified_at'},
    'goal_reviews': {'goal_id', 'fingerprint', 'hidden_at'},
    'autonomous_csv_sources': {'goal_id', 'config_json'},
    'autonomous_csv_plans': {'id', 'goal_id', 'spec_json', 'mean_policy_json', 'status', 'report_json', 'report_sha256', 'error', 'exports_json', 'created_at'},
}


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    return db


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_database(path):
    db = readonly(path)
    try:
        objects = db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if any(kind not in {'table', 'index'} or (sql and 'VIRTUAL TABLE' in sql.upper()) for kind, _, sql in objects):
            raise ValueError('備份含不支援的資料庫物件。')
        actual_tables = {name for kind, name, _ in objects if kind == 'table'}
        if len(actual_tables & {'autonomous_goals', 'autonomous_steps'}) == 1:
            raise ValueError('自主目標資料表不完整。')
        if actual_tables & {'autonomous_handoffs', 'goal_reviews'} and 'autonomous_goals' not in actual_tables:
            raise ValueError('自主階段資料表不完整。')
        delivery_tables = {'autonomous_delivery', 'autonomous_artifacts'}
        if len(actual_tables & delivery_tables) == 1 or (delivery_tables <= actual_tables and 'autonomous_goals' not in actual_tables):
            raise ValueError('自主文件資料表不完整。')
        csv_tables = {'autonomous_csv_sources', 'autonomous_csv_plans'}
        if len(actual_tables & csv_tables) == 1 or (csv_tables <= actual_tables and 'autonomous_goals' not in actual_tables):
            raise ValueError('自主CSV資料表不完整。')
        if not set(TABLES) - {'task_runs', 'memory_sequence', 'local_memory_messages', 'answer_runs', 'autonomous_goals', 'autonomous_steps', 'autonomous_handoffs', 'companion_profile', 'goal_reviews'} - delivery_tables - csv_tables <= actual_tables <= set(TABLES):
            raise ValueError('資料庫格式不相容，請使用建立備份時的相容版本。')
        for name, columns in TABLES.items():
            if name not in actual_tables:
                continue
            if {row[1] for row in db.execute(f'PRAGMA table_info({name})')} != columns:
                raise ValueError('資料庫欄位不相容：' + name)
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('資料庫完整性檢查未通過。')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('資料庫關聯檢查未通過。')
        if csv_tables <= actual_tables:
            from .autonomy_csv import validate_saved
            validate_saved(db)
        if 'goal_reviews' in actual_tables:
            import re
            if any(not isinstance(r[0], str) or not re.fullmatch('[0-9a-f]{64}', r[0]) for r in db.execute('SELECT fingerprint FROM goal_reviews')):
                raise ValueError('回顧狀態指紋格式不符。')
        if delivery_tables <= actual_tables:
            from .artifacts import payload, MAX_FILES
            for row in db.execute('SELECT name,file_key,content,sha256,size_bytes,status FROM autonomous_artifacts'):
                data = payload(row[0], row[2])
                if row[1] != row[0].casefold() or hashlib.sha256(data).hexdigest() != row[3] or len(data) != row[4] or row[5] not in {'prepared','verified','uncertain'}:
                    raise ValueError('自主文件內容與紀錄不符。')
            if db.execute('SELECT 1 FROM autonomous_artifacts GROUP BY goal_id HAVING COUNT(*)>? LIMIT 1', (MAX_FILES,)).fetchone():
                raise ValueError('自主文件數量超過上限。')
        if 'companion_profile' in actual_tables:
            from .companion_profile import validate
            from .acceptance import strict_json
            profiles = db.execute('SELECT id,revision,settings_json FROM companion_profile').fetchall()
            if len(profiles) > 1 or any(row[0] != 1 or type(row[1]) is not int or row[1] < 1 for row in profiles):
                raise ValueError('夥伴設定版本不完整。')
            for row in profiles:
                validate(strict_json(row[2]))
        if 'autonomous_handoffs' in actual_tables and db.execute('''SELECT 1 FROM autonomous_handoffs h
            JOIN autonomous_goals child ON child.id=h.goal_id
            JOIN autonomous_goals parent ON parent.id=h.parent_goal_id
            WHERE child.session_id != parent.session_id OR child.id=parent.id LIMIT 1''').fetchone():
            raise ValueError('自主階段來源不屬於同一對話。')
        if 'memory_sequence' in actual_tables:
            counters = db.execute('SELECT id,last_id FROM memory_sequence').fetchall()
            largest = db.execute('SELECT COALESCE(MAX(id),0) FROM memories').fetchone()[0]
            if len(counters) != 1 or counters[0][0] != 1 or type(counters[0][1]) is not int or counters[0][1] < max(0, largest):
                raise ValueError('記憶編號狀態不完整。')
        return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in TABLES if name in actual_tables}
    finally:
        db.close()


def create_backup(database, destination=None):
    database = Path(database).resolve(strict=True)
    destination = Path(destination) if destination else database.parent / 'backups' / ('assistant-' + stamp() + '.paibackup')
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError('備份目的檔已存在，不會覆寫。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='backup-', dir=destination.parent) as tmp:
        snapshot = Path(tmp) / 'assistant.sqlite3'
        source, target = readonly(database), sqlite3.connect(snapshot)
        deadline = time.monotonic() + 60
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('資料庫忙碌，備份逾時，請稍後重試。')
            if total * page_size > MAX_DATABASE_BYTES:
                raise ValueError('目前備份上限為 512 MiB。')
        try:
            source.backup(target, pages=256, progress=progress, sleep=0.05)
        finally:
            source.close()
            target.close()
        counts = validate_database(snapshot)
        manifest = {'format': 'personal-ai-assistant-backup', 'format_version': 1,
                    'app_version': __version__, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'database_bytes': snapshot.stat().st_size, 'sha256': digest(snapshot), 'counts': counts}
        # Exclusive creation protects existing backups even if another process races us.
        with destination.open('xb') as output:
            try:
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(snapshot, 'assistant.sqlite3')
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                destination.unlink()
                raise
    return {'path': str(destination), **manifest}


def _unpack(backup, directory):
    with zipfile.ZipFile(backup) as archive:
        if sorted(archive.namelist()) != ['assistant.sqlite3', 'manifest.json']:
            raise ValueError('備份內容不符格式，未還原。')
        if archive.getinfo('manifest.json').file_size > 65536:
            raise ValueError('備份描述過大。')
        size = archive.getinfo('assistant.sqlite3').file_size
        if size > MAX_DATABASE_BYTES or size < 512:
            raise ValueError('備份資料庫大小不符合限制。')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('format') != 'personal-ai-assistant-backup' or manifest.get('format_version') != 1:
            raise ValueError('不支援的備份格式版本。')
        snapshot = Path(directory) / 'assistant.sqlite3'
        with archive.open('assistant.sqlite3') as source, snapshot.open('xb') as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if snapshot.stat().st_size != size or size != manifest.get('database_bytes') or digest(snapshot) != manifest.get('sha256'):
            raise ValueError('備份雜湊或大小不符，未還原。')
        counts = validate_database(snapshot)
        if counts != manifest.get('counts'):
            raise ValueError('備份資料筆數不符，未還原。')
        return snapshot, manifest


def inspect_backup(backup):
    with tempfile.TemporaryDirectory(prefix='assistant-check-') as tmp:
        _, manifest = _unpack(backup, tmp)
        return manifest


def restore_backup(backup, data_dir):
    data_dir = Path(data_dir).resolve()
    if data_dir.exists():
        raise FileExistsError('還原需要不存在的新資料夾，不會取代目前資料。')
    # Validate everything before creating the requested destination.
    with tempfile.TemporaryDirectory(prefix='assistant-restore-') as tmp:
        snapshot, manifest = _unpack(backup, tmp)
        data_dir.mkdir(parents=True, exist_ok=False)
        destination = data_dir / 'assistant.sqlite3'
        created = False
        try:
            with destination.open('xb') as target:
                created = True
                with snapshot.open('rb') as source:
                    shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            # Only our newly created file/directory; never recursively delete.
            if created and destination.exists():
                destination.unlink()
            if not any(data_dir.iterdir()):
                data_dir.rmdir()
            raise
    return {'data_dir': str(data_dir), **manifest}
