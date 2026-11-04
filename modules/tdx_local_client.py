"""本地通达信盘后数据源（只读）。

配置 ``TDX_DIR``（如 ``D:/new_tdx64``）后，K 线、股票列表、名称和行业都从本地文件
读取，不依赖任何网络接口，也不需要启动通达信客户端。

读取的文件：

- ``vipdoc/{sh,sz,bj}/lday/*.day``：日线，每条 32 字节（``<IIIIIfII``），无文件头
- ``T0002/hq_cache/gbbq``：股本变迁与除权除息（加密，由 pytdx 解密）
- ``T0002/hq_cache/{shs,szs,bjs}.tnf``：代码与简称（50 字节文件头 + 360 字节定长记录）
- ``T0002/hq_cache/base.dbf``：流通股本、总股本、上市日期
- ``T0002/hq_cache/tdxhy.cfg`` + ``tdxzs3.cfg``：行业

约定：

- **绝不写入通达信目录**。gbbq 解密结果缓存到 ``~/.cache/zettaranc/tdx``。
- 前复权在**读取时**按 gbbq 现算，不落库。因此本地不存在"旧前复权基准和新行混用"的问题。
- 两种前复权口径：看盘和选股用通达信口径（含减法，见 ``tdx_qfq_coefficients``），
  回测算收益用等比口径（见 ``ratio_qfq_factors``）。
- 拿不到 gbbq 时抛错，不把未复权价格冒充前复权价格。
- 股票键统一用"市场前缀 + 六位代码"（``sh600519``）：``sh000001`` 是上证指数，
  ``sz000001`` 是平安银行，六位代码不唯一。

单位与 tushare 对齐：``vol`` 为手（文件内为股），``amount`` 为千元（文件内为元）。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import struct
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .core.errors import ErrorCode, ZettarancError
from .core.kline import resolve_kline_limit

logger = logging.getLogger(__name__)

_DAY_DTYPE = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<u4"),
        ("high", "<u4"),
        ("low", "<u4"),
        ("close", "<u4"),
        ("amount", "<f4"),
        ("volume", "<u4"),
        ("reserved", "<u4"),
    ]
)

# (市场前缀, 代码前缀) → 板块。其余代码（指数、基金、债券等）不算 A 股个股。
_BOARD_BY_PREFIX: dict[str, dict[tuple[str, ...], str]] = {
    "sh": {("600", "601", "603", "605"): "主板", ("688", "689"): "科创板"},
    "sz": {("000", "001", "002", "003"): "主板", ("300", "301", "302"): "创业板"},
    "bj": {("920", "430", "83", "87", "88"): "北交所"},
}

_EXCHANGE_BY_MARKET = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}
_MARKET_BY_GBBQ = {0: "sz", 1: "sh", 2: "bj"}
_TNF_FILES = {"sh": "shs.tnf", "sz": "szs.tnf", "bj": "bjs.tnf"}
_TNF_HEADER = 50
_TNF_RECORD = 360
_TNF_NAME_OFFSET = 31
_GBBQ_DIVIDEND_CATEGORY = 1  # 除权除息
_GBBQ_SHARE_CATEGORIES = (2, 3, 5, 6, 7, 8, 9, 10, 11, 12)  # 流通股本会变的记录：送配上市、解禁、增发等
_MARKET_INDEX = "000001.SH"  # 用上证指数的最新日期代表本地行情的最新交易日


def configured_root() -> Path | None:
    """返回已配置且结构正确的通达信目录；未配置或目录不对时返回 None。"""
    raw = os.environ.get("TDX_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not (root / "vipdoc").is_dir():
        logger.warning("[tdx] TDX_DIR=%s 下没有 vipdoc 目录，已忽略", raw)
        return None
    return root


def board_of(symbol: str) -> str | None:
    """按"市场前缀 + 代码"判断板块；不是 A 股个股返回 None。"""
    code = symbol[2:]
    for prefixes, board in _BOARD_BY_PREFIX.get(symbol[:2], {}).items():
        if code.startswith(prefixes):
            return board
    return None


def to_symbol(ts_code: str) -> str:
    """``600519.SH`` → ``sh600519``。没写交易所时按代码段推断，``600519`` 也能查到。"""
    code, _, exchange = ts_code.strip().partition(".")
    if not exchange:
        exchange = "sh" if code.startswith(("5", "6", "9")) and not code.startswith("92") else "bj" if code.startswith(("4", "8", "92")) else "sz"
    return f"{exchange.lower()}{code}"


def to_ts_code(symbol: str) -> str:
    """``sh600519`` → ``600519.SH``。"""
    return f"{symbol[2:]}.{symbol[:2].upper()}"


def business_days_behind(trade_date: str, today: date | None = None) -> int:
    """``trade_date`` 距今隔了几个工作日（只排除周末，不含法定节假日）。"""
    start = datetime.strptime(trade_date, "%Y%m%d").date()
    end = today or date.today()
    if end <= start:
        return 0
    # busday_count 含起点不含终点，平移一天后等于"起点之后到今天"的工作日数。
    # 单位写明是天：裸整数 +1 的写法 NumPy 已弃用，将来会直接报错。
    one_day = np.timedelta64(1, "D")
    return int(np.busday_count(np.datetime64(start, "D") + one_day, np.datetime64(end, "D") + one_day))


@functools.lru_cache(maxsize=2048)
def _read_day_file(path: str, mtime_ns: int, size: int) -> np.ndarray:
    """读取并校验一个 .day 文件，返回按日期升序的结构化数组。"""
    if size % _DAY_DTYPE.itemsize:
        raise ValueError(f"{path} 长度 {size} 不是 32 的整数倍")
    bars = np.fromfile(path, dtype=_DAY_DTYPE)
    ok = (
        (bars["date"] >= 19900101)
        & (bars["date"] <= 21001231)
        & (bars["close"] > 0)
        & (bars["high"] >= bars["low"])
        & (bars["high"] >= bars["open"])
        & (bars["high"] >= bars["close"])
        & (bars["low"] <= bars["open"])
        & (bars["low"] <= bars["close"])
    )
    if not ok.all():
        logger.warning("[tdx] %s 有 %d 条非法记录，已跳过", path, int((~ok).sum()))
        bars = bars[ok]
    return bars


def _load_gbbq_events(gbbq_path: Path) -> dict[str, np.ndarray]:
    """解密 gbbq，返回 {symbol: 除权除息事件数组}，结果按文件版本缓存。

    事件列依次为：日期、每 10 股派现、配股价、每 10 股送转、每 10 股配股。
    全量解密约 15 秒，多进程选股时每个进程都解一次不可接受，所以缓存到磁盘。
    缓存放在用户目录，不随 DATA_DIR 变化，也不写入通达信目录。
    """
    stat = gbbq_path.stat()
    cache_file = _cache_dir() / f"gbbq_{stat.st_size}_{stat.st_mtime_ns}.npz"
    if cache_file.exists():
        try:
            return _read_events_cache(cache_file)
        except (OSError, ValueError, KeyError) as e:
            logger.warning("[tdx] gbbq 缓存损坏，重新解密: %s", e)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            from pytdx.reader.gbbq_reader import GbbqReader
    except ImportError as e:
        raise ZettarancError(
            ErrorCode.DATA_SOURCE_ERROR,
            "读取通达信除权数据需要 pytdx：pip install pytdx",
            cause=e,
        ) from e

    df = GbbqReader().get_df(str(gbbq_path))
    df = df[df["category"] == _GBBQ_DIVIDEND_CATEGORY]
    events: dict[str, np.ndarray] = {}
    for (market, code), group in df.groupby(["market", "code"]):
        prefix = _MARKET_BY_GBBQ.get(int(market))
        if prefix is None:
            continue
        events[f"{prefix}{code}"] = group[
            ["datetime", "hongli_panqianliutong", "peigujia_qianzongguben", "songgu_qianzongguben", "peigu_houzongguben"]
        ].to_numpy(dtype=np.float64)

    try:
        _write_events_cache(cache_file, events)
    except OSError as e:
        logger.warning("[tdx] gbbq 缓存写入失败，下次仍需重新解密: %s", e)
    return events


def _load_gbbq_floats(gbbq_path: Path) -> dict[str, np.ndarray]:
    """解密 gbbq，返回 {symbol: 流通股本变动数组}，单位万股，结果按文件版本缓存。

    列依次为：日期、变动前流通盘、变动后流通盘。pytdx 没有给股本类记录的字段命名，
    这个读法核对过：2026-11-01 全市场最后一次变动后的流通盘，和 base.dbf 的流通股本
    5581 只里 5573 只相差不超过 1%。
    """
    stat = gbbq_path.stat()
    cache_file = _cache_dir() / f"gbfloat_{stat.st_size}_{stat.st_mtime_ns}.npz"
    if cache_file.exists():
        try:
            return _read_events_cache(cache_file)
        except (OSError, ValueError, KeyError) as e:
            logger.warning("[tdx] 股本缓存损坏，重新解密: %s", e)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        from pytdx.reader.gbbq_reader import GbbqReader

    df = GbbqReader().get_df(str(gbbq_path))
    df = df[df["category"].isin(_GBBQ_SHARE_CATEGORIES)].sort_values("datetime")
    floats: dict[str, np.ndarray] = {}
    for (market, code), group in df.groupby(["market", "code"]):
        prefix = _MARKET_BY_GBBQ.get(int(market))
        if prefix is None:
            continue
        floats[f"{prefix}{code}"] = group[["datetime", "hongli_panqianliutong", "songgu_qianzongguben"]].to_numpy(dtype=np.float64)

    try:
        _write_events_cache(cache_file, floats)
    except OSError as e:
        logger.warning("[tdx] 股本缓存写入失败，下次仍需重新解密: %s", e)
    return floats


def float_shares_on(dates: np.ndarray, events: np.ndarray | None) -> np.ndarray | None:
    """每个交易日当时的流通股本（万股）。第一次变动之前用变动前的流通盘。"""
    if events is None or not len(events):
        return None
    pos = np.searchsorted(events[:, 0], dates, side="right") - 1
    before = events[0, 1] if events[0, 1] > 0 else events[0, 2]
    shares = np.where(pos >= 0, events[np.clip(pos, 0, None), 2], before)
    return np.where(shares > 0, shares, np.nan)


def _cache_dir() -> Path:
    """gbbq 解密结果的缓存目录。"""
    return Path.home() / ".cache" / "zettaranc" / "tdx"


def _write_events_cache(path: Path, events: dict[str, np.ndarray]) -> None:
    """把事件字典拼成三个数组写入 npz（不用 pickle：缓存文件不该能执行代码）。"""
    symbols = sorted(events)
    lengths = np.array([len(events[s]) for s in symbols], dtype=np.int64)
    data = np.concatenate([events[s] for s in symbols]) if symbols else np.empty((0, 5))
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再改名：多进程同时构建缓存时，读者不会读到半个文件
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with tmp.open("wb") as f:
        np.savez(f, symbols=np.array(symbols), lengths=lengths, data=data)
    tmp.replace(path)
    # 通达信每次更新都会换 gbbq，旧缓存留着没用
    for old in path.parent.glob(f"{path.name.split('_')[0]}_*.npz"):
        if old != path:
            old.unlink(missing_ok=True)


def _read_events_cache(path: Path) -> dict[str, np.ndarray]:
    """读取 :func:`_write_events_cache` 写的缓存。"""
    with np.load(path, allow_pickle=False) as z:
        symbols = z["symbols"]
        lengths = z["lengths"]
        data = z["data"]
    events: dict[str, np.ndarray] = {}
    start = 0
    for symbol, length in zip(symbols.tolist(), lengths.tolist()):
        events[symbol] = data[start : start + length]
        start += length
    return events


def tdx_qfq_coefficients(dates: np.ndarray, events: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """通达信口径的前复权系数：``复权价 = A × 原价 + B``（最新一根 A=1、B=0）。

    每个事件把除权日之前的价格换成 ``(P - c) / m``，其中
    ``c = (派现 - 配股数 × 配股价) / 10``、``m = 1 + (送转数 + 配股数) / 10``，多个事件逐次复合。
    现金分红是减法，所以这不是纯比例关系。这个口径与腾讯前复权逐分一致，
    开源实现 injoyai/tdx 的测试把它对齐到了通达信桌面端。

    除权日在最新 K 线之后的事件（已公告未除权）尚未生效，必须丢弃。
    除权日若停牌，落到其后第一根 K 线。
    """
    a = np.ones(len(dates), dtype=np.float64)
    b = np.zeros(len(dates), dtype=np.float64)
    if events is None or not len(events) or not len(dates):
        return a, b
    for event_date, cash, rights_price, bonus, rights in events[np.argsort(events[:, 0])[::-1]]:
        if int(event_date) > dates[-1]:
            continue
        idx = int(np.searchsorted(dates, int(event_date), side="left"))
        if idx <= 0:
            continue
        m = 1 + (bonus + rights) / 10
        c = (cash - rights * rights_price) / 10
        if m <= 0:
            logger.warning("[tdx] 除权比例异常，跳过事件 %s: m=%s", int(event_date), m)
            continue
        b[:idx] -= a[:idx] * c / m
        a[:idx] /= m
    return a, b


def ratio_qfq_factors(dates: np.ndarray, closes: np.ndarray, events: np.ndarray | None) -> np.ndarray:
    """等比前复权因子（最新一根为 1）：除权日之前乘以 除权参考价 / 昨收。

    等比序列保持每根 K 线的真实涨跌幅，用来算回测收益。通达信口径含减法，离最新日
    越远，同样的涨跌在价格上被放大得越多，不适合直接算收益。
    """
    factors = np.ones(len(closes), dtype=np.float64)
    if events is None or not len(events) or not len(dates):
        return factors
    for event_date, cash, rights_price, bonus, rights in events:
        if int(event_date) > dates[-1]:
            continue
        idx = int(np.searchsorted(dates, int(event_date), side="left"))
        if idx <= 0:
            continue
        prev_close = closes[idx - 1]
        reference = (prev_close * 10 - cash + rights * rights_price) / (10 + bonus + rights)
        if prev_close <= 0 or reference <= 0:
            logger.warning("[tdx] 除权参考价异常，跳过事件 %s: %s", int(event_date), reference)
            continue
        factors[:idx] *= reference / prev_close
    return factors


def _round_half_up(values: np.ndarray, decimals: int = 2) -> np.ndarray:
    """四舍五入到分（ETF 到厘）；np.round 是银行家舍入，和软件显示会差一位。"""
    unit = 10**decimals
    return np.floor(values * unit + 0.5 + 1e-9) / unit


# 场内 ETF 代码段（同 DSH bot-api local-klines.ts 的 ETF_PREFIXES）
_ETF_PREFIXES = {"sh": ("51", "56", "58"), "sz": ("159",)}
#: ETF 单日涨跌幅上限 20%，收盘到收盘超过 25% 只能是份额拆分或合并
_ETF_SPLIT_JUMP = 0.25


def is_etf(symbol: str) -> bool:
    """``sh588200`` 这样的代码是否场内 ETF。"""
    return symbol[2:].startswith(_ETF_PREFIXES.get(symbol[:2], ()))


def decode_bars(bars: np.ndarray, etf: bool) -> dict[str, np.ndarray]:
    """原始 .day 记录 → 自然单位：价格元、成交量股、成交额元。

    个股：价格 ×100 存，成交量单位股。ETF（2026-11-03 在 DSH 工作台核对 588200）：价格 ×1000 存；
    成交量一般按股存，会溢出 uint32 时改按手存，逐根用「成交额 ÷ 成交量 落在当天高低价之间」判断，
    两种都不像的记录丢掉。gbbq 不记 ETF 份额拆分，只保留最后一次拆分之后的历史。
    """
    scale = 1000.0 if etf else 100.0
    out = {f: bars[f].astype(np.float64) / scale for f in ("open", "high", "low", "close")}
    out["date"] = bars["date"].astype(np.int64)
    out["amount_yuan"] = bars["amount"].astype(np.float64)
    volume = bars["volume"].astype(np.float64)
    if etf:
        lo, hi = out["low"] * 0.98, out["high"] * 1.02
        with np.errstate(divide="ignore", invalid="ignore"):
            per_share = out["amount_yuan"] / volume
            per_lot = per_share / 100
        as_shares = (volume > 0) & (per_share >= lo) & (per_share <= hi)
        as_lots = (volume > 0) & ~as_shares & (per_lot >= lo) & (per_lot <= hi)
        keep = as_shares | as_lots | (volume == 0)
        volume = np.where(as_lots, volume * 100, volume)
        out = {k: v[keep] for k, v in out.items()}
        volume = volume[keep]
        close = out["close"]
        jumps = np.flatnonzero(np.abs(close[1:] / close[:-1] - 1) > _ETF_SPLIT_JUMP) if len(close) > 1 else []
        if len(jumps):
            start = int(jumps[-1]) + 1
            out = {k: v[start:] for k, v in out.items()}
            volume = volume[start:]
    out["volume_shares"] = volume
    return out


class TdxLocalClient:
    """本地通达信行情读取器。实例无状态，可安全地在多进程间传递。"""

    def __init__(self, root: Path | str | None = None) -> None:
        resolved = Path(root) if root else configured_root()
        if resolved is None:
            raise ZettarancError(ErrorCode.CONFIG_MISSING, "未配置 TDX_DIR，或该目录下没有 vipdoc")
        self.root = resolved

    # ---------- 文件定位 ----------

    def _day_path(self, symbol: str) -> Path:
        return self.root / "vipdoc" / symbol[:2] / "lday" / f"{symbol}.day"

    @property
    def _hq_cache(self) -> Path:
        return self.root / "T0002" / "hq_cache"

    # ---------- K 线 ----------

    def read_raw_bars(self, ts_code: str) -> np.ndarray | None:
        """读取未复权原始记录；文件不存在或损坏时返回 None。"""
        path = self._day_path(to_symbol(ts_code))
        try:
            stat = path.stat()
            return _read_day_file(str(path), stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("[tdx] 读取 %s 失败: %s", path, e)
            return None

    def raw_series(self, ts_code: str) -> dict[str, np.ndarray] | None:
        """未复权 K 线，自然单位（价格元、成交量股、成交额元），ETF 已按自己的口径解码。"""
        bars = self.read_raw_bars(ts_code)
        if bars is None or not len(bars):
            return None
        series = decode_bars(bars, is_etf(to_symbol(ts_code)))
        return series if len(series["date"]) else None

    def _events(self) -> dict[str, np.ndarray]:
        path = self._hq_cache / "gbbq"
        try:
            stat = path.stat()
        except OSError as e:
            raise ZettarancError(ErrorCode.DATA_SOURCE_ERROR, f"找不到通达信除权文件: {path}", cause=e) from e
        return _cached_events(str(path), stat.st_mtime_ns, stat.st_size)

    def _float_events(self, ts_code: str) -> np.ndarray | None:
        """这只股票的流通股本变动；读不到时返回 None，换手率就留空，不影响 K 线。"""
        path = self._hq_cache / "gbbq"
        try:
            stat = path.stat()
            return _cached_floats(str(path), stat.st_mtime_ns, stat.st_size).get(to_symbol(ts_code))
        except (OSError, ValueError, KeyError, ImportError, struct.error) as e:
            logger.warning("[tdx] 读取流通股本失败，换手率留空: %s", e)
            return None

    def prepare(self) -> None:
        """预热除权和股本缓存（首次约 30 秒，之后读磁盘缓存）。"""
        self._events()
        self._float_events("000001.SZ")

    def _rows(self, ts_code: str, basis: str) -> list[dict]:
        """按价格口径生成 K 线：``display`` 通达信口径，``return`` 等比口径，``raw`` 不复权。"""
        series = self.raw_series(ts_code)
        if series is None:
            return []
        etf = is_etf(to_symbol(ts_code))
        dates = series["date"]
        raw = {f: series[f] for f in ("open", "high", "low", "close")}
        volume_shares = series["volume_shares"]
        amount_yuan = series["amount_yuan"]
        extra = _today_overlay.get(to_symbol(ts_code))
        if extra is not None and int(extra["trade_date"]) > dates[-1]:
            dates = np.append(dates, int(extra["trade_date"]))
            raw = {f: np.append(v, extra[f]) for f, v in raw.items()}
            volume_shares = np.append(volume_shares, extra["volume_shares"])
            amount_yuan = np.append(amount_yuan, extra["amount_yuan"])
        if basis == "raw":
            prices = raw
        else:
            events = self._events().get(to_symbol(ts_code))
            if basis == "return":
                factors = ratio_qfq_factors(dates, raw["close"], events)
                prices = {f: v * factors for f, v in raw.items()}
            elif basis == "display":
                a, b = tdx_qfq_coefficients(dates, events)
                prices = {f: _round_half_up(a * v + b, 3 if etf else 2) for f, v in raw.items()}
            else:
                raise ValueError(f"未知价格口径: {basis}")
        close = prices["close"]
        prev = np.concatenate(([close[0]], close[:-1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = np.where(prev != 0, (close / prev - 1) * 100, 0.0)
        vols = volume_shares / 100
        amounts = amount_yuan / 1000
        rows = [
            {
                "ts_code": ts_code,
                "trade_date": str(int(dates[i])),
                "open": round(float(prices["open"][i]), 4),
                "high": round(float(prices["high"][i]), 4),
                "low": round(float(prices["low"][i]), 4),
                "close": round(float(close[i]), 4),
                "vol": float(vols[i]),
                "amount": float(amounts[i]),
                "pct_chg": round(float(pct[i]), 4),
            }
            for i in range(len(dates))
        ]
        shares = float_shares_on(dates, self._float_events(ts_code))
        if shares is not None:
            # 手 / 万股 正好是百分比：手 × 100 股 ÷ (万股 × 10000 股) × 100%
            turnover = vols / shares
            for i, row in enumerate(rows):
                if np.isfinite(turnover[i]):
                    row["turnover_rate"] = round(float(turnover[i]), 4)
        return rows

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
        basis: str = "display",
    ) -> list[dict]:
        """返回升序 K 线。``days`` 与日期区间的优先级见 ``resolve_kline_limit``。

        ``basis``：``display`` 通达信前复权（看盘、选股、指标），``return`` 等比前复权
        （回测算收益），``raw`` 不复权（指数）。
        """
        rows = self._rows(ts_code, basis)
        if start_date:
            rows = [r for r in rows if r["trade_date"] >= start_date]
        if end_date:
            rows = [r for r in rows if r["trade_date"] <= end_date]
        limit = resolve_kline_limit(days, start_date)
        return rows[-limit:] if limit else rows

    def get_daily(self, ts_code: str, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame | None:
        """前复权日线 DataFrame，列名对齐 tushare ``daily``。"""
        rows = self.get_kline_dicts(ts_code, days=0, start_date=start_date, end_date=end_date)
        return pd.DataFrame(rows) if rows else None

    def get_index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame | None:
        """指数日线（指数没有复权）。"""
        rows = self.get_kline_dicts(ts_code, days=0, start_date=start_date, end_date=end_date, basis="raw")
        return pd.DataFrame(rows) if rows else None

    def latest_trade_date(self) -> str | None:
        """本地行情的最新交易日（取上证指数最后一根）。"""
        bars = self.read_raw_bars(_MARKET_INDEX)
        return str(int(bars["date"][-1])) if bars is not None and len(bars) else None

    def trade_dates(self) -> list[str]:
        """本地交易日序列（取上证指数的 K 线日期）。"""
        bars = self.read_raw_bars(_MARKET_INDEX)
        return [str(int(d)) for d in bars["date"]] if bars is not None else []

    # ---------- 证券信息 ----------

    def stock_names(self) -> dict[str, str]:
        """{symbol: 简称}，来自 tnf 文件。"""
        names: dict[str, str] = {}
        for market, filename in _TNF_FILES.items():
            path = self._hq_cache / filename
            try:
                raw = path.read_bytes()
            except OSError as e:
                logger.warning("[tdx] 读取 %s 失败: %s", path, e)
                continue
            for off in range(_TNF_HEADER, len(raw) - _TNF_RECORD + 1, _TNF_RECORD):
                code = raw[off : off + 6].decode("ascii", "ignore")
                if len(code) != 6 or not code.isdigit():
                    continue
                name = raw[off + _TNF_NAME_OFFSET : off + _TNF_RECORD].split(b"\x00", 1)[0]
                names[f"{market}{code}"] = re.sub(r"\s+", "", name.decode("gbk", "ignore"))
        return names

    def _base_info(self) -> dict[str, dict[str, Any]]:
        """{symbol: 股本与上市日期}，来自 base.dbf。"""
        path = self._hq_cache / "base.dbf"
        try:
            raw = path.read_bytes()
        except OSError as e:
            logger.warning("[tdx] 读取 %s 失败: %s", path, e)
            return {}
        count = struct.unpack("<I", raw[4:8])[0]
        header_len, record_len = struct.unpack("<HH", raw[8:12])
        fields: dict[str, tuple[int, int]] = {}
        offset = 1  # 第 0 字节是删除标记
        for pos in range(32, header_len - 1, 32):
            name = raw[pos : pos + 11].split(b"\x00", 1)[0].decode("ascii", "ignore")
            length = raw[pos + 16]
            fields[name] = (offset, length)
            offset += length

        def field(record: bytes, key: str) -> str:
            start, length = fields[key]
            return record[start : start + length].decode("gbk", "ignore").strip()

        def number(text: str) -> float:
            try:
                return float(text)
            except ValueError:
                return 0.0

        market_by_sc = {"0": "sz", "1": "sh", "2": "bj"}
        info: dict[str, dict[str, Any]] = {}
        for i in range(count):
            record = raw[header_len + i * record_len : header_len + (i + 1) * record_len]
            market = market_by_sc.get(field(record, "SC"))
            if market is None:
                continue
            info[f"{market}{field(record, 'GPDM')}"] = {
                # base.dbf 的股本单位是万股
                "float_share": number(field(record, "LTAG")),
                "total_share": number(field(record, "ZGB")),
                "list_date": field(record, "SSDATE"),
            }
        return info

    def _industries(self) -> dict[str, str]:
        """{symbol: 行业名}，来自 tdxhy.cfg + tdxzs3.cfg（行业码按前缀逐级回退）。"""
        try:
            mapping = (self._hq_cache / "tdxhy.cfg").read_bytes().decode("gbk", "ignore").splitlines()
            index_cfg = (self._hq_cache / "tdxzs3.cfg").read_bytes().decode("gbk", "ignore").splitlines()
        except OSError as e:
            logger.warning("[tdx] 读取行业文件失败: %s", e)
            return {}
        names = {}
        for line in index_cfg:
            parts = line.split("|")
            if len(parts) >= 6:
                names[parts[5]] = parts[0]

        def lookup(code: str) -> str:
            while code:
                if code in names:
                    return names[code]
                code = code[:-1]
            return ""

        result: dict[str, str] = {}
        for line in mapping:
            parts = line.split("|")
            if len(parts) < 6:
                continue
            market = _MARKET_BY_GBBQ.get(int(parts[0])) if parts[0].isdigit() else None
            if market:
                result[f"{market}{parts[1]}"] = lookup(parts[5]) or lookup(parts[2])
        return result

    def _last_date(self, symbol: str) -> str:
        path = self._day_path(symbol)
        with path.open("rb") as f:
            f.seek(-_DAY_DTYPE.itemsize, os.SEEK_END)
            return str(struct.unpack("<I", f.read(4))[0])

    def get_stock_list(self, exchange: str | None = None) -> list[dict]:
        """本地所有 A 股个股（含停牌和已无新行情的文件，由 ``last_date`` 区分）。"""
        names = self.stock_names()
        base = self._base_info()
        industries = self._industries()
        stocks: list[dict] = []
        for market in _BOARD_BY_PREFIX:
            if exchange and _EXCHANGE_BY_MARKET[market] != exchange:
                continue
            for path in sorted((self.root / "vipdoc" / market / "lday").glob("*.day")):
                symbol = path.stem.lower()
                board = board_of(symbol)
                if board is None or path.stat().st_size == 0:
                    continue
                info = base.get(symbol, {})
                name = names.get(symbol, "")
                stocks.append(
                    {
                        "ts_code": to_ts_code(symbol),
                        "name": name,
                        "market": board,
                        "exchange": _EXCHANGE_BY_MARKET[market],
                        "industry": industries.get(symbol, ""),
                        "list_date": info.get("list_date", ""),
                        "float_share": info.get("float_share", 0.0),
                        "total_share": info.get("total_share", 0.0),
                        "is_st": is_st_name(name),
                        "last_date": self._last_date(symbol),
                    }
                )
        return stocks

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None:
        """证券基础信息；不传代码时返回全部 A 股。"""
        rows = self.get_stock_list()
        if ts_code:
            rows = [r for r in rows if r["ts_code"] == ts_code]
        if name:
            rows = [r for r in rows if name in r["name"]]
        return pd.DataFrame(rows) if rows else None

    def stock_name(self, ts_code: str) -> str:
        """单只股票的简称，查不到返回空串。"""
        return _cached_names(str(self.root)).get(to_symbol(ts_code), "")


def is_st_name(name: str) -> bool:
    """按当前简称判断 ST / 退市整理。历史 ST 状态无法从当前简称还原。"""
    # 区分大小写：A 股简称里 ST 都是大写，英文词（如 Stock）不算
    return "ST" in name or name.endswith("退") or name.startswith("退市")


# 本地还没下载的当天 K 线（未复权，价格单位元、量单位股、额单位元），由调用方用实时行情填入。
# 只在比本地最后一根更新时才拼上；进程内有效，用完调 clear_today_overlay。
_today_overlay: dict[str, dict] = {}


def set_today_overlay(bars: dict[str, dict]) -> None:
    """登记当天 K 线：``{代码: {trade_date, open, high, low, close, volume_shares, amount_yuan}}``，
    代码写 600795 或 600795.SH 都行。"""
    _today_overlay.clear()
    _today_overlay.update({to_symbol(code): bar for code, bar in bars.items()})


def clear_today_overlay() -> None:
    _today_overlay.clear()


@functools.lru_cache(maxsize=2)
def _cached_events(path: str, mtime_ns: int, size: int) -> dict[str, np.ndarray]:
    return _load_gbbq_events(Path(path))


@functools.lru_cache(maxsize=2)
def _cached_floats(path: str, mtime_ns: int, size: int) -> dict[str, np.ndarray]:
    return _load_gbbq_floats(Path(path))


@functools.lru_cache(maxsize=2)
def _cached_names(root: str) -> dict[str, str]:
    return TdxLocalClient(root).stock_names()


def local_kline_rows(
    ts_code: str,
    days: int = 60,
    start_date: str | None = None,
    end_date: str | None = None,
    basis: str = "display",
) -> list[dict] | None:
    """配置了通达信目录时返回本地 K 线；未配置返回 None，由调用方走原路径。"""
    root = configured_root()
    if root is None:
        return None
    return TdxLocalClient(root).get_kline_dicts(
        ts_code, days=days, start_date=start_date, end_date=end_date, basis=basis
    )


def local_stock_name(ts_code: str) -> str | None:
    """配置了通达信目录时返回简称；未配置返回 None。"""
    root = configured_root()
    if root is None:
        return None
    return TdxLocalClient(root).stock_name(ts_code) or None


__all__ = [
    "TdxLocalClient",
    "board_of",
    "business_days_behind",
    "clear_today_overlay",
    "set_today_overlay",
    "configured_root",
    "is_etf",
    "is_st_name",
    "local_kline_rows",
    "local_stock_name",
    "ratio_qfq_factors",
    "tdx_qfq_coefficients",
    "to_symbol",
    "to_ts_code",
]
