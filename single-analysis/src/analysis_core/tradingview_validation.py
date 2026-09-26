"""Isolated model validation service. Never opens production ledgers or publishes signals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from analysis_core.config import Settings
from analysis_core.market import Markets
from analysis_core.model import DirectionModel
from analysis_core.store import Store, encode


async def validate(output: Path, extra_fields: str = ""):
    # Must be a fresh directory; no rerun of a recorded validation/model request.
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False, mode=0o700)
    settings = Settings(_env_file=None, runtime_dir=output, tradingview_enabled=True)  # type: ignore[call-arg]
    store = Store(output / "validation.db")
    markets = (
        Markets(include_tradingview=True, tradingview_extra_fields=extra_fields)
        if extra_fields else Markets(include_tradingview=True)
    )
    try:
        context = await markets.evidence("BTCUSDT", {"validation_only": True})
        context["direction_required"] = True
        context["validation_only"] = "Isolated schema/reference test, not a selected live candidate"
        (output / "context.json").write_text(encode(context))
        result = await DirectionModel(settings, store).decide("tv-validation:BTCUSDT", context)
        inputs = store.rows("SELECT payload FROM events WHERE kind='MODEL_INPUT'")
        outputs = store.rows("SELECT payload FROM events WHERE kind='MODEL_OUTPUT'")
        assert len(inputs) == len(outputs) == 1
        report = {
            "completed_at": datetime.now(UTC).isoformat(), "published_signals": 0,
            "model_calls": 1, "decision": result.model_dump(),
            "prompt_sha256": json.loads(inputs[0]["payload"])["prompt_sha256"],
            "context_sha256": hashlib.sha256((output / "context.json").read_bytes()).hexdigest(),
            "validation_scope": "Real fresh market and model schema/reference test; not performance proof",
        }
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await markets.close()


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extra-fields", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(validate(args.output, args.extra_fields))


if __name__ == "__main__":
    main()
