"""Keep a provisional report attached to its own saved clarification step."""
import json

from .documents import check_citations
from . import goal_guidance_sources, goal_reply_sources


FOLLOWUP_GUIDANCE = (
    '有新補充時，依原目標逐欄辨認對象、目前狀態及數字的用途。'
    '先保留已確認或未受更正影響的欄位；某筆交接、扣除或盤點尚未確認時，'
    '待確認範圍限於該筆及其影響的結果，不擴及同一對象的其他已知欄位，也不重問整份工作有無異動。'
    '相鄰的更正分句換了對象，若仍在對照同一項紀錄，先以最近明說的欄位解讀「那筆」「照原數」或「補登」，'
    '再核對新對象該欄的原值與原話。例如前句只更正甲批的退回量，接著說乙批要等補登才結數，'
    '若語意仍在說退回，應只問乙批退回總數，保留乙批原有數，也不把甲批的數字套到乙批。'
    '新分句若明指其他欄位、已換話題，或是引述別人的說法，按該分句和來源判斷。'
    '若原話仍可能指兩個欄位，且採用哪一欄會改變成果，只確認所指欄位；已給出的數字不用重報。'
    '分開保存文件原數、已確認的增加或減少量、與正式採用的總數。'
    '引文、假設與未結數保留為背景；可能重複或不知道增量是否另計時，只問實際需要採用的正式總數或計算所缺的關係。'
    '已確認為互不重複的增量可依原數推得總數，不再要求重報。'
    '先完成可確認的欄位。仍需ask時，question直接點名每個會影響交付的待補欄位；'
    '如需說明已知數或其他對象，放在partial_result。若已能完成或使用者允許留待確認，依原要求交付。'
)


MAX_CHARS = 4000


def validate(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS or '\0' in text:
        raise ValueError('待補期間的成果需為1至4000字。')
    try:
        text.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('待補期間的成果含無效文字。') from exc


def saved(action, observation):
    args = action.get('args', {})
    if 'partial_result' not in observation and (not isinstance(args, dict) or 'partial_result' not in args):
        return None
    if not isinstance(args, dict):
        raise ValueError('待補期間的成果缺少保存動作。')
    partial = observation.get('partial_result')
    validate(partial)
    question = args.get('question')
    if (action.get('tool') != 'ask' or set(args) != {'question', 'partial_result'}
            or args['partial_result'] != partial or not isinstance(question, str)
            or not question.strip() or observation.get('question') != question):
        raise ValueError('待補期間的成果與保存問題不一致。')
    return partial


def question_citations(question, excerpts, reply_sources, guidance_sources=()):
    documents = check_citations(question, excerpts)
    if any(not item['range_valid'] for item in documents):
        raise ValueError('問題有未讀取的來源行號，請先讀取或改以文字描述缺漏。')
    replies = goal_reply_sources.citations(question, reply_sources, label='問題')
    guidance = goal_guidance_sources.citations(question, guidance_sources, label='問題')
    if not (documents or replies or guidance):
        return None
    result = {'documents': documents, 'replies': replies}
    if guidance:
        result['guidance'] = guidance
    return result


def waiting(question, action, observation):
    partial = saved(action, observation)
    details = {'partial_result': partial} if partial is not None else {}
    if 'question_citations' in observation:
        details['question_citations'] = observation['question_citations']
    if details and goal_reply_sources.checked_question(action, observation) != question:
        raise ValueError('保存的待補資料與目前問題不一致。')
    return details


def read_waiting(db, gid, question):
    row = db.execute('SELECT action_json,observation_json FROM autonomous_steps WHERE goal_id=? '
                     'ORDER BY sequence DESC LIMIT 1', (gid,)).fetchone()
    if row is not None:
        return waiting(question, json.loads(row[0]), json.loads(row[1]))
    return {}


def validate_saved(db, *, reply_sources_present, guidance_sources_present=False):
    query = '''SELECT DISTINCT g.id,g.status,g.question,g.notes FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id
        WHERE s.action_json LIKE '%"partial_result"%' OR s.observation_json LIKE '%"partial_result"%'
        OR s.observation_json LIKE '%"question_citations"%' '''
    if guidance_sources_present:
        query += ''' UNION SELECT g.id,g.status,g.question,g.notes FROM autonomous_goals g
            JOIN autonomous_guidance_sources c ON c.goal_id=g.id '''
    for gid, status, current_question, notes in db.execute(query):
        replies = goal_reply_sources.read(db, gid, notes) if reply_sources_present else []
        excerpts = []
        steps = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                           'WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        guidance = goal_guidance_sources.read(db, gid, notes, len(steps)) if guidance_sources_present else []
        for sequence, action_json, observation_json in steps:
            action, observation = json.loads(action_json), json.loads(observation_json)
            partial = saved(action, observation)
            if partial is not None:
                expected = check_citations(partial, excerpts)
                if observation.get('citation_checks') != expected or any(not row['range_valid'] for row in expected):
                    raise ValueError('待補期間的成果引用與保存來源不一致。')
            prior_guidance = goal_guidance_sources.prior_sources(guidance, sequence)
            if 'question_citations' in observation or (action.get('tool') == 'ask' and prior_guidance):
                question = goal_reply_sources.checked_question(action, observation)
                prior = [source for source in replies if source['ask_sequence'] < sequence]
                expected = question_citations(question, excerpts, prior, prior_guidance)
                if (observation.get('question_citations') != expected
                        or (expected is None and 'question_citations' in observation)):
                    raise ValueError('問題引用與保存的來源不一致。')
            excerpts.extend(observation.get('excerpts', []))
        if status == 'needs_input':
            read_waiting(db, gid, current_question)
