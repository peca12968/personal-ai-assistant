import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assistant.engine import Assistant
from assistant.retrieval import terms
from assistant.store import Store


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.store = Store(self.path)

    def test_expiry_includes_end_date_and_excludes_next_day(self):
        mid = self.store.remember("專案位於 F 槽", expires_on="2026-11-02")
        self.assertEqual(self.store.retrieve("project", today="2026-11-02")[0]["id"], mid)
        self.assertEqual(self.store.retrieve("project", today="2026-11-03"), [])
        self.assertEqual(len(self.store.memories()), 1)  # Expiry never deletes data.

    def test_inactive_preference_not_injected_into_any_question(self):
        mid = self.store.remember("一律用英文回答", "preference", active=False)
        self.assertEqual(self.store.retrieve("hello"), [])
        self.store.set_memory_active(mid, True)
        self.assertEqual(self.store.retrieve("hello")[0]["id"], mid)

    def test_edit_replaces_old_fact_without_duplicate(self):
        mid = self.store.remember("專案存放 D 槽")
        self.store.edit_memory(mid, "專案存放 F 槽")
        rows = self.store.retrieve("project location")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "專案存放 F 槽")

    def test_invalid_edit_leaves_previous_content_intact(self):
        mid = self.store.remember("原始記憶")
        for expires in ("2026-02-30", "2026-1-02", "20261102", "tomorrow"):
            with self.subTest(expires=expires), self.assertRaises(ValueError):
                self.store.edit_memory(mid, "新內容", expires_on=expires)
        self.assertEqual(self.store.memories()[0]["content"], "原始記憶")

    def test_missing_memory_update_fails_clearly(self):
        with self.assertRaises(ValueError):
            self.store.edit_memory(999, "missing")
        with self.assertRaises(ValueError):
            self.store.set_memory_active(999, True)

    def test_general_preferences_do_not_crowd_out_relevant_fact(self):
        expected = self.store.remember("資料庫用 SQLite")
        for n in range(12):
            self.store.remember(f"偏好第 {n} 項", "preference")
        rows = self.store.retrieve("database", 4)
        self.assertEqual(rows[0]["id"], expected)
        self.assertLessEqual(sum(r["kind"] == "preference" for r in rows), 2)

    def test_zero_limit_returns_nothing(self):
        self.store.remember("總是繁體中文", "preference")
        self.assertEqual(self.store.retrieve("hello", 0), [])

    def test_aliases_do_not_match_substrings_in_english(self):
        for query in ("latest", "contest", "repositoryish", "networkingness"):
            self.assertFalse(any(t.startswith("@") for t in terms(query)), query)

    def test_identical_facts_are_not_repeated_in_context(self):
        self.store.remember("Python 3.12")
        self.store.remember("Ｐｙｔｈｏｎ 3.12")
        self.assertEqual(len(self.store.retrieve("python")), 1)

    def test_specific_memory_ranks_above_long_generic_list(self):
        expected = self.store.remember("資料庫為 SQLite")
        self.store.remember("資料庫、專案、備份、資料夾、圖片、瀏覽器、試算表、網路，全部都需要另外確認。")
        self.assertEqual(self.store.retrieve("database", 1)[0]["id"], expected)

    def test_answer_context_contains_current_memory_only(self):
        mid = self.store.remember("專案存放 D 槽")
        self.store.edit_memory(mid, "專案存放 F 槽")
        self.store.remember("專案存放 Z 槽", active=False)
        self.store.remember("專案存放 E 槽", expires_on="2000-01-01")
        class Capture:
            def answer(inner, prompt, cancel=None):
                inner.context = json.loads(prompt.split("以下 JSON 是使用者資料與當前問題：\n")[1])
                return "F 槽"
        engine = Capture()
        sid = self.store.new_session()
        result = Assistant(self.store, engine).ask(sid, "Where is my project?")
        self.assertEqual(result["memory_ids"], [mid])
        self.assertEqual([m["content"] for m in engine.context["memories"]], ["專案存放 F 槽"])
        self.assertTrue(engine.context["memories"][0]["updated_at"])


class MigrationTests(unittest.TestCase):
    def test_old_database_migrates_with_readable_backup_and_preserved_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT NOT NULL,
                kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO memories(content, kind) VALUES ('保留原始記憶', 'fact');""")
            db.close()
            store = Store(path)
            row = store.memories()[0]
            self.assertEqual(row["content"], "保留原始記憶")
            self.assertEqual(row["active"], 1)
            self.assertIsNone(row["expires_on"])
            store.edit_memory(row["id"], "編輯後記憶")
            backups = list((Path(tmp) / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            db = sqlite3.connect(backups[0])
            self.assertEqual(db.execute("SELECT content FROM memories").fetchone()[0], "保留原始記憶")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()
            Store(path)
            self.assertEqual(len(list((Path(tmp) / "backups").glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
