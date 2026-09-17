"""Single owner and durable recovery window for Telegram's remote long poll."""

import asyncio
import hashlib
import socket
import time
from contextlib import asynccontextmanager

# Telegram's server allows long polls up to 50s. Allow an additional 10s for transit.
# A local timeout/cancellation does not prove that the remote request has stopped.
RECOVERY_SECONDS = 60
REQUEST_SECONDS = 90
STATE_KEY = "telegram_poll_request"


class PollOwnershipError(RuntimeError):
    pass


class PollingGuard:
    def __init__(self, store, token):
        self.store = store
        bot_id = token.split(":", 1)[0]
        self.address = "\0single-analysis.telegram." + hashlib.sha256(bot_id.encode()).hexdigest()
        self.owner: socket.socket | None = None
        self.lock = asyncio.Lock()

    def acquire(self):
        if self.owner is not None:
            return
        owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Linux abstract socket: shared across working dirs and PrivateTmp;
            # automatically released on process death, contains no credential.
            owner.bind(self.address)
        except OSError:
            owner.close()
            raise PollOwnershipError("本机已有此分析Bot的轮询实例，拒绝重复启动") from None
        self.owner = owner

    def startup(self):
        self.acquire()
        previous = self.store.state(STATE_KEY, {})
        self.store.set(STATE_KEY, {
            **previous,
            "not_before": max(previous.get("not_before", 0), time.time() + RECOVERY_SECONDS),
            "phase": "startup_drain",
        })

    @asynccontextmanager
    async def request(self):
        async with self.lock:
            self.acquire()
            previous = self.store.state(STATE_KEY, {})
            deadline = previous.get("not_before", 0)
            if time.time() < deadline:
                await asyncio.sleep(deadline - time.time())
            started = time.time()
            attempt = previous.get("attempt", 0) + 1
            record = {
                "attempt": attempt,
                "started_at": started,
                "phase": "in_flight",
                # Written BEFORE sending: even SIGKILL leaves a recovery window.
                "not_before": started + REQUEST_SECONDS + RECOVERY_SECONDS,
            }
            self.store.set(STATE_KEY, record)
            try:
                async with asyncio.timeout(REQUEST_SECONDS):
                    yield
            except (Exception, asyncio.CancelledError):
                self.store.set(STATE_KEY, {
                    **record, "phase": "uncertain", "finished_at": time.time(),
                    "not_before": time.time() + RECOVERY_SECONDS,
                })
                raise
            else:
                self.store.set(STATE_KEY, {
                    **record, "phase": "completed", "finished_at": time.time(),
                    "not_before": 0,
                })

    def close(self):
        if self.owner is not None:
            self.owner.close()
            self.owner = None
