"""Signal-system screening: ten-candidate evidence pool, six reviewed, zero to three selected."""

from datetime import UTC, datetime

from analysis_core.model import DirectionModel
from analysis_core.store import identity
from analysis_core.vendor.signal.domain.market_requirements import PRICE_ACTION_MINIMUM_CANDLES
from analysis_core.vendor.signal.domain.models import EvidenceBundle
from analysis_core.vendor.signal.evidence.builder import EvidenceBuilder
from analysis_core.vendor.signal.providers.bybit import BybitPublicClient
from analysis_core.vendor.signal.providers.cross_exchange import CrossExchangePublicClient
from analysis_core.vendor.signal.providers.deep_market import BybitDeepMarketCollector
from analysis_core.vendor.signal.screening.models import ModelScreeningResponse
from analysis_core.vendor.signal.screening.validation import (
    _enforce_selection_quality_gate,
    _neutralize_forbidden_screening_terms,
    _normalize_redundant_evidence_ids,
    _sanitize_evidence_citations,
)

TIMEFRAMES = ("3m", "5m", "15m", "30m", "1h", "4h")


def prioritize(candidates, snapshots):
    return sorted(
        (c for c in candidates if c.symbol in snapshots),
        key=lambda c: (
            -sum(
                len(snapshots[c.symbol].candles.get(tf, ())) >= PRICE_ACTION_MINIMUM_CANDLES
                for tf in TIMEFRAMES
            ),
            c.rank,
        ),
    )


def strict_schema(value):
    if isinstance(value, dict):
        value.pop("default", None)
        if isinstance(value.get("properties"), dict):
            value["required"] = list(value["properties"])
            value["additionalProperties"] = False
        for child in value.values():
            strict_schema(child)
    elif isinstance(value, list):
        for child in value:
            strict_schema(child)


def bundle_payload(bundle):
    # Keep real market-context evidence inline; never send a dangling batch reference.
    payload = bundle.model_dump(mode="json", exclude={"market_context"})
    payload.pop("canonical_last", None)
    payload.pop("canonical_mark", None)
    context = payload.get("candidate_context")
    if context:
        context.pop("last_price", None)
        context.pop("raw_features", None)
    evidence = [
        item for item in payload["evidence_items"] if not item["evidence_id"].endswith(".1M")
    ]
    for item in evidence:
        if item["category"] == "candidate_filter":
            item["summary"] = "Filter context is a screening hint, not a model conclusion"
            item["values"] = context or {}
    payload["evidence_items"] = evidence
    allowed = {item["evidence_id"] for item in evidence}
    payload["allowed_evidence_ids"] = sorted(allowed)
    payload["tool_assessments"] = [
        {**tool, "evidence_ids": [eid for eid in tool["evidence_ids"] if eid in allowed]}
        for tool in payload["tool_assessments"]
        if tool["tool"] != "BYBIT_NATIVE_ULTRASHORT_1M"
    ]
    return payload


class Screening:
    def __init__(self, settings, store, *, runner=None, collector=None, builder=None):
        self.store = store
        self.runner = runner or DirectionModel(settings, store)
        self.collector = collector
        self.builder = builder or EvidenceBuilder()

    async def select(self, cid, candidates):
        pool = tuple(candidates[:10])
        if len({c.symbol for c in pool}) != len(pool):
            raise ValueError("Duplicate screening candidate symbols")
        if len(pool) < 6:
            raise ValueError("筛选需要6份有效行情证据，当前候选不足；本轮不开仓")
        if self.collector is None:
            public = BybitPublicClient(max_attempts=1)
            references = CrossExchangePublicClient()
            try:
                snapshots, failures = await BybitDeepMarketCollector(
                    public, reference_client=references
                ).collect_many(tuple(c.symbol for c in pool))
            finally:
                await public.close()
                await references.close()
        else:
            snapshots, failures = await self.collector.collect_many(tuple(c.symbol for c in pool))
        bundles: list[EvidenceBundle] = []
        for candidate in prioritize(pool, snapshots):
            if len(bundles) == 6:
                break
            try:
                bundles.append(self.builder.build(snapshots[candidate.symbol], candidate))
            except Exception as error:
                failures[candidate.symbol] = type(error).__name__ + ": " + str(error)[:400]
        self.store.event(
            "SCREENING_EVIDENCE",
            {
                "cycle_id": cid,
                "pool": [c.symbol for c in pool],
                "bundles": [b.model_dump(mode="json") for b in bundles],
                "failures": failures,
            },
        )
        if len(bundles) != 6 or len({b.symbol for b in bundles}) != 6:
            raise ValueError("不足6份有效筛选证据；本轮不开仓，详见SCREENING_EVIDENCE")
        analysis_id = "screen_" + identity(cid)
        context = {
            "schema_version": 1,
            "contract_kind": "MARKET_SCREENING",
            "analysis_id": analysis_id,
            "mode": "SCHEDULED",
            "requested_at": datetime.now(UTC).isoformat(),
            "candidate_symbols_in_order": [b.symbol for b in bundles],
            "evidence_bundles": [bundle_payload(b) for b in bundles],
            "selection_limit": 3,
        }
        schema = ModelScreeningResponse.model_json_schema()
        strict_schema(schema)
        document = await self.runner.request(
            analysis_id, context, schema, "screening.md", event_prefix="SCREENING_MODEL"
        )
        # Reject malformed coverage before the original deterministic abstention helpers.
        if not isinstance(document, dict) or not isinstance(document.get("assessments"), list):
            raise ValueError("Invalid screening document")
        items = document["assessments"]
        if (
            len(items) != 6
            or any(not isinstance(item, dict) for item in items)
            or {item.get("symbol") for item in items} != {b.symbol for b in bundles}
            or sum(item.get("status") == "SELECTED" for item in items) > 3
        ):
            raise ValueError("Screening coverage or selection limit mismatch")
        ranks = [item.get("selection_rank") for item in items if item.get("status") == "SELECTED"]
        if any(type(rank) is not int for rank in ranks) or sorted(ranks) != list(
            range(1, len(ranks) + 1)
        ):
            raise ValueError("Screening ranks must be unique and contiguous")
        if any(
            item.get("status") != "SELECTED" and item.get("selection_rank") is not None
            for item in items
        ):
            raise ValueError("Unselected screening candidate has a rank")
        allowed_original = {
            b["symbol"]: set(b["allowed_evidence_ids"]) for b in context["evidence_bundles"]
        }
        for item in items:
            citations = list(item.get("evidence_ids", []))
            for field in ("reasons", "risks"):
                for claim in item.get(field, []):
                    if not isinstance(claim, dict):
                        raise ValueError("Screening reason/risk must be an object")
                    citations.extend(claim.get("evidence_ids", []))
            if not set(citations) <= allowed_original[item["symbol"]]:
                raise ValueError("Screening cites unavailable evidence")
        _normalize_redundant_evidence_ids(document)
        _sanitize_evidence_citations(document, bundles)
        _enforce_selection_quality_gate(document)
        _neutralize_forbidden_screening_terms(document)
        response = ModelScreeningResponse.model_validate(document)
        if response.analysis_id != analysis_id:
            raise ValueError("Screening analysis identity mismatch")
        allowed = {b["symbol"]: set(b["allowed_evidence_ids"]) for b in context["evidence_bundles"]}
        for item in response.assessments:
            if not set(item.evidence_ids) <= allowed[item.symbol]:
                raise ValueError("Screening cites unavailable evidence")
        selected = [
            item.symbol
            for item in sorted(response.assessments, key=lambda a: a.selection_rank or 99)
            if item.selection_rank is not None
        ]
        self.store.event(
            "SCREENING_RESULT",
            {"cycle_id": cid, "selected": selected, "response": response.model_dump(mode="json")},
        )
        by_symbol = {c.symbol: c for c in pool}
        return [by_symbol[symbol].model_dump(mode="json") for symbol in selected]
