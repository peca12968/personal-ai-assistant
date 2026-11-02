import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from assistant.documents import MAX_FILE_BYTES, check_citations, chunks, read_document, retrieve_documents
from assistant.engine import Assistant
from assistant.store import Store


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.sid = self.store.new_session()

    def file(self, name="notes.txt", text="第一行\n第二行\n", encoding="utf-8"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_import_does_not_modify_original_and_hash_matches_bytes(self):
        path = self.file()
        original = path.read_bytes()
        did = self.store.attach_document(self.sid, path)
        row = self.store.documents(self.sid, True)[0]
        self.assertEqual(row["id"], did)
        self.assertEqual(row["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(path.read_bytes(), original)

    def test_utf16_bom_and_utf8_bom_are_supported(self):
        for encoding in ("utf-16", "utf-8-sig"):
            self.assertEqual(read_document(self.file(encoding=encoding))["content"].splitlines()[0], "第一行")

    def test_invalid_encoding_or_binary_is_rejected_without_partial_import(self):
        path = self.root / "binary.txt"
        for raw in (b"\xff\xff\xff", b"a\x00b", b"a\x01b"):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.store.attach_document(self.sid, path)
        self.assertEqual(self.store.documents(self.sid), [])

    def test_unsupported_and_empty_files_are_rejected(self):
        for name, content in (("word.docx", "fake"), ("empty.txt", " \n\t")):
            with self.assertRaises(ValueError):
                self.store.attach_document(self.sid, self.file(name, content))

    def test_large_file_is_rejected(self):
        path = self.file(text="x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.store.attach_document(self.sid, path)

    def test_file_count_limit_and_reload_do_not_duplicate(self):
        for i in range(8):
            self.store.attach_document(self.sid, self.file(f"{i}.txt"))
        self.store.attach_document(self.sid, self.root / "0.txt")
        with self.assertRaises(ValueError):
            self.store.attach_document(self.sid, self.file("overflow.txt"))
        self.assertEqual(len(self.store.documents(self.sid)), 8)

    def test_total_size_limit_is_enforced(self):
        for i in range(4):
            self.store.attach_document(self.sid, self.file(f"{i}.txt", "x" * MAX_FILE_BYTES))
        with self.assertRaises(ValueError):
            self.store.attach_document(self.sid, self.file("overflow.txt"))

    def test_snapshot_does_not_silently_follow_source_changes(self):
        path = self.file(text="版本甲")
        did = self.store.attach_document(self.sid, path)
        old_hash = self.store.documents(self.sid)[0]["sha256"]
        path.write_text("版本乙", encoding="utf-8")
        self.assertEqual(self.store.documents(self.sid, True)[0]["content"], "版本甲")
        self.assertEqual(self.store.attach_document(self.sid, path), did)
        self.assertEqual(self.store.documents(self.sid, True)[0]["content"], "版本乙")
        self.assertNotEqual(self.store.documents(self.sid)[0]["sha256"], old_hash)

    def test_duplicate_filenames_have_distinct_source_ids(self):
        a = self.store.attach_document(self.sid, self.file("a/notes.txt", "甲"))
        b = self.store.attach_document(self.sid, self.file("b/notes.txt", "乙"))
        self.assertNotEqual(a, b)

    def test_detach_is_session_scoped_and_preserves_original(self):
        path = self.file()
        did = self.store.attach_document(self.sid, path)
        other = self.store.new_session()
        self.store.detach_document(other, did)
        self.assertEqual(len(self.store.documents(self.sid)), 1)
        self.assertEqual(self.store.documents(other), [])
        self.store.detach_document(self.sid, did)
        self.assertEqual(self.store.documents(self.sid), [])
        self.assertTrue(path.exists())

    def test_chunks_preserve_blank_line_and_physical_line_numbers(self):
        pieces = chunks({"content": "甲\r\n\r\n乙\r\n丙"}, max_lines=2)
        self.assertEqual((pieces[0]["start_line"], pieces[0]["end_line"]), (1, 2))
        self.assertIn("L3: 乙", pieces[1]["text"])

    def test_long_line_is_split_and_marked_partial(self):
        pieces = chunks({"content": "x" * 4000})
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece["partial_line"] for piece in pieces))
        self.assertTrue(all(piece["start_line"] == 1 for piece in pieces))

    def test_query_finds_late_evidence_and_context_stays_bounded(self):
        text = "\n".join([f"普通內容 {i}" for i in range(500)] + ["資料庫的設定是霧嶺-908"])
        self.store.attach_document(self.sid, self.file(text=text))
        result = retrieve_documents("database 設定？", self.store.documents(self.sid, True))
        self.assertTrue(any("霧嶺-908" in row["text"] for row in result["excerpts"]))
        self.assertLessEqual(sum(len(row["text"]) for row in result["excerpts"]), 14000)
        self.assertLessEqual(len(result["excerpts"]), 8)
        self.assertTrue(result["coverage_limited"])

    def test_two_files_are_both_represented_in_comparison(self):
        ids = [self.store.attach_document(self.sid, self.file(f"{i}.txt", "備份政策：" + str(i))) for i in range(2)]
        result = retrieve_documents("比較 backup 政策", self.store.documents(self.sid, True))
        self.assertEqual({row["document_id"] for row in result["excerpts"]}, set(ids))

    def test_private_local_path_not_sent_to_engine(self):
        path = self.file("private-folder/notes.txt", "資料庫設定")
        self.store.attach_document(self.sid, path)
        result = Assistant(self.store, None).prepare_context(self.sid, "database")
        self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))

    def test_bad_citation_is_reported_and_snapshot_keeps_original_document(self):
        path = self.file(text="數量為 12。")
        did = self.store.attach_document(self.sid, path)
        class Engine:
            def answer(self, prompt, cancel=None):
                return "數量為 12 [D999:L300]。"
        result = Assistant(self.store, Engine()).ask(self.sid, "數量多少？")
        self.assertIn("無法對照", result["content"])
        self.assertFalse(self.store.answer_checks(result["id"])[0]["range_valid"])
        self.store.detach_document(self.sid, did)
        self.assertIn("12", self.store.answer_context(result["id"])["documents"]["excerpts"][0]["text"])


class CitationTests(unittest.TestCase):
    def test_valid_single_and_contiguous_ranges(self):
        excerpts = [{"document_id": 1, "start_line": 2, "end_line": 4}, {"document_id": 1, "start_line": 5, "end_line": 8}]
        checks = check_citations("[D1:L2] [D1:L3-L7]", excerpts)
        self.assertTrue(all(row["range_valid"] for row in checks))

    def test_unknown_missing_gap_reversed_and_malformed_citations_fail(self):
        excerpts = [{"document_id": 1, "start_line": 2, "end_line": 3}, {"document_id": 1, "start_line": 5, "end_line": 6}]
        checks = check_citations("[D8:L2] [D1:L1] [D1:L2-L6] [D1:L3-L2] [D1:wrong] [D1:L2-L999999999]", excerpts)
        self.assertEqual(len(checks), 6)
        self.assertTrue(all(not row["range_valid"] for row in checks))


if __name__ == "__main__":
    unittest.main()
