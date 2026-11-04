"""Single owned offline inference process for the qualified segmented adapter."""
import gc
import importlib.metadata
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from assistant import local_models
from assistant.engine import MAX_PROMPT_BYTES
from assistant.local_worker import SYSTEM_GUIDANCE, Failure
from scripts.learning import qualified_segment as qualified
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.runtime import gpu_lease


def infer(folder):
    configure()
    if any(importlib.metadata.version(k) != v for k, v in EXPECTED_VERSIONS.items()):
        raise Failure('environment')
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, StaticCache

    _, batch, description, _, weights = qualified.qualify()
    request = local_models.read_json(folder / 'request.json', local_models.MAX_REQUEST_BYTES)
    prompt = request['prompt']
    if request['description'] != description or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise Failure('invalid_request')
    base, _ = local_models.verify_assets(batch['model'])
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise Failure('gpu_unavailable')
    torch.set_num_threads(8)
    torch.manual_seed(20261104)
    torch.cuda.set_per_process_memory_fraction(0.85)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    system = SYSTEM_GUIDANCE + '\n以下是程式提供的本次實際選用引擎資訊。回答引擎相關問題時以此為準；其他問題不需重述。\n' + json.dumps(description, ensure_ascii=False)
    ids = tokenizer.apply_chat_template([dict(role='system', content=system), dict(role='user', content=prompt)],
                                        tokenize=True, add_generation_prompt=True, enable_thinking=False)
    if not 0 < len(ids) <= local_models.MAX_INPUT_TOKENS:
        raise Failure('input_too_large')
    model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
        use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
    model = PeftModel.from_pretrained(model, qualified.RELEASE / 'short-001/adapter', is_trainable=False, local_files_only=True)
    if adapter_hash(model) != weights:
        raise Failure('model_changed')
    model.eval()
    gc.collect()
    torch.cuda.empty_cache()
    tokens = torch.tensor([ids], device='cuda')
    with torch.inference_mode():
        cache = StaticCache(config=model.config, max_cache_len=len(ids) + local_models.MAX_OUTPUT_TOKENS)
        for start in range(0, len(ids) - 1, 256):
            end = min(start + 256, len(ids) - 1)
            output = model(input_ids=tokens[:, start:end], attention_mask=torch.ones((1, end), device='cuda', dtype=torch.long),
                           past_key_values=cache, use_cache=True, logits_to_keep=1)
            cache = output.past_key_values
            if cache.get_seq_length() != end:
                raise Failure('inference_failed')
            del output
        output = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), past_key_values=cache,
            max_new_tokens=local_models.MAX_OUTPUT_TOKENS, disable_compile=True, do_sample=False,
            pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id, logits_to_keep=1, use_cache=True)
    generated = output[0, len(ids):]
    if len(generated) >= local_models.MAX_OUTPUT_TOKENS and int(generated[-1]) != tokenizer.eos_token_id:
        raise Failure('generation_limit')
    answer = tokenizer.decode(generated, skip_special_tokens=True).strip()
    if not answer:
        raise Failure('empty_response')
    return dict(answer=answer, fingerprint=description['selection_fingerprint'])


def main(folder):
    with (folder / 'chat.started').open('x', encoding='ascii') as f:
        f.write(str(os.getpid()))
    try:
        with gpu_lease():
            result = infer(folder)
    except Failure as exc:
        result = dict(error=exc.code)
    except RuntimeError as exc:
        if 'out of memory' in str(exc).lower():
            result = dict(error='gpu_memory')
        elif 'lease' in str(exc).lower() or 'busy' in str(exc).lower():
            result = dict(error='gpu_busy')
        else:
            raise
    qualified.segments.gpu.save(folder / 'result.json', result)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
