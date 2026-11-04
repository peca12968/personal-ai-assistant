"""CPU-only, synthetic Qwen3/LoRA interruption and continuation experiment."""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.learning import checkpoint_state as ck

STEPS = 17
ROWS = [[1, 7+i, 4, 16+i, 5, 23-i, 2] for i in range(7)]
RECIPE = {'steps': STEPS, 'accumulation': 3, 'seed': 827, 'lr': 0.006, 'dropout': 0.2}
MODES = {'continuous', 'crash', 'segment', 'resumed', 'drop-moments', 'reset-rng', 'reset-sampler', 'reject'}
PARENT = ROOT/'data/learning/checkpoint-probes'


def write(path, value):
    ck.commit_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf8'))


def read(path):
    return json.loads(path.read_text('utf8'))


def worker(folder, mode, reference=None):
    folder = folder.resolve(strict=True)
    if folder.parent != PARENT.resolve() or mode not in MODES:
        raise ValueError('Unknown experiment directory or phase')
    import torch
    import transformers
    import peft
    from peft import LoraConfig, get_peft_model
    from transformers import Qwen3Config, Qwen3ForCausalLM

    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    seed = RECIPE['seed']
    torch.manual_seed(seed)
    config = Qwen3Config(vocab_size=32, hidden_size=32, intermediate_size=48, num_hidden_layers=2,
                         num_attention_heads=4, num_key_value_heads=2, head_dim=8, max_position_embeddings=64,
                         attention_dropout=0.0, pad_token_id=0, eos_token_id=2, use_cache=False)
    config._attn_implementation = 'eager'
    model = get_peft_model(Qwen3ForCausalLM(config), LoraConfig(r=4, lora_alpha=8, lora_dropout=RECIPE['dropout'],
                           target_modules=['q_proj','v_proj'], bias='none', task_type='CAUSAL_LM'))
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=RECIPE['lr'], weight_decay=0)
    frozen = hashlib.sha256()
    initial_adapter = hashlib.sha256()
    for name, param in model.named_parameters():
        fingerprint = initial_adapter if param.requires_grad else frozen
        fingerprint.update(name.encode())
        fingerprint.update(ck.tensor_bytes(param))
    binding = {'recipe': RECIPE, 'row_count': len(ROWS), 'rows_sha256': ck.digest(json.dumps(ROWS).encode()),
               'frozen_base_sha256': frozen.hexdigest(), 'initial_adapter_sha256': initial_adapter.hexdigest(),
               'sources': {p.name: ck.digest(p.read_bytes()) for p in (Path(__file__), Path(ck.__file__))},
               'runtime': [str(torch.__version__), transformers.__version__, peft.__version__, sys.version],
               'optimizer': json.loads(json.dumps({k:v for k,v in optimizer.param_groups[0].items() if k != 'params'}))}
    sampler = random.Random(seed)
    order, cursor, history = [], 0, []
    blobs = folder / 'checkpoints'
    if mode == 'reject':
        reject_invalid(blobs, reference, binding, model, optimizer, sampler)
        return
    if reference:
        order, cursor, history = ck.restore(blobs, reference, binding, model, optimizer, sampler)
    if mode == 'drop-moments':
        for buffers in optimizer.state.values():
            buffers['exp_avg'].zero_()
            buffers['exp_avg_sq'].zero_()
    if mode == 'reset-rng':
        torch.manual_seed(seed)
    if mode == 'reset-sampler':
        sampler.seed(seed)
    target = 7 if mode == 'crash' else 11 if mode == 'segment' else STEPS
    model.train()
    while len(history) < target:
        optimizer.zero_grad(set_to_none=True)
        indices, losses = [], []
        for _ in range(RECIPE['accumulation']):
            if cursor == len(order):
                order = list(range(len(ROWS)))
                sampler.shuffle(order)
                cursor = 0
            index = order[cursor]; cursor += 1
            ids = torch.tensor([ROWS[index]], dtype=torch.long)
            labels = ids.clone(); labels[:, :4] = -100
            loss = model(input_ids=ids, labels=labels, use_cache=False).loss
            (loss / RECIPE['accumulation']).backward()
            indices.append(index); losses.append(loss.item())
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
        optimizer.step()
        history.append({'samples': indices, 'loss': sum(losses)/len(losses)})
        if mode == 'crash' and len(history) == 5:
            sha = ck.capture(blobs, binding, model, optimizer, sampler, order, cursor, history)
            write(folder/'committed.json', {'sha': sha, 'step': 5, 'binding': binding})
    if mode == 'crash':
        # The next blob has been flushed but is not committed to its digest name.
        with patch.object(ck.os, 'replace', side_effect=lambda *_: os._exit(73)):
            ck.capture(blobs, binding, model, optimizer, sampler, order, cursor, history)
        raise RuntimeError('Failure injection did not interrupt publication')
    # Negative controls intentionally have incomplete optimizer or RNG state;
    # compare completed trainable weights independently of the checkpoint header.
    h = hashlib.sha256()
    for name, param in model.named_parameters():
        if param.requires_grad:
            h.update(name.encode()); h.update(ck.tensor_bytes(param))
    model.eval()
    with torch.inference_mode():
        logits = model(torch.tensor([[1, 9, 4, 8, 5, 6]]), use_cache=False).logits
    sha = None
    if mode in ('continuous', 'segment', 'resumed'):
        sha = ck.capture(blobs, binding, model, optimizer, sampler, order, cursor, history)
    write(folder/(mode+'.json'), {'pid': os.getpid(), 'step': len(history), 'sha': sha, 'weights': h.hexdigest(),
                                 'logits': ck.digest(ck.tensor_bytes(logits)), 'history': history})


def reject_invalid(blobs, reference, binding, model, optimizer, sampler):
    import torch
    tensors, header = ck.read(blobs, reference, binding)
    original = {name: param.detach().clone() for name, param in model.named_parameters()}
    sampler_before, torch_before = sampler.getstate(), torch.get_rng_state()

    def expect_failure(folder, sha, proposed, message):
        try:
            ck.restore(folder, sha, proposed, model, optimizer, sampler)
        except ValueError as exc:
            if message not in str(exc):
                raise
        else:
            raise ValueError('Invalid checkpoint was accepted')
        if (optimizer.state or sampler.getstate() != sampler_before or not torch.equal(torch_before, torch.get_rng_state())
                or any(not torch.equal(original[n], p) for n, p in model.named_parameters())):
            raise ValueError('Failure left a partial restore')

    expect_failure(blobs, reference, {**binding, 'rows_sha256': '0'*64}, 'binding mismatch')
    bad = blobs.parent/'invalid'
    bad.mkdir()
    (bad/(reference+'.safetensors')).write_bytes(b'truncated')
    expect_failure(bad, reference, binding, 'digest mismatch')
    sha = ck.publish(bad, tensors, {**header, 'cursor': -1})
    expect_failure(bad, sha, binding, 'sampler position mismatch')
    key = 'exp_avg/' + header['names'][-1]
    sha = ck.publish(bad, {**tensors, key: torch.zeros(1, dtype=torch.float64)}, header)
    expect_failure(bad, sha, binding, 'tensor shape, dtype or contents mismatch')
    write(blobs.parent/'rejections.json', {'verified':['binding','digest','cursor','optimizer_tensor'],
                                           'restoration_state_unchanged': True})


def run():
    python = ROOT/'data/learning/runtime'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        raise ValueError('找不到獨立學習環境。')
    parent = PARENT
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='cpu-', dir=parent))
    print('本機實驗目錄', folder, flush=True)
    env = {**os.environ, 'CUDA_VISIBLE_DEVICES': '-1', 'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1'}
    def call(mode, sha=None, expected=0):
        command = [str(python), '-I', str(Path(__file__).resolve()), '_worker', str(folder), mode]
        if sha:
            command.append(sha)
        with (folder/(mode+'.log')).open('wb') as log:
            result = subprocess.run(command, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode != expected:
            raise ValueError(f'{mode} 結束碼 {result.returncode}，詳見實驗目錄。')
        print(mode, 'verified exit', expected, flush=True)
    call('continuous')
    call('crash', expected=73)
    committed = read(folder/'committed.json')
    if not list((folder/'checkpoints').glob('*.partial')):
        raise ValueError('缺少已中斷寫入的證據。')
    call('segment', committed['sha'])
    call('resumed', read(folder/'segment.json')['sha'])
    direct, resumed = (read(folder/(name+'.json')) for name in ('continuous','resumed'))
    for key in ('sha','weights','logits','history','step'):
        if direct[key] != resumed[key]:
            raise ValueError('連續訓練與接續結果不同：'+key)
    for mode in ('drop-moments','reset-rng','reset-sampler'):
        call(mode, committed['sha'])
        if read(folder/(mode+'.json'))['weights'] == direct['weights']:
            raise ValueError('缺失狀態的負控制未檢出差異：'+mode)
    call('reject', committed['sha'])
    write(folder/'result.json', {'status':'verified','cpu_only':True,'steps':STEPS,'boundary_steps':[5,11],
                               'weights':direct['weights'],'final_checkpoint':direct['sha'],'logits':direct['logits'],
                               'equal':['training_history','checkpoint_bytes','trainable_weights','evaluation_logits'],
                               'negative_controls_changed_weights':['drop-moments','reset-rng','reset-sampler']})
    print('Synthetic CPU continuation verified.')


if __name__ == '__main__':
    if sys.argv[1:] == ['run']:
        run()
    elif len(sys.argv) in (4,5) and sys.argv[1] == '_worker':
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv)==5 else None)
    else:
        raise SystemExit('Usage: python scripts/learning/check_checkpoint.py run')
