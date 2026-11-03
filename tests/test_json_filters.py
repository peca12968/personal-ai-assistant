"""Typed JSON predicates must select exactly the intended statistical population."""
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


def source(content):
    return {'id':1,'name':'filters.json','content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()}


def predicate(pointer, operator, kind, value):
    return {'pointer':pointer,'operator':operator,'type':kind,'value':value}


def arguments(filters, pointer='/records', value_pointer='/value'):
    return {'document_id':1,'pointer':pointer,'value_pointer':value_pointer,'filters':filters}


def summarize(rows, filters):
    return stats.summarize([source(json.dumps({'records':rows},ensure_ascii=False))],arguments(filters))


class JsonFilterTests(unittest.TestCase):
    def test_full_population_and_original_extrema_positions(self):
        content=fixture('filtered_records')
        filters=[predicate('/status','eq','string','paid'),predicate('/metrics/quantity','gte','number','10')]
        result=stats.summarize([source(content)],arguments(filters,value_pointer='/metrics/cost'))
        rows=json.loads(content,parse_float=Fraction)['records']
        expected=[(i,r['metrics']['cost']) for i,r in enumerate(rows) if r['status']=='paid' and r['metrics']['quantity']>=10]
        self.assertEqual((result['source_count'],result['count'],result['excluded_count']),(300,40,260))
        self.assertEqual(result['sum']['exact'],str(sum(v for _,v in expected)))
        self.assertEqual(result['mean']['exact'],str(sum(v for _,v in expected)/len(expected)))
        self.assertEqual((result['minimum_index'],result['maximum_index']),(28,268))
        self.assertEqual(result['minimum']['exact'],'-361/10')
        self.assertEqual(result['maximum']['exact'],'543/10')
        self.assertTrue(result['complete']);self.assertTrue(result['filtered'])
        self.assertEqual(result['filter_count'],2)
        self.assertEqual(result['filters'],filters)
        self.assertEqual(result['excerpts'],[]);self.assertFalse(result['raw_values_provided'])
        filters[0]['value']='pending'
        self.assertEqual(result['filters'][0]['value'],'paid')

    def test_numeric_comparison_exact_beyond_float_and_all_operators(self):
        content=fixture('filtered_precision')
        result=stats.summarize([source(content)],arguments([predicate('/id','gt','number','9007199254740992')]))
        self.assertEqual((result['source_count'],result['count'],result['excluded_count']),(3,2,1))
        self.assertEqual(result['sum']['exact'],'3/10')
        self.assertEqual(result['mean']['exact'],'3/20')
        rows=[{'key':-1,'value':1},{'key':0,'value':2},{'key':1,'value':4}]
        for op,total in [('eq','2'),('ne','5'),('gt','4'),('gte','6'),('lt','1'),('lte','3')]:
            with self.subTest(operator=op):
                self.assertEqual(summarize(rows,[predicate('/key',op,'number','-0.0e0')])['sum']['exact'],total)
        self.assertEqual(summarize([{'key':10,'value':7}],[predicate('/key','eq','number','1e1')])['count'],1)

    def test_string_boolean_null_equality_is_explicit_and_not_coerced(self):
        for kind,values,target in [('string',['Paid','paid','ｐａｉｄ'],'paid'),('boolean',[True,False,True],False),('null',[None,None,None],None)]:
            rows=[{'key':v,'value':i+1} for i,v in enumerate(values)]
            for op in ('eq','ne'):
                expected=[r['value'] for r in rows if (r['key']==target)==(op=='eq')]
                with self.subTest(kind=kind,op=op):
                    result=summarize(rows,[predicate('/key',op,kind,target)])
                    self.assertEqual(result['count'],len(expected));self.assertEqual(result['sum']['exact'],str(sum(expected)))
        for kind,value,actual in [('number','1',True),('string','1',1),('boolean',False,0),('null',None,'null'),('string','paid',None),('number','1',{}),('number','1',[])]:
            with self.subTest(kind=kind,actual=actual),self.assertRaisesRegex(ValueError,'索引0'):
                summarize([{'key':actual,'value':1}],[predicate('/key','eq',kind,value)])

    def test_every_filter_field_is_checked_even_after_nonmatch(self):
        rows=[{'status':'pending','value':None}]
        first=predicate('/status','eq','string','paid');second=predicate('/quantity','gte','number','1')
        for filters in ([first,second],[second,first]):
            with self.assertRaisesRegex(ValueError,'索引0'):
                summarize(rows,filters)
        for op in ('eq','ne'):
            with self.assertRaisesRegex(ValueError,'索引0'):
                summarize([{'value':1}],[predicate('/absent',op,'null',None)])
        # A proven-excluded row's aggregation value is outside the requested population.
        result=summarize([{'status':'pending'},{'status':'paid','value':4}],[first])
        self.assertEqual(result['sum']['exact'],'4')
        with self.assertRaisesRegex(ValueError,'索引1'):
            summarize([{'status':'pending'},{'status':'paid','value':None}],[first])

    def test_empty_selection_has_undefined_mean_and_no_silent_fill(self):
        f=predicate('/status','eq','string','paid')
        result=summarize([{'status':'pending','value':None},{'status':'pending'}],[f])
        self.assertEqual((result['source_count'],result['count'],result['excluded_count']),(2,0,2))
        self.assertEqual(result['sum']['exact'],'0')
        for key in ('mean','minimum','maximum','minimum_index','maximum_index'):
            self.assertIsNone(result[key])
        self.assertTrue(result['complete'])
        self.assertEqual(summarize([],[f])['source_count'],0)
        with self.assertRaises(ValueError):summarize([],[{**f,'operator':'contains'}])

    def test_exact_schema_limits_unicode_and_no_executable_expressions(self):
        f=predicate('/key','eq','number','1')
        invalid=[None,{},'x',[f]*6,[{}],[{**f,'operator':[]}],[{**f,'type':[]}],
                 [{**f,'operator':'__import__'}],[{**f,'extra':1}],
                 [{**f,'pointer':'#/key'}],[{**f,'pointer':'/bad~2'}],
                 [{**f,'value':1}],[{**f,'value':True}],[{**f,'value':1.0}],
                 [{**f,'value':' 1'}],[{**f,'value':'1/2'}],[{**f,'value':'NaN'}],
                 [{**f,'value':'9'*2001}],[{**f,'value':'1e99999999'}],
                 [predicate('/key','eq','string','x'*2001)],
                 [predicate('/key','eq','string','\ud800')],
                 [predicate('/key','gt','string','abc')],
                 [predicate('/key','eq','boolean',1)],
                 [predicate('/key','eq','null','null')]]
        for filters in invalid:
            with self.subTest(filters=str(filters)[:80]),self.assertRaises(ValueError):
                stats.validate(arguments(filters))
        stats.validate(arguments([f]*5))
        stats.validate(arguments([predicate('/key','eq','string','x'*2000)]))
        with self.assertRaises(ValueError):stats.validate({**arguments([]),'where':'value>1'})

    def test_escaped_and_root_filter_paths_and_legacy_population(self):
        filters=[predicate('/a~1b/m~0n/','eq','string','命中')]
        result=summarize([{'a/b':{'m~n':{'':'命中'}},'value':3}],filters)
        self.assertEqual(result['sum']['exact'],'3')
        result=stats.summarize([source('[1,2,3]')],arguments([predicate('','gt','number','1')],pointer='',value_pointer=''))
        self.assertEqual(result['sum']['exact'],'5')
        for filters in (None,[]):
            params=arguments([],pointer='',value_pointer='')
            if filters is None:params.pop('filters')
            result=stats.summarize([source('[1,2,3]')],params)
            self.assertEqual((result['source_count'],result['count'],result['excluded_count']),(3,3,0))
            self.assertFalse(result['filtered']);self.assertEqual(result['filters'],[])

    def test_filtered_bounds_duplicate_keys_and_ordered_overflow(self):
        f=predicate('/key','eq','string','yes')
        huge='9'*2000
        raw='{"records":[{"key":"no","value":1e99999},{"key":"yes","value":3}]}'
        self.assertEqual(stats.summarize([source(raw)],arguments([f]))['sum']['exact'],'3')
        for raw in ('{"records":[{"key":"no","key":"yes","value":1}]}',
                    '{"records":[{"key":"no","value":1e1},{"key":"yes","value":'+huge+'},{"key":"yes","value":1},{"key":"yes","value":-'+huge+'}]}'):
            with self.assertRaises(ValueError):stats.summarize([source(raw)],arguments([f]))
        with self.assertRaises(ValueError):
            stats.summarize([source('{"records":[{"key":1e99999,"value":1}]}')],arguments([predicate('/key','gt','number','0')]))

    def test_cancel_during_predicate_returns_no_partial_result(self):
        stop=threading.Event();original=stats.number;calls=[]
        def cancelling(value):
            answer=original(value);calls.append(value)
            if len(calls)==3:stop.set()
            return answer
        with patch.object(stats,'number',side_effect=cancelling),self.assertRaises(EngineError):
            stats.summarize([source('{"records":[{"key":1,"value":2},{"key":3,"value":4}]}')],arguments([predicate('/key','gt','number','0')]),stop)
        self.assertTrue(stop.is_set())

    def test_goal_observation_backup_and_stage_preserve_scope_without_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'data.json';path.write_text(fixture('filtered_precision'),encoding='utf-8')
            store.attach_document(sid,path);path.write_text('{"records":[]}',encoding='utf-8')
            params=arguments([predicate('/id','gt','number','9007199254740992')])
            gid=a.create_goal(store,sid,'依匯入副本篩選後統計',file_delivery=True)
            result=a.run_goal(store,sid,gid,ScriptedEngine(
                action('summarize_json',**params),
                action('write_artifact',filename='result.txt',content='符合2筆 [D1:L1]'),
                action('ask',question='合成測試暫停')),max_steps=3)
            self.assertEqual(result['steps'][0]['observation']['count'],2)
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual(result['steps'][0]['observation']['filters'],params['filters'])
            child=a.create_goal(store,sid,'接續核對',parent_id=gid)
            stage=a.get_goal(store,sid,child)['previous_stage']
            text=json.dumps(stage)
            self.assertIn('filter_count',text);self.assertIn('excluded_count',text)
            self.assertIn('filter_conditions_omitted',text)
            self.assertNotIn('9007199254740992',text)
            archive=create_backup(store.path,folder/'backup.paibackup')
            restore_backup(archive['path'],folder/'restored')
            restored=Store(folder/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],result['steps'])
            self.assertEqual(path.read_text(encoding='utf-8'),'{"records":[]}')


if __name__=='__main__':unittest.main()
