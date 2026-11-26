# latest_data 数据说明

**这是本项目的基板数据，唯一数据源。** 回测（`backtest/`）与实盘（`live/`）都只读这里，不再有上游构建流程。

```
latest_data/
├── bars_daily.parquet     个股日频面板（834,191 × 40）
├── etf_daily.parquet      板块 ETF 与 SPY 日频（17,688 × 18）
└── members_daily.parquet  逐日成分股与板块归属（742,109 × 4）
```

统一时间范围：**2021-01-04 ~ 2026-11-13**

---

## 1. bars_daily.parquet

个股日频面板。**主键 = `permno` + `date`**（615 只 permno / 613 个 symbol）。

### 1.1 标识与行情

| 字段 | 类型 | 含义 | 空值 |
|---|---|---|---|
| `permno` | int64 | CRSP 永久证券编号，同一公司改名不变 | 0 |
| `date` | datetime | 交易日 | 0 |
| `symbol` | str | 当期股票代码（改名票用当期代码） | 0 |
| `sector_etf` | str | 所属板块 ETF 代码，如 `XLK` | 882 |
| `in_index` | bool | 当日是否在册（S&P 500 成分） | 0 |
| `open/high/low/close` | float64 | **后复权** OHLC，单位美元 | 17~19 |

> `close` 为后复权价（`DlyPrc × 累计复权因子`）。**不同票的复权基准不同**，跨票比较绝对价格无意义，只能比较收益率。

### 1.2 成交与市值

| 字段 | 类型 | 含义 | 空值 |
|---|---|---|---|
| `amount` | float64 | 当日成交额，**美元** | 1,098 |
| `mcap` | float64 | 当日**总市值**，美元 | 1,078 |
| `turnover` | float64 | 日换手率 = `amount / mcap` | 1,215 |
| `turn20` | float64 | 20 日均换手率（简单均值，≥10 个有效日） | 6,729 |
| `turn120` | float64 | 120 日均换手率（简单均值，≥60 个有效日） | 37,229 |
| `turn_score` | float64 | **排序分** = `turn20² / turn120` | 37,229 |

> `turn_score` 是策略的选股排序键：分子要求「本身够活跃」，分母要求「比自身半年常态更活跃」。
> 注意：回测口径下当日权重为 1.0（简单均值）；实盘若在收盘前下单，当日 `turnover` 需按 **0.48** 折算。

### 1.3 技术指标

| 字段 | 类型 | 含义 | 空值 |
|---|---|---|---|
| `ma20/ma50/ma60/ma200` | float64 | 简单移动均线 | 264~6,700 |
| `slope20_5d` | float64 | MA20 的 5 日斜率 | 409 |
| `slope60_5d` | float64 | MA60 的 5 日斜率 | 1,035 |
| `slope20_10d` | float64 | MA20 的 10 日斜率 | 561 |
| `adx14` | float64 | ADX(14) 趋势强度 | 781 |
| `plus_di14` / `minus_di14` | float64 | +DI / −DI | 380 |
| `rsrs20_40_raw` | float64 | RSRS 原值 = β × R²（N=20） | 0 |
| `rsrs20_40_z` | float64 | RSRS 标准分（M=40，用不含当日的历史值） | 1,811 |

### 1.4 状态列（`*_state`）

所有指标都配一个 `int8` 状态列，取值 **0 = 正常 / 1 = 样本不足 / 2 = 意外缺失**：

`ma20_state`、`ma50_state`、`ma60_state`、`ma200_state`、
`slope20_5d_state`、`slope60_5d_state`、`slope20_10d_state`、
`adx14_state`、`rsrs20_40_raw_state`、`rsrs20_40_z_state`、
`turn20_state`、`turn120_state`、`turn_score_state`

> 用途：区分「数据不够所以是 NaN」（state=1，正常）与「本该有值却缺了」（state=2，异常）。

### 1.5 空值成因（均已核实，非数据缺陷）

| 成因 | 说明 |
|---|---|
| `SW`(permno 25146) 的 881 行 | 合并前的合成占位行，不补 |
| 分拆上市前 | 如 GEHC 2023-01-04 前、SOLS 2025-10-30 前 |
| 退市日 | 如 TWTR 2022-10-28、ATVI 2023-10-13、SIVB 2023-03-13 |
| 零成交日 | 源端 `TURNOVER=0`，成交额本身不存在 |

---

## 2. etf_daily.parquet

板块 ETF 与 SPY。**主键 = `etf_symbol` + `date`**。

包含 **12 个标的**：`SPY` + 11 个板块 ETF（`XLK XLF XLV XLY XLP XLE XLI XLB XLU XLC XLRE`）。

> 策略实际只用 **10 个**（不含 `XLRE` 房地产），`XLRE` 数据保留备查。

| 字段 | 类型 | 含义 |
|---|---|---|
| `date` / `etf_symbol` | — | 主键 |
| `open/high/low/close` | float64 | 后复权 OHLC |
| `volume` | float64 | 成交量（股） |
| `amount` | float64 | 成交额（美元） |
| `rsrs20_40_raw` / `rsrs20_40_z` | float64 | RSRS 原值与标准分 —— **板块排序用这个** |
| `slope120_weighted` | float64 | 120 日加权斜率 —— 熊市选 ETF 时与 RSRS 取交集 |
| `ma200` | float64 | 200 日均线 |
| `bear_ratio21` | float64 | 过去 21 日中收盘低于 MA200 的**占比** |
| `is_bull` | boolean | `bear_ratio21 <= 0.5` 时为 True |
| `*_state` | int8 | 同上，0/1/2 |

> ⚠️ **`bear_ratio21` 与 `is_bull` 只有 SPY 有值**（非空率 8.33%，其余 11 个 ETF 全为空）。
> 牛熊判定必须取 `etf_symbol == 'SPY'` 那一行：
> ```python
> spy = etf[(etf.etf_symbol == "SPY") & (etf.date == dt)].iloc[0]
> is_bear = spy.bear_ratio21 > 0.5
> ```

---

## 3. members_daily.parquet

**逐日**成分股与板块归属。主键 = `date` + `permno`。

| 字段 | 类型 | 含义 |
|---|---|---|
| `date` | datetime | 交易日 |
| `permno` | int64 | 证券编号 |
| `sector_etf` | str | 所属板块 ETF 代码 |
| `symbol` | str | 当期代码 |

每日约 **503 只**（2021 年 505，2022–2026 年 503）。882 行 `sector_etf` 为空（个别票无板块归属）。

> **用途**：确定「某天某板块有哪些成分股」。这是逐日变动的，不是静态快照。

---

## 4. 常用取数方式

```python
import pandas as pd

D = "latest_data"

# 单只票的时间序列
bars = pd.read_parquet(f"{D}/bars_daily.parquet")
aapl = bars[bars.symbol == "AAPL"].set_index("date").sort_index()

# 某日全市场快照
snap = bars[bars.date == "2026-11-13"]

# 某日某板块的成分股
mem = pd.read_parquet(f"{D}/members_daily.parquet")
codes = mem[(mem.date == "2026-11-13") & (mem.sector_etf == "XLK")].permno

# 板块 RSRS 排名
etf = pd.read_parquet(f"{D}/etf_daily.parquet")
sec = (etf[(etf.date == "2026-11-13") & (etf.etf_symbol != "SPY")]
       .dropna(subset=["rsrs20_40_z"])
       .nlargest(3, "rsrs20_40_z")[["etf_symbol", "rsrs20_40_z"]])

# 牛熊判定
spy = etf[(etf.etf_symbol == "SPY") & (etf.date == "2026-11-13")].iloc[0]
is_bear = spy.bear_ratio21 > 0.5

# 9 只候选池（牛市）
c = snap[snap.permno.isin(codes)]
c = c[c.turn20.notna() & (c.turn20 <= 0.20) & c.turn120.notna()]
c = c[c.mcap.notna() & (c.mcap > 500e8)]
top3 = c.nlargest(3, "turn_score")
```

---

## 5. 使用注意

1. **单位**：`amount`、`mcap`、`amount` 均为**美元**；`turnover`、`turn20`、`turn120` 是**小数**（0.0068 = 0.68%）。
2. **价格是后复权**，只能算收益率，不能直接当交易价。
3. **`permno` 才是主键**，`symbol` 会随改名变化（如 `CDAY` → `DAY`、`MMC` → `MRSH`）。
4. **逐日成分股**：不要用 `symbol` 去匹配历史，要用 `permno`。
5. **`state` 列**：取值 2 才是真正的异常缺失，取值 1 是样本不足。
6. **数据不可再生**：上游构建流程已移除，改数据前请先备份到仓库外。

---

## 6. 数据质量

| 检查项 | 结果 |
|---|---|
| `amount ≤ 0` | 0 行 |
| `mcap ≤ 0` | 0 行 |
| `amount / mcap` 中位 | 0.00685（日换手 0.69%，合理） |
| 市值与价格一致性（段内） | 99.9966% 的行「市值涨跌幅 = 价格涨跌幅」 |
| 已修正的系统性错误 | 单位错误（×1000/÷10/÷25/÷9400）、流通 vs 总股本口径（CVNA/ABNB）、整段倍数错误（CRH/CTVA/COIN/ECHO/APO/XYL/DD/TTD/PODD/HOOD/APP/FMC）、单点跳变（PKG/KLAC/BKNG/HOLX/DELL/COP/CRWD/MNST/OMC/BDX/HON/HBAN），合计约 1.3 万行 |
