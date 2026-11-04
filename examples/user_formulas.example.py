"""你自己的通达信选股公式：本地 Python 版本（示例模板）。

用法：
1. 复制本文件到技能目录下的 ``data/user_formulas.py``（data/ 不进仓库），或放到任意位置并在
   ``.env`` 里设 ``ZT_USER_FORMULAS=<文件路径>``；
2. 照着你在通达信里的公式代码逐行翻译（函数名与通达信一致，变量名可以保留中文）；
3. 在 ``FORMULAS`` 里登记「选股名 → 函数」，每日报告用 ``.env`` 的 ``ZT_REPORT_BLOCKS`` 选要跑哪几个。

什么时候用到这里：通达信开着、且在收盘后跑当天报告时，报告直接调用通达信自己的公式引擎
（按 ``ZT_TDX_FORMULA_MAP`` 映射到通达信里的公式名），结果和一键选股一致；只有通达信没开、
或补算历史日期时，才用这里的 Python 版本。所以两边的公式应当是同一个。

写完先和通达信核对：挑一个交易日，用 ``python -m modules.cli tq-select --formula <通达信公式名>
--date YYYYMMDD`` 取通达信的结果，再用本地版本跑同一天，逐只对比。

下面是一个**示例公式**（不是任何人的交易方法，只演示写法）。通达信写法：

    RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100;
    K:=SMA(RSV,3,1); D:=SMA(K,3,1); J:=3*K-2*D;
    选股: J<0 AND C>MA(C,20) AND V<MA(V,5);
"""

from __future__ import annotations

import numpy as np

from modules.tdx_formulas import HHV, IF, LLV, MA, SMA, Bars, _div  # noqa: F401 — 按需引入通达信函数


def j_negative_above_ma20_shrinking(b: Bars) -> np.ndarray:
    C, H, L, V = b.C, b.H, b.L, b.V
    RSV = IF(HHV(H, 9) == LLV(L, 9), 50, _div(C - LLV(L, 9), HHV(H, 9) - LLV(L, 9)) * 100)
    K = SMA(RSV, 3, 1)
    D = SMA(K, 3, 1)
    J = 3 * K - 2 * D
    return (J < 0) & (C > MA(C, 20)) & (V < MA(V, 5))


# 常用写法提示：
# - 通达信 A AND B → (A) & (B)；OR → |；NOT(X) → NOT(X) 或 ~X
# - 带周期参数的 HHV/LLV/MA/COUNT/EVERY/EXIST/REF 直接对应同名函数
# - 除法用 _div(a, b)：除以 0 得到无效值，和通达信一致
# - 流通市值 FINANCE(40) → b.float_mv（元）；换手率 DYNAINFO(37) → b.turnover / 100（小数）
# - CODELIKE('30') → b.codelike('30')

FORMULAS = {
    "示例·J负站上20日线缩量": j_negative_above_ma20_shrinking,
}
