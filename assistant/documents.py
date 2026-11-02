"""Read explicitly selected text files and retrieve attributable, bounded excerpts."""
import hashlib
import math
import os
import re
import stat
from pathlib import Path

from .retrieval import terms

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SESSION_BYTES = 8 * 1024 * 1024
MAX_DOCUMENTS = 8
EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".tsv", ".log", ".yaml", ".yml", ".toml", ".js", ".ts", ".html", ".css"}
STOP = {"the", "is", "a", "an", "of", "in", "to", "and", "what", "this", "請問", "我的", "什麼", "文件", "這份", "整理", "哪些"}


def read_document(path, *, scope=None, max_bytes=MAX_FILE_BYTES):
    if scope is not None:
        from .scope import check_path
        check_path(path, scope)
    path = Path(path).expanduser().resolve(strict=True)
    if path.suffix.lower() not in EXTENSIONS:
        raise ValueError("目前支援文字與程式檔；PDF、Word 請先另存為 UTF-8 文字。")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("請選擇一般檔案。")
    max_bytes = min(max_bytes, MAX_FILE_BYTES)
    if info.st_size > max_bytes:
        raise ValueError("每個文件最多 2 MiB。")
    with path.open("rb") as stream:
        if scope is not None:
            from .scope import verify_open_file
            verify_open_file(stream, scope)
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("請選擇一般檔案。")
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("每個文件最多 2 MiB。")
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        content = raw.decode(encoding)
    except UnicodeError:
        raise ValueError("無法讀取文字編碼，請另存為 UTF-8 或含 BOM 的 UTF-16。") from None
    if "\x00" in content or any(ord(char) < 32 and char not in "\n\r\t\f" for char in content):
        raise ValueError("檔案包含非文字資料，未匯入。")
    if not content.strip():
        raise ValueError("文件沒有可分析的文字。")
    return {"name": path.name, "source_path": str(path), "path_key": os.path.normcase(str(path)),
            "content": content, "sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": encoding, "size_bytes": len(raw)}


def chunks(document, max_chars=1400, max_lines=30):
    output, buffer = [], []
    length = 0
    def flush():
        nonlocal buffer, length
        if buffer:
            output.append({"start_line": buffer[0][0], "end_line": buffer[-1][0],
                           "text": "\n".join(f"L{n}: {line}" for n, line in buffer), "partial_line": False})
            buffer, length = [], 0
    for number, line in enumerate(document["content"].splitlines(), 1):
        if len(line) > max_chars:
            flush()
            for offset in range(0, len(line), max_chars):
                output.append({"start_line": number, "end_line": number,
                               "text": f"L{number}: " + line[offset:offset + max_chars], "partial_line": True})
        else:
            if buffer and (length + len(line) > max_chars or len(buffer) >= max_lines):
                flush()
            buffer.append((number, line))
            length += len(line) + 1
    flush()
    return output


def retrieve_documents(query, documents, max_excerpts=8, max_chars=14000):
    query_terms = terms(query) - STOP
    all_ranked, per_document, catalog = [], [], []
    pieces_by_id, lines_by_id = {}, {}
    for document in documents:
        pieces = chunks(document)
        pieces_by_id[document['id']] = pieces
        lines_by_id[document['id']] = document['content'].splitlines()
        catalog.append({"document_id": document["id"], "name": document["name"], "sha256": document["sha256"],
                        "total_lines": len(document["content"].splitlines()), "total_chunks": len(pieces)})
        ranked = []
        for index, piece in enumerate(pieces):
            tokens = terms(piece["text"]) - STOP
            score = len(tokens & query_terms) / math.sqrt(max(1, len(tokens)))
            row = {**piece, "document_id": document["id"], "name": document["name"],
                   "sha256": document["sha256"], "matched_query": score > 0}
            ranked.append((score, -index, document["id"], row))
        ranked.sort(key=lambda item: item[:3], reverse=True)
        if ranked:
            per_document.append(ranked[0])
            all_ranked.extend(ranked[1:])
    # Include each selected file before allocating more excerpts to a single file.
    candidates = sorted(per_document, key=lambda item: item[:3], reverse=True) + sorted(all_ranked, key=lambda item: item[:3], reverse=True)
    selected, used, selected_keys = [], 0, set()
    for _, negative_index, document_id, row in candidates:
        if len(selected) >= max_excerpts:
            break
        if used + len(row["text"]) <= max_chars:
            selected.append(row)
            selected_keys.add((document_id, -negative_index))
            used += len(row["text"])
    # Preserve the ranked selection and file representation first. Spend only
    # remaining space on nearby full lines, where scope/exception sentences can
    # fall just across a chunk boundary. Never stitch across an omitted line.
    blocked = set()
    for _ in range(2):
        for direction in (1, -1):
            for index, row in enumerate(selected):
                if row['partial_line'] or (index, direction) in blocked:
                    continue
                line_number = row['end_line'] + 1 if direction == 1 else row['start_line'] - 1
                lines = lines_by_id[row['document_id']]
                already_present = any(other['document_id'] == row['document_id'] and
                    other['start_line'] <= line_number <= other['end_line'] for other in selected)
                if not 1 <= line_number <= len(lines) or already_present:
                    blocked.add((index, direction))
                    continue
                line = lines[line_number - 1]
                addition = f'L{line_number}: {line}'
                if len(line) > 1400 or used + len(addition) + 1 > max_chars:
                    blocked.add((index, direction))
                    continue
                if direction == 1:
                    row['text'] += '\n' + addition
                    row['end_line'] = line_number
                else:
                    row['text'] = addition + '\n' + row['text']
                    row['start_line'] = line_number
                row['neighbor_lines_added'] = row.get('neighbor_lines_added', 0) + 1
                used += len(addition) + 1
    selected.sort(key=lambda row: (row["document_id"], row["start_line"]))
    # Expansion can finish a document without selecting every original chunk.
    # Partial long-line pieces count only when that exact piece was selected.
    complete_ranges = {}
    for row in selected:
        if not row['partial_line']:
            ranges = complete_ranges.setdefault(row['document_id'], [])
            if ranges and row['start_line'] <= ranges[-1][1] + 1:
                ranges[-1][1] = max(ranges[-1][1], row['end_line'])
            else:
                ranges.append([row['start_line'], row['end_line']])
    limited = any((did, index) not in selected_keys and not any(
        start <= piece['start_line'] and end >= piece['end_line']
        for start, end in complete_ranges.get(did, []))
        for did, pieces in pieces_by_id.items() for index, piece in enumerate(pieces))
    return {"catalog": catalog, "excerpts": selected,
            "coverage_limited": limited}


def check_citations(answer, excerpts):
    """Check only source/range existence, never claim semantic support."""
    checks = []
    for match in re.finditer(r"\[D\d+[^\]\r\n]{0,80}\]", answer):
        token = match.group()
        parsed = re.fullmatch(r"\[D(\d+):L(\d+)(?:-L(\d+))?\]", token)
        valid = False
        if parsed:
            doc_id, start = int(parsed[1]), int(parsed[2])
            end = int(parsed[3] or parsed[2])
            cursor = start
            intervals = sorted((row["start_line"], row["end_line"]) for row in excerpts if row["document_id"] == doc_id)
            if start >= 1 and end >= start:
                for low, high in intervals:
                    if low <= cursor <= high:
                        cursor = high + 1
                valid = cursor > end
        checks.append({"citation": token, "range_valid": valid})
    return checks
