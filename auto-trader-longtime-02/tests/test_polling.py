"""Reproduce lingering server polls without accessing Telegram or real credentials."""

import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from longtime.config import Settings
from longtime.polling import RECOVERY_SECONDS, STATE_KEY, PollingGuard
from longtime.store import Store
from longtime.telegram import Telegram, TelegramAPIError


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("longtime.polling.time.time", lambda: now[0])

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr("longtime.polling.asyncio.sleep", sleep)
    return now


async def test_timeout_retry_waits_for_lingering_server_request(tmp_path, clock):
    attempts = []
    remote_expires = [0.0]

    async def wire(request):
        attempts.append(clock[0])
        if len(attempts) == 1:
            clock[0] += 60
            # Request still lives remotely after the local connection is lost.
            remote_expires[0] = clock[0] + 50
            raise httpx.ReadTimeout("lost response")
        if clock[0] < remote_expires[0]:
            return httpx.Response(409, json={"ok": False, "description": "other getUpdates"})
        return httpx.Response(200, json={"ok": True, "result": []})

    settings = Settings(_env_file=None, telegram_token="timeout-test")
    bot = Telegram(
        settings, Store(tmp_path / "db"), httpx.AsyncClient(transport=httpx.MockTransport(wire))
    )
    bot.store.set("entries_paused", True)
    bot.store.set("telegram_offset", 17)
    try:
        with pytest.raises(TelegramAPIError, match="ReadTimeout"):
            await bot.poll()
        # Prove the previous 5s retry would overlap the remote lease.
        assert clock[0] + 5 < remote_expires[0]
        bot.poll_error(TelegramAPIError("ReadTimeout", transient=True), lambda *args: None)
        await bot.poll()
        assert attempts[1] >= remote_expires[0]
        assert attempts[1] - (attempts[0] + 60) >= RECOVERY_SECONDS
        assert bot.store.state("telegram_offset") == 17
        assert bot.store.state("entries_paused") is True
        assert not bot.store.rows("SELECT * FROM cycles")
        assert bot.store.state(STATE_KEY)["phase"] == "completed"
    finally:
        await bot.close()


@pytest.mark.parametrize("status", [401, 409])
def test_permanent_errors_alert_immediately(tmp_path, status):
    from unittest.mock import Mock

    bot = Telegram(Settings(_env_file=None), Store(tmp_path / "db"), AsyncMock())
    alert = Mock()
    bot.poll_error(TelegramAPIError(f"HTTP {status}"), alert)
    alert.assert_called_once()


async def test_transient_failure_threshold_persists_and_success_resets(tmp_path, clock):
    from unittest.mock import Mock

    store = Store(tmp_path / "db")
    alert = Mock()
    bot = Telegram(Settings(_env_file=None), store, AsyncMock())
    error = TelegramAPIError("Telegram getUpdates failed (ReadTimeout)", transient=True)
    bot.poll_error(error, alert)
    clock[0] += 60
    bot.poll_error(error, alert)
    assert not alert.called
    await bot.close()
    bot = Telegram(Settings(_env_file=None), store, AsyncMock())
    try:
        clock[0] += 60
        bot.poll_error(error, alert)
        alert.assert_called_once()
        assert len(store.rows("SELECT * FROM events")) == 3
        bot.call = AsyncMock(return_value=[])
        await bot.poll()
        assert store.state("TELEGRAM_POLL:failure") is None
        alert.reset_mock()
        bot.poll_error(error, alert)
        assert not alert.called
    finally:
        await bot.close()


async def test_html_gateway_error_is_transient_and_does_not_leak_body(tmp_path):
    async def wire(request):
        return httpx.Response(502, text="private gateway response")

    bot = Telegram(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    try:
        with pytest.raises(TelegramAPIError) as exc:
            await bot.poll()
        assert exc.value.transient
        assert "502" in str(exc.value)
        assert "private" not in str(exc.value)
    finally:
        await bot.close()


def test_processing_failure_is_not_hidden_by_network_threshold(tmp_path):
    from unittest.mock import Mock

    bot = Telegram(Settings(_env_file=None), Store(tmp_path / "db"), AsyncMock())
    alert = Mock()
    bot.poll_error(ValueError("invalid command"), alert)
    alert.assert_called_once()


@pytest.mark.parametrize("status", [409, 502])
async def test_http_failure_and_restart_preserve_quiet_window(tmp_path, clock, status):
    settings = Settings(_env_file=None, telegram_token="restart-test")
    store = Store(tmp_path / "db")
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        status, json={"ok": False, "description": "other getUpdates"}
    )
    bot = Telegram(settings, store, client)
    with pytest.raises(TelegramAPIError):
        await bot.poll()
    deadline = store.state(STATE_KEY)["not_before"]
    await bot.close()
    client = AsyncMock()

    async def success(*args, **kwargs):
        assert clock[0] >= deadline
        return httpx.Response(200, json={"ok": True, "result": []})

    client.post.side_effect = success
    restarted = Telegram(settings, Store(store.path), client)
    try:
        await restarted.poll()
        assert client.post.await_count == 1
    finally:
        await restarted.close()


async def test_cancelled_poll_leaves_recovery_deadline(tmp_path, clock):
    guard = PollingGuard(Store(tmp_path / "db"), "cancel-test")
    try:
        with pytest.raises(asyncio.CancelledError):
            async with guard.request():
                assert guard.store.state(STATE_KEY)["phase"] == "in_flight"
                raise asyncio.CancelledError
        assert guard.store.state(STATE_KEY)["not_before"] == clock[0] + RECOVERY_SECONDS
    finally:
        guard.close()


async def test_crash_marker_and_startup_drain_are_honored(tmp_path, clock):
    store = Store(tmp_path / "db")
    store.set(STATE_KEY, {"attempt": 9, "not_before": 1150, "phase": "in_flight"})
    guard = PollingGuard(store, "crash-test")
    try:
        guard.startup()
        async with guard.request():
            assert clock[0] >= 1150
            assert store.state(STATE_KEY)["attempt"] == 10
    finally:
        guard.close()


def test_owner_excludes_other_process_and_runtime_directory(tmp_path):
    guard = PollingGuard(Store(tmp_path / "first"), "process-test:private")
    guard.acquire()
    script = """
from longtime.polling import PollingGuard, PollOwnershipError
g = PollingGuard(None, 'process-test:rotated')
try:
    g.acquire()
except PollOwnershipError:
    raise SystemExit(17)
g.close()
"""
    try:
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False)
        assert result.returncode == 17
    finally:
        guard.close()
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False)
    assert result.returncode == 0


async def test_concurrent_poll_calls_use_committed_offset(tmp_path):
    requests = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wire(request):
        payload = json.loads(request.content)
        requests.append(payload["offset"])
        if len(requests) == 1:
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 50}]})
        return httpx.Response(200, json={"ok": True, "result": []})

    bot = Telegram(
        Settings(_env_file=None, telegram_token="concurrent-test", telegram_chat_id=1),
        Store(tmp_path / "db"),
        httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    try:
        first = asyncio.create_task(bot.poll())
        await entered.wait()
        second = asyncio.create_task(bot.poll())
        release.set()
        await asyncio.gather(first, second)
        assert requests == [0, 51]
    finally:
        await bot.close()


async def test_failed_poll_rebuilds_only_poll_transport(tmp_path, monkeypatch):
    first, second = AsyncMock(), AsyncMock()
    first.post.side_effect = httpx.ReadTimeout("disconnected")
    factory = iter([first, second])
    monkeypatch.setattr(Telegram, "new_poll_client", staticmethod(lambda: next(factory)))
    bot = Telegram(
        Settings(_env_file=None, telegram_token="reconnect-test"), Store(tmp_path / "db")
    )
    delivery = bot.client
    try:
        with pytest.raises(TelegramAPIError):
            await bot.poll()
        first.aclose.assert_awaited_once()
        assert bot.poll_client is second
        assert bot.client is delivery and not delivery.is_closed
    finally:
        await bot.close()


async def test_total_timeout_keeps_drain_window_and_safe_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr("longtime.polling.REQUEST_SECONDS", 0.01)
    client = AsyncMock()

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    client.post.side_effect = hang
    bot = Telegram(
        Settings(_env_file=None, telegram_token="deadline-test"), Store(tmp_path / "db"), client
    )
    try:
        with pytest.raises(TelegramAPIError, match="TimeoutError"):
            await bot.poll()
        assert bot.store.state(STATE_KEY)["phase"] == "uncertain"
    finally:
        await bot.close()
