# 模拟盘日志与回溯

这份文档讲的是**「三个月后还能不能回答当时发生了什么」**，不是「怎么看今天的输出」。
日常信号长什么样看 `docs/PAPER_TRADING.md`；这里只管留痕、查证、复现。

---

## 0. 为什么不能靠 GitHub Actions 的 run log

Actions 的 stdout 有三个致命问题：

1. **会过期**：默认保留 90 天（可调最长 400 天），过期后连一个字节都没有；
2. **不可查询**：只能按 job 翻页看，没法问「AIG 在过去半年被剔除过几次」；
3. **不属于仓库**：clone 下来看不到，换台机器就丢。

所以真正的日志在 **data 分支的 `state/live/`** 里。Actions 的 artifact 只保留 14 天，
用途是「这次 job 红了，看看 traceback」，**不作为任何回溯依据**。

---

## 1. 目录结构

主仓的 `state/` 在 `.gitignore` 里（本地开发用），CI 上通过 `TA_STATE_DIR` 指向
data 分支的 `state/live`：

```
state/live/
├── positions.json           # 当前持仓（账本状态）
├── pushed.json              # 幂等键：今天推过没有
├── ledger.csv               # 历史流水（结果：买了什么、净值多少）
├── runs.jsonl               # 运行流水，全部运行一个文件，每次运行一行
└── decisions/
    └── 2026-11-30_4821/     # <asof>_<run_id>，一次运行一个目录
            ├── meta.json      # 参数快照、数据源状态、git sha
            ├── quotes.csv     # 当时拿到的每只票的报价（可重放的核心）
            ├── candidates.csv # 候选池全表 + 每只票没入选的确切原因
            ├── sectors.csv     # 11 个板块的 RSRS 与排名
            ├── decision.json   # 结论：买卖动作、价格、持仓、净值
            └── push.md         # 实际推送给你的正文（截断后的版本）
```

**`ledger.csv` 记的是结果，`decisions/` 记的是过程。** 净值对不上时先看前者，
「为什么买它」只能看后者。

---

## 2. 每个文件里有什么

### 2.1 `runs.jsonl`：一次运行一行

每次运行**必定**写一行，包括没发信号的运行。

| 字段 | 含义 |
|---|---|
| `run_id` | CI 用 `GITHUB_RUN_ID`；本地跑是 `L` + 时间戳（一眼能区分来源） |
| `ts_utc` / `ts_et` | 运行时刻，两个时区都记（排查时不用自己换算） |
| `asof` | 这次决策针对的**交易日（ET）**。注意跨时区时它与 `ts_utc` 的日期可能差一天 |
| `kind` | `rebalance`（调仓）/ `week`（周报） |
| `status` | 见下表，**这是排查的第一站** |
| `reason` | 中断/空跑的具体原因，`ok` 时是动作描述 |
| `dry_run` | 是否空跑 |
| `git_sha` | 代码版本，用来排除「改了参数却没重跑」这类误会 |
| `duration_s` | 耗时，异常慢时能看出来 |
| `notify` | `ok` / `failed` / `skipped` |
| `pick` / `sector` | 当期选出的标的与板块（空仓时为空） |
| `decision_dir` | 对应快照目录；`noop`/`abort` 时为空 |
| `code` | 进程退出码 |

`status` 只有五种，含义不能混：

| status | 含义 | 需要处理吗 |
|---|---|---|
| `ok` | 算完并推送成功 | 不用 |
| `noop` | 按规则就不该跑（非调仓日 / 不在执行窗口 / 今天已推过） | 不用，这是正常的 |
| `abort` | 数据不自洽主动中止（报价不足、滞后超限、预热不足） | 要看一眼 |
| `error` | 异常或推送失败 | 必须看 |
| `dryrun` | 手动空跑 | 不用，且**不计入「该有记录」** |

### 2.2 `candidates.csv`：最重要的一张表

推送里只有结论，事后追问「为什么不是 X」全靠这张表。它包含**整个候选池**（约 500 行），
不只是进入下探的那 9 只。

| 列 | 含义 |
|---|---|
| `symbol` / `sector` | 代码与所属板块 ETF |
| `price` | 当时的未复权价（盘中实时价） |
| `turnover20` | 20 日均换手率（小数，0.0086 = 0.86%） |
| `mcap` | 市值（**美元**，不是亿美元） |
| `mcap_ok` | 是否通过市值门槛 |
| `turn_ok` | 是否在换手率上限内（≤20%） |
| `ma10` / `ma20` / `ma50` | 均线（复权价口径） |
| `stack_ok` | 是否多头排列（MA10 > MA20 > MA50） |
| `sector_rank` | 所属板块当期在 11 个板块中的 RSRS 排名；没进前 3 为 NaN |
| `in_top3` | 是否进入该板块换手率前 3（只有这些票才会被下探） |
| `selected` | 是否最终入选 |
| `reason` | **没入选的第一个原因**，见下表 |

`reason` 的取值与优先级（一只票只记**最先命中**的那一个）：

| reason | 含义 |
|---|---|
| 空字符串 | 入选 |
| `no_local_data` | 成分股但本地没有行情数据（需补数据） |
| `no_quote` | 当天没拿到报价 |
| `small_mcap` | 市值不达 500 亿美元 |
| `no_turnover` | 换手率有效天数不足，算不出 20 日均值 |
| `turn_cap` | 20 日均换手率超 20% 上限 |
| `not_in_top3` | 在本板块换手率排不进前 3 |
| `stack_fail` | 均线不是多头排列 |
| `not_selected` | 条件都满足，但更靠前的板块已经选出标的 |

⚠ **`reason=small_mcap` 不代表它其它条件都满足**，只是它在这条上先被拦住了。
要判断「如果没有门槛它会不会入选」，得看 `turnover20` 在板块内排第几。

### 2.3 `quotes.csv`：可重放的核心

每只票的 `price / high / low / volume / turnover_pct / total_shares / ts / src`。
`src` 是 `tencent` 或 `sina`（新浪兜底时换手率口径略差，会标出来），
`ts` 是**数据源时间戳**——`lag_min` 就是用它算的，不是抓取时刻。

没有这个文件就无法重放：行情面板可以被后来补全或修正，但当天的报价只有这一份。

### 2.4 `meta.json`

`params` 是**参数快照**（RSRS 的 N/M、换手率上限、市值门槛、下探只数），
`pool` 是候选池规模与快照日期，`quotes` 是报价条数与中位滞后。

半年后想确认「当时门槛是 200 还是 500」，答案在这里，不在代码里。

### 2.5 `decision.json`

`action`（建仓/换仓/继续持有/清仓/空仓）、`pick`、`price`、`holding`（上期持仓与净值）、
`holding_return`、`notes`（推送里的全部备注）、`notify_ok`。

### 2.6 `push.md`

实际发出的正文。pushplus 有 2000 字上限，超了会被截断——**你手机上看到的和文件里的可能
差最后几行**，文件里是截断后的真实内容。

---

## 3. 怎么查

所有命令在项目根目录执行。

```bash
# 列出所有决策快照
python -m scripts.live_audit --list

# 某天的完整决策：板块排名、下探过程、剔除原因计数、离门槛最近的票
python -m scripts.live_audit --date 2026-11-30

# 某只票那天为什么没入选
python -m scripts.live_audit --why AIG --date 2026-11-30

# 运行流水（含中止与失败）
python -m scripts.live_audit --runs --since 2026-11-01

# 该跑却没跑的调仓日（静默失败检测）
python -m scripts.live_audit --gaps --since 2026-06-01

# 用快照里的报价重算一遍，与当时的结论比对（口径漂移检测）
python -m scripts.live_audit --replay --date 2026-11-30

# 当月运行统计
python -m scripts.live_audit --stats 2026-11
```

`--why` 的输出长这样：

```
快照：decisions/2026-11/2026-11-10_L20261114T141133
  AIG  板块=XLF  价=82.86  20日均换手=0.89%  市值=481 亿美元
  均线 MA10/20/50 = 81.30 / 81.06 / 77.66  多头排列=True
  板块排名=-  进入前3=False  最终入选=False
  结论：市值不达标
  距门槛还差 19 亿美元
```

`--gaps` 会把「那天跑了但中止」也列出来：

```
⚠ 4 个调仓日没有成功记录（要么没跑，要么跑了但中止）：
  2026-08-31  无任何运行记录
  2026-09-30  abort:stale_quotes_52min
```

退出码：`0` 正常；`1` 发现问题（replay 不一致 / 有 gap）；`2` 参数或数据问题。
可以直接用在 CI 里当检查项。

---

## 4. 注意事项

**1. Actions 的 run log 不作为证据。** 90 天就没了。任何「当时到底怎么选的」都以
`state/live/` 为准；artifact 只用来排 traceback。

**2. 只追加，不修改。** `runs/` 和 `decisions/` 都是 append-only。不要手工改动历史快照——
它是证据，改了就没法再拿它跟回测对账。要修正当前状态就改 `positions.json` / `ledger.csv`，
并在 commit message 里写明原因。

**3. 「没有记录」≠「没跑」，但「没记录」本身就是线索。** 所有出口都会写一行流水，
所以某天完全查不到记录，只可能是 cron 没触发或 job 没起来（去 Actions 看）——
这条判断能省掉大量瞎猜。

**4. `dry_run` 会写日志，但不提交。** 本地 `--dry-run` 会在 `state/live/` 留下快照
（方便验证格式）；CI 上 `dry_run=true` 时回写步骤被跳过，所以不会污染 data 分支。
`--gaps` 会忽略 `dryrun` 记录。

**5. 同一天重跑不会覆盖。** 快照目录名带 `run_id`，撞车时自动加 `-2`、`-3`。
所以「上午那次中止、下午手动重跑成功」两个记录都在，排查时不要只看最后一个。

**6. 时区。** `asof` 是**纽约时间的交易日**，`ts_utc` / `ts_et` 是运行时刻。
北京时间早上跑的 job，`ts_utc` 的日期可能比 `asof` 早一天，按日期 grep 时注意用 `asof`。

**7. 按月滚文件，别合并。** `runs/2026-11.jsonl` 这种粒度每月只有几十行，
合并成一个文件会让每次 diff 都巨大，且容易冲突。

**8. 体量。** 一次运行约 130 KB 未压缩（`quotes.csv` 52 KB + `candidates.csv` 75 KB）。
调仓月频 + 周报，一年约 64 次运行 ≈ 8 MB，永久保留没有压力，不需要清理策略。

**9. 不要删 `decisions/`。** `--replay` 和 `--why` 都依赖它。真要归档，按年打包成
`.tar.gz` 后移到 `archive/`，不要直接删。

**10. 仓库里没有敏感信息。** 报价与换手率都是公开行情，`journal` 不记录
`PUSHPLUS_TOKEN` 或任何密钥。日志可以正常推公开仓库（如果你把仓库设为公开）。

**11. 本地与 CI 是两套目录。** 本地是 `state/live`（被 gitignore），
CI 是 data 分支的 `state/live`。本地跑出来的快照不会出现在仓库里，别指望本地测试的记录
能在 `--gaps` 里看到——那需要把 data 分支 checkout 到 `./data` 并设 `TA_STATE_DIR`。

**12. `--gaps` 只查月频调仓。** 周报漏发影响很小，不纳入缺口判定，避免天天告警。

**13. `--replay` 的局限。** 它验证的是「同样的输入 → 同样的输出」，但候选池成员用的是
**当前**的成分股快照（`pool.load()`），半年前的名单已变，所以个股集合会不同。
replay 不一致时先看 `meta.json` 的 `git_sha` 和 `params`，确认是不是参数改过；
参数没变却选股不同，才是真的口径漂移。

---

## 5. 出问题时的排查顺序

**「手机没收到信号」**

1. `--runs --since <那天>` 看有没有记录；
2. 没有记录 → cron 没触发或 job 没跑，去 Actions 看有没有 run；
3. `status=noop` → 看 `reason`：`kind_none`（非调仓日/非周末最后交易日）、
   `not_in_window`（时钟不在窗口内）、`already_pushed`（今天推过了）；
4. `status=abort` → 看 `reason`：`quotes_xx_of_yy`（数据源挂了）、
   `stale_quotes_XXmin`（报价滞后超过 45 分钟）、`system_exit`（预热不足）；
5. `status=ok` 但 `notify=failed` → 推送环节失败，去 artifact 里看 pushplus 返回码；
6. 全都没有 → 检查 `Settings → Actions` 是否被禁用、schedule 是否被 GitHub 暂停
   （仓库 60 天无提交会自动停用定时任务）。

**「为什么选的是 X 不是 Y」**

```bash
python -m scripts.live_audit --why Y --date <那天>
python -m scripts.live_audit --date <那天>
```
先看 Y 的 `reason`，再看 `--date` 里的下探过程：可能是 Y 所在板块没进 RSRS 前 3，
也可能是板块对但 Y 的换手率排第 4。

**「净值对不上」**

`ledger.csv` 是净值序列（每次调仓滚一次），`decision.json` 里的 `holding` 是当时的持仓
与建仓价。先确认是不是「继续持有」的期也被记了——那段收益只记一次，周报不重复记。

**「怀疑口径改坏了」**

```bash
python -m scripts.live_audit --replay --date <改之前最后一次调仓>
```
重算出不同标的 → 对着 `meta.json` 的 `params` 和 `git_sha` 找差异。

---

## 6. 设计取舍

**为什么用 git 而不用数据库 / 对象存储**
一年 64 次运行、8 MB 数据，git 的版本化、`git log`、`git show`、离线可读全都白送。
引入数据库等于多一个会挂的东西，而它要解决的问题这里不存在。

**为什么存全量而不是只存指纹**
指纹（hash）能证明「数据变过」，但回答不了「它当时换手率排第几」。
全量一年才 8 MB，没必要省。

**为什么先写快照再推送**
顺序反了会出现「消息到了手机、仓库里没痕迹」——这是最难查的状态：
你明明收到了错误信号，却找不到它是怎么算出来的。

**为什么 status 要分四类而不是成功/失败**
`noop`（本来就不该跑）和 `abort`（该跑但数据不行）的处置方式完全不同：
前者不用管，后者要看数据源。混在一起就变成了「天天有失败」的狼来了。

**为什么不记每次 cron 唤醒**
一天 4 次 cron，绝大多数落在窗口外。全记会让流水变成噪音，掩盖真正的中止。
但**每次真正进入计算的运行都会记一行**，包括最后空仓的。
