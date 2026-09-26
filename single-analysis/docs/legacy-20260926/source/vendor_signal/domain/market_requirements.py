from __future__ import annotations

from typing import Final

# The longest mandatory indicator in a price-action evidence item is EMA50.
# Keep collectors and builders on this shared floor so a timeframe accepted by
# collection can always be transformed into evidence.
PRICE_ACTION_MINIMUM_CANDLES: Final = 50
