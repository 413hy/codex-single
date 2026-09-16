import time

from analysis_core.notifications import cycle_notice
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
