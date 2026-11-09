import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class SourceDecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        document = self.root / 'rules.txt'
        document.write_text(
            '青甲批一｜正式｜高：另架。\n青甲批｜未採用｜高：提案架。\n'
            '青甲批｜正式｜高：甲架。\n青甲批｜正式｜低：前台。\n'
            '青甲批｜排除｜特殊。\n', encoding='utf-8')
        self.did = self.store.attach_document(self.sid, document)
        self.gid = autonomy.create_goal(self.store, self.sid, '核對青甲批位置')
        self.ranges = [{'document_id': self.did, 'start_line': 1, 'end_line': 5}]
        self.spec = {'scope': '青甲批', 'conditions': ['級別'], 'domains': {'級別': ['高', '低', '特殊']},
                     'known': {'級別': '高'}, 'ranges': self.ranges}

    def run_actions(self, *actions):
        return autonomy.run_goal(self.store, self.sid, self.gid, ScriptedEngine(*actions), max_steps=len(actions))

    def read_rules(self):
        return self.run_actions(action('read_ranges', ranges=self.ranges))

    def test_original_range_excludes_near_name_and_preserves_auditable_rows(self):
        self.read_rules()
        checked = self.run_actions(action('check_source_decision', **self.spec),
                                   action('finish', report=f'高級到甲架 [D{self.did}:L3]。'))
        observation = checked['steps'][1]['observation']
        self.assertEqual(observation['decision']['possible_results'], ['甲架'])
        self.assertEqual([row['status'] for row in observation['source_rows']],
                         ['other_scope', 'not_adopted', 'included', 'included', 'excluded'])
        self.assertEqual(observation['source_rows'][2]['table_row'], 1)
        self.assertEqual(observation['table']['excluded'], [['特殊']])
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid), checked)
        with restored.connect() as db:
            original = checked['steps'][0]['observation'].copy()
            excerpt = original['excerpts'][0]
            excerpt['text'] = excerpt['text'].replace('高：甲架。', '高：變更架。')
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=1',
                       (json.dumps(original, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '原文決策表'):
            validate_database(restored.path)

    def test_one_formal_correction_changes_one_row_only(self):
        self.read_rules()
        before = goal_guidance.snapshot(self.store, self.sid, self.gid)
        saved = goal_guidance.save(self.store, self.sid, self.gid, '青甲批｜更正｜高：強化架',
                                   expected_fingerprint=before['fingerprint'])
        self.assertEqual(saved['goal']['guidance_sources'][0]['citation'], '[更正#1]')
        result = self.run_actions(action('check_source_decision', **self.spec, amendment='[更正#1]'))
        observation = result['steps'][-1]['observation']
        self.assertEqual(observation['decision']['possible_results'], ['強化架'])
        self.assertEqual(observation['source_rows'][2]['status'], 'superseded')
        self.assertEqual(observation['table']['rows'][0], {'when': ['低'], 'result': '前台'})
        other = self.run_actions(action('check_source_decision', **{**self.spec, 'scope': '青甲批一'},
                                        amendment='[更正#1]'))
        self.assertEqual(other['steps'][-1]['action']['tool'], 'invalid')
        self.assertIn('完整範圍', other['steps'][-1]['observation']['error'])

    def test_requires_all_lines_and_an_exact_saved_answer_before_filling_a_gap(self):
        result = self.run_actions(action('check_source_decision', **self.spec))
        self.assertIn('尚未完整查讀', result['steps'][0]['observation']['error'])
        self.read_rules()
        narrowed = {**self.spec, 'ranges': [{'document_id': self.did, 'start_line': 1, 'end_line': 2}]}
        waiting = self.run_actions(action('check_source_decision', **narrowed),
                                   action('ask', question='高級的正式位置是什麼？'))
        self.assertEqual(waiting['steps'][-2]['observation']['decision']['coverage']['missing_rows'], [['高']])
        reply = goal_replies.snapshot(self.store, self.sid, self.gid)
        goal_replies.save(self.store, self.sid, self.gid, '青甲批｜更正｜高：補強架',
                          expected_fingerprint=reply['fingerprint'])
        result = self.run_actions(action('check_source_decision', **narrowed, amendment='[補充#4]'))
        observation = result['steps'][-1]['observation']
        self.assertEqual(observation['decision']['possible_results'], ['補強架'])
        self.assertEqual(observation['amendment']['citation'], '[補充#4]')
