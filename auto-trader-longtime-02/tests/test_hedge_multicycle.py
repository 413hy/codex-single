"""Longer lifecycle sequences exercise state carried across multiple generations."""

import json
from decimal import Decimal as D

import pytest
from test_hedge_strategy import hedge as hedge_fixture

from longtime.hedge import HedgeExecutor, distance_price
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
        bid = D(90 + generation * 2)
        ask = bid + 1

        async def review_quote(symbol, *, review_bid=bid, review_ask=ask):
            return review_bid, review_ask

        ex.quote = review_quote
        result = await e.hedge.decide(
            g["group_id"],
            f"review-{generation}",
            Decision(symbol=g["symbol"], decision=side, reason="test"),
            expected_generation=generation,
        )
        assert result == "HEDGE_DIRECTION_APPLIED"
        current = e.hedge.get(g["group_id"])
        assert current["generation"] == generation + 1 and current["phase"] == "SINGLE"
        active = store.trade(current["active"])
        assert active["side"] == side
        reference = ask if side == "LONG" else bid
        qty = D(active["qty"])
        tick = D(json.loads(active["details"])["tick"])
        expected_tp = distance_price(
            side, reference, qty, D(active["tp_target_net_pnl"]), tick, favorable=True
        )
        expected_hedge = distance_price(
            side, reference, qty, D(json.loads(active["details"])["sl_loss"]), tick,
            favorable=False,
        )
        assert D(current["reference"]) == reference
        assert D(current["tp"]) == D(active["tp_price"]) == expected_tp
        assert D(active["sl_price"]) == expected_hedge
        assert D(ex.submissions[count + 1]["price"]) == expected_tp
        assert D(ex.submissions[count + 2]["price"]) == expected_hedge
        count = len(ex.submissions)
        await e.hedge.decide(
            g["group_id"],
            f"review-{generation}",
            Decision(symbol=g["symbol"], decision=side, reason="test"),
            expected_generation=generation,
        )
        assert len(ex.submissions) == count
    assert not store.rows("SELECT * FROM orders WHERE kind='SL'")
