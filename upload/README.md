# upload：数据更新（与基板隔离）

`upload/` 是**独立的一套数据**，由 `update.py` 从外部源重新生成，**全程只读 `latest_data`，不写入**。

## 为什么要隔离

`latest_data/` 是不可再生的基板数据（构建流程已删除）。日常更新如果直接改它，一旦出错就无法回滚。
所以更新产物一律生成在 `upload/`，实盘（`live/`）改读这里。

## 数据源：全部来自新浪

| 接口 | 提供 | 频率 |
|---|---|---|
| `hq.sinajs.cn/list=gb_xxx` | 实时快照：**总股本**（字段 [19]）、总市值（[12]）、现价 | 每日一次，批量 |
| `US_MinKService.getDailyK` | 日线 OHLCV + **成交额**（`a` 字段），历史到 1984 年 | 每日增量 |

**为什么不用 Yahoo / 东财**：
- Yahoo 响应不稳定
- 东财限频

**为什么不用新浪的 JSONP 报价接口**：`US_StockService.*` 全部返回 `Service not valid`，新浪没有股本类接口，股本只能走 `hq.sinajs.cn`。

## 口径

### 价格：后复权

新浪返回的是**拆股调整价**（split-adjusted），不含分红调整；而 `latest_data` 是**后复权**（含分红）。

两者比值 `k` 因票而异：

| 票 | k | 原因 |
|---|---|---|
| MSFT | 1.00 | 无拆股 |
| AAPL | 4.00 | 2020 年 4:1 拆股 |
| NVDA | 40.0 | 多次拆股 |
| T | 1.31 | 分拆华纳兄弟 |

所以每票算一次 `k = bars.close[末日] / sina.close[末日]`，新增行 `close = sina × k`，保证接点连续。

**k 的漂移**：无拆股票全期极差 0.3~6.7%（分红累积）。短期更新无影响；若某票发生拆股，k 会在拆股日跳变（NVDA 极差 3909%），需重算。

### 市值：股本 × 价格

```
mcap = 实时股本 × 新浪当日收盘
```

**已验证**（2026-11-13 同日对比 10 只票）：

| 票 | 股本×价(亿$) | latest_data(亿$) | 误差 |
|---|---|---|---|
| AAPL | 51,773.8 | 51,773.8 | **0.00%** |
| MSFT | 38,904.3 | 38,904.4 | **-0.00%** |
| JPM | 9,193.5 | 9,193.5 | **0.00%** |
| XOM | 6,958.7 | 6,958.7 | **0.00%** |
| NVDA | 39,246.8 | 39,254.9 | -0.02% |
| PEP | 1,986.5 | 1,982.0 | +0.23% |

**误差中位 0.00%，最大 0.23%** —— 不需要反推股本，也不用价格递推，直接是真实值。

### 成交额

新浪 `a` 字段，交易所口径的真实成交额（不是估算）。

## 输出

`upload/` 下生成三个文件，结构与 `latest_data` 完全一致：

```
upload/
├── bars_daily.parquet       date >= 2025-12-31
├── etf_daily.parquet
├── members_daily.parquet
├── adjust_k.csv             每票的复权比例（持久化）
└── permno_map.csv           新增票的 permno 分配（待实现）
```

**只保留 2025-12-31 之后的行**（你指定的起点）。指标计算时临时借用 `latest_data` 的 2024-06 起点作为预热窗口，算完即丢弃。

## 用法

```powershell
python upload/update.py --limit 10     # 小样本试跑（10 只）
python upload/update.py --verify       # 只算 k 并抽样，不写盘
python upload/update.py                # 全量
```

## 三个脚本都是增量的

| 脚本 | 做什么 |
|---|---|
| `update_members.py` | 抓 S&P 500 名单；老票沿用 permno，新票从 `1_000_001` 起分配并写 `permno_map.csv` |
| `update.py` | 按 permno_map 拉日线与股本，接 `latest_data` 的复权尺度算 `k`，重算全部指标 |
| `update_etf.py` | 12 个 ETF 日线与 RSRS；合并后统一重算 raw/z，保证两列同源 |

一键执行（顺序不能乱）：`python upload/update_all.py`

## 已知限制

| # | 问题 | 现状 |
|---|---|---|
| 1 | 失败票 | 只打印到控制台，未落盘重试；实际每天 0~2 只 |
| 2 | 新浪代码写法 | 连字符要转点号（`BRK-B` → `BRK.B`），`sina_sym()` 已处理 |
| 3 | 股本缺失 | `BRK.B` / `BF.B` 快照为空，用 `latest_data` 末日 `mcap/close` 反推 |
| 4 | ETF 斜率 | `slope120_weighted` 新增段是对数回归近似，重叠区沿用原值 |

## 与实盘的衔接

`live/signal.py` 读 `upload/`；`backtest/` 读 `latest_data/`。

计划给 `signal.py` 加 `--data-dir` 参数，默认 `latest_data`，实盘传 `upload`。
