"""Run bounded declarative inputs in a fresh interpreter; emit facts, not grades."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.acceptance import load_suite


def observe(suite):
    rows = []
    for case in suite['cases']:
        try:
            if case['kind'] == 'communication':
                from assistant.communication import conversation_style
                actual = conversation_style(case['question'], case['history'])
            elif case['kind'] == 'followup':
                from assistant.context import followup_query
                actual = followup_query(case['question'], case['history'])
            else:
                from assistant.retrieval import rank_memories
                actual = [row['id'] for row in rank_memories(case['query'], case['memories'], case['limit'])]
            encoded = json.dumps(actual, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode('utf-8')) > 40_000:
                raise ValueError('observation too large')
            rows.append({'id': case['id'], 'actual': actual})
        except Exception as exc:
            rows.append({'id': case['id'], 'error': type(exc).__name__})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    suite = load_suite(args.suite)
    args.output.write_text(json.dumps(observe(suite), ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
