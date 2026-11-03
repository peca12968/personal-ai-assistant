"""Local model selection and evidence, without importing optional ML packages."""
import json
import re
from pathlib import Path

from scripts.learning.fetch_model import validate_manifest, verify
from scripts.learning.protocol import digest, file_hash

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT_TOKENS = 24576
MAX_OUTPUT_TOKENS = 512
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024 + 8192


def read_json(path, limit=2 * 1024 * 1024):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Local model metadata exceeds limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate metadata key')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite metadata')))


def select(experiment=None):
    manifest_path = ROOT/'scripts/learning/model.json'
    manifest = validate_manifest(read_json(manifest_path, 65536))
    value = {'schema': 'local_model_selection_v1', 'model_id': manifest['model_id'],
             'revision': manifest['revision'], 'manifest_sha256': file_hash(manifest_path),
             'experiment': experiment, 'adapter_files': None, 'evidence_sha256': {}}
    if experiment is not None:
        if not isinstance(experiment, str) or not re.fullmatch('[a-z0-9_-]{1,64}', experiment):
            raise ValueError('研究版本名稱需為1到64個小寫英數字、-或_。')
        folder = ROOT/'data/learning/experiments'/experiment
        # Research adapters are deliberately opt-in and bound to the complete
        # frozen experiment. They are not the daily model's automatic upgrade.
        from scripts.learning.experiment import load_frozen
        from scripts.learning.protocol import PHASES, assess
        for name in ('manifest.json', 'suite.json'):
            read_json(folder/name)
        load_frozen(folder)
        reports = {phase: read_json(folder/(phase+'.json')) for phase in PHASES}
        assessment = read_json(folder/'assessment.json')
        if assessment != assess(reports) or not assessment['passed']:
            raise ValueError('研究參數未通過完整學習、保留與重載驗證。')
        files = reports['stage-b'].get('adapter_files')
        if (not isinstance(files, dict) or set(files) != {'README.md', 'adapter_config.json', 'adapter_model.safetensors'}
                or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in files.values())):
            raise ValueError('研究參數檔案清單不符。')
        value['adapter_files'] = files
        names = ['manifest.json', 'suite.json', 'assessment.json', *(phase+'.json' for phase in PHASES)]
        value['evidence_sha256'] = {name: file_hash(folder/name) for name in names}
    value['fingerprint'] = digest(value)
    return value


def description(selection):
    return {'kind': 'local_qwen', 'model': selection['model_id'], 'revision': selection['revision'],
            'parameter_version': selection['experiment'] or 'unadapted_base',
            'selection_fingerprint': selection['fingerprint'], 'runs_on_this_computer': True,
            'supports_images': False, 'automatic_learning_enabled': False,
            'research_adapter': selection['experiment'] is not None,
            'input_token_limit': MAX_INPUT_TOKENS, 'output_token_limit': MAX_OUTPUT_TOKENS}


def verify_assets(selection):
    if select(selection.get('experiment')) != selection:
        raise ValueError('Selected model metadata changed')
    manifest = validate_manifest(read_json(ROOT/'scripts/learning/model.json', 65536))
    base = ROOT/'data/learning/models/Qwen3-4B'/manifest['revision']
    for item in manifest['files']:
        if not verify(base/item['name'], item):
            raise ValueError('Pinned base asset missing or changed')
    adapter = None
    if selection['experiment'] is not None:
        adapter = ROOT/'data/learning/experiments'/selection['experiment']/'stage-b-adapter'
        for name, expected in selection['adapter_files'].items():
            if file_hash(adapter/name) != expected:
                raise ValueError('Adapter asset changed')
        config = read_json(adapter/'adapter_config.json', 65536)
        if config.get('peft_type') != 'LORA' or config.get('task_type') != 'CAUSAL_LM':
            raise ValueError('Unsupported adapter architecture')
    return base, adapter
