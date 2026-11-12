"""Bounded historical status for chat, without replaying tool data or old sources."""
import json
from . import delivery_preference, goal_sources


def goal_context(store, sid):
    # The engine also imports this module while autonomy is being initialized.
    from .autonomy import signature
    from .goal_review import next_action
    with store.connect() as db:
        # Goals and current imported-source versions must describe one observation.
        # Do not use the cross-session review query: it reads full reports/receipts.
        db.execute('BEGIN')
        rows = [dict(row) for row in db.execute('''SELECT g.id,g.objective,g.status,g.updated_at,
            g.question,g.sources_json,length(CAST(g.notes AS BLOB))>0 AS user_notes_present,
            (SELECT parent_goal_id FROM autonomous_handoffs h WHERE h.goal_id=g.id) AS parent_goal_id,
            EXISTS(SELECT 1 FROM autonomous_delivery d WHERE d.goal_id=g.id) AS file_delivery,
            (SELECT COUNT(*) FROM autonomous_artifacts a WHERE a.goal_id=g.id) AS saved_artifact_count,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id) AS step_count
            FROM autonomous_goals g WHERE g.session_id=? ORDER BY g.rowid DESC LIMIT 4''', (sid,))]
        for row in rows:
            if row['file_delivery'] and (change := delivery_preference.latest(db, row['id'])):
                row['file_delivery'] = change['file_delivery']
        sources = signature(db.execute(
            'SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)).fetchall())
        scoped = {row['id'] for row in rows if goal_sources.saved_supplement(db, row['id'])}
    goals = []
    for row in rows[:3]:
        sources_match = goal_sources.matching_scope(json.loads(row['sources_json']), sources, scoped=row['id'] in scoped)
        action, explanation = next_action(row['status'], sources_match, row['step_count'])
        # A saved question is model output, not proof it remains unanswered.
        # Explicit replies clear it; newer guidance/current chat may already answer it.
        question = row['question'] if row['status'] == 'needs_input' else ''
        goals.append({'id': row['id'], 'objective': row['objective'][:400],
                      'objective_truncated': len(row['objective']) > 400,
                      'recorded_status': row['status'], 'step_count': row['step_count'],
                      'recorded_question': question[:800], 'question_truncated': len(question) > 800,
                      'user_notes_present': bool(row['user_notes_present']),
                      'sources_match': sources_match, 'next_action': action,
                      'next_action_explanation': explanation,
                      'parent_goal_id': row['parent_goal_id'],
                      'file_delivery': bool(row['file_delivery']),
                      'saved_artifact_count': row['saved_artifact_count'],
                      'updated_at': row['updated_at']})
    return {'goals': goals, 'limited': len(rows) > 3}
