"""Check GitHub workflow syntax before publication; never execute workflow code."""
import hashlib
import io
import os
import platform
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.7.12'
ASSET = 'actionlint_1.7.12_windows_amd64.zip'
ASSET_SHA256 = '6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9'
URL = 'https://github.com/rhysd/actionlint/releases/download/v' + VERSION + '/' + ASSET
MAX_BYTES = 32 * 1024 * 1024


def download():
    request = urllib.request.Request(URL, headers={'User-Agent': 'personal-ai-assistant-workflow-check'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(MAX_BYTES + 1)


def atomic_write(path, content):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_tool(cache, fetch=None):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ASSET
    if archive.exists():
        with archive.open('rb') as stream:
            content = stream.read(MAX_BYTES + 1)
    else:
        content = (fetch or download)()
    if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != ASSET_SHA256:
        raise ValueError('actionlint download/cache checksum mismatch; no checker executed.')
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        entries = [info for info in package.infolist() if info.filename == 'actionlint.exe']
        if len(entries) != 1 or entries[0].file_size > MAX_BYTES:
            raise ValueError('actionlint archive does not contain one bounded executable.')
        executable = package.read(entries[0])
    if not archive.exists():
        atomic_write(archive, content)
    binary = cache / 'bin' / 'actionlint.exe'
    binary.parent.mkdir(exist_ok=True)
    # Recheck the local executable against the verified archive on every use.
    if not binary.exists() or binary.stat().st_size != len(executable) or binary.read_bytes() != executable:
        atomic_write(binary, executable)
    return binary


def check(root=ROOT):
    root = Path(root).resolve()
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('This project workflow checker currently requires Windows x64.')
    workflows = sorted(path for path in (root / '.github' / 'workflows').glob('*')
                       if path.is_file() and path.suffix in ('.yml', '.yaml'))
    if not workflows:
        raise ValueError('No GitHub workflows found; validation was not performed.')
    binary = ensure_tool(root / 'data' / 'tools' / ('actionlint-' + VERSION))
    # Disable optional external shell/Python linters; actionlint still checks
    # YAML, workflow structure, expressions and action inputs. No shell eval.
    subprocess.run([str(binary), '-shellcheck=', '-pyflakes=', *map(str, workflows)],
                   cwd=root, check=True, timeout=60)
    print(f'Workflow syntax checked by actionlint {VERSION}: {len(workflows)} file(s).')


if __name__ == '__main__':
    try:
        check()
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print('Workflow validation failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
