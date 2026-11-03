"""Explicit, bounded CUDA LoRA experiment in a separate optional Python environment.

Each phase is a fresh process. Never reads the personal assistant database, promotes
an adapter, launches a scheduler, or uploads data. Default command is offline.
"""
import argparse
import gc
import importlib.metadata
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.learning.protocol import HERE, ROOT, PHASES, SETTINGS, EXPECTED_VERSIONS, assess, digest, file_hash, make_suite, sources, training_rows
from scripts.learning.fetch_model import validate_manifest, verify
from scripts.learning.runtime import gpu_lease


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def local_folder(name):
    if not name or len(name) > 64 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in name):
        raise ValueError('Run name must contain 1..64 lowercase ASCII letters, digits, - or _')
    return ROOT / 'data/learning/experiments' / name


def initialize(name):
    folder = local_folder(name)
    folder.mkdir(parents=True, exist_ok=False)
    suite = make_suite()
    save(folder/'suite.json', suite)
    save(folder/'manifest.json', {'schema': 'weight_experiment_v1', 'sources': sources(),
         'suite_sha256': digest(suite), 'settings': SETTINGS, 'created': datetime.now(timezone.utc).isoformat(),
         'model': json.loads((HERE/'model.json').read_text(encoding='utf-8'))})
    print(str(folder), flush=True)


def load_frozen(folder):
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    suite = json.loads((folder/'suite.json').read_text(encoding='utf-8'))
    if (manifest['sources'] != sources() or manifest['suite_sha256'] != digest(suite)
            or manifest['settings'] != SETTINGS or suite != make_suite()):
        raise ValueError('Frozen source, configuration or suite changed; use a new experiment')
    return manifest, suite


def configure():
    os.environ['HF_HOME'] = str(ROOT/'data/learning/huggingface')
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'


def adapter_hash(model):
    import hashlib
    from peft import get_peft_model_state_dict
    h = hashlib.sha256()
    for key, tensor in sorted(get_peft_model_state_dict(model).items()):
        h.update(key.encode('utf-8'))
        h.update(str(tuple(tensor.shape)).encode('ascii'))
        # Float32 is exact for stored BF16/FP32 adapters; stable across save/reload.
        h.update(tensor.detach().float().cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


def prompt_ids(tokenizer, prompt):
    return tokenizer.apply_chat_template(
        [{'role': 'system', 'content': SETTINGS['system']}, {'role': 'user', 'content': prompt}],
        tokenize=True, add_generation_prompt=True, enable_thinking=False)


def encode_row(tokenizer, row):
    prefix = prompt_ids(tokenizer, row['prompt'])
    target = tokenizer.encode(row['answer'] + tokenizer.eos_token, add_special_tokens=False)
    if not target or len(prefix) + len(target) > SETTINGS['max_sequence_tokens']:
        raise ValueError('Training sequence exceeds fixed limit; never truncate silently')
    return prefix + target, [-100] * len(prefix) + target


def evaluate(model, tokenizer, suite, folder, phase):
    import torch
    model.eval()
    model.config.use_cache = True
    rows = []
    with torch.inference_mode():
        for case in suite['evaluation']:
            ids = prompt_ids(tokenizer, case['prompt'])
            if len(ids) > SETTINGS['max_sequence_tokens']:
                raise ValueError('Evaluation prompt exceeds limit')
            inputs = torch.tensor([ids], device='cuda')
            output = model.generate(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                                    max_new_tokens=SETTINGS['max_new_tokens'], do_sample=False,
                                    pad_token_id=tokenizer.eos_token_id, use_cache=True)
            answer = tokenizer.decode(output[0, len(ids):], skip_special_tokens=True).strip()
            row = {**case, 'output': answer, 'correct': answer.casefold() == case['answer'].casefold(),
                   'generated_tokens': int(output.shape[1] - len(ids))}
            rows.append(row)
            save(folder/(phase+'-evaluation.partial.json'), rows)
            print(f"{phase} {case['id']}: {row['correct']} {answer[:100]!r}", flush=True)
    return rows


def train(model, tokenizer, suite, phase, folder):
    import torch
    rows = [encode_row(tokenizer, row) for row in training_rows(suite, phase)]
    rng = random.Random(SETTINGS['seed'])
    steps = SETTINGS['steps_a'] if phase == 'stage-a' else SETTINGS['steps_b']
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    model.train()
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                 lr=SETTINGS['learning_rate'], weight_decay=0.0)
    order, cursor, history = [], 0, []
    deadline = time.monotonic() + 1800
    for step in range(steps):
        if time.monotonic() >= deadline:
            raise TimeoutError('Training exceeded the 30-minute experiment budget')
        optimizer.zero_grad(set_to_none=True)
        loss_sum = 0.0
        for _ in range(SETTINGS['accumulation']):
            if cursor == len(order):
                order = list(range(len(rows))); rng.shuffle(order); cursor = 0
            ids, labels = rows[order[cursor]]; cursor += 1
            tokens = torch.tensor([ids], device='cuda')
            result = model(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                           labels=torch.tensor([labels], device='cuda'), use_cache=False)
            if not torch.isfinite(result.loss):
                raise ValueError('Non-finite training loss; candidate rejected')
            loss_sum += float(result.loss.detach())
            (result.loss / SETTINGS['accumulation']).backward()
            del result
        norm = torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
        if not torch.isfinite(norm):
            raise ValueError('Non-finite gradient; candidate rejected')
        optimizer.step()
        history.append({'step': step+1, 'loss': loss_sum / SETTINGS['accumulation']})
        save(folder/(phase+'-training.partial.json'), history)
        if (step + 1) % 10 == 0:
            print(f"{phase} step {step+1}/{steps}, loss {history[-1]['loss']:.5f}", flush=True)
    del optimizer
    model.gradient_checkpointing_disable()
    gc.collect(); torch.cuda.empty_cache()
    return history


def run_phase(name, phase):
    folder = local_folder(name)
    manifest, suite = load_frozen(folder)
    index = PHASES.index(phase)
    if index:
        previous = json.loads((folder/(PHASES[index-1]+'.json')).read_text(encoding='utf-8'))
        if previous['status'] != 'complete':
            raise ValueError('Previous phase did not complete')
    # Exclusive creation prevents duplicate/concurrent phases and silent retry.
    with (folder/(phase+'.started.json')).open('x', encoding='utf-8') as stream:
        json.dump({'pid': os.getpid(), 'started': datetime.now(timezone.utc).isoformat()}, stream)
    result = {'phase': phase, 'status': 'running', 'pid': os.getpid(), 'started': time.time()}
    try:
        configure()
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import LoraConfig, PeftModel, get_peft_model
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError('This experiment requires CUDA with BF16; no silent CPU fallback')
        torch.set_num_threads(8)
        torch.manual_seed(SETTINGS['seed']); torch.cuda.manual_seed_all(SETTINGS['seed'])
        result['environment'] = {name: importlib.metadata.version(name) for name in
                                 ('torch', 'transformers', 'peft', 'accelerate', 'safetensors')}
        if result['environment'] != EXPECTED_VERSIONS:
            raise ValueError('Optional environment does not match pinned package versions')
        result['installed_packages'] = sorted((dist.metadata['Name'], dist.version)
                                              for dist in importlib.metadata.distributions())
        if index and result['installed_packages'] != [tuple(row) for row in previous['installed_packages']]:
            raise ValueError('Installed packages changed between experiment phases')
        result['gpu'] = torch.cuda.get_device_name(0)
        model_manifest = validate_manifest(manifest['model'])
        model_dir = ROOT/'data/learning/models/Qwen3-4B'/model_manifest['revision']
        for item in model_manifest['files']:
            if not verify(model_dir/item['name'], item):
                raise ValueError('Pinned model file failed verification: '+item['name'])
        tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True,
                    trust_remote_code=False, use_safetensors=True, torch_dtype=torch.bfloat16,
                    device_map={'': 0}, attn_implementation='sdpa')
        if phase == 'stage-a':
            model = get_peft_model(model, LoraConfig(r=SETTINGS['rank'], lora_alpha=SETTINGS['alpha'],
                        lora_dropout=SETTINGS['dropout'], target_modules=SETTINGS['target_modules'],
                        bias='none', task_type='CAUSAL_LM'))
        elif phase in ('stage-b', 'reload-b'):
            origin = 'stage-a' if phase == 'stage-b' else 'stage-b'
            previous = json.loads((folder/(origin+'.json')).read_text(encoding='utf-8'))
            adapter_dir = folder/(origin+'-adapter')
            if {p.name: file_hash(p) for p in adapter_dir.iterdir() if p.is_file()} != previous['adapter_files']:
                raise ValueError('Saved adapter changed')
            model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=phase == 'stage-b',
                                             local_files_only=True)
        if phase != 'baseline':
            result['weights_before'] = adapter_hash(model)
            if phase in ('stage-b', 'reload-b') and result['weights_before'] != previous['weights_after']:
                raise ValueError('Loaded adapter weights differ from recorded predecessor')
        if phase in ('stage-a', 'stage-b'):
            result['trainable_parameters'] = sum(p.numel() for p in model.parameters() if p.requires_grad)
            result['training_rows_sha256'] = digest(training_rows(suite, phase))
            result['training'] = train(model, tokenizer, suite, phase, folder)
            target = folder/(phase+'-adapter')
            target.mkdir(exist_ok=False)
            model.save_pretrained(target, safe_serialization=True)
            result['adapter_files'] = {p.name: file_hash(p) for p in target.iterdir() if p.is_file()}
        if phase != 'baseline':
            result['weights_after'] = adapter_hash(model)
        result['evaluation'] = evaluate(model, tokenizer, suite, folder, phase)
        result['peak_allocated_bytes'] = torch.cuda.max_memory_allocated()
        load_frozen(folder)
        result['status'] = 'complete'
    except BaseException as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        result['ended'] = time.time()
        save(folder/(phase+'.json'), result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'init', *PHASES, 'report'), nargs='?', default='check')
    parser.add_argument('--run')
    args = parser.parse_args()
    if args.command == 'check':
        suite = make_suite()
        assert len(suite['training']) == 112 and len(suite['evaluation']) == 40
        assert not ({r['prompt'] for r in suite['training']} & {r['prompt'] for r in suite['evaluation']})
        print('Offline protocol validated; no ML imports, model calls or training.')
    elif args.command == 'init':
        initialize(args.run)
    elif args.command == 'report':
        folder = local_folder(args.run); load_frozen(folder)
        result = assess({p: json.loads((folder/(p+'.json')).read_text(encoding='utf-8')) for p in PHASES})
        with (folder/'assessment.json').open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        with gpu_lease():
            run_phase(args.run, args.command)


if __name__ == '__main__':
    main()
