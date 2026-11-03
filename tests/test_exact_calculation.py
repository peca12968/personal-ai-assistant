import tempfile
import unittest
from decimal import Inexact, Rounded, ROUND_UP, localcontext
from fractions import Fraction
from pathlib import Path

from assistant import autonomy as a
from assistant.store import Store
from assistant.backup import create_backup, restore_backup
from tests.test_autonomy import ScriptedEngine, action


def calc(operation, *values):
    return a.calculate({'operation': operation, 'values': list(values)})


class ExactCalculationTests(unittest.TestCase):
    def test_reuse_fraction_and_large_exact_integer(self):
        first = calc('quotient', '1', '7')
        self.assertTrue(first['rounded'])
        second = calc('product', first['exact'], '49')
        self.assertEqual(second['exact'], '7')
        third = calc('sum', second['exact'], '0.125')
        self.assertEqual(third['exact'], '57/8')
        self.assertEqual(third['decimal'], '7.125')
        self.assertFalse(third['rounded'])
        value = '12345678901234567890123456789012345678901234567890'
        middle = calc('sum', value, '1')
        self.assertEqual(middle['exact'], str(int(value) + 1))
        self.assertTrue(middle['rounded'])
        self.assertEqual(calc('difference', middle['exact'], value)['exact'], '1')

    def test_long_decimals_and_equivalent_input_forms(self):
        first = calc('sum', '0.12345678901234567890123456789',
                     '0.00000000000000000000000000001')
        self.assertEqual(Fraction(first['exact']), Fraction('0.12345678901234567890123456790'))
        self.assertEqual(calc('product', first['exact'], '10')['decimal'],
                         '1.234567890123456789012345679')
        for source, expected in [('0006/0008', '3/4'), ('-0/3', '0'),
                                 ('-10/5', '-2'), ('000.1250', '1/8')]:
            with self.subTest(source=source):
                self.assertEqual(calc('sum', source)['exact'], expected)
        for x in range(-12, 13):
            for y in range(1, 13):
                self.assertEqual(calc('product', f'{x}/{y}', str(y))['exact'], str(x))

    def test_display_policy_independent_of_ambient_decimal_context(self):
        normal = calc('quotient', '2', '3')
        with localcontext() as ambient:
            ambient.prec = 2
            ambient.rounding = ROUND_UP
            ambient.Emax = 2
            ambient.Emin = -2
            ambient.traps[Inexact] = True
            ambient.traps[Rounded] = True
            self.assertEqual(calc('quotient', '2', '3'), normal)
            self.assertEqual(calc('sum', '1' + '0' * 80)['exact'], '1' + '0' * 80)
            self.assertTrue(ambient.traps[Inexact])
            self.assertEqual(ambient.prec, 2)
        self.assertEqual(normal['decimal_precision'], 40)
        self.assertEqual(normal['decimal_rounding'], 'ROUND_HALF_EVEN')
        for last, expected in [('25', '2'), ('35', '4')]:
            result = calc('sum', '1.' + '0' * 38 + last)
            self.assertEqual(result['decimal'], '1.' + '0' * 38 + expected)
            self.assertTrue(result['rounded'])

    def test_strict_grammar_zero_and_arity_rejections(self):
        for value in ['1/0', '0/000', '1/-3', '+1/3', '1 / 3', '1/2/3',
                      '1.2/3', '.5', '1.', '1e2', '1_000', 'NaN', 'Infinity',
                      '１２', '1\n', ' 1', '1+2', '__import__("os")', 1, True, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                calc('sum', value)
        for op, values in [('sum', []), ('product', ['1'] * 51),
                           ('difference', ['1']), ('quotient', ['1', '0/3']),
                           ('eval', ['1'])]:
            with self.subTest(op=op, values=values), self.assertRaises(ValueError):
                a.calculate({'operation': op, 'values': values})

    def test_bounded_values_intermediates_and_legacy_maximum_product(self):
        largest = '9' * 2000
        self.assertEqual(calc('sum', largest)['exact'], largest)
        denominator = '1' + '0' * 1999
        self.assertEqual(calc('quotient', '1', denominator)['exact'], '1/' + denominator)
        for value in ['1' * 2001, '1/' + '1' * 2001, '0.' + '0' * 1999 + '1']:
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                calc('sum', value)
        with self.assertRaises(ValueError):
            calc('sum', largest, '1', '-' + largest)
        with self.assertRaises(ValueError):
            calc('product', largest, largest)
        # The old 50 operands of 20 integer + 10 fractional digits remain valid.
        result = calc('product', *(['9' * 20 + '.' + '9' * 10] * 50))
        self.assertEqual(Fraction(result['exact']), Fraction('9' * 20 + '.' + '9' * 10) ** 50)

    def test_persisted_steps_backup_and_stage_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'source.sqlite3')
            sid = store.new_session()
            gid = a.create_goal(store, sid, '分步計算，保存精確值')
            goal = a.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate', operation='quotient', values=['1', '7']),
                action('calculate', operation='product', values=['1/7', '49'])), max_steps=2)
            self.assertEqual(goal['status'], 'paused')
            self.assertEqual(goal['steps'][1]['observation']['exact'], '7')
            child = a.create_goal(store, sid, '接續核對', parent_id=gid)
            context = a.get_goal(store, sid, child)['previous_stage']['checkpoint']
            meta = context['activity'][0]['observation_metadata']['value']
            self.assertEqual(meta['exact'], '1/7')
            self.assertEqual(meta['decimal_precision'], 40)
            self.assertEqual(meta['decimal_rounding'], 'ROUND_HALF_EVEN')
            # Reload uses the saved exact result; historical steps stay immutable.
            result = a.run_goal(Store(store.path), sid, gid, ScriptedEngine(
                action('calculate', operation='sum', values=['7', '0.125']),
                action('finish', report='精確結果57/8，亦即7.125。')), max_steps=2)
            self.assertEqual(result['status'], 'report_ready')
            self.assertEqual(result['steps'][:2], goal['steps'])
            archive = create_backup(store.path, root / 'saved.paibackup')
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored, sid, gid)['steps'], result['steps'])

    def test_rejected_fraction_is_failed_attempt_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.sqlite3')
            sid = store.new_session()
            gid = a.create_goal(store, sid, '核對分數')
            result = a.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate', operation='sum', values=['1/0']),
                action('calculate', operation='sum', values=['1/3']),
                action('finish', report='第一次分母為零失敗；第二次為精確1/3。')), max_steps=3)
            failed = result['steps'][0]
            self.assertEqual(failed['action']['tool'], 'invalid')
            self.assertEqual(failed['observation']['failed_action']['args']['values'], ['1/0'])
            self.assertEqual(result['steps'][1]['observation']['exact'], '1/3')
            self.assertEqual(result['status'], 'report_ready')


if __name__ == '__main__':
    unittest.main()
