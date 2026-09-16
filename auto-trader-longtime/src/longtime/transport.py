"""Signing/clock transport extracted from bybit-trader; locked to Mainnet Demo."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from longtime.config import DEMO_URL


class BybitAPIError(RuntimeError):
    def __init__(
        self, code: int, message: str, *, method: str, path: str, definitive_rejection: bool = True
    ) -> None:
        super().__init__(f"Bybit {method} {path} failed: {code} {message}")
        self.definitive_rejection = definitive_rejection
        self.code = code
        self.message = message
        self.method = method
        self.path = path


class DemoTransport:
    def __init__(self, settings, client=None):
        self._trading_enabled = settings.trading_enabled
        self._api_key = settings.bybit_api_key.get_secret_value()
        self._api_secret = settings.bybit_api_secret.get_secret_value()
        self._client = client or httpx.AsyncClient(
            base_url=DEMO_URL, timeout=15, follow_redirects=False
        )
        if str(self._client.base_url).rstrip("/") != DEMO_URL:
            raise ValueError("Private transport must target Bybit Mainnet Demo")
        self._clock_offset_ms = 0
        self._recv_window = 10000

    async def close(self):
        await self._client.aclose()

    async def sync_clock(self):
        started = _local_ms()
        doc = await self._public("GET", "/v5/market/time")
        finished = _local_ms()
        server = int(doc["result"]["timeNano"]) // 1000000
        self._clock_offset_ms = server - (started + finished) // 2

    async def _public(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, params=params)
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise BybitAPIError(
                -1,
                "network or invalid JSON response",
                method=method,
                path=path,
                definitive_rejection=False,
            ) from error
        return _validate_document(document, response.status_code, method=method, path=path)

    async def _private(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/v5/") or "://" in path:
            raise ValueError("Invalid private API path")
        if method != "GET" and not self._trading_enabled:
            raise ValueError("TRADING_ENABLED=false: private exchange mutations disabled")
        payload = params or {}
        timestamp = _local_ms() + self._clock_offset_ms
        if method == "GET":
            query_items = sorted((key, str(value)) for key, value in payload.items())
            query = urlencode(query_items)
            signature_payload = f"{timestamp}{self._api_key}{self._recv_window}{query}"
            request_kwargs: dict[str, Any] = {"params": query_items}
        else:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            signature_payload = f"{timestamp}{self._api_key}{self._recv_window}{body}"
            request_kwargs = {"content": body, "headers": {"Content-Type": "application/json"}}
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = cast(dict[str, str], request_kwargs.setdefault("headers", {}))
        headers.update(
            {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": str(timestamp),
                "X-BAPI-RECV-WINDOW": str(self._recv_window),
                "X-BAPI-SIGN": signature,
            }
        )
        try:
            response = await self._client.request(method, path, **request_kwargs)
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise BybitAPIError(
                -1,
                "network or invalid JSON response",
                method=method,
                path=path,
                definitive_rejection=False,
            ) from error
        return _validate_document(document, response.status_code, method=method, path=path)


def _validate_document(value: Any, status_code: int, *, method: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BybitAPIError(
            -1, "response is not an object", method=method, path=path, definitive_rejection=False
        )
    document = cast(dict[str, Any], value)
    raw_code = document.get("retCode")
    if type(raw_code) is not int:
        raise BybitAPIError(
            -1,
            "response missing valid retCode",
            method=method,
            path=path,
            definitive_rejection=False,
        )
    code = raw_code
    if status_code >= 400 or code != 0:
        raise BybitAPIError(
            code or status_code,
            str(document.get("retMsg") or "unknown error"),
            method=method,
            path=path,
            definitive_rejection=200 <= status_code < 300 and code > 0,
        )
    return document


def _local_ms() -> int:
    return time.time_ns() // 1_000_000
