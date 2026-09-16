from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import logging.handlers
import signal
import time

from longtime.exchange import Exchange
from longtime.hedge import HedgeExecutor as Executor
from longtime.market import Markets
from longtime.monitor import Monitor
from longtime.signal_consumer import SignalConsumer
from longtime.store import Store
from longtime.telegram import Telegram

log = logging.getLogger(__name__)


def cycle_id(now=None, seconds=1200):
    bucket = int(time.time() if now is None else now) // seconds
    return str(bucket) if seconds == 1200 else f"interval:{seconds}:{bucket}"


class App:
    def __init__(self, settings, *, store=None, exchange=None, markets=None):
        self.settings = settings
        self.store = store or Store(settings.runtime_dir / "trader.db")
        self.exchange = exchange or Exchange(settings)
        self.markets = markets or Markets()
        self.executor = Executor(settings, self.store, self.exchange, self.markets)
        self.monitor = Monitor(self.executor)
        self.cycle_lock = asyncio.Lock()
        self.consumer = SignalConsumer(self)

    async def cycle(self, *args, **kwargs):
        # Retained CLI entrypoint consumes available publications; never runs analysis.
        await self.consumer.tick()
        return True

    async def retry(self, incident, callback_id):
        payload, kind = json.loads(incident["payload"]), incident["kind"]
        self.store.event(
            "MANUAL_RETRY",
            {"incident_id": incident["incident_id"], "callback_id": callback_id, "kind": kind},
        )
        if kind == "protection":
            async with self.executor.lock:
                return await self.executor.protect(payload["trade_id"], manual_kind=payload["kind"])
        if kind == "reconcile":
            async with self.executor.lock:
                return await self.executor.reconcile_entry(payload["trade_id"])
        if kind in ("candidate", "cycle"):
            if payload.get("symbol"):
                rows = self.store.rows(
                    "SELECT trade_id FROM trades WHERE symbol=? AND status='INTENT'",
                    (payload["symbol"],),
                )
                if rows:
                    async with self.executor.lock:
                        return await self.executor.reconcile_entry(rows[0]["trade_id"])
            self.store.resolve(incident["scope"])
            return True  # Old analysis failures moved to the analysis service; no replay.
        if kind == "delivery":
            self.store.execute(
                "UPDATE outbox SET status='PENDING',attempts=0,next_attempt=0 WHERE status='FAILED'"
            )
            return True
        if incident["scope"].startswith("EXTERNAL:"):
            return await self.monitor.check_external(payload["symbol"])
        await self.monitor.tick()
        return not bool(
            self.store.rows(
                "SELECT incident_id FROM incidents WHERE incident_id=? AND status IN ('OPEN','RUNNING')",
                (incident["incident_id"],),
            )
        )

    async def close(self):
        await self.markets.close()
        await self.exchange.close()


async def scheduled_cycles(app, stop):
    # Poll local publication feed; interval belongs exclusively to single-analysis.
    await periodic(
        stop, 0.5, app.consumer.tick, lambda e: app.executor.alert("SIGNAL_FEED", "monitor", {}, e)
    )


async def periodic(stop, seconds, action, on_error):
    while not stop.is_set():
        start = time.monotonic()
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            on_error(error)
        try:
            await asyncio.wait_for(stop.wait(), max(0.1, seconds - (time.monotonic() - start)))
        except TimeoutError:
            pass


async def serve(settings):
    settings.require_credentials()
    settings.require_telegram()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    app = App(settings)
    bot = Telegram(settings, app.store)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    try:
        await bot.preflight()
        try:
            await app.monitor.startup()
        except Exception as error:
            app.executor.alert("STARTUP", "monitor", {}, error)
            await bot.deliver()
            raise

        tasks = [
            asyncio.create_task(scheduled_cycles(app, stop), name="scheduler"),
            asyncio.create_task(
                periodic(
                    stop,
                    5,
                    app.monitor.tick,
                    lambda e: app.executor.alert("MONITOR", "monitor", {}, e),
                ),
                name="monitor",
            ),
            asyncio.create_task(
                periodic(
                    stop,
                    1,
                    bot.deliver,
                    lambda e: app.executor.alert("DATABASE_OUTBOX", "monitor", {}, e),
                ),
                name="outbox",
            ),
            asyncio.create_task(
                periodic(
                    stop,
                    1,
                    lambda: bot.poll(app.retry),
                    lambda e: app.executor.alert("TELEGRAM_POLL", "monitor", {}, e),
                ),
                name="telegram",
            ),
            asyncio.create_task(stop.wait(), name="stop"),
        ]
        log.info("Bybit Demo service ready; trading_enabled=%s", settings.trading_enabled)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.get_name() != "stop":
                task_error = task.exception() or RuntimeError("Background task exited unexpectedly")
                app.executor.alert("SCHEDULER", "cycle", {}, task_error)
                await bot.deliver()
                raise RuntimeError("Service background task failed") from task_error
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.close()
        await app.close()


def logging_setup(runtime_dir):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        runtime_dir / "service.log", maxBytes=10_000_000, backupCount=10
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, logging.StreamHandler()],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class ProcessLock:
    def __init__(self, path):
        self.file = path.open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Another trader process already owns this runtime") from None

    def close(self):
        self.file.close()
