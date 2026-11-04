import os
import sqlite3
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum
from modules.database import get_db_connection


from ..datasource import dict_to_daily
from ..indicators import DailyData
from ..tdx_local_client import local_kline_rows

# 向后兼容别名
_dict_to_daily = dict_to_daily


def _klines_dict_to_daily(klines: list[dict]) -> list[DailyData]:
    """将 strategies 模块用的 dict klines 转为 indicators 模块用的 DailyData"""
    return _dict_to_daily(klines)


def _ensure_daily_klines(klines: list) -> list[DailyData]:
    """确保输入序列是 list[DailyData]。若是 list[dict] 则自动转换。"""
    if not klines:
        return []
    if isinstance(klines[0], DailyData):
        return klines
    return _dict_to_daily(klines)


class StrategyType(Enum):
    """战法类型"""

    # 基础战法
    B1 = "B1"  # 买点1
    B2 = "B2"  # 买点2（确认）
    B3 = "B3"  # 买点3
    SB1 = "SB1"  # 超级B1

    # 复合战法
    CHANGAN = "长安战法"  # 三日确认战法
    SI_FEN_ZHI_SAN = "四分之三阴量"  # 假突破识别
    NANA = "娜娜图形"  # 连续放量涨+缩量回调
    CHAOFAN = "超级B1"  # 超级买点

    # 异动战法
    YIDONG_DILIAN = "异动+地量地价"  # 异动后缩量买点

    # 特殊形态
    PINGHANG = "平行重炮"  # 双阳夹阴
    KENGQI = "坑里起好货"  # 填坑战法
    DUIchen = "对称VA"  # 对称战法

    # 逃顶信号
    S1 = "S1"  # 初级逃顶（丑陋大绿帽）
    S2 = "S2"  # 确认逃顶（MACD顶背离）
    S3 = "S3"  # 最后逃生（反抽无力）

    # 主力阶段
    XISHOU = "吸筹"  # 麒麟会吸筹阶段
    LASHENG = "拉升"  # 麒麟会拉升阶段
    PAIFA = "派发"  # 麒麟会派发阶段
    LUOLUO = "回落"  # 麒麟会回落阶段

    # 观察/提示
    WATCH = "观察"  # 阶段判断、提示信号

    # 砖形图信号
    BRICK_EXIT = "四块砖翻绿"  # 红砖翻绿 → 止损
    BRICK_REDUCE = "四块砖减仓"  # 红砖满4块 → 减仓一半
    BRICK_BOUNCE = "四块砖反弹"  # 绿砖满4块 → 可能止跌，观察B1


class Priority(Enum):
    """信号优先级"""

    CRITICAL = 3  # 紧急：止损、逃顶
    OPPORTUNITY = 2  # 机会：买点、战法
    OBSERVE = 1  # 观察：提示、减仓、阶段判断


class Action(Enum):
    """交易建议"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"


@dataclass
class StrategySignal:
    """战法信号"""

    ts_code: str
    trade_date: str
    strategy: StrategyType
    confidence: float  # 置信度 0-1
    description: str
    details: dict[str, Any] = field(default_factory=dict)

    # 交易建议
    action: str = "WATCH"  # BUY/SELL/HOLD/WATCH
    target_price: float | None = None
    stop_loss: float | None = None
    risk_ratio: float | None = None

    # 扩展字段（部分策略使用）
    price: float | None = None  # 信号产生时的价格
    reason: str | None = None  # 信号原因说明

    # 信号优先级（由策略检测函数自动填入）
    priority: Priority = Priority.OBSERVE


def get_kline_data(ts_code: str, days: int = 120) -> list[dict]:
    """
    获取K线数据，并关联指标缓存与资金流数据

    配置 ``TDX_DIR`` 时 K 线读本地通达信；本地没有指标缓存和资金流，这些字段为 0。
    """
    local_rows = local_kline_rows(ts_code, days=days)
    if local_rows is not None:
        return [_kline_row_to_dict(row, local_rows[i - 1] if i > 0 else row) for i, row in enumerate(local_rows)]

    # 联表查询：K线 + 指标缓存(Bollinger/RSI/DMI) + 资金流
    # 注意：先按 DESC 取最近 N 条，再在 Python 端反转回正序
    # （SQLite 在子查询里包一层即可避免 ORDER BY + LIMIT 顺序冲突）
    # 查询出错（如空库缺表）时也要关连接，否则 Windows 上数据库文件一直被锁
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                k.ts_code, k.trade_date, k.open, k.high, k.low, k.close, k.vol, k.amount, k.pct_chg,
                i.boll_upper, i.boll_mid, i.boll_lower, i.rsi6, i.adx, i.dmi_plus, i.dmi_minus,
                m.buy_lg_amount, m.buy_elg_amount, m.sell_lg_amount, m.sell_elg_amount, m.net_mf
            FROM (
                SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
                FROM daily_kline
                WHERE ts_code = ?
                ORDER BY trade_date DESC
                LIMIT ?
            ) k
            LEFT JOIN indicator_cache i ON k.ts_code = i.ts_code AND k.trade_date = i.trade_date
            LEFT JOIN moneyflow m ON k.ts_code = m.ts_code AND k.trade_date = m.trade_date
            ORDER BY k.trade_date ASC
        """,
            (ts_code, days),
        ).fetchall()
    finally:
        conn.close()

    return [_kline_row_to_dict(dict(row), rows[i - 1] if i > 0 else row) for i, row in enumerate(rows)]


def _kline_row_to_dict(row, prev) -> dict:
    """把一行 K 线（可含指标缓存、资金流字段）转成战法层使用的 dict。"""
    prev_close = prev["close"]
    prev_vol = prev["vol"]
    extra = row.get

    return {
        "ts_code": row["ts_code"],
        "trade_date": row["trade_date"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "vol": row["vol"],
        "amount": row["amount"],
        "pct_chg": row["pct_chg"],
        "prev_close": prev_close,
        "prev_vol": prev_vol,
        "is_rise": row["close"] > prev_close,
        "is_beidou": row["vol"] >= prev_vol * 2,
        "is_suoliang": row["vol"] <= prev_vol * 0.5,
        "is_jiayin": row["close"] < row["open"] and row["close"] > prev_close,
        "is_yinxian": row["close"] < prev_close,
        "is_fangliang_yinxian": row["close"] < prev_close and row["vol"] > prev_vol * 1.5,
        # MDC 扩展字段（LEFT JOIN 可能为 NULL，统一 fallback）
        "boll_upper": extra("boll_upper") or 0,
        "boll_mid": extra("boll_mid") or 0,
        "boll_lower": extra("boll_lower") or 0,
        "rsi6": extra("rsi6") or 0,
        "adx": extra("adx") or 0,
        "dmi_plus": extra("dmi_plus") or 0,
        "dmi_minus": extra("dmi_minus") or 0,
        "net_mf": extra("net_mf") or 0,
        "large_inflow": (extra("buy_lg_amount") or 0) + (extra("buy_elg_amount") or 0),
        "large_outflow": (extra("sell_lg_amount") or 0) + (extra("sell_elg_amount") or 0),
    }


def _calc_kdj(klines: list[dict]) -> tuple[float, float, float]:
    """计算 KDJ（委托到 indicators.calculate_kdj，支持 dict 输入）"""
    from ..indicators import calculate_kdj

    return calculate_kdj(klines)


def _calc_bbi(klines: list[dict]) -> float:
    """计算 BBI（委托到 indicators.calculate_bbi，支持 dict 输入）"""
    from ..indicators import calculate_bbi

    return calculate_bbi(klines)


def _get_kdj(klines: list[DailyData], index: int) -> tuple[float, float, float]:
    """获取 KDJ，有属性直接读取，无属性则整段预计算一次并索引（只补未设过的属性）。

    precompute_kdj_sequence[m] 与 calculate_kdj(klines[:m+1]) 逐位等值（已实证），
    用整段预计算 + 索引替代每次前缀切片重算；用 is-None 守卫避免覆盖调用方手工设定的值。
    """
    today = klines[index]
    if getattr(today, "kdj_j", None) is not None:
        return today.kdj_k or 0.0, today.kdj_d or 0.0, today.kdj_j or 0.0
    from ..indicators import precompute_kdj_sequence

    kdj_sequence = precompute_kdj_sequence(klines)
    if index >= len(kdj_sequence):
        return 0.0, 0.0, 0.0
    k, d, j = kdj_sequence[index]
    for i, (kk, dd, jj) in enumerate(kdj_sequence):
        if getattr(klines[i], "kdj_j", None) is None:
            klines[i].kdj_k, klines[i].kdj_d, klines[i].kdj_j = kk, dd, jj
    return k, d, j


def _get_bbi(klines: list[DailyData], index: int) -> float:
    """获取 BBI，有属性直接读取，无属性则整段预计算一次并索引（只补未设过的属性）。"""
    today = klines[index]
    if getattr(today, "bbi", None) is not None:
        return today.bbi or 0.0
    from ..indicators import precompute_bbi_sequence

    bbi_sequence = precompute_bbi_sequence(klines)
    if index >= len(bbi_sequence):
        return 0.0
    bbi = bbi_sequence[index]
    for i, b in enumerate(bbi_sequence):
        if getattr(klines[i], "bbi", None) is None:
            klines[i].bbi = b
    return bbi


def _get_macd_dif(klines: list[DailyData], index: int) -> float:
    """获取 MACD DIF，有属性直接读取，无属性则动态计算并缓存"""
    today = klines[index]
    if getattr(today, "macd_dif", None) is not None:
        return today.macd_dif or 0.0
    from ..indicators import calculate_macd

    difs, _, _ = calculate_macd(klines[: index + 1])
    for i in range(len(difs)):
        klines[i].macd_dif = difs[i]
    return today.macd_dif or 0.0
