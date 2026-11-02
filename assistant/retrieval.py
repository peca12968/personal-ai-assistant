"""Bounded lexical retrieval, with explicit bilingual aliases (not embeddings)."""
import math
import re
import unicodedata
from collections import Counter

# Intentionally small and reviewable. Unknown concepts retain literal matching.
CONCEPTS = {
    "project": ("project", "projects", "專案", "项目"),
    "repository": ("repository", "repositories", "repo", "repos", "儲存庫", "存储库", "代码仓库"),
    "backup": ("backup", "backups", "備份", "备份"),
    "database": ("database", "databases", "資料庫", "数据库"),
    "folder": ("folder", "folders", "directory", "directories", "資料夾", "文件夹", "目錄", "目录"),
    "network": ("network", "networking", "網路", "網絡", "网络"),
    "browser": ("browser", "browsers", "瀏覽器", "浏览器"),
    "spreadsheet": ("spreadsheet", "spreadsheets", "試算表", "电子表格"),
    "image": ("image", "images", "圖片", "图片"),
    "timezone": ("timezone", "time zone", "時區", "时区"),
    "code": ("source code", "程式碼", "原始碼", "源代码", "代码"),
    "test": ("test", "tests", "testing", "測試", "测试"),
    "error": ("error", "errors", "錯誤", "错误"),
}


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def terms(text):
    text = normalize(text)
    tokens = set(re.findall(r"[a-z0-9_]+", text))
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(block[i:i + 2] for i in range(len(block) - 1))
        if len(block) == 1:
            tokens.add(block)
    for concept, aliases in CONCEPTS.items():
        for alias in aliases:
            # English aliases must match full words; 'test' must not match 'latest'.
            found = (re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", text)
                     if alias.isascii() else alias in text)
            if found:
                tokens.add("@" + concept)
                break
    return tokens


def rank_memories(query, rows, limit=8):
    if limit <= 0:
        return []
    query_terms = terms(query)
    indexed = [(row, terms(row["content"])) for row in rows]
    frequencies = Counter(token for _, tokens in indexed for token in tokens)
    ranked, fallback = [], []
    for row, tokens in indexed:
        shared = query_terms & tokens
        if shared:
            score = sum((2 if token.startswith("@") else 1) *
                        (1 + math.log((len(rows) + 1) / (frequencies[token] + 1)))
                        for token in shared) / math.sqrt(max(1, len(tokens)))
            ranked.append((score, row["id"], row))
        elif row["kind"] == "preference":
            fallback.append(row)
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # Reserve up to two general preferences without letting them fill all slots.
    fallback.sort(key=lambda row: row["id"], reverse=True)
    reserve = min(2, max(0, limit - 1), len(fallback)) if ranked else min(2, limit, len(fallback))
    candidates = [item[2] for item in ranked[:limit - reserve]] + fallback[:reserve]
    selected, seen = [], set()
    for row in candidates:
        content_key = normalize(row["content"]).strip()
        if content_key not in seen:
            selected.append(row)
            seen.add(content_key)
    return selected
