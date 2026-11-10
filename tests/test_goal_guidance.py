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
        with self.assertRaisesRegex(ValueError, '待確認的候選'):
            amend('配送班別正式改為晚班。')
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
