"""Longer lifecycle sequences exercise state carried across multiple generations."""

import json

import pytest
from test_hedge_strategy import hedge as hedge_fixture

from longtime.hedge import HedgeExecutor
from longtime.model import Decision

hedge = hedge_fixture


@pytest.mark.parametrize("directions", [("LONG",) * 8, ("SHORT",) * 8, ("LONG", "SHORT") * 4])
async def test_eight_generations_restart_skip_and_duplicate(hedge, directions):
    e, store, ex, g, link = hedge
    for generation, side in enumerate(directions):
        g = e.hedge.get(g["group_id"])
        child = store.trade(g["child"])
        link = json.loads(child["details"])["entry_link"]
        ex.fill(link, ex.orders[link]["qty"])
        await e.hedge.tick()
        g = e.hedge.get(g["group_id"])
        assert g["phase"] == "LOCKED" and g["generation"] == generation
        count = len(ex.submissions)
        # Recreate execution object to exercise durable state, not only in-memory state.
        e = HedgeExecutor(e.settings, store, ex, e.markets)
        await e.hedge.tick()
        assert len(ex.submissions) == count
        assert (
            await e.hedge.decide(
                g["group_id"],
                f"skip-{generation}",
                Decision(symbol=g["symbol"], decision="SKIP", reason="test"),
            )
            == "SKIP_MODEL"
        )
        assert len(ex.submissions) == count
        result = await e.hedge.decide(
            g["group_id"],
            f"review-{generation}",
            Decision(symbol=g["symbol"], decision=side, reason="test"),
            expected_generation=generation,
        )
        assert result == "HEDGE_DIRECTION_APPLIED"
        current = e.hedge.get(g["group_id"])
        assert current["generation"] == generation + 1 and current["phase"] == "SINGLE"
        assert store.trade(current["active"])["side"] == side
        count = len(ex.submissions)
        await e.hedge.decide(
            g["group_id"],
            f"review-{generation}",
            Decision(symbol=g["symbol"], decision=side, reason="test"),
            expected_generation=generation,
        )
        assert len(ex.submissions) == count
    assert not store.rows("SELECT * FROM orders WHERE kind='SL'")
