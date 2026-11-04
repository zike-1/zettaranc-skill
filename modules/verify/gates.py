"""
五项硬指标自动达标判定

阈值集中化（spec：Sharpe/Calmar/WinRate/MaxDD/OOS_IS）

注意：GateResult 在 pipeline.py 定义（避免重复定义）
"""

from __future__ import annotations

from .pipeline import AggregateMetrics, GateResult


# 阈值集中化（spec Global Constraints）
THRESHOLDS: dict[str, dict] = {
    "sharpe": {"min": 0.5, "direction": "higher", "label": "Sharpe"},
    "calmar": {"min": 0.5, "direction": "higher", "label": "Calmar"},
    "win_rate": {"min": 0.40, "direction": "higher", "label": "WinRate"},
    "max_drawdown": {"max": 0.25, "direction": "lower", "label": "MaxDD"},
    "oos_is_ratio": {"min": 0.60, "direction": "higher", "label": "OOS/IS"},
    # 样本太少时其他指标都没有统计意义，比如 1 笔盈利就是 100% 胜率
    "total_trades": {"min": 100, "direction": "higher", "label": "Trades"},
    # 配对差 95% 区间下界（%），见 verify/baseline.py
    "edge_vs_random": {"min": 0.0, "direction": "higher", "label": "EdgeVsRandom"},
}


__all__ = ["THRESHOLDS", "GateResult", "check_gates"]


def check_gates(
    metrics: AggregateMetrics,
    wf: object | None = None,  # WFResult, Task 6 定义
    baseline: object | None = None,  # verify.baseline.BaselineResult
) -> dict[str, GateResult]:
    """
    硬指标自动判定：
    - 优先 4 项（Sharpe/Calmar/WinRate/MaxDD）从 metrics 取
    - 最少交易笔数：样本太少时其他指标没有统计意义
    - OOS/IS 仅在 wf 不为 None 时判定
    - 随机入场对照仅在 baseline 不为 None 时判定：配对差 95% 区间下界要大于 0
    - 失败时给改进建议
    """
    gates: dict[str, GateResult] = {}

    # 1. Sharpe
    gates["sharpe"] = _check_higher(
        "sharpe",
        metrics.sharpe,
        THRESHOLDS["sharpe"]["min"],
        "Sharpe",
        "增大收益弹性 / 降低波动",
    )

    # 2. Calmar
    gates["calmar"] = _check_higher(
        "calmar",
        metrics.calmar,
        THRESHOLDS["calmar"]["min"],
        "Calmar",
        "提升年化收益或降低回撤",
    )

    # 3. WinRate
    gates["win_rate"] = _check_higher(
        "win_rate",
        metrics.win_rate,
        THRESHOLDS["win_rate"]["min"],
        "WinRate",
        "收紧入场条件（如降低 j_threshold）",
    )

    # 4. MaxDD（方向是 lower）
    gates["max_drawdown"] = _check_lower(
        "max_drawdown",
        metrics.max_drawdown,
        THRESHOLDS["max_drawdown"]["max"],
        "MaxDD",
        "收紧止损至 -3%（当前 -5%）",
    )

    # 5. 最少交易笔数
    total_trades = getattr(metrics, "total_trades", None)
    if total_trades is not None:
        gates["total_trades"] = _check_higher(
            "total_trades",
            total_trades,
            THRESHOLDS["total_trades"]["min"],
            "Trades",
            "扩大股票池或回测区间",
        )

    # 6. OOS/IS（仅在 wf 不为 None 时）
    if wf is not None and hasattr(wf, "oos_is_ratio"):
        ratio = wf.oos_is_ratio
        if ratio is None:
            # **算不出来**（IS 段 Sharpe 非正 / OOS 段无有效样本）。
            # 这不是"未达标"——比值在数学上就没有意义。标为不适用（applicable
            # =False），报告里显示 "N/A"，不计入失败项。
            # value 用 None 而非 0.0：0.0 会被下游读成"比值确实是 0"。
            gates["oos_is_ratio"] = GateResult(
                name="oos_is_ratio",
                value=None,
                threshold=THRESHOLDS["oos_is_ratio"]["min"],
                passed=True,
                message="OOS/IS 无法计算（IS 段 Sharpe 非正或 OOS 段无有效样本），本项不适用",
                applicable=False,
            )
        else:
            gates["oos_is_ratio"] = _check_higher(
                "oos_is_ratio",
                ratio,
                THRESHOLDS["oos_is_ratio"]["min"],
                "OOS/IS",
                "减少过拟合风险（缩小参数搜索空间）",
            )

    # 7. 比同股随机入场更好（按股票聚类的 95% 区间下界 > 0）
    ci_low = getattr(baseline, "edge_ci_low_pct", None)
    if ci_low is not None:
        gates["edge_vs_random"] = _check_higher(
            "edge_vs_random",
            ci_low,
            THRESHOLDS["edge_vs_random"]["min"],
            "EdgeVsRandom",
            "入场信号没有显示出优于随机买入，先查规则再调参数",
        )

    return gates


def _check_higher(
    name: str,
    value: float,
    threshold: float,
    label: str,
    suggestion: str,
) -> GateResult:
    passed = value >= threshold
    msg = "" if passed else f"{label} {value:.2f} < {threshold:.2f}，建议：{suggestion}"
    return GateResult(
        name=name,
        value=value,
        threshold=threshold,
        passed=passed,
        message=msg,
    )


def _check_lower(
    name: str,
    value: float,
    threshold: float,
    label: str,
    suggestion: str,
) -> GateResult:
    passed = value <= threshold
    msg = "" if passed else f"{label} {value:.2%} > {threshold:.2%}，建议：{suggestion}"
    return GateResult(
        name=name,
        value=value,
        threshold=threshold,
        passed=passed,
        message=msg,
    )
