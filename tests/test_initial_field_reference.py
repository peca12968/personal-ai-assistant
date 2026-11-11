import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from assistant import autonomy, decision_table, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from scripts.evaluate_goal_replies import prepare
from test_autonomy import ScriptedEngine, action


class InitialFieldReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        suite = Path(__file__).parents[1] / 'evals/goal_initial_field_reference.json'
        case = json.loads(suite.read_text(encoding='utf-8'))[1]
        self.store, self.sid, self.gid = prepare(self.root / 'assistant.sqlite3', case)
        fields = ['收樣班別', '驗入班別']
        shifts = ['早班', '晚班', '夜班']
        outcomes = [['前桌', '乙桌', '夜桌'], ['甲桌', '後桌', '夜桌'], ['丙桌', '丁桌', '夜桌']]
        self.table = {'conditions': fields, 'domains': {field: shifts for field in fields}, 'excluded': [],
                      'known': dict.fromkeys(fields, '早班'),
                      'rows': [{'when': [left, right], 'result': outcomes[i][j]}
                               for i, left in enumerate(shifts) for j, right in enumerate(shifts)]}

    def run_actions(self, *actions):
        engine = ScriptedEngine(*actions)
        return autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=len(actions))

    def reply(self, answer):
        current = goal_replies.snapshot(self.store, self.sid, self.gid)
        goal_replies.save(self.store, self.sid, self.gid, answer, expected_fingerprint=current['fingerprint'])
        return current['step_count']

    def guide(self, content):
        current = goal_guidance.snapshot(self.store, self.sid, self.gid)
        return goal_guidance.save(self.store, self.sid, self.gid, content, expected_fingerprint=current['fingerprint'])

    def read_documents(self):
        return self.run_actions(action('read_ranges', ranges=[
            {'document_id': row['id'], 'start_line': 1, 'end_line': 1} for row in self.store.documents(self.sid)]))

    def initial(self, source='[補充#1]'):
        return self.run_actions(action('check_decision', field_reference=source, initial_table=self.table))

    def test_early_reply_and_later_pre_table_update_keep_the_newer_shift_after_resolution_and_restore(self):
        self.run_actions(action('ask', question='本次現場有什麼變動？'))
        self.reply('正式改成晚班。')
        self.read_documents()
        self.guide('收樣班別正式改為夜班。')
        checked = self.initial()
        step = checked['steps'][-1]
        proof = step['observation']['field_reference']
        self.assertEqual(proof['base_step'], step['sequence'])
        self.assertEqual(proof['initial_table_sha256'], decision_table.digest(self.table))
        self.assertEqual(proof['source_sha256'], checked['reply_sources'][0]['sha256'])
        change, = proof['initial_condition_updates']
        self.assertEqual(change['evidence']['source_sha256'], checked['guidance_sources'][0]['sha256'])
        self.assertEqual(change['evidence']['quote'], '收樣班別正式改為夜班')
        self.assertEqual({branch['field']: branch['possible_results'] for branch in proof['branches']},
                         {'收樣班別': ['丙桌'], '驗入班別': ['丁桌']})
        ask = self.run_actions(action('ask', question=decision_table.reference_question(step)))
        self.assertEqual(ask['status'], 'needs_input')
        answer = self.reply('收樣班別')
        result = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        resolved = result['steps'][-1]['observation']
        self.assertEqual(resolved['possible_results'], ['丙桌'])
        self.assertEqual(resolved['field_reference']['initial_condition_updates'], [change])
        plan, _, _ = decision_table._recheck_saved(result['steps'], result['reply_sources'], result['guidance_sources'])
        self.assertEqual(plan[1]['known'], {'收樣班別': '夜班', '驗入班別': '早班'})
        saved = create_backup(self.store.path, self.root / 'verified.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), result)
        proof['initial_condition_updates'][0]['evidence']['source_sha256'] = '0' * 64
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_same_position_guidance_order_supersedes_both_candidates(self):
        self.run_actions(action('ask', question='本次現場有什麼變動？'))
        self.reply('正式改成晚班。')
        self.read_documents()
        self.guide('收樣班別正式改為夜班。')
        self.guide('驗入班別正式改為夜班。')
        checked = self.initial()
        proof = checked['steps'][-1]['observation']['field_reference']
        self.assertEqual(proof['status'], 'superseded')
        self.assertEqual([row['evidence']['citation'] for row in proof['initial_condition_updates']], ['[更正#1]', '[更正#2]'])
        self.assertEqual(checked['steps'][-1]['observation']['possible_results'], ['夜桌'])
        self.assertIsNone(decision_table.reference_question(checked['steps'][-1]))
        repeated = self.initial()
        self.assertEqual(repeated['steps'][-1]['action']['tool'], 'invalid')
        self.assertEqual(repeated['steps'][:-1], checked['steps'])

    def test_older_named_value_applies_before_ambiguous_change_and_multiple_ambiguous_sources_block(self):
        self.read_documents()
        self.guide('驗入班別正式改為夜班。')
        self.run_actions(action('ask', question='還有哪項現場變動？'))
        source = self.reply('正式改成晚班。')
        current = autonomy.get_goal(self.store, self.sid, self.gid)
        plan = decision_table.observe({'field_reference': f'[補充#{source}]', 'initial_table': self.table},
                                      current['steps'], current['reply_sources'], current['guidance_sources'])
        self.assertEqual(plan['field_reference']['status'], 'needs_field')
        self.assertEqual({branch['field']: branch['possible_results'] for branch in plan['field_reference']['branches']},
                         {'收樣班別': ['夜桌'], '驗入班別': ['乙桌']})
        self.run_actions(action('ask', question='最後再更正什麼？'))
        newer = self.reply('正式改成夜班。')
        checked = self.initial(f'[補充#{newer}]')
        self.assertEqual(checked['steps'][-1]['action']['tool'], 'invalid')
        self.assertEqual(checked['reply_sources'][0]['answer'], '正式改成晚班。')
        self.assertEqual(checked['reply_sources'][1]['answer'], '正式改成夜班。')
