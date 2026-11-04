"""Finite native-idle GPU experiment comparing four updates with a two-step handoff."""
import argparse
from array import array
import importlib.metadata
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from assistant import idle_learning as idle, learning_batches as batches, learned_models, local_models
from assistant.engine import CodexEngine
from scripts.learning import checkpoint_state as ck
from scripts.learning.batch_worker import encode, prompt_ids
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.runtime import gpu_lease

PARENT = ROOT/'data/learning/gpu-checkpoint-probes'
PHASES = ('continuous', 'first', 'resumed')
IDLE_MS = 600_000


def save(path, obj):
    ck.commit_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False).encode('utf8'))


def sources():
    return {p.name: batches.file_hash(p) for p in (Path(__file__), Path(ck.__file__))}


def input_gate(admitted):
    sample = idle.idle_sample()
    if (sample['idle_ms'] < IDLE_MS or sample['last_input_tick'] != admitted['last_input_tick']
            or sample['ticks_ms'] < admitted['ticks_ms']):
        raise batches.Stopped('input_resumed')
    return sample


def inputs(plan):
    if sources() != plan['sources']:
        raise batches.Stale('研究程式來源已變更。')
    job = idle.folder(plan['data_dir'], plan['reference_job'])
    reference = idle.read_manifest(job, live=True)
    path = batches.folder(plan['data_dir'], reference['batch_id'])
    manifest = batches.read_manifest(path, live=True)
    if (batches.queue.digest(reference) != plan['job_digest'] or manifest['id'] != reference['batch_id']
            or batches.queue.digest(manifest) != reference['batch_manifest_sha256'] or not manifest['parent']):
        raise batches.Stale('參考規約或父模型不符。')
    batches.examples_current(learned_models.QueueReader(plan['data_dir']), manifest)
    return manifest


def parent_evidence(plan, manifest):
    parent_id = manifest['parent']['id']
    parent, _, reports = batches.candidate(plan['data_dir'], parent_id)
    path = batches.folder(plan['data_dir'], parent_id)
    if (batches.queue.digest(parent) != manifest['parent']['manifest_sha256']
            or batches.file_hash(path/'receipt.json') != manifest['parent']['receipt_sha256']):
        raise batches.Stale('父批次證據已變更。')
    return path, reports['train']['weights_after']


def worker(folder, phase, plan, reference):
    configure()
    if os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
        raise ValueError('CUDA 工作區設定不符。')
    environment = {name:importlib.metadata.version(name) for name in EXPECTED_VERSIONS}
    if environment != EXPECTED_VERSIONS:
        raise ValueError('ML 環境不符。')
    manifest = inputs(plan)
    input_gate(plan['admitted'])
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import PeftModel
    from torch.nn.attention import sdpa_kernel, SDPBackend

    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise ValueError('本實驗需要單一支援 BF16 的 CUDA GPU。')
    recipe = {**manifest['recipe'], 'steps':4}
    torch.set_num_threads(8)
    torch.manual_seed(recipe['seed'])
    torch.cuda.manual_seed_all(recipe['seed'])
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.cuda.set_per_process_memory_fraction(0.85)
    base, _ = local_models.verify_assets(manifest['model'])
    parent_path, expected_weights = parent_evidence(plan, manifest)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    rows = batches.training_rows(manifest)
    encoded = [encode(tokenizer,row) for row in rows]
    model = AutoModelForCausalLM.from_pretrained(base,local_files_only=True,trust_remote_code=False,
        use_safetensors=True,torch_dtype=torch.bfloat16,device_map={'':0},attn_implementation='sdpa')
    model = PeftModel.from_pretrained(model,parent_path/'adapter',is_trainable=True,local_files_only=True)
    initial = adapter_hash(model)
    if initial != expected_weights:
        raise ValueError('實際載入的父參數不符。')
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.enable_input_require_grads()
    model.train()
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=recipe['learning_rate'], weight_decay=0.0)
    binding = {'device':'cuda:0','gpu':torch.cuda.get_device_name(0),'capability':list(torch.cuda.get_device_capability()),
               'cuda':torch.version.cuda, 'torch_build':str(torch.__version__), 'environment':environment,
               'experiment':batches.queue.digest(plan), 'initial_adapter_sha256':initial, 'recipe':recipe,
               'rows_sha256':batches.queue.digest(rows),'row_count':len(rows),
               'optimizer':json.loads(json.dumps({k:v for k,v in optimizer.param_groups[0].items() if k!='params'}))}
    sampler = random.Random(recipe['seed'])
    order, cursor, history = [], 0, []
    blobs = folder/'checkpoints'
    if phase == 'resumed':
        order,cursor,history = ck.restore(blobs,reference,binding,model,optimizer,sampler)
        if len(history) != 2:
            raise ValueError('接續點必須為第二步。')
    target = 2 if phase == 'first' else 4
    with sdpa_kernel(SDPBackend.MATH):
        while len(history) < target:
            input_gate(plan['admitted'])
            optimizer.zero_grad(set_to_none=True)
            samples, losses = [], []
            for _ in range(recipe['accumulation']):
                if cursor == len(order):
                    order = list(range(len(rows)))
                    sampler.shuffle(order)
                    cursor = 0
                index = order[cursor]
                cursor += 1
                ids, labels = encoded[index]
                tokens = torch.tensor([ids],device='cuda')
                result = model(input_ids=tokens,attention_mask=torch.ones_like(tokens),
                               labels=torch.tensor([labels],device='cuda'),use_cache=False)
                losses.append(float(result.loss.detach()))
                (result.loss/recipe['accumulation']).backward()
                samples.append(index)
                del result
            torch.nn.utils.clip_grad_norm_(trainable,1.0,error_if_nonfinite=True)
            optimizer.step()
            history.append({'samples':samples,'loss':sum(losses)/len(losses)})
        inputs(plan)
        input_gate(plan['admitted'])
        sha = ck.capture(blobs,binding,model,optimizer,sampler,order,cursor,history)
        record = {'phase':phase, 'sha':sha, 'consumed':reference, 'binding':binding,'step':len(history),
                  'weights':adapter_hash(model), 'history':history,'pid':os.getpid()}
        if target == 4:
            model.gradient_checkpointing_disable()
            model.eval()
            check = manifest['checks'][0]['prompt']
            tokens = torch.tensor([prompt_ids(tokenizer,check)],device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=tokens,use_cache=False).logits[0,-1].float()
                output = model.generate(input_ids=tokens,attention_mask=torch.ones_like(tokens),
                    do_sample=False,use_cache=True,max_new_tokens=recipe['max_output_tokens'],
                    pad_token_id=tokenizer.eos_token_id,eos_token_id=tokenizer.eos_token_id)
            record.update(logits_sha256=ck.digest(ck.tensor_bytes(logits)), generated=output[0,len(tokens[0]):].tolist())
            ck.commit_bytes(folder/(phase+'.logits'),ck.tensor_bytes(logits))
    inputs(plan)
    input_gate(plan['admitted'])
    save(folder/(phase+'.json'),record)


def run_owned(command, folder, phase, check_input, check_sources):
    actual = batches.PhaseExit(folder/(phase+'.started'))
    with (folder/(phase+'.log')).open('xb') as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ,'CUBLAS_WORKSPACE_CONFIG':':4096:8'}, cwd=ROOT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        confirmed = False
        try:
            next_check = 0
            while child.poll() is None:
                actual.observe()
                check_input()
                if time.monotonic() >= next_check:
                    check_sources()
                    next_check = time.monotonic()+1
                time.sleep(0.1)
        finally:
            try:
                try:
                    actual.observe()
                finally:
                    CodexEngine.stop(child)
                    child.wait(timeout=10)
                actual.wait()
                confirmed = os.name != 'nt' or actual.pid is not None
            finally:
                actual.close()
                save(folder/(phase+'.exit.json'), {'launcher':child.pid, 'worker':actual.pid,
                     'exit_confirmed':confirmed, 'returncode':child.returncode})
    if child.returncode != 0 or not confirmed:
        raise ValueError(f'{phase} 未完成或無法核對程序退出，詳見本機紀錄。')


def run(data_dir, reference_job):
    data_dir = str(Path(data_dir).resolve())
    job = idle.read_manifest(idle.folder(data_dir,reference_job),live=True)
    admitted = idle.idle_sample()
    if admitted['idle_ms'] < IDLE_MS:
        raise ValueError('本次觀察尚未閒置滿十分鐘。')
    with gpu_lease():
        pass
    PARENT.mkdir(parents=True,exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='pilot-',dir=PARENT))
    print('本機實驗目錄',folder,flush=True)
    plan = {'data_dir':data_dir,'reference_job':reference_job,'job_digest':batches.queue.digest(job),
            'sources':sources(),'admitted':admitted,'backend':'deterministic, math SDPA, TF32 off, cuBLAS :4096:8',
            'criterion':'byte-identical checkpoints, changed weights, same held-out logits and tokens',
            'deadline_seconds':360,'lineage':'fresh parent, direct 4 vs 2+2; no activation'}
    inputs(plan)
    save(folder/'plan.json',plan)
    plan_digest = batches.queue.digest(plan)
    deadline = time.monotonic()+plan['deadline_seconds']
    python = ROOT/'data/learning/runtime/Scripts/python.exe'
    resumed_from = None
    def check():
        if time.monotonic()>=deadline:
            raise TimeoutError('研究工作逾時。')
        input_gate(admitted)
        if local_models.read_json(folder/'plan.json') != plan:
            raise batches.Stale('研究規約已變更。')
        inputs(plan)
    try:
        for phase in PHASES:
            check()
            reference = resumed_from if phase=='resumed' else None
            command = [str(python),'-I',str(Path(__file__).resolve()),'_worker',str(folder),phase,plan_digest]
            if reference:
                command.append(reference)
            run_owned(command,folder,phase,lambda:input_gate(admitted),check)
            check()
            parent_evidence(plan,inputs(plan))
            record = local_models.read_json(folder/(phase+'.json'))
            if (record['phase'] != phase or record['consumed'] != reference or record['binding']['experiment'] != plan_digest
                    or record['step'] != (2 if phase=='first' else 4)
                    or not re.fullmatch('[0-9a-f]{64}',record['sha'])
                    or batches.file_hash(folder/'checkpoints'/(record['sha']+'.safetensors')) != record['sha']):
                raise ValueError('階段、來源或檢查點指紋不符。')
            if phase == 'first':
                resumed_from = record['sha']
            print(phase,'completed',flush=True)
        left,right = [local_models.read_json(folder/(phase+'.json')) for phase in ('continuous','resumed')]
        comparison = {k:left[k]==right[k] for k in ('sha','binding','weights','history','logits_sha256','generated')}
        logits = []
        for record in (left,right):
            raw = (folder/(record['phase']+'.logits')).read_bytes()
            if not raw or ck.digest(raw) != record['logits_sha256']:
                raise ValueError('保留輸入的輸出指紋不符。')
            values = array('f')
            values.frombytes(raw)
            logits.append(values)
        logit_error = max(abs(a-b) for a,b in zip(*logits,strict=True))
        loss_error = max(abs(a['loss']-b['loss']) for a,b in zip(left['history'],right['history'],strict=True))
        check()
        passed = all(comparison.values()) and left['weights']!=left['binding']['initial_adapter_sha256']
        result = {'status':'verified' if passed else 'mismatch','equal':comparison,
                  'max_absolute_heldout_logit_difference':logit_error,'max_absolute_step_loss_difference':loss_error,
                  'weight_change':left['weights']!=left['binding']['initial_adapter_sha256']}
    except BaseException as exc:
        save(folder/'result.json',{'status':'stopped','reason':str(exc),'type':type(exc).__name__})
        raise
    save(folder/'result.json',result)
    return 0 if passed else 1


if __name__=='__main__':
    if sys.argv[1:2]==['_worker']:
        folder=Path(sys.argv[2]).resolve(strict=True)
        phase=sys.argv[3]
        expected_arguments = 6 if phase=='resumed' else 5
        if folder.parent!=PARENT.resolve() or phase not in PHASES or len(sys.argv)!=expected_arguments:
            raise ValueError('研究階段或路徑不符。')
        with (folder/(phase+'.started')).open('x',encoding='ascii') as marker:
            marker.write(str(os.getpid()))
        plan=local_models.read_json(folder/'plan.json')
        if batches.queue.digest(plan)!=sys.argv[4]:
            raise ValueError('研究規約指紋不符。')
        with gpu_lease():
            worker(folder,phase,plan,sys.argv[5] if phase=='resumed' else None)
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--data-dir',required=True,type=Path)
        parser.add_argument('--reference-job',required=True)
        args=parser.parse_args()
        raise SystemExit(run(args.data_dir,args.reference_job))
