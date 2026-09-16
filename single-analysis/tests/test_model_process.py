"""Exercise actual subprocess, stdin/schema, environment isolation and timeout paths."""

import asyncio
import json
from pathlib import Path

import pytest

from analysis_core.config import Settings
from analysis_core.model import DirectionModel, ModelServiceError
from analysis_core.store import Store


def executable(tmp_path, body):
    path = tmp_path / "model-fixture"
    path.write_text("#!/usr/bin/python3\n" + body)
    path.chmod(0o700)
    return str(path)


async def test_real_child_gets_no_exchange_or_bot_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_API_SECRET", "never-pass-this")
    monkeypatch.setenv("TELEGRAM_TOKEN", "never-pass-token")
    binary = executable(
        tmp_path,
        """import json, os, pathlib, sys
assert "BYBIT_API_SECRET" not in os.environ
assert "TELEGRAM_TOKEN" not in os.environ
args=sys.argv
assert args[args.index("--model")+1]=="gpt-5.6-terra"
assert 'model_reasoning_effort="medium"' in args
assert "multi_agent" in args and "shell_tool" in args
assert "skip_host_skill_discovery" in args and "project_doc_max_bytes=0" in args
assert "memories" in args and "skill_mcp_dependency_install" in args
prompt=sys.stdin.read()
assert "UNTRUSTED MARKET DATA" in prompt
schema=json.loads(pathlib.Path(args[args.index("--output-schema")+1]).read_text())
assert schema["additionalProperties"] is False
out=pathlib.Path(args[args.index("--output-last-message")+1])
out.write_text(json.dumps({"symbol":"TESTUSDT","decision":"SHORT","reason":"direction"}))
""",
    )
    store = Store(tmp_path / "db")
    model = DirectionModel(Settings(_env_file=None, codex_bin=binary), store)
    assert (await model.decide("s", {"symbol": "TESTUSDT"})).decision == "SHORT"
    assert [r["kind"] for r in store.rows("SELECT kind FROM events")] == [
        "MODEL_INPUT",
        "MODEL_OUTPUT",
    ]


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        '{"symbol":"OTHERUSDT","decision":"LONG","reason":"x"}',
        '{"symbol":"TESTUSDT","decision":"LONG","reason":"x","qty":10}',
    ],
)
async def test_real_child_invalid_output_never_accepted(tmp_path, response):
    binary = executable(
        tmp_path,
        'import sys,pathlib\npathlib.Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text('
        + repr(response)
        + ")\n",
    )
    model = DirectionModel(Settings(_env_file=None, codex_bin=binary), Store(tmp_path / "db"))
    with pytest.raises(ValueError):
        await model.decide("s", {"symbol": "TESTUSDT"})


async def test_real_child_timeout_is_killed_and_not_retried(tmp_path):
    pidfile = tmp_path / "pid"
    binary = executable(
        tmp_path,
        f"import os,time,pathlib\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    settings = Settings(_env_file=None, codex_bin=binary).model_copy(update={"model_timeout": 0.2})
    store = Store(tmp_path / "db")
    with pytest.raises(ModelServiceError, match="超时"):
        await DirectionModel(settings, store).decide("s", {"symbol": "TESTUSDT"})
    assert not await asyncio.to_thread(lambda: Path("/proc/" + pidfile.read_text()).exists())
    assert len(store.rows("SELECT * FROM events WHERE kind='MODEL_INPUT'")) == 1


async def test_real_child_error_preserves_input_once(tmp_path):
    binary = executable(
        tmp_path, 'import sys\nsys.stderr.write("ERROR: injected runtime failure")\nsys.exit(7)\n'
    )
    store = Store(tmp_path / "db")
    model = DirectionModel(Settings(_env_file=None, codex_bin=binary), store)
    with pytest.raises(RuntimeError, match="exit=7"):
        await model.decide("s", {"symbol": "TESTUSDT"})
    records = store.rows("SELECT * FROM events")
    assert [r["kind"] for r in records] == ["MODEL_INPUT", "MODEL_FAILURE"]
    assert json.loads(records[-1]["payload"])["returncode"] == 7


def test_capacity_classification_ignores_untrusted_evidence_text():
    from analysis_core.model import service_failure

    assert service_failure("market data says selected model is at capacity") is None
    assert "繁忙" in service_failure(
        "ERROR: Selected model is at capacity. Please try a different model."
    )


def test_column_encoding_preserves_every_candle_value_without_mutation():
    from analysis_core.model import direction_context

    row = {
        "symbol": "TESTUSDT",
        "timeframe": "1m",
        "source": "BYBIT",
        "open_time": "2026-09-09T00:00:00Z",
        "close_time": "2026-09-09T00:01:00Z",
        "open": "0.001",
        "high": "0.002",
        "low": "0.001",
        "close": "0.0015",
        "volume": "3.20",
        "turnover": "0.0048",
        "completed": False,
    }
    context = {"symbol": "TESTUSDT", "candles": {"1m": [row]}}
    result = direction_context(context)
    reconstructed = {
        **result["candle_metadata"]["1m"],
        **dict(zip(result["candle_columns"], result["candles"]["1m"][0], strict=True)),
    }
    assert reconstructed == row
    assert context["candles"]["1m"][0] is row
    with pytest.raises(ValueError):
        direction_context({"symbol": "OTHERUSDT", "candles": {"1m": [row]}})
    with pytest.raises(ValueError):
        direction_context({"symbol": "TESTUSDT", "candles": {"1m": [{**row, "new_field": 1}]}})


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Selected model is at capacity. Please try a different model.", "繁忙"),
        ("Your workspace is out of credits. Add credits to continue.", "额度"),
        ("unexpected status 401 Unauthorized: Missing bearer or basic authentication", "认证"),
        ("429 Too Many Requests", "请求受限"),
        ("503 Service Unavailable", "暂不可用"),
        ("connection reset by peer", "连接失败"),
    ],
)
async def test_real_child_upstream_errors_classified_once_without_fallback(
    tmp_path, error, expected
):
    calls = tmp_path / "calls"
    binary = executable(
        tmp_path,
        "import sys,pathlib\n"
        f"p=pathlib.Path({str(calls)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        "assert sys.argv[sys.argv.index('--model')+1]=='gpt-5.6-terra'\n"
        f"sys.stderr.write('ERROR: '+{error!r}+'\\n')\nsys.exit(1)\n",
    )
    store = Store(tmp_path / "db")
    model = DirectionModel(Settings(_env_file=None, codex_bin=binary), store)
    with pytest.raises(ModelServiceError, match=expected):
        await model.request("screen-test", {}, {}, "screening.md", event_prefix="SCREENING_MODEL")
    assert calls.read_text() == "x"
    failure = json.loads(
        store.rows("SELECT payload FROM events WHERE kind='SCREENING_MODEL_FAILURE'")[0]["payload"]
    )
    assert failure["errors"] == ["ERROR: " + error]
    assert not store.rows("SELECT * FROM orders")


async def test_real_child_missing_result_is_diagnosable(tmp_path):
    binary = executable(tmp_path, "pass\n")
    store = Store(tmp_path / "db")
    with pytest.raises(ValueError, match="未生成结果"):
        await DirectionModel(Settings(_env_file=None, codex_bin=binary), store).decide(
            "missing-result", {"symbol": "TESTUSDT"}
        )
    record = json.loads(
        store.rows("SELECT payload FROM events WHERE kind='MODEL_FAILURE'")[0]["payload"]
    )
    assert record["reason"] == "missing_output" and record["signal_id"] == "missing-result"


async def test_real_child_nonzero_exit_cannot_use_written_direction(tmp_path):
    binary = executable(
        tmp_path,
        "import sys,pathlib\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{}')\n"
        "sys.stderr.write('ERROR: Selected model is at capacity.')\nsys.exit(1)\n",
    )
    store = Store(tmp_path / "db")
    with pytest.raises(ModelServiceError):
        await DirectionModel(Settings(_env_file=None, codex_bin=binary), store).decide(
            "s", {"symbol": "TESTUSDT"}
        )
    assert not store.rows("SELECT * FROM events WHERE kind='MODEL_OUTPUT'")


async def test_cancellation_kills_child_without_misreporting_service_failure(tmp_path):
    pidfile = tmp_path / "pid"
    binary = executable(
        tmp_path,
        f"import os,pathlib,time\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    store = Store(tmp_path / "db")
    task = asyncio.create_task(
        DirectionModel(Settings(_env_file=None, codex_bin=binary), store).decide(
            "cancel", {"symbol": "TESTUSDT"}
        )
    )
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await asyncio.to_thread(lambda: Path("/proc/" + pidfile.read_text()).exists())
    event = json.loads(
        store.rows("SELECT payload FROM events WHERE kind='MODEL_INTERRUPTED'")[0]["payload"]
    )
    assert event["reason"] == "CancelledError"


@pytest.mark.parametrize("text", ["401 Unauthorized", "429 Too Many Requests", "timed out"])
def test_untrusted_context_does_not_classify_as_service_failure(text):
    from analysis_core.model import service_failure

    assert service_failure('user\n{"market_comment": "' + text + '"}') is None


def test_request_id_digits_do_not_become_rate_limit_error():
    from analysis_core.model import service_failure

    assert service_failure("ERROR: invalid configuration; request id: req_429abcdef") is None


@pytest.mark.parametrize("response", ["LONG", "SHORT", "SKIP"])
async def test_primary_schema_and_host_validation_require_direction(tmp_path, response):
    from unittest.mock import AsyncMock

    model = DirectionModel(Settings(_env_file=None), Store(tmp_path / "db"))
    model.request = AsyncMock(
        return_value={
            "symbol": "TESTUSDT",
            "decision": response,
            "reason": "relative best; low confidence",
        }
    )
    if response == "SKIP":
        with pytest.raises(ValueError, match="普通首选"):
            await model.decide("primary", {"symbol": "TESTUSDT", "direction_required": True})
    else:
        assert (
            await model.decide("primary", {"symbol": "TESTUSDT", "direction_required": True})
        ).decision == response
    schema = model.request.call_args.args[2]
    assert schema["properties"]["decision"]["enum"] == ["LONG", "SHORT"]
    assert model.request.await_count == 1


async def test_extra_hedge_schema_keeps_abstention(tmp_path):
    from unittest.mock import AsyncMock

    model = DirectionModel(Settings(_env_file=None), Store(tmp_path / "db"))
    model.request = AsyncMock(
        return_value={"symbol": "TESTUSDT", "decision": "SKIP", "reason": "conflicting structure"}
    )
    assert (
        await model.decide("extra", {"symbol": "TESTUSDT", "direction_required": False})
    ).decision == "SKIP"
    assert "SKIP" in model.request.call_args.args[2]["properties"]["decision"]["enum"]


async def test_cancellation_during_subprocess_creation_reaps_child(tmp_path, monkeypatch):
    import os

    real_create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def delayed_create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        children.append(child)
        started.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    binary = executable(tmp_path, "import time\ntime.sleep(30)\n")
    store = Store(tmp_path / "db")
    task = asyncio.create_task(
        DirectionModel(Settings(_env_file=None, codex_bin=binary), store).decide(
            "spawn-cancel", {"symbol": "TESTUSDT"}
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await asyncio.to_thread(Path(f"/proc/{children[0].pid}").exists)
        assert store.rows("SELECT * FROM events WHERE kind='MODEL_INTERRUPTED'")
    finally:
        for child in children:
            if child.returncode is None:
                os.killpg(child.pid, 9)
                await child.wait()
