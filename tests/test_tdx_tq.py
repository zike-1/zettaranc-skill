"""通达信引擎选股的纯逻辑：结果解析、次新股剔除、什么时候能用通达信引擎。不连通达信。"""

from datetime import datetime

from modules.daily_selection_report import tq_usable_for
from modules.tdx_tq import TqSelection, drop_short_history, latest_date, parse_selection


def _result():
    return {
        "ErrorId": "0",
        "002181.SZ": {"选股": [{"Date": "20261103", "Value": "1"}]},
        "600557.SH": {"选股": [{"Date": "20261103", "Value": "0"}]},
        "002623.SZ": {"选股": [{"Date": "20261102", "Value": "1"}]},  # 停牌：最后一根早一天，不算
        "603468.SH": {"选股": [{"Date": "20261103", "Value": "1.00"}]},
        "000001.SZ": {"选股": []},
    }


def test_parse_selection_only_counts_hits_on_the_trade_day():
    sel = parse_selection(_result(), "20261103")
    assert sel.picks == ["002181.SZ", "603468.SH"]
    assert sel.scanned == 3 and sel.output == "选股"


def test_latest_date_is_the_newest_last_bar():
    assert latest_date(_result()) == "20261103"
    assert latest_date({"ErrorId": "0"}) == ""


def test_new_listings_without_enough_history_are_dropped():
    sel = TqSelection(formula="X", trade_date="20261103", picks=["002181.SZ", "603468.SH"])
    counts = {"002181.SZ": 1272, "603468.SH": 58}
    out = drop_short_history(sel, lambda code, day: counts.get(code, 0), min_bars=120)
    assert out.picks == ["002181.SZ"] and out.skipped_short == ["603468.SH"]


def test_tdx_engine_only_for_today_after_close():
    """公式里的 DYNAINFO 只反映当前行情：补算历史日期、盘中都不用通达信引擎"""
    assert tq_usable_for("20261104", datetime(2026, 11, 4, 20, 0))
    assert not tq_usable_for("20261104", datetime(2026, 11, 4, 10, 30))
    assert not tq_usable_for("20261103", datetime(2026, 11, 4, 20, 0))


def test_local_bar_count_only_counts_bars_up_to_the_trade_date(monkeypatch):
    import numpy as np

    import modules.tdx_local_client as tlc
    from modules.tdx_tq import local_bar_count

    class Client:
        def __init__(self, root): pass
        def raw_series(self, code): return {"date": np.array([20261030, 20261102, 20261103, 20261104])}

    monkeypatch.setattr(tlc, "configured_root", lambda: "x")
    monkeypatch.setattr(tlc, "TdxLocalClient", Client)
    assert local_bar_count("600795.SH", "20261102") == 2
    assert local_bar_count("600795.SH") == 4


class _FakeTq:
    """get_market_data 的替身：按 dividend_type 返回不复权 / 前复权两张表。"""

    def __init__(self, frames):
        self.frames = frames

    def get_market_data(self, field_list, stock_list, period, count, dividend_type):
        return self.frames[dividend_type]


def _frames(dates, raw_close, front_close, code="601988.SH"):
    import pandas as pd

    idx = pd.to_datetime(dates)
    col = lambda values: pd.DataFrame({code: values}, index=idx)  # noqa: E731
    raw = {f: col([1.0] * len(dates)) for f in ("Open", "High", "Low")}
    raw.update(Close=col(raw_close), Volume=col([325202688.0, 353461376.0]), Amount=col([221933.42, 237740.03]))
    return {"none": raw, "front": {"Close": col(front_close)}}


def test_tq_quote_gives_today_bar_in_tencent_units(monkeypatch):
    import modules.tdx_tq as tdx_tq

    fake = _FakeTq(_frames(["2026-11-04", "2026-11-05"], [6.83, 6.77], [6.83, 6.77]))
    monkeypatch.setattr(tdx_tq, "_client", lambda: fake)
    out = tdx_tq.tq_quote(["601988"], today="20261105")
    bar = out["601988"]
    assert bar["price"] == 6.77 and bar["last_close"] == 6.83
    assert bar["volume_lots"] == 3534613.76 and bar["amount_wan"] == 237740.03
    assert bar["quote_time"].startswith("20261105")


def test_tq_quote_leaves_stale_and_ex_dividend_days_to_other_sources(monkeypatch):
    import modules.tdx_tq as tdx_tq

    stale = _FakeTq(_frames(["2026-11-03", "2026-11-04"], [6.92, 6.83], [6.92, 6.83]))
    monkeypatch.setattr(tdx_tq, "_client", lambda: stale)
    assert tdx_tq.tq_quote(["601988"], today="20261105") == {}
    # 今天除权：前复权的昨收比不复权的低
    exdiv = _FakeTq(_frames(["2026-11-04", "2026-11-05"], [6.83, 6.60], [6.60, 6.60]))
    monkeypatch.setattr(tdx_tq, "_client", lambda: exdiv)
    assert tdx_tq.tq_quote(["601988"], today="20261105") == {}


def test_tq_unavailable_becomes_oserror_so_next_source_is_tried(monkeypatch):
    import pytest

    import modules.tdx_tq as tdx_tq
    from modules.daily_selection_report import fill_today_bars

    def boom():
        raise tdx_tq.TqUnavailable("通达信没有运行")

    monkeypatch.setattr(tdx_tq, "_client", boom)
    with pytest.raises(OSError):
        tdx_tq.tq_quote(["601988"], today="20261105")
    backup = {"601988": {"quote_time": "20261105150000", "price": 6.77, "last_close": 6.83, "open": 6.79,
                         "high": 6.81, "low": 6.66, "volume_lots": 3534614, "amount_wan": 237740}}
    overlay, result = fill_today_bars({"601988": (6.83, "20261104")},
                                      [("通达信", tdx_tq.tq_quote), ("腾讯", lambda codes: backup)], pause=0)
    assert overlay["601988"]["close"] == 6.77
    assert result.sources == {"腾讯": 1} and "通达信" in result.error
