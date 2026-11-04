"""Optional-runtime worker for deterministic seeded-LoRA segments from the base model."""
import importlib.metadata
import json
import os
import random
import time

from scripts.learning import check_gpu_checkpoint as gpu
from scripts.learning.batch_worker import encode, prompt_ids, evaluate
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from assistant import local_models, learning_batches as batches


def run(manifest, plan, request, folder, job, check_sources):
    deadline = time.monotonic()+360
    configure()
    if os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
        raise ValueError('CUDA 工作區設定不符。')
    environment = {name: importlib.metadata.version(name) for name in EXPECTED_VERSIONS}
    if environment != EXPECTED_VERSIONS or manifest['parent'] is not None:
        raise ValueError('底座分段的環境或起始模型不符。')
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import LoraConfig, get_peft_model
    from torch.nn.attention import sdpa_kernel, SDPBackend
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
    def check():
        gpu.input_gate(request['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('底座分段工作達到六分鐘時限。')
        check_sources()
    check()
    recipe = {**manifest['recipe'], 'steps':plan['training_steps']}
    torch.set_num_threads(8)
    torch.manual_seed(recipe['seed'])
    torch.cuda.manual_seed_all(recipe['seed'])
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.cuda.set_per_process_memory_fraction(0.85)
    base, adapter = local_models.verify_assets(manifest['model'])
    if adapter is not None:
        raise ValueError('底座工作不可隱式載入適配器。')
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    rows = batches.training_rows(manifest)
    encoded = [encode(tokenizer, row) for row in rows]
    for case in batches.evaluation_cases(manifest):
        encode(tokenizer, case)
    model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
        use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'':0}, attn_implementation='sdpa')
    model = get_peft_model(model, LoraConfig(r=recipe['rank'], lora_alpha=recipe['alpha'],
        lora_dropout=recipe['dropout'], target_modules=recipe['target_modules'], bias='none', task_type='CAUSAL_LM'))
    initial = adapter_hash(model)
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.enable_input_require_grads()
    model.train()
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=recipe['learning_rate'], weight_decay=0.0)
    binding = dict(device='cuda:0', gpu=torch.cuda.get_device_name(0), capability=list(torch.cuda.get_device_capability()),
        cuda=torch.version.cuda, torch_build=str(torch.__version__), environment=environment,
        experiment=batches.queue.digest(plan), initial_adapter_sha256=initial, recipe=recipe,
        rows_sha256=batches.queue.digest(rows), row_count=len(rows), origin='seeded_base_lora_v1',
        model_sha256=batches.queue.digest(manifest['model']),
        optimizer=json.loads(json.dumps({k:v for k,v in optimizer.param_groups[0].items() if k != 'params'})))
    sampler = random.Random(recipe['seed'])
    order, cursor, history = [], 0, []
    start, target = request['start'], request['end']
    if not 0 <= start < target <= recipe['steps']:
        raise ValueError('分段步數範圍不符。')
    blobs = folder/'checkpoints'
    if request['phase'] == 'resumed':
        order, cursor, history = gpu.ck.restore(blobs, request['from_blob'], binding, model, optimizer, sampler)
    elif start != 0 or request['from_blob'] is not None:
        raise ValueError('底座起點不可從未提交的狀態恢復。')
    if len(history) != start:
        raise ValueError('已載入步數與指定起點不符。')
    with sdpa_kernel(SDPBackend.MATH):
        while len(history) < target:
            check()
            optimizer.zero_grad(set_to_none=True)
            samples, losses = [], []
            for _ in range(recipe['accumulation']):
                gpu.input_gate(request['admitted'])
                if cursor == len(order):
                    order = list(range(len(rows)))
                    sampler.shuffle(order)
                    cursor = 0
                index = order[cursor]
                cursor += 1
                ids, labels = encoded[index]
                tokens = torch.tensor([ids], device='cuda')
                result = model(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                               labels=torch.tensor([labels], device='cuda'), use_cache=False)
                if not torch.isfinite(result.loss):
                    raise ValueError('訓練損失不是有限值。')
                losses.append(float(result.loss.detach()))
                (result.loss/recipe['accumulation']).backward()
                samples.append(index)
                del result
            torch.nn.utils.clip_grad_norm_(trainable, 1.0, error_if_nonfinite=True)
            optimizer.step()
            history.append(dict(samples=samples, loss=sum(losses)/len(losses)))
        check()
        sha = gpu.ck.capture(blobs, binding, model, optimizer, sampler, order, cursor, history)
        record = dict(phase=request['phase'], sha=sha, consumed=request['from_blob'], binding=binding,
                      step=len(history), weights=adapter_hash(model), history=history, pid=os.getpid())
        if target == recipe['steps']:
            model.gradient_checkpointing_disable()
            model.eval()
            tokens = torch.tensor([prompt_ids(tokenizer, manifest['checks'][0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=tokens, use_cache=False).logits[0,-1].float()
                output = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), do_sample=False,
                    use_cache=True, max_new_tokens=recipe['max_output_tokens'],
                    pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id)
            record.update(logits_sha256=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
                          generated=output[0,len(tokens[0]):].tolist())
            gpu.ck.commit_bytes(job/(request['phase']+'.logits'), gpu.ck.tensor_bytes(logits))
            record['rows'] = evaluate(model, tokenizer, manifest, job, request['phase'], deadline)
    check()
    gpu.save(job/(request['phase']+'.json'), record)
