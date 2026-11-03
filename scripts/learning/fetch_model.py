"""Fetch only pinned official model assets into the ignored local learning cache."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name('model.json')


def validate_manifest(value):
    if (not isinstance(value, dict) or value.get('model_id') != 'Qwen/Qwen3-4B'
            or not isinstance(value.get('revision'), str) or not re.fullmatch('[0-9a-f]{40}', value['revision'])
            or not isinstance(value.get('files'), list) or not 1 <= len(value['files']) <= 32):
        raise ValueError('Invalid pinned model manifest')
    seen = set()
    for item in value['files']:
        if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                or not re.fullmatch('[A-Za-z0-9_.-]+', item['name']) or item['name'] in {'.', '..'}
                or item['name'] in seen or type(item.get('bytes')) is not int or not 0 < item['bytes'] <= 5*1024**3):
            raise ValueError('Invalid model asset')
        algorithm = 'sha256' if 'sha256' in item else 'git_blob_sha1'
        if (set(item) != {'name', 'bytes', algorithm}
                or not isinstance(item.get(algorithm), str)
                or not re.fullmatch('[0-9a-f]{64}' if algorithm == 'sha256' else '[0-9a-f]{40}', item[algorithm])):
            raise ValueError('Missing pinned asset hash')
        seen.add(item['name'])
    if sum(item['bytes'] for item in value['files']) > 10*1024**3:
        raise ValueError('Model exceeds the pinned download budget')
    return value


def verify(path, item):
    if not path.is_file() or path.stat().st_size != item['bytes']:
        return False
    digest = hashlib.sha256() if 'sha256' in item else hashlib.sha1()
    if 'git_blob_sha1' in item:
        digest.update(f"blob {item['bytes']}\0".encode())
    with path.open('rb') as stream:
        while block := stream.read(1024*1024):
            digest.update(block)
    return digest.hexdigest() == item.get('sha256', item.get('git_blob_sha1'))


def fetch_asset(folder, manifest, item):
    destination = folder/item['name']
    if verify(destination, item):
        print(item['name']+': verified existing file', flush=True)
        return
    if destination.exists():
        raise ValueError('Existing model file failed verification; preserved: '+str(destination))
    partial = folder/(item['name']+'.part')
    url = f"https://huggingface.co/{manifest['model_id']}/resolve/{manifest['revision']}/{item['name']}?download=true"
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item['bytes']:
            raise ValueError('Partial file exceeds expected size; preserved')
        if offset == item['bytes']:
            break
        headers = {'User-Agent': 'personal-ai-assistant-model-fetch', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status == 206:
                    if response.headers.get('Content-Range') != f"bytes {offset}-{item['bytes']-1}/{item['bytes']}":
                        raise ValueError('Unexpected partial response range')
                elif response.status == 200:
                    offset = 0
                else:
                    raise ValueError('Unexpected download response')
                with partial.open('ab' if offset else 'wb') as stream:
                    last = time.monotonic()
                    while block := response.read(1024*1024):
                        if offset + len(block) > item['bytes']:
                            raise ValueError('Response exceeds pinned model asset size')
                        stream.write(block); offset += len(block)
                        if time.monotonic()-last >= 10:
                            print(f"{item['name']}: {offset}/{item['bytes']} bytes", flush=True)
                            last = time.monotonic()
            if offset != item['bytes']:
                raise OSError('Incomplete model response')
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2)
    if not verify(partial, item):
        raise ValueError('Downloaded model asset hash mismatch; partial file preserved')
    partial.replace(destination)
    print(item['name']+': verified', flush=True)


def main():
    manifest = validate_manifest(json.loads(MANIFEST.read_text(encoding='utf-8')))
    folder = ROOT/'data/learning/models/Qwen3-4B'/manifest['revision']
    folder.mkdir(parents=True, exist_ok=True)
    for item in manifest['files']:
        fetch_asset(folder, manifest, item)
    (folder/'verified-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print('Pinned base model ready: '+str(folder), flush=True)


if __name__ == '__main__':
    main()
