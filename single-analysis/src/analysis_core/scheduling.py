"""Persistent fixed cadence anchored at midnight in Asia/Shanghai."""
import json

from analysis_core.store import encode, identity

# 1970-01-01 00:00 Asia/Shanghai. Continuous cadence also supports 70, 100, etc.
ANCHOR = -8 * 3600


def next_slot(now, minutes):
    seconds = minutes * 60
    return ANCHOR + ((now - ANCHOR) // seconds + 1) * seconds


def _get(db, key, default=None):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put_due(db, due):
    db.execute(
        'INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        ('next_analysis_at', encode(due)),
    )


def reset_schedule(store, now, *, only_overdue=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        due = _get(db, 'next_analysis_at')
        if not only_overdue or due is None or due <= now:
            _put_due(db, next_slot(now, _get(db, 'analysis_interval', 20)))


def claim_due(store, now):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _get(db, 'analysis_paused', False):
            return None
        if db.execute("SELECT 1 FROM cycles WHERE status='RUNNING'").fetchone():
            return None
        minutes = _get(db, 'analysis_interval', 20)
        following = next_slot(now, minutes)
        due = _get(db, 'next_analysis_at')
        if due is None:
            _put_due(db, following)
            return None
        if now < due:
            return None
        # Only claim the current scheduled slot; older missed slots are never replayed.
        current = following - minutes * 60
        if due != current:
            _put_due(db, following)
            return None
        cid = 'scheduled:' + identity(due)
        inserted = db.execute(
            "INSERT OR IGNORE INTO cycles(cycle_id,started_at,status) VALUES (?,?,'RUNNING')",
            (cid, now),
        ).rowcount
        _put_due(db, following)
        return cid if inserted else None
