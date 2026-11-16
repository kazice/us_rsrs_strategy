# 数据说明（MANIFEST）

> 本文件由 `scripts/data_manifest.py` 自动生成，每次更新数据后**原地重写**，
> 跟着数据一起提交，所以永远和实际内容一致。**不要手工编辑**，改了下次也会被覆盖。
>
> 生成时间：2026-11-16 04:19 UTC

## 概况

| 数据集 | 路径 | 规模 | 起始 | 结束 | 体积 |
|---|---|---|---|---|---|
| 日线 | `curated/bars_daily/` | 515 只 / 2,513,556 行 | 2005-01-03 | **2026-11-10** | 81.4 MB |
| 20 日换手率面板 | `curated/fundamentals/turnover_daily.parquet` | 566 只 / 423,245 行 | 2023-04-03 | **2026-10-26** | 1.4 MB |
| 总股本（季报） | `curated/fundamentals/shares_outstanding.parquet` | 501 只 / 49,338 行 | 披露 2009-04-15 | 披露 **2026-11-13** | 0.4 MB |
| 全市场快照 | `meta/universe/universe_latest.parquet` | 1,000 行 | — | as of **2026-10-27** | 0.1 MB |

## 数据新鲜度

- 行情最新交易日：**2026-11-10**（距今 6 天） —— ⚠ 超过 5 天阈值
- 落后于最新日的标的：503 只，如 XYL(2026-10-26), XOM(2026-10-26), XEL(2026-10-26), WY(2026-10-26), WST(2026-10-26), WSM(2026-10-26), WM(2026-10-26), WDAY(2026-10-26) …
- ⚠ 换手率面板只到 2026-10-26，比行情落后 15 天，20 日均值会偏旧

## 明细

### 日线 `curated/bars_daily/`

按 `symbol=<TICKER>/part.parquet` 分区，文件内不含 symbol 列，靠 hive 分区还原。

- 文件数：515
- 标的数：515，合计 2,513,556 行
- 日期范围：2005-01-03 ~ 2026-11-10

### 换手率 `curated/fundamentals/turnover_daily.parquet`

列 `date / symbol / turnover`；市值门槛反推股本、20 日均换手率排序都靠它。

- 标的数：566，合计 423,245 行
- 日期范围：2023-04-03 ~ 2026-10-26

### 总股本 `curated/fundamentals/shares_outstanding.parquet`

列 `symbol / end / val / filed / form / fy / fp`；按**披露日 filed** 取用，避免前视。

- 标的数：501，合计 49,338 行
- 报告期覆盖到：2026-11-13
- 披露日范围：2009-04-15 ~ 2026-11-13

### 全市场快照 `meta/universe/universe_latest.parquet`

市值 / 价格兜底来源（股本取不到时用 `market_cap / price` 反推）。

- 行数：1,000
- 快照日：2026-10-27

### 增量状态 `meta/state.json`

每只股票上次抓到哪一天，增量更新靠它断点续抓。

- 条目数：1,128
- 最后写入：2026-11-16 04:18 UTC

### 成分股快照 `ref/us_sector_holdings.csv`

- 不存在（运行时退回 config/ref 的静态快照，不影响使用）

## 这个分支里没有的东西

| 没放 | 原因 |
|---|---|
| `raw/`（yfinance 原始响应缓存） | 体积大，属于抓取中间产物，删了能重抓 |
| `curated/options/` | 期权链，势垒研究用，模拟盘不读 |
| `curated/factors/`、`curated/index/` | 因子面板与指数成分变动，研究用 |
| 代码 | 在 `main` 分支，workflow 会把两边 checkout 到一起 |
