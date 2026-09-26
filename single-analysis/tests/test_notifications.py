import time

from analysis_core.notifications import cycle_notice, signal_detail
from analysis_core.store import Store


def test_cycle_summary_is_bounded_and_shows_time_primary_and_hedge(tmp_path):
    s = Store(tmp_path / "db")
    s.claim_cycle("c")
    s.execute("update cycles set status='SUCCESS',completed_at=?", (time.time() + 125,))
    for i in range(20):
        sid = str(i)
        s.signal(sid, "c", f"C{i}USDT", {"direction_required": i == 0, "priority_review": True})
        s.signal_result(sid, "PUBLISHED", "LONG", {"reason": "中文证据" * 200})
    text = cycle_notice(s, "c")
    assert "北京时间" in text and "约2分钟" in text and "⭐ 首选 · 双仓" in text
    assert len(text.encode("utf-16-le")) // 2 < 4096


def test_detail_shows_tv_initial_direction_and_bybit_final_reason(tmp_path):
    store = Store(tmp_path / "db")
    store.claim_cycle("c")
    store.signal("one", "c", "BTCUSDT", {
        "tv_initial": {"direction": "SHORT", "confidence": "LOW",
                       "reason": "15m与1h均线偏空，网页值未确认收盘"},
    })
    store.signal_result("one", "PUBLISHED", "LONG", {"reason": "Bybit已收盘K线反转"})
    store.event("FINAL_SELECTION_RESULT", {"cycle_id": "c", "result": {"selected": [
        {"symbol": "BTCUSDT", "direction": "LONG", "confidence": "MEDIUM"},
    ]}})
    text, _ = signal_detail(store, "one")
    assert "TradingView 初判：做空 · LOW" in text
    assert "15m与1h均线偏空" in text
    assert "最终复核：做多 · MEDIUM" in text
    assert "TradingView＋Bybit 复核后的最终理由：Bybit已收盘K线反转" in text
