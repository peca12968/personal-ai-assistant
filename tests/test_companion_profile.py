import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, companion_profile as p
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class CompanionProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def save(self, **changes):
        current = p.get_profile(self.store)
        return p.save_profile(self.store, {**current['settings'], **changes}, expected_revision=current['revision'])

    def test_default_is_not_user_confirmation_and_read_does_not_write(self):
        first = p.get_profile(self.store)
        self.assertEqual(first['source'], 'default')
        self.assertEqual(first['revision'], 0)
        first['settings']['assistant_name'] = '不能修改全域預設'
        self.assertEqual(p.get_profile(self.store)['settings'], p.DEFAULTS)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companion_profile').fetchone()[0], 0)

    def test_preferences_cross_sessions_and_answer_snapshot_stays_historical(self):
        saved = self.save(assistant_name='小禾', user_name='阿澄', notes='不要每次喊名字。')
        other = self.store.new_session()
        app = Assistant(Store(self.store.path), ScriptedEngine('已回答'))
        context = app.prepare_context(other, '這題用詳細步驟解釋，不要稱呼我。')
        self.assertEqual(context['companion_profile'], saved)
        result = app.ask(other, '2加3是多少？')
        self.assertEqual(self.store.answer_context(result['id'])['companion_profile'], saved)
        updated = self.save(assistant_name='小杉')
        self.assertEqual(app.prepare_context(self.sid, '目前名字？')['companion_profile'], updated)
        self.assertEqual(self.store.answer_context(result['id'])['companion_profile'], saved)

    def test_stale_editor_cannot_overwrite_save_or_reset(self):
        initial = self.save(assistant_name='小禾')
        latest = self.save(tone='direct')
        with self.assertRaisesRegex(ValueError, '重新載入'):
            p.save_profile(self.store, initial['settings'], expected_revision=initial['revision'])
        reset = p.reset_profile(self.store, expected_revision=latest['revision'])
        self.assertEqual(reset['revision'], latest['revision'] + 1)
        self.assertEqual(reset['settings'], p.DEFAULTS)
        with self.assertRaises(ValueError):
            p.save_profile(self.store, initial['settings'], expected_revision=0)
        self.assertEqual(p.get_profile(self.store), reset)

    def test_validation_rejects_unknown_fields_types_and_controls_without_writing(self):
        for changed in [{'assistant_name': ''}, {'assistant_name': 'x' * 41}, {'user_name': '名字\n指令'},
                        {'notes': 'x' * 1201}, {'notes': '\x00'}, {'notes': '\ud800'},
                        {'tone': []}, {'tone': 'anything'}, {'detail': True}, {'extra': 'new tool'}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                p.save_profile(self.store, {**p.DEFAULTS, **changed}, expected_revision=0)
        self.assertEqual(p.get_profile(self.store)['revision'], 0)
        self.assertEqual(p.validate({**p.DEFAULTS, 'notes': ' 行一\n\t行二 '})['notes'], '行一\n\t行二')

    def test_json_reader_rejects_duplicates_size_and_missing_fields(self):
        path = self.root / 'profile.json'
        for content in ['{"tone":"warm","tone":"direct"}', '{}', ' ' * 12001]:
            path.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                p.read_settings(path)
        path.write_text(json.dumps(p.DEFAULTS), encoding='utf-8-sig')
        self.assertEqual(p.read_settings(path), p.DEFAULTS)

    def test_autonomous_run_freezes_profile_and_resume_uses_current_without_old_profile_replay(self):
        first = self.save(assistant_name='FIRST_PRIVATE_NAME', notes='FIRST_PRIVATE_NOTE')
        gid = autonomy.create_goal(self.store, self.sid, '算出費用')
        def change_during_call():
            self.save(assistant_name='SECOND_NAME', notes='SECOND_NOTE')
            return action('calculate', operation='sum', values=['1', '2'])
        engine = ScriptedEngine(change_during_call, action('calculate', operation='product', values=['3', '4']))
        result = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=2)
        for prompt in engine.prompts:
            payload = json.loads(prompt.split('目標與已觀察資料：\n')[1])
            self.assertEqual(payload['companion_profile'], first)
        self.assertTrue(all(s['observation']['companion_profile'] == first for s in result['steps']))
        next_engine = ScriptedEngine(action('finish', report='12'))
        final = autonomy.run_goal(self.store, self.sid, gid, next_engine)
        self.assertNotIn('FIRST_PRIVATE_NAME', next_engine.prompts[0])
        self.assertNotIn('FIRST_PRIVATE_NOTE', next_engine.prompts[0])
        self.assertEqual(final['steps'][-1]['observation']['companion_profile']['revision'], 2)
        self.assertEqual(final['steps'][0]['observation']['companion_profile'], first)

    def test_chat_text_and_model_output_do_not_change_saved_profile(self):
        saved = self.save(assistant_name='小禾')
        result = Assistant(self.store, ScriptedEngine('我已永久改名小杉。')).ask(self.sid, '以後妳叫小杉。')
        self.assertEqual(p.get_profile(self.store), saved)
        self.assertEqual(self.store.answer_context(result['id'])['current_request'], '以後妳叫小杉。')
        # This tests host persistence only; model claims require live review.

    def test_backups_preserve_profile_and_missing_old_table_migrates_to_defaults(self):
        saved = self.save(assistant_name='小禾', support='listen_first')
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(p.get_profile(restored), saved)
        with restored.connect() as db:
            db.execute('DROP TABLE companion_profile')
        self.assertNotIn('companion_profile', validate_database(restored.path))
        legacy = create_backup(restored.path)
        restore_backup(legacy['path'], self.root / 'legacy')
        upgraded = Store(self.root / 'legacy' / 'assistant.sqlite3')
        self.assertEqual(p.get_profile(upgraded)['source'], 'default')
        self.assertEqual(p.get_profile(self.store), saved)

    def test_invalid_profile_blocks_backup(self):
        self.save()
        with self.store.connect() as db:
            db.execute("UPDATE companion_profile SET settings_json='{}' WHERE id=1")
        with self.assertRaises(ValueError):
            validate_database(self.store.path)

    def test_cli_profile_commands_do_not_initialize_engine(self):
        from assistant.__main__ import main
        output = io.StringIO()
        # The CLI reconfigures standard streams when available; StringIO has no reconfigure.
        with patch('assistant.__main__.CodexEngine', side_effect=AssertionError('must not start model')):
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--show-companion']), contextlib.redirect_stdout(output):
                main()
            self.assertEqual(json.loads(output.getvalue())['revision'], 0)
            settings = self.root / 'profile.json'
            settings.write_text(json.dumps({**p.DEFAULTS, 'assistant_name': '小禾'}), encoding='utf-8')
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--set-companion', str(settings), '--profile-revision', '0']), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(p.get_profile(self.store)['settings']['assistant_name'], '小禾')
            with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--reset-companion', '--profile-revision', '1']), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(p.get_profile(self.store)['revision'], 2)


if __name__ == '__main__':
    unittest.main()
