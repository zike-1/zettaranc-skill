"""补当天 K 线的行情来源回退：腾讯 → 同花顺 → 东财。字段映射按各家实测 / 官方文档口径。"""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from modules import a_stock_data_client
from modules.daily_selection_report import fill_today_bars
from modules.hithink_client import HithinkFinanceClient, snapshot_session_date


def _quote(day="20261103", price=5.35, last=5.27, lots=1414253):
    return {"quote_time": f"{day}161153", "price": price, "last_close": last, "open": 5.27, "high": 5.37,
            "low": 5.21, "volume_lots": lots, "amount_wan": 75108.37}


def test_next_source_only_gets_codes_the_previous_one_failed_or_missed():
    local = {"600795": (5.27, "20261102"), "002623": (19.28, "20261102"), "600036": (39.0, "20261102")}
    calls = []

    def tencent(codes):
        calls.append(("腾讯", list(codes)))
        # 只返回两只：002623 停牌（不该再交给下一家），600036 没返回
        return {"600795": _quote(), "002623": {**_quote(price=19.28, last=19.28, lots=0)}}

    def east(codes):
        calls.append(("东财", list(codes)))
        return {"600036": _quote(price=39.5, last=39.0)}

    overlay, result = fill_today_bars(local, [("腾讯", tencent), ("东财", east)], pause=0)
    assert calls == [("腾讯", ["600795", "002623", "600036"]), ("东财", ["600036"])]
    assert set(overlay) == {"600795", "600036"}
    assert result.sources == {"腾讯": 1, "东财": 1}
    assert result.skipped == [("002623", "停牌或无成交（腾讯）")]
    assert result.trade_date == "20261103" and result.error == ""


def test_request_failure_hands_the_whole_rest_to_the_next_source():
    local = {"600795": (5.27, "20261102")}

    def broken(codes):
        raise OSError("timeout")

    overlay, result = fill_today_bars(local, [("腾讯", broken), ("东财", lambda c: {"600795": _quote()})], pause=0)
    assert list(overlay) == ["600795"] and result.sources == {"东财": 1}
    assert "腾讯：timeout" in result.error

    _, nothing = fill_today_bars(local, [("腾讯", broken)], pause=0)
    assert nothing.skipped == [("600795", "各行情来源都没有")]


def test_eastmoney_quote_maps_lots_yuan_and_unix_time():
    """f5 手、f6 元、f124 Unix 秒；停牌字段是 "-" """
    resp = MagicMock()
    resp.json.return_value = {"data": {"diff": [
        {"f12": "600795", "f2": 5.35, "f5": 1414253, "f6": 751083703.0, "f15": 5.37, "f16": 5.21, "f17": 5.27,
         "f18": 5.27, "f124": 1793693513},
        {"f12": "002623", "f2": "-", "f5": "-", "f6": "-", "f15": "-", "f16": "-", "f17": "-", "f18": 19.28,
         "f124": 1793691288},
    ]}}
    with patch.object(a_stock_data_client, "_em_get", return_value=resp) as get:
        q = a_stock_data_client.eastmoney_quote(["600795", "002623", "588200"])
    assert "1.600795,0.002623,1.588200" == get.call_args.kwargs["params"]["secids"]  # 沪市 ETF 走 1.
    assert q["600795"]["volume_lots"] == 1414253 and q["600795"]["amount_wan"] == 75108.3703
    assert q["600795"]["quote_time"].startswith("20261103")
    assert q["002623"]["price"] == 0 and q["002623"]["volume_lots"] == 0


def test_hithink_quote_bars_converts_shares_and_uses_batch_timestamp():
    """官方文档：volume 股、turnover 元、data.timestamp 毫秒"""
    client = HithinkFinanceClient(api_key="sk-test")
    client._trading_days_cache = ["20261102", "20261103", "20261104"]
    data = {"timestamp": 1793693513000, "item": [
        {"thscode": "600795.SH", "ticker": "600795", "volume": 141425300, "turnover": 751083703,
         "last_price": 5.35, "open_price": 5.27, "high_price": 5.37, "low_price": 5.21, "prev_price": 5.27},
    ]}
    with patch.object(client, "_get", return_value=data) as get:
        q = client.quote_bars(["600795"])
    assert get.call_args.args[1] == {"thscodes": "600795.SH"}
    assert q["600795"]["volume_lots"] == 1414253 and q["600795"]["quote_time"].startswith("20261103")
    assert HithinkFinanceClient(api_key="").quote_bars(["600795"]) == {}


def test_hithink_snapshot_time_is_mapped_to_its_trading_day():
    """快照时间是取数时间：11-04 早上 07:44 取到的是 11-03 的行情"""
    days = ["20261030", "20261102", "20261103", "20261104"]
    sh = ZoneInfo("Asia/Shanghai")
    assert snapshot_session_date(datetime(2026, 11, 4, 7, 44, tzinfo=sh), days) == "20261103"
    assert snapshot_session_date(datetime(2026, 11, 3, 16, 11, tzinfo=sh), days) == "20261103"
    assert snapshot_session_date(datetime(2026, 11, 3, 10, 30, tzinfo=sh), days) == ""  # 盘中不用
    assert snapshot_session_date(datetime(2026, 11, 1, 20, 0, tzinfo=sh), days) == "20261030"  # 周日
