"""Independent SQLite ledger. Intent durability precedes every exchange mutation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from longtime.notices import incident_text


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
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,cycle_id TEXT,signal_id TEXT,symbol TEXT NOT NULL,
                    side TEXT NOT NULL,position_idx INTEGER NOT NULL,status TEXT NOT NULL,
                    owned INTEGER NOT NULL DEFAULT 1,analysis TEXT,entry_price TEXT,exit_price TEXT,
                    qty TEXT,margin TEXT,leverage TEXT,tp_price TEXT,tp_target_net_pnl TEXT,
                    sl_price TEXT,order_id TEXT,tp_order_id TEXT,sl_order_id TEXT,
                    opened_at REAL,closed_at REAL,holding_duration REAL,realized_pnl TEXT,
                    fees TEXT,net_pnl TEXT,close_reason TEXT,details TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS owned_active_slot ON trades(symbol,position_idx)
                    WHERE owned=1 AND status IN ('INTENT','OPEN');
                CREATE TABLE IF NOT EXISTS hedge_groups (
                    group_id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    link_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,kind TEXT NOT NULL,
                    payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'INTENT',
                    exchange_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,receipt TEXT);
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

    def insert_trade(self, trade):
        columns = list(trade)
        with self.connect() as db:
            allowed = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
            if not set(columns) <= allowed:
                raise ValueError("Invalid trade fields")
            db.execute(
                f"INSERT INTO trades({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(trade[k] for k in columns),
            )

    def trade(self, trade_id):
        rows = self.rows("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
        return rows[0] if rows else None

    def update_trade(self, trade_id, *, notification=None, **fields):
        with self.connect() as db:
            allowed = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
            if not set(fields) <= allowed:
                raise ValueError("Invalid trade fields")
            db.execute(
                f"UPDATE trades SET {','.join(k + '=?' for k in fields)} WHERE trade_id=?",
                (*fields.values(), trade_id),
            )
            if notification:
                db.execute(
                    "INSERT OR IGNORE INTO outbox(event_key,text) VALUES (?,?)", notification
                )

    def reserved(self):
        return {
            r["symbol"]
            for r in self.rows(
                "SELECT symbol FROM trades WHERE status IN ('INTENT','OPEN','SETTLING')"
            )
        }

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
            if not symbol and payload.get("trade_id"):
                trade = db.execute(
                    "SELECT symbol FROM trades WHERE trade_id=?", (payload["trade_id"],)
                ).fetchone()
                symbol = trade[0] if trade else None
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
