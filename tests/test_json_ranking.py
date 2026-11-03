"""Global exact ranking, explicit selection and pages are different populations."""
import json
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, json_statistics as stats
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts.evaluate_json_statistics import fixture
from test_autonomy import ScriptedEngine, action
from test_json_groups import args, document, run


def order(metric='sum', direction='desc'):
    return {'metric':metric, 'direction':direction}


class JsonRankingTests(unittest.TestCase):
    def test_global_top25_pages_match_independent_fraction_reference(self):
        raw=fixture('ranked_groups')
        rows=json.loads(raw,parse_float=Fraction)['records']
        values={}
        for row in rows:
            if row['status']=='paid':values.setdefault(row['team'],[]).append(Fraction(row['value']))
        ranked=sorted(values,key=lambda key:sum(values[key]),reverse=True)[:25]
        pages=[];offset=0
        while offset is not None:
            page=stats.summarize([document(raw)],args(order_by=order(),top_n=25,offset=offset,
                filters=[{'pointer':'/status','operator':'eq','type':'string','value':'paid'}]))
            pages.append(page);offset=page['next_offset']
        selected=[group for page in pages for group in page['groups']]
        self.assertEqual([g['keys'][0]['value'] for g in selected],ranked)
        self.assertEqual([g['rank'] for g in selected],list(range(1,26)))
        self.assertEqual([p['returned_group_count'] for p in pages],[20,5])
        total=sum(sum(v) for v in values.values())
        for p in pages:
            self.assertEqual((p['source_count'],p['count'],p['excluded_count']),(840,600,240))
            self.assertEqual((p['group_count'],p['selected_group_count'],p['unselected_group_count']),(240,25,215))
            self.assertEqual(Fraction(p['sum']['exact']),total)
            self.assertEqual(Fraction(p['mean']['exact']),total/600)
            self.assertTrue(p['complete']);self.assertTrue(p['page_is_partial'])
            self.assertFalse(p['all_groups_in_page']);self.assertEqual(p['excerpts'],[])
        for key,g in zip(ranked,selected):
            self.assertEqual(g['count'],len(values[key]))
            self.assertEqual(Fraction(g['sum']['exact']),sum(values[key]))
            self.assertEqual(Fraction(g['mean']['exact']),sum(values[key])/len(values[key]))

    def test_all_metrics_and_directions_use_exact_values_and_stable_ties(self):
        groups={'A':[3,1], 'B':[2], 'C':[-1,5,2], 'D':[4,0], 'E':[-2]}
        rows=[{'team':k,'value':v} for k,vs in groups.items() for v in vs]
        functions={'sum':sum,'mean':lambda vs:Fraction(sum(vs),len(vs)),
                   'count':len,'minimum':min,'maximum':max}
        for metric,fn in functions.items():
            for direction in ('asc','desc'):
                with self.subTest(metric=metric,direction=direction):
                    result=run(rows,order_by=order(metric,direction))
                    expected=sorted(groups,key=lambda k:fn(groups[k]),reverse=direction=='desc')
                    self.assertEqual([g['keys'][0]['value'] for g in result['groups']],expected)
                    self.assertEqual(result['order_by'],order(metric,direction))
                    self.assertEqual(result['tie_breaker'],'first_seen')
        result=stats.summarize([document(fixture('ranked_exact_means'))],args(order_by=order('mean'),top_n=2))
        self.assertEqual([g['keys'][0]['value'] for g in result['groups']],['C','A'])
        self.assertEqual([Fraction(g['mean']['exact']) for g in result['groups']],[10**50+2,10**50+1])
        self.assertEqual(Fraction(result['mean']['exact']),10**50+Fraction(3,5))
        typed=stats.summarize([document(fixture('typed_groups'))],args(group_by=['/key'],order_by=order(),top_n=3))
        self.assertEqual([(g['keys'][0]['type'],g['keys'][0]['value']) for g in typed['groups']],
                         [('number','0'),('string','True'),('null',None)])
        filtered=run([{'team':'B','value':999,'take':False},
                      {'team':'A','value':1,'take':True},{'team':'B','value':1,'take':True}],
                     filters=[{'pointer':'/take','operator':'eq','type':'boolean','value':True}],
                     order_by=order(),top_n=1)
        self.assertEqual(filtered['groups'][0]['keys'][0]['value'],'A')
        self.assertEqual(filtered['count'],2)

    def test_selection_empty_tail_and_legacy_population_flags(self):
        rows=[{'team':i,'value':i} for i in range(7)]
        page=run(rows,order_by=order(),top_n=2)
        self.assertFalse(page['page_is_partial']);self.assertTrue(page['all_selected_groups_in_page'])
        self.assertFalse(page['all_groups_in_page']);self.assertIsNone(page['next_offset'])
        self.assertEqual(page['omitted_after'],0);self.assertEqual(page['unselected_group_count'],5)
        tail=run(rows,order_by=order(),top_n=2,offset=10)
        self.assertEqual((tail['omitted_before'],tail['omitted_after']),(2,0))
        self.assertTrue(tail['page_is_partial']);self.assertFalse(tail['all_selected_groups_in_page'])
        self.assertEqual(tail['group_count'],7);self.assertIsNone(tail['next_offset'])
        empty=run([],order_by=order('mean'),top_n=5)
        self.assertEqual(empty['selected_group_count'],0);self.assertTrue(empty['all_groups_in_page'])
        self.assertIsNone(empty['mean']);self.assertFalse(empty['page_is_partial'])
        full=run(rows,order_by=order(),top_n=1000)
        self.assertEqual(full['selected_group_count'],7);self.assertEqual(full['unselected_group_count'],0)
        bounded=run([{'team':i,'value':i} for i in range(1000)],order_by=order(),top_n=1)
        self.assertEqual((bounded['group_count'],bounded['unselected_group_count']),(1000,999))
        self.assertEqual(bounded['groups'][0]['keys'][0]['value'],'999')
        legacy=run(rows)
        self.assertEqual(legacy['group_order'],'first_seen');self.assertNotIn('rank',legacy['groups'][0])
        self.assertNotIn('order_by',legacy);self.assertNotIn('selected_group_count',legacy)

    def test_invalid_arguments_are_rejected_without_coercion(self):
        bad_orders=[None,[],{},'sum',{'metric':'sum'},{'metric':'sum','direction':'DESC'},
                    {'metric':'median','direction':'desc'},{'metric':[],'direction':'asc'},
                    {'metric':'sum','direction':True},{'metric':'sum','direction':'desc','extra':1}]
        for value in bad_orders:
            with self.subTest(value=value),self.assertRaises(ValueError):stats.validate(args(order_by=value))
        for value in (None,True,0,-1,1001,1.0,'2'):
            with self.subTest(top_n=value),self.assertRaises(ValueError):stats.validate(args(order_by=order(),top_n=value))
        with self.assertRaises(ValueError):stats.validate(args(top_n=2))
        plain={'document_id':1,'pointer':'/records','value_pointer':'/value'}
        for extra in ({'order_by':order()},{'top_n':2},{'order_by':order(),'top_n':2}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):stats.validate({**plain,**extra})

    def test_top_n_does_not_hide_invalid_or_oversized_unselected_groups(self):
        for top in (1,1000):
            with self.subTest(top=top),self.assertRaisesRegex(ValueError,'索引1'):
                stats.summarize([document(fixture('ranked_missing'))],args(order_by=order(),top_n=top))
        rows=['{"team":"top","value":1}']
        rows+=['{"team":"tiny","value":1e-1999}']+['{"team":"tiny","value":0}']*10
        rows+=['{"team":"negative","value":-1e-1999}']
        with self.assertRaises(ValueError):
            stats.summarize([document('{"records":['+','.join(rows)+']}')],args(order_by=order(),top_n=1))
        with self.assertRaisesRegex(ValueError,'1000'):
            run([{'team':i,'value':i} for i in range(1001)],order_by=order(),top_n=1)

    def test_character_limited_pages_preserve_ranks_and_selection(self):
        rows=[{'team':str(i)+'x'*990,'category':'y'*1000,'value':i} for i in range(24)]
        groups=[];offset=0
        while offset is not None:
            page=run(rows,group_by=['/team','/category'],order_by=order(),top_n=22,offset=offset)
            self.assertLess(page['returned_group_count'],20)
            self.assertLessEqual(sum(len(json.dumps(g,ensure_ascii=False,separators=(',',':'))) for g in page['groups']),stats.GROUP_PAGE_CHARS)
            groups+=page['groups'];offset=page['next_offset']
        self.assertEqual([g['rank'] for g in groups],list(range(1,23)))
        self.assertEqual([g['sum']['exact'] for g in groups],[str(n) for n in range(23,1,-1)])

    def test_cancellation_after_finalization_returns_no_partial_result(self):
        stop=threading.Event();original=stats.Accumulator.result;calls=0
        def checked(instance):
            nonlocal calls
            result=original(instance);calls+=1
            if calls==3:stop.set()
            return result
        with patch.object(stats.Accumulator,'result',checked),self.assertRaises(EngineError):
            stats.summarize([document('{"records":[{"team":"A","value":1},{"team":"B","value":2}]}')],args(order_by=order(),top_n=1),stop)

    def test_real_goal_backup_and_stage_keep_metadata_without_ranked_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'data.json';path.write_text(fixture('ranked_exact_means'),encoding='utf-8')
            store.attach_document(sid,path);path.write_text('{}',encoding='utf-8')
            gid=a.create_goal(store,sid,'按平均排名',file_delivery=True)
            result=a.run_goal(store,sid,gid,ScriptedEngine(
                action('summarize_json',**args(order_by=order('mean'),top_n=2)),
                action('write_artifact',filename='rank.txt',content='排名 [D1:L1]'),
                action('ask',question='測試暫停')),max_steps=3)
            observed=result['steps'][0]['observation']
            self.assertEqual(observed['selected_group_count'],2)
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual(path.read_text(encoding='utf-8'),'{}')
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'])
            for key in ('order_by','top_n','selected_group_count','unselected_group_count','group_details_omitted'):
                self.assertIn(key,stage)
            self.assertNotIn(str(10**50+2),stage);self.assertNotIn('"keys"',stage)
            archive=create_backup(store.path,folder/'backup.paibackup')
            restore_backup(archive['path'],folder/'restored')
            restored=Store(folder/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'][0]['observation'],observed)


if __name__=='__main__':unittest.main()
