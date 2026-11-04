"""选股数据获取层（支持 DataSource 注入）。"""

from ..database import get_db_connection
from ..datasource import DataSource, dict_to_daily, get_datasource
from ..indicators import DailyData
from ..tdx_local_client import is_st_name

# 向后兼容别名
_dict_to_daily = dict_to_daily


def get_all_stocks(datasource: DataSource | None = None) -> list[dict]:
    """
    获取所有股票基本信息

    优先从注入的 datasource 获取，为空时回退到本地 SQLite
    """
    if datasource is None:
        datasource = get_datasource()

    stocks = datasource.get_stock_list()
    if stocks:
        # 过滤主板/创业板/科创板
        return [s for s in stocks if s.get("market") in ("主板", "创业板", "科创板", None)]

    # 回退到本地
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ts_code, name, industry, market
        FROM stock_basic
        WHERE market IN ('主板', '创业板', '科创板')
        ORDER BY ts_code
    """)
    stocks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return stocks


def select_tradable(stocks: list[dict], include_st: bool = False) -> tuple[list[dict], dict]:
    """从股票池里去掉没有最新行情的股票和 ST，并返回统计。

    最新行情以池内最新的 ``last_date`` 为准。没有该字段的数据源不做这项过滤。
    ST 按当前简称判断，只适用于当前选股；历史回测不能用当前简称回填。
    """
    latest = max((s.get("last_date") or "" for s in stocks), default="")
    tradable: list[dict] = []
    stale = 0
    st = 0
    for stock in stocks:
        last_date = stock.get("last_date")
        if latest and last_date and last_date < latest:
            stale += 1
            continue
        if not include_st and is_st_name(stock.get("name") or ""):
            st += 1
            continue
        tradable.append(stock)
    return tradable, {"as_of": latest or None, "universe": len(stocks), "skipped_stale": stale, "skipped_st": st}


def get_recent_klines(ts_code: str, days: int = 60, datasource: DataSource | None = None) -> list[DailyData]:
    """
    获取近期 K 线数据

    从注入的 datasource 获取并转换为 DailyData（升序）
    """
    if datasource is None:
        datasource = get_datasource()

    rows = datasource.get_kline_dicts(ts_code, days=days)
    if not rows:
        return []

    return _dict_to_daily(rows)
