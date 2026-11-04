#!/usr/bin/env python3
"""用通达信官方接口核对本地前复权（只读）。

先启动通达信客户端并登录，再运行：

    .venv/Scripts/python.exe scripts/verify_tdx_qfq.py
    .venv/Scripts/python.exe scripts/verify_tdx_qfq.py --sample 200 --count 800

只调用 tqcenter 的 get_stock_list 和 get_market_data，不下单，也不修改任何板块。
差异超过 1 分的股票会列出来，结果按退出码表示：0 全部一致，1 有差异，2 无法连接客户端。
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOLERANCE_YUAN = 0.011  # 显示口径四舍五入到分，允许 1 分的差异


def load_tq(root: Path):
    """加载通达信自带的 tqcenter。"""
    user_dir = root / "PYPlugins" / "user"
    if not (user_dir / "tqcenter.py").exists():
        raise FileNotFoundError(f"找不到 {user_dir / 'tqcenter.py'}")
    sys.path.insert(0, str(user_dir))
    os.chdir(user_dir)  # tqcenter 按相对路径加载 DLL
    from tqcenter import tq

    tq.initialize(__file__)
    return tq


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tdx-dir", default=os.environ.get("TDX_DIR", ""), help="通达信目录，默认取 .env 的 TDX_DIR")
    parser.add_argument("--sample", type=int, default=100, help="抽样股票数")
    parser.add_argument("--count", type=int, default=500, help="每只股票比较的 K 线根数")
    parser.add_argument("--seed", type=int, default=20261031)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    root = Path(args.tdx_dir)
    os.environ["TDX_DIR"] = str(root)
    from modules.tdx_local_client import TdxLocalClient

    client = TdxLocalClient(root)
    stocks = [s for s in client.get_stock_list() if s["market"] in ("主板", "创业板", "科创板")]
    codes = random.Random(args.seed).sample(sorted(s["ts_code"] for s in stocks), min(args.sample, len(stocks)))

    try:
        tq = load_tq(root)
        data = tq.get_market_data(
            field_list=["Open", "High", "Low", "Close"],
            stock_list=codes,
            period="1d",
            count=args.count,
            dividend_type="front",
            fill_data=False,
        )
    except Exception as e:  # noqa: BLE001 - 客户端没开时错误类型不固定，统一报告
        print(f"连接通达信客户端失败，请先启动并登录：{type(e).__name__}: {e}")
        return 2
    if not data or "Close" not in data:
        print("通达信客户端没有返回数据，请确认已登录并下载盘后数据")
        return 2

    checked = 0
    bad: list[tuple[str, str, str, float, float]] = []
    worst = 0.0
    fields = {"Open": "open", "High": "high", "Low": "low", "Close": "close"}
    for code in codes:
        rows = {r["trade_date"]: r for r in client.get_kline_dicts(code, days=args.count)}
        if code not in data["Close"]:
            continue
        for tdx_field, local_field in fields.items():
            series = data[tdx_field][code].dropna()
            for ts, value in series.items():
                date = ts.strftime("%Y%m%d") if hasattr(ts, "strftime") else str(ts)[:8]
                row = rows.get(date)
                if row is None:
                    continue
                checked += 1
                diff = abs(float(value) - row[local_field])
                worst = max(worst, diff)
                if diff > TOLERANCE_YUAN:
                    bad.append((code, date, local_field, float(value), row[local_field]))

    affected = sorted({b[0] for b in bad})
    print(f"比较 {len(codes)} 只股票、{checked} 个价格；最大差 {worst:.4f} 元；超过 1 分的 {len(bad)} 个，涉及 {len(affected)} 只")
    for item in bad[:20]:
        print("  ", item)
    if checked == 0:
        print("没有可比较的数据")
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
