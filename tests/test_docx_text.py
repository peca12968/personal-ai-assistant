import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, docx_text as d
from assistant.documents import read_document,retrieve_documents,check_citations
from assistant.store import Store
from assistant.folder_search import search_folder,attach_result
from assistant.backup import create_backup,restore_backup
from scripts.docx_fixture import W,R,P,package,paragraph,example
from test_autonomy import ScriptedEngine,action


class DocxTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'sample.docx'

    def test_body_tables_and_referenced_stories_order_and_scope(self):
        self.path.write_bytes(example());result=read_document(self.path);text=result['content']
        for value in ('青鳥','白鷺','120','運費固定40元','ORBIT-72','版本：R7','5個日曆天','尚非決議'):
            self.assertIn(value,text)
        self.assertNotIn('UNREFERENCED_OLD_NOTE',text)
        self.assertLess(text.index('採購方案'),text.index('【表格1／列1'))
        self.assertLess(text.index('【表格1／列3'),text.index('運費規則'))
        self.assertIn('不是Word頁碼',text);self.assertIn('文字方塊',text)
        self.assertEqual(result['encoding'],d.ENCODING)
        expected=hashlib.sha256(d.ENCODING.encode()+b'\0'+example()+b'\0'+text.encode()).hexdigest()
        self.assertEqual(result['sha256'],expected)
        self.assertNotIn(hashlib.sha256(example()).hexdigest(),text)
        self.assertEqual(self.path.read_bytes(),example())

    def test_text_runs_links_fields_tabs_breaks_and_nested_tables(self):
        body='<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t></w:r><w:hyperlink r:id="outside"><w:r><w:t>連結顯示字</w:t></w:r></w:hyperlink><w:fldSimple w:instr="DO_NOT_EXECUTE"><w:r><w:t>快取值</w:t></w:r></w:fldSimple></w:p>'
        body+='<w:tbl><w:sdt><w:sdtContent><w:tr><w:tc>'+paragraph('外格')+'<w:tbl><w:tr><w:tc>'+paragraph('內格')+'</w:tc></w:tr></w:tbl></w:tc></w:tr></w:sdtContent></w:sdt></w:tbl>'
        text=d.project(package(body))
        self.assertIn('甲\t乙\n丙連結顯示字快取值',text)
        self.assertNotIn('DO_NOT_EXECUTE',text)
        self.assertEqual(text.count('【表格2／列1／儲存格1】'),1)
        self.assertIn('外格',text);self.assertIn('內格',text)

    def test_no_ocr_and_revision_or_alternate_document_rejection(self):
        for body in ('<w:p><w:r><w:drawing/></w:r></w:p>',paragraph('')+'<w:altChunk r:id="html"/>',
                     '<w:ins>'+paragraph('pending')+'</w:ins>',
                     paragraph('text')+'<w:pPrChange/>'):
            with self.subTest(body=body),self.assertRaises(ValueError):d.project(package(body))
        text=d.project(package(paragraph('有文字')+'<w:p><w:r><w:drawing/></w:r></w:p>'))
        self.assertIn('[圖形／物件文字未解析]',text)

    def test_dtd_utf16_entities_depth_nodes_and_expansion_limits(self):
        xml=f'<!DOCTYPE w:document [<!ENTITY x "NOT_ALLOWED">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'
        for raw in (xml.encode(),xml.encode('utf-16')):
            with self.assertRaisesRegex(ValueError,'DTD'):d.project(package('',extra={'word/document.xml':raw}))
        deep='<w:sdt>'*130+paragraph('deep')+'</w:sdt>'*130
        with self.assertRaises(ValueError):d.project(package(deep))
        with self.assertRaises(ValueError):d.project(package('<w:p/>'*50001))
        with patch.object(d,'MAX_PART',200),self.assertRaises(ValueError):d.project(example())
        with patch.object(d,'MAX_XML',200),self.assertRaises(ValueError):d.project(example())

    def test_corrupt_duplicate_missing_external_and_traversal_parts(self):
        bad=[b'not a zip',package(paragraph('a'),extra={'../outside.xml':'x'}),
             package(paragraph('a'),extra={'word/document.xml':'broken xml'}),
             package(paragraph('a'),extra={'_rels/.rels':f'<Relationships xmlns="{P}"><Relationship Id="x" Type="{R}/officeDocument" Target="https://example.invalid/a" TargetMode="External"/></Relationships>'}),
             package('<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'+paragraph('text'))]
        buf=io.BytesIO(package(paragraph('a')))
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(buf,'a') as z:z.writestr('word/document.xml','duplicate')
        bad.append(buf.getvalue())
        for raw in bad:
            with self.subTest(size=len(raw)),self.assertRaises(ValueError):d.project(raw)

    def test_snapshot_reimport_hash_source_changes_and_citations(self):
        self.path.write_bytes(example());store=Store(self.root/'data'/'assistant.sqlite3');sid=store.new_session()
        did=store.attach_document(sid,self.path);old=store.documents(sid,include_content=True)[0]
        catalog=retrieve_documents('運費', [old])
        self.assertEqual(catalog['catalog'][0]['citation_basis'],'extracted_text_lines')
        target=next(i for i,line in enumerate(old['content'].splitlines(),1) if '運費固定40元' in line)
        gid=a.create_goal(store,sid,'確認運費')
        self.path.write_bytes(package(paragraph('新運費999元')))
        result=a.run_goal(store,sid,gid,ScriptedEngine(action('read',document_id=did,start_line=target,end_line=target),
            action('finish',report=f'運費40元[D{did}:L{target}]')),max_steps=2)
        self.assertIn('運費40元',result['result'])
        self.assertTrue(result['steps'][-1]['observation']['citation_checks'][0]['range_valid'])
        pending=a.create_goal(store,sid,'下一項')
        store.attach_document(sid,self.path)
        self.assertNotEqual(old['sha256'],store.documents(sid)[0]['sha256'])
        self.assertEqual(a.run_goal(store,sid,pending,ScriptedEngine())['steps'],[])
        with patch.object(d,'project',return_value='changed projection'):
            changed=read_document(self.path)
        self.assertNotEqual(changed['sha256'],read_document(self.path)['sha256'])

    def test_search_preview_scope_revalidation_and_backup(self):
        self.path.write_bytes(example());store=Store(self.root/'data'/'assistant.sqlite3');sid=store.new_session()
        report=search_folder(self.root,'ORBIT-72')
        result=next(x for x in report['results'] if x['relative_path']=='sample.docx')
        self.assertEqual(result['citation_basis'],'extracted_text_lines')
        did=attach_result(store,sid,report,result)
        self.assertEqual(store.documents(sid,include_content=True)[0]['encoding'],d.ENCODING)
        restore_backup(create_backup(store.path)['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(restored.documents(sid,include_content=True),store.documents(sid,include_content=True))
        self.path.write_bytes(package(paragraph('new')))
        with self.assertRaises(ValueError):attach_result(store,sid,report,result)

    def test_strict_namespace_and_utf16_xml_text_are_supported(self):
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(example())) as source,zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as dest:
            for name in source.namelist():
                value=source.read(name).decode().replace(W,d.W[1]).replace(R,d.R[1])
                dest.writestr(name,value.encode())
        text=d.project(output.getvalue())
        self.assertIn('ORBIT-72',text);self.assertIn('運費固定40元',text)
        xml=f'<w:document xmlns:w="{W}"><w:body>{paragraph("中文 café 😀")}</w:body></w:document>'
        self.assertIn('中文 café 😀',d.project(package('',extra={'word/document.xml':xml.encode('utf-16')})))

    def test_document_window_explains_projection_and_rejects_bad_import(self):
        import tkinter as tk
        from assistant.document_ui import DocumentWindow
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        store=Store(self.root/'data'/'assistant.sqlite3');sid=store.new_session()
        self.path.write_bytes(example())
        view=DocumentWindow(root,store,sid);view.window.withdraw()
        view.import_paths([self.path]);view.listing.selection_set(view.listing.get_children()[0]);view.select()
        self.assertIn('不是Word頁碼',view.detail.get('1.0','end'))
        broken=self.root/'broken.docx';broken.write_bytes(b'broken')
        with patch('assistant.document_ui.messagebox.showerror') as error:view.import_paths([broken])
        self.assertTrue(error.called);self.assertEqual(len(store.documents(sid)),1)

    def test_package_metadata_change_does_not_create_text_difference(self):
        original=example();output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source,zipfile.ZipFile(output,'w') as dest:
            for name in source.namelist():
                info=zipfile.ZipInfo(name,(2025,2,2,0,0,0));dest.writestr(info,source.read(name))
        other=output.getvalue();self.assertNotEqual(original,other)
        self.path.write_bytes(original);first=read_document(self.path)
        self.path.write_bytes(other);second=read_document(self.path)
        self.assertEqual(first['content'],second['content'])
        self.assertNotEqual(first['sha256'],second['sha256'])
        from assistant.document_diff import compare
        result=compare([{'id':1,**first},{'id':2,**second}],{'old_document_id':1,'new_document_id':2,'offset':0})
        self.assertTrue(result['line_text_equal']);self.assertTrue(result['decoded_text_equal'])
        self.assertFalse(result['source_fingerprint_equal']);self.assertIsNone(result['byte_hash_equal'])


if __name__=='__main__':unittest.main()
