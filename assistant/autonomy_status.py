"""Bounded historical status for chat, without replaying tool data or old sources."""


def goal_context(store, sid):
    with store.connect() as db:
        rows = db.execute('''SELECT g.id,g.objective,g.status,g.updated_at,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id) AS step_count
            FROM autonomous_goals g WHERE g.session_id=? ORDER BY g.rowid DESC LIMIT 4''', (sid,)).fetchall()
    return {'goals': [{'id': row['id'], 'objective': row['objective'][:400],
                      'objective_truncated': len(row['objective']) > 400,
                      'recorded_status': row['status'], 'step_count': row['step_count'],
                      'updated_at': row['updated_at']} for row in rows[:3]],
            'limited': len(rows) > 3}
