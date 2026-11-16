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

    def test_joined_originals_are_read_before_an_unneeded_question(self):
        roster = self.attach('場務與保管.txt', '校準交接\n參與者：段晴、林青。')
        room = self.attach('時段、房間.txt', '藍石室\n正式時段：14:25 到 15:00。')
        objective = f'先完整查讀「場務與保管.txt」第 2 行 與 [D{room}:L2] 再提問。交付本批驗收摘要。'
        gid = autonomy.create_goal(self.store, self.sid, objective)
        answer = f'段晴、林青。[D{roster}:L2]\n時段 14:25 到 15:00。[D{room}:L2]'
        result = autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('ask', question='正式時段是哪一段？'),
            action('finish', report=answer),
        ), max_steps=2)
        self.assertEqual(result['status'], 'report_ready')
        read, delivered = result['steps']
        self.assertEqual(read['observation']['required_read_recovery'], {'before_tool': 'ask'})
        self.assertEqual(read['action']['args']['ranges'], [
            {'document_id': roster, 'start_line': 2, 'end_line': 2},
            {'document_id': room, 'start_line': 2, 'end_line': 2},
        ])
        self.assertEqual(delivered['action']['tool'], 'finish')
        self.assertEqual(result['result'], answer)

    def test_joined_waiver_from_a_saved_reply_survives_restore(self):
        front = self.attach('前場.txt', '前場桌：榆臺。')
        back = self.attach('後場.txt', '後場桌：曉臺。')
        objective = f'先讀完 [D{front}:L1-L2] 和 [D{back}:L1-L2] 再交付。'
        gid = autonomy.create_goal(self.store, self.sid, objective)
        waiting = autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('finish', report='前後場摘要。'),
            action('ask', question='請提供前場與後場的第 2 行原文。'),
        ), max_steps=2)
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertEqual(waiting['steps'][0]['observation']['required_read_recovery'], {'before_tool': 'finish'})
        current = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid,
                          f'這次不用再完整查讀 [D{front}:L2] 以及 後場.txt 第 2 行。',
                          expected_fingerprint=current['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'waiver.paibackup')
        restore_backup(backup['path'], self.root / 'restored')
        store = Store(self.root / 'restored' / 'assistant.sqlite3')
        restored = autonomy.get_goal(store, self.sid, gid, include_reply_positions=True)
        self.assertEqual(goal_required_reads.requirements(restored), [(front, 1, 1), (back, 1, 1)])
        answer = f'前場榆臺。[D{front}:L1]\n後場曉臺。[D{back}:L1]'
        done = autonomy.run_goal(store, self.sid, gid, ScriptedEngine(action('finish', report=answer)), max_steps=1)
        self.assertEqual(done['result'], answer)
        validate_database(store.path)

    def test_source_joiners_apply_only_between_complete_exact_references(self):
        sources = [{'document_id': 1, 'name': '甲和乙.txt'}, {'document_id': 2, 'name': '丙、丁.txt'},
                   {'document_id': 3, 'name': 'D9 第 1 行.txt'}, {'document_id': 4, 'name': '甲和乙.txt'}]
        objective = '請完整查讀 [D1:L2]及「丙、丁.txt」第 2 行以及 D9 第 1 行.txt 第 4 行。'
        self.assertEqual(goal_required_reads.requirements({'objective': objective, 'sources': sources}),
                         [(1, 2, 2), (2, 2, 2), (3, 4, 4)])
        for target in (
            '[D1:L1] 或 [D2:L1]',
            '[D1:L1] 和甲和乙.txt 第 2 行',
            '[D1:L1] 以及未知.txt 第 2 行',
            '[D1:L1] 與',
            '[D1:L1] 及 [D2:L4-L3]',
            '[D1:L1] 和 [D2:L1] 再問值班人',
        ):
            with self.subTest(target=target):
                self.assertEqual(goal_required_reads.requirements({
                    'objective': f'請完整查讀 {target}。', 'sources': sources,
                }), [])
        for objective in (
            '若有需要，請完整查讀 [D1:L1] 與 [D2:L1]。',
            '例句：「請完整查讀 [D1:L1] 和 [D2:L1]」。',
            '請問能完整查讀 [D1:L1] 以及 [D2:L1]？',
        ):
            with self.subTest(objective=objective):
                self.assertEqual(goal_required_reads.requirements({'objective': objective, 'sources': sources}), [])

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
