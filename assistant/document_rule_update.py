"""Read a complete, scoped replacement for one row of an existing decision table."""
import re


_LABEL = r'[^「」\r\n\x00]{1,120}'
_SCOPE = re.compile(rf'^本次為「(?P<scope>{_LABEL})」。')
_CITATION = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_PAIR = re.compile(rf'「({_LABEL})」為「({_LABEL})」')
_RULE = re.compile(rf'本輪「(?P<scope>{_LABEL})」正式生效。若(?P<when>{_PAIR.pattern}(?:、{_PAIR.pattern}){{0,4}})，'
                   rf'原「(?P<before>{_LABEL})」改為「(?P<after>{_LABEL})」。其他配對沿用原表。')


def objective_scope(objective):
    match = _SCOPE.match(objective)
    return match['scope'] if match else None


def validate(args):
    if (args.keys() != {'document_rule', 'scope'} or not isinstance(args['scope'], str)
            or not re.fullmatch(_LABEL, args['scope']) or not isinstance(args['document_rule'], str)
            or not _CITATION.fullmatch(args['document_rule'])):
        raise ValueError('文件規則更正須指定本次完整範圍與一個已完整查讀的文件行。')


def _parse(text, scope):
    match = _RULE.fullmatch(text)
    if not match or match['scope'] != scope or match['before'] == match['after']:
        return None
    pairs = _PAIR.findall(match['when'])
    if len(dict(pairs)) != len(pairs):
        return None
    return {'when': dict(pairs), 'before': match['before'], 'after': match['after']}


def read_rules(steps, base_step, scope):
    found = {}
    for step in steps:
        if step['sequence'] <= base_step or step['action']['tool'] not in {'read', 'read_ranges'}:
            continue
        for excerpt in step['observation'].get('excerpts', []):
            if excerpt.get('partial_line') is not False or excerpt.get('requested_range') is not True:
                continue
            first, last = excerpt['start_line'], excerpt['end_line']
            lines = excerpt['text'].splitlines()
            if len(lines) != last - first + 1:
                raise ValueError('已讀文件規則的行數與查讀範圍不一致。')
            for line, rendered in enumerate(lines, first):
                prefix = f'L{line}: '
                if not rendered.startswith(prefix):
                    raise ValueError('已讀文件規則的行號與查讀範圍不一致。')
                quote = rendered[len(prefix):]
                rule = _parse(quote, scope)
                if rule is None:
                    continue
                citation = f'[D{excerpt["document_id"]}:L{line}]'
                evidence = {'citation': citation, 'source_sha256': excerpt['sha256'], 'quote': quote}
                value = {'scope': scope, **rule, 'read_step': step['sequence'], 'evidence': evidence}
                prior = found.setdefault(citation, value)
                if prior['evidence'] != evidence:
                    raise ValueError('同一文件規則行有不同版本或內容。')
    return found


def verify_context(rule, objective, sources):
    did = int(_CITATION.fullmatch(rule['evidence']['citation'])[1])
    if (rule['scope'] != objective_scope(objective)
            or not any(source['document_id'] == did and source['sha256'] == rule['evidence']['source_sha256']
                       for source in sources)):
        raise ValueError('文件規則更正須符合目前目標明示範圍及原匯入版本。')
