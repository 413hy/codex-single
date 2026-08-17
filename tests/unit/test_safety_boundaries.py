from pathlib import Path

import pytest

PRODUCTION_ROOT = Path("src/bybit_signal")


@pytest.mark.parametrize(
    "forbidden",
    (
        "remove_keyboard",
        "ReplyKeyboardRemove",
        "/v5/order/",
        "/v5/position/",
        "/v5/account/",
        "api-demo.bybit.com",
        "stream-demo.bybit.com",
    ),
)
def test_production_source_has_no_keyboard_removal_or_private_trade_endpoint(
    forbidden: str,
) -> None:
    matches = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if forbidden in path.read_text(encoding="utf-8"):
            matches.append(str(path))
    assert not matches, f"forbidden production capability {forbidden!r}: {matches}"
