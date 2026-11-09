import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from assistant import answer_runs
from assistant.backup import create_backup, restore_backup
from assistant.context import make_context
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting, daily_meeting_lookup as lookup
from scripts.daily_expenses import DailyAssistant


DATED = ('會議排程：2028-02-28：「王小晴」可開會：09:00-10:30，「李小安」為 09:10-10:30。'
         '2028-02-29：「王小晴」可開會：10:00-11:30，「李小安」為 10:10-11:30。'
         '兩人需要連續 20 分鐘。只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM。')
SAME_DAY = ('「王小晴」可開會：09:00-10:30，「李小安」為 09:10-10:30。'
            '兩人需要連續 20 分鐘。只輸出最早可行時段，格式 HH:MM-HH:MM。')
ZONED = ('跨時區會議：基準時區 UTC+08:00。候選日期：2026-11-09。'
         '「王小晴」 UTC+08:00：2026-11-09 09:00-11:00。'
         '「李小安」 UTC-08:00：2026-11-08 17:30-19:00。兩人需要連續 30 分鐘。'
         '只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM UTC±HH:MM。')
QUERY = '查會議紀錄，日期 2028-02-29，參與者「王小晴」。'
RANGE_QUERY = '查會議紀錄，日期範圍 2028-02-29 到 2028-03-01，參與者「王小晴」。'


class DailyMeetingLookupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root/'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '一般回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_query_lists_both_verified_versions_and_a_listed_source_can_be_used(self):
        first = self.app.ask(self.sid, DATED)
        second = self.app.ask(self.sid, '上一場會議，2028-02-28 「李小安」的空檔改為無空檔，重算。')
        self.app.ask(self.sid, SAME_DAY)
        foreign = self.store.new_session()
        self.app.ask(foreign, DATED)
        result = self.app.ask(self.sid, QUERY)
        proof = lookup.read(self.store, result['id'])
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']], [second['id'], first['id']])
        self.assertEqual([item['result'] for item in proof['plan']['matches']],
                         ['2028-02-29 10:10-10:30', '2028-02-28 09:10-09:30'])
        self.assertIn(f'#{first["id"]}｜王小晴、李小安', result['content'])
        changed = self.app.ask(self.sid, f'沿用會議回答 #{first["id"]}，會議改半小時，重算。')
        self.assertEqual(changed['content'], '2028-02-28 09:10-09:40')
        self.assertEqual(meeting.read(self.store, changed['id'])['meeting_source']['answer_id'], first['id'])
        self.assertEqual(lookup.read(self.store, result['id']), proof)
        self.engine.answer.assert_not_called()

    def test_exact_people_and_reference_dates_exclude_pending_and_undated_answers(self):
        zoned = self.app.ask(self.sid, ZONED)
        undated = self.app.ask(self.sid, SAME_DAY)
        self.app.ask(self.sid, SAME_DAY.replace('王小晴', '王小安'))
        self.app.ask(self.sid, DATED)
        pending = self.app.ask(self.sid, '上一場會議，王小晴 09:10-10:00 沒空，重算。')
        self.assertTrue(meeting.pending_date(meeting.read(self.store, pending['id'])))
        result = self.app.ask(self.sid, '找會議紀錄，基準日期 2026-11-09，參與者李小安。')
        proof = lookup.read(self.store, result['id'])
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']], [zoned['id']])
        self.assertNotIn(pending['id'], [item['answer_id'] for item in proof['plan']['checked']])
        local = self.app.ask(self.sid, '查會議紀錄，日期 2026-11-08，參與者「李小安」。')
        self.assertEqual(lookup.read(self.store, local['id'])['plan']['matches'], [])
        by_name = self.app.ask(self.sid, '查會議紀錄，參與者「王小晴」及「李小安」。')
        self.assertIn(f'#{undated["id"]}｜王小晴、李小安｜未記錄日期', by_name['content'])
        self.assertEqual(lookup.parse('查會議紀錄，參與者「和一」與「甲」。')['attendees'], ['和一','甲'])
        self.engine.answer.assert_not_called()

    def test_lookup_retries_and_restores_with_the_same_sources_and_refuses_source_changes(self):
        first = self.app.ask(self.sid, DATED)
        stop = Event()
        begin = answer_runs.begin

        def begin_then_stop(*args, **kwargs):
            uid = begin(*args, **kwargs)
            stop.set()
            return uid

        with patch.object(answer_runs, 'begin', side_effect=begin_then_stop), self.assertRaises(EngineError):
            self.app.ask(self.sid, RANGE_QUERY, stop)
        result = self.app.retry(self.sid)
        proof = lookup.read(self.store, result['id'])
        self.assertEqual(proof['plan']['matches'][0]['answer_id'], first['id'])
        self.assertEqual(proof['plan']['matches'][0]['matched_dates'], ['2028-02-29'])
        bundle = create_backup(self.store.path, self.root/'lookup.paibackup')
        restore_backup(bundle['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(lookup.read(restored, result['id']), proof)
        context = meeting.read(restored, first['id'])
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False, indent=2), first['id']))
        self.assertEqual(meeting.read(restored, first['id']), context)
        with self.assertRaisesRegex(ValueError, '原始來源'):
            lookup.read(restored, result['id'])
        self.engine.answer.assert_not_called()

    def test_scan_limits_invalid_dates_and_ordinary_attachment_routes(self):
        first = self.app.ask(self.sid, DATED)
        second = self.app.ask(self.sid, DATED)
        self.app.ask(self.sid, DATED)
        with patch.object(lookup, 'MAX_SCAN', 2), patch.object(lookup, 'MAX_RESULTS', 1):
            result = self.app.ask(self.sid, QUERY)
            proof = lookup.read(self.store, result['id'])
            self.assertNotIn(first['id'], [item['answer_id'] for item in proof['plan']['checked']])
            self.assertNotIn(f'#{second["id"]}｜', result['content'])
            self.assertTrue(proof['plan']['limited'])
            self.assertIn('最近 2 筆', result['content'])
        with self.assertRaisesRegex(ValueError, '有效的完整日期'):
            self.app.ask(self.sid, '查會議紀錄，日期 2027-02-29。')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.sid, QUERY)['content'], '一般回答')
        notes = self.root/'notes.txt'
        notes.write_text('參考事項', encoding='utf-8')
        self.store.attach_document(self.sid, notes)
        self.assertEqual(self.app.ask(self.sid, QUERY)['content'], '一般回答')
        self.assertIsNone(lookup.parse(QUERY + '然後通知所有人。'))

    def test_a_corrupt_meeting_is_reported_and_does_not_hide_other_matches(self):
        first = self.app.ask(self.sid, DATED)
        corrupt = self.app.ask(self.sid, DATED)
        with self.store.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('不符的時段', corrupt['id']))
        result = self.app.ask(self.sid, QUERY)
        proof = lookup.read(self.store, result['id'])
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']], [first['id']])
        self.assertEqual(proof['plan']['checked'][0], dict(answer_id=corrupt['id'], sha256=None))
        self.assertIn('有 1 筆保存的會議回答無法核對。', result['content'])
        self.engine.answer.assert_not_called()

    def test_topic_filter_is_exact_and_combines_with_dates_and_people(self):
        title = '採購，風險；檢討'
        selected = self.app.ask(self.sid, f'會議主題：「{title}」。' + DATED)
        self.app.ask(self.sid, f'會議主題：「{title}補充」。' + DATED)
        self.app.ask(self.sid, DATED)
        result = self.app.ask(self.sid, f'查會議紀錄，主題「{title}」，日期 2028-02-29，參與者「王小晴」。')
        proof = lookup.read(self.store, result['id'])
        self.assertEqual(proof['plan']['schema'], lookup.PLAN_V4)
        self.assertEqual(proof['plan']['criteria']['topic_mode'], 'exact')
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']], [selected['id']])
        self.assertIn(f'#{selected["id"]}｜「{title}」', result['content'])
        self.assertIn(f'會議主題「{title}」', meeting.describe(meeting.read(self.store, selected['id'])))
        self.assertEqual(meeting.recent_completed(self.store, self.sid)[-1]['topic'], title)
        unmatched = self.app.ask(self.sid, '查會議紀錄，主題「採購」。')
        self.assertEqual(lookup.read(self.store, unmatched['id'])['plan']['matches'], [])
        self.engine.answer.assert_not_called()

    def test_older_answer_can_be_labeled_and_relabeling_creates_a_separate_version(self):
        first = self.app.ask(self.sid, DATED)
        original = meeting.read(self.store, first['id'])
        self.app.ask(self.sid, '計算 1 + 1，只回答整數')
        titled = self.app.ask(self.sid, f'沿用會議回答 #{first["id"]}，會議主題改為「驗收。進度」，重算。')
        retitled = self.app.ask(self.sid, '上一場會議，主題改成「交接」；時長改為 25 分鐘，重算。')
        self.assertEqual(titled['content'], first['content'])
        self.assertEqual(retitled['content'], '2028-02-28 09:10-09:35')
        snapshots = {first['id']: original}
        for answer, name in ((titled, '驗收。進度'), (retitled, '交接')):
            record = meeting.read(self.store, answer['id'])
            self.assertEqual(record['meeting_plan']['topic'], name)
            snapshots[answer['id']] = record
            search = self.app.ask(self.sid, f'查會議紀錄，主題「{name}」。')
            self.assertEqual([item['answer_id'] for item in lookup.read(self.store, search['id'])['plan']['matches']],
                             [answer['id']])
        with self.assertRaisesRegex(ValueError, '只能指定一次會議主題'):
            self.app.ask(self.sid, f'沿用會議回答 #{titled["id"]}，主題改為「甲」，主題改為「乙」，重算。')
        bundle = create_backup(self.store.path, self.root/'topic.paibackup')
        restore_backup(bundle['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual({mid: meeting.read(restored, mid) for mid in snapshots}, snapshots)
        self.engine.answer.assert_not_called()

    def test_disjoint_ranges_return_each_version_once_with_only_qualifying_candidate_dates(self):
        tag = '會議主題：「年度盤點」。'
        first = self.app.ask(self.sid, tag + DATED)
        later_dates = DATED.replace('2028-02-28', '2028-03-01').replace('2028-02-29', '2028-03-02')
        second = self.app.ask(self.sid, tag + later_dates)
        self.app.ask(self.sid, tag + SAME_DAY)
        self.app.ask(self.sid, tag + DATED.replace('王小晴', '王小安'))
        self.app.ask(self.sid, tag.replace('盤點', '盤點補充') + DATED)
        self.app.ask(self.store.new_session(), tag + DATED)
        result = self.app.ask(self.sid, '查會議紀錄，日期 2028-02-29 到 2028-02-29 或 2028-03-02～2028-03-03'
                                       ' 或 2028-03-02 至 2028-03-02，主題「年度盤點」，參與者王小晴。')
        matches = lookup.read(self.store, result['id'])['plan']['matches']
        self.assertEqual([(item['answer_id'], item['matched_dates']) for item in matches],
                         [(second['id'], ['2028-03-02']), (first['id'], ['2028-02-29'])])
        self.assertIn(f'#{first["id"]}｜「年度盤點」｜王小晴、李小安｜2028-02-29｜2028-02-28 09:10-09:30',
                      result['content'])
        self.assertEqual(matches[0]['dates'], ['2028-03-01', '2028-03-02'])
        self.engine.answer.assert_not_called()

    def test_range_validation_and_non_search_requests_keep_their_routes(self):
        for phrase in ('2027-02-29 到 2027-03-01', '2028-03-02 到 2028-03-01'):
            with self.subTest(phrase=phrase), self.assertRaisesRegex(ValueError, '有效的完整日期|起日必須'):
                self.app.ask(self.sid, f'查會議紀錄，日期範圍 {phrase}。')
        self.assertIsNone(lookup.parse(RANGE_QUERY, schema=lookup.PLAN_V2))
        self.assertIsNone(lookup.parse(RANGE_QUERY[:-1] + '，日期 2028-02-28。'))
        self.assertIsNone(lookup.parse(RANGE_QUERY[:-1] + '，然後通知所有人。'))
        self.assertIsNone(lookup.parse('查會議紀錄，日期 ' + ' 或 '.join(['2028-02-29 到 2028-03-01'] * 4) + '。'))
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.sid, RANGE_QUERY)['content'], '一般回答')
        notes = self.root/'notes.txt'
        notes.write_text('參考事項', encoding='utf-8')
        self.store.attach_document(self.sid, notes)
        self.assertEqual(self.app.ask(self.sid, RANGE_QUERY)['content'], '一般回答')

    def test_topic_fragment_lists_versions_without_choosing_the_correction_source(self):
        other = self.app.ask(self.sid, '會議主題：「跨部門盤點」。' + SAME_DAY)
        original = self.app.ask(self.sid, '會議主題：「月末盤點」。' + SAME_DAY)
        revised = self.app.ask(self.sid, '上一場會議，時長改為 25 分鐘，重算。')
        self.app.ask(self.sid, '會議主題：「月末清點」。' + SAME_DAY)
        self.app.ask(self.sid, SAME_DAY)
        self.app.ask(self.store.new_session(), '會議主題：「月末盤點」。' + SAME_DAY)
        result = self.app.ask(self.sid, '查會議紀錄，主題包含「盤點」。')
        proof = lookup.read(self.store, result['id'])
        self.assertEqual(proof['plan']['criteria']['topic_mode'], 'contains')
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']],
                         [revised['id'], original['id'], other['id']])
        self.assertIn('找到 3 筆', result['content'])
        self.assertIn(f'#{original["id"]}｜「月末盤點」', result['content'])
        changed = self.app.ask(self.sid, f'沿用會議回答 #{original["id"]}，時長改為 30 分鐘，重算。')
        self.assertEqual(changed['content'], '09:10-09:40')
        self.assertEqual(meeting.read(self.store, changed['id'])['meeting_source']['answer_id'], original['id'])
        self.assertEqual(lookup.read(self.store, result['id']), proof)
        self.engine.answer.assert_not_called()

    def test_topic_fragment_respects_literal_text_and_combines_with_range_and_people(self):
        topic = '採購，ＡＢＣ；驗收'
        selected = self.app.ask(self.sid, f'會議主題：「{topic}」。' + DATED)
        self.app.ask(self.sid, f'會議主題：「{topic.replace("ＡＢＣ", "ABC")}」。' + DATED)
        self.app.ask(self.sid, f'會議主題：「{topic}」。' + SAME_DAY)
        self.app.ask(self.sid, f'會議主題：「{topic}」。' + DATED.replace('王小晴', '王小安'))
        query = RANGE_QUERY[:-1] + '，主題含「，ＡＢＣ；」。'
        result = self.app.ask(self.sid, query)
        proof = lookup.read(self.store, result['id'])
        self.assertEqual([item['answer_id'] for item in proof['plan']['matches']], [selected['id']])
        self.assertEqual(proof['plan']['matches'][0]['matched_dates'], ['2028-02-29'])
        self.assertIn(f'#{selected["id"]}｜「{topic}」', result['content'])
        self.assertIsNone(lookup.parse(query, schema=lookup.PLAN_V3))
        self.assertIsNone(lookup.parse('查會議紀錄，主題包含「」。'))
        self.assertIsNone(lookup.parse(query[:-1] + '，主題「採購」。'))
        self.assertIsNone(lookup.parse(query[:-1] + '，然後通知所有人。'))
        self.engine.answer.assert_not_called()

    def test_legacy_lookup_retry_replays_its_original_schema(self):
        def stop(_):
            raise EngineError('停止', 'cancelled')

        first = self.app.ask(self.sid, '會議主題：「盤點」。' + DATED)
        for schema, query in ((lookup.PLAN_V1, QUERY), (lookup.PLAN_V2, '查會議紀錄，主題「盤點」。'),
                              (lookup.PLAN_V3, RANGE_QUERY)):
            with self.subTest(schema=schema):
                plan = lookup.capture(self.store, self.sid, query, schema=schema)
                context = make_context(query, [], [])
                context.update(response_mode=lookup.MODE, documents={'catalog': [], 'excerpts': []},
                               **{lookup.FIELD: plan})
                with self.assertRaises(EngineError):
                    self.app._run_recorded(self.sid, query, context, stop, None, None, None, lookup.FIELD)
                retried = self.app.retry(self.sid)
                proof = lookup.read(self.store, retried['id'])
                self.assertEqual(proof['plan'], plan)
                self.assertEqual(proof['schema'], lookup.RECEIPTS[schema])
                self.assertNotIn('topic_mode', proof['plan']['criteria'])
                self.assertEqual('date_ranges' in proof['plan']['criteria'], schema == lookup.PLAN_V3)
                self.assertEqual('topic' in proof['plan']['criteria'], schema != lookup.PLAN_V1)
                self.assertEqual(proof['plan']['matches'][0]['answer_id'], first['id'])
        self.engine.answer.assert_not_called()

