"""Explicit, bounded source revision with protected checks and recoverable releases.

Candidate directories isolate versions, NOT operating-system permissions.
Python tests execute candidate code under the current Windows account.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .engine import CodexEngine, EngineError, ROOT
from .acceptance import compare, grade, strict_json, validate_suite

# The model cannot edit this controller, publishing, storage, tests or credentials.
EDITABLE = ('assistant/communication.py', 'assistant/context.py',
            'assistant/retrieval.py', 'prompts/communication.txt')
CHECKS = [('-m', 'unittest', 'discover', '-s', 'tests', '-v')] + [
    ('scripts/' + name + '.py',) for name in
    ('evaluate', 'evaluate_context', 'evaluate_documents', 'evaluate_search',
     'evaluate_understanding', 'evaluate_tasks', 'evaluate_workflows', 'evaluate_memory_commands', 'evaluate_answer_runs')]
CHECKS.append(('scripts/evaluate_understanding.py', '--suite', 'evals/communication_context.json'))
CHECKS.append(('scripts/evaluate_understanding.py', '--suite', 'evals/communication_scope.json'))
CHECKS.append(('scripts/evaluate_understanding.py', '--suite', 'evals/companion_profile.json'))
CHECKS.append(('scripts/evaluate_understanding.py', '--suite', 'evals/product_help.json'))
CHECKS.append(('scripts/evaluate_understanding.py', '--suite', 'evals/topic_context.json'))
CHECKS.append(('scripts/evaluate_goal_drafts.py',))
CHECKS.append(('scripts/evaluate_goal_replies.py',))
CHECKS.append(('scripts/evaluate_json_documents.py',))
CHECKS.append(('scripts/evaluate_tool_recovery.py',))
CHECKS.append(('scripts/evaluate_date_months.py',))
CHECKS.append(('scripts/evaluate_docx_reading.py',))


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, encoding='utf-8', errors='strict',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).strip()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def clean_head(root):
    if git(root, 'status', '--porcelain'):
        raise ValueError('專案有尚未發布的變更；請先完成或保存現有工作，再自我改進。')
    if not git(root, 'branch', '--show-current'):
        raise ValueError('目前不在分支上，不能自我改進。')
    return git(root, 'rev-parse', 'HEAD')


def parse_proposal(raw, selected):
    """Accept exact paths only: no deletion, new files, escaping or duplicate edits."""
    if len(raw.encode('utf-8')) > 400_000:
        raise ValueError('模型變更過大。')
    try:
        proposal = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('模型未傳回有效 JSON 變更。') from exc
    if not isinstance(proposal, dict) or not isinstance(proposal.get('files'), list):
        raise ValueError('變更格式錯誤。')
    files = {}
    for item in proposal['files']:
        if not isinstance(item, dict):
            raise ValueError('變更項目格式錯誤。')
        name, content = item.get('path'), item.get('content')
        if not isinstance(name, str) or name not in selected or name not in EDITABLE or name in files:
            raise ValueError('變更超出選取的模組，或重複路徑。')
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 100_000 or '\x00' in content:
            raise ValueError('變更內容空白、過大或包含無效字元。')
        if name.endswith('.py'):
            ast.parse(content, filename=name)
        files[name] = content.encode('utf-8')
    if not files:
        raise ValueError('模型沒有提出程式變更。')
    return files


def run_checked(command, cwd, log, cancel=None, timeout=180):
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    with Path(log).open('wb') as output:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel and cancel.is_set():
                    raise EngineError('自我改進已停止，尚未套用的變更保留於本機紀錄。')
                if time.monotonic() >= deadline:
                    raise EngineError('驗證或發布逾時；請查看本機紀錄。')
                time.sleep(.1)
            if process.returncode:
                raise ValueError('驗證或發布失敗，詳細紀錄：' + str(log))
        finally:
            CodexEngine.stop(process)
            process.wait(timeout=10)


def unchanged(root, baseline, original):
    if clean_head(root) != baseline:
        raise ValueError('驗證期間版本已變動，停止套用。')
    if any((root / name).is_symlink() or digest((root / name).read_bytes()) != digest(content)
           for name, content in original.items()):
        raise ValueError('驗證期間原始檔案已變動，停止套用。')


def improve(goal, selected=None, *, root=ROOT, engine=None, cancel=None, progress=None, acceptance=None, holdout=None):
    """One explicit goal -> one candidate -> checks -> apply -> private publication."""
    root = Path(root).resolve()
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
        raise ValueError('改善方向需為 1–4000 字。')
    selected = list([EDITABLE[0], EDITABLE[3]] if selected is None else selected)
    if not selected or len(selected) != len(set(selected)) or any(name not in EDITABLE for name in selected):
        raise ValueError('只可選擇列出的溝通與理解模組。')
    suite = validate_suite(acceptance, selected) if acceptance is not None else None
    withheld = validate_suite(holdout) if holdout is not None else None
    if withheld is not None:
        if suite is None:
            raise ValueError('保留驗收需搭配提供給模型的驗收案例。')
        def inputs(case):
            return json.dumps({key:value for key,value in case.items() if key not in ('id','expect')},sort_keys=True,ensure_ascii=False)
        provided = {inputs(case) for case in suite['cases']}
        if any(inputs(case) in provided for case in withheld['cases']):
            raise ValueError('保留案例不可重複本輪提供給模型的輸入。')
    state_dir = root / 'data' / 'evolution'
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / 'active.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('已有自我改進鎖定；若前次異常中斷，確認程序停止後再移除 data/evolution/active.lock。') from exc
    report = None
    applied = False
    try:
        with handle:
            handle.write(str(os.getpid()))
        baseline = clean_head(root)
        run_id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        folder = state_dir / run_id
        candidate = folder / 'candidate'
        candidate.mkdir(parents=True)
        report = {'id': run_id, 'goal': goal.strip(), 'base_commit': baseline,
                  'selected': selected, 'status': 'preparing', 'checks': [], 'directory': str(folder),
                  'acceptance': {'status': 'pending' if suite else 'not_provided'},
                  'holdout': {'status': 'pending' if withheld else 'not_provided'}}

        def save(status):
            report['status'] = status
            temp = folder / 'report.tmp'
            temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(folder / 'report.json')
            if progress:
                progress(status)

        save('preparing')
        # Copy tracked source only; conversations, memories, keys and .git are not copied.
        original = {}
        for name in git(root, 'ls-files', '-z').split('\0'):
            if not name:
                continue
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('原始碼包含連結或越界路徑。')
            original[name] = path.read_bytes()
            target = candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original[name])
        baseline_observations = None
        withheld_baseline = None
        suite_bytes = json.dumps(suite, ensure_ascii=False, indent=2).encode('utf-8') if suite else None
        suite_path = folder / 'acceptance.json'
        if suite:
            suite_path.write_bytes(suite_bytes)
            report['acceptance_sha256'] = digest(suite_bytes)
        withheld_bytes = json.dumps(withheld,ensure_ascii=False,indent=2).encode('utf-8') if withheld else None
        withheld_path = folder / 'holdout.json'
        if withheld:
            withheld_path.write_bytes(withheld_bytes)
            report['holdout_sha256'] = digest(withheld_bytes)

        def unchanged_suites():
            for path, frozen in ((suite_path,suite_bytes),(withheld_path,withheld_bytes)):
                if frozen is not None and (path.is_symlink() or path.read_bytes() != frozen):
                    raise ValueError('驗收案例被改動，停止本輪。')

        def observe(phase, *, held_out=False):
            unchanged_suites()
            observed_suite = withheld if held_out else suite
            observed_path = withheld_path if held_out else suite_path
            output = folder / (phase + '-observations.json')
            run_checked([sys.executable, 'scripts/observe_acceptance.py', '--suite', str(observed_path),
                         '--output', str(output)], candidate, folder / (phase + '-acceptance.log'), cancel, timeout=60)
            unchanged_suites()
            with output.open('rb') as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('驗收觀察過大。')
            result = strict_json(raw.decode('utf-8'))
            grade(observed_suite, result)  # Host validates IDs, structure and types.
            return result

        if suite:
            save('baseline_testing')
            baseline_observations = observe('baseline')
            if withheld:
                save('holdout_baseline_testing')
                withheld_baseline = observe('baseline-holdout',held_out=True)
                report['holdout'] = {'status':'baseline_measured','baseline':grade(withheld,withheld_baseline)}
            for name, content in original.items():
                if (candidate / name).is_symlink() or (candidate / name).read_bytes() != content:
                    raise ValueError('改前測試更動原始碼，停止本輪。')
            report['acceptance'] = {'status': 'baseline_measured', 'baseline': grade(suite, baseline_observations)}
            if all(row['passed'] for row in report['acceptance']['baseline']):
                report['acceptance'] = compare(suite, baseline_observations, baseline_observations)
                if withheld:
                    passed = all(row['passed'] for row in report['holdout']['baseline'])
                    report['holdout']['status'] = 'baseline_passed' if passed else 'baseline_failed'
                    if not passed:
                        save('needs_reproduction')
                        return report
                save('no_change_needed')
                return report
        unchanged_suites()
        source = {name: original[name].decode('utf-8') for name in selected}
        prompt = ('你正在修改自己的個人 AI 助理程式。完成一個明確的改善，保留公開函式與既有行為。'
                  '只改給定檔案，不執行工具，不存取網路或個人資料，不加入隱藏副作用或外部依賴。'
                  '回傳純 JSON，不要 Markdown：{"files":[{"path":"指定路徑","content":"完整新檔內容"}]}。'
                  '每個檔案最多 100000 UTF-8 bytes。無法達成時回傳空 files，不能假稱完成。\n' +
                  json.dumps({'goal': goal.strip(), 'source': source, 'acceptance': suite}, ensure_ascii=False))
        save('generating')
        if cancel and cancel.is_set():
            raise EngineError('自我改進已停止，沒有套用變更。')
        engine = engine or CodexEngine(folder, timeout=300)
        raw = engine.answer(prompt, cancel)
        (folder / 'proposal.json').write_text(raw, encoding='utf-8')
        edits = parse_proposal(raw, selected)
        edits = {name: content for name, content in edits.items() if content != original[name]}
        if not edits:
            raise ValueError('模型未產生實際差異。')
        report['files'] = {name: digest(content) for name, content in edits.items()}
        for name, content in edits.items():
            (candidate / name).write_bytes(content)
        save('testing')
        for index, command in enumerate(CHECKS):
            run_checked([sys.executable, *command], candidate, folder / f'check-{index}.log', cancel)
            report['checks'].append({'command': list(command), 'passed': True})
            save('testing')
        if suite:
            save('acceptance_testing')
            observations = observe('candidate')
            report['acceptance'] = compare(suite, baseline_observations, observations)
            save('acceptance_testing')
            if report['acceptance']['status'] != 'verified_improvement':
                raise ValueError('本輪驗收未通過：需全部案例符合預期，且至少修好一個原本失敗的案例。原程式未套用。')
        if withheld:
            save('holdout_testing')
            withheld_observations = observe('candidate-holdout',held_out=True)
            comparison = compare(withheld,withheld_baseline,withheld_observations)
            comparison['status'] = 'passed' if comparison['candidate_passed'] == comparison['total'] else 'failed'
            report['holdout'] = comparison
            save('holdout_testing')
            if comparison['status'] != 'passed':
                raise ValueError('保留驗收未通過，原程式未套用；失敗案例不會自動提供給提案模型。')
        unchanged_suites()
        # Checks cannot silently alter source or protected validators.
        for name, old in original.items():
            path = candidate / name
            if path.is_symlink() or path.read_bytes() != edits.get(name, old):
                raise ValueError('驗證過程改動了候選檔案，拒絕套用。')
        if cancel and cancel.is_set():
            raise EngineError('自我改進已停止，沒有套用變更。')
        unchanged(root, baseline, original)
        save('applying')
        replaced = []
        try:
            for name, content in edits.items():
                path = root / name
                temp = path.with_name(path.name + '.evolution-tmp')
                with temp.open('xb') as out:
                    out.write(content)
                temp.replace(path)
                replaced.append(name)
        except Exception:
            for name in replaced:
                (root / name).write_bytes(original[name])
            raise
        applied = True
        save('publishing')
        # Publication uses the original protected script, not a model-selected command.
        # Once applying starts, finish publication rather than interrupting a Git push.
        command = [sys.executable, str(root / 'scripts/publish.py'), '--message',
                   'Verified self-improvement ' + run_id, '--expected-head', baseline]
        for name in edits:
            command.extend(['--expected-file', name])
            command.extend(['--expected-sha256', name + '=' + digest(edits[name])])
        run_checked(command, root, folder / 'publish.log', timeout=300)
        report['commit'] = git(root, 'rev-parse', 'HEAD')
        branch = git(root, 'branch', '--show-current')
        remote = git(root, 'ls-remote', 'origin', 'refs/heads/' + branch).split()[0]
        if remote != report['commit']:
            raise ValueError('本機與遠端版本不一致，尚不能宣稱發布完成。')
        save('published')
        return report
    except Exception as exc:
        if report is not None:
            report['error'] = str(exc)
            report['current_commit'] = git(root, 'rev-parse', 'HEAD')
            save('applied_unpublished' if applied else 'not_applied')
        raise
    finally:
        lock.unlink(missing_ok=True)


def rollback(run_id, *, root=ROOT):
    """Revert only the still-current published revision; preserve later user work."""
    import re
    root = Path(root).resolve()
    if not isinstance(run_id, str) or not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}', run_id):
        raise ValueError('無效的自我改進紀錄 ID。')
    state_dir = root / 'data/evolution'
    lock = state_dir / 'active.lock'
    with lock.open('x', encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    try:
        folder = state_dir / run_id
        path = folder / 'report.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        commit = report.get('commit')
        if report.get('status') != 'published' or clean_head(root) != commit:
            raise ValueError('只可回復目前最新、已發布的自我改進版本；後續變更需另行處理。')
        if git(root, 'rev-parse', commit + '^') != report.get('base_commit'):
            raise ValueError('版本父節點與紀錄不符。')
        names = set(git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines())
        if not names or names != set(report.get('files', {})) or not names.issubset(EDITABLE):
            raise ValueError('版本變更範圍與自我改進紀錄不符。')
        run_checked(['git', 'revert', '--no-edit', commit], root, folder / 'rollback.log')
        report['rollback'] = {'status': 'reverted_unpublished', 'commit': git(root, 'rev-parse', 'HEAD')}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        run_checked([sys.executable, str(root / 'scripts/publish.py'), '--message',
                     'Revert self-improvement ' + run_id], root, folder / 'rollback-publish.log', timeout=300)
        branch = git(root, 'branch', '--show-current')
        if git(root, 'ls-remote', 'origin', 'refs/heads/' + branch).split()[0] != report['rollback']['commit']:
            raise ValueError('回復已完成，但尚未確認遠端同步。')
        report['rollback']['status'] = 'published'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report['rollback']
    finally:
        lock.unlink(missing_ok=True)
