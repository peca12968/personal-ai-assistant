"""Reviewed duplicate-retrieval regressions, including real local context consumers."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy, memory_aliases
from assistant.acceptance import validate_suite, grade
from assistant.engine import Assistant
from assistant.retrieval import rank_memories
from assistant.store import Store
from scripts.observe_acceptance import observe
from test_autonomy import ScriptedEngine, action

ROOT = Path(__file__).resolve().parents[1]


class RetrievalRevisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def crowded(self):
        critical = self.store.remember('星橋禁止覆寫原件，必須先備份。')
        for _ in range(10):
            latest = self.store.remember('星橋工作使用Python。')
        return critical, latest

    def test_reviewed_self_revision_cases(self):
        suite = validate_suite(json.loads((ROOT / 'evals/retrieval_revisions.json').read_text(encoding='utf-8')))
        for result in grade(suite, observe(suite)):
            with self.subTest(case=result['id']):
                self.assertTrue(result['passed'], result)

    def test_chat_gets_previously_crowded_rule_without_deleting_duplicate_rows(self):
        critical, latest = self.crowded()
        originals = self.store.memories()
        app = Assistant(self.store, ScriptedEngine('依已保存內容，使用Python，先備份且不覆寫原件。'))
        answer = app.ask(self.sid, '星橋工作使用什麼語言，以及修改檔案時要注意什麼？只根據已保存記憶回答。')
        context = self.store.answer_context(answer['id'])
        self.assertEqual([r['id'] for r in context['memories']], [latest, critical])
        self.assertEqual(self.store.memories(), originals)
        # Original records are preserved; disabling later duplicates selects the next valid ID.
        self.store.set_memory_active(latest, False)
        next_rows = self.store.retrieve('星橋工作')
        self.assertEqual([r['id'] for r in next_rows], [latest - 1, critical])
        self.assertEqual(self.store.answer_context(answer['id']), context)

    def test_aliases_and_expiry_remain_applied_before_deduplication(self):
        critical, latest = self.crowded()
        self.store.remember('星橋工作使用Python。', active=False)
        self.store.remember('星橋禁止覆寫原件，必須先備份。', expires_on='2000-01-01')
        memory_aliases.save(self.store, [['星橋', 'StarBridge']], expected_revision=0)
        rows = self.store.retrieve('StarBridge')
        self.assertEqual([r['id'] for r in rows], [latest, critical])
        self.assertTrue(all(r['retrieval_aliases']['groups'] == [['星橋', 'StarBridge']] for r in rows))
        self.store.forget(critical)
        self.assertEqual([r['id'] for r in self.store.retrieve('StarBridge')], [latest])

    def test_original_objects_metadata_and_distinct_contradictions_survive(self):
        rows = [{'id': 9, 'kind': 'lesson', 'content': '  ＡＬＰＨＡ allow  ', 'source': 'latest'},
                {'id': 1, 'kind': 'fact', 'content': 'alpha deny', 'source': 'contradiction'},
                {'id': 8, 'kind': 'fact', 'content': 'alpha allow', 'source': 'older'}]
        before = copy.deepcopy(rows)
        result = rank_memories('alpha', rows, 2)
        self.assertEqual(rows, before)
        self.assertEqual([r['id'] for r in result], [9, 1])
        self.assertIs(result[0], rows[0])
        self.assertIs(result[1], rows[1])
        self.assertEqual([r['source'] for r in result], ['latest', 'contradiction'])

    def test_distinct_preference_reserve_and_full_limit_with_many_duplicates(self):
        for n in range(7):
            self.store.remember(f'alpha fact{n}')
        older = self.store.remember('warm tone', 'preference')
        for _ in range(12):
            latest = self.store.remember('brief replies', 'preference')
        rows = self.store.retrieve('alpha', 8)
        self.assertEqual(len(rows), 8)
        self.assertEqual([r['id'] for r in rows[-2:]], [latest, older])
        self.assertEqual([r['id'] for r in rows[:6]], [7, 6, 5, 4, 3, 2])
        self.assertEqual([r['id'] for r in self.store.retrieve('unrelated', 8)], [latest, older])

    def test_autonomous_recall_passes_both_original_facts_to_next_action(self):
        critical, latest = self.crowded()
        gid = autonomy.create_goal(self.store, self.sid, '核對星橋工作規則')
        engine = ScriptedEngine(action('recall', query='星橋工作'), action('finish', report='先備份，不覆寫原件。'))
        goal = autonomy.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual([r['id'] for r in goal['steps'][0]['observation']['memories']], [latest, critical])
        self.assertIn('星橋禁止覆寫原件，必須先備份。', engine.prompts[1])
        self.assertEqual(goal['status'], 'report_ready')


if __name__ == '__main__':
    unittest.main()
