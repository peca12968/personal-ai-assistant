import copy
import json
import tempfile
import threading
import unittest
from decimal import Decimal, Context, localcontext, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN, Inexact
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from assistant import autonomy as a, number_rounding as m
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def args(value,places=2,mode='half_up'):return {'value':value,'places':places,'mode':mode}


class NumberRoundingTests(unittest.TestCase):
    def test_exact_quote_difference_fixed_zeros_and_no_negative_zero(self):
        r=m.calculate(args('1132887/200'))
        self.assertEqual((r['input_exact'],r['exact'],r['fixed_decimal'],r['difference_exact']),('1132887/200','141611/25','5664.44','1/200'))
        self.assertTrue(r['value_changed']);self.assertTrue(r['is_tie'])
        r=m.calculate(args('3/2',4));self.assertEqual(r['fixed_decimal'],'1.5000');self.assertFalse(r['value_changed'])
        for value in ('-0','-0.0001','0/7'):
            r=m.calculate(args(value));self.assertEqual(r['fixed_decimal'],'0.00');self.assertEqual(r['exact'],'0')
        self.assertEqual(m.calculate(args('12',0))['fixed_decimal'],'12')

    def test_all_signed_modes_against_independent_decimal_quantize(self):
        modes={'half_even':ROUND_HALF_EVEN,'half_up':ROUND_HALF_UP,'floor':ROUND_FLOOR,'ceiling':ROUND_CEILING,'truncate':ROUND_DOWN}
        with localcontext(Context(prec=60)):
            for value in ('-2.355','-2.345','2.345','2.355','-0.005','0.005','-0.006','0.004','9.995','-9.995','2.5','3.5','-2.5','-3.5','0'):
                for places in (0,2,12):
                    for mode,rounding in modes.items():
                        with self.subTest(value=value,places=places,mode=mode):
                            r=m.calculate(args(value,places,mode));expected=Decimal(value).quantize(Decimal(1).scaleb(-places),rounding=rounding)
                            self.assertEqual(Fraction(r['exact']),Fraction(expected))
                            self.assertEqual(Fraction(r['fixed_decimal']),Fraction(expected))
                            self.assertEqual(Fraction(r['difference_exact']),Fraction(expected)-Fraction(value))
                            self.assertEqual(r['value_changed'],Fraction(value)!=Fraction(expected))
                            self.assertEqual(len(r['fixed_decimal'].split('.')[1]) if places else 0,places)

    def test_avoids_preview_double_rounding_and_ambient_context(self):
        value='2.674'+'9'*64
        with localcontext() as ctx:
            ctx.prec=2;ctx.traps[Inexact]=True
            r=m.calculate(args(value));self.assertEqual(r['fixed_decimal'],'2.67');self.assertFalse(r['is_tie'])
            self.assertEqual(Fraction(r['input_exact']),Fraction(value))
            self.assertEqual(m.calculate(args('1/3',12,'ceiling'))['fixed_decimal'],'0.333333333334')

    def test_strict_schema_modes_places_and_number_grammar(self):
        invalid=[{},[],args('1',True),args('1',-1),args('1',13),args('1',2.0),args('1','2'),args('1',2,None),args('1',2,[]),args('1',2,'HALF_UP'),args('1',2,'half-up'),{**args('1'),'currency':'TWD'}]
        invalid += [args(v) for v in ('1/0','1/-2','1e2','+1',' 1','1+2','NaN',1,True,None,'1'*2001)]
        for item in invalid:
            with self.subTest(item=str(item)[:100]):
                with self.assertRaises(ValueError):m.calculate(item)

    def test_large_integer_display_and_bounded_reduced_results(self):
        value='9'*2000
        r=m.calculate(args(value,12));self.assertEqual(r['exact'],value);self.assertEqual(r['fixed_decimal'],value+'.'+'0'*12)
        # A bounded rational input can have a rounding result with an oversized reduced numerator.
        with self.assertRaises(ValueError):m.calculate(args(('9'*1999+'8')+'/3',12))
        self.assertEqual(m.calculate(args(('9'*1999+'8')+'/3',0))['status'],'number_rounded')
        # Even when the rounded value is small, its exact difference must remain bounded.
        denominator=10**1999+7
        value=str(denominator//3)+'/'+str(denominator)
        with self.assertRaises(ValueError):m.calculate(args(value,2))
        self.assertEqual(m.calculate(args(value,0))['exact'],'0')

    def test_cancellation_before_and_after_arithmetic_preserves_input(self):
        data=args('1/7');original=copy.deepcopy(data);cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError):m.calculate(data,cancel)
        cancel.clear();parse=m.exact_math.parse
        def stopping(value):
            result=parse(value);cancel.set();return result
        with patch.object(m.exact_math,'parse',side_effect=stopping):
            with self.assertRaises(EngineError):m.calculate(data,cancel)
        self.assertEqual(data,original)

    def test_real_dispatch_stage_backup_resume_and_citation_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite3');sid=store.new_session();gid=a.create_goal(store,sid,'核對2.345恰半取偶2位')
            first=a.run_goal(store,sid,gid,ScriptedEngine(action('round_number',**args('2.345',2,'half_even'))),max_steps=1)
            obs=first['steps'][0]['observation'];self.assertEqual(obs['fixed_decimal'],'2.34');self.assertNotIn('excerpts',obs)
            child=a.create_goal(store,sid,'繼續核對',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'])
            self.assertIn('half_even',stage);self.assertIn('fixed_decimal',stage);self.assertIn('difference_exact',stage)
            backup=create_backup(store.path,root/'copy.paibackup');restore_backup(backup['path'],root/'restored')
            restored=Store(root/'restored'/'assistant.sqlite3');self.assertEqual(a.get_goal(restored,sid,gid)['steps'],first['steps'])
            result=a.run_goal(restored,sid,gid,ScriptedEngine(action('finish',report='[D1:L1]'),action('finish',report='核對結果2.34。')),max_steps=2)
            self.assertEqual(result['steps'][1]['action']['tool'],'invalid');self.assertEqual(result['status'],'report_ready')
            self.assertEqual(result['steps'][0],first['steps'][0])


if __name__=='__main__':unittest.main()
