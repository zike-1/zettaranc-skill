"""随机入场对照：策略的入场信号是否比同股随机买入更好。

验收只看策略自身的收益和胜率，分不清是信号有效还是市场整体在涨。这里把每笔交易和
"同一只股票、同样持有天数、同样费用、随机日期买入"做配对比较，再按股票聚类做
bootstrap：同一只股票的多笔交易不独立，直接按笔算会高估显著性。

判定：配对差的 95% 区间下界大于 0，才算信号比随机入场好。
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_WARMUP_BARS = 120  # 随机入场也要等指标有足够的历史，与策略可入场的区间对齐
_SAMPLES_PER_TRADE = 200
_BOOTSTRAP_ROUNDS = 4000


@dataclass
class BaselineResult:
    """随机入场对照的结果（收益单位：%）。"""

    trades: int = 0
    stocks: int = 0
    strategy_avg_pct: float = 0.0
    random_avg_pct: float = 0.0
    edge_avg_pct: float = 0.0
    edge_ci_low_pct: float | None = None
    edge_ci_high_pct: float | None = None
    per_stock_edge: list[float] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """报告用的字典。"""
        return {
            "trades": self.trades,
            "stocks": self.stocks,
            "strategy_avg_pct": round(self.strategy_avg_pct, 3),
            "random_avg_pct": round(self.random_avg_pct, 3),
            "edge_avg_pct": round(self.edge_avg_pct, 3),
            "edge_ci_low_pct": None if self.edge_ci_low_pct is None else round(self.edge_ci_low_pct, 3),
            "edge_ci_high_pct": None if self.edge_ci_high_pct is None else round(self.edge_ci_high_pct, 3),
        }


def bootstrap_mean_ci(values: list[float], rounds: int = _BOOTSTRAP_ROUNDS, seed: int = 7) -> tuple[float, float]:
    """均值的 95% bootstrap 区间。"""
    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    means = rng.choice(data, (rounds, len(data))).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def random_entry_baseline(
    klines_map: dict[str, list],
    trades: list,
    buy_cost: float,
    sell_cost: float,
    seed: int = 20261031,
) -> BaselineResult:
    """按股票聚类比较策略与同股随机入场。

    Args:
        klines_map: {ts_code: K 线（升序）}
        trades: 已完成的交易，需要 ts_code、pnl_pct、entry_date、exit_date
        buy_cost: 买入成本比例（含滑点）
        sell_cost: 卖出成本比例（含滑点）
        seed: 随机种子，同样的输入结果可复现

    Returns:
        BaselineResult；没有可比较的交易时 ``edge_ci_low_pct`` 为 None
    """
    per_stock: dict[str, list[float]] = {}
    strategy: list[float] = []
    baseline: list[float] = []

    for code, klines in klines_map.items():
        stock_trades = [t for t in trades if t.ts_code == code and t.exit_reason != "数据末尾"]
        if not stock_trades:
            continue
        index = {k.trade_date: i for i, k in enumerate(klines)}
        opens = np.array([k.open for k in klines], dtype=float)
        rng = random.Random(zlib.crc32(code.encode()) + seed)
        n = len(klines)
        for t in stock_trades:
            start = index.get(t.entry_date)
            end = index.get(t.exit_date)
            if start is None or end is None:
                continue
            hold = max(1, end - start)
            latest_start = n - hold - 1
            if latest_start <= _WARMUP_BARS:
                continue
            samples = []
            for _ in range(_SAMPLES_PER_TRADE):
                s = rng.randrange(_WARMUP_BARS, latest_start)
                samples.append((opens[s + hold] * (1 - sell_cost)) / (opens[s] * (1 + buy_cost)) - 1)
            random_pct = float(np.mean(samples)) * 100
            strategy.append(t.pnl_pct)
            baseline.append(random_pct)
            per_stock.setdefault(code, []).append(t.pnl_pct - random_pct)

    if not per_stock:
        return BaselineResult()

    edges = [float(np.mean(v)) for v in per_stock.values()]
    result = BaselineResult(
        trades=len(strategy),
        stocks=len(per_stock),
        strategy_avg_pct=float(np.mean(strategy)),
        random_avg_pct=float(np.mean(baseline)),
        edge_avg_pct=float(np.mean(edges)),
        per_stock_edge=edges,
    )
    if len(edges) >= 2:
        result.edge_ci_low_pct, result.edge_ci_high_pct = bootstrap_mean_ci(edges)
    return result


__all__ = ["BaselineResult", "bootstrap_mean_ci", "random_entry_baseline"]
