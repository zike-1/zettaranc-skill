"""通达信函数移植的口径：手算的小例子。这里锁住函数语义，防止以后改坏；
用户自己的公式不在仓库里，只测加载机制和内置示例。"""

import numpy as np
import pytest

from modules.tdx_formulas import (
    BARSLAST,
    COUNT,
    CROSS,
    EMA,
    EVERY,
    HHV,
    HHVBARS,
    LLV,
    MA,
    REF,
    SMA,
    VALUEWHEN,
    Bars,
    example_j_negative_above_bbi,
    load_formulas,
)

NAN = np.nan


def test_sma_and_ema_use_tdx_recursion_from_first_value():
    x = [10.0, 20.0, 30.0]
    # SMA(X,3,1): Y=(X+2Y')/3
    assert SMA(x, 3, 1).tolist() == pytest.approx([10.0, 40 / 3, (30 + 2 * 40 / 3) / 3])
    # EMA(X,3): Y=(2X+2Y')/4
    assert EMA(x, 3).tolist() == pytest.approx([10.0, 15.0, 22.5])
    # 前面的无效值跳过，从第一个有效值起算
    assert np.isnan(SMA([NAN, 6.0, 9.0], 3, 1)[0]) and SMA([NAN, 6.0, 9.0], 3, 1)[2] == pytest.approx(7.0)


def test_ma_needs_full_window_but_hhv_llv_count_use_what_exists():
    x = [1.0, 3.0, 2.0, 5.0]
    assert np.isnan(MA(x, 3)[1]) and MA(x, 3)[2:].tolist() == pytest.approx([2.0, 10 / 3])
    assert HHV(x, 3).tolist() == [1.0, 3.0, 3.0, 5.0]
    assert LLV(x, 2).tolist() == [1.0, 1.0, 2.0, 2.0]
    assert COUNT(np.array(x) > 1.5, 3).tolist() == [0, 1, 2, 3]
    assert EVERY(np.array(x) > 0.5, 3).tolist() == [False, False, True, True]


def test_ref_with_constant_and_per_bar_offsets():
    x = [1.0, 2.0, 3.0, 4.0]
    assert REF(x, 1)[1:].tolist() == [1.0, 2.0, 3.0] and np.isnan(REF(x, 1)[0])
    assert REF(x, np.array([0, 1, 2, 5])).tolist()[:3] == [1.0, 1.0, 1.0] and np.isnan(REF(x, np.array([0, 1, 2, 5]))[3])


def test_cross_barslast_valuewhen_hhvbars():
    a = np.array([1.0, 3.0, 2.0, 4.0])
    b = np.array([2.0, 2.0, 2.5, 3.0])
    assert CROSS(a, b).tolist() == [False, True, False, True]
    assert np.isnan(BARSLAST([False, True, False, False])[0])
    assert BARSLAST([False, True, False, False])[1:].tolist() == [0, 1, 2]
    assert VALUEWHEN([False, True, False, True], [5.0, 6.0, 7.0, 8.0])[1:].tolist() == [6.0, 6.0, 8.0]
    # 最高的那根距今几根；并列取最近的
    assert HHVBARS([1.0, 5.0, 2.0, 5.0, 3.0], 5).tolist() == [0, 0, 1, 0, 1]


def _bars(n=300, seed=1, code="600000"):
    rng = np.random.default_rng(seed)
    close = 10 * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    vol = rng.uniform(1e5, 5e5, n)
    return Bars(code=code, O=open_, H=high, L=low, C=close, V=vol,
                float_mv=close * 1e9, turnover=np.full(n, 2.0))


def test_example_formula_returns_one_bool_per_bar():
    """内置示例：J 值为负且收盘在 BBI 之上；MA(C,24) 之前不可能选出"""
    b = _bars()
    out = example_j_negative_above_bbi(b)
    assert out.dtype == bool and len(out) == len(b.C)
    assert not out[:23].any()


def test_load_formulas_merges_user_file(tmp_path, monkeypatch):
    """用户公式文件里的 FORMULAS 并进来；文件不存在时只有示例；没有 FORMULAS 报错"""
    user = tmp_path / "my_formulas.py"
    user.write_text(
        "from modules.tdx_formulas import MA\n"
        "def above_ma20(b):\n    return b.C > MA(b.C, 20)\n"
        "FORMULAS = {'站上20日线': above_ma20}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZT_USER_FORMULAS", str(user))
    formulas = load_formulas()
    assert set(formulas) == {"示例·J负站上BBI", "站上20日线"}
    assert formulas["站上20日线"](_bars()).dtype == bool

    monkeypatch.setenv("ZT_USER_FORMULAS", str(tmp_path / "missing.py"))
    assert set(load_formulas()) == {"示例·J负站上BBI"}

    bad = tmp_path / "bad.py"
    bad.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setenv("ZT_USER_FORMULAS", str(bad))
    with pytest.raises(ImportError):
        load_formulas()
