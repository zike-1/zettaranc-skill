#!/usr/bin/env python3
"""
少妇战法六步闭环引擎

六步 SOP：择时 → 选股 → 等 B1 → 设止损 → 卤煮减半 → 连续两日收盘跌破 BBI 清仓。
规则和出处见 :mod:`modules.strategies.shaofu_rules`，这里只负责按规则走流程。

时序（日线数据，不用未来信息）：
  - T 日收盘判断信号或离场条件，T+1 日开盘成交
  - 买入 K 线（T+1）收盘后才确定止损价，从这根收盘开始检查离场
  - 开盘涨停不追，开盘跌停卖不出就顺延

离场按七层应对的优先级检查：没涨就走 → 盈转亏必走 → 止损 → S1 与嘀嘀 → 破位与破黄线 → 死叉。
第 4 层放飞是减仓，不离场：卤煮减半和第四块红砖各减一次。

核心趋势线：
  - 白线（Z哥白线）= EMA(EMA(C,10),10)
  - 黄线（大哥线）= (MA14+MA28+MA57+MA114)/4
  - BBI = (MA3+MA6+MA12+MA24)/4

用法：
    from modules.loop_engine import ShaofuLoopEngine, LoopConfig
    trades = ShaofuLoopEngine().run_stock(klines)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, cast

from .strategies.sell_signals import detect_s1
from .strategies.shaofu_b1 import detect_shaofu_b1
from .strategies.shaofu_rules import rule_value
from .strategies.resonance import (
    DETECTOR_REGISTRY,
    SignalDetector,
    detect_b2_resonance,
    detect_changan_resonance,
    detect_nana_resonance,
    detect_pinghang_resonance,
)
from .indicators import (
    DailyData,
    TradeSignal,
    calculate_bbi,
    calculate_kdj,
    calculate_zg_white,
    calculate_dg_yellow,
    detect_b1_today,
    detect_double_line_cross,
    detect_trade_signal,
)
from .indicators.price_patterns.brick import red_brick_streak
from .indicators.price_patterns.screener_helper import detect_didi
from .constants import BACKTEST_DEFAULT_TRAILING_STOP_PCT


# ============================================================
# 状态定义
# ============================================================


class LoopState(Enum):
    """六步闭环状态"""

    TIMING = "择时"  # Step 1: 看大环境
    SELECTING = "选股"  # Step 2: 筛选标的
    WAITING_B1 = "等B1"  # Step 3: 等待 B1 信号
    HOLDING = "持仓"  # Step 4-5: 止损/止盈管理
    EXITED = "离场"  # Step 6: 平仓完成


# ============================================================
# 数据结构
# ============================================================


@dataclass
class LoopConfig:
    """策略参数配置"""

    # 默认值都来自 strategies/shaofu_rules.py，出处写在那里
    j_threshold: float = rule_value("j_threshold")  # B1 J 值阈值
    stop_loss_pct: float = rule_value("stop_loss_pct")  # 买入 K 线最低价向下的比例
    stop_loss_method: str = rule_value("stop_loss_method")  # "entry_low" | "n_structure_low" | "j_negative_low" | "atr_based"
    # v3.10.1 新增：ATR 动态止损
    atr_stop_window: int = 14  # ATR 计算窗口
    atr_stop_multiplier: float = 2.0  # ATR 乘数（止损距离 = ATR × 倍数）
    # v3.10.1 新增：移动止损（trailing stop）
    trailing_stop_enabled: bool = False  # 是否启用移动止损
    trailing_stop_pct: float = BACKTEST_DEFAULT_TRAILING_STOP_PCT  # 移动止损比例（高点回落超过此比例则止损）
    exit_line: str = rule_value("exit_line")  # 破位离场线："bbi"（1.0）| "white"（双线 Pro）
    bbi_break_days: int = rule_value("bbi_break_days")  # 连续跌破离场线的天数
    bbi_break_threshold: float = rule_value("bbi_break_threshold")  # 收盘低于离场线超过此比例才算跌破
    break_requires_reclaim: bool = rule_value("break_requires_reclaim")  # 入场后站上过离场线，破位才生效
    min_holding_days: int = rule_value("min_holding_days")  # 持有满此天数才检查离场
    no_rise_days: int = rule_value("no_rise_days")  # 没涨就走：观察天数，0 表示关闭
    no_rise_grace_days: int = rule_value("no_rise_grace_days")  # 没涨就走：额外宽限天数
    profit_to_loss_exit: bool = rule_value("profit_to_loss_exit")  # 盈转亏必走
    dead_cross_exit: bool = rule_value("dead_cross_exit")  # 白线死叉黄线清仓
    lu_half: bool = rule_value("lu_half")  # 卤煮减半
    big_yang_pct: float = rule_value("big_yang_pct")  # 中大阳线的最低涨幅（%）
    fourth_brick_half: bool = rule_value("fourth_brick_half")  # 第四块红砖减半
    s1_exit: bool = rule_value("s1_exit")  # S1 清仓
    didi_exit: bool = rule_value("didi_exit")  # 嘀嘀清仓
    yellow_break_exit: bool = rule_value("yellow_break_exit")  # 破黄线清仓
    yellow_break_days: int = rule_value("yellow_break_days")  # 连续收在黄线下的天数
    position_pct: float = rule_value("position_pct")  # 单笔仓位比例
    vol_shrink_threshold: float = rule_value("vol_shrink_threshold")  # 缩量判定阈值（当日量 / 前日量 < 此值）
    # 成本（回测用，全部置 0 可得到不含成本的结果）
    commission_rate: float = rule_value("commission_rate")
    stamp_duty_rate: float = rule_value("stamp_duty_rate")
    transfer_fee_rate: float = rule_value("transfer_fee_rate")
    slippage_pct: float = rule_value("slippage_pct")

    # v3.10.x: 多策略共振开关（默认全部关闭，保持原 B1 单策略行为）
    enable_b2: bool = False  # B2（B1 后放量长阳确认）
    enable_changan: bool = False  # 长安战法（三日 B1）
    enable_nana: bool = False  # 娜娜图形（主升浪回踩）
    enable_pinghang: bool = False  # 平行重炮（双阳夹阴）
    min_signals: int = 1  # 投票入场最少策略数（>=min_signals 才入场）
    signal_weights: dict[str, float] = field(
        default_factory=lambda: {
            "B1": 1.0,
            "B2": 1.2,
            "长安": 1.5,
            "娜娜": 1.3,
            "平行重炮": 1.1,
        }
    )
    min_signal_strength: float = 0.0  # 信号总强度阈值（加权和低于此值则不入场）

    @classmethod
    def from_registry(cls, strategy_name: str = "shaofu_v1") -> LoopConfig | None:
        """
        从 param_registry 读取 LoopConfig（可选扩展点）。

        注意：本方法需要循环依赖避免，所以延迟 import：
        - modules.verify.registry_writer.load_config_from_registry
        - 若 modules.verify 不可用（如 zettaranc_skill 未安装 verify 子包），
          返回 None
        """
        try:
            from modules.verify.registry_writer import load_config_from_registry

            return load_config_from_registry(strategy_name)
        except ImportError:
            return None


@dataclass
class LoopTrade:
    """一笔完整的少妇战法交易记录

    记录从入场到出价的完整交易生命周期，包含：
    - 入场信息：日期、价格、原因（B1 信号详情）
    - 出场信息：日期、价格、原因（卤煮止盈/白线跌破/止损/白线死叉黄线）
    - 过程指标：盈亏比、持仓天数、最大浮盈/浮亏
    - 仓位管理：卤煮减仓记录、实际仓位比例
    - 市场环境：入场时的市场状态（BULL/BEAR/SIDEWAYS）
    """

    ts_code: str
    entry_date: str
    entry_price: float
    entry_reason: str  # B1 信号详情
    stop_loss_price: float  # Step 4: 收盘价止损位
    # v3.10.1：移动止损追踪入场后最高点（用于动态抬高止损价）
    highest_after_entry: float = 0.0
    triggered_strategies: list[str] = field(default_factory=list)  # 触发的策略列表（多策略共振时使用）
    signal_strength: float = 0.0  # 信号总强度（多策略共振时使用）
    exit_date: str = ""
    exit_price: float = 0
    exit_reason: str = ""  # "没涨就走" | "盈转亏" | "止损" | "S1" | "嘀嘀" | "BBI跌破" | "白线跌破" | "破黄线" | "白线死叉黄线"
    pnl_pct: float = 0
    holding_days: int = 0
    max_favorable: float = 0  # 最大浮盈%
    max_adverse: float = 0  # 最大浮亏%
    partial_exits: list[dict[str, Any]] = field(default_factory=list)  # 卤煮减仓记录
    position_pct: float = 0.0  # 该笔交易的实际仓位比例（占总资金）
    entry_index: int = -1  # 买入 K 线在序列中的下标
    max_close: float = 0.0  # 入场后最高收盘价（判断没涨就走、盈转亏）
    line_reclaimed: bool = False  # 入场后收盘是否站上过离场线
    yellow_reclaimed: bool = False  # 入场后收盘是否站上过黄线（含买入当天）
    remaining_fraction: float = 1.0  # 还没卖出的比例
    pending_exit_reason: str = ""  # 已触发离场但开盘跌停卖不出，顺延到下一日
    market_regime: str = ""  # 入场时的市场状态（BULL/BEAR/SIDEWAYS）
    strategy_source: str = ""  # v3.10.0：触发该交易的策略名（B1/B2/SB1/长安 等）


# ============================================================
# 辅助函数
# ============================================================


def detect_bbi_break_streak(
    closes: list[float], bbi_values: list[float], days: int = 2, threshold: float = 0.0
) -> bool:
    """
    检测收盘价是否连续 N 天跌破 BBI

    Args:
        closes: 收盘价序列
        bbi_values: BBI 值序列（与 closes 等长）
        days: 连续跌破天数要求
        threshold: 跌破阈值比例（0.01 = 收盘价低于 BBI 1% 才算跌破）

    Returns:
        是否连续 N 天跌破
    """
    if len(closes) < days or len(bbi_values) < days:
        return False

    for i in range(1, days + 1):
        if closes[-i] >= bbi_values[-i] * (1 - threshold):
            return False

    return True


def _calc_stop_loss_price(
    klines: list[DailyData],
    day_idx: int,
    method: str = "entry_low",
    stop_loss_pct: float = -0.07,
    atr_multiplier: float = 2.0,
    atr_window: int = 14,
) -> float:
    """
    根据不同方法计算止损价

    四种方法（SOP 第 4 步）：
    - entry_low: 入场 K 线最低价 + 止损比例缓冲
    - n_structure_low: N 型结构前低（入场前最近一个回调低点）
    - j_negative_low: J 值转负那天 K 线的最低价
    - atr_based: v3.10.1 ATR 动态止损 = 入场价 - ATR × 倍数
      （高波动自动放宽，低波动自动收紧）

    Args:
        klines: K 线数据
        day_idx: 入场日索引
        method: 止损方法（"entry_low"/"n_structure_low"/"j_negative_low"/"atr_based"）
        stop_loss_pct: 止损比例（负值，如 -0.07 表示 -7%），仅 entry_low 模式使用
        atr_multiplier: ATR 倍数（止损距离 = ATR × 倍数），仅 atr_based 模式使用
        atr_window: ATR 计算窗口，仅 atr_based 模式使用

    Returns:
        止损价
    """
    entry_kline = klines[day_idx]

    if method == "entry_low":
        # 入场日最低价 × (1 + 止损比例)
        # stop_loss_pct=-0.07 表示入场低下方 7% 为止损位
        return entry_kline.low * (1 + stop_loss_pct)

    if method == "n_structure_low":
        # 回溯找入场前最近的回调低点（N 型结构的前低）
        lookback = min(day_idx, 30)
        window = klines[day_idx - lookback : day_idx]
        if not window:
            return entry_kline.low
        return min(k.low for k in window)

    if method == "j_negative_low":
        # 找入场前 J 值首次转负的那根 K 线
        for i in range(day_idx - 1, max(day_idx - 30, -1), -1):
            sub_klines = klines[: i + 1]
            if len(sub_klines) < 9:
                continue
            _, _, j = calculate_kdj(sub_klines)
            if j <= 0:
                return klines[i].low
        # 未找到则退化为 entry_low
        return entry_kline.low

    if method == "atr_based":
        # v3.10.1：ATR 动态止损
        # 止损价 = 入场参考价 - ATR × 倍数
        # 入场参考价用 entry_kline.close，更贴近实际成交
        from .core.atr import calculate_atr

        # 用入场前 window+1 根 K 线计算 ATR（不偷看入场日之后数据）
        atr_window_klines = klines[max(0, day_idx - atr_window - 1) : day_idx + 1]
        atr_value = calculate_atr(atr_window_klines, window=min(atr_window, len(atr_window_klines) - 1))
        if atr_value <= 0:
            # 数据不足 fallback 到 entry_low
            return entry_kline.low * (1 + stop_loss_pct)
        entry_ref = entry_kline.close or entry_kline.open
        return entry_ref - atr_value * atr_multiplier

    # 默认
    return entry_kline.low


def calc_trailing_stop_price(
    highest_after_entry: float,
    trailing_stop_pct: float,
) -> float:
    """
    计算移动止损价（v3.10.1）

    移动止损：当股价上涨时，止损价同步上移（保护浮盈）；
    当股价下跌时，止损价固定不变（不下移）。

    Args:
        highest_after_entry: 入场后最高价
        trailing_stop_pct: 移动止损比例（负值，如 -0.05 表示从高点回落 5%）

    Returns:
        移动止损价
    """
    if highest_after_entry <= 0:
        return 0.0
    return highest_after_entry * (1 + trailing_stop_pct)


# ============================================================
# 核心引擎
# ============================================================


class ShaofuLoopEngine:
    """
    少妇战法六步闭环引擎

    状态机驱动，逐日处理 K 线数据，自动完成：
    - B1 入场检测（含 J 值阈值、N 型上移、缩量回调、MACD 否决）
    - 收盘价止损
    - 卤煮减半止盈
    - 白线两日破位离场
    - 白线死叉黄线紧急离场

    设计原则：
    - Python 层只做数据判断，不生成话术
    - 使用收盘价，不看盘中价（SOP 明确要求）
    - 支持回测场景（顺序遍历历史数据）
    """

    # 指标只需要有限的历史：黄线要 114 根，再留足 EMA 和 KDJ 的收敛长度。
    # 不截断的话每天都从头重算，长序列回测是平方复杂度。
    _INDICATOR_WINDOW = 320

    def __init__(self, config: LoopConfig | None = None) -> None:
        """
        初始化引擎

        Args:
            config: 策略参数配置，None 则使用默认值
        """
        self.config = config or LoopConfig()

    def _prefix(self, klines: list[DailyData], day_idx: int) -> list[DailyData]:
        """截至 day_idx 的 K 线（含），只保留计算指标需要的长度。"""
        return klines[max(0, day_idx + 1 - self._INDICATOR_WINDOW) : day_idx + 1]

    # ----------------------------------------------------------
    # 公开检查方法（供外部测试和调用）
    # ----------------------------------------------------------

    def check_entry(self, klines: list[DailyData]) -> dict[str, Any] | None:
        """
        Step 3: 检查 B1 入场条件（公开接口）

        综合判断：
        1. KDJ J 值 <= 阈值（默认 12，最好负值）
        2. N 型上移结构：近期有 higher lows
        3. 缩量回调：当日成交量低于前日
        4. MACD 未一票否决

        Args:
            klines: 完整 K 线数据（按日期升序）

        Returns:
            信号字典或 None
        """
        # 少妇战法 B1 判定已提升到战法层（modules/strategies/shaofu_b1.py）：
        # 指标级的 B1 波检测 + 牛绳 + N 型结构 + 缩量回调 + J 值阈值。
        facts = detect_shaofu_b1(
            klines,
            j_threshold=self.config.j_threshold,
            vol_shrink_threshold=self.config.vol_shrink_threshold,
        )
        if facts is None:
            return None

        j_val = facts["j_value"]
        today = klines[-1]
        n_structure_ok = facts["n_structure_ok"]
        vol_shrink = facts["vol_shrink"]

        reason_parts = [
            f"J={j_val:.1f}",
            f"振幅={facts['b1_amplitude']:.1f}%",
            f"评分={facts['b1_score']}",
        ]
        if n_structure_ok:
            reason_parts.append("N型上移")
        if vol_shrink:
            reason_parts.append("缩量回调")

        # 多策略共振投票（v3.10.x 合并 loop_engine_enhanced）
        extra = self._vote_signals(klines)
        if extra:
            reason_parts.append(f"共振: {', '.join(extra['triggered_strategies'])}")
            return {
                "is_b1": True,
                "j_value": j_val,
                "entry_price": today.close,
                "signal": True,
                "reason": "B1: " + ", ".join(reason_parts),
                "triggered_strategies": extra["triggered_strategies"],
                "signal_strength": extra["signal_strength"],
            }

        return {
            "is_b1": True,
            "j_value": j_val,
            "entry_price": today.close,
            "signal": True,
            "reason": "B1: " + ", ".join(reason_parts),
        }

    def check_stop_loss(self, current: DailyData, entry_low: float) -> bool:
        """
        Step 4: 检查收盘价止损（公开接口）

        SOP: "看收盘价，不看盘中"
        触发条件：当日收盘价 < 止损位

        Args:
            current: 当日 K 线数据
            entry_low: 止损价位

        Returns:
            是否触发止损
        """
        return current.close < entry_low

    def check_lu_zhu(
        self,
        recent_klines: list[DailyData],
        line_value: float,
        prev_line_value: float | None = None,
    ) -> bool:
        """
        Step 5: 检查卤煮减半条件（公开接口）

        SOP: BBI 线上两根中大阳线 → 减半。中阳线指涨幅 4% 以上。

        Args:
            recent_klines: 最近的 K 线列表（至少 2 根）
            line_value: 当日离场线的值
            prev_line_value: 前一日离场线的值，None 时用当日值

        Returns:
            是否触发卤煮减半
        """
        if len(recent_klines) < 2:
            return False

        today = recent_klines[-1]
        yesterday = recent_klines[-2]
        prev_line = line_value if prev_line_value is None else prev_line_value

        if today.close <= line_value or yesterday.close <= prev_line:
            return False

        minimum = self.config.big_yang_pct
        return (
            today.close > today.open
            and yesterday.close > yesterday.open
            and today.pct_chg >= minimum
            and yesterday.pct_chg >= minimum
        )

    def check_white_line_exit(self, closes: list[float], white_values: list[float]) -> bool:
        """
        Step 6: 检查离场线两日破位（公开接口）

        SOP: 收盘价连续两天跌破离场线 → 清仓。离场线由 ``config.exit_line`` 决定。

        Args:
            closes: 最近 N 天的收盘价列表
            white_values: 最近 N 天的离场线值列表

        Returns:
            是否触发两日破位离场
        """
        return detect_bbi_break_streak(
            closes, white_values, self.config.bbi_break_days, self.config.bbi_break_threshold
        )

    # ----------------------------------------------------------
    # 内部检查方法（供 run_stock 循环使用）
    # ----------------------------------------------------------

    def _check_entry_internal(self, klines: list[DailyData], day_idx: int) -> dict[str, Any] | None:
        """
        内部入口检查（供 run_stock 逐日调用）

        Args:
            klines: K 线数据
            day_idx: 当前日索引

        Returns:
            信号字典（含 reason 字段）或 None
        """
        return self.check_entry(self._prefix(klines, day_idx))

    def _check_stop_loss_internal(
        self,
        klines: list[DailyData],
        day_idx: int,
        trade: LoopTrade,
    ) -> bool:
        """
        内部止损检查（v3.10.1 支持 trailing stop）

        Args:
            klines: K 线数据
            day_idx: 当前日索引
            trade: 当前交易

        Returns:
            是否触发止损（原始止损价 或 移动止损 任一触发）
        """
        current = klines[day_idx]
        # 原始止损检查
        if self.check_stop_loss(current, trade.stop_loss_price):
            return True
        # v3.10.1：移动止损检查（仅在启用时）
        if self.config.trailing_stop_enabled and trade.highest_after_entry > 0:
            trailing_price = calc_trailing_stop_price(trade.highest_after_entry, self.config.trailing_stop_pct)
            if trailing_price > 0 and current.close < trailing_price:
                return True
        return False

    def _exit_line_value(self, sub: list[DailyData]) -> float:
        """当前离场线的值（BBI 或白线）。"""
        if self.config.exit_line == "white":
            return calculate_zg_white(sub)
        return calculate_bbi(sub)

    def _check_lu_zhu_internal(self, klines: list[DailyData], day_idx: int) -> bool:
        """
        内部卤煮检查

        Args:
            klines: K 线数据
            day_idx: 当前日索引

        Returns:
            是否触发卤煮减半
        """
        if day_idx < 25:
            return False

        return self.check_lu_zhu(
            [klines[day_idx - 1], klines[day_idx]],
            self._exit_line_value(self._prefix(klines, day_idx)),
            self._exit_line_value(self._prefix(klines, day_idx - 1)),
        )

    def _check_white_line_exit_internal(self, klines: list[DailyData], day_idx: int) -> bool:
        """
        内部破位检查：收盘连续跌破离场线（BBI 或白线）

        Args:
            klines: K 线数据
            day_idx: 当前日索引

        Returns:
            是否触发两日破位离场
        """
        days_needed = self.config.bbi_break_days
        if day_idx < days_needed:
            return False

        closes = []
        line_values = []
        for i in range(day_idx - days_needed + 1, day_idx + 1):
            sub = self._prefix(klines, i)
            if len(sub) < 10:
                return False
            closes.append(sub[-1].close)
            line_values.append(self._exit_line_value(sub))

        return self.check_white_line_exit(closes, line_values)

    def _check_dead_cross_exit(self, klines: list[DailyData], day_idx: int) -> bool:
        """
        检查白线死叉黄线（紧急离场）

        SOP: 白线死叉黄线 = 无条件清仓
        "白线在黄线上 = 主力牵着牛绳（洗盘），白线在黄线下 = 牛绳断了（反弹）"

        Args:
            klines: K 线数据
            day_idx: 当前日索引

        Returns:
            是否触发白线死叉黄线离场
        """
        sub = self._prefix(klines, day_idx)
        if len(sub) < 20:
            return False

        is_gold, is_dead = detect_double_line_cross(sub)
        return is_dead

    def _check_n_structure(self, klines: list[DailyData], day_idx: int) -> bool:
        """
        检查 N 型上移结构

        在近 20 天内寻找两个低点，后一个低点高于前一个（higher lows）。
        这是 B1 信号的辅助确认条件。

        Args:
            klines: K 线数据
            day_idx: 当前日索引

        Returns:
            是否形成 N 型上移
        """
        lookback = min(day_idx, 20)
        if lookback < 10:
            return True  # 数据不足时放行

        window = klines[day_idx - lookback : day_idx + 1]
        if len(window) < 10:
            return True

        # 将窗口分为前后两半，比较各自的最低价
        mid = len(window) // 2
        first_half = window[:mid]
        second_half = window[mid:]

        if not first_half or not second_half:
            return True

        first_low = min(k.low for k in first_half)
        second_low = min(k.low for k in second_half)

        # 后半段低点 >= 前半段低点（允许 2% 容差）
        return second_low >= first_low * 0.98

    # ----------------------------------------------------------
    # 离场检查（共享逻辑，消除 run_stock / process_day 重复）
    # ----------------------------------------------------------

    def _buy_cost(self) -> float:
        """买入的费率（佣金 + 过户费）。"""
        return self.config.commission_rate + self.config.transfer_fee_rate

    def _sell_cost(self) -> float:
        """卖出的费率（佣金 + 印花税 + 过户费）。"""
        return self.config.commission_rate + self.config.stamp_duty_rate + self.config.transfer_fee_rate

    def net_pnl_pct(self, trade: LoopTrade, exit_price: float) -> float:
        """一笔交易的净收益（%）：含手续费，并把已减半卖出的部分算进去。"""
        invested = trade.entry_price * (1 + self._buy_cost())
        if invested <= 0:
            return 0.0
        proceeds = sum(leg["fraction"] * leg["price"] for leg in trade.partial_exits)
        proceeds += trade.remaining_fraction * exit_price
        return (proceeds * (1 - self._sell_cost()) / invested - 1) * 100

    def _close_trade(self, trade: LoopTrade, date: str, price: float, reason: str, pnl_pct: float | None = None) -> LoopTrade:
        """平仓并返回已完成的交易。不传 pnl_pct 时按净收益计算。"""
        trade.exit_date = date
        trade.exit_price = price
        trade.exit_reason = reason
        trade.pnl_pct = self.net_pnl_pct(trade, price) if pnl_pct is None else pnl_pct
        return trade

    @staticmethod
    def _limit_pct(klines: list[DailyData]) -> float:
        """涨跌停幅度。ST 需要名称才能判断，这里只按代码分板块。"""
        code = klines[0].ts_code
        if code.startswith(("300", "301", "302", "688", "689")):
            return 0.20
        if code.startswith(("920", "430", "83", "87", "88")):
            return 0.30
        return 0.10

    def is_limit_up_open(self, klines: list[DailyData], day_idx: int) -> bool:
        """开盘即涨停，买不进去。"""
        prev_close = klines[day_idx - 1].close
        return prev_close > 0 and klines[day_idx].open >= prev_close * (1 + self._limit_pct(klines)) * 0.999

    def is_limit_down_open(self, klines: list[DailyData], day_idx: int) -> bool:
        """开盘即跌停，卖不出去。"""
        prev_close = klines[day_idx - 1].close
        return prev_close > 0 and klines[day_idx].open <= prev_close * (1 - self._limit_pct(klines)) * 1.001

    def _entry_index(self, klines: list[DailyData], trade: LoopTrade) -> int:
        """买入 K 线下标；外部直接构造的交易没有这个字段，按日期查一次。"""
        if trade.entry_index < 0:
            trade.entry_index = next((i for i, k in enumerate(klines) if k.trade_date == trade.entry_date), 0)
        return trade.entry_index

    def _observe_close(self, klines: list[DailyData], day_idx: int, trade: LoopTrade) -> None:
        """把 day_idx 这根收盘后的状态记到交易上。"""
        bar = klines[day_idx]
        pnl_pct = (bar.close - trade.entry_price) / trade.entry_price * 100
        trade.max_favorable = max(trade.max_favorable, pnl_pct)
        trade.max_adverse = min(trade.max_adverse, pnl_pct)
        trade.highest_after_entry = max(trade.highest_after_entry, bar.high)
        trade.max_close = max(trade.max_close, bar.close)
        trade.holding_days = day_idx - self._entry_index(klines, trade) + 1
        if not trade.line_reclaimed and bar.close > self._exit_line_value(self._prefix(klines, day_idx)):
            trade.line_reclaimed = True
        if self.config.yellow_break_exit and not trade.yellow_reclaimed:
            yellow = calculate_dg_yellow(self._prefix(klines, day_idx))
            trade.yellow_reclaimed = yellow > 0 and bar.close > yellow

    def _check_yellow_break(self, klines: list[DailyData], day_idx: int) -> bool:
        """连续几根收盘都在黄线下（第一根可以忍，之后必须走）。"""
        days = self.config.yellow_break_days
        if day_idx < days:
            return False
        idxs = range(day_idx - days + 1, day_idx + 1)
        yellows = [calculate_dg_yellow(self._prefix(klines, i)) for i in idxs]
        return all(y > 0 for y in yellows) and detect_bbi_break_streak([klines[i].close for i in idxs], yellows, days, 0.0)

    def _check_s1(self, klines: list[DailyData], day_idx: int) -> bool:
        sub = self._prefix(klines, day_idx)
        return detect_s1(sub, len(sub) - 1) is not None

    def _check_fourth_brick(self, klines: list[DailyData], day_idx: int) -> bool:
        """今天正好是第四块红砖。砖值的递推 80 根已经收敛。"""
        return red_brick_streak(klines[max(0, day_idx - 79) : day_idx + 1]) == 4

    def exit_reason(self, klines: list[DailyData], day_idx: int, trade: LoopTrade) -> str:
        """按七层应对的优先级检查 day_idx 收盘后是否该离场，返回原因（空串表示继续持有）。"""
        cfg = self.config
        if trade.holding_days < cfg.min_holding_days:
            return ""

        close = klines[day_idx].close

        # 第 1 层：没涨就走（入场后收盘从未高于成本）
        if (
            cfg.no_rise_days > 0
            and trade.holding_days >= cfg.no_rise_days + cfg.no_rise_grace_days
            and trade.max_close <= trade.entry_price
        ):
            return "没涨就走"

        # 第 2 层：盈转亏必走
        if cfg.profit_to_loss_exit and trade.max_close > trade.entry_price and close < trade.entry_price:
            return "盈转亏"

        # 第 3 层：止损
        if self._check_stop_loss_internal(klines, day_idx, trade):
            return "止损"

        # 第 5 层：S1 与嘀嘀
        if cfg.s1_exit and self._check_s1(klines, day_idx):
            return "S1"
        if cfg.didi_exit and detect_didi(self._prefix(klines, day_idx))["is_didi"]:
            return "嘀嘀"

        # 第 6 层：破位（跌破的前提是站上过）
        if (not cfg.break_requires_reclaim or trade.line_reclaimed) and self._check_white_line_exit_internal(klines, day_idx):
            return "白线跌破" if cfg.exit_line == "white" else "BBI跌破"
        if cfg.yellow_break_exit and trade.yellow_reclaimed and self._check_yellow_break(klines, day_idx):
            return "破黄线"

        # 第 7 层：死叉
        if cfg.dead_cross_exit and self._check_dead_cross_exit(klines, day_idx):
            return "白线死叉黄线"

        return ""

    def _manage_position(
        self, klines: list[DailyData], day_idx: int, trade: LoopTrade
    ) -> tuple[LoopTrade | None, LoopTrade | None]:
        """持仓处理：用昨日收盘决定动作，今日开盘成交。"""
        decision_idx = day_idx - 1
        if decision_idx < self._entry_index(klines, trade):
            return trade, None

        self._observe_close(klines, decision_idx, trade)
        reason = trade.pending_exit_reason or self.exit_reason(klines, decision_idx, trade)
        bar = klines[day_idx]

        if reason:
            if self.is_limit_down_open(klines, day_idx):
                trade.pending_exit_reason = reason
                return trade, None
            fill = bar.open * (1 - self.config.slippage_pct)
            return None, self._close_trade(trade, bar.trade_date, fill, reason)

        # 第 4 层：放飞。两种减半各一次，同一天只减一次
        done = {leg["type"] for leg in trade.partial_exits}
        kind = ""
        if self.config.lu_half and "卤煮减半" not in done and self._check_lu_zhu_internal(klines, decision_idx):
            kind = "卤煮减半"
        elif self.config.fourth_brick_half and "第四块砖" not in done and self._check_fourth_brick(klines, decision_idx):
            kind = "第四块砖"
        if kind and not self.is_limit_down_open(klines, day_idx):
            fill = bar.open * (1 - self.config.slippage_pct)
            trade.partial_exits.append({"date": bar.trade_date, "price": fill, "fraction": trade.remaining_fraction / 2, "type": kind})
            trade.remaining_fraction /= 2
            trade.entry_reason += f" [已{kind}]"

        return trade, None

    def _try_enter(self, ts_code: str, klines: list[DailyData], day_idx: int) -> LoopTrade | None:
        """用昨日收盘判断 B1，今日开盘买入。"""
        signal = self._check_entry_internal(klines, day_idx - 1)
        if signal is None or self.is_limit_up_open(klines, day_idx):
            return None
        bar = klines[day_idx]
        return LoopTrade(
            ts_code=ts_code,
            entry_date=bar.trade_date,
            entry_price=bar.open * (1 + self.config.slippage_pct),
            entry_reason=signal.get("reason", "B1信号"),
            # 买入 K 线的最低价要等它收盘才确定，止损也从这根收盘开始检查
            stop_loss_price=_calc_stop_loss_price(
                klines,
                day_idx,
                self.config.stop_loss_method,
                self.config.stop_loss_pct,
                atr_multiplier=self.config.atr_stop_multiplier,
                atr_window=self.config.atr_stop_window,
            ),
            position_pct=self.config.position_pct,
            triggered_strategies=list(signal.get("triggered_strategies") or []),
            signal_strength=float(signal.get("signal_strength") or 0.0),
            entry_index=day_idx,
        )

    # ----------------------------------------------------------
    # 多策略共振（合并自 loop_engine_enhanced.py；默认关闭以保持原 B1 单策略行为）
    # ----------------------------------------------------------

    def _vote_signals(self, klines: list[DailyData]) -> dict[str, Any] | None:
        """投票决定多策略共振入场。

        当 LoopConfig 中任意 enable_* 为 True 时，按权重统计触发的策略。
        仅在触发策略数 ≥ min_signals 且加权信号强度 ≥ min_signal_strength 时返回。
        """
        cfg = self.config
        any_enabled = cfg.enable_b2 or cfg.enable_changan or cfg.enable_nana or cfg.enable_pinghang
        if not any_enabled:
            return None

        triggered: list[str] = ["B1"]
        details: dict[str, dict[str, Any]] = {"B1": {}}

        if cfg.enable_b2:
            b2 = detect_b2_resonance(klines)
            if b2:
                triggered.append("B2")
                details["B2"] = b2
        if cfg.enable_changan:
            ch = detect_changan_resonance(klines)
            if ch:
                triggered.append("长安")
                details["长安"] = ch
        if cfg.enable_nana:
            n = detect_nana_resonance(klines)
            if n:
                triggered.append("娜娜")
                details["娜娜"] = n
        if cfg.enable_pinghang:
            p = detect_pinghang_resonance(klines)
            if p:
                triggered.append("平行重炮")
                details["平行重炮"] = p

        if len(triggered) < cfg.min_signals:
            return None

        total_strength = sum(cfg.signal_weights.get(s, 1.0) for s in triggered)
        if total_strength < cfg.min_signal_strength:
            return None

        return {
            "triggered_strategies": triggered,
            "signal_strength": total_strength,
            "signal_details": details,
        }

    # ----------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------
    def run_stock(
        self,
        klines: list[DailyData],
        ts_code: str = "",
        market_filter: dict[str, bool] | None = None,
    ) -> list[LoopTrade]:
        """对一只股票运行完整的六步闭环

        Args:
            klines: K 线（升序）
            ts_code: 股票代码，默认取 K 线上的
            market_filter: 择时结果 {信号日: 能否开仓}；None 表示不择时，
                字典里没有的日期视为不能开仓
        """
        if not klines or len(klines) < 31:
            return []

        if not ts_code:
            ts_code = klines[0].ts_code if klines else ""

        completed_trades: list[LoopTrade] = []
        current_trade: LoopTrade | None = None

        for day_idx in range(31, len(klines)):
            allow_entry = market_filter is None or market_filter.get(klines[day_idx - 1].trade_date, False)
            current_trade, completed = self.process_day(ts_code, klines, day_idx, current_trade, allow_entry=allow_entry)
            if completed is not None:
                completed_trades.append(completed)

        # 数据末尾：按最后收盘价结算，标记出来便于统计时区分
        if current_trade is not None:
            last = klines[-1]
            self._observe_close(klines, len(klines) - 1, current_trade)
            completed_trades.append(self._close_trade(current_trade, last.trade_date, last.close, "数据末尾"))

        return completed_trades

    def process_day(
        self,
        ts_code: str,
        klines: list[DailyData],
        day_idx: int,
        current_trade: LoopTrade | None = None,
        allow_entry: bool = True,
    ) -> tuple[LoopTrade | None, LoopTrade | None]:
        """处理单日开盘的动作：用昨日收盘的结论，在今日开盘买入或卖出。

        Returns:
            (updated_trade, completed_trade)
        """
        if day_idx < 31 or day_idx >= len(klines):
            return current_trade, None

        if current_trade is None:
            return (self._try_enter(ts_code, klines, day_idx) if allow_entry else None), None

        return self._manage_position(klines, day_idx, current_trade)


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="少妇战法六步闭环引擎")
    parser.add_argument("ts_code", help="股票代码，如 600487.SH")
    parser.add_argument("--days", type=int, default=240, help="回测天数（默认 240）")
    parser.add_argument(
        "--j-threshold",
        type=float,
        default=rule_value("j_threshold"),
        help="B1 J 值阈值",
    )
    parser.add_argument(
        "--stop-loss-method",
        choices=["entry_low", "n_structure_low", "j_negative_low"],
        default="entry_low",
        help="止损方法",
    )
    parser.add_argument(
        "--bbi-days",
        type=int,
        default=2,
        help="离场线连续跌破天数（默认 2）",
    )

    args = parser.parse_args()

    # 获取 K 线数据
    try:
        from modules.indicators import get_kline_data

        raw_klines = get_kline_data(args.ts_code, args.days)
    except ImportError:
        print("无法导入 get_kline_data，请确保 modules.indicators 可用", file=sys.stderr)
        sys.exit(1)

    if not raw_klines:
        print(f"未获取到 {args.ts_code} 的 K 线数据", file=sys.stderr)
        sys.exit(1)

    # 转换为 DailyData
    klines: list[DailyData] = []
    for k in raw_klines:
        klines.append(
            DailyData(
                ts_code=args.ts_code,
                trade_date=k.trade_date,
                open=k.open,
                high=k.high,
                low=k.low,
                close=k.close,
                vol=k.vol,
                amount=k.amount,
                pct_chg=k.pct_chg,
            )
        )

    # 构建配置并运行
    config = LoopConfig(
        j_threshold=args.j_threshold,
        stop_loss_method=args.stop_loss_method,
        bbi_break_days=args.bbi_days,
    )
    engine = ShaofuLoopEngine(config)
    trades = engine.run_stock(klines, args.ts_code)

    # 输出结果
    print(f"{'=' * 60}")
    print(f"少妇战法闭环回测: {args.ts_code}")
    print(f"{'=' * 60}")
    print(f"K 线天数:   {len(klines)}")
    print(f"交易次数:   {len(trades)}")

    if trades:
        wins = sum(1 for t in trades if t.pnl_pct > 0)
        losses = sum(1 for t in trades if t.pnl_pct < 0)
        win_rate = wins / len(trades) * 100 if trades else 0
        avg_pnl = sum(t.pnl_pct for t in trades) / len(trades)
        avg_days = sum(t.holding_days for t in trades) / len(trades)

        print(f"盈利次数:   {wins}")
        print(f"亏损次数:   {losses}")
        print(f"胜率:       {win_rate:.1f}%")
        print(f"平均收益:   {avg_pnl:+.2f}%")
        print(f"平均持仓:   {avg_days:.1f} 天")
        print(f"{'=' * 60}")

        print("\n交易明细:")
        for i, t in enumerate(trades, 1):
            status = "+" if t.pnl_pct > 0 else "-" if t.pnl_pct < 0 else "="
            partial = " [减半]" if t.partial_exits else ""
            print(
                f"  {i}. {t.entry_date} → {t.exit_date}  "
                f"{t.pnl_pct:+.2f}%{partial}  "
                f"({t.exit_reason})  "
                f"持仓{t.holding_days}天  "
                f"最大浮盈{t.max_favorable:+.1f}%  "
                f"最大浮亏{t.max_adverse:+.1f}%"
            )
    else:
        print("未产生交易信号")

    print()
