"""Read complete, scoped replacements and explicit version adoption lines."""
import re


_LABEL = r'[^「」\r\n\x00]{1,120}'
_SCOPE = re.compile(rf'^本次為「(?P<scope>{_LABEL})」。')
_CITATION = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_PAIR = re.compile(rf'「({_LABEL})」為「({_LABEL})」')
_ROW = (rf'若(?P<when>{_PAIR.pattern}(?:、{_PAIR.pattern}){{0,4}})，'
        rf'原「(?P<before>{_LABEL})」改為「(?P<after>{_LABEL})」。其他配對沿用原表。')
_RULE = re.compile(rf'本輪「(?P<scope>{_LABEL})」正式生效。{_ROW}')
_VERSIONED_RULE = re.compile(rf'本輪「(?P<scope>{_LABEL})」的「(?P<version>{_LABEL})」版規則。{_ROW}')
_ADOPTION = re.compile(rf'本輪「(?P<scope>{_LABEL})」正式採用版本「(?P<version>{_LABEL})」。')
MAX_RECORDS = 8


def objective_scope(objective):
    match = _SCOPE.match(objective)
    return match['scope'] if match else None


def validate(args):
    if not isinstance(args.get('scope'), str) or not re.fullmatch(_LABEL, args['scope']):
        raise ValueError('文件規則更正須指定本次完整範圍。')
    if args.keys() == {'document_rule', 'scope'}:
        citations = [args['document_rule']]
    elif args.keys() == {'document_rules', 'scope'} and isinstance(args['document_rules'], list):
        citations = args['document_rules']
    else:
        citations = []
    if (not 1 <= len(citations) <= MAX_RECORDS
            or any(not isinstance(citation, str) or not _CITATION.fullmatch(citation) for citation in citations)
            or len(set(citations)) != len(citations)):
        raise ValueError('文件規則更正須指定一至八個不同且已完整查讀的文件行。')


def _parse(text, scope, include_versions):
    match = _RULE.fullmatch(text)
    version = None
    if not match and include_versions:
        adoption = _ADOPTION.fullmatch(text)
        if adoption and adoption['scope'] == scope:
            return {'adopts': adoption['version']}
        match = _VERSIONED_RULE.fullmatch(text)
        if match:
            version = match['version']
    if not match or match['scope'] != scope or match['before'] == match['after']:
        return None
    pairs = _PAIR.findall(match['when'])
    if len(dict(pairs)) != len(pairs):
        return None
    rule = {'when': dict(pairs), 'before': match['before'], 'after': match['after']}
    return {**rule, 'version': version} if version is not None else rule


def read_rules(steps, base_step, scope, *, include_versions=False):
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
                rule = _parse(quote, scope, include_versions)
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
