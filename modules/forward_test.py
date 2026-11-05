"""前向检验：每日报告入选的股票，入选后 5 / 10 个交易日表现如何，和同日随机基准比。

报告每天在结果出来之前写下入选名单（``REPORT_OUT_DIR/data/YYYY-MM-DD.json``），所以这里算的是
前向记录，不是回测。对照有三组：

- **同日随机基准**：报告当天的选股范围（沪深主板、当天有行情、上市满 120 根）全部个股的平均收益，
  也就是同一天从同一范围随机抽股票的期望值。没有历史 ST 记录，不能用当前 ST 标记回填，所以
  基准不剔除 ST（写进 notes）。
- **上证指数**：不复权收盘。
- **被剔除的票**：报告里按技能规则剔除的那组，用来检验过滤规则本身有没有用。

收益口径同「入选后表现」：入选日收盘到第 N 个交易日收盘，等比复权。同一个数据日期的报告
只算一次（以后生成的为准）。只读本地通达信文件；随机基准按日期缓存在 ``data/forward/``。
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

HORIZONS = (5, 10)
#: 随机基准的范围：上市满多少根（同选股）
MIN_BARS = 120
#: 少于这么多个交易日不下结论
MIN_DAYS = 20
#: 结束日之后再过这么多个交易日，才把随机基准当成定稿缓存（期间补下载的行情还能算进去）
SETTLE_DAYS = 5
#: t 分布 97.5% 分位（自由度 1-30），更多时用 1.96
_T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]

Series = tuple[np.ndarray, np.ndarray]  # (日期 int YYYYMMDD, 收盘)


def forward_return(series: Series | None, day: str, n: int, calendar: np.ndarray | None = None) -> float | None:
    """day 收盘到其后第 n 个交易日收盘的涨跌幅（%）；当天没有行情或还没走到第 n 天 = None。

    calendar（市场交易日，上证指数日线日期）给了时，第 n 天按市场日历数，停牌的股票取结束日之前最后一根
    收盘（停牌估值），所有股票和指数的结束日一致；不给时按这只股票自己的第 n 根 K 线。
    """
    if series is None:
        return None
    dates, closes = series
    idx = np.searchsorted(dates, int(day))
    if idx >= len(dates) or int(dates[idx]) != int(day):
        return None
    base = float(closes[idx])
    if not base:
        return None
    if calendar is None:
        return round((float(closes[idx + n]) / base - 1) * 100, 4) if idx + n < len(dates) else None
    cal = np.searchsorted(calendar, int(day))
    if cal >= len(calendar) or int(calendar[cal]) != int(day) or cal + n >= len(calendar):
        return None
    end = int(calendar[cal + n])
    last = int(np.searchsorted(dates, end, side="right")) - 1
    return round((float(closes[last]) / base - 1) * 100, 4)


def t975(df: int) -> float:
    return _T975[df - 1] if 1 <= df <= len(_T975) else 1.96


def mean_ci(values: list[float]) -> tuple[float | None, float | None, float | None]:
    """均值与 95% 区间（按天聚类：每个交易日一个值）。少于 2 个值没有区间。"""
    if not values:
        return None, None, None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return round(mean, 3), None, None
    half = t975(len(values) - 1) * statistics.stdev(values) / math.sqrt(len(values))
    return round(mean, 3), round(mean - half, 3), round(mean + half, 3)


def group_stats(days: list[dict], group: str, n: int) -> dict:
    """某一组（picks / dropped）在 n 日上的汇总。days 里每天有 universe 均值和这一组每只的收益。

    平均收益、随机基准、超额都按交易日等权（每天先取当天这一组的平均），三者口径一致可以直接相减；
    胜率、跑赢随机比例按只数算。
    """
    per_day_excess: list[float] = []
    per_day_avg: list[float] = []
    returns: list[float] = []
    beat = 0
    for day in days:
        universe = day["universe"].get(str(n))
        rets = [r for r in (item["returns"].get(str(n)) for item in day[group]) if r is not None]
        if universe is None or not rets:
            continue
        returns.extend(rets)
        beat += sum(1 for r in rets if r > universe)
        per_day_avg.append(statistics.fmean(rets))
        per_day_excess.append(statistics.fmean(rets) - universe)
    mean, low, high = mean_ci(per_day_excess)
    return {
        "days": len(per_day_excess),
        "count": len(returns),
        "avg": round(statistics.fmean(per_day_avg), 3) if per_day_avg else None,
        "win_rate": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1) if returns else None,
        "beat_random_rate": round(beat / len(returns) * 100, 1) if returns else None,
        "excess_vs_random": mean,
        "excess_ci": [low, high] if low is not None else None,
    }


def verdict(stats: dict) -> str:
    """一句话结论：样本够不够、区间在不在 0 的一侧。"""
    if stats["days"] < MIN_DAYS:
        return f"样本不足：已完成 {stats['days']} 个交易日，至少 {MIN_DAYS} 个才下结论"
    ci = stats["excess_ci"]
    if ci is None:
        return "样本不足"
    if ci[0] > 0:
        return "目前跑赢同日随机（区间下界大于 0）"
    if ci[1] < 0:
        return "目前跑输同日随机（区间上界小于 0）"
    return "和同日随机没有显著差别（区间跨过 0）"


def load_reports(data_dir: Path) -> list[dict]:
    """data 目录里的每日报告，同一个数据日期只留最后生成的那份，按数据日期升序。"""
    latest: dict[str, dict] = {}
    for path in sorted(data_dir.glob("????-??-??.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("跳过读不了的报告 %s: %s", path, e)
            continue
        key = str(report.get("data_date") or "")
        if key:
            latest[key] = report
    return [latest[k] for k in sorted(latest)]


def universe_returns(
    day: str, codes: Iterable[str], series_of: Callable[[str], Series | None], calendar: np.ndarray | None = None
) -> dict[str, list[float]]:
    """同日随机基准：范围内每只股票的 n 日收益（当天有行情、到 day 为止满 MIN_BARS 根）。"""
    out: dict[str, list[float]] = {str(n): [] for n in HORIZONS}
    for code in codes:
        series = series_of(code)
        if series is None:
            continue
        idx = np.searchsorted(series[0], int(day))
        if idx >= len(series[0]) or int(series[0][idx]) != int(day) or idx + 1 < MIN_BARS:
            continue
        for n in HORIZONS:
            r = forward_return(series, day, n, calendar)
            if r is not None:
                out[str(n)].append(r)
    return out


def forward_test(
    data_dir: Path,
    series_of: Callable[[str], Series | None],
    universe_codes: Callable[[], list[str]],
    bench: Series | None,
    local_through: str,
    cache_dir: Path | None = None,
    universe_series_of: Callable[[str], Series | None] | None = None,
) -> dict:
    """汇总前向检验。series_of(code) 给等比复权收盘；universe_codes() 给选股范围（沪深主板个股）。

    universe_series_of：算随机基准时逐只读、不缓存（几千只全留在内存里太大）；缺省同 series_of。
    """
    universe_series_of = universe_series_of or series_of
    calendar = bench[0] if bench is not None else None
    reports = load_reports(data_dir)
    cache_dir = cache_dir or data_dir / "forward"
    codes: list[str] | None = None
    days: list[dict] = []
    pending = 0
    for report in reports:
        day = str(report["data_date"])
        cache = cache_dir / f"universe-{day}.json"
        means: dict[str, float | None] | None = None
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("through") == local_through or cached.get("complete"):
                means = cached["means"]
        except (OSError, ValueError, KeyError):
            pass
        if means is None:
            if codes is None:
                codes = universe_codes()
            rets = universe_returns(day, codes, universe_series_of, calendar)
            means = {k: (round(statistics.fmean(v), 4) if v else None) for k, v in rets.items()}
            # 「定稿」不能只看均值非空（部分股票行情还没下载时也非空，会被永久缓存成缺了一部分的基准）：
            # 要等市场日历走过最长周期的结束日、再过 SETTLE_DAYS 个交易日，期间每天按行情版本重算
            complete = False
            if calendar is not None:
                pos = int(np.searchsorted(calendar, int(day)))
                complete = pos < len(calendar) and int(calendar[pos]) == int(day) and pos + max(HORIZONS) + SETTLE_DAYS < len(calendar)
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"day": day, "through": local_through, "complete": complete, "means": means,
                                             "sizes": {k: len(v) for k, v in rets.items()}}), encoding="utf-8")
            except OSError as e:
                logger.warning("随机基准缓存写不进去 %s: %s", cache, e)

        def items(entries: list[dict]) -> list[dict]:
            out = []
            for entry in entries:
                code = str(entry.get("code", ""))
                series = series_of(code) if code else None
                out.append({
                    "code": code,
                    "name": entry.get("name", ""),
                    "sources": entry.get("sources", []),
                    "returns": {str(n): forward_return(series, day, n, calendar) for n in HORIZONS},
                })
            return out

        picks = items(report.get("picks", []))
        dropped = items(report.get("dropped", []))
        if any(p["returns"][str(HORIZONS[0])] is None for p in picks):
            pending += 1
        days.append({
            "date": day,
            "universe": means,
            "bench": {str(n): forward_return(bench, day, n, calendar) for n in HORIZONS},
            "picks": picks,
            "dropped": dropped,
        })

    summary = {str(n): {"picks": group_stats(days, "picks", n), "dropped": group_stats(days, "dropped", n)} for n in HORIZONS}
    for n in HORIZONS:
        summary[str(n)]["verdict"] = verdict(summary[str(n)]["picks"])
        bench_vals = [d["bench"][str(n)] for d in days if d["bench"][str(n)] is not None and any(
            p["returns"][str(n)] is not None for p in d["picks"])]
        summary[str(n)]["bench_avg"] = round(statistics.fmean(bench_vals), 3) if bench_vals else None
        uni_vals = [d["universe"][str(n)] for d in days if d["universe"].get(str(n)) is not None and any(
            p["returns"][str(n)] is not None for p in d["picks"])]
        summary[str(n)]["random_avg"] = round(statistics.fmean(uni_vals), 3) if uni_vals else None

    sources: dict[str, dict] = {}
    for name in sorted({s for d in days for p in d["picks"] for s in p["sources"]}):
        filtered = [{**d, "picks": [p for p in d["picks"] if name in p["sources"]]} for d in days]
        sources[name] = {str(n): group_stats(filtered, "picks", n) for n in HORIZONS}

    return {
        "ok": True,
        "horizons": list(HORIZONS),
        "local_through": local_through,
        "report_days": len(days),
        "pending_days": pending,
        "summary": summary,
        "by_source": sources,
        "days": [
            {
                "date": d["date"],
                "picks": len(d["picks"]),
                "dropped": len(d["dropped"]),
                "universe": d["universe"],
                "bench": d["bench"],
                "picks_avg": {str(n): _avg(p["returns"][str(n)] for p in d["picks"]) for n in HORIZONS},
                "dropped_avg": {str(n): _avg(p["returns"][str(n)] for p in d["dropped"]) for n in HORIZONS},
            }
            for d in reversed(days)
        ],
        "notes": [
            "同日随机基准 = 报告当天沪深主板、当天有行情、上市满 120 根的全部个股的平均收益；没有历史 ST 记录，基准不剔除 ST。",
            "平均收益、随机基准、超额都按交易日等权（每天先取当天的平均）；胜率、跑赢随机按只数算。第 N 日按市场交易日历数，停牌股取结束日前最后一根收盘。",
            "区间按交易日聚类（每天一个超额值）；相邻交易日的 5 / 10 日窗口互相重叠，区间偏窄，只作参考。",
            f"至少 {MIN_DAYS} 个已完成的交易日、且区间不跨过 0，才算有结论。规则是机械计算，不构成投资建议。",
        ],
    }


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 3) if vals else None


def run_forward_test(out_dir: Path) -> dict:
    """用本地通达信数据跑前向检验（命令行 forward-test 用）。"""
    from modules.tdx_local_client import TdxLocalClient, configured_root, ratio_qfq_factors, to_symbol
    from modules.tdx_selection import DEFAULT_BOARDS

    root = configured_root()
    if root is None:
        return {"ok": False, "error": "没有配置通达信目录：在技能的 .env 里设置 TDX_DIR"}
    data_dir = out_dir / "data"
    if not data_dir.is_dir():
        return {"ok": False, "error": f"还没有每日报告：{data_dir} 不存在"}
    client = TdxLocalClient(root)
    events = client._events()
    cache: dict[str, Series | None] = {}

    def load(code: str) -> Series | None:
        raw = client.raw_series(code)
        if raw is None or len(raw["date"]) == 0:
            return None
        factors = ratio_qfq_factors(raw["date"], raw["close"], events.get(to_symbol(code)))
        return raw["date"].astype(np.int64), raw["close"] * factors

    def series_of(code: str) -> Series | None:
        if code not in cache:
            cache[code] = load(code)
        return cache[code]

    def universe_codes() -> list[str]:
        return [s["ts_code"].split(".")[0] for s in client.get_stock_list() if s["market"] in DEFAULT_BOARDS]

    bench_raw = client.raw_series("000001.SH")
    bench = (bench_raw["date"].astype(np.int64), bench_raw["close"]) if bench_raw is not None else None
    local_through = client.latest_trade_date() or ""
    return forward_test(data_dir, series_of, universe_codes, bench, local_through, universe_series_of=load)
