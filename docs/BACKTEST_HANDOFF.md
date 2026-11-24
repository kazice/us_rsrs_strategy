# 回测交接文档（给下一个 AI / 工程师）

> 目标：拿到这台机器、这个仓库，**不读源码也能在 10 分钟内跑出定稿回测、看懂产物、重生成看板**。
> 阅读顺序：第 1 节（一条命令跑通）→ 第 3 节（定稿参数逐条）→ 第 6 节（无前视口径）→ 第 9 节（陷阱）。
> 模拟盘 / 实盘链路看 `docs/HANDOFF.md`，本文只讲**回测**。

---

## 1. 一条命令跑通（已实测，约 30 秒）

Windows PowerShell，在仓库根目录：

```powershell
$env:PYTHONPATH = "src"
python -m scripts.sector_stock_rsrs --n 20 --m 40 --pick turnma --trend-mode strong --trend-stack 20,60 --slope-mas 20:10,60:5 --trend-above 50 --trend-cross 50,200 --adx-min 10 --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 --min-mcap 500 --exclude-sectors XLRE --regime-ma 200 --regime-bear nofilter --regime-window 21 --bear-etf-only --bear-min-score 0 --bear-rank both --bear-intersect-top 3 --bear-slope-window 120 --start 2021-11-13 --end 2026-11-13 --stop-loss 0.10 --stop-repick-dom 11 --tag sl10dom11
```

跑完打印 61 期逐期持仓表 + 汇总，结果写到：

```
reports/etf/rsrs_stock_n20_m40_top1_turnma_pool_no-XLRE_sl10_srdom11_strong_20-60_a50_x50-200_adx10_tratio_bref_cap20_tail48_mc500_reg200_bearnofilter_rw21_betfonly_bmin0_brankBoth3x120_sl10dom11/
```

**对账基准（改代码前后必比这几个数，均为定稿的固定 10% 止损口径）**：

| 区间 | 期数 | 累计 | 年化 | 夏普 | 最大回撤 |
|---|---|---|---|---|---|
| 含 2026 延展段（默认，`--end 2026-11-13`） | 61 | **+2835.5%** | 97.4% | **2.587** | −34.3% |
| 只要 CRSP 段（`--end 2025-12-31`） | 50 | +726.0% | 67.3% | 2.238 | −22.1% |

含 2026 版：ETF 版 +287.8%、SPY +72.6%；选股跑赢 ETF 32/61（累计选股差 +245.6%）；
熊市 12 期（买 ETF 5 / 空仓 7）。板块层只用 10 个 ETF（剔除 XLRE），XLRE 的 33 只成分股移出候选池。
止损 61 期触发 4 次、重选 2 次；50 期触发 3 次、重选 1 次。

**不设止损的对照（去掉 `--stop-loss` / `--stop-repick-dom`，tag `pool2026`）**：

| 区间 | 期数 | 累计 | 年化 | 夏普 | 最大回撤 |
|---|---|---|---|---|---|
| 含 2026 延展段 | 61 | +2472.2% | 92.1% | 2.396 | −36.7% |
| 只要 CRSP 段 | 50 | +832.3% | 72.2% | 2.300 | −22.1% |

止损在 CRSP 段是负贡献（收益 −106pp、夏普 −0.06、回撤持平），61 期的优势全部来自 2026-03
那一次割肉（F −18.2% → +5.4%，换 OXY）。本文下面所有敏感性表如果没特别说明，都是**不设止损**
口径，别拿它们和上面的 +2835.5% 直接比。

> ⚠ 2026 段（10 期，累计 +274.3%）只有本地行情，不是 CRSP：由
> `scripts/extend_pool_to_latest.py` 写进 `bars_ext/membership_ext/tickers_ext/universe_ext`，
> `src='local_ext'`。收益高度集中在 3 期：SNDK +72.4%（2026-03）、SNDK +54.6%（2026-04）、
> MRNA +37.1%（2026-08），不要拿 2026 段单独当年化能力。已知缺口：2026 年被剔除的 24 只票
> （MMC/DAY/PAYC/LW/MOH/MTCH/HOLX/CTRA/BK/EPAM/CPB/POOL/CAG/EA/AVB/EQR 等）在剔除前仍在指数内，
> 但本地行情只覆盖「当前成分」，这些票没有 2026 行情 → 那几个月里不在候选池，属于轻量幸存者偏差。

**别把「并集」当成「某天的成分数」**：窗口内并集 581（剔除 XLRE 后 548）指 2021-11 ~ 2025-12
期间**曾经当过**成分股的证券总数（首日 505 + 期内新进 76，同期剔除 70），回测按 `membership_mask`
逐日取，任一交易日只有 **503~506 只**（中位 503）。503 而不是 500，是因为指数按证券计：
GOOGL/GOOG、FOX/FOXA、NWS/NWSA 各算两只。每期真正参与排序的是「当日成分 ∩ 该板块 ∩ 市值门槛」，
50 期中位 11 只、最多 38 只。

> ⚠ 趋势闸对参数很敏感，改 `--slope-mas` / `--adx-min` 前先看这张敏感性表
> （同一数据源、同区间 2021-11 ~ 2025-12 / 50 期，均为 CRSP 段、均**不设止损**，不含 2026）：
>
> | MA20 斜率 | MA60 斜率 | ADX | 累计 | 夏普 | 最大回撤 |
> |---|---|---|---|---|---|
> | 5 日 | 5 日 | >20 | +269.4% | 1.220 | −30.2% |
> | 5 日 | 5 日 | >10 | +252.7% | 1.136 | −50.1% |
> | 10 日 | 5 日 | >20 | +253.1% | 1.170 | −30.2% |
> | **10 日（剔除 XLRE）** | **5 日** | **>10** | **+832.3%** | **2.300** | **−22.1%** |
> | **10 日** | **5 日** | **>10** | **+559.5%** | **1.930** | **−30.2%** |
> | 10 日 | 无 | >10 | +272.7% | 1.200 | −34.5% |
>
> 两组条件单独放松都会让结果变差（+269% → +253%），叠在一起却是 +559%，这是路径依赖下
> 选股落点变化 + 复利放大的结果，不代表"更严谨"或"更优"——引用数字时必须同时写明这组参数。
>
> 剔除 XLRE 也是同一性质：50 期里只变了 3 期，其中 2022-09-30 由「选 XLRE 但没选出个股 → 空仓」
> 变成「熊市买 ETF:XLI +13.8%」，2024-12-31 由「XLE KMI −9.9%」变成「XLC META +11.8%」，
> 复利放大后 +559% → +832%。换句话说，这 273pp 来自榜单位移后的两次换手，不是房地产板块
> 本身在拖后腿（它在 50 期里只被选中过 1 次，且当期没选出个股）。
任何一位不对，说明改动已经改变了策略行为，先查口径再报结论。

> **止损档位实测**（同区间、同数据，`--stop-repick-dom 11`；括号内为改前的无止损净值）：
>
> | 档位 | 61 期累计 | 61 期夏普 | 61 期回撤 | 50 期累计 | 50 期夏普 | 50 期回撤 |
> |---|---|---|---|---|---|---|
> | 无止损 | +2472.2% | 2.396 | −36.7% | +832.3% | 2.300 | −22.1% |
> | 固定 10%（定稿） | **+2835.5%** | **2.587** | −34.3% | +726.0% | 2.238 | −22.1% |
> | 固定 7% | +2345.2% | 2.440 | **−31.8%** | +561.3% | 2.008 | **−18.2%** |
> | MA20 两日确认 + 线下 min(7%, 布林下轨) | +1363.2% | 2.024 | −29.2% | +269.3% | 1.374 | −21.5% |
>
> 规律：止损越贴身，收益掉得越多、回撤压得越低，**但没有一档在 50 期里跑赢不设止损**（10% 持平偏负、
> 7% 夏普掉 0.29、MA20 掉 0.93）。10% 档在 61 期全面占优，是因为 2021~2024 一次都没触发过
> （4 次里有 3 次在 2025-10 之后），本质是吃到了 2026 段波动放大后的单个样本。
> 结论：**别把止损当 alpha**，它的作用是尾部保护；继续扫 5%/8%/12% 大概率只是换一个噪声落点。

> 与上一版（ticker 口径 CRSP 视图，+207.1% / 夏普 1.02 / 回撤 −41.6%）的差别只来自数据：
> 50 期里 46 期选股完全相同，4 期不同（2023-02 META、2024-11 PLTR、2025-06 COIN、2025-11 WBD），
> 都发生在「成分期前的换手率历史」与「改名前的价格历史」这两处——池层把这两段补齐了，
> 排序分（turn20²/turn120）与 MA200 因此有值，旧视图里这些票在刚纳入指数的几个月里是缺数据的。

> 逐期收益口径（2026-11 修正）：`个股收益`/`ETF收益` 现在含「持有期起」当日（含着当日换手成本），
> 基准取上一期期末净值，所以逐期复利连乘 **严格等于** `nav_daily` 全区间收益，年度汇总（净值口径）
> 与分年明细能逐项对上。该修正只动逐期数值（选股差、跑赢期数），**累计/年化/波动/夏普/回撤不变**。

> ⚠ **更早的数字已作废**：`--data-source local`（yfinance + westock + 今天 503 只快照）同区间跑出
> +637.9% / 夏普 1.52，其中约 2/3 来自数据错误（幸存者偏差为主）。数据源现在只剩池层一套，
> `local` / `crsp` / `crsp_ext` 这些分支连代码一起删掉了，详见 `docs/DATA_AUDIT_CRSP.md`。

`PYTHONPATH=src` 不能省（脚本里 `from quant.data import pool, store`）。也可以 `pip install -e .` 装成包。

---

## 2. 环境与数据前置

| 项 | 要求 | 缺失时的症状 |
|---|---|---|
| Python | ≥ 3.10（本机 3.10.2） | 语法 / 类型报错 |
| 依赖 | `pip install -e .`：pandas / numpy / pyarrow / duckdb / yfinance / plotly … | `ImportError` |
| 行情（ETF） | `data/curated/bars_daily/symbol=<SYM>/`（分区 parquet，本机 515 个代码） | 板块层没有 ETF 报价，直接报错退出 |
| **成分股池**（唯一个股数据源） | `data/curated/sp500_pool/{bars,universe,membership,tickers,events,exits}.parquet`（CRSP，到 2025-12-31） | 直接报错退出（先跑 `scripts/build_sp500_pool.py`） |
| **2026 延展段**（池层的一部分） | `data/curated/sp500_pool/{bars,membership,tickers,universe}_ext.parquet`（`src='local_ext'`） | 缺它时回测自动截断到 2025-12-31（先跑 `scripts/extend_pool_to_latest.py`） |
| 成分股名单 | `config/ref/us_sector_holdings.csv`（SPDR 官方 holdings，503 只） | 板块归属只能全按 SIC 近似 |

个股数据只有池层一套：ETF 不在标普 500 成分股池里，所以板块层/基准仍从本地行情分支读，
这是唯一例外。旧的 `data/curated/crsp_sp500`（ticker 口径的派生视图）已删除。

`data/` 是**独立 git 分支**（`origin/data`），不在 main 里。本地没有时：

```powershell
git worktree add ../data_branch origin/data      # 或用环境变量指过去
$env:TA_DATA_ROOT = "d:\path\to\data"
```

`config/settings.yaml` 的 `paths.data` 默认就是 `./data`。

---

## 3. 定稿参数逐条对照

目录名**就是参数快照**，名字一致 ⇒ 参数没跑偏。

| 参数 | 值 | 作用 |
|---|---|---|
| 数据源 | **sp500_pool（security_id 主键）** | 没有 `--data-source` 开关了：成分股 PIT 名单、换手率 = 成交量/总股本、市值 = `DlyCap`、复权价、外部预热段全部来自池层 |
| `--n 20 --m 40` | 20 / 40 | RSRS 回归 / 标准化窗口（板块层与个股层共用） |
| `--pick turnma` | 换手率降序 + 趋势闸 | 个股层不用 RSRS 排序，改用筹码活跃度 |
| `--trend-mode strong` | 强化趋势闸 | 多头排列 + 均线斜率 + 站上长期均线 + ADX 四道闸 |
| `--trend-stack 20,60` | MA20 > MA60 | `strong` 下的排列要求；不是默认的 10,20,50 |
| `--slope-mas 20:10,60:5` | MA20 用 10 日窗、MA60 用 5 日窗 | 写法「均线:窗口」，每根均线可独立指定；只写均线名则用 `--stack-slope` 的窗口 |
| `--trend-above 50` / `--trend-cross 50,200` | MA50 / MA50>MA200 | 收盘站上 MA50 且 MA50 在 MA200 之上 |
| `--adx-min 10 --adx-len 14` | 10 / 14 | 趋势强度下限（只要有一点趋势即可，弱趋势交给排列与斜率闸） |
| 换手率口径 | 池层 `turnover` | 成交量 ÷ 当日总股本（CRSP 行）；成分期之前由外部源补，已按重叠区间校验 |
| `--turn-rank-mode ratio` | `turn20² / turn120` | 排序 = 绝对换手率 × 相对自身半年常态的放大倍数 |
| `--turn-cap 0.20` | 20% | 20 日均换手 > 20% 的票当期剔除（分母失真） |
| `--turn-tail 0.48` | 0.48 | 信号日当天换手只算 0.48（收盘前 30 分钟时点） |
| `--exclude-sectors XLRE` | 剔除房地产板块 | 板块榜只剩 10 个 ETF，XLRE 的 33 只成分股一并移出候选池；`--exclude-sectors ""` = 11 个板块全用 |
| `--min-mcap 500` | 500 亿美元 | 市值门槛（池层 `DlyCap`，严格大于，无前视） |
| `--bull-refill` | 开 | 牛市当期选不出来 → 次日起逐日重挑，买到为止 |
| `--regime-ma 200 --regime-window 21` | MA200 / 21 日 | SPY 判牛熊：过去 21 日跌破占比 ≥50% 才算熊市 |
| `--regime-bear nofilter` | 不过滤 | 熊市不强制多头排列 |
| `--bear-etf-only` | 开 | 熊市直接买最强板块 ETF，完全不碰个股 |
| `--bear-rank both` | RSRS 前 3 ∩ 斜率前 3 | 熊市板块须双榜同时命中，交集为空则空仓 |
| `--bear-intersect-top 3` / `--bear-slope-window 120` | 3 / 120 | 交集深度 / 斜率回归窗口 |
| `--bear-min-score 0` | > 0 | 熊市买 ETF 要求板块 RSRS 为正 |
| `--stop-loss 0.10` | 10% | 收盘价 ≤ 买入价 × 0.90 当日收盘割肉（61 期触发 4 次、50 期 3 次） |
| `--stop-repick-dom 11` | 11 | 割肉日在该自然月 **11 号之前**才允许重选：次日起逐日按同一口径挑，**排除刚割的那只**；≥11 号则空仓到月末。0 = 关闭，改用 `--stop-repick-days` 的交易日口径 |
| `--start 2021-11-13 --end 2026-11-13` | 5 年 | 月频（`--freq M` 默认），月末最后交易日为锚点 |
| `--tag sl10dom11` | 标签 | **只是目录名后缀**（`ptag` 已自动带 `_sl10_srdom11`），不是逻辑参数（见 9.1） |

### 3.1 关键实现位置（`scripts/sector_stock_rsrs.py`，约 1300 行）

| 函数 / 段落 | 职责 |
|---|---|
| `rsrs_panel_fast` | 向量化 RSRS：`beta*r2`，`strict` 过滤 + **`shift(1)` 标准化**（无前视） |
| `dmi_panels` / `adx_panel` | Wilder ADX（含 ±DI，可选方向闸） |
| `weighted_slope_panel` | 熊市板块斜率榜（加权回归、归一化成 %/交易日） |
| `parse_slope_spec` | `--slope-mas` 的「均线:窗口」解析（如 `20:10,60:5`） |
| `load_etf` / `load_pool` | 板块 ETF 走本地行情；个股按 security_id 从池层取价/量/市值/换手/掩码/板块 |
| 换手率面板 | `rolling(20, min_periods=10)`，信号日一项 × `--turn-tail` |
| 主循环 | 逐调仓日：判牛熊 → 板块排序 → 板块下探 → 趋势闸 → 记一行 `holds` |
| `stop_setup` / 主循环止损分支 | 买入时定止损模式（买点在 MA20 上下）与止损价；固定百分比模式只看 `base_px` |
| `turnma_pick_on(exclude=…)` | 割肉后重选，口径同调仓日；`exclude` 排除刚割的那只，下一名顺补 |
| 末尾 | `stats.csv` 指标 + **目录名拼接** + 写盘 |

### 3.2 目录名 → 参数反查表

目录名 = `rsrs_stock_n{sn}_m{sm}_top1` + 各开关按固定顺序拼接，`--tag` 永远在最后。

| 片段 | 含义 |
|---|---|
| `_turnma` | `--pick turnma` |
| `_strong_20-60` | `--trend-mode strong` + `--trend-stack 20,60` |
| `_slma20x10-60x5` | `--slope-mas 20:10,60:5`（默认这组不写进目录名） |
| `_a50` / `_x50-200` | `--trend-above 50` / `--trend-cross 50,200` |
| `_adx10` | `--adx-min 10` |
| `_tratio` | `--turn-rank-mode ratio` |
| `_bref` / `_bbref` | `--bull-refill` / `--bear-refill` |
| `_cap20` / `_tail48` | `--turn-cap 0.20` / `--turn-tail 0.48` |
| `_mc500` | `--min-mcap 500`（池层 `DlyCap`，无股本反推/快照前视） |
| `_reg200` / `_rw21` / `_bearnofilter` | `--regime-ma 200` / `--regime-window 21` / `--regime-bear nofilter` |
| `_betf` / `_betfonly` / `_bspy` | `--bear-etf` / `--bear-etf-only` / `--bear-spy` |
| `_bmin0` | `--bear-min-score 0` |
| `_brankBoth3x120` | `--bear-rank both --bear-intersect-top 3 --bear-slope-window 120` |
| `_al1` | `--anchor-lag 1`（提前一个交易日调仓） |
| `_pool` | 数据源（固定）：sp500_pool，security_id 主键。历史目录里的 `_crsp` / `_turnws` / `_ppit` 属于已删除的旧口径 |
| `_no-XLRE` | `--exclude-sectors XLRE`（默认；11 个板块全用时不出现这段） |
| `_sl10` | `--stop-loss 0.10`（固定百分比止损；没有这段 = 不设止损） |
| `_srdom11` | `--stop-repick-dom 11`（割肉日 < 当月 11 号才允许重选）；`_stma20` = 旧版 MA20 两日确认止损 |

> ⚠ 成分股池只有一套：`sp500_pool` 的 `membership` 就是 PIT 名单，没有 snapshot / mcap / pit 三种口径，
> `--pool-mode` / `--crsp-scope` / `--data-source` 这些开关已经从脚本里删掉。

---

## 4. 产物结构与字段

`reports/etf/<参数串>/` 固定 4 个 CSV（`utf-8-sig`，Excel 可直接打开）：

| 文件 | 内容 |
|---|---|
| `nav_daily.csv` | `date,nav` 日度净值（起点 ≈1.0，已扣 `--cost`） |
| `nav_etf_daily.csv` | 对照净值：每期直接买所选板块 ETF |
| `holds.csv` | 逐期明细，26 列（见下） |
| `stats.csv` | 三行指标 `个股版` / `ETF版` / `SPY`，列 `累计,年化,波动,夏普,最大回撤` |

`holds.csv` 列：`信号日`（锚点）、`持有期起`、`持有期止`、`板块`、`板块RSRS`、`板块斜率%`、`斜率序`、
`补选日`（refill 生效日，空 = 当日选出）、`板块RSRS序`、`牛市`(1/0)、`个股`（展示用 ticker；
空 = 空仓，`ETF:XLK` = 熊市买 ETF）、`个股ID`（**实际持仓主键 security_id**，ETF/空仓为空）、
`候选尝试`（下探到第几个候选才买到）、`个股RSRS`、`同板块候选`、`位置分位`、`个股收益`、`ETF收益`、
`选股差`（= 个股收益 − ETF 收益）、`止损日`、`止损类型`、`再选股`、`候选前3`（该板块当期换手率排序前三，
逗号分隔，给 `scripts/build_picks_chart` 画走势图用）、`候选明细`、`候选闸门`、`SPY`。

**收益集中度（引用收益时必带）**：池层口径 50 期里最佳单期仍是 2024-10-31 **APP +105.9%**；
策略收益高度依赖少数几期，这是策略性质，不是 bug。任何"提升稳定性"的改造都要先量化它会不会削掉这几期。

---

## 5. 看板（两个 HTML）

两个看板都是**单文件、离线可看**，由脚本从回测产物生成，产物入库、脚本入库：

```powershell
$env:PYTHONPATH = "src"

# 主看板：净值曲线 / 指标卡 / 参数表 / 逐期交易 / 年度收益（指向定稿的 10% 止损产出）
python -m scripts.build_web_report --pattern "*sl10dom11" --out reports/web/index.html --title "RSRS 板块→个股 回测（池层口径 · 10% 止损）"

# 副看板：熊市段 11 个板块 ETF 走势 + 双条件每期选谁
python -m scripts.bear_etf_trends
```

- `build_web_report.py`：`--dir` 指定目录（默认 `reports/etf` 下**最新**一批）、`--pattern` 按目录名匹配、
  `--out` 输出路径、`--title` 标题。它读上表 4 个 CSV，参数表靠 `parse_params()` **从目录名反推**，
  所以新参数一定要写进目录名，否则看板上会显示 `-`。
- `bear_etf_trends.py`：**参数硬编码在文件头**（`REGIME_MA=200`、`REGIME_WIN=21`、`RSRS 20/40`、
  `SLOPE_WIN=120`、`INTERSECT=3`、`START/END=2021-11-13~2025-12-31`）。换回测口径时要同步改这几行，
  否则副看板讲的是旧口径。图里画的是**全部 11 个板块 ETF**（含 XLRE）只为对照走势，
  定稿的排序与熊市选择只用 10 个。
- 本地看：`python -m http.server 8765 --directory reports/web`，浏览器开 `http://127.0.0.1:8765/index.html`。

---

## 6. 无前视口径（这段别改，改了历史结论全废）

| 环节 | 无前视怎么保证 | 破坏它的写法 |
|---|---|---|
| RSRS z 值 | 均值/标准差用 `raw.shift(1)`，**不含当日** | 改成含当日的滚动窗口 |
| 市值门槛 | 池层 `mcap = DlyCap`（CRSP 当期真实市值，逐日） | 用今天的快照/全样本中位数反推 |
| 市值价格 | CRSP 的 `DlyCap` 本身就是当期市值，不自己乘价 | 用复权价 × 股本（拆股后市值算错） |
| 换手率 | 池层 `turnover`（成交量 ÷ 当日总股本），`rolling(20)`，信号日一项 × 0.48 | 信号日不折（回测会虚高）或实盘再折一遍 |
| 成分股池 | 池层 `membership_mask` 逐日 PIT（含退市/被剔除的票） | 用今天的成分股名单回测过去 |
| 成交额 CV 类因子 | `--cv-lag 1` 才只用信号日之前的数据 | 默认 0 含当日（对收盘前决策是可接受的，但要写明） |
| 调仓锚点 | `--anchor-lag 1` 可整体前移一天，窗口与买价同步移动 | 只移买价不移窗口 |

> 2026-11 的改动：市值门槛的股本从"3 年中位数"改为"最近 5 日滚动中位数"（提交 `5f8380b`）。
> 换手率反推口径降级为快照缺失时的兜底，窗口 3 年 → 5 个交易日；全池与官方快照比对，
> 5 日窗口 ≤2% 误差占 96.0%。

---

## 7. 怎么扫参数

1. **每次跑必带 `--tag`**：`--start/--end/--cost/--spec/--turnma-top/--turnma-sectors` **不进目录名**，
   不加 tag 会直接把上一个结果覆盖掉。
2. 批量示例（PowerShell）：

```powershell
$env:PYTHONPATH = "src"
foreach ($reg in 120, 200) {
  foreach ($top in 3, 4) {
    python -m scripts.sector_stock_rsrs --pick turnma --trend-mode strong --trend-stack 20,60 `
      --slope-mas 20:10,60:5 --trend-above 50 --trend-cross 50,200 --adx-min 10 `
      --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 `
      --min-mcap 500 --regime-ma $reg --regime-window 21 --bear-etf-only --bear-min-score 0 `
      --bear-rank both --bear-intersect-top $top --bear-slope-window 120 `
      --start 2021-11-13 --end 2025-12-31 --tag "reg$reg`_t$top"
  }
}
```

3. 结果汇总（一个目录一行）：

```powershell
python -c "import pandas as pd,glob;rows=[(d.split('etf/')[1],pd.read_csv(d+'/stats.csv',index_col=0).loc['个股版']) for d in glob.glob('reports/etf/*')];df=pd.DataFrame([r[1] for r in rows],index=[r[0] for r in rows]);print(df.sort_values('夏普',ascending=False).round(3).to_string())"
```

**纪律**：参数扫描只用于**稳健性检验**（看结论是否处处成立），不能挑夏普最高的那一档当结论。
市值门槛 100/200/300/500 四档收益不单调（500 最好、300 最差）就是明证。

---

## 8. 自检与对账

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q          # test_rsrs / test_mcap / test_turnover / test_calendar / test_bar_source
```

跑完回测按顺序看四件事：

1. **期数**：`holds.csv` 行数应为 61（2021-11 ~ 2026-11 月频）/ 50（`--end 2025-12-31`）。
   少于预期 → 预热不足或 anchor 偏移。
2. **stats.csv 三行**：与第 1 节的基准逐位比。
3. **持仓分布**：61 期 = 个股 49 期 + 熊市 ETF 5 期 + 空仓 7 期；止损 4 期、重选 2 期。
   突然变多/变少 → 趋势闸、牛熊判定或止损条件被改坏。
4. **挪动 `--start` 看稳定性**：起点平移一年，夏普不应出现数量级变化，否则是少数几期在撑。

---

## 9. 陷阱

### 9.1 `_merged` 只是标签，不是合并

`--tag merged` 仅在目录名末尾加 `_merged`。没有"多段拼接"的逻辑；**不要**手工往这个目录里追加 CSV，
下一次同参数运行会整个覆盖。要保留不同结论，用不同 tag。

### 9.2 `reports/` 被 gitignore，但两个看板必须入库

`.gitignore` 第 3 行忽略 `reports/`，只对 `reports/web/*.html` 开了白名单（见文件内注释）。
删掉白名单会让看板在下次提交时静默消失。

### 9.3 目录名是唯一可靠的参数记录

`build_web_report.parse_params()` 从目录名反推参数表。新增参数**必须**同时写进目录名拼接段
（`scripts/sector_stock_rsrs.py` 约 1218–1298 行），否则看板与后续接手者都无法还原口径。

### 9.4 `--trend-mode strong` 的预热很长

`strong` 要看 MA200，脚本按 `trend_w × 1.6 + 60` 日历日预热。`--start` 定得比 MA200 更长时，
开头整段会是 NaN，被误判成"都不满足趋势"→ 全程空仓。看到 0 笔交易先怀疑这个。

### 9.5 熊市判定是"占比"不是"单日"

`--regime-window 21` + 默认 `--regime-bear-ratio 0.5`：过去 21 日跌破 MA200 的天数占比 ≥50% 才算熊市。
写成单日跌破会在均线附近反复横跳，回测与推送都会变。

### 9.6 空仓是合法结果

9 期空仓不是 bug。候选全部不满足趋势闸时当期就空仓，**不要"兜底买一只"**——
兜底会让回测与实盘脱钩，也会把收益结构改掉。

### 9.7 换手率是小数不是百分数

`0.0086` = 0.86%。`成交量 ÷ 换手率 = 股本` 依赖这个单位。写成百分数会得到 100 倍偏差的股本。

### 9.8 Windows 中文乱码

PowerShell 默认代码页会让中文输出变乱码（`��ȡ��`），**只影响显示**，不影响 CSV 与脚本。
需要正常显示时先 `$env:PYTHONUTF8 = "1"`。

### 9.9 止损有三个容易搞错的地方

1. **止损位基准是买入日收盘价**（`base_px`），不是信号日收盘价，也不是加权成本。重选后基准随新票重置。
2. **"11 号之前"是日历日不是交易日**。`--stop-repick-dom 11` 判 `割肉日.day < 11`；
   月初建仓时它只相当于 6~7 个交易日，月末建仓会在次月 11 号前仍然可重选。想要"前 N 个交易日"
   就用 `--stop-repick-days`（两者互斥，`dom > 0` 时优先）。
3. **重选一定排除原票**，下一名顺补；但只重选一次（`n_stop == 1`），重选后又止损就空仓到月末。

### 9.10 本机 `.py` 曾被加密

`docs/HANDOFF.md` 第 0 节记录过：本机部分 `.py` 曾被加密软件包了文件头。
当前工作区是明文（`git show` 可核），但导出给别人前用 `git show HEAD:path > path.txt` 验证一遍更稳。

---

## 10. 文件地图（回测相关）

| 路径 | 职责 |
|---|---|
| `scripts/sector_stock_rsrs.py` | **回测主脚本**：双层 RSRS + 趋势闸 + 熊市分支 + 净值计算 + 写盘（security_id 主键） |
| `scripts/build_sp500_pool.py` | CRSP 原始 CSV → `data/curated/sp500_pool`（成分区间 / 逐日行情 / 复权 / 板块 / 体检） |
| `scripts/extend_pool_to_latest.py` | 2025-12-31 之后用本地行情 + 成分公告延展池层（写 `*_ext.parquet`，`src='local_ext'`） |
| `src/quant/data/pool.py` | **唯一数据源读取层**：`bars/universe/membership/tickers` + 宽表面板 + PIT 掩码 + 退市收益 |
| `scripts/backfill_pool_history.py` / `backfill_pool_turnover.py` | 补成分期之前的外部价格 / 换手率（预热段，`src='external'`） |
| `scripts/check_holdings_membership.py` | **逐期持仓成分校验**：A 层 `pool.membership()`，B 层用 2025-12-31 在册名单 + 变更表独立回放 2026 段；明细写 `reports/data_audit/holdings_membership_check.csv` |
| `docs/DATA_AUDIT_CRSP.md` | **数据审计**：收益虚高的来源、四项对账、归因实验、修正清单 |
| `scripts/sector_rsrs.py` | 单层板块 RSRS（`rsrs_panel` 参考实现、`anchors`、`perf` 被主脚本复用） |
| `scripts/build_web_report.py` | 主看板生成（`reports/web/index.html`） |
| `scripts/bear_etf_trends.py` | 熊市 ETF 副看板（`reports/web/bear_etfs.html`） |
| `scripts/backfill_turnover.py` | 用 SEC 股本回补 2021-01 ~ 2023-10 的换手率（`ws` 口径上线前的历史） |
| `src/quant/data/store.py` | `load_panel(field, symbols, start, end)` 读分区 parquet |
| `src/quant/data/universe.py` | `latest_file()` 市值快照路径 |
| `config/ref/us_sector_holdings.csv` | 板块 → 成分股映射（503 只） |
| `tests/test_rsrs.py` / `test_mcap.py` / `test_turnover.py` | 口径回归测试 |

`scripts/backfill_turnover.py` 用法：

```powershell
$env:PYTHONPATH = "src"
python -m scripts.backfill_turnover --dry-run                       # 先看覆盖率
python -m scripts.backfill_turnover --start 2021-01-01 --end 2023-10-01
```

（`--max-resid 0.10` 重叠期残差上限，超过整只不补；默认写盘前自动备份。）

---

## 11. 速查

```powershell
$env:PYTHONPATH = "src"

# 定稿回测（第 1 节完整命令）
python -m scripts.sector_stock_rsrs --n 20 --m 40 --pick turnma ... --stop-loss 0.10 --stop-repick-dom 11 --tag sl10dom11

# 逐期持仓成分校验（退出码非 0 即有票在买入时点还没进指数）
python -m scripts.check_holdings_membership --dir "reports/etf/<上面的目录>"

# 数据侧（唯一数据源：sp500_pool）
python -m scripts.build_sp500_pool --start 2019-01-01 --pool-start 2021-01-01
python -m scripts.backfill_pool_history --limit 500        # 补预热段价格（缺的话）
python -m scripts.backfill_pool_turnover                   # 补预热段换手率
python -m scripts.build_pool_data_board                    # 股池体检看板
python -m scripts.extend_pool_to_latest                    # 2025-12-31 之后延展到本地最新（2026 段）

# 看板
python -m scripts.build_web_report --pattern "*_pool" --out reports/web/index.html --title "…"
python -m scripts.bear_etf_trends
python -m http.server 8765 --directory reports/web

# 测试
python -m pytest -q

# 看某一期的决策
python -c "import pandas as pd;h=pd.read_csv(r'reports/etf/<参数串>/holds.csv');print(h[h['信号日']=='2026-09-30'].T)"
```

改口径前先做三件事：① `git log -p` 看这段是谁为什么改的；② 用第 1 节的基准跑一次确认没漂；
③ 把新数字写进本文第 1 节与 `docs/HANDOFF.md` 的对应行。**不留数字的结论等于没跑过。**

## 12. 数据源历史（为什么有两套口径）

| 时间 | 口径 | 同区间结果（2021-11 ~ 2025-12，50 期） |
|---|---|---|
| ~2026-11 之前 | `local`：yfinance 行情 + westock 换手率 + 今天 503 只快照 + 反推股本 | +637.9% / 夏普 1.52 |
| 2026-11 起 | `crsp`：标普 500 标准数据（PIT 成分 / 真实股本与市值 / 自带复权） | **+207.1% / 夏普 1.02（回撤 −41.6%）** |

差额约 2/3 来自数据错误：平均每期混入 58 只当时不在指数里的票、漏掉 58 只当时还在的票；
换手率对多股本结构（GOOG/GOOGL、FOX/FOXA、NWS、DELL）低估 2~3 倍；市值在 500 亿门槛上 1% 的票日判反。
完整证据见 `docs/DATA_AUDIT_CRSP.md`。
