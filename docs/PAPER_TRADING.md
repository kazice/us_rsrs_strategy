# 模拟盘（GitHub Actions + pushplus）

定稿策略：板块 → 个股双层 RSRS，板块内按 20 日均换手率取前 3、要求多头排列，
最多下探 3 个板块；月末最后一个交易日**收盘前 30 分钟**决策，单票满仓、不设止损。

定稿参数（与回测产出一一对应）：

```
--n 20 --m 40 --pick turnma --turnma-top 3 --turnma-sectors 3
--trend-mode ma_stack --stack-order desc
--turn-shares ws --turn-cap 0.20 --turn-tail 0.48
--pool-mode pit --min-mcap 500
```

产物目录 `reports/etf/rsrs_stock_n20_m40_top1_turnma_ma_stack_desc_turnws_cap20_tail48_ppit_mc500_3y_mc500/`
（2023-11 ~ 2026-10：累计 +1616.1%、夏普 3.024、最大回撤 −37.2%，stats.csv 日度净值口径）。

**市值门槛 500 亿美元是数据清洗口径，不是调参**：小盘票的换手率分母（股本）小、噪声大，
且天然高换手，不设门槛会让「按换手率排序」系统性偏向指数尾部（NCLH 市值 65~100 亿美元、
日换手 5%，曾长期排在 XLY 第一）。阈值不能按收益挑——100/200/300/500 四档的回测收益
并不单调（500 亿最高、300 亿最差），详见 `docs/PROGRESS.md`。

---

## 一、它每天在做什么

| 时间（ET） | 谁 | 动作 |
|---|---|---|
| 收盘后 +8 分钟 | `scripts.paper_close_update` | 补当天行情（yahoo）→ 把当天换手率写进面板 → 提交 data 分支 |
| 收盘前 30 分钟 | `scripts.live_signal` | 判断今天是不是调仓日/周末 → 拉实时报价 → 算当期信号 → pushplus 推送 → 写账本 |
| 其它时间 | 同上 | 取当日收盘时间后不在窗口内，`exit 0`，什么都不做 |

推送只在两种日子发出：

- **调仓日**（月末最后一个交易日）：动作 + 板块 + 买入参考价 + 候选表 + 上期持仓收益；
- **周内最后交易日**：持仓、建仓价、持有期收益、累计净值、下个调仓日。

两者撞在同一天时只推调仓那条。

## 二、为什么时间不写死

- 夏令时把 16:00 ET 推到 20:00Z / 21:00Z；
- 早收日（感恩节次日、7/3、平安夜）是 13:00 ET；
- GitHub Actions 的 schedule 会漂 5~30 分钟。

所以 workflow 里挂了一组 cron（16:30/17:30/19:30/20:30Z）当**唤醒器**，是否真的执行由
`quant.live.calendar` 现算：取当日收盘时间 → 减 30 分钟 → 只接受「早不超过 5 分钟、
晚不超过 40 分钟」。早到会让换手率被系统性低估，所以早的余量给得很小；迟到只是晚几分钟
知道买什么，给它 40 分钟吃掉排队延迟。

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

GitHub 上：Actions → paper-trade → Run workflow，可选 `kind`（auto/rebalance/week）与
`dry_run`。

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

⚠ **回测与实盘的池子口径仍不同**：回测用 `--pool-mode pit`（按官方公告重建逐日名单，
锚点 503 只 + 期内补入 57 只），实盘用每日刷新的当前快照（`pool.load()`）。
两者都会随指数调整更新，但重建方式不同，且实盘还会多出 `BRK-B` / `BF-B` 这类
行情源缺失的票。所以不要拿 live 的表现逐日对回测曲线，只能对「同一个月是否选了同一只」
这种粒度。

⚠ **策略层面的固有时滞**：标普 500 的调整通常在季度 rebalance 生效（3/6/9/12 月第三个周五
收盘后），而本策略月末才调仓。持仓票若在月中被宣布调出，要到下个调仓日才动，
这段时间要承受指数基金的抛压。这是策略设计的结果，不是实现缺陷。

## 六、口径差异（相对回测，必须知道）

| 项 | 回测 | 模拟盘 | 处理 |
|---|---|---|---|
| 买入价 | 信号日 `adj_close` | 15:30 时点现价 | 推送里写明「参考价」，不假装是成交价 |
| RSRS 的 high/low | 完整日振幅 | 缺收盘前 30 分钟 | 已知偏差，排序边界附近可能翻转 |
| 换手率 | 19 天完整 + 当日 ×0.48 | 19 天完整 + 当日**累计值** | 实盘**不再乘 0.48**（报价本身就是截至此刻），乘两遍会重复折价 |
| 换手率数据源 | westock 日 K（成交量/总股本） | 腾讯报价换手率字段 | 同源同口径；新浪按「成交量/市值反推股本」做校验与兜底 |
| 成分股 | 2026-11-11 静态快照（492 只有行情） | 每日刷新 + 变更日志，代码归一 | live 比回测多 `BRK-B`/`BF-B`，两边口径不同，不可直接对曲线 |
| 止损 | 不设 | 不设 | 单票满仓，回测最大回撤 −37.2% |
| 市值门槛 | ≥500 亿美元，市值 = 未复权价 × 反推股本（全样本中位数） | 同一阈值，市值 = 未复权价 × **当日真实股本**（快照 → 反推 5 日 → SEC 三级兜底，逐票留来源） | 实盘不再用 3 年中位数（它会把拆股/回购后的股本算错）。503 只实测 500 亿门槛附近 **14 只**判定翻转；引用历史收益时要说明 |

## 七、已知风险（不是 bug，是取舍）

1. **调度不可靠是外部约束**：GitHub 免费 runner 的 schedule 可能延迟 30 分钟甚至丢掉。
   兜底手段是手动 `Run workflow`；错过了就当天空仓不动，不补单。
2. **报价源可能滞后**：`quant.live.quotes` 会给每个报价打时间戳，滞后 >10 分钟在推送里
   标注，>45 分钟直接中止不推送（宁可不发，也不发一个错的）。
3. **静默失败最危险**：推送失败会让 job 变红（`::error::`），但如果你不看 Actions，
   就只剩「手机没响」这一条线索。建议每周扫一眼 Actions 的绿点。
4. **数据完整性闸只做到「拿不到就剔除并写进推送」**：某只票当天无报价会掉出候选，
   不会静默当作有数据。
5. **没有风控层**：不设止损、不加仓位上限、不做组合分散。这是研究策略，不是资管产品。

## 八、文件职责

| 文件 | 职责 |
|---|---|
| `src/quant/live/calendar.py` | 交易日历、当日收盘时间（联网+内置+人工覆盖）、调仓/周末判定、执行窗口 |
| `src/quant/live/quotes.py` | 腾讯/新浪批量报价，价格、盘中高低、累计成交量、换手率、时间戳 |
| `src/quant/live/turnover.py` | 历史 19 天（ws 面板）+ 当天（报价）合成 20 日均换手率 |
| `src/quant/live/pool.py` | 候选池：成分股快照刷新、调入调出变更日志、代码归一、新票识别 |
| `src/quant/live/signal.py` | 当期信号：板块 RSRS → 市值/换手率过滤 → 换手率 top3 → 多头排列 → 下探 |
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
