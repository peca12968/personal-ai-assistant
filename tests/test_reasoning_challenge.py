import itertools
import json
import math
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from fractions import Fraction
from pathlib import Path

from scripts import evaluate_reasoning_pair as pair


class ChallengeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = pair.build_cases(20261104, 'challenge')

    def category(self, name):
        return [c for c in self.cases if c['category'] == name]

    def test_legacy_suite_unchanged_and_new_cases_reproducible_separate(self):
        legacy = pair.build_cases(20261103)
        self.assertEqual(pair.sha(pair.encoded(legacy)), 'a085af9a52a4f8a3f992d93a1d7c2d5279b39313978a40feed0885627ba1561d')
        self.assertEqual(self.cases, pair.build_cases(20261104, 'challenge'))
        self.assertEqual(len(self.cases), 36)
        self.assertEqual(len({c['id'] for c in self.cases}), 36)
        self.assertFalse({c['id'] for c in legacy} & {c['id'] for c in self.cases})
        with self.assertRaises(ValueError): pair.build_cases(1, 'unknown')

    def test_logic_by_boolean_truth_table(self):
        def truth(clause, assignment):
            op, (left, right) = clause
            a, b = assignment[left], assignment[right]
            tables = {'implies': (1, 1, 0, 1), 'iff': (1, 0, 0, 1), 'xor': (0, 1, 1, 0),
                      'or': (0, 1, 1, 1), 'nand': (1, 1, 1, 0), 'and': (0, 0, 0, 1)}
            return tables[op][2*a+b]
        for c in self.category('logic'):
            p = c['proof']
            valid = [bits for bits in itertools.product((0, 1), repeat=8) if all(truth(clause, bits) for clause in p['clauses'])]
            self.assertEqual(len(valid), p['solution_count'])
            self.assertGreater(len(valid), 1)
            values = {truth(p['query'], bits) for bits in valid}
            self.assertEqual(c['answer'], '必真' if values == {1} else '必假' if values == {0} else '不確定')

    def test_quantitative_by_independent_enumeration_and_conservation(self):
        for c in self.category('quantitative'):
            p = c['proof']; kind = p['kind']
            if kind == 'conditional_balls':
                red, blue, green = p['counts']
                balls = list('R'*red + 'B'*blue + 'G'*green)
                numerator = denominator = 0
                for draw in itertools.combinations(range(len(balls)), 5):
                    colors = [balls[i] for i in draw]
                    if 'B' in colors and 'G' in colors:
                        denominator += 1
                        numerator += colors.count('R') == 3
                expected = Fraction(numerator, denominator)
            elif kind == 'dilution':
                remaining = Fraction(p['initial'])
                for withdrawal in p['withdrawals']:
                    removed = remaining / p['volume'] * withdrawal
                    remaining -= removed
                expected = remaining
            elif kind == 'staged_rates':
                answer = Fraction(c['answer']); a, b, leak = p['rates']
                final = answer - p['first'] - p['together']
                self.assertGreater(final, 0)
                filled = Fraction(p['first'], a) + Fraction(p['together'], a) + Fraction(p['together'], b)
                filled += final/b - (final+p['together'])/leak
                self.assertEqual(filled, 1)
                continue
            elif kind == 'conditional_dice':
                payouts = []
                for a in range(1, p['faces']+1):
                    for b in range(a+1, p['faces']+1):
                        if a+b >= p['threshold']: payouts.append(a*b-p['fee'])
                expected = Fraction(sum(payouts), len(payouts))
            elif kind == 'exact_divisibility':
                total = 0
                # Inclusion-exclusion for exactly two divisibility events,
                # excluding multiples of 11, versus the generator's row scan.
                for size in range(2, 5):
                    for ds in itertools.combinations(p['divisors'], size):
                        count = p['n']//math.lcm(*ds) - p['n']//math.lcm(*ds, p['exclude'])
                        total += (-1)**(size-2) * math.comb(size, 2) * count
                expected = Fraction(total)
            else:
                candidates = []
                for mask in range(1 << 10):
                    chosen = [i for i in range(10) if mask & (1 << i)]
                    if len(chosen) != 4 or (1 in chosen and 5 in chosen) or (7 in chosen and 2 not in chosen): continue
                    if sum(p['costs'][i] for i in chosen) <= p['budget']:
                        candidates.append((sum(p['values'][i] for i in chosen), chosen))
                best = max(value for value, _ in candidates)
                self.assertEqual(sorted(p['best_sets']), sorted(chosen for value, chosen in candidates if value == best))
                expected = Fraction(best)
            self.assertEqual(Fraction(c['answer']), expected, c['id'])

    def test_spatial_by_moving_only_coordinates_of_one_cells(self):
        for c in self.category('spatial'):
            p = c['proof']; n = c['grid_size']
            points = {(r, col) for r, row in enumerate(p['initial']) for col, value in enumerate(row) if value}
            for op in p['operations']:
                if op[0] == 'rotate': points = {(col, n-1-r) for r, col in points}
                elif op[0] == 'transpose': points = {(col, r) for r, col in points}
                elif op[0] == 'horizontal': points = {(r, n-1-col) for r, col in points}
                elif op[0] == 'vertical': points = {(n-1-r, col) for r, col in points}
                elif op[0] == 'row_shift': points = {(r, (col-op[2]) % n if r == op[1] else col) for r, col in points}
                elif op[0] == 'swap_rows': points = {(op[2] if r == op[1] else op[1] if r == op[2] else r, col) for r, col in points}
                else: points.symmetric_difference_update({tuple(op[1:])})
            answer = '/'.join(''.join(str(int((r, col) in points)) for col in range(n)) for r in range(n))
            self.assertEqual(answer, c['answer'])

    def test_tracking_by_position_maps_and_deques(self):
        branches = {'if_before': set(), 'if_first': set()}
        for c in self.category('tracking'):
            p = c['proof']; state = deque(p['initial'])
            for op, expected in zip(p['operations'], p['states']):
                def rearrange(mapping):
                    old = list(state)
                    return deque(old[mapping.get(i, i)] for i in range(len(old)))
                if op[0] == 'left': state.rotate(-op[1])
                elif op[0] == 'reverse_range':
                    start, end = op[1:]
                    state = rearrange({i: start+end-1-i for i in range(start, end)})
                elif op[0] == 'swap_letters':
                    a, b = state.index(op[1]), state.index(op[2]); state = rearrange({a: b, b: a})
                elif op[0] == 'if_before':
                    branch = state.index('C') < state.index('F'); branches[op[0]].add(branch)
                    if branch: state = rearrange({0: 11, 11: 0})
                    else: state.rotate(-4)
                elif op[0] == 'delete': state = deque(v for i, v in enumerate(state) if i != op[1])
                elif op[0] == 'insert':
                    state.rotate(-op[1]); state.appendleft(op[2]); state.rotate(op[1])
                elif op[0] == 'reverse': state = rearrange({i: len(state)-1-i for i in range(len(state))})
                else:
                    branch = state[0] in 'ABCDEF'; branches[op[0]].add(branch)
                    state = rearrange({i: 10-i for i in range(2, 9)} if branch else {1: 9, 9: 1})
                self.assertEqual(list(state), expected, (c['id'], op))
            self.assertEqual(','.join(state), c['answer'])
        self.assertTrue(all(values == {True, False} for values in branches.values()))

    def test_all_eight_item_constraints_have_exactly_one_solution(self):
        for c in self.category('constraints'):
            valid = []
            for order in itertools.permutations('ABCDEFGH'):
                positions = {v: i for i, v in enumerate(order)}
                for op, a, b, n in c['proof']['clauses']:
                    x, y = positions[a], positions[b]
                    ok = x < y if op == 'before' else abs(x-y) == n if op == 'distance' else abs(x-y) > 1 if op == 'not_adjacent' else x != n
                    if not ok: break
                else: valid.append(','.join(order))
            self.assertEqual(valid, [c['answer']])

    def test_bilinear_rules_are_unique_in_the_stated_finite_domain(self):
        for c in self.category('induction'):
            p = c['proof']; m = p['modulus']; candidates = []
            for a, b, cc, d in itertools.product(range(-2, 3), range(-2, 3), range(-2, 3), range(m)):
                if all((a*x*y+b*x+cc*y+d-z) % m == 0 for x, y, z in p['examples']): candidates.append([a, b, cc, d])
            self.assertEqual(candidates, [p['coefficients']])
            a, b, cc, d = candidates[0]; x, y = p['target']
            total = a*x*y+b*x+cc*y+d
            self.assertEqual(int(c['answer']), total - (total//m)*m)

    def test_public_projection_grading_and_size_rules(self):
        calls = []
        for index, batch in enumerate(pair.batches(self.cases)):
            prompt = pair.public_prompt(batch)
            public = json.loads(prompt[len(pair.INSTRUCTIONS):])
            self.assertEqual(public, [{'id': c['id'], 'prompt': c['prompt']} for c in batch])
            self.assertLess(len(prompt), 16000)
            answer = json.dumps({c['id']: c['answer'] for c in batch})
            scores = pair.grade(batch, answer)
            self.assertTrue(all(r['correct'] for r in scores))
            for repeat in range(2):
                for arm in ('direct', 'assistant'):
                    calls.append({'arm': arm, 'repeat': repeat, 'batch': index, 'case_ids': [c['id'] for c in batch],
                                  'answer': answer, 'error': None, 'scores': scores, 'elapsed_seconds': 1})
        summary = pair.summarize({'calls': calls, 'status': 'complete', 'protocol': {'repeats': 2}}, self.cases)
        self.assertEqual(summary['direct']['correct'], 72)
        self.assertEqual(summary['assistant']['correct'], 72)
        seven = self.category('spatial')[0]
        with self.assertRaises(ValueError): pair.normalized(seven, '/'.join(['00000']*5))
        with self.assertRaises(ValueError): pair.normalized({'category': 'spatial', 'grid_size': True}, '0')
        self.assertIn('challenge_generator_sha256', pair.frozen(self.cases, {'profile': 'challenge'}))

    def test_offline_cli_does_not_create_output_or_call_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'not-created'
            run = subprocess.run([sys.executable, 'scripts/evaluate_reasoning_pair.py', '--profile', 'challenge',
                                  '--seed', '20261104', '--out', str(out)], cwd=pair.ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('no model calls', run.stdout)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
