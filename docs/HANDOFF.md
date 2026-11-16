# 模拟盘交接文档（给接手开发的 AI / 工程师）

> 读这份文档的顺序建议：第 0 节 → 第 1 节（策略核心）→ 第 3 节（文件地图）→ 第 7 节（口径陷阱）→ 第 9 节（改需求去哪改）。
> 第 4、5 节是数据结构与数据契约，写代码时查。第 8 节是当前状态与待办。

---

## 0. 接手前必须知道的三件事

**① 这是研究用途的模拟盘，不是资管产品。** 单票满仓、不设止损、不做仓位管理、月频调仓。
任何"加风控/加仓位/加止损"的改动都会让实盘与回测不可比，必须先和需求方确认。

**② 有三处硬口径不能凭感觉改。** 改了它们，实盘就不再是回测那一套：

| 口径 | 定值 | 为什么不能动 |
|---|---|---|
| 换手率当日折价 | 回测里乘 0.48，**实盘不乘** | 实盘拿到的报价本身就是"截至 15:30 的累计值"，再乘一遍等于折两遍 |
| 市值门槛 | 500 亿美元，严格大于 | 阈值按"小盘换手率分母失真"这条经济理由定；100/200/300/500 四档回测收益**不单调**（500 最高、300 最差），按收益挑阈值就是过拟合 |
| RSRS 参数 | N=20 / M=40 / strict | 与板块层、个股层共用同一份实现，任一处不同都会让选股漂移 |

**③ 本机磁盘上的 `.py` 被公司加密软件加密**（文件头 `%TSD-Header-###%`）。
Python 进程能正常读（白名单解密），所以**本地运行不受影响**；但用普通方式复制文件、
上传到 GitHub 会变成乱码。要导出明文副本，必须把扩展名改成 `.txt` 再复制。
这一条只影响"往外交付"，不影响开发本身。

---

## 1. 策略核心

### 1.1 一句话

**先用 11 个 SPDR 板块 ETF 的 RSRS 选出最强的板块，再在该板块内按 20 日均换手率从高到低
找前 3 只，买第一只均线多头排列的；月末最后一个交易日收盘前 30 分钟换仓，持有一个月。**

### 1.2 决策链路（按执行顺序）

| 步骤 | 输入 | 规则 | 输出 | 代码 |
|---|---|---|---|---|
| 1. 取行情 | data 分支的 parquet | 最近 400 天 high/low/adj_close，11 只 ETF + SPY + 约 503 只成分股 | 三个宽表 | `signal.compute` 前半 |
| 2. 拼今日 bar | 实时报价（腾讯/新浪） | 现价 × 最近复权因子 → adj_close；盘中高低 → high/low | 尾部追加一行 | `signal._append_live_row` |
| 3. 板块层 RSRS | ETF 的 high/low | N=20 回归、M=40 标准化、strict 过滤 | 11 个板块的 z 值 | `rsrs_panel_fast` |
| 4. 全池换手率 | 19 天面板 + 当日累计 | `(sum19 + today) / 20`，有效天数 ≥10 | 每只票的 20 日均换手 | `turnover.build` |
| 5. 换手率上限 | 上一步 | 20 日均换手 > 20% 的票当期剔除 | 可排序池 | `signal.compute` |
| 6. 市值门槛 | 未复权价 × **当日真实股本** | < 500 亿美元的票当期剔除（取不到市值的**保留**）；股本按 快照 → 换手率反推 → SEC 三级兜底，逐票留来源 | 可排序池 | `mcap.gate` |
| 7. 板块下探 | 板块 RSRS 降序 | 最多看前 3 个板块；每板块内按换手率降序取前 3 只 | 至多 9 个候选 | `signal.compute` 循环 |
| 8. 多头排列 | 候选的 MA10/20/50 | MA10 > MA20 > MA50；遇到第一个满足的就买入并停止下探 | 1 只或空仓 | `signal.ma_of` |
| 9. 执行 | 15:30 现价 | 市价单、单票满仓、持有一个月、不设止损 | 持仓 | 人工 |

**注意步骤 7 的两个"3"**：板块看前 3、每板块看前 3，是回测定稿参数（`TURNMA_SECTORS=3`、
`TURNMA_TOP=3`），改任一个都会让实盘与回测脱钩。

**空仓是合法输出**：9 个候选都不满足多头排列 → 当期空仓，不动。回测里同样处理，
不是异常，不要"兜底买一只"。

### 1.3 关键公式

**RSRS（`scripts/sector_stock_rsrs.py::rsrs_panel_fast`）**

```
对最近 N=20 天的 (low, high) 做二元回归：high = α + β · low
raw = β × R²                      # 用 R² 惩罚拟合差的斜率
strict 过滤：R² < 0.5 或 |β| > 10 的位置置 NaN
z = (raw − mean(raw.shift(1)[-M:])) / std(raw.shift(1)[-M:])
```

⚠ **z-score 的均值/标准差用的是"不含当日"的历史序列**（`raw.shift(1)`），不是含当日的滚动窗口。
写成含当日会引入当日自身，分数整体被压平。另外 `M` 窗口至少要 5 个有效点，不足则记 0。

板块层与个股层用**同一个函数**，只是喂的数据不同（ETF 的 high/low vs 个股的 high/low）。

**换手率**

| 场景 | 公式 | 说明 |
|---|---|---|
| 回测 | `rolling(20, min_periods=10).mean()`，信号日当天 × **0.48** | 0.48 = 收盘前 30 分钟时点约占全日成交量的比例 |
| 实盘 | `(前 19 天完整值 + 当日累计值) / 20` | 当日累计值已经是 0.48 的量，**不能再乘** |

分母是**当日总股本**（腾讯报价的换手率字段 = 成交量 ÷ 总股本，与回测的 ws 面板同源）。

**市值（`mcap.share_sources` / `mcap.gate`）**

```
股本 = ① 快照：universe_latest.parquet 的 market_cap ÷ price       # Nasdaq 官方，日更，前 1000 名
       ② 反推：median(成交量 ÷ 当日换手率)，最近 5 个交易日         # 快照缺这个代码时用
       ③ SEC ：filed ≤ asof 的最后一条申报股本，超过 400 天算过期    # 前两级都没有时用
市值 = 当日未复权价 × 股本
判定 = 市值 > 500e8（严格大于）
```

用未复权价是必须的：复权价在拆股后会被缩放，市值会算错。

**为什么不再用 3 年中位数**（2026-11 改口径）：中位数反映的是「过去三年的平均股本」，回购/增发/
分拆之后就会偏离当期值。数据分支 503 只实测，500 亿门槛附近 14 只判定与真实市值相反
（DD 510 亿 → 实际 204 亿、CVNA 487 亿 → 798 亿、KMB 363 亿 → 672 亿）。
换手率反推口径本身没有丢，降级为「快照缺失时」的兜底，窗口从 3 年收到 **5 个交易日**
（PYPL 实测：3 年中位 978M / 20 日中位 853M / 5 日中位 831M，SEC 申报 830.66M，只有 5 日窗口
跟得上 2026-10-27 那次回购造成的股本跳变；全池与官方快照比对，5 日窗口 ≤2% 误差占 96.0%）。

**快照的 `asof` 是抓取日、按容器时区（UTC+8）记录**，会比 ET 交易日早 1 天左右；`mcap` 允许
超前 2 天，超前更多判为「来自未来的快照」并弃用（用 `--now` 复现历史时点会踩到）。

### 1.4 为什么用"换手率"而不是 RSRS 排序选个股

板块层已经用 RSRS 表达"动量方向"，个股层要的是**筹码活跃度**而不是再算一次动量
（同一因子在同板块内高度相关，排序近乎随机）。换手率高的票在板块上行期更容易被资金推动，
这是策略的原始假设。回测里选股相对板块 ETF 的累计超额约 +207%（500 亿档，36 期），
但这个数字**高度依赖少数几期**，见第 8 节。

### 1.5 收益结构（接手者必须知道的脆弱点）

500 亿档 36 期：最佳单期 2024-11 APP **+105.9%**，占全部对数收益的 **29%**；
前 3 期占 **61%**；剔掉最佳 1 期后，累计从 +1616% 掉到约 +503%。
**这不是实现缺陷，是策略本身的性质**。任何"提升稳定性"的改造都要先看它会不会把这几期削掉。

---

## 2. 运行链路

### 2.1 两个 job

| job | 触发 | 做什么 |
|---|---|---|
| `signal` | 4 条 cron（收盘前 30 分钟对应 UTC）+ 手动 | 判日 → 拉报价 → 算信号 → 落快照 → 推送 → 记账 → 写流水 → 回写 data |
| `data` | 收盘后（另一个 cron 组，实际由脚本自判） | 刷新成分股 → 新票补行情 → 更新日线 → 写入当日换手率 → 回写 data |

cron 只是**唤醒器**，是否真干活由 Python 判断（`calendar.occasion_for` +
`calendar.in_execution_window`）。原因：夏令时、早收日、Actions 自身漂移 5~30 分钟。

### 2.2 信号 job 的完整时序

```
_now(args)                       # --now 可复现任意时刻，缺省取系统时钟
  ↓
occasion_for(today_et)           # kind: rebalance / week / none；给出收盘时间与触发时刻
  ↓ 不在执行窗口 → finish(0, "noop")
already_pushed(kind, day)?       # 幂等键，防同一天推两次
  ↓ 已推过 → finish(0, "noop")
fetch_quotes(syms)               # 腾讯为主，新浪校验/兜底
  ↓ 报价不足 50% → finish(2, "abort")
describe_lag(...)                # 滞后 >45 分钟且非 --now 复现 → finish(2, "abort")
  ↓
signal.compute(asof, quotes)     # 第 1 节那条链路
  ↓
books.load_position() / mark_to_market / plan_action
  ↓
render_rebalance(...)            # 生成标题与 markdown 正文
  ↓
journal.snapshot(...)            # ★ 先落盘快照（推送失败也留痕）
  ↓
notify.send(...)                 # pushplus
  ↓ 失败 → finish(1, "error")
books.mark_pushed(...) / books.record(...)
  ↓
journal.finish(0, "ok", detail)  # 写一行流水
```

### 2.3 为什么日志必须先于推送

推送成功后如果进程崩溃，仓库里没有痕迹，就会出现"消息到了手机、却没有证据说明它怎么算出来的"。
所以快照先落盘，推送结果再回填 `decision.json` 的 `notify_ok`。

---

## 3. 文件地图

> 「依赖」列指"谁 import 它"。改动前先看依赖，别只改一处。

### 3.1 入口

| 文件 | 职责 | 关键符号 | 依赖 |
|---|---|---|---|
| `scripts/live_signal.py` | 信号 job 入口：判日、拉报价、算信号、渲染、快照、推送、记账 | `main` / `_run` / `render_rebalance` / `render_week` / `_pool_context` | CI 直接调用 |
| `scripts/live_audit.py` | 日志查询：`--list/--date/--why/--runs/--gaps/--replay/--stats` | `cmd_date` / `cmd_why` / `cmd_gaps` / `cmd_replay` | 人工、CI 可选 |
| `scripts/paper_close_update.py` | 收盘后更新：成分股刷新、新票补行情、日线增量、写当日换手率、**跑完自检** | `main` / `append_turnover` / `check_freshness` / `members` | CI 的 data job；自检不过就返回 1（Actions 标红），数据仍回写 |

### 3.2 模拟盘本体 `src/quant/live/`

| 文件 | 职责 | 关键符号 | 改动注意 |
|---|---|---|---|
| `calendar.py` | 交易日历、当日收盘时间（人工 > 联网 > 内置，冲突取更早）、调仓/周报判定、执行窗口 | `ET` / `session_close_et` / `close_utc` / `occasion_for` / `in_execution_window` / `is_trading_day` / `Occasion` | 内置假日表只覆盖 2024~2030，过期要补；`TA_MARKET_CLOSE_ET` 是人工覆盖口 |
| `quotes.py` | 腾讯/新浪批量报价：价、盘中高低、累计量、换手率、数据源时间戳 | `Quote` / `fetch_quotes` / `describe_lag` | 腾讯字段序号会变，`_resolve_turnover` 用恒等式自校验，别改成硬编码下标 |
| `turnover.py` | 20 日均换手率：历史面板 + 当日累计 | `TurnoverResult` / `build` / `load_history` / `median_of` | `TAIL_FACTOR` 固定 1.0，**不要**把回测的 0.48 抄进来 |
| `pool.py` | 候选池：SSGA 官方 holdings 刷新、调入调出日志、代码归一（`BRK.B`→`BRK-B`） | `load` / `refresh` / `RefreshResult` / `diff` / `recent_changes` / `normalize_symbol` | 刷新失败必须沿用旧快照，绝不中断信号 |
| `signal.py` | **策略核心**：板块 RSRS → 换手率 → 市值门槛 → 多头排列 → 下探 | `compute` / `Signal` / `Candidate` / `_append_live_row` / `ma_of` / `top_sectors` | 改排序逻辑只改这里；`pool_table` 是日志用的全池表，别删 |
| `mcap.py` | 500 亿美元市值门槛，市值用**当日真实股本**（快照 → 反推 → SEC） | `MIN_MCAP` / `share_sources` / `gate` / `Gate` | 三级兜底的**顺序不能调**：SEC 表有洞（CHTR 最后一条停在 2016、FOX/A 值是 1 股），只配当最后兜底 |
| `books.py` | 持仓、流水、幂等键；净值按"上一期实际收益"滚动 | `Position` / `load_position` / `plan_action` / `mark_to_market` / `record` / `already_pushed` | 「继续持有」也要记一行，否则净值曲线断档 |
| `notify.py` | pushplus 推送，2000 字上限，以响应体 `code` 判成功 | `send` / `token` | 失败要打 `::error::` 让 job 变红 |
| `journal.py` | 运行流水 + 决策快照 | `Journal` / `run_id` / `snapshot` / `finish` / `patch_decision` / `all_runs` | 先快照后推送；tmp+rename；路径存 posix |

### 3.3 数据层 `src/quant/data/`

| 文件 | 职责 | 关键符号 |
|---|---|---|
| `store.py` | parquet 读写、宽表加载、交易日历、状态文件 | `data_root` / `bars_dir` / `read_bars` / `upsert_bars` / `load_panel` / `list_symbols` / `trading_days` / `resample_dates` / `duckdb_conn` |
| `fetch.py` | 回填与每日增量；重叠区间价格漂移 >0.1% 判为拆股/分红，自动全量重刷该只 | `backfill` / `update` |
| `universe.py` | 全市场快照（Nasdaq screener / iShares IWB），提供 `market_cap`、`price` | `build` / `latest_file` |
| `validate.py` | 数据校验 | — |
| `sources/*.py` | 数据源适配：`yahoo` → `alpaca` → `tiingo` 降级链 | `base.BarSource` / `registry.build_chain` |

### 3.4 模拟盘的间接依赖（看起来像回测脚本，但必须留着）

| 文件 | 为什么在这里 |
|---|---|
| `scripts/sector_stock_rsrs.py` | `signal.py` 从它导入 `rsrs_panel_fast`。它同时是回测脚本（`--min-mcap`、`--pool-mode pit` 都在这里） |
| `scripts/sector_rsrs.py` | 上一行的依赖（`anchors` / `perf`），也是板块 RSRS 的原始实现 |
| `scripts/build_sector_holdings.py` | `pool.refresh()` 调用它抓 SSGA 的 11 个 xlsx |
| `scripts/archive_data_year.py` | data 分支按年归档，控制仓库体积 |

⚠ 如果要把 `rsrs_panel_fast` 抽到 `quant/live/` 或 `quant/factors/`，必须先用同一份数据
逐值比对（原实现注释里写了"已逐值比对"），否则回测和实盘会悄悄分叉。

### 3.5 配置与 CI

| 文件 | 说明 |
|---|---|
| `config/settings.yaml` | 数据源降级链、批量参数、路径。`paths.data` 会被 `TA_DATA_ROOT` 覆盖 |
| `config/ref/us_sector_holdings.csv` | 成分股兜底快照；data 分支有刷新版时优先用那份（`pool.pool_path`） |
| `.github/workflows/paper_trade.yml` | 两个 job、cron、Secret、`always()` 回写、push 重试 |

---

## 4. 数据结构

```python
Signal        # compute 的返回值，一次决策的全部产物
  asof, sector, pick, price, adj_price
  sector_scores: pd.Series      # 11 个板块的 RSRS，降序
  candidates: list[Candidate]   # 实际下探到的票（最多 9 个）
  pool_table: pd.DataFrame      # 全池（约 500 行），日志用，含 reason
  factors: dict                 # 未复权价 → adj_close 的复权因子
  notes: list[str]              # 推送里的备注
  pool_size, pool_as_of, min_mcap

Candidate     # 下探过程中的一个候选
  symbol, sector, sector_rank, turnover, ma{10,20,50}, ok, why

Quote         # 一条实时报价
  symbol, price, prev_close, high, low, volume,
  turnover_pct(%), total_shares, ts(ET), src(tencent|sina)

Position      # 账本里的持仓
  symbol, sector, entry_date, entry_price(未复权), entry_adj(复权),
  nav, last_mark_date, last_mark_adj, moves, history

Gate          # 市值门槛结果
  kept, dropped(市值不足), unknown(取不到市值→保留), caps(每只市值),
  srcs(每只的市值来源 snapshot/ws/sec), snapshot_as_of, notes

TurnoverResult
  mean(20日均值), today(当日), days(有效天数), notes, asof

RefreshResult # 候选池刷新
  ok, as_of, n_symbols, added, removed, moved, new_symbols(需补行情), message

Occasion      # 一次推送场合
  kind(rebalance|week|none), date, close_et, trigger_utc, reason
```

`pool_table` 的 `reason` 取值与优先级（一只票只记**最先命中**的那个）：
`no_local_data` → `no_quote` → `small_mcap` → `no_turnover` → `turn_cap` →
`not_in_top3` → `stack_fail` → `not_selected` → `""`（入选）。

---

## 5. 数据契约（`data` 分支，workflow 里 checkout 到 `./data`）

```
curated/bars_daily/symbol=<SYM>/xxx.parquet   日线：date/open/high/low/close/adj_close/volume/dividends/stock_splits
curated/fundamentals/turnover_daily.parquet   换手率：symbol/date/turnover(小数)/src
ref/us_sector_holdings.csv                    成分股快照：symbol/sector_etf/... /as_of
ref/index_changes.csv                         调仓日志：date/action/symbol/sector_etf/note
ref/history/us_sector_holdings_<asof>.csv     每次刷新留档
meta/universe/universe_latest.parquet         市值/价格快照（Daily，市值的主来源）
curated/fundamentals/shares_outstanding.parquet  SEC 申报股本（仅市值最后兜底；表有洞，别当主源）
state/live/positions.json                     当前持仓
state/live/pushed.json                        幂等键
state/live/ledger.csv                         历史流水
state/live/runs/YYYY-MM.jsonl                 运行流水（每次一行）
state/live/decisions/YYYY-MM/<asof>_<run_id>/ 决策快照
```

环境变量：`TA_DATA_ROOT`（数据根）、`TA_STATE_DIR`（日志根）、`TA_RUN_ID`（CI 注入）、
`TA_MARKET_CLOSE_ET`（收盘时间人工覆盖）、`PUSHPLUS_TOKEN`。

**`turnover` 是小数不是百分数**（0.0086 = 0.86%）。回测里 `成交量 ÷ 换手率 = 股本` 依赖这个单位，
换成百分数会让市值全部偏小 100 倍。

---

## 6. 日志与回溯

完整说明在 `docs/LIVE_LOG.md`。接手时只需要记住四条：

1. 每次运行都写一行 `runs/*.jsonl`，**包括中止和空跑**；所以"查不到记录"本身指向"没跑起来"。
2. 每次成功算完都落一份 `decisions/` 快照，事后能回答"为什么不是 X"。
3. Actions 的 run log / artifact **不作回溯依据**（90 天过期、不可查询）。
4. `--replay` 用快照里的报价重算一遍并比对，用来发现口径漂移；不一致先看 `meta.json` 的
   `params` 和 `git_sha`。

---

## 7. 口径陷阱清单

| # | 现象 | 原因 | 怎么处理 |
|---|---|---|---|
| 1 | 实盘选股比回测偏保守 | 实盘的成分股是**当前快照**，回测是 `--pool-mode pit` 按公告重建的逐日名单 | 只对"同月是否选同一只"这种粒度，不对逐日曲线 |
| 2 | 实盘与回测在 500 亿门槛附近判定不一致 | 回测的 `--min-mcap` 用全样本中位数或当日快照（含前视），实盘用当日真实股本（快照/反推/SEC） | 503 只实测：500 亿档有 **14 只**判定翻转（DD、CVNA、KMB 等拆股/股本变动票）。引用历史收益时必须说明；要对齐就把回测的 `--min-mcap` 股本也换成同一套 |
| 3 | 实盘换手率比回测低一半 | 把回测的 `--turn-tail 0.48` 也乘进了实盘 | 实盘 `TAIL_FACTOR=1.0`，不要再折 |
| 4 | 某只票在回测里有、实盘没有 | `BRK-B`/`BF-B` 这类代码在行情源缺失，实盘会剔除并在推送里列出 | 补行情；不要静默丢弃（推送里必须露出来） |
| 5 | 净值对不上 | "继续持有"的期没记流水，或周报重复记收益 | 净值只在**调仓**时滚一次；周报只报不平账 |
| 6 | 换手率全为 0 / 市值全为 NaN | `turnover_daily.parquet` 的单位或日期没对齐 | 检查 `turnover` 是小数、`date` 是交易日 |
| 7 | 拆股后收益算错 | 用了未复权价算收益 | 收益一律用 `adj` 口径；未复权价只用于展示成本 |
| 8 | 15:30 决策但盘中高低点偏窄 | 当天的 bar 是实时报价合成的，缺收盘前 30 分钟的振幅 | 已知偏差，RSRS 排序边界附近可能翻转，不修 |
| 9 | 某天完全没信号 | 可能是 `noop`（非调仓日/不在窗口/已推过）也可能是 `abort`（报价不足/滞后） | `python -m scripts.live_audit --runs --since <日期>` 看 `status` 与 `reason` |
| 10 | 本地跑 CLI 报 `UnicodeEncodeError: 'gbk'` | Windows 控制台默认 GBK，推送正文里有 `✓` | `set PYTHONUTF8=1`；CI 是 Linux 不受影响 |
| 11 | 复制出去的 `.py` 是乱码 | 公司加密软件按扩展名加密 | 改成 `.txt` 再复制（见第 0 节） |
| 12 | 早收日（感恩节次日/7-3/平安夜）错过窗口 | 时钟没按当日实际收盘算 | 一律走 `calendar.session_close_et`，不要写死 16:00 ET |
| 13 | 市值突然整批变了 | 快照 `asof` 是抓取日（容器 UTC+8），比 ET 交易日早 1 天；或快照没刷新 | 看推送里的「市值来源：快照 N / 反推 N（快照 as of …）」；`asof` 超前 >2 天会被判为未来快照而弃用 |
| 14 | 信号用的是几天前的收盘 | data job 挂了，bars 面板没更新，而报价是实时的 | 两道：①data job 末尾 `check_freshness` 自检（ETF 必须 100%、成分股/换手率 ≥95% 有当日数据，否则运行返回 1 标红）；②信号层 `trading_lag_days` 兜底（缺 1 天告警、≥2 天中止） |
| 15 | 选出一只完全不相干的票 | 把一份**不属于信号日**的报价当成当日 bar 追加：面板尾部多一根"昨天的复制品"或一个"零区间点"，RSRS 的 R² 被拉向 1、β 变形，板块排序整体改写（实测 11-13 的 XLP 1.06 会被算成 0.44，MNST 变 APP） | `_append_live_row` 三级降级：①日线已有当日 bar → 用日线；②没有但有当日盘中报价（`Quote.ts` 的 ET 日期 == 信号日 且 ≥50% 带 high>low）→ 用报价合成；③都没有 → **退回最近一根真实日线**，口径日随之改成那天并写进推送 |

---

## 8. 当前状态与待办

### 8.1 已就绪

- 策略链路完整：板块 RSRS → 换手率 → 500 亿市值门槛 → 多头排列 → 下探 → 推送 → 记账。
- 日志系统完整：运行流水（含全部失败路径）+ 决策快照 + 查询工具 + 完整文档。
- 回测口径：PIT 成分名单、换手率补齐、市值门槛，36 期（2023-11 ~ 2026-10）
  累计 +1616.1%、夏普 3.024、最大回撤 −37.2%（500 亿档，`stats.csv` 日度净值口径）。

### 8.2 未完成 / 已知问题

| 事项 | 状态 |
|---|---|
| 模拟盘尚未真实跑过 | `state/live` 下没有账本，首期建仓等 2026-11-30 |
| 回测的股本估计含前视 | 实盘已改（市值改用当日真实股本），回测未改；要对齐需把回测 `--min-mcap` 的股本换成同一套后重跑五档。现状是两边在门槛附近 14 只票上判定不同 |
| data job 断档会静默用旧收盘 | 已修：data job 跑完自检 `check_freshness`（ETF 严格、成分股与换手率 ≥95%），不达标返回 1 让 Actions 当天变红；信号层 `trading_lag_days` 再兜一层（缺 ≥2 天中止） |
| 报价不属于信号日时会造出一根假 bar | 已修：`_append_live_row` 三级降级（日线 → 当日盘中报价 → 退回最近日线），口径日一律跟着数据走；`Signal.asof` 是实际口径日、`requested_asof` 是请求日。调仓日若口径日 ≠ 请求日则中止（不按旧口径下单记账），周报只告警 |
| 收益高度集中 | 2024-11 APP 单独占 29% 对数收益，前 3 期占 61%。这不是 bug，但任何"提升稳定性"的改造都要先量化它对这几期的影响 |
| `--gaps` 只查月频调仓 | 周报不查（漏发影响小） |
| `--replay` 的成员名单用的是当前快照 | 只能验证"同输入同输出"，不能还原"当时的名单" |
| 门槛会改变下探路径 | 2024-12-31 那期在 500 亿下整期空仓（XLB 前几名全被筛掉）。引用收益时必须说明这一点 |

---

## 9. 改需求时该动哪里

| 想做的事 | 改哪个文件 | 连带要改/确认 |
|---|---|---|
| 换板块因子（RSRS → 别的） | `signal.compute` 的板块层 + `rsrs_panel_fast` | 回测同一函数；日志 `sectors.csv` 的列 |
| 换个股排序因子（换手率 → 别的） | `signal.compute` 的 `turn_ok` 排序 | `turnover.py` 可能需要新面板；`pool_table` 的列要加 |
| 调市值门槛 | `mcap.MIN_MCAP` | **同时**改回测 `--min-mcap` 并重跑，否则两边不是一套；文档三处要同步（PAPER_TRADING/HANDOFF/PROGRESS） |
| 调下探深度（3×3） | `signal.TURNMA_TOP` / `TURNMA_SECTORS` | 回测同名参数 |
| 加止损 / 加仓位 | 需要改 `books.record` 与回测引擎 | 回测必须同步，否则不可比；先跟需求方确认 |
| 加一种推送内容 | `live_signal.render_rebalance` | 推送 2000 字上限，超了会截断；`push.md` 存的是截断后的 |
| 加一个查询命令 | `live_audit.py` 加 `cmd_xxx` + argparse | 保持退出码约定（0 正常 / 1 发现问题 / 2 参数或数据问题） |
| 换交易日历源 | `calendar.online_close_et` | 合并规则"取更早的收盘"不能改 |
| 换数据源（行情） | `data/sources/*` | `store.load_panel` 的字段契约不能变 |
| 加一个新的后台 job | `.github/workflows/paper_trade.yml` | 记得 `TA_RUN_ID`、`concurrency`、回写重试 |

---

## 10. 禁止事项

1. **不要用未来数据做判断**。任何"当期"字段只能用截至信号日的信息（市值门槛的快照已按 `asof`
   拦未来快照），不要再引入新的前视。
2. **不要在没有随机对照的情况下声张 alpha**。这个项目的验收标准是"优于随机/优于基准"，
   不是"回测曲线好看"。
3. **不要按收益挑参数**。市值门槛的阈值扫描已经证明收益不单调（500 亿最好、300 亿最差），
   挑最好看的那一档就是过拟合。
4. **不要为了"看起来更好"改口径**。换手率折价、股本反推、复权口径这三处一旦改动，
   历史结论全部作废。
5. **不要删 `decisions/` 快照**。它是唯一的决策证据，`--replay` 依赖它。
6. **不要在 `signal.compute` 里写盘**。落盘是 `journal` 的职责，混在一起会让"算信号"和
   "留痕"互相绑架（比如落盘失败就不出信号）。
7. **不要静默丢弃数据**。拿不到报价、没有本地行情、取不到市值，都要在推送和日志里露出来。

---

## 11. 本地复现

```powershell
# 依赖
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

# 指向数据与日志（data 分支 checkout 到 ./data 时可省略）
$env:TA_DATA_ROOT = "d:\path\to\data"
$env:TA_STATE_DIR = "d:\path\to\data\state\live"
$env:PYTHONUTF8   = "1"

# 空跑一次（不推送、不记账，但会写本地日志）
python -m scripts.live_signal --dry-run --force --kind rebalance --now "2026-11-10 15:30"

# 看这次留下的日志
python -m scripts.live_audit --list
python -m scripts.live_audit --date 2026-11-10
python -m scripts.live_audit --why AIG --date 2026-11-10
python -m scripts.live_audit --replay --date 2026-11-10
```

`--now` 复现历史时点时会用**当前**报价当那天的 bar，信号只能用来验证链路是否跑通，
不代表当时的真实决策。真实回溯看 `decisions/` 快照。

---

## 12. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/PAPER_TRADING.md` | 模拟盘运行机制、候选池、与回测的口径差异、上线配置 |
| `docs/LIVE_LOG.md` | 日志系统的目录、字段、命令、13 条注意事项、排查顺序 |
| `docs/HANDOFF.md` | 本文：策略核心、文件地图、口径陷阱 |
| `README.md` | 仓库概览与本机运行 | 
