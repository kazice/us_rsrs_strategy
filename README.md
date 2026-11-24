# 板块轮动 + 换手率选股 · 模拟盘

标普 500 按 10 个 SPDR 板块 ETF 的 RSRS 排序选板块（XLRE 板块层剔除）。牛市里板块内按
`turn20²/turn120` 取前 3，要求 strong 趋势五闸（MA20>MA60、MA20 的 10 日与 MA60 的 5 日斜率为正、
收盘>MA50、MA50>MA200、ADX(14)>10），最多下探 3 个板块；熊市不碰个股，只买
「RSRS 前 3 ∩ 120 日加权斜率前 3」里最强的板块 ETF。月末最后一个交易日**收盘前 30 分钟**决策，
单票满仓；**收盘价相对买入价跌 10% 当日割肉**，割肉日在当月 11 日之前则次日起逐日重选
（排除刚割的那只），否则空仓到下一个调仓日；调仓日空仓时牛市逐日补选到下一个调仓日。
信号通过 pushplus 推送到微信，账本与决策日志提交回 `data` 分支。

定稿口径（与回测一一对应，改任一项都会与回测脱钩）：

```
--n 20 --m 40 --pick turnma --turnma-top 3 --turnma-sectors 3
--trend-mode strong --trend-stack 20,60 --slope-mas 20:10,60:5
--trend-above 50 --trend-cross 50,200 --adx-min 10
--turn-rank-mode ratio --turn-cap 0.20 --turn-tail 0.48 --bull-refill
--min-mcap 500 --exclude-sectors XLRE
--regime-ma 200 --regime-window 21 --bear-etf-only --bear-rank both
--bear-intersect-top 3 --bear-slope-window 120 --bear-min-score 0
--stop-loss 0.10 --stop-repick-dom 11
```

`--stop-loss 0.10` = 固定 10% 止损；`--stop-repick-dom 11` = 割肉日在该自然月 11 号之前才允许
重新选股，且重选一律排除刚止损的那只。

区间默认取池层的最后一天（含 2026 延展段时是 2026-11-13）；只要 CRSP 段就传 `--end 2025-12-31`。

数据源只有一套：`data/curated/sp500_pool`（security_id = CRSP PERMNO 主键）。成分区间、复权价、
成交量/总股本、当期市值、板块归属都来自它；ticker 只是展示用的别名，不参与选股。
2025-12-31 之后（CRSP 数据包边界）由 `scripts/extend_pool_to_latest.py` 用本地行情按重叠区间
缩放进同一价格空间，写成 `*_ext.parquet`（`src='local_ext'`），仍由同一个 `pool` 接口读取。

市值门槛 500 亿美元是**数据清洗口径**（剔除换手率分母失真的小盘票），不是调参：
100/200/300/500 四档的回测收益并不单调，阈值按经济理由定，不按收益挑。

市值用**当日真实股本**算，不再用股本估算：Nasdaq 日快照（`market_cap / price`）→ 最近 5 日
「成交量 ÷ 换手率」反推 → SEC 申报股本，逐级兜底，每只票的来源写进推送与 `candidates.csv`。

**回测数据源（2026-11 定稿）**：买来的标普 500 标准数据经 `scripts/build_sp500_pool.py` 清洗成
`data/curated/sp500_pool`（security_id 主键）——PIT 成分区间、成交量÷总股本的真实换手率、
当期真实市值、复权因子，以及成分期之前的外部预热段。旧口径用今天的 503 只快照回测过去，
平均每期混入 58 只当时不在指数里的票、漏掉 58 只当时还在的票，累计收益虚高 2 倍以上：

| 版本 | 区间 | 累计 | 年化 | 夏普 | 最大回撤 |
|---|---|---|---|---|---|
| 旧口径（本地快照 + 估算股本，已作废） | 50 期 | +637.9% | 62.7% | 1.52 | −25.8% |
| 旧 CRSP ticker 视图（已删除） | 50 期 | +207.1% | 31.4% | 1.02 | −41.6% |
| **定稿：固定 10% 止损** | **50 期**（CRSP 段） | **+726.0%** | 67.3% | 2.24 | **−22.1%** |
| **定稿：固定 10% 止损** | **61 期**（含 2026 延展段） | **+2835.5%** | 97.4% | **2.59** | −34.3% |
| 对照：不设止损 | 50 期 | +832.3% | 72.2% | 2.30 | −22.1% |
| 对照：不设止损 | 61 期 | +2472.2% | 92.1% | 2.40 | −36.7% |
| SPY（同期 61 期） | 61 期 | +72.6% | 11.6% | 0.67 | −24.5% |

止损不是免费的：50 期收益从 +832.3% 降到 +726.0%、夏普 2.30→2.24，换来回撤从 −22.1% 持平；
61 期因为 2026-03 那次割肉（F −18.2% → +5.4%，换 OXY）收益反而更高、回撤从 −36.7% 收到 −34.3%。
61 期里只触发 4 次止损（2021-2024 一次都没有），样本极少，别把这段改善当稳定 alpha。

2026 段的行情来自本地行情源（`scripts/extend_pool_to_latest.py` 把 2025-12-31 之后按重叠区间缩放进
CRSP 价格空间，写回池层的 `*_ext.parquet`，`src='local_ext'`），不是 CRSP 原始数据；
成分按 2026 公告回放。**2026 只有 10 期，累计 +274.3%，其中 3 期（SNDK 两期 +72.4%/+54.6%、
MRNA +37.1%）贡献了绝大部分，这一段不能单独当成年化能力引用。**

证据与归因见 `docs/DATA_AUDIT_CRSP.md`。**引用收益只能引池层口径。**

## 目录

```
.github/workflows/paper_trade.yml   两个 job：盘中信号、收盘后数据更新
config/settings.yaml                数据源与路径
config/ref/us_sector_holdings.csv   成分股兜底快照
docs/HANDOFF.md                     交接文档：策略核心、逐文件职责、口径陷阱（接手先读这个）
docs/BACKTEST_HANDOFF.md            回测交接：一条命令复现、定稿参数、产物字段、无前视口径、扫参
docs/DATA_AUDIT_CRSP.md             数据审计：收益虚高的来源、四项对账、归因实验、修正清单
docs/RESEARCH_SECTOR_MOMENTUM.md    因子研究：板块 RSRS 排序对未来 20 日收益到底有没有用
docs/PAPER_TRADING.md               模拟盘：链路、候选池、与回测的口径差异
docs/LIVE_LOG.md                    日志与回溯：目录、字段、查询命令、注意事项
scripts/live_signal.py              信号入口（含日志落盘）
scripts/live_audit.py               日志查询工具
scripts/paper_close_update.py       收盘后更新行情 / 换手率 / 成分股（末尾做完整性自检）
src/quant/live/                     模拟盘本体（calendar/quotes/turnover/pool/signal/mcap/books/notify/journal）
src/quant/data/                     行情层（store/fetch/universe/sources）
```

## 接手开发从这里开始

`docs/HANDOFF.md` 是给下一个开发者/AI 的完整交接：策略核心与公式、每个文件的职责、
数据结构、数据契约、12 条口径陷阱、改需求该动哪个文件、禁止事项。

跑回测看 `docs/BACKTEST_HANDOFF.md`：一条命令复现定稿结果（含对账基准数字）、参数逐条对照、
目录名反查表、产物字段、无前视口径清单、参数扫描套路与陷阱。

```powershell
$env:PYTHONPATH = "src"
python -m scripts.sector_stock_rsrs --n 20 --m 40 --pick turnma --trend-mode strong --trend-stack 20,60 --slope-mas 20:10,60:5 --trend-above 50 --trend-cross 50,200 --adx-min 10 --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 --min-mcap 500 --exclude-sectors XLRE --regime-ma 200 --regime-bear nofilter --regime-window 21 --bear-etf-only --bear-min-score 0 --bear-rank both --bear-intersect-top 3 --bear-slope-window 120 --start 2021-11-13 --end 2026-11-13 --stop-loss 0.10 --stop-repick-dom 11 --tag sl10dom11
```

看板：`python -m scripts.build_web_report --pattern "*sl10dom11" --out reports/web/index.html`，
产物为单文件 HTML（`reports/web/index.html`、`reports/web/bear_etfs.html`）。

逐期走势图：`python -m scripts.build_picks_chart --pattern "*sl10dom11"`，产出
`reports/web/picks.html`——每期一图，画该板块内换手率排序前 3 名的日线
（信号日前 3 个月 → 持有期止），★ 标记实际买入的那只。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

# 空跑一次（不推送、不记账，但会写本地日志）
python -m scripts.live_signal --dry-run --force --kind rebalance
```

需要先把 `data` 分支 checkout 到 `./data`，或者用环境变量指过去：

```powershell
$env:TA_DATA_ROOT = "d:\path\to\data"
$env:TA_STATE_DIR = "d:\path\to\data\state\live"
```

## 查历史决策

```bash
python -m scripts.live_audit --date 2026-11-30        # 那天为什么这么选
python -m scripts.live_audit --why AIG --date 2026-11-30
python -m scripts.live_audit --gaps --since 2026-06-01
python -m scripts.live_audit --replay --date 2026-11-30
```

Actions 的 run log 只保留 90 天，**不作为回溯依据**；证据在 `data` 分支的 `state/live/`。
详见 `docs/LIVE_LOG.md`。

## 免责

研究用途，只有一条固定 10% 止损、不做仓位管理，不是资管产品。
