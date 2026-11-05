#!/usr/bin/env python3
"""Z哥量化工具 CLI 入口（v4.3+ slim 布局）

本文件只负责：
1. `main()`: argparse 入口
2. `build_parser()`: 子命令注册表
3. `cmd_analyze/screen/score/workflow/diagnose/watchlist/sync/track/self_optimize`
   的薄封装（保持向后兼容 `from modules.cli import cmd_X`）

命令实现按职责拆到 `modules.cli_commands/` 包：
    analyze, score           → cli_commands.analyze
    screen                   → cli_commands.screen
    diagnose, workflow        → cli_commands.diagnose
    watchlist, trade, daily   → cli_commands.portfolio
    sync, track, self-optimize, monitor, market-timing → cli_commands.data
    backtest, verify, simulate → cli_commands.backtest / simulate

用法：
    zt analyze 600487.SH --json
    zt screen --strategy B1 --limit 20 --json
    zt backtest shaofu 600487.SH --days 250 --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

from .core.net import disable_proxy
from .core.errors import ErrorCode
from .cli_commands import (
    STRATEGY_ALIAS,
    STRATEGY_CHOICES,
    cmd_analyze,
    cmd_daily,
    cmd_diagnose,
    cmd_score,
    cmd_screen,
    cmd_self_optimize,
    cmd_sync,
    cmd_trade,
    cmd_track,
    cmd_watchlist,
    cmd_workflow,
)
from .cli_commands import cmd_backtest, cmd_market_timing, cmd_monitor, cmd_simulate, cmd_verify_v10
from .cli_commands.backtest import add_verify_v10_parser
from .cli_commands.data import add_self_optimize_parser

logger = __import__("logging").getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """构建并返回 zt CLI 的 ArgumentParser（支持独立导入测试）"""
    parser = argparse.ArgumentParser(
        prog="zt",
        description="Z哥量化工具 CLI（v4.3+ 统一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  zt analyze 600487.SH
  zt analyze 600487.SH --json
  zt screen --strategy B1 --limit 20
  zt score 600487.SH
  zt diagnose 600487.SH
  zt watchlist add 600487.SH --tags 通信设备,5G
  zt watchlist scan
  zt backtest shaofu 600487.SH --days 250 --json
  zt backtest multi 600487.SH --strategy b1,b2
  zt backtest portfolio 600487.SH,601318.SH
  zt trade add "4月25号买了100股茅台1800块"
  zt trade list
  zt trade review
  zt daily
  zt sync init
  zt sync sync 600487.SH
  zt simulate 600487.SH --days 250 --cost-model advanced --slippage dynamic
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令", required=True)

    # ── analyze ──
    p_analyze = subparsers.add_parser("analyze", help="分析单只股票（指标 + 主力阶段 + 战法信号 + 诊断）")
    p_analyze.add_argument("ts_code", help="股票代码，如 600487.SH")
    p_analyze.add_argument("--days", type=int, default=120, help="分析天数")
    p_analyze.add_argument("--json", action="store_true", help="JSON输出")

    # ── screen ──
    p_screen = subparsers.add_parser("screen", help="批量选股（11 种策略）")
    p_screen.add_argument("--strategy", choices=STRATEGY_CHOICES, default="B1", help="筛选策略（11 种别名）")
    p_screen.add_argument("--limit", type=int, default=20, help="返回条数（0=全部命中，默认 20）")
    p_screen.add_argument("--pool", type=int, default=0, help="扫描数量（默认 0=全市场）")
    p_screen.add_argument("--include-st", action="store_true", help="保留 ST 股票（默认按当前简称剔除）")
    p_screen.add_argument("--no-parallel", action="store_true", help="禁用多进程并行")
    p_screen.add_argument("--json", action="store_true", help="JSON输出")
    p_screen.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="覆盖策略参数（可重复），如 --param j_threshold=20",
    )
    p_screen.add_argument("--top", type=int, default=0, help="--limit 的别名，优先级更高")

    # ── score ──
    p_score = subparsers.add_parser("score", help="单只股票综合评分")
    p_score.add_argument("ts_code", nargs="?", help="股票代码，如 600487.SH")
    p_score.add_argument("--json", action="store_true", help="JSON输出")

    # ── workflow ──
    p_workflow = subparsers.add_parser("workflow", help="每日五步工作流")
    p_workflow.add_argument("--json", action="store_true", help="JSON输出（与 daily 同一份数据）")

    # ── diagnose ──
    p_diag = subparsers.add_parser("diagnose", help="持仓诊断")
    p_diag.add_argument("ts_code", help="股票代码")
    p_diag.add_argument("--days", type=int, default=120, help="分析天数")
    p_diag.add_argument("--json", action="store_true", help="JSON输出")

    # ── watchlist ──
    p_wl = subparsers.add_parser("watchlist", help="自选股管理")
    p_wl.add_argument("action", choices=["add", "remove", "list", "scan", "report"], help="操作")
    p_wl.add_argument("ts_code", nargs="?", help="股票代码（add/remove 必填）")
    p_wl.add_argument("--tags", help="标签，逗号分隔")
    p_wl.add_argument("--json", action="store_true", help="JSON输出（仅 scan 操作）")

    # ── sync ──
    p_sync = subparsers.add_parser("sync", help="数据同步（init/sync/status/stk-factor/index/0amv）")
    p_sync_sub = p_sync.add_subparsers(dest="sync_action", required=True)

    p_sync_sub.add_parser("init", help="初始化数据库")
    p_sync_run = p_sync_sub.add_parser("sync", help="同步日线 K 线（+ 可选指标缓存）")
    p_sync_run.add_argument("ts_code", nargs="?", help="股票代码（不传 = 全市场批量）")
    p_sync_run.add_argument("--days", type=int, default=730, help="同步天数")
    p_sync_run.add_argument(
        "--incremental",
        action="store_true",
        help="严格增量：每只股票从其最后一根 K 线的下一天续拉（默认按 --days 窗口整段重拉）",
    )
    p_sync_run.add_argument("--indicators", action="store_true", help="批量同步完成后计算并缓存技术指标")
    p_sync_run.add_argument(
        "--skip-indicators", action="store_true", help="跳过指标缓存（单只默认同步，批量需 --indicators）"
    )
    p_sync_status = p_sync_sub.add_parser("status", help="查看同步状态")
    p_sync_status.add_argument("--json", action="store_true", help="JSON输出")
    p_sync_factor = p_sync_sub.add_parser("stk-factor", help="同步 Tushare 官方指标（diff 验证用）")
    p_sync_factor.add_argument("ts_code", nargs="?", help="股票代码（不传 = 全市场）")
    p_sync_factor.add_argument("--days", type=int, default=365, help="同步天数")

    p_sync_index = p_sync_sub.add_parser("index", help="通过 hithink 同步主要指数日线到 DuckDB")
    p_sync_index.add_argument(
        "--duckdb", default=None, help="DuckDB 数据库路径（默认读 DUCKDB_PATH 或 data/market.duckdb）"
    )
    p_sync_index.add_argument("--start", default="20160101", help="起始日期 YYYYMMDD")
    p_sync_index.add_argument("--end", default=None, help="结束日期 YYYYMMDD，默认今天")
    p_sync_index.add_argument("--codes", default=None, help="指数代码，逗号分隔；默认 6 个主要指数")

    p_sync_0amv = p_sync_sub.add_parser("0amv", help="把 0AMV 活跃市值 CSV 导入 DuckDB")
    p_sync_0amv.add_argument(
        "--duckdb", default=None, help="DuckDB 数据库路径（默认读 DUCKDB_PATH 或 data/market.duckdb）"
    )
    p_sync_0amv.add_argument("--csv", default=None, help="0AMV CSV 路径（默认 data/0amv_active_market_value.csv）")

    # ── track ──
    p_track = subparsers.add_parser("track", help="自我改进系统 - 跟踪池管理")
    p_track.add_argument("track_action", choices=["add", "remove", "list", "info", "status", "stats"], help="操作")
    p_track.add_argument("ts_code", nargs="?", help="股票代码")
    p_track.add_argument("--reason", help="跟踪/移除原因")
    p_track.add_argument("--strategy", nargs="+", help="策略标签（可多个）")
    p_track.add_argument("--name", help="股票名称")
    p_track.add_argument("--notes", help="备注")
    p_track.add_argument("--status", choices=["active", "paused", "removed"], default="active", help="状态筛选")
    p_track.add_argument("--json", action="store_true", help="JSON输出")
    add_self_optimize_parser(subparsers)

    # ── backtest ──
    p_bt = subparsers.add_parser("backtest", help="策略回测")
    p_bt_sub = p_bt.add_subparsers(dest="backtest_sub", required=True)

    p_bt_shaofu = p_bt_sub.add_parser("shaofu", help="少妇战法六步回测")
    p_bt_shaofu.add_argument("ts_code", help="股票代码")
    p_bt_shaofu.add_argument("--days", type=int, default=250, help="回测天数")
    p_bt_shaofu.add_argument("--no-timing", action="store_true", help="不按活跃市值择时")
    p_bt_shaofu.add_argument("--json", action="store_true", help="JSON输出")

    p_bt_multi = p_bt_sub.add_parser("multi", help="多策略融合回测")
    p_bt_multi.add_argument("ts_code", help="股票代码")
    p_bt_multi.add_argument("--days", type=int, default=120, help="回测天数")
    p_bt_multi.add_argument("--json", action="store_true", help="JSON输出")

    p_bt_portfolio = p_bt_sub.add_parser("portfolio", help="多股票组合回测")
    p_bt_portfolio.add_argument("codes", help="股票代码，逗号分隔")
    p_bt_portfolio.add_argument("--days", type=int, default=120, help="回测天数")
    p_bt_portfolio.add_argument("--json", action="store_true", help="JSON输出")

    p_bt_b2 = p_bt_sub.add_parser("b2-confirm", help="B1观察+B2确认+次日开盘回测")
    p_bt_b2.add_argument("codes", nargs="?", help="股票代码，逗号分隔；单股也可用 ts_code 位置")
    p_bt_b2.add_argument("--days", type=int, default=500, help="回测天数")
    p_bt_b2.add_argument("--b2-min-pct", type=float, default=4.0, help="B2 涨幅阈值")
    p_bt_b2.add_argument("--b2-min-vol", type=float, default=2.0, help="B2 量比阈值")
    p_bt_b2.add_argument("--b2-j-max", type=float, default=55.0, help="B2 当日 J 值上限")
    p_bt_b2.add_argument("--b1-j-threshold", type=float, default=-10.0, help="B1 J 值阈值")
    p_bt_b2.add_argument("--observe-min", type=int, default=3, help="观察窗口起点")
    p_bt_b2.add_argument("--observe-max", type=int, default=5, help="观察窗口终点")
    p_bt_b2.add_argument("--max-gap-open-pct", type=float, default=5.0, help="次日高开过滤")
    p_bt_b2.add_argument("--stop-loss-pct", type=float, default=-0.05, help="止损比例")
    p_bt_b2.add_argument("--bbi-days", type=int, default=2, help="BBI 连续跌破天数")
    p_bt_b2.add_argument("--min-hold", type=int, default=2, help="最少持仓天数")
    p_bt_b2.add_argument("--walk-forward", action="store_true", help="运行 Walk-forward 验证")
    p_bt_b2.add_argument("--folds", type=int, default=4, help="Walk-forward 折数")
    p_bt_b2.add_argument("--window", type=int, default=120, help="Walk-forward 窗口天数")
    p_bt_b2.add_argument("--active-mv-gate", action="store_true", help="启用活跃市值全局闸门")
    p_bt_b2.add_argument("--active-mv-duckdb", default=None, help="活跃市值 DuckDB 路径")
    p_bt_b2.add_argument("--active-mv-path", default=None, help="活跃市值 CSV 路径")
    p_bt_b2.add_argument("--json", action="store_true", help="JSON输出")

    # ── trade ──
    p_trade = subparsers.add_parser("trade", help="交易记录管理")
    p_trade_sub = p_trade.add_subparsers(dest="trade_sub", required=True)

    p_trade_add = p_trade_sub.add_parser("add", help="添加交易记录")
    p_trade_add.add_argument("text", help="交易描述（口语化）")
    p_trade_add.add_argument("--json", action="store_true", help="JSON输出")

    p_trade_list = p_trade_sub.add_parser("list", help="列出最近交易记录")
    p_trade_list.add_argument("--limit", type=int, default=20, help="列出条数")
    p_trade_list.add_argument("--json", action="store_true", help="JSON输出")

    p_trade_review = p_trade_sub.add_parser("review", help="构建复盘上下文（给 LLM）")
    p_trade_review.add_argument("--json", action="store_true", help="JSON输出")

    p_trade_stats = p_trade_sub.add_parser("stats", help="交易统计摘要")
    p_trade_stats.add_argument("--json", action="store_true", help="JSON输出")

    # ── daily ──
    p_daily = subparsers.add_parser("daily", help="每日五步工作流")
    p_daily.add_argument("--json", action="store_true", help="JSON输出")

    # ── report ──
    p_report = subparsers.add_parser("report", help="每日选股报告（HTML：活跃市值、持仓、今日选股、红利组B1、入选后表现）")
    p_report.add_argument("--out", default="", help="报告目录（默认 .env 的 REPORT_OUT_DIR，再不行用当前目录下的 reports）：YYYY-MM-DD.html、data/YYYY-MM-DD.json、index.html")
    p_report.add_argument("--blocks", default="", help="要跑的选股名，逗号分隔（默认 .env 的 ZT_REPORT_BLOCKS）")
    p_report.add_argument("--no-dividend", action="store_true", help="不扫红利组（电力/银行/保险）")
    p_report.add_argument("--no-quotes", action="store_true", help="本地缺当天 K 线时不用实时行情补")
    p_report.add_argument("--no-sync", action="store_true", help="不同步到工作台「今日选股」分组")
    p_report.add_argument("--refresh-amv", action="store_true", help="先打开指南针更新活跃市值，更新完正常退出")
    p_report.add_argument("--skip-if-current", action="store_true", help="今天休市（或还没开盘）且最近一个交易日的报告已生成时跳过；任务计划用")

    # ── tq-formulas / tq-select：用通达信自己的公式引擎（需要通达信开着）──
    subparsers.add_parser("tq-formulas", help="列出通达信里的自定义条件选股公式（JSON）")
    p_tqs = subparsers.add_parser("tq-select", help="用通达信引擎跑一个选股公式（JSON）")
    p_tqs.add_argument("--formula", required=True, help="通达信里的公式名，如 我的B1选股")
    p_tqs.add_argument("--date", default="", help="交易日 YYYYMMDD；默认用通达信最新一根（盘中即当天实时）")
    p_tqs.add_argument("--boards", default="主板", help="板块，逗号分隔；all 表示不过滤")

    # ── review：工作台选股页签过滤、Z哥诊断卡（只读）──
    p_rev = subparsers.add_parser("review", help="按每日报告的规则复核几只股票：留下/剔除、原因、要点（JSON）")
    p_rev.add_argument("--codes", required=True, help="股票代码，逗号分隔，如 600795.SH,002541")
    p_rev.add_argument("--source", default="", help="选出它们的公式名，写进买入理由")
    p_rev.add_argument("--date", default="", help="选股日 YYYYMMDD：本地数据早于它的标「待复核」，不参与剔除")

    # ── forward-test：每日报告入选股的前向检验（对比同日随机基准，只读）──
    p_fwd = subparsers.add_parser("forward-test", help="前向检验：入选后 5/10 日表现对比同日随机基准与上证（JSON）")
    p_fwd.add_argument("--out", default="", help="报告目录（默认 .env 的 REPORT_OUT_DIR）")

    # ── amv-refresh ──
    p_amv = subparsers.add_parser("amv-refresh", help="打开指南针更新活跃市值（0AMV），更新完正常退出")
    p_amv.add_argument("--wait", type=int, default=180, help="最多等几秒")

    # ── market ──
    p_market = subparsers.add_parser("market", help="市场择时指标")
    p_market_sub = p_market.add_subparsers(dest="market_sub", required=True)
    p_market_amv = p_market_sub.add_parser("amv", help="活跃市值多空状态（SOP 第一步择时）")
    p_market_amv.add_argument("--json", action="store_true", help="JSON输出")
    p_market_timing = p_market_sub.add_parser("timing", help="计算市场择时指标")
    p_market_timing.add_argument("--date", default=None, help="交易日 YYYYMMDD，默认最新")
    p_market_timing.add_argument("--index", default="000001.SH", help="大盘指数代码")
    p_market_timing.add_argument("--days", type=int, default=120, help="指数 K 线回溯天数")
    p_market_timing.add_argument("--duckdb", default=None, help="DuckDB 全市场数据库路径")
    p_market_timing.add_argument("--json", action="store_true", help="JSON输出")

    # ── monitor ──
    p_monitor = subparsers.add_parser("monitor", help="自选股主动预警与扫描推送")
    p_monitor.add_argument("--days", type=int, default=30, help="同步 K 线回溯天数")
    p_monitor.add_argument("--no-push", action="store_true", help="关闭推送通知")
    p_monitor.add_argument("--json", action="store_true", help="JSON输出")

    # ── simulate ──
    p_sim = subparsers.add_parser("simulate", help="端到端交易模拟回测（择时+选股+仓位+卖出）")
    p_sim.add_argument("codes", nargs="?", help="股票代码，逗号分隔；省略则使用前 500 只")
    p_sim.add_argument("--days", type=int, default=250, help="回测天数")
    p_sim.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    p_sim.add_argument("--max-positions", type=int, default=5, help="最大同时持仓")
    p_sim.add_argument("--risk", type=float, default=0.02, help="单笔风险占净值比例")
    p_sim.add_argument("--score", type=float, default=70.0, help="入选信号最低综合评分")
    p_sim.add_argument("--signals", type=int, default=2, help="最小共振标签数")
    p_sim.add_argument("--benchmark", type=str, default="000300.SH", help="基准指数代码")
    p_sim.add_argument(
        "--cost-model",
        choices=["simple", "advanced"],
        default="simple",
        help="成本模型（simple=仅佣金，advanced=佣金+印花税+过户费）",
    )
    p_sim.add_argument("--slippage", choices=["fixed", "dynamic"], default="fixed", help="滑点模型")
    p_sim.add_argument("--atr-sizing", action="store_true", help="启用 ATR 波动率仓位调整")
    p_sim.add_argument("--max-position-pct", type=float, default=0.20, help="单票最大仓位占比")
    p_sim.add_argument("--no-st", action="store_true", help="不允许交易 ST/*ST 股票")
    p_sim.add_argument("--t1-lock", dest="t1_lock", action="store_true", default=True, help="启用 T+1 卖出锁定（默认）")
    p_sim.add_argument("--no-t1-lock", dest="t1_lock", action="store_false", default=True, help="禁用 T+1 卖出锁定")
    p_sim.add_argument("--strategy-mode", choices=["simple", "resonance"], default="simple", help="选股模式")
    p_sim.add_argument("--strategy-lookback", type=int, default=5, help="战法信号回看交易日数")
    p_sim.add_argument("--min-resonance-score", type=float, default=0.35, help="共振模式最低入选分")
    p_sim.add_argument("--json", action="store_true", help="JSON输出")
    p_sim.add_argument("--narrate", action="store_true", help="LLM 生成 Z哥风格点评")
    p_sim.add_argument("--walk-forward", action="store_true", help="启用 walk-forward 参数寻优")
    p_sim.add_argument("--wf-train-days", type=int, default=120, help="训练窗口天数（默认 120）")
    p_sim.add_argument("--wf-test-days", type=int, default=60, help="验证窗口天数（默认 60）")
    p_sim.add_argument(
        "--wf-objective",
        choices=["calmar", "sharpe", "sortino", "total_return"],
        default="calmar",
        help="Walk-forward 目标函数",
    )

    add_verify_v10_parser(subparsers)

    return parser


def _wants_json(argv: list[str]) -> bool:
    """命令行里是否带了 ``--json``。

    argparse 自身的参数错误（未知参数 / 缺必填参数）会在解析阶段就退出，
    那时还没有 ``args`` 可用——只能回看 argv。用于让**参数错误**也产出
    合法 JSON，避免宿主在 ``--json`` 模式下拿到空 stdout。
    """
    return "--json" in argv


def _emit_error(exc: BaseException, *, as_json: bool) -> None:
    """输出错误并退出。

    ``--json`` 模式下 stdout 是**合法 JSON**（统一错误外壳，宿主可按
    ``error_code`` 分支）；stderr 仍保留人类可读信息。退出码 2。
    """
    from .core.command_result import exception_to_dict

    payload = exception_to_dict(exc)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(str(exc) if str(exc) else payload["message"], file=sys.stderr)
    sys.exit(2)


def _argparse_error_message(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    """提取 argparse 的具体错误原因。

    argparse 默认把 "the following arguments are required: ts_code" 写到 stderr
    然后退出。宿主在 ``--json`` 模式下拿到的若只是笼统的"命令行参数错误"，
    就无法告诉用户**缺哪个参数**。

    这里重放一次解析（``--help`` 除外，它会正常退出），捕获 argparse 自己
    写出的最后一行错误信息。
    """
    import io
    import contextlib

    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            parser.parse_args(argv)
    except SystemExit:
        pass
    except argparse.ArgumentError:
        # argparse 内部错误（理论上不该发生）——同样返回可读文案
        return "命令行参数错误"
    lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    # 末行通常是 "prog: error: <原因>"
    for line in reversed(lines):
        if ": error: " in line:
            return line.split(": error: ", 1)[1].strip()
    return "命令行参数错误"


def cmd_report(args) -> None:
    """生成每日选股报告，写到 ``<out>/YYYY-MM-DD.html``，并把路径打印出来。"""
    from datetime import date
    from pathlib import Path

    from . import daily_selection_report as dsr

    blocks = tuple(name.strip() for name in args.blocks.split(",") if name.strip()) or None
    out_dir = Path(args.out or os.environ.get("REPORT_OUT_DIR", "").strip() or "reports")
    session = dsr.latest_session_date() if args.skip_if_current or args.refresh_amv else ""
    if args.skip_if_current:
        reason = dsr.skip_reason(out_dir / "data", session, date.today())
        if reason:
            print(f"跳过：{reason}")
            return
    amv_refresh = None
    if args.refresh_amv:
        from dataclasses import asdict

        from .zhinanzhen_refresh import refresh_amv

        # 等最近一个交易日的活跃市值：休市日、开盘前补跑时不去等今天这份不存在的数据
        target = f"{session[:4]}-{session[4:6]}-{session[6:]}" if session else f"{date.today():%Y-%m-%d}"
        result = refresh_amv(target)
        amv_refresh = {**asdict(result), "updated": result.updated}
        print(f"活跃市值 {result.before} → {result.after}" + (f"（{result.error}）" if result.error else ""))
    path = dsr.write_report(
        out_dir,
        amv_refresh=amv_refresh,
        sync_dsh=not args.no_sync,
        blocks=blocks,
        include_dividend=not args.no_dividend,
        use_quotes=not args.no_quotes,
    )
    print(path)


def _print_json(payload) -> None:
    import json

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def cmd_tq_formulas(args) -> None:
    """列出通达信自定义选股公式。失败时输出 {ok:false,error}，退出码 0（调用方按 ok 判断）。"""
    from . import tdx_tq

    try:
        _print_json({"ok": True, "formulas": tdx_tq.list_formulas()})
    except tdx_tq.TqUnavailable as e:
        _print_json({"ok": False, "error": str(e)})
    finally:
        tdx_tq.close()


def cmd_tq_select(args) -> None:
    """用通达信引擎选股，输出 {ok, formula, trade_date, picks:[{code,name}], ...}。"""
    from . import tdx_tq
    from .tdx_local_client import local_stock_name

    try:
        sel = tdx_tq.select(args.formula, args.date or None)
        trade_date = sel.trade_date
        boards = None if args.boards.strip().lower() == "all" else tuple(b for b in args.boards.split(",") if b)
        picks = tdx_tq.filter_universe(sel.picks, boards)
        _print_json({
            "ok": True, "engine": "通达信", "formula": args.formula, "trade_date": trade_date,
            "boards": list(boards) if boards else "all", "scanned": sel.scanned, "total_hits": len(sel.picks),
            "skipped_short": sel.skipped_short,
            "picks": [{"code": c, "name": local_stock_name(c) or ""} for c in picks],
        })
    except tdx_tq.TqUnavailable as e:
        _print_json({"ok": False, "error": str(e)})
    finally:
        tdx_tq.close()


def cmd_review(args) -> None:
    """按每日报告的规则复核几只股票，输出一行 JSON。"""
    from .daily_selection_report import review_codes

    date = args.date.strip() if len(args.date.strip()) == 8 and args.date.strip().isdigit() else ""
    _print_json(review_codes([c for c in args.codes.split(",") if c.strip()], args.source.strip(), trade_date=date))


def cmd_forward_test(args) -> None:
    """前向检验，输出一行 JSON。"""
    from pathlib import Path

    from .forward_test import run_forward_test

    out_dir = Path(args.out or os.environ.get("REPORT_OUT_DIR", "").strip() or "reports")
    _print_json(run_forward_test(out_dir))


def cmd_amv_refresh(args) -> None:
    """打开指南针更新活跃市值，打印更新前后的日期。"""
    from datetime import date

    from .zhinanzhen_refresh import refresh_amv

    result = refresh_amv(f"{date.today():%Y-%m-%d}", wait_seconds=args.wait)
    print(f"活跃市值 {result.before} → {result.after}；本次打开={result.started_by_us} 已退出={result.closed} 等待={result.waited_seconds}s")
    if result.error:
        print(result.error)


def _ensure_database() -> None:
    """首次运行时自动建表，避免 README 的第一条命令就报 no such table。"""
    from .database import get_connection, init_database

    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'daily_kline'").fetchone()
    if not exists:
        # init_database 会打印提示，不能混进 --json 的 stdout
        with contextlib.redirect_stdout(sys.stderr):
            init_database()


def main() -> None:
    """zt CLI 主入口"""
    parser = build_parser()

    # argparse 的参数错误也要走统一外壳：--json 模式下 stdout 不能是空的。
    as_json = _wants_json(sys.argv[1:])
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        exit_code = exc.code
        if as_json and exit_code not in (0, None):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": ErrorCode.INVALID_PARAM.value,
                        "message": _argparse_error_message(parser, sys.argv[1:]),
                        "cause": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            # SystemExit.code 可能是 str（argparse 的 --help 等场景），
            # 这里只关心非零整数退出码
            sys.exit(exit_code if isinstance(exit_code, int) else 2)
        raise

    handlers = {
        "analyze": cmd_analyze,
        "screen": cmd_screen,
        "score": cmd_score,
        "workflow": cmd_workflow,
        "diagnose": cmd_diagnose,
        "watchlist": cmd_watchlist,
        "sync": cmd_sync,
        "backtest": cmd_backtest,
        "trade": cmd_trade,
        "daily": cmd_daily,
        "report": cmd_report,
        "review": cmd_review,
        "forward-test": cmd_forward_test,
        "amv-refresh": cmd_amv_refresh,
        "tq-formulas": cmd_tq_formulas,
        "tq-select": cmd_tq_select,
        "market": cmd_market_timing,
        "track": cmd_track,
        "self-optimize": cmd_self_optimize,
        "monitor": cmd_monitor,
        "simulate": cmd_simulate,
        "verify": cmd_verify_v10,
    }

    from .core.errors import ZettarancError

    # JSON 是给程序读的，固定 UTF-8；否则 Windows 默认 GBK，宿主按 UTF-8 解析会失败
    if getattr(args, "json", False):
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        _ensure_database()
        # handler 的返回值是进程退出码。多数 handler 返回 None（等价 0），
        # 但 cmd_verify_v10 / cmd_self_optimize 会返回非 0——以前这里丢弃返回值，
        # 参数错误或未知 action 时进程仍退 0，宿主脚本会误判成功。
        exit_code = handlers[args.command](args) or 0
    except ZettarancError as e:
        _emit_error(e, as_json=bool(getattr(args, "json", False)))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as e:
        # 这些是业务层漏网的异常（未包成 ZettarancError）。此前它们会向上冒泡成
        # 裸 traceback，--json 模式下 stdout 同样是空的。
        _emit_error(e, as_json=bool(getattr(args, "json", False)))

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    disable_proxy()
    main()
