"""活跃市值卡片：有开高低收画 K 线，旧报告只有收盘价退回折线。"""

from modules.daily_report_html import _amv_kline


def _bar(d, o, h, l, c, s):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "state": s}


def test_ohlc_series_draws_candles_with_state_background():
    svg = _amv_kline([_bar("2026-11-03", 10, 11, 9.5, 10.5, "BEAR"), _bar("2026-11-04", 10.5, 11.2, 10.4, 11, "BULL")])
    assert 'class="kline"' in svg
    assert svg.count("<rect") == 4  # 两条背景 + 两根实体
    assert "var(--up-bg)" in svg and "var(--down-bg)" in svg


def test_close_only_series_falls_back_to_line():
    svg = _amv_kline([{"date": "2026-11-03", "close": 10, "state": "BEAR"}, {"date": "2026-11-04", "close": 11, "state": "BULL"}])
    assert 'class="kline"' not in svg and "<line" in svg
