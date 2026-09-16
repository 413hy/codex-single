"""Executable CLI black-box tests: subprocess inputs, outputs and exit codes only."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_cli(tmp_path, *args, extra_env=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BYBIT_", "TELEGRAM_", "TRADING_", "RUNTIME_", "MODEL_", "CODEX_"))
    }
    env["RUNTIME_DIR"] = str(tmp_path / "runtime")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "longtime", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_config_check_is_secret_free_and_demo_only(tmp_path):
    result = run_cli(
        tmp_path,
        "config-check",
        extra_env={
            "BYBIT_API_KEY": "blackbox-secret-key",
            "BYBIT_API_SECRET": "blackbox-secret-secret",
            "TELEGRAM_TOKEN": "blackbox-token",
        },
    )
    assert result.returncode == 0
    doc = json.loads(result.stdout)
    assert doc["endpoint"] == "https://api-demo.bybit.com"
    assert doc["trading_enabled"] is False
    assert "blackbox-" not in result.stdout + result.stderr


@pytest.mark.parametrize("command", ["preflight", "cycle", "serve"])
def test_missing_credentials_fails_before_any_network_action(tmp_path, command):
    result = run_cli(tmp_path, command)
    assert result.returncode != 0
    assert "Missing BYBIT_API_KEY" in result.stderr


def test_unknown_command_and_help(tmp_path):
    assert run_cli(tmp_path, "invalid-command").returncode == 2
    assert run_cli(tmp_path, "--help").returncode == 0


def test_status_empty_database(tmp_path):
    result = run_cli(tmp_path, "status")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"cycles": [], "trades": []}


def test_lock_blocks_second_process_without_touching_exchange(tmp_path):
    import fcntl

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with (runtime / "service.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_cli(tmp_path, "status")
    assert result.returncode != 0
    assert "Another trader process" in result.stderr
