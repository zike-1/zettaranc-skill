"""通达信公式函数库（Python 版），用来在本地跑你自己的通达信选股公式。

本仓库不包含任何第三方或付费课程的公式原文：函数库 + 一个自写示例在这里，你的公式写在自己的
文件里（见下方「公式注册」和 examples/user_formulas.example.py）。照着通达信代码逐行翻译，
变量名可以保留中文原名，方便对照。

通达信函数口径（与官方帮助一致）：
- ``SMA(X,N,M)``：Y = (M*X + (N-M)*Y') / N，从第一个有效值起算；
- ``EMA(X,N)``：Y = (2*X + (N-1)*Y') / (N+1)，从第一个有效值起算；
- ``MA``：不足 N 根为无效；``HHV/LLV/COUNT``：不足 N 根时用已有的；
- 无效值（NaN）参与比较一律为假，与通达信「数据不足不选出」一致；
- ``DYNAINFO(37)``（换手率）在公式里按小数比较：``Bars.turnover`` 是百分数，用时除以 100。

每个公式函数接收整段 K 线（``Bars``），返回每根 K 线是否满足（bool 数组），选股取最后一根。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ───────────────────────── 通达信函数 ─────────────────────────


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def REF(x, n) -> np.ndarray:
    """n 可以是常数，也可以是逐根变化的序列（如 REF(C,VDAY)）。"""
    x = _f(x)
    if np.isscalar(n) or np.ndim(n) == 0:
        n = int(n)
        out = np.full_like(x, np.nan)
        if n == 0:
            return x.copy()
        out[n:] = x[:-n]
        return out
    n = _f(n)
    idx = np.arange(len(x)) - np.nan_to_num(n, nan=-1).astype(np.int64)
    ok = (~np.isnan(n)) & (idx >= 0) & (idx < len(x))
    out = np.full_like(x, np.nan)
    out[ok] = x[idx[ok]]
    return out


def MA(x, n: int) -> np.ndarray:
    x = _f(x)
    out = np.full_like(x, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(np.nan_to_num(x), 0, 0.0))
        out[n - 1 :] = (c[n:] - c[:-n]) / n
        # 窗口里有无效值的结果也无效
        bad = np.convolve(np.isnan(x).astype(int), np.ones(n, dtype=int), "valid") > 0
        out[n - 1 :][bad] = np.nan
    return out


def _recursive(x, alpha_num: float, alpha_den: float) -> np.ndarray:
    x = _f(x)
    out = np.full_like(x, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if not len(valid):
        return out
    start = valid[0]
    y = x[start]
    out[start] = y
    for i in range(start + 1, len(x)):
        v = x[i]
        if not np.isnan(v):
            y = (alpha_num * v + (alpha_den - alpha_num) * y) / alpha_den
        out[i] = y
    return out


def SMA(x, n: int, m: int) -> np.ndarray:
    return _recursive(x, m, n)


def EMA(x, n: int) -> np.ndarray:
    return _recursive(x, 2, n + 1)


def _rolling(x, n: int, fn) -> np.ndarray:
    x = _f(x)
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        window = x[max(0, i - n + 1) : i + 1] if n > 0 else x[: i + 1]
        window = window[~np.isnan(window)]
        if len(window):
            out[i] = fn(window)
    return out


def HHV(x, n: int) -> np.ndarray:
    return _fast_extreme(x, n, np.fmax)


def LLV(x, n: int) -> np.ndarray:
    return _fast_extreme(x, n, np.fmin)


def _fast_extreme(x, n: int, op) -> np.ndarray:
    """滚动最大/最小（不足 n 根用已有的；NaN 忽略）。"""
    x = _f(x)
    if n <= 0:
        return op.accumulate(x)
    out = x.copy()
    for k in range(1, n):
        shifted = np.full_like(x, np.nan)
        shifted[k:] = x[:-k]
        out = op(out, shifted)
    return out


def COUNT(cond, n: int) -> np.ndarray:
    c = np.nan_to_num(_f(cond)).astype(np.int64)
    cs = np.cumsum(np.insert(c, 0, 0))
    idx = np.arange(1, len(c) + 1)
    lo = np.maximum(idx - n, 0) if n > 0 else np.zeros_like(idx)
    return (cs[idx] - cs[lo]).astype(np.float64)


def EVERY(cond, n: int) -> np.ndarray:
    # 不足 n 根时通达信判为假
    out = COUNT(cond, n) == n
    out[: n - 1] = False
    return out


def EXIST(cond, n: int) -> np.ndarray:
    return COUNT(cond, n) > 0


def CROSS(a, b) -> np.ndarray:
    a, b = _f(a), _f(b)
    if np.ndim(b) == 0:
        b = np.full_like(a, float(b))
    return (a > b) & (REF(a, 1) <= REF(b, 1))


def BARSLAST(cond) -> np.ndarray:
    """距上一次条件成立的根数（当根成立为 0）；从未成立为 NaN。"""
    c = np.asarray(cond, dtype=bool)
    out = np.full(len(c), np.nan)
    last = -1
    for i, v in enumerate(c):
        if v:
            last = i
        if last >= 0:
            out[i] = i - last
    return out


def VALUEWHEN(cond, x) -> np.ndarray:
    c = np.asarray(cond, dtype=bool)
    x = _f(x)
    out = np.full_like(x, np.nan)
    cur = np.nan
    for i in range(len(c)):
        if c[i]:
            cur = x[i]
        out[i] = cur
    return out


def HHVBARS(x, n: int) -> np.ndarray:
    """n 根内最高值到当前的根数（并列取最近一根）。"""
    x = _f(x)
    out = np.full_like(x, np.nan)
    for i in range(len(x)):
        start = max(0, i - n + 1)
        window = x[start : i + 1]
        if np.all(np.isnan(window)):
            continue
        rev = np.nan_to_num(window[::-1], nan=-np.inf)
        out[i] = int(np.argmax(rev))
    return out


def IF(cond, a, b) -> np.ndarray:
    return np.where(np.asarray(cond, dtype=bool), a, b)


def MAX(a, b) -> np.ndarray:
    return np.fmax(_f(a), _f(b)) if np.ndim(a) or np.ndim(b) else max(a, b)


def MIN(a, b) -> np.ndarray:
    return np.fmin(_f(a), _f(b))


def ABS(x) -> np.ndarray:
    return np.abs(_f(x))


def NOT(x) -> np.ndarray:
    return ~np.asarray(x, dtype=bool)


def _div(a, b) -> np.ndarray:
    """通达信除以 0 得到无效值。"""
    a, b = _f(a), _f(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b == 0, np.nan, a / np.where(b == 0, 1, b))


# ───────────────────────── 数据 ─────────────────────────


@dataclass
class Bars:
    code: str  # 6 位代码，供 CODELIKE
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray  # 手
    float_mv: np.ndarray  # FINANCE(40) 流通市值，元；未知为 NaN
    turnover: np.ndarray  # 换手率，%（DYNAINFO(37) 是同一个数除以 100）；未知为 NaN

    def codelike(self, *prefixes: str) -> bool:
        return self.code.startswith(prefixes)


# ───────────────────────── 公式注册 ─────────────────────────
#
# 本仓库不包含任何第三方或付费课程的公式原文。你自己的公式写在一个 Python 文件里，
# 定义 FORMULAS = {"选股名": 函数}，用环境变量 ZT_USER_FORMULAS 指向它；不设时读技能目录下的
# data/user_formulas.py（data/ 不进仓库）。写法参考 examples/user_formulas.example.py。
# 通达信开着时每日报告优先用通达信自己的公式引擎（modules.tdx_tq），这里的 Python 版本只在
# 通达信不可用或补算历史日期时用。


def example_j_negative_above_bbi(b: Bars) -> np.ndarray:
    """示例公式（本项目自写，演示函数库用法）：KDJ 的 J 值为负，且收盘在 BBI 之上。"""
    C, H, L = b.C, b.H, b.L
    rsv = IF(HHV(H, 9) == LLV(L, 9), 50, _div(C - LLV(L, 9), HHV(H, 9) - LLV(L, 9)) * 100)
    k = SMA(rsv, 3, 1)
    d = SMA(k, 3, 1)
    j = 3 * k - 2 * d
    bbi = (MA(C, 3) + MA(C, 6) + MA(C, 12) + MA(C, 24)) / 4
    return (j < 0) & (C > bbi)


#: 内置的只有示例；真正用的公式来自用户文件
BUILTIN_FORMULAS = {"示例·J负站上BBI": example_j_negative_above_bbi}


def user_formulas_path() -> Path:
    raw = os.environ.get("ZT_USER_FORMULAS", "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent.parent / "data" / "user_formulas.py"


def load_formulas() -> dict:
    """内置示例 + 用户公式文件里的 FORMULAS。用户文件不存在时只有示例；加载出错时报错，不静默。"""
    formulas = dict(BUILTIN_FORMULAS)
    path = user_formulas_path()
    if not path.is_file():
        return formulas
    spec = importlib.util.spec_from_file_location("zt_user_formulas", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载用户公式文件：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    user = getattr(module, "FORMULAS", None)
    if not isinstance(user, dict):
        raise ImportError(f"{path} 里没有 FORMULAS 字典")
    formulas.update(user)
    return formulas
