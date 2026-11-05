import json
import unittest

from assistant.evidence_logic import analyse, analyse_indexed, analyse_named, source_segments


def observation(number, source):
    return {'literal': number, 'source': source}


def rule(conditions, result, source):
    return {'when': conditions, 'then': result, 'source': source}


def analyse_case(question, claims, rules, facts, query):
    extraction = {'claims': claims, 'rules': rules, 'facts': facts, 'query': query}
    return analyse(question, json.dumps(extraction, ensure_ascii=False))


class EvidenceLogicTests(unittest.TestCase):
    def test_source_ids_preserve_original_unicode_offsets(self):
        question = '  若甲運轉，乙亮。\r\n已知乙沒有亮🙂。能否確定甲運轉？'
        segments = source_segments(question)
        self.assertEqual(''.join(row['text'] for row in segments), question)
        for row in segments:
            self.assertEqual(question[row['start']:row['end']], row['text'])
        with self.assertRaises(ValueError):
            source_segments('。' * 65)

    def test_indexed_analysis_recovers_verbatim_source_and_rejects_wrong_ids(self):
        question = '若甲運轉，乙亮。已知乙沒有亮。能否確定甲運轉？'
        value = {
            'rules': [{'when': ['甲運轉'], 'then': '乙亮', 'kind': 'required', 'source': 1}],
            'facts': [observation('!乙亮', 2)],
            'query': observation('甲運轉', 3),
        }
        result = analyse_indexed(question, json.dumps(value, ensure_ascii=False))
        self.assertEqual(result['status'], 'refuted')
        self.assertEqual(result['constraints']['facts'][0]['source'], '已知乙沒有亮。')
        self.assertEqual(result['constraints']['query']['source'], '能否確定甲運轉？')
        for source in (True, '3', 0, 4):
            value['query']['source'] = source
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, '片段編號'):
                analyse_indexed(question, json.dumps(value, ensure_ascii=False))

    def test_possible_rule_keeps_names_without_forcing_its_effect(self):
        question = '漏水也可能使警示亮。已觀察到漏水。能判斷警示亮嗎？'
        extraction = {
            'rules': [{'when': ['漏水'], 'then': '警示亮', 'kind': 'possible', 'source': '漏水也可能使警示亮'}],
            'facts': [observation('漏水', '已觀察到漏水')],
            'query': observation('警示亮', '能判斷警示亮嗎'),
        }
        result = analyse_named(question, json.dumps(extraction, ensure_ascii=False))
        self.assertEqual(result['status'], 'undetermined')
        self.assertEqual(result['compatible_worlds'], 2)
        self.assertEqual(result['extraction'], extraction)
        self.assertEqual(result['constraints']['rules'], [])

    def test_named_negation_reuses_same_proposition_and_keeps_source(self):
        question = '有紀錄稱開關閉合，也有紀錄稱開關未閉合。問開關是否閉合。'
        extraction = {
            'rules': [],
            'facts': [observation('開關閉合', '有紀錄稱開關閉合'),
                      observation('!開關閉合', '也有紀錄稱開關未閉合')],
            'query': observation('開關閉合', '問開關是否閉合'),
        }
        result = analyse_named(question, json.dumps(extraction, ensure_ascii=False))
        self.assertEqual(result['constraints']['claims'], ['開關閉合'])
        self.assertEqual(result['status'], 'inconsistent')
        extraction['facts'][1]['source'] = '已去現場查證'
        with self.assertRaises(ValueError):
            analyse_named(question, json.dumps(extraction, ensure_ascii=False))

    def test_competing_cause_does_not_disambiguate_power(self):
        question = '主電源中斷必定亮紅燈。已知感應器故障，也已亮紅燈。能確認主電源中斷嗎？'
        result = analyse_case(
            question, ['主電源中斷', '亮紅燈', '感應器故障'],
            [rule([1], 2, '主電源中斷必定亮紅燈')],
            [observation(2, '已亮紅燈'), observation(3, '已知感應器故障')],
            observation(1, '能確認主電源中斷嗎'),
        )
        self.assertEqual(result['status'], 'undetermined')
        self.assertEqual(result['compatible_worlds'], 2)
        self.assertTrue(result['witnesses']['supports']['主電源中斷'])
        self.assertFalse(result['witnesses']['opposes']['主電源中斷'])
        checks = {item['claim']: item for item in result['observation_checks']}
        self.assertTrue(checks['主電源中斷']['distinguishes'])
        self.assertFalse(checks['感應器故障']['distinguishes'])
        self.assertEqual(checks['感應器故障']['if_true'], 'undetermined')
        self.assertEqual(checks['感應器故障']['if_false'], 'inconsistent')

    def test_refutation_follows_through_both_rules(self):
        question = '過熱推出停機，停機推出警報。確認無警報。問過熱是否成立。'
        result = analyse_case(
            question, ['過熱', '停機', '警報'],
            [rule([1], 2, '過熱推出停機'), rule([2], 3, '停機推出警報')],
            [observation(-3, '確認無警報')], observation(1, '問過熱是否成立'),
        )
        self.assertEqual(result['status'], 'refuted')
        self.assertEqual(result['witnesses'], {'opposes': {'過熱': False, '停機': False, '警報': False}})

    def test_conjunction_requires_both_and_negative_query_is_preserved(self):
        question = '只要掃描與有效同時成立，就亮燈。已掃描，已有效。能判斷不亮燈嗎？'
        rules = [rule([1, 2], 3, '只要掃描與有效同時成立，就亮燈')]
        query = observation(-3, '能判斷不亮燈嗎')
        first = observation(1, '已掃描')
        second = observation(2, '已有效')
        partial = analyse_case(question, ['掃描', '有效', '亮燈'], rules, [first], query)
        complete = analyse_case(question, ['掃描', '有效', '亮燈'], rules, [first, second], query)
        self.assertEqual(partial['status'], 'undetermined')
        self.assertEqual(complete['status'], 'refuted')

    def test_conflicting_observations_do_not_entail_everything(self):
        question = '第一份說開關閉合，第二份說開關未閉合。能確認開關閉合嗎？'
        result = analyse_case(
            question, ['開關閉合'], [],
            [observation(1, '第一份說開關閉合'), observation(-1, '第二份說開關未閉合')],
            observation(1, '能確認開關閉合嗎'),
        )
        self.assertEqual(result['status'], 'inconsistent')
        self.assertEqual(result['compatible_worlds'], 0)
        self.assertEqual(result['witnesses'], {})
        self.assertFalse(result['observation_checks'][0]['distinguishes'])

    def test_unbound_and_malformed_extractions_stop_before_verdict(self):
        question = '確認閘門關閉。能確認閘門關閉嗎？'
        valid = {'claims': ['閘門關閉'], 'rules': [], 'facts': [observation(1, '確認閘門關閉')],
                 'query': observation(1, '能確認閘門關閉嗎')}
        for broken in (
            {**valid, 'query': observation(True, '能確認閘門關閉嗎')},
            {**valid, 'query': observation(2, '能確認閘門關閉嗎')},
            {**valid, 'claims': ['閘門打開']},
            {**valid, 'facts': [observation(1, '虛構的實地確認')]},
            {**valid, 'rules': [rule([], 1, '確認閘門關閉')]},
        ):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                analyse(question, json.dumps(broken, ensure_ascii=False))
        raw = json.dumps(valid, ensure_ascii=False)
        for broken in ('```json\n' + raw + '\n```', raw.replace('"rules": []', '"rules": [], "rules": []')):
            with self.assertRaises(ValueError):
                analyse(question, broken)


if __name__ == '__main__':
    unittest.main()
