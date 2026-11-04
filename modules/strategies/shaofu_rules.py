"""少妇战法规则的唯一来源。

每条规则都写明依据，``kind`` 区分三类：

- ``知识库``：知识库有明确说法，``source`` 是相对 ``01_markdown/`` 的路径和行号
- ``解读``：知识库有原则但没给可执行的定义，这里取了一个具体读法
- ``工程``：知识库没有涉及，属于回测实现需要的假设

``LoopConfig`` 的默认值从这里取。改规则只改这一个文件，``tests/test_shaofu_rules.py``
会检查默认值、README、SKILL.md 与这里一致，并在本机有知识库时检查出处存在。

知识库是粉丝整理的公开笔记（不随本仓库发布；用环境变量 ``ZK_WIKI_DIR`` 指向你自己的副本），它是笔记和
摘要，不是直播逐字稿。原文互相矛盾时的取舍见 ``knowledge/source-priority.md``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rule:
    """一条规则及其依据。"""

    value: Any
    kind: str
    source: str
    note: str = ""


SHAOFU_RULES: dict[str, Rule] = {
    # 择时
    "market_timing": Rule("amv_state", "知识库", "concepts/活跃市值.md:21-31；concepts/少妇战法.md:22；materials/知行小菜鸟_静水流深/2026-04-09-431挥杆与持仓一场关于交易人性与周期的终极拆解.md:116-120", "单日 ≥+4% 或两日合计 ≥+4% 转多头，单日 ≤-2.3% 转空头且优先，其余延续（通达信本地数据文件说明 6.2）。多头才开仓，空头不强制清仓"),
    # 入场
    "j_threshold": Rule(-10, "知识库", "concepts/少妇战法.md:24,51", "少妇战法 SOP 用 -10 以下；通用 B1 才是 J<13"),
    "entry_fill": Rule("next_open", "知识库", "concepts/B1建仓波.md:95；concepts/Zettaranc 短线交易操作手册.md:105", "B1 出现后第二天开盘建仓"),
    "require_white_above_yellow": Rule(True, "知识库", "concepts/双线战法.md:23；concepts/知行趋势线.md:50", "黄线不足 114 根时无法判断，不入场"),
    "vol_shrink_threshold": Rule(1.0, "知识库", "materials/知行课代表/2025-11-15-策略篇六知行趋势线白黄线的本质与应用.md:242；materials/嗨大了大叔ZGNB/2025-08-12-通达信的使用10.md:42", "公式写的是今日量小于昨日量，原代码的 0.8 没有出处"),
    # 止损
    "stop_loss_method": Rule("entry_low", "知识库", "concepts/少妇战法.md:25；concepts/四不原则.md:106", "买入 K 线最低价向下"),
    "stop_loss_pct": Rule(-0.02, "知识库", "concepts/Zettaranc 短线交易操作手册.md:36,111", "原文区间 1%-3%，流程图写 K 线低点 -2%"),
    "min_holding_days": Rule(1, "知识库", "concepts/B1建仓波.md:51", "跌穿止损直接全卖，没有保护期；T+1 由次日开盘成交保证"),
    # 七层应对的前两层，优先级高于止损
    "no_rise_days": Rule(3, "知识库", "concepts/B1建仓波.md:49", "3 天内必须恢复上涨"),
    "no_rise_grace_days": Rule(2, "解读", "concepts/B1建仓波.md:50", "原文最多再看 1-2 天，取上限：入场后 5 根 K 线收盘从未高于成本就走"),
    "profit_to_loss_exit": Rule(True, "解读", "concepts/七层应对.md:52-55", "收盘曾高于成本、之后收盘跌回成本以下就走，浮盈不设门槛"),
    # 第 4 层：放飞
    "lu_half": Rule(True, "知识库", "concepts/少妇战法.md:26", "BBI 线上两根中大阳线减半"),
    "big_yang_pct": Rule(4.0, "知识库", "concepts/七层应对.md:67；concepts/B2战法.md:24", "中阳线为涨幅 4% 以上"),
    "fourth_brick_half": Rule(True, "知识库", "concepts/七层应对.md:68；concepts/砖形图.md:29", "第四块红砖至少卖一半，只减一次"),
    # 第 5 层：出货信号
    "s1_exit": Rule(True, "知识库", "concepts/七层应对.md:76；concepts/S1信号.md:49", "S1 有三档处理，七层应对写立即卖出，取清仓"),
    "didi_exit": Rule(True, "知识库", "concepts/七层应对.md:77", "按七层应对的定义：高位连续两根阴线，第二根收盘低于第一根最低价。嘀嘀战法页的滚动止损是另一种说法，未实现"),
    # 第 6 层：破位
    "exit_line": Rule("bbi", "知识库", "concepts/少妇战法.md:27", "少妇战法 1.0 用 BBI；双线战法 Pro 版才用白线（concepts/双线战法.md）"),
    "bbi_break_days": Rule(2, "知识库", "concepts/少妇战法.md:27", "连续两日收盘跌破"),
    "bbi_break_threshold": Rule(0.0, "知识库", "concepts/少妇战法.md:27", "原文没有 1% 缓冲"),
    "break_requires_reclaim": Rule(True, "解读", "concepts/少妇战法1.3 每日持股检查手册.md:60", "跌破的前提是曾在线上；B1 多在线下买入，未站上前由止损和没涨就走管"),
    "yellow_break_exit": Rule(True, "知识库", "concepts/七层应对.md:83-84", "跌破黄线可以忍一根，第二根仍收在黄线下必须清仓；跌破的前提同样是站上过"),
    "yellow_break_days": Rule(2, "知识库", "concepts/七层应对.md:84", "忍一根，所以是连续两根"),
    # 第 7 层
    "dead_cross_exit": Rule(True, "知识库", "concepts/七层应对.md:86-89", "最后防线"),
    # 两个 30% 原则
    "two_30_rally_pass": Rule(30.0, "知识库", "concepts/两个30%原则.md:23", "建仓波涨幅一般在 30% 左右"),
    "two_30_rally_warn": Rule(40.0, "知识库", "concepts/两个30%原则.md:24", "40% 以上偏多，疑似一波流"),
    "two_30_rally_reject": Rule(50.0, "知识库", "concepts/两个30%原则.md:25", "50% 以上直接放弃"),
    "two_30_turnover_pass": Rule(30.0, "知识库", "concepts/两个30%原则.md:29", "几根中大阳线的累计换手不超过 30%"),
    "two_30_turnover_reject": Rule(40.0, "知识库", "concepts/两个30%原则.md:30", "高于 40% 视为废 B1"),
    "two_30_window": Rule(30, "知识库", "materials/TANGOO/2026-03-15-新增B1补丁换手限制.md:57", "公式取近 30 天"),
    "two_30_top_n": Rule(3, "知识库", "materials/TANGOO/2026-03-15-新增B1补丁换手限制.md:20,57", "最大的三根阳量"),
    "two_30_rally_window": Rule(30, "解读", "concepts/两个30%原则.md:23", "原文没写怎么量涨幅。取近 30 天最高价，和它之前的最低价相比"),
    # 仓位
    "position_pct": Rule(0.10, "知识库", "concepts/Zettaranc 短线交易操作手册.md:107", "首次试仓不超过总资金 10%"),
    # 成交与成本
    "exit_fill": Rule("next_open", "工程", "日线回测无法在收盘前成交；次日开盘不用未来信息", "原文有 14:55 执行的说法（concepts/嘀嘀战法.md:37），日线数据做不到"),
    "commission_rate": Rule(0.00025, "工程", "常见费率，与模拟器一致", "不计最低 5 元"),
    "stamp_duty_rate": Rule(0.0005, "工程", "财政部 税务总局公告 2023 年第 39 号，卖出单边", ""),
    "transfer_fee_rate": Rule(0.00001, "工程", "与模拟器一致", "买卖双向"),
    "slippage_pct": Rule(0.001, "工程", "假设单边 0.1%", "没有成交明细，无法实测"),
}


def rule_value(key: str) -> Any:
    """取规则的值。"""
    return SHAOFU_RULES[key].value


__all__ = ["Rule", "SHAOFU_RULES", "rule_value"]
