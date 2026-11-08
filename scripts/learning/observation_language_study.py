"""Compile synthetic observations with hidden fact-state annotations for scoring."""
from . import decision_code_study as decision


SCHEMA = 'synthetic_prerequisite_observation_language_v1'
INSTRUCTIONS = (
    '能確定可以回 {supported}，能確定不可以回 {refuted}，還不能確定回 {undetermined}。只輸出代碼。',
    '只回一個代碼。{undetermined} 表示資料不足，{supported} 表示確定可行，{refuted} 表示確定不可行。',
)


def compile_group(group, seen):
    if not isinstance(group, dict) or 'observations' not in group:
        raise ValueError('Observation language scenarios require annotated observations.')
    description = {key: value for key, value in group.items() if key != 'observations'}
    decision.validate_group(description, seen)
    rule, action = group['rule'], group['action']
    codes = dict(zip(decision.VERDICTS, group['codes'], strict=True))
    rule_clause = decision.rule_text(rule, action, group['conditions'])
    observations = group['observations']
    if not isinstance(observations, list) or len(observations) != len(codes):
        raise ValueError('Provide three alternative observation records per scenario.')
    result = []
    observed_verdicts = set()
    observed_facts = set()
    for index, row in enumerate(observations):
        if not isinstance(row, dict) or set(row) not in ({'states', 'fact'}, {'states', 'fact', 'witnessed'}):
            raise ValueError('Invalid observation fields.')
        states, fact = row['states'], row['fact']
        witnessed = row.get('witnessed', False)
        if (not isinstance(states, str) or len(states) != len(group['conditions'])
                or set(states) - decision.OBSERVATIONS.keys()
                or not isinstance(fact, str) or not 1 <= len(fact) <= 400 or fact != fact.strip()
                or not fact.endswith('。') or fact in observed_facts
                or type(witnessed) is not bool or (witnessed and rule not in decision.NECESSARY_RULES)):
            raise ValueError('Invalid observation states, fact wording or direct witness.')
        verdict = decision.decide(rule, states, witnessed)
        if verdict in observed_verdicts:
            raise ValueError('Each scenario must cover all three verdicts.')
        observed_verdicts.add(verdict)
        observed_facts.add(fact)
        instruction = INSTRUCTIONS[(index + len(seen)) % len(INSTRUCTIONS)].format(**codes)
        question = f'{rule_clause}{fact}按這次的紀錄，現在能否{action}？'
        prompt = question + instruction if index % 2 else instruction + question
        result.append(dict(id=f"{group['id']}_{index + 1}", contrast=group['id'], prompt=prompt,
                           answer=codes[verdict], verdict=verdict, code_map=codes))
    return result


def compile_study(spec):
    if (not isinstance(spec, dict) or set(spec) != {'schema', 'training', 'heldout', 'transfer', 'full_transfer'}
            or spec['schema'] != SCHEMA):
        raise ValueError('Observation language study schema mismatch.')
    result, seen = {}, set()
    for split, count in (('training', 5), ('heldout', 2)):
        groups = spec[split]
        if not isinstance(groups, list) or len(groups) != count:
            raise ValueError('Unexpected number of observation language scenarios.')
        result[split] = [row for group in groups for row in compile_group(group, seen)]
    return result
