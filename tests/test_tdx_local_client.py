"""本地通达信数据源：用构造的文件测试，不依赖本机有没有通达信。"""

from __future__ import annotations

import struct
from datetime import date

import numpy as np
import pytest

from modules import tdx_local_client as tdx
from modules.core.errors import ZettarancError
from modules.tdx_local_client import (
    TdxLocalClient,
    board_of,
    business_days_behind,
    is_st_name,
    ratio_qfq_factors,
    tdx_qfq_coefficients,
    to_symbol,
    to_ts_code,
)


def _write_day(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for d, o, h, low, c, amount, vol in rows:
            f.write(struct.pack("<IIIIIfII", d, int(o * 100), int(h * 100), int(low * 100), int(c * 100), amount, vol, 0))


def _write_tnf(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(b"\x00" * 50)
        for code, name in rows:
            record = bytearray(360)
            record[0:6] = code.encode("ascii")
            encoded = name.encode("gbk")
            record[31 : 31 + len(encoded)] = encoded
            f.write(record)


@pytest.fixture
def tdx_root(tmp_path, monkeypatch):
    root = tmp_path / "tdx"
    bars = [(20260101 + i, 10.0, 10.5, 9.5, 10.0 + i * 0.1, 1_000_000.0, 100_000) for i in range(5)]
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh600519.day", bars)
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh600000.day", bars[:2])  # 没有新行情
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh000001.day", bars)  # 上证指数
    _write_day(root / "vipdoc" / "sz" / "lday" / "sz000001.day", bars)
    _write_day(root / "vipdoc" / "sz" / "lday" / "sz399001.day", bars)
    _write_day(root / "vipdoc" / "sz" / "lday" / "sz000002.day", bars)
    (root / "vipdoc" / "bj" / "lday").mkdir(parents=True)
    hq = root / "T0002" / "hq_cache"
    _write_tnf(hq / "shs.tnf", [("000001", "上证指数"), ("600519", "贵州茅台"), ("600000", "浦发银行")])
    _write_tnf(hq / "szs.tnf", [("000001", "平安银行"), ("000002", "*ST万科")])
    _write_tnf(hq / "bjs.tnf", [])
    (hq / "gbbq").write_bytes(b"x")
    monkeypatch.setattr(tdx, "_cached_events", lambda *a: {"sh600519": np.array([[20260104, 5.0, 0.0, 0.0, 0.0]])})
    monkeypatch.setenv("TDX_DIR", str(root))
    return root


def test_symbol_conversion_keeps_market():
    """六位代码不唯一：sh000001 是指数，sz000001 是平安银行"""
    assert to_symbol("000001.SH") == "sh000001"
    assert to_symbol("000001.SZ") == "sz000001"
    assert to_ts_code("sz000001") == "000001.SZ"


@pytest.mark.parametrize(
    ("symbol", "board"),
    [
        ("sh600519", "主板"),
        ("sh688981", "科创板"),
        ("sz000001", "主板"),
        ("sz300750", "创业板"),
        ("bj920992", "北交所"),
        ("sh000001", None),
        ("sz399001", None),
        ("sh510300", None),
    ],
)
def test_board_of(symbol, board):
    assert board_of(symbol) == board


def test_tdx_qfq_is_affine_not_proportional():
    """通达信口径：P' = (P - 派现/10) / (1 + 送转/10)，派现是减法"""
    dates = np.array([20260101, 20260102, 20260103])
    events = np.array([[20260103, 10.0, 0.0, 10.0, 0.0]])  # 10 派 10 送 10
    a, b = tdx_qfq_coefficients(dates, events)
    # 除权日之前：(P - 1) / 2；除权日当天及之后不变
    assert a.tolist() == [0.5, 0.5, 1.0]
    assert b.tolist() == [-0.5, -0.5, 0.0]
    assert a[0] * 21 + b[0] == pytest.approx(10.0)


def test_tdx_qfq_compounds_events_in_order():
    """多次除权逐次复合：先算早的事件，再算晚的事件"""
    dates = np.array([20260101, 20260102, 20260103])
    events = np.array([[20260103, 5.0, 0.0, 0.0, 0.0], [20260102, 10.0, 0.0, 0.0, 0.0]])
    a, b = tdx_qfq_coefficients(dates, events)
    # 第一根：先减 1 元（20260102 事件），再减 0.5 元（20260103 事件）
    assert a[0] * 100 + b[0] == pytest.approx(98.5)
    assert a[1] * 100 + b[1] == pytest.approx(99.5)
    assert a[2] * 100 + b[2] == pytest.approx(100.0)


def test_future_event_not_applied():
    """已公告未除权的事件不能生效，否则全部历史都会偏低"""
    dates = np.array([20260101, 20260102])
    a, b = tdx_qfq_coefficients(dates, np.array([[20260601, 9.0, 0.0, 0.0, 0.0]]))
    assert a.tolist() == [1.0, 1.0]
    assert b.tolist() == [0.0, 0.0]
    assert ratio_qfq_factors(dates, np.array([10.0, 10.0]), np.array([[20260601, 9.0, 0.0, 0.0, 0.0]])).tolist() == [1.0, 1.0]


def test_rights_issue_in_formula():
    """配股：c = (派现 - 配股数 × 配股价) / 10，m 同时含送转和配股"""
    dates = np.array([20260101, 20260102])
    a, b = tdx_qfq_coefficients(dates, np.array([[20260102, 0.0, 5.0, 0.0, 2.0]]))  # 10 配 2，配股价 5
    assert a[0] * 11 + b[0] == pytest.approx((11 + 1.0) / 1.2)


def test_ex_date_on_suspended_day_falls_to_next_bar():
    """除权日停牌：落到其后第一根 K 线，之前的都要复权"""
    dates = np.array([20260101, 20260105])
    a, b = tdx_qfq_coefficients(dates, np.array([[20260103, 10.0, 0.0, 0.0, 0.0]]))
    assert b.tolist() == [-1.0, 0.0]


def test_ratio_factor_keeps_daily_return():
    """等比口径保持除权日以外的真实涨跌幅，用来算回测收益"""
    dates = np.array([20260101, 20260102, 20260103])
    closes = np.array([10.0, 11.0, 10.0])
    f = ratio_qfq_factors(dates, closes, np.array([[20260103, 10.0, 0.0, 0.0, 0.0]]))
    adj = closes * f
    assert adj[1] / adj[0] == pytest.approx(1.1)
    assert f[2] == 1.0


def test_day_file_with_bad_length_returns_none(tdx_root):
    path = tdx_root / "vipdoc" / "sh" / "lday" / "sh600001.day"
    path.write_bytes(b"\x00" * 33)
    assert TdxLocalClient(tdx_root).read_raw_bars("600001.SH") is None


def test_invalid_records_are_dropped(tdx_root):
    path = tdx_root / "vipdoc" / "sh" / "lday" / "sh600002.day"
    _write_day(path, [(20260101, 10.0, 9.0, 11.0, 10.0, 1.0, 1), (20260102, 10.0, 10.5, 9.5, 10.0, 1.0, 1)])
    bars = TdxLocalClient(tdx_root).read_raw_bars("600002.SH")
    assert bars["date"].tolist() == [20260102]


def test_missing_file_returns_empty(tdx_root):
    assert TdxLocalClient(tdx_root).get_kline_dicts("600003.SH") == []


def test_kline_units_match_tushare(tdx_root):
    rows = TdxLocalClient(tdx_root).get_kline_dicts("000001.SZ", days=0)
    assert rows[0]["vol"] == 1000.0  # 手
    assert rows[0]["amount"] == 1000.0  # 千元
    assert rows[0]["trade_date"] == "20260101"


def test_display_and_return_basis_differ_only_with_dividend(tdx_root):
    client = TdxLocalClient(tdx_root)
    display = client.get_kline_dicts("600519.SH", days=0)
    ratio = client.get_kline_dicts("600519.SH", days=0, basis="return")
    assert display[-1]["close"] == ratio[-1]["close"]
    assert display[0]["close"] == pytest.approx(10.0 - 0.5)  # 10 派 5，减 0.5 元
    assert ratio[0]["close"] == pytest.approx(10.0 * (10.2 - 0.5) / 10.2, abs=1e-3)
    assert client.get_kline_dicts("000001.SZ", days=0)[0]["close"] == 10.0


def test_today_overlay_appends_newer_bar_in_local_units(tdx_root):
    """实时行情补的当天一根：比本地新才拼上，单位换成手 / 千元，复权口径照常；清掉后恢复"""
    client = TdxLocalClient(tdx_root)
    bar = {"trade_date": "20260106", "open": 10.4, "high": 10.9, "low": 10.3, "close": 10.8,
           "volume_shares": 200_000.0, "amount_yuan": 2_000_000.0}
    tdx.set_today_overlay({"600519": bar, "000001.SZ": {**bar, "trade_date": "20260105"}})
    try:
        rows = client.get_kline_dicts("600519.SH", days=0)
        assert [r["trade_date"] for r in rows][-2:] == ["20260105", "20260106"]
        assert rows[-1]["close"] == 10.8 and rows[-1]["vol"] == 2000.0 and rows[-1]["amount"] == 2000.0
        assert rows[-1]["pct_chg"] == pytest.approx((10.8 / 10.4 - 1) * 100, abs=1e-3)
        # 日期不比本地新的不拼
        assert client.get_kline_dicts("000001.SZ", days=0)[-1]["trade_date"] == "20260105"
    finally:
        tdx.clear_today_overlay()
    assert client.get_kline_dicts("600519.SH", days=0)[-1]["trade_date"] == "20260105"


def test_days_and_date_range(tdx_root):
    client = TdxLocalClient(tdx_root)
    assert [r["trade_date"] for r in client.get_kline_dicts("000001.SZ", days=2)] == ["20260104", "20260105"]
    assert [r["trade_date"] for r in client.get_kline_dicts("000001.SZ", days=2, start_date="20260102", end_date="20260103")] == ["20260102", "20260103"]


def test_stock_list_excludes_index_and_keeps_market(tdx_root):
    stocks = {s["ts_code"]: s for s in TdxLocalClient(tdx_root).get_stock_list()}
    assert set(stocks) == {"600519.SH", "600000.SH", "000001.SZ", "000002.SZ"}
    assert stocks["000001.SZ"]["name"] == "平安银行"
    assert stocks["000002.SZ"]["is_st"] is True
    assert stocks["600000.SH"]["last_date"] == "20260102"
    assert TdxLocalClient(tdx_root).stock_name("000001.SZ") == "平安银行"


def test_select_tradable_removes_stale_and_st(tdx_root):
    from modules.screener.data import select_tradable

    tradable, meta = select_tradable(TdxLocalClient(tdx_root).get_stock_list())
    assert {s["ts_code"] for s in tradable} == {"600519.SH", "000001.SZ"}
    assert meta == {"as_of": "20260105", "universe": 4, "skipped_stale": 1, "skipped_st": 1}
    with_st, _ = select_tradable(TdxLocalClient(tdx_root).get_stock_list(), include_st=True)
    assert "000002.SZ" in {s["ts_code"] for s in with_st}


def test_read_paths_use_local_when_configured(tdx_root):
    """配置 TDX_DIR 后，指标层、战法层和组合数据源读的都是本地文件"""
    from modules.datasource import CompositeDataSource, get_datasource
    from modules.datasource_resolution import resolve_provider
    from modules.indicators import get_kline_data
    from modules.strategies.core import get_kline_data as strategy_klines

    assert resolve_provider().provider == "tdx"
    assert get_kline_data("000001.SZ", 3)[-1].trade_date == "20260105"
    assert strategy_klines("000001.SZ", 3)[-1]["trade_date"] == "20260105"
    assert CompositeDataSource("auto").get_kline_dicts("000001.SZ", days=3)[-1]["trade_date"] == "20260105"
    assert get_datasource("tdx").name == "tdx-local"


def test_not_configured_falls_back(monkeypatch):
    from modules.datasource_resolution import resolve_provider
    from modules.tdx_local_client import local_kline_rows, local_stock_name

    monkeypatch.delenv("TDX_DIR", raising=False)
    assert local_kline_rows("000001.SZ") is None
    assert local_stock_name("000001.SZ") is None
    assert resolve_provider().provider != "tdx"
    with pytest.raises(ZettarancError):
        TdxLocalClient()


def test_wrong_directory_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DIR", str(tmp_path))
    assert tdx.configured_root() is None


def test_business_days_behind_skips_weekends():
    # 2026-10-30 是周五，2026-10-31 是周六
    assert business_days_behind("20261030", date(2026, 10, 31)) == 0
    assert business_days_behind("20261029", date(2026, 10, 31)) == 1
    assert business_days_behind("20261030", date(2026, 11, 2)) == 1


@pytest.mark.parametrize(("name", "expected"), [("*ST万科", True), ("ST中装", True), ("贵州茅台", False), ("退市海润", True), ("华夏幸福退", True), ("", False), ("Stock1", False)])
def test_is_st_name(name, expected):
    assert is_st_name(name) is expected


def test_data_status_warns_when_stale(monkeypatch):
    from modules.cli_commands._helpers import data_status

    monkeypatch.delenv("TDX_DIR", raising=False)
    stale = data_status("20200102")
    assert stale["as_of"] == "20200102"
    assert stale["business_days_behind"] > 100
    assert stale["warnings"]
    assert data_status(None)["warnings"]


def test_anthropic_key_not_sent_to_default_provider(monkeypatch):
    from modules.llm_providers import MiniMaxProvider

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ZettarancError):
        MiniMaxProvider()


def test_write_env_file_keeps_other_settings(tmp_path, monkeypatch):
    from modules.setup_wizard import write_env_file

    path = tmp_path / ".env"
    path.write_text("# 注释\nDATA_MODE=websearch\nTUSHARE_API_URL=https://x\nLLM_API_KEY=keep\nTDX_DIR=D:/tdx\n", encoding="utf-8")
    write_env_file(token="abc", mode="jnb", env_path=path)
    text = path.read_text(encoding="utf-8")
    assert "DATA_MODE=jnb" in text and "DATA_MODE=websearch" not in text
    assert "TUSHARE_TOKEN=abc" in text
    assert "TUSHARE_API_URL=https://x" in text
    assert "LLM_API_KEY=keep" in text
    assert "TDX_DIR=D:/tdx" in text
    assert "# 注释" in text


def test_st_limit_changes_on_rule_date():
    from modules.simulator.execution_constraints import _limit_pct

    assert _limit_pct("600000.SH", True, "20260703") == 0.05
    assert _limit_pct("600000.SH", True, "20260706") == 0.10
    assert _limit_pct("300001.SZ", True, "20260101") == 0.20
    assert _limit_pct("688001.SH", True, "20260101") == 0.20
    assert _limit_pct("600000.SH", False, "20260101") == 0.10


def test_events_cache_round_trip(tmp_path):
    """gbbq 缓存不用 pickle，读回来要和写入的一致"""
    events = {
        "sh600519": np.array([[20260101, 1.0, 0.0, 0.0, 0.0], [20260601, 2.0, 0.0, 0.0, 0.0]]),
        "sz000001": np.array([[20260301, 3.0, 0.0, 1.0, 0.0]]),
    }
    path = tmp_path / "gbbq.npz"
    tdx._write_events_cache(path, events)
    loaded = tdx._read_events_cache(path)
    assert set(loaded) == set(events)
    for key, value in events.items():
        assert loaded[key].tolist() == value.tolist()


def test_verify_universe_is_random_not_first_codes(tdx_root):
    """验收股票池不能是按代码排序的前 N 只，也不按当前状态过滤"""
    from modules.verify.cli import select_verify_universe

    codes = select_verify_universe(3)
    assert len(codes) == 3
    assert select_verify_universe(3) == codes  # 固定种子可复现
    assert set(select_verify_universe(100)) == {"600519.SH", "600000.SH", "000001.SZ", "000002.SZ"}


def test_random_baseline_detects_no_edge():
    """随机入场对照：策略收益等于市场漂移时，差应接近 0；收益明显更好时差为正"""
    from types import SimpleNamespace

    from modules.verify.baseline import random_entry_baseline

    k = [SimpleNamespace(trade_date=str(20260000 + i), open=100.0) for i in range(300)]
    good = [SimpleNamespace(ts_code=c, exit_reason="止损", pnl_pct=5.0, entry_date=k[150].trade_date, exit_date=k[155].trade_date) for c in ("A", "B", "C")]
    flat = [SimpleNamespace(ts_code=c, exit_reason="止损", pnl_pct=0.0, entry_date=k[150].trade_date, exit_date=k[155].trade_date) for c in ("A", "B", "C")]
    result = random_entry_baseline({"A": k, "B": k, "C": k}, good, buy_cost=0, sell_cost=0)
    assert result.edge_avg_pct == pytest.approx(5.0)
    assert random_entry_baseline({"A": k, "B": k, "C": k}, flat, buy_cost=0, sell_cost=0).edge_avg_pct == pytest.approx(0.0)


def test_float_shares_on_uses_share_at_that_date():
    """换手率要用当时的流通股本：第一次变动前用变动前的，之后用最近一次变动后的"""
    import numpy as np

    from modules.tdx_local_client import float_shares_on

    events = np.array([[20240110.0, 1000.0, 1500.0], [20240301.0, 1500.0, 3000.0]])
    dates = np.array([20240102, 20240110, 20240228, 20240301, 20240501])
    assert float_shares_on(dates, events).tolist() == [1000.0, 1500.0, 1500.0, 3000.0, 3000.0]
    assert float_shares_on(dates, None) is None
    assert float_shares_on(dates, np.empty((0, 3))) is None


def test_to_symbol_infers_exchange_for_bare_codes():
    """不带交易所后缀的代码按代码段推断"""
    from modules.tdx_local_client import to_symbol

    assert to_symbol("600519.SH") == "sh600519"
    assert to_symbol("600519") == "sh600519"
    assert to_symbol("000001") == "sz000001"
    assert to_symbol("300750") == "sz300750"
    assert to_symbol("688981") == "sh688981"
    assert to_symbol("920007") == "bj920007"
    assert to_symbol("430047") == "bj430047"


def test_db_path_falls_back_when_directory_is_not_writable(tmp_path, monkeypatch):
    """数据库目录不可写时改用临时目录，可写时不变"""
    import modules.database as database

    monkeypatch.setenv("DB_PATH", str(tmp_path / "stock.db"))
    assert database.get_db_path() == (tmp_path / "stock.db").resolve()

    monkeypatch.setattr(database, "_dir_is_writable", lambda path: False)
    fallback = database.get_db_path()
    assert fallback.name == "stock.db"
    assert fallback.parent.name == "zettaranc"
    assert fallback.parent != tmp_path


def _etf_record(day, price, volume, amount):
    """ETF 记录：价格 ×1000 存"""
    p = int(round(price * 1000))
    return (day, p, p + 5, p - 5, p, amount, volume, 0)


def test_etf_prices_use_thousandths_and_volume_unit_is_detected_per_bar():
    """ETF 价格 ×1000；成交量一般是股，溢出时按手存，用 成交额÷成交量 落在高低价之间判断"""
    bars = np.array([
        _etf_record(20261102, 0.795, 4_099_290_100 // 100, 3.26e9),  # 按手存：3.26e9 / 40992901 / 100 ≈ 0.795
        _etf_record(20261103, 0.754, 2_000_000, 1_508_000.0),           # 按股存
        _etf_record(20261104, 0.760, 3_000_000, 9_999_999_999.0),       # 两种都不像：丢掉
    ], dtype=tdx._DAY_DTYPE)
    out = tdx.decode_bars(bars, etf=True)
    assert out["date"].tolist() == [20261102, 20261103]
    assert out["close"].tolist() == pytest.approx([0.795, 0.754])
    assert out["volume_shares"].tolist() == [4_099_290_100, 2_000_000]


def test_etf_history_starts_after_last_share_split():
    """gbbq 不记 ETF 拆分：收盘跳变超过 25% 之前的历史不要（588200 在 2026-07-21 从 3.539 到 1.349）"""
    prices = [3.50, 3.539, 1.349, 1.36, 1.37]
    bars = np.array([_etf_record(20260716 + i, p, 1_000_000, p * 1_000_000) for i, p in enumerate(prices)],
                    dtype=tdx._DAY_DTYPE)
    out = tdx.decode_bars(bars, etf=True)
    assert out["close"].tolist() == pytest.approx([1.349, 1.36, 1.37])
    assert tdx.decode_bars(bars, etf=False)["close"][0] == pytest.approx(35.0)  # 个股口径不截断


def test_is_etf_by_code_range():
    assert tdx.is_etf("sh588200") and tdx.is_etf("sh510300") and tdx.is_etf("sz159915")
    assert not tdx.is_etf("sh600795") and not tdx.is_etf("sz000001")
