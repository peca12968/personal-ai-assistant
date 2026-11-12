from assistant.goal_replies import snapshot as reply_snapshot
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import EngineError
from assistant.store import Store
from assistant.autonomy_status import goal_context


def action(tool, **args):
    return json.dumps({'tool': tool, 'args': args, 'note': '執行必要步驟'}, ensure_ascii=False)


class ScriptedEngine:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []

    def answer(self, prompt, cancel=None):
        self.prompts.append(prompt)
        value = next(self.answers)
        return value() if callable(value) else value


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def goal(self, objective='整理文件並完成可用成果'):
        return a.create_goal(self.store, self.sid, objective)

    def attach(self, content, sid=None, name='note.txt'):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, path)

    def test_tool_loop_uses_observations_and_persists_report(self):
        did = self.attach('方案甲費用120元\n方案乙費用80元\n保留30天')
        gid = self.goal()
        report = f'合計200元。[D{did}:L1-L2]'
        engine = ScriptedEngine(action('search', query='費用'),
            action('read', document_id=did, start_line=1, end_line=3),
            action('calculate', operation='sum', values=['120', '80']), action('finish', report=report))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['result'], report)
        self.assertIn('保留30天', engine.prompts[2])
        self.assertIn('"exact": "200"', engine.prompts[3])
        self.assertEqual(result['steps'][-1]['observation']['citation_checks'][0]['range_valid'], True)
        self.assertEqual(a.get_goal(Store(self.store.path), self.sid, gid), result)
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None), result)

    def test_ask_reply_resume_and_session_isolation(self):
        gid = self.goal('安排會議')
        first = ScriptedEngine(action('ask', question='哪一天？'))
        result = a.run_goal(self.store, self.sid, gid, first)
        self.assertEqual(result['status'], 'needs_input')
        self.assertNotIn(a.goal_partials.FOLLOWUP_GUIDANCE, first.prompts[0])
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None), result)
        other = self.store.new_session()
        with self.assertRaises(ValueError):
            a.reply(self.store, other, gid, '明天', expected_fingerprint=reply_snapshot(self.store, self.sid, gid)["fingerprint"])
        self.assertEqual(a.goals(self.store, other), [])
        a.reply(self.store, self.sid, gid, '明天下午', expected_fingerprint=reply_snapshot(self.store, self.sid, gid)["fingerprint"])
        engine = ScriptedEngine(action('finish', report='明天下午的會議準備清單。'))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertIn('明天下午', engine.prompts[0])
        self.assertEqual(engine.prompts[0].count(a.goal_partials.FOLLOWUP_GUIDANCE), 1)
        self.assertEqual(len(result['steps']), 2)
        with self.assertRaises(ValueError):
            a.reply(self.store, self.sid, gid, '不應覆寫成果', expected_fingerprint=reply_snapshot(self.store, self.sid, gid)["fingerprint"])

    def test_unread_citation_rejected_then_repaired(self):
        did = self.attach('已確認事項')
        gid = self.goal()
        engine = ScriptedEngine(action('finish', report=f'事項[D{did}:L1]'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('finish', report=f'事項[D{did}:L1]'))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertNotIn(a.DOCUMENT_CLAIMS_GUIDANCE, engine.prompts[0])
        self.assertNotIn(a.DOCUMENT_CLAIMS_GUIDANCE, engine.prompts[1])
        self.assertIn(a.DOCUMENT_CLAIMS_GUIDANCE, engine.prompts[2])
        self.assertEqual(result['status'], 'report_ready')

    def test_partial_report_waits_separately_and_survives_restore_and_reply(self):
        did = self.attach('甲批確認可出27件')
        gid = self.goal('先提供已知數量，再等收貨人。')
        partial = '甲批可出27件。' * 120 + f'[D{did}:L1]'
        question = '乙批收貨人是誰？'
        engine = ScriptedEngine(action('read', document_id=did, start_line=1, end_line=1),
                                action('ask', question=question, partial_result=partial))
        waiting = a.run_goal(self.store, self.sid, gid, engine)
        self.assertGreater(len(partial), 800)
        self.assertEqual((waiting['status'], waiting['result'], waiting['question'], waiting['partial_result']),
                         ('needs_input', '', question, partial))
        self.assertEqual(waiting['artifacts'], [])
        self.assertEqual(waiting['steps'][-1]['observation']['citation_checks'],
                         [{'citation': f'[D{did}:L1]', 'range_valid': True}])
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None), waiting)

        child = a.create_goal(self.store, self.sid, '另建下一階段。', parent_id=gid)
        progress = a.get_goal(self.store, self.sid, child)['previous_stage']['checkpoint']
        self.assertTrue(progress['activity'][-1]['observation_metadata']['value']['partial_result_omitted'])
        self.assertNotIn(partial, json.dumps(progress, ensure_ascii=False))
        archive = self.root / 'waiting.paibackup'
        create_backup(self.store.path, archive)
        restored_dir = self.root / 'restored'
        restore_backup(archive, restored_dir)
        restored = Store(restored_dir / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), waiting)
        review = reply_snapshot(restored, self.sid, gid)
        self.assertEqual(review['goal']['partial_result'], partial)
        a.reply(restored, self.sid, gid, '王同仁；甲批更正為26件。', expected_fingerprint=review['fingerprint'])
        paused = a.get_goal(restored, self.sid, gid)
        self.assertNotIn('partial_result', paused)
        self.assertEqual(paused['steps'][-1]['observation']['partial_result'], partial)
        resumed = ScriptedEngine(action('finish', report='甲批26件，乙批收貨人王同仁。'))
        final = a.run_goal(restored, self.sid, gid, resumed)
        self.assertEqual(final['result'], '甲批26件，乙批收貨人王同仁。')
        self.assertIn('甲批更正為26件', resumed.prompts[0])
        self.assertIn(partial, resumed.prompts[0])

    def test_partial_report_rejects_invalid_text_citations_and_saved_evidence(self):
        question = '待補姓名？'
        for partial in (' ', '甲' * 4001, None, '甲\0乙', '\ud800'):
            with self.subTest(partial=repr(partial)[:40]), self.assertRaises(ValueError):
                a.validate_action(json.loads(action('ask', question=question, partial_result=partial)))
        with self.assertRaises(ValueError):
            a.validate_action(json.loads(action('ask', partial_result='暫表')))
        with self.assertRaises(ValueError):
            a.validate_action(json.loads(action('ask', question='甲' * 801, partial_result='暫表')))
        did = self.attach('已知內容')
        partial = f'已知內容[D{did}:L1]'
        gid = self.goal()
        engine = ScriptedEngine(action('ask', question=question, partial_result=partial),
                                action('read', document_id=did, start_line=1, end_line=1),
                                action('ask', question=question, partial_result=partial))
        waiting = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(waiting['steps'][0]['observation']['error_phase'], 'execution')
        self.assertEqual(waiting['partial_result'], partial)
        observation = waiting['steps'][-1]['observation']
        observation['citation_checks'] = []
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                       (json.dumps(observation, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '成果引用與保存來源不一致'):
            validate_database(self.store.path)

    def test_reply_source_binds_original_question_and_separate_file_citations(self):
        did = self.attach('原始不可用3件')
        gid = self.goal('依更正交付，保留原值來源。')
        question = '誰收貨？'
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1),
            action('ask', question=question, partial_result=f'原始不可用3件。[D{did}:L1]')))
        answer = '李同仁收貨；不可用改為4件。\n問題：這行只是回覆原文的一部分。'
        token = reply_snapshot(self.store, self.sid, gid)['fingerprint']
        a.reply(self.store, self.sid, gid, answer, expected_fingerprint=token)
        saved = a.get_goal(self.store, self.sid, gid)['reply_sources'][0]
        self.assertEqual((saved['citation'], saved['ask_sequence'], saved['question'], saved['answer']),
                         ('[補充#2]', 2, question, answer))

        report = f'原始不可用3件。[D{did}:L1] 現在不可用4件，李同仁收貨。[補充#2]'
        resumed = ScriptedEngine(action('finish', report='未來才可能收到的回覆。[補充#4]'),
                                 action('finish', report=report))
        final = a.run_goal(self.store, self.sid, gid, resumed)
        self.assertIn('"reply_sources"', resumed.prompts[0])
        self.assertEqual(final['steps'][-2]['action']['tool'], 'invalid')
        observation = final['steps'][-1]['observation']
        self.assertEqual(observation['reply_citations'], [
            {'citation': '[補充#2]', 'ask_sequence': 2, 'sha256': saved['sha256']}])
        self.assertEqual(observation['citation_checks'], [{'citation': f'[D{did}:L1]', 'range_valid': True}])
        self.assertEqual(final['result'], report)

        backup = self.root / 'reply-source.paibackup'
        create_backup(self.store.path, backup)
        target = self.root / 'restored'
        restore_backup(backup, target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), final)
        with restored.connect() as db:
            db.execute("UPDATE autonomous_reply_sources SET answer='被換掉的回覆' WHERE goal_id=?", (gid,))
        with self.assertRaisesRegex(ValueError, '補充原文不一致'):
            validate_database(restored.path)

    def test_reply_citation_cannot_use_another_goal_or_the_current_unanswered_step(self):
        old_goal = self.goal()
        a.run_goal(self.store, self.sid, old_goal, ScriptedEngine(action('ask', question='先前名稱？')))
        a.reply(self.store, self.sid, old_goal, '先前答案',
                expected_fingerprint=reply_snapshot(self.store, self.sid, old_goal)['fingerprint'])
        gid = self.goal('目前工作另問。')
        engine = ScriptedEngine(action('ask', question='本次名稱？', partial_result='沿用名稱。[補充#1]'),
                                action('ask', question='本次名稱？', partial_result='提前引用。[補充#2]'),
                                action('ask', question='本次名稱？'))
        waiting = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual([step['action']['tool'] for step in waiting['steps']], ['invalid', 'invalid', 'ask'])
        self.assertNotIn('reply_sources', waiting)
        self.assertEqual(waiting['question'], '本次名稱？')

    def test_followup_question_checks_document_and_reply_sources_separately(self):
        did = self.attach('甲批收貨人待確認。\n乙批覆核人待確認。')
        gid = self.goal('收貨人與覆核人分次確認。')
        first = a.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=2),
            action('ask', question='兩位的姓名各是誰？')))
        first_review = reply_snapshot(self.store, self.sid, gid)
        a.reply(self.store, self.sid, gid, '只確認甲批收貨人是曾同仁。',
                expected_fingerprint=first_review['fingerprint'])
        question = f'乙批覆核人是誰？[D{did}:L2] 甲批收貨人已補充。[補充#2]'
        partial = f'曾同仁為甲批收貨人。[補充#2] 乙批仍待補。'
        resumed = ScriptedEngine(
            action('ask', question=f'乙批覆核人是誰？[D{did}:L99]', partial_result=partial),
            action('ask', question='乙批覆核人是誰？[補充#99]', partial_result=partial),
            action('ask', question=question, partial_result=partial),
        )
        second = a.run_goal(self.store, self.sid, gid, resumed)
        self.assertEqual(second['status'], 'needs_input')
        self.assertEqual(second['question'], question)
        self.assertNotIn('question_citations', first)
        self.assertEqual(second['steps'][:2], first['steps'])
        failures = [step['observation']['error'] for step in second['steps'][2:4]]
        self.assertIn('問題有未讀取的來源行號', failures[0])
        self.assertIn('問題引用了本目標尚未保存的回覆', failures[1])
        observation = second['steps'][-1]['observation']
        self.assertEqual(observation['citation_checks'], [])
        self.assertEqual(second['question_citations'], {
            'documents': [{'citation': f'[D{did}:L2]', 'range_valid': True}],
            'replies': observation['reply_citations'],
        })
        review = reply_snapshot(self.store, self.sid, gid)
        self.assertEqual(review['goal']['question_citations'], second['question_citations'])
        with self.assertRaisesRegex(ValueError, '已變更'):
            a.reply(self.store, self.sid, gid, '舊問題的回覆',
                    expected_fingerprint=first_review['fingerprint'])

        archive = self.root / 'question-citations.paibackup'
        target = self.root / 'restored'
        create_backup(self.store.path, archive)
        restore_backup(archive, target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), second)
        observation['question_citations']['replies'][0]['sha256'] = '0' * 64
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(observation, ensure_ascii=False), gid, len(second['steps'])))
        with self.assertRaisesRegex(ValueError, '問題引用與保存的來源不一致'):
            validate_database(restored.path)

    def test_question_only_can_record_document_source_without_partial_result(self):
        did = self.attach('窗口姓名待補。')
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1),
            action('ask', question=f'窗口姓名是什麼？[D{did}:L1]')))
        self.assertEqual(result['status'], 'needs_input')
        self.assertNotIn('partial_result', result)
        self.assertEqual(result['question_citations'], {
            'documents': [{'citation': f'[D{did}:L1]', 'range_valid': True}],
            'replies': [],
        })

    def test_read_cannot_access_other_session_and_reports_incomplete_range(self):
        other = self.store.new_session()
        secret = self.attach('PRIVATE_OTHER_SESSION', other)
        did = self.attach('public', name='public.txt')
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(
            action('read', document_id=secret, start_line=1, end_line=1),
            action('read', document_id=did, start_line=1, end_line=2)), max_steps=2)
        self.assertNotIn('PRIVATE_OTHER_SESSION', json.dumps(result))
        self.assertEqual([s['observation']['requested_ranges'][0]['status'] for s in result['steps']],
                         ['document_not_attached', 'line_out_of_bounds'])

    def test_ordered_batch_retains_readable_ranges_and_exact_citation_coverage(self):
        first = self.attach('已確認數量\n已確認窗口', name='first.txt')
        second = self.attach('長' * 10000 + '\n可讀期限', name='second.txt')
        other = self.store.new_session()
        foreign = self.attach('PRIVATE_OTHER_SESSION', other, name='foreign.txt')
        ranges = [
            {'document_id': did, 'start_line': start, 'end_line': end}
            for did, start, end in ((first, 1, 2), (foreign, 1, 1), (second, 3, 3),
                                    (second, 1, 1), (second, 2, 2), (first, 2, 2),
                                    (first, 2, 1), (first, 1, 201))
        ]
        gid = self.goal()
        engine = ScriptedEngine(
            action('read_ranges', ranges=ranges),
            action('finish', report=f'缺漏行不能引用。[D{second}:L3]'),
            action('ask', question='代理人是誰？',
                   partial_result=f'數量和窗口已確認。[D{first}:L1-L2] 期限已讀。[D{second}:L2]'),
        )
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(result['status'], 'needs_input')
        batch = result['steps'][0]['observation']
        self.assertEqual([row['status'] for row in batch['requested_ranges']],
                         ['provided', 'document_not_attached', 'line_out_of_bounds', 'character_limit',
                          'provided', 'provided', 'invalid_range', 'range_too_large'])
        for actual, expected in zip(batch['requested_ranges'], ranges, strict=True):
            self.assertEqual({field: actual[field] for field in expected}, expected)
        self.assertEqual([(row['document_id'], row['text']) for row in batch['excerpts']],
                         [(first, 'L1: 已確認數量\nL2: 已確認窗口'), (second, 'L2: 可讀期限')])
        self.assertNotIn('PRIVATE_OTHER_SESSION', json.dumps(result))
        self.assertEqual(result['steps'][1]['observation']['error_phase'], 'execution')
        self.assertTrue(all(row['range_valid'] for row in result['steps'][2]['observation']['citation_checks']))

        child = a.create_goal(self.store, self.sid, '接續可用資料', parent_id=gid)
        checkpoint = a.get_goal(self.store, self.sid, child)['previous_stage']['checkpoint']
        self.assertEqual(checkpoint['activity'][0]['request']['value']['ranges'], ranges)
        self.assertNotIn('已確認數量', json.dumps(checkpoint, ensure_ascii=False))

    def test_literal_document_lookup_pages_and_scopes_full_line_citations(self):
        did = self.attach('\n'.join(['甲🧭Code 1'] + [f'Code {i}' for i in range(2, 7)]
                                   + ['Code ' + '長' * 8100, 'Code 8', 'code 9']))
        other = self.attach('Code other', name='other.txt')
        gid = self.goal()
        lookup = {'document_id': did, 'query': 'Code', 'start_line': 1}
        engine = ScriptedEngine(action('find_document_text', **lookup),
            action('find_document_text', **{**lookup, 'start_line': 7}),
            action('find_document_text', **{**lookup, 'query': 'Ｃｏｄｅ'}),
            action('finish', report=f'錯誤聲稱已讀超長行 [D{did}:L7]'),
            action('finish', report=f'第 8 行已提供 [D{did}:L8]'))
        result = a.run_goal(self.store, self.sid, gid, engine)
        first, second, no_match, invalid, final = result['steps']
        self.assertNotIn(a.document_ranges.LOOKUP_EVIDENCE_GUIDANCE, engine.prompts[0])
        self.assertTrue(all(a.document_ranges.LOOKUP_EVIDENCE_GUIDANCE in p for p in engine.prompts[1:]))
        page = first['observation']
        self.assertEqual(page['total_matching_lines'], 8)
        self.assertEqual(page['next_start_line'], 7)
        self.assertEqual(page['matches'][0], {'line_number': 1, 'match_start_char': 3,
            'match_end_char': 6, 'line_chars': 8, 'full_line_provided': True})
        self.assertTrue(all(row['document_id'] == did != other for row in page['excerpts']))
        page = second['observation']
        self.assertEqual(page['total_matching_lines'], 8)
        self.assertIsNone(page['next_start_line'])
        self.assertEqual([row['full_line_provided'] for row in page['matches']], [False, True])
        self.assertEqual([row['start_line'] for row in page['excerpts']], [8])
        self.assertEqual(no_match['observation']['total_matching_lines'], 0)
        self.assertEqual(no_match['observation']['excerpts'], [])
        self.assertEqual(invalid['action']['tool'], 'invalid')
        self.assertTrue(final['observation']['citation_checks'][0]['range_valid'])

    def test_literal_document_lookup_batches_distinct_pages_and_shares_excerpts(self):
        lines = ['Code Other'] + [f'Code {n}' for n in range(2, 8)]
        lines += [f'Other {n}' for n in range(8, 14)] + ['VeryLong ' + '長' * 8100]
        did = self.attach('\n'.join(lines))
        self.attach('Code Other', name='excluded.txt')
        requests = [{'query': text, 'start_line': 1} for text in ('Code', 'Other', 'VeryLong', 'Absent')]
        engine = ScriptedEngine(
            action('find_document_text', document_id=did, queries=requests),
            action('find_document_text', document_id=did, query='Other', start_line=13),
            action('finish', report=f'把只有位置的結果當成已讀 [D{did}:L6]'),
            action('finish', report=f'合併原行 [D{did}:L1]，另查續頁 [D{did}:L13]'))
        result = a.run_goal(self.store, self.sid, self.goal(), engine)
        batch, continuation, invalid, final = result['steps']
        observation = batch['observation']
        self.assertEqual([row['start_line'] for row in observation['excerpts']], [1, 2, 3, 4, 5, 8, 9, 10])
        self.assertTrue(all(row['document_id'] == did for row in observation['excerpts']))
        pages = observation['queries']
        self.assertEqual([(p['query'], p['total_matching_lines'], p['next_start_line']) for p in pages],
                         [('Code', 7, 7), ('Other', 7, 13), ('VeryLong', 1, None), ('Absent', 0, None)])
        self.assertEqual([(p['matches'][0]['match_start_char'], p['matches'][0]['full_line_provided'])
                          for p in pages[:3]], [(1, True), (6, True), (1, False)])
        self.assertFalse(pages[0]['matches'][-1]['full_line_provided'])
        self.assertEqual([e['start_line'] for e in continuation['observation']['excerpts']], [13])
        self.assertNotIn('queries', continuation['observation'])
        self.assertEqual(invalid['action']['tool'], 'invalid')
        self.assertTrue(all(c['range_valid'] for c in final['observation']['citation_checks']))

    def test_literal_document_lookup_rejects_duplicate_and_incomplete_batch(self):
        did = self.attach('Code')
        query = {'query': 'Code', 'start_line': 1}
        for queries in ([], [query] * 2, [query] * 7, [query, {'query': 'Other'}],
                        [query, {'query': 'Other', 'start_line': True}]):
            with self.subTest(queries=queries), self.assertRaises(ValueError):
                a.validate_action(json.loads(action('find_document_text', document_id=did, queries=queries)))
        documents = self.store.documents(self.sid, include_content=True)
        args = {'document_id': did, 'queries': [query, {'query': 'Other', 'start_line': 2}]}
        with self.assertRaises(ValueError):
            a.document_ranges.lookup(documents, args)

    def test_literal_lookup_keeps_source_failures_and_same_numbered_lines_separate(self):
        first = self.attach('\n'.join(f'Key Alpha {n}' for n in range(1, 9)))
        second = self.attach('\n'.join(f'Key Beta {n}' for n in range(1, 8)), name='beta.txt')
        short = self.attach('Key Short', name='short.txt')
        other = self.store.new_session()
        foreign = self.attach('Key Foreign', sid=other, name='foreign.txt')
        request = {'query': 'Key', 'start_line': 1}
        sources = [
            {'document_id': second, 'queries': [request, {'query': 'Absent', 'start_line': 1}]},
            {'document_id': foreign, 'queries': [request]},
            {'document_id': short, 'queries': [{**request, 'start_line': 2}]},
            {'document_id': first, 'queries': [request]},
        ]
        engine = ScriptedEngine(
            action('find_document_text', sources=sources),
            action('finish', report=f'未讀來源 [D{foreign}:L1]'),
            action('finish', report=f'乙與甲分別核對 [D{second}:L1] [D{first}:L1]'))
        result = a.run_goal(self.store, self.sid, self.goal(), engine)
        found, invalid, finished = result['steps']
        observation = found['observation']
        self.assertEqual([(s['document_id'], s['status']) for s in observation['sources']],
                         [(second, 'searched'), (foreign, 'document_not_attached'),
                          (short, 'line_out_of_bounds'), (first, 'searched')])
        self.assertNotIn('queries', observation['sources'][1])
        self.assertNotIn('queries', observation['sources'][2])
        self.assertEqual(observation['sources'][2]['line_count'], 1)
        self.assertEqual(observation['sources'][0]['queries'][1]['total_matching_lines'], 0)
        self.assertEqual(observation['sources'][3]['queries'][0]['total_matching_lines'], 8)
        self.assertEqual([(e['document_id'], e['start_line']) for e in observation['excerpts']],
                         [(did, n) for did in (second, first) for n in range(1, 5)])
        self.assertEqual([m['full_line_provided'] for m in observation['sources'][0]['queries'][0]['matches']],
                         [True, True, True, True, False, False])
        self.assertEqual(invalid['action']['tool'], 'invalid')
        self.assertTrue(all(c['range_valid'] for c in finished['observation']['citation_checks']))

    def test_literal_lookup_validates_cross_document_limits(self):
        queries = [{'query': f'Key {n}', 'start_line': 1} for n in range(5)]
        def source(did):
            return {'document_id': did, 'queries': queries}
        for sources in ([], [source(1), source(1)], [source(1), source(2), source(3)],
                        [source(n) for n in range(1, 6)], [source(True)],
                        [{'document_id': 1, 'query': 'Key', 'start_line': 1}]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                a.validate_action(json.loads(action('find_document_text', sources=sources)))

    def test_literal_document_lookup_validation_and_cancellation(self):
        did = self.attach('批號=01')
        args = {'document_id': did, 'query': '批號', 'start_line': 1}
        for patch in ({'document_id': True}, {'start_line': 0}, {'query': '\x00'}, {'query': '甲\n乙'},
                      {'query': ' '}, {'query': '查' * 121}, {'query': '\ud800'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                a.document_ranges.validate_lookup({**args, **patch})
        docs = self.store.documents(self.sid, include_content=True)
        with self.assertRaises(ValueError):
            a.document_ranges.lookup(docs, {**args, 'document_id': did + 1})
        with self.assertRaises(ValueError):
            a.document_ranges.lookup(docs, {**args, 'start_line': 2})
        stop = threading.Event()
        stop.set()
        with self.assertRaises(EngineError) as failed:
            a.document_ranges.lookup(docs, args, stop)
        self.assertEqual(failed.exception.code, 'cancelled')

    def test_batch_range_validation_rejects_duplicate_excess_and_non_integer_args(self):
        ranges = [{'document_id': did, 'start_line': 1, 'end_line': 1} for did in range(1, 9)]
        a.validate_action(json.loads(action('read_ranges', ranges=ranges)))
        invalid = [[], ranges + [ranges[0]], [ranges[0], ranges[0]],
                   [{'document_id': 1, 'start_line': True, 'end_line': 1}],
                   [{'document_id': 1, 'start_line': 1, 'end_line': '2'}]]
        for rows in invalid:
            with self.subTest(ranges=rows), self.assertRaises(ValueError):
                a.validate_action(json.loads(action('read_ranges', ranges=rows)))

    def test_source_change_before_or_during_model_call_stops_action(self):
        did = self.attach('old')
        gid = self.goal()
        def mutate():
            self.attach('new')
            return action('finish', report='不能保存過時成果')
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(mutate))
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['steps'], [])
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None)['steps'], [])
        self.assertIn('改變', result['notice'])

    def test_cancelled_generation_does_not_execute_action(self):
        cancel = threading.Event()
        def response():
            cancel.set()
            return action('finish', report='不應保存')
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(response), cancel=cancel)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['steps'], [])

    def test_failure_preserves_steps_then_recovery_continues(self):
        gid = self.goal()
        def fail():
            raise RuntimeError('PRIVATE_ENGINE_ERROR')
        with self.assertRaises(RuntimeError):
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('calculate', operation='sum', values=['1']), fail))
        saved = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(saved['status'], 'failed')
        self.assertNotIn('PRIVATE_ENGINE_ERROR', json.dumps(saved))
        a.update(self.store, gid, 'running')  # Previous process exited; no active lease.
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report='保留先前計算1。')))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(len(result['steps']), 2)

    def test_active_lease_blocks_duplicate_runner_without_state_change(self):
        gid = self.goal()
        with lease(self.store, gid):
            with self.assertRaises(LeaseBusy):
                a.run_goal(self.store, self.sid, gid, None)
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'pending')

    def test_step_budgets_survive_restart_and_invalid_actions_count(self):
        gid = self.goal()
        invalid = action('shell', command='DO_NOT_EXECUTE')
        for _ in range(3):
            result = a.run_goal(Store(self.store.path), self.sid, gid, ScriptedEngine(*([invalid] * 12)), max_steps=12)
        self.assertEqual(len(result['steps']), 32)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None)['steps'], result['steps'])
        self.assertTrue(all(s['action']['tool'] == 'invalid' for s in result['steps']))

    def test_exact_arithmetic_and_rejection(self):
        self.assertEqual(a.calculate({'operation': 'sum', 'values': ['0.1', '0.2']})['exact'], '3/10')
        self.assertTrue(a.calculate({'operation': 'quotient', 'values': ['1', '3']})['rounded'])
        self.assertEqual(a.calculate({'operation': 'product', 'values': ['-2', '0.5']})['exact'], '-1')
        self.assertEqual(a.calculate({'operation': 'difference', 'values': ['1', '3']})['exact'], '-2')
        for args in [{'operation': 'sum', 'values': ['__import__("os")']},
                     {'operation': 'quotient', 'values': ['1', '0']},
                     {'operation': 'sum', 'values': [1]}, {'operation': 'sum', 'values': ['1e20']}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                a.calculate(args)

    def test_recall_excludes_disabled_memory(self):
        self.store.remember('喜歡簡短清楚的回答', 'preference')
        old = self.store.remember('DISABLED_PRIVATE_MEMORY', 'preference')
        self.store.forget(old)
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(action('recall', query='回答偏好')), max_steps=1)
        self.assertNotIn('DISABLED_PRIVATE_MEMORY', json.dumps(result, ensure_ascii=False))
        self.assertTrue(result['steps'][0]['observation']['memories'])

    def test_backup_roundtrip_and_legacy_database_migration(self):
        gid = self.goal()
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='日期？')))
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        with restored.connect() as db:
            db.execute('DROP TABLE autonomous_conversation_history')
            db.execute('DROP TABLE autonomous_continuous_runs')
            db.execute('DROP TABLE autonomous_conversations')
            db.execute('DROP TABLE autonomous_csv_plans')
            db.execute('DROP TABLE autonomous_csv_sources')
            db.execute('DROP TABLE goal_reviews')
            db.execute('DROP TABLE autonomous_delivery_changes')
            db.execute('DROP TABLE autonomous_artifacts')
            db.execute('DROP TABLE autonomous_delivery')
            db.execute('DROP TABLE autonomous_handoffs')
            db.execute('DROP TABLE autonomous_reply_sources')
            db.execute('DROP TABLE autonomous_question_withdrawals')
            db.execute('DROP TABLE autonomous_guidance_sources')
            db.execute('DROP TABLE autonomous_decision_scopes')
            db.execute('DROP TABLE autonomous_quantity_scopes')
            db.execute('DROP TABLE autonomous_steps')
            db.execute('DROP TABLE autonomous_goals')
        self.assertNotIn('autonomous_goals', validate_database(restored.path))
        upgraded = Store(restored.path)
        self.assertEqual(a.goals(upgraded, self.sid), [])
        with upgraded.connect() as db:
            db.execute('DROP TABLE autonomous_steps')
        with self.assertRaisesRegex(ValueError, '不完整'):
            validate_database(upgraded.path)

    def test_cli_can_create_and_read_without_model(self):
        base = [sys.executable, '-m', 'assistant', '--data-dir', str(self.root), '--session', self.sid]
        result = subprocess.run([*base, '--new-goal', '比較文件'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        gid = json.loads(result.stdout)['id']
        result = subprocess.run([*base, '--goal-status', gid], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(json.loads(result.stdout)['objective'], '比較文件')
        result = subprocess.run([*base, '--run-goal', gid, '--goal-steps', '0'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_chat_status_is_bounded_and_does_not_replay_saved_tool_data(self):
        for _ in range(4):
            gid = self.goal('x' * 450)
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report='PRIVATE_OLD_REPORT')))
        context = goal_context(self.store, self.sid)
        self.assertTrue(context['limited'])
        self.assertEqual(len(context['goals']), 3)
        self.assertTrue(context['goals'][0]['objective_truncated'])
        self.assertNotIn('PRIVATE_OLD_REPORT', json.dumps(context))
        self.assertEqual(context['goals'][0]['recorded_status'], 'report_ready')
        self.assertEqual(goal_context(self.store, self.store.new_session())['goals'], [])


if __name__ == '__main__':
    unittest.main()
