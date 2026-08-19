from __future__ import annotations

import asyncio
from typing import cast

import pytest

from bybit_signal.analysis.codex import (
    _subprocess_platform_options,
    _terminate_process_tree,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        del input
        self.returncode = 0
        return b"ok", b""

    async def wait(self) -> int:
        self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

def test_posix_codex_process_starts_in_new_session() -> None:
    options = _subprocess_platform_options("posix")

    assert options == {"start_new_session": True}
    assert "creationflags" not in options


@pytest.mark.asyncio
async def test_posix_timeout_terminates_entire_codex_process_group(
) -> None:
    process = _FakeProcess()
    calls: list[tuple[int, int]] = []

    await _terminate_process_tree(
        cast(asyncio.subprocess.Process, process),
        platform_name="posix",
        kill_process_group=lambda pid, sig: calls.append((pid, sig)),
    )

    assert calls == [(4242, 9)]
    assert process.returncode == -9
