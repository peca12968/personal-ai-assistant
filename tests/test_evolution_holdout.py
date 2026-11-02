import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import evolution as ev
from assistant.evolution_status import improvement_context


class HoldoutTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.name='assistant/communication.py'
        for name in ('assistant/acceptance.py','scripts/observe_acceptance.py'):
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ev.ROOT/name).read_bytes())
        (self.root/'assistant/__init__.py').write_text('',encoding='utf-8')
        (self.root/'.gitignore').write_text('data/\n__pycache__/\n',encoding='utf-8')
        self.original='def conversation_style(question, history):\n    return {"length_hint": "concise" if question.startswith("KEEP") else "mixed"}\n'
        (self.root/self.name).write_text(self.original,encoding='utf-8')
        for args in [('init','-q'),('config','user.email','test@example.invalid'),('config','user.name','test'),('add','.'),('commit','-qm','baseline')]:self.git(*args)
        self.base=ev.clean_head(self.root);self.prompts=[]
        self.provided={'version':1,'cases':[{'id':'fix','kind':'communication','question':'FIX public example','history':[],'expect':{'length_hint':'detailed'}}]}
        self.held={'version':1,'cases':[{'id':'secret_case','kind':'communication','question':'KEEP_PRIVATE_SENTINEL_9','history':[],'expect':{'length_hint':'concise'}}]}
        self.good='def conversation_style(question, history):\n    return {"length_hint": "concise" if question.startswith("KEEP") else "detailed"}\n'
        self.overfit='def conversation_style(question, history):\n    return {"length_hint": "detailed"}\n'
        self.addCleanup(patch.stopall)
        patch.object(ev,'CHECKS',[('-m','py_compile',self.name)]).start()

    def git(self,*args):
        subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True)

    def engine(self,content=None,mutation=None):
        outer=self
        class Engine:
            def answer(self,prompt,cancel=None):
                outer.prompts.append(prompt)
                if mutation:mutation()
                return json.dumps({'files':[{'path':outer.name,'content':content or outer.good}]})
        return Engine()

    def run_case(self,engine=None,**kwargs):
        return ev.improve('Fix the public example',[self.name],root=self.root,engine=engine or self.engine(),
                          acceptance=self.provided,holdout=self.held,**kwargs)

    def report(self):
        return json.loads(next((self.root/'data/evolution').glob('*/report.json')).read_text(encoding='utf-8'))

    def test_public_pass_hidden_failure_blocks_apply_and_never_discloses_cases_in_prompt(self):
        with self.assertRaisesRegex(ValueError,'保留驗收未通過'):self.run_case(self.engine(self.overfit))
        report=self.report()
        self.assertEqual(report['status'],'not_applied')
        self.assertEqual(report['acceptance']['status'],'verified_improvement')
        self.assertEqual(report['holdout']['status'],'failed')
        self.assertEqual(report['holdout']['regressed_ids'],['secret_case'])
        self.assertEqual(ev.clean_head(self.root),self.base)
        self.assertEqual((self.root/self.name).read_text(encoding='utf-8'),self.original)
        self.assertNotIn('KEEP_PRIVATE_SENTINEL_9',self.prompts[0]);self.assertNotIn('secret_case',self.prompts[0])
        self.assertFalse((Path(report['directory'])/'candidate/holdout.json').exists())
        self.assertTrue((Path(report['directory'])/'candidate-holdout-observations.json').exists())

    def test_both_suites_pass_real_fresh_observers_and_local_publish_records_counts_only_for_chat(self):
        remote=self.root/'data/remote.git';remote.parent.mkdir()
        subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        self.git('remote','add','origin',str(remote))
        original=ev.run_checked
        def run(command,cwd,log,cancel=None,timeout=180):
            if str(command[1]).endswith('publish.py'):
                self.git('add',self.name);self.git('commit','-qm','local verified candidate');self.git('push','-q','origin','HEAD')
            else:original(command,cwd,log,cancel,timeout)
        with patch.object(ev,'run_checked',side_effect=run):report=self.run_case()
        self.assertEqual(report['status'],'published')
        self.assertEqual(report['holdout']['status'],'passed')
        self.assertEqual((report['holdout']['baseline_passed'],report['holdout']['candidate_passed']),(1,1))
        compact=improvement_context(self.root,'自我改進')[0]
        self.assertTrue(compact['revision_verified'])
        self.assertFalse(compact['holdout_recorded']['included_in_proposal_prompt'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(compact))
        self.assertNotIn('secret_case',json.dumps(compact))

    def test_original_hidden_failure_is_recorded_even_when_public_cases_need_no_change(self):
        self.provided['cases'][0]['expect']['length_hint']='mixed'
        self.held['cases'][0]['expect']['length_hint']='detailed'
        report=self.run_case()
        self.assertEqual(report['status'],'needs_reproduction')
        self.assertEqual(report['holdout']['status'],'baseline_failed')
        self.assertFalse(self.prompts)
        self.assertEqual(improvement_context(self.root,'自我改進')[0]['recorded_status'],'needs_reproduction')
        self.assertEqual(ev.clean_head(self.root),self.base)

    def test_all_original_cases_pass_without_generating_or_claiming_new_candidate(self):
        self.provided['cases'][0]['expect']['length_hint']='mixed'
        report=self.run_case()
        self.assertEqual(report['status'],'no_change_needed')
        self.assertEqual(report['holdout']['status'],'baseline_passed')
        self.assertNotIn('candidate_passed',report['holdout']);self.assertFalse(self.prompts)

    def test_requires_provided_cases_and_rejects_same_input_even_with_different_id_or_expectation(self):
        with self.assertRaisesRegex(ValueError,'需搭配'):
            ev.improve('goal',[self.name],root=self.root,engine=self.engine(),holdout=self.held)
        duplicate=json.loads(json.dumps(self.provided));duplicate['cases'][0]['id']='renamed'
        duplicate['cases'][0]['expect']['length_hint']='concise'
        with self.assertRaisesRegex(ValueError,'不可重複'):
            ev.improve('goal',[self.name],root=self.root,engine=self.engine(),acceptance=self.provided,holdout=duplicate)
        self.assertFalse(self.prompts);self.assertFalse((self.root/'data/evolution/active.lock').exists())

    def test_detaches_caller_input_and_checks_other_unselected_modules(self):
        (self.root/'assistant/context.py').write_text('def followup_query(question, history):\n    return None\n',encoding='utf-8')
        self.git('add','.');self.git('commit','-qm','protected context')
        self.held={'version':1,'cases':[{'id':'other_module','kind':'followup','question':'HIDDEN_TOPIC_RESET','history':[],'expect':None}]}
        original=ev.run_checked
        def run(command,*args,**kwargs):
            if str(command[1]).endswith('publish.py'):raise ValueError('local publication boundary')
            return original(command,*args,**kwargs)
        def mutate():self.held['cases'][0]['expect']='changed by caller'
        with patch.object(ev,'run_checked',side_effect=run),self.assertRaisesRegex(ValueError,'publication boundary'):
            self.run_case(self.engine(mutation=mutate))
        self.assertEqual(self.report()['holdout']['status'],'passed')
        self.assertNotIn('HIDDEN_TOPIC_RESET',self.prompts[0])

    def test_tampering_frozen_holdout_during_baseline_is_rejected(self):
        original=ev.run_checked
        def run(command,cwd,log,*args,**kwargs):
            original(command,cwd,log,*args,**kwargs)
            if '--suite' in command:
                (Path(cwd).parent/'holdout.json').write_text('{}',encoding='utf-8')
        with patch.object(ev,'run_checked',side_effect=run),self.assertRaisesRegex(ValueError,'驗收案例被改動'):
            self.run_case()
        self.assertFalse(self.prompts);self.assertEqual(ev.clean_head(self.root),self.base)

    def test_tampering_frozen_holdout_during_candidate_checks_is_rejected(self):
        command=('-c',"from pathlib import Path; Path('../holdout.json').write_text('{}',encoding='utf-8')")
        with patch.object(ev,'CHECKS',[command]),self.assertRaisesRegex(ValueError,'驗收案例被改動'):
            self.run_case()
        self.assertEqual(ev.clean_head(self.root),self.base)

    def test_hidden_original_failure_may_be_resolved_without_exposing_its_expected_answer(self):
        (self.root/self.name).write_text('def conversation_style(question, history):\n    return {"length_hint":"mixed"}\n',encoding='utf-8')
        self.git('add','.');self.git('commit','-qm','both suites originally fail')
        original=ev.run_checked
        def run(command,*args,**kwargs):
            if str(command[1]).endswith('publish.py'):raise ValueError('local publication boundary')
            return original(command,*args,**kwargs)
        with patch.object(ev,'run_checked',side_effect=run),self.assertRaisesRegex(ValueError,'publication boundary'):
            self.run_case()
        report=self.report()
        self.assertEqual(report['holdout']['resolved_ids'],['secret_case'])
        self.assertEqual((report['holdout']['baseline_passed'],report['holdout']['candidate_passed']),(0,1))
        self.assertNotIn('KEEP_PRIVATE_SENTINEL_9',self.prompts[0])

    def test_holdout_cancel_after_observation_never_applies(self):
        cancel=threading.Event();original=ev.run_checked
        def run(command,cwd,log,*args,**kwargs):
            original(command,cwd,log,*args,**kwargs)
            if 'candidate-holdout' in str(log):cancel.set()
        with patch.object(ev,'run_checked',side_effect=run),self.assertRaises(ev.EngineError):self.run_case(cancel=cancel)
        self.assertEqual(self.report()['status'],'not_applied');self.assertEqual(ev.clean_head(self.root),self.base)

    def test_malformed_holdout_observations_cannot_claim_pass(self):
        original=ev.run_checked
        def run(command,cwd,log,*args,**kwargs):
            original(command,cwd,log,*args,**kwargs)
            if 'candidate-holdout' in str(log):
                Path(command[command.index('--output')+1]).write_text('[{"id":"wrong","actual":true}]',encoding='utf-8')
        with patch.object(ev,'run_checked',side_effect=run),self.assertRaisesRegex(ValueError,'驗收觀察格式'):
            self.run_case()
        self.assertEqual(ev.clean_head(self.root),self.base)

    def test_cli_loads_separate_suites_and_requires_provided_path(self):
        from assistant.__main__ import main
        provided=self.root/'public.json';held=self.root/'private.json'
        provided.write_text(json.dumps(self.provided),encoding='utf-8');held.write_text(json.dumps(self.held),encoding='utf-8')
        argv=['assistant','--improve','test','--acceptance',str(provided),'--holdout',str(held)]
        with patch('sys.argv',argv),patch('assistant.evolution.improve',return_value={'status':'test_only'}) as run,contextlib.redirect_stdout(io.StringIO()):main()
        self.assertEqual(run.call_args.kwargs['acceptance'],self.provided)
        self.assertEqual(run.call_args.kwargs['holdout'],self.held)
        with patch('sys.argv',['assistant','--holdout',str(held)]),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main()


if __name__=='__main__':unittest.main()
