import copy
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from assistant import autonomy as a, month_schedule as m
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def args(anchor='2027-01-31',offsets=None,policy='clamp'):
    return {'date':anchor,'offsets':[1,2,3] if offsets is None else offsets,'day_policy':policy}


class MonthScheduleTests(unittest.TestCase):
    def test_twelve_months_keep_original_anchor_and_all_weekdays(self):
        r=m.calculate(args(offsets=list(range(1,13))))
        expected=['2027-02-28','2027-03-31','2027-04-30','2027-05-31','2027-06-30','2027-07-31','2027-08-31','2027-09-30','2027-10-31','2027-11-30','2027-12-31','2028-01-31']
        self.assertEqual([x['date'] for x in r['items']],expected)
        self.assertEqual(r['count'],12);self.assertTrue(r['all_items_provided']);self.assertEqual(r['anchor_date'],'2027-01-31')
        for i,(row,text) in enumerate(zip(r['items'],expected)):
            value=date.fromisoformat(text)
            self.assertEqual(row['index'],i);self.assertEqual(row['months'],i+1)
            self.assertEqual(row['weekday_iso'],value.isoweekday())
            self.assertEqual(row['calendar_days'],(value-date(2027,1,31)).days)
            self.assertEqual(row['day_adjusted'],value.day!=31)
        self.assertNotIn('excerpts',r)

    def test_order_duplicates_negative_zero_and_leap_centuries(self):
        r=m.calculate(args('2024-02-29',[12,-12,0,1,12]))
        self.assertEqual([x['date'] for x in r['items']],['2025-02-28','2023-02-28','2024-02-29','2024-03-29','2025-02-28'])
        self.assertEqual(r['offsets'],[12,-12,0,1,12])
        r=m.calculate(args('2027-02-14',[-1,0,1],'month_end'))
        self.assertEqual([x['date'] for x in r['items']],['2027-01-31','2027-02-28','2027-03-31'])
        self.assertTrue(all(x['day_adjusted'] for x in r['items']))
        for year,last in [(1900,28),(2000,29)]:
            r=m.calculate(args(f'{year}-01-31',[1]))
            self.assertEqual(r['items'][0]['date'],f'{year}-02-{last}')

    def test_strict_shape_offsets_and_policy(self):
        invalid=[{},[],{**args(),'calendar':'weekdays'},args(offsets=[]),args(offsets=[0]*25),args(offsets=(0,1)),args(offsets=[True]),args(offsets=[1.0]),args(offsets=['1']),args(offsets=[121]),args(offsets=[-121]),args(policy='auto'),args(policy=[]),args(anchor='2027-02-29'),args(anchor='2027-1-31')]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(ValueError):m.calculate(item)
        r=m.calculate(args(offsets=[-120,120]+[0]*22));self.assertEqual(r['count'],24)

    def test_no_partial_results_first_error_index_and_complete_prevalidation(self):
        data=args(offsets=[0,1,2],policy='same_day');original=copy.deepcopy(data)
        with self.assertRaisesRegex(ValueError,'第2筆.*1月') as caught:m.calculate(data)
        self.assertIn('同日不存在',str(caught.exception));self.assertEqual(data,original)
        with patch.object(m.date_tools,'calculate') as calculate:
            with self.assertRaises(ValueError):m.calculate(args(offsets=[0,True]))
            calculate.assert_not_called()
        self.assertEqual(m.calculate(args(offsets=[0,2],policy='same_day'))['count'],2)

    def test_date_bounds_are_rejected_without_rollover(self):
        for data in (args('9999-12-31',[0,1]),args('0001-01-31',[-1]),args('9999-01-31',[12])):
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError,'超出'):m.calculate(data)
        self.assertEqual(m.calculate(args('0001-01-31',[0]))['items'][0]['date'],'0001-01-31')
        self.assertEqual(m.calculate(args('9999-12-31',[0]))['items'][0]['date'],'9999-12-31')

    def test_cancellation_before_and_mid_batch_and_detached_output(self):
        cancel=threading.Event();cancel.set();data=args();original=copy.deepcopy(data)
        with self.assertRaises(EngineError):m.calculate(data,cancel)
        cancel.clear();calculate=m.date_tools.calculate
        def stopping(value):
            result=calculate(value);cancel.set();return result
        with patch.object(m.date_tools,'calculate',side_effect=stopping) as observed:
            with self.assertRaises(EngineError):m.calculate(data,cancel)
            self.assertEqual(observed.call_count,1)
        self.assertEqual(data,original)
        r=m.calculate(data);r['offsets'][0]=99;r['items'][0]['date']='fake';self.assertEqual(data,original)

    def test_dispatch_stage_omission_backup_and_resume_no_citation_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite3');sid=store.new_session();gid=a.create_goal(store,sid,'核對月排程')
            first=a.run_goal(store,sid,gid,ScriptedEngine(action('month_schedule',**args())),max_steps=1)
            self.assertEqual(first['steps'][0]['observation']['count'],3)
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'])
            self.assertIn('schedule_details_omitted',stage);self.assertIn('2027-01-31',stage);self.assertNotIn('2027-02-28',stage)
            backup=create_backup(store.path,root/'copy.paibackup');restore_backup(backup['path'],root/'restored')
            restored=Store(root/'restored'/'assistant.sqlite3');self.assertEqual(a.get_goal(restored,sid,gid)['steps'],first['steps'])
            result=a.run_goal(restored,sid,gid,ScriptedEngine(action('finish',report='[D1:L1]'),action('finish',report='核對完成，僅試算。')),max_steps=2)
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid');self.assertEqual(result['status'],'report_ready')
            self.assertEqual(result['steps'][0],first['steps'][0])

    def test_failed_batch_preserves_original_attempt_without_success_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session();gid=a.create_goal(store,sid,'同日不存在先問')
            data=args(offsets=[0,1,2],policy='same_day')
            r=a.run_goal(store,sid,gid,ScriptedEngine(action('month_schedule',**data),action('ask',question='二月沒有31日，要如何處理？')),max_steps=2)
            failed=r['steps'][0];self.assertEqual(failed['action']['tool'],'invalid')
            self.assertEqual(failed['observation']['error_phase'],'execution');self.assertEqual(failed['observation']['failed_action']['args'],data)
            self.assertNotIn('items',failed['observation']);self.assertEqual(r['status'],'needs_input')


if __name__=='__main__':unittest.main()
