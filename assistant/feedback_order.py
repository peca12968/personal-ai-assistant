"""Transactional feedback save order, independent of wall-clock observations."""


def initialize(db):
    # Called inside Store's initialization write transaction. Only legacy stores
    # lacking the table are seeded; never silently repair missing current rows.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_versions'").fetchone():
        validate(db)
        return
    db.execute('''CREATE TABLE feedback_versions (
        revision INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL UNIQUE REFERENCES feedback(message_id) ON DELETE CASCADE,
        order_basis TEXT NOT NULL CHECK(order_basis IN ('legacy_timestamp','recorded_update')))''')
    db.execute('''INSERT INTO feedback_versions(message_id,order_basis)
        SELECT message_id,'legacy_timestamp' FROM feedback ORDER BY updated_at,message_id''')


def record(db, message_id):
    """Caller must update the feedback and its order in the same transaction."""
    db.execute('DELETE FROM feedback_versions WHERE message_id=?', (message_id,))
    db.execute("INSERT INTO feedback_versions(message_id,order_basis) VALUES(?,'recorded_update')", (message_id,))


def validate(db):
    if db.execute('''SELECT 1 FROM feedback f LEFT JOIN feedback_versions v ON v.message_id=f.message_id
        WHERE v.message_id IS NULL LIMIT 1''').fetchone():
        raise ValueError('回饋保存順序不完整。')
    if db.execute('''SELECT 1 FROM feedback_versions v LEFT JOIN feedback f ON f.message_id=v.message_id
        WHERE f.message_id IS NULL OR typeof(v.revision)!='integer' OR v.revision<1
        OR typeof(v.message_id)!='integer' OR v.message_id<1 OR typeof(v.order_basis)!='text'
        OR v.order_basis NOT IN ('legacy_timestamp','recorded_update') LIMIT 1''').fetchone():
        raise ValueError('回饋保存順序格式不符。')
    counts = db.execute('SELECT COUNT(*),COUNT(DISTINCT revision),COUNT(DISTINCT message_id) FROM feedback_versions').fetchone()
    if len(set(counts)) != 1:
        raise ValueError('回饋保存順序重複。')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone():
        raise ValueError('回饋保存順序計數器不存在。')
    largest = db.execute('SELECT COALESCE(MAX(revision),0) FROM feedback_versions').fetchone()[0]
    sequences = db.execute("SELECT seq FROM sqlite_sequence WHERE name='feedback_versions'").fetchall()
    if (len(sequences) > 1 or (not sequences and largest) or
            sequences and (type(sequences[0][0]) is not int or sequences[0][0] < largest or sequences[0][0] < 0)):
        raise ValueError('回饋保存順序計數器不完整。')
