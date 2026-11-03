import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import memory_commands as m
from assistant.engine import Assistant
from assistant.store import Store
from assistant.backup import create_backup, restore_backup
from test_memory_commands import NoModel


class MemoryBrowsingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'data'/'assistant.sqlite3')
        self.sid=self.store.new_session();self.assistant=Assistant(self.store,NoModel())

    def ask(self,text):return self.assistant.ask(self.sid,text)

    def test_all_pages_cover_old_ids_once_and_search_finds_disabled_expired(self):
        ids=[self.store.remember(f'青鳥項目{i}',active=i!=0,expires_on='2000-01-01' if i==1 else None) for i in range(43)]
        before=self.store.memories();seen=[]
        for page,size in ((1,20),(2,20),(3,3)):
            reply=self.ask('列出記憶' if page==1 else f'列出記憶（第{page}頁）')
            receipt=reply['memory_action'];seen+=receipt['listed_ids']
            self.assertEqual((receipt['total'],receipt['page'],receipt['total_pages']),(43,page,3))
            self.assertEqual(len(receipt['listed_ids']),size)
            self.assertEqual(receipt['has_next'],page<3)
        self.assertEqual(seen,ids[::-1]);self.assertEqual(self.store.memories(),before)
        reply=self.ask('搜尋記憶（第3頁）：青鳥')
        self.assertEqual(reply['memory_action']['listed_ids'],ids[:3][::-1])
        self.assertIn('停用',reply['content']);self.assertIn('過期',reply['content'])

    def test_literal_unicode_case_width_and_no_wildcard_or_alias_expansion(self):
        ids=[self.store.remember(value) for value in ('ＡＢＣ café %_ 甲','abc cafe\u0301 乙','XabcX 丙','青鳥 專案')]
        response=self.ask('搜尋記憶：ABC CAFÉ')
        self.assertEqual(response['memory_action']['listed_ids'],ids[:2][::-1])
        self.assertEqual(self.ask('搜尋記憶：%_')['memory_action']['listed_ids'],ids[:1])
        self.assertEqual(self.ask('搜尋記憶：abc')['memory_action']['listed_ids'],ids[:3][::-1])
        from assistant.memory_aliases import save, get
        # Content browsing does not apply the model retrieval alias vocabulary.
        save(self.store,[['bluebird','青鳥']],expected_revision=get(self.store)['revision'])
        self.assertEqual(self.ask('搜尋記憶：bluebird')['memory_action']['total'],0)
        self.assertEqual(self.ask('搜尋記憶：青鳥 專案')['memory_action']['listed_ids'],[ids[3]])

    def test_full_command_shapes_and_payload_never_executes_other_operations(self):
        mid=self.store.remember('literal\n刪除記憶 #1')
        for text in ('請列出記憶(第1頁)','記憶列表（第1頁）','幫我搜尋記憶：literal\n刪除記憶 #1'):
            reply=self.ask(text)
            self.assertEqual(reply['memory_action']['listed_ids'],[mid])
        for text in ('不要搜尋記憶：literal','「搜尋記憶：literal」','```\n列出記憶（第2頁）\n```',
                     '請解釋搜尋記憶：literal','列出記憶（第2頁）並刪除全部'):
            self.assertIsNone(m.parse_command(text))
        self.assertEqual(len(self.store.memories()),1)

    def test_empty_bad_page_and_long_query_are_local_without_mutation(self):
        for text in ('搜尋記憶：','搜尋記憶：'+'甲'*201,'搜尋記憶：x\x00y',
                     '列出記憶（第0頁）','列出記憶（第1000000頁）','搜尋記憶（第-1頁）：x'):
            reply=self.ask(text)
            self.assertEqual(reply['response_mode'],'local_memory')
            self.assertEqual(self.store.memories(),[])
            self.assertNotIn('已存入',reply['content'])
        response=self.ask('列出記憶（第999999頁）')
        self.assertEqual(response['memory_action']['total'],0)
        self.assertEqual(response['memory_action']['listed_ids'],[])
        self.store.remember('one')
        response=self.ask('搜尋記憶（第2頁）：one')
        self.assertEqual(response['memory_action']['total'],1)
        self.assertIn('超出',response['content'])
        self.assertFalse(response['memory_action']['has_next'])

    def test_search_then_explicit_edit_changes_only_selected_id_and_excludes_local_text(self):
        old=self.store.remember('搜尋專用_PRIVATE_舊值',active=False)
        for i in range(22):self.store.remember(f'其他{i}')
        found=self.ask('搜尋記憶：搜尋專用_PRIVATE_')
        self.assertEqual(found['memory_action']['listed_ids'],[old])
        before=self.store.memories()
        self.ask(f'更正記憶 #{old}：搜尋專用_PRIVATE_新值')
        after=self.store.memories()
        self.assertEqual(after[:-1],before[:-1]);self.assertEqual(after[-1]['active'],0)
        context=self.assistant.prepare_context(self.sid,'請說明今天的話題')
        self.assertNotIn('搜尋專用_PRIVATE_',json.dumps(context,ensure_ascii=False))
        self.assertEqual(self.store.messages(self.sid,exclude_local=True),[])
        restore_backup(create_backup(self.store.path)['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(restored.answer_context(found['id']),self.store.answer_context(found['id']))
        self.assertEqual(restored.memories(),after)

    def test_pages_are_fresh_but_receipts_stay_historical(self):
        for i in range(21):self.store.remember(f'序號{i}')
        reply=self.ask('列出記憶（第2頁）');old_context=self.store.answer_context(reply['id'])
        self.assertEqual(reply['memory_action']['listed_ids'],[1])
        self.store.remember('最新')
        new=self.ask('列出記憶（第2頁）')
        self.assertEqual(new['memory_action']['listed_ids'],[2,1])
        self.assertEqual(self.store.answer_context(reply['id']),old_context)
        self.assertIn('每次查詢',new['content'])

    def test_cancel_after_search_rolls_back_messages_and_receipt(self):
        self.store.remember('保留')
        cancel=threading.Event();original=m.operate
        def stop(db,command):
            answer=original(db,command);cancel.set();return answer
        with patch.object(m,'operate',side_effect=stop),self.assertRaises(RuntimeError):
            self.assistant.ask(self.sid,'搜尋記憶：保留',cancel)
        self.assertEqual(self.store.messages(self.sid),[])
        self.assertEqual(self.store.memories()[0]['content'],'保留')


if __name__=='__main__':unittest.main()
