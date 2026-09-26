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
from analysis_core.selection import SelectionModels, final_decision
from analysis_core.signals import HedgeSniffer, SignalBus
from analysis_core.store import Store, encode, identity


class AnalysisApp:
    def __init__(self, settings, *, markets=None, model=None, selection=None, sniffer=None):
        self.settings = settings
        self.store = Store(settings.runtime_dir / "analysis.db")
        self.bus = SignalBus(settings.runtime_dir / "signals.db")
        self.markets = markets or Markets(
            include_tradingview=True,
            tradingview_extra_fields=settings.tradingview_extra_fields,
        )
        self.model = model or DirectionModel(settings, self.store)
        self.selection = selection or SelectionModels(settings, self.store)
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
            service_failed = False
            try:
                final = []
                by_symbol = {}
                try:
                    pool = await self.markets.discover()
                    exclusions = getattr(self.markets, "discovery_exclusions", [])
                    self.store.event("TV_DISCOVERY", {
                        "cycle_id": cid, "pool": pool, "excluded": exclusions,
                    })
                    pool_symbols = {item["symbol"] for item in pool}
                    for old in self.store.rows(
                        "SELECT scope FROM incidents WHERE scope LIKE 'TV_EVIDENCE:%' "
                        "AND status IN ('OPEN','RUNNING')"
                    ):
                        if old["scope"].removeprefix("TV_EVIDENCE:") not in pool_symbols:
                            self.store.resolve(old["scope"])
                    tv_bundles = []
                    for candidate in pool:
                        try:
                            reference = await self.markets.tradingview_evidence(candidate)
                            self.store.event("TV_SYMBOL_EVIDENCE", {
                                "cycle_id": cid, "symbol": candidate["symbol"],
                                "evidence": reference,
                            })
                            tv_bundles.append({
                                "symbol": candidate["symbol"],
                                "discovery": candidate,
                                "tradingview": reference,
                            })
                            self.store.resolve("TV_EVIDENCE:" + candidate["symbol"])
                        except Exception as error:
                            ok = False
                            self.incident("TV_EVIDENCE:" + candidate["symbol"], error)
                            self.store.event("TV_EVIDENCE_FAILURE", {
                                "cycle_id": cid, "symbol": candidate["symbol"],
                                "error": type(error).__name__ + ": " + str(error)[:300],
                            })
                    initial = await self.selection.choose_tv(cid, tv_bundles)
                    tv_by_symbol = {item["symbol"]: item for item in tv_bundles}
                    refinement = []
                    for item in initial:
                        bundle = tv_by_symbol[item.symbol]
                        try:
                            evidence = await self.markets.evidence(item.symbol, {
                                "symbol": item.symbol, "tradingview": bundle["tradingview"],
                                "tv_initial": item.model_dump(),
                            })
                            self.store.event("BYBIT_SYMBOL_EVIDENCE", {
                                "cycle_id": cid, "symbol": item.symbol,
                                "evidence": evidence,
                            })
                            refinement.append({
                                "symbol": item.symbol, "tv_initial": item.model_dump(),
                                "tradingview": evidence["tradingview"], "bybit": evidence,
                            })
                            self.store.resolve("BYBIT_REFINEMENT:" + item.symbol)
                        except Exception as error:
                            ok = False
                            self.incident("BYBIT_REFINEMENT:" + item.symbol, error)
                            self.store.event("BYBIT_REFINEMENT_FAILURE", {
                                "cycle_id": cid, "symbol": item.symbol,
                                "error": type(error).__name__ + ": " + str(error)[:300],
                            })
                    final = await self.selection.choose_final(cid, refinement)
                    by_symbol = {item["symbol"]: item for item in refinement}
                    self.store.resolve("NORMAL_SELECTION")
                except ModelServiceError as error:
                    self.incident("MODEL_SERVICE", error)
                    service_failed = True
                    ok = False
                except Exception as error:
                    self.incident("NORMAL_SELECTION", error)
                    ok = False

                # Read the exchange-confirmed hedge snapshot after normal refinement,
                # before publication, so overlapping signals carry the group identity.
                try:
                    hedged = self.sniffer.sniff()
                    self.store.event("HEDGE_SNIFF", {"cycle_id": cid, "symbols": hedged})
                    self.store.resolve("SNIFFER")
                except Exception as error:
                    self.incident("SNIFFER", error)
                    ok = False
                    return False

                published = set()
                for item in final:
                    symbol = item.symbol
                    sid = identity(cid, symbol)
                    context = by_symbol[symbol]["bybit"]
                    candidate = {
                        "symbol": symbol, "selection_rank": item.rank,
                        "direction_required": item.rank == 1,
                        "priority_hedge": symbol in hedged,
                        "tv_initial": by_symbol[symbol]["tv_initial"],
                        "observed_at": context["observed_at"],
                    }
                    if not self.store.signal(sid, cid, symbol, candidate):
                        continue
                    try:
                        age = time.time() - datetime.fromisoformat(context["observed_at"]).timestamp()
                        if not -5 <= age <= 600:
                            raise ValueError("Bybit方向证据在发布时已过期或时间异常")
                        decision = final_decision(item)
                        payload = self.bus.publish(
                            sid, decision, cycle_id=cid, analysis_started_at=now,
                            normal=True,
                            hedge=({k: hedged[symbol][k] for k in ("group_id", "generation")}
                                   if symbol in hedged else None),
                            observed_at=context["observed_at"],
                        )
                        self.store.signal_result(sid, "PUBLISHED", decision.decision, payload)
                        self.store.resolve("DIRECTION:" + symbol)
                        published.add(symbol)
                    except Exception as error:
                        self.store.signal_result(sid, "ERROR")
                        self.incident("DIRECTION:" + symbol, error)
                        ok = False

                if final and final[0].symbol in published:
                    self.store.resolve("PRIMARY_DIRECTION")
                else:
                    self.incident("PRIMARY_DIRECTION", RuntimeError(
                        "普通首选未能发布明确方向；等待下一轮新行情"
                    ))
                    ok = False

                for symbol in sorted(set(hedged) - {item.symbol for item in final}):
                    if service_failed:
                        break
                    sid = identity(cid, symbol)
                    if not self.store.signal(sid, cid, symbol, {
                        "symbol": symbol, "priority_hedge": True,
                    }):
                        continue
                    try:
                        # An initial TradingView candidate may already have full
                        # refinement evidence even when absent from the final 1—3.
                        context = (
                            by_symbol[symbol]["bybit"] if symbol in by_symbol
                            else await self.markets.evidence(symbol, {"symbol": symbol})
                        )
                        self.store.event("HEDGE_SYMBOL_EVIDENCE", {
                            "cycle_id": cid, "symbol": symbol, "evidence": context,
                        })
                        context["direction_required"] = False
                        context["priority_review"] = "LOCKED双仓需判向；证据冲突可SKIP。"
                        self.store.execute("UPDATE signals SET evidence=? WHERE signal_id=?",
                                           (encode(context), sid))
                        age = time.time() - datetime.fromisoformat(context["observed_at"]).timestamp()
                        if not -5 <= age <= 600:
                            raise ValueError("Bybit方向证据在发布时已过期或时间异常")
                        decision = await self.model.decide(sid, context)
                        payload = self.bus.publish(
                            sid, decision, cycle_id=cid, analysis_started_at=now,
                            normal=False,
                            hedge={k: hedged[symbol][k] for k in ("group_id", "generation")},
                            observed_at=context["observed_at"],
                        )
                        self.store.signal_result(sid, "PUBLISHED", decision.decision, payload)
                        self.store.resolve("DIRECTION:" + symbol)
                    except ModelServiceError as error:
                        self.store.signal_result(sid, "ERROR_MODEL_SERVICE")
                        self.incident("MODEL_SERVICE", error)
                        service_failed = True
                        ok = False
                    except Exception as error:
                        self.store.signal_result(sid, "ERROR")
                        self.incident("DIRECTION:" + symbol, error)
                        ok = False
                if ok:
                    self.store.resolve("MODEL_SERVICE")
                return ok
            except asyncio.CancelledError:
                ok = False
                interrupted = True
                raise
            except Exception as error:
                ok = False
                self.incident("CYCLE", error)
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
                self.store.execute(
                    "UPDATE signals SET status='INTERRUPTED' "
                    "WHERE cycle_id=? AND status='SELECTED'", (cid,),
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
        app.store.execute(
            "UPDATE signals SET status='INTERRUPTED' WHERE status='SELECTED' "
            "AND cycle_id IN (SELECT cycle_id FROM cycles WHERE status='INTERRUPTED')"
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
