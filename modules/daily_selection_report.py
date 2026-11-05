"""每日选股报告：只给技能的判断结论，不铺数据。

先 ``collect_report`` 收集成一份 dict（同时存 JSON，供历史表现跟踪和工作台同步），
再由 ``modules.daily_report_html`` 渲染成 HTML。内容：

1. 大盘：活跃市值多空 + 次日翻转门槛。只作背景，不否决个股；
2. 持仓：DSH「持仓」分组。加仓 = 今日 B1（连续几天），另列放飞和卖出信号；
3. 今日选股：本地跑移植的通达信选股公式（modules.tdx_formulas，沪深主板），按技能的卖出信号等
   条件过滤，每只只给买入理由；通达信自己的一键选股结果文件只用来对照；
4. 红利组：通达信行业 电力(T0102) / 银行(T1001) / 保险(T1003)，只列今日 B1；
5. 历史入选表现：之前入选的票 5 / 10 个交易日后的涨跌，对比上证指数。

本地通达信还没下载当天数据时，用腾讯实时行情补当天一根（见 ``prepare_today_overlay``）。
数据只读：通达信目录（blocknew、hq_cache）和 DSH 自选文件都不写。
判断规则全部来自技能已有信号和知识库（放飞见 knowledge/trading-core.md 第 7 层），不是投资建议。
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

HOLDING_GROUP_NAME = "持仓"


# ───────────────────────── 配置（.env / 环境变量，调用时读取） ─────────────────────────


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def default_tdx_dir() -> Path | None:
    """通达信安装目录（TDX_DIR）。"""
    return Path(_env("TDX_DIR")) if _env("TDX_DIR") else None


def default_watch_files() -> tuple[Path | None, Path | None]:
    """DSH 工作台自选文件：DSH_WATCHLIST_FILE，或工作台目录（ZT_DSH_HOME，其次 DSH_HOME）下的 watchlists.json。

    ZT_DSH_HOME 优先：系统里常有给其他 DSH 安装用的 DSH_HOME 环境变量，它会盖过技能 .env 里的同名设置
    （2026-11-04 晚的定时任务因此读错目录：持仓为空、选股名单没交给工作台）。都没配置时不读自选。
    """
    home = _env("ZT_DSH_HOME") or _env("DSH_HOME")
    if _env("DSH_WATCHLIST_FILE"):
        watch = Path(_env("DSH_WATCHLIST_FILE"))
    elif home:
        watch = Path(home) / "watchlists.json"
    else:
        return None, None
    return watch, watch.with_name("watchlist-groups.json")


def configured_blocks() -> tuple[str, ...]:
    """报告要跑的选股名（ZT_REPORT_BLOCKS，逗号分隔），如「B1选股,金砖共振」。"""
    return tuple(name.strip() for name in _env("ZT_REPORT_BLOCKS").split(",") if name.strip())


def tdx_formula_names() -> dict[str, str]:
    """报告里的选股名 → 通达信里的公式名（ZT_TDX_FORMULA_MAP，如「B1选股=我的B1,金砖共振=我的金砖」）。
    不在表里的选股名直接当公式名用。"""
    mapping: dict[str, str] = {}
    for item in _env("ZT_TDX_FORMULA_MAP").replace("；", ",").replace(";", ",").split(","):
        if "=" in item:
            left, right = item.split("=", 1)
            if left.strip() and right.strip():
                mapping[left.strip()] = right.strip()
    return mapping


#: 红利组：通达信行业代码前缀 → 显示名（tdxzs3.cfg：电力 T0102、银行 T1001、保险 T1003）
DIVIDEND_INDUSTRIES = {"T0102": "电力", "T1001": "银行", "T1003": "保险"}
#: 卖出信号回看几个交易日
SELL_LOOKBACK = 5
BUY_STRATEGIES = {"B1", "SB1", "超级B1"}
SELL_STRATEGIES = {"S1", "S2", "S3", "四块砖翻绿"}
#: 技能里「四块砖翻绿」是任何一次红砖转绿（持仓止损信号），选股时不一票否决，见 selection_exclusion
BRICK_FLIP = "四块砖翻绿"
#: 中阳 / 大阳放飞门槛（knowledge/trading-core.md：4% 中阳、7% 大阳）
MID_YANG_PCT = 4.0
BIG_YANG_PCT = 7.0
BOLL_PERIOD = 20
BOLL_WIDTH = 2.0
#: 单只分析取的日线根数（analyze_core 与砖块状态共用）；周线 BOLL 另取 400 根
ANALYZE_DAYS = 250
#: 历史表现看入选后第几个交易日
FOLLOW_UP_DAYS = (5, 10)
#: 活跃市值走势图显示多少天
AMV_SERIES_DAYS = 60
#: 实时行情每批多少只、批间隔几秒（免费接口，串行限速；东财另有自己的 1 秒节流）
QUOTE_BATCH = 50
QUOTE_PAUSE = 0.6
#: 收盘后多久才补当天 K 线
QUOTE_AFTER = (15, 5)
#: 判断最近一个交易日用的几只沪市权重股：休市日、开盘前它们的行情时间停在上一个交易日
SESSION_PROBES = ("600519", "601318", "600036")


# ───────────────────────── 通达信文件（只读） ─────────────────────────


def parse_block_cfg(raw: bytes) -> dict[str, str]:
    """blocknew.cfg：每条 120 字节，前 50 字节板块名、后 70 字节文件代码，GBK。返回 名称 → 代码。"""
    blocks: dict[str, str] = {}
    for offset in range(0, len(raw) - 119, 120):
        record = raw[offset : offset + 120]
        name = record[:50].split(b"\0")[0].decode("gbk", "replace").strip()
        code = record[50:120].split(b"\0")[0].decode("gbk", "replace").strip()
        if name and code:
            blocks[name] = code
    return blocks


def parse_blk(text: str) -> list[str]:
    """板块文件：每行「市场位 + 6 位代码」，1=沪 0=深 2=北。返回 6 位代码，去重保序。"""
    codes: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) == 7 and line.isdigit() and line[0] in "012" and line[1:] not in codes:
            codes.append(line[1:])
    return codes


def parse_block_dat(raw: bytes) -> list[tuple[str, str, float]]:
    """板块旁边的 .dat：每只 80 字节，返回 [(6 位代码, 入选日 YYYYMMDD, 入选日收盘价)]。

    只用已核对过的字段：第 2~7 字节代码，第 33 字节起 uint32 日期、第 37 字节起 float32 价格。
    2026-11-03 核对：确认战法 25 只代码与 .blk 完全一致，9 只的价格等于当天通达信收盘价。
    其余字节含义不明，不读。
    """
    rows: list[tuple[str, str, float]] = []
    for offset in range(0, len(raw) - 79, 80):
        record = raw[offset : offset + 80]
        code = record[2:8].decode("ascii", "replace")
        if not code.isdigit():
            continue
        day = struct.unpack("<I", record[33:37])[0]
        price = struct.unpack("<f", record[37:41])[0]
        rows.append((code, str(day), round(price, 2)))
    return rows


def parse_industry_cfg(text: str, prefixes: dict[str, str]) -> dict[str, str]:
    """tdxhy.cfg：`市场|代码|T行业|||X行业`。返回 代码 → 行业显示名（只保留 prefixes 命中的）。"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        for prefix, label in prefixes.items():
            if parts[2].startswith(prefix):
                result[parts[1]] = label
                break
    return result


@dataclass
class SelectionBlock:
    name: str
    codes: list[str]
    updated_at: datetime | None
    missing: bool = False


def read_selection_block(tdx_dir: Path, name: str, trade_date: str = "") -> SelectionBlock:
    """读通达信一键选股的结果，用来和本地移植公式的结果对照。

    以 .dat 里入选日等于 trade_date 的记录为准，没有 .dat 时退回 .blk。2026-11-03 核对：
    金砖共振 .blk 为空而 .dat 有 6 只，用户截图的通达信选股结果正是这 6 只；确认战法两份一致。
    """
    blocknew = tdx_dir / "T0002" / "blocknew"
    try:
        mapping = parse_block_cfg((blocknew / "blocknew.cfg").read_bytes())
    except OSError:
        return SelectionBlock(name, [], None, missing=True)
    code = mapping.get(name)
    if code is None:
        return SelectionBlock(name, [], None, missing=True)
    path = blocknew / f"{code}.blk"
    try:
        codes = parse_blk(path.read_bytes().decode("gbk", "replace"))
        updated = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return SelectionBlock(name, [], None, missing=True)
    dat_path = blocknew / f"{code}.dat"
    if trade_date and dat_path.is_file():
        try:
            codes = list(dict.fromkeys(c for c, day, _ in parse_block_dat(dat_path.read_bytes()) if day == trade_date))
            updated = max(updated, datetime.fromtimestamp(dat_path.stat().st_mtime))
        except OSError as e:
            logger.warning("读 %s 失败，退回 .blk: %s", dat_path, e)
    return SelectionBlock(name, codes, updated)


def compare_with_tdx(local: list[str], block: SelectionBlock, trade_date: str) -> dict:
    """本地公式结果与通达信一键选股对照。通达信文件不是该交易日收盘后（15:00 以后）写的就不对照。"""
    close_time = datetime.strptime(trade_date, "%Y%m%d").replace(hour=15) if trade_date else None
    fresh = bool(block.updated_at and close_time and block.updated_at >= close_time)
    info = {
        "count": len(block.codes),
        "updated_at": f"{block.updated_at:%Y-%m-%d %H:%M}" if block.updated_at else "",
        "fresh": fresh,
        "missing": block.missing,
    }
    if fresh:
        info["only_local"] = [c for c in local if c not in block.codes]
        info["only_tdx"] = [c for c in block.codes if c not in local]
    return info


def read_dividend_universe(tdx_dir: Path) -> dict[str, str]:
    path = tdx_dir / "T0002" / "hq_cache" / "tdxhy.cfg"
    try:
        return parse_industry_cfg(path.read_bytes().decode("gbk", "replace"), DIVIDEND_INDUSTRIES)
    except OSError:
        return {}


def read_dsh_watchlist(watch_file: Path | None, groups_file: Path | None) -> tuple[list[str], list[str]]:
    """DSH 终端的 A 股自选（只读）。返回 (持仓, 其余自选)，6 位代码；没配置工作台时为空。"""
    if watch_file is None or groups_file is None:
        return [], []
    try:
        watch = json.loads(watch_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    holding_ids: set[str] = set()
    try:
        groups = json.loads(groups_file.read_text(encoding="utf-8")).get("groups", [])
        holding_ids = {g["id"] for g in groups if g.get("name") == HOLDING_GROUP_NAME}
    except (OSError, ValueError, KeyError):
        pass
    holdings: list[str] = []
    others: list[str] = []
    for row in watch.get("cn", []):
        code = str(row.get("symbol", "")).split(".")[0]
        if len(code) != 6 or not code.isdigit():
            continue
        (holdings if holding_ids & set(row.get("groups", [])) else others).append(code)
    return holdings, others


# ───────────────────────── 当天 K 线（实时行情补齐） ─────────────────────────


@dataclass
class OverlayResult:
    trade_date: str = ""
    applied: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""
    #: 每个来源补上了几只，如 {"腾讯": 3, "东财": 1}
    sources: dict[str, int] = field(default_factory=dict)


def quote_sources() -> list[tuple[str, object]]:
    """补当天 K 线的行情来源，按顺序尝试：腾讯 → 同花顺（配置了 Key 才用）→ 东财。

    每个来源是 ``fetch(codes) -> {6 位代码: 行情 dict}``，字段口径同 tencent_quote。
    """
    from modules.a_stock_data_client import eastmoney_quote, tencent_quote
    from modules.hithink_client import HithinkFinanceClient

    sources: list[tuple[str, object]] = [("腾讯", tencent_quote)]
    hithink = HithinkFinanceClient()
    if hithink.is_configured:
        sources.append(("同花顺", hithink.quote_bars))
    sources.append(("东财", eastmoney_quote))
    return sources


def fill_today_bars(
    local: dict[str, tuple[float, str]], sources: list[tuple[str, object]], pause: float = QUOTE_PAUSE
) -> tuple[dict[str, dict], OverlayResult]:
    """逐个来源补当天 K 线。上一个来源请求失败或没返回的票才交给下一个；停牌、昨收对不上的不再重试。"""
    result = OverlayResult()
    overlay: dict[str, dict] = {}
    pending = list(local)
    failures: list[str] = []
    for name, fetch in sources:
        if not pending:
            break
        missing: list[str] = []
        for start in range(0, len(pending), QUOTE_BATCH):
            batch = pending[start : start + QUOTE_BATCH]
            try:
                quotes = fetch(batch)
            except (OSError, ValueError) as e:
                failures.append(f"{name}：{e}")
                missing.extend(pending[start:])
                break
            for code in batch:
                quote = quotes.get(code)
                if quote is None:
                    missing.append(code)
                    continue
                bar, reason = quote_to_bar(quote, *local[code])
                if bar is not None:
                    overlay[code] = bar
                    result.sources[name] = result.sources.get(name, 0) + 1
                    result.trade_date = max(result.trade_date, bar["trade_date"])
                elif reason:
                    result.skipped.append((code, f"{reason}（{name}）"))
            if start + QUOTE_BATCH < len(pending):
                time.sleep(pause)
        pending = missing
    result.skipped.extend((code, "各行情来源都没有") for code in pending)
    if failures:
        result.error = "；".join(failures)
    result.applied = len(overlay)
    return overlay, result


def quote_to_bar(quote: dict, local_close: float, local_date: str) -> tuple[dict | None, str]:
    """实时行情（腾讯 / 同花顺 / 东财，同一口径）→ 当天未复权 K 线。返回 (bar, 跳过原因)。

    昨收和本地最后一根收盘对不上时跳过：多半是今天除权除息，本地除权文件还没有这条记录，
    硬拼会让复权口径错位。
    """
    when = str(quote.get("quote_time", ""))[:8]
    if len(when) != 8 or not when.isdigit():
        return None, "行情没有时间"
    if when <= local_date:
        return None, ""  # 本地已有当天
    # 只补紧挨着本地最后一根的那一天：中间缺了交易日硬接上去，均线和「近 5 日」信号都会算错。
    # 没有节假日表，按工作日数：节后第一天本地还没下载时也会跳过，报告里写明原因，宁缺勿错。
    from modules.tdx_local_client import business_days_behind

    gap = business_days_behind(local_date, datetime.strptime(when, "%Y%m%d").date())
    if gap > 1:
        return None, f"本地日线停在 {local_date}，中间缺 {gap - 1} 个工作日，先在通达信下载盘后数据"
    if not quote.get("price") or not quote.get("volume_lots"):
        return None, "停牌或无成交"
    if abs(float(quote["last_close"]) - local_close) > 0.011:
        return None, f"昨收 {quote['last_close']} 与本地 {local_close} 不符（可能今天除权）"
    return {
        "trade_date": when,
        "open": float(quote["open"]),
        "high": float(quote["high"]),
        "low": float(quote["low"]),
        "close": float(quote["price"]),
        "volume_shares": float(quote["volume_lots"]) * 100,
        "amount_yuan": float(quote["amount_wan"]) * 10000,
    }, ""


def prepare_today_overlay(codes: list[str], now: datetime | None = None) -> OverlayResult:
    """收盘后本地缺当天 K 线时，用实时行情补一根（登记到 tdx_local_client，进程内有效）。"""
    from modules.tdx_local_client import TdxLocalClient, configured_root, set_today_overlay

    now = now or datetime.now()
    result = OverlayResult()
    if now.weekday() >= 5 or (now.hour, now.minute) < QUOTE_AFTER:
        return result
    root = configured_root()
    if root is None:
        return result
    client = TdxLocalClient(root)
    local: dict[str, tuple[float, str]] = {}
    for code in dict.fromkeys(codes):
        series = client.raw_series(code)  # ETF 价格按 ×1000 解码，和个股一样换成元
        if series is not None:
            local[code] = (float(series["close"][-1]), str(int(series["date"][-1])))
    overlay, result = fill_today_bars(local, quote_sources())
    # 补算历史日期时（now 是过去的某天），行情接口给的是「现在」的行情：比报告日期新的不要
    limit = now.strftime("%Y%m%d")
    late = [code for code, bar in overlay.items() if bar["trade_date"] > limit]
    for code in late:
        del overlay[code]
        result.skipped.append((code, f"行情日期晚于 {limit}"))
    if late:
        result.applied = len(overlay)
        result.trade_date = max((bar["trade_date"] for bar in overlay.values()), default="")
        result.sources = {}
    set_today_overlay(overlay)
    return result


# ───────────────────────── 判断规则（纯函数） ─────────────────────────


def boll_position(closes: list[float], period: int = BOLL_PERIOD, width: float = BOLL_WIDTH) -> str:
    """最新收盘在布林带的位置；带宽为近 period 期最窄时加「收口」。数据不足返回「数据不足」。

    口径同通达信 BOLL：中轨 MA(C,N)，上下轨 ± width × 总体标准差。
    """
    if len(closes) < period:
        return "数据不足"

    def band(end: int) -> tuple[float, float, float]:
        window = closes[end - period : end]
        mid = sum(window) / period
        std = statistics.pstdev(window)
        return mid - width * std, mid, mid + width * std

    lower, mid, upper = band(len(closes))
    close = closes[-1]
    if close > upper:
        label = "上轨之上"
    elif close >= mid:
        label = "中轨~上轨"
    elif close >= lower:
        label = "下轨~中轨"
    else:
        label = "下轨之下"
    if len(closes) >= period * 2 - 1 and mid > 0:
        widths = []
        for end in range(len(closes) - period + 1, len(closes) + 1):
            lo, m, up = band(end)
            widths.append((up - lo) / m if m else 0)
        if widths[-1] <= min(widths):
            label += "·收口"
    return label


def weekly_closes(rows: list[dict]) -> list[float]:
    """日线 → 周收盘（按 ISO 周分组，取每周最后一根；当周未结束时用最新一根，同通达信）。"""
    closes: list[float] = []
    last_key: tuple[int, int] | None = None
    for row in rows:
        d = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
        key = d.isocalendar()[:2]
        if key == last_key:
            closes[-1] = row["close"]
        else:
            closes.append(row["close"])
            last_key = key
    return closes


def flyaway_verdict(red_bricks: int, brick_trend: str, pct_chg: float, close: float, bbi: float) -> str:
    """放飞：第四块红砖必放飞；BBI 之上的中阳(≥4%) / 大阳(≥7%) 放飞。返回空串表示不触发。"""
    if brick_trend == "RED" and red_bricks == 4:
        return "第四块红砖，至少放飞一半"
    if close > bbi > 0 and pct_chg >= BIG_YANG_PCT:
        return "BBI上大阳，放飞"
    if close > bbi > 0 and pct_chg >= MID_YANG_PCT:
        return "BBI上中阳，分批放飞"
    return ""


def is_true_b1(kind: str, description: str) -> bool:
    """技能把 MACD 死叉多、灾后重建、阳线关键K 也记成 B1 类型；只有 J 值 B1 和超级B1 才算 B1 标准。"""
    if kind in ("SB1", "超级B1"):
        return True
    return kind == "B1" and description.strip().startswith("B1买点")


def sell_label(kind: str, description: str) -> str:
    """卖出信号的显示名：S1 类型下有阶梯放量下跌、出货五式等多种形态，写具体形态名。"""
    if kind != "S1" or "：" not in description:
        return kind
    head, _, rest = description.partition("：")
    head = head.replace(" ", "")
    if head in ("出货五式", "关键K"):
        return f"{head}·{rest.split(' - ')[0].strip()}"
    return head


def b1_streak(buy_dates: set[str], trade_dates: list[str]) -> int:
    """从最新交易日往回数，连续几天有 B1 类买点；今天没有返回 0。"""
    count = 0
    for day in reversed(trade_dates):
        if day not in buy_dates:
            break
        count += 1
    return count


def add_position_verdict(streak: int) -> str:
    """持仓加仓：目前只看 B1。活跃市值只作背景，不否决。"""
    if streak <= 0:
        return ""
    return "今日B1，可加仓" if streak == 1 else f"连续{streak}天B1，可加仓"


def line_position(close: float, white: float, yellow: float) -> str:
    """相对白线（短期趋势）和黄线（多空线）的位置。"""
    if white <= 0 or yellow <= 0:
        return ""
    if close >= white:
        return "白线上方"
    if close >= yellow:
        return "白黄之间"
    return "跌破黄线"


def green_before_red(daily: list, rows: list[dict]) -> tuple[int, bool]:
    """今天是红砖时，之前连着几块绿砖，以及今天最低价是否低于这段绿砖的最低价。今天不是红砖 → (0, False)。

    砖色口径同 detect_four_brick_system：今日砖值 >= 昨日为红。
    """
    from modules.indicators.price_patterns.brick import calculate_brick_series

    bricks = calculate_brick_series(daily)
    if len(bricks) < 3 or bricks[-1] < bricks[-2]:
        return 0, False
    green = 0
    i = len(bricks) - 2
    while i >= 1 and bricks[i] < bricks[i - 1]:
        green += 1
        i -= 1
    if green == 0:
        return 0, False
    green_lows = [float(r["low"]) for r in rows[-(green + 1):-1]]
    return green, bool(green_lows) and float(rows[-1]["low"]) < min(green_lows)


def selection_exclusion(name: str, recent_sells: list[str], brick_action: str, red_bricks: int, macd_veto: bool, close: float, yellow: float,
                        white: float = 0, green_before: int = 0, new_low: bool = False) -> str:
    """选股结果剔除原因；空串 = 保留。活跃市值不在这里，它只作背景。

    近 5 日的「四块砖翻绿」（任何一次红砖转绿）不一票否决，按四块砖原文判断（knowledge/trading-core.md 3.2、
    brick-moving.md）：白线之上的绿砖看作洗盘；绿砖数满 4 块、翻红且不创新低才试错。两条都不满足才剔除。
    """
    if "ST" in name.upper():
        return "ST"
    other_sells = [sell for sell in recent_sells if sell != BRICK_FLIP]
    if other_sells:
        return f"近{SELL_LOOKBACK}日{other_sells[0]}"
    if brick_action == "止损":
        return "红砖翻绿"
    if red_bricks >= 4:
        return f"已第{red_bricks}块红砖"
    if BRICK_FLIP in recent_sells:
        above_white = white > 0 and close >= white
        counted = green_before >= 4 and not new_low
        if not (above_white or counted):
            if green_before >= 4:
                return "绿砖数满4块翻红但创新低（白线下方）"
            return f"近{SELL_LOOKBACK}日红砖翻绿，白线下方且绿砖未数满4块"
    if macd_veto:
        return "MACD一票否决"
    if yellow > 0 and close < yellow:
        return "跌破黄线"
    return ""


def follow_up_returns(
    rows: list[dict], pick_date: str, horizons: tuple[int, ...] = FOLLOW_UP_DAYS, calendar: list[str] | None = None
) -> dict[int, float | None]:
    """入选日收盘买入，第 N 个交易日收盘的涨跌幅（%）；还没走到那天的为 None。rows 用等比复权口径。

    calendar（市场交易日，通常取上证指数日线）给了时，第 N 天按市场日历数，停牌的股票取那天之前最后一根
    收盘（停牌估值），所有股票、指数的结束日一致；不给时按这只股票自己的第 N 根 K 线（旧口径，测试用）。
    """
    dates = [str(r["trade_date"]) for r in rows]
    if pick_date not in dates:
        return dict.fromkeys(horizons)
    i = dates.index(pick_date)
    base = float(rows[i]["close"])
    out: dict[int, float | None] = {}
    for n in horizons:
        if not base:
            out[n] = None
            continue
        if calendar is None:
            out[n] = round((float(rows[i + n]["close"]) / base - 1) * 100, 2) if i + n < len(rows) else None
            continue
        if pick_date not in calendar or calendar.index(pick_date) + n >= len(calendar):
            out[n] = None
            continue
        end = calendar[calendar.index(pick_date) + n]
        j = max(k for k in range(i, len(dates)) if dates[k] <= end)
        out[n] = round((float(rows[j]["close"]) / base - 1) * 100, 2)
    return out


# ───────────────────────── 单只股票 ─────────────────────────


@dataclass
class StockView:
    code: str
    name: str = ""
    industry: str = ""
    as_of: str = ""
    error: str = ""
    brick: str = ""
    brick_trend: str = ""
    red_bricks: int = 0
    brick_action: str = ""
    b1_today: str = ""
    b1_text: str = ""
    b1_streak: int = 0
    b1_score: float = 0
    add_verdict: str = ""
    flyaway: str = ""
    sells: list[str] = field(default_factory=list)
    sells_today: list[str] = field(default_factory=list)
    boll_day: str = ""
    boll_week: str = ""
    macd_veto: bool = False
    close: float = 0
    pct_chg: float = 0
    white: float = 0
    yellow: float = 0
    line: str = ""
    #: 今天红砖时，之前连着几块绿砖（回调后翻红）；今天不是红砖 = 0
    green_before: int = 0
    #: 今天的最低价低于这段绿砖的最低价（翻红但创新低）
    new_low: bool = False


def analyze_view(code: str, industry: str = "") -> StockView:
    from modules.cli_commands.analyze import analyze_core
    from modules.indicators.data_layer import DailyData
    from modules.indicators.price_patterns.brick import detect_four_brick_system
    from modules.strategies.core import get_kline_data

    view = StockView(code=code, industry=industry)
    try:
        rows = get_kline_data(code, days=400)
        if len(rows) < 30:
            view.error = "K线不足"
            return view
        core = analyze_core(code, days=ANALYZE_DAYS)
    except Exception as e:  # noqa: BLE001 — 单只失败不影响整份报告，原因写进报告
        view.error = str(e)[:40] or type(e).__name__
        return view

    result, diagnosis, score = core["result"], core["diagnosis"], core["score"]
    last = rows[-1]
    view.name = getattr(diagnosis, "name", "") or code
    view.as_of = str(result.trade_date)
    view.close = float(last["close"])
    view.pct_chg = round(float(last.get("pct_chg") or 0), 2)
    view.macd_veto = bool(getattr(diagnosis, "macd_veto", False))
    view.yellow = float(getattr(diagnosis, "yellow_line", 0) or 0)
    view.white = float(getattr(diagnosis, "white_line", 0) or 0)
    view.line = line_position(view.close, view.white, view.yellow)

    view.brick_trend = result.brick_trend
    view.red_bricks = result.brick_count if result.brick_trend == "RED" else 0
    color = {"RED": "红砖", "GREEN": "绿砖"}.get(result.brick_trend, "无砖")
    view.brick = f"第{result.brick_count}块{color}" if result.brick_count else color
    daily = [
        DailyData(ts_code=r["ts_code"], trade_date=r["trade_date"], open=r["open"], high=r["high"], low=r["low"],
                  close=r["close"], vol=r["vol"], amount=r["amount"], pct_chg=r["pct_chg"],
                  prev_close=rows[i - 1]["close"] if i else r["close"])
        for i, r in enumerate(rows)
    ]
    # 砖值是递推 SMA，起点不同结果会差一点：和 analyze_core 用同样的 250 根
    brick_state = detect_four_brick_system(daily[-ANALYZE_DAYS:])
    view.brick_action = brick_state.get("brick_action", "")
    if brick_state.get("is_brick_flip_green"):
        view.brick = "红砖翻绿"
    view.green_before, view.new_low = green_before_red(daily[-ANALYZE_DAYS:], rows)

    trade_dates = [str(r["trade_date"]) for r in rows]
    recent_dates = set(trade_dates[-SELL_LOOKBACK:])
    today = trade_dates[-1]
    buy_dates: set[str] = set()
    for signal in core["signals"]:
        kind = signal.strategy.value
        day = str(signal.trade_date)
        description = (signal.description or "").strip()
        if kind in BUY_STRATEGIES and is_true_b1(kind, description):
            buy_dates.add(day)
            if day == today and not view.b1_today:
                view.b1_today = "超级B1" if kind in ("SB1", "超级B1") else "B1"
                view.b1_text = description
        if kind in SELL_STRATEGIES and day in recent_dates:
            label = sell_label(kind, description)
            if label not in view.sells:
                view.sells.append(label)
            if day == today and label not in view.sells_today:
                view.sells_today.append(label)
    view.b1_streak = b1_streak(buy_dates, trade_dates)
    view.b1_score = float(getattr(score, "b1_score", 0) or 0)
    view.add_verdict = add_position_verdict(view.b1_streak)
    view.flyaway = flyaway_verdict(view.red_bricks, view.brick_trend, view.pct_chg, view.close, float(result.bbi or 0))
    closes = [float(r["close"]) for r in rows]
    view.boll_day = boll_position(closes)
    view.boll_week = boll_position(weekly_closes(rows))
    return view


def buy_reasons(view: StockView, sources: list[str]) -> list[str]:
    """选股的买入理由：哪个选股公式选出来的 + 技能确认到的要点，短标签。"""
    reasons = [f"{s}选出" for s in sources]
    if view.b1_streak > 1:
        reasons.append(f"连续{view.b1_streak}天B1")
    if view.b1_text:
        reasons.append(view.b1_text)
    elif view.b1_today:
        reasons.append(f"今日{view.b1_today}")
    if view.green_before:
        reasons.append(f"回调{view.green_before}块绿砖后翻红")
    elif view.brick_trend == "RED" and view.red_bricks:
        reasons.append(f"第{view.red_bricks}块红砖")
    if view.line in ("白线上方", "白黄之间"):
        reasons.append("回踩白黄之间" if view.line == "白黄之间" else view.line)
    return reasons


# ───────────────────────── 活跃市值 ─────────────────────────


def amv_summary(series_days: int = AMV_SERIES_DAYS) -> dict | None:
    from modules.active_market_value import load_active_market_value, market_states, next_day_triggers

    try:
        points = load_active_market_value()
        states = market_states(points)
    except (OSError, ValueError) as e:
        logger.warning("读取活跃市值失败: %s", e)
        return None
    if not states:
        return None
    last = states[-1]
    trig = next_day_triggers(last)
    days_in_state = sum(1 for s in states if s.since == last.since and s.state == last.state)
    closes = {p.date: p.close for p in points}
    return {
        "date": last.date,
        "state": last.state,
        "since": last.since,
        "days_in_state": days_in_state,
        "pct_chg": last.pct_chg,
        "bull_at": trig["next_day_bull_at"],
        "bear_at": trig["next_day_bear_at"],
        "series": [{"date": s.date, "close": closes.get(s.date, 0), "state": s.state} for s in states[-series_days:]],
    }


# ───────────────────────── 历史表现 ─────────────────────────


def report_days(data_dir: Path) -> list[str]:
    """data 目录里已有报告的日期（文件名 YYYY-MM-DD.json），升序。"""
    return sorted(p.stem for p in data_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))


def load_history(data_dir: Path, before: str) -> list[dict]:
    """之前各天存下的报告数据（不含 before 当天），按日期升序。"""
    reports = []
    for day in report_days(data_dir):
        if day >= before:
            continue
        path = data_dir / f"{day}.json"
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            logger.warning("跳过读不了的历史报告 %s: %s", path, e)
    return reports


def latest_session_date(fetch=None) -> str:
    """最近一个交易日（YYYYMMDD），按几只沪市权重股的腾讯行情时间判断；查不到返回空串。

    休市日、开盘前行情时间停在上一个交易日，所以节假日返回的是节前最后一个交易日。
    只用来决定任务计划要不要跑、活跃市值等到哪天，查不到就当不知道，不往外抛。
    """
    if fetch is None:
        from modules.a_stock_data_client import tencent_quote as fetch
    try:
        quotes = fetch(list(SESSION_PROBES))
    except Exception as e:  # noqa: BLE001 —— 网络、解析失败都只意味着「不知道」
        logger.warning("查最近一个交易日失败：%s", e)
        return ""
    days = (str(quote.get("quote_time", ""))[:8] for quote in quotes.values())
    return max((d for d in days if len(d) == 8 and d.isdigit()), default="")


def report_for_session(data_dir: Path, session_date: str, recent: int = 10) -> str:
    """最近几份报告里，数据日期是 ``session_date`` 的那份的文件日期（YYYY-MM-DD）；没有返回空串。"""
    for day in reversed(report_days(data_dir)[-recent:]):
        try:
            data = json.loads((data_dir / f"{day}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("data_date") == session_date:
            return day
    return ""


def skip_reason(data_dir: Path, session_date: str, today: date) -> str:
    """任务计划用：今天没有新的交易日（休市或还没开盘），且最近一个交易日的报告已经有了，返回跳过说明。

    报告按运行当天命名，休市日再跑一次会多出一份内容和上一交易日重复的报告，还要让指南针空等、
    请求几千只股票的行情。今天是交易日、查不到最近交易日、或上一个交易日还没有报告时返回空串，照常生成。
    """
    if not session_date or session_date >= f"{today:%Y%m%d}":
        return ""
    day = report_for_session(data_dir, session_date)
    return f"今天没有新的交易日（最近一个是 {session_date}），它的报告 {day} 已经生成" if day else ""


def follow_up_section(history: list[dict]) -> dict:
    """历史入选的票后来怎么样：入选日收盘到第 5 / 10 个交易日收盘，等比复权；对比上证指数。"""
    from modules.tdx_local_client import local_kline_rows

    rows_cache: dict[str, list[dict]] = {}

    def rows_of(code: str, basis: str = "return") -> list[dict]:
        key = f"{code}:{basis}"
        if key not in rows_cache:
            rows_cache[key] = local_kline_rows(code, days=0, basis=basis) or []
        return rows_cache[key]

    calendar = [str(r["trade_date"]) for r in rows_of("000001.SH", "raw")] or None
    entries: list[dict] = []
    # 同一个数据日期只算一次：休市日、补跑会多出文件名不同、内容相同的报告，以后生成的为准
    latest: dict[str, dict] = {}
    for report in history:
        latest[report.get("data_date") or report.get("date", "")] = report
    for report in latest.values():
        pick_date = report.get("data_date", "")
        bench = follow_up_returns(rows_of("000001.SH", "raw"), pick_date, calendar=calendar)
        for pick in report.get("picks", []):
            returns = follow_up_returns(rows_of(pick["code"]), pick_date, calendar=calendar)
            entries.append({
                "date": pick_date,
                "code": pick["code"],
                "name": pick.get("name", ""),
                "sources": pick.get("sources", []),
                "returns": {str(n): v for n, v in returns.items()},
                "bench": {str(n): v for n, v in bench.items()},
            })
    summary = {}
    for n in FOLLOW_UP_DAYS:
        done = [e for e in entries if e["returns"][str(n)] is not None]
        if not done:
            continue
        excess = [e["returns"][str(n)] - (e["bench"][str(n)] or 0) for e in done]
        summary[str(n)] = {
            "count": len(done),
            "avg": round(statistics.mean(e["returns"][str(n)] for e in done), 2),
            "win_rate": round(sum(1 for e in done if e["returns"][str(n)] > 0) / len(done) * 100, 1),
            "avg_excess": round(statistics.mean(excess), 2),
        }
    entries.sort(key=lambda e: (e["date"], e["code"]), reverse=True)
    return {"entries": entries, "summary": summary}


# ───────────────────────── 汇总 ─────────────────────────


def tq_usable_for(trade_date: str, now: datetime) -> bool:
    """通达信引擎能不能算这个交易日：公式里的 DYNAINFO 等实时函数只反映「现在」的行情
    （2026-11-04 盘中补算 11-03，金砖共振的换手率条件用了当天早盘的换手率，一只都选不出），
    所以只在算「今天、且已收盘」时用。补算历史日期一律用本地移植公式。"""
    return trade_date == now.strftime("%Y%m%d") and (now.hour, now.minute) >= (15, 0)


def run_block_selection(blocks: tuple[str, ...], trade_date: str, use_tq: bool = True, now: datetime | None = None
                        ) -> tuple[dict[str, list[str]], str, str, int, int]:
    """跑各选股公式，返回 ({选股名: [6 位代码]}, 引擎, 说明, 扫描只数, 出错只数)。

    算今天收盘后的结果、且通达信开着时用通达信引擎（结果与一键选股一致）；它不可用、
    它的数据里没有 trade_date 这一根（扫描到 0 只），或是补算历史日期时，退回本地移植的公式。
    没移植的公式在退回时不出现在结果里。
    """
    from modules.tdx_formulas import load_formulas
    from modules.tdx_selection import DEFAULT_BOARDS, run_selection

    if not blocks or not trade_date:
        return {}, "", "", 0, 0
    note = ""
    formula_names = tdx_formula_names()
    if use_tq and not tq_usable_for(trade_date, now or datetime.now()):
        note = "补算历史日期：通达信公式里的实时函数只反映当前行情，改用本地移植公式"
        use_tq = False
    if use_tq:
        from modules import tdx_tq

        try:
            picks: dict[str, list[str]] = {}
            scanned = 0
            for name in blocks:
                sel = tdx_tq.select(formula_names.get(name, name), trade_date)
                if sel.scanned == 0:
                    raise tdx_tq.TqUnavailable(f"通达信数据里还没有 {trade_date} 的 K 线")
                scanned = max(scanned, sel.scanned)
                picks[name] = [c.split(".")[0] for c in tdx_tq.filter_universe(sel.picks, DEFAULT_BOARDS)]
            return picks, "通达信", "", scanned, 0
        except tdx_tq.TqUnavailable as e:
            note = f"通达信引擎不可用，改用本地移植公式：{e}"
            logger.warning(note)
        finally:
            tdx_tq.close()
    available = load_formulas()
    names = tuple(n for n in blocks if n in available)
    if not names:
        return {}, "本地移植", note, 0, 0
    run = run_selection(names=names, trade_date=trade_date)
    return dict(run.picks), "本地移植", note, run.scanned, len(run.errors)


def _view_dict(view: StockView) -> dict:
    return asdict(view)


# ───────────────────────── 工作台复核 ─────────────────────────

#: 复核一次最多看几只（每只约半秒）
REVIEW_LIMIT = 120
_CODE_RE = re.compile(r"(\d{6})(\.(SH|SZ|BJ))?|(\d{5})\.HK")
#: 港股复核只作参考：写进每只的 notes
HK_NOTES = [
    "港股仅作参考：选股与卖出规则按 A 股框架和参数，未在港股上验证",
    "本地没有港股除权记录，港股 K 线未复权，除权除息日附近的指标会失真",
    "活跃市值是 A 股指标，对港股只作背景",
]


def review_codes(codes: list[str], source: str = "", now: datetime | None = None, trade_date: str = "") -> dict:
    """工作台用：按每日报告同一套规则复核几只股票（选股页签的过滤、Z哥诊断卡）。

    每只给出 status（kept 留下 / dropped 剔除 / stale 待复核）、剔除原因或买入理由，以及砖块、B1、卖出信号
    等要点。代码原样保留交易所（000001.SH 是上证指数，不能截成 000001 再按代码段推断成平安银行）。
    trade_date（选股日）给了时，本地数据早于它的标 stale：盘中通达信按当天实时数据选出，本地日线还停在昨天，
    不能拿昨天的卖出信号剔除今天才出现的信号。收盘后本地缺当天 K 线时用实时行情补一根，和报告口径一致。
    只读，不写任何文件；活跃市值只作背景。
    """
    from modules.tdx_local_client import clear_today_overlay, to_symbol

    targets: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        match = _CODE_RE.fullmatch(raw.strip().upper())
        if not match:
            continue
        code = f"{match.group(4)}.HK" if match.group(4) else f"{match.group(1)}{match.group(2) or ''}"
        if to_symbol(code) not in seen:
            seen.add(to_symbol(code))
            targets.append(code)
    targets = targets[:REVIEW_LIMIT]
    # 收盘后补当天 K 线只对 A 股，且只对按代码段推断得出同一交易所的（行情接口按代码段取市场，
    # 000001.SH 上证指数会被取成深市的 000001）；港股读本地同步的日线
    overlay_codes = [c.split(".")[0] for c in targets if not c.endswith(".HK") and to_symbol(c) == to_symbol(c.split(".")[0])]
    overlay = prepare_today_overlay(overlay_codes, now) if overlay_codes else OverlayResult()
    items = []
    try:
        for code in targets:
            view = analyze_view(code)
            hk = code.endswith(".HK")
            stale = bool(trade_date) and not view.error and bool(view.as_of) and view.as_of < trade_date
            if stale:
                status, reason = "stale", f"本地数据截至 {view.as_of}，早于选股日 {trade_date}，待复核"
            else:
                reason = f"无法分析：{view.error}" if view.error else selection_exclusion(
                    view.name, view.sells, view.brick_action, view.red_bricks, view.macd_veto, view.close, view.yellow,
                    white=view.white, green_before=view.green_before, new_low=view.new_low,
                )
                status = "dropped" if reason else "kept"
            items.append({
                "code": code,
                "name": view.name,
                "market": "HK" if hk else "A",
                "status": status,
                "kept": status == "kept",
                "reason": reason,
                "reasons": buy_reasons(view, [source] if source else []) if status == "kept" else [],
                "view": _view_dict(view),
                "notes": HK_NOTES if hk else [],
            })
    finally:
        clear_today_overlay()
    amv = amv_summary()
    if amv is not None:
        amv = {key: value for key, value in amv.items() if key != "series"}
    return {"ok": True, "items": items, "amv": amv, "overlay_date": overlay.trade_date}


def collect_report(
    tdx_dir: Path | None = None,
    watch_file: Path | None = None,
    groups_file: Path | None = None,
    blocks: tuple[str, ...] | None = None,
    include_dividend: bool = True,
    use_quotes: bool = True,
    use_tq: bool = True,
    data_dir: Path | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict:
    from modules.tdx_local_client import TdxLocalClient, business_days_behind, clear_today_overlay
    from modules.tdx_selection import DEFAULT_BOARDS, STALE_DAYS

    now = now or datetime.now()
    today = today or now.date()
    tdx_dir = tdx_dir or default_tdx_dir()
    if tdx_dir is None:
        raise ValueError("没有配置通达信目录：在技能的 .env 里设置 TDX_DIR")
    if watch_file is None and groups_file is None:
        watch_file, groups_file = default_watch_files()
    blocks = configured_blocks() if blocks is None else blocks
    holdings, _ = read_dsh_watchlist(watch_file, groups_file)
    dividend = read_dividend_universe(tdx_dir) if include_dividend else {}
    client = TdxLocalClient(tdx_dir)
    local_date = client.latest_trade_date() or ""

    # 当天行情补齐：本地整体落后时补选股范围内全部个股，否则只补落后的几只和持仓、红利组
    stocks = [s for s in client.get_stock_list() if s["market"] in DEFAULT_BOARDS and not s["is_st"]]
    recent = (datetime.strptime(local_date, "%Y%m%d") - timedelta(days=STALE_DAYS)).strftime("%Y%m%d") if local_date else ""
    active = [s for s in stocks if s["last_date"] >= recent]
    behind_local = business_days_behind(local_date, today) if local_date else 0
    lagging = [s["ts_code"].split(".")[0] for s in active if behind_local >= 1 or s["last_date"] < local_date]
    universe = list(dict.fromkeys(holdings + list(dividend) + lagging))
    overlay = prepare_today_overlay(universe, now) if use_quotes else OverlayResult()
    data_date = max(local_date, overlay.trade_date)

    cache: dict[str, StockView] = {}

    def view_of(code: str) -> StockView:
        if code not in cache:
            cache[code] = analyze_view(code, dividend.get(code, ""))
        return cache[code]

    try:
        holding_views = [view_of(c) for c in holdings]

        # 选股：优先用通达信自己的公式引擎（通达信开着时），否则本地跑移植的公式；
        # 通达信一键选股写的结果文件只用来对照
        selection_picks, engine, engine_note, scanned, errors = run_block_selection(blocks, data_date, use_tq, now)
        sources: dict[str, list[str]] = {}
        block_info = []
        for name in blocks:
            local = selection_picks.get(name)
            block_info.append({
                "name": name,
                "formula": tdx_formula_names().get(name, name),
                "known": local is not None,
                "count": len(local or []),
                "tdx": compare_with_tdx(local or [], read_selection_block(tdx_dir, name, data_date), data_date),
            })
            for code in local or []:
                sources.setdefault(code, []).append(name)
        picks: list[dict] = []
        dropped: list[dict] = []
        for code, names in sources.items():
            v = view_of(code)
            reason = f"无法分析：{v.error}" if v.error else selection_exclusion(
                v.name, v.sells, v.brick_action, v.red_bricks, v.macd_veto, v.close, v.yellow,
                white=v.white, green_before=v.green_before, new_low=v.new_low,
            )
            if reason:
                dropped.append({"code": code, "name": v.name, "sources": names, "reason": reason})
            else:
                picks.append({**_view_dict(v), "sources": names, "reasons": buy_reasons(v, names)})
        picks.sort(key=lambda p: (-len(p["sources"]), -p["b1_streak"], -p["b1_score"]))

        dividend_section = None
        if include_dividend:
            views = [view_of(c) for c in dividend]
            views = [v for v in views if not (v.name and "ST" in v.name.upper())]
            order = {"电力": 0, "银行": 1, "保险": 2}
            b1 = sorted((v for v in views if v.b1_today and not v.error), key=lambda v: (order.get(v.industry, 9), -v.b1_streak, v.code))
            dividend_section = {
                "total": len(views),
                "failed": sum(1 for v in views if v.error),
                "b1": [_view_dict(v) for v in b1],
            }
        empty_follow_up: dict = {"entries": [], "summary": {}}
        follow_up = follow_up_section(load_history(data_dir, f"{today:%Y-%m-%d}")) if data_dir else empty_follow_up
    finally:
        clear_today_overlay()

    amv = amv_summary()
    as_of = sorted({v.as_of for v in cache.values() if v.as_of})
    report = {
        "date": f"{today:%Y-%m-%d}",
        "generated_at": f"{now:%Y-%m-%d %H:%M}",
        "data_date": data_date,
        "local_date": local_date,
        "behind_days": business_days_behind(data_date, today) if data_date else None,
        "stock_dates": [as_of[0], as_of[-1]] if as_of else [],
        "overlay": {
            "applied": overlay.applied,
            "trade_date": overlay.trade_date,
            "skipped": [{"code": c, "reason": r} for c, r in overlay.skipped],
            "error": overlay.error,
            "sources": overlay.sources,
        },
        "amv": amv,
        "amv_behind": bool(amv and data_date and amv["date"].replace("-", "") < data_date),
        "holdings": [_view_dict(v) for v in holding_views],
        "blocks": block_info,
        "selection": {
            "engine": engine,
            "note": engine_note,
            "scanned": scanned,
            "boards": list(DEFAULT_BOARDS),
            "errors": errors,
        },
        "picks": picks,
        "dropped": dropped,
        "dividend": dividend_section,
        "follow_up": follow_up,
        "rules": {
            "exclude": (f"近{SELL_LOOKBACK}日 S1/S2/S3、近{SELL_LOOKBACK}日红砖翻绿且不在白线之上（绿砖数满4块翻红且不创新低的除外）、"
                        "今日红砖翻绿、第4块红砖及以后、MACD一票否决、跌破黄线、ST"),
            "add": "持仓加仓只看 B1：今日有 B1 类买点即可加仓，连续几天一并标出",
            "amv": "活跃市值只作背景，不否决选股和加仓",
        },
    }
    return report


def write_report(out_dir: Path, sync_dsh: bool = True, amv_refresh: dict | None = None, **kwargs) -> Path:
    """生成当天报告：data/YYYY-MM-DD.json + YYYY-MM-DD.html，并刷新 index.html 指向最新一天。

    ``sync_dsh``：把入选名单交给工作台（写 report-picks.json，工作台一分钟内同步进「今日选股」分组）。
    """
    from modules.daily_report_html import render_index, render_report

    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    report = collect_report(data_dir=data_dir, **kwargs)
    if amv_refresh is not None:
        report["amv_refresh"] = amv_refresh
    if sync_dsh:
        from modules.dsh_watchlist_sync import write_picks_file

        report["dsh_sync"] = asdict(write_picks_file(report["picks"], report["date"]))
    (data_dir / f"{report['date']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    days = report_days(data_dir)
    path = out_dir / f"{report['date']}.html"
    path.write_text(render_report(report, days), encoding="utf-8")
    (out_dir / "index.html").write_text(render_index(days), encoding="utf-8")
    return path
