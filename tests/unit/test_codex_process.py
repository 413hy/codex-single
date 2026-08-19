from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from bybit_signal.analysis.codex import (
    _resolve_model_skill_path,
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


def test_installed_package_finds_model_skill_in_checked_out_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "checkout"
    skill = (
        repository
        / ".agents"
        / "skills"
        / "analyze-bybit-ultrashort-signals"
    )
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "references" / "operating-contract.md").write_text(
        "contract",
        encoding="utf-8",
    )
    installed_source = (
        tmp_path
        / "venv"
        / "lib"
        / "site-packages"
        / "bybit_signal"
        / "analysis"
        / "codex.py"
    )

    resolved = _resolve_model_skill_path(
        tmp_path / "runtime-workspace",
        source_file=installed_source,
        current_directory=repository,
    )

    assert resolved == skill.resolve()


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
