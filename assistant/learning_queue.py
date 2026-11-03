"""Explicit local training examples with immutable revisions; no training or model calls."""
import hashlib
import json
import re
import uuid

from .feedback_improvement import feedback_source

MAX_ITEMS = 500
MAX_REVISIONS = 5000
PAGE_SIZE = 20
LATEST = '''SELECT q.* FROM learning_examples q JOIN
    (SELECT id,MAX(revision) AS revision FROM learning_examples GROUP BY id) last
    ON q.id=last.id AND q.revision=last.revision'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError(f'{label}需為1至{limit}字的有效文字。')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(f'{label}含無效文字。') from None
    return value


def validate_source(source):
    if source is None:
        return None
    if (not isinstance(source, dict) or set(source) != {'message_id','fingerprint','feedback_revision','question','correction'}
            or type(source['message_id']) is not int or not 1 <= source['message_id'] <= 9223372036854775807
            or type(source['feedback_revision']) is not int or not 1 <= source['feedback_revision'] <= 9223372036854775807
            or not isinstance(source['fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', source['fingerprint'])):
        raise ValueError('學習來源格式不符。')
    text(source['question'], 16000, '原問題')
    text(source['correction'], 4000, '原更正')
    return source


def source_from_feedback(store, message_id, *, db=None):
    if type(message_id) is not int or not 1 <= message_id <= 9223372036854775807:
        raise ValueError('回答編號無效。')
    value = feedback_source(store, message_id, db=db)
    context = value['context']
    if (value['rating'] != -1 or not value['correction'].strip() or not isinstance(context, dict)
            or context.get('response_mode') == 'local_memory' or context.get('image_inputs')):
        raise ValueError('請選擇有具體更正及原問題快照的純文字模型回答。')
    return validate_source({'message_id':message_id,'fingerprint':value['fingerprint'],
        'feedback_revision':value['update_revision'],'question':context.get('current_request'),
        'correction':value['correction']})


def source_current(store, db, source):
    if source is None:
        return True
    try:
        return source_from_feedback(store, source['message_id'], db=db) == source
    except (ValueError, KeyError, TypeError):
        return False


def help_context(question):
    """Static product capabilities only; never reads saved learning examples."""
    related = any(word in question.casefold() for word in ('學習佇列','学习队列','閒置訓練','learning queue','training example','idle training'))
    related = related or ('學習' in question and any(word in question for word in ('例子','範例','回饋','更正')))
    if not related:
        return None
    return {
        'scope':'僅描述這個個人助理的已實作功能，不適用其他軟體；不是使用者資料或操作結果。',
        'queue_contents_provided':False, 'idle_training_implemented':False,
        'finite_manual_batches_available':True,
        'confirmation_updates_weights':False,
        'entry':'回饋紀錄 → 選取已保存的具體更正 → 整理成學習例子；或記憶與夥伴設定 → 本機學習佇列新建獨立例子。',
        'authoring':'原問題可編輯為不依賴前文的完整問題；正確答案留白，使用者自行填寫完整正確答案後按確認。原錯誤回答與更正意見不會自動成為訓練答案。',
        'source_change':'原回饋或回答快照變更，重新整理或核對項目時會顯示需重新核對，保存時也會重新檢查，不能直接沿用；舊例子不自動改寫。載入項目後按重新核對（保留草稿）、閱讀新更正並修改完整答案，再按確認加入／更新。',
        'confirmation':'只保存已確認待訓練版本；尚未啟動閒置訓練、沒有更新模型權重，不能宣稱已學會。',
        'manual_training':'另有scripts/learning/batches.py的明確選取、準備與手動執行有限批次流程；只產生待核對候選，不自動啟用。按確認不會執行此流程，閒置排程尚未實作。',
        'withdrawal':'保留歷史，排除後續學習；不代表刪除備份或已訓練參數已忘記。',
    }


def decode(row):
    row = dict(row)
    raw = row.pop('source_json')
    if not isinstance(raw, str) or len(raw) > 120000:
        raise ValueError('學習來源超過保存上限。')
    row['source'] = validate_source(json.loads(raw))
    if (not isinstance(row['id'], str) or not re.fullmatch('[0-9a-f]{32}', row['id'])
            or type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISIONS
            or row['status'] not in ('confirmed', 'withdrawn')
            or not isinstance(row['recorded_at'], str) or not 1 <= len(row['recorded_at']) <= 50):
        raise ValueError('學習項目版本或狀態不符。')
    text(row['prompt'], 2000, '學習問題'); text(row['answer'], 2000, '正確答案')
    if row['fingerprint'] != digest({k:row[k] for k in ('id','revision','status','prompt','answer','source')}):
        raise ValueError('學習項目內容與指紋不符。')
    return row


def latest(db, item_id):
    if not isinstance(item_id, str) or not re.fullmatch('[0-9a-f]{32}', item_id):
        raise ValueError('學習項目編號無效。')
    row = db.execute('SELECT * FROM learning_examples WHERE id=? ORDER BY revision DESC LIMIT 1', (item_id,)).fetchone()
    if row is None:
        raise ValueError('找不到學習項目。')
    return decode(row)


def observed(store, db, row):
    row['readiness'] = ('withdrawn' if row['status'] == 'withdrawn' else
                        'ready' if source_current(store, db, row['source']) else 'needs_review')
    return row


def get(store, item_id):
    with store.connect() as db:
        db.execute('BEGIN')
        return observed(store, db, latest(db, item_id))


def history(store, item_id, number=1):
    if type(number) is not int or not 1 <= number <= MAX_REVISIONS:
        raise ValueError('歷史頁碼無效。')
    with store.connect() as db:
        db.execute('BEGIN')
        current = latest(db, item_id)
        rows = db.execute('SELECT * FROM learning_examples WHERE id=? ORDER BY revision DESC LIMIT ? OFFSET ?',
                          (item_id, PAGE_SIZE, (number-1)*PAGE_SIZE)).fetchall()
        return {'page':number,'pages':(current['revision']+PAGE_SIZE-1)//PAGE_SIZE,
                'items':[decode(row) for row in rows]}


def page(store, number=1):
    if type(number) is not int or not 1 <= number <= MAX_ITEMS:
        raise ValueError('學習佇列頁碼無效。')
    with store.connect() as db:
        db.execute('BEGIN')
        count = db.execute('SELECT COUNT(DISTINCT id) FROM learning_examples').fetchone()[0]
        rows = db.execute(LATEST+' ORDER BY q.rowid DESC LIMIT ? OFFSET ?', (PAGE_SIZE, (number-1)*PAGE_SIZE)).fetchall()
        return {'page':number, 'total':count, 'pages':max(1, (count+PAGE_SIZE-1)//PAGE_SIZE),
                'items':[observed(store, db, decode(row)) for row in rows]}


def append(db, item_id, revision, status, prompt, answer, source):
    # Reserve one withdrawal for every possible active item even at capacity.
    limit = MAX_REVISIONS if status == 'withdrawn' else MAX_REVISIONS-MAX_ITEMS
    if db.execute('SELECT COUNT(*) FROM learning_examples').fetchone()[0] >= limit:
        raise ValueError('學習歷史已達可新增確認的上限；保留容量供撤回。' if status == 'confirmed' else '學習歷史已達上限。')
    value = {'id':item_id,'revision':revision,'status':status,'prompt':prompt,'answer':answer,'source':source}
    db.execute('''INSERT INTO learning_examples(id,revision,status,prompt,answer,source_json,fingerprint)
        VALUES (?,?,?,?,?,?,?)''', (item_id,revision,status,prompt,answer,encoded(source),digest(value)))
    return latest(db, item_id)


def confirm(store, prompt, answer, *, source=None, item_id=None, expected_revision=None):
    text(prompt, 2000, '學習問題'); text(answer, 2000, '正確答案')
    source = json.loads(encoded(validate_source(source)))  # Detach caller-owned data.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = None
        if item_id is not None:
            old = latest(db, item_id)
            if type(expected_revision) is not int or old['revision'] != expected_revision:
                raise ValueError('項目已變更，請重新核對已保存內容；草稿不會覆蓋新版本。')
            # A feedback-linked item keeps its lineage; new independent examples
            # use the explicit new-item entry rather than dropping stale evidence.
            if (old['source'] is None) != (source is None) or (source and old['source']['message_id'] != source['message_id']):
                raise ValueError('不能替換原項目的來源；請建立新的獨立例子。')
        elif expected_revision is not None:
            raise ValueError('新項目不能指定舊版本。')
        elif db.execute('SELECT COUNT(DISTINCT id) FROM learning_examples').fetchone()[0] >= MAX_ITEMS:
            raise ValueError('學習項目已達500筆上限。')
        if not source_current(store, db, source):
            raise ValueError('原回答或更正已變更，請重新核對來源後再確認。')
        for row in db.execute(LATEST+" WHERE q.status='confirmed' AND q.prompt=?", (prompt,)):
            other = decode(row)
            if other['id'] != item_id:
                raise ValueError('已有相同問題的確認項目；請更新或撤回原項目，避免重複或矛盾答案。')
        if old and all(old[k] == v for k,v in {'status':'confirmed','prompt':prompt,'answer':answer,'source':source}.items()):
            return observed(store, db, old)
        value = append(db, item_id or uuid.uuid4().hex, old['revision']+1 if old else 1,
                       'confirmed', prompt, answer, source)
        return observed(store, db, value)


def withdraw(store, item_id, expected_revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = latest(db, item_id)
        if type(expected_revision) is not int or old['revision'] != expected_revision:
            raise ValueError('項目已變更，請重新核對後撤回。')
        if old['status'] == 'withdrawn':
            return observed(store, db, old)
        value = append(db,item_id,old['revision']+1,'withdrawn',old['prompt'],old['answer'],old['source'])
        return observed(store, db, value)


def validate_saved(db):
    """Bounded structural/history validation; hashes do not authenticate truth."""
    if db.execute('SELECT COUNT(*) FROM learning_examples').fetchone()[0] > MAX_REVISIONS:
        raise ValueError('學習歷史超限。')
    cursor = db.execute('SELECT * FROM learning_examples ORDER BY id,revision')
    keys = [d[0] for d in cursor.description]
    last = {}
    for raw in cursor:
        row = decode(dict(zip(keys, raw)))
        previous = last.get(row['id'])
        if row['revision'] != (previous['revision']+1 if previous else 1) or (previous is None and row['status'] != 'confirmed'):
            raise ValueError('學習版本歷史不完整。')
        if previous:
            a, b = previous['source'], row['source']
            if (a is None) != (b is None) or (a and a['message_id'] != b['message_id']):
                raise ValueError('學習來源歷史不一致。')
            if row['status'] == 'withdrawn' and (previous['status'] != 'confirmed' or any(row[k] != previous[k] for k in ('prompt','answer','source'))):
                raise ValueError('學習撤回歷史不一致。')
        last[row['id']] = row
    if len(last) > MAX_ITEMS:
        raise ValueError('學習項目超限。')
    prompts = [row['prompt'] for row in last.values() if row['status'] == 'confirmed']
    if len(prompts) != len(set(prompts)):
        raise ValueError('學習佇列包含重複確認問題。')
