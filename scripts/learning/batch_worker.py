"""One finite GPU phase over a frozen confirmed-example manifest. No database reads."""
import argparse
import gc
import importlib.metadata
import os
import random
import sys
import time
from pathlib import Path

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant import learning_batches as batches, local_models
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.runtime import gpu_lease


def prompt_ids(tokenizer, prompt):
    return tokenizer.apply_chat_template([{'role':'system','content':batches.RECIPE['system']},
        {'role':'user','content':prompt}],tokenize=True,add_generation_prompt=True,enable_thinking=False)


def encode(tokenizer, row):
    prefix=prompt_ids(tokenizer,row['prompt'])
    target=tokenizer.encode(row['answer']+tokenizer.eos_token,add_special_tokens=False)
    if not prefix or not target or len(target)>batches.RECIPE['max_output_tokens'] or len(prefix)+len(target)>batches.RECIPE['max_sequence_tokens']:
        raise ValueError('確認問答超過固定訓練／回答token額度，沒有裁切。')
    return prefix+target,[-100]*len(prefix)+target


def evaluate(model, tokenizer, manifest, path, phase, deadline):
    import torch
    model.eval(); rows=[]
    with torch.inference_mode():
        for case in batches.evaluation_cases(manifest):
            if time.monotonic()>=deadline: raise TimeoutError('有限評估逾時。')
            ids=prompt_ids(tokenizer,case['prompt'])
            tokens=torch.tensor([ids],device='cuda')
            output=model.generate(input_ids=tokens,attention_mask=torch.ones_like(tokens),
                max_new_tokens=batches.RECIPE['max_output_tokens'],do_sample=False,
                eos_token_id=tokenizer.eos_token_id,pad_token_id=tokenizer.eos_token_id,use_cache=True)
            generated=output[0,len(ids):]
            rows.append({'id':case['id'],'output':tokenizer.decode(generated,skip_special_tokens=True),
                         'tokens':len(generated),'ended_with_eos':len(generated)>0 and int(generated[-1])==tokenizer.eos_token_id})
            batches.save(path/(phase+'-answers.partial.json'),rows)
            print(phase,case['id'],'collected',flush=True)
    return rows


def train(model, encoded, path, deadline):
    import torch
    recipe=batches.RECIPE
    rng=random.Random(recipe['seed']); order=[]; cursor=0; history=[]
    model.config.use_cache=False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.enable_input_require_grads(); model.train()
    optimizer=torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],lr=recipe['learning_rate'],weight_decay=0.0)
    for step in range(recipe['steps']):
        if time.monotonic()>=deadline: raise TimeoutError('有限訓練逾時。')
        optimizer.zero_grad(set_to_none=True); loss=0
        for _ in range(recipe['accumulation']):
            if cursor==len(order):
                order=list(range(len(encoded))); rng.shuffle(order); cursor=0
            ids,labels=encoded[order[cursor]]; cursor+=1
            tokens=torch.tensor([ids],device='cuda')
            result=model(input_ids=tokens,attention_mask=torch.ones_like(tokens),
                         labels=torch.tensor([labels],device='cuda'),use_cache=False)
            if not torch.isfinite(result.loss): raise ValueError('訓練損失非有限值。')
            loss+=float(result.loss.detach()); (result.loss/recipe['accumulation']).backward(); del result
        norm=torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad],1.0)
        if not torch.isfinite(norm): raise ValueError('梯度非有限值。')
        optimizer.step(); history.append({'step':step+1,'loss':loss/recipe['accumulation']})
        batches.save(path/'training.partial.json',history)
        if (step+1)%16==0: print('step',step+1,'/',recipe['steps'],flush=True)
    del optimizer
    model.gradient_checkpointing_disable(); model.config.use_cache=True
    gc.collect(); torch.cuda.empty_cache()
    return history


def phase(path, name):
    manifest=batches.read_manifest(path,live=True)
    with (path/(name+'.started')).open('x',encoding='ascii') as stream:
        stream.write(str(os.getpid()))
    result={'schema':'confirmed_learning_phase_v1','phase':name,'status':'running',
            'manifest_sha256':batches.queue.digest(manifest),'started':time.time(),'pid':os.getpid()}
    succeeded=False
    model=None
    try:
        configure()
        result['environment']={name:importlib.metadata.version(name) for name in EXPECTED_VERSIONS}
        if result['environment']!=EXPECTED_VERSIONS: raise ValueError('ML套件版本不符。')
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        from peft import LoraConfig, get_peft_model, PeftModel
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported(): raise ValueError('需要支援BF16的CUDA GPU。')
        torch.set_num_threads(8); torch.manual_seed(batches.RECIPE['seed']); torch.cuda.manual_seed_all(batches.RECIPE['seed'])
        torch.cuda.set_per_process_memory_fraction(0.85)
        base,_=local_models.verify_assets(manifest['model'])
        tokenizer=AutoTokenizer.from_pretrained(base,local_files_only=True,trust_remote_code=False)
        encoded=[encode(tokenizer,row) for row in batches.training_rows(manifest)]
        # Checks are preflighted but never included in optimizer data.
        for row in batches.evaluation_cases(manifest): encode(tokenizer,row)
        model=AutoModelForCausalLM.from_pretrained(base,local_files_only=True,trust_remote_code=False,
            use_safetensors=True,torch_dtype=torch.bfloat16,device_map={'':0},attn_implementation='sdpa')
        result['gpu']=torch.cuda.get_device_name(0); torch.cuda.reset_peak_memory_stats()
        expected_weights=None
        if name=='reload':
            previous=local_models.read_json(path/'train.json')
            if batches.adapter_files(path/'adapter')!=previous['adapter_files']: raise ValueError('保存候選參數已變更。')
            adapter=path/'adapter'; expected_weights=previous['weights_after']
        elif manifest['parent']:
            parent_manifest,receipt,reports=batches.candidate(path.parent.parent,manifest['parent']['id'])
            origin=batches.folder(path.parent.parent,manifest['parent']['id'])
            if batches.queue.digest(parent_manifest)!=manifest['parent']['manifest_sha256'] or batches.file_hash(origin/'receipt.json')!=manifest['parent']['receipt_sha256']:
                raise ValueError('前代證據不符。')
            adapter=origin/'adapter'; expected_weights=reports['train']['weights_after']
        else: adapter=None
        if adapter is not None:
            model=PeftModel.from_pretrained(model,adapter,is_trainable=name=='train',local_files_only=True)
        elif name=='train':
            model=get_peft_model(model,LoraConfig(r=batches.RECIPE['rank'],lora_alpha=batches.RECIPE['alpha'],
                lora_dropout=batches.RECIPE['dropout'],target_modules=batches.RECIPE['target_modules'],bias='none',task_type='CAUSAL_LM'))
        if adapter is not None or name=='train':
            result['weights_before']=adapter_hash(model)
            if expected_weights is not None and result['weights_before']!=expected_weights: raise ValueError('載入參數與前代紀錄不符。')
        deadline=time.monotonic()+1500
        if name=='train':
            result['training_rows_sha256']=batches.queue.digest(batches.training_rows(manifest))
            result['trainable_parameters']=sum(p.numel() for p in model.parameters() if p.requires_grad)
            result['training']=train(model,encoded,path,deadline)
            target=path/'adapter'; target.mkdir(exist_ok=False); model.save_pretrained(target,safe_serialization=True)
            result['adapter_files']=batches.adapter_files(target)
        if adapter is not None or name=='train': result['weights_after']=adapter_hash(model)
        result['rows']=evaluate(model,tokenizer,manifest,path,name,deadline)
        result['peak_gpu_bytes']=torch.cuda.max_memory_allocated()
        batches.read_manifest(path,live=True)
        result['status']='complete'
        succeeded=True
    except BaseException as exc:
        result.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:2000])
    finally:
        model=None
        gc.collect()
        if 'torch' in locals() and torch.cuda.is_available(): torch.cuda.empty_cache()
        result['ended']=time.time(); batches.save(path/(name+'.json'),result)
    return succeeded


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir',type=Path,required=True); parser.add_argument('--phase',choices=batches.PHASES,required=True)
    args=parser.parse_args(); path=args.batch_dir.resolve()
    batches.identifier(path.name)
    # Hold the lease until process exit/teardown, never release CUDA allocations
    # while another phase can begin inside this same worker.
    with gpu_lease(): succeeded=phase(path,args.phase)
    return 0 if succeeded else 1


if __name__=='__main__': raise SystemExit(main())
