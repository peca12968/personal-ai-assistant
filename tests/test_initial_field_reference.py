import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from assistant import autonomy, decision_table, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.goal_stages import digest
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

    def save_same_step_sources(self, *, guidance_first):
        self.run_actions(action('ask', question='本次現場有什麼變動？'))
        if guidance_first:
            self.guide('收樣班別正式改為夜班。')
        self.reply('正式改成晚班。')
        if not guidance_first:
            self.guide('收樣班別正式改為夜班。')

    def check_reference_after_table(self):
        self.run_actions(action('check_decision', **self.table), action('ask', question='現場有什麼變動？'))
        source = self.reply('正式改成晚班。')
        checked = self.run_actions(action('check_decision', field_reference=f'[補充#{source}]'))
        return decision_table.reference_question(checked['steps'][-1])

    def prepare_document_rules(self, *texts):
        documents = []
        for number, text in enumerate(texts):
            path = self.root / f'notice-{number}.txt'
            path.write_text(text, encoding='utf-8')
            documents.append(self.store.attach_document(self.sid, path))
        self.gid = autonomy.create_goal(self.store, self.sid, '本次為「甲批」。核對班別及去向。')
        self.check_reference_after_table()
        return documents

    def read_document_rule(self, text):
        document_id, = self.prepare_document_rules(text)
        self.run_actions(action('read', document_id=document_id, start_line=1, end_line=1))
        return {'document_rule': f'[D{document_id}:L1]', 'scope': '甲批'}

    def check_document_versions(self, *, missing=False, read_second=True, targets=(('西桌', '西桌'), ('南桌', '南桌'))):
        variants = tuple(
            f'本輪「甲批」的「v{number}」版規則。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
            f'原「甲桌」改為「{left}」。其他配對沿用原表。\n'
            f'本輪「甲批」的「v{number}」版規則。若「收樣班別」為「早班」、「驗入班別」為「晚班」，'
            f'原「乙桌」改為「{right}」。其他配對沿用原表。'
            for number, (left, right) in enumerate(targets, 1))
        if missing:
            variants = variants[:1]
        docs = self.prepare_document_rules(*variants, '本輪「甲批」正式採用版本「v1」。\n本輪「甲批」正式採用版本「v2」。')
        if not read_second:
            docs = [docs[0], docs[-1]]
        ranges = [{'document_id': doc, 'start_line': 1, 'end_line': 2} for doc in docs]
        self.run_actions(action('read_ranges', ranges=ranges),
                         action('check_decision', document_rules=[f'[D{doc}:L{line}]' for doc in docs for line in (1, 2)], scope='甲批'))
        return {'question': '本次「甲批」正式採用哪個版本？請回覆「v1」或「v2」。', 'document_version': True}

    def test_version_first_then_field_preserves_both_actual_answers(self):
        question = self.check_document_versions(targets=(('西桌', '西桌'), ('北桌', '南桌')))
        current = autonomy.get_goal(self.store, self.sid, self.gid)
        plan = decision_table.document_question_plan(current['steps'][-1])
        self.assertEqual(plan, {'first': 'version', 'cases': 4, 'costs': {
            'field': {'worst_case': 2, 'total_questions': 8}, 'version': {'worst_case': 2, 'total_questions': 6}}})
        self.assertIsNone(decision_table.reference_question_for_steps(current['steps']))
        self.run_actions(action('ask', **question))
        version = f'[補充#{self.reply("v2")}]'
        checked = self.run_actions(action('check_decision', version_confirmation=version))
        self.assertEqual(checked['steps'][-1]['observation']['field_reference']['status'], 'needs_field')
        field_question = decision_table.reference_question_for_steps(checked['steps'])
        self.run_actions(action('ask', question=field_question))
        field = f'[補充#{self.reply("收樣班別")}]'
        resolved = self.run_actions(action('check_decision', field_reference=field, resolve=True))
        observation = resolved['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['北桌'])
        proof = observation['field_reference']
        self.assertEqual(proof['version_confirmation']['reply_citation'], version)
        self.assertEqual(proof['confirmation']['citation'], field)

    def test_field_first_asks_version_only_if_the_answer_leaves_different_results(self):
        for field_name, expected in [('收樣班別', '西桌'), ('驗入班別', '南桌')]:
            with self.subTest(field_name=field_name):
                version_question = self.check_document_versions(targets=(('西桌', '北桌'), ('西桌', '南桌')))
                current = autonomy.get_goal(self.store, self.sid, self.gid)
                plan = decision_table.document_question_plan(current['steps'][-1])
                self.assertEqual(plan['first'], 'field')
                self.assertEqual(plan['costs']['field']['total_questions'], 6)
                self.assertIsNone(decision_table.document_version_question(current['steps'], current['reply_sources'], ()))
                self.run_actions(action('ask', question=decision_table.reference_question_for_steps(current['steps'])))
                field = f'[補充#{self.reply(field_name)}]'
                resolved = self.run_actions(action('check_decision', field_reference=field, resolve=True))
                question = decision_table.document_version_question(resolved['steps'], resolved['reply_sources'], ())
                if field_name == '收樣班別':
                    self.assertIsNone(question)
                else:
                    self.assertEqual(question, version_question)
                    self.run_actions(action('ask', **question))
                    version = f'[補充#{self.reply("v2")}]'
                    resolved = self.run_actions(action('check_decision', version_confirmation=version))
                    self.assertEqual(resolved['steps'][-1]['observation']['field_reference']['version_confirmation']['reply_citation'], version)
                observation = resolved['steps'][-1]['observation']
                self.assertEqual(observation['possible_results'], [expected])
                self.assertEqual(observation['field_reference']['confirmation']['citation'], field)

    def test_later_version_rules_bind_original_selection_and_restore(self):
        question = self.check_document_versions(read_second=False)
        self.run_actions(action('ask', **question))
        reply = f'[補充#{self.reply("v2")}]'
        selected = self.run_actions(action('check_decision', version_confirmation=reply))
        selection = selected['steps'][-1]
        self.assertNotIn('possible_results', selection['observation'])
        document = next(row['id'] for row in self.store.documents(self.sid) if row['name'] == 'notice-1.txt')
        citations = [f'[D{document}:L1]', f'[D{document}:L2]']
        finished = self.run_actions(action('read', document_id=document, start_line=1, end_line=2),
                                    action('check_decision', version_rules=citations))
        step = finished['steps'][-1]
        self.assertEqual(step['observation']['possible_results'], ['南桌'])
        proof = step['observation']['field_reference']
        self.assertEqual(proof['version_confirmation'], selection['observation']['field_reference']['version_confirmation'])
        receipt = proof['version_rules']
        self.assertEqual(receipt['confirmation_sha256'], digest(selection))
        self.assertEqual([record['evidence']['citation'] for record in receipt['records']], citations)
        saved = create_backup(self.store.path, self.root / 'later-version-rules.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), finished)
        receipt['records'].pop()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def prepare_waiting_version_at_the_north_gate(self, index):
        suite = Path(__file__).parents[1] / 'evals/goal_waiting_document_version.json'
        case = json.loads(suite.read_text(encoding='utf-8'))[index]
        self.store, self.sid, self.gid = prepare(self.root / f'waiting-version-{index}.sqlite3', case)
        fields = ['採樣班別', '核驗班別', '入口']
        pairs = [('早班', '早班'), ('晚班', '晚班'), ('晚班', '早班'), ('早班', '晚班')]
        north = ('前桌', '後桌', '甲桌', '乙桌')
        south = ('會合席',) * 4 if index == 0 else ('前席', '後席', '甲席', '乙席')
        self.table = {'conditions': fields, 'domains': {fields[0]: ['早班', '晚班'], fields[1]: ['早班', '晚班'],
                                                      '入口': ['北口', '南口']}, 'excluded': [],
                      'known': dict(zip(fields, ('早班', '早班', '北口'))),
                      'rows': [{'when': [*pair, gate], 'result': result}
                               for gate, results in [('北口', north), ('南口', south)]
                               for pair, result in zip(pairs, results, strict=True)]}
        self.check_reference_after_table()
        ids = [3, 4, 5]
        scope = ('池灣 A6', '杉澗 B7')[index]
        checked = self.run_actions(action('read_ranges', ranges=[{'document_id': did, 'start_line': 1, 'end_line': 2}
                                                                  for did in ids]),
                                   action('check_decision', document_rules=[f'[D{did}:L{line}]' for did in ids for line in (1, 2)], scope=scope))
        ask = decision_table.document_version_question(checked['steps'], checked['reply_sources'], ())
        return self.run_actions(action('ask', **ask, partial_result='北口暫表，兩版去向不同。'))

    def test_waiting_version_question_is_withdrawn_or_replaced_and_restore_rechecks_it(self):
        for index, target in [(0, '會合席'), (1, '甲席')]:
            with self.subTest(index=index):
                old = self.prepare_waiting_version_at_the_north_gate(index)
                stale = goal_replies.snapshot(self.store, self.sid, self.gid)
                self.guide('入口正式改為南口。')
                amended = autonomy.get_goal(self.store, self.sid, self.gid)
                self.assertEqual(amended['steps'][:len(old['steps'])], old['steps'])
                self.assertEqual(amended['reply_sources'], old['reply_sources'])
                withdrawn, = amended['withdrawn_questions']
                self.assertEqual(withdrawn['question'], old['question'])
                step = amended['steps'][len(old['steps'])]
                review = step['observation']['field_reference']['waiting_version_recheck']
                self.assertTrue(review['withdraws_question'])
                with self.assertRaisesRegex(ValueError, '已有變更|已變更'):
                    goal_replies.save(self.store, self.sid, self.gid, 'v2', expected_fingerprint=stale['fingerprint'])
                if index == 0:
                    self.assertEqual(amended['status'], 'paused')
                    self.assertEqual(step['observation']['possible_results'], [target])
                else:
                    self.assertEqual(amended['status'], 'needs_input')
                    self.assertEqual(amended['question'], review['next_question'])
                    self.assertIn('甲席', amended['partial_result'])
                    self.assertIn('乙席', amended['partial_result'])
                    source = f'[補充#{self.reply("採樣班別")}]'
                    amended = self.run_actions(action('check_decision', field_reference=source, resolve=True))
                    self.assertEqual(amended['steps'][-1]['observation']['possible_results'], [target])
                saved = create_backup(self.store.path, self.root / f'version-waiting-{index}.paibackup')
                restored = self.root / f'restored-waiting-{index}'
                restore_backup(saved['path'], restored)
                backup = Store(restored / 'assistant.sqlite3')
                self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), amended)
                with backup.connect() as db:
                    db.execute('DELETE FROM autonomous_question_withdrawals WHERE goal_id=?', (self.gid,))
                with self.assertRaisesRegex(ValueError, '版本重核缺少相符'):
                    validate_database(backup.path)

    def test_waiting_version_review_keeps_necessary_question_and_uses_named_correction(self):
        question = self.check_document_versions()
        asked = self.run_actions(action('ask', **question))
        self.guide('收樣班別正式改為早班。')
        amended = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(amended['steps'], asked['steps'])
        self.assertEqual(amended['question'], asked['question'])
        preview = amended['reference_review']
        self.assertIn('前桌', decision_table.describe_waiting_review(preview))
        self.assertFalse(preview['field_reference']['waiting_version_recheck']['withdraws_question'])
        version = f'[補充#{self.reply("v2")}]'
        resolved = self.run_actions(action('check_decision', version_confirmation=version))
        proof = resolved['steps'][-1]['observation']['field_reference']
        self.assertEqual(proof['overwritten_fields'], ['收樣班別'])
        self.assertEqual([branch['possible_results'] for branch in proof['branches']], [['前桌'], ['南桌']])
        receipt = proof['version_confirmation']
        self.assertEqual([item['citation'] for item in receipt['source_order']], ['[更正#1]', version])
        self.assertEqual(receipt['condition_changes'][0]['evidence']['quote'], '收樣班別正式改為早班')

    def test_missing_version_rules_offer_version_first_without_guessing_question_count(self):
        question = self.check_document_versions(missing=True, targets=(('西桌', '北桌'), ('南桌', '南桌')))
        current = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(decision_table.document_question_plan(current['steps'][-1]),
                         {'first': 'version', 'reason': 'missing_version_rules'})
        self.assertIsNone(decision_table.reference_question_for_steps(current['steps']))
        self.assertEqual(decision_table.document_version_question(current['steps'], current['reply_sources'], ()), question)
        self.run_actions(action('ask', **question))
        source = f'[補充#{self.reply("v1")}]'
        resolved = self.run_actions(action('check_decision', version_confirmation=source))
        self.assertIsNotNone(decision_table.reference_question_for_steps(resolved['steps']))

    def test_document_version_confirmation_preserves_actual_reply_and_later_correction_on_restore(self):
        question = self.check_document_versions()
        partial = 'v1 去西桌，v2 去南桌。版本待確認。'
        asked = self.run_actions(action('ask', **question, partial_result=partial))
        self.assertEqual(asked['status'], 'needs_input')
        self.assertEqual(asked['partial_result'], partial)
        number = self.reply('v2')
        self.guide('本輪「甲批」正式採用版本「v1」。')
        citation = f'[補充#{number}]'
        resolved = self.run_actions(action('check_decision', version_confirmation=citation))
        step = resolved['steps'][-1]
        observation = step['observation']
        self.assertEqual(observation['possible_results'], ['西桌'])
        receipt = observation['field_reference']['version_confirmation']
        self.assertEqual((receipt['version'], receipt['reply_citation']), ('v1', citation))
        self.assertEqual(receipt['question_sha256'], digest(question['question']))
        self.assertEqual([(row['citation'], row['version']) for row in receipt['sources']], [(citation, 'v2'), ('[更正#1]', 'v1')])
        saved = create_backup(self.store.path, self.root / 'document-version-confirmation.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        receipt['sources'].reverse()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(observation, ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_document_version_confirmation_rejects_same_version_answer_to_an_independent_question(self):
        self.check_document_versions()
        self.run_actions(action('ask', question='本次回報表單採用哪個版號？'))
        number = self.reply('v2')
        rejected = self.run_actions(action('check_decision', version_confirmation=f'[補充#{number}]'))
        self.assertEqual(rejected['steps'][-1]['action']['tool'], 'invalid')
        self.assertIn('正式採用問題的實際答覆', rejected['steps'][-1]['observation']['error'])

    def test_selected_document_version_without_read_rules_keeps_destination_unresolved(self):
        question = self.check_document_versions(missing=True)
        self.run_actions(action('ask', **question))
        number = self.reply('本輪「甲批」正式採用版本「v2」。')
        resolved = self.run_actions(action('check_decision', version_confirmation=f'[補充#{number}]'))
        observation = resolved['steps'][-1]['observation']
        self.assertNotIn('possible_results', observation)
        self.assertEqual(observation['max_questions'], 0)
        proof = observation['field_reference']
        self.assertEqual(proof['version_confirmation']['status'], 'missing_rules')
        self.assertEqual(proof['version_confirmation']['version'], 'v2')
        self.assertEqual(proof['branches'][0]['version_results'], [{'version': 'v2', 'status': 'missing_rules'}])
        self.assertIsNone(decision_table.document_version_question(resolved['steps'], resolved['reply_sources'], ()))

    def test_later_document_reads_rebuild_original_rows_and_preserve_conflicting_results(self):
        docs = self.prepare_document_rules(
            '本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，原「甲桌」改為「聯絡席」。其他配對沿用原表。',
            '本輪「甲批」正式生效。若「收樣班別」為「早班」、「驗入班別」為「晚班」，原「乙桌」改為「聯絡席」。其他配對沿用原表。',
            '本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，原「甲桌」改為「西桌」。其他配對沿用原表。')
        citations = [f'[D{document}:L1]' for document in docs]
        self.run_actions(action('read', document_id=docs[0], start_line=1, end_line=1),
                         action('check_decision', document_rule=citations[0], scope='甲批'))
        common = self.run_actions(action('read', document_id=docs[1], start_line=1, end_line=1),
                                  action('check_decision', document_rules=citations[:2], scope='甲批'))
        self.assertEqual(common['steps'][-1]['observation']['possible_results'], ['聯絡席'])
        disputed = self.run_actions(action('read', document_id=docs[2], start_line=1, end_line=1),
                                    action('check_decision', document_rules=citations, scope='甲批'))
        step = disputed['steps'][-1]
        proof = step['observation']['field_reference']
        self.assertEqual(proof['status'], 'needs_field')
        branches = {branch['field']: branch for branch in proof['branches']}
        self.assertEqual(branches['收樣班別']['recorded_results'], ['聯絡席', '西桌'])
        self.assertEqual(branches['驗入班別']['possible_results'], ['聯絡席'])
        receipt = proof['document_rules']
        self.assertEqual(receipt['base_step'], 3)
        conflict, = [row for row in receipt['rows'] if row['status'] == 'conflicting']
        self.assertEqual(conflict['before'], '甲桌')
        self.assertEqual(conflict['results'], [{'result': '聯絡席', 'citations': citations[:1]},
                                               {'result': '西桌', 'citations': citations[2:]}])
        self.run_actions(action('ask', question='這次改成晚班的是收樣班別還是驗入班別？'))
        answer = self.reply('收樣班別')
        checked = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        self.assertEqual(checked['steps'][-1]['observation']['status'], 'incomplete_table')
        saved = create_backup(self.store.path, self.root / 'multiple-document-rules.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), checked)
        conflict['results'].pop()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_duplicate_document_results_preserve_both_citations_without_creating_a_conflict(self):
        line = '本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，原「甲桌」改為「乙桌」。其他配對沿用原表。'
        docs = self.prepare_document_rules(line, line)
        citations = [f'[D{document}:L1]' for document in docs]
        checked = self.run_actions(action('read_ranges', ranges=[{'document_id': document, 'start_line': 1, 'end_line': 1}
                                                                for document in docs]),
                                   action('check_decision', document_rules=citations, scope='甲批'))
        observation = checked['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['乙桌'])
        row, = observation['field_reference']['document_rules']['rows']
        self.assertEqual(row['results'], [{'result': '乙桌', 'citations': citations}])

    def test_document_version_requires_explicit_same_scope_adoption(self):
        line = '本輪「甲批」的「v1」版規則。若「收樣班別」為「晚班」、「驗入班別」為「早班」，原「甲桌」改為「西桌」。其他配對沿用原表。'
        docs = self.prepare_document_rules(line, line.replace('v1', 'v2').replace('西桌', '乙桌'),
                                           '本輪「乙批」正式採用版本「v1」。\n本輪「甲批」正式採用版本「v2」。')
        citations = [f'[D{document}:L1]' for document in docs[:2]]
        self.run_actions(action('read_ranges', ranges=[{'document_id': document, 'start_line': 1, 'end_line': 1}
                                                       for document in docs]))
        offered = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertIsNone(decision_table.document_rule_candidate(
            offered['steps'], offered['reply_sources'], (), offered['objective'], offered['sources']))
        adoption = f'[D{docs[2]}:L2]'
        checked = self.run_actions(action('read', document_id=docs[2], start_line=2, end_line=2),
                                   action('check_decision', document_rules=[*citations, adoption], scope='甲批'))
        observation = checked['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['乙桌'])
        receipt = observation['field_reference']['document_rules']
        self.assertEqual(receipt['adopted_version'], {'version': 'v2', 'citations': [adoption]})
        self.assertEqual([record['evidence']['citation'] for record in receipt['records']], [*citations, adoption])
        self.assertEqual(receipt['rows'][0]['results'], [{'result': '乙桌', 'citations': citations[1:]}])

    def test_conflicting_adoption_keeps_a_shared_result_only_when_all_versions_resolve(self):
        rule = ('本輪「甲批」的「v1」版規則。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
                '原「甲桌」改為「乙桌」。其他配對沿用原表。')
        docs = self.prepare_document_rules(rule, rule.replace('v1', 'v2'),
                                           '本輪「甲批」正式採用版本「v1」。\n本輪「甲批」正式採用版本「v2」。')
        ranges = [{'document_id': doc, 'start_line': 1, 'end_line': 2 if doc == docs[-1] else 1} for doc in docs]
        citations = [f'[D{doc}:L1]' for doc in docs] + [f'[D{docs[-1]}:L2]']
        checked = self.run_actions(action('read_ranges', ranges=ranges),
                                   action('check_decision', document_rules=citations, scope='甲批'))
        observation = checked['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['乙桌'])
        proof = observation['field_reference']
        self.assertEqual(proof['status'], 'agreement')
        review = proof['document_rules']['version_review']
        self.assertEqual(review['status'], 'conflicting_adoption')
        self.assertFalse(review['field_changes_result'])
        self.assertNotIn('adopted_version', proof['document_rules'])
        self.assertEqual([version['adoption_citations'] for version in review['versions']], [[x] for x in citations[-2:]])
        self.assertTrue(all(branch['version_results'] == [
            {'version': version, 'status': 'resolved', 'possible_results': ['乙桌']} for version in ('v1', 'v2')]
                            for branch in proof['branches']))
        self.run_actions(action('finish', report='兩種欄名及兩個版本都去乙桌，欄名待查。'))
        self.guide('收樣班別正式改為夜班。')
        updated = autonomy.get_goal(self.store, self.sid, self.gid)
        step = updated['steps'][-1]
        self.assertEqual(step['observation']['max_questions'], 1)
        self.assertTrue(step['observation']['field_reference']['document_rules']['version_review']['field_changes_result'])
        self.assertIsNotNone(decision_table.reference_question_for_steps(updated['steps']))

    def test_adopted_version_with_missing_rules_can_be_completed_by_a_later_read(self):
        rule = ('本輪「甲批」的「v1」版規則。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
                '原「甲桌」改為「西桌」。其他配對沿用原表。')
        docs = self.prepare_document_rules(rule, '本輪「甲批」正式採用版本「v2」。',
                                           rule.replace('v1', 'v2').replace('西桌', '乙桌'))
        citations = [f'[D{doc}:L1]' for doc in docs]
        missing = self.run_actions(action('read_ranges', ranges=[{'document_id': doc, 'start_line': 1, 'end_line': 1}
                                                                 for doc in docs[:2]]),
                                   action('check_decision', document_rules=citations[:2], scope='甲批'))
        observation = missing['steps'][-1]['observation']
        self.assertEqual(observation['max_questions'], 0)
        self.assertIsNone(decision_table.reference_question_for_steps(missing['steps']))
        self.assertNotIn('possible_results', observation)
        self.assertEqual(observation['field_reference']['branches'][0]['version_results'],
                         [{'version': 'v2', 'status': 'missing_rules'}])
        checked = self.run_actions(action('read', document_id=docs[2], start_line=1, end_line=1),
                                   action('check_decision', document_rules=citations, scope='甲批'))
        observation = checked['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['乙桌'])
        self.assertEqual(observation['field_reference']['document_rules']['adopted_version'],
                         {'version': 'v2', 'citations': [citations[1]]})

    def test_late_conflicting_adoption_invalidates_old_agreement_and_replays_missing_version(self):
        rule = ('本輪「甲批」的「v1」版規則。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
                '原「甲桌」改為「乙桌」。其他配對沿用原表。')
        docs = self.prepare_document_rules(rule, '本輪「甲批」正式採用版本「v1」。', '本輪「甲批」正式採用版本「v2」。')
        citations = [f'[D{doc}:L1]' for doc in docs]
        old = self.run_actions(action('read_ranges', ranges=[{'document_id': doc, 'start_line': 1, 'end_line': 1}
                                                             for doc in docs[:2]]),
                               action('check_decision', document_rules=citations[:2], scope='甲批'))
        self.assertEqual(old['steps'][-1]['observation']['possible_results'], ['乙桌'])
        disputed = self.run_actions(action('read', document_id=docs[2], start_line=1, end_line=1),
                                    action('check_decision', document_rules=citations, scope='甲批'))
        step = disputed['steps'][-1]
        self.assertEqual(step['observation']['max_questions'], 0)
        self.assertNotIn('possible_results', step['observation'])
        branch = step['observation']['field_reference']['branches'][0]
        self.assertEqual(branch['status'], 'version_uncertain')
        self.assertEqual(branch['recorded_results'], ['乙桌'])
        self.assertEqual(branch['version_results'][-1], {'version': 'v2', 'status': 'missing_rules'})
        self.assertEqual(disputed['steps'][:len(old['steps'])], old['steps'])
        saved = create_backup(self.store.path, self.root / 'disputed-document-versions.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), disputed)
        branch['version_results'].pop()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_effective_document_rule_requires_recheck_and_merges_branches_on_restore(self):
        args = self.read_document_rule('本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
                                       '原「甲桌」改為「乙桌」。其他配對沿用原表。')
        resolved = self.run_actions(action('ask', question='這次改成晚班的是收樣班別還是驗入班別？'),
                                    action('finish', report='沿用舊去向。'), action('check_decision', **args))
        self.assertEqual([s['action']['tool'] for s in resolved['steps'][-3:]], ['invalid', 'invalid', 'check_decision'])
        step = resolved['steps'][-1]
        self.assertEqual(step['observation']['possible_results'], ['乙桌'])
        proof = step['observation']['field_reference']
        self.assertEqual(proof['status'], 'agreement')
        receipt = proof['document_rule']
        self.assertEqual(receipt['evidence']['citation'], args['document_rule'])
        self.assertEqual((receipt['before'], receipt['after']), ('甲桌', '乙桌'))
        current, _, _ = decision_table._recheck_saved(resolved['steps'], resolved['reply_sources'], ())
        self.assertEqual(current[1]['known'], self.table['known'])
        self.assertEqual([row for n, row in enumerate(current[1]['rows'], 1) if n != receipt['row']],
                         [row for n, row in enumerate(self.table['rows'], 1) if n != receipt['row']])
        saved = create_backup(self.store.path, self.root / 'document-rule.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        with backup.connect() as db:
            db.execute('UPDATE autonomous_goals SET objective=? WHERE id=?', ('本次為「乙批」。核對班別及去向。', self.gid))
        with self.assertRaisesRegex(ValueError, '目前目標明示範圍'):
            validate_database(backup.path)

    def test_effective_document_rule_keeps_the_other_branch_and_binds_real_reply(self):
        args = self.read_document_rule('本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，'
                                       '原「甲桌」改為「西桌」。其他配對沿用原表。')
        checked = self.run_actions(action('check_decision', **args))
        proof = checked['steps'][-1]['observation']['field_reference']
        self.assertEqual({b['field']: b['possible_results'] for b in proof['branches']},
                         {'收樣班別': ['西桌'], '驗入班別': ['乙桌']})
        self.run_actions(action('ask', question='這次改成晚班的是收樣班別還是驗入班別？'))
        answer = self.reply('收樣班別')
        resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        observation = resolved['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['西桌'])
        self.assertEqual(observation['field_reference']['confirmation']['citation'], f'[補充#{answer}]')
        self.assertEqual(observation['field_reference']['document_rule'], proof['document_rule'])

    def test_document_rule_rejects_other_scope_unadopted_and_unverified_rows(self):
        line = '本輪「甲批」正式生效。若「收樣班別」為「晚班」、「驗入班別」為「早班」，原「甲桌」改為「乙桌」。其他配對沿用原表。'
        for text in (line.replace('甲批', '乙批'), line.replace('正式生效', '未採用'), line.replace('甲桌', '後桌'),
                     line.replace('、「驗入班別」為「早班」', ''), '附件引述：' + line):
            with self.subTest(text=text):
                args = self.read_document_rule(text)
                rejected = self.run_actions(action('check_decision', **args))
                self.assertEqual(rejected['steps'][-1]['action']['tool'], 'invalid')
                state = self.run_actions(action('ask', question='這次改成晚班的是收樣班別還是驗入班別？'))
                self.assertEqual(state['status'], 'needs_input')

    def test_natural_field_confirmation_keeps_the_later_named_change(self):
        self.check_reference_after_table()
        question = '這次改成晚班的是收樣班別還是驗入班別？'
        self.run_actions(action('ask', question=question))
        source = f'[補充#{self.reply("收樣班別")}]'
        self.guide('收樣班別正式改為夜班。')
        resolved = self.run_actions(action('check_decision', field_reference=source, resolve=True))
        observation = resolved['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['丙桌'])
        proof = observation['field_reference']
        self.assertEqual(proof['confirmation']['quote'], '收樣班別')
        receipt = proof['post_reply_recheck']
        self.assertEqual(receipt['natural_question'], {'question_sha256': digest(question),
                                                      'candidate_fields': ['收樣班別', '驗入班別']})
        self.assertEqual(receipt['known'], {'收樣班別': '夜班', '驗入班別': '早班'})
        self.assertEqual([s['citation'] for s in receipt['source_order']], [source, '[更正#1]'])

    def test_natural_field_confirmation_binds_intervening_document_steps_on_restore(self):
        self.check_reference_after_table()
        self.read_documents()
        self.run_actions(action('search', query='收樣班別'),
                         action('ask', question='這次改成晚班的是收樣班別還是驗入班別？'))
        answer = self.reply('收樣班別')
        self.guide('收樣班別正式改為夜班。')
        resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        step = resolved['steps'][-1]
        self.assertEqual(step['observation']['possible_results'], ['丙桌'])
        receipt = step['observation']['field_reference']['post_reply_recheck']
        self.assertEqual(receipt['base_step'], 3)
        lookups = resolved['steps'][3:5]
        self.assertEqual([read['action']['tool'] for read in lookups], ['read_ranges', 'search'])
        self.assertEqual(receipt['natural_question']['lookup_steps'], [
            {'sequence': read['sequence'], 'sha256': digest(read)} for read in lookups])
        saved = create_backup(self.store.path, self.root / 'field-after-lookup.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        read = lookups[0]
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps({**read['observation'], 'excerpts': []}, ensure_ascii=False), self.gid, read['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_document_steps_do_not_bridge_a_change_of_scope_or_question(self):
        for scenario in ('changed conditions', 'new table', 'other tool', 'independent question'):
            with self.subTest(scenario=scenario):
                self.gid = autonomy.create_goal(self.store, self.sid, '核對最新班別及去向')
                self.check_reference_after_table()
                if scenario == 'changed conditions':
                    self.guide('驗入班別正式改為晚班。')
                if scenario == 'new table':
                    self.run_actions(action('check_decision', **{**self.table, 'known': {'收樣班別': '早班', '驗入班別': '晚班'}}))
                if scenario == 'other tool':
                    self.run_actions(action('calculate', operation='sum', values=['2', '3']))
                self.read_documents()
                question = ('通知對象是收樣班別還是驗入班別？' if scenario == 'independent question'
                            else '這次改成晚班的是收樣班別還是驗入班別？')
                self.run_actions(action('ask', question=question))
                self.reply('收樣班別')
                self.guide('收樣班別正式改為夜班。')
                goal = autonomy.get_goal(self.store, self.sid, self.gid, include_reply_positions=True)
                self.assertIsNone(decision_table.post_reply_reference_candidate(
                    goal['steps'], goal['reply_sources'], goal['guidance_sources']))

    def test_natural_recipient_question_stays_waiting_and_preserves_sources_on_restore(self):
        self.check_reference_after_table()
        question = '為了確認收件人，請問「正式改成晚班」指的是「收樣班別」還是「驗入班別」？'
        self.run_actions(action('ask', question=question))
        self.guide('驗入班別正式改為夜班。')
        waiting = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertEqual(waiting['question'], question)
        self.assertEqual(waiting['steps'][-1]['action']['tool'], 'ask')
        source = f'[補充#{self.reply("驗入班別")}]'
        self.guide('收樣班別正式改為夜班。')
        resolved = self.run_actions(action('check_decision', field_reference=source, resolve=True))
        step = resolved['steps'][-1]
        self.assertEqual(step['observation']['possible_results'], ['夜桌'])
        proof = step['observation']['field_reference']
        self.assertEqual(proof['confirmation']['quote'], '驗入班別')
        receipt = proof['post_reply_recheck']
        self.assertEqual(receipt['known'], {'收樣班別': '夜班', '驗入班別': '夜班'})
        self.assertEqual([s['citation'] for s in receipt['source_order']], ['[更正#1]', source, '[更正#2]'])
        saved = create_backup(self.store.path, self.root / 'natural-field.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        receipt['natural_question']['question_sha256'] = digest('另一個問題？')
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_natural_field_route_requires_original_change_and_complete_candidates(self):
        for question in ('通知對象是收樣班別還是驗入班別？',
                         '文件原話是「這次改成晚班的是收樣班別還是驗入班別？」',
                         '這次改成夜班的是收樣班別還是驗入班別？',
                         '這次改成晚班的是收樣班別還是覆核班別？',
                         '這次改成晚班的是收樣班別還是收樣班別？'):
            with self.subTest(question=question):
                self.gid = autonomy.create_goal(self.store, self.sid, '核對最新班別及去向')
                self.check_reference_after_table()
                self.run_actions(action('ask', question=question))
                source = f'[補充#{self.reply("收樣班別")}]'
                self.guide('收樣班別正式改為夜班。')
                goal = autonomy.get_goal(self.store, self.sid, self.gid, include_reply_positions=True)
                self.assertIsNone(decision_table.post_reply_reference_candidate(
                    goal['steps'], goal['reply_sources'], goal['guidance_sources']))
                rejected = self.run_actions(action('check_decision', field_reference=source, resolve=True))
                self.assertEqual(rejected['steps'][-1]['action']['tool'], 'invalid')

    def test_full_reply_preserves_count_and_applies_later_named_changes_in_order(self):
        field_question = self.check_reference_after_table()
        remaining = '本次要交付幾份？'
        self.run_actions(action('ask', question=f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。',
                                reference_remaining=remaining))
        answer = self.reply('收樣班別\n6 份')
        self.assertEqual(self.guide('收樣班別正式改為夜班。')['step_count'], answer)
        self.assertEqual(self.guide('驗入班別正式改為晚班。')['step_count'], answer)
        source = f'[補充#{answer}]'
        resolved = self.run_actions(action('check_decision', field_reference=source, resolve=True))
        observation = resolved['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['丁桌'])
        proof = observation['field_reference']
        self.assertEqual(proof['confirmation']['quote'], '收樣班別')
        self.assertEqual(proof['remaining_reply']['quote'], '6 份')
        self.assertEqual(proof['remaining_reply']['citation'], source)
        receipt = proof['post_reply_recheck']
        self.assertEqual(receipt['known'], {'收樣班別': '夜班', '驗入班別': '晚班'})
        self.assertEqual([item['citation'] for item in receipt['source_order']], [source, '[更正#1]', '[更正#2]'])
        self.assertEqual([(c['field'], c['before'], c['after']) for c in receipt['changes']],
                         [('收樣班別', '晚班', '夜班'), ('驗入班別', '早班', '晚班')])

    def test_later_guidance_after_a_waiting_recheck_preserves_the_original_reply_on_restore(self):
        self.run_actions(action('ask', question=self.check_reference_after_table()))
        self.guide('驗入班別正式改為晚班。')
        answer = self.reply('收樣班別')
        waiting = autonomy.get_goal(self.store, self.sid, self.gid)['steps'][-1]
        self.assertTrue(waiting['action']['args']['waiting_recheck'])
        self.assertEqual(self.guide('收樣班別正式改為夜班。')['step_count'], answer + 1)
        resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        step = resolved['steps'][-1]
        self.assertEqual(step['observation']['possible_results'], ['丁桌'])
        proof = step['observation']['field_reference']
        self.assertEqual(proof['post_reply_recheck']['base_step'], waiting['sequence'])
        self.assertEqual([s['citation'] for s in proof['post_reply_recheck']['source_order']], [f'[補充#{answer}]', '[更正#2]'])
        self.assertEqual(proof['waiting_recheck']['changes'][0]['evidence']['citation'], '[更正#1]')
        saved = create_backup(self.store.path, self.root / 'later-guidance.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        proof['post_reply_recheck']['source_order'].reverse()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

    def test_later_guidance_rejects_the_wrong_citation_and_unsupported_mixed_note(self):
        self.run_actions(action('ask', question=self.check_reference_after_table()))
        answer = self.reply('收樣班別')
        self.guide('收樣班別正式改為夜班。驗入班別可能要調整。')
        wrong = self.run_actions(action('check_decision', field_reference='[更正#1]', resolve=True))
        self.assertIn('原欄名答覆', wrong['steps'][-1]['observation']['error'])
        rejected = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        self.assertEqual(rejected['steps'][-1]['action']['tool'], 'invalid')
        self.assertIn('完整欄名及明確新值', rejected['steps'][-1]['observation']['error'])
        self.assertEqual(rejected['reply_sources'][-1]['answer'], '收樣班別')

    def test_later_guidance_can_use_the_remaining_field_answer_after_a_partial_reply(self):
        field_question = self.check_reference_after_table()
        remaining = '本次要交付幾份？'
        self.run_actions(action('ask', question=f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。',
                                reference_remaining=remaining))
        count_source = self.reply('補問：6 份')
        pending = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(pending['question'], field_question)
        field_source = self.reply('收樣班別')
        self.guide('收樣班別正式改為夜班。')
        resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{field_source}]', resolve=True))
        observation = resolved['steps'][-1]['observation']
        self.assertEqual(observation['possible_results'], ['丙桌'])
        proof = observation['field_reference']
        self.assertEqual(proof['confirmation']['citation'], f'[補充#{field_source}]')
        self.assertEqual(proof['partial_reply']['citation'], f'[補充#{count_source}]')
        self.assertEqual(proof['partial_reply']['quote'], '6 份')

    def test_same_step_reply_and_guidance_follow_real_note_order(self):
        for guidance_first, expected in [(True, '甲桌'), (False, '丙桌')]:
            with self.subTest(guidance_first=guidance_first):
                self.gid = autonomy.create_goal(self.store, self.sid, '核對最新班別及去向')
                self.save_same_step_sources(guidance_first=guidance_first)
                source_goal = autonomy.get_goal(self.store, self.sid, self.gid, include_reply_positions=True)
                reply, = source_goal['reply_sources']
                guide, = source_goal['guidance_sources']
                self.assertEqual(reply['ask_sequence'], guide['steps_seen'])
                self.assertEqual(guide['notes_start'] < reply['notes_start'], guidance_first)
                # A forged overlap must not be used to choose an order.
                overlap = {**guide, 'notes_start': reply['notes_start']}
                self.assertIsNone(decision_table.initial_reference_candidate(source_goal['steps'], [reply], [overlap]))
                checked = self.initial()
                self.assertNotIn('notes_start', checked['reply_sources'][0])
                step = checked['steps'][-1]
                proof = step['observation']['field_reference']
                sources = (guide, reply) if guidance_first else (reply, guide)
                self.assertEqual(proof['initial_source_order'], [
                    {'citation': item['citation'], 'source_sha256': item['sha256']} for item in sources])
                self.assertEqual({b['field']: b['possible_results'] for b in proof['branches']},
                                 {'收樣班別': [expected], '驗入班別': ['丁桌']})
                self.run_actions(action('ask', question=decision_table.reference_question(step)))
                answer = self.reply('收樣班別')
                resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
                self.assertEqual(resolved['steps'][-1]['observation']['possible_results'], [expected])

    def test_same_step_source_order_survives_waiting_recheck_and_rejects_reordered_backup_proof(self):
        self.save_same_step_sources(guidance_first=True)
        checked = self.initial()
        step = checked['steps'][-1]
        self.run_actions(action('ask', question=decision_table.reference_question(step)))
        revised = self.guide('驗入班別正式改為晚班。')['goal']['reference_review']
        self.assertEqual({b['field']: b['possible_results'] for b in revised['field_reference']['branches']},
                         {'收樣班別': ['後桌'], '驗入班別': ['丁桌']})
        answer = self.reply('收樣班別')
        resolved = self.run_actions(action('check_decision', field_reference=f'[補充#{answer}]', resolve=True))
        self.assertEqual(resolved['steps'][-1]['observation']['possible_results'], ['後桌'])
        saved = create_backup(self.store.path, self.root / 'source-order.paibackup')
        restored = self.root / 'restored'
        restore_backup(saved['path'], restored)
        backup = Store(restored / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(backup, self.sid, self.gid), resolved)
        step['observation']['field_reference']['initial_source_order'].reverse()
        with backup.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation'], ensure_ascii=False), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(backup.path)

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
