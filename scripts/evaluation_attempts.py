"""Private, once-only engine boundary evidence; never a grade or liveness receipt."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scripts.evaluation_reports import MAX_JSON_BYTES, MAX_TRACE_BYTES, canonical_digest, load_json


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def exclusive_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('引擎邊界紀錄超過32 MiB，未繼續呼叫。')
    # A partial write is deliberately retained: its existence forbids a repeat.
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        import os
        os.fsync(stream.fileno())


@contextmanager
def report_lease(path):
    """OS-held SQLite lock, not file existence; never unlink a lease file."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path.with_name(path.name + '.lease.sqlite3'), timeout=0, isolation_level=None)
    try:
        try:
            db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:
            if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise ValueError('此評估報告仍有本機流程持有工作鎖，未開始另一輪。') from None
            raise
        yield
    finally:
        db.close()


class AttemptJournal:
    def __init__(self, path, report):
        path = Path(path).resolve()
        self.folder = path.with_name(path.name + '.attempts')
        self.binding = {'schema': 'understanding_attempts_v1', 'report_path': str(path),
                        **{key: report[key] for key in ('created_at', 'fingerprint', 'case_ids', 'mode')}}
        self.run_hash = canonical_digest(self.binding)
        if self.folder.exists():
            if load_json(self.folder / 'binding.json') != self.binding:
                raise ValueError('引擎邊界紀錄屬於不同報告／來源，未覆寫或重試。')
        else:
            self.folder.mkdir()
            exclusive_json(self.folder / 'binding.json', self.binding)

    def paths(self, case_id):
        index = self.binding['case_ids'].index(case_id)
        return (self.folder / f'{index}.request.json', self.folder / f'{index}.outcome.json')

    def check(self, report):
        """Resume only never-attempted cases; do not reconstruct missing rows."""
        rows = {row['id']: row for row in report['results']}
        allowed = {'binding.json'}
        for case_id in self.binding['case_ids']:
            request_path, outcome_path = self.paths(case_id)
            allowed.update((request_path.name, outcome_path.name))
            row = rows.get(case_id)
            if row is None:
                if request_path.exists() or outcome_path.exists():
                    raise ValueError('有已嘗試但未加入報告的案例；保留失敗／中斷證據，不會由 --resume 重送。請使用新報告邊界。')
                continue
            request, outcome = load_json(request_path), load_json(outcome_path)
            if (not isinstance(request, dict) or not isinstance(outcome, dict)
                    or request.get('schema') != 'understanding_request_v1'
                    or request.get('run_sha256') != self.run_hash or request.get('id') != case_id
                    or request.get('fixture') != row['fixture']
                    or request.get('fixture_sha256') != row['fixture_sha256']
                    or request.get('request') != row['request']
                    or request.get('request_sha256') != row['request_sha256']
                    or outcome.get('schema') != 'understanding_outcome_v1'
                    or outcome.get('request_evidence_sha256') != canonical_digest(request)
                    or outcome.get('status') != 'returned'
                    or outcome.get('engine_answer') != row['engine_answer']
                    or outcome.get('engine_answer_sha256') != digest(row['engine_answer'])):
                raise ValueError('已完成案例與實際引擎邊界紀錄不同，未改寫報告。')
        if any(item.name not in allowed for item in self.folder.iterdir()):
            raise ValueError('引擎邊界資料夾含未知紀錄，未開始新呼叫。')

    def invoke(self, case, request, engine, cancel):
        if not isinstance(request, str) or len(request.encode('utf-8')) > MAX_TRACE_BYTES:
            raise ValueError('實際引擎請求需為不超過2 MiB的UTF-8文字。')
        request_path, outcome_path = self.paths(case['id'])
        if request_path.exists() or outcome_path.exists():
            raise ValueError('此案例已有引擎邊界紀錄，不會重送或覆寫。')
        before = {'schema': 'understanding_request_v1', 'run_sha256': self.run_hash,
                  'id': case['id'], 'fixture': case, 'fixture_sha256': canonical_digest(case),
                  'request': request, 'request_sha256': digest(request),
                  'captured_at': datetime.now(timezone.utc).isoformat()}
        before_hash = canonical_digest(before)
        exclusive_json(request_path, before)
        outcome = {'schema': 'understanding_outcome_v1',
                   'request_evidence_sha256': before_hash}
        try:
            raw = engine.answer(request, cancel)
        except BaseException as exc:
            # Includes explicit interruption. No raw answer is invented from an error.
            detail = str(exc).encode('utf-8', errors='replace')[:32768].decode('utf-8', errors='replace')
            code = getattr(exc, 'code', None)
            code = code[:200].encode('utf-8', errors='replace').decode('utf-8') if isinstance(code, str) else None
            outcome.update(status='error', error_type=type(exc).__name__, error=detail,
                           error_code=code)
            exclusive_json(outcome_path, outcome)
            raise
        reason = None
        if not isinstance(raw, str):
            reason = 'non_text'
        else:
            try:
                encoded = raw.encode('utf-8')
                if len(encoded) > MAX_TRACE_BYTES:
                    reason = 'output_too_large'
                elif not raw.strip():
                    reason = 'empty_response'
            except UnicodeError:
                reason = 'invalid_utf8'
        if reason:
            outcome.update(status='invalid_return', error_code=reason, return_type=type(raw).__name__)
            if reason == 'output_too_large':
                outcome.update(engine_answer_bytes=len(encoded), engine_answer_sha256=digest(raw))
            if isinstance(raw, str) and reason not in ('invalid_utf8', 'output_too_large'):
                outcome.update(engine_answer=raw, engine_answer_sha256=digest(raw))
            exclusive_json(outcome_path, outcome)
            raise ValueError('引擎未傳回可保存的完整文字：' + reason)
        outcome.update(status='returned', engine_answer=raw, engine_answer_sha256=digest(raw))
        exclusive_json(outcome_path, outcome)
        return raw
