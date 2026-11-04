"""少妇战法（shaofu）单股回测。

本 module 是 shaofu 回测的**唯一实现**。原先这些实现位于
``modules/backtest_six_step.py``，而那个 module 又被"新"的
``modules/backtest`` 包反过来 import——依赖方向倒挂。v4.8.0 起实现迁到这里，
``modules/backtest_six_step.py`` 降级为兼容 re-export。

六步 SOP：择时 -> 选股 -> 等 B1 -> 设止损 -> 止盈(卤煮) -> 离场(BBI两日破位)。
策略与离场规则由 :class:`modules.loop_engine.ShaofuLoopEngine` 提供，
本 module 只负责"跑一次 + 算指标 + 格式化"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..core.metrics import TRADING_DAYS_PER_YEAR, calc_trade_stats, compute_drawdown, compute_sharpe
from ..core.net import disable_proxy
from ..indicators import DailyData, get_kline_data
from ..loop_engine import LoopConfig, LoopTrade, ShaofuLoopEngine

logger = logging.getLogger(__name__)


@dataclass
class ShaofuBacktestResult:
    """少妇战法回测结果"""

    ts_code: str
    trades: list[LoopTrade] = field(default_factory=list)  # 所有完成的交易
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0  # 胜率
    avg_pnl: float = 0  # 平均盈亏%
    avg_win: float = 0  # 平均盈利%
    avg_loss: float = 0  # 平均亏损%
    max_win: float = 0  # 最大单笔盈利%
    max_loss: float = 0  # 最大单笔亏损%
    avg_holding_days: float = 0  # 平均持仓天数
    profit_factor: float = 0  # 盈亏比（总盈利/总亏损）
    total_return: float = 0  # 累计收益%
    max_drawdown: float = 0  # 最大回撤%
    sharpe_ratio: float = 0  # 夏普比率
    equity_curve: list[float] = field(default_factory=list)  # 每日资金曲线（从 100 起）
    in_market_ratio: float = 0  # 持仓天数占比

    def to_dict(self) -> dict:
        """规范化结果字典（见 :func:`shaofu_result_to_dict`）。"""
        return shaofu_result_to_dict(self)


def shaofu_result_to_dict(result: ShaofuBacktestResult) -> dict:
    """规范化的结果字典 —— CLI 与 API 都从这份派生。

    单位约定（对外展示单位）：
    - ``total_return`` / ``max_drawdown`` 为**百分数**（已 ×100）
    - ``avg_pnl`` / ``avg_win`` / ``avg_loss`` / ``max_win`` / ``max_loss`` /
      ``pnl_pct`` 为**百分数**；``avg_loss`` / ``max_loss`` 是有符号的负值
    - ``win_rate`` 为分数（0.455）

    CLI 用扁平契约，API 用 ``summary`` 嵌套契约——字段集与取值相同。

    写成模块级函数（而非方法）以便鸭子类型：测试可以用 SimpleNamespace 替身。
    """
    return {
        "ts_code": result.ts_code,
        "total_trades": result.total_trades,
        "win_count": result.win_count,
        "loss_count": result.loss_count,
        "win_rate": round(result.win_rate, 3),
        "avg_pnl": round(result.avg_pnl, 2),
        "avg_win": round(result.avg_win, 2),
        "avg_loss": round(result.avg_loss, 2),
        "max_win": round(result.max_win, 2),
        "max_loss": round(result.max_loss, 2),
        "profit_factor": round(result.profit_factor, 2),
        "total_return": round(result.total_return * 100, 2),
        "max_drawdown": round(result.max_drawdown * 100, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 2),
        "avg_holding_days": round(result.avg_holding_days, 1),
        "in_market_ratio": round(getattr(result, "in_market_ratio", 0) or 0, 3),
        "trades": [
            {
                "entry_date": t.entry_date,
                "entry_price": round(t.entry_price, 2),
                "exit_date": t.exit_date,
                "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                "exit_reason": t.exit_reason,
                "pnl_pct": round(t.pnl_pct, 2),
                "holding_days": t.holding_days,
            }
            for t in result.trades
        ],
    }


def _calc_metrics(result: ShaofuBacktestResult) -> None:
    """
    从交易列表计算所有统计指标

    Args:
        result: 回测结果对象（trades 字段需已填充）
    """
    trades = result.trades
    if not trades:
        return

    # 交易统计：统一走 core.metrics.calc_trade_stats（唯一的交易统计算法）
    pnl_list = [t.pnl_pct for t in trades]
    holding_days = [t.holding_days for t in trades if hasattr(t, "holding_days")]
    stats = calc_trade_stats(pnl_list, holding_days)

    result.total_trades = stats.total_trades
    result.win_count = stats.win_count
    result.loss_count = stats.loss_count
    result.win_rate = stats.win_rate
    result.avg_pnl = stats.avg_pnl
    result.avg_win = stats.avg_win
    result.avg_loss = stats.avg_loss
    result.max_win = stats.max_win
    result.max_loss = stats.max_loss
    result.profit_factor = stats.profit_factor
    result.avg_holding_days = stats.avg_holding_days

    # 没有 K 线时只能逐笔复利。夏普必须按每日净值算（见 _apply_daily_curve），这里不给。
    equity = 100.0
    curve = [equity]
    for t in trades:
        pos_pct = getattr(t, "position_pct", 1.0) or 1.0
        equity *= 1 + (t.pnl_pct / 100.0) * pos_pct
        curve.append(equity)
    result.equity_curve = curve
    result.total_return = (equity / 100.0) - 1.0
    max_dd, _ = compute_drawdown(curve)
    result.max_drawdown = max_dd


def daily_portfolio_returns(klines: list[DailyData], trades: list[LoopTrade], buy_cost: float, sell_cost: float) -> list[float]:
    """把交易还原成每日账户收益率（仓位按 position_pct，空仓日收益为 0）。

    买入当日按买入价到收盘价算；减半和清仓当日，卖出部分按昨收到卖出价算。
    手续费在成交当日扣除。
    """
    index = {k.trade_date: i for i, k in enumerate(klines)}
    returns = [0.0] * len(klines)
    for t in trades:
        start = index.get(t.entry_date)
        end = index.get(t.exit_date)
        if start is None or end is None or t.entry_price <= 0:
            continue
        pos = getattr(t, "position_pct", 1.0) or 1.0
        sells = {index[p["date"]]: p for p in t.partial_exits if p["date"] in index}
        held = 1.0
        for i in range(start, end + 1):
            bar = klines[i]
            ref = t.entry_price if i == start else klines[i - 1].close
            if ref <= 0:
                continue
            day = 0.0
            if i == start:
                day -= buy_cost
            if i in sells:
                leg = sells[i]
                day += leg["fraction"] * (leg["price"] * (1 - sell_cost) / ref - 1)
                held -= leg["fraction"]
            if i == end:
                exit_price = t.exit_price or bar.close
                day += held * (exit_price * (1 - sell_cost) / ref - 1)
            else:
                day += held * (bar.close / ref - 1)
            returns[i] += pos * day
    return returns


def _apply_daily_curve(result: ShaofuBacktestResult, klines: list[DailyData], buy_cost: float, sell_cost: float) -> None:
    """用每日账户净值计算收益、回撤和夏普。

    此前夏普用每笔收益按 252/平均持仓天数 年化，等于假设资金一直连续满仓交易，
    实际一年只有几笔时会把数值放大数倍。
    """
    returns = daily_portfolio_returns(klines, result.trades, buy_cost, sell_cost)
    equity = 100.0
    curve = [equity]
    for r in returns:
        equity *= 1 + r
        curve.append(equity)
    result.equity_curve = curve
    result.total_return = equity / 100.0 - 1.0
    result.max_drawdown = compute_drawdown(curve)[0]
    result.sharpe_ratio = compute_sharpe(returns, periods_per_year=TRADING_DAYS_PER_YEAR)
    result.in_market_ratio = sum(1 for r in returns if r != 0) / len(returns) if returns else 0.0


def backtest_shaofu_single(
    ts_code: str,
    days: int = 250,
    config: LoopConfig | None = None,
    klines: list[DailyData] | None = None,
    market_filter: dict[str, bool] | None = None,
) -> ShaofuBacktestResult:
    """
    单股票少妇战法回测

    Args:
        ts_code: 股票代码
        days: 回测天数
        config: 策略参数，None 使用默认
        klines: 外部传入的 K 线数据，None 则从数据库读取
        market_filter: 择时结果 {信号日: 能否开仓}，None 表示不择时

    Returns:
        ShaofuBacktestResult with all metrics
    """
    # 取消代理（与 backtest.py 保持一致）
    disable_proxy()

    # 1. 获取 K 线数据（等比前复权，保持真实涨跌幅）
    if klines is None:
        klines = get_kline_data(ts_code, days, basis="return")

    result = ShaofuBacktestResult(ts_code=ts_code)

    if not klines or len(klines) < 30:
        return result

    # 2. 创建引擎并运行
    engine = ShaofuLoopEngine(config)
    trades = engine.run_stock(klines, ts_code=ts_code, market_filter=market_filter)

    if not trades:
        return result

    result.trades = trades

    # 3. 计算指标
    _calc_metrics(result)
    _apply_daily_curve(result, klines, engine._buy_cost(), engine._sell_cost())

    return result


def summary_text(result: ShaofuBacktestResult) -> str:
    """
    格式化单股回测结果为可读文本

    Args:
        result: 少妇战法回测结果

    Returns:
        格式化字符串
    """
    lines = [
        f"{'=' * 60}",
        f"少妇战法回测结果: {result.ts_code}",
        f"{'=' * 60}",
        f"总交易次数:   {result.total_trades}",
        f"盈利次数:     {result.win_count}",
        f"亏损次数:     {result.loss_count}",
        f"胜率:         {result.win_rate:.1%}",
        f"盈亏比:       {result.profit_factor:.2f}",
        # avg_pnl / avg_win / avg_loss / max_win / max_loss 是百分数
        # （由 LoopTrade.pnl_pct 聚合而来），不能用 :.2%（那会再乘 100）
        f"平均盈亏:     {result.avg_pnl:+.2f}%",
        f"平均盈利:     {result.avg_win:+.2f}%",
        f"平均亏损:     {result.avg_loss:+.2f}%",
        f"最大单笔盈:   {result.max_win:+.2f}%",
        f"最大单笔亏:   {result.max_loss:+.2f}%",
        f"平均持仓天数: {result.avg_holding_days:.1f}",
        f"累计收益:     {result.total_return:+.2%}",
        f"最大回撤:     {result.max_drawdown:.2%}",
        f"夏普比率:     {result.sharpe_ratio:.2f}",
        f"{'=' * 60}",
    ]

    if result.trades:
        lines.append("最近5笔交易:")
        for t in result.trades[-5:]:
            pnl = t.pnl_pct if hasattr(t, "pnl_pct") else 0.0
            marker = "+" if pnl > 0 else ""
            lines.append(f"  {t.entry_date}->{t.exit_date or '持有中'} {marker}{pnl:.2f}%")

    return "\n".join(lines)


__all__ = [
    "ShaofuBacktestResult",
    "_calc_metrics",
    "backtest_shaofu_single",
    "summary_text",
]
