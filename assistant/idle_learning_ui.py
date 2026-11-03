"""Root-owned desktop editor and progress window for one finite idle job."""
import json,os,queue,threading,time,tkinter as tk,uuid
from tkinter import ttk,filedialog
from . import learning_jobs as jobs,learning_queue as examples,idle_learning as idle
from .data_window import DataWindow
from .ui_layout import fit_window,ScrollableForm
from .ui_workers import start_worker

class IdleLearningWindow(DataWindow):
    shutdown_controls=('prepare_btn','load_btn','run_btn','inspect_btn','new_btn','stop_btn')
    shutdown_text='已要求停止，等候學習／驗收程序收尾後關閉；已保存資料會保留。'

    def __init__(self,parent,store):
        if getattr(parent._root(),'_assistant_closing',False): raise ValueError('程式正在關閉，不能開啟學習工作。')
        self.store=store; self.busy=False; self.cancel=threading.Event(); self.events=queue.Queue()
        self.job=None;self.parent_batch=None;self.existing_batch=None;self.basket={};self.page=1;self.pages=1;self.rows={}
        self.cases=[{'id':'calculation','group':'control','question':'17+26等於多少？只答數字。','answer':'43'},
                    {'id':'traditional_word','group':'control','question':'英文cat的中文是什麼？只答一個繁體中文字。','answer':'貓'}]
        self.own_window(parent);self.window.title('閒置學習工作');fit_window(self.window,960,850)
        intro=ttk.Label(self.window,text='選取已確認例子 → 設定完整答案驗收 → 準備並檢視 → 明確開始一次工作。\n輸入恢復會要求停止；關閉視窗會停止並等候收尾。沒有常駐排程或自動啟用。',padding=12,wraplength=850);intro.pack(fill='x')
        intro.bind('<Configure>',lambda e:intro.configure(wraplength=max(120,e.width-24)))
        self.tabs=ttk.Notebook(self.window);self.tabs.pack(fill='both',expand=True)
        train=ttk.Frame(self.tabs); cases=ttk.Frame(self.tabs); result=ttk.Frame(self.tabs)
        self.tabs.add(train,text='1 選取例子');self.tabs.add(cases,text='2 驗收與時間');self.tabs.add(result,text='3 已準備工作／結果')
        self.editor_controls=[]
        self.train_form=ScrollableForm(train,event_root=self.window);train=self.train_form.body
        table=ttk.Frame(train);table.pack(fill='x',padx=12,pady=8)
        self.listing=ttk.Treeview(table,columns=('state','revision','question'),show='headings',height=8,selectmode='extended')
        for key,title,width in [('state','狀態',140),('revision','版本',55),('question','已確認問題',640)]:
            self.listing.heading(key,text=title);self.listing.column(key,width=width)
        list_scroll=ttk.Scrollbar(table,command=self.listing.yview);list_scroll.pack(side='right',fill='y')
        self.listing.configure(yscrollcommand=list_scroll.set);self.listing.pack(fill='both',expand=True)
        nav=ttk.Frame(train);nav.pack(fill='x',padx=12)
        self.edit_button(nav,'上一頁',lambda:self.move(-1));self.edit_button(nav,'下一頁',lambda:self.move(1));self.edit_button(nav,'重新整理',self.refresh)
        self.page_label=ttk.Label(nav);self.page_label.pack(side='left',padx=6)
        self.edit_button(nav,'加入選取項目',self.add_items)
        self.chosen=tk.Listbox(train,height=5,selectmode='extended');self.chosen.pack(fill='x',padx=12,pady=8)
        select=ttk.Frame(train);select.pack(fill='x',padx=12)
        self.edit_button(select,'移除所選',self.remove_items);self.edit_button(select,'選取前代候選（可省略）',self.pick_parent)
        self.edit_button(select,'使用已準備批次',self.pick_batch);self.edit_button(select,'改為新批次',self.fresh_batch)
        self.selection_label=self.train_form.wrapped_label(train,padding=12);self.selection_label.pack(fill='x')
        self.form=ScrollableForm(cases,event_root=self.window);body=self.form.body
        self.form.wrapped_label(body,text='先加入至少一題目標及完整正確答案。兩個預設控制題可保留；每題精確比較完整輸出。這只是指定案例，不是全面能力評分。前文需求可匯入既有題庫。',padding=12).pack(fill='x')
        self.case_list=ttk.Treeview(body,columns=('group','question','answer'),show='headings',height=5)
        for key,title,width in [('group','分類',70),('question','驗收問題',460),('answer','預期完整答案',280)]:
            self.case_list.heading(key,text=title);self.case_list.column(key,width=width)
        self.case_list.pack(fill='x',padx=12)
        edit=ttk.Frame(body,padding=12);edit.pack(fill='x')
        self.group=ttk.Combobox(edit,values=('目標','控制'),state='readonly',width=8);self.group.set('目標');self.group.pack(side='left')
        self.edit_button(edit,'加入下方驗收題',self.add_case);self.edit_button(edit,'移除選取題',self.remove_case);self.edit_button(edit,'匯入題庫（取代列表）',self.import_cases)
        ttk.Label(body,text='驗收問題（最多16000字）').pack(anchor='w',padx=12)
        self.question=tk.Text(body,height=3,wrap='word',font='TkDefaultFont');self.question.pack(fill='x',padx=12)
        ttk.Label(body,text='你確認的完整答案（最多2000字）').pack(anchor='w',padx=12)
        self.answer=tk.Text(body,height=3,wrap='word',font='TkDefaultFont');self.answer.pack(fill='x',padx=12)
        settings=ttk.Frame(body,padding=12);settings.pack(fill='x');self.settings={}
        for key,title,value in [('idle_seconds','閒置門檻（秒）',600),('wait_seconds','等待上限（秒）',3600),('work_seconds','工作上限（秒）',1800)]:
            row=len(self.settings);ttk.Label(settings,text=title).grid(row=row,column=0,sticky='w',pady=3)
            field=ttk.Entry(settings,width=7);field.insert(0,str(value));field.grid(row=row,column=1,padx=6);self.settings[key]=field
        self.form.wrapped_label(body,text='閒置門檻30至3600秒；等待及工作上限各1至3600秒。只觀察目前Windows工作階段輸入，另檢查共用GPU鎖。準備不會啟動模型。',padding=12).pack(fill='x')
        self.preview=tk.Text(result,wrap='word',state='disabled',font='TkDefaultFont');scroll=ttk.Scrollbar(result,command=self.preview.yview);scroll.pack(side='right',fill='y');self.preview.configure(yscrollcommand=scroll.set);self.preview.pack(fill='both',expand=True)
        ttk.Button(result,text='開啟本機規約',command=self.open_manifest).pack(anchor='e',padx=12,pady=4)
        buttons=ttk.Frame(self.window,padding=12);buttons.pack(fill='x')
        for name,title,command in [('prepare_btn','準備工作',self.prepare),('load_btn','載入既有工作',self.load),('run_btn','開始已準備工作',self.run),('stop_btn','停止',self.stop),('inspect_btn','重新核對結果',self.inspect),('new_btn','編輯新工作',self.new)]:
            button=ttk.Button(buttons,text=title,command=command);button.pack(side='left',padx=3);setattr(self,name,button)
        self.notice=tk.StringVar(value='尚未準備或啟動工作。');notice=ttk.Label(self.window,textvariable=self.notice,wraplength=880,padding=12);notice.pack(fill='x')
        notice.bind('<Configure>',lambda e:notice.configure(wraplength=max(120,e.width-24)))
        # Reserve the status and actions before allocating the resizable notebook.
        self.tabs.pack_forget();buttons.pack_forget();notice.pack_forget()
        notice.pack(side='bottom',fill='x');buttons.pack(side='bottom',fill='x');self.tabs.pack(fill='both',expand=True)
        self.refresh();self.draw_cases();self.draw_basket();self.controls();self.schedule_poll()

    def edit_button(self,parent,text,command):
        button=ttk.Button(parent,text=text,command=command);button.pack(side='left',padx=3);self.editor_controls.append(button);return button

    def editable(self): return not self.busy and not self.unavailable() and self.job is None

    def controls(self):
        editing=self.editable()
        for button in self.editor_controls:button.configure(state='normal' if editing else 'disabled')
        for field in [self.question,self.answer,*self.settings.values()]:field.configure(state='normal' if editing else 'disabled')
        self.group.configure(state='readonly' if editing else 'disabled')
        free=not self.busy and not self.unavailable()
        for name,allowed in [('prepare_btn',editing),('load_btn',free),('new_btn',free),('inspect_btn',free and self.job is not None),('run_btn',free and bool(self.job and self.job['startable'])),('stop_btn',self.busy and not self.unavailable())]:
            getattr(self,name).configure(state='normal' if allowed else 'disabled')

    def refresh(self):
        if not self.editable():return
        try:
            data=examples.page(self.store,self.page);self.pages=data['pages'];self.rows={r['id']:r for r in data['items']}
            self.listing.delete(*self.listing.get_children())
            for row in data['items']:self.listing.insert('','end',iid=row['id'],values=({'ready':'已確認','withdrawn':'已撤回','needs_review':'需重新核對'}[row['readiness']],row['revision'],row['prompt'][:150]))
            self.page_label.configure(text=f"{self.page}/{self.pages}頁，共{data['total']}項")
        except Exception as exc:self.notice.set(str(exc))

    def move(self,delta):
        if self.editable():self.page=max(1,min(self.pages,self.page+delta));self.refresh()

    def add_items(self):
        if not self.editable():return
        if self.existing_batch:self.notice.set('目前使用已準備批次；請先按改為新批次。');return
        chosen=[self.rows[i] for i in self.listing.selection()]
        if any(r['readiness']!='ready' for r in chosen):self.notice.set('只能加入目前已確認且來源有效的項目。');return
        if len(set(self.basket)|{r['id'] for r in chosen})>64:self.notice.set('含前代重播最多64項。');return
        for row in chosen:self.basket[row['id']]=json.loads(json.dumps(row))
        self.draw_basket()

    def remove_items(self):
        if not self.editable():return
        keys=list(self.basket)
        for i in self.chosen.curselection():self.basket.pop(keys[i])
        self.draw_basket()

    def draw_basket(self):
        self.chosen.delete(0,'end')
        for row in self.basket.values():self.chosen.insert('end',f"版本{row['revision']} · {row['prompt'][:100]}")
        self.selection_label.configure(text=f"使用已準備批次：{self.existing_batch}" if self.existing_batch else f"新選取{len(self.basket)}項；前代：{self.parent_batch or '原始底座'}。準備時會重新核對所選版本。")

    def pick(self,job=False):
        base=self.store.path.parent/'learning-batches'
        if job:base=base/'_idle_jobs'
        path=filedialog.askopenfilename(parent=self.window,initialdir=base,title='選取本機規約 manifest.json',filetypes=[('規約','manifest.json')])
        return jobs.picked_id(self.store.path.parent,path,job=job) if path else None

    def pick_parent(self):
        if not self.editable():return
        try:
            value=self.pick()
            if value:self.parent_batch=value;self.existing_batch=None;self.draw_basket()
        except Exception as exc:self.notice.set(str(exc))

    def pick_batch(self):
        if not self.editable():return
        try:
            value=self.pick()
            if value:self.existing_batch=value;self.parent_batch=None;self.draw_basket()
        except Exception as exc:self.notice.set(str(exc))

    def fresh_batch(self):
        if self.editable():self.parent_batch=self.existing_batch=None;self.draw_basket()

    def draw_cases(self):
        self.case_list.delete(*self.case_list.get_children())
        for row in self.cases:self.case_list.insert('','end',iid=row['id'],values=('目標' if row['group']=='target' else '控制',row['question'][:120],row['answer'][:100]))

    def add_case(self):
        if not self.editable():return
        try:
            question=self.question.get('1.0','end-1c');answer=self.answer.get('1.0','end-1c')
            examples.text(question,16000,'驗收問題');examples.text(answer,2000,'完整答案')
            if len(self.cases)>=32:raise ValueError('驗收最多32題。')
            self.cases.append({'id':uuid.uuid4().hex,'group':'target' if self.group.get()=='目標' else 'control','question':question,'answer':answer})
            self.draw_cases();self.question.delete('1.0','end');self.answer.delete('1.0','end')
        except Exception as exc:self.notice.set(str(exc))

    def remove_case(self):
        if self.editable():self.cases=[r for r in self.cases if r['id'] not in self.case_list.selection()];self.draw_cases()

    def import_cases(self):
        if not self.editable():return
        path=filedialog.askopenfilename(parent=self.window,title='匯入完整驗收題庫，取代列表',filetypes=[('JSON','*.json')])
        if not path:return
        try:self.cases=idle.checks.suite(idle.models.local_models.read_json(path,1024*1024))['cases'];self.draw_cases()
        except Exception as exc:self.notice.set(str(exc))

    def launch(self,operation):
        if self.busy or self.unavailable():return
        self.busy=True;self.cancel.clear();self.started=time.monotonic();self.stage='處理中';self.controls()
        events=self.events
        def work():
            try:result=operation()
            except Exception as exc:events.put(('failed',str(exc)[:2000]))
            else:events.put(('finished',result))
        start_worker(work,lambda error:events.put(('failed',error)),daemon=False)

    def prepare(self):
        if not self.editable():return
        try:
            if self.question.get('1.0','end-1c').strip() or self.answer.get('1.0','end-1c').strip():raise ValueError('下方驗收題尚未加入列表，請先加入或清空後再準備。')
            suite=idle.checks.suite({'schema':'full_assistant_cases_v1','cases':self.cases})
            settings={key:int(field.get()) for key,field in self.settings.items()};idle.limits(**settings)
            selected=[{'id':r['id'],'revision':r['revision']} for r in self.basket.values()]
            parent,existing=self.parent_batch,self.existing_batch
            self.launch(lambda:jobs.prepare(self.store,selected,suite,settings,parent=parent,existing=existing,cancel=self.cancel))
        except Exception as exc:self.notice.set(str(exc))

    def load(self):
        if self.busy or self.unavailable():return
        try:
            job_id=self.pick(job=True)
            if job_id:self.launch(lambda:{'preview':jobs.inspect(self.store.path.parent,job_id)})
        except Exception as exc:self.notice.set(str(exc))

    def inspect(self):
        if self.busy or self.unavailable() or not self.job:return
        job_id=self.job['id'];self.launch(lambda:{'preview':jobs.inspect(self.store.path.parent,job_id)})

    def run(self):
        if self.busy or self.unavailable() or not self.job or not self.job['startable']:return
        job_id=self.job['id'];self.job['startable']=False
        def work():
            result=idle.run(self.store.path.parent,job_id,cancel=self.cancel,on_progress=lambda value:self.events.put(('progress',value)))
            try:return {'preview':jobs.inspect(self.store.path.parent,job_id),'run_result':result}
            except Exception as exc:return {'run_result':result,'error':'工作已結束，但結果核對失敗：'+str(exc)}
        self.launch(work)

    def stop(self):
        if self.busy:self.cancel.set();self.stage='已要求停止，等候收尾';self.stop_btn.configure(state='disabled')

    def new(self):
        if self.busy or self.unavailable():return
        self.job=None;self.notice.set('編輯新工作；之前的規約與結果仍保留。');self.controls();self.tabs.select(0);self.refresh()

    def open_manifest(self):
        if self.job and not self.unavailable():os.startfile(self.job['manifest_path'])

    def poll(self):
        if not self.begin_poll():return
        for _ in range(20):
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='progress':
                if not self.cancel.is_set():self.stage=jobs.LABELS.get(value['status'],value['status'])
            else:
                self.busy=False
                if kind=='failed':self.notice.set('未完成：'+value)
                else:
                    if value.get('preview'):
                        self.job=value['preview'];self.preview.configure(state='normal');self.preview.delete('1.0','end');self.preview.insert('1.0',self.job['text']);self.preview.configure(state='disabled');self.tabs.select(2)
                        self.notice.set(jobs.LABELS[self.job['status']]+'；來源'+('有效。' if self.job['current'] else '已變更。'))
                    if value.get('error'):
                        self.notice.set(value['error']+(f" 已保留批次：{value['batch_id']}，可用「使用已準備批次」重新選取。" if value.get('batch_id') else ''))
                self.controls();break
        if self.busy and not self.closing:self.notice.set(f'{self.stage}；本視窗工作尚未收尾，已經過{int(time.monotonic()-self.started)}秒。')
        self.schedule_poll()
