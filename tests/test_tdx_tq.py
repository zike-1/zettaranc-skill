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
