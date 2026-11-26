# 板块轮动 + 换手率选股

标普 500 按 **10 个** SPDR 板块 ETF（XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLB/XLU/XLC，**不含 XLRE 房地产**）的 RSRS 排序选板块；
牛市在板块内按 `turn20²/turn120` 取前 3 只，3 板块共 9 只候选，再过 6 条趋势闸；
熊市不碰个股，只买「RSRS 前 3 ∩ 120 日加权斜率前 3」交集里最强的板块 ETF。

**每月最后一个交易日收盘前 30 分钟**决策：先卖出持仓，再买入新标的。

## 目录

```
latest_data/     只读基板（回测用，不参与每日更新）
upload/          实盘数据（每日收盘后更新，commit 到 main）
live/            实盘信号、账本、日历、推送
backtest/        回测脚本与口径
.github/         Actions：paper_trade.yml / cron_check.yml
```

实盘只读 `upload/`，回测只读 `latest_data/`。两者口径相同，区别只在数据区间：

| 目录 | 区间 | 谁用 |
|---|---|---|
| `latest_data/` | 2021-01 ~ 2026-11-13 | 回测基线 |
| `upload/` | 2025-12-31 ~ 至今，每日推进 | 实盘 |

## 策略口径

### 标的池
- **板块**：10 个 SPDR 行业 ETF
- **个股**：`members_daily.parquet` 中当日归属该板块的 S&P 500 成员（逐日 permno，约 503 只）

### 硬门槛
| 条件 | 阈值 |
|---|---|
| 20 日均换手率 `turn20` | ≤ 20% |
| 120 日均换手率 `turn120` | 非空且 > 0（至少 60 个有效日） |
| 当日总市值 `mcap` | > 500 亿美元 |

### 板块层
10 个板块 ETF 按 `rsrs20_40_z`（RSRS N=20 M=40，strict：R²≥0.5、\|β\|≤10）降序，取**前 3**。

### 个股层
每个板块按 `turn_score = turn20²/turn120` 降序取前 3 → **9 只候选池**。

### 趋势闸（6 条全 AND）
1. `MA20 > MA60`
2. `MA20` 的 **10 日斜率** > 0
3. `MA60` 的 **5 日斜率** > 0
4. `收盘 > MA50`
5. `MA50 > MA200`
6. `ADX(14) > 10`

按板块 RSRS 顺序、板块内按 `turn_score` 顺序，取**第一只**通过的买入。

### 熊市
- **判定**：SPY 收盘价低于 MA200 的天数，过去 21 个交易日占比 **> 50%**（`bear_ratio21 > 0.5`）
- **操作**：不买个股，买板块 ETF（RSRS 前 3 ∩ `slope120_weighted` 前 3 的交集，取 RSRS 最高者）
- **交集为空则空仓**，不补选
- ETF **不设止损**

### 交易规则
| 项 | 规则 |
|---|---|
| 调仓时点 | 每月最后一个交易日，收盘前 30 分钟 |
| 持仓 | 1 个标的，100% 全仓 |
| 同票续持 | 不重复收费 |
| 止损 | 个股相对买入价 **−10%**；止损日在每月 **11 号前**可补选 1 次（不得同一只） |
| 牛市空仓 | 次一交易日起**逐日补选**，直到选出或到下一调仓日 |
| 成本 | 单边 5bp，滑点未建模 |

### 换手率折算
| 场景 | 当日 `turnover` 权重 |
|---|---|
| 回测 | **1.0** |
| 实盘（收盘前 30 分钟） | **0.48**（当日成交额只走完约一半） |

## 实盘运行

由 Cloudflare Worker 在收盘前 30 分钟 / 收盘后 30 分钟各触发一次
`workflow_dispatch`，`paper_trade.yml` 内部按日历判断该不该动，不动就直接退出、不提交。

```powershell
# 本地复盘（不写账本）
python live/signal.py --date 2026-11-25 --kind rebalance

# 手动完整跑一轮（需要 PUSHPLUS_TOKEN）
python live/signal.py --kind auto --push

# 数据更新
python upload/update_all.py
```

四种动作、账本结构、当日 bar 合成方式见 `live/README.md`。

## 回测结果（2022-01-03 ~ 2026-11-13，`latest_data`）

| 指标 | 策略 | SPY |
|---|---|---|
| 累计收益 | **1363.47%** | 68.25% |
| 年化 | **73.70%** | 11.30% |
| 年化波动 | 35.67% | 17.36% |
| 夏普 | **2.0663** | 0.6511 |
| 最大回撤 | **−39.88%** | −24.50% |
| 交易 | 54 笔 / 止损 4 次 / 胜率 68.5% | — |
| 空仓天数占比 | 13.6% | — |

```powershell
python backtest/backtest_benchmark.py --tag v1
```

产出：`reports/bt_v2/bt_<tag>/`（`nav_daily.csv`、`hold_daily.csv`、`holds.csv`、`stats.csv`）。

## 数据说明

| 文件 | 内容 |
|---|---|
| `*/bars_daily.parquet` | 个股日频：行情、`turn20/turn120/turn_score`、`mcap`、均线、`adx14` |
| `*/etf_daily.parquet` | 板块 ETF 与 SPY：`rsrs20_40_raw/z`、`slope120_weighted`、`bear_ratio21` |
| `*/members_daily.parquet` | 逐日成分股与板块归属 |
| `upload/adjust_k.csv` | 未复权价 × k = 复权口径，止损用它折算实时价 |
| `upload/permno_map.csv` | symbol → permno 映射，新纳入的票从 `1_000_001` 起分配 |

`latest_data/` 是不可再生的基板（构建流程已随旧结构移除），只读，请自行备份。
