from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analysis_core.config import Settings
from analysis_core.screening import Screening
from analysis_core.store import Store
from analysis_core.vendor.signal.domain.models import EvidenceBundle


class Candidate:
    def __init__(self, index):
        self.symbol, self.rank = f"C{index}USDT", index + 1

    def model_dump(self, **kwargs):
        return {"symbol": self.symbol, "rank": self.rank}


def bundle(symbol):
    now = datetime.now(UTC).isoformat()
    eid = symbol + ".PA.5M"
    return EvidenceBundle.model_validate(
        {
            "symbol": symbol,
            "generated_at": now,
            "source_snapshot_sha256": "a" * 64,
            "canonical_last": {
                "symbol": symbol,
                "price_type": "LAST",
                "value": "100",
                "timestamp": now,
            },
            "canonical_mark": {
                "symbol": symbol,
                "price_type": "MARK",
                "value": "100",
                "timestamp": now,
            },
            "evidence_items": [
                {
                    "evidence_id": eid,
                    "category": "price_action",
                    "source": "BYBIT",
                    "observed_at": now,
                    "summary": "已完成结构证据",
                    "values": {},
                }
            ],
            "tool_assessments": [
                {
                    "tool": "PUBLIC",
                    "status": "AVAILABLE",
                    "reason": "已采集行情",
                    "evidence_ids": [eid],
                }
            ],
        }
    )


class Collector:
    async def collect_many(self, symbols):
        self.symbols = symbols
        return {
            s: SimpleNamespace(candles={"5m": [None] * (1 if s == "C0USDT" else 50)})
            for s in symbols
        }, {}


class Builder:
    def build(self, snapshot, candidate):
        return bundle(candidate.symbol)


class Runner:
    def __init__(self, count=2, mutate=None):
        self.count, self.mutate, self.calls = count, mutate, 0

    async def request(self, sid, context, schema, *args, **kwargs):
        self.calls += 1
        self.context = context
        symbols = context["candidate_symbols_in_order"]
        selected = symbols[-self.count :] if self.count else []
        assessments = []
        for symbol in symbols:
            eid = symbol + ".PA.5M"
            assessments.append(
                {
                    "symbol": symbol,
                    "status": "SELECTED" if symbol in selected else "NOT_SELECTED",
                    "selection_rank": selected.index(symbol) + 1 if symbol in selected else None,
                    "value": "MEDIUM" if symbol in selected else "UNDETERMINED",
                    "direction": "LONG" if symbol in selected else "SKIP",
                    "value_summary": "结构质量横向比较",
                    "ranking_rationale": "结构清晰且参与稳定",
                    "reasons": [
                        {"category": c, "statement": "公开结构证据支持复核", "evidence_ids": [eid]}
                        for c in ("STRUCTURE_CLARITY", "PARTICIPATION")
                    ],
                    "risks": [
                        {
                            "category": "CHOPPY",
                            "severity": "LOW",
                            "statement": "存在局部反复风险",
                            "evidence_ids": [eid],
                        }
                    ],
                    "uncertainties": [],
                    "evidence_ids": [eid],
                }
            )
        document = {
            "schema_version": 2,
            "contract_kind": "MARKET_SCREENING",
            "mode": "SCHEDULED",
            "analysis_id": context["analysis_id"],
            "selection_summary": "十币小长线相对比较，至少一个明确方向",
            "assessments": assessments,
        }
        if self.mutate:
            self.mutate(document)
        return document


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_ten_candidate_relative_ranking_selects_one_to_three(tmp_path, count):
    store = Store(tmp_path / "test.db")
    runner, collector = Runner(count), Collector()
    screening = Screening(
        Settings(_env_file=None), store, runner=runner, collector=collector, builder=Builder()
    )
    result = await screening.select("cycle", [Candidate(i) for i in range(20)])
    assert len(collector.symbols) == 10
    assert runner.calls == 1
    assert runner.context["candidate_symbols_in_order"] == [f"C{i}USDT" for i in range(1, 10)] + [
        "C0USDT"
    ]
    assert [c["symbol"] for c in result] == (runner.context["candidate_symbols_in_order"][-count:])
    assert len(store.rows("SELECT * FROM events WHERE kind='SCREENING_RESULT'")) == 1


async def test_insufficient_ten_bundles_never_calls_model(tmp_path):
    runner = Runner()
    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=runner,
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="不足"):
        await screening.select("cycle", [Candidate(i) for i in range(5)])
    assert runner.calls == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(analysis_id="wrong-analysis"),
        lambda d: d["assessments"][0].update(symbol="OUTSIDEUSDT"),
        lambda d: d["assessments"][-1].update(selection_rank=3),
    ],
)
async def test_invalid_screening_is_rejected(tmp_path, mutation):
    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(mutate=mutation),
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError):
        await screening.select("cycle", [Candidate(i) for i in range(10)])


async def test_four_selections_rejected_not_truncated(tmp_path):
    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(4),
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="limit"):
        await screening.select("cycle", [Candidate(i) for i in range(10)])


async def test_relative_selection_preserves_disclosed_risk_instead_of_silent_removal(tmp_path):
    def mutate(d):
        d["assessments"][-1]["risks"][0].update(category="THIN_LIQUIDITY", severity="HIGH")

    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(mutate=mutate),
        collector=Collector(),
        builder=Builder(),
    )
    result = await screening.select("cycle", [Candidate(i) for i in range(10)])
    assert [c["symbol"] for c in result] == ["C9USDT", "C0USDT"]


async def test_invalid_nested_citation_rejected_before_normalization(tmp_path):
    def mutate(d):
        d["assessments"][-1]["reasons"][0]["evidence_ids"] = ["OUTSIDEUSDT.PA.5M"]

    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(mutate=mutate),
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="unavailable evidence"):
        await screening.select("cycle", [Candidate(i) for i in range(10)])


async def test_non_object_screening_reason_has_clear_validation_error(tmp_path):
    def mutate(document):
        document["assessments"][-1]["reasons"] = ["malformed"]

    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(mutate=mutate),
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="reason/risk must be an object"):
        await screening.select("cycle", [Candidate(i) for i in range(10)])


async def test_duplicate_input_candidates_are_rejected_before_collect_or_model(tmp_path):
    runner, collector = Runner(), Collector()
    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=runner,
        collector=collector,
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="Duplicate screening"):
        await screening.select("duplicate", [Candidate(i) for i in range(5)] + [Candidate(0)])
    assert runner.calls == 0 and not hasattr(collector, "symbols")


@pytest.mark.parametrize("count", [0, 4])
async def test_empty_or_excess_selection_is_contract_failure(tmp_path, count):
    runner = Runner(count)
    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=runner,
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="limit"):
        await screening.select("cycle", [Candidate(i) for i in range(10)])
    assert runner.calls == 1


async def test_best_candidate_can_honestly_be_low_confidence(tmp_path):
    def mutate(doc):
        for a in doc["assessments"]:
            if a["status"] == "SELECTED":
                a.update(value="LOW", value_summary="十币相对最好但绝对置信度偏低")

    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(1, mutate),
        collector=Collector(),
        builder=Builder(),
    )
    selected = await screening.select("cycle", [Candidate(i) for i in range(10)])
    assert len(selected) == 1 and selected[0]["relative_confidence"] == "LOW"
    assert selected[0]["selection_rank"] == 1 and selected[0]["screening_direction"] == "LONG"


async def test_selected_skip_is_rejected_without_invented_direction(tmp_path):
    def mutate(doc):
        doc["assessments"][-1]["direction"] = "SKIP"

    screening = Screening(
        Settings(_env_file=None),
        Store(tmp_path / "db"),
        runner=Runner(1, mutate),
        collector=Collector(),
        builder=Builder(),
    )
    with pytest.raises(ValueError, match="requires rank, direction"):
        await screening.select("cycle", [Candidate(i) for i in range(10)])
