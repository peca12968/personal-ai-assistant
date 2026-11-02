import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, document_diff as d
from assistant.backup import create_backup, restore_backup
from assistant.documents import check_citations
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def doc(did, content, raw=None):
    return {'id': did, 'name': f'version{did}.txt', 'content': content,
            'sha256': hashlib.sha256(raw if raw is not None else content.encode()).hexdigest(),
            'source_path': 'DO_NOT_REOPEN_PRIVATE_PATH'}


def args(old=1, new=2, offset=0):
    return {'old_document_id': old, 'new_document_id': new, 'offset': offset}


class DocumentDiffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def attach(self, name, content, sid=None):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, path)

    def test_insert_delete_replace_counts_ranges_and_original_provenance(self):
        documents = [doc(1, 'Title\nremoved\nanchor\nold cost\nsame\nend'),
                     doc(2, 'Title\nanchor\nnew cost\nsame\ninserted\nend')]
        result = d.compare(documents, args())
        self.assertTrue(result['comparison_complete'])
        self.assertTrue(result['all_changes_in_page'])
        self.assertTrue(result['page_content_complete'])
        self.assertEqual(result['total_changes'], 3)
        self.assertEqual(result['changed_line_counts'], {'old': 2, 'new': 2})
        self.assertEqual([c['type'] for c in result['changes']], ['delete', 'replace', 'insert'])
        self.assertEqual(result['changes'][0]['old_range'], {'start_line': 2, 'end_line': 2})
        self.assertIsNone(result['changes'][0]['new_range'])
        self.assertEqual(result['changes'][2]['new_range'], {'start_line': 5, 'end_line': 5})
        self.assertEqual(result['changes'][2]['old_anchor_after_line'], 5)
        for row in result['excerpts']:
            self.assertEqual(row['sha256'], documents[row['document_id'] - 1]['sha256'])
            self.assertFalse(row['partial_line'])
        self.assertNotIn('DO_NOT_REOPEN_PRIVATE_PATH', json.dumps(result))
        self.assertTrue(all(r['range_valid'] for r in check_citations('[D1:L2] [D1:L4] [D2:L3] [D2:L5]', result['excerpts'])))

    def test_line_equality_is_not_byte_or_decoded_equality(self):
        same = d.compare([doc(1, 'a\nb'), doc(2, 'a\nb')], args())
        self.assertTrue(same['line_text_equal'] and same['decoded_text_equal'] and same['byte_hash_equal'])
        for changed in ['a\r\nb', 'a\nb\n']:
            result = d.compare([doc(1, 'a\nb'), doc(2, changed)], args())
            self.assertTrue(result['line_text_equal'])
            self.assertFalse(result['decoded_text_equal'])
            self.assertFalse(result['byte_hash_equal'])
            self.assertEqual(result['total_changes'], 0)
            self.assertEqual(result['excerpts'], [])
        encoding = d.compare([doc(1, 'a\nb'), doc(2, 'a\nb', 'a\nb'.encode('utf-16'))], args())
        self.assertTrue(encoding['decoded_text_equal'])
        self.assertFalse(encoding['byte_hash_equal'])
        self.assertFalse(d.compare([doc(1, 'Case '), doc(2, 'case')], args())['line_text_equal'])

    def test_pagination_reports_full_totals_and_never_implies_empty_page_is_equal(self):
        old = '\n'.join(f'anchor{i}\nold{i}' for i in range(14))
        new = '\n'.join(f'anchor{i}\nnew{i}' for i in range(14))
        documents = [doc(1, old), doc(2, new)]
        changes, offsets = [], []
        offset = 0
        while offset is not None:
            result = d.compare(documents, args(offset=offset))
            offsets.append(offset)
            changes.extend(c['change_id'] for c in result['changes'])
            self.assertEqual(result['total_changes'], 14)
            self.assertEqual(result['changed_line_counts'], {'old': 14, 'new': 14})
            self.assertFalse(result['all_changes_in_page'])
            self.assertLessEqual(sum(len(e['text']) for e in result['excerpts']), 9600)
            offset = result['next_offset']
        self.assertEqual(offsets, [0, 6, 12])
        self.assertEqual(changes, list(range(1, 15)))
        empty = d.compare(documents, args(offset=14))
        self.assertEqual(empty['returned_changes'], 0)
        self.assertFalse(empty['line_text_equal'])
        self.assertEqual(empty['omitted_before'], 14)
        with self.assertRaises(ValueError):
            d.compare(documents, args(offset=15))

    def test_large_block_and_long_line_only_grant_actual_excerpt_coverage(self):
        documents = [doc(1, '\n'.join(f'old {i}' for i in range(90))),
                     doc(2, '\n'.join(f'new {i}' for i in range(90)))]
        result = d.compare(documents, args())
        self.assertTrue(result['comparison_complete'] and result['all_changes_in_page'])
        self.assertFalse(result['page_content_complete'])
        self.assertEqual(result['changes'][0]['old_next_line'], 41)
        self.assertEqual(result['changes'][0]['old_range']['end_line'], 90)
        self.assertEqual(result['excerpts'][0]['end_line'], 40)
        checks = check_citations('[D1:L1-L40] [D1:L41] [D2:L90]', result['excerpts'])
        self.assertEqual([r['range_valid'] for r in checks], [True, False, False])
        huge = d.compare([doc(1, 'a' * 1000), doc(2, 'b' * 1000)], args())
        self.assertEqual(huge['excerpts'], [])
        self.assertEqual(huge['changes'][0]['old_text_status'], 'not_provided')
        self.assertEqual(huge['changes'][0]['old_next_line'], 1)
        self.assertFalse(huge['page_content_complete'])

    def test_insert_at_start_delete_end_empty_side_and_no_l0_citation(self):
        inserted = d.compare([doc(1, 'a'), doc(2, 'prefix\na')], args())
        self.assertIsNone(inserted['changes'][0]['old_range'])
        self.assertEqual(inserted['changes'][0]['old_anchor_after_line'], 0)
        self.assertFalse(check_citations('[D1:L0]', inserted['excerpts'])[0]['range_valid'])
        removed = d.compare([doc(1, 'a\ntail'), doc(2, 'a')], args())
        self.assertIsNone(removed['changes'][0]['new_range'])
        empty = d.compare([doc(1, ''), doc(2, 'new')], args())
        self.assertEqual(empty['old_source']['line_count'], 0)
        self.assertEqual(empty['changed_line_counts'], {'old': 0, 'new': 1})

    def test_scope_and_strict_argument_validation(self):
        documents = [doc(1, 'public'), doc(2, 'public changed')]
        for value in [None, [], {}, {**args(), 'path': 'secret'}, args(old=True), args(new=-1),
                      args(new=1), args(offset=True), args(offset=-1), args(offset=4001), args(offset='0')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.validate_action({'tool': 'compare_documents', 'args': value, 'note': 'compare'})
        foreign = self.attach('foreign.txt', 'PRIVATE_OTHER_SESSION', self.store.new_session())
        local = self.attach('local.txt', 'local')
        with self.assertRaisesRegex(ValueError, '目前目標'):
            d.compare(self.store.documents(self.sid, True), args(local, foreign))
        with self.assertRaises(ValueError):
            d.compare(documents, args(new=999))

    def test_input_bounds_reject_before_alignment_instead_of_partial_success(self):
        for text in ['a' * 200001, '\n'.join('x' for _ in range(2001))]:
            with patch('assistant.document_diff.SequenceMatcher', side_effect=AssertionError('must not align')), self.assertRaises(ValueError):
                d.compare([doc(1, text), doc(2, 'small')], args())
        maximum = '\n'.join('same' for _ in range(2000))
        self.assertTrue(d.compare([doc(1, maximum), doc(2, maximum)], args())['line_text_equal'])

    def test_change_ranges_reconstruct_new_text_with_repeats_and_moves(self):
        for old, new in [('a\nb\na\nc', 'a\nc\na\nb'), ('x\nx\ny', 'x\ny\nx'), ('a\nb', 'c\nd')]:
            result = d.compare([doc(1, old), doc(2, new)], args())
            old_lines, new_lines = old.splitlines(), new.splitlines()
            rebuilt, cursor = [], 0
            for change in result['changes']:
                old_range, new_range = change['old_range'], change['new_range']
                start = old_range['start_line'] - 1 if old_range else change['old_anchor_after_line']
                rebuilt.extend(old_lines[cursor:start])
                if new_range:
                    rebuilt.extend(new_lines[new_range['start_line'] - 1:new_range['end_line']])
                cursor = old_range['end_line'] if old_range else start
            rebuilt.extend(old_lines[cursor:])
            self.assertEqual(rebuilt, new_lines)

    def test_goal_cannot_cite_metadata_or_unchanged_lines_without_reading(self):
        old = self.attach('old.txt', 'unchanged\nold value')
        new = self.attach('new.txt', 'unchanged\nnew value')
        gid = a.create_goal(self.store, self.sid, '比較兩份文件')
        engine = ScriptedEngine(action('compare_documents', **args(old, new)),
            action('finish', report=f'第一行不變。[D{old}:L1]'),
            action('read', document_id=old, start_line=1, end_line=1),
            action('finish', report=f'第一行不變。[D{old}:L1]；值已更改。[D{old}:L2][D{new}:L2]'))
        goal = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(goal['steps'][1]['action']['tool'], 'invalid')
        self.assertEqual(goal['status'], 'report_ready')
        self.assertTrue(all(c['range_valid'] for c in goal['steps'][-1]['observation']['citation_checks']))

    def test_imported_snapshot_not_original_path_and_source_change_stops_resume(self):
        old = self.attach('old.txt', 'original old')
        new = self.attach('new.txt', 'original new')
        gid = a.create_goal(self.store, self.sid, '比較')
        (self.root / 'old.txt').write_text('EXTERNAL_CHANGED_DO_NOT_READ', encoding='utf-8')
        first = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('compare_documents', **args(old, new))), max_steps=1)
        self.assertNotIn('EXTERNAL_CHANGED_DO_NOT_READ', json.dumps(first, ensure_ascii=False))
        self.assertIn('original old', json.dumps(first, ensure_ascii=False))
        self.store.attach_document(self.sid, self.root / 'old.txt')
        stopped = a.run_goal(self.store, self.sid, gid, None)
        self.assertEqual(stopped['steps'], first['steps'])
        self.assertEqual(stopped['status'], 'paused')
        self.assertIn('改變', stopped['notice'])

    def test_cancelled_alignment_does_not_commit_partial_observation(self):
        old = self.attach('old.txt', 'old')
        new = self.attach('new.txt', 'new')
        gid = a.create_goal(self.store, self.sid, '比較')
        cancel = threading.Event()
        def aligned():
            cancel.set()
            return [('replace', 0, 1, 0, 1)]
        with patch('assistant.document_diff.SequenceMatcher') as matcher:
            matcher.return_value.get_opcodes.side_effect = aligned
            with self.assertRaises(EngineError):
                a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('compare_documents', **args(old, new))), cancel=cancel)
        goal = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(goal['status'], 'paused')
        self.assertEqual(goal['steps'], [])

    def test_goal_results_survive_backup_with_source_hashes_and_excerpt_flags(self):
        old = self.attach('old.txt', '\n'.join('old' for _ in range(80)))
        new = self.attach('new.txt', '\n'.join('new' for _ in range(80)))
        gid = a.create_goal(self.store, self.sid, '比較')
        goal = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('compare_documents', **args(old, new))), max_steps=1)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        self.assertEqual(a.get_goal(Store(self.root / 'restored/assistant.sqlite3'), self.sid, gid), goal)
        self.assertFalse(goal['steps'][0]['observation']['page_content_complete'])


if __name__ == '__main__':
    unittest.main()
