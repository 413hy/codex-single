"""Single writer publication ledger; trading consumers open this database read-only."""

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path


class SignalBus:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS publications (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL, published_at REAL NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL)"
            )

            db.execute("CREATE INDEX IF NOT EXISTS publication_expiry ON publications(expires_at)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def publish(self, signal_id, decision, *, normal, hedge, observed_at, now=None):
        now = time.time() if now is None else now
        payload = dict(
            version=1,
            signal_id=signal_id,
            **decision.model_dump(),
            normal_candidate=normal,
            hedge=hedge,
            observed_at=observed_at,
            published_at=now,
            expires_at=now + 60,
        )
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO publications(signal_id,symbol,published_at,expires_at,payload) VALUES (?,?,?,?,?)",
                (
                    signal_id,
                    decision.symbol,
                    now,
                    now + 60,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            stored = db.execute(
                "SELECT payload FROM publications WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return json.loads(stored[0])


class HedgeSniffer:
    """Reads an exchange-confirmed snapshot owned by the hedge trading monitor."""

    def __init__(self, path):
        self.path = Path(path)

    def sniff(self, now=None):
        now = time.time() if now is None else now
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            values = {
                r["key"]: json.loads(r["value"])
                for r in db.execute(
                    "SELECT key,value FROM state WHERE key IN ('monitor_heartbeat','exchange_positions')"
                )
            }
            age = now - values.get("monitor_heartbeat", 0)
            if not 0 <= age <= 20:
                raise RuntimeError("对冲仓位快照超过20秒或时间异常，无法确认待判向币种")
            positions = values.get("exchange_positions", [])
            result = {}
            for row in db.execute("SELECT document FROM hedge_groups"):
                g = json.loads(row[0])
                if g["phase"] != "LOCKED":
                    continue
                pair = []
                for tid in (g["active"], g["child"]):
                    t = db.execute("SELECT * FROM trades WHERE trade_id=?", (tid,)).fetchone()
                    if (
                        not t or t["status"] != "OPEN" or t["owned"] != 1
                        or t["symbol"] != g["symbol"]
                    ):
                        raise RuntimeError("双仓账本状态与等待判向状态不一致")
                    matching = [
                        p
                        for p in positions
                        if p["symbol"] == t["symbol"]
                        and int(p["positionIdx"]) == t["position_idx"]
                        and p["side"] == ("Buy" if t["side"] == "LONG" else "Sell")
                    ]
                    if len(matching) != 1 or Decimal(matching[0]["size"]) <= 0:
                        raise RuntimeError("双仓交易所快照缺少对应仓位")
                    actual = matching[0]
                    qty, entry = Decimal(t["qty"]), Decimal(t["entry_price"])
                    if Decimal(actual["size"]) > qty or abs(
                        Decimal(actual["avgPrice"]) - entry
                    ) > entry * Decimal(".000001"):
                        raise RuntimeError("双仓数量/均价与本系统账本不一致")
                    pair.append(actual)
                if {int(p["positionIdx"]) for p in pair} != {1, 2}:
                    raise RuntimeError("双向仓位槽位无效")
                result[g["symbol"]] = dict(
                    group_id=g["group_id"], generation=g["generation"], positions=pair
                )
            return result
        finally:
            db.close()
