import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import answer_runs
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_criteria as options
from scripts.daily_expenses import DailyAssistant

QUERY = ('要挑背包。白袋：90 元、4 公斤；棕袋：130 元、2 公斤；灰袋：150 元、1 公斤。'
         '預算最多 100 元，重量最多 4 公斤。合格者挑最輕。只回答名稱。')
CHANGE = '只把預算上限改成 135 元，其餘照舊。只回答名稱。'


class OptionTests(unittest.TestCase):
    def test_revise_filter_preserve_ranking_then_report_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3')
            app=DailyAssistant(store,None)
            sid=store.new_session()
            first=app.ask(sid,QUERY)
            next_answer=app.ask(sid,CHANGE)
            last=app.ask(sid,'更正，只把重量上限改為 1 公斤，其他條件不變。沒有符合的只回答「無」。')
            self.assertEqual([r['content'] for r in (first,next_answer,last)], ['白袋','棕袋','無'])
            ctx=store.answer_context(last['id'])
            self.assertEqual(ctx['option_source']['answer_id'],next_answer['id'])
            self.assertEqual(ctx['calculation']['checks'][-1]['rejected'],['元'])
            self.assertFalse(ctx['calculation']['model_called'])
            self.assertEqual(store.answer_context(first['id'])['option_plan']['limits']['元']['value'],100)

    def test_ambiguous_change_asks_without_modifying_limits_and_can_be_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3')
            app=DailyAssistant(store,None)
            sid=store.new_session()
            first=app.ask(sid,QUERY)
            question=app.ask(sid,'更正，把上限調低一點，其餘照舊。')
            self.assertIn('新的數值和單位',question['content'])
            self.assertIn('預算最多 100 元',question['content'])
            self.assertIn('重量最多 4 公斤',question['content'])
            self.assertEqual(store.answer_context(first['id'])['option_plan'], store.answer_context(question['id'])['option_plan'])
            self.assertEqual(app.ask(sid,CHANGE)['content'],'棕袋')

    def test_full_grammar_and_equal_ranks(self):
        q='我要選方案。松包：100 元、2 天、可冷藏；石包：100 元、2 天、可冷藏；雨包：80 元、1 天、不可冷藏。必須冷藏，預算最多 110 元。合格者挑最快，同速挑便宜。只回答方案名稱。'
        self.assertEqual(options.resolve(options.parse(q))[0],'松包、石包')
        for invalid in (q.replace('必須冷藏','除非可以免冷藏'),q.replace('2 天','約 2 天'),
                        q.replace('1 天、不可冷藏','1 天'),q.replace('100 元','每人 100 元',1),
                        q.replace('100 元','100 美元',1),q.replace('松包：','石包：'),q+'幫我下單。',
                        q.replace('我要選方案','我要選不含石包的方案'),q.replace('110 元','110 天')):
            self.assertIsNone(options.parse(invalid))
        for invalid in ('以下是範例：'+CHANGE, CHANGE+'並寄信。','只把座位和預算改為 9，其他條件不變。'):
            self.assertIsNone(options.revision(invalid))

    def test_original_rows_survive_generated_price_and_deadline_changes(self):
        import random
        rng=random.Random(612)
        for _ in range(90):
            items=[(n,rng.randint(1,8),rng.randrange(4),rng.choice([True,False])) for n in '甲乙丙']
            terms='；'.join(f"{n}：{cost} 元、{str(days)+' 天' if days else '當天送達'}、{'可' if cold else '不可'}冷藏"
                           for n,cost,days,cold in items)
            plan=options.parse('我要選配送方案。'+terms+'。預算最多 5 元，必須冷藏且 3 天內送達。合格者挑最快，同速挑便宜。只回答名稱。')
            cost_limit=rng.randint(1,8); day_limit=rng.randrange(4)
            for field,val in [('預算上限',f'{cost_limit} 元'),('送達期限',f'{day_limit} 天')]:
                self.assertIsNone(options.revise(plan,options.revision(f'只把{field}改為 {val}，其他條件不變。')))
            valid=[(n,days,cost) for n,cost,days,cold in items if cold and cost<=cost_limit and days<=day_limit]
            best=min((v[1:] for v in valid),default=None)
            expected='、'.join(n for n,days,cost in valid if (days,cost)==best) or '無符合方案'
            self.assertEqual(options.resolve(plan)[0],expected)

    def test_competing_message_cannot_rebase_a_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3')
            app=DailyAssistant(store,None); sid=store.new_session()
            app.ask(sid,QUERY)
            begin=answer_runs.begin
            def raced(*args, **kwargs):
                store.add_message(sid,'user','先討論另一件事。')
                return begin(*args, **kwargs)
            with patch.object(answer_runs,'begin',side_effect=raced), self.assertRaisesRegex(ValueError,'提交前'):
                app.ask(sid,CHANGE)
            self.assertEqual(answer_runs.runs(store,sid,1)[0]['status'],'failed')
            with self.assertRaisesRegex(ValueError,'核對原先'):app.retry(sid)

    def test_relative_and_absolute_followups_chain_from_latest_accepted_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None)
            sid = store.new_session()
            first = app.ask(sid, QUERY)
            for question, before, after, kind, winner in [
                    ('預算再加 35 元。', 100, 135, 'relative', '棕袋'),
                    ('預算再降 20 元。', 135, 115, 'relative', '白袋'),
                    ('預算降到 20 元。', 115, 20, 'absolute', '無符合方案'),
                    ('只把預算上限提高至 150 元，其他條件不變。', 20, 150, 'absolute', '灰袋')]:
                reply = app.ask(sid, question)
                ctx = store.answer_context(reply['id'])
                change = ctx['option_plan']['last_adjustment']
                self.assertEqual((change['before'], change['after'], change['mode']), (before, after, kind))
                self.assertEqual(ctx['option_plan']['limits']['公斤']['value'], 4)
                self.assertEqual(reply['content'], winner)
            self.assertEqual(store.answer_context(first['id'])['option_plan']['limits']['元']['value'],100)

    def test_adjustment_boundaries_do_not_guess_or_round(self):
        import copy
        plan = options.parse(QUERY)
        for q in ('預算降到 160 元。', '預算提高至 90 元。', '預算再降 101 元。', '預算增加 20 公斤。',
                  '預算上限降低一點。', '預算再降 20。', '預算再降一成。', '預算再降 20%。', '只把預算上限調低 20%，其餘照舊。', '預算再加 9999999 元。'):
            previous = copy.deepcopy(plan)
            self.assertTrue(options.revise(plan, options.revision(q)))
            self.assertEqual(plan, previous)
        for q in ('甲的價格降 20 元。', '他說「預算再降 20 元」。', '若預算再降 20 元。',
                  '預算再降 20 元，順便下單。', '想知道預算降 20 元的意思。'):
            self.assertIsNone(options.revision(q))
        self.assertIsNone(options.revise(plan, options.revision('預算降低 100 元。')))
        self.assertEqual(plan['limits']['元']['value'], 0)
        seats = options.parse('要挑自習室。甲：8 席、30 元；乙：12 席、50 元。人數至少 8 席。合格者挑最便宜。只回答名稱。')
        self.assertIsNone(options.revise(seats, options.revision('席數下限增加 2 席。')))
        self.assertEqual(options.resolve(seats)[0], '乙')

    def test_retry_applies_a_relative_change_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None)
            sid = store.new_session()
            origin = app.ask(sid, QUERY)
            with patch.object(options, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, '預算再加 35 元。')
            result = app.retry(sid)
            ctx = store.answer_context(result['id'])
            self.assertEqual(ctx['option_source']['answer_id'], origin['id'])
            self.assertEqual(ctx['option_plan']['last_adjustment']['after'], 135)
            self.assertEqual(len(store.messages(sid, 10)), 4)

    def test_retry_source_integrity_and_model_only(self):
        class Raw:
            def answer(self,prompt,cancel=None):
                return '原始引擎'
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3')
            app=DailyAssistant(store,Raw())
            sid=store.new_session()
            first=app.ask(sid,QUERY)
            with patch.object(options,'resolve',side_effect=EngineError('停止','cancelled')), self.assertRaises(EngineError):
                app.ask(sid,CHANGE)
            self.assertEqual(app.retry(sid)['content'],'棕袋')
            self.assertEqual(len(store.messages(sid,10)),4)
            other=store.new_session()
            self.assertEqual(app.ask(other,CHANGE)['content'],'原始引擎')
            pure=DailyAssistant(store,Raw(),allow_local_calculation=False)
            self.assertEqual(pure.ask(other,QUERY)['content'],'原始引擎')
            latest=store.messages(sid,1)[0]['id']
            with store.connect() as db:
                db.execute('UPDATE messages SET content=? WHERE id=?',('改過的結果',latest))
            with self.assertRaisesRegex(ValueError,'已變動'):app.ask(sid,CHANGE)
            store.add_message(sid,'user','新的主題')
            self.assertEqual(app.ask(sid,CHANGE)['content'],'原始引擎')
