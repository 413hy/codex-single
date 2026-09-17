"""Autonomous analysis producer. No trading credentials or exchange mutations."""

import argparse
import asyncio
import fcntl
import json
import logging
import signal
import time
from datetime import datetime

from analysis_core.config import Settings
from analysis_core.market import Markets
from analysis_core.model import DirectionModel, ModelServiceError
from analysis_core.notifications import cycle_keyboard, cycle_notice
from analysis_core.scheduling import claim_due, reset_schedule
from analysis_core.screening import Screening
from analysis_core.signals import HedgeSniffer, SignalBus
from analysis_core.store import Store, encode, identity


class AnalysisApp:
    def __init__(self, settings, *, markets=None, model=None, screening=None, sniffer=None):
        self.settings = settings
        self.store = Store(settings.runtime_dir / "analysis.db")
        self.bus = SignalBus(settings.runtime_dir / "signals.db")
        self.markets = markets or Markets()
        self.model = model or DirectionModel(settings, self.store)
        self.screening = screening or Screening(settings, self.store, runner=self.model)
        self.sniffer = sniffer or HedgeSniffer(settings.hedge_db)
        self.lock = asyncio.Lock()

    def incident(self, scope, error):
        self.store.incident(
            scope,
            "analysis",
            {},
            str(error)[:500]
            if isinstance(error, (ValueError, RuntimeError))
            else type(error).__name__,
        )

    async def cycle(self, cid=None):
        if self.lock.locked():
            return False
        async with self.lock:
            now = time.time()
            if cid is None:
                cid = claim_due(self.store, now)
                if cid is None:
                    return True
            elif not self.store.claim_cycle(cid):
                return True
            ok = True
            interrupted = False
            try:
                # Snapshot required before scanning; unavailable snapshot cannot silently
                # remove pending locked positions from the mandatory direction list.
                hedged = self.sniffer.sniff()
                self.store.event("HEDGE_SNIFF", dict(cycle_id=cid, symbols=hedged))
                self.store.resolve("SNIFFER")
                selected = []
                service_failed = False
                try:
                    scan = await self.markets.scan(set())
                    self.store.event("SCAN", dict(cycle_id=cid, scan=scan.model_dump(mode="json")))
                    if scan.failures:
                        self.incident(
                            "SCAN_DATA", RuntimeError("部分候选行情采集失败；受影响币跳过")
                        )
                    else:
                        self.store.resolve("SCAN_DATA")
                    selected = await self.screening.select(cid, scan.candidates)
                    if not 1 <= len(selected) <= 3 or len({c["symbol"] for c in selected}) != len(
                        selected
                    ):
                        raise ValueError("普通筛选必须返回1—3个不同候选，不能正常返回空名单")
                    self.store.resolve("SCREENING")
                except ModelServiceError as error:
                    self.incident("MODEL_SERVICE", error)
                    service_failed = True
                    ok = False
                except Exception as error:
                    self.incident("SCREENING", error)
                    selected = []
                    ok = False
                normal = {c["symbol"]: c for c in selected}
                primary = selected[0]["symbol"] if selected else None
                primary_published = False
                # Extra hedge names do not count against the normal 0-3 selection slots.
                names = sorted(hedged) + [s for s in normal if s not in hedged]
                for symbol in names:
                    if service_failed:
                        break
                    sid = identity(cid, symbol)
                    candidate = dict(
                        normal.get(symbol, {"symbol": symbol}), priority_hedge=symbol in hedged
                    )
                    if not self.store.signal(sid, cid, symbol, candidate):
                        continue
                    try:
                        context = await self.markets.evidence(symbol, candidate)
                        context["direction_required"] = symbol == primary
                        if symbol in normal:
                            context["trend_ranking"] = {
                                k: normal[symbol][k]
                                for k in (
                                    "selection_rank",
                                    "screening_direction",
                                    "relative_confidence",
                                    "ranking_rationale",
                                )
                                if k in normal[symbol]
                            }
                        if symbol in hedged:
                            context["priority_review"] = (
                                "该币已有双向仓位，需要重点判向；同轮只分析一次。"
                                "若同时为普通首选，遵守direction_required；额外双仓证据不足可SKIP。"
                            )
                            # No trading quantities or entry PnL biases are supplied to the model.
                        self.store.execute(
                            "UPDATE signals SET evidence=? WHERE signal_id=?",
                            (encode(context), sid),
                        )
                        decision = await self.model.decide(sid, context)
                        if symbol == primary and decision.decision == "SKIP":
                            raise ValueError(
                                "普通首选未给出明确方向，本轮分析失败，不重试或伪造方向"
                            )
                        age = (
                            time.time() - datetime.fromisoformat(context["observed_at"]).timestamp()
                        )
                        if not -5 <= age <= self.settings.model_timeout + 30:
                            raise ValueError("方向分析行情已过期，禁止发布")
                        payload = self.bus.publish(
                            sid,
                            decision,
                            normal=symbol in normal,
                            hedge=(
                                {k: hedged[symbol][k] for k in ("group_id", "generation")}
                                if symbol in hedged
                                else None
                            ),
                            observed_at=context["observed_at"],
                        )
                        self.store.signal_result(sid, "PUBLISHED", decision.decision, payload)
                        if symbol == primary:
                            primary_published = True
                        self.store.resolve("DIRECTION:" + symbol)
                    except ModelServiceError as error:
                        self.store.signal_result(sid, "ERROR_MODEL_SERVICE")
                        self.incident("MODEL_SERVICE", error)
                        service_failed = True
                        ok = False
                    except ValueError as error:
                        if str(error).startswith("SKIP_INSUFFICIENT_WEEK_HISTORY"):
                            self.store.signal_result(sid, "SKIP_INSUFFICIENT_WEEK_HISTORY")
                        else:
                            self.store.signal_result(sid, "ERROR")
                            self.incident("DIRECTION:" + symbol, error)
                            ok = False
                    except Exception as error:
                        self.store.signal_result(sid, "ERROR")
                        self.incident("DIRECTION:" + symbol, error)
                        ok = False
                if primary is not None and not primary_published:
                    self.incident(
                        "PRIMARY_DIRECTION",
                        RuntimeError(
                            "普通首选未能发布有效多空方向；检查行情与模型错误，等待下一轮"
                        ),
                    )
                    ok = False
                elif primary_published:
                    self.store.resolve("PRIMARY_DIRECTION")
                if ok:
                    self.store.resolve("MODEL_SERVICE")
                return ok
            except asyncio.CancelledError:
                ok = False
                interrupted = True
                raise
            except Exception as error:
                ok = False
                self.incident("SNIFFER", error)
                return False
            finally:
                self.store.execute(
                    "UPDATE cycles SET status=?,completed_at=? WHERE cycle_id=?",
                    (
                        "INTERRUPTED" if interrupted else "SUCCESS" if ok else "PARTIAL_ERROR",
                        time.time(),
                        cid,
                    ),
                )
                if cid.startswith("scheduled:"):
                    reset_schedule(self.store, time.time(), only_overdue=True)
                self.store.queue("cycle:" + cid, cycle_notice(self.store, cid), cycle_keyboard(self.store, cid))
                self.store.set("analysis_heartbeat", time.time())

    async def close(self):
        await self.markets.close()


async def run(settings, mode):
    from analysis_core.bot import AnalysisBot

    app = AnalysisApp(settings)
    bot = AnalysisBot(settings, app.store)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    tasks = []
    try:
        if mode == "cycle":
            result = await app.cycle("manual:" + identity(time.time_ns()))
            print(json.dumps({"success": result}))
            return
        await bot.preflight()
        app.store.execute(
            "UPDATE cycles SET status='INTERRUPTED',completed_at=? WHERE status='RUNNING'",
            (time.time(),),
        )

        # Startup skips downtime slots and migrates old start-relative schedules.
        reset_schedule(app.store, time.time())

        async def scheduler():
            while not stop.is_set():
                await app.cycle()
                await asyncio.sleep(1)

        async def notifications():
            while not stop.is_set():
                delay = 0.5
                try:
                    await bot.deliver()
                except Exception as error:
                    delay = bot.communication_failure("TELEGRAM_DELIVERY", error)
                await asyncio.sleep(delay)

        async def commands():
            while not stop.is_set():
                delay = 0.5
                try:
                    await bot.poll()
                except Exception as error:
                    delay = bot.communication_failure("TELEGRAM_POLL", error)
                await asyncio.sleep(delay)

        tasks = [
            asyncio.create_task(scheduler()),
            asyncio.create_task(notifications()),
            asyncio.create_task(commands()),
            asyncio.create_task(stop.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            error = None if t.cancelled() else t.exception()
            if error is not None:
                raise error
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.close()
        await app.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "cycle", "check"])
    args = parser.parse_args()
    settings = Settings()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.mode == "check":
        print(
            json.dumps(
                {
                    "runtime": str(settings.runtime_dir),
                    "hedge_db": str(settings.hedge_db),
                    "bot_configured": bool(
                        settings.telegram_token.get_secret_value()
                        and settings.telegram_chat_id
                        and settings.telegram_user_id
                    ),
                }
            )
        )
        return
    with (settings.runtime_dir / "analysis.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(settings, args.mode))


if __name__ == "__main__":
    main()
