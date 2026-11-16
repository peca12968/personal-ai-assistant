import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from assistant import artifacts, autonomy, goal_guidance, goal_replies, goal_required_reads, goal_sources
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalRequiredSupplementTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'state' / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def attach(self, path, content, *, sid=None):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, file)

    def waiting(self, *, named=False):
        did = self.attach('register.txt', '工作桌：紫安桌。')
        directive = ('請完整讀取 register.txt 第 1 至 2 行。' if named
                     else f'請完整查讀 [D{did}:L1-L2]。')
        gid = autonomy.create_goal(self.store,self.sid,directive + '交付清單 note.md。',file_delivery=True)
        history = ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1),
            action('ask', question='register.txt 的匯入副本缺少第 2 行。請提供該行從第一字到最後一字的完整原文。',
                   partial_result=f'工作桌：紫安桌。[D{did}:L1]\n檢查動作：待查。'),
        )
        waiting = autonomy.run_goal(self.store,self.sid,gid,history,max_steps=2)
        self.assertEqual(waiting['status'],'needs_input')
        return did,gid,waiting

    def supplement(self,gid,old,new,*,missing=2,replacement=1,scope='目前問題'):
        view = goal_guidance.snapshot(self.store,self.sid,gid)
        content = f'補件 [D{old}:L{missing}] 改由 [D{new}:L{replacement}]\n請撤下{scope}。'
        return goal_guidance.save(self.store,self.sid,gid,content,expected_fingerprint=view['fingerprint'])

    def test_same_name_replacement_recovers_in_the_same_goal_and_restores_artifact(self):
        original,gid,pending = self.waiting(named=True)
        other = self.attach('unselected.txt','另案要求拆除樣本。')
        replacement = self.attach('supplement/register.txt','檢查動作：看封籤是否完整。')
        view = goal_guidance.snapshot(self.store,self.sid,gid)
        self.assertTrue(view['required_document_supplement'])
        self.assertFalse(view['sources_match'])
        self.supplement(gid,original,replacement)
        reopened = autonomy.get_goal(self.store,self.sid,gid,include_reply_positions=True)
        self.assertEqual(reopened['status'],'paused')
        self.assertEqual(reopened['steps'][:2], pending['steps'])
        self.assertEqual([row['document_id'] for row in reopened['sources']], [original,replacement])
        self.assertNotIn(other,[row['document_id'] for row in reopened['sources']])
        self.assertEqual(reopened['withdrawn_questions'][0]['question'],pending['question'])
        self.assertEqual(goal_required_reads.requirements(reopened),[(original,1,1),(replacement,1,1)])
        archive = create_backup(self.store.path,self.root/'before-reading.paibackup')
        restore_backup(archive['path'],self.root/'before-reading')
        copy = Store(self.root/'before-reading'/'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(copy,self.sid,gid),autonomy.get_goal(self.store,self.sid,gid))
        body = f'工作桌：紫安桌。[D{original}:L1]\n檢查動作：看封籤是否完整。[D{replacement}:L1] [更正#1]'
        engine = ScriptedEngine(
            action('write_artifact',filename='note.md',content='尚未讀取。'),
            action('write_artifact',filename='note.md',content=body),
            action('deliver',filenames=['note.md'],report='已交付。'),
        )
        finished = autonomy.run_goal(copy,self.sid,gid,engine,max_steps=3)
        self.assertEqual(finished['status'],'report_ready')
        self.assertEqual(finished['steps'][3]['action']['args']['ranges'],[
            {'document_id':replacement,'start_line':1,'end_line':1},
        ])
        self.assertEqual(finished['steps'][3]['observation']['required_read_recovery'],{'before_tool':'write_artifact'})
        done = create_backup(copy.path,self.root/'complete.paibackup')
        restore_backup(done['path'],self.root/'complete')
        restored = Store(self.root/'complete'/'assistant.sqlite3')
        artifacts.rebuild(restored,self.sid,gid)
        self.assertEqual((artifacts.directory(restored,gid)/'note.md').read_bytes(),body.encode('utf-8'))
        validate_database(restored.path)

    def test_partial_work_carries_another_missing_line_forward_with_separate_supplements(self):
        original = self.attach('notice.txt','工作桌：紫安桌。')
        gid = autonomy.create_goal(self.store,self.sid,f'完整查讀 [D{original}:L1-L3]。完成文字摘要。')
        before = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('read',document_id=original,start_line=1,end_line=1),
            action('ask',question=f'請補上 D{original} 第2行的原文。'),
        ),max_steps=2)
        self.assertEqual(before['status'],'needs_input')
        first = self.attach('fix-a.txt','第一步：核對封籤。')
        self.supplement(gid,original,first)
        waiting = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('ask',question=f'請補上 D{original} 第3行的原文。'),
            action('ask',question=f'請補上 D{original} 第3行的原文。',
                   partial_result=f'已知第一步核對封籤。[D{first}:L1] 工作桌紫安桌。[D{original}:L1]'),
        ),max_steps=2)
        self.assertEqual(waiting['status'],'needs_input')
        self.assertIn(f'[D{first}:L1]',waiting['partial_result'])
        last = self.attach('fix-b.txt','第二步：登記封籤碼。')
        self.supplement(gid,original,last,missing=3)
        done = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('finish',report='已完成。'),
            action('finish',report=f'先核對封籤再登記。[D{first}:L1][D{last}:L1]'),
        ),max_steps=2)
        self.assertEqual(done['status'],'report_ready')
        self.assertEqual(len(done['withdrawn_questions']),2)
        validate_database(self.store.path)

    def test_a_long_replacement_is_read_in_pages_before_delivery(self):
        original,gid,_ = self.waiting()
        replacement = self.attach('long.txt','甲' * 10002 + '檢查動作：確認封籤。')
        self.supplement(gid,original,replacement)
        body = f'工作桌：紫安桌。[D{original}:L1]\n檢查動作：確認封籤。[D{replacement}:L1]'
        engine = ScriptedEngine(
            *(action('write_artifact',filename='note.md',content='提早定稿。') for _ in range(3)),
            action('write_artifact',filename='note.md',content=body),
            action('deliver',filenames=['note.md'],report='已交付。'),
        )
        done = autonomy.run_goal(self.store,self.sid,gid,engine,max_steps=5)
        self.assertEqual(done['status'],'report_ready')
        pages = [step['action']['args'] for step in done['steps'][3:6]]
        self.assertEqual(pages,[{'document_id':replacement,'line_number':1,'start_char':start}
                                for start in (1,4001,8001)])
        self.assertEqual((artifacts.directory(self.store,gid)/'note.md').read_text(encoding='utf-8'), body)
        validate_database(self.store.path)

    def test_prior_user_answer_remains_available_after_the_missing_line_is_replaced(self):
        original = self.attach('notice.txt','窗口：紫安桌。')
        gid = autonomy.create_goal(self.store,self.sid,f'先完整查讀 [D{original}:L1-L2]。')
        autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('read',document_id=original,start_line=1,end_line=1),
            action('ask',question='本批實際簽收人是誰？'),
        ),max_steps=2)
        view = goal_replies.snapshot(self.store,self.sid,gid)
        goal_replies.save(self.store,self.sid,gid,'本批簽收人是溫寧。',expected_fingerprint=view['fingerprint'])
        before = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('ask',question=f'請提供 D{original} 第 2 行的原文。',
                   partial_result='簽收人：溫寧。[補充#2]'),
        ),max_steps=1)
        self.assertEqual(before['status'],'needs_input')
        replacement = self.attach('provided.txt','須核對封籤。')
        self.supplement(gid,original,replacement)
        after = autonomy.get_goal(self.store,self.sid,gid)
        self.assertEqual(after['reply_sources'],before['reply_sources'])
        self.assertEqual([row['ask_sequence'] for row in after['withdrawn_questions']],[3])
        final = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
            action('read',document_id=replacement,start_line=1,end_line=1),
            action('finish',report=f'溫寧簽收 [補充#2]，核對封籤。[D{replacement}:L1]'),
        ),max_steps=2)
        self.assertEqual(final['status'],'report_ready')
        validate_database(self.store.path)

    def test_mismatched_missing_or_replacement_range_leaves_question_intact(self):
        original,gid,pending = self.waiting()
        replacement = self.attach('supplement.txt','檢查封籤。')
        another_sid = self.store.new_session()
        foreign = self.attach('foreign.txt','另案知會。',sid=another_sid)
        for args in ({'missing':1}, {'replacement':2}, {'scope':'分問「另一個問題？」'}):
            with self.subTest(args=args),self.assertRaises(ValueError):
                self.supplement(gid,original,replacement,**args)
        with self.assertRaises(ValueError):
            self.supplement(gid,original,foreign)
        refreshed = autonomy.get_goal(self.store,self.sid,gid)
        for key in ('sources','steps','status','question','notes'):
            self.assertEqual(refreshed[key],pending[key])
        validate_database(self.store.path)

    def test_readable_and_compound_questions_do_not_open_the_supplement_path(self):
        for trailing, question in (
            ('\n原公告已完整。','請補 register.txt 第 2 行的原文。'),
            ('','請提供 register.txt 第 2 行的原文。誰是簽收人？'),
        ):
            with self.subTest(trailing=trailing,question=question):
                if trailing:
                    original = self.attach('register.txt','工作桌：紫安桌。'+trailing)
                    gid = autonomy.create_goal(self.store,self.sid,f'先完整讀取 [D{original}:L2]。')
                    setup = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
                        action('read',document_id=original,start_line=1,end_line=2),
                        action('ask', question='請提供 register.txt 第 2 行的原文。'),
                    ),max_steps=2)
                    self.assertEqual(setup['status'],'needs_input')
                else:
                    # Use a distinct path and the actual stored document reference.
                    original = self.attach('branch/notice.txt','窗口：紫安桌。')
                    gid = autonomy.create_goal(self.store,self.sid,f'完整查讀 [D{original}:L2]。')
                    autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(
                        action('ask',question=f'請提供 D{original} 第2行的原文。誰是簽收人？'),
                    ),max_steps=1)
                candidate = self.attach(f'fix-{gid}.txt','補件候選。')
                self.assertFalse(goal_guidance.snapshot(self.store,self.sid,gid).get('required_document_supplement'))
                with self.assertRaises(ValueError):
                    self.supplement(gid,original,candidate)

    def test_restore_rechecks_original_ask_and_selected_copy(self):
        original,gid,_ = self.waiting()
        replacement = self.attach('provided.txt','修訂資料。')
        self.supplement(gid,original,replacement)
        actual = autonomy.get_goal(self.store,self.sid,gid)
        action_step = actual['steps'][-1]
        self.assertEqual(action_step['action']['tool'],goal_sources.REQUIRED_SUPPLEMENT)
        obs = action_step['observation']
        sequence = action_step['sequence']
        for changed in ({'ask_sha256':'0'*64},{'replacement_line_count':7}):
            with self.subTest(changed=changed):
                with self.store.connect() as db:
                    db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                               (json.dumps({**obs,**changed},ensure_ascii=False),gid,sequence))
                with self.assertRaisesRegex(ValueError,'原文補件'):
                    validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(obs,ensure_ascii=False),gid,sequence))
        validate_database(self.store.path)
        self.store.detach_document(self.sid,replacement)
        validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps({**obs,'replacement_line_count':8},ensure_ascii=False),gid,sequence))
        with self.assertRaisesRegex(ValueError,'原文補件'):
            validate_database(self.store.path)

    def test_question_accepts_matching_filename_and_id_with_a_field_description(self):
        original = self.attach('roller-guide.txt','本輪工作桌。')
        gid = autonomy.create_goal(self.store,self.sid,f'先完整查讀 [D{original}:L3]。')
        question = f'請提供 roller-guide.txt（D{original}）第 3 行的正式檢修處置原文。'
        waiting = autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(action('ask',question=question)),max_steps=1)
        self.assertEqual(waiting['status'],'needs_input')
        replacement = self.attach('fix.txt','正式檢修處置：換備用輪。')
        self.assertTrue(goal_guidance.snapshot(self.store,self.sid,gid)['required_document_supplement'])
        self.supplement(gid,original,replacement,missing=3)
        self.assertEqual(autonomy.get_goal(self.store,self.sid,gid)['status'],'paused')

        for malformed in (
            question.replace(f'（D{original}）',f'（D{replacement}）'),
            question.replace('的正式檢修處置','的正式檢修處置以及誰負責的'),
        ):
            with self.subTest(malformed=malformed):
                gid = autonomy.create_goal(self.store,self.sid,f'先完整查讀 [D{original}:L3]。')
                autonomy.run_goal(self.store,self.sid,gid,ScriptedEngine(action('ask',question=malformed)),max_steps=1)
                self.attach(f'{gid}.txt','補件候選。')
                self.assertFalse(goal_guidance.snapshot(self.store,self.sid,gid).get('required_document_supplement'))


if __name__ == '__main__':
    unittest.main()
