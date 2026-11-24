# 模拟盘（GitHub Actions + pushplus）

定稿策略：板块 → 个股双层 RSRS。牛市里板块内按 `turn20²/turn120` 取前 3，要求 strong 趋势五闸
（MA20>MA60、MA20 的 10 日与 MA60 的 5 日斜率为正、收盘>MA50、MA50>MA200、ADX(14)>10），
最多下探 3 个板块；
熊市完全不碰个股，只买「板块 RSRS 前 3 ∩ 120 日加权斜率前 3」里最强的那只 ETF，RSRS ≤0 就空仓。
月末最后一个交易日**收盘前 30 分钟**决策，单票满仓；**收盘价相对买入价跌 10% 当日收盘割肉**，
割肉日在该自然月 11 号之前则次日起逐日重选（排除刚割的那只），否则空仓到下一个调仓日。
调仓日空仓时，牛市在其后每个交易日补选一次，直到下一个调仓日。

定稿参数（与回测产出一一对应，`signal.py` 的常量必须和这份命令逐项对齐）：

```
--n 20 --m 40 --pick turnma --turnma-top 3 --turnma-sectors 3
--trend-mode strong --trend-stack 20,60 --slope-mas 20:10,60:5
--trend-above 50 --trend-cross 50,200 --adx-min 10
--turn-rank-mode ratio --turn-cap 0.20 --turn-tail 0.48 --bull-refill
--min-mcap 500 --exclude-sectors XLRE
--regime-ma 200 --regime-window 21 --bear-etf-only --bear-rank both
--bear-intersect-top 3 --bear-slope-window 120 --bear-min-score 0
--stop-loss 0.10 --stop-repick-dom 11
--start 2021-11-13 --end 2026-11-13
```

回测产物目录（池层口径，板块层剔除 XLRE，固定 10% 止损）
`reports/etf/rsrs_stock_n20_m40_top1_turnma_pool_no-XLRE_sl10_srdom11_strong_..._sl10dom11/`
（2021-11 ~ 2026-11，61 期：累计 **+2835.5%**、年化 97.4%、波动 37.7%、夏普 2.587、
最大回撤 −34.3%；同期 SPY +72.6%）。只算 CRSP 段（到 2025-12-31、50 期）是
**+726.0% / 年化 67.3% / 波动 30.1% / 夏普 2.238 / 最大回撤 −22.1%**。
对照的「不设止损」口径是 61 期 +2472.2% / 92.1% / 2.396 / −36.7%，50 期
+832.3% / 72.2% / 2.300 / −22.1%——止损在 CRSP 段是负贡献（收益 −106pp、夏普 −0.06、回撤持平），
61 期的优势全部来自 2026-03 那一次割肉（F −18.2% → +5.4%，换 OXY），61 期总共只触发 4 次。
2026 那 10 期来自本地行情延展（`src='local_ext'`，非 CRSP），累计 +274.3%，其中 3 期
（SNDK +72.4%/+54.6%、MRNA +37.1%）贡献了绝大部分，**不能单独当年化能力引用**。
旧的本地快照口径（同区间 +637.9%，把 2026 段也算进去时曾报 +2989.5%）与 ticker 口径
CRSP 视图（+207.1%）都已作废，代码也已删除。

**回测数据源只有一套**：`data/curated/sp500_pool`（security_id = CRSP PERMNO 主键），
价格 / 换手率（成交量÷总股本）/ 市值（DlyCap）/ 成分区间（PIT）四类全部来自池层。
`--data-source` 开关已删除（`src/quant/data/source.py` 一并删除），没有第二套可选口径。
CRSP 数据包的硬边界是 2025-12-31，边界之后由 `scripts/extend_pool_to_latest.py`
用本地行情按重叠区间缩放进同一价格空间、写成 `*_ext.parquet`（`src='local_ext'`），
仍由同一个 `pool` 接口读取，所以回测能跑到 2026-11-13。
实盘仍用 SSGA 当前快照 + 腾讯/新浪实时报价，与回测的 PIT 名单不同，见「六、口径差异」。

**市值门槛 500 亿美元是数据清洗口径，不是调参**：小盘票的换手率分母（股本）小、噪声大，
且天然高换手，不设门槛会让「按换手率排序」系统性偏向指数尾部（NCLH 市值 65~100 亿美元、
日换手 5%，曾长期排在 XLY 第一）。阈值不能按收益挑——100/200/300/500 四档的回测收益
并不单调（500 亿最高、300 亿最差），详见 `docs/BACKTEST_HANDOFF.md` 的扫参一节。

---

## 一、它每天在做什么

| 时间（ET） | 谁 | 动作 |
|---|---|---|
| 收盘后 +8 分钟 | `scripts.paper_close_update` | 补当天行情（yahoo）→ 把当天换手率写进面板 → 提交 data 分支 |
| 收盘前 30 分钟 | `scripts.live_signal` | 判断今天该干什么（调仓/周报/**止损**/补选/都不做）→ 拉实时报价 → 算当期信号 → pushplus 推送 → 写账本 |
| 其它时间 | 同上 | 取当日收盘时间后不在窗口内，`exit 0`，什么都不做 |

**每个交易日都会在收盘前 30 分钟算一次**（`scripts/cron_sync.py` 的 `day_triggers` 改成所有
交易日都排 signal）。原因只有一个：牛市补选要求在调仓日空仓后逐日重挑，不是推送日就不算，
就会永远等不到补选。非推送日绝大多数只是空跑 `exit 0`，不推送、不记账。

推送在这些日子发出：

- **调仓日**（月末最后一个交易日）：动作 + 板块 + 买入参考价 + 候选表 + 上期持仓收益；
- **周内最后交易日**：持仓、建仓价、持有期收益、累计净值、下个调仓日；
- **补选成交日**（牛市、且上次调仓日空仓）：只在真的挑到票的那天推一条，等待日不打扰。

调仓日与周报撞在同一天时只推调仓那条。补选日若恰好也是周内最后交易日，推补选那条
（更紧急，且筛选表的内容一样全）。

## 一之一、牛熊与补选（与回测 `--regime-*` / `--bull-refill` 一一对应）

- **判熊**：SPY 收盘价与 MA200 比较，取过去 **21 个交易日跌破占比**，≥50% 判熊。单日跌破
  不翻转（2026-03 就是单日跌破被判熊，把后面 +54% 的一期让给了 ETF）。统计量预热不足时
  按牛市处理。
- **熊市只做 ETF**：不选个股，买「板块 RSRS 前 3 ∩ 120 日加权斜率前 3」交集里 RSRS 最强的
  那只 SPDR 板块 ETF；交集为空、或最强板块 RSRS ≤0 → 当期空仓。ETF 代码直接记进账本
  （`positions.json` 的 `symbol` 就是 `XLK` 这种），与个股走同一套持仓/收益计算。
- **牛市补选**：调仓日 9 个候选全不过闸 → 账本写 `refill.json`（含 `until` = 下一个调仓日、
  `bull` = 冻结的分支）。其后每个交易日重算一次，命中就按当日价买入并关闭窗口；到
  `until` 仍没命中就整段空仓。**分支冻结**：补选期间即使 SPY 已跌破 MA200，也继续按牛市
  链路选股——回测就是在信号日判定的牛市里逐日重挑的，中途切换分支会凭空多出一笔 ETF 交易。
- **不补选的两种情形**：熊市空仓不补（回测的 `--bear-refill` 没在定稿命令里）；调仓日
  有票就更不补。
- **止损（回测 + 实盘都已生效）**：`--stop-loss 0.10 --stop-repick-dom 11` —— 收盘价相对买入价跌
  10% 当日割肉；割肉日在该自然月 **11 号之前** → 次日起逐日重选（口径与调仓日一致，但**排除刚割的
  那只**），否则空仓到下一个调仓日。61 期只触发 4 次、重选 2 次。
  - 实盘由 `scripts/live_signal.py` 的 `--kind stop` 承担：**有持仓的每个交易日**收盘前 30 分钟跑一次，
    只拉持仓票一个报价（不跑全池 RSRS），跌幅按复权口径算（拆股不会被误判）；
  - 触发后推一条「止损卖出」，账本记 `kind=stop` 并滚动净值；11 号之前则开启「止损重选」
    （`refill.json` 带 `origin=stop` + `exclude=刚割的票`），次日逐日重挑、排除原票；
  - ⚠ 唯一的近似：回测用**收盘价**判定、收盘价成交；实盘用收盘前 30 分钟的现价，最后半小时的
    波动可能让它早割或晚割一天。ET 收盘前 30 分钟这个时点与买卖决策是同一个，不是新增误差。

## 二、为什么时间不写死

- 夏令时把 16:00 ET 推到 20:00Z / 21:00Z；
- 早收日（感恩节次日、7/3、平安夜）是 13:00 ET；
- GitHub Actions 的 schedule 会漂 5~30 分钟。

所以 workflow 里挂了一组 cron（16:30/17:30/19:30/20:30Z）当**唤醒器**，是否真的执行由
`quant.live.calendar` 现算：取当日收盘时间 → 减 30 分钟 → 只接受「早不超过 5 分钟、
晚不超过 40 分钟」。早到会让换手率被系统性低估，所以早的余量给得很小；迟到只是晚几分钟
知道买什么，给它 40 分钟吃掉排队延迟。

**每个交易日的判定顺序**（止损上线后，四者互斥）：月末最后交易日 → `kind=rebalance`；
本周最后交易日 → `kind=week`；有持仓 → `kind=stop`（只查 10% 止损）；空仓且补选窗口开着 →
`kind=refill`（牛市补选 / 止损重选）；都不满足 → `noop`。所以准点触发器现在是**每个交易日**
都会 dispatch，不再只挑调仓日/周末。

**实测事实（2026-11-17）**：schedule 不是"漂 5~30 分钟"，而是整段延迟数小时或丢失——当天
5 次定时运行全落在 UTC 00:24~04:03，cron 设的却是 16:30~21:30Z，窗口内一次都没有。
因此 cron 只能当兜底，**准点触发靠 `scripts/dispatch_signal.py`**：由一个准时的外部定时器
（Windows 任务计划 / 云服务器 cron，每 5 分钟一次）调它，它自己判断今天是否推送日、是否到
触发时刻，到点就用 API 触发 `workflow_dispatch`（无排队）。需要环境变量 `GH_DISPATCH_TOKEN`
（fine-grained PAT，仅该仓库 Actions: Read and write）。重复触发由本地标记与账本幂等键兜住。

交易所若调整交易时段，设环境变量 `TA_MARKET_CLOSE_ET=15:00` 即可覆盖，不用改代码。

## 三、一次性配置

### 1. 建 data 分支（只放数据，和代码分开）

```bash
git init && git add . && git commit -m "init"
git checkout --orphan data
git rm -rf .            # 清空工作区
printf 'data/\n.venv/\n__pycache__/\n' > .gitignore   # 别让归档文件反过来影响主仓
mkdir -p curated/bars_daily curated/fundamentals meta state/live
# 把本地 data/curated 与 data/meta 拷进来（首次一键）
git add -A && git commit -m "data: bootstrap"
git push -u origin data
git checkout main
```

> data 分支的目录结构是 `curated/`、`meta/`、`state/`（**不含最外层的 data/**）。
> workflow 把它 checkout 到 `./data`，再令 `TA_DATA_ROOT=./data`，两边路径就对上了。
> 体积：`curated/bars_daily` 142.7 MB，`curated/fundamentals/turnover_daily.parquet` 1.1 MB。
> 每年跑一次 `python -m scripts.archive_data_year`，把滚出两年的年份导进 `archive/`，
> 主目录就能稳定在 20 MB 量级。

### 2. 配密钥

| 名称 | 类型 | 用途 |
|---|---|---|
| `PUSHPLUS_TOKEN` | Secret | 必填，推送用 |
| `ALPACA_KEY_ID` / `ALPACA_SECRET` | Secret | 可选，yahoo 429 时的降级源 |
| `TIINGO_TOKEN` | Secret | 可选，同上 |

没有 Alpaca/Tiingo 也能跑，只是 yahoo 被限流时当天行情会缺、换手率靠腾讯补。

### 3. 开写权限

Settings → Actions → General → Workflow permissions 选 **Read and write**，
否则回写 data 分支会 403。

## 四、日常使用

```bash
# 本地空跑，看推送正文但不发送、不记账
python -m scripts.live_signal --dry-run

# 假装现在是某个时刻（复现某次决策）
python -m scripts.live_signal --now "2026-11-30 15:30" --dry-run

# 强制算一次（无视窗口与幂等键）
python -m scripts.live_signal --force --dry-run

# 收盘后手动补数据
python -m scripts.paper_close_update
```

```bash
# 手动跑一次牛市补选 / 止损重选（窗口外跑会直接 noop）
python -m scripts.live_signal --kind refill --dry-run

# 只查持仓止损：有持仓且跌破 -10% 才推送，否则 noop
python -m scripts.live_signal --kind stop --dry-run --force
```

GitHub 上：Actions → paper-trade → Run workflow，可选 `kind`（auto/rebalance/week/refill/stop）
与 `dry_run`。

## 五、候选池与标普 500 的调入调出

候选池不是一张静态 CSV，而是「每天刷新 + 留变更日志 + 新票自动补行情」：

| 环节 | 做法 |
|---|---|
| 刷新源 | SSGA 官方每日 holdings（11 个板块 ETF 的 xlsx），最贴近「ETF 里到底装了什么」 |
| 可及性 | 国内网络**不可达**（TLS 握手超时），只在 Actions 上刷新；拉不到就沿用旧快照，绝不中断信号 |
| 落盘 | `data/ref/us_sector_holdings.csv`（最新）+ `ref/history/…_<asof>.csv`（每次留档） |
| 变更日志 | `data/ref/index_changes.csv`，追加 `add / delete / move` 三类事件 |
| 新票补行情 | 刷新后与 `list_symbols()` 比对，缺行情的新纳入票自动回补 520 天历史 |
| 持仓被调出 | 推送里单列一句「已不在成分中，本期必须换出」——这和「排名掉下去」是两回事 |

四个必须防的坑（都已处理）：

1. **新纳入的票进不来** —— 池子不更新就永远看不到，而新纳入常伴随指数基金的被动买盘；
2. **被剔除的票出不去** —— 留着就可能买到正在被指数基金抛售的票；
3. **新票没有本地行情被静默丢掉** —— 现在会在推送里显式列出「N/M 只成分股无行情」；
4. **代码口径不一致** —— SSGA 写 `BRK.B`，行情源是 `BRK-B`，不归一就永远查不到。
   伯克希尔（标普 500 权重前 10）此前一直不在候选池里，就是这条造成的。

⚠ **成分口径（2026-11 已修）**：实盘现在按 `asof` 走 point-in-time（`pool.members_asof`）。
有 ≤asof 的历史快照存档就用存档（连板块归属都是当时的），否则用官方变更表
（`data/curated/index/sp500_changes_since_2019.csv`）从最新快照倒推——撤掉 asof 之后才
`add` 的票、把之后才 `delete` 的票加回；快照落后于 asof 时反过来把已公告的调整推进去。
之前直接读最新快照，历史回放会把 6 月才纳入的 `MRVL` 在 4 月选出来（前视）；
修完后 2026 年 11 个调仓日各回滚 6~34 只，候选池从「今天的样子」还原成「那天的样子」。

还剩两处差异，都写在推送里，不再静默：
1. 官方变更表只更新到 **2026-08-18**，之后的指数调整没有记录 → `asof` 晚于这个日期时，
   推送会显式告警「变更表过期，之后的调整未纳入」；
2. 回测池按 CRSP 行情截断：2026 年内被剔除的 17 只（`BK`/`CAG`/`CPB`/`EA`/`EPAM`…）
   在回测里 2026 年整段没有行情，`membership` 被截到 2025-12-31；实盘 PIT 反而更完整
   （那些票当时确实还在指数里）。所以两边的差集是**固定的、可解释的一批票**，
   拿 live 对回测曲线时要先扣掉它们。

⚠ **策略层面的固有时滞**：标普 500 的调整通常在季度 rebalance 生效（3/6/9/12 月第三个周五
收盘后），而本策略月末才调仓。持仓票若在月中被宣布调出，要到下个调仓日才动，
这段时间要承受指数基金的抛压。这是策略设计的结果，不是实现缺陷。

## 六、口径差异（相对回测，必须知道）

| 项 | 回测 | 模拟盘 | 处理 |
|---|---|---|---|
| 买入价 | 信号日 `adj_close` | 15:30 时点现价 | 推送里写明「参考价」，不假装是成交价 |
| RSRS 的 high/low | 完整日振幅 | 缺收盘前 30 分钟 | 已知偏差，排序边界附近可能翻转 |
| 换手率 | 面板 `turnover_daily`（2021-2025 段与 CRSP 对账覆盖，延展段同源） | 同一份面板 + 当日累计报价 | 实盘**不再乘 0.48**（报价本身就是截至此刻），乘两遍会重复折价；面板历史段的越界值由 `scripts/repair_turnover_panel.py` 清理，写入与读取各有一道护栏 |
| 换手率排序 | `turn20²/turn120`（turn120 至少 60 天） | 同一公式，turn120 用 `turnover.mean_n(window=120, min_days=60)` | turn120 有效天数不够的票**不参与排序**（不是当 0） |
| 趋势闸 | `--trend-mode strong` 五闸全 AND | 同一套（`signal._gate_panels` / `_gate`），面板向量化 | 已用 2026-05~10 五个调仓日 + 2022 熊市四个调仓日逐期对齐回测持仓 |
| 牛熊 | SPY vs MA200，21 日跌破占比 ≥50% 判熊 | 同一口径 | 补选期间分支冻结在开启日，见「一之一」 |
| 补选 | `--bull-refill` 逐日重挑 | `refill.json` + 每个交易日算一次 | 等待日不推送；分支冻结，避免中途翻熊乱买 ETF |
| 成分股 | 池层 `membership`（CRSP 逐日 PIT；2026 年被剔除的 17 只因无行情被截到 2025-12-31） | 当前快照 + 历史存档 + 官方变更表，按 `asof` 还原（`pool.members_asof`） | 实盘回放已无前视；差集只剩那 17 只「回测没行情」的票，固定可解释 |
| 止损 | 收盘价 ≤ 买入价 × 0.90 当日割；割肉日 < 当月 11 号则重选（排除原票） | **已实现**（`--kind stop`，每交易日收盘前） | 回测用收盘价、实盘用收盘前 30 分钟现价，这是唯一近似；判据都走复权口径，见「一之一」 |
| 市值门槛 | ≥500 亿美元，市值 = 未复权价 × 反推股本（全样本中位数，当日 DlyCap） | 同一阈值，市值 = 未复权价 × **当日真实股本**（快照 → 反推 5 日 → SEC 三级兜底，逐票留来源） | 实盘不再用 3 年中位数（它会把拆股/回购后的股本算错）。503 只实测 500 亿门槛附近 **14 只**判定翻转；引用历史收益时要说明。历史回放必须用 `asof` 当日收盘价，用今天的报价会把当时刚过线的票（KDP 这类 500 亿边界票）误剔 |

## 七、已知风险（不是 bug，是取舍）

1. **调度不可靠是外部约束**：GitHub 免费 runner 的 schedule 可能延迟 30 分钟甚至丢掉。
   兜底手段是手动 `Run workflow`；错过了就当天空仓不动，不补单。
2. **报价源可能滞后**：`quant.live.quotes` 会给每个报价打时间戳，滞后 >10 分钟在推送里
   标注，>45 分钟直接中止不推送（宁可不发，也不发一个错的）。
3. **静默失败最危险**：推送失败会让 job 变红（`::error::`），但如果你不看 Actions，
   就只剩「手机没响」这一条线索。建议每周扫一眼 Actions 的绿点。
4. **数据完整性闸只做到「拿不到就剔除并写进推送」**：某只票当天无报价会掉出候选，
   不会静默当作有数据。
5. **没有组合层风控**：只有一条固定 10% 单票止损，不加仓位上限、
   不做组合分散。这是研究策略，不是资管产品。

## 八、文件职责

| 文件 | 职责 |
|---|---|
| `src/quant/live/calendar.py` | 交易日历、当日收盘时间（联网+内置+人工覆盖）、调仓/周末判定、执行窗口 |
| `src/quant/live/quotes.py` | 腾讯/新浪批量报价，价格、盘中高低、累计成交量、换手率、时间戳 |
| `src/quant/live/turnover.py` | 历史 19 天（ws 面板）+ 当天（报价）合成均换手率；`build` = turn20，`mean_n(window=120, min_days=60)` = turn120 |
| `src/quant/live/pool.py` | 候选池：成分股快照刷新、调入调出变更日志、代码归一、新票识别 |
| `src/quant/live/signal.py` | 当期信号：牛熊判定 → 熊市 ETF / 牛市「板块 RSRS → turn20²/turn120 前 3 → strong 五闸 → 下探」 |
| `src/quant/live/books.py` | 账本；`refill.json` 记录牛市补选窗口（`open/since/until/bull`） |
| `src/quant/live/mcap.py` | 市值门槛：未复权价 × 当日真实股本（Nasdaq 日快照 → 5 日换手率反推 → SEC 申报兜底） |
| `src/quant/live/journal.py` | 运行流水与决策快照（用法见 `docs/LIVE_LOG.md`） |
| `src/quant/live/books.py` | 持仓、流水、已推送标记（幂等） |
| `src/quant/live/notify.py` | pushplus 推送 |
| `scripts/live_signal.py` | 信号入口 |
| `scripts/live_audit.py` | 日志查询：`--date / --why / --runs / --gaps / --replay / --stats` |
| `scripts/paper_close_update.py` | 收盘后数据更新入口（含成分股刷新与新票补行情） |
| `scripts/archive_data_year.py` | data 分支按年归档 + 裁剪 |
| `.github/workflows/paper_trade.yml` | 调度与回写（失败也提交日志） |

## 九、日志与回溯

每次运行都会在 data 分支的 `state/live/` 留下两份东西：一行**运行流水**（含中止与失败）
和一份**决策快照**（当时的报价、全池候选、剔除原因、参数、结论、推送正文）。

```bash
python -m scripts.live_audit --date 2026-11-30    # 当天为什么这么选
python -m scripts.live_audit --why AIG --date 2026-11-30  # 某只票为什么没入选
python -m scripts.live_audit --gaps --since 2026-06-01    # 该跑却没跑的调仓日
python -m scripts.live_audit --replay --date 2026-11-30   # 用快照重算，查口径漂移
```

**Actions 的 run log 只保留 90 天且不可查询，不作为回溯依据**；artifact 只用来排 traceback。
完整的目录结构、字段含义、注意事项与排查顺序见 [`docs/LIVE_LOG.md`](LIVE_LOG.md)。
