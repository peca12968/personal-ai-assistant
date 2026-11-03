"""Median operates on retained observations, never on group averages or pages."""
import json
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from statistics import median
from unittest.mock import patch

from assistant import autonomy as a, json_statistics as stats
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts.evaluate_json_statistics import fixture
from test_autonomy import ScriptedEngine, action
from test_json_groups import args, document, run


class JsonMedianTests(unittest.TestCase):
    def test_filtered_ranked_pages_and_global_median_match_independent_reference(self):
        raw=fixture('median_groups')
        rows=json.loads(raw,parse_int=Fraction)['records']
        values={}
        for row in rows:
            if row['status']=='paid':values.setdefault(row['team'],[]).append(row['value'])
        ranked=sorted(values,key=lambda k:median(values[k]),reverse=True)[:25]
        all_values=[v for vs in values.values() for v in vs]
        pages=[];offset=0
        while offset is not None:
            page=stats.summarize([document(raw)],args(include_median=True,
                filters=[{'pointer':'/status','operator':'eq','type':'string','value':'paid'}],
                order_by={'metric':'median','direction':'desc'},top_n=25,offset=offset))
            pages.append(page);offset=page['next_offset']
        groups=[g for p in pages for g in p['groups']]
        self.assertEqual([p['returned_group_count'] for p in pages],[20,5])
        self.assertEqual([g['keys'][0]['value'] for g in groups],ranked)
        self.assertEqual([g['rank'] for g in groups],list(range(1,26)))
        for group,key in zip(groups,ranked):
            self.assertEqual(group['count'],len(values[key]))
            self.assertEqual(Fraction(group['median']['exact']),median(values[key]))
        for page in pages:
            self.assertEqual(Fraction(page['median']['exact']),median(all_values))
            self.assertEqual((page['source_count'],page['count'],page['excluded_count']),
                             (len(rows),len(all_values),90))
            self.assertEqual((page['group_count'],page['selected_group_count'],page['unselected_group_count']),(90,25,65))
            self.assertEqual(page['median_method'],'midpoint')
            self.assertTrue(page['median_included']);self.assertEqual(page['excerpts'],[])
            self.assertFalse(page['raw_values_provided'])

    def test_odd_even_empty_repeated_negative_and_exact_order(self):
        for values in ([],[2],[-3,1],[-2,-2,7],[-4,0,1,99],[5,5,5,5]):
            with self.subTest(values=values):
                result=stats.summarize([document(json.dumps(values))],
                    {'document_id':1,'pointer':'','value_pointer':'','include_median':True})
                if values:self.assertEqual(Fraction(result['median']['exact']),median(list(map(Fraction,values))))
                else:self.assertIsNone(result['median'])
        for direction,expected in [('desc',['B','A','C']),('asc',['A','C','B'])]:
            result=stats.summarize([document(fixture('median_precision'))],args(include_median=True,
                order_by={'metric':'median','direction':direction}))
            self.assertEqual([g['keys'][0]['value'] for g in result['groups']],expected)
            self.assertEqual(result['median']['exact'],str(10**50+1))
            a_group=next(g for g in result['groups'] if g['keys'][0]['value']=='A')
            self.assertEqual(Fraction(a_group['median']['exact']),10**50+Fraction(1,2))
            self.assertTrue(a_group['median']['rounded'])

    def test_opt_in_is_strict_and_default_schema_is_unchanged(self):
        rows=[{'team':'A','value':1}]
        self.assertEqual(run(rows),run(rows,include_median=False))
        self.assertNotIn('median',run(rows))
        self.assertNotIn('median',run(rows)['groups'][0])
        for option in (None,0,1,'true',[],{}):
            with self.subTest(option=option),self.assertRaises(ValueError):
                stats.validate(args(include_median=option))
        for extra in ({},{'include_median':False}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                stats.validate(args(order_by={'metric':'median','direction':'asc'},**extra))
        result=run(rows,include_median=True,
            filters=[{'pointer':'/value','operator':'gt','type':'number','value':'2'}],
            order_by={'metric':'median','direction':'asc'},top_n=1)
        self.assertIsNone(result['median']);self.assertEqual(result['groups'],[])

    def test_missing_values_and_unselected_median_overflow_fail_whole_operation(self):
        with self.assertRaisesRegex(ValueError,'索引1'):
            stats.summarize([document(fixture('median_missing'))],args(include_median=True))
        # Both groups and the global sum/mean are zero. Group B's midpoint has
        # denominator 10**2000 (2001 digits), despite valid source numbers.
        tail='9'*1999+'8'
        raw='{"records":[{"team":"A","value":0},'+','.join(
            '{"team":"B","value":'+v+'}' for v in ('-1','0','2e-2000',tail+'e-2000'))+']}'
        ordinary=args(order_by={'metric':'sum','direction':'desc'},top_n=1)
        self.assertEqual(stats.summarize([document(raw)],ordinary)['groups'][0]['keys'][0]['value'],'A')
        with self.assertRaisesRegex(ValueError,'2000'):
            stats.summarize([document(raw)],{**ordinary,'include_median':True})

    def test_group_population_differs_from_median_of_group_medians(self):
        rows=[{'team':'A','value':x} for x in [1,2,3,4,5]]+[{'team':'B','value':100}]
        result=run(rows,include_median=True,order_by={'metric':'median','direction':'desc'},top_n=1)
        self.assertEqual(result['median']['exact'],'7/2')
        self.assertEqual(result['groups'][0]['median']['exact'],'100')
        self.assertEqual(result['count'],6)
        self.assertFalse(result['all_groups_in_page']);self.assertTrue(result['all_selected_groups_in_page'])

    def test_cancel_during_median_finalization_returns_no_partial_result(self):
        stop=threading.Event();original=stats.Accumulator.result
        def checked(instance):
            stop.set()
            return original(instance)
        with patch.object(stats.Accumulator,'result',checked),self.assertRaises(EngineError):
            stats.summarize([document('[1,2]')],
                {'document_id':1,'pointer':'','value_pointer':'','include_median':True},stop)

    def test_goal_snapshot_backup_stage_and_citation_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'data.json';path.write_text(fixture('median_precision'),encoding='utf-8')
            store.attach_document(sid,path);path.write_text('{}',encoding='utf-8')
            gid=a.create_goal(store,sid,'按中位數排名',file_delivery=True)
            result=a.run_goal(store,sid,gid,ScriptedEngine(
                action('summarize_json',**args(include_median=True,order_by={'metric':'median','direction':'desc'},top_n=2)),
                action('write_artifact',filename='rank.txt',content='排名 [D1:L1]'),
                action('ask',question='測試暫停')),max_steps=3)
            observed=result['steps'][0]['observation']
            self.assertEqual(observed['median']['exact'],str(10**50+1))
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual(path.read_text(encoding='utf-8'),'{}')
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'])
            for key in ('include_median','median_method','median_included'):self.assertIn(key,stage)
            self.assertNotIn(str(10**50+1),stage)
            archive=create_backup(store.path,folder/'backup.paibackup')
            restore_backup(archive['path'],folder/'restored')
            restored=Store(folder/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'][0]['observation'],observed)


if __name__=='__main__':unittest.main()
