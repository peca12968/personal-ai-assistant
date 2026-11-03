"""Grouped JSON statistics must preserve the complete, typed population."""
import hashlib
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


def document(raw):
    return {'id':1, 'name':'groups.json', 'content':raw,
            'sha256':hashlib.sha256(raw.encode()).hexdigest()}


def args(**extra):
    return {'document_id':1, 'pointer':'/records', 'value_pointer':'/value',
            'group_by':['/team'], **extra}


def run(rows, **extra):
    return stats.summarize([document(json.dumps({'records':rows},ensure_ascii=False))],args(**extra))


class JsonGroupTests(unittest.TestCase):
    def test_all_groups_and_population_match_independent_exact_reference(self):
        raw=fixture('group_records')
        params=args(filters=[{'pointer':'/status','operator':'eq','type':'string','value':'paid'}])
        reference={}
        rows=json.loads(raw,parse_float=Fraction)['records']
        for i,row in enumerate(rows):
            if row['status']=='paid':reference.setdefault(row['team'],[]).append((i,row['value']))
        pages=[]; offset=0
        while offset is not None:
            page=stats.summarize([document(raw)],{**params,'offset':offset})
            pages.append(page);offset=page['next_offset']
        self.assertEqual([p['returned_group_count'] for p in pages],[20,3])
        groups=[g for p in pages for g in p['groups']]
        self.assertEqual([g['keys'][0]['value'] for g in groups],list(reference))
        for group,values in zip(groups,reference.values()):
            total=sum((v for _,v in values),Fraction())
            self.assertEqual(group['count'],len(values))
            self.assertEqual(group['sum']['exact'],str(total))
            self.assertEqual(group['mean']['exact'],str(total/len(values)))
            self.assertEqual(group['minimum_index'],min(values,key=lambda x:x[1])[0])
            self.assertEqual(group['maximum_index'],max(values,key=lambda x:x[1])[0])
        total=sum((row['value'] for row in rows if row['status']=='paid'),Fraction())
        for page in pages:
            self.assertEqual((page['source_count'],page['count'],page['excluded_count']),(300,200,100))
            self.assertEqual(page['group_count'],23)
            self.assertEqual(page['sum']['exact'],str(total))
            self.assertEqual(page['mean']['exact'],str(total/200))
            self.assertTrue(page['complete']);self.assertTrue(page['page_is_partial'])
            self.assertFalse(page['all_groups_in_page'])
            self.assertEqual(page['excerpts'],[]);self.assertFalse(page['raw_values_provided'])
        self.assertNotEqual(total/200,sum(Fraction(g['mean']['exact']) for g in groups)/23)

    def test_type_identity_numeric_equivalence_and_first_seen_order(self):
        result=stats.summarize([document(fixture('typed_groups'))],args(group_by=['/key']))
        groups=result['groups']
        self.assertEqual([(g['keys'][0]['type'],g['keys'][0]['value']) for g in groups],
                         [('boolean',True),('number','1'),('string','1'),('null',None),('number','0'),('string','True')])
        self.assertEqual([g['count'] for g in groups],[1,2,1,1,2,1])
        self.assertEqual(groups[1]['sum']['exact'],'3/10')
        self.assertEqual(groups[1]['mean']['exact'],'3/20')
        self.assertEqual(groups[4]['sum']['exact'],'90')
        self.assertEqual(result['sum']['exact'],'2103/10')
        self.assertEqual(result['mean']['exact'],'2103/80')
        self.assertTrue(result['all_groups_in_page'])
        self.assertFalse(result['page_is_partial'])

    def test_two_pointers_escaping_case_unicode_and_large_numeric_keys(self):
        rows=[{'a/b':{'~key':label},'n':n,'value':i} for i,(label,n) in enumerate([
            ('A',9007199254740992),('A',9007199254740993),('a',1),('Ａ',1),('A',9007199254740993)])]
        result=run(rows,group_by=['/a~1b/~0key','/n'])
        self.assertEqual(result['group_count'],4)
        self.assertEqual(result['groups'][1]['count'],2)
        self.assertEqual(result['groups'][1]['sum']['exact'],'5')
        self.assertEqual(result['groups'][1]['keys'][1]['value'],'9007199254740993')
        root=stats.summarize([document('[1,1.0,2]')],args(pointer='',value_pointer='',group_by=['']))
        self.assertEqual([g['count'] for g in root['groups']],[2,1])

    def test_missing_container_and_unselected_group_fields_are_distinct(self):
        for key in ({},[], 'x'*1001):
            with self.subTest(key=str(key)[:20]),self.assertRaisesRegex(ValueError,'索引1'):
                run([{'team':'A','value':1},{'team':key,'value':2}])
        with self.assertRaisesRegex(ValueError,'索引1'):
            run([{'team':None,'value':1},{'value':2}])
        result=run([{'team':None,'value':1},{'team':None,'value':2}])
        self.assertEqual(result['group_count'],1)
        rule={'pointer':'/take','operator':'eq','type':'boolean','value':True}
        result=run([{'take':True,'team':'A','value':3},{'take':False}],filters=[rule])
        self.assertEqual((result['count'],result['excluded_count']),(1,1))
        with self.assertRaisesRegex(ValueError,'索引1'):
            run([{'take':True,'team':'A','value':3},{'team':'B','value':4}],filters=[rule])

    def test_empty_excluded_and_beyond_last_page_never_imply_complete_group_display(self):
        result=run([])
        self.assertEqual(result['groups'],[]);self.assertEqual(result['group_count'],0)
        self.assertTrue(result['all_groups_in_page']);self.assertIsNone(result['mean'])
        rule={'pointer':'/team','operator':'eq','type':'string','value':'B'}
        result=run([{'team':'A'}],filters=[rule])
        self.assertEqual(result['count'],0);self.assertEqual(result['sum']['exact'],'0')
        result=run([{'team':'A','value':1}],offset=1)
        self.assertEqual(result['groups'],[]);self.assertIsNone(result['next_offset'])
        self.assertEqual(result['omitted_before'],1);self.assertTrue(result['page_is_partial'])
        self.assertFalse(result['all_groups_in_page']);self.assertEqual(result['group_count'],1)

    def test_page_character_budget_keeps_whole_group_records(self):
        rows=[{'team':'x'*900+str(i),'category':'y'*900,'value':i} for i in range(25)]
        offset=0;received=[]
        while offset is not None:
            page=run(rows,offset=offset,group_by=['/team','/category'])
            encoded=sum(len(json.dumps(g,ensure_ascii=False,separators=(',',':'))) for g in page['groups'])
            self.assertLessEqual(encoded,stats.GROUP_PAGE_CHARS)
            self.assertGreater(page['returned_group_count'],0)
            self.assertLess(page['returned_group_count'],20)
            received.extend(g['keys'][0]['value'] for g in page['groups'])
            offset=page['next_offset']
        self.assertEqual(received,[r['team'] for r in rows])
        with patch.object(stats,'GROUP_PAGE_CHARS',100),self.assertRaises(ValueError):run(rows)

    def test_argument_group_limits_and_original_non_grouped_contract(self):
        for delta in ({'group_by':[]},{'group_by':['/a']*2},{'group_by':['/a','/b','/c']},
                      {'group_by':'/team'},{'group_by':[True]},{'group_by':['/bad~2']},
                      {'offset':True},{'offset':-1},{'offset':1001},{'extra':1}):
            with self.subTest(delta=delta),self.assertRaises(ValueError):stats.validate(args(**delta))
        plain={'document_id':1,'pointer':'/records','value_pointer':'/value'}
        self.assertNotIn('groups',stats.summarize([document('{"records":[{"value":1}]}')],plain))
        with self.assertRaises(ValueError):stats.validate({**plain,'offset':0})
        self.assertEqual(run([{'team':i,'value':1} for i in range(1000)])['group_count'],1000)
        with self.assertRaisesRegex(ValueError,'1000'):run([{'team':i,'value':1} for i in range(1001)])

    def test_all_groups_validate_numeric_bounds_even_outside_returned_page(self):
        maximum='9'*2000
        raw='{"records":[{"team":"A","value":'+maximum+'},{"team":"B","value":-'+maximum+'},{"team":"A","value":1},{"team":"B","value":-1}]}'
        with self.assertRaisesRegex(ValueError,'索引2'):stats.summarize([document(raw)],args())
        rows=['{"team":"G'+str(i)+'","value":0}' for i in range(21)]
        rows+=['{"team":"A","value":1e-1999}']+['{"team":"A","value":0}']*10
        rows+=['{"team":"B","value":-1e-1999}']
        with self.assertRaises(ValueError):stats.summarize([document('{"records":['+','.join(rows)+']}')],args())
        with self.assertRaises(ValueError):run([{'team':1,'value':None}])
        with self.assertRaises(ValueError):stats.summarize([document('{"records":[{"team":1e2000,"value":1}]}')],args())

    def test_cancellation_during_group_identity_returns_no_partial_result(self):
        stop=threading.Event();original=stats.number;count=0
        def cancel(value):
            nonlocal count
            result=original(value);count+=1
            if count==3:stop.set()
            return result
        with patch.object(stats,'number',side_effect=cancel),self.assertRaises(EngineError):
            stats.summarize([document('{"records":[{"team":1,"value":2},{"team":3,"value":4}]}')],args(),stop)

    def test_real_goal_backup_handoff_and_citation_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'data.json';path.write_text(fixture('typed_groups'),encoding='utf-8')
            store.attach_document(sid,path);path.write_text('{}',encoding='utf-8')
            gid=a.create_goal(store,sid,'按型別分組',file_delivery=True)
            result=a.run_goal(store,sid,gid,ScriptedEngine(
                action('summarize_json',**args(group_by=['/key'])),
                action('write_artifact',filename='groups.txt',content='共6組 [D1:L1]'),
                action('ask',question='合成測試暫停')),max_steps=3)
            self.assertEqual(result['steps'][0]['observation']['group_count'],6)
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual(path.read_text(encoding='utf-8'),'{}')
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'])
            self.assertIn('group_details_omitted',stage);self.assertIn('group_count',stage)
            self.assertNotIn('2103/10',stage);self.assertNotIn('"keys"',stage)
            archive=create_backup(store.path,folder/'backup.paibackup')
            restore_backup(archive['path'],folder/'restored')
            restored=Store(folder/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],result['steps'])


if __name__=='__main__':unittest.main()
