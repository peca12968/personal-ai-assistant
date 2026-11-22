"""Prepare, preflight, run and inspect frozen full-assistant local candidate checks."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__ in (None,''): sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant.acceptance import strict_json
from assistant import parameter_acceptance as checks
from assistant import local_models, local_worker
from scripts.learning import fetch_model
from scripts.learning.protocol import EXPECTED_VERSIONS


def count_inputs(manifest, tokenizer):
    limit = local_models.MAX_INPUT_TOKENS
    rows = []
    for phase in checks.PHASES:
        for case in manifest['suite']['cases']:
            request = {'schema': 'local_inference_request_v1', 'prompt': manifest['prompts'][phase][case['id']],
                       'selection': manifest['selections'][phase]}
            messages = local_worker.request_messages(request)
            tokens = len(tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                       enable_thinking=False))
            rows.append({'phase': phase, 'case_id': case['id'], 'input_tokens': tokens,
                         'over_by': max(0, tokens - limit)})
    return {'status': 'input_preflight', 'check_id': manifest['id'], 'manifest_sha256': checks.queue.digest(manifest),
            'input_token_limit': limit, 'all_inputs_fit': all(0 < r['input_tokens'] <= limit for r in rows),
            'cases': rows}


def preflight_model_dir(selection):
    """Verify tokenizer inputs before counting. Weight contents are checked before a passing result."""
    pinned = local_models.ROOT / 'scripts/learning/model.json'
    with pinned.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != selection['manifest_sha256']:
        raise ValueError('分詞器清單與選用版本不符。')
    manifest = fetch_model.validate_manifest(strict_json(raw.decode('utf8')))
    if (manifest['model_id'], manifest['revision']) != (selection['model_id'], selection['revision']):
        raise ValueError('分詞器清單與選用版本不符。')
    base = local_models.ROOT / 'data/learning/models/Qwen3-4B' / manifest['revision']
    for item in manifest['files']:
        asset = base / item['name']
        if asset.suffix == '.safetensors':
            if not asset.is_file() or asset.stat().st_size != item['bytes']:
                raise ValueError('預檢缺少指定的模型檔案或大小不符。')
        elif not fetch_model.verify(asset, item):
            raise ValueError('預檢分詞器或模型設定檔已變更。')
    return base


def preflight(data_dir, check_id):
    path = checks.folder(data_dir, check_id)
    manifest = checks.read_manifest(path, live=True)
    receipt = local_models.read_json(path / 'receipt.json')
    if (not isinstance(receipt, dict) or receipt.get('schema') != 'full_assistant_receipt_v1'
            or receipt.get('manifest_sha256') != checks.queue.digest(manifest)):
        raise ValueError('驗收收據與規約不一致。')
    if importlib.metadata.version('transformers') != EXPECTED_VERSIONS['transformers']:
        raise ValueError('分詞器環境版本與本機工作程序不符。')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    from transformers import AutoTokenizer

    # Both arms use the same pinned base tokenizer, including when the baseline is a prior batch.
    for selection in manifest['selections'].values():
        checks.models.validate(selection)
    selected = manifest['selections']['baseline']
    base_selection = selected.get('base', selected)
    base = preflight_model_dir(base_selection)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    report = count_inputs(manifest, tokenizer)
    if report['all_inputs_fit']:
        local_models.verify_assets(base_selection)
        report['asset_check'] = 'full_base_assets'
    else:
        report['asset_check'] = 'configuration_hashes_and_weight_sizes'
    if checks.read_manifest(path, live=True) != manifest:
        raise ValueError('預檢期間驗收規約已變更。')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','prepare','preflight','run','report'),nargs='?',default='check')
    parser.add_argument('--data-dir',type=Path,default=checks.ROOT/'data')
    parser.add_argument('--candidate'); parser.add_argument('--baseline',help='原版批次ID；省略則使用原始底座')
    parser.add_argument('--suite',type=Path); parser.add_argument('--id')
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if args.command=='prepare' and (args.candidate is None or args.suite is None): parser.error('prepare需要--candidate與--suite。')
    if args.command in ('preflight','run','report') and args.id is None: parser.error('preflight/run/report需要--id。')
    if args.command!='prepare' and any(value is not None for value in (args.candidate,args.baseline,args.suite)):
        parser.error('--candidate、--baseline與--suite只可搭配prepare。')
    if args.id is not None and args.command not in ('preflight','run','report'): parser.error('--id只可搭配preflight/run/report。')
    try:
        if args.command=='check':
            print('Full-assistant validation available; no database opened, no model loaded, no training or activation. Model-only comparison: direct local arithmetic routing is disabled.')
            return 0
        if args.command == 'run':
            receipt = local_models.read_json(checks.folder(args.data_dir, args.id) / 'receipt.json')
            if not isinstance(receipt, dict) or receipt.get('status') != 'prepared':
                raise ValueError('驗收只能執行一次；可用 report 查閱已有紀錄。')
        if args.command in ('preflight','run'):
            runtime = checks.ROOT / 'data/learning/runtime'
            if Path(sys.prefix).resolve() == runtime.resolve():
                result = preflight(args.data_dir, args.id)
            else:
                python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
                child = subprocess.run([str(python), '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'preflight',
                                        '--data-dir', str(args.data_dir), '--id', args.id],
                                       stdout=subprocess.PIPE, encoding='utf8')
                if child.returncode:
                    sys.stdout.write(child.stdout)
                    return child.returncode
                result = json.loads(child.stdout)
            if args.command == 'preflight' or not result['all_inputs_fit']:
                print(json.dumps(result,ensure_ascii=False,indent=2))
                return 0 if result['all_inputs_fit'] else 1
        if args.command=='prepare':
            data=checks.models.local_models.read_json(args.suite,1024*1024)
            result={'id':checks.prepare(args.data_dir,args.candidate,data,baseline=args.baseline),'status':'prepared'}
        elif args.command=='run': result=checks.run(args.data_dir,args.id)
        else: result=checks.report(args.data_dir,args.id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        receipt=result.get('receipt',result)
        return 1 if (receipt.get('status') in ('failed','stale','cancelled') or receipt.get('assessment',{}).get('passed') is False
                     or result.get('sources_and_parameters_still_current') is False) else 0
    except (ImportError,ValueError,OSError,RuntimeError,KeyError,TypeError,sqlite3.Error) as exc: parser.exit(1,'完整驗收未完成：'+str(exc)+'\n')


if __name__=='__main__': raise SystemExit(main())
