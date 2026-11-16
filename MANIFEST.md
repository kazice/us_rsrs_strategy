# 数据说明（MANIFEST）

> 本文件由 `scripts/data_manifest.py` 自动生成，每次更新数据后**原地重写**，
> 跟着数据一起提交，所以永远和实际内容一致。**不要手工编辑**，改了下次也会被覆盖。
>
> 生成时间：2026-11-16 06:57 UTC

## 概况

| 数据集 | 路径 | 规模 | 起始 | 结束 | 体积 |
|---|---|---|---|---|---|
| 日线 | `curated/bars_daily/` | 515 只 / 2,537,386 行 | 2005-01-03 | **2026-11-13** | 82.2 MB |
| 20 日换手率面板 | `curated/fundamentals/turnover_daily.parquet` | 576 只 / 427,749 行 | 2023-04-03 | **2026-11-13** | 1.3 MB |
| 总股本（季报） | `curated/fundamentals/shares_outstanding.parquet` | 501 只 / 49,338 行 | 披露 2009-04-15 | 披露 **2026-11-13** | 0.4 MB |
| 全市场快照 | `meta/universe/universe_latest.parquet` | 1,000 行 | — | as of **2026-11-16** | 0.0 MB |

## 数据新鲜度

- 行情最新交易日：**2026-11-13**（距今 3 天）
- 所有标的都跟到了最新交易日
- 换手率面板到 2026-11-13，不低于行情最新日

## 明细

### 日线 `curated/bars_daily/`

按 `symbol=<TICKER>/part.parquet` 分区，文件内不含 symbol 列，靠 hive 分区还原。

- 文件数：515
- 标的数：515，合计 2,537,386 行
- 日期范围：2005-01-03 ~ 2026-11-13

### 换手率 `curated/fundamentals/turnover_daily.parquet`

列 `date / symbol / turnover`；市值门槛反推股本、20 日均换手率排序都靠它。

- 标的数：576，合计 427,749 行
- 日期范围：2023-04-03 ~ 2026-11-13

### 总股本 `curated/fundamentals/shares_outstanding.parquet`

列 `symbol / end / val / filed / form / fy / fp`；按**披露日 filed** 取用，避免前视。

- 标的数：501，合计 49,338 行
- 报告期覆盖到：2026-11-13
- 披露日范围：2009-04-15 ~ 2026-11-13

### 全市场快照 `meta/universe/universe_latest.parquet`

市值 / 价格兜底来源（股本取不到时用 `market_cap / price` 反推）。

- 行数：1,000
- 快照日：2026-11-16

### 成分股快照 `ref/us_sector_holdings.csv`

- 行数：503，as of 2026-11-12

## 这个分支里没有的东西

| 没放 | 原因 |
|---|---|
| `raw/`（yfinance 原始响应缓存） | 体积大，属于抓取中间产物，删了能重抓 |
| `curated/options/` | 期权链，势垒研究用，模拟盘不读 |
| `curated/factors/`、`curated/index/` | 因子面板与指数成分变动，研究用 |
| 代码 | 在 `main` 分支，workflow 会把两边 checkout 到一起 |
