from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from bybit_signal.notifications.keyboards import (
    back_to_cycle_keyboard,
    cycle_details_keyboard,
    details_keyboard,
    main_reply_keyboard,
)


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {key for child in value for key in _all_keys(child)}
    return set()


def test_main_reply_keyboard_has_required_collapsible_flags() -> None:
    keyboard = main_reply_keyboard()
    assert keyboard["resize_keyboard"] is True
    assert keyboard["is_persistent"] is False
    assert keyboard["one_time_keyboard"] is False
    assert "remove_keyboard" not in _all_keys(keyboard)


def test_details_keyboard_callback_is_bounded() -> None:
    keyboard = details_keyboard("cycle_20260817_1530", "CYSUSDT")
    callback = keyboard["inline_keyboard"][0][0]["callback_data"]
    assert len(callback.encode("utf-8")) <= 64
    assert "remove_keyboard" not in _all_keys(keyboard)


def test_details_keyboard_rejects_oversized_callback() -> None:
    with pytest.raises(ValueError, match="1-64"):
        details_keyboard("x" * 60, "CYSUSDT")


def test_cycle_details_and_back_keyboard_form_a_bounded_round_trip() -> None:
    cycle = cycle_details_keyboard("analysis_01", ("CYSUSDT", "GPSUSDT"))
    back = back_to_cycle_keyboard("analysis_01")

    assert len(cycle["inline_keyboard"]) == 2
    assert cycle["inline_keyboard"][0][0]["callback_data"] == (
        "detail:analysis_01:CYSUSDT"
    )
    assert back["inline_keyboard"][0][0]["callback_data"] == "back:analysis_01"
    assert "remove_keyboard" not in _all_keys((cycle, back))
