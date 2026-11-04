"""指南针活跃市值读取与多空状态。用构造的文件测试，不依赖本机有没有指南针。"""

from __future__ import annotations

import struct

import pytest

from modules.active_market_gate import AmvStateGate, GateAction, amv_state_gate
from modules.active_market_value import (
    ActiveMarketValuePoint,
    amv_entry_filter,
    clear_cache,
    current_market_state,
    load_active_market_value,
    market_states,
    next_day_triggers,
)
from modules.compass_vdat import AMV_CODE, configured_data_dir, read_amv_rows, read_series
from modules.core.errors import ZettarancError


def _record(date, close, high=None, low=None):
    h = high if high is not None else close + 1
    lo = low if low is not None else close - 1
    return struct.pack("<I6f", date, close, h, lo, close, 1000.0, 2000.0)


def _block(code, records, pad_to=250):
    name = code.encode().ljust(32, b"\x00")
    body = b"".join(records) + b"\x00" * (28 * (pad_to - len(records)))
    return name + body + b"\xaa" * 16


@pytest.fixture
def vdat(tmp_path, monkeypatch):
    root = tmp_path / "ChinaStk"
    (root / "Z_SK").mkdir(parents=True)
    index_entry = AMV_CODE.encode().ljust(32, b"\x00") + struct.pack("<II", 20261030, 1) + b"\x00" * 40
    other = _block("Z_SK0XXX", [_record(20260101, 5.0)])
    first = _block(AMV_CODE, [_record(20260102 + i, 100.0 + i) for i in range(3)])
    second = _block(AMV_CODE, [_record(20260105, 110.0), _record(20260106, 111.0)])
    path = root / "Z_SK" / "day.vdat"
    path.write_bytes(b"\x00" * 60 + index_entry + other + first + second)
    monkeypatch.setenv("ZHINANZHEN_DATA_DIR", str(root))
    clear_cache()
    yield path
    clear_cache()


def test_reads_only_requested_code_and_skips_index(vdat):
    rows = read_series(vdat)
    assert [r["date"] for r in rows] == ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"]
    assert rows[0]["close"] == 100.0
    assert rows[-1]["close"] == 111.0
    assert rows[0]["volume"] == 1000.0


def test_stops_at_invalid_record(tmp_path):
    bad = _record(20260105, 100.0, high=90.0, low=95.0)  # 最高价低于最低价
    path = tmp_path / "day.vdat"
    path.write_bytes(_block(AMV_CODE, [_record(20260102, 100.0), bad, _record(20260106, 100.0)]))
    assert [r["date"] for r in read_series(path)] == ["2026-01-02"]


def test_unparseable_file_raises(tmp_path):
    path = tmp_path / "day.vdat"
    path.write_bytes(b"\x00" * 1000)
    with pytest.raises(ZettarancError):
        read_series(path)


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHINANZHEN_DATA_DIR", "")
    assert configured_data_dir() is None
    assert read_amv_rows() is None
    monkeypatch.setenv("ZHINANZHEN_DATA_DIR", str(tmp_path))
    assert configured_data_dir() is None


def test_market_value_module_reads_local_file(vdat):
    points = load_active_market_value()
    assert points[-1].date == "2026-01-06"
    assert points[-1].pct_chg == pytest.approx((111 / 110 - 1) * 100, abs=1e-3)


def _points(pcts):
    out = []
    close = 100.0
    out.append(ActiveMarketValuePoint("2026-01-01", close, close, close, close, 0, 0))
    for i, pct in enumerate(pcts):
        close *= 1 + pct / 100
        out.append(ActiveMarketValuePoint(f"2026-01-{i + 2:02d}", close, close, close, close, 0, 0))
    return out


def test_single_day_up_turns_bull_and_persists():
    states = market_states(_points([1.0, 4.0, 0.5, -1.0]))
    assert [s.state for s in states] == ["UNKNOWN", "UNKNOWN", "BULL", "BULL", "BULL"]
    assert states[-1].since == "2026-01-03"


def test_two_day_arithmetic_sum_turns_bull():
    """两日相加（2.0+2.0=4.0）算术口径，复合口径是 4.04，这里取边界用相加"""
    assert market_states(_points([2.0, 2.0]))[-1].state == "BULL"
    assert market_states(_points([2.0, 1.9]))[-1].state == "UNKNOWN"


def test_bear_has_priority_over_two_day_bull():
    """当日 -2.3% 时，即使两日相加 ≥4% 也是空头"""
    states = market_states(_points([7.0, -2.5]))
    assert states[-1].state == "BEAR"


def test_next_day_bull_trigger_matches_state_machine():
    """空头里今天 +3.94%，明天门槛是 +0.06%，而不是用两日合计倒推的数"""
    today = market_states(_points([-2.5, -2.17, 3.94]))[-1]
    assert today.state == "BEAR"
    triggers = next_day_triggers(today)
    assert triggers["next_day_bull_at"] == pytest.approx(0.06, abs=1e-3)
    assert triggers["next_day_bear_at"] is None
    assert market_states(_points([-2.5, -2.17, 3.94, 0.07]))[-1].state == "BULL"
    assert market_states(_points([-2.5, -2.17, 3.94, 0.05]))[-1].state == "BEAR"


def test_next_day_bull_trigger_after_down_day_is_single_day_line():
    assert next_day_triggers(market_states(_points([-2.5, -1.0]))[-1])["next_day_bull_at"] == pytest.approx(4.0)


def test_next_day_triggers_in_bull_only_watch_bear_line():
    triggers = next_day_triggers(market_states(_points([4.5, 1.0]))[-1])
    assert triggers == {"next_day_bull_at": None, "next_day_bear_at": -2.3}


def test_bear_threshold_is_inclusive():
    assert market_states(_points([-2.3]))[-1].state == "BEAR"
    assert market_states(_points([-2.2]))[-1].state == "UNKNOWN"


def test_entry_filter_and_gate(vdat):
    allowed = amv_entry_filter()
    assert set(allowed) == {"20260102", "20260103", "20260104", "20260105", "20260106"}
    assert allowed["20260105"] is True  # 102 → 110，单日超过 +4%
    assert allowed["20260103"] is False  # 之前没有转多信号
    gate = amv_state_gate()
    assert gate.evaluate("20260105") is GateAction.OPEN
    assert gate.evaluate("2026-01-05") is GateAction.OPEN
    assert gate.evaluate("20300101") is GateAction.WAIT


def test_gate_is_fail_closed():
    gate = AmvStateGate({"20260101": True, "20260102": False})
    assert gate.evaluate("20260101") is GateAction.OPEN
    assert gate.evaluate("20260102") is GateAction.WAIT
    assert gate.evaluate(None) is GateAction.WAIT


def test_no_data_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHINANZHEN_DATA_DIR", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    clear_cache()
    assert current_market_state() is None
    assert amv_entry_filter() is None
    assert amv_state_gate() is None
