import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from assistant import autonomy, goal_required_reads, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalRequiredReadsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'state' / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def attach(self, name, content):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(self.sid, path)

    def test_ordinary_goal_fills_only_the_missing_characters_before_asking(self):
        doc = self.attach('register.txt', '窗口：白檯。\n' + '甲' * 9000 + '本批採冬版。')
        objective = ('依 register.txt 第 2 行尾決議。第 2 行是長行，'
                     '請把該行從第一字讀到最後一字。寫本批移交摘要。')
        gid = autonomy.create_goal(self.store, self.sid, objective)
        setup = ScriptedEngine(
            action('read', document_id=doc, start_line=1, end_line=1),
            action('read_line', document_id=doc, line_number=2, start_char=1),
            action('read_line', document_id=doc, line_number=2, start_char=8001),
        )
        before = autonomy.run_goal(self.store, self.sid, gid, setup, max_steps=3)
        self.assertEqual(before['status'], 'paused')
        backup = create_backup(self.store.path, self.root / 'before.paibackup')
        restore_backup(backup['path'], self.root / 'restored')
        store = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(store, self.sid, gid), before)
        recovery = ScriptedEngine(
            action('ask', question='要採哪版？', partial_result=f'窗口白檯。[D{doc}:L1]'),
            action('finish', report=f'本批採冬版。[D{doc}:L2]\n窗口白檯。[D{doc}:L1]'),
        )
        result = autonomy.run_goal(store, self.sid, gid, recovery, max_steps=2)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual([step['action']['tool'] for step in result['steps'][3:]], ['read_line', 'finish'])
        local = result['steps'][3]
        self.assertEqual(local['action']['args'], {'document_id': doc, 'line_number': 2, 'start_char': 4001})
        self.assertEqual(local['observation']['required_read_recovery'], {'before_tool': 'ask'})
        self.assertIn('required_source_reads', recovery.prompts[0])
        self.assertNotIn('required_source_reads', recovery.prompts[1])
        validate_database(store.path)

    def test_unavailable_lines_allow_a_focused_question_and_an_explicit_waiver(self):
        doc = self.attach('notice.txt', '窗口：曉桌。')
        gid = autonomy.create_goal(self.store, self.sid, f'請完整查讀 [D{doc}:L1-L2] 後再交付。')
        engine = ScriptedEngine(
            action('finish', report='完成。'),
            action('finish', report='完成。'),
            action('ask', question=f'請提供 D{doc} 第 2 行的原文。', partial_result=f'窗口：曉桌。[D{doc}:L1]'),
        )
        waiting = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=3)
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertEqual([step['action']['tool'] for step in waiting['steps']], ['read_ranges', 'invalid', 'ask'])
        self.assertIn('曉桌', waiting['partial_result'])
        current = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, f'這次不用再完整查讀 [D{doc}:L2]。',
                          expected_fingerprint=current['fingerprint'])
        goal = autonomy.get_goal(self.store, self.sid, gid, include_reply_positions=True)
        self.assertEqual(goal_required_reads.requirements(goal), [(doc, 1, 1)])
        resolved = autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('finish', report=f'現有公告的窗口：曉桌。[D{doc}:L1]')), max_steps=1)
        self.assertEqual(resolved['status'], 'report_ready')

    def test_explicit_ranges_recover_before_a_file_is_first_saved(self):
        doc = self.attach('list.txt', '既有窗口：榆臺。')
        gid = autonomy.create_goal(self.store, self.sid, f'先讀完 [D{doc}:L1] 再交付。輸出 list.md。', file_delivery=True)
        engine = ScriptedEngine(
            action('write_artifact', filename='list.md', content='未核對。'),
            action('write_artifact', filename='list.md', content=f'窗口：榆臺。[D{doc}:L1]'),
            action('deliver', filenames=['list.md'], report='交付清單。'),
        )
        result = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=3)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual([step['action']['tool'] for step in result['steps']], ['read_ranges', 'write_artifact', 'deliver'])
        self.assertEqual(result['steps'][0]['observation']['required_read_recovery'], {'before_tool': 'write_artifact'})
        validate_database(self.store.path)

    def test_explicit_sources_are_not_inferred_from_examples_or_ambiguous_names(self):
        sources = [{'document_id': 1, 'name': 'table.txt'}, {'document_id': 2, 'name': 'table.txt'}]
        for objective in (
            '若有需要，請完整查讀 [D1:L2]。',
            '例句：「請完整讀取 [D1:L2]」。',
            '請問能完整查讀 [D1:L2]？',
            '請完整查讀 table.txt 第 2 行。',
            '依 table.txt 第 2 行尾決議。第 2 行是長行，請把該行從第一字讀到最後一字。',
            '這次不用完整查讀 [D1:L2]。',
        ):
            with self.subTest(objective=objective):
                self.assertEqual(goal_required_reads.requirements({'objective': objective, 'sources': sources}), [])
        goal = {'objective': '先完整查讀 [D1:L2-L4]、[D2:L1]。', 'sources': sources,
                'guidance_sources': [{'notes_start': 5, 'content': '不用再完整查讀 [D1:L3]。'}]}
        self.assertEqual(goal_required_reads.requirements(goal), [(1, 2, 2), (1, 4, 4), (2, 1, 1)])


if __name__ == '__main__':
    unittest.main()
