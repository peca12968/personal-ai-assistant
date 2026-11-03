"""On-demand bounded local search; results are not sent to a model or persisted."""
import math
import os
import stat
import time
from pathlib import Path

from .documents import EXTENSIONS, MAX_FILE_BYTES, STOP, chunks, read_document
from .docx_text import metadata
from .retrieval import terms
from .scope import check_path, is_link, select_root

EXCLUDED_DIRS = {'node_modules', 'venv', 'env', '__pycache__', 'dist', 'build', 'data', 'backups'}
EXCLUDED_FILES = {'auth.json', 'credentials.json', 'config.local.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock'}


def search_folder(folder, query, *, cancel=None, max_files=500, max_bytes=32 * 1024 * 1024,
                  max_entries=5000, max_depth=12, max_results=50, timeout=15):
    if not query.strip() or len(query) > 2000 or not (terms(query) - STOP):
        raise ValueError('請輸入 1–2000 字的有效搜尋詞。')
    if min(max_files, max_bytes, max_entries, max_results) <= 0 or max_depth < 0 or timeout <= 0:
        raise ValueError('搜尋上限需為正數。')
    root = select_root(folder)
    deadline = time.monotonic() + timeout
    query_terms = terms(query) - STOP
    report = {'root': str(root), 'query': query, 'results': [], 'entries_seen': 0,
              'files_read': 0, 'bytes_read_budget': 0, 'excluded': 0, 'unreadable': 0,
              'limits_hit': [], 'cancelled': False}
    pending = [(root, 0)]
    matches = []
    def stop():
        if cancel and cancel.is_set():
            report['cancelled'] = True
            return True
        if time.monotonic() >= deadline:
            if 'time' not in report['limits_hit']:
                report['limits_hit'].append('time')
            return True
        return False
    while pending and not stop():
        directory, depth = pending.pop()
        try:
            check_path(directory, root)
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if stop():
                        break
                    if report['entries_seen'] >= max_entries:
                        report['limits_hit'].append('entries')
                        pending.clear()
                        break
                    report['entries_seen'] += 1
                    entries.append(entry)
            entries.sort(key=lambda entry: entry.name.casefold())
        except (OSError, ValueError):
            report['unreadable'] += 1
            continue
        for entry in entries:
            if stop():
                break
            path = Path(entry.path)
            name = entry.name.casefold()
            try:
                info = entry.stat(follow_symlinks=False)
                if name.startswith('.') or is_link(info):
                    report['excluded'] += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if name in EXCLUDED_DIRS:
                        report['excluded'] += 1
                    elif depth >= max_depth:
                        if 'depth' not in report['limits_hit']:
                            report['limits_hit'].append('depth')
                    else:
                        pending.append((path, depth + 1))
                    continue
                if name in EXCLUDED_FILES or path.suffix.lower() not in EXTENSIONS or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    report['excluded'] += 1
                    continue
                if report['files_read'] >= max_files:
                    report['limits_hit'].append('files')
                    pending.clear()
                    break
                remaining = max_bytes - report['bytes_read_budget']
                if remaining <= 0:
                    report['limits_hit'].append('bytes')
                    pending.clear()
                    break
                cap = min(remaining, MAX_FILE_BYTES)
                if info.st_size > cap:
                    report['excluded'] += 1
                    if info.st_size <= MAX_FILE_BYTES and 'bytes' not in report['limits_hit']:
                        report['limits_hit'].append('bytes')
                    continue
                report['files_read'] += 1
                try:
                    document = read_document(path, scope=root, max_bytes=cap)
                except (ValueError, OSError):
                    # A failed decode/read may still consume bytes; reserve the full cap.
                    report['bytes_read_budget'] += cap
                    raise
                report['bytes_read_budget'] += document['size_bytes']
                relative = path.relative_to(root).as_posix()
                filename_score = len(query_terms & terms(relative))
                best = None
                for piece in chunks(document):
                    if stop():
                        break
                    content_terms = terms(piece['text']) - STOP
                    overlap = len(query_terms & content_terms)
                    score = filename_score + overlap / math.sqrt(max(1, len(content_terms)))
                    if score > 0 and (best is None or score > best[0]):
                        best = (score, piece)
                if best:
                    matches.append({'relative_path': relative, 'sha256': document['sha256'],
                                    'size_bytes': document['size_bytes'], 'score': best[0], **best[1], **metadata(document)})
            except (OSError, ValueError):
                report['unreadable'] += 1
    matches.sort(key=lambda row: (-row['score'], row['relative_path']))
    report['results'] = matches[:max_results]
    if len(matches) > max_results:
        report['limits_hit'].append('results')
    report['limits_hit'] = sorted(set(report['limits_hit']))
    return report


def attach_result(store, sid, report, result):
    # Revalidate both scope and content; never import silently changed search results.
    root = select_root(report['root'])
    path = root / result['relative_path']
    return store.attach_document(sid, path, scope=root, expected_sha256=result['sha256'])
