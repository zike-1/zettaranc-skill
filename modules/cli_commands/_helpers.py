"""CLI 子命令共享工具函数。"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn

from modules.core.errors import ErrorCode

# CLI 统一的错误退出码。顶层 cli.py 的异常路径也用 2——此前 error() 退 1、
# 异常退 2，宿主脚本要记两套码。
ERROR_EXIT_CODE = 2


def json_output(data: Any) -> None:
    """将数据序列化为 JSON 并打印到 stdout"""
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def error(msg: str, *, as_json: bool = False, error_code: ErrorCode | str = ErrorCode.INVALID_PARAM) -> NoReturn:
    """打印错误信息并退出。

    ``as_json=True`` 时 stdout 是**合法 JSON**（统一错误外壳，宿主可按
    ``error_code`` 分支），stderr 仍保留人类可读信息。

    默认只打 stderr——非 JSON 模式下这是正确行为（stdout 保持干净）。
    """
    if as_json:
        code = error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": code,
                    "message": msg,
                    "cause": None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(ERROR_EXIT_CODE)


def data_status(last_date: str | None) -> dict:
    """数据来源与新鲜度，所有分析类命令的 JSON 都带上，宿主据此判断能不能说"现在"。

    只排除周末，不知道法定节假日，所以节假日后可能多报，措辞上留了余地。
    """
    from modules.datasource_resolution import resolve_provider
    from modules.tdx_local_client import business_days_behind

    resolution = resolve_provider()
    warnings: list[str] = []
    behind = None
    if last_date:
        behind = business_days_behind(last_date)
        if behind >= 1:
            warnings.append(f"数据最新到 {last_date}，比最近工作日晚 {behind} 个工作日（节假日除外），结论不代表当前行情")
    else:
        warnings.append("没有取到数据日期")

    # SOP 第一步择时：把活跃市值状态带给宿主，非多头时要提醒
    from modules.active_market_value import current_market_state

    timing = None
    state = current_market_state()
    if state is not None:
        timing = {"state": state.state, "date": state.date, "since": state.since, "pct_chg": state.pct_chg}
        if state.state != "BULL":
            warnings.append(f"活跃市值 {state.date} 不在多头区间（{state.state}，自 {state.since} 起），SOP 第一步不开仓")
    return {
        "as_of": last_date,
        "data_source": resolution.provider or "a-stock-data/sqlite",
        "business_days_behind": behind,
        "market_timing": timing,
        "warnings": warnings,
    }


def warn(msg: str) -> None:
    """打印警告信息到 stderr"""
    print(f"警告: {msg}", file=sys.stderr)
