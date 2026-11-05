"""Deterministic original images and traceable real chat image evaluation."""
import argparse
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version
from assistant import image_inputs
from assistant.engine import Assistant, CodexEngine, EngineError, render_prompt
from assistant.store import Store
from scripts.evaluate_reasoning_pair import PinnedEngine
from scripts.evaluate_understanding import validate_cases, load_report, save_report, fingerprint
from scripts.evaluation_reports import TRACE_MODE, TRACE_CONTEXT_MARKER, canonical_digest, load_json, validate_report, verify_suite

SUITE=ROOT/'evals/image_inputs.json'


def signature(suite):
    return {**fingerprint(suite),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'report_validation_sha256':hashlib.sha256((ROOT/'scripts/evaluation_reports.py').read_bytes()).hexdigest(),
            'pinned_engine_sha256':hashlib.sha256((ROOT/'scripts/evaluate_reasoning_pair.py').read_bytes()).hexdigest(),
            'requirements_sha256':hashlib.sha256((ROOT/'requirements.txt').read_bytes()).hexdigest(),
            'protocol_sha256':canonical_digest({'pillow_version':pillow_version,'model':'gpt-6.1-sol','effort':'medium'})}


def image_bytes(spec):
    canvas=Image.new('RGB',(900,520),'white');draw=ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=34)
    if spec['kind']=='bars':
        values=spec['values']
        assert len(values)==3 and all(type(v) is int and 0<v<=20 for v in values)
        for index,value in enumerate(values):
            x=100+index*270;y=430-value*16
            draw.rectangle((x,y,x+120,430),fill=('#3a83c2','#d29836','#538f42')[index])
            draw.text((x+40,y-45),str(value),fill='black',font=font)
            draw.text((x+40,450),'ABC'[index],fill='black',font=font)
    elif spec['kind']=='shapes':
        assert 0<=spec['circles']<=5 and 0<=spec['squares']<=5
        for i in range(spec['circles']):draw.ellipse((40+i*160,80,140+i*160,180),fill='#df2020',outline='black',width=3)
        for i in range(spec['squares']):draw.rectangle((40+i*160,300,140+i*160,400),fill='#176dcc',outline='black',width=3)
    elif spec['kind']=='instruction_card':
        assert 0<=spec['squares']<=5 and len(spec['text'])<100
        draw.text((30,35),spec['text'],fill='black',font=ImageFont.load_default(size=25))
        for i in range(spec['squares']):draw.rectangle((50+i*190,240,150+i*190,340),fill='#159a37',outline='black',width=3)
    else:raise ValueError('未知合成圖片類型。')
    stream=io.BytesIO();canvas.save(stream,format='PNG');return stream.getvalue()


def prepare(store,case):
    sid=store.new_session('合成圖片驗證')
    folder=store.path.parent/'source-images';folder.mkdir(exist_ok=True)
    specs=case['images'];assert 1<=len(specs)<=4
    paths=[]
    for position,spec in enumerate(specs,1):
        path=folder/f'image-{position}.png';path.write_bytes(image_bytes(spec));paths.append(path)
    ids=image_inputs.attach_paths(store,sid,paths)
    return sid,ids,paths


class ImagePinnedEngine(PinnedEngine):
    def answer_images(self,prompt,images,cancel=None):
        self.request=prompt
        self.payload_sha256=[hashlib.sha256(data).hexdigest() for data in images]
        result=CodexEngine.answer(self,prompt,cancel,images=images)
        if self.metadata.get('model')!=self.model or self.metadata.get('reasoning effort')!=self.effort:
            raise EngineError('圖片測量的實際模型設定不符。')
        return result


def collect(store,case,engine,unchanged):
    sid,ids,paths=prepare(store,case);calls=[]
    class Recorded:
        def answer_images(self,prompt,images,cancel=None):
            if calls:raise ValueError('圖片案例意外呼叫多次。')
            unchanged();calls.append({'request':prompt,'sha256':[hashlib.sha256(b).hexdigest() for b in images]})
            trace=store.path.with_suffix('.request.json')
            save_report(trace,{**calls[0],'status':'started'})
            try:result=engine.answer_images(prompt,images,cancel)
            except Exception as exc:
                save_report(trace,{**calls[0],'status':'error','error':str(exc)});raise
            calls[0]['answer']=result
            save_report(trace,{**calls[0],'status':'returned'})
            unchanged();return result
    answer=Assistant(store,Recorded(),allow_local_transcription=False, allow_local_evidence=False).ask(sid,case['prompt'],image_ids=ids)
    unchanged();context=store.answer_context(answer['id']);request=calls[0]['request']
    assert request==render_prompt(context)
    assert calls[0]['sha256']==[item['sha256'] for item in context['image_inputs']]
    body=json.dumps(context,ensure_ascii=False);offset=len(request)-len(body)
    assert request[:offset].endswith(TRACE_CONTEXT_MARKER)
    return {**{k:case[k] for k in ('id','category','prompt','criteria')},'fixture':case,'fixture_sha256':canonical_digest(case),
            'context':context,'request':request,'request_sha256':hashlib.sha256(request.encode()).hexdigest(),
            'request_context_offset':offset,'engine_call_count':1,'engine_answer':calls[0]['answer'],
            'answer':answer['content'],'human_grade':None,'mechanical_checks':{},
            'state_path':str(store.path),'session_id':sid,'answer_message_id':answer['id'],
            'image_payload_sha256':calls[0]['sha256'],'source_image_paths':[str(p) for p in paths],
            'cli_reported':engine.metadata}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,default=SUITE)
    parser.add_argument('--live',action='store_true');parser.add_argument('--report',type=Path)
    args=parser.parse_args();cases=validate_cases(load_json(args.suite))
    if not args.live:
        if args.report:parser.error('--report需搭配--live。')
        with tempfile.TemporaryDirectory() as tmp:
            for i,case in enumerate(cases):
                folder=Path(tmp)/str(i);folder.mkdir();store=Store(folder/'assistant.sqlite3')
                sid,ids,paths=prepare(store,case)
                metadata,data=image_inputs.prepare(store,sid,ids)
                assert [p.read_bytes() for p in paths]==list(data) and len(metadata)==len(case['images'])
        print(f'Validated {len(cases)} original image fixtures; no model answers graded.');return
    if not args.report:parser.error('需指定新的本機報告路徑。')
    frozen=signature(args.suite)
    report=load_report(args.report,frozen,[c['id'] for c in cases],False,mode=TRACE_MODE)
    folder=args.report.resolve().with_suffix('');folder.mkdir(parents=True,exist_ok=False)
    def unchanged():
        if signature(args.suite)!=frozen:raise ValueError('圖片測量期間來源或固定條件變更。')
    for i,case in enumerate(cases):
        unchanged()
        state=folder/str(i);state.mkdir()
        engine=ImagePinnedEngine(state/'engine','gpt-6.1-sol','medium')
        row=collect(Store(state/'assistant.sqlite3'),case,engine,unchanged)
        report['results'].append(row);validate_report(report);verify_suite(report,args.suite.read_bytes())
        save_report(args.report,report);print(case['id']+': collected; review pending',flush=True)
    unchanged()


if __name__=='__main__':main()
