import json
import tempfile
import unittest
from pathlib import Path

from assistant import artifacts, autonomy, goal_comparison_handoff, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ComparisonHandoffTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.names = ['春桃 A2 的置物點交', '春桃 A20 的樣品核收']
        self.fields = ['箱號', '驗收台']
        self.parents = []
        for work, field, value in zip(self.names, self.fields, ['B-22', '榆光台']):
            gid = autonomy.create_goal(self.store, self.sid, work)
            self.parents.append(gid)
            autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=f'請提供{field}。')))
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, value, expected_fingerprint=view['fingerprint'])
        pairs = '和'.join(f'「{work}」的「{field}」' for work, field in zip(self.names, self.fields))
        question = f'比較{pairs}最後已確認的欄值。每件一行。'
        self.answer = Assistant(self.store, ScriptedEngine('歷史比較內容不可代替來源。')).ask(self.sid, question)
        self.objective = ('沿用剛才的比較表。交付 JSON 陣列，每列只包含'
                          '「工作」、「欄位」、「範圍」、「確認值」、「來源種類」、「來源編號」。')

    def rows(self, value='榆光台', kind='補充'):
        return json.dumps([
            {'工作': self.names[0], '欄位': self.fields[0], '範圍': '', '確認值': 'B-22', '來源種類': '補充', '來源編號': 1},
            {'工作': self.names[1], '欄位': self.fields[1], '範圍': '', '確認值': value, '來源種類': kind, '來源編號': 1},
        ], ensure_ascii=False)

    def test_comparison_requires_explicit_bring_in_and_can_limit_rows(self):
        for options, title in (({}, self.objective),
                               ({'from_chat': True}, '整理一份待辦清單。'),
                               ({'from_chat': True}, self.objective + '不要沿用剛才的比較表。')):
            gid = autonomy.create_goal(self.store, self.sid, title, **options)
            snapshot = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
            self.assertNotIn(goal_comparison_handoff.BASIS, snapshot or {})
        name, field = self.names[1], self.fields[1]
        gid = autonomy.create_goal(self.store, self.sid, f'沿用剛才的比較表。只帶入「{name}」的「{field}」。',
                                   from_chat=True)
        snapshot = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        basis = snapshot[goal_comparison_handoff.BASIS]
        self.assertEqual((basis['answer_id'], basis['indices']), (self.answer['id'], [1]))
        prompt = ScriptedEngine(action('finish', report='榆光台'))
        autonomy.run_goal(self.store, self.sid, gid, prompt, max_steps=1)
        payload = json.loads(prompt.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertEqual([row['goal_id'] for row in payload['verified_work_fields']], [self.parents[1]])
        self.assertNotIn('B-22', json.dumps(payload['verified_work_fields'], ensure_ascii=False))

    def test_comparison_with_uncounted_list_binds_latest_answer_on_restore(self):
        pairs = '\n'.join(f'「{work}」的「{field}」' for work, field in zip(self.names, self.fields))
        question = f'請對照下列任務各自最後一次已正式保存的欄值：\n{pairs}\n每件一行。'
        answer = Assistant(self.store, ScriptedEngine('B-22\n榆光台')).ask(self.sid, question)
        context = self.store.answer_context(answer['id'])
        self.assertEqual(context['current_request'], question)
        rows = context['autonomous_goals']['saved_work_fields']
        self.assertEqual([row['goal_id'] for row in rows], self.parents)
        self.assertEqual([row['value'] for row in rows], ['B-22', '榆光台'])

        gid = autonomy.create_goal(self.store, self.sid, self.objective, from_chat=True, file_delivery=True)
        snapshot = autonomy.get_goal(self.store, self.sid, gid)
        basis = snapshot['conversation_context'][goal_comparison_handoff.BASIS]
        self.assertEqual(basis['answer_id'], answer['id'])
        self.assertEqual(basis['indices'], [0, 1])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'natural')
        restored = Store(self.root / 'natural' / 'assistant.sqlite3')
        self.assertEqual(restored.answer_context(answer['id']), context)
        self.assertEqual(autonomy.get_goal(restored, self.sid, gid), snapshot)

    def test_comparison_checks_current_sources_and_historical_file_bytes(self):
        gid = autonomy.create_goal(self.store, self.sid, self.objective, from_chat=True, file_delivery=True)
        original_answer = self.store.answer_context(self.answer['id'])
        other_before = autonomy.get_goal(self.store, self.sid, self.parents[0])
        wrong = ScriptedEngine(action('write_artifact', filename='wrong.json', content=self.rows('別架')))
        state = autonomy.run_goal(self.store, self.sid, gid, wrong, max_steps=1)
        self.assertFalse(state['artifacts'])
        self.assertIn('現行欄值不一致', state['steps'][-1]['observation']['error'])

        def change_during_delivery():
            parent = self.parents[1]
            view = goal_guidance.snapshot(self.store, self.sid, parent)
            goal_guidance.save(self.store, self.sid, parent, '驗收台正式改為銀枝桌。',
                               expected_fingerprint=view['fingerprint'])
            return action('deliver', filenames=['handoff.json'], report='已交付。')

        initial = ScriptedEngine(action('write_artifact', filename='handoff.json', content=self.rows()),
                                 change_during_delivery)
        state = autonomy.run_goal(self.store, self.sid, gid, initial, max_steps=2)
        self.assertEqual(state['status'], 'paused')
        self.assertIn('剛變更', state['steps'][-1]['observation']['error'])
        old_bytes = (artifacts.directory(self.store, gid) / 'handoff.json').read_bytes()
        self.assertEqual(old_bytes, self.rows().encode('utf-8'))

        retry = ScriptedEngine(action('deliver', filenames=['handoff.json'], report='已交付。'),
                               action('write_artifact', filename='handoff-v2.json', content=self.rows('銀枝桌', '更正')),
                               action('deliver', filenames=['handoff-v2.json'], report='已交付新版。'))
        final = autonomy.run_goal(self.store, self.sid, gid, retry, max_steps=3)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('重新建立', final['steps'][-3]['observation']['error'])
        receipts = [item['observation'][goal_comparison_handoff.RECEIPT] for item in final['steps']
                    if goal_comparison_handoff.RECEIPT in item['observation']]
        self.assertEqual([item['rows'][1]['value'] for item in receipts], ['榆光台', '銀枝桌', '銀枝桌'])
        self.assertEqual(receipts[0]['rows'][0], receipts[-1]['rows'][0])
        checkpoint = create_backup(self.store.path)
        restore_backup(checkpoint['path'], self.root / 'restored')
        received = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(received.answer_context(self.answer['id']), original_answer)
        self.assertEqual(autonomy.get_goal(received, self.sid, self.parents[0]), other_before)
        self.assertEqual(autonomy.get_goal(received, self.sid, gid)['steps'], final['steps'])
        artifacts.rebuild(received, self.sid, gid)
        folder = artifacts.directory(received, gid)
        self.assertEqual((folder / 'handoff.json').read_bytes(), old_bytes)
        self.assertEqual((folder / 'handoff-v2.json').read_bytes(), self.rows('銀枝桌', '更正').encode('utf-8'))

        with received.connect() as db:
            row, = db.execute('SELECT observation_json FROM autonomous_steps WHERE goal_id=? AND sequence=2',
                              (gid,)).fetchone()
            modified = json.loads(row)
            modified[goal_comparison_handoff.RECEIPT]['rows'][1]['value'] = '混用'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(modified, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '歷史欄列'):
            validate_database(received.path)


if __name__ == '__main__':
    unittest.main()
