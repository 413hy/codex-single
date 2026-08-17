from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from bybit_signal.domain.models import (
    AnalysisCycleResult,
    EvidenceBundle,
    SignalConclusion,
)


class SignalStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("PRAGMA journal_mode=WAL")
            await database.execute("PRAGMA foreign_keys=ON")
            await database.executescript(
                """
                CREATE TABLE IF NOT EXISTS cycles (
                    analysis_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conclusions (
                    analysis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    strength TEXT NOT NULL,
                    direction TEXT,
                    tracking_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, symbol),
                    FOREIGN KEY (analysis_id) REFERENCES cycles(analysis_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS conclusions_symbol_time
                    ON conclusions(symbol, generated_at DESC);
                CREATE TABLE IF NOT EXISTS evidence_bundles (
                    analysis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, symbol),
                    FOREIGN KEY (analysis_id) REFERENCES cycles(analysis_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    analysis_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, chat_id, kind)
                );
                """
            )
            await database.commit()

    async def save_cycle(
        self,
        cycle: AnalysisCycleResult,
        bundles: tuple[EvidenceBundle, ...],
    ) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("PRAGMA foreign_keys=ON")
            await database.execute("BEGIN IMMEDIATE")
            try:
                await database.execute(
                    """
                    INSERT INTO cycles(
                        analysis_id, started_at, completed_at, context_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cycle.analysis_id,
                        cycle.started_at.isoformat(),
                        cycle.completed_at.isoformat(),
                        cycle.context_sha256,
                        cycle.model_dump_json(),
                    ),
                )
                await database.executemany(
                    """
                    INSERT INTO conclusions(
                        analysis_id, symbol, generated_at, strength, direction,
                        tracking_status, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            cycle.analysis_id,
                            conclusion.assessment.symbol,
                            conclusion.generated_at.isoformat(),
                            conclusion.assessment.strength.value,
                            conclusion.assessment.direction.value
                            if conclusion.assessment.direction is not None
                            else None,
                            conclusion.tracking_status.value,
                            conclusion.model_dump_json(),
                        )
                        for conclusion in cycle.conclusions
                    ],
                )
                await database.executemany(
                    """
                    INSERT INTO evidence_bundles(
                        analysis_id, symbol, snapshot_sha256, payload_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            cycle.analysis_id,
                            bundle.symbol,
                            bundle.source_snapshot_sha256,
                            bundle.model_dump_json(),
                        )
                        for bundle in bundles
                    ],
                )
                await database.commit()
            except Exception:
                await database.rollback()
                raise

    async def latest_cycle(self) -> AnalysisCycleResult | None:
        row = await self._fetchone(
            "SELECT payload_json FROM cycles ORDER BY completed_at DESC LIMIT 1",
            (),
        )
        return AnalysisCycleResult.model_validate_json(row[0]) if row else None

    async def conclusion(
        self,
        analysis_id: str,
        symbol: str,
    ) -> SignalConclusion | None:
        row = await self._fetchone(
            """
            SELECT payload_json FROM conclusions
            WHERE analysis_id = ? AND symbol = ?
            """,
            (analysis_id, symbol),
        )
        return SignalConclusion.model_validate_json(row[0]) if row else None

    async def latest_conclusion(self, symbol: str) -> SignalConclusion | None:
        row = await self._fetchone(
            """
            SELECT payload_json FROM conclusions
            WHERE symbol = ?
            ORDER BY generated_at DESC LIMIT 1
            """,
            (symbol,),
        )
        return SignalConclusion.model_validate_json(row[0]) if row else None

    async def latest_conclusions(self) -> tuple[SignalConclusion, ...]:
        rows = await self._fetchall(
            """
            SELECT payload_json FROM (
                SELECT payload_json, symbol,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY generated_at DESC
                       ) AS row_number
                FROM conclusions
            )
            WHERE row_number = 1
            ORDER BY symbol
            """,
            (),
        )
        return tuple(SignalConclusion.model_validate_json(row[0]) for row in rows)

    async def signal_history(
        self,
        symbol: str,
        *,
        limit: int = 20,
    ) -> tuple[SignalConclusion, ...]:
        bounded_limit = max(1, min(limit, 100))
        rows = await self._fetchall(
            """
            SELECT payload_json FROM conclusions
            WHERE symbol = ?
            ORDER BY generated_at DESC LIMIT ?
            """,
            (symbol, bounded_limit),
        )
        return tuple(SignalConclusion.model_validate_json(row[0]) for row in rows)

    async def previous_strong_symbols(self) -> tuple[str, ...]:
        row = await self._fetchone(
            "SELECT analysis_id FROM cycles ORDER BY completed_at DESC LIMIT 1",
            (),
        )
        if row is None:
            return ()
        rows = await self._fetchall(
            """
            SELECT symbol FROM conclusions
            WHERE analysis_id = ? AND strength = 'STRONG'
            ORDER BY symbol
            """,
            (row[0],),
        )
        return tuple(str(item[0]) for item in rows)

    async def latest_bundle(self, symbol: str) -> EvidenceBundle | None:
        row = await self._fetchone(
            """
            SELECT evidence_bundles.payload_json
            FROM evidence_bundles
            JOIN cycles USING (analysis_id)
            WHERE evidence_bundles.symbol = ?
            ORDER BY cycles.completed_at DESC LIMIT 1
            """,
            (symbol,),
        )
        return EvidenceBundle.model_validate_json(row[0]) if row else None

    async def health_summary(self) -> dict[str, str | int | None]:
        row = await self._fetchone(
            """
            SELECT analysis_id, completed_at, payload_json
            FROM cycles ORDER BY completed_at DESC LIMIT 1
            """,
            (),
        )
        if row is None:
            return {
                "last_analysis_id": None,
                "last_completed_at": None,
                "strong_signal_count": 0,
            }
        payload = json.loads(row[2])
        return {
            "last_analysis_id": str(row[0]),
            "last_completed_at": str(row[1]),
            "strong_signal_count": int(payload.get("strong_signal_count", 0)),
        }

    async def bot_state(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM bot_state WHERE key = ?", (key,))
        return str(row[0]) if row else None

    async def set_bot_state(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute(
                """
                INSERT INTO bot_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await database.commit()

    async def delivery_exists(self, analysis_id: str, chat_id: int, kind: str) -> bool:
        row = await self._fetchone(
            """
            SELECT 1 FROM deliveries
            WHERE analysis_id = ? AND chat_id = ? AND kind = ?
            """,
            (analysis_id, chat_id, kind),
        )
        return row is not None

    async def mark_delivered(
        self,
        analysis_id: str,
        chat_id: int,
        kind: str,
        delivered_at: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute(
                """
                INSERT OR IGNORE INTO deliveries(
                    analysis_id, chat_id, kind, delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (analysis_id, chat_id, kind, delivered_at),
            )
            await database.commit()

    async def _fetchone(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> aiosqlite.Row | None:
        async with aiosqlite.connect(self._database_path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(statement, parameters)
            return await cursor.fetchone()

    async def _fetchall(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self._database_path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(statement, parameters)
            return list(await cursor.fetchall())
