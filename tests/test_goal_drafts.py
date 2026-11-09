import copy
import json
import io
import sys
from contextlib import redirect_stdout
from unittest.mock import patch
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, goal_drafts as d
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine


def proposal(key, quote):
    return {'items': [{'kind': 'goal', 'text': '比較兩方案六個月費用。',
                       'sources': [{'key': key, 'quote': quote}]}], 'questions': []}


class GoalDraftTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.store = Store(self.root/'state.sqlite3')
        self.sid = self.store.new_session()
        self.raw = '比較兩方案六個月費用，不需產檔。'
        self.mid = self.store.add_message(self.sid, 'user', self.raw)
        self.source = d.capture(self.store, self.sid)

    def result(self):
        return d.generate(self.source, ScriptedEngine(json.dumps(proposal('M'+str(self.mid), self.raw))))

    def test_generate_has_exact_user_evidence_and_no_persistent_side_effects(self):
        before = self.store.messages(self.sid)
        result = self.result()
        self.assertEqual(result['objective_text'], '目標：比較兩方案六個月費用。')
        self.assertEqual(self.store.messages(self.sid), before)
        self.assertEqual(self.store.memories(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0], 0)
        self.assertEqual(result['source'], self.source)

    def test_sources_include_earlier_correction_feedback_brief_and_request_but_not_other_or_local(self):
        correction = self.store.add_message(self.sid, 'user', '更正：改成六個月。')
        assistant = self.store.add_message(self.sid, 'assistant', '助理提案，不是決定')
        self.store.rate(assistant, -1, '只比總額')
        local = self.store.add_message(self.sid, 'user', '記住：PRIVATE_LOCAL')
        with self.store.connect() as db: db.execute('INSERT INTO local_memory_messages VALUES (?)', (local,))
        other = self.store.new_session(); self.store.add_message(other, 'user', 'PRIVATE_OTHER')
        for _ in range(20): self.store.add_message(self.sid, 'user', '背景討論')
        self.store.save_session_brief(self.sid, '比較費用', '保留來源', '')
        source = d.capture(self.store, self.sid, '這次只列差額')
        catalog = d.catalog(source)
        self.assertEqual(catalog['M'+str(correction)], '更正：改成六個月。')
        self.assertEqual(catalog['F'+str(assistant)], '只比總額')
        self.assertEqual(catalog['B_constraints_text'], '保留來源')
        self.assertEqual(catalog['R'], '這次只列差額')
        self.assertNotIn('M'+str(assistant), catalog)
        self.assertNotIn('PRIVATE_', json.dumps(source))
        self.assertTrue(source['context']['messages_limited'])

    def test_invalid_grounding_permissions_and_empty_intent_are_not_silently_accepted(self):
        valid = proposal('M'+str(self.mid), self.raw)
        bad = [dict(valid, file_delivery=True), {'items':[], 'questions':[]},
               proposal('M999', self.raw), proposal('M'+str(self.mid), '不存在的原話')]
        duplicate = copy.deepcopy(valid); duplicate['items'] *= 2; bad.append(duplicate)
        wrong = copy.deepcopy(valid); wrong['items'][0]['kind']='execute'; bad.append(wrong)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError): d.validate(value, self.source)
        uncertain = {'items': [], 'questions': ['這次希望完成什麼？']}
        self.assertEqual(d.compose(d.validate(uncertain, self.source)), '')
        empty_sid = self.store.new_session(); self.store.add_message(empty_sid, 'assistant', '我替你決定寄信')
        with self.assertRaises(ValueError): d.generate(d.capture(self.store, empty_sid), None)

    def test_named_draft_recovers_exact_older_sources_and_rechecks_them_on_save(self):
        request = '回到「北門盤點」。'
        opening = '話題：「北門盤點」。週三松石倉，交付三欄唯讀清單，不通知。'
        correction = '最後正式決定為週四杉林倉，若下雨才考慮橡木倉。'
        first = self.store.add_message(self.sid, 'user', opening)
        self.store.add_message(self.sid, 'assistant', '助理提案：對外通知。')
        last = self.store.add_message(self.sid, 'user', correction)
        local = self.store.add_message(self.sid, 'user', '記住：不在話題來源')
        with self.store.connect() as db:
            db.execute('INSERT INTO local_memory_messages VALUES (?)', (local,))
        self.store.add_message(self.sid, 'user', '話題：「北門演練」。週一另有工作。')
        for _ in range(17):
            self.store.add_message(self.sid, 'user', '近期無關訊息')
        self.store.save_session_brief(self.sid, '其他工作', '其他限制', '其他決定')

        source = d.capture(self.store, self.sid, request)
        self.assertEqual(d.catalog(source), {f'M{first}': opening, f'M{last}': correction, 'R': request})
        self.assertEqual(source['available_documents'], [])
        self.assertIn(correction, d.describe_source(source))
        long_request = request + '請整理成目標。' + '僅在本話題整理。' * 160
        self.assertEqual(d.capture(self.store, self.sid, long_request)['context']['named_topic'],
                         source['context']['named_topic'])

        answer = proposal(f'M{last}', correction)
        answer['items'][0]['text'] = '整理週四杉林倉盤點的三欄唯讀清單，不通知。'
        result = d.generate(source, ScriptedEngine(json.dumps(answer)))
        gid = a.create_goal(self.store, self.sid, result['objective_text'], goal_draft=result)
        goal = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(goal['status'], 'pending')
        self.assertIsNone(goal['conversation_context'])
        self.store.add_message(self.sid, 'user', '回到「北門盤點」。正式改到週五杉林倉。')
        with self.assertRaisesRegex(ValueError, '來源.*已變更'):
            a.create_goal(self.store, self.sid, result['objective_text'], goal_draft=result)

    def test_unresolved_named_scope_blocks_generation_and_keeps_generic_drafting_available(self):
        cases = [
            ('從未開立', [], 'not_found', '找不到'),
            ('重複話題', ['話題：「重複話題」。甲場。', '話題：「重複話題」。乙場。'], 'ambiguous', '多段'),
            ('過長話題', ['話題：「過長話題」。' + '甲' * 3600], 'source_limit', '超過'),
        ]
        other = self.store.new_session()
        self.store.add_message(other, 'user', '話題：「從未開立」。只在別的對話。')
        for title, rows, status, explanation in cases:
            with self.subTest(status=status):
                for row in rows:
                    self.store.add_message(self.sid, 'user', row)
                request = f'回到「{title}」。整理成目標。'
                source = d.capture(self.store, self.sid, request)
                self.assertEqual(source['context']['named_topic']['status'], status)
                self.assertEqual(d.catalog(source), {'R': request})
                with self.assertRaisesRegex(ValueError, explanation):
                    d.generate(source, None)
        self.assertEqual(self.result()['objective_text'], '目標：比較兩方案六個月費用。')

    def test_cancel_and_engine_failure_preserve_source_and_do_not_create_goal(self):
        stop = threading.Event(); stop.set()
        with self.assertRaises(EngineError): d.generate(self.source, None, stop)
        stop.clear()
        def reply():
            stop.set(); return json.dumps(proposal('M'+str(self.mid), self.raw))
        with self.assertRaises(EngineError): d.generate(self.source, ScriptedEngine(reply), stop)
        with self.assertRaises(ValueError): d.generate(self.source, ScriptedEngine('{}'))
        self.assertEqual(d.fingerprint(self.source), d.fingerprint(d.capture(self.store, self.sid)))

    def test_save_checks_source_atomically_and_keeps_permissions_explicit(self):
        result = self.result()
        gid = a.create_goal(self.store, self.sid, result['objective_text'], goal_draft=result)
        goal = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(goal['status'], 'pending')
        self.assertEqual(goal['steps'], [])
        self.assertFalse(goal['file_delivery'])
        self.assertIsNone(goal['csv_source'])
        self.assertIsNone(goal['conversation_context'])
        self.store.add_message(self.sid, 'user', '更正：改成三個月。')
        with self.assertRaises(ValueError): a.create_goal(self.store, self.sid, result['objective_text'], goal_draft=result)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0], 1)
        # A user-written replacement remains available through the ordinary API.
        fresh = a.create_goal(self.store, self.sid, '改成三個月。')
        self.assertEqual(a.get_goal(self.store,self.sid,fresh)['objective'], '改成三個月。')

    def test_source_time_is_not_a_change_but_feedback_brief_documents_and_session_are(self):
        original = self.result()
        altered = copy.deepcopy(original); altered['source']['context']['captured_at']='later'
        with self.store.connect() as db: d.ensure_current(db,self.sid,altered)
        for mutation in (
                lambda: self.store.save_session_brief(self.sid,'新目標','',''),
                lambda: self.store.rate(self.store.add_message(self.sid,'assistant','old'),-1,'新的更正'),
                self.attach_document):
            source=d.capture(self.store,self.sid)
            result=d.generate(source,ScriptedEngine(json.dumps(proposal('M'+str(self.mid),self.raw))))
            mutation()
            with self.store.connect() as db, self.assertRaises(ValueError): d.ensure_current(db,self.sid,result)
        with self.store.connect() as db, self.assertRaises(ValueError): d.ensure_current(db,self.store.new_session(),original)

    def attach_document(self):
        path=self.root/'input.txt';path.write_text('費用',encoding='utf-8');self.store.attach_document(self.sid,path)

    def test_cli_outputs_reviewable_draft_without_saving_or_starting(self):
        from assistant.__main__ import main
        # CLI uses the standard database name in its selected data directory.
        store=Store(self.root/'assistant.sqlite3');sid=store.new_session()
        mid=store.add_message(sid,'user',self.raw)
        output=io.StringIO()
        with patch.object(sys,'argv',['assistant','--data-dir',str(self.root),'--session',sid,'--draft-goal','依照本對話']), \
                patch('assistant.__main__.CodexEngine',return_value=ScriptedEngine(json.dumps(proposal('M'+str(mid),self.raw)))), redirect_stdout(output):
            main()
        result=json.loads(output.getvalue())
        self.assertIn('比較兩方案六個月',result['objective_text'])
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0],0)


if __name__ == '__main__': unittest.main()
