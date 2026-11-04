"""每日选股报告的规则与文件解析（纯函数，不读真实通达信目录）。"""

import json
import struct
from dataclasses import dataclass
from datetime import date

import pytest

import modules.daily_selection_report as dsr
from modules.daily_selection_report import (
    add_position_verdict,
    b1_streak,
    boll_position,
    flyaway_verdict,
    follow_up_returns,
    follow_up_section,
    is_true_b1,
    latest_session_date,
    parse_blk,
    parse_block_cfg,
    parse_block_dat,
    parse_industry_cfg,
    quote_to_bar,
    read_dsh_watchlist,
    read_selection_block,
    selection_exclusion,
    sell_label,
    skip_reason,
    weekly_closes,
)
from modules.dsh_watchlist_sync import picks_payload, write_picks_file


def _cfg_record(name: str, code: str) -> bytes:
    return name.encode("gbk").ljust(50, b"\0") + code.encode("gbk").ljust(70, b"\0")


def test_block_cfg_maps_chinese_block_names_to_file_codes():
    """blocknew.cfg 每条 120 字节：板块名（GBK）→ 文件代码"""
    raw = _cfg_record("B1选股", "B1XG") + _cfg_record("金砖共振", "JZGZ")
    assert parse_block_cfg(raw) == {"B1选股": "B1XG", "金砖共振": "JZGZ"}


def test_blk_lines_become_six_digit_codes_without_market_prefix():
    """板块文件每行 市场位+代码，空行和重复行去掉"""
    text = "\r\n1600557\r\n0002623\r\n1600557\r\n2920985\r\nbad\r\n"
    assert parse_blk(text) == ["600557", "002623", "920985"]


def test_industry_cfg_keeps_only_dividend_industries():
    """电力 T0102 下的子行业也算电力；其它行业不收"""
    text = "1|600795|T010202|||X620102\n1|601318|T1003|||X5102\n0|000001|T1001|||X500102\n0|000002|T110201|||X530101\n"
    result = parse_industry_cfg(text, {"T0102": "电力", "T1001": "银行", "T1003": "保险"})
    assert result == {"600795": "电力", "601318": "保险", "000001": "银行"}


def test_dsh_watchlist_splits_holdings_group_from_other_a_shares(tmp_path):
    """持仓分组单列；港股、加密货币、ETF 以外的非 6 位代码不收"""
    watch = tmp_path / "watchlists.json"
    groups = tmp_path / "watchlist-groups.json"
    watch.write_text(json.dumps({
        "cn": [
            {"symbol": "600036.SH"},
            {"symbol": "600795.SH", "groups": ["g1"]},
        ],
        "crypto": [{"symbol": "588170"}],
        "hk": [{"symbol": "00700.HK", "groups": ["g1"]}],
    }), encoding="utf-8")
    groups.write_text(json.dumps({"groups": [{"id": "g1", "name": "持仓"}]}), encoding="utf-8")
    assert read_dsh_watchlist(watch, groups) == (["600795"], ["600036"])


def test_boll_position_labels_close_against_bands():
    """收盘在上轨外、轨内、下轨外分别给出位置"""
    flat = [10.0, 10.2] * 10
    assert boll_position(flat + [11.0]) == "上轨之上"
    assert boll_position(flat + [8.0]) == "下轨之下"
    assert boll_position(flat[:-1] + [10.15]).startswith("中轨~上轨")
    assert boll_position([10.0] * 5) == "数据不足"


def test_boll_marks_squeeze_when_band_is_narrowest_in_window():
    """带宽收窄到近 20 期最窄时标收口"""
    widening = [10 + (i % 2) * (2.0 - i * 0.05) for i in range(40)]
    assert boll_position(widening).endswith("·收口")


def test_weekly_closes_take_last_close_of_each_iso_week():
    """周线收盘取每周最后一根，当周未完也计入"""
    rows = [
        {"trade_date": "20261026", "close": 1.0},
        {"trade_date": "20261030", "close": 2.0},
        {"trade_date": "20261102", "close": 3.0},
        {"trade_date": "20261103", "close": 4.0},
    ]
    assert weekly_closes(rows) == [2.0, 4.0]


def test_flyaway_on_fourth_red_brick_or_yang_above_bbi():
    """第四块红砖必放飞；BBI 上中阳/大阳放飞；BBI 下的阳线不放飞"""
    assert flyaway_verdict(4, "RED", 1.0, 10, 9).startswith("第四块红砖")
    assert flyaway_verdict(2, "RED", 7.5, 10, 9) == "BBI上大阳，放飞"
    assert flyaway_verdict(2, "RED", 4.2, 10, 9) == "BBI上中阳，分批放飞"
    assert flyaway_verdict(2, "RED", 7.5, 8, 9) == ""
    assert flyaway_verdict(5, "RED", 1.0, 10, 9) == ""


def test_add_position_is_b1_only_and_shows_streak():
    """加仓只看 B1，连续几天写出来；活跃市值不参与"""
    assert add_position_verdict(0) == ""
    assert add_position_verdict(1) == "今日B1，可加仓"
    assert add_position_verdict(3) == "连续3天B1，可加仓"


def test_b1_streak_counts_back_from_latest_day():
    days = ["20261028", "20261029", "20261030", "20261102", "20261103"]
    assert b1_streak({"20261102", "20261103", "20261028"}, days) == 2
    assert b1_streak({"20261102"}, days) == 0
    assert b1_streak(set(days), days) == 5


def test_only_j_value_b1_and_super_b1_count_as_b1():
    """技能里 MACD 死叉多、关键K阳线等也用 B1 类型，不算 B1 标准"""
    assert is_true_b1("B1", "B1买点 J=-10.11 处于主力吸筹期(高安全)")
    assert is_true_b1("SB1", "超级B1 J=-5.68 放量跌后缩量企稳")
    assert not is_true_b1("B1", "关键K：关键阳 - 底部/回调阳")
    assert not is_true_b1("B1", "MACD 死叉多：眼看死叉未成，白线拐头向上，空中加油")


def test_sell_label_names_the_s1_pattern():
    assert sell_label("S1", "阶梯放量下跌：连续3天量增价跌，主力出货") == "阶梯放量下跌"
    assert sell_label("S1", "出货五式：高位放量滞涨 - 说明") == "出货五式·高位放量滞涨"
    assert sell_label("S1", "MACD 金叉空：眼看金叉未成") == "MACD金叉空"
    assert sell_label("S3", "MDC 细节") == "S3"
    assert sell_label("四块砖翻绿", "") == "四块砖翻绿"


def test_block_dat_records_give_code_date_and_close():
    """.dat 每只 80 字节：代码、入选日、入选日收盘价"""
    def record(market, code, day, price):
        raw = bytearray(80)
        raw[0] = market
        raw[2:8] = code.encode("ascii")
        raw[33:37] = struct.pack("<I", day)
        raw[37:41] = struct.pack("<f", price)
        return bytes(raw)

    raw = record(1, "600558", 20261103, 6.24) + record(0, "002696", 20261103, 12.36)
    assert parse_block_dat(raw) == [("600558", "20261103", 6.24), ("002696", "20261103", 12.36)]


def test_tdx_result_prefers_dat_records_of_the_trade_day(tmp_path):
    """通达信结果以 .dat 当天记录为准（金砖共振 .blk 为空时 .dat 才是用户看到的结果），没有 .dat 退回 .blk"""
    def record(code, day):
        raw = bytearray(80)
        raw[2:8] = code.encode("ascii")
        raw[33:37] = struct.pack("<I", day)
        return bytes(raw)

    blocknew = tmp_path / "T0002" / "blocknew"
    blocknew.mkdir(parents=True)
    (blocknew / "blocknew.cfg").write_bytes(_cfg_record("金砖共振", "JZGZ") + _cfg_record("确认战法", "QRZF"))
    (blocknew / "JZGZ.blk").write_bytes(b"")
    (blocknew / "JZGZ.dat").write_bytes(record("600558", 20261103) + record("600001", 20261102))
    (blocknew / "QRZF.blk").write_bytes(b"\r\n0002181\r\n")
    assert read_selection_block(tmp_path, "金砖共振", "20261103").codes == ["600558"]
    assert read_selection_block(tmp_path, "确认战法", "20261103").codes == ["002181"]
    assert read_selection_block(tmp_path, "B1选股", "20261103").missing


def test_quote_bar_needs_newer_date_and_matching_previous_close():
    quote = {"quote_time": "20261103150003", "price": 10.8, "last_close": 10.4, "open": 10.5, "high": 10.9,
             "low": 10.3, "volume_lots": 2000, "amount_wan": 216.0}
    bar, reason = quote_to_bar(quote, 10.4, "20261102")
    assert reason == "" and bar["trade_date"] == "20261103"
    assert bar["volume_shares"] == 200_000 and bar["amount_yuan"] == 2_160_000
    assert quote_to_bar(quote, 10.4, "20261103") == (None, "")  # 本地已有当天
    assert quote_to_bar(quote, 10.0, "20261102")[1].startswith("昨收")  # 可能今天除权
    assert quote_to_bar({**quote, "volume_lots": 0}, 10.4, "20261102")[1] == "停牌或无成交"


def test_follow_up_returns_count_trading_days_after_pick():
    rows = [{"trade_date": f"2026110{i}", "close": c} for i, c in enumerate([10, 11, 12, 9, 10, 10.5, 13], start=1)]
    assert follow_up_returns(rows, "20261102", (1, 5, 6)) == {1: 9.09, 5: 18.18, 6: None}
    assert follow_up_returns(rows, "20261231", (5,)) == {5: None}


def test_picks_file_uses_exchange_suffix_and_dedupes(tmp_path):
    """交给工作台的名单：代码带交易所后缀、去重；工作台目录不存在时报原因"""
    picks = [{"code": "002541", "name": "鸿路钢构"}, {"code": "588200", "name": "ETF"}, {"code": "002541", "name": "x"}]
    assert picks_payload(picks, "2026-11-03") == {
        "date": "2026-11-03", "group": "今日选股",
        "picks": [{"symbol": "002541.SZ", "name": "鸿路钢构"}, {"symbol": "588200.SH", "name": "ETF"}],
    }
    target = tmp_path / "report-picks.json"
    assert write_picks_file(picks, "2026-11-03", target).ok
    assert json.loads(target.read_text(encoding="utf-8"))["picks"][0]["symbol"] == "002541.SZ"
    missing = write_picks_file(picks, "2026-11-03", tmp_path / "nope" / "report-picks.json")
    assert not missing.ok and "不存在" in missing.error


def test_selection_drops_st_sell_signals_flip_green_and_broken_yellow_line():
    """选股过滤：保留时返回空串，剔除时给出原因"""
    keep = dict(name="国电电力", recent_sells=[], brick_action="禁止抄底", red_bricks=0, macd_veto=False, close=10.0, yellow=9.0)
    assert selection_exclusion(**keep) == ""
    assert selection_exclusion(**{**keep, "name": "*ST某某"}) == "ST"
    assert selection_exclusion(**{**keep, "recent_sells": ["S1"]}) == "近5日S1"
    assert selection_exclusion(**{**keep, "brick_action": "止损"}) == "红砖翻绿"
    assert selection_exclusion(**{**keep, "red_bricks": 5}) == "已第5块红砖"
    assert selection_exclusion(**{**keep, "macd_veto": True}) == "MACD一票否决"
    assert selection_exclusion(**{**keep, "close": 8.5}) == "跌破黄线"


def test_latest_session_date_takes_newest_probe_quote():
    quotes = {
        "600519": {"quote_time": "20261231150003"},
        "601318": {"quote_time": "20270104150002"},
        "600036": {"quote_time": ""},
    }
    assert latest_session_date(lambda codes: quotes) == "20270104"


def test_latest_session_date_is_unknown_when_quotes_fail():
    def unreachable(codes):
        raise OSError("timed out")

    assert latest_session_date(unreachable) == ""


def _report_json(data_dir, day, data_date, picks=()):
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "data_date": data_date, "picks": [{"code": c, "name": c} for c in picks]}
    (data_dir / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_skip_only_when_no_new_session_and_its_report_exists(tmp_path):
    data = tmp_path / "data"
    _report_json(data, "2026-12-31", "20261231")
    holiday = date(2027, 1, 1)
    assert "2026-12-31" in skip_reason(data, "20261231", holiday)
    assert skip_reason(data, "20261231", date(2026, 12, 31)) == ""  # 今天就是交易日
    assert skip_reason(data, "", holiday) == ""  # 查不到最近交易日
    assert skip_reason(tmp_path / "none", "20261231", holiday) == ""  # 那天 20:00 没开机，还没有报告


def test_follow_up_counts_each_data_date_once(tmp_path, monkeypatch):
    rows = [{"trade_date": f"202612{d:02d}", "close": 10 + d} for d in range(1, 31)]
    monkeypatch.setattr("modules.tdx_local_client.local_kline_rows", lambda code, days=0, basis="return": rows)
    data = tmp_path / "data"
    _report_json(data, "2026-12-10", "20261210", picks=["600000"])
    _report_json(data, "2026-12-11", "20261210", picks=["600000"])  # 休市日多出的一份
    section = follow_up_section(dsr.load_history(data, "2026-12-31"))
    assert [e["code"] for e in section["entries"]] == ["600000"]
    assert section["summary"]["5"]["count"] == 1


def test_report_command_skips_before_opening_zhinanzhen(tmp_path, monkeypatch, capsys):
    from modules.cli import build_parser, cmd_report

    def must_not_run(*args, **kwargs):
        raise AssertionError("跳过时不该打开指南针，也不该生成报告")

    monkeypatch.setattr(dsr, "latest_session_date", lambda: "20261231")
    monkeypatch.setattr(dsr, "skip_reason", lambda data_dir, session, today: "今天休市")
    monkeypatch.setattr(dsr, "write_report", must_not_run)
    monkeypatch.setattr("modules.zhinanzhen_refresh.refresh_amv", must_not_run)
    cmd_report(build_parser().parse_args(["report", "--refresh-amv", "--skip-if-current", "--out", str(tmp_path)]))
    assert "跳过：今天休市" in capsys.readouterr().out


@dataclass
class _FakeRefresh:
    before: str = ""
    after: str = ""
    error: str = ""
    updated: bool = False


@pytest.mark.parametrize("session, expected", [("20261231", "2026-12-31"), ("", None)])
def test_amv_refresh_waits_for_latest_session(tmp_path, monkeypatch, session, expected):
    from modules.cli import build_parser, cmd_report

    targets = []

    def fake_refresh(target):
        targets.append(target)
        return _FakeRefresh()

    monkeypatch.setattr(dsr, "latest_session_date", lambda: session)
    monkeypatch.setattr(dsr, "write_report", lambda out_dir, **kwargs: out_dir / "x.html")
    monkeypatch.setattr("modules.zhinanzhen_refresh.refresh_amv", fake_refresh)
    cmd_report(build_parser().parse_args(["report", "--refresh-amv", "--out", str(tmp_path)]))
    assert targets == [expected or f"{date.today():%Y-%m-%d}"]
