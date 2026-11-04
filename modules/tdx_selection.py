"""用本地通达信日线跑移植的选股公式（modules.tdx_formulas），代替手动点「一键选股」。

- 价格口径 ``display``（通达信前复权）：2026-11-03 对照，确认战法 25 只里 24 只一致（第 25 只本地
  文件缺当天 K 线），不复权只有 21 只一致；
- 选股范围默认沪深主板：同日对照，移植公式多选出的 10 只全是创业板 / 科创板，
  说明用户通达信的一键选股范围不含这两个板块；
- 每只取最近 ``LOOKBACK`` 根，足够覆盖公式里最长的 MA(C,114)，EMA/SMA 起点差异可忽略；
- 多进程扫描。调用方用 ``tdx_local_client.set_today_overlay`` 补的当天 K 线会一并传给子进程。
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from modules import tdx_local_client
from modules.tdx_formulas import Bars, load_formulas
from modules.tdx_local_client import TdxLocalClient, configured_root, float_shares_on

logger = logging.getLogger(__name__)

LOOKBACK = 500
DEFAULT_BOARDS = ("主板",)
#: 本地最后一根距最新交易日超过这么多天的视为停牌，不扫
STALE_DAYS = 10


@dataclass
class SelectionRun:
    trade_date: str
    basis: str
    boards: tuple[str, ...]
    scanned: int = 0
    picks: dict[str, list[str]] = field(default_factory=dict)  # 公式名 → 6 位代码
    errors: list[tuple[str, str]] = field(default_factory=list)


def bars_from_rows(code: str, rows: list[dict], float_shares_wan: float | None) -> Bars:
    C = np.array([r["close"] for r in rows], dtype=np.float64)
    turnover = np.array([r.get("turnover_rate", np.nan) for r in rows], dtype=np.float64)
    # FINANCE(40) 是当前流通市值：最新流通股本 × 收盘价
    float_mv = C * float_shares_wan * 10000 if float_shares_wan else np.full_like(C, np.nan)
    return Bars(
        code=code,
        O=np.array([r["open"] for r in rows], dtype=np.float64),
        H=np.array([r["high"] for r in rows], dtype=np.float64),
        L=np.array([r["low"] for r in rows], dtype=np.float64),
        C=C,
        V=np.array([r["vol"] for r in rows], dtype=np.float64),
        float_mv=float_mv,
        turnover=turnover,
    )


def _worker_init(overlay: dict[str, dict]) -> None:
    tdx_local_client._today_overlay.clear()
    tdx_local_client._today_overlay.update(overlay)


def _scan_chunk(args: tuple[list[str], tuple[str, ...], str, str]) -> tuple[int, dict[str, list[str]], list[tuple[str, str]]]:
    codes, names, basis, trade_date = args
    formulas = load_formulas()  # 子进程各自加载用户公式文件
    client = TdxLocalClient(configured_root())
    picks: dict[str, list[str]] = {n: [] for n in names}
    errors: list[tuple[str, str]] = []
    scanned = 0
    for code in codes:
        try:
            rows = client.get_kline_dicts(code, days=LOOKBACK, basis=basis)
            if len(rows) < 30 or rows[-1]["trade_date"] != trade_date:
                continue
            shares = float_shares_on(np.array([int(trade_date)]), client._float_events(code))
            float_shares = float(shares[0]) if shares is not None and np.isfinite(shares[0]) else None
            b = bars_from_rows(code, rows, float_shares)
            scanned += 1
            for name in names:
                if bool(formulas[name](b)[-1]):
                    picks[name].append(code)
        except Exception as e:  # noqa: BLE001 — 单只出错不影响整体，记下来
            errors.append((code, f"{type(e).__name__}: {e}"[:80]))
    return scanned, picks, errors


def candidate_codes(client: TdxLocalClient, trade_date: str, boards: tuple[str, ...]) -> list[str]:
    """范围内、最近 STALE_DAYS 天内有行情的个股（本地缺最新一根的靠当天补齐）。"""
    from datetime import datetime, timedelta

    cutoff = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=STALE_DAYS)).strftime("%Y%m%d")
    return [
        s["ts_code"].split(".")[0]
        for s in client.get_stock_list()
        if s["market"] in boards and s["last_date"] >= cutoff and not s["is_st"]
    ]


def run_selection(
    names: tuple[str, ...] | None = None,
    basis: str = "display",
    codes: list[str] | None = None,
    boards: tuple[str, ...] = DEFAULT_BOARDS,
    trade_date: str | None = None,
    workers: int | None = None,
) -> SelectionRun:
    """跑选股。trade_date 不传时取本地最新交易日（有当天补齐时由调用方传入补齐后的日期）。"""
    root = configured_root()
    if root is None:
        raise RuntimeError("未配置 TDX_DIR")
    client = TdxLocalClient(root)
    client.prepare()  # 先在主进程把除权、股本缓存写到磁盘，子进程直接读
    trade_date = trade_date or client.latest_trade_date() or ""
    if codes is None:
        codes = candidate_codes(client, trade_date, boards)
    names = names or tuple(load_formulas())
    run = SelectionRun(trade_date=trade_date, basis=basis, boards=boards, picks={n: [] for n in names})
    workers = workers or max(1, min(12, (os.cpu_count() or 2) - 2))
    chunks = [codes[i::workers * 4] for i in range(workers * 4)]
    jobs = [(c, names, basis, trade_date) for c in chunks if c]
    overlay = dict(tdx_local_client._today_overlay)
    if workers == 1 or len(codes) < 200:
        results = [_scan_chunk(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(overlay,)) as pool:
            results = list(pool.map(_scan_chunk, jobs))
    for scanned, picks, errors in results:
        run.scanned += scanned
        run.errors.extend(errors)
        for name, found in picks.items():
            run.picks[name].extend(found)
    order = {c: i for i, c in enumerate(codes)}
    for name in run.picks:
        run.picks[name].sort(key=lambda c: order.get(c, 0))
    return run
