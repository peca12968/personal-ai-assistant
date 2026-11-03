import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from assistant import evolution as ev
from assistant.engine import EngineError
import test_evolution as fixtures


class Clock:
    def __init__(self):self.value=0.0
    def now(self):return self.value
    def sleep(self,amount):self.value+=amount


class Child:
    def __init__(self,clock,duration):self.clock=clock;self.duration=duration;self.returncode=None;self.stopped=False;self.waited=False
    def poll(self):
        if self.returncode is None and self.clock.value>=self.duration:self.returncode=0
        return self.returncode
    def stop(self):
        if self.poll() is None:self.stopped=True;self.returncode=-15
    def wait(self,timeout):self.waited=True;return self.returncode


def simulated(run,root,timeout,duration,cancel=None):
    clock=Clock();child=Child(clock,duration)
    if cancel=='mid':
        class Cancel:
            def is_set(self):return clock.value>=.2
        cancel=Cancel()
    with patch.object(ev.subprocess,'Popen',return_value=child),patch.object(ev.time,'monotonic',side_effect=clock.now),patch.object(ev.time,'sleep',side_effect=clock.sleep),patch.object(ev.CodexEngine,'stop',side_effect=lambda p:p.stop()):
        try:run(['synthetic'],root,root/'probe.log',cancel,timeout=timeout)
        except Exception as exc:return child,exc
    return child,None


class EvolutionDeadlineTests(unittest.TestCase):
    def test_observed_187_second_suite_completes_but_ordinary_and_real_overruns_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);limits=ev.execution_limits()
            child,error=simulated(ev.run_checked,root,limits['full_test_seconds'],187.096)
            self.assertIsNone(error);self.assertFalse(child.stopped);self.assertTrue(child.waited)
            for timeout,duration in [(limits['check_seconds'],187.096),(limits['full_test_seconds'],limits['full_test_seconds']+1)]:
                child,error=simulated(ev.run_checked,root,timeout,duration)
                self.assertIsInstance(error,EngineError);self.assertEqual(error.code,'validation_timeout')
                self.assertIn(str(timeout),str(error));self.assertTrue(child.stopped and child.waited)
            child,error=simulated(ev.run_checked,root,limits['publication_seconds'],360)
            self.assertIsNone(error);self.assertFalse(child.stopped);self.assertTrue(child.waited)

    def test_invalid_limits_cannot_launch_a_child_or_disable_deadlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(ev.subprocess,'Popen') as popen:
                for value in (0,-1,True,False,None,'600',float('nan'),float('inf'),-float('inf'),900.1,10**1000):
                    with self.subTest(value=str(value)[:30]):
                        with self.assertRaises(ValueError):ev.run_checked(['synthetic'],root,root/'invalid.log',timeout=value)
                popen.assert_not_called();self.assertFalse((root/'invalid.log').exists())

    def test_cancellation_before_launch_and_mid_check_retains_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cancel=threading.Event();cancel.set()
            with patch.object(ev.subprocess,'Popen') as popen:
                with self.assertRaises(EngineError) as caught:ev.run_checked(['synthetic'],root,root/'cancel.log',cancel)
                self.assertEqual(caught.exception.code,'cancelled');popen.assert_not_called()
            child,error=simulated(ev.run_checked,root,600,1000,'mid')
            self.assertIsInstance(error,EngineError);self.assertEqual(error.code,'cancelled')
            self.assertTrue(child.stopped and child.waited)

    def test_real_child_is_reaped_after_short_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=subprocess.Popen;children=[]
            command=[sys.executable,'-c','import time; time.sleep(5)']
            def spawn(*args,**kwargs):
                child=original(*args,**kwargs)
                if args[0]==command:children.append(child)
                return child
            with patch.object(ev.subprocess,'Popen',side_effect=spawn):
                with self.assertRaises(EngineError) as caught:
                    ev.run_checked(command,root,root/'child.log',timeout=.15)
            self.assertEqual(caught.exception.code,'validation_timeout')
            self.assertEqual(len(children),1);self.assertIsNotNone(children[0].poll())

    def test_improvement_routes_limits_and_records_only_completed_checks(self):
        fixture=fixtures.EvolutionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        suite=fixture.install_acceptance_fixture('mixed');original=ev.run_checked;seen=[]
        def run(command,cwd,log,cancel=None,timeout=180):
            seen.append((list(command),timeout))
            if tuple(command[1:])==ev.FULL_TEST_CHECK:
                child,error=simulated(original,Path(cwd),timeout,187.096)
                if error:raise error
            elif 'publish.py' in str(command[1]):raise ValueError('publication boundary')
            else:original(command,cwd,log,cancel,timeout)
        checks=[ev.FULL_TEST_CHECK,('-m','py_compile',fixture.name)]
        with patch.object(ev,'CHECKS',checks),patch.object(ev,'run_checked',side_effect=run):
            with self.assertRaisesRegex(ValueError,'publication boundary'):
                ev.improve('synthetic deadline case',[fixture.name],root=fixture.root,
                           engine=fixture.engine('def conversation_style(question, history):\n    return {"length_hint":"detailed"}\n'),acceptance=suite)
        report=fixture.reports()[0];self.assertEqual(report['status'],'applied_unpublished')
        self.assertEqual(report['execution_limits'],ev.execution_limits())
        self.assertEqual([r['timeout_seconds'] for r in report['checks']],[600,180])
        self.assertEqual(report['acceptance']['status'],'verified_improvement')
        self.assertEqual([timeout for command,timeout in seen if 'observe_acceptance.py' in str(command[1])],[60,60])
        self.assertEqual(seen[-1][1],900)
        detached=ev.execution_limits();detached['check_seconds']=1;self.assertEqual(ev.execution_limits()['check_seconds'],180)

    def test_legacy_rollback_records_current_publication_limit_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run_id='20261103-000000-abcdef12';folder=root/'data/evolution'/run_id;folder.mkdir(parents=True)
            commit,base,reverted='1'*40,'2'*40,'3'*40
            report={'id':run_id,'status':'published','commit':commit,'base_commit':base,'files':{'assistant/context.py':'a'*64}}
            path=folder/'report.json';path.write_text(json.dumps(report),encoding='utf-8')
            def git(_, *args):
                if args==('rev-parse',commit+'^'):return base
                if args[0]=='diff-tree':return 'assistant/context.py'
                if args==('rev-parse','HEAD'):return reverted
                if args==('branch','--show-current'):return 'main'
                if args[0]=='ls-remote':return reverted+'\trefs/heads/main'
                raise AssertionError(args)
            with patch.object(ev,'clean_head',return_value=commit),patch.object(ev,'git',side_effect=git),patch.object(ev,'run_checked') as run:
                result=ev.rollback(run_id,root=root)
            self.assertEqual(result['status'],'published');self.assertEqual(result['publication_timeout_seconds'],900)
            self.assertEqual(run.call_args_list[-1].kwargs['timeout'],900)
            saved=json.loads(path.read_text('utf-8'));self.assertNotIn('execution_limits',saved)
            self.assertEqual({k:v for k,v in saved.items() if k!='rollback'},report)


if __name__=='__main__':unittest.main()
