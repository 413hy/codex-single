"""Read-only signal subscriber. No model calls, scanning, or analysis scheduling."""

import asyncio
import json
import math
import sqlite3
import time
from pathlib import Path

from longtime.model import Decision
from longtime.store import encode, identity


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
                    "SELECT * FROM publications WHERE expires_at>? ORDER BY id",
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

    @staticmethod
    def validate(row):
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("信号必须是对象")
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
        version = payload.get("version")
        if version == 2:
            required |= {"cycle_id", "analysis_started_at"}
        if (
            set(payload) != required
            or type(version) is not int
            or version not in (1, 2)
            or type(payload["normal_candidate"]) is not bool
        ):
            raise ValueError("信号结构无效")
        if payload["signal_id"] != row["signal_id"] or payload["symbol"] != row["symbol"]:
            raise ValueError("信号身份不匹配")
        if not isinstance(payload["signal_id"], str) or not 1 <= len(payload["signal_id"]) <= 200:
            raise ValueError("信号编号无效")
        if any(type(payload[key]) not in (int, float) for key in ("published_at", "expires_at")):
            raise ValueError("信号时间类型无效")
        published = float(payload["published_at"])
        expires = float(payload["expires_at"])
        if (
            not math.isfinite(published)
            or not math.isfinite(expires)
            or published != row["published_at"]
            or expires != row["expires_at"]
            or expires - published != 600
        ):
            raise ValueError("信号时间字段不一致")
        sid = "feed:" + payload["signal_id"]
        cycle_id = sid
        if version == 2:
            started = payload["analysis_started_at"]
            cycle_id = payload["cycle_id"]
            if (
                not isinstance(cycle_id, str)
                or not 1 <= len(cycle_id) <= 200
                or type(started) not in (int, float)
                or not 0 < float(started) <= published
            ):
                raise ValueError("分析轮次字段无效")
        decision = Decision.model_validate(
            {k: payload[k] for k in ("symbol", "decision", "reason")}
        )
        hedge = payload["hedge"]
        if hedge is not None and (
            not isinstance(hedge, dict)
            or set(hedge) != {"group_id", "generation"}
            or not isinstance(hedge["group_id"], str)
            or not 1 <= len(hedge["group_id"]) <= 200
            or type(hedge["generation"]) is not int
            or hedge["generation"] < 0
        ):
            raise ValueError("对冲信号归属无效")
        return payload, decision, cycle_id, published, expires

    def reject(self, row, key):
        # Keep only bounded identity metadata: validation errors can contain raw input.
        at = time.time()
        evidence = {"publication_id": row["id"], "rejection_id": key, "status": "REJECTED"}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "INSERT OR IGNORE INTO state(key,value) VALUES (?,?)",
                (key, encode(evidence)),
            ).rowcount:
                return
            db.execute(
                "INSERT INTO events(created_at,kind,payload) VALUES (?,?,?)",
                (at, "SIGNAL_REJECTED", encode(evidence)),
            )
        self.app.executor.alert(
            key, "monitor", evidence, ValueError("发布信号结构、身份或时间字段无效，已拒绝且不重放")
        )

    async def consume(self, row):
        key = "SIGNAL_REJECTED:" + identity(str(self.path.resolve()), row["id"], row["signal_id"])
        if self.store.state(key) is not None:
            return
        try:
            payload, decision, cycle_id, published, expires = self.validate(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            # Only decoding/validation is isolated here. Storage failures still fail the
            # feed tick, and execution failures retain the existing durable-claim path.
            self.reject(row, key)
            return
        if not published <= time.time() < expires:
            return
        sid = "feed:" + payload["signal_id"]
        hedge = payload["hedge"]
        # Atomic durable claim: any failure/crash after claim is handled through order
        # intent reconciliation, NEVER by replaying an old direction.
        if not self.store.signal(sid, cycle_id, decision.symbol, payload):
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
