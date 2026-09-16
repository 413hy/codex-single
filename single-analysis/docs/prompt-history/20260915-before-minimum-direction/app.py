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

    def interval(self):
        return self.store.state("analysis_interval", 20) * 60

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
            seconds = self.interval()
            cid = cid or f"{seconds}:{int(now) // seconds}"
            if not self.store.claim_cycle(cid):
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
                    self.store.resolve("SCREENING")
                except ModelServiceError as error:
                    self.incident("MODEL_SERVICE", error)
                    service_failed = True
                    ok = False
                except Exception as error:
                    self.incident("SCREENING", error)
                    ok = False
                normal = {c["symbol"]: c for c in selected}
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
                        if symbol in hedged:
                            context["priority_review"] = (
                                "该币已有双向仓位，需要重点判向；证据不足仍须SKIP，不强求方向，同轮不二次调用。"
                            )
                            # No trading quantities or entry PnL biases are supplied to the model.
                        self.store.execute(
                            "UPDATE signals SET evidence=? WHERE signal_id=?",
                            (encode(context), sid),
                        )
                        decision = await self.model.decide(sid, context)
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
                        self.store.queue(
                            "analysis:" + sid,
                            f"🧠 {symbol} · { {'LONG': '看多', 'SHORT': '看空', 'SKIP': '无方向'}[decision.decision] }\n{'双仓重点判向' if symbol in hedged else '普通筛选'}\n{decision.reason}\n信号有效期60秒",
                        )
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
                if ok:
                    self.store.resolve("MODEL_SERVICE")
                self.store.queue(
                    "cycle:" + cid,
                    f"📋 分析周期完成\n普通入选 {len(normal)} 币 · 双仓重点 {len(hedged)} 币\n方向分析去重后 {len(names)} 币\n状态：{'正常' if ok else '部分失败，请查看异常'}",
                )
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

        async def scheduler():
            while not stop.is_set():
                if not app.store.state("analysis_paused", False) and time.time() >= app.store.state(
                    "interval_effective_at", 0
                ):
                    await app.cycle()
                await asyncio.sleep(1)

        async def notifications():
            while not stop.is_set():
                try:
                    await bot.deliver()
                    await bot.poll()
                except Exception as error:
                    app.incident("ANALYSIS_BOT", error)
                    await asyncio.sleep(5)
                await asyncio.sleep(0.5)

        tasks = [
            asyncio.create_task(scheduler()),
            asyncio.create_task(notifications()),
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
