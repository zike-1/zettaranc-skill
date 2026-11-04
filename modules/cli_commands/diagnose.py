"""CLI diagnose + workflow 子命令实现。"""

from __future__ import annotations

from modules.core.errors import ErrorCode, ZettarancError

from ._helpers import data_status, json_output


def cmd_workflow(args) -> None:
    """每日五步工作流（来自 screener.py workflow action）"""
    if getattr(args, "json", False):
        # workflow 的文字版是打印流程，JSON 版与 daily 同一份数据
        from .portfolio import cmd_daily

        cmd_daily(args)
        return

    from modules.screener import daily_workflow

    daily_workflow()


def cmd_diagnose(args) -> None:
    """持仓诊断"""
    from modules.portfolio_diagnosis import diagnose_stock, format_report

    ts_code = args.ts_code
    diagnosis = diagnose_stock(ts_code, days=args.days)

    # 价格为 0 说明没取到 K 线，不能把全 0 的诊断当结果输出
    if not getattr(diagnosis, "price", 0):
        raise ZettarancError(
            ErrorCode.DATA_SOURCE_ERROR,
            f"{ts_code} 没有 K 线数据。配置 TDX_DIR，或先运行 zt sync sync {ts_code}",
        )

    # ── JSON 输出 ──
    if args.json:
        from dataclasses import asdict

        from modules.indicators import get_kline_data

        klines = get_kline_data(ts_code, 1)
        json_output({**asdict(diagnosis), **data_status(klines[-1].trade_date if klines else None)})
        return

    # ── 人类可读输出（保持原样） ──
    print(format_report(diagnosis))
