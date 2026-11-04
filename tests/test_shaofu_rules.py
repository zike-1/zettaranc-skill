"""少妇战法规则：默认值与知识库一致，引擎行为符合 SOP 的时序。"""

from __future__ import annotations

import pytest

from modules.indicators import DailyData
from modules.loop_engine import LoopConfig, LoopTrade, ShaofuLoopEngine
from modules.strategies.shaofu_rules import SHAOFU_RULES, rule_value


def _kline(date: str, close: float, open_: float | None = None, high: float | None = None, low: float | None = None, code: str = "600519.SH", pct: float = 0.0) -> DailyData:
    o = close if open_ is None else open_
    return DailyData(
        ts_code=code,
        trade_date=date,
        open=o,
        high=high if high is not None else max(o, close) * 1.001,
        low=low if low is not None else min(o, close) * 0.999,
        close=close,
        vol=10000.0,
        amount=10000.0 * close / 10,
        pct_chg=pct,
        prev_close=close,
    )


def _flat(n: int, price: float = 100.0, code: str = "600519.SH") -> list[DailyData]:
    return [_kline(f"2026{i:04d}", price, code=code) for i in range(1, n + 1)]


def _no_cost(**kwargs) -> LoopConfig:
    return LoopConfig(commission_rate=0, stamp_duty_rate=0, transfer_fee_rate=0, slippage_pct=0, **kwargs)


def _open_trade(klines: list[DailyData], idx: int, entry: float = 100.0, stop: float = 90.0) -> LoopTrade:
    return LoopTrade(
        ts_code=klines[0].ts_code,
        entry_date=klines[idx].trade_date,
        entry_price=entry,
        entry_reason="测试",
        stop_loss_price=stop,
        position_pct=0.1,
        entry_index=idx,
    )


def test_defaults_come_from_rules():
    """LoopConfig 的默认值只能来自规则文件"""
    cfg = LoopConfig()
    for key in (
        "j_threshold",
        "stop_loss_pct",
        "stop_loss_method",
        "bbi_break_days",
        "bbi_break_threshold",
        "min_holding_days",
        "no_rise_days",
        "no_rise_grace_days",
        "lu_half",
        "big_yang_pct",
        "exit_line",
        "position_pct",
        "vol_shrink_threshold",
        "fourth_brick_half",
        "s1_exit",
        "didi_exit",
        "yellow_break_exit",
        "yellow_break_days",
    ):
        assert getattr(cfg, key) == rule_value(key), key


def test_knowledge_base_values():
    """这几个值是知识库明确写的，改动必须同时改出处"""
    assert rule_value("j_threshold") == -10
    assert rule_value("stop_loss_pct") == -0.02
    assert rule_value("exit_line") == "bbi"
    assert rule_value("bbi_break_days") == 2
    assert rule_value("bbi_break_threshold") == 0.0
    assert rule_value("big_yang_pct") == 4.0
    assert rule_value("vol_shrink_threshold") == 1.0
    assert rule_value("two_30_turnover_reject") == 40.0
    assert rule_value("two_30_top_n") == 3
    assert SHAOFU_RULES["j_threshold"].kind == "知识库"


def test_rule_sources_exist_in_knowledge_base():
    """本机有知识库时，每条出处的文件和行号都要存在"""
    import os
    import re
    from pathlib import Path

    root = Path(os.environ.get("ZK_WIKI_DIR", "") or "__no_zk_wiki__") / "01_markdown"
    if not root.is_dir():
        pytest.skip("本机没有知识库")
    for key, rule in SHAOFU_RULES.items():
        for path, lines in re.findall(r"((?:concepts|materials)/[^:；，（）]+?\.md)(?::([\d,\-]+))?", rule.source):
            file = root / path
            assert file.is_file(), f"{key}: {path}"
            total = len(file.read_text(encoding="utf-8").splitlines())
            for number in re.findall(r"\d+", lines):
                assert int(number) <= total, f"{key}: {path}:{number}"
        if rule.kind != "工程":
            assert re.search(r"(concepts|materials)/", rule.source), key


def test_entry_fills_at_next_open_after_signal(monkeypatch):
    """T 日收盘出信号，T+1 开盘买入；止损基于买入 K 线的最低价"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(40)
    klines[35] = _kline(klines[35].trade_date, 100.0, open_=98.0, low=97.0)
    monkeypatch.setattr(engine, "_check_entry_internal", lambda ks, i: {"reason": "B1"} if i == 34 else None)

    trade, completed = engine.process_day("600519.SH", klines, 35, None)

    assert completed is None
    assert trade.entry_date == klines[35].trade_date
    assert trade.entry_price == 98.0
    assert trade.stop_loss_price == pytest.approx(97.0 * 0.98)


def test_limit_up_open_cannot_buy(monkeypatch):
    """开盘涨停买不进去"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(40)
    klines[35] = _kline(klines[35].trade_date, 110.0, open_=110.0, low=110.0, high=110.0)
    monkeypatch.setattr(engine, "_check_entry_internal", lambda ks, i: {"reason": "B1"} if i == 34 else None)

    trade, _ = engine.process_day("600519.SH", klines, 35, None)

    assert trade is None


def test_limit_down_open_defers_exit():
    """开盘跌停卖不出，顺延到下一个可卖的开盘"""
    engine = ShaofuLoopEngine(_no_cost(no_rise_days=0, profit_to_loss_exit=False, dead_cross_exit=False, break_requires_reclaim=True))
    klines = _flat(45)
    trade = _open_trade(klines, 34, entry=100.0, stop=99.0)
    klines[35] = _kline(klines[35].trade_date, 95.0)  # 收盘跌破止损
    klines[36] = _kline(klines[36].trade_date, 85.5, open_=85.5, low=85.5, high=85.5)  # 跌停开盘
    klines[37] = _kline(klines[37].trade_date, 88.0, open_=87.0)

    trade, completed = engine.process_day("600519.SH", klines, 35, trade)
    assert completed is None
    trade, completed = engine.process_day("600519.SH", klines, 36, trade)
    assert completed is None and trade.pending_exit_reason == "止损"
    trade, completed = engine.process_day("600519.SH", klines, 37, trade)
    assert completed.exit_reason == "止损"
    assert completed.exit_date == klines[37].trade_date
    assert completed.exit_price == 87.0


def test_no_rise_exit_after_grace():
    """入场后 3+2 根 K 线收盘从未高于成本 → 没涨就走"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(45, price=99.5)
    trade = _open_trade(klines, 34, entry=100.0, stop=90.0)

    for day in range(35, 39):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None, f"第 {day} 天不该离场"
    trade, completed = engine.process_day("600519.SH", klines, 39, trade)
    assert completed.exit_reason == "没涨就走"
    assert completed.holding_days == 5


def test_profit_to_loss_exit():
    """收盘高于成本之后又跌回成本以下 → 盈转亏必走"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(45, price=101.0)
    trade = _open_trade(klines, 34, entry=100.0, stop=90.0)
    klines[35] = _kline(klines[35].trade_date, 99.5)

    trade, completed = engine.process_day("600519.SH", klines, 35, trade)
    assert completed is None
    trade, completed = engine.process_day("600519.SH", klines, 36, trade)
    assert completed.exit_reason == "盈转亏"


def test_stop_loss_has_no_protection_period():
    """跌穿止损直接走，没有保护期"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(45)
    trade = _open_trade(klines, 34, entry=100.0, stop=98.0)
    klines[34] = _kline(klines[34].trade_date, 97.0)

    trade, completed = engine.process_day("600519.SH", klines, 35, trade)
    assert completed.exit_reason == "止损"


def test_break_needs_reclaim_first():
    """B1 多在 BBI 下方买入：没站上过 BBI 时破位规则不生效，由止损和没涨就走负责"""
    engine = ShaofuLoopEngine(_no_cost(no_rise_days=0, profit_to_loss_exit=False, dead_cross_exit=False))
    klines = _flat(45, price=100.0)
    for i in range(34, 40):
        klines[i] = _kline(klines[i].trade_date, 95.0 - (i - 34) * 0.1)
    trade = _open_trade(klines, 34, entry=95.0, stop=80.0)

    for day in range(35, 41):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None
    assert trade.line_reclaimed is False

    # 关掉这个前提，同样的走势会被破位规则带走
    engine2 = ShaofuLoopEngine(_no_cost(no_rise_days=0, profit_to_loss_exit=False, dead_cross_exit=False, break_requires_reclaim=False))
    trade2 = _open_trade(klines, 34, entry=95.0, stop=80.0)
    completed2 = None
    for day in range(35, 41):
        trade2, completed2 = engine2.process_day("600519.SH", klines, day, trade2)
        if completed2:
            break
    assert completed2.exit_reason == "BBI跌破"


def test_half_exit_changes_pnl_once():
    """卤煮减半只发生一次，并真实计入收益"""
    engine = ShaofuLoopEngine(_no_cost(no_rise_days=0, profit_to_loss_exit=False, dead_cross_exit=False, fourth_brick_half=False))
    klines = _flat(45, price=100.0)
    trade = _open_trade(klines, 34, entry=100.0, stop=80.0)
    klines[35] = _kline(klines[35].trade_date, 105.0, open_=100.5, pct=5.0)
    klines[36] = _kline(klines[36].trade_date, 110.0, open_=105.5, pct=4.76)
    klines[37] = _kline(klines[37].trade_date, 116.0, open_=112.0, pct=5.45)
    klines[38] = _kline(klines[38].trade_date, 122.0, open_=117.0, pct=5.2)

    for day in range(35, 40):
        trade, _ = engine.process_day("600519.SH", klines, day, trade)

    assert len(trade.partial_exits) == 1
    assert trade.partial_exits[0]["price"] == 112.0
    assert trade.remaining_fraction == 0.5
    # 一半 112 卖出，一半 120 卖出：收益 (112+120)/2 / 100 - 1 = 16%
    assert engine.net_pnl_pct(trade, 120.0) == pytest.approx(16.0)


def _only_layer(**on) -> ShaofuLoopEngine:
    """只打开要测的那一层，其他离场和放飞都关掉。"""
    base = dict(
        no_rise_days=0,
        profit_to_loss_exit=False,
        dead_cross_exit=False,
        lu_half=False,
        fourth_brick_half=False,
        s1_exit=False,
        didi_exit=False,
        yellow_break_exit=False,
        bbi_break_days=99,
    )
    base.update(on)
    return ShaofuLoopEngine(_no_cost(**base))


def test_yellow_break_tolerates_one_close():
    """跌破黄线忍一根，第二根仍在黄线下才走"""
    engine = _only_layer(yellow_break_exit=True)
    klines = _flat(130, price=100.0)
    trade = _open_trade(klines, 125, entry=100.0, stop=50.0)
    klines[126] = _kline(klines[126].trade_date, 90.0)
    klines[127] = _kline(klines[127].trade_date, 89.0)

    for day in (126, 127):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None
    trade.yellow_reclaimed = True
    trade, completed = engine.process_day("600519.SH", klines, 128, trade)
    assert completed.exit_reason == "破黄线"


def test_yellow_break_needs_reclaim_first():
    """买入后从没站上黄线时，破黄线不生效"""
    engine = _only_layer(yellow_break_exit=True)
    klines = _flat(130, price=100.0)
    for i in range(125, 130):
        klines[i] = _kline(klines[i].trade_date, 90.0)
    trade = _open_trade(klines, 125, entry=90.0, stop=50.0)
    for day in range(126, 130):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None
    assert trade.yellow_reclaimed is False


def test_didi_exit():
    """高位连续两根阴线，第二根收盘低于第一根最低价"""
    engine = _only_layer(didi_exit=True)
    klines = _flat(60, price=100.0)
    trade = _open_trade(klines, 50, entry=90.0, stop=50.0)
    klines[51] = _kline(klines[51].trade_date, 96.0, open_=100.0, low=95.0, high=100.0)
    klines[52] = _kline(klines[52].trade_date, 92.0, open_=96.0, low=91.0, high=96.0)

    trade, completed = engine.process_day("600519.SH", klines, 52, trade)
    assert completed is None
    trade, completed = engine.process_day("600519.SH", klines, 53, trade)
    assert completed.exit_reason == "嘀嘀"


def test_s1_exit(monkeypatch):
    """S1 触发就清仓，优先级在破位之前"""
    from modules import loop_engine

    engine = _only_layer(s1_exit=True, bbi_break_days=2, break_requires_reclaim=False)
    klines = _flat(60, price=100.0)
    trade = _open_trade(klines, 50, entry=90.0, stop=50.0)
    monkeypatch.setattr(loop_engine, "detect_s1", lambda ks, i: object())
    trade, completed = engine.process_day("600519.SH", klines, 52, trade)
    assert completed.exit_reason == "S1"


def test_fourth_brick_halves_once(monkeypatch):
    """第四块红砖减半，只减一次；和卤煮减半互不占用"""
    from modules import loop_engine

    engine = _only_layer(fourth_brick_half=True)
    klines = _flat(60, price=100.0)
    trade = _open_trade(klines, 50, entry=100.0, stop=50.0)
    monkeypatch.setattr(loop_engine, "red_brick_streak", lambda ks: 4)
    for day in (52, 53):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None
    assert [leg["type"] for leg in trade.partial_exits] == ["第四块砖"]
    assert trade.remaining_fraction == 0.5


def test_exit_layers_can_be_switched_off():
    """每一层都能单独关掉，方便比较"""
    engine = _only_layer()
    klines = _flat(130, price=100.0)
    for i in range(126, 129):
        klines[i] = _kline(klines[i].trade_date, 90.0)
    trade = _open_trade(klines, 125, entry=100.0, stop=50.0)
    trade.yellow_reclaimed = True
    for day in range(126, 130):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        assert completed is None


def test_brick_series_matches_single_values():
    """一次算出的砖值序列，和逐段计算的结果相同"""
    from modules.indicators.price_patterns.brick import calculate_brick_series, calculate_brick_value

    price = 100.0
    klines = []
    for i in range(60):
        step = [1.5, -0.8, 2.2, -1.9, 0.7, 3.1, -2.4][i % 7]
        price += step
        klines.append(_kline(f"2026{i + 1:04d}", price, open_=price - step, high=price + 1.0, low=price - step - 1.0))

    series = calculate_brick_series(klines)
    for i in range(len(klines)):
        assert series[i] == pytest.approx(calculate_brick_value(klines[: i + 1]), abs=0.011), i


def test_red_brick_streak_counts_like_four_brick_system():
    from modules.indicators import detect_four_brick_system
    from modules.indicators.price_patterns.brick import red_brick_streak

    price = 100.0
    klines = []
    for i in range(50):
        step = 2.0 if i % 11 < 7 else -2.5
        price += step
        klines.append(_kline(f"2026{i + 1:04d}", price, open_=price - step, high=price + 0.5, low=price - abs(step) - 0.5))
    for end in range(20, 50):
        sub = klines[: end + 1]
        system = detect_four_brick_system(sub)
        is_red = system["brick_action"] in ("持有", "减仓")
        assert red_brick_streak(sub) == (system["brick_consecutive"] if is_red else 0), end


def _wave(volumes_turnover: list[tuple[float, float, float]]) -> list[DailyData]:
    """按 (收盘, 成交量, 换手率) 造 K 线，收盘高于昨收算阳量。"""
    out = []
    prev = volumes_turnover[0][0]
    for i, (close, vol, turnover) in enumerate(volumes_turnover):
        bar = _kline(f"2026{i + 1:04d}", close, open_=max(close, prev) + 0.5 if close < prev else close)
        prev = close
        bar.vol = vol
        bar.turnover_rate = turnover
        out.append(bar)
    return out


def test_two_30_sums_top_three_up_days():
    """累计换手是最大的三根阳量相加，缩量阴线和小阳量不算"""
    from modules.indicators import check_two_30_rule

    rows = [(100.0, 100.0, 1.0)] * 20 + [(110.0, 900.0, 10.0), (115.0, 800.0, 9.0), (120.0, 700.0, 8.0), (118.0, 950.0, 12.0), (117.0, 100.0, 1.0)]
    result = check_two_30_rule(_wave(rows))
    assert result["b1_turnover"] == 27.0  # 10 + 9 + 8，放量阴线的 12 不算
    assert result["b1_pass_30"] is True
    assert result["b1_30_status"] == "通过"


def test_two_30_rejects_high_turnover_and_rally():
    from modules.indicators import check_two_30_rule

    high_turnover = [(100.0, 100.0, 1.0)] * 20 + [(105.0, 900.0, 20.0), (108.0, 800.0, 15.0), (110.0, 700.0, 10.0)]
    assert check_two_30_rule(_wave(high_turnover))["b1_30_status"] == "废B1"

    high_rally = [(100.0, 100.0, 1.0)] * 20 + [(120.0, 900.0, 5.0), (140.0, 800.0, 5.0), (160.0, 700.0, 5.0)]
    result = check_two_30_rule(_wave(high_rally))
    assert result["b1_rally_pct"] > 50
    assert result["b1_30_status"] == "废B1"
    assert result["b1_pass_30"] is False


def test_two_30_between_lines_is_only_warning():
    from modules.indicators import check_two_30_rule

    rows = [(100.0, 100.0, 1.0)] * 20 + [(105.0, 900.0, 15.0), (108.0, 800.0, 12.0), (110.0, 700.0, 8.0)]
    result = check_two_30_rule(_wave(rows))
    assert result["b1_turnover"] == 35.0
    assert result["b1_30_status"] == "偏高"
    assert result["b1_pass_30"] is False


def test_two_30_without_turnover_is_not_a_pass():
    """数据源没有换手率时不能当作通过"""
    from modules.indicators import check_two_30_rule

    result = check_two_30_rule(_flat(40))
    assert result["b1_turnover"] is None
    assert result["b1_30_status"] == "换手未知"
    assert result["b1_pass_30"] is False


def test_kline_loader_keeps_turnover(monkeypatch):
    """analyze_stock 走的取数入口要带上换手率，否则两个 30% 永远是换手未知"""
    from modules.indicators import data_layer

    row = {"ts_code": "600519.SH", "trade_date": "20260101", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 1.0, "amount": 1.0, "pct_chg": 0.0, "turnover_rate": 0.66}
    monkeypatch.setattr(data_layer, "local_kline_rows", lambda *a, **k: [row])
    assert data_layer.get_kline_data("600519.SH")[0].turnover_rate == 0.66


def test_build_wave_stage_is_not_a_buy_signal(monkeypatch):
    """建仓波只说阶段，不能在 J 很高时报 B1 买入"""
    import modules.indicators as indicators
    from modules.datasource import daily_to_dict
    from modules.strategies import detect_all_strategies

    wave = {"wave": "建仓波", "confidence": 0.85, "stats": {"gain_pct": 17.18}}
    monkeypatch.setattr(indicators, "detect_three_waves", lambda _klines: wave)
    signals = detect_all_strategies("600519.SH", klines=daily_to_dict(_flat(40)))
    stage = [s for s in signals if s.description.startswith("三波理论·建仓波")]
    assert len(stage) == 1
    assert stage[0].action == "WATCH"
    assert stage[0].strategy.value != "B1"


def test_costs_reduce_pnl():
    """默认计入佣金、印花税和过户费"""
    engine = ShaofuLoopEngine()
    trade = LoopTrade(ts_code="600519.SH", entry_date="20260101", entry_price=100.0, entry_reason="", stop_loss_price=90.0)
    expected = (110.0 * (1 - engine._sell_cost()) / (100.0 * (1 + engine._buy_cost())) - 1) * 100
    assert engine.net_pnl_pct(trade, 110.0) == pytest.approx(expected)
    assert expected < 10.0


def test_white_line_variant_uses_white():
    """双线版用白线作为离场线"""
    engine = ShaofuLoopEngine(_no_cost(exit_line="white", no_rise_days=0, profit_to_loss_exit=False, dead_cross_exit=False, break_requires_reclaim=False))
    klines = _flat(45, price=100.0)
    for i in range(34, 40):
        klines[i] = _kline(klines[i].trade_date, 90.0)
    trade = _open_trade(klines, 34, entry=90.0, stop=50.0)
    completed = None
    for day in range(35, 41):
        trade, completed = engine.process_day("600519.SH", klines, day, trade)
        if completed:
            break
    assert completed.exit_reason == "白线跌破"


def test_yellow_line_missing_blocks_entry():
    """黄线不足 114 根算不出来时，不能算作白线在黄线之上"""
    from modules.strategies import shaofu_b1

    klines = _flat(60)
    original = shaofu_b1.detect_b1_today
    shaofu_b1.detect_b1_today = lambda ks: {"is_b1": True, "b1_j_value": -20.0, "b1_score": 4, "b1_amplitude": 1.0}
    try:
        assert shaofu_b1.detect_shaofu_b1(klines, j_threshold=-10, vol_shrink_threshold=2.0) is None
    finally:
        shaofu_b1.detect_b1_today = original


def test_market_filter_blocks_entry(monkeypatch):
    """择时不允许的信号日不开仓"""
    engine = ShaofuLoopEngine(_no_cost())
    klines = _flat(60)
    monkeypatch.setattr(engine, "_check_entry_internal", lambda ks, i: {"reason": "B1"} if i == 40 else None)

    assert engine.run_stock(klines, market_filter={}) == []
    assert len(engine.run_stock(klines, market_filter={klines[40].trade_date: True})) == 1


def test_daily_sharpe_is_not_annualized_from_trades():
    """夏普按每日账户净值算：一年几笔交易不能被放大"""
    from modules.backtest.shaofu import ShaofuBacktestResult, _apply_daily_curve, _calc_metrics

    klines = _flat(252, price=100.0)
    for i in range(50, 55):
        klines[i] = _kline(klines[i].trade_date, 100.0 + (i - 49))
    klines[55] = _kline(klines[55].trade_date, 105.0, open_=105.0)
    trade = LoopTrade(
        ts_code="600519.SH",
        entry_date=klines[50].trade_date,
        entry_price=100.0,
        entry_reason="",
        stop_loss_price=90.0,
        position_pct=0.1,
        exit_date=klines[55].trade_date,
        exit_price=105.0,
        exit_reason="测试",
        pnl_pct=5.0,
        holding_days=5,
    )
    result = ShaofuBacktestResult(ts_code="600519.SH", trades=[trade])
    _calc_metrics(result)
    assert result.sharpe_ratio == 0  # 没有 K 线时不给夏普
    _apply_daily_curve(result, klines, 0.0, 0.0)
    assert result.total_return == pytest.approx(0.005, abs=1e-3)  # 10% 仓位赚 5%
    assert 0 < result.in_market_ratio < 0.05
    assert len(result.equity_curve) == len(klines) + 1


def test_docs_match_rules():
    """README 和 SKILL.md 写的数字要和规则文件一致"""
    from pathlib import Path

    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    assert "j_threshold = -10" in readme
    assert "stop_loss_pct = -0.02" in readme
    assert "stop_loss_pct = -0.07" not in readme
    assert "入场价 × 93%" not in readme
    assert "J ≤ 12" not in readme
    assert "J ≤ -10" in skill


def test_eval_cards_blinds_answers():
    """题目导出时不能带答案标记，打分要能换算回原来的选项"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "eval_cards.py"
    spec = importlib.util.spec_from_file_location("eval_cards", path)
    eval_cards = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_cards)

    card = eval_cards.parse_card(
        {
            "id": "ch1-s1-c1",
            "chapter": 1,
            "chapter_title": "先活下来",
            "type": "decision",
            "difficulty": "easy",
            "text": "### 标题\n\n**情境**\n\n题干\n\n**提示**：泄题\n\n**选项**\n\n1. ❌ 错的\n   - ❌ 解析\n2. ✅ 对的\n   - ✅ 解析\n3. ❌ 也错\n\n**正确答案**：对的\n",
        }
    )
    assert card["options"] == ["错的", "对的", "也错"]
    assert card["correct"] == [1]
    assert "泄题" not in card["stem"] and "题干" in card["stem"]

    options, order = eval_cards.shuffled(card)
    right = options.index("对的") + 1
    wrong = options.index("错的") + 1
    assert eval_cards.score([card], {card["id"]: right})["right"] == 1
    assert eval_cards.score([card], {card["id"]: wrong})["right"] == 0
    assert eval_cards.parse_card({"id": "x", "chapter": 1, "text": "没有选项的问答卡"}) is None
