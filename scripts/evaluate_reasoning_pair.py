"""Paired, task-specific reasoning experiment. Never converts scores to human IQ."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from assistant.engine import Assistant, CodexEngine, EngineError, find_codex
from assistant.companion_profile import DEFAULTS, save_profile
from assistant.store import Store
from scripts.evaluation_reports import strict_json
from scripts.evaluate_understanding import fingerprint, save_report
from scripts.reasoning_cases import CATEGORIES, make_cases

INSTRUCTIONS='''請獨立解答以下六題原創推理題，所有必要條件都在題內。不要使用工具或外部資料。
只輸出一個JSON物件，鍵為每題id，值為該題的最終答案字串。不得省略題目或增加其他鍵，不要解釋、推理過程或Markdown。
數值使用精確整數、有限小數或a/b分數字串，不要單位或百分號。序列使用英文逗號，方格使用/分列；邏輯判斷只用必真、必假、不確定。
每題等權，請按題意作答；不清楚時可答「無法作答」，不需編造。以下JSON僅包含題目，沒有答案鍵：
'''


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def public_prompt(cases):
    return INSTRUCTIONS+json.dumps([{'id':c['id'],'prompt':c['prompt']} for c in cases],ensure_ascii=False)


def batches(cases):
    return [[next(c for c in cases if c['category']==cat and c['id'].endswith('_'+str(i)))
             for cat in CATEGORIES] for i in range(1,7)]


class PinnedEngine(CodexEngine):
    """Use the production engine restrictions; record only public CLI metadata."""
    def __init__(self,folder,model,effort,timeout=240):
        super().__init__(folder,timeout=timeout)
        self.model=model;self.effort=effort;self.metadata={};self.request=None

    def command(self,output):
        self.stderr_path=Path(output).parent/'stderr.log'
        command=super().command(output)
        return command[:-1]+['--model',self.model,'-c','model_reasoning_effort='+json.dumps(self.effort),command[-1]]

    def answer(self,prompt,cancel=None):
        self.request=prompt
        answer=super().answer(prompt,cancel)
        if self.metadata.get('model')!=self.model or self.metadata.get('reasoning effort')!=self.effort:
            raise EngineError('CLI模型／推理設定未能與指定值核對，本次不作有效對照。','model_identity_unverified')
        return answer

    def stop(self,process):
        super().stop(process)
        # Do not retain debug streams or hidden reasoning. Read a bounded
        # prefix solely to extract the CLI's public configuration header.
        try:
            with self.stderr_path.open('rb') as stream:prefix=stream.read(8192).decode('utf-8',errors='replace')
            for key in ('model','reasoning effort','model provider'):
                match=re.search(r'^'+re.escape(key)+r':\s*([^\r\n]+)',prefix,re.M)
                if match:self.metadata[key]=match.group(1).strip()
        except OSError:pass


def normalized(case,value):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('答案須為有界字串。')
    value=value.strip()
    if value=='無法作答':return ('abstained',)
    if case['category'] in ('quantitative','induction'):
        if len(value)>100 or not re.fullmatch(r'[+-]?(?:\d+/[+-]?\d+|\d+(?:\.\d*)?|\.\d+)',value):raise ValueError('數值格式無效。')
        if '/' in value:
            numerator,denominator=value.split('/')
            return Fraction(int(numerator),int(denominator))
        return Fraction(value)
    if case['category'] in ('tracking','constraints'):
        parts=[p.strip() for p in value.split(',')]
        if any(not re.fullmatch('[A-Z]',p) for p in parts):raise ValueError('序列格式無效。')
        return tuple(parts)
    if case['category']=='spatial':
        rows=[p.strip() for p in value.split('/')]
        if len(rows)!=5 or any(not re.fullmatch('[01]{5}',p) for p in rows):raise ValueError('方格格式無效。')
        return tuple(rows)
    if value not in ('必真','必假','不確定'):raise ValueError('邏輯選項無效。')
    return value


def grade(cases,answer,error=None):
    failure=error
    values={}
    if not failure:
        try:
            text=answer.strip()
            if text.startswith('```json\n') and text.endswith('\n```'):text=text[8:-4]
            values=strict_json(text.encode('utf-8'))
            if not isinstance(values,dict) or set(values)-{c['id'] for c in cases}:
                raise ValueError('額外或無效答案鍵。')
        except (ValueError,UnicodeError) as exc:
            failure='format: '+str(exc);values={}
    rows=[]
    for case in cases:
        row={'id':case['id'],'category':case['category'],'correct':False,'answer':values.get(case['id'])}
        if failure:row['status']='execution_error' if error else 'format_error'
        elif case['id'] not in values:row['status']='missing'
        elif isinstance(values[case['id']],str) and values[case['id']].strip()=='無法作答':row['status']='abstained'
        else:
            try:
                row['correct']=normalized(case,values[case['id']])==normalized(case,case['answer'])
                row['status']='correct' if row['correct'] else 'incorrect'
            except (ValueError,ZeroDivisionError):row['status']='format_error'
        rows.append(row)
    return rows


def frozen(cases,protocol):
    base=fingerprint(ROOT/'evals/understanding.json')
    base['suite_sha256']=sha(encoded(cases))
    base['protocol_sha256']=sha(encoded(protocol))
    base['runner_sha256']=sha(Path(__file__).read_bytes())
    base['generator_sha256']=sha((ROOT/'scripts/reasoning_cases.py').read_bytes())
    return base


def perform(folder,arm,prompt,protocol):
    folder.mkdir(parents=True,exist_ok=False)
    engine=PinnedEngine(folder,protocol['model'],protocol['effort'],protocol['timeout_seconds'])
    started=time.perf_counter();answer='';error=None
    try:
        if arm=='direct':answer=engine.answer(prompt)
        else:
            store=Store(folder/'synthetic.sqlite3')
            save_profile(store,{**DEFAULTS,'tone':'direct'},expected_revision=0)
            answer=Assistant(store,engine).ask(store.new_session(),prompt)['content']
    except (EngineError,ValueError,OSError) as exc:error=str(exc)
    return {'arm':arm,'elapsed_seconds':time.perf_counter()-started,'answer':answer,'error':error,
            'cli_reported':engine.metadata,'request':engine.request,
            'request_sha256':sha((engine.request or '').encode('utf-8'))}


def summarize(report,cases):
    seen=set();case_batches=batches(cases)
    for call in report['calls']:
        key=(call['arm'],call['repeat'],call['batch'])
        if key in seen or call['arm'] not in ('direct','assistant') or type(call['repeat']) is not int or call['repeat'] not in (0,1) or type(call['batch']) is not int or not 0<=call['batch']<6:
            raise ValueError('重複或無效的測量身份。')
        seen.add(key)
        expected=case_batches[call['batch']]
        if call['case_ids']!=[c['id'] for c in expected] or call['scores']!=grade(expected,call['answer'],call['error']):
            raise ValueError('案例或分數與原回答不一致。')
    if report['status']=='complete' and len(seen)!=24:raise ValueError('測量尚未完整。')
    result={}
    for arm in ('direct','assistant'):
        calls=[c for c in report['calls'] if c['arm']==arm]
        scores=[row for call in calls for row in call['scores']]
        rounds=[]
        for repeat in range(report['protocol']['repeats']):
            rows=[row for call in calls if call['repeat']==repeat for row in call['scores']]
            rounds.append({'correct':sum(r['correct'] for r in rows),'collected':len(rows),'planned':len(cases)})
        categories={cat:{'correct':sum(r['correct'] for r in scores if r['category']==cat),
                         'collected':sum(r['category']==cat for r in scores),
                         'planned':6*report['protocol']['repeats']} for cat in CATEGORIES}
        consistent=0;paired=0
        for case in cases:
            observations=[r for r in scores if r['id']==case['id']]
            if len(observations)==2:
                paired+=1
                try:consistent+=normalized(case,observations[0]['answer'])==normalized(case,observations[1]['answer'])
                except (ValueError,ZeroDivisionError):pass
        result[arm]={'correct':sum(r['correct'] for r in scores),'collected':len(scores),
                     'planned':len(cases)*report['protocol']['repeats'],
                     'format_errors':sum(r['status'] in ('format_error','missing') for r in scores),
                     'execution_errors':sum(r['status']=='execution_error' for r in scores),
                     'abstained':sum(r['status']=='abstained' for r in scores),
                     'rounds':rounds,'categories':categories,'consistent_pairs':consistent,'paired_cases':paired,
                     'call_seconds_total':sum(c['elapsed_seconds'] for c in calls)}
    return result


def write_summary(folder,report,cases):
    summary=summarize(report,cases);save_report(folder/'summary.json',summary)
    p=report['protocol'];lines=['# 同模型推理對照測驗','',
        '這是36題原創文字推理測驗，不是經人類常模校準的IQ量表，不能換算人類智商。',
        f'模型指定：{p["model"]}；推理設定：{p["effort"]}；每邊{p["repeats"]}輪；每次六題、上限{p["timeout_seconds"]}秒。',
        f'收集狀態：{report["status"]}。私人資料未使用；助理採全新資料庫及direct風格。',
        '直接模型也透過相同Codex CLI，不等同ChatGPT網站；兩邊工具均停用。每對呼叫並行，耗時不是純推理速度。','',
        '| 項目 | 直接模型 | 個人助理 |','|---|---:|---:|']
    for cat,label in CATEGORIES.items():
        cells=[str(summary[a]['categories'][cat]['correct'])+'/'+str(summary[a]['categories'][cat]['planned']) for a in ('direct','assistant')]
        lines.append('| '+label+' | '+' | '.join(cells)+' |')
    for label,values in [('總計',[f'{summary[a]["correct"]}/{summary[a]["planned"]}' for a in ('direct','assistant')]),
                         ('格式缺漏／不符',[str(summary[a]['format_errors']) for a in ('direct','assistant')]),
                         ('執行失敗題數',[str(summary[a]['execution_errors']) for a in ('direct','assistant')]),
                         ('明確無法作答',[str(summary[a]['abstained']) for a in ('direct','assistant')]),
                         ('兩輪答案一致',[f'{summary[a]["consistent_pairs"]}/{summary[a]["paired_cases"]}' for a in ('direct','assistant')])]:
        lines.append('| '+label+' | '+' | '.join(values)+' |')
    lines+=['','每題每輪等權、沒有挑選最好答案、沒有模型自評。整數／有限小數／分數按精確值比對，其餘按指定結構核對。原始回答及每題結果保存在同資料夾report.json。',
            '重複題不是額外獨立題目；兩輪只提供初步穩定性觀察。此為文字與符號任務，未測視覺感知、人類記憶常模、生活判斷、私人記憶或自主工具。小樣本差異不能證明一般智能較高。',
            '答案鍵未放入模型輸入；題目與評分程式由開發代理設計，未經獨立心理計量驗證，也無法保證所有形式在訓練中從未出現。',
            '參考方法：[OpenAI評估建議](https://developers.openai.com/api/docs/guides/evaluation-best-practices)。']
    (folder/'RESULTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--model',default='gpt-6.1-sol')
    parser.add_argument('--effort',choices=('low','medium','high'),default='medium')
    parser.add_argument('--seed',type=int,default=20261103)
    args=parser.parse_args()
    cases=make_cases(args.seed)
    if not args.live:
        for case in cases:normalized(case,case['answer'])
        print('Validated 36 generated tasks; no model calls, no IQ score.');return
    folder=args.out.resolve();folder.mkdir(parents=True,exist_ok=False)
    protocol={'model':args.model,'effort':args.effort,'seed':args.seed,'repeats':2,'timeout_seconds':240,
              'batch_size':6,'parallel_arms':True,'tools':'disabled','private_memory':False,
              'assistant_profile':{**DEFAULTS,'tone':'direct'},'instructions':INSTRUCTIONS,
              'cli_version':subprocess.check_output([find_codex(),'--version'],text=True).strip()}
    signature=frozen(cases,protocol)
    report={'format':'paired_reasoning_v1','created_at':datetime.now(timezone.utc).isoformat(),
            'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'protocol':protocol,'fingerprint':signature,'status':'collecting','cases':cases,'calls':[]}
    save_report(folder/'report.json',report)
    def unchanged():
        if frozen(cases,protocol)!=signature:raise ValueError('來源或評分流程已改變，停止對照。')
    try:
        for repeat in range(2):
            for index,batch in enumerate(batches(cases)):
                unchanged();prompt=public_prompt(batch)
                arms=('direct','assistant') if (repeat+index)%2==0 else ('assistant','direct')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending={pool.submit(perform,folder/f'r{repeat+1}-b{index+1}-{arm}',arm,prompt,protocol):arm for arm in arms}
                    for future in as_completed(pending):
                        call=future.result();unchanged()
                        call.update(repeat=repeat,batch=index,case_ids=[c['id'] for c in batch],
                                    scores=grade(batch,call['answer'],call['error']))
                        report['calls'].append(call);save_report(folder/'report.json',report)
                        print(f'round {repeat+1}, batch {index+1}, {call["arm"]}: '+str(sum(r['correct'] for r in call['scores']))+'/6',flush=True)
                if any(c['error'] for c in report['calls']):raise ValueError('有執行錯誤；保留已收集結果，不自動重試。')
        unchanged();report['status']='complete'
    except Exception as exc:
        report['status']='incomplete';report['collection_error']=str(exc)
        raise
    finally:
        save_report(folder/'report.json',report);write_summary(folder,report,cases)
    print('Complete. This is task accuracy, not a human IQ score.',flush=True)


if __name__=='__main__':main()
