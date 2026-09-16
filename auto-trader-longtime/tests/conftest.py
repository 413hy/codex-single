import copy
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from longtime.config import Settings
from longtime.execution import Executor
from longtime.model import Decision
from longtime.risk import Instrument
from longtime.store import Store
from longtime.vendor.models import Candle


class FakeExchange:
    def __init__(self):
        self.balance = D(100)
        self.position_rows = []
        self.orders = {}
        self.submissions = []
        self.cancels = []
        self.fail_kind = None
        self.ambiguous_entry = False
        self.leverage = D(5)
        self.idx = 0
        self.account_calls = 0
        self.pnl_rows = []
        self.exit_execs = {}
        self.entry_at = time.time()

    async def sync_clock(self):
        pass

    async def close(self):
        pass

    async def account(self):
        self.account_calls += 1
        return {
            "available": str(self.balance),
            "wallet_balance": "100",
            "margin_mode": "REGULAR_MARGIN",
        }

    async def active_positions(self):
        return copy.deepcopy(self.position_rows)

    async def positions(self, symbol=None):
        return copy.deepcopy([p for p in self.position_rows if not symbol or p["symbol"] == symbol])

    async def open_orders(self, symbol=None):
        return [
            copy.deepcopy(o)
            for o in self.orders.values()
            if o["orderStatus"] in {"New", "Untriggered", "PartiallyFilled"}
            and (not symbol or o["symbol"] == symbol)
        ]

    async def instrument(self, symbol):
        return Instrument(
            symbol,
            D(".01"),
            D(".001"),
            D(".001"),
            D(5),
            D(10000),
            D(10000),
            self.leverage,
            D(".01"),
        )

    async def last_price(self, symbol):
        return D(100)

    async def fees(self, symbol):
        return D(".00055"), D(".0002")

    async def quote(self, symbol):
        return D("99.99"), D("100")

    async def configure_symbol(self, symbol, side, leverage):
        self.leverage = leverage
        return self.idx

    async def order(self, symbol, link):
        return copy.deepcopy(self.orders.get(link))

    async def submit(self, payload):
        self.submissions.append(copy.deepcopy(payload))
        kind = (
            "ENTRY"
            if not payload["reduceOnly"]
            else ("TP" if payload["orderType"] == "Limit" else "SL")
        )
        if self.fail_kind == kind:
            raise RuntimeError("injected create failure")
        oid = "oid-" + payload["orderLinkId"]
        row = {
            **payload,
            "orderId": oid,
            "createdTime": str(int(time.time() * 1000)),
            "updatedTime": str(int(time.time() * 1000)),
            "cumExecQty": "0",
        }
        if kind == "ENTRY":
            row.update(orderStatus="Filled", avgPrice="100", cumExecQty=payload["qty"])
            self.position_rows.append(
                {
                    "symbol": payload["symbol"],
                    "side": payload["side"],
                    "positionIdx": payload["positionIdx"],
                    "size": payload["qty"],
                    "avgPrice": "100",
                    "leverage": str(self.leverage),
                    "takeProfit": "0",
                    "stopLoss": "0",
                }
            )
            self.balance -= D(payload["qty"]) * 100 / self.leverage
        else:
            row.update(
                orderStatus="New" if kind == "TP" else "Untriggered", leavesQty=payload["qty"]
            )
        self.orders[payload["orderLinkId"]] = row
        if kind == "ENTRY" and self.ambiguous_entry:
            raise RuntimeError("response lost after exchange accepted")
        return oid

    async def cancel(self, symbol, oid):
        self.cancels.append(oid)
        for o in self.orders.values():
            if o["orderId"] == oid:
                o["orderStatus"] = "Cancelled"

    async def executions(self, symbol, oid, at=None):
        if oid in self.exit_execs:
            return self.exit_execs[oid]
        for o in self.orders.values():
            if o["orderId"] == oid and not o["reduceOnly"]:
                return [
                    {
                        "orderId": oid,
                        "execType": "Trade",
                        "execQty": o["qty"],
                        "execFee": str(D(o["qty"]) * 100 * D(".00055")),
                        "execTime": str(int(self.entry_at * 1000)),
                    }
                ]
        return []

    async def history_window(self, path, symbol, start, end):
        return self.pnl_rows if path.endswith("closed-pnl") else list(self.orders.values())

    def close_at_tp(self, trade, pnl="0.497", partial=False):
        row = next(o for o in self.orders.values() if o["orderId"] == trade["tp_order_id"])
        qty = D(trade["qty"]) / (2 if partial else 1)
        row.update(orderStatus="Filled", cumExecQty=str(qty), avgPrice=trade["tp_price"])
        fee = qty * D(trade["tp_price"]) * D(".0002")
        now = int((self.entry_at + 60) * 1000)
        row["updatedTime"] = str(now)
        self.exit_execs[row["orderId"]] = [
            {
                "orderId": row["orderId"],
                "execType": "Trade",
                "execQty": str(qty),
                "execFee": str(fee),
                "execTime": str(now),
            }
        ]
        self.pnl_rows = [
            {
                "orderId": row["orderId"],
                "symbol": trade["symbol"],
                "side": row["side"],
                "execType": "Trade",
                "closedSize": str(qty),
                "avgEntryPrice": trade["entry_price"],
                "avgExitPrice": trade["tp_price"],
                "closedPnl": pnl,
                "updatedTime": str(now),
            }
        ]
        self.position_rows = []


class FakeMarkets:
    async def reachability(self, symbol):
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        return tuple(
            Candle(
                symbol=symbol,
                timeframe="1m",
                open_time=now - timedelta(minutes=1440 - i),
                close_time=now - timedelta(minutes=1439 - i),
                open=D(120 if i % 2 else 80),
                close=D(120 if i % 2 else 80),
                high=D(121 if i % 2 else 81),
                low=D(119 if i % 2 else 79),
                volume=D(10000),
                turnover=D(1000000),
                completed=True,
                source="BYBIT",
            )
            for i in range(1440)
        )


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    import asyncio

    original = asyncio.sleep

    async def fast_sleep(_):
        await original(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    settings = Settings(_env_file=None, trading_enabled=True, runtime_dir=tmp_path)
    store = Store(tmp_path / "trader.db")
    ex, market = FakeExchange(), FakeMarkets()
    engine = Executor(settings, store, ex, market)
    store.signal("sig", "123", "TESTUSDT", {})
    decision = Decision(symbol="TESTUSDT", decision="LONG", reason="短中期方向合理")
    return engine, store, ex, market, decision
