"""用通达信自己的公式引擎选股（通达信 TQ 接口 ``PYPlugins/sys/tqcenter.py``）。

公式在通达信公式管理器里维护，这里按公式名调用，结果就是通达信算出来的，不用再移植。
前提是通达信客户端正在运行：TQ 接口连到运行中的客户端上计算。

- 只用公式列表、批量选股、股票列表这几类只读接口；tqcenter 里的下单、撤单、查持仓等
  交易函数一律不调用；
- tqcenter 会往 stdout 打印提示，这里统一转到 stderr，保证命令行 JSON 输出干净；
- 2026-11-04 用三个自定义选股公式对照 11-03 的一键选股（沪深主板）：结果一致，
  只多出两只上市不足 120 根 K 线的次新股（公式里有 MA(C,114)，一键选股不选它们），
  所以默认剔除 K 线不足 ``MIN_BARS`` 根的。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

def tq_dir() -> Path | None:
    """通达信 TQ 接口所在目录：TDX_DIR/PYPlugins/sys。"""
    root = os.environ.get("TDX_DIR", "").strip()
    return Path(root) / "PYPlugins" / "sys" if root else None
#: 选股公式类型（tqcenter：0 指标、1 条件选股、2 专家系统）
FORMULA_XG = 1
#: K 线不足这么多根的不选（公式里最长的是 MA(C,114)；一键选股同样不选次新股）
MIN_BARS = 120
#: 取多少天的历史给公式算（覆盖 MA114 + 递推指标收敛）
HISTORY_DAYS = 500

_lock = threading.Lock()
_tq = None


class TqUnavailable(RuntimeError):
    """通达信没开、TQ 接口加载失败或调用失败。"""


def tdx_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TdxW.exe", "/NH"], capture_output=True,
                         text=True, encoding="gbk", errors="replace", check=False).stdout
    return "TdxW.exe" in out


def _client():
    """懒加载 tqcenter（import 时会加载 TPythClient.dll），并初始化连接。"""
    global _tq
    if _tq is not None:
        return _tq
    tq_path = tq_dir()
    if tq_path is None:
        raise TqUnavailable("没有配置通达信目录（.env 里的 TDX_DIR）")
    if not (tq_path / "tqcenter.py").is_file():
        raise TqUnavailable(f"找不到通达信 TQ 接口：{tq_path / 'tqcenter.py'}（需要带 TQ 策略插件的通达信金融终端）")
    if not tdx_running():
        raise TqUnavailable("通达信没有运行，TQ 接口无法计算")
    if str(tq_path) not in sys.path:
        sys.path.insert(0, str(tq_path))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from tqcenter import tq  # noqa: PLC0415 — 只在需要时加载 DLL

            tq.initialize(__file__)
    except Exception as e:  # noqa: BLE001 — DLL / 连接失败统一报不可用
        raise TqUnavailable(f"TQ 接口初始化失败：{e}") from e
    _tq = tq
    return tq


def close() -> None:
    global _tq
    if _tq is not None:
        with contextlib.redirect_stdout(sys.stderr):
            _tq.close()
        _tq = None


def tq_quote(codes: list[str], today: str | None = None) -> dict[str, dict]:
    """通达信 TQ 取当天日线，口径同 ``a_stock_data_client.tencent_quote``：``{6 位代码: {price, last_close, open,
    high, low, volume_lots, amount_wan, quote_time}}``，给每日报告 / Z哥诊断补「本地还没下载的当天 K 线」。

    - 用 ``get_market_data``（不复权日线，取最后两根）：开高低收单位元，Volume 单位股，Amount 单位万元。
      2026-11-05 实测 601988 / 510300 / 000001 / 300750：11-04 那根和本地 .day 文件逐项一致；
    - 只返回最后一根就是 ``today`` 的票（通达信没连上行情时最后一根停在昨天，留给下一个来源）；
    - 今天除权除息的（前复权和不复权的昨收对不上）不返回：TQ 的「昨收」是不复权的旧价，核对不出除权，
      交给腾讯等来源用交易所给的昨收核对；
    - ``refresh_kline`` 实测只刷新通达信的内存缓存，不写 vipdoc，这里不调用；本函数也不写任何文件。

    通达信没开、接口失败时抛 OSError（调用方据此换下一个来源）。
    """
    from datetime import datetime

    from modules.tdx_local_client import to_symbol, to_ts_code

    today = today or datetime.now().strftime("%Y%m%d")
    wanted = {to_ts_code(to_symbol(code)): code for code in codes}
    if not wanted:
        return {}
    try:
        with _lock:
            tq = _client()
            with contextlib.redirect_stdout(sys.stderr):
                raw = tq.get_market_data(
                    field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                    stock_list=list(wanted), period="1d", count=2, dividend_type="none",
                )
                front = tq.get_market_data(field_list=["Close"], stock_list=list(wanted), period="1d", count=2,
                                           dividend_type="front")
    except TqUnavailable as e:
        raise OSError(str(e)) from e
    except Exception as e:  # noqa: BLE001 — DLL / 连接异常一律当作这个来源不可用
        raise OSError(f"TQ 取行情失败：{e}") from e
    if not isinstance(raw, dict) or "Close" not in raw:
        raise OSError("TQ 没有返回日线")
    out: dict[str, dict] = {}
    closes = raw["Close"]
    for ts_code, code in wanted.items():
        if ts_code not in closes.columns:
            continue
        col = closes[ts_code].dropna()
        if len(col) < 2:
            continue
        when = col.index[-1]
        when = when.strftime("%Y%m%d") if hasattr(when, "strftime") else str(when).replace("-", "")[:8]
        if when != today:
            continue
        prev_raw = float(col.iloc[-2])
        try:
            prev_front = float(front["Close"][ts_code].dropna().iloc[-2])
        except (KeyError, IndexError, TypeError):
            continue  # 核对不了除权就不用这个来源
        if abs(prev_front - prev_raw) > 1e-6:
            continue  # 今天除权除息
        row = {f: float(raw[f][ts_code].iloc[-1]) for f in ("Open", "High", "Low", "Close", "Volume", "Amount")}
        out[code] = {
            "price": row["Close"],
            "last_close": prev_raw,
            "open": row["Open"],
            "high": row["High"],
            "low": row["Low"],
            "volume_lots": row["Volume"] / 100,
            "amount_wan": row["Amount"],
            "quote_time": f"{today}150000",
        }
    return out


#: fill_today_bars 看到这个标记就不在批次之间停顿（本机接口，不怕限流）
tq_quote.local = True  # type: ignore[attr-defined]


def list_formulas(include_system: bool = False) -> list[dict]:
    """通达信里的条件选股公式：[{code, name, system}]。默认只列用户自己的。"""
    with _lock:
        tq = _client()
        with contextlib.redirect_stdout(sys.stderr):
            items = tq.formula_get_all(formula_type=FORMULA_XG)
    if not isinstance(items, list):
        raise TqUnavailable("读取通达信公式列表失败")
    # 用户公式的 acName 多半是空的，公式名就是 acCode（公式管理器里看到的名字）
    out = [{"code": it.get("acCode", ""), "name": it.get("acName") or it.get("acCode", ""), "system": bool(it.get("isSys"))}
           for it in items]
    return [f for f in out if include_system or not f["system"]]


@dataclass
class TqSelection:
    formula: str
    trade_date: str
    picks: list[str] = field(default_factory=list)  # 600795.SH 形式
    scanned: int = 0
    skipped_short: list[str] = field(default_factory=list)  # 次新股（K 线不足 MIN_BARS）
    output: str = ""  # 公式的选股输出线名，如「选股」「XG」


def parse_selection(result: dict, trade_date: str) -> TqSelection:
    """formula_process_mul_xg 的返回 → 入选名单。只认日期等于 trade_date 且值非 0 的；
    停牌股最后一根早于 trade_date，不算。"""
    sel = TqSelection(formula="", trade_date=trade_date)
    for code, lines in result.items():
        if code == "ErrorId" or not isinstance(lines, dict) or not lines:
            continue
        name, series = next(iter(lines.items()))
        sel.output = sel.output or name
        if not series or series[-1].get("Date") != trade_date:
            continue
        sel.scanned += 1
        try:
            hit = float(series[-1].get("Value") or 0) != 0
        except ValueError:
            hit = False
        if hit:
            sel.picks.append(code)
    sel.picks.sort()
    return sel


def latest_date(result: dict) -> str:
    """结果里最晚的日期（停牌股的最后一根更早，不影响）。"""
    dates = [
        series[-1].get("Date", "")
        for code, lines in result.items()
        if code != "ErrorId" and isinstance(lines, dict) and lines
        for series in [next(iter(lines.values()))]
        if series
    ]
    return max(dates) if dates else ""


def drop_short_history(sel: TqSelection, bar_count, min_bars: int = MIN_BARS) -> TqSelection:
    """剔除 K 线不足 min_bars 根的次新股；bar_count(600795.SH, 选股日) 返回到选股日为止的本地 K 线根数。"""
    keep = []
    for code in sel.picks:
        (keep if bar_count(code, sel.trade_date) >= min_bars else sel.skipped_short).append(code)
    sel.picks = keep
    return sel


def local_bar_count(code: str, trade_date: str = "") -> int:
    """本地 K 线根数，只数日期不晚于 trade_date 的（补算历史日期时按当时的上市天数判断次新股）。"""
    from modules.tdx_local_client import TdxLocalClient, configured_root

    root = configured_root()
    series = TdxLocalClient(root).raw_series(code) if root else None
    if series is None:
        return 0
    if trade_date:
        return int((series["date"] <= int(trade_date)).sum())
    return len(series["date"])


def select(formula: str, trade_date: str | None = None, stock_list: list[str] | None = None, min_bars: int = MIN_BARS,
           bar_count=local_bar_count) -> TqSelection:
    """用通达信引擎跑一个条件选股公式。

    trade_date 不传：用通达信最新的一根（盘中就是当天实时这根，和一键选股一样），实际日期写进结果。
    trade_date 传了：取那一根。注意 DYNAINFO 之类的实时函数只反映「现在」的行情，
    补算历史日期时这类公式会不准（如金砖共振的换手率条件），调用方要自己判断能不能用。
    stock_list 用 600795.SH 形式；不传时取通达信的全部 A 股。
    """
    from datetime import datetime, timedelta

    if trade_date:
        start = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=int(HISTORY_DAYS * 1.5))).strftime("%Y%m%d")
        # count=0 才会按 start/end 取；count 非 0 时 tqcenter 忽略日期
        window = {"start_time": start, "end_time": trade_date, "count": 0}
    else:
        window = {"count": HISTORY_DAYS}
    with _lock:
        tq = _client()
        with contextlib.redirect_stdout(sys.stderr):
            stocks = stock_list or tq.get_stock_list(market="5")
            result = tq.formula_process_mul_xg(
                formula_name=formula, formula_arg="", return_count=1, return_date=True,
                stock_list=stocks, stock_period="1d", dividend_type=1, **window,
            )
    if not isinstance(result, dict) or result.get("ErrorId") not in ("0", "19"):
        raise TqUnavailable(f"通达信选股失败：{formula}")
    if result.get("ErrorId") == "19":
        raise TqUnavailable(f"通达信选股返回数据过大被截断：{formula}")
    if not trade_date:
        trade_date = latest_date(result)
    sel = drop_short_history(parse_selection(result, trade_date), bar_count, min_bars)
    sel.formula = formula
    return sel


def filter_universe(codes: list[str], boards: tuple[str, ...] | None, exclude_st: bool = True) -> list[str]:
    """按板块和 ST 过滤（板块名同 tdx_local_client：主板 / 创业板 / 科创板 / 北交所）；boards 为 None 不过滤板块。"""
    from modules.tdx_local_client import board_of, configured_root, is_st_name, local_stock_name, to_symbol

    out = []
    for code in codes:
        if boards is not None and board_of(to_symbol(code)) not in boards:
            continue
        if exclude_st and configured_root() is not None and is_st_name(local_stock_name(code) or ""):
            continue
        out.append(code)
    return out
