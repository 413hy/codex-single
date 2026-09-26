"""Analysis-only SQLite ledger; this project never submits exchange orders."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from analysis_core.notices import incident_text


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False
    )


def identity(*parts):
    return hashlib.sha256(encode(parts).encode()).hexdigest()[:24]


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,started_at REAL NOT NULL,completed_at REAL,
                    status TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,cycle_id TEXT NOT NULL,symbol TEXT NOT NULL,
                    status TEXT NOT NULL,side TEXT,analysis TEXT,evidence TEXT NOT NULL,
                    created_at REAL NOT NULL,UNIQUE(cycle_id,symbol));
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,
                    payload TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,
                    error TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS active_incident ON incidents(scope)
                    WHERE status IN ('OPEN','RUNNING');
                CREATE TABLE IF NOT EXISTS outbox (
                    event_key TEXT PRIMARY KEY,text TEXT NOT NULL,markup TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',message_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,created_at REAL NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS callbacks (
                    callback_id TEXT PRIMARY KEY,created_at REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=3000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def state(self, key, default=None):
        rows = self.rows("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key, value):
        self.execute(
            "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encode(value)),
        )

    def event(self, kind, payload):
        self.execute(
            "INSERT INTO events(created_at,kind,payload) VALUES (?,?,?)",
            (time.time(), kind, encode(payload)),
        )

    def claim_cycle(self, cycle_id):
        return bool(
            self.execute(
                "INSERT OR IGNORE INTO cycles(cycle_id,started_at,status) VALUES (?,?,'RUNNING')",
                (cycle_id, time.time()),
            )
        )

    def signal(self, signal_id, cycle_id, symbol, evidence):
        return bool(
            self.execute(
                "INSERT OR IGNORE INTO signals VALUES (?,?,?,'SELECTED',NULL,NULL,?,?)",
                (signal_id, cycle_id, symbol, encode(evidence), time.time()),
            )
        )

    def signal_result(self, signal_id, status, side=None, analysis=None):
        self.execute(
            "UPDATE signals SET status=?,side=COALESCE(?,side),analysis=COALESCE(?,analysis) WHERE signal_id=?",
            (status, side, encode(analysis) if analysis is not None else None, signal_id),
        )

    def queue(self, key, text, markup=None):
        self.execute(
            "INSERT OR IGNORE INTO outbox(event_key,text,markup) VALUES (?,?,?)",
            (key, text, encode(markup) if markup else None),
        )

    def incident(self, scope, kind, payload, error):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT incident_id FROM incidents WHERE scope=? AND status IN ('OPEN','RUNNING')",
                (scope,),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE incidents SET error=?,payload=? WHERE incident_id=?",
                    (error, encode(payload), existing[0]),
                )
                return existing[0]
            iid = identity(scope, time.time_ns())
            db.execute(
                "INSERT INTO incidents VALUES (?,?,?,?,'OPEN',?,?)",
                (iid, scope, kind, encode(payload), time.time(), error),
            )
            symbol = payload.get("symbol")
            text = incident_text(scope, kind, error, iid, symbol)
            markup = {"inline_keyboard": [[{"text": "🔄 重试", "callback_data": "retry:" + iid}]]}
            db.execute(
                "INSERT INTO outbox(event_key,text,markup) VALUES (?,?,?)",
                ("alert:" + iid, text, encode(markup)),
            )
            return iid

    def resolve(self, scope):
        self.execute(
            "UPDATE incidents SET status='RESOLVED' WHERE scope=? AND status IN ('OPEN','RUNNING')",
            (scope,),
        )

    def claim_callback(self, callback_id, iid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "INSERT OR IGNORE INTO callbacks VALUES (?,?)", (callback_id, time.time())
            ).rowcount:
                return False
            return bool(
                db.execute(
                    "UPDATE incidents SET status='RUNNING' WHERE incident_id=? AND status='OPEN'",
                    (iid,),
                ).rowcount
            )
