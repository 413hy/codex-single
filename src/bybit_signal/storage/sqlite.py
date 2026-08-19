from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bybit_signal.domain.enums import CycleMode, CycleStatus, RetryStatus
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    AnalysisToolResult,
    EvidenceBundle,
    FailureEvent,
    FreshnessAssessment,
    RetryJob,
    SignalConclusion,
    SignalOutcome,
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
                CREATE TABLE IF NOT EXISTS runtime_state (
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
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '6')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                CREATE TABLE IF NOT EXISTS tool_calls (
                    analysis_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    symbol TEXT,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS tool_calls_analysis
                    ON tool_calls(analysis_id, completed_at);
                CREATE TABLE IF NOT EXISTS freshness_checks (
                    analysis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS threshold_events (
                    event_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    critical INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS threshold_events_symbol_time
                    ON threshold_events(symbol, observed_at DESC);
                CREATE INDEX IF NOT EXISTS threshold_events_analysis_symbol
                    ON threshold_events(analysis_id, symbol);
                CREATE TABLE IF NOT EXISTS event_deliveries (
                    event_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, chat_id, kind)
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    analysis_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS failure_events (
                    failure_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    workflow TEXT NOT NULL,
                    step TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS failure_events_analysis_time
                    ON failure_events(analysis_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS retry_jobs (
                    token TEXT PRIMARY KEY,
                    failure_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (failure_id) REFERENCES failure_events(failure_id)
                        ON DELETE CASCADE
                );
                """
            )
            await database.commit()

    async def save_failure(self, event: FailureEvent) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute(
                """
                INSERT INTO failure_events(
                    failure_id, analysis_id, occurred_at, workflow, step, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(failure_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (
                    event.failure_id,
                    event.analysis_id,
                    event.occurred_at.isoformat(),
                    event.workflow.value,
                    event.step.value,
                    event.model_dump_json(),
                ),
            )
            await database.commit()

    async def failure(self, failure_id: str) -> FailureEvent | None:
        row = await self._fetchone(
            "SELECT payload_json FROM failure_events WHERE failure_id = ?",
            (failure_id,),
        )
        return FailureEvent.model_validate_json(row[0]) if row else None

    async def create_retry_job(self, event: FailureEvent) -> RetryJob:
        await self.save_failure(event)
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("PRAGMA foreign_keys=ON")
            await database.execute("BEGIN IMMEDIATE")
            try:
                cursor = await database.execute(
                    "SELECT payload_json FROM retry_jobs WHERE failure_id = ?",
                    (event.failure_id,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    await database.rollback()
                    return RetryJob.model_validate_json(row[0])
                while True:
                    token = secrets.token_hex(8)
                    exists = await database.execute(
                        "SELECT 1 FROM retry_jobs WHERE token = ?",
                        (token,),
                    )
                    if await exists.fetchone() is None:
                        break
                job = RetryJob(
                    token=token,
                    failure_id=event.failure_id,
                    status=RetryStatus.AVAILABLE,
                    created_at=datetime.now(UTC),
                )
                await database.execute(
                    """
                    INSERT INTO retry_jobs(
                        token, failure_id, status, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job.token,
                        job.failure_id,
                        job.status.value,
                        job.created_at.isoformat(),
                        job.model_dump_json(),
                    ),
                )
                await database.commit()
                return job
            except BaseException:
                await database.rollback()
                raise

    async def retry_job(self, token: str) -> RetryJob | None:
        row = await self._fetchone(
            "SELECT payload_json FROM retry_jobs WHERE token = ?",
            (token,),
        )
        return RetryJob.model_validate_json(row[0]) if row else None

    async def claim_retry(self, token: str, requested_by: int) -> RetryJob | None:
        now = datetime.now(UTC)
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("BEGIN IMMEDIATE")
            try:
                cursor = await database.execute(
                    "SELECT payload_json FROM retry_jobs WHERE token = ?",
                    (token,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await database.rollback()
                    return None
                current = RetryJob.model_validate_json(row[0])
                if current.status not in {
                    RetryStatus.AVAILABLE,
                    RetryStatus.FAILED,
                    RetryStatus.INTERRUPTED,
                }:
                    await database.rollback()
                    return None
                claimed = current.model_copy(
                    update={
                        "status": RetryStatus.RUNNING,
                        "claimed_at": now,
                        "completed_at": None,
                        "requested_by": requested_by,
                        "result_analysis_id": None,
                        "error": None,
                    }
                )
                await database.execute(
                    """
                    UPDATE retry_jobs
                    SET status = ?, payload_json = ?
                    WHERE token = ?
                    """,
                    (claimed.status.value, claimed.model_dump_json(), token),
                )
                await database.commit()
                return claimed
            except BaseException:
                await database.rollback()
                raise

    async def finish_retry(
        self,
        token: str,
        *,
        status: RetryStatus,
        result_analysis_id: str | None = None,
        error: str | None = None,
    ) -> RetryJob | None:
        if status not in {
            RetryStatus.SUCCEEDED,
            RetryStatus.FAILED,
            RetryStatus.SUPERSEDED,
        }:
            raise ValueError("finish_retry requires a terminal status")
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("BEGIN IMMEDIATE")
            try:
                cursor = await database.execute(
                    "SELECT payload_json FROM retry_jobs WHERE token = ?",
                    (token,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await database.rollback()
                    return None
                current = RetryJob.model_validate_json(row[0])
                if current.status is not RetryStatus.RUNNING:
                    await database.rollback()
                    return current
                completed = current.model_copy(
                    update={
                        "status": status,
                        "completed_at": datetime.now(UTC),
                        "result_analysis_id": result_analysis_id,
                        "error": error[:1500] if error else None,
                    }
                )
                await database.execute(
                    """
                    UPDATE retry_jobs
                    SET status = ?, payload_json = ?
                    WHERE token = ?
                    """,
                    (completed.status.value, completed.model_dump_json(), token),
                )
                await database.commit()
                return completed
            except BaseException:
                await database.rollback()
                raise

    async def interrupt_running_retry_jobs(self) -> int:
        rows = await self._fetchall(
            "SELECT payload_json FROM retry_jobs WHERE status = ?",
            (RetryStatus.RUNNING.value,),
        )
        if not rows:
            return 0
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("BEGIN IMMEDIATE")
            try:
                count = 0
                for row in rows:
                    current = RetryJob.model_validate_json(row[0])
                    interrupted = current.model_copy(
                        update={
                            "status": RetryStatus.INTERRUPTED,
                            "completed_at": datetime.now(UTC),
                            "error": "服务重启中断了后台重试; 可安全再次点击重试。",
                        }
                    )
                    cursor = await database.execute(
                        """
                        UPDATE retry_jobs SET status = ?, payload_json = ?
                        WHERE token = ? AND status = ?
                        """,
                        (
                            interrupted.status.value,
                            interrupted.model_dump_json(),
                            interrupted.token,
                            RetryStatus.RUNNING.value,
                        ),
                    )
                    count += cursor.rowcount
                await database.commit()
                return count
            except BaseException:
                await database.rollback()
                raise

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

    async def save_tool_results(
        self,
        analysis_id: str,
        results: tuple[AnalysisToolResult, ...],
    ) -> None:
        if not results:
            return
        async with aiosqlite.connect(self._database_path) as database:
            await database.executemany(
                """
                INSERT OR REPLACE INTO tool_calls(
                    analysis_id, request_id, tool, symbol, status,
                    requested_at, completed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        analysis_id,
                        result.request_id,
                        result.tool,
                        result.symbol,
                        result.status.value,
                        result.requested_at.isoformat(),
                        result.completed_at.isoformat(),
                        result.model_dump_json(),
                    )
                    for result in results
                ],
            )
            await database.commit()

    async def tool_results(self, analysis_id: str) -> tuple[AnalysisToolResult, ...]:
        rows = await self._fetchall(
            """
            SELECT payload_json FROM tool_calls
            WHERE analysis_id = ? ORDER BY completed_at, request_id
            """,
            (analysis_id,),
        )
        return tuple(AnalysisToolResult.model_validate_json(row[0]) for row in rows)

    async def save_freshness_checks(
        self,
        analysis_id: str,
        checks: tuple[FreshnessAssessment, ...],
    ) -> None:
        if not checks:
            return
        async with aiosqlite.connect(self._database_path) as database:
            await database.executemany(
                """
                INSERT OR REPLACE INTO freshness_checks(
                    analysis_id, symbol, checked_at, decision, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        analysis_id,
                        check.symbol,
                        check.checked_at.isoformat(),
                        check.decision.value,
                        check.model_dump_json(),
                    )
                    for check in checks
                ],
            )
            await database.commit()

    async def freshness_checks(self, analysis_id: str) -> tuple[FreshnessAssessment, ...]:
        rows = await self._fetchall(
            """
            SELECT payload_json FROM freshness_checks
            WHERE analysis_id = ? ORDER BY symbol
            """,
            (analysis_id,),
        )
        return tuple(FreshnessAssessment.model_validate_json(row[0]) for row in rows)

    async def record_threshold_event(
        self,
        *,
        event_id: str,
        analysis_id: str,
        symbol: str,
        family_id: str,
        observed_at: str,
        critical: bool,
        payload: Mapping[str, object],
    ) -> bool:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("BEGIN IMMEDIATE")
            try:
                existing = await database.execute(
                    """
                    SELECT 1 FROM threshold_events
                    WHERE analysis_id = ? AND symbol = ? LIMIT 1
                    """,
                    (analysis_id, symbol),
                )
                if await existing.fetchone() is not None:
                    await database.rollback()
                    return False
                await database.execute(
                    """
                    INSERT INTO threshold_events(
                        event_id, analysis_id, symbol, family_id,
                        observed_at, critical, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        analysis_id,
                        symbol,
                        family_id,
                        observed_at,
                        int(critical),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                await database.commit()
                return True
            except BaseException:
                await database.rollback()
                raise

    async def threshold_events(
        self,
        *,
        symbol: str | None = None,
        limit: int = 50,
    ) -> tuple[dict[str, object], ...]:
        bounded = max(1, min(limit, 200))
        if symbol is None:
            rows = await self._fetchall(
                """
                SELECT payload_json FROM threshold_events
                ORDER BY observed_at DESC LIMIT ?
                """,
                (bounded,),
            )
        else:
            rows = await self._fetchall(
                """
                SELECT payload_json FROM threshold_events
                WHERE symbol = ? ORDER BY observed_at DESC LIMIT ?
                """,
                (symbol, bounded),
            )
        return tuple(json.loads(str(row[0])) for row in rows)

    async def triggered_directive_keys(
        self,
        analysis_ids: tuple[str, ...],
    ) -> frozenset[tuple[str, str, str]]:
        if not analysis_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in analysis_ids)
        rows = await self._fetchall(
            f"""
            SELECT DISTINCT analysis_id, symbol, family_id
            FROM threshold_events
            WHERE analysis_id IN ({placeholders})
            """,
            tuple(analysis_ids),
        )
        return frozenset((str(row[0]), str(row[1]), str(row[2])) for row in rows)

    async def triggered_analysis_symbol_keys(
        self,
        analysis_ids: tuple[str, ...],
    ) -> frozenset[tuple[str, str]]:
        if not analysis_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in analysis_ids)
        rows = await self._fetchall(
            f"""
            SELECT DISTINCT analysis_id, symbol
            FROM threshold_events
            WHERE analysis_id IN ({placeholders})
            """,
            tuple(analysis_ids),
        )
        return frozenset((str(row[0]), str(row[1])) for row in rows)

    async def event_delivery_exists(self, event_id: str, chat_id: int, kind: str) -> bool:
        row = await self._fetchone(
            """
            SELECT 1 FROM event_deliveries
            WHERE event_id = ? AND chat_id = ? AND kind = ?
            """,
            (event_id, chat_id, kind),
        )
        return row is not None

    async def mark_event_delivered(
        self,
        event_id: str,
        chat_id: int,
        kind: str,
        delivered_at: str,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute(
                """
                INSERT OR IGNORE INTO event_deliveries(
                    event_id, chat_id, kind, delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (event_id, chat_id, kind, delivered_at),
            )
            await database.commit()

    async def save_outcomes(self, outcomes: tuple[SignalOutcome, ...]) -> None:
        if not outcomes:
            return
        async with aiosqlite.connect(self._database_path) as database:
            await database.executemany(
                """
                INSERT OR REPLACE INTO outcomes(
                    analysis_id, symbol, evaluated_at, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        outcome.analysis_id,
                        outcome.symbol,
                        outcome.evaluated_at.isoformat(),
                        outcome.model_dump_json(),
                    )
                    for outcome in outcomes
                ],
            )
            await database.commit()

    async def signal_outcomes(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
    ) -> tuple[SignalOutcome, ...]:
        bounded = max(1, min(limit, 500))
        if symbol is None:
            rows = await self._fetchall(
                """
                SELECT payload_json FROM outcomes
                ORDER BY evaluated_at DESC LIMIT ?
                """,
                (bounded,),
            )
        else:
            rows = await self._fetchall(
                """
                SELECT payload_json FROM outcomes
                WHERE symbol = ? ORDER BY evaluated_at DESC LIMIT ?
                """,
                (symbol, bounded),
            )
        return tuple(SignalOutcome.model_validate_json(row[0]) for row in rows)

    async def latest_cycle(self) -> AnalysisCycleResult | None:
        row = await self._fetchone(
            "SELECT payload_json FROM cycles ORDER BY completed_at DESC LIMIT 1",
            (),
        )
        return _cycle_from_payload(row[0]) if row else None

    async def cycle(self, analysis_id: str) -> AnalysisCycleResult | None:
        row = await self._fetchone(
            "SELECT payload_json FROM cycles WHERE analysis_id = ?",
            (analysis_id,),
        )
        return _cycle_from_payload(row[0]) if row else None

    async def evidence_bundles(
        self,
        analysis_id: str,
        symbols: tuple[str, ...] = (),
    ) -> tuple[EvidenceBundle, ...]:
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            rows = await self._fetchall(
                f"""
                SELECT payload_json FROM evidence_bundles
                WHERE analysis_id = ? AND symbol IN ({placeholders})
                ORDER BY symbol
                """,
                (analysis_id, *symbols),
            )
        else:
            rows = await self._fetchall(
                """
                SELECT payload_json FROM evidence_bundles
                WHERE analysis_id = ? ORDER BY symbol
                """,
                (analysis_id,),
            )
        return tuple(EvidenceBundle.model_validate_json(row[0]) for row in rows)

    async def commit_monitoring_version(
        self,
        cycle: AnalysisCycleResult,
        *,
        authoritative_scheduled_analysis_id: str,
    ) -> bool:
        """CAS monitoring directives into an existing signal cycle.

        Scheduled results may update only while they are the latest successful scheduled
        cycle. Emergency results additionally must be the latest emergency result for their
        symbol, so late reviews cannot overwrite a newer scheduled or urgent version.
        """

        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("BEGIN IMMEDIATE")
            try:
                scheduled_rows = await database.execute_fetchall(
                    """
                    SELECT payload_json FROM cycles
                    WHERE analysis_id NOT LIKE 'urgent_%'
                    ORDER BY completed_at DESC
                    """
                )
                latest_scheduled = next(
                    (
                        _cycle_from_payload(row[0])
                        for row in scheduled_rows
                        if _cycle_from_payload(row[0]).status is CycleStatus.SUCCESS
                    ),
                    None,
                )
                if (
                    latest_scheduled is None
                    or latest_scheduled.analysis_id != authoritative_scheduled_analysis_id
                ):
                    await database.rollback()
                    return False
                if cycle.mode is CycleMode.SCHEDULED:
                    if cycle.analysis_id != authoritative_scheduled_analysis_id:
                        await database.rollback()
                        return False
                else:
                    if len(cycle.conclusions) != 1:
                        await database.rollback()
                        return False
                    symbol = cycle.conclusions[0].assessment.symbol
                    cursor = await database.execute(
                        """
                        SELECT conclusions.analysis_id
                        FROM conclusions JOIN cycles USING (analysis_id)
                        WHERE conclusions.symbol = ?
                          AND cycles.analysis_id LIKE 'urgent_%'
                        ORDER BY cycles.completed_at DESC LIMIT 1
                        """,
                        (symbol,),
                    )
                    row = await cursor.fetchone()
                    if row is None or str(row[0]) != cycle.analysis_id:
                        await database.rollback()
                        return False
                await database.execute(
                    """
                    UPDATE cycles
                    SET context_sha256 = ?, payload_json = ?
                    WHERE analysis_id = ?
                    """,
                    (
                        cycle.context_sha256,
                        cycle.model_dump_json(),
                        cycle.analysis_id,
                    ),
                )
                for conclusion in cycle.conclusions:
                    await database.execute(
                        """
                        UPDATE conclusions
                        SET payload_json = ?
                        WHERE analysis_id = ? AND symbol = ?
                        """,
                        (
                            conclusion.model_dump_json(),
                            cycle.analysis_id,
                            conclusion.assessment.symbol,
                        ),
                    )
                await database.commit()
                return True
            except BaseException:
                await database.rollback()
                raise

    async def latest_scheduled_cycle(
        self,
        *,
        successful_only: bool = False,
    ) -> AnalysisCycleResult | None:
        rows = await self._fetchall(
            """
            SELECT payload_json FROM cycles
            WHERE analysis_id NOT LIKE 'urgent_%'
            ORDER BY completed_at DESC
            """,
            (),
        )
        for row in rows:
            cycle = _cycle_from_payload(row[0])
            if not successful_only or cycle.status is CycleStatus.SUCCESS:
                return cycle
        return None

    async def recent_scheduled_cycles(
        self, *, limit: int = 20, successful_only: bool = True
    ) -> tuple[AnalysisCycleResult, ...]:
        rows = await self._fetchall(
            """
            SELECT payload_json FROM cycles
            WHERE analysis_id NOT LIKE 'urgent_%'
            ORDER BY completed_at DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        )
        cycles = tuple(_cycle_from_payload(row[0]) for row in rows)
        return tuple(
            cycle for cycle in cycles if not successful_only or cycle.status is CycleStatus.SUCCESS
        )

    async def outcome_exists(self, analysis_id: str, symbol: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM outcomes WHERE analysis_id = ? AND symbol = ?",
            (analysis_id, symbol),
        )
        return row is not None

    async def latest_emergency_cycle(self) -> AnalysisCycleResult | None:
        row = await self._fetchone(
            """
            SELECT payload_json FROM cycles
            WHERE analysis_id LIKE 'urgent_%'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (),
        )
        return _cycle_from_payload(row[0]) if row else None

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
        conclusions = await self.previous_scheduled_selected_conclusions()
        return tuple(conclusion.assessment.symbol for conclusion in conclusions)

    async def previous_scheduled_strong_conclusions(
        self,
    ) -> tuple[SignalConclusion, ...]:
        return await self.previous_scheduled_selected_conclusions()

    async def previous_scheduled_selected_conclusions(
        self,
    ) -> tuple[SignalConclusion, ...]:
        cycle = await self.latest_scheduled_cycle(successful_only=True)
        if cycle is None:
            return ()
        selected = sorted(
            (
                conclusion
                for conclusion in cycle.conclusions
                if conclusion.assessment.selection_rank is not None
            ),
            key=lambda conclusion: conclusion.assessment.selection_rank or 0,
        )
        if selected:
            return tuple(selected)
        return tuple(
            conclusion
            for conclusion in cycle.conclusions
            if conclusion.assessment.strength.value == "STRONG"
        )

    async def selected_scheduled_conclusion(
        self,
        symbol: str,
    ) -> SignalConclusion | None:
        conclusions = await self.previous_scheduled_selected_conclusions()
        return next(
            (conclusion for conclusion in conclusions if conclusion.assessment.symbol == symbol),
            None,
        )

    async def active_monitoring_conclusions(self) -> tuple[SignalConclusion, ...]:
        cycle = await self.latest_scheduled_cycle(successful_only=True)
        if cycle is None:
            return ()
        active: list[SignalConclusion] = []
        for scheduled in await self.previous_scheduled_selected_conclusions():
            emergency = await self._latest_emergency_conclusion(
                scheduled.assessment.symbol,
                after=cycle.completed_at.isoformat(),
            )
            if emergency is not None:
                if emergency.assessment.monitoring_directives:
                    active.append(emergency)
                continue
            if scheduled.assessment.monitoring_directives:
                active.append(scheduled)
        return tuple(active)

    async def _latest_emergency_conclusion(
        self,
        symbol: str,
        *,
        after: str,
    ) -> SignalConclusion | None:
        row = await self._fetchone(
            """
            SELECT conclusions.payload_json
            FROM conclusions
            JOIN cycles USING (analysis_id)
            WHERE conclusions.symbol = ?
              AND cycles.analysis_id LIKE 'urgent_%'
              AND cycles.completed_at > ?
            ORDER BY cycles.completed_at DESC LIMIT 1
            """,
            (symbol, after),
        )
        return SignalConclusion.model_validate_json(row[0]) if row else None

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
        latest = await self.latest_scheduled_cycle()
        successful = await self.latest_scheduled_cycle(successful_only=True)
        emergency = await self.latest_emergency_cycle()
        if latest is None:
            return {
                "last_analysis_id": None,
                "last_completed_at": None,
                "strong_signal_count": 0,
                "selected_signal_count": 0,
                "model_status": "NO_DATA",
                "last_successful_analysis_id": None,
                "last_emergency_analysis_id": None,
            }
        return {
            "last_analysis_id": latest.analysis_id,
            "last_completed_at": latest.completed_at.isoformat(),
            "strong_signal_count": latest.strong_signal_count,
            "selected_signal_count": latest.selected_signal_count,
            "model_status": latest.status.value,
            "last_successful_analysis_id": (
                successful.analysis_id if successful is not None else None
            ),
            "last_emergency_analysis_id": (
                emergency.analysis_id if emergency is not None else None
            ),
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

    async def runtime_state(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM runtime_state WHERE key = ?", (key,))
        return str(row[0]) if row else None

    async def set_runtime_state(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute(
                """
                INSERT INTO runtime_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await database.commit()

    async def delete_runtime_state(self, key: str) -> None:
        async with aiosqlite.connect(self._database_path) as database:
            await database.execute("DELETE FROM runtime_state WHERE key = ?", (key,))
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


def _cycle_from_payload(payload: str) -> AnalysisCycleResult:
    document = json.loads(payload)
    analysis_id = str(document.get("analysis_id") or "")
    if "mode" not in document:
        document["mode"] = (
            CycleMode.EMERGENCY.value
            if analysis_id.startswith("urgent_")
            else CycleMode.SCHEDULED.value
        )
    if "status" not in document:
        failures = document.get("diagnostics", {}).get("failures", {})
        document["status"] = (
            CycleStatus.FAILED.value
            if isinstance(failures, dict) and "CODEX" in failures
            else CycleStatus.SUCCESS.value
        )
    return AnalysisCycleResult.model_validate(document)
