from assistant.goal_replies import snapshot as reply_snapshot
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, goal_guidance as g, goal_replies, goal_review
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalGuidanceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'assistant.sqlite3')
        self.sid=self.store.new_session();self.gid=a.create_goal(self.store,self.sid,'10人，每人80元，計算費用')

    def save(self, content='更正：改成12人，每人90元。', fingerprint=None):
        return g.save(self.store,self.sid,self.gid,content,expected_fingerprint=fingerprint or g.snapshot(self.store,self.sid,self.gid)['fingerprint'])

    def test_paused_goal_accepts_correction_without_running_or_erasing_old_steps(self):
        a.run_goal(self.store,self.sid,self.gid,ScriptedEngine(action('calculate',operation='product',values=['10','80'])),max_steps=1)
        before=a.get_goal(self.store,self.sid,self.gid)
        result=self.save();after=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual(after['status'],'paused');self.assertEqual(after['steps'],before['steps'])
        self.assertEqual(after['objective'],before['objective'])
        self.assertIn('已記錄1步',after['notes']);self.assertIn('12人',after['notes'])
        self.assertEqual(result['step_count'],1)
        self.assertFalse(after['file_delivery']);self.assertIsNone(after['csv_source'])
        self.assertEqual(self.store.messages(self.sid),[])

    def test_report_correction_rechecks_the_unresolved_branches_atomically(self):
        fields = ['配送班別', '複核班別', '入口']
        domains = {fields[0]: ['早班', '晚班'], fields[1]: ['早班', '晚班'], '入口': ['北口', '南口']}
        rows = [{'when': list(values), 'result': result} for values, result in zip(
            product(*domains.values()), ('前台', '前台', '覆核席', '丙台', '覆核席', '乙台', '後台', '後台'))]
        gid = a.create_goal(self.store, self.sid, '核對班別與入口')

        def run(*actions):
            return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

        def reply(content):
            snapshot = reply_snapshot(self.store, self.sid, gid)
            return goal_replies.save(self.store, self.sid, gid, content, expected_fingerprint=snapshot['fingerprint'])

        run(action('check_decision', conditions=fields, domains=domains, rows=rows, excluded=[],
                   known=dict(zip(fields, ('早班', '早班', '北口')))), action('ask', question='本輪更正？'))
        reply('正式改成晚班。')
        first = run(action('check_decision', field_reference='[補充#2]'),
                    action('finish', report='覆核席，欄位待查。[補充#2]'))
        observed = g.snapshot(self.store, self.sid, gid)

        def amend(content):
            return g.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])

        self.assertEqual(observed['reference_report']['status'], 'agreement')
        with self.assertRaisesRegex(ValueError, '完整欄名'):
            amend('配送班別正式改為夜班。')
        with self.assertRaisesRegex(ValueError, '完整欄名'):
            amend('有人提議「入口正式改為南口」，未採用。')
        self.assertEqual(a.get_goal(self.store, self.sid, gid), first)
        amend('入口正式改為南口。')
        with self.assertRaisesRegex(ValueError, '重新載入'):
            amend('入口正式改為南口。')
        state = a.get_goal(self.store, self.sid, gid)
        self.assertEqual((state['status'], state['result'], state['steps'][:-1]), ('paused', '', first['steps']))
        checked = state['steps'][-1]['observation']['field_reference']
        self.assertEqual(checked['status'], 'needs_field')
        self.assertEqual([branch['possible_results'] for branch in checked['branches']], [['乙台'], ['丙台']])
        self.assertEqual(checked['recheck']['change'], {'field': '入口', 'before': '北口', 'after': '南口'})
        self.assertEqual(checked['recheck']['evidence']['quote'], '入口正式改為南口')
        run(action('ask', question='改成晚班的是配送或複核班別？', partial_result='乙台或丙台。[更正#1]'))
        reply('複核班別')
        final = run(action('check_decision', field_reference='[補充#6]', resolve=True),
                    action('finish', report='丙台。[補充#2] [更正#1] [補充#6]'))
        self.assertEqual(final['steps'][-2]['observation']['possible_results'], ['丙台'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), final)
        with restored.connect() as db:
            db.execute('DELETE FROM autonomous_steps WHERE goal_id=? AND sequence=4', (gid,))
        with self.assertRaisesRegex(ValueError, '成果更正須緊接'):
            validate_database(restored.path)

    def test_later_candidate_values_override_the_reference_in_order(self):
        fields = ['巡檢狀態', '記錄狀態']
        values = ['關閉', '啟用']
        rows = [{'when': list(pair), 'result': '入口' if pair == ('啟用', '啟用') else '共用席'}
                for pair in product(values, repeat=2)]
        gid = a.create_goal(self.store, self.sid, '核對兩個狀態')

        def run(*actions):
            return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

        def amend(content):
            snapshot = g.snapshot(self.store, self.sid, gid)
            g.save(self.store, self.sid, gid, content, expected_fingerprint=snapshot['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)

        run(action('check_decision', conditions=fields, domains=dict.fromkeys(fields, values),
                   known=dict.fromkeys(fields, '關閉'), rows=rows, excluded=[]), action('ask', question='哪項更正？'))
        waiting = reply_snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '正式改成啟用。', expected_fingerprint=waiting['fingerprint'])
        before = run(action('check_decision', field_reference='[補充#2]'), action('finish', report='共用席，欄名待查。'))

        changed = amend('巡檢狀態正式改為關閉。')
        step = changed['steps'][-1]
        proof = step['observation']['field_reference']
        self.assertEqual(changed['steps'][:-1], before['steps'])
        self.assertEqual((proof['status'], proof['overwritten_fields']), ('agreement', ['巡檢狀態']))
        self.assertEqual([branch['known'] for branch in proof['branches']],
                         [dict.fromkeys(fields, '關閉'), {'巡檢狀態': '關閉', '記錄狀態': '啟用'}])
        self.assertEqual(proof['recheck']['change'], {'field': '巡檢狀態', 'before': '關閉', 'after': '關閉'})

        finished = run(action('finish', report='共用席，第一筆欄名待查。[更正#1]'))
        changed = amend('記錄狀態正式改為啟用。')
        self.assertEqual(changed['steps'][:-1], finished['steps'])
        step = changed['steps'][-1]['observation']
        proof = step['field_reference']
        self.assertEqual((proof['status'], proof['overwritten_fields']), ('superseded', fields))
        self.assertEqual([branch['known'] for branch in proof['branches']],
                         [{'巡檢狀態': '關閉', '記錄狀態': '啟用'}] * 2)
        self.assertEqual((step['status'], step['max_questions'], step['possible_results']), ('resolved', 0, ['共用席']))
        plan, _, _ = a.decision_table._recheck_saved(changed['steps'], changed['reply_sources'], changed['guidance_sources'])
        self.assertNotIn('pending_field_reference', plan[1])
        final = run(action('finish', report='巡檢關閉、記錄啟用，共用席。[更正#1] [更正#2]'))
        self.assertNotIn('reference_report', g.snapshot(self.store, self.sid, gid))
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'ordered')
        restored = Store(self.root / 'ordered' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), final)
        with restored.connect() as db:
            proof['overwritten_fields'].reverse()
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=7',
                       (json.dumps(step, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '決策表結果'):
            validate_database(restored.path)

    def test_waiting_reference_recheck_withdraws_only_after_the_result_agrees(self):
        fields = ['清點班別', '收件班別', '入口']
        shifts = ['早班', '晚班', '夜班']
        domains = {fields[0]: shifts, fields[1]: shifts, '入口': ['北口', '南口']}
        rows = []
        for left, right, entry in product(*domains.values()):
            destination = '會合台' if entry == '南口' else ('甲席' if right == '早班' else '乙席')
            rows.append({'when': [left, right, entry], 'result': destination})
        question = '第一筆改值指哪個欄位？請回完整欄名「清點班別」、「收件班別」。'

        for next_action in ('withdraw', 'reply', 'compound', 'legacy_withdraw', 'legacy_reply'):
            with self.subTest(next_action=next_action):
                gid = a.create_goal(self.store, self.sid, '核對班別與入口')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

                def reply(content):
                    observed = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])

                def amend(content):
                    observed = g.snapshot(self.store, self.sid, gid)
                    return g.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])

                run(action('check_decision', conditions=fields, domains=domains, rows=rows, excluded=[],
                           known=dict(zip(fields, ('早班', '早班', '北口')))), action('ask', question='當次更正？'))
                reply('正式改成晚班。')
                asked = question if next_action != 'compound' else '第一筆指哪一欄？也請提供通知對象。'
                before = run(action('check_decision', field_reference='[補充#2]'),
                             action('ask', question=asked, partial_result='更正前甲席或乙席。'))
                old = reply_snapshot(self.store, self.sid, gid)
                if next_action in ('legacy_withdraw', 'legacy_reply'):
                    with patch.object(g, 'apply_waiting_review', side_effect=lambda db, state: state):
                        amend('入口正式改為南口。')
                    observed = reply_snapshot(self.store, self.sid, gid)
                    self.assertEqual(observed['goal']['reference_review']['possible_results'], ['會合台'])
                    if next_action == 'legacy_reply':
                        reply('清點班別')
                        after = a.get_goal(self.store, self.sid, gid)
                        self.assertEqual(after['steps'], before['steps'])
                        self.assertEqual(after['reply_sources'][-1]['answer'], '清點班別')
                    else:
                        goal_replies.withdraw(self.store, self.sid, gid, 1, expected_fingerprint=observed['fingerprint'])
                        after = a.get_goal(self.store, self.sid, gid)
                        self.assertEqual(after['steps'][-1]['observation']['possible_results'], ['會合台'])
                        self.assertEqual(after['withdrawn_questions'][0]['ask_sequence'], 4)
                    validate_database(self.store.path)
                    continue
                if next_action == 'compound':
                    saved = amend('入口正式改為南口。')
                    self.assertNotIn('reference_review', saved['goal'])
                    after = a.get_goal(self.store, self.sid, gid)
                    self.assertEqual((after['question'], after['steps']), (asked, before['steps']))
                    continue

                saved = amend('清點班別正式改為夜班。')
                self.assertEqual(saved['goal']['question'], question)
                self.assertEqual(saved['step_count'], 4)
                proof = saved['goal']['reference_review']['field_reference']
                self.assertEqual(proof['status'], 'needs_field')
                self.assertTrue(all(branch['known']['清點班別'] == '夜班' for branch in proof['branches']))
                self.assertEqual(a.get_goal(self.store, self.sid, gid)['steps'], before['steps'])
                with self.assertRaisesRegex(ValueError, '尚未保存'):
                    goal_replies.save(self.store, self.sid, gid, '清點班別', expected_fingerprint=old['fingerprint'])

                if next_action == 'withdraw':
                    saved = amend('入口正式改為南口。')['goal']
                    self.assertEqual((saved['status'], saved['question']), ('paused', ''))
                    self.assertEqual([item['ask_sequence'] for item in saved['reply_sources']], [2])
                    self.assertEqual([(item['ask_sequence'], item['guidance_citation']) for item in saved['withdrawn_questions']],
                                     [(4, '[更正#2]')])
                    state = a.get_goal(self.store, self.sid, gid)
                    checked = state['steps'][-1]['observation']
                    self.assertEqual(checked['possible_results'], ['會合台'])
                    self.assertEqual(len(checked['field_reference']['waiting_recheck']['changes']), 2)
                    self.assertEqual(state['steps'][:-1], before['steps'])
                    final = run(action('finish', report='會合台。[補充#2] [更正#1] [更正#2]'))
                else:
                    saved = reply('清點班別')['goal']
                    self.assertEqual(saved['reply_sources'][-1]['citation'], '[補充#4]')
                    self.assertNotIn('withdrawn_questions', saved)
                    final = run(action('check_decision', field_reference='[補充#4]', resolve=True),
                                action('finish', report='甲席。[補充#2] [更正#1] [補充#4]'))
                    checked = final['steps'][-2]['observation']
                    self.assertEqual(checked['possible_results'], ['甲席'])
                    self.assertEqual(checked['field_reference']['recheck']['change']['after'], '夜班')
                archive = create_backup(self.store.path, self.root / f'{next_action}.paibackup')
                restore_backup(archive['path'], self.root / next_action)
                restored = Store(self.root / next_action / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), final)
                if next_action == 'withdraw':
                    with restored.connect() as db:
                        db.execute('DELETE FROM autonomous_question_withdrawals WHERE goal_id=?', (gid,))
                    with self.assertRaisesRegex(ValueError, '缺少與更正相符'):
                        validate_database(restored.path)

    def test_compound_reference_recheck_keeps_the_independent_question_bound(self):
        fields = ['收件班別', '校對班別', '入口']
        domains = {fields[0]: ['早班', '晚班', '夜班'], fields[1]: ['早班', '晚班'], '入口': ['北口', '南口']}
        rows = [{'when': list(values), 'result': '會合台' if values[2] == '南口' else
                 ('甲台' if values[1] == '早班' else '乙台')} for values in product(*domains.values())]
        remaining = '本次要通知誰？'
        question = ('第一筆改值指哪個欄位？請回完整欄名「收件班別」、「校對班別」。\n'
                    f'{remaining}\n請分兩行回答，先回完整欄名，再答補問。')

        for mode in ('narrow', 'split', 'prefixed'):
            with self.subTest(mode=mode):
                narrow = mode == 'narrow'
                gid = a.create_goal(self.store, self.sid, '核對去向及獨立的通知對象')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

                def reply(content):
                    observed = reply_snapshot(self.store, self.sid, gid)
                    goal_replies.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])

                run(action('check_decision', conditions=fields, domains=domains, rows=rows, excluded=[],
                           known=dict(zip(fields, ('早班', '早班', '北口')))), action('ask', question='當次更正？'))
                reply('正式改為晚班。')
                before = run(action('check_decision', field_reference='[補充#2]'),
                             action('ask', question=question, reference_remaining=remaining, partial_result='甲台或乙台。'))
                observed = g.snapshot(self.store, self.sid, gid)
                content = '入口正式改為南口。' if narrow else '收件班別正式改為夜班。'
                g.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])
                current = a.get_goal(self.store, self.sid, gid)
                self.assertEqual(current['status'], 'needs_input')
                if narrow:
                    self.assertEqual(current['question'], remaining)
                    self.assertEqual(current['steps'][:-2], before['steps'])
                    self.assertEqual(current['steps'][-2]['observation']['possible_results'], ['會合台'])
                    self.assertEqual(current['withdrawn_questions'][0]['question'], question)
                    self.assertEqual(len(current['reply_sources']), 1)
                    with self.assertRaisesRegex(ValueError, '尚未保存'):
                        goal_replies.save(self.store, self.sid, gid, '舊題的回答', expected_fingerprint=observed['fingerprint'])
                    reply('收件班別')  # This literal could also be a field name.
                    current = a.get_goal(self.store, self.sid, gid)
                    self.assertEqual(current['reply_sources'][-1]['question'], remaining)
                    self.assertEqual(current['reply_sources'][-1]['ask_sequence'], 6)
                    with self.assertRaisesRegex(ValueError, '獨立補問'):
                        a.decision_table.observe({'field_reference':'[補充#6]', 'resolve':True}, current['steps'],
                                                 current['reply_sources'], current['guidance_sources'])
                    final = run(action('finish', report='會合台，通知對象以補問為準。[補充#6]'))
                else:
                    self.assertEqual((current['question'], current['steps']), (question, before['steps']))
                    self.assertEqual(current['reference_review']['field_reference']['status'], 'needs_field')
                    reply('欄名：收件班別\n補問：羅值班員' if mode == 'prefixed' else '收件班別\n羅值班員')
                    final = run(action('check_decision', field_reference='[補充#4]', resolve=True),
                                action('finish', report='甲台，通知羅值班員。[補充#4]'))
                    checked = final['steps'][-2]['observation']
                    self.assertEqual(checked['possible_results'], ['甲台'])
                    proof = checked['field_reference']
                    self.assertEqual((proof['confirmation']['quote'], proof['remaining_reply']['quote']),
                                     ('收件班別', '羅值班員'))
                    self.assertEqual(proof['remaining_reply']['question'], remaining)
                dest = self.root / f'compound-{mode}'
                archive = create_backup(self.store.path, dest.with_suffix('.paibackup'))
                restore_backup(archive['path'], dest)
                restored = Store(dest / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), final)
                if narrow:
                    with restored.connect() as db:
                        db.execute('UPDATE autonomous_steps SET action_json=? WHERE goal_id=? AND sequence=6',
                                   (action('ask', question=remaining, extra='改動'), gid))
                    with self.assertRaisesRegex(ValueError, '保留補問'):
                        validate_database(restored.path)

    def test_single_compound_answer_preserves_the_other_question_and_each_source(self):
        literal = {'branches': [{'field': '欄名：收件班別'}, {'field': '檢查班別'}]}
        original = a.decision_table._reference_question(literal)
        _, parsed = a.decision_table._compound_reply(literal, {
            'question': f'{original}\n本次要通知誰？\n請分兩行回答，先回完整欄名，再答補問。',
            'answer': '欄名：收件班別\n補問：羅值班員'})
        self.assertEqual((parsed['field'], parsed['remaining']), ('欄名：收件班別', '補問：羅值班員'))
        fields = ['收件班別', '檢查班別']
        domains = {field: ['早班', '晚班', '夜班'] for field in fields}
        rows = [{'when': list(values), 'result': '甲台' if values[1] == '早班' else '乙台'}
                for values in product(*domains.values())]
        field_question = '第一筆改值指哪個欄位？請回完整欄名「收件班別」、「檢查班別」。'
        remaining = '本次要通知誰？'
        question = f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。'

        for mode in ('field', 'remaining', 'natural_field', 'natural_remaining'):
            with self.subTest(mode=mode):
                kind = mode.removeprefix('natural_')
                gid = a.create_goal(self.store, self.sid, '核對去向與獨立補問')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

                def reply(content, fingerprint=None):
                    observed = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, content,
                                             expected_fingerprint=fingerprint or observed['fingerprint'])

                run(action('check_decision', conditions=fields, domains=domains, rows=rows, excluded=[],
                           known=dict.fromkeys(fields, '早班')), action('ask', question='本輪更正？'))
                reply('正式改成晚班。')
                run(action('check_decision', field_reference='[補充#2]'),
                    action('ask', question=question, reference_remaining=remaining))
                if kind == 'remaining':
                    observed = g.snapshot(self.store, self.sid, gid)
                    g.save(self.store, self.sid, gid, '收件班別正式改為夜班。', expected_fingerprint=observed['fingerprint'])
                before = a.get_goal(self.store, self.sid, gid)
                fingerprint = reply_snapshot(self.store, self.sid, gid)['fingerprint']
                with self.assertRaisesRegex(ValueError, '完整欄名'):
                    reply('欄名：不存在的欄名')
                if mode.startswith('natural_'):
                    with self.assertRaisesRegex(ValueError, '明確答案'):
                        reply('通知許值班員或鄭主任，欄名還沒確認。')
                self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                first_text = {'field':'欄名：收件班別', 'remaining':'補問：收件班別',
                              'natural_field':'第一筆指的是收件班別，通知對象還沒確認。',
                              'natural_remaining':'通知收件班別，欄名我還沒確認。'}[mode]
                saved = reply(first_text)
                current = a.get_goal(self.store, self.sid, gid)
                self.assertEqual((current['status'], current['question']),
                                 ('needs_input', remaining if kind == 'field' else field_question))
                self.assertNotIn('reference_partial_reply', saved)
                self.assertEqual(current['steps'][:len(before['steps'])], before['steps'])
                proof = current['steps'][-2]['observation']['field_reference']['partial_reply']
                self.assertEqual((proof['item'], proof['quote'], proof['citation']), (kind, '收件班別', '[補充#4]'))
                self.assertEqual(first_text[proof['start']:proof['end']], proof['quote'])
                self.assertEqual(current['reply_sources'][-1]['answer'], first_text)
                with self.assertRaisesRegex(ValueError, '尚未保存'):
                    reply('已過期的回覆', fingerprint)
                if kind == 'remaining':
                    with self.assertRaisesRegex(ValueError, '獨立補問'):
                        a.decision_table.observe({'field_reference': '[補充#4]', 'resolve': True}, current['steps'],
                                                 current['reply_sources'], current['guidance_sources'])
                followup = current['steps'][-1]['sequence']
                reply('羅值班員' if kind == 'field' else '收件班別')
                finish = action('finish', report=f'甲台。已核對兩項。[補充#4] [補充#{followup}]')
                if kind == 'field':
                    final = run(finish)
                else:
                    final = run(action('check_decision', field_reference=f'[補充#{followup}]', resolve=True), finish)
                self.assertEqual(final['reply_sources'][-1]['question'], current['question'])
                plan, _, _ = a.decision_table._recheck_saved(final['steps'], final['reply_sources'],
                                                             final.get('guidance_sources', ()))
                self.assertEqual(plan[1]['known']['收件班別'], '晚班' if kind == 'field' else '夜班')
                archive = create_backup(self.store.path, self.root / f'partial-{mode}.paibackup')
                destination = self.root / f'partial-{mode}'
                restore_backup(archive['path'], destination)
                restored = Store(destination / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), final)
                with restored.connect() as db:
                    db.execute('UPDATE autonomous_steps SET action_json=? WHERE goal_id=? AND sequence=?',
                               (action('ask', question=current['question'], extra='改動'), gid, followup))
                with self.assertRaisesRegex(ValueError, '保留補問'):
                    validate_database(restored.path)

    def test_natural_partial_reply_binds_exact_values_to_the_question(self):
        fields = {'收件班別', '檢查班別'}
        parse = a.decision_table._partial_reply_span
        for answer, question, kind, quote in (
                ('領件人還不確定；第一筆指的是「收件班別」。', '本輪由誰領件？', 'field', '「收件班別」'),
                ('由王小明領件，欄名仍待確認。', '本輪由誰領件？', 'remaining', '王小明'),
                ('通知王和平，欄名待查。', '本次要通知誰？', 'remaining', '王和平'),
                ('欄名我還沒確認，這輪交付 6 份。', '本輪要交付幾份？', 'remaining', '6 份'),
                ('六份，欄名待查。', '要交付幾份？', 'remaining', '六份'),
                ('第一筆指收件班別，補問待確認。', '何時到站？', 'field', '收件班別')):
            with self.subTest(answer=answer):
                actual, start, end = parse(answer, question, fields)
                self.assertEqual((actual, answer[start:end]), (kind, quote))
        self.assertIsNone(parse('收件班別\n欄名待查', '本次要通知誰？', fields))
        for answer in ('通知許值班員或鄭主任，欄名待查。', '通知許值班員和鄭主任，欄名待查。',
                       '通知王小明但未決定，欄名待查。', '通知不知道，欄名待查。',
                       '交付三份，欄名待查。', '欄名待查，通知對象待查。',
                       '第一筆指的是收件班別，通知對象待查，入口改南口。', '欄名待查。'):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, '原題及草稿'):
                parse(answer, '本次要通知誰？', fields)

    def test_waiting_answered_item_revisions_keep_the_original_question(self):
        fields = ['收件班別', '覆核班別']
        values = ['早班', '晚班']
        rows = [{'when': list(pair), 'result': result} for pair, result in zip(
            product(values, repeat=2), ('前台', '西台', '東台', '後台'))]
        field_question = '第一筆改值指哪個欄位？請回完整欄名「收件班別」、「覆核班別」。'
        independent = '本輪要交付幾份？'
        compound = f'{field_question}\n{independent}\n請分兩行回答，先回完整欄名，再答補問。'
        for kind in ('field', 'remaining', 'proposal'):
            with self.subTest(kind=kind):
                gid = a.create_goal(self.store, self.sid, '核對去向及交付份數')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions))

                def reply(content, fingerprint=None):
                    observed = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, content,
                                             expected_fingerprint=fingerprint or observed['fingerprint'])

                def amend(content):
                    observed = g.snapshot(self.store, self.sid, gid)
                    return g.save(self.store, self.sid, gid, content, expected_fingerprint=observed['fingerprint'])

                run(action('check_decision', conditions=fields, domains=dict.fromkeys(fields, values), rows=rows,
                           excluded=[], known=dict.fromkeys(fields, '早班')), action('ask', question='當次更正？'))
                reply('正式改成晚班。')
                run(action('check_decision', field_reference='[補充#2]'),
                    action('ask', question=compound, reference_remaining=independent))
                original_text = '欄名：收件班別' if kind == 'field' else '補問：6 份'
                reply(original_text)
                before = a.get_goal(self.store, self.sid, gid)
                ask_sequence = before['steps'][-1]['sequence']
                old = reply_snapshot(self.store, self.sid, gid)['fingerprint']
                if kind == 'proposal':
                    amend('有人提議「交付份數正式改為9份」，未採用。')
                    pending = a.get_goal(self.store, self.sid, gid)
                    self.assertNotIn('answered_reference_review', pending)
                    self.assertEqual((pending['steps'], pending['question'], pending['reply_sources']),
                                     (before['steps'], before['question'], before['reply_sources']))
                    reply('收件班別')
                    final = run(action('check_decision', field_reference=f'[補充#{ask_sequence}]', resolve=True),
                                action('finish', report=f'東台，6 份。[補充#{ask_sequence}]'))
                    self.assertEqual([step['action']['tool'] for step in final['steps'][-2:]],
                                     ['check_decision', 'finish'])
                    self.assertEqual(final['steps'][-2]['observation']['possible_results'], ['東台'])
                    archive = create_backup(self.store.path, self.root / 'answered-proposal.paibackup')
                    restore_backup(archive['path'], self.root / 'answered-proposal')
                    continue
                bad = '第一筆欄名正式改為不存在。' if kind == 'field' else '交付份數正式改為甲或乙。'
                with self.assertRaisesRegex(ValueError, '明確的新值'):
                    amend(bad)
                self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                if kind == 'field':
                    amend('第一筆欄名正式改為覆核班別。')
                    expected = [('收件班別', '覆核班別')]
                else:
                    amend('交付份數正式改為 7 份。')
                    amend('有人提議「交付份數正式改為9份」，未採用。')
                    amend('已答補問正式改為 8 份。')
                    expected = [('6 份', '7 份'), ('7 份', '8 份')]
                pending = a.get_goal(self.store, self.sid, gid)
                self.assertEqual((pending['status'], pending['question'], pending['steps'], pending['reply_sources']),
                                 ('needs_input', before['question'], before['steps'], before['reply_sources']))
                preview = pending['answered_reference_review']
                proof = preview['field_reference']['answered_revision']
                self.assertEqual([(item['before'], item['after']) for item in proof['changes']], expected)
                self.assertEqual(proof['value'], expected[-1][1])
                self.assertEqual(proof['ask_sequence'], ask_sequence)
                with self.assertRaisesRegex(ValueError, '尚未保存'):
                    reply('過期回覆', old)
                with patch.object(g, 'MAX_STEPS', ask_sequence + 1):
                    with self.assertRaisesRegex(ValueError, '剩餘步數不足'):
                        reply('3 份' if kind == 'field' else '收件班別')
                self.assertEqual(a.get_goal(self.store, self.sid, gid), pending)
                reply('3 份' if kind == 'field' else '收件班別')
                checked = a.get_goal(self.store, self.sid, gid)
                self.assertEqual(checked['steps'][:-1], before['steps'])
                self.assertEqual(checked['steps'][-1]['observation']['field_reference']['answered_revision'], proof)
                self.assertEqual(checked['reply_sources'][-1]['ask_sequence'], ask_sequence)
                self.assertEqual(checked['reply_sources'][-2]['answer'], original_text)
                if kind == 'field':
                    self.assertEqual(preview['possible_results'], ['西台'])
                    final = run(action('finish', report='西台，3 份。[更正#1] [補充#6]'))
                else:
                    final = run(action('check_decision', field_reference=f'[補充#{ask_sequence}]', resolve=True),
                                action('finish', report='東台，8 份。[更正#3] [補充#6]'))
                plan, _, _ = a.decision_table._recheck_saved(final['steps'], final['reply_sources'], final['guidance_sources'])
                expected_known = {'收件班別': '早班', '覆核班別': '晚班'} if kind == 'field' else {'收件班別': '晚班', '覆核班別': '早班'}
                self.assertEqual(plan[1]['known'], expected_known)
                archive = create_backup(self.store.path, self.root / f'answered-{kind}.paibackup')
                destination = self.root / f'answered-{kind}'
                restore_backup(archive['path'], destination)
                restored = Store(destination / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), final)
                changed = checked['steps'][-1]['observation']
                changed['field_reference']['answered_revision']['changes'][-1]['evidence']['start'] = 0
                with restored.connect() as db:
                    db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                               (json.dumps(changed, ensure_ascii=False), gid, ask_sequence + 1))
                with self.assertRaisesRegex(ValueError, '決策表結果'):
                    validate_database(restored.path)

    def test_natural_answered_revisions_bind_context_and_reject_uncertain_values(self):
        fields = ['接件班別', '核驗班別']
        values = ['早班', '晚班']
        field_question = '第一筆改值指哪個欄位？請回完整欄名「接件班別」、「核驗班別」。'
        fixtures = (
            ('count', '本輪要交付幾份？', '補問：4 份', '七份',
             '剛才份數說錯了，應該是七份。有人提議「份數改成九份，欄名填核驗班別」，未採用。欄名還沒確認。'),
            ('field', '本次要通知誰？', '欄名：接件班別', '核驗班別',
             '通知對象還沒確認；欄名剛才填錯了，正確是核驗班別。'),
            ('person', '本次要通知誰？', '補問：范同仁', '陸值班員',
             '通知對象改成陸值班員，欄名還沒確認。'),
        )
        for kind, remaining, original_answer, expected, correction in fixtures:
            with self.subTest(kind=kind):
                gid = a.create_goal(self.store, self.sid, '核對合成班別與獨立補問')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions), max_steps=len(actions))

                def reply(answer):
                    current = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, answer, expected_fingerprint=current['fingerprint'])

                def amend(content):
                    current = g.snapshot(self.store, self.sid, gid)
                    return g.save(self.store, self.sid, gid, content, expected_fingerprint=current['fingerprint'])

                rows = [{'when': list(pair), 'result': result} for pair, result in zip(
                    product(values, repeat=2), ('前台', '西台', '東台', '後台'))]
                run(action('check_decision', conditions=fields, domains=dict.fromkeys(fields, values), rows=rows,
                           excluded=[], known=dict.fromkeys(fields, '早班')), action('ask', question='當次更正？'))
                reply('正式改成晚班。')
                run(action('check_decision', field_reference='[補充#2]'), action('ask',
                    question=f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。',
                    reference_remaining=remaining))
                reply(original_answer)
                before = a.get_goal(self.store, self.sid, gid)
                if kind == 'count':
                    for invalid in (
                        '剛才份數說錯了，應該是七份或八份，欄名還沒確認。',
                        '剛才份數說錯了，應該是七份。份數還沒確認。',
                        '剛才份數說錯了，應該是七份。入口改為南口。',
                        '剛才份數說錯了，應該是七份。份數改成八份。',
                    ):
                        with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '本輪要交付幾份'):
                            amend(invalid)
                        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                amend(correction)
                pending = a.get_goal(self.store, self.sid, gid)
                self.assertEqual((pending['question'], pending['steps'], pending['reply_sources']),
                                 (before['question'], before['steps'], before['reply_sources']))
                proof = pending['answered_reference_review']['field_reference']['answered_revision']
                change, = proof['changes']
                self.assertEqual(change['after'], expected)
                self.assertEqual(correction[change['evidence']['start']:change['evidence']['end']], expected)
                self.assertTrue(change['retained'])
                for kept in change['retained']:
                    self.assertEqual(correction[kept['start']:kept['end']], kept['quote'])
                if kind == 'field':
                    self.assertEqual(pending['answered_reference_review']['possible_results'], ['西台'])
                reply('葉值班員' if kind == 'field' else '接件班別')
                checked = a.get_goal(self.store, self.sid, gid)
                self.assertEqual(checked['steps'][-1]['observation']['field_reference']['answered_revision'], proof)
                if kind != 'field':
                    run(action('check_decision', field_reference=f'[補充#{before["steps"][-1]["sequence"]}]', resolve=True))
                archive = create_backup(self.store.path, self.root / f'natural-revision-{kind}.paibackup')
                target = self.root / f'natural-revision-{kind}'
                restore_backup(archive['path'], target)
                restored = Store(target / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), a.get_goal(self.store, self.sid, gid))
                changed = checked['steps'][-1]['observation']
                changed['field_reference']['answered_revision']['changes'][0]['retained'][0]['kind'] = 'invented'
                with restored.connect() as db:
                    db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                               (json.dumps(changed, ensure_ascii=False), gid, checked['steps'][-1]['sequence']))
                with self.assertRaisesRegex(ValueError, '決策表結果'):
                    validate_database(restored.path)

    def test_combined_reply_keeps_each_source_and_later_named_values(self):
        fields = ['收件班別', '覆件班別']
        values = ['早班', '晚班', '夜班']
        outcomes = ('前台', '西台', '夜覆', '東台', '後台', '夜覆', '夜審', '夜審', '夜台')
        rows = [{'when': list(pair), 'result': result} for pair, result in zip(product(values, repeat=2), outcomes)]
        field_question = '第一筆改值指哪個欄位？請回完整欄名「收件班別」、「覆件班別」。'
        fixtures = (
            ('count', '本輪要交付幾份？', '補問：4 份',
             '剛才份數說錯了，應該是八份。收件班別正式改為夜班。欄名是收件班別。',
             {'收件班別':'夜班', '覆件班別':'早班'}, '八份', '夜審'),
            ('field', '本次要通知誰？', '欄名：收件班別',
             '通知葉值班員。欄名改為覆件班別。收件班別正式改為晚班。',
             {'收件班別':'晚班', '覆件班別':'晚班'}, '覆件班別', '後台'),
            ('prior', '本輪要交付幾份？', '補問：6 份',
             '欄名是覆件班別。份數改為八份。有人提議「欄名改為收件班別，份數改為九份」，未採用。',
             {'收件班別':'早班', '覆件班別':'晚班'}, '八份', '西台'),
        )
        for kind, remaining, original, answer, expected_known, expected_value, expected_result in fixtures:
            with self.subTest(kind=kind):
                gid = a.create_goal(self.store, self.sid, '核對合成分項更正及同則答覆')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions), max_steps=len(actions))

                def reply(text, fingerprint=None):
                    current = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, text,
                                             expected_fingerprint=fingerprint or current['fingerprint'])

                run(action('check_decision', conditions=fields, domains=dict.fromkeys(fields, values), rows=rows,
                           excluded=[], known=dict.fromkeys(fields, '早班')), action('ask', question='當次更正？'))
                reply('正式改成晚班。')
                run(action('check_decision', field_reference='[補充#2]'), action('ask',
                    question=f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。',
                    reference_remaining=remaining))
                reply(original)
                if kind == 'prior':
                    current = g.snapshot(self.store, self.sid, gid)
                    g.save(self.store, self.sid, gid, '交付份數正式改為 7 份。', expected_fingerprint=current['fingerprint'])
                before = a.get_goal(self.store, self.sid, gid)
                fingerprint = reply_snapshot(self.store, self.sid, gid)['fingerprint']
                if kind == 'count':
                    for invalid in (
                        '份數改為八份。欄名是收件班別或覆件班別。',
                        '份數改為八份或九份。欄名是收件班別。',
                        '份數改為八份。欄名是收件班別。收件班別正式改為晚班。收件班別正式改為夜班。',
                        '份數改為八份。欄名是收件班別。請另改入口。',
                    ):
                        with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                            reply(invalid)
                        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                    quoted = '有人說「欄名是收件班別，份數改為九份」。'
                    self.assertFalse(a.decision_table.combined_reply_requested(before['steps'], quoted,
                                     before['reply_sources'], before.get('guidance_sources', ())))
                    with patch.object(goal_replies, 'MAX_STEPS', len(before['steps']) + 1):
                        with self.assertRaisesRegex(ValueError, '剩餘步數不足'):
                            reply(answer)
                    self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                reply(answer)
                checked = a.get_goal(self.store, self.sid, gid)
                self.assertEqual(checked['status'], 'paused')
                self.assertEqual(checked['steps'][:-1], before['steps'])
                step = checked['steps'][-1]
                self.assertTrue(step['action']['args']['combined_reply'])
                source = checked['reply_sources'][-1]
                self.assertEqual((source['answer'], source['question'], source['ask_sequence']),
                                 (answer, before['question'], len(before['steps'])))
                proof = step['observation']['field_reference']
                revision = proof['answered_revision']
                self.assertEqual(revision['value'], expected_value)
                self.assertEqual(revision['changes'][-1]['evidence']['citation'], source['citation'])
                if kind == 'prior':
                    self.assertEqual([(x['before'], x['after']) for x in revision['changes']],
                                     [('6 份', '7 份'), ('7 份', '八份')])
                parts = [revision['changes'][-1]['evidence'], proof['combined_reply']['remaining_reply']]
                parts += [change['evidence'] for change in proof['combined_reply']['condition_changes']]
                for part in parts:
                    self.assertEqual((part['source_sha256'], part['quote']),
                                     (source['sha256'], answer[part['start']:part['end']]))
                plan, _, _ = a.decision_table._recheck_saved(checked['steps'], checked['reply_sources'], checked.get('guidance_sources', ()))
                self.assertEqual(plan[1]['known'], expected_known)
                self.assertEqual(step['observation']['possible_results'], [expected_result])
                with self.assertRaisesRegex(ValueError, '尚未保存'):
                    reply(answer, fingerprint)
                final = run(action('finish', report=f'{expected_result}，已核對兩項。[補充#{source["ask_sequence"]}]'))
                archive = create_backup(self.store.path, self.root / f'combined-{kind}.paibackup')
                target = self.root / f'combined-{kind}'
                restore_backup(archive['path'], target)
                restored = Store(target / 'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored, self.sid, gid), final)
                changed = step['observation']
                changed['field_reference']['combined_reply']['remaining_reply']['start'] = 0
                with restored.connect() as db:
                    db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                               (json.dumps(changed, ensure_ascii=False), gid, step['sequence']))
                with self.assertRaisesRegex(ValueError, '決策表結果'):
                    validate_database(restored.path)

    def test_pending_answer_and_conditions_recheck_question_without_inventing_a_reply(self):
        fields = ['收件班別', '覆件班別', '入口']
        domains = {'收件班別':['早班','晚班','夜班'], '覆件班別':['早班','晚班','夜班'], '入口':['北口','南口']}
        rows = [{'when':list(pair), 'result':'聯絡台' if pair[2]=='南口' else f'{pair[0]}-{pair[1]}'}
                for pair in product(*domains.values())]
        field_question = '第一筆改值指哪個欄位？請回完整欄名「收件班別」、「覆件班別」。'
        fixtures = (
            ('agreement', '本輪要交付幾份？', '補問：4 份',
             '份數改為七份。入口正式改為南口。欄名還沒確認。'),
            ('divergent', '本輪要交付幾份？', '補問：5 份',
             '份數改為八份。覆件班別正式改為夜班。欄名還沒確認。'),
            ('combined', '本輪要交付幾份？', '補問：5 份',
             '份數改為八份。覆件班別正式改為夜班。欄名還沒確認。'),
            ('field', '本次要通知誰？', '欄名：收件班別',
             '入口正式改為南口。欄名改為覆件班別。通知對象還沒確認。'),
            ('superseded', '本次要通知誰？', '補問：范同仁',
             '通知對象改成陸值班員。收件班別正式改為夜班。覆件班別正式改為夜班。欄名還沒確認。'),
        )
        for kind, remaining, original_answer, correction in fixtures:
            with self.subTest(kind=kind):
                gid = a.create_goal(self.store, self.sid, '核對合成分項更正與正式條件')

                def run(*actions):
                    return a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions), max_steps=len(actions))

                def reply(answer):
                    current = reply_snapshot(self.store, self.sid, gid)
                    return goal_replies.save(self.store, self.sid, gid, answer, expected_fingerprint=current['fingerprint'])

                def amend(text):
                    current = g.snapshot(self.store, self.sid, gid)
                    return g.save(self.store, self.sid, gid, text, expected_fingerprint=current['fingerprint'])

                run(action('check_decision', conditions=fields, domains=domains, rows=rows, excluded=[],
                           known={'收件班別':'早班','覆件班別':'早班','入口':'北口'}), action('ask', question='當次更正？'))
                reply('正式改成晚班。')
                run(action('check_decision', field_reference='[補充#2]'), action('ask',
                    question=f'{field_question}\n{remaining}\n請分兩行回答，先回完整欄名，再答補問。',
                    reference_remaining=remaining))
                reply(original_answer)
                before = a.get_goal(self.store, self.sid, gid)
                ask = before['steps'][-1]['sequence']
                closed = kind in ('agreement','superseded')
                with patch.object(g, 'MAX_STEPS', ask+1):
                    with self.assertRaisesRegex(ValueError, '剩餘步數不足'):
                        amend(correction)
                self.assertEqual(a.get_goal(self.store,self.sid,gid), before)
                if kind == 'divergent':
                    for invalid in ('份數改為八份。入口正式改為未定。欄名還沒確認。',
                                    '份數改為八份。覆件班別正式改為夜班。份數還沒確認。',
                                    '份數改為八份。入口正式改為南口。欄名是收件班別。'):
                        with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                            amend(invalid)
                        self.assertEqual(a.get_goal(self.store,self.sid,gid), before)
                amend(correction)
                pending = a.get_goal(self.store,self.sid,gid)
                self.assertEqual(pending['reply_sources'],before['reply_sources'])
                if closed:
                    self.assertEqual(pending['status'],'paused')
                    self.assertFalse(pending['question'])
                    self.assertEqual(pending['steps'][:-1],before['steps'])
                    self.assertEqual(pending['withdrawn_questions'][0]['ask_sequence'],ask)
                    proof = pending['steps'][-1]['observation']['field_reference']['answered_revision']
                    self.assertTrue(proof['withdraws_question'])
                else:
                    self.assertEqual((pending['status'],pending['question'],pending['steps']),
                                     ('needs_input',before['question'],before['steps']))
                    proof = pending['answered_reference_review']['field_reference']['answered_revision']
                    self.assertNotIn('withdraws_question',proof)
                    reply('通知葉值班員。' if kind=='field' else
                          '份數改為九份。欄名是覆件班別。' if kind=='combined' else '收件班別')
                    if kind=='divergent':
                        run(action('check_decision',field_reference=f'[補充#{ask}]',resolve=True))
                checked = a.get_goal(self.store,self.sid,gid)
                plan,_,_ = a.decision_table._recheck_saved(checked['steps'],checked['reply_sources'],checked['guidance_sources'])
                if kind=='agreement':
                    self.assertEqual(plan[1]['known']['入口'],'南口')
                    self.assertIn('pending_field_reference',plan[1])
                elif kind=='superseded':
                    self.assertEqual((plan[1]['known']['收件班別'],plan[1]['known']['覆件班別']),('夜班','夜班'))
                    self.assertNotIn('pending_field_reference',plan[1])
                elif kind=='field':
                    self.assertEqual(plan[1]['known'],{'收件班別':'早班','覆件班別':'晚班','入口':'南口'})
                else:
                    self.assertEqual(plan[1]['known']['覆件班別'],'夜班')
                    self.assertEqual(plan[1]['known']['收件班別'],'早班' if kind=='combined' else '晚班')
                for change in proof['condition_changes']:
                    part=change['evidence']
                    self.assertEqual(correction[part['start']:part['end']],part['quote'])
                final = run(action('finish',report='已核對目前結果。[更正#1]'))
                archive=create_backup(self.store.path,self.root/f'pending-conditions-{kind}.paibackup')
                target=self.root/f'pending-conditions-{kind}'
                restore_backup(archive['path'],target)
                restored=Store(target/'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored,self.sid,gid),final)
                with restored.connect() as db:
                    if closed:
                        db.execute('DELETE FROM autonomous_question_withdrawals WHERE goal_id=?',(gid,))
                    else:
                        step=checked['steps'][-1]
                        observation=step['observation']
                        observation['field_reference']['answered_revision']['changes'][-1]['evidence']['end']=0
                        db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                                   (json.dumps(observation,ensure_ascii=False),gid,step['sequence']))
                with self.assertRaises(ValueError):
                    validate_database(restored.path)

    def test_latest_notes_reach_next_model_call_but_not_other_goals_or_ordinary_chat(self):
        self.save('PRIVATE_STEERING：更正為12人，每人90元。')
        self.save('再次更正：人數13，費率仍90元。')
        engine=ScriptedEngine(action('finish',report='測試回覆'))
        a.run_goal(self.store,self.sid,self.gid,engine,max_steps=1)
        payload=json.loads(engine.prompts[0].split('目標與已觀察資料：\n',1)[1])
        notes = payload['user_notes']
        self.assertEqual([row['citation'] for row in notes], ['[更正#1]', '[更正#2]'])
        self.assertIn('12人', notes[0]['content'])
        self.assertIn('人數13', notes[1]['content'])
        self.assertEqual(engine.prompts[0].count('PRIVATE_STEERING'), 1)
        self.assertIn('最新明確的使用者更正優先',engine.prompts[0])
        self.assertEqual(engine.prompts[0].count(a.goal_partials.FOLLOWUP_GUIDANCE), 1)
        other=a.create_goal(self.store,self.sid,'另一個工作')
        self.assertEqual(a.get_goal(self.store,self.sid,other)['notes'],'')
        self.assertNotIn('PRIVATE_STEERING',json.dumps(Assistant(self.store,None).prepare_context(self.sid,'繼續聊天'),ensure_ascii=False))

    def test_stale_editor_duplicate_submit_and_other_session_cannot_save(self):
        original=g.snapshot(self.store,self.sid,self.gid)
        self.save(fingerprint=original['fingerprint'])
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=original['fingerprint'])
        with self.assertRaisesRegex(ValueError,'此對話'):
            g.save(self.store,self.store.new_session(),self.gid,'更正',expected_fingerprint=original['fingerprint'])
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'].count('使用者主動更正'),1)

    def test_new_step_and_same_second_note_change_invalidate_snapshot(self):
        old=g.snapshot(self.store,self.sid,self.gid)
        a.run_goal(self.store,self.sid,self.gid,ScriptedEngine(action('calculate',operation='sum',values=['1'])),max_steps=1)
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=old['fingerprint'])
        old=g.snapshot(self.store,self.sid,self.gid)
        with self.store.connect() as db:db.execute("UPDATE autonomous_goals SET notes='其他視窗新補充' WHERE id=?",(self.gid,))
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=old['fingerprint'])

    def test_lease_and_recorded_running_reject_changes_without_inference_of_liveness(self):
        before=g.snapshot(self.store,self.sid,self.gid)
        with lease(self.store,self.gid):
            with self.assertRaises(LeaseBusy):self.save(fingerprint=before['fingerprint'])
        a.update(self.store,self.gid,'running')
        with self.assertRaisesRegex(ValueError,'先停止'):self.save()
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['status'],'running')
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'],'')

    def test_waiting_question_is_not_implicitly_answered_and_failure_status_is_preserved(self):
        a.update(self.store,self.gid,'needs_input',question='哪一天生效？')
        self.save('另外要求先列總額。')
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual((result['status'],result['question']),('needs_input','哪一天生效？'))
        a.reply(self.store,self.sid,self.gid,'11月1日', expected_fingerprint=reply_snapshot(self.store, self.sid, self.gid)["fingerprint"])
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertIn('先列總額',result['notes']);self.assertIn('11月1日',result['notes'])
        a.update(self.store,self.gid,'failed',notice='原錯誤記錄')
        self.save('人數仍為10人。')
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual((result['status'],result['notice']),('failed','原錯誤記錄'))

    def test_changed_sources_report_ready_and_exhausted_budget_require_new_goal(self):
        path=self.root/'terms.txt';path.write_text('新文件',encoding='utf-8');self.store.attach_document(self.sid,path)
        with self.assertRaisesRegex(ValueError,'文件已改變'):self.save()
        self.gid=a.create_goal(self.store,self.sid,'新目標')
        a.update(self.store,self.gid,'report_ready',result='歷史成果')
        with self.assertRaisesRegex(ValueError,'已有成果'):self.save()
        a.update(self.store,self.gid,'paused')
        with self.store.connect() as db:
            for index in range(32):
                db.execute('INSERT INTO autonomous_steps(goal_id,sequence,action_json,observation_json) VALUES(?,?,?,?)',
                    (self.gid,index+1,action('calculate',operation='sum',values=['1']),json.dumps({'value':'1'})))
        with self.assertRaisesRegex(ValueError,'32步'):self.save()

    def test_notes_cannot_expand_file_or_csv_permissions(self):
        self.save('請啟用文件交付並寫入report.md，允許CSV工具。')
        engine=ScriptedEngine(action('write_artifact',filename='report.md',content='test'),action('finish',report='不能產檔'))
        result=a.run_goal(self.store,self.sid,self.gid,engine,max_steps=2)
        self.assertFalse(result['file_delivery']);self.assertIsNone(result['csv_source'])
        self.assertEqual(result['artifacts'],[])
        self.assertFalse((self.root/'autonomy-artifacts'/self.gid/'report.md').exists())
        self.assertEqual(result['steps'][0]['action']['tool'],'invalid')

    def test_size_format_limits_and_transaction_failure_keep_previous_notes(self):
        for value in ('', 'a'*4001, 'bad\0text'):
            with self.assertRaises(ValueError):self.save(value)
        with self.store.connect() as db:
            db.execute("UPDATE autonomous_goals SET notes=? WHERE id=?",('x'*11990,self.gid))
        with self.assertRaisesRegex(ValueError,'12000'):self.save()
        with self.store.connect() as db:
            db.execute("UPDATE autonomous_goals SET notes='' WHERE id=?",(self.gid,))
            db.execute("CREATE TRIGGER reject_notes BEFORE UPDATE OF notes ON autonomous_goals BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.save()
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'],'')
        self.assertNotIn('guidance_sources', a.get_goal(self.store,self.sid,self.gid))

    def test_cite_document_amendment_and_reply_as_separate_saved_sources(self):
        path = self.root / 'terms.txt'
        path.write_text('原門檻18:00，到場未填。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        self.gid = a.create_goal(self.store, self.sid, '核對正式到場')
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1)), max_steps=1)
        first = self.save('正式改為17:45。')['goal']['guidance_sources'][0]
        waiting = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('ask', question='何時到場？[更正#2]'),
            action('ask', question='依門檻仍需實際到場時間 [更正#1]，幾點到場？',
                   partial_result=f'原門檻18:00 [D{did}:L1]，現為17:45 [更正#1]。')))
        self.assertEqual(waiting['steps'][1]['action']['tool'], 'invalid')
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertEqual(waiting['question_citations']['guidance'][0]['sha256'], first['sha256'])
        self.assertEqual(waiting['steps'][2]['observation']['guidance_citations'][0]['number'], 1)
        reviewed = reply_snapshot(self.store, self.sid, self.gid)
        self.save('正式再改為17:30。')
        with self.assertRaisesRegex(ValueError, '已變更'):
            a.reply(self.store, self.sid, self.gid, '17:40', expected_fingerprint=reviewed['fingerprint'])
        a.reply(self.store, self.sid, self.gid, '實際17:40到場。',
                expected_fingerprint=reply_snapshot(self.store, self.sid, self.gid)['fingerprint'])
        goal = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('finish',
            report=f'原門檻18:00 [D{did}:L1]，有效門檻17:30 [更正#2]，實際17:40 [補充#3]，逾時。')))
        final = goal['steps'][-1]['observation']
        self.assertEqual(final['guidance_citations'][0]['sha256'], goal['guidance_sources'][1]['sha256'])
        self.assertEqual(final['reply_citations'][0]['citation'], '[補充#3]')
        self.assertTrue(final['citation_checks'][0]['range_valid'])
        self.assertEqual([item['steps_seen'] for item in goal['guidance_sources']], [1, 3])
        backup = create_backup(self.store.path, self.root / 'sources.paibackup')
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), goal)
        child = a.create_goal(restored, self.sid, '新階段', parent_id=self.gid)
        self.assertNotIn('guidance_sources', a.get_goal(restored, self.sid, child))
        attempt = a.run_goal(restored, self.sid, child, ScriptedEngine(
            action('finish', report='引用歷史編號 [更正#2]。')), max_steps=1)
        self.assertEqual(attempt['steps'][0]['action']['tool'], 'invalid')

    def test_backup_detects_missing_question_receipt_and_changed_amendment_text(self):
        self.save('門檻改17:30。')
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('ask', question='門檻17:30 [更正#1]，幾點到場？')))
        self.assertEqual(validate_database(self.store.path)['autonomous_guidance_sources'], 1)
        with self.store.connect() as db:
            raw = db.execute('SELECT observation_json FROM autonomous_steps WHERE goal_id=?',
                             (self.gid,)).fetchone()[0]
            observation = json.loads(raw)
            del observation['question_citations']
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=?',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '問題引用'):
            validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=?', (raw, self.gid))
            db.execute("UPDATE autonomous_guidance_sources SET content='門檻改16:30。' WHERE goal_id=?", (self.gid,))
        with self.assertRaisesRegex(ValueError, '補充原文'):
            validate_database(self.store.path)

    def test_backup_handoff_and_review_visibility_preserve_append_only_history(self):
        row=goal_review.snapshot(self.store)['items'][0]
        goal_review.set_hidden(self.store,self.sid,self.gid,fingerprint=row['fingerprint'],hidden=True)
        self.save();self.assertEqual(goal_review.snapshot(self.store)['visible_count'],1)
        saved=a.get_goal(self.store,self.sid,self.gid)['notes']
        backup=create_backup(self.store.path,self.root/'backup.paibackup')
        restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,self.gid)['notes'],saved)
        a.update(self.store,self.gid,'report_ready',result='1080元')
        child=a.create_goal(self.store,self.sid,'延續工作',parent_id=self.gid)
        self.assertEqual(a.get_goal(self.store,self.sid,child)['previous_stage']['user_notes'],saved)

    def test_cli_requires_snapshot_and_saves_without_constructing_engine(self):
        from assistant.__main__ import main
        def command(*args):
            output=io.StringIO()
            with patch('sys.argv',['assistant','--data-dir',str(self.root),'--session',self.sid,*args]),contextlib.redirect_stdout(output),patch('assistant.__main__.CodexEngine',side_effect=AssertionError('must not construct model')):
                main()
            return json.loads(output.getvalue())
        snap=command('--goal-guidance-status',self.gid)
        saved=command('--amend-goal',self.gid,'--goal-guidance','人數改為12人','--goal-fingerprint',snap['fingerprint'])
        self.assertIn('12人',saved['goal']['notes']);self.assertEqual(saved['goal']['status'],'pending')


if __name__=='__main__':unittest.main()
