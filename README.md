# 板块轮动 + 换手率选股 · 模拟盘

标普 500 按 11 个 SPDR 板块 ETF 的 RSRS 排序选板块。牛市里板块内按 `turn20²/turn120` 取前 3，
要求 strong 趋势五闸（MA20>MA60、两条均线 5 日斜率为正、收盘>MA50、MA50>MA200、ADX(14)>20），
最多下探 3 个板块；熊市不碰个股，只买「RSRS 前 3 ∩ 120 日加权斜率前 3」里最强的板块 ETF。
月末最后一个交易日**收盘前 30 分钟**决策，单票满仓、不设止损；调仓日空仓时牛市逐日补选到
下一个调仓日。信号通过 pushplus 推送到微信，账本与决策日志提交回 `data` 分支。

定稿口径（与回测一一对应，改任一项都会与回测脱钩）：

```
--n 20 --m 40 --pick turnma --turnma-top 3 --turnma-sectors 3
--trend-mode strong --trend-stack 20,60 --stack-slope 5 --slope-mas 20,60
--trend-above 50 --trend-cross 50,200 --adx-min 20
--turn-shares ws --turn-rank-mode ratio --turn-cap 0.20 --turn-tail 0.48 --bull-refill
--min-mcap 500
--regime-ma 200 --regime-window 21 --bear-etf-only --bear-rank both
--bear-intersect-top 3 --bear-slope-window 120 --bear-min-score 0
```

市值门槛 500 亿美元是**数据清洗口径**（剔除换手率分母失真的小盘票），不是调参：
100/200/300/500 四档的回测收益并不单调，阈值按经济理由定，不按收益挑。

市值用**当日真实股本**算，不再用股本估算：Nasdaq 日快照（`market_cap / price`）→ 最近 5 日
「成交量 ÷ 换手率」反推 → SEC 申报股本，逐级兜底，每只票的来源写进推送与 `candidates.csv`。

## 目录

```
.github/workflows/paper_trade.yml   两个 job：盘中信号、收盘后数据更新
config/settings.yaml                数据源与路径
config/ref/us_sector_holdings.csv   成分股兜底快照
docs/HANDOFF.md                     交接文档：策略核心、逐文件职责、口径陷阱（接手先读这个）
docs/BACKTEST_HANDOFF.md            回测交接：一条命令复现、定稿参数、产物字段、无前视口径、扫参
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
python -m scripts.sector_stock_rsrs --n 20 --m 40 --pick turnma --trend-mode strong --trend-stack 20,60 --stack-slope 5 --slope-mas 20,60 --trend-above 50 --trend-cross 50,200 --adx-min 20 --turn-shares ws --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 --min-mcap 500 --regime-ma 200 --regime-bear nofilter --regime-window 21 --bear-etf-only --bear-min-score 0 --bear-rank both --bear-intersect-top 3 --bear-slope-window 120 --start 2021-11-13 --end 2026-10-26 --tag merged
```

看板：`python -m scripts.build_web_report --pattern "*_merged" --out reports/web/index.html`，
产物为单文件 HTML（`reports/web/index.html`、`reports/web/bear_etfs.html`）。

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

研究用途，不设止损、不做仓位管理，不是资管产品。
