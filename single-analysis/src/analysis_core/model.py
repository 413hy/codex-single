"""No-tool Codex invocation adapted from ai-trader/bridge/model.py; one attempt only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from analysis_core.config import (
    DIRECTION_MODEL,
    DIRECTION_REASONING,
)
from analysis_core.store import encode


class ModelServiceError(RuntimeError):
    """Shared upstream outage; one incident, no per-symbol retry storm."""


def service_failure(diagnostic):
    errors = "\n".join(
        line.lower() for line in diagnostic.splitlines() if line.startswith("ERROR:")
    )
    if "selected model is at capacity" in errors:
        return "指定GPT模型服务繁忙(capacity)，本轮剩余分析停止；恢复运行后的下一轮使用新行情"
    if any(x in errors for x in ("out of credits", "insufficient_quota", "usage limit")):
        return "GPT额度暂不可用，本轮剩余分析停止；请检查额度后重试"
    if any(x in errors for x in ("401 unauthorized", "invalid_api_key", "authentication failed")):
        return "GPT认证失败(401)，本轮剩余分析停止；需检查模型服务登录或凭据"
    if any(
        x in errors
        for x in (
            "429 too many requests",
            "status 429",
            "http 429",
            "rate_limit_exceeded",
            "rate limit reached",
        )
    ):
        return "GPT请求受限(429)，本轮剩余分析停止；下一轮使用新行情重新检查"
    if any(x in errors for x in ("503 service unavailable", "server_overloaded")):
        return "GPT上游服务暂不可用(503)，本轮剩余分析停止；下一轮使用新行情重新检查"
    if any(x in errors for x in ("connection reset", "connection refused", "timed out")):
        return "GPT服务连接失败，本轮剩余分析停止；下一轮使用新行情重新检查"
    return None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}USDT$")
    decision: Literal["LONG", "SHORT", "SKIP"]
    reason: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True)
class ModelProfile:
    purpose: str
    model_id: str
    reasoning_effort: str
    prompt_names: frozenset[str]
    event_prefix: str


DIRECTION_PROFILE = ModelProfile(
    "direction", DIRECTION_MODEL, DIRECTION_REASONING,
    frozenset({"direction_v15.md"}), "MODEL",
)


def _parse_aware_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Candle timestamp must have a timezone")
    return parsed


async def _kill_and_reap(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
        return True
    except TimeoutError:
        # The group was killed; keep a waiter attached if the OS has not reaped it yet.
        asyncio.create_task(proc.wait())
        return False


async def _reap_late_spawn(spawning):
    try:
        proc = await spawning
    except Exception:
        return False
    return await _kill_and_reap(proc)


def direction_context(context):
    """Lossless column encoding: invariant candle identity stays at timeframe level."""
    if "tradingview" in context:
        context = {**context, "tradingview": {
            key: value for key, value in context["tradingview"].items() if key != "provenance"
        }}
    if "candles" not in context:
        return context
    columns = [
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "completed",
    ]
    period_minutes = {
        "1m": 1, "3m": 3, "5m": 5, "10m": 10,
        "15m": 15, "30m": 30, "1h": 60, "2h": 120,
    }
    observed = _parse_aware_time(context["observed_at"]) if "observed_at" in context else None
    metadata, candles, latest_closed = {}, {}, {}
    for timeframe, rows in context["candles"].items():
        if timeframe not in period_minutes:
            raise ValueError("Unexpected direction candle timeframe")
        if not rows:
            raise ValueError("Empty direction candle input")
        identity = {key: rows[0][key] for key in ("symbol", "timeframe", "source")}
        if identity["symbol"] != context["symbol"] or identity["timeframe"] != timeframe:
            raise ValueError("Direction candle metadata mismatch")
        previous_open = None
        incomplete_seen = False
        for row in rows:
            if any(row[key] != value for key, value in identity.items()):
                raise ValueError("Inconsistent direction candle metadata")
            if set(row) != set(columns) | set(identity):
                raise ValueError("Unexpected direction candle fields")
            opened = _parse_aware_time(row["open_time"])
            closed_at = _parse_aware_time(row["close_time"])
            if (
                (previous_open is not None and opened <= previous_open)
                or closed_at - opened != timedelta(minutes=period_minutes[timeframe])
            ):
                raise ValueError("Direction candles must be chronological with valid close times")
            previous_open = opened
            if type(row["completed"]) is not bool:
                raise ValueError("Direction candle completed must be boolean")
            if row["completed"] and observed is not None and closed_at > observed:
                raise ValueError("Completed direction candle ends after observed_at")
            if incomplete_seen and row["completed"]:
                raise ValueError("Completed direction candle follows an incomplete candle")
            incomplete_seen = incomplete_seen or not row["completed"]
        metadata[timeframe] = identity
        candles[timeframe] = [[row[key] for key in columns] for row in rows]
        closed = [row for row in rows if row["completed"] is True]
        latest_closed[timeframe] = closed[-1] if closed else None
        supplied_latest = context.get("latest_closed_candles", {})
        if timeframe in supplied_latest and supplied_latest[timeframe] != latest_closed[timeframe]:
            raise ValueError("Latest closed candle disagrees with direction history")
    if "latest_closed_candles" in context and set(context["latest_closed_candles"]) != set(candles):
        raise ValueError("Latest closed candle timeframes disagree with direction history")
    return {
        **context,
        "candles": candles,
        "candle_columns": columns,
        "candle_metadata": metadata,
        "latest_closed_candles": latest_closed,
        "candle_encoding": "columns-v1; 每行字段按candle_columns顺序，身份字段在candle_metadata",
    }


class ModelService:
    def __init__(self, settings, store, profile: ModelProfile):
        self.settings, self.store, self.profile = settings, store, profile

    async def request(self, signal_id, context, schema, prompt_name, *, event_prefix=None):
        if prompt_name not in self.profile.prompt_names:
            raise ValueError("Model profile and prompt do not match")
        event_prefix = event_prefix or self.profile.event_prefix
        base_prompt = (Path(__file__).parent / "prompts" / prompt_name).read_text()
        prompt = base_prompt + "\nUNTRUSTED MARKET DATA:\n" + encode(context)
        input_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
        started = time.monotonic()
        self.store.event(
            event_prefix + "_INPUT",
            {
                "signal_id": signal_id,
                "purpose": self.profile.purpose,
                "model": self.profile.model_id,
                "reasoning_effort": self.profile.reasoning_effort,
                "schema": schema,
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(base_prompt.encode()).hexdigest(),
                "input_sha256": input_sha256,
            },
        )
        # Credentials are never inherited by the direction process.
        env = {
            k: os.environ[k] for k in ("HOME", "PATH", "LANG", "SSL_CERT_FILE") if k in os.environ
        }
        with tempfile.TemporaryDirectory(prefix="analysis_core-direction-") as tmp:
            path = Path(tmp)
            (path / "schema.json").write_text(encode(schema))
            command = [
                self.settings.codex_bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.profile.model_id,
                "-c",
                f'model_reasoning_effort="{self.profile.reasoning_effort}"',
                "-c",
                'web_search="disabled"',
                "-c",
                "project_doc_max_bytes=0",
                "--enable",
                "skip_host_skill_discovery",
                "--output-schema",
                str(path / "schema.json"),
                "--output-last-message",
                str(path / "output.json"),
                "--cd",
                tmp,
            ]
            for feature in (
                "shell_tool",
                "plugins",
                "apps",
                "browser_use",
                "computer_use",
                "skill_search",
                "skill_mcp_dependency_install",
                "memories",
                "remote_plugin",
                "multi_agent",
            ):
                command += ["--disable", feature]
            command += ["-"]
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=tmp,
                    env=env,
                    start_new_session=True,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
            try:
                proc = await asyncio.wait_for(
                    asyncio.shield(spawning), self.settings.model_timeout
                )
            except (TimeoutError, asyncio.CancelledError) as error:
                cleanup = asyncio.create_task(_reap_late_spawn(spawning))
                try:
                    reaped = await asyncio.wait_for(asyncio.shield(cleanup), timeout=1)
                except TimeoutError:
                    reaped = False
                self.store.event(
                    event_prefix + "_INTERRUPTED",
                    {
                        "signal_id": signal_id, "reason": type(error).__name__,
                        "phase": "startup", "reaped_before_return": reaped,
                    },
                )
                if isinstance(error, TimeoutError):
                    raise ModelServiceError(
                        f"GPT分析超时({self.settings.model_timeout}秒)，本轮剩余分析停止；"
                        "下一轮使用新行情重新检查"
                    ) from None
                raise
            try:
                remaining = self.settings.model_timeout - (time.monotonic() - started)
                _, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), max(0, remaining)
                )
            except (TimeoutError, asyncio.CancelledError) as error:
                reaped = await _kill_and_reap(proc)
                self.store.event(
                    event_prefix + "_INTERRUPTED",
                    {
                        "signal_id": signal_id, "pid": proc.pid,
                        "reason": type(error).__name__, "phase": "inference",
                        "reaped_before_return": reaped,
                    },
                )
                if isinstance(error, TimeoutError):
                    raise ModelServiceError(
                        f"GPT分析超时({self.settings.model_timeout}秒)，本轮剩余分析停止；"
                        "下一轮使用新行情重新检查"
                    ) from None
                raise
            if proc.returncode:
                # Child has no exchange keys; bounded diagnostics remain local.
                diagnostic = err.decode(errors="replace")
                self.store.event(
                    event_prefix + "_FAILURE",
                    {
                        "signal_id": signal_id,
                        "purpose": self.profile.purpose,
                        "model": self.profile.model_id,
                        "reasoning_effort": self.profile.reasoning_effort,
                        "input_sha256": input_sha256,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "returncode": proc.returncode,
                        "diagnostic": diagnostic[-2000:],
                        "errors": [
                            line[:500]
                            for line in diagnostic.splitlines()
                            if line.startswith("ERROR:")
                        ][-10:],
                    },
                )
                shared = service_failure(diagnostic)
                if shared:
                    raise ModelServiceError(shared)
                raise RuntimeError(
                    f"GPT调用失败(exit={proc.returncode})，本轮SKIP；"
                    f"诊断已记录{event_prefix}_FAILURE，signal_id={signal_id}"
                )
            output = path / "output.json"
            if not output.is_file():
                self.store.event(
                    event_prefix + "_FAILURE",
                    {"signal_id": signal_id, "returncode": 0, "reason": "missing_output"},
                )
                raise ValueError(
                    f"GPT调用结束但未生成结果；详见{event_prefix}_FAILURE，signal_id={signal_id}"
                )
            raw = output.read_text()
            self.store.event(event_prefix + "_OUTPUT", {
                "signal_id": signal_id,
                "purpose": self.profile.purpose,
                "model": self.profile.model_id,
                "reasoning_effort": self.profile.reasoning_effort,
                "input_sha256": input_sha256,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "raw": raw,
            })
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError(
                    f"GPT返回无效JSON，未采用；详见{event_prefix}_OUTPUT，signal_id={signal_id}"
                ) from None


class DirectionModel(ModelService):
    def __init__(self, settings, store):
        super().__init__(settings, store, DIRECTION_PROFILE)

    async def decide(self, signal_id, context):
        if "tradingview" not in context:
            raise ValueError("TradingView evidence is required for hedge direction")
        schema = Decision.model_json_schema()
        schema["properties"]["symbol"]["enum"] = [context["symbol"]]
        required = context.get("direction_required") is True
        if required:
            schema["properties"]["decision"]["enum"] = ["LONG", "SHORT"]
        checked = direction_context(context)
        if "candles" in checked:
            # Keep the complete validated snapshot in the host audit event; send
            # only recent closed bars and summary statistics to the model.
            from analysis_core.selection import compact_bybit, compact_tradingview

            checked = {
                "symbol": context["symbol"],
                "observed_at": context["observed_at"],
                "direction_required": required,
                "priority_review": context.get("priority_review"),
                "tradingview": compact_tradingview(context["tradingview"]),
                "bybit": compact_bybit(context),
            }
        raw = await self.request(signal_id, checked, schema, "direction_v15.md")
        result = Decision.model_validate(raw)
        if result.symbol != context["symbol"]:
            raise ValueError("Model symbol mismatch")
        if required and result.decision == "SKIP":
            raise ValueError("普通首选必须明确判向，模型返回SKIP；本轮分析失败，不伪造方向")
        return result
