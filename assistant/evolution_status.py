"""Read-only, bounded provenance for questions about the assistant's own updates."""
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path


def status_request(question):
    """Do not retrieve private self-edit history for arbitrary source-code work."""
    if not isinstance(question,str) or not 1<=len(question)<=16000:
        return False
    # Code and quoted examples are data, not requests for the assistant's history.
    lines=[];fence=None
    for line in question.splitlines(keepends=True):
        marker=re.match(r'^[ \t]*(`{3,}|~{3,})([^\r\n]*)',line)
        if fence is not None:
            if marker and marker[1][0]==fence[0] and len(marker[1])>=fence[1] and not marker[2].strip():
                fence=None
            lines.append('\n')
        elif marker:
            fence=(marker[1][0],len(marker[1]));lines.append('\n')
        elif line.lstrip().startswith('>'):
            lines.append('\n')
        else:
            lines.append(line)
    view=''.join(lines)
    view=re.sub(r'「[^「」]*」|『[^『』]*』|“[^“”]*”|"[^"\r\n]*"|`[^`\r\n]*`',' ',view)
    if any(symbol in view for symbol in '「」『』“”"`'):
        return False
    view=unicodedata.normalize('NFKC',view)
    if re.search(r'自我改|自改|改碼|變聰明|self.improv',view,re.I):
        return True
    return bool(re.search(
        r'(?:你|妳|本助理|本助手|這個助理|這個助手)'
        r'(?:(?:是不是|是否|有沒有|剛剛|剛才|最近|已經|曾經|自己|又|已|有|真的|目前)){0,4}'
        r'(?:改了|修改了|修改過|改過)(?:自己的?)?程式'
        r'|修改(?:你|妳|本助理|本助手|這個助理|這個助手)(?:自己)?的程式',view))


def improvement_context(root, question):
    if not status_request(question):
        return []
    root = Path(root).resolve()
    folder = root / 'data/evolution'
    if not folder.is_dir() or folder.is_symlink() or not folder.resolve().is_relative_to(root):
        return []
    results = []
    paths = sorted(folder.glob('*/report.json'), key=lambda p: p.parent.name, reverse=True)[:20]
    for path in paths:
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > 100_000:
                continue
            report = json.loads(path.read_text(encoding='utf-8'))
            if not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}', report.get('id', '')):
                continue
            status = report.get('status')
            if status not in {'published', 'applied_unpublished', 'not_applied', 'preparing',
                              'generating', 'testing', 'applying', 'publishing', 'baseline_testing',
                              'acceptance_testing', 'no_change_needed', 'holdout_baseline_testing',
                              'holdout_testing', 'needs_reproduction'}:
                continue
            row = {'id': report['id'], 'recorded_status': status, 'revision_verified': False}
            commit = report.get('commit', '')
            files = report.get('files', {})
            allowed = {'assistant/communication.py', 'assistant/context.py',
                       'assistant/retrieval.py', 'prompts/communication.txt'}
            if status == 'published' and re.fullmatch('[a-f0-9]{40}', commit) and files and set(files).issubset(allowed):
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                changed = set(git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).decode().splitlines())
                verified = changed == set(files) and all(
                    re.fullmatch('[a-f0-9]{64}', sha) and hashlib.sha256(git('show', commit + ':' + name)).hexdigest() == sha
                    for name, sha in files.items())
                if verified:
                    row.update(revision_verified=True, commit=commit, modules=sorted(files),
                               checks_recorded_passed=bool(report.get('checks')) and all(
                                   item.get('passed') is True for item in report['checks']),
                               current_files_match=all(not (root / name).is_symlink() and
                                   (root / name).resolve().is_relative_to(root) and
                                   hashlib.sha256((root / name).read_bytes()).hexdigest() == sha for name, sha in files.items()),
                               rollback_recorded=bool(report.get('rollback')))
                    acceptance = report.get('acceptance', {})
                    counts = [acceptance.get(key) for key in ('baseline_passed', 'candidate_passed', 'total')]
                    if (acceptance.get('status') == 'verified_improvement' and
                        all(type(value) is int for value in counts) and 0 <= counts[0] < counts[1] == counts[2] <= 40):
                        row['acceptance_recorded'] = {'status': 'verified_improvement',
                            'baseline_passed': counts[0], 'candidate_passed': counts[1], 'total': counts[2]}
                    holdout = report.get('holdout', {})
                    counts = [holdout.get(key) for key in ('baseline_passed','candidate_passed','total')]
                    if (holdout.get('status') == 'passed' and all(type(value) is int for value in counts)
                            and 0 <= counts[0] <= counts[1] == counts[2] <= 40 and counts[2] > 0):
                        row['holdout_recorded'] = {'status':'passed','baseline_passed':counts[0],
                            'candidate_passed':counts[1],'total':counts[2], 'included_in_proposal_prompt':False}
            # No user-written goal, error, source contents or absolute path reaches chat.
            results.append(row)
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            continue
        if len(results) == 3:
            break
    return results
