import argparse
import asyncio
import json
import sqlite3

from longtime import emergency
from longtime.config import DEMO_URL, Settings
from longtime.service import App, ProcessLock, logging_setup, serve
from longtime.telegram import Telegram


async def command(name, settings):
    if name == "serve":
        await serve(settings)
        return
    app = App(settings)
    try:
        if name == "preflight":
            settings.require_credentials()
            await app.exchange.sync_clock()
            print(
                json.dumps(
                    {
                        "endpoint": DEMO_URL,
                        "account": await app.exchange.account(),
                        "positions": len(await app.exchange.active_positions()),
                        "orders": len(await app.exchange.open_orders()),
                    },
                    ensure_ascii=False,
                )
            )
            if settings.telegram_token.get_secret_value():
                bot = Telegram(settings, app.store)
                try:
                    print(json.dumps({"telegram": await bot.preflight()}))
                finally:
                    await bot.close()
        elif name == "cycle":
            settings.require_credentials()
            await app.monitor.startup()
            print(
                json.dumps(
                    {"success": await app.cycle(), "trading_enabled": settings.trading_enabled}
                )
            )
        elif name == "scan":
            raise ValueError("选币已迁移至 /root/single-analysis")
        elif name == "status":
            print(
                json.dumps(
                    {
                        "cycles": app.store.rows(
                            "SELECT * FROM cycles ORDER BY started_at DESC LIMIT 3"
                        ),
                        "trades": app.store.rows(
                            "SELECT symbol,side,status,tp_price,sl_price,net_pnl FROM trades ORDER BY opened_at DESC LIMIT 20"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await app.close()


def main():
    parser = argparse.ArgumentParser(description="Independent Bybit Mainnet Demo trader")
    parser.add_argument(
        "command", choices=["config-check", "preflight", "scan", "cycle", "serve", "status"]
    )
    args = parser.parse_args()
    settings = Settings()
    if args.command == "config-check":
        print(
            json.dumps(
                {
                    "endpoint": DEMO_URL,
                    "signal_source": str(settings.signal_db),
                    "trading_enabled": settings.trading_enabled,
                    "runtime": str(settings.runtime_dir),
                }
            )
        )
        return
    logging_setup(settings.runtime_dir)
    lock = ProcessLock(settings.runtime_dir / "service.lock")
    try:
        try:
            asyncio.run(command(args.command, settings))
        except sqlite3.Error:
            emergency.queue(settings.runtime_dir, "STARTUP", "数据库启动失败")

            async def report():
                bot = Telegram(settings, None)
                try:
                    await bot.deliver_emergency()
                finally:
                    await bot.close()

            if settings.telegram_token.get_secret_value():
                asyncio.run(report())
            raise
    finally:
        lock.close()
