import hashlib
import json
import tempfile
import threading
import unittest
from decimal import localcontext, ROUND_UP
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, json_statistics as stats
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def doc(content):
    return {'id':1,'name':'data.json','content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()}


def args(pointer='', value_pointer=''):
    return {'document_id':1,'pointer':pointer,'value_pointer':value_pointer}


class JsonStatisticsTests(unittest.TestCase):
    def test_whole_large_nested_array_exact_results_and_escaped_paths(self):
        content=json.dumps({'a/b':[{'m~n':{'':i}} for i in range(300)]})
        result=stats.summarize([doc(content)],args('/a~1b','/m~0n/'))
        self.assertEqual(result['count'],300)
        self.assertEqual(result['sum']['exact'],'44850')
        self.assertEqual(result['mean']['exact'],'299/2')
        self.assertEqual(result['minimum']['exact'],'0')
        self.assertEqual(result['maximum']['exact'],'299')
        self.assertEqual((result['minimum_index'],result['maximum_index']),(0,299))
        self.assertTrue(result['complete']);self.assertFalse(result['raw_values_provided'])
        self.assertEqual(result['excerpts'],[])
        self.assertNotIn('children',result)

    def test_json_numbers_are_exact_and_preview_ignores_decimal_context(self):
        content='[9007199254740993,0.1,0.2,-9007199254740993,1e-3]'
        with localcontext() as ctx:
            ctx.prec=2;ctx.rounding=ROUND_UP;ctx.Emax=2
            result=stats.summarize([doc(content)],args())
        self.assertEqual(result['sum']['exact'],'301/1000')
        self.assertEqual(result['mean']['exact'],'301/5000')
        self.assertEqual(result['mean']['decimal'],'0.0602')
        self.assertEqual(result['maximum']['exact'],'9007199254740993')
        self.assertEqual(result['minimum']['exact'],'-9007199254740993')
        result=stats.summarize([doc('[0,0,1]')],args())
        self.assertEqual(result['mean']['exact'],'1/3');self.assertTrue(result['mean']['rounded'])
        self.assertEqual(result['minimum_index'],0)

    def test_empty_array_has_zero_sum_and_undefined_mean_extremes(self):
        result=stats.summarize([doc('[]')],args())
        self.assertEqual(result['count'],0);self.assertEqual(result['sum']['exact'],'0')
        for key in ('mean','minimum','maximum','minimum_index','maximum_index'):
            self.assertIsNone(result[key])
        self.assertTrue(result['complete'])

    def test_no_missing_null_boolean_string_or_nested_container_coercion(self):
        for raw in ('null','true','false','"2"','[]','{}'):
            with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'索引1'):
                stats.summarize([doc('[1,'+raw+',3]')],args())
        with self.assertRaisesRegex(ValueError,'索引1'):
            stats.summarize([doc('[{"x":1},{}]')],args('','/x'))
        with self.assertRaises(ValueError):stats.summarize([doc('{"x":1}')],args())

    def test_whole_document_validation_scope_and_exact_argument_shape(self):
        for content in ('{"a":[1],"x":1,"x":2}', '[1,2] true','[NaN]','[1,]','["\\ud800"]'):
            with self.subTest(content=content),self.assertRaises(ValueError):stats.summarize([doc(content)],args('/a') if content.startswith('{') else args())
        for changed in ({'document_id':True},{'pointer':'#/a'},{'value_pointer':'/x~2'}, {'offset':0}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):stats.validate({**args(),**changed})
        with self.assertRaises(ValueError):stats.summarize([doc('[1]')],{**args(),'document_id':2})
        with self.assertRaises(ValueError):stats.summarize([doc('['+','.join('1' for _ in range(10000))+']')],args())

    def test_numeric_bounds_include_ordered_intermediate_sum_and_exponent(self):
        for literal in ('1e2000','1e-2000','0e2001','1e9999999999999999999999','9'*2001):
            with self.subTest(literal=literal[:30]),self.assertRaises(ValueError):stats.summarize([doc('['+literal+']')],args())
        self.assertEqual(stats.number('10e-2000').denominator,10**1999)
        self.assertEqual(stats.number('1e'+'0'*1000+'1999').numerator,10**1999)
        maximum='9'*2000
        with self.assertRaises(ValueError):stats.summarize([doc('['+maximum+',1,-'+maximum+']')],args())

    def test_cancellation_returns_no_partial_statistics(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(EngineError):stats.summarize([doc('[1,2]')],args(),stop)
        stop.clear();original=stats.number
        def cancel_after_number(text):
            value=original(text);stop.set();return value
        with patch.object(stats,'number',side_effect=cancel_after_number),self.assertRaises(EngineError):
            stats.summarize([doc('[1,2]')],args(),stop)

    def test_goal_integration_snapshot_backup_and_no_unread_citation_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'data.json';path.write_text('[1,2,3]',encoding='utf-8')
            store.attach_document(sid,path);path.write_text('[999]',encoding='utf-8')
            gid=a.create_goal(store,sid,'統計匯入副本',file_delivery=True)
            result=a.run_goal(store,sid,gid,ScriptedEngine(
                action('summarize_json',**args()),
                action('write_artifact',filename='result.txt',content='總和6。[D1:L1]'),
                action('write_artifact',filename='result.txt',content='依data.json根陣列工具統計，3筆，總和6，平均2。'),
                action('deliver',report='已交付統計結果',filenames=['result.txt'])),max_steps=4)
            self.assertEqual(result['status'],'report_ready')
            self.assertEqual(result['steps'][0]['observation']['sum']['exact'],'6')
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual(path.read_text(encoding='utf-8'),'[999]')
            backup=create_backup(store.path,folder/'backup.paibackup');restore_backup(backup['path'],folder/'restored')
            self.assertEqual(a.get_goal(Store(folder/'restored'/'assistant.sqlite3'),sid,gid)['steps'],result['steps'])
            parent=a.create_goal(store,sid,'先統計')
            a.run_goal(store,sid,parent,ScriptedEngine(action('summarize_json',**args())),max_steps=1)
            child=a.create_goal(store,sid,'繼續',parent_id=parent)
            checkpoint=a.get_goal(store,sid,child)['previous_stage']
            raw=json.dumps(checkpoint)
            self.assertIn('value_pointer',raw);self.assertNotIn('decimal_precision',raw)


if __name__=='__main__':unittest.main()
