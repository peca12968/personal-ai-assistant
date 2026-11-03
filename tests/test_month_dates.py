import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy as a, date_tools as d
from assistant.backup import create_backup, restore_backup
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def months(day, n, policy='clamp'):
    return {'operation':'add_months','date':day,'months':n,'day_policy':policy}


class MonthDateTests(unittest.TestCase):
    def test_distinct_day_policies_and_zero_months(self):
        with self.assertRaisesRegex(ValueError,'同日不存在'):
            d.calculate(months('2027-01-31',1,'same_day'))
        for args,expected,adjusted in [
            (months('2027-01-31',1),'2027-02-28',True),
            (months('2027-01-15',1),'2027-02-15',False),
            (months('2027-01-15',1,'month_end'),'2027-02-28',True),
            (months('2027-01-15',0,'month_end'),'2027-01-31',True),
            (months('2027-01-15',0),'2027-01-15',False),
            (months('2027-01-31',0,'same_day'),'2027-01-31',False)]:
            with self.subTest(args=args):
                result=d.calculate(args)
                self.assertEqual(result['end']['date'],expected)
                self.assertEqual(result['day_adjusted'],adjusted)
                self.assertEqual(result['start']['date'],args['date'])
                self.assertEqual(result['day_policy'],args['day_policy'])
                self.assertEqual(result['months'],args['months'])

    def test_original_anchor_and_chained_clamping_are_different(self):
        anchored=[d.calculate(months('2027-01-31',n))['end']['date'] for n in (1,2,3)]
        self.assertEqual(anchored,['2027-02-28','2027-03-31','2027-04-30'])
        self.assertEqual(d.calculate(months(anchored[0],1))['end']['date'],'2027-03-28')
        # A clamped result loses the original day; reversing cannot recover it.
        self.assertEqual(d.calculate(months(anchored[0],-1))['end']['date'],'2027-01-28')
        self.assertEqual(d.calculate(months('2027-02-28',1,'month_end'))['end']['date'],'2027-03-31')

    def test_leap_century_reverse_cross_year_and_limits(self):
        cases=[('2028-03-31',-1,'2028-02-29',-31),('2027-03-31',-1,'2027-02-28',-31),
               ('2096-02-29',48,'2100-02-28',1460),('1999-12-31',2,'2000-02-29',60),
               ('1899-12-31',2,'1900-02-28',59),('2399-12-31',2,'2400-02-29',60),
               ('2027-01-31',-2,'2026-11-30',-62),('2027-01-31',120,'2037-01-31',3653),
               ('2027-01-31',-120,'2017-01-31',-3652)]
        for start,offset,end,days in cases:
            with self.subTest(start=start,offset=offset):
                result=d.calculate(months(start,offset))
                self.assertEqual(result['end']['date'],end)
                self.assertEqual(result['calendar_days'],days)
                self.assertEqual(result['end']['weekday'],d.calculate({'operation':'weekday','date':end})['weekday'])

    def test_strict_types_policy_no_implicit_holidays_and_date_overflow(self):
        base=months('2027-01-31',1)
        bad=[{**base,'months':n} for n in (True,False,1.0,'1',None,121,-121)]
        bad += [{**base,'day_policy':p} for p in ('auto','rollover','weekdays',None,[],{})]
        bad += [{**base,'excluded_dates':[]},{**base,'days':30},
                {k:v for k,v in base.items() if k!='day_policy'},
                months('0001-01-01',-1),months('9999-12-31',1),months('2027-02-29',1)]
        for args in bad:
            with self.subTest(args=args),self.assertRaises(ValueError): d.calculate(args)
        self.assertEqual(d.calculate(months('0001-01-01',0,'same_day'))['end']['date'],'0001-01-01')
        self.assertEqual(d.calculate(months('9999-12-01',0,'month_end'))['end']['date'],'9999-12-31')
        self.assertEqual(d.calculate(months('2027-01-31',1))['end']['weekday'],'星期日')

    def test_short_months_and_long_months_all_target_months(self):
        expected=['2030-01-31','2030-02-28','2030-03-31','2030-04-30','2030-05-31','2030-06-30',
                  '2030-07-31','2030-08-31','2030-09-30','2030-10-31','2030-11-30','2030-12-31']
        for offset,end in enumerate(expected):
            with self.subTest(offset=offset):
                self.assertEqual(d.calculate(months('2030-01-31',offset))['end']['date'],end)
                self.assertEqual(d.calculate(months('2030-01-09',offset,'month_end'))['end']['date'],end)
                result=d.calculate(months('2030-01-09',offset,'same_day'))
                self.assertEqual(result['end']['date'],end[:-2]+'09')
                self.assertFalse(result['day_adjusted'])
                self.assertEqual(result['target_month_days'],int(end[-2:]))

    def test_typed_loop_failure_history_and_checkpoint_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'data'/'assistant.sqlite3');sid=store.new_session()
            gid=a.create_goal(store,sid,'核對月末日期')
            requests=[months('2027-01-31',1,'same_day'),months('2027-01-31',1,'clamp')]
            result=a.run_goal(store,sid,gid,ScriptedEngine(*[action('date_math',**r) for r in requests]),max_steps=2)
            first,second=result['steps']
            self.assertEqual(first['action']['tool'],'invalid')
            self.assertEqual(first['observation']['failed_action']['args'],requests[0])
            self.assertEqual(second['observation']['end']['date'],'2027-02-28')
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            context=a.get_goal(store,sid,child)['previous_stage']['checkpoint']
            self.assertEqual(context['activity'][0]['failed_attempt']['request']['value'],requests[0])
            metadata=context['activity'][1]['observation_metadata']['value']
            self.assertEqual(metadata['months'],1)
            self.assertEqual(metadata['day_policy'],'clamp')
            self.assertTrue(metadata['day_adjusted'])
            restore_backup(create_backup(store.path)['path'],Path(tmp)/'restored')
            restored=Store(Path(tmp)/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],result['steps'])
            self.assertEqual(a.get_goal(restored,sid,child)['previous_stage']['checkpoint'],context)


if __name__=='__main__':unittest.main()
