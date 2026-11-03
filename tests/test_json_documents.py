import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, json_documents as d
from assistant.documents import check_citations
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action
from assistant.backup import create_backup, restore_backup


def doc(content, did=1):
    return {'id':did,'name':'input.json','content':content,'sha256':hashlib.sha256(content.encode()).hexdigest(),
            'source_path':'PRIVATE_PATH_DO_NOT_OPEN'}


def args(pointer='', offset=0, did=1):
    return {'document_id':did,'pointer':pointer,'offset':offset}


def citation(row):
    return f"[D{row['document_id']}:L{row['start_line']}:C{row['start_char']}-C{row['end_char']}]"


class JsonDocumentTests(unittest.TestCase):
    def test_pointer_escaping_empty_keys_unicode_and_scalar_types(self):
        documents=[doc('{"foo":["bar","baz"],"":0,"a/b":1,"m~n":8,"~1":9," ":true,"é":"pre","é":"decomposed","nil":null}')]
        for pointer, expected in [('/foo/0','bar'),('/', '0'),('/a~1b','1'),('/m~0n','8'),('/~01','9'),('/ ',True),('/é','pre'),('/é','decomposed'),('/nil',None)]:
            with self.subTest(pointer=pointer):
                result=d.read(documents,args(pointer))
                self.assertEqual(result['value'],expected);self.assertTrue(result['value_complete'])
        self.assertEqual(d.read(documents,args('/nil'))['kind'],'null')
        self.assertEqual(d.read(documents,args('/ '))['kind'],'boolean')
        for pointer in ('foo','#/foo','/a~2b','/foo/01','/foo/-','/foo/2','/foo/٠','/foo/0/x','/missing'):
            with self.subTest(pointer=pointer),self.assertRaises(ValueError):d.read(documents,args(pointer))
        self.assertEqual(d.read([doc('{"01":7}')],args('/01'))['value'],'7')

    def test_container_pages_preserve_order_counts_and_no_full_content_claim(self):
        documents=[doc(json.dumps({'records':list(range(27)),'nested':{'hidden':'value'}}))]
        first=d.read(documents,args('/records'));last=d.read(documents,args('/records',20))
        self.assertEqual(first['total_children'],27);self.assertEqual(first['next_offset'],20)
        self.assertEqual([r['pointer'] for r in first['children']],['/records/'+str(i) for i in range(20)])
        self.assertEqual([r['value'] for r in last['children']],[str(i) for i in range(20,27)])
        self.assertIsNone(last['next_offset']);self.assertFalse(last['all_children_in_page'])
        root=d.read(documents,args());self.assertFalse(root['children'][0]['value_complete'])
        self.assertNotIn('value',root['children'][0]);self.assertEqual(root['children'][0]['child_count'],27)
        empty=d.read(documents,args('/records',27));self.assertEqual(empty['children'],[])
        self.assertFalse(empty['all_children_in_page'])
        with self.assertRaises(ValueError):d.read(documents,args('/records',28))

    def test_numbers_preserve_lexical_precision_and_not_float_values(self):
        content='{"big":9007199254740993,"tiny":0.12345678901234567890,"exp":1e999,"negative":-0,"scale":123.4500}'
        for key,expected in [('big','9007199254740993'),('tiny','0.12345678901234567890'),('exp','1e999'),('negative','-0'),('scale','123.4500')]:
            result=d.read([doc(content)],args('/'+key))
            self.assertEqual(result['kind'],'number');self.assertEqual(result['value'],expected)
            self.assertEqual(result['value_encoding'],'number_literal')

    def test_span_parser_agrees_with_standard_json_on_nested_documents(self):
        values=[None,False,True,0,-19,1.25,'quote " and slash \\ and 😀\u2028line',
                {'a/b':{'~x':['',0,True,None]},'空白 ':[]},
                [{'count':i,'label':'列'+str(i)} for i in range(25)]]
        def reconstruct(node):
            if node.kind=='object':return {key:reconstruct(child) for key,child in node.children}
            if node.kind=='array':return [reconstruct(child) for _,child in node.children]
            return node.value
        def verify(node,content):
            parsed=json.loads(content[node.start:node.end],parse_int=str,parse_float=str)
            self.assertEqual(reconstruct(node),parsed)
            for _,child in node.children:verify(child,content)
        for value in values:
            for ascii_only in (True,False):
                for indent in (None,2):
                    content=json.dumps(value,ensure_ascii=ascii_only,indent=indent)
                    root=d.Parser(content,None).parse();verify(root,content)

    def test_invalid_or_ambiguous_json_never_returns_partial_success(self):
        for content in ('{"a":1,"a":2}','{"a":1,"\\u0061":2}','{"ok":1,"nested":{"x":1,"x":2}}',
                        '[1,]','{"x":1,}','[NaN]','Infinity','01','{} {}','{"x":1}//comment',
                        '{"x":"\\ud800"}','{"x":','[true false]','{"a" 1}','"unclosed'):
            with self.subTest(content=content),self.assertRaises(ValueError):d.read([doc(content)],args())

    def test_limits_and_typed_arguments_are_enforced(self):
        for request in ({},dict(args(),path='other.json'),args(offset=True),args(offset=-1),args(did=False),args(pointer=None),args(pointer='x'*1025)):
            with self.subTest(request=request),self.assertRaises(ValueError):
                a.validate_action({'tool':'read_json','args':request,'note':'read'})
        for content in ('"'+'x'*200000+'"','['*34+'0'+']'*34,'['+','.join(['0']*10001)+']',
                        json.dumps({'k'*129:1})):
            with self.subTest(size=len(content)),self.assertRaises(ValueError):d.read([doc(content)],args())
        nested=0
        for _ in range(9):nested={'k'*128:nested}
        with self.assertRaises(ValueError):d.read([doc(json.dumps(nested))],args())

    def test_exact_excerpts_for_unicode_crlf_and_partial_line_citations(self):
        content='\r\n{ "emoji":"😀",\r\n "price":123.4500, "message":"甲\u2028乙" }\r\n'
        documents=[doc(content)];before=copy.deepcopy(documents)
        result=d.read(documents,args('/price'));rows=result['excerpts']
        self.assertTrue(rows);self.assertEqual(result['value'],'123.4500')
        for row in rows:
            line=content.splitlines()[row['start_line']-1]
            self.assertEqual(row['text'],f"L{row['start_line']}: "+line[row['start_char']-1:row['end_char']])
            self.assertTrue(check_citations(citation(row),rows)[0]['range_valid'])
        self.assertFalse(check_citations('[D1:L3]',rows)[0]['range_valid'])
        self.assertNotIn('PRIVATE_PATH',json.dumps(result));self.assertEqual(documents,before)

    def test_scalar_character_pages_have_bounded_real_coverage(self):
        content=json.dumps('甲😀'*3000,ensure_ascii=False);offset=0;received='';pages=[]
        while offset is not None:
            page=d.read([doc(content)],args(offset=offset));pages.append(page)
            self.assertLessEqual(page['returned_chars'],4000)
            received+=''.join(r['text'].split(': ',1)[1] for r in page['excerpts'])
            self.assertFalse(page['value_complete']);self.assertNotIn('value',page)
            offset=page['next_offset']
        self.assertEqual(received,content)
        self.assertEqual(pages[0]['total_chars'],len(content))
        self.assertTrue(check_citations(f'[D1:L1:C1-C{len(content)}]',[r for p in pages for r in p['excerpts']])[0]['range_valid'])
        with self.assertRaises(ValueError):d.read([doc(content)],args(offset=len(content)))

    def test_unicode_line_separator_pages_limit_excerpt_count_and_advance(self):
        content=json.dumps('甲\u2028'*100,ensure_ascii=False);offset=0;seen=[]
        while offset is not None:
            page=d.read([doc(content)],args(offset=offset))
            self.assertLessEqual(len(page['excerpts']),40);self.assertGreater(page['returned_chars'],0)
            seen.append((offset,page['returned_chars']));offset=page['next_offset']
        self.assertEqual(sum(size for _,size in seen),len(content))

    def test_scope_and_cancellation(self):
        with self.assertRaises(ValueError):d.read([doc('{}')],args(did=2))
        cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError):d.read([doc('{}')],args(),cancel)
        class DuringParse:
            calls=0
            def is_set(self):
                self.calls+=1
                return self.calls>5
        with self.assertRaises(EngineError):d.read([doc('[1,2,3,4,5]')],args(),DuringParse())

    def test_tool_loop_backup_and_handoff_keep_source_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'prices.json';path.write_text('{"price":12.50,"count":4,"secret":"LOCAL_ONLY_VALUE"}',encoding='utf-8')
            did=store.attach_document(sid,path);gid=a.create_goal(store,sid,'計算總額，不產檔')
            observed=d.read(store.documents(sid,include_content=True),args('/price',did=did))
            ref=citation(observed['excerpts'][0]);path.write_text('{"price":999}',encoding='utf-8')
            result=a.run_goal(store,sid,gid,ScriptedEngine(action('read_json',**args('/price',did=did)),
                action('calculate',operation='product',values=['12.50','4']),action('finish',report='總額50元。'+ref)))
            self.assertEqual(result['status'],'report_ready');self.assertTrue(result['steps'][-1]['observation']['citation_checks'][0]['range_valid'])
            self.assertEqual(result['steps'][0]['observation']['value'],'12.50');self.assertFalse(result['file_delivery'])
            parent=a.create_goal(store,sid,'讀取欄位後分階段接續')
            a.run_goal(store,sid,parent,ScriptedEngine(action('read_json',**args('/secret',did=did))),max_steps=1)
            child=a.create_goal(store,sid,'繼續核對',parent_id=parent)
            snapshot=a.get_goal(store,sid,child)['previous_stage']
            self.assertIn('/secret',json.dumps(snapshot));self.assertNotIn('LOCAL_ONLY_VALUE',json.dumps(snapshot))
            backup=create_backup(store.path,folder/'backup.paibackup');restore_backup(backup['path'],folder/'restored')
            restored=Store(folder/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],result['steps'])

    def test_file_delivery_cannot_cite_unread_line_and_source_change_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);store=Store(folder/'state.sqlite3');sid=store.new_session()
            path=folder/'input.json';path.write_text('{"price":12.50,"other":"未查讀"}',encoding='utf-8')
            did=store.attach_document(sid,path);gid=a.create_goal(store,sid,'只交付price',file_delivery=True)
            observed=d.read(store.documents(sid,include_content=True),args('/price',did=did))
            ref=citation(observed['excerpts'][0])
            result=a.run_goal(store,sid,gid,ScriptedEngine(action('read_json',**args('/price',did=did)),
                action('write_artifact',filename='price.txt',content='12.50[D1:L1]'),
                action('write_artifact',filename='price.txt',content='12.50'+ref),
                action('deliver',report='已保存price欄位。'+ref,filenames=['price.txt'])),max_steps=4)
            self.assertEqual(result['status'],'report_ready');self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
            self.assertEqual((folder/'autonomy-artifacts'/gid/'price.txt').read_text(encoding='utf-8'),'12.50'+ref)
            other=store.new_session();other_id=store.attach_document(other,path)
            pending=a.create_goal(store,sid,'核對price')
            invalid=a.run_goal(store,sid,pending,ScriptedEngine(action('read_json',**args('/price',did=other_id))),max_steps=1)
            self.assertEqual(invalid['steps'][0]['action']['tool'],'invalid')
            def change():
                path.write_text('{"price":99}',encoding='utf-8');store.attach_document(sid,path)
                return action('read_json',**args('/price',did=did))
            stopped=a.run_goal(store,sid,pending,ScriptedEngine(change),max_steps=1)
            self.assertEqual(len(stopped['steps']),1);self.assertEqual(stopped['status'],'paused')


if __name__=='__main__':unittest.main()
