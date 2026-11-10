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

    def test_declared_scope_requires_all_original_lines_but_accepts_split_ranges(self):
        self.gid = autonomy.create_goal(self.store, self.sid, f'決策規則範圍：[D{self.did}:L1-L5]\n交付青甲批')
        first = {'document_id': self.did, 'start_line': 1, 'end_line': 2}
        remainder = {'document_id': self.did, 'start_line': 3, 'end_line': 5}
        partial = self.run_actions(action('read_ranges', ranges=[first]))
        self.assertEqual(partial['decision_rule_scope']['missing_ranges'], [remainder])
        failed = self.run_actions(action('check_source_decision', **{**self.spec, 'ranges': [first]}),
                                  action('finish', report='過早定稿'))
        self.assertEqual([step['action']['tool'] for step in failed['steps'][-2:]], ['invalid', 'invalid'])
        self.assertIn('全部宣告', failed['steps'][-2]['observation']['error'])
        self.assertIn('全部宣告', failed['steps'][-1]['observation']['error'])
        final = self.run_actions(action('read_ranges', ranges=[remainder]),
                                 action('check_source_decision', **{**self.spec, 'ranges': [remainder, first]}),
                                 action('finish', report='青甲批位置為甲架。'))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['decision_rule_scope']['missing_ranges'], [])
        self.assertEqual(final['steps'][-2]['observation']['declared_ranges'], self.ranges)
        self.assertEqual(final['steps'][-1]['observation']['source_check_step'], 5)
        self.assertEqual(final['steps'][-2]['observation']['source_rows'][0]['citation'], f'[D{self.did}:L1]')

    def test_new_correction_needs_a_fresh_scope_check_and_receipt_survives_restore(self):
        self.gid = autonomy.create_goal(self.store, self.sid, f'決策規則範圍：[D{self.did}:L1-L5]\n交付青甲批')
        self.read_rules()
        self.run_actions(action('check_source_decision', **self.spec))
        review = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '青甲批｜更正｜高：新架',
                           expected_fingerprint=review['fingerprint'])
        final = self.run_actions(action('finish', report='仍為舊值'),
                                 action('check_source_decision', **self.spec, amendment='[更正#1]'),
                                 action('finish', report='現為新架 [更正#1]。'))
        self.assertEqual(final['steps'][2]['action']['tool'], 'invalid')
        self.assertEqual(final['steps'][-1]['observation']['source_check_step'], 4)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid), final)
        with restored.connect() as db:
            observation = final['steps'][-1]['observation'].copy()
            observation['source_check_step'] = 2
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '缺少相符'):
            validate_database(restored.path)

    def test_malformed_or_unattached_declaration_does_not_create_goal(self):
        before = autonomy.goals(self.store, self.sid)
        for raw in ('決策規則範圍：待補', f'決策規則範圍：[D{self.did}:L1-L2]、[D{self.did}:L2]', '決策規則範圍：[D99:L1]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                autonomy.create_goal(self.store, self.sid, raw + '\n核對位置')
        self.assertEqual(autonomy.goals(self.store, self.sid), before)

    def version_rules(self, lines):
        path = self.root / 'versions.txt'
        path.write_text('\n'.join(lines), encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        self.gid = autonomy.create_goal(self.store, self.sid, f'決策規則範圍：[D{did}:L1-L{len(lines)}]\n核對青甲批')
        spec = {**self.spec, 'domains': {'級別': ['高', '低']},
                'ranges': [{'document_id': did, 'start_line': 1, 'end_line': len(lines)}]}
        self.run_actions(action('read_ranges', ranges=spec['ranges']))
        return did, spec

    def test_explicit_adoption_uses_exact_batch_and_retains_shared_rules(self):
        did, spec = self.version_rules([
            '青甲批｜v1｜正式｜高：甲架。', '青甲批｜v2｜正式｜高：乙架。',
            '青甲批｜正式｜低：前台。', '青甲批｜採用版本｜v1。',
            '青甲批一｜採用版本｜v2。',
        ])
        result = self.run_actions(action('check_source_decision', **spec), action('finish', report='本批採甲架。'))
        observation = result['steps'][1]['observation']
        self.assertEqual(observation['decision']['possible_results'], ['甲架'])
        self.assertEqual(observation['adopted_version'], {'name': 'v1', 'citations': [f'[D{did}:L4]']})
        self.assertEqual(observation['table']['rows'], [{'when': ['高'], 'result': '甲架'},
                                                        {'when': ['低'], 'result': '前台'}])
        self.assertEqual([row['status'] for row in observation['source_rows']],
                         ['included', 'other_version', 'included', 'version_adopted', 'other_scope'])
        self.assertEqual(result['steps'][-1]['observation']['source_check_step'], 2)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid), result)

    def test_no_adoption_keeps_both_versions_and_checks_relevant_assignments(self):
        _, spec = self.version_rules([
            '青甲批｜v1｜正式｜高：甲架。', '青甲批｜v1｜正式｜低：前台。',
            '青甲批｜v2｜正式｜高：乙架。', '青甲批｜v2｜正式｜低：前台。',
        ])
        result = self.run_actions(action('check_source_decision', **spec),
                                  action('check_source_decision', **{**spec, 'known': {'級別': '低'}}))
        high, low = [step['observation'] for step in result['steps'][-2:]]
        self.assertNotIn('adopted_version', high)
        self.assertEqual(high['decision']['coverage']['conflicting_rows'][0]['results'], ['甲架', '乙架'])
        self.assertEqual(low['decision']['possible_results'], ['前台'])
        self.assertEqual(low['source_rows'][1]['table_row'], low['source_rows'][3]['table_row'])

    def test_conflicting_and_missing_adoption_cannot_select_a_default(self):
        for adopted, message in [(['v1', 'v2'], '互相衝突'), (['v3'], '缺少本批所採用版本')]:
            with self.subTest(adopted=adopted):
                _, spec = self.version_rules([
                    '青甲批｜v1｜正式｜高：甲架。', '青甲批｜v2｜正式｜高：乙架。',
                    *(f'青甲批｜採用版本｜{version}。' for version in adopted),
                ])
                result = self.run_actions(action('check_source_decision', **spec))
                self.assertEqual(result['steps'][-1]['action']['tool'], 'invalid')
                self.assertIn(message, result['steps'][-1]['observation']['error'])
