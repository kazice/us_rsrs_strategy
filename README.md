# 板块轮动 + 换手率选股

标普 500 按 **10 个** SPDR 板块 ETF（XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLB/XLU/XLC，**不含 XLRE 房地产**）的 RSRS 排序选板块；
牛市在板块内按 `turn20²/turn120` 取前 3 只，3 板块共 9 只候选，再过 6 条趋势闸；
熊市不碰个股，只买「RSRS 前 3 ∩ 120 日加权斜率前 3」交集里最强的板块 ETF。

**每月最后一个交易日收盘**决策：先卖出持仓，再买入新标的。

## 目录

```
latest_data/     唯一数据源（3 个 parquet）
backtest/        回测脚本与口径文档
live/            实盘信号生成
config/          配置
logs/            运行日志
```

`data/`、`scripts/`、`src/`、`tests/`、`docs/`、`reports/` 已全部移除，数据只保留 `latest_data/`。

## 策略口径

### 标的池
- **板块**：10 个 SPDR 行业 ETF
- **个股**：`members_daily.parquet` 中当日归属该板块的 S&P 500 成员（逐日 permno，2026 年每日约 503 只）

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
| 调仓时点 | 每月最后一个交易日，**当天收盘**先卖后买 |
| 持仓 | 1 个标的，100% 全仓 |
| 同票续持 | 不重复收费 |
| 止损 | 个股相对买入价 **−10%**；止损日在每月 **11 号前**可补买 1 次（不得同一只） |
| 牛市空仓 | 次一交易日起**逐日补选**，直到选出或到下一调仓日 |
| 成本 | 单边 5bp，滑点未建模 |

### 换手率折算
| 场景 | 当日 `turnover` 权重 |
|---|---|
| 回测 | **1.0** |
| 实盘（收盘前下单） | **0.48** |

## 使用

```powershell
# 回测
python backtest/backtest_benchmark.py --tag v1

# 实盘信号
python live/signal.py
python live/signal.py --hold CL --date 2026-11-13
```

产出：`reports/bt_v2/bt_<tag>/`（`nav_daily.csv`、`hold_daily.csv`、`holds.csv`、`stats.csv`）

## 回测结果（2021-12-31 ~ 2026-10-26）

| 指标 | 策略 | SPY |
|---|---|---|
| 累计收益 | **1517.24%** | 70.70% |
| 年化 | **78.35%** | 11.76% |
| 年化波动 | 36.09% | 17.25% |
| 夏普 | **2.1710** | 0.6816 |
| 最大回撤 | **−39.69%** | −24.50% |
| 交易 | 51 笔 / 止损 4 次 / 胜率 70.6% | — |

## 数据说明

| 文件 | 内容 |
|---|---|
| `latest_data/bars_daily.parquet` | 个股日频：行情、`turn20/turn120/turn_score`、`mcap`、均线、`adx14`、RSRS |
| `latest_data/etf_daily.parquet` | 板块 ETF 与 SPY：`rsrs20_40_z`、`slope120_weighted`、`bear_ratio21` |
| `latest_data/members_daily.parquet` | 逐日成分股与板块归属 |

**`latest_data/` 不可再生**（构建流程已随 `data/`、`scripts/` 一并移除），请自行做好备份。
