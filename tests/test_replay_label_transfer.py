import unittest

from scripts.learning import replay_label_transfer as study


class LabelTransferTests(unittest.TestCase):
    def test_phase_limit_is_bounded_before_accessing_a_reference(self):
        for seconds in (False, 0, 540.0, 541, 3600):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, '六或九分鐘'):
                study.assemble('missing-reference', seconds)

    def test_fact_groups_rotate_each_code_through_all_meanings_without_overlap(self):
        spec = study.fixture()
        self.assertEqual({k: [len(rows) for rows in groups] for k, groups in spec.items()},
                         {'training': [3] * 6, 'holdout': [3] * 6})
        unique = set()
        for groups in spec.values():
            for group in groups:
                self.assertEqual(len({r['verdict'] for r in group}), 1)
                self.assertEqual(len({r['answer'] for r in group}), 3)
                for state in study.logic.VERDICTS:
                    self.assertEqual(len({r['code_map'][state] for r in group}), 3)
                unique.update(study.prior.normalized(r['prompt']) for r in group)
        self.assertEqual(len(unique), 36)

    def test_complete_new_mapping_group_is_required_and_any_prior_loss_blocks_pass(self):
        cases = [dict(id='protected', group='fresh', answer='K')]
        cases.extend(study.fixture()['holdout'][2])

        def reply(values):
            return dict(rows=[dict(id=c['id'], output=text, tokens=2, ended_with_eos=True)
                              for c, text in zip(cases, values, strict=True)])

        target = [c['answer'] for c in cases[1:]]
        parent = reply(['K', target[0], '?', '?'])
        partial = study.compare(cases, parent, reply(['K', target[0], target[1], '?']))
        self.assertFalse(partial['passed'])
        self.assertEqual(partial['gained_complete_contrasts'], [])
        lost = study.compare(cases, parent, reply(['?', *target]))
        self.assertEqual(lost['lost'], ['protected'])
        self.assertFalse(lost['passed'])
        kept = study.compare(cases, parent, reply(['K', *target]))
        self.assertTrue(kept['passed'])
        self.assertFalse(kept['automatic_activation'])
