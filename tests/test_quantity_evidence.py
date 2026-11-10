import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy, goal_replies, quantity_evidence
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class QuantityEvidenceTests(unittest.TestCase):
    def test_missing_unit_keeps_original_number_and_binds_later_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            path = root / 'weights.txt'
            path.write_text('本批主料 1.20 公斤，配料 450（單位待填）。', encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, '核對本批重量')
            original = {'label': '本批配料', 'citation': f'[D{did}:L1]', 'quote': '配料 450'}
            main = {'label': '主料', 'citation': f'[D{did}:L1]', 'quote': '本批主料 1.20 公斤'}
            initial = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=1),
                action('check_quantities', unit='公斤', candidates=[main, original]),
                action('ask', question='本批配料的單位？', partial_result='主料 1.20 公斤，配料 450 待補單位。')))
            first = initial['steps'][1]['observation']
            self.assertEqual(first['status'], 'needs_evidence')
            self.assertEqual(first['possible_exact'], ['6/5'])
            self.assertEqual((first['quantities'][1]['source_exact'], first['quantities'][1]['status']),
                             ('450', 'unit_unconfirmed'))
            snapshot = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '本批配料的單位是公克。', expected_fingerprint=snapshot['fingerprint'])
            resolved = {**original, 'unit_evidence': {'citation': '[補充#3]', 'quote': '本批配料的單位是公克',
                                                      'unit': '公克'}}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_quantities', unit='公斤', candidates=[resolved]),
                action('calculate_expression', expression={'operation': 'sum', 'values': ['1.2', '0.45']}),
                action('finish', report='本批 1.65 公斤。')))
            checked = result['steps'][3]['observation']
            self.assertEqual((checked['status'], checked['possible_exact']), ('confirmed', ['9/20']))
            proof = checked['quantities'][0]
            self.assertIsNone(proof['source_unit'])
            self.assertEqual(proof['effective_unit'], '公克')
            self.assertEqual(proof['source_sha256'], first['quantities'][1]['source_sha256'])
            self.assertEqual(proof['unit_evidence']['source_sha256'], result['reply_sources'][0]['sha256'])
            self.assertEqual(result['steps'][4]['observation']['exact'], '33/20')
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            altered = copy.deepcopy(checked)
            altered['quantities'][0]['unit_evidence']['quote_start'] = 1
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                           (json.dumps(altered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '數量核對與原話'):
                validate_database(restored.path)

    def test_exact_equivalence_conflict_and_rejects_a_clipped_unit(self):
        specs = [
            {'label': '第一份', 'citation': '[目標]', 'quote': '前段容積 0.7 公升'},
            {'label': '第二份', 'citation': '[補充#1]', 'quote': '前段容積 700 mL'}]
        steps = [{'sequence': 1, 'action': {'tool': 'ask'}, 'observation': {}}]
        reply = {'citation': '[補充#1]', 'ask_sequence': 1, 'answer': '前段容積 700 mL。', 'sha256': 'a' * 64}
        args = {'unit': '公升', 'candidates': specs}
        equal = quantity_evidence.observe(args, steps, [], '前段容積 0.7 公升。', [reply])
        self.assertEqual((equal['status'], equal['possible_exact']), ('equivalent', ['7/10']))
        changed = {**reply, 'answer': '前段容積 750 mL。'}
        args['candidates'] = [specs[0], {**specs[1], 'quote': '前段容積 750 mL'}]
        unequal = quantity_evidence.observe(args, steps, [], '前段容積 0.7 公升。', [changed])
        self.assertEqual((unequal['status'], unequal['possible_exact']), ('different', ['7/10', '3/4']))
        with self.assertRaisesRegex(ValueError, '截斷'):
            quantity_evidence.observe({'unit': '公升', 'candidates': [
                {**specs[0], 'quote': '前段容積 0.7'}]}, steps, [], '前段容積 0.7 公升。')
        for text in ('不是公克。', '如果是A批，本批單位是公克。'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '完整且明確'):
                quantity_evidence.observe({'unit': '公斤', 'candidates': [
                    {'label': '本批配料', 'citation': '[目標]', 'quote': '配料 450',
                     'unit_evidence': {'citation': '[補充#1]', 'unit': '公克',
                                       'quote': '公克' if text.startswith('不是') else '本批單位是公克'}}]},
                    steps, [], '本批配料 450。', [{**reply, 'answer': text}])
