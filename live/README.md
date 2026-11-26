# 实盘信号

脚本：`live/signal.py`　数据：`latest_data/`（唯一数据源）

与 `backtest/backtest_benchmark.py` **同一套口径**，只是把回测循环换成"算当期信号"。

## 运行

```powershell
python live/signal.py                 # 用数据里最新交易日
python live/signal.py --date 2026-11-13
python live/signal.py --hold CL       # 带上当前持仓，输出换仓指令
python live/signal.py --json          # 机器可读
```

## 输出内容

1. **牛熊状态**：SPY 收盘低于 MA200 的占比（>50% 判熊）
2. **板块 RSRS 排名**：10 个板块 ETF 的 `rsrs20_40_z`，标注前 3
3. **候选池**：牛市时 9 只（3 板块 × 3 只），逐只显示是否通过 6 条趋势闸及卡在哪条
4. **最终指令**：买哪只 / 空仓 / 继续持有

## 口径（与 backtest 完全一致）

| 项 | 规则 |
|---|---|
| 调仓时点 | 每月最后一个交易日，**当天收盘**先卖后买 |
| 板块池 | 10 个 SPDR ETF，不含 XLRE |
| 板块层 | RSRS 取前 3 |
| 个股层 | 每板块 `turn20²/turn120` 取前 3 |
| 硬门槛 | `turn20 ≤ 20%`、`turn120` 有效、`mcap > 500 亿美元` |
| 趋势闸 | MA20>MA60、MA20 的 10 日斜率>0、MA60 的 5 日斜率>0、close>MA50、MA50>MA200、ADX>10（全 AND） |
| 熊市 | 买 ETF（RSRS 前3 ∩ 斜率前3），交集为空则空仓，**不补选** |
| 牛市空仓 | 次一交易日起**逐日补选** |
| 止损 | 个股 −10%，11 号前可补买 1 次（不同票） |

## 与回测的唯一差异：换手率折算

| 场景 | 当日 `turnover` 权重 |
|---|---|
| **回测** | **1.0**（当日数据完整） |
| **实盘** | **0.48**（收盘前下单，当日成交额未走完） |

实盘若在收盘后运行，当日数据已完整，可按 1.0 使用；若在盘中预估，需按 0.48 折算。
当前脚本读的是 `latest_data` 里的已结算数据，等于 1.0。

## 示例输出（2026-11-13）

```
信号日: 2026-11-13   状态: 牛市  (SPY 跌破MA200占比 0.0)

板块 RSRS 排名:
   1. XLP   rsrs_z= 1.0009  slope120=0.0008 ←入选
   2. XLF   rsrs_z= 0.6907  slope120=0.0002 ←入选
   3. XLC   rsrs_z= 0.6186  slope120=-0.0004 ←入选
   ...

候选池 9 只:
  XLP  TGT     close=  165.90 turn20=0.0080 mcap=  733.5亿USD adx=20.52  FAIL MA20>MA60
  XLP  CL      close=  102.06 turn20=0.0060 mcap=  792.3亿USD adx=39.37  PASS
  XLF  AJG     close=  285.51 turn20=0.0072 mcap=  725.5亿USD adx=34.86  PASS
  ...

>>> 买入 CL（XLP）
    依据: 9只候选池中第一只通过趋势闸
```

## 数据依赖

`latest_data/` 下的三个文件，全部为已修复版本：

| 文件 | 内容 |
|---|---|
| `bars_daily.parquet` | 个股日频：行情、`turn20/turn120/turn_score`、`mcap`、均线、`adx14` |
| `etf_daily.parquet` | 板块 ETF 与 SPY：`rsrs20_40_z`、`slope120_weighted`、`bear_ratio21` |
| `members_daily.parquet` | 逐日成分股与板块归属 |

**注意**：`data/` 目录已删除，`latest_data/` 是唯一数据源，不再有重建流程。
