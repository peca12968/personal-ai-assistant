"""User-started, time/step bounded continuation of one selected autonomous goal."""
import threading
import time
import uuid

from . import autonomy
from .answer_runs import lease
from .engine import EngineError


REASONS = {
    'running': '執行紀錄尚未結束；不代表程序仍存活',
    'report_ready': '成果待確認', 'needs_input': '等待你的補充',
    'step_budget': '此次步數上限', 'goal_limit': '目標累計32步上限',
    'deadline': '此次時間上限', 'cancelled': '已要求停止本機流程',
    'sources_changed': '參考來源變更，需另建目標',
    'repeated_errors': '連續三次工具錯誤', 'failed': '流程錯誤，未自動重試',
    'interrupted': '先前本機執行中斷，雲端請求狀態未知',
}


class Deadline:
    """Composite Event-like cancellation; also checked inside model/tool polling."""
    def __init__(self, cancel, seconds):
        self.cancel = cancel
        self.expires = time.monotonic() + seconds
        self.reason = None

    def is_set(self):
        if self.reason is None:
            if self.cancel.is_set():
                self.reason = 'cancelled'
            elif time.monotonic() >= self.expires:
                self.reason = 'deadline'
        return self.reason is not None


def validate_limits(max_steps, max_seconds):
    if type(max_steps) is not int or not 1 <= max_steps <= autonomy.MAX_STEPS:
        raise ValueError('持續執行的新增步數需為1到32；每個目標仍累計最多32步。')
    if type(max_seconds) is not int or not 60 <= max_seconds <= 7200:
        raise ValueError('持續執行時間需為60到7200秒（1到120分鐘）。')


def recover_locked(store, gid, step_count):
    """Caller has checked the session and acquired the goal lease, including manual runs."""
    with store.connect() as db:
        db.execute("""UPDATE autonomous_continuous_runs
            SET reason='interrupted',end_step=?,stopped_at=CURRENT_TIMESTAMP
            WHERE goal_id=? AND reason='running'""", (step_count, gid))


def run_continuous(store, sid, gid, engine, *, max_steps=24, max_seconds=1800,
                   cancel=None, progress=None):
    validate_limits(max_steps, max_seconds)
    token = Deadline(cancel if cancel is not None else threading.Event(), max_seconds)
    with lease(store, gid):
        goal = autonomy.get_goal(store, sid, gid)  # Validate session before mutation.
        # Only the acquired exclusive lease proves that the earlier local owner exited.
        recover_locked(store, gid, len(goal['steps']))
        if goal['status'] in ('report_ready', 'needs_input'):
            return autonomy.get_goal(store, sid, gid)
        rid = uuid.uuid4().hex
        with store.connect() as db:
            db.execute('''INSERT INTO autonomous_continuous_runs
                (id,goal_id,max_steps,max_seconds,start_step) VALUES(?,?,?,?,?)''',
                (rid, gid, max_steps, max_seconds, len(goal['steps'])))

        def finish(reason):
            with store.connect() as db:
                steps = db.execute('SELECT COUNT(*) FROM autonomous_steps WHERE goal_id=?', (gid,)).fetchone()[0]
                db.execute('''UPDATE autonomous_continuous_runs SET reason=?,end_step=?,
                    stopped_at=CURRENT_TIMESTAMP WHERE id=?''', (reason, steps, rid))
                if reason in ('deadline', 'cancelled', 'interrupted'):
                    db.execute("""UPDATE autonomous_goals SET status='paused',notice=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""", (REASONS[reason] + '；已保存動作保留，可查看後接續。', gid))

        try:
            _, reason = autonomy._run_locked(store, sid, gid, engine, max_steps=max_steps,
                cancel=token, progress=progress or (lambda value: None), failure_limit=3)
        except EngineError as exc:
            if exc.code == 'cancelled' and token.is_set():
                finish(token.reason)
                return autonomy.get_goal(store, sid, gid)
            finish('failed')
            raise
        except BaseException as exc:
            finish('failed' if isinstance(exc, Exception) else 'interrupted')
            raise
        # A late cancel must not erase an already recorded question or delivery.
        finish(token.reason if reason == 'cancelled' and token.reason else reason)
        return autonomy.get_goal(store, sid, gid)
