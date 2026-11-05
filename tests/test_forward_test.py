"""前向检验（纯函数 + 合成数据，不读真实通达信目录）。"""

import json

import numpy as np

from modules.forward_test import MIN_DAYS, forward_return, forward_test, load_reports, mean_ci, verdict

DAYS = [20261102, 20261103, 20261104, 20261105, 20261106, 20261109, 20261110, 20261111, 20261112, 20261113, 20261116, 20261117]


def series(*closes: float):
    return np.array(DAYS[: len(closes)], dtype=np.int64), np.array(closes, dtype=float)


def test_forward_return_needs_the_day_and_enough_bars():
    s = series(10, 10, 11, 12, 13, 14, 15)
    assert forward_return(s, "20261102", 5) == 40.0
    assert forward_return(s, "20261103", 5) == 50.0
    assert forward_return(s, "20261104", 5) is None  # 还没走到第 5 天
    assert forward_return(s, "20261101", 5) is None  # 当天没有行情
    assert forward_return(None, "20261102", 5) is None


def test_mean_ci_and_verdict():
    assert mean_ci([]) == (None, None, None)
    assert mean_ci([1.0]) == (1.0, None, None)
    mean, low, high = mean_ci([1.0, 2.0, 3.0])
    assert mean == 2.0 and low < 2.0 < high
    few = {"days": 3, "excess_ci": [0.1, 0.2]}
    assert verdict(few).startswith("样本不足")
    assert verdict({"days": MIN_DAYS, "excess_ci": [0.1, 0.5]}).startswith("目前跑赢")
    assert verdict({"days": MIN_DAYS, "excess_ci": [-0.5, -0.1]}).startswith("目前跑输")
    assert verdict({"days": MIN_DAYS, "excess_ci": [-0.1, 0.5]}).startswith("和同日随机没有显著差别")


def _report(data_dir, name, data_date, picks, dropped=()):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{name}.json").write_text(json.dumps({
        "date": name, "data_date": data_date,
        "picks": [{"code": c, "name": c, "sources": ["我的B1"]} for c in picks],
        "dropped": [{"code": c, "name": c, "reason": "x"} for c in dropped],
    }), encoding="utf-8")


def test_load_reports_keeps_one_per_data_date(tmp_path):
    _report(tmp_path, "2026-11-02", "20261102", ["A"])
    _report(tmp_path, "2026-11-03", "20261102", ["B"])  # 休市日多出的一份，以后生成的为准
    reports = load_reports(tmp_path)
    assert [r["data_date"] for r in reports] == ["20261102"] and reports[0]["picks"][0]["code"] == "B"


def test_forward_test_compares_picks_with_same_day_universe(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.forward_test.MIN_BARS", 1)
    data = tmp_path / "data"
    _report(data, "2026-11-02", "20261102", ["A"], dropped=["C"])
    _report(data, "2026-11-03", "20261103", ["B"])
    rising = series(10, 10, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12)  # 11-02、11-03 入选，5 日后都是 +20%
    flat = series(*[10.0] * 12)
    falling = series(10, 10, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9)
    by_code = {"A": rising, "B": rising, "C": falling, "U1": flat, "U2": rising}
    out = forward_test(data, by_code.get, lambda: ["U1", "U2", "A", "B", "C"], flat, "20261117")
    five = out["summary"]["5"]
    assert five["picks"]["count"] == 2 and five["picks"]["avg"] == 20.0
    assert five["random_avg"] is not None and five["picks"]["excess_vs_random"] > 0
    assert five["dropped"]["avg"] == -10.0
    assert five["verdict"].startswith("样本不足")
    assert out["by_source"]["我的B1"]["5"]["count"] == 2
    assert out["days"][0]["date"] == "20261103"  # 最新在前
    # 随机基准按日期缓存
    assert (data / "forward" / "universe-20261102.json").exists()
    again = forward_test(data, by_code.get, lambda: (_ for _ in ()).throw(AssertionError("应读缓存")), flat, "20261117")
    assert again["summary"]["5"]["random_avg"] == five["random_avg"]


def test_forward_return_uses_the_market_calendar_for_suspended_stocks():
    calendar = np.array(DAYS, dtype=np.int64)
    halted = (np.array([20261102, 20261103, 20261116], dtype=np.int64), np.array([10.0, 11.0, 15.0]))
    assert forward_return(halted, "20261102", 5, calendar) == 10.0  # 第 5 个交易日 11-09 停牌：取 11-03 收盘
    assert forward_return(halted, "20261102", 10, calendar) == 50.0  # 11-16 复牌


def test_group_averages_are_day_weighted_like_the_random_baseline():
    from modules.forward_test import group_stats

    days = [
        {"universe": {"5": 0.0}, "picks": [{"returns": {"5": 10.0}}]},
        {"universe": {"5": 0.0}, "picks": [{"returns": {"5": 0.0}}] * 9},
    ]
    stats = group_stats(days, "picks", 5)
    assert stats["avg"] == 5.0 and stats["excess_vs_random"] == 5.0 and stats["count"] == 10


def test_universe_cache_is_final_only_after_the_settle_window(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.forward_test.MIN_BARS", 1)
    data = tmp_path / "data"
    _report(data, "2026-11-02", "20261102", ["A"])
    flat = series(*[10.0] * 12)
    forward_test(data, {"A": flat, "U": flat}.get, lambda: ["U"], flat, "20261117")
    cached = json.loads((data / "forward" / "universe-20261102.json").read_text(encoding="utf-8"))
    assert cached["complete"] is False  # 10 日结束日 11-16 之后还没过 5 个交易日
