import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant import autonomy as a, decision_table as d, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from scripts.evaluate_goal_replies import matches_followup, prepare, prepare_planning
from test_autonomy import ScriptedEngine, action


class ReferencePlanningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cases = json.loads((Path(__file__).parents[1] / 'evals/goal_reference_planning.json').read_text(encoding='utf-8'))

    def prepared(self, index, suffix=''):
        case = self.cases[index]
        store, sid, gid = prepare(self.root / f'{index}{suffix}.sqlite3', case)
        setup = prepare_planning(store, sid, gid, case)
        return case, store, sid, gid, setup

    def test_repair_links_each_failure_and_preserves_later_reply_routes(self):
        for index in range(3):
            with self.subTest(index=index):
                case, store, sid, gid, setup = self.prepared(index)
                before = a.get_goal(store, sid, gid)
                self.assertEqual(before['status'], 'paused')
                self.assertFalse(before['question'])
                self.assertEqual(len(before['reply_sources']), 1)
                attempts = case['reference_planning_setup']['attempts']
                rejected = before['steps'][4:]
                self.assertEqual(len(rejected), attempts)
                self.assertTrue(all(s['action']['tool']=='invalid' and s['observation']['error']==d._MISSING_REFERENCE_MARKER
                                    for s in rejected))
                remaining = case['reference_planning_setup']['remaining']
                fixed = a.run_goal(store,sid,gid,ScriptedEngine(action('ask',question=setup['question'],
                                   reference_remaining=remaining)),max_steps=1)
                ask = fixed['steps'][-1]
                self.assertEqual(ask['action']['args']['question'],setup['question'])
                self.assertTrue(matches_followup(fixed, {'reference_remaining': ['本次由誰領件？', remaining]}))
                self.assertFalse(matches_followup(fixed, {'reference_remaining': '本次由誰領件？'}))
                receipt = ask['observation']['reference_repair']
                self.assertEqual(receipt['base_step'],4)
                self.assertEqual(receipt['base_sha256'],d.digest(before['steps'][3]))
                self.assertEqual(receipt['failed_steps'],[{'sequence':s['sequence'],'sha256':d.digest(s)} for s in rejected])
                self.assertEqual(fixed['steps'][:-1],before['steps'])
                for entry in case['partial_followup']:
                    observed = goal_replies.snapshot(store,sid,gid)
                    if 'question' in entry:self.assertEqual(observed['goal']['question'],entry['question'])
                    if 'reply' in entry:
                        goal_replies.save(store,sid,gid,entry['reply'],expected_fingerprint=observed['fingerprint'])
                    else:
                        goal_guidance.save(store,sid,gid,entry['correction'],expected_fingerprint=observed['fingerprint'])
                if index==2:
                    current=a.get_goal(store,sid,gid)
                    citation=current['reply_sources'][-1]['citation']
                    a.run_goal(store,sid,gid,ScriptedEngine(action('check_decision',field_reference=citation,resolve=True)),max_steps=1)
                current=a.get_goal(store,sid,gid)
                plan,_,_=d._recheck_saved(current['steps'],current['reply_sources'],current.get('guidance_sources',()))
                if index==0:
                    self.assertEqual(current['withdrawn_questions'][0]['ask_sequence'],ask['sequence']+2)
                    self.assertEqual(len(current['reply_sources']),2)
                    self.assertEqual(plan[1]['known']['入口'],'南口')
                elif index==1:
                    self.assertEqual(plan[1]['known'],{'接樣班別':'早班','驗收班別':'晚班'})
                    self.assertEqual(current['steps'][-1]['observation']['field_reference']['combined_reply']['remaining_reply']['value'],'陳值班員')
                else:
                    self.assertEqual(plan[1]['known'],{'入冊班別':'晚班','複點班別':'早班'})
                    self.assertEqual(current['steps'][-1]['observation']['field_reference']['remaining_reply']['quote'],'6 份')
                archive=create_backup(store.path,self.root/f'repaired-{index}.paibackup')
                target=self.root/f'repaired-{index}'
                restore_backup(archive['path'],target)
                restored=Store(target/'assistant.sqlite3')
                self.assertEqual(a.get_goal(restored,sid,gid),current)
                corrupted=ask['observation']
                corrupted['reference_repair']['base_sha256']='0'*64
                with restored.connect() as db:
                    db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                               (json.dumps(corrupted,ensure_ascii=False),gid,ask['sequence']))
                with self.assertRaisesRegex(ValueError,'分項規劃修正'):
                    validate_database(restored.path)

    def test_changed_question_new_notes_and_unrelated_failure_require_new_check(self):
        for kind in ('question','notes','unrelated'):
            with self.subTest(kind=kind):
                case,store,sid,gid,setup=self.prepared(0,kind)
                question=setup['question']
                if kind=='question':
                    question=question.replace('本輪要交付幾份？','本次要通知誰？')
                elif kind=='notes':
                    current=goal_guidance.snapshot(store,sid,gid)
                    goal_guidance.save(store,sid,gid,'入口正式改為南口。',expected_fingerprint=current['fingerprint'])
                else:
                    a.run_goal(store,sid,gid,ScriptedEngine({'tool':'unknown','args':{},'note':'unrelated failure'}),max_steps=1)
                remaining=question.split('\n')[1]
                result=a.run_goal(store,sid,gid,ScriptedEngine(action('ask',question=question,reference_remaining=remaining)),max_steps=1)
                self.assertEqual(result['steps'][-1]['action']['tool'],'invalid')
                self.assertFalse(result['question'])
                self.assertNotIn('reference_repair',result['steps'][-1]['observation'])
                self.assertIn('核對',result['steps'][-1]['observation']['error'])

    def test_ordinary_and_historical_unmarked_questions_keep_their_routes(self):
        with patch.object(d,'planned_reference_ask',return_value=None):
            case,store,sid,gid,setup=self.prepared(0,'legacy')
        original=a.get_goal(store,sid,gid)
        self.assertEqual(original['status'],'needs_input')
        self.assertNotIn('reference_remaining',original['steps'][-1]['action']['args'])
        self.assertNotIn('reference_partial_reply',goal_replies.snapshot(store,sid,gid))
        self.assertFalse(matches_followup(original, {'reference_remaining': setup['question'].split('\n')[1]}))
        pending=setup['before_ask']['steps']
        for question in ('請完整說明欄名與通知安排？',setup['question'].rsplit('\n',1)[0]):
            with self.subTest(question=question):
                requested=a.validate_action(json.loads(action('ask',question=question)))
                result=a.execute(store,store.documents(sid),requested,pending,gid=gid,sid=sid)
                self.assertEqual(result['question'],question)
                self.assertNotIn('reference_repair',result)
        current=goal_replies.snapshot(store,sid,gid)
        goal_replies.save(store,sid,gid,'補問：4 份',expected_fingerprint=current['fingerprint'])
        after=a.get_goal(store,sid,gid)
        self.assertEqual(after['steps'],original['steps'])
        self.assertEqual(after['reply_sources'][-1]['answer'],'補問：4 份')
        archive=create_backup(store.path,self.root/'legacy.paibackup')
        restore_backup(archive['path'],self.root/'legacy-restored')
        self.assertEqual(a.get_goal(Store(self.root/'legacy-restored/assistant.sqlite3'),sid,gid),after)

    def test_repaired_compound_waiting_recheck_keeps_only_the_independent_question(self):
        case,store,sid,gid,setup=self.prepared(0,'waiting')
        remaining=case['reference_planning_setup']['remaining']
        fixed=a.run_goal(store,sid,gid,ScriptedEngine(action('ask',question=setup['question'],
                         reference_remaining=remaining)),max_steps=1)
        ask=fixed['steps'][-1]['sequence']
        current=goal_guidance.snapshot(store,sid,gid)
        goal_guidance.save(store,sid,gid,'入口正式改為南口。',expected_fingerprint=current['fingerprint'])
        pending=a.get_goal(store,sid,gid)
        self.assertEqual(pending['question'],remaining)
        self.assertEqual(pending['withdrawn_questions'][0]['ask_sequence'],ask)
        self.assertEqual(pending['steps'][-2]['observation']['possible_results'],['聯絡台'])
        self.assertEqual(len(pending['reply_sources']),1)
        current=goal_replies.snapshot(store,sid,gid)
        goal_replies.save(store,sid,gid,'6 份',expected_fingerprint=current['fingerprint'])
        answered=a.get_goal(store,sid,gid)
        self.assertEqual(answered['reply_sources'][-1]['question'],remaining)
        self.assertEqual(answered['reply_sources'][-1]['ask_sequence'],ask+2)
        archive=create_backup(store.path,self.root/'waiting.paibackup')
        restore_backup(archive['path'],self.root/'waiting-restored')
        self.assertEqual(a.get_goal(Store(self.root/'waiting-restored/assistant.sqlite3'),sid,gid),answered)
