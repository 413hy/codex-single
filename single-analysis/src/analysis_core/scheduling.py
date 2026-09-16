"""Durable spacing between automatic analysis starts, independent of wall-clock buckets."""
import json

from analysis_core.store import encode, identity


def claim_due(store, now):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')

        def get(key, default):
            row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

        if get('analysis_paused', False):
            return None
        seconds = get('analysis_interval', 20) * 60
        due = get('next_analysis_at', None)
        if due is None:
            latest = db.execute('SELECT MAX(started_at) FROM cycles').fetchone()[0]
            due = latest + seconds if latest is not None else now
        if now < due:
            return None
        cid = 'scheduled:' + identity(now)
        db.execute("INSERT INTO cycles(cycle_id,started_at,status) VALUES (?,?,'RUNNING')", (cid, now))
        db.execute(
            'INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('next_analysis_at', encode(now + seconds)),
        )
        return cid
