"""Content-addressed AdamW checkpoints for the bounded continuation experiments."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def tensor_bytes(tensor):
    import torch
    return tensor.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes()


def commit_bytes(destination, raw):
    folder = destination.parent
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix='.partial', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(folder, tensors, metadata):
    import torch
    from safetensors.torch import save

    if 'metadata' in tensors:
        raise ValueError('Reserved checkpoint key')
    encoded = json.dumps(metadata, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
    raw = save({**tensors, 'metadata': torch.tensor(list(encoded), dtype=torch.uint8)})
    if len(raw) > MAX_BYTES:
        raise ValueError('Checkpoint exceeds size limit')
    sha = digest(raw)
    commit_bytes(Path(folder) / (sha + '.safetensors'), raw)
    return sha


def read(folder, sha, binding):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('Invalid checkpoint identifier')
    with (Path(folder) / (sha + '.safetensors')).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or digest(raw) != sha:
        raise ValueError('Checkpoint digest mismatch')
    import torch
    from safetensors.torch import load
    tensors = load(raw)
    header = tensors.pop('metadata')
    if header.dtype != torch.uint8 or header.ndim != 1 or header.numel() > 2**20:
        raise ValueError('Invalid checkpoint metadata')
    metadata = json.loads(tensor_bytes(header))
    schema = 'cuda_adamw_boundary_v1' if binding.get('device') == 'cuda:0' else 'cpu_adamw_boundary_v1'
    if not isinstance(metadata, dict) or metadata.get('schema') != schema or metadata.get('binding') != binding:
        raise ValueError('Checkpoint source, recipe or data binding mismatch')
    return tensors, metadata


def parameters(model, optimizer, device):
    named = {name: p for name, p in model.named_parameters() if p.requires_grad}
    if len(optimizer.param_groups) != 1 or [id(p) for p in optimizer.param_groups[0]['params']] != [id(p) for p in named.values()]:
        raise ValueError('Expected one ordered optimizer group for all trainable parameters')
    if device not in ('cpu', 'cuda:0') or not named or any(str(p.device) != device for p in named.values()):
        raise ValueError('Expected trainable tensors on the bound device')
    return named


def capture(folder, binding, model, optimizer, sampler, order, cursor, history):
    import torch

    device = binding.get('device', 'cpu')
    named = parameters(model, optimizer, device)
    tensors = {'torch_rng': torch.get_rng_state()}
    if device == 'cuda:0':
        tensors['cuda_rng'] = torch.cuda.get_rng_state(device)
    for name, param in named.items():
        tensors['weight/' + name] = param.detach().cpu().contiguous()
        state = optimizer.state[param]
        if set(state) != {'step', 'exp_avg', 'exp_avg_sq'}:
            raise ValueError('Unexpected AdamW state')
        for key, value in state.items():
            tensors[key + '/' + name] = value.detach().cpu().contiguous()
    schema = 'cuda_adamw_boundary_v1' if device == 'cuda:0' else 'cpu_adamw_boundary_v1'
    metadata = {'schema': schema, 'binding': binding, 'names': list(named),
                'step': len(history), 'order': order, 'cursor': cursor, 'sampler_rng': sampler.getstate(),
                'history': history}
    return publish(folder, tensors, metadata)


def restore(folder, sha, binding, model, optimizer, sampler):
    import random
    import torch

    tensors, meta = read(folder, sha, binding)
    device = binding.get('device', 'cpu')
    named = parameters(model, optimizer, device)
    expected = {'torch_rng'} | {key + '/' + name for name in named for key in ('weight', 'step', 'exp_avg', 'exp_avg_sq')}
    if device == 'cuda:0':
        expected.add('cuda_rng')
    if set(tensors) != expected or meta['names'] != list(named):
        raise ValueError('Checkpoint parameter names mismatch')
    order, cursor, history, step = (meta[k] for k in ('order', 'cursor', 'history', 'step'))
    if (not isinstance(order, list) or not all(type(i) is int for i in order)
            or type(cursor) is not int or not isinstance(history, list)
            or type(step) is not int or not 0 < step <= binding['recipe']['steps'] or len(history) != step):
        raise ValueError('Checkpoint training position mismatch')
    replay = random.Random(binding['recipe']['seed'])
    replay_order, replay_cursor = [], 0
    for row in history:
        if (not isinstance(row, dict) or set(row) != {'samples', 'loss'}
                or not isinstance(row['samples'], list) or len(row['samples']) != binding['recipe']['accumulation']
                or type(row['loss']) not in (float, int) or not math.isfinite(row['loss'])):
            raise ValueError('Invalid training history')
        for index in row['samples']:
            if replay_cursor == len(replay_order):
                replay_order = list(range(binding['row_count']))
                replay.shuffle(replay_order)
                replay_cursor = 0
            if type(index) is not int or index != replay_order[replay_cursor]:
                raise ValueError('Sample history mismatch')
            replay_cursor += 1
    if (order, cursor) != (replay_order, replay_cursor):
        raise ValueError('Checkpoint sampler position mismatch')
    for name, param in named.items():
        for key in ('weight', 'exp_avg', 'exp_avg_sq'):
            t = tensors[key + '/' + name]
            if t.shape != param.shape or t.dtype != param.dtype or not torch.isfinite(t).all():
                raise ValueError('Checkpoint tensor shape, dtype or contents mismatch')
        counter = tensors['step/' + name]
        if counter.shape != torch.Size([]) or counter.dtype != torch.float32 or counter.item() != step:
            raise ValueError('Checkpoint optimizer step mismatch')
    version, state, gaussian = meta['sampler_rng']
    rng_state = (version, tuple(state), gaussian)
    if rng_state != replay.getstate():
        raise ValueError('Checkpoint sampler RNG mismatch')
    torch.Generator().set_state(tensors['torch_rng'])
    if device == 'cuda:0':
        torch.Generator(device=device).set_state(tensors['cuda_rng'])
    states = {name: {'step': tensors['step/' + name],
                     'exp_avg': tensors['exp_avg/' + name].to(device),
                     'exp_avg_sq': tensors['exp_avg_sq/' + name].to(device)} for name in named}
    with torch.no_grad():
        for name, param in named.items():
            param.copy_(tensors['weight/' + name])
            optimizer.state[param] = states[name]
    sampler.setstate(rng_state)
    torch.set_rng_state(tensors['torch_rng'])
    if device == 'cuda:0':
        torch.cuda.set_rng_state(tensors['cuda_rng'], device)
    return order, cursor, history
