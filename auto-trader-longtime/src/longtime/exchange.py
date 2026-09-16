from __future__ import annotations

import time
from decimal import Decimal as D

from longtime.risk import Instrument, number
from longtime.transport import BybitAPIError, DemoTransport

LIVE = {"New", "PartiallyFilled", "Untriggered", "Triggered"}
TERMINAL = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}


class Exchange(DemoTransport):
    async def rows(self, path, params):
        result, seen = [], set()
        for _ in range(100):
            doc = await self._private("GET", path, params)
            body = doc["result"]
            if not isinstance(body.get("list"), list):
                raise ValueError("Missing exchange list: " + path)
            result.extend(body["list"])
            cursor = body.get("nextPageCursor")
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError("Repeated exchange cursor: " + path)
            seen.add(cursor)
            params = {**params, "cursor": cursor}
        raise ValueError("Exchange pagination incomplete: " + path)

    async def positions(self, symbol=None):
        params = {"category": "linear", "limit": 200}
        params.update({"symbol": symbol} if symbol else {"settleCoin": "USDT"})
        return await self.rows("/v5/position/list", params)

    async def active_positions(self):
        return [p for p in await self.positions() if number(p["size"]) > 0]

    async def account(self):
        account = (await self._private("GET", "/v5/account/info"))["result"]
        wallet = (
            await self._private("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        )["result"]["list"][0]
        if account.get("marginMode") != "REGULAR_MARGIN":
            raise ValueError("Account must use Cross Margin (REGULAR_MARGIN)")
        available = number(wallet["totalAvailableBalance"])
        balance = number(wallet["totalWalletBalance"])
        im_rate = number(wallet.get("accountIMRate") or 0)
        mm_rate = number(wallet.get("accountMMRate") or 0)
        if available < 0 or im_rate >= 1 or mm_rate >= 1:
            raise ValueError("Abnormal account margin status")
        return {
            "available": str(available),
            "wallet_balance": str(balance),
            "margin_mode": account["marginMode"],
            "im_rate": str(im_rate),
            "mm_rate": str(mm_rate),
            "at": time.time(),
        }

    async def open_orders(self, symbol=None):
        params = {"category": "linear", "openOnly": 0, "limit": 50}
        params.update({"symbol": symbol} if symbol else {"settleCoin": "USDT"})
        return await self.rows("/v5/order/realtime", params)

    async def order(self, symbol, link):
        params = {"category": "linear", "symbol": symbol, "orderLinkId": link}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = await self.rows(path, params)
            matches = [r for r in rows if r.get("orderLinkId") == link]
            if len(matches) > 1:
                raise ValueError("Ambiguous order identity")
            if matches:
                return matches[0]
        return None

    async def instrument(self, symbol):
        doc = await self._public(
            "GET", "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}
        )
        instrument = Instrument.parse(doc["result"]["list"][0])
        if instrument.symbol != symbol:
            raise ValueError("Instrument symbol mismatch")
        return instrument

    async def quote(self, symbol):
        doc = await self._public(
            "GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol}
        )
        q = doc["result"]["list"][0]
        bid, ask = number(q["bid1Price"]), number(q["ask1Price"])
        if q["symbol"] != symbol or not 0 < bid <= ask:
            raise ValueError("Invalid executable quote")
        return bid, ask

    async def last_price(self, symbol):
        doc = await self._public(
            "GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol}
        )
        row = doc["result"]["list"][0]
        price = number(row["lastPrice"])
        if row["symbol"] != symbol or price <= 0:
            raise ValueError("Invalid last traded price")
        return price

    async def fees(self, symbol):
        # Demo does not support /v5/account/fee-rate (documented API allowlist,
        # verified retCode=10001). Use explicit conservative cost estimates,
        # covering the 0.11% taker tier observed in this Demo account.
        # Real entry/exit fees always come from exchange executions; never tune strategy.
        return D("0.0011"), D("0.0004")

    async def configure_symbol(self, symbol, side, leverage):
        if not 0 < leverage <= 5:
            raise ValueError("Leverage exceeds 5x boundary")
        rows = await self.positions(symbol)
        if any(number(p["size"]) > 0 for p in rows):
            raise ValueError("SKIP_EXISTING_POSITION")
        indexes = {int(p["positionIdx"]) for p in rows}
        if indexes == {0}:
            idx = 0
        elif indexes == {1, 2}:
            idx = 1 if side == "LONG" else 2
        else:
            raise ValueError("Cannot determine exchange position mode")
        try:
            await self._private(
                "POST",
                "/v5/position/set-leverage",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "buyLeverage": format(leverage, "f"),
                    "sellLeverage": format(leverage, "f"),
                },
            )
        except BybitAPIError as error:
            if error.code != 110043:
                raise
        actual = await self.positions(symbol)
        if any(number(p["size"]) > 0 for p in actual):
            raise ValueError("SKIP_EXISTING_POSITION")
        slot = next(p for p in actual if int(p["positionIdx"]) == idx)
        if number(slot["leverage"]) != leverage:
            raise ValueError("Leverage verification failed")
        return idx

    async def submit(self, payload):
        doc = await self._private("POST", "/v5/order/create", payload)
        if not doc["result"].get("orderId"):
            raise ValueError("Order response missing orderId")
        return doc["result"]["orderId"]

    async def cancel(self, symbol, order_id):
        try:
            await self._private(
                "POST",
                "/v5/order/cancel",
                {"category": "linear", "symbol": symbol, "orderId": order_id},
            )
        except BybitAPIError as error:
            if error.code != 110001:
                raise

    async def executions(self, symbol, order_id, at=None):
        return await self.rows(
            "/v5/execution/list",
            {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id,
                "limit": 100,
                **(
                    {"startTime": int((at - 86400) * 1000), "endTime": int((at + 86400) * 1000)}
                    if at
                    else {}
                ),
            },
        )

    async def history_window(self, path, symbol, start, end):
        rows = {}
        # API permits at most 7 days per request. A position has no holding deadline.
        while start <= end:
            stop = min(end, start + 7 * 86400 * 1000 - 1)
            part = await self.rows(
                path,
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": start,
                    "endTime": stop,
                    "limit": 50,
                },
            )
            for row in part:
                rows[row.get("orderId", row.get("id"))] = row
            start = stop + 1
        return list(rows.values())
