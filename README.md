# zettaranc（万千）· 思维操作系统

> **散户最难的不是选股，是卖出时管住手。**

前阳光私募冠军基金经理、B站百大UP主的交易纪律，封装成可运行在真实行情上的 AI Skill。
基于 ~200 万字语料蒸馏，60+ 指标，30+ 战法，可回测可模拟、可自改进、可走完整闭环。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.13.1-green)](docs/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![Skill](https://img.shields.io/badge/Skill--Schema--V2-✓-purple)](SKILL.md)

> **关于本 fork**：本仓库是 [lululu811/zettaranc-skill](https://github.com/lululu811/zettaranc-skill) 的 fork（MIT，保留原版权声明），新增本地 A 股数据下的每日选股报告、通达信公式引擎选股、指南针活跃市值自动更新、实时行情补当天 K 线（腾讯 → 同花顺 → 东财）和 DSH 交易终端联动，全部可选，见下文[「本地 A 股数据与每日报告」](#本地-a-股数据与每日报告本-fork-新增)。
> 本技能是基于公开资料和粉丝笔记的**框架视角模拟，不是 zettaranc 本人**，不代表本人观点。本项目仅供学习研究使用，不构成投资建议。据此操作，风险自负。

---

## 30 秒体验（无需 Token）

```bash
# 1. 克隆并安装
git clone https://github.com/lululu811/zettaranc-skill.git && cd zettaranc-skill
pip install -r requirements.txt && pip install -e .

# 2. 零配置模式（自动走免费数据源 a-stock-data，无需 tushare 积分）
echo "DATA_MODE=websearch" > .env

# 3. 立即体验
zt analyze 600519.SH  # 用框架分析茅台，自动获取实时行情
```

> **推荐用本地通达信数据**：在 `.env` 写 `TDX_DIR=D:/new_tdx64`，K 线、股票列表、名称和行业都读本地文件，不走网络，全市场选股十几秒。先在通达信里下载盘后数据。详见下文「接入真实行情」。
>
> **数据源说明**：默认集成 [a-stock-data](https://github.com/simonlin1212/a-stock-data) 免费数据源（腾讯/百度/东财/通达信），零积分即可使用实时行情、K 线、股票信息。v4.2.0 起支持 [同花顺官方金融数据服务](https://fuyao.aicubes.cn/)（hithink-finance），配置 `HITHINK_FINANCE_API_KEY` 后自动作为最优先数据源。详见 [CONFIG_GUIDE.md](docs/CONFIG_GUIDE.md)。

---

## 它能做什么

| 能力 | 说明 | 示例 |
|------|------|------|
| 📊 **股票分析** | 60+ 技术指标 + 30+ 战法自动识别 + 综合评分 | `zt analyze 600487.SH --json` |
| 📈 **策略回测** | 少妇战法六步 / 多策略融合 / 组合回测 / B2 确认 / Walk-forward 寻优 | `zt backtest shaofu 600487.SH` |
| 🧪 **端到端模拟** | T+1 + 涨跌停 + 真实成本 + ATR 仓位 + 战法共振 | `zt simulate 000001.SZ --days 250` |
| 🔍 **智能选股** | 13 种筛股条件 + 战法共振 + 环境权重动态调整 | `zt screen --strategy B1 --limit 20` |
| 🧭 **市场择时** | 大盘择时指标，判断当前是否适合持仓 | `zt market timing --json` |
| 👁️ **观察池监控** | 自选股批量监控 + 主动预警 + 飞书推送 | `zt watchlist scan --json` |
| 🤖 **宿主集成** | 所有命令支持 `--json`，Claude / Cursor 可直接调用 | `zt daily --json` |
| 🧬 **自我改进** | 跟踪池 + 月度复盘 + Darwin 自优化管线 | `zt self-optimize run --target trading` |
| 🌐 **Web 看板**（可选） | FastAPI 后端 + React 19 + ECharts 前端 | `zt-web` + `cd frontend && npm run dev` |

---

## 核心策略：少妇战法

**六步闭环状态机驱动的趋势跟踪系统**，解决散户"不会买、不会卖、拿不住"三大痛点。

### 两条核心趋势线

| 名称 | 公式 | 含义 | 作用 |
|------|------|------|------|
| **白线** | `EMA(EMA(C,10),10)` | 短期趋势，"牵牛绳" | 入场/出场参考线 |
| **黄线** | `(MA14+MA28+MA57+MA114)/4` | 中期趋势，"牛的方向" | 择时过滤 |

> **白线 > 黄线** = 主力牵牛，下跌是洗盘，可以持仓；**白线 < 黄线** = 牛绳断了，反弹是逃命，无条件清仓。

### 六步闭环 SOP

| 步骤 | 内容 | 判定 |
|------|------|------|
| 1 | 择时 | 活跃市值多头区间才开仓（单日 ≥+4% 或两日合计 ≥+4% 转多，单日 ≤-2.3% 转空） |
| 2 | 选股 | N 型上移结构 |
| 3 | 等 B1 | J ≤ -10 + 白线在黄线上 + 缩量 + N 型上移；次日开盘买入 |
| 4 | 设止损 | 买入 K 线最低价向下 2%（原文 1%-3%） |
| 5 | 卤煮减半 | BBI 上两根 4% 以上的阳线 → 减半 |
| 6 | 离场 | 站上 BBI 后连续 2 日收盘跌破 → 清仓 |

每条规则的出处见 [`modules/strategies/shaofu_rules.py`](modules/strategies/shaofu_rules.py)，那里也是默认参数的唯一来源。

### 入场条件

T 日收盘判断，T+1 日开盘买入；开盘涨停不追。

| 条件 | 说明 | 阈值 |
|------|------|------|
| KDJ J 值 | 少妇战法 SOP 要大幅负值（通用 B1 才是 J<13） | ≤ -10 |
| 缩量回调 | 当日量 < 前日量 × 阈值 | < 1.0 |
| N 型上移 | 近期底部不断抬高 | higher lows |
| 白线 > 黄线 | 黄线不足 114 根时不入场 | 趋势过滤 |

### 离场规则

按七层应对的优先级检查，T 日收盘判断，T+1 日开盘卖出；开盘跌停卖不出就顺延。

| 优先级 | 触发条件 | 操作 |
|--------|----------|------|
| 没涨就走 | 入场后 5 根 K 线收盘从未高于成本 | 清仓 |
| 盈转亏必走 | 收盘曾高于成本，又跌回成本以下 | 清仓 |
| 止损 | 收盘价低于止损价，没有保护期 | 清仓 |
| 放飞 | BBI 上两根 4% 以上的阳线；第四块红砖 | 各卖出一半，各发生一次 |
| S1、嘀嘀 | 高位放量大阴；高位连续两根阴线下台阶 | 清仓 |
| 破位 | 站上过 BBI 后，连续 2 日收盘跌破 | 清仓 |
| 破黄线 | 站上过黄线后，连续 2 根收在黄线下（忍一根） | 清仓 |
| 死叉 | 白线死叉黄线 | 清仓 |

每一层都能在 `LoopConfig` 里单独关掉。

### 默认参数

```python
LoopConfig(
    j_threshold = -10,           # B1 J 值阈值
    stop_loss_pct = -0.02,       # 买入 K 线最低价向下 2%
    exit_line = "bbi",           # 双线版用 "white"
    bbi_break_days = 2,          # 连续跌破天数
    bbi_break_threshold = 0.0,   # 没有缓冲
    no_rise_days = 3,            # 没涨就走：观察 3 天
    no_rise_grace_days = 2,      # 没涨就走：再看 2 天
    lu_half = True,              # 卤煮减半
    big_yang_pct = 4.0,          # 中阳线的最低涨幅
    fourth_brick_half = True,    # 第四块红砖减半
    s1_exit = True,              # S1 清仓
    didi_exit = True,            # 嘀嘀清仓
    yellow_break_exit = True,    # 破黄线清仓，连续 2 根
    vol_shrink_threshold = 1.0,  # 当日量小于昨日量
    position_pct = 0.10,         # 首仓 10%
)
```

回测收益已含佣金、印花税、过户费和单边 0.1% 滑点；夏普按每日账户净值计算，空仓日收益为 0。

### 回测效果

2026-11-02 按知识库规则做的全市场实验：本地通达信数据，主板、创业板、科创板 5136 只（保留本地还在的停牌、退市文件），行情主要从 2021-08 开始，等比前复权，已含成本和滑点。对照是同一只股票、同样持有天数、同样费用、同样择时条件下随机日期买入，区间按股票聚类计算；同一个月的交易并不独立，按月聚类时区间会更宽。本地没有退市股，结果会偏好。

| 口径 | 笔数 | 每笔净收益均值 | 胜率 | 同条件随机入场 | 差（95% 区间） |
|------|-----:|-----:|-----:|-----:|-----:|
| SOP，不择时 | 20842 | -0.24% | 32.5% | -0.06% | -0.11%（-0.22 ~ -0.00） |
| SOP，活跃市值多头才开仓 | 6800 | +0.09% | 33.2% | +0.31% | -0.19%（-0.35 ~ -0.04） |
| SOP，活跃市值 + 大盘白线在黄线上 | 4805 | +0.07% | 32.8% | +0.20% | -0.15%（-0.33 ~ +0.02） |
| 双线版（白线离场），活跃市值 | 6800 | +0.10% | 32.6% | +0.32% | -0.21%（-0.37 ~ -0.06） |

和修改前相比（缩量标准放宽到当日量小于昨日量，补上第四块砖、S1、嘀嘀、破黄线）：SOP 活跃市值口径从 4265 笔、每笔 +0.25% 变成 6800 笔、每笔 +0.09%，差从 -0.23% 变成 -0.19%。这些修改是为了和原文一致，没有提高收益。同一批买入只换卖出规则，每笔少赚 0.14 个百分点（按月区间 -0.65 ~ +0.27），平均持有从 6.3 根缩短到 4.7 根。嘀嘀占离场约三分之一，技能里“高位”的判定比较宽，看到结果后没有调整。通用 B1 口径这次没有重跑，上一版结果见 `zettaranc-review-2026-10-31`。

活跃市值择时抬高了收益，而且随机买入也一样抬高，说明这部分来自择时本身。B1 入场信号在这个样本里没有显示出额外的优势。这个实验不含主线和主题选股、量比确认和五分制评分，所以不能说明整个体系如何，回测也不能当作未来收益的依据。**不构成投资建议。**

规则还可以用知识库的训练场卡片检查，见 `scripts/eval_cards.py`。这份题库的题干常常写明了规则，高分不能说明技能好，覆盖率的缺口更有参考价值。

旧版 README 的中国平安示例（8 笔、+40.74%、夏普 1.95）没有写回测区间，用的是当日收盘成交、不含成本的旧口径，已删除。

```bash
zt backtest shaofu 601318.SH --days 500
zt verify v1.0 --limit 200 --days 800   # 含最少笔数和随机入场对照
```

### 统计检验

策略是否有效不能只看回测收益。项目内置统计检验框架：

| 指标 | 门槛 |
|------|------|
| 夏普 t 检验 p-value | < 0.05 |
| Bootstrap CI 下界 | > 0.3 |
| 胜率 | > 40%（高盈亏比可降至 25%） |
| 盈亏比 | > 1.5 |
| 最大回撤 | < 25% |

统计检验的**底层能力**（`modules/statistics/`：夏普 t 检验、蒙特卡洛置换检验、子区间分析）仍在，可直接调用：

```python
from modules.statistics import sharpe_t_test, monte_carlo_permutation_test

p_value = sharpe_t_test(sharpe_ratio, n_periods)
```

> ⚠️ **该框架尚未接入验收链路**：v4.6.0 起，统计检验与 v1.0 验收（`zt verify v1.0`）是两条独立的路径——验收用的是五项硬指标（Sharpe / Calmar / WinRate / MaxDD / OOS-IS），不含统计检验。历史上的 `backtest_shaofu_with_validation` 入口因零调用方已删除。设计稿见 [docs/STATISTICS_VALIDATION.md](docs/STATISTICS_VALIDATION.md)。

详见 [docs/STATISTICS_VALIDATION.md](docs/STATISTICS_VALIDATION.md)。

---

## 安装

```bash
git clone https://github.com/lululu811/zettaranc-skill.git
cd zettaranc-skill

# 运行时依赖
pip install -r requirements.txt
pip install -e .

# 如需跑测试 / 开发，再装开发依赖（pytest 等）
pip install -e ".[dev]"
```

> 安装后注册三个命令：`zt`（CLI 主入口）、`zt-web`（FastAPI 后端）、`zt-monitor`（自选股主动监控）。不装包也可直接 `python -m modules.cli` 调用。
>
> **本 fork（Windows）**：请把依赖装进技能目录下的 `.venv`，步骤见下文「本地 A 股数据与每日报告」一节的「安装」。DSH 交易终端的选股页签、`scripts/daily_report.cmd` 和 SKILL.md 里的命令都只调用这里的 Python。

### 接入真实行情

```bash
cp .env.example .env
```

**方式一：本地通达信（推荐）**

```ini
TDX_DIR=D:/new_tdx64
```

- 只读 `vipdoc/*/lday/*.day`、`T0002/hq_cache/` 下的 `gbbq`、`*.tnf`、`base.dbf`、`tdxhy.cfg`，不写入通达信目录，也不需要启动客户端。
- 选股、指标用通达信口径的前复权，回测算收益用等比口径。前复权读取时现算，不落库。
- 行情新鲜度取决于客户端里下载的盘后数据。所有 `--json` 输出都带 `as_of`、`data_source` 和 `warnings`，落后时会提示。
- 全市场选股：`zt screen --strategy B1 --limit 20 --json`。`--limit` 是返回条数，`--pool` 才是扫描数量（默认全市场）。默认剔除没有最新行情的股票和 ST。
- 启动客户端后可运行 `python scripts/verify_tdx_qfq.py`，用通达信官方接口核对前复权。

活跃市值另外配置指南针的数据目录，`zt market amv` 查看当前多空状态，回测和选股提示都会用它：

```ini
ZHINANZHEN_DATA_DIR=D:/Program Files/WavMain/ANALYSE/Data/ChinaStk
```

**方式二：Tushare**

编辑 `.env`：

```ini
DATA_MODE=jnb
TUSHARE_TOKEN=你的token
TUSHARE_API_URL=https://api.waditu.com/dataapi
```

> [!NOTE]
> - **数据模式**：`DATA_MODE=jnb` 配置 Tushare 数据最全；未配置时自动走 a-stock-data 免费源；`DATA_MODE=websearch` 可留空。
> - **Token 获取**：前往 [Tushare 官网](https://tushare.pro/user/token) 注册。
> - **`TUSHARE_API_URL` 是基础路径**，SDK 会自动追加 `/{接口名}`（如 `/daily`）。推荐 `https://api.waditu.com/dataapi`（官方运营中转，路径式，接受官方 token）；**不能填** `https://api.tushare.pro`——官方端点只接受根路径 POST，被追加路径后返回 404。
> - **可选加速**：配置 `HITHINK_FINANCE_API_KEY`（同花顺官方）或 `INDEVS_API_KEY` 会自动提升数据源优先级。
> - **LLM 配置**：可选。配置 `LLM_API_KEY` 后启用 LLM 对话及点评；未配置则仅输出命令行分析。
> - **完整环境变量说明**：[docs/CONFIG_GUIDE.md](docs/CONFIG_GUIDE.md)

---

## 本地 A 股数据与每日报告（本 fork 新增）

只在 Windows 上测试过。全部功能都是可选的：没配置的部分自动跳过，并在报告里写明原因。

### 需要什么

| 软件 | 用途 | 必需？ |
|---|---|---|
| Python 3.12+ | 运行技能（装在技能目录的 `.venv` 里，见下面「安装」） | 必需 |
| 通达信客户端（下载过盘后数据） | 日线、除权、行业、股票列表 | 每日报告必需 |
| 通达信金融终端的 TQ 策略插件（安装目录下 `PYPlugins/sys/tqcenter.py`） | 用**通达信自己的公式引擎**跑你的选股公式 | 可选；没有时用你的 Python 版公式 |
| 指南针（全赢决策系统） | 活跃市值 0AMV（择时） | 可选；没有时报告不显示活跃市值 |
| [DSH 交易终端](https://github.com/zhu1090093659/dsh-trading) | 读自选里的「持仓」分组，同步「今日选股」分组 | 可选 |

### 安装

依赖必须装进技能目录下的 `.venv`：DSH 交易终端的选股页签、`scripts/daily_report.cmd` 和 SKILL.md 里的命令都固定调用 `.venv\Scripts\python.exe`（或 `zt.exe`），装到别处它们会找不到。在技能目录里运行（PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
```

下文的 `python -m modules.cli …` 都指这个 `.venv` 里的 Python，即 `.venv\Scripts\python.exe -m modules.cli …`。

### 配置（`.env`）

参考 `.env.example` 里的注释，最少要设 `TDX_DIR`：

```ini
TDX_DIR=D:/new_tdx64
ZHINANZHEN_DATA_DIR=D:/Program Files/WavMain/ANALYSE/Data/ChinaStk
REPORT_OUT_DIR=D:/stock/reports
ZT_REPORT_BLOCKS=我的B1,我的搬砖
ZT_TDX_FORMULA_MAP=我的B1=通达信里的B1公式名,我的搬砖=通达信里的搬砖公式名
DSH_HOME=D:/dsh-home          # 用 DSH 交易终端时
```

路径都按你自己的安装位置填。

### 每日报告

```bash
python -m modules.cli report                 # 生成 REPORT_OUT_DIR/YYYY-MM-DD.html，index.html 指向最新一天
python -m modules.cli report --refresh-amv   # 先打开指南针更新活跃市值（见下）再生成
```

报告内容（HTML，手机也能看）：

- **活跃市值**：多空状态、第几天、明天涨跌多少会翻转，近 60 天走势。只作背景，不否决选股和加仓；
- **持仓**（来自 DSH 自选的「持仓」分组）：砖块、加仓（只看 B1，连续几天会写出来）、放飞、卖出信号、BOLL 日/周位置；
- **今日选股**：跑 `ZT_REPORT_BLOCKS` 里的公式，按技能的卖出信号等规则过滤，每只只写买入理由；被剔除的写明原因；
- **红利组**（电力 / 银行 / 保险）：只列当天出现 B1 的；
- **入选后表现**：之前入选的票 5 / 10 个交易日后的涨跌，对比上证指数。

用 Windows 任务计划每天收盘后调用 `scripts/daily_report.cmd`（例如工作日 20:00），日志写在 `REPORT_OUT_DIR/logs/`。脚本带 `--skip-if-current`：休市日（或还没开盘）且最近一个交易日的报告已经生成时直接跳过，不打开指南针，也不会多出一份和上一交易日重复的报告。最近一个交易日按几只沪市权重股的实时行情时间判断，查不到时照常生成。

**数据来源与当天 K 线**：日线读本地通达信文件；当天还没下载盘后数据时，用实时行情补当天一根，按 **腾讯 → 同花顺（配置了 `HITHINK_FINANCE_API_KEY` 才用）→ 东财** 的顺序，前一家失败或没返回的才交给下一家，批量、串行、限速请求。昨收和本地对不上（可能当天除权）的不补，报告里写明。

### 选股公式：只在你自己的电脑上

**本仓库不包含任何第三方或付费课程的公式原文。** 接入你自己的公式有两层：

1. **通达信引擎（推荐）**：公式保存在通达信「公式管理器 → 条件选股公式」里，`.env` 的 `ZT_TDX_FORMULA_MAP` 把报告里的选股名对应到通达信公式名。通达信开着、且在收盘后跑当天报告时，报告直接调用通达信自己的引擎，结果和一键选股一致。
   - 列出通达信里的公式：`python -m modules.cli tq-formulas`
   - 手动跑一个：`python -m modules.cli tq-select --formula <公式名> [--date YYYYMMDD] [--boards 主板|all]`
   - 只用公式列表和批量选股这两类只读接口，**不调用 TQ 插件里的任何下单 / 撤单 / 查持仓函数**。
   - 公式里有 `DYNAINFO` 等实时函数时，结果只反映「现在」的行情，所以补算历史日期不用通达信引擎。
2. **Python 版本（回退）**：通达信没开或补算历史日期时用。复制 `examples/user_formulas.example.py` 到 `data/user_formulas.py`（或用 `ZT_USER_FORMULAS` 指定位置），照着通达信代码逐行翻译，函数库在 `modules/tdx_formulas.py`。写完用 `tq-select` 取通达信同一天的结果逐只核对。

选股范围默认沪深主板、剔除 ST 和上市不足 120 个交易日的次新股，和通达信一键选股的常见设置一致。

### 指南针活跃市值自动更新

只打开再关闭指南针**不会**更新本地 0AMV 文件；要在指南针里打开 0AMV 页面它才下载并写入。`python -m modules.cli amv-refresh`（或 `report --refresh-amv`）会：

1. 指南针没开就打开它（已经开着就不动，只等数据）；
2. 启动完成后，往指南针右下角的命令框发「0AMV」和回车（发给指南针自己的窗口，不模拟全局键盘）；
3. 数据更新到当天后，点主窗口关闭，再在确认弹窗里点「退出(Y)」。

### 与 DSH 交易终端联动

设了 `DSH_HOME` 后：报告读 `watchlists.json` 里「持仓」分组的股票；生成报告时把入选名单写成 `DSH_HOME/report-picks.json`，终端每分钟检查一次，同步进「今日选股」分组（只删它自己加的行，你手动加的自选不动）。终端侧的配置见 dsh-trading fork 的 `docs/guides/a-share-local-data.md`。

### 知识库与出处

规则出处写在 `modules/strategies/shaofu_rules.py` 和 `knowledge/source-priority.md`，形式是「文件路径:行号」。依据的是粉丝整理的公开笔记（版权归原作者），**本仓库不包含其原文**。想核对出处或跑训练场题库，把你自己合法获得的副本路径设到 `ZK_WIKI_DIR`，相关测试会自动启用，没有时跳过。

---

## CLI 工具

16 个顶层子命令，分析、选股、回测类都支持 `--json`（输出固定 UTF-8，带 `as_of` 和 `warnings`）：

```bash
zt analyze 600487.SH --json                  # 股票分析（指标 + 战法 + 信号）
zt screen --strategy B1 --limit 20 --json    # 全市场选股，返回前 20 条
zt score 600487.SH                            # 单股综合评分
zt market timing --json                       # 市场择时指标
zt workflow                                   # 每日五步工作流（等价 daily）
zt diagnose 600487.SH --json                 # 持仓诊断
zt watchlist add 600487.SH                    # 自选股：add/remove/list/scan/report
zt sync status                                # 数据同步状态
zt backtest shaofu 600487.SH --days 250 --json  # 少妇战法回测
zt backtest b2-confirm 600487.SH --json      # B1观察+B2确认回测
zt simulate 000001.SZ --days 250 --capital 100000 --json   # 端到端模拟
zt verify v1.0 --limit 200 --days 800 --walk-forward       # v1.0 验收（含最少笔数、随机入场对照）
zt trade add "口语化交易描述"                  # 记录交易
zt self-optimize run --target trading --rounds 3           # Darwin 自优化
zt monitor --json                             # 自选股主动监控
zt track list --status active --json          # 跟踪池管理
```

### 数据同步

```bash
zt sync init                                # 初始化数据库（只需一次）
zt sync status                              # 查看同步状态

# 首次建库 / 补历史（非增量）
zt sync sync --days 730 --indicators

# 日常盘后（推荐）：严格增量
zt sync sync --incremental --days 30 --indicators
#   K 线：每只股票从自己最后一根 K 线的下一天续拉，不重拉历史
#   --days 只对"库里无数据"的新股兜底，有数据的股票忽略它
#   指标：只重算本次有新 K 线的股票，且只写回新增交易日（带 250 根预热，不覆盖旧值）

# 单只补数
zt sync sync 600487.SH                      # 严格增量 + 默认同步指标缓存
zt sync sync 600487.SH --days 120           # --days 仅作用于指标缓存窗口
zt sync sync 600487.SH --skip-indicators
```

| 模式 | K 线起点 | 指标缓存 |
|---|---|---|
| 默认（不带 `--incremental`） | `今天 - days`，落后超 2 天的股票整段重拉 | 全量重算（`--indicators`） |
| `--incremental` | 每只股票自己最后一根 K 线的下一天 | 只写回新增交易日 |

增量不重算旧行。若怀疑历史数据有误（如之前用小 `--days` 反复刷过指标），先跑一次非增量的 `zt sync sync --days 730 --indicators` 修正，之后再走增量。

### 选股策略别名

`--strategy` 支持 16 个别名（13 个唯一条件，`牵牛/牛绳`、`沙漏/沙漏评分` 为同义）：

`B1` `B2` `B3` `完美图形` `超级B1` `长安战法` `建仓波` `吸筹` `安全` `超跌` `突破` `牵牛` `牛绳` `沙漏` `沙漏评分` `量比战法`

完整参数说明见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)。

---

## Web 看板（可选）

```bash
pip install fastapi uvicorn pydantic-settings   # API 依赖不在 requirements.txt
zt-web                                         # 后端 http://localhost:8000
cd frontend && npm install && npm run dev      # 前端 http://localhost:5173，/api 代理到 8000
```

> **两套前端并存**：`frontend/` 为「浅色玻璃拟态」新版；`frontend-legacy/` 是改版前的深色金融终端版（端口 5174，`/api` 同样代理到 8000），可并排对照切换。
>
> ```bash
> cd frontend-legacy && npm install && npm run dev   # http://localhost:5174
> ```
>
> 已知问题：`api` 包未包含在 editable 安装中（`pyproject.toml` 的 `include = ["modules*"]`），`zt-web` 会报 `ModuleNotFoundError: No module named 'api'`；可临时用
> `PYTHONPATH=. python -c "from api.main import start_web; start_web()"` 启动后端。

---

## 文档导航

| 我想知道… | 去看 |
|---|---|
| 怎么用 CLI / 安装依赖 / 第一次运行 | 本文 + [USER_GUIDE.md](docs/USER_GUIDE.md) |
| 怎么配置环境变量、数据源 key | [CONFIG_GUIDE.md](docs/CONFIG_GUIDE.md) |
| 想参与开发 / 提 PR / 写新指标 | [CONTRIBUTING.md](docs/CONTRIBUTING.md) |
| 哪个版本改了什么 | [CHANGELOG.md](docs/CHANGELOG.md) |
| 完整文档索引 | [INDEX.md](docs/INDEX.md) |
| AI Agent 角色协议 | [SKILL.md](SKILL.md) |
| 交易体系知识库（29 篇） | [knowledge/](knowledge/) |

> ⚠️ **免责声明**：本项目仅供学习研究使用，不构成投资建议。据此操作，风险自负。

## 许可证

[MIT](LICENSE) · 语料基于公开直播/付费课整理蒸馏
