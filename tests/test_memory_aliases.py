import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import memory_aliases as m, autonomy
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class MemoryAliasesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def save(self, groups):
        return m.save(self.store, groups, expected_revision=m.get(self.store)['revision'])

    def test_default_read_is_empty_and_does_not_write(self):
        self.assertEqual(m.get(self.store), {'source': 'default', 'revision': 0, 'groups': [], 'updated_at': None})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_aliases').fetchone()[0], 0)
        self.assertEqual(self.store.retrieve('unknown'), [])

    def test_bilingual_names_retrieve_original_with_only_matched_provenance(self):
        mid = self.store.remember(r'星橋素材位於 F:\Work\SB。')
        other = self.store.remember('青鳥素材位於另一顆磁碟。')
        originals = self.store.memories()
        self.assertEqual(self.store.retrieve('Where are StarBridge files?'), [])
        self.save([['星橋', 'StarBridge', '小星'], ['茶壺', 'PRIVATE_UNMATCHED_ALIAS']])
        row = self.store.retrieve('Where are StarBridge files?', 1)[0]
        self.assertEqual(row['id'], mid)
        self.assertEqual(row['content'], originals[1]['content'])
        self.assertEqual(row['retrieval_aliases']['groups'], [['星橋', 'StarBridge', '小星']])
        self.assertNotIn('PRIVATE_UNMATCHED_ALIAS', json.dumps(row))
        self.assertEqual(self.store.retrieve('小星資料在哪？', 1)[0]['id'], mid)
        self.assertEqual(self.store.memories(), originals)
        self.assertNotIn(other, [r['id'] for r in self.store.retrieve('StarBridge')])

    def test_full_words_normalization_phrases_and_no_transitive_guessing(self):
        self.store.remember('藍月計畫已保存。')
        self.save([['藍月', 'Blue Moon', 'BLUE.M']])
        self.assertTrue(self.store.retrieve('ＢＬＵＥ ＭＯＯＮ'))
        self.assertTrue(self.store.retrieve('What about blue.m?'))
        for query in ['Blue Moons', 'MyBlue Moon', 'blue-moon', 'BLUEAM', 'Moon', '星月']:
            with self.subTest(query=query):
                self.assertEqual(self.store.retrieve(query), [])
        # A configured nickname must not trigger inside an unrelated Latin word.
        self.save([['藍月', 'cat']])
        self.assertEqual(self.store.retrieve('concatenate'), [])

    def test_empty_reset_stale_revision_and_no_aba_overwrite(self):
        groups = [['星橋', 'StarBridge']]
        first = self.save(groups)
        groups[0][0] = 'caller mutation'
        self.assertEqual(m.get(self.store)['groups'][0][0], '星橋')
        reset = self.save([])
        self.assertEqual(reset['revision'], first['revision'] + 1)
        self.assertEqual(reset['source'], 'user_saved')
        with self.assertRaisesRegex(ValueError, '重新載入'):
            m.save(self.store, [['星橋', 'stale']], expected_revision=first['revision'])
        with self.assertRaises(ValueError):
            m.save(self.store, [], expected_revision=True)
        self.assertEqual(m.get(self.store), reset)

    def test_validation_limits_duplicates_overlap_and_controls(self):
        invalid = [None, {}, [['星橋']], [['星橋', 'StarBridge']] * 9,
                   [['a' + str(i) for i in range(7)]], [['x', 'two']], [['x' * 65, 'two']],
                   [['--', 'two']], [['foo', 'ＦＯＯ']], [['星橋', 'foo'], ['foo', 'other']],
                   [['星橋', 'line\nbreak']], [['星橋', 'bad|term']], [['星橋', '\ud800bad']],
                   [['星橋', None]], [['星橋', 'nul\x00']], [['星橋', 'tab\t']], [['星橋', 'del\x7f']]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save(value)
        self.assertEqual(m.get(self.store)['revision'], 0)
        valid = [[f'group{i}word{j}' for j in range(6)] for i in range(8)]
        self.assertEqual(self.save(valid)['groups'], valid)

    def test_expired_disabled_deleted_and_limited_memories_stay_excluded(self):
        active = self.store.remember('星橋資料是現行版。')
        disabled = self.store.remember('星橋停用的舊資料。', active=False)
        expired = self.store.remember('星橋過期的舊資料。', expires_on='2000-01-01')
        deleted = self.store.remember('星橋刪除的舊資料。')
        self.store.forget(deleted)
        self.save([['星橋', 'StarBridge']])
        self.assertEqual([r['id'] for r in self.store.retrieve('StarBridge')], [active])
        self.assertEqual(self.store.retrieve('StarBridge', 0), [])
        self.store.set_memory_active(active, False)
        self.assertEqual(self.store.retrieve('StarBridge'), [])

    def test_new_chats_share_configuration_and_old_answer_snapshots_survive_reset(self):
        self.store.remember('星橋素材放在F槽。')
        self.save([['星橋', 'StarBridge']])
        app = Assistant(self.store, ScriptedEngine('素材放在F槽。'))
        other = self.store.new_session()
        answer = app.ask(other, 'StarBridge')
        historical = self.store.answer_context(answer['id'])
        self.assertEqual(historical['memories'][0]['retrieval_aliases']['revision'], 1)
        self.save([])
        self.assertEqual(app.prepare_context(self.sid, 'StarBridge')['memories'], [])
        self.assertEqual(self.store.answer_context(answer['id']), historical)
        self.assertEqual(self.store.memories()[0]['content'], '星橋素材放在F槽。')

    def test_autonomous_recall_records_snapshot_and_uses_current_next_time(self):
        self.store.remember('星橋素材放在F槽。')
        self.save([['星橋', 'StarBridge']])
        gid = autonomy.create_goal(self.store, self.sid, '核對素材位置')
        first = autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(action('recall', query='StarBridge')), max_steps=1)
        self.assertEqual(first['steps'][0]['observation']['memories'][0]['retrieval_aliases']['revision'], 1)
        self.save([])
        engine = ScriptedEngine(action('recall', query='StarBridge'))
        after = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=1)
        self.assertEqual(after['steps'][1]['observation']['memories'], [])
        self.assertEqual(after['steps'][0], first['steps'][0])
        self.assertIn('舊回答或工具紀錄中的稱呼匹配是歷史', engine.prompts[0])

    def test_chat_or_model_cannot_write_alias_settings(self):
        before = self.save([['星橋', 'StarBridge']])
        Assistant(self.store, ScriptedEngine('我已設定新別名。')).ask(self.sid, '以後Blue Moon叫星橋，永久記住搜尋用法。')
        self.assertEqual(m.get(self.store), before)

    def test_backup_roundtrip_legacy_migration_and_corrupt_json(self):
        saved = self.save([['星橋', 'StarBridge']])
        self.store.remember('星橋素材保留。')
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(m.get(restored), saved)
        self.assertTrue(restored.retrieve('StarBridge'))
        with restored.connect() as db:
            db.execute('DROP TABLE memory_aliases')
        self.assertNotIn('memory_aliases', validate_database(restored.path))
        legacy = create_backup(restored.path)
        restore_backup(legacy['path'], self.root / 'legacy')
        self.assertEqual(m.get(Store(self.root / 'legacy' / 'assistant.sqlite3'))['groups'], [])
        with self.store.connect() as db:
            db.execute("UPDATE memory_aliases SET groups_json='{}'")
        with self.assertRaises(ValueError):
            validate_database(self.store.path)

    def test_saved_vocabulary_is_atomic_on_write_failure(self):
        before = self.save([['星橋', 'StarBridge']])
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_alias BEFORE UPDATE ON memory_aliases BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.save([['青鳥', 'Bluebird']])
        self.assertEqual(m.get(self.store), before)

    def test_cli_and_file_validation_do_not_start_model(self):
        from assistant.__main__ import main
        path = self.root / 'aliases.json'
        path.write_text(json.dumps([['星橋', 'StarBridge']]), encoding='utf-8-sig')
        with patch('assistant.__main__.CodexEngine', side_effect=AssertionError('no model')):
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--show-memory-aliases']), contextlib.redirect_stdout(io.StringIO()) as output:
                main()
            self.assertEqual(json.loads(output.getvalue())['revision'], 0)
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--set-memory-aliases', str(path), '--aliases-revision', '0']), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(m.get(self.store)['groups'], [['星橋', 'StarBridge']])
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--set-memory-aliases', str(path)]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()
        for content in [' ' * 12001, '{}', '[ ["one","one"] ]', '{"a":1,"a":2}']:
            path.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                m.read_file(path)


if __name__ == '__main__':
    unittest.main()
