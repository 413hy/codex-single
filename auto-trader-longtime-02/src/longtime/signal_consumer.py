"""Read-only signal subscriber. No model calls, scanning, or analysis scheduling."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from longtime.model import Decision


class SignalConsumer:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.path = Path(app.settings.signal_db)

    def pending(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            # Only retrieve currently-live publications. Historical rows remain in the
            # producer ledger, and each subscriber keeps its own durable signal claim.
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM publications WHERE expires_at>? ORDER BY id LIMIT 100",
                    (time.time(),),
                )
            ]
        finally:
            db.close()

    async def tick(self):
        async with self.app.cycle_lock:
            for row in self.pending():
                await self.consume(row)
            self.store.set("signal_heartbeat", time.time())
            self.store.resolve("SIGNAL_FEED")

    async def consume(self, row):
        payload = json.loads(row["payload"])
        required = {
            "version",
            "signal_id",
            "symbol",
            "decision",
            "reason",
            "normal_candidate",
            "hedge",
            "observed_at",
            "published_at",
            "expires_at",
        }
        if (
            set(payload) != required
            or payload["version"] != 1
            or type(payload["normal_candidate"]) is not bool
        ):
            raise ValueError("信号结构无效")
        if payload["signal_id"] != row["signal_id"] or payload["symbol"] != row["symbol"]:
            raise ValueError("信号身份不匹配")
        now = time.time()
        published = float(payload["published_at"])
        expires = float(payload["expires_at"])
        if (
            published != row["published_at"]
            or expires != row["expires_at"]
            or expires - published != 60
        ):
            raise ValueError("信号时间字段不一致")
        if not published <= now < expires:
            return
        decision = Decision.model_validate(
            {k: payload[k] for k in ("symbol", "decision", "reason")}
        )
        hedge = payload["hedge"]
        if hedge is not None and (
            not isinstance(hedge, dict)
            or set(hedge) != {"group_id", "generation"}
            or type(hedge["generation"]) is not int
        ):
            raise ValueError("对冲信号归属无效")
        sid = "feed:" + payload["signal_id"]
        # Atomic durable claim: any failure/crash after claim is handled through order
        # intent reconciliation, NEVER by replaying an old direction.
        if not self.store.signal(sid, sid, decision.symbol, payload):
            return
        try:
            if decision.decision == "SKIP":
                self.store.signal_result(sid, "SKIP_MODEL", "SKIP")
                return
            result = None
            if hasattr(self.app.executor, "hedge") and hedge:
                group = self.app.executor.hedge.get(hedge["group_id"])
                if (
                    group
                    and group["symbol"] == decision.symbol
                    and group["phase"] == "LOCKED"
                    and group["generation"] == hedge["generation"]
                ):
                    result = await self.app.executor.hedge.decide(
                        group["group_id"],
                        sid,
                        decision,
                        deadline=expires,
                        expected_generation=hedge["generation"],
                    )
            if result is None:
                if not payload["normal_candidate"]:
                    result = "SKIP_NOT_NORMAL_CANDIDATE"
                elif self.store.state("entries_paused", False):
                    result = "SKIP_PAUSED"
                elif time.time() >= expires:
                    result = "SKIP_SIGNAL_EXPIRED"
                else:
                    result = await self.app.executor.enter("feed", sid, decision, deadline=expires)
            effective_side = "SKIP" if result == "SKIP_TP_UNREACHABLE" else decision.decision
            self.store.signal_result(sid, result, effective_side, decision.model_dump())
        except asyncio.CancelledError:
            self.store.signal_result(sid, "INTERRUPTED", decision.decision)
            self.store.event("SIGNAL_INTERRUPTED", {"signal_id": sid, "recovery": "original_order_intents_only"})
            raise
        except ValueError as error:
            if str(error).startswith("SKIP_"):
                self.store.signal_result(sid, str(error).split(":")[0])
                return
            self.store.signal_result(sid, "ERROR")
            self.app.executor.alert("SIGNAL:" + sid, "monitor", {"symbol": decision.symbol}, error)
        except Exception as error:
            self.store.signal_result(sid, "ERROR")
            self.app.executor.alert("SIGNAL:" + sid, "monitor", {"symbol": decision.symbol}, error)
