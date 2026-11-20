# 回测交接文档（给下一个 AI / 工程师）

> 目标：拿到这台机器、这个仓库，**不读源码也能在 10 分钟内跑出定稿回测、看懂产物、重生成看板**。
> 阅读顺序：第 1 节（一条命令跑通）→ 第 3 节（定稿参数逐条）→ 第 6 节（无前视口径）→ 第 9 节（陷阱）。
> 模拟盘 / 实盘链路看 `docs/HANDOFF.md`，本文只讲**回测**。

---

## 1. 一条命令跑通（已实测，约 30 秒）

Windows PowerShell，在仓库根目录：

```powershell
$env:PYTHONPATH = "src"
python -m scripts.sector_stock_rsrs --data-source crsp --n 20 --m 40 --pick turnma --trend-mode strong --trend-stack 20,60 --stack-slope 5 --slope-mas 20,60 --trend-above 50 --trend-cross 50,200 --adx-min 20 --turn-shares ws --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 --min-mcap 500 --regime-ma 200 --regime-bear nofilter --regime-window 21 --bear-etf-only --bear-min-score 0 --bear-rank both --bear-intersect-top 3 --bear-slope-window 120 --start 2021-11-13 --end 2025-12-31 --tag crsp
```

跑完打印 50 期逐期持仓表 + 汇总，结果写到：

```
reports/etf/rsrs_stock_n20_m40_top1_turnma_crsp_strong_20-60_sl5_slma20-60_a50_x50-200_adx20_turnws_tratio_bref_cap20_tail48_mc500_reg200_bearnofilter_rw21_betfonly_bmin0_brankBoth3x120_crsp/
```

**对账基准（改代码前后必比这几个数）**：个股版累计 **+207.1%**、年化 31.4%、波动 30.7%、
夏普 **1.023**、最大回撤 −41.6%；ETF 版 +42.2%；SPY +54.8%。区间 2021-11-15 ~ 2025-12-31，共 50 期；
选股跑赢 ETF 28/50 期（累计选股差 +83.9%）；熊市 12 期（买 ETF 4 / 空仓 8）。
任何一位不对，说明改动已经改变了策略行为，先查口径再报结论。

> ⚠ **旧数字已作废**：`--data-source local`（yfinance + westock + 今天 503 只快照）同区间跑出
> +637.9% / 夏普 1.52，其中约 2/3 来自数据错误（幸存者偏差为主）。详见 `docs/DATA_AUDIT_CRSP.md`，
> 那篇文章里有四项对账与归因实验。**引用收益只能引 CRSP 口径。**

`PYTHONPATH=src` 不能省（脚本里 `from quant.data import store, universe`）。也可以 `pip install -e .` 装成包。

---

## 2. 环境与数据前置

| 项 | 要求 | 缺失时的症状 |
|---|---|---|
| Python | ≥ 3.10（本机 3.10.2） | 语法 / 类型报错 |
| 依赖 | `pip install -e .`：pandas / numpy / pyarrow / duckdb / yfinance / plotly … | `ImportError` |
| 行情 | `data/curated/bars_daily/symbol=<SYM>/`（分区 parquet，本机 515 个代码） | 候选池大面积消失、ETF 无数据 |
| Universe 快照 | `data/meta/universe/universe_latest.parquet`（Nasdaq 市值快照，含 `market_cap` / `price`） | 市值门槛的股本只能靠反推 / SEC |
| 成分股名单 | `config/ref/us_sector_holdings.csv`（SPDR 官方 holdings，503 只） | 板块下探无人可挑 |
| SEC 股本 | `data/curated/fundamentals/shares_outstanding.parquet`（可缺） | 退回全样本中位数兜底，见第 9 节 |
| **CRSP 标准数据**（定稿口径） | `data/curated/crsp_sp500/{daily,membership,symbols}.parquet`，由 `data/raw/crsp_sp500/*.csv` 转出 | `--data-source crsp` 直接报错退出（先跑 `python -m scripts.import_crsp_sp500`） |

`data/` 是**独立 git 分支**（`origin/data`），不在 main 里。本地没有时：

```powershell
git worktree add ../data_branch origin/data      # 或用环境变量指过去
$env:TA_DATA_ROOT = "d:\path\to\data"
```

`config/settings.yaml` 的 `paths.data` 默认就是 `./data`。

---

## 3. 定稿参数逐条对照

目录名**就是参数快照**，名字一致 ⇒ 参数没跑偏。

| 参数 | 值 | 作用 |
|---|---|---|
| `--data-source crsp` | **定稿口径** | 成分股用 PIT 名单、换手率 = 成交量/总股本、市值 = CRSP `DlyCap`、价格自带复权因子。`local` = 旧口径（已作废，仅作对照） |
| `--n 20 --m 40` | 20 / 40 | RSRS 回归 / 标准化窗口（板块层与个股层共用） |
| `--pick turnma` | 换手率降序 + 趋势闸 | 个股层不用 RSRS 排序，改用筹码活跃度 |
| `--trend-mode strong` | 强化趋势闸 | 多头排列 + 均线斜率 + 站上长期均线 + ADX 四道闸 |
| `--trend-stack 20,60` | MA20 > MA60 | `strong` 下的排列要求；不是默认的 10,20,50 |
| `--stack-slope 5` / `--slope-mas 20,60` | 5 / 20,60 | 均线须 5 日内上行，只查 MA20、MA60（MA5 噪声太大） |
| `--trend-above 50` / `--trend-cross 50,200` | MA50 / MA50>MA200 | 收盘站上 MA50 且 MA50 在 MA200 之上 |
| `--adx-min 20 --adx-len 14` | 20 / 14 | 趋势强度下限 |
| `--turn-shares ws` | westock 日 K 换手率 | 换手率口径（成交量 ÷ 当日总股本），定稿口径 |
| `--turn-rank-mode ratio` | `turn20² / turn120` | 排序 = 绝对换手率 × 相对自身半年常态的放大倍数 |
| `--turn-cap 0.20` | 20% | 20 日均换手 > 20% 的票当期剔除（分母失真） |
| `--turn-tail 0.48` | 0.48 | 信号日当天换手只算 0.48（收盘前 30 分钟时点） |
| `--min-mcap 500` | 500 亿美元 | 市值门槛，严格大于；**默认 `--mcap-shares ws` = 无前视口径** |
| `--bull-refill` | 开 | 牛市当期选不出来 → 次日起逐日重挑，买到为止 |
| `--regime-ma 200 --regime-window 21` | MA200 / 21 日 | SPY 判牛熊：过去 21 日跌破占比 ≥50% 才算熊市 |
| `--regime-bear nofilter` | 不过滤 | 熊市不强制多头排列 |
| `--bear-etf-only` | 开 | 熊市直接买最强板块 ETF，完全不碰个股 |
| `--bear-rank both` | RSRS 前 3 ∩ 斜率前 3 | 熊市板块须双榜同时命中，交集为空则空仓 |
| `--bear-intersect-top 3` / `--bear-slope-window 120` | 3 / 120 | 交集深度 / 斜率回归窗口 |
| `--bear-min-score 0` | > 0 | 熊市买 ETF 要求板块 RSRS 为正 |
| `--start 2021-11-13 --end 2026-10-26` | 5 年 | 月频（`--freq M` 默认），月末最后交易日为锚点 |
| `--tag merged` | 标签 | **只是目录名后缀**，不是"合并"逻辑（见 9.1） |

### 3.1 关键实现位置（`scripts/sector_stock_rsrs.py`，约 1300 行）

| 函数 / 段落 | 职责 |
|---|---|
| `rsrs_panel_fast` | 向量化 RSRS：`beta*r2`，`strict` 过滤 + **`shift(1)` 标准化**（无前视） |
| `adx_panel` | Wilder ADX |
| `load_members` | 读 `config/ref/us_sector_holdings.csv`，贴市值快照 |
| 市值面板（约 520–583 行） | `--min-mcap` 股本：`implied.rolling(5).median()` → SEC → 全样本中位数 |
| 换手率面板 | `rolling(20, min_periods=10)`，信号日一项 × `--turn-tail` |
| 主循环 | 逐调仓日：判牛熊 → 板块排序 → 板块下探 → 趋势闸 → 记一行 `holds` |
| 末尾 | `stats.csv` 指标 + **目录名拼接** + 写盘 |

### 3.2 目录名 → 参数反查表

目录名 = `rsrs_stock_n{sn}_m{sm}_top1` + 各开关按固定顺序拼接，`--tag` 永远在最后。

| 片段 | 含义 |
|---|---|
| `_turnma` | `--pick turnma` |
| `_strong_20-60` | `--trend-mode strong` + `--trend-stack 20,60` |
| `_sl5` / `_slma20-60` | `--stack-slope 5` / `--slope-mas 20,60` |
| `_a50` / `_x50-200` | `--trend-above 50` / `--trend-cross 50,200` |
| `_adx20` | `--adx-min 20` |
| `_turnws` | `--turn-shares ws`（`_shsnap` = 旧口径，含前视） |
| `_tratio` | `--turn-rank-mode ratio` |
| `_bref` / `_bbref` | `--bull-refill` / `--bear-refill` |
| `_cap20` / `_tail48` | `--turn-cap 0.20` / `--turn-tail 0.48` |
| `_mc500` / `_mcstat` | `--min-mcap 500` / `--mcap-shares static`（**含前视，仅对照**） |
| `_reg200` / `_rw21` / `_bearnofilter` | `--regime-ma 200` / `--regime-window 21` / `--regime-bear nofilter` |
| `_betf` / `_betfonly` / `_bspy` | `--bear-etf` / `--bear-etf-only` / `--bear-spy` |
| `_bmin0` | `--bear-min-score 0` |
| `_brankBoth3x120` | `--bear-rank both --bear-intersect-top 3 --bear-slope-window 120` |
| `_ppit` / `_pmcap500` | `--pool-mode pit` / `mcap`（**没有这两个就是 snapshot，含幸存者偏差**） |
| `_al1` | `--anchor-lag 1`（提前一个交易日调仓） |
| `_merged` | `--tag merged` |
| `_crsp` / `_crsp_pool` / `_crsp_metrics` | `--data-source crsp`；后两个是 `--crsp-scope` 的归因实验 |

> ⚠ 本文的定稿结果用的是默认 `--pool-mode snapshot`（用当前成分股回测过去，存在幸存者偏差）。
> `README.md` 里"定稿口径"写的 `--pool-mode pit` 是**实盘对照**用的 PIT 版本，两套数字不可混用。

---

## 4. 产物结构与字段

`reports/etf/<参数串>/` 固定 4 个 CSV（`utf-8-sig`，Excel 可直接打开）：

| 文件 | 内容 |
|---|---|
| `nav_daily.csv` | `date,nav` 日度净值（起点 ≈1.0，已扣 `--cost`） |
| `nav_etf_daily.csv` | 对照净值：每期直接买所选板块 ETF |
| `holds.csv` | 逐期明细，19 列（见下） |
| `stats.csv` | 三行指标 `个股版` / `ETF版` / `SPY`，列 `累计,年化,波动,夏普,最大回撤` |

`holds.csv` 列：`信号日`（锚点）、`持有期起`、`持有期止`、`板块`、`板块RSRS`、`板块斜率%`、`斜率序`、
`补选日`（refill 生效日，空 = 当日选出）、`板块RSRS序`、`牛市`(1/0)、`个股`（空 = 空仓，`ETF:XLK` = 熊市买 ETF）、
`候选尝试`（下探到第几个候选才买到）、`个股RSRS`、`同板块候选`、`个股收益`、`ETF收益`、`选股差`
（= 个股收益 − ETF 收益）、`止损日`、`SPY`。

**收益集中度（引用收益时必带）**：60 期对数总收益 3.146，最佳单期 2024-10-31 **APP +105.9%**（占 23%），
前 3 期占 **48.9%**；剔掉最佳 1 期后累计从 **+2989% 降到 +1029%**。这是策略性质，不是 bug，
任何"提升稳定性"的改造都要先量化它会不会削掉这几期。

---

## 5. 看板（两个 HTML）

两个看板都是**单文件、离线可看**，由脚本从回测产物生成，产物入库、脚本入库：

```powershell
$env:PYTHONPATH = "src"

# 主看板：净值曲线 / 指标卡 / 参数表 / 逐期交易 / 年度收益
python -m scripts.build_web_report --pattern "*_merged" --out reports/web/index.html --title "RSRS 板块→个股 回测（无前视口径 · MA200）"

# 副看板：熊市段 11 个板块 ETF 走势 + 双条件每期选谁
python -m scripts.bear_etf_trends
```

- `build_web_report.py`：`--dir` 指定目录（默认 `reports/etf` 下**最新**一批）、`--pattern` 按目录名匹配、
  `--out` 输出路径、`--title` 标题。它读上表 4 个 CSV，参数表靠 `parse_params()` **从目录名反推**，
  所以新参数一定要写进目录名，否则看板上会显示 `-`。
- `bear_etf_trends.py`：**参数硬编码在文件头**（`REGIME_MA=120`、`RSRS 20/40`、`SLOPE_WIN=120`、
  `INTERSECT=3`、`START/END=2021-11-13~2026-10-26`）。换回测口径时要同步改这几行，否则副看板讲的是旧口径。
- 本地看：`python -m http.server 8765 --directory reports/web`，浏览器开 `http://127.0.0.1:8765/index.html`。

---

## 6. 无前视口径（这段别改，改了历史结论全废）

| 环节 | 无前视怎么保证 | 破坏它的写法 |
|---|---|---|
| RSRS z 值 | 均值/标准差用 `raw.shift(1)`，**不含当日** | 改成含当日的滚动窗口 |
| 市值门槛 | `--mcap-shares ws`：`成交量 ÷ 换手率` 的**最近 5 日滚动中位数**（`min_periods=3`，可 ffill ≤250 日）→ SEC → 全样本中位数（仅补数据缺口） | `--mcap-shares static`（全样本中位数 + 今日快照） |
| 市值价格 | 用**未复权价** × 股本 | 用复权价（拆股后市值算错） |
| 换手率 | `rolling(20)`，信号日一项 × 0.48 | 信号日不折（回测会虚高）或实盘再折一遍 |
| 成分股池 | `--pool-mode pit` 才还原每期名单；snapshot 有幸存者偏差 | 把 snapshot 结果当"可交易收益"引用 |
| 成交额 CV 类因子 | `--cv-lag 1` 才只用信号日之前的数据 | 默认 0 含当日（对收盘前决策是可接受的，但要写明） |
| 调仓锚点 | `--anchor-lag 1` 可整体前移一天，窗口与买价同步移动 | 只移买价不移窗口 |

> 2026-11 的改动：市值门槛的股本从"3 年中位数"改为"最近 5 日滚动中位数"（提交 `5f8380b`）。
> 换手率反推口径降级为快照缺失时的兜底，窗口 3 年 → 5 个交易日；全池与官方快照比对，
> 5 日窗口 ≤2% 误差占 96.0%。

---

## 7. 怎么扫参数

1. **每次跑必带 `--tag`**：`--start/--end/--cost/--spec/--turnma-top/--turnma-sectors` **不进目录名**，
   不加 tag 会直接把上一个结果覆盖掉。
2. 批量示例（PowerShell）：

```powershell
$env:PYTHONPATH = "src"
foreach ($reg in 120, 200) {
  foreach ($top in 3, 4) {
    python -m scripts.sector_stock_rsrs --pick turnma --trend-mode strong --trend-stack 20,60 `
      --stack-slope 5 --slope-mas 20,60 --trend-above 50 --trend-cross 50,200 --adx-min 20 `
      --turn-shares ws --turn-rank-mode ratio --bull-refill --turn-cap 0.20 --turn-tail 0.48 `
      --min-mcap 500 --regime-ma $reg --regime-window 21 --bear-etf-only --bear-min-score 0 `
      --bear-rank both --bear-intersect-top $top --bear-slope-window 120 `
      --start 2021-11-13 --end 2026-10-26 --tag "reg$reg`_t$top"
  }
}
```

3. 结果汇总（一个目录一行）：

```powershell
python -c "import pandas as pd,glob;rows=[(d.split('etf/')[1],pd.read_csv(d+'/stats.csv',index_col=0).loc['个股版']) for d in glob.glob('reports/etf/*')];df=pd.DataFrame([r[1] for r in rows],index=[r[0] for r in rows]);print(df.sort_values('夏普',ascending=False).round(3).to_string())"
```

**纪律**：参数扫描只用于**稳健性检验**（看结论是否处处成立），不能挑夏普最高的那一档当结论。
市值门槛 100/200/300/500 四档收益不单调（500 最好、300 最差）就是明证。

---

## 8. 自检与对账

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q          # test_rsrs / test_mcap / test_turnover / test_calendar / test_bar_source
```

跑完回测按顺序看四件事：

1. **期数**：`holds.csv` 行数应为 60（5 年月频）。少于预期 → 预热不足或 anchor 偏移。
2. **stats.csv 三行**：与第 1 节的基准逐位比。
3. **空仓 / ETF 期数**：9 期空仓、4 期 ETF。突然变多 → 趋势闸或牛熊判定被改坏。
4. **挪动 `--start` 看稳定性**：起点平移一年，夏普不应出现数量级变化，否则是少数几期在撑。

---

## 9. 陷阱

### 9.1 `_merged` 只是标签，不是合并

`--tag merged` 仅在目录名末尾加 `_merged`。没有"多段拼接"的逻辑；**不要**手工往这个目录里追加 CSV，
下一次同参数运行会整个覆盖。要保留不同结论，用不同 tag。

### 9.2 `reports/` 被 gitignore，但两个看板必须入库

`.gitignore` 第 3 行忽略 `reports/`，只对 `reports/web/*.html` 开了白名单（见文件内注释）。
删掉白名单会让看板在下次提交时静默消失。

### 9.3 目录名是唯一可靠的参数记录

`build_web_report.parse_params()` 从目录名反推参数表。新增参数**必须**同时写进目录名拼接段
（`scripts/sector_stock_rsrs.py` 约 1218–1298 行），否则看板与后续接手者都无法还原口径。

### 9.4 `--trend-mode strong` 的预热很长

`strong` 要看 MA200，脚本按 `trend_w × 1.6 + 60` 日历日预热。`--start` 定得比 MA200 更长时，
开头整段会是 NaN，被误判成"都不满足趋势"→ 全程空仓。看到 0 笔交易先怀疑这个。

### 9.5 熊市判定是"占比"不是"单日"

`--regime-window 21` + 默认 `--regime-bear-ratio 0.5`：过去 21 日跌破 MA200 的天数占比 ≥50% 才算熊市。
写成单日跌破会在均线附近反复横跳，回测与推送都会变。

### 9.6 空仓是合法结果

9 期空仓不是 bug。候选全部不满足趋势闸时当期就空仓，**不要"兜底买一只"**——
兜底会让回测与实盘脱钩，也会把收益结构改掉。

### 9.7 换手率是小数不是百分数

`0.0086` = 0.86%。`成交量 ÷ 换手率 = 股本` 依赖这个单位。写成百分数会得到 100 倍偏差的股本。

### 9.8 Windows 中文乱码

PowerShell 默认代码页会让中文输出变乱码（`��ȡ��`），**只影响显示**，不影响 CSV 与脚本。
需要正常显示时先 `$env:PYTHONUTF8 = "1"`。

### 9.9 本机 `.py` 曾被加密

`docs/HANDOFF.md` 第 0 节记录过：本机部分 `.py` 曾被加密软件包了文件头。
当前工作区是明文（`git show` 可核），但导出给别人前用 `git show HEAD:path > path.txt` 验证一遍更稳。

---

## 10. 文件地图（回测相关）

| 路径 | 职责 |
|---|---|
| `scripts/sector_stock_rsrs.py` | **回测主脚本**：双层 RSRS + 趋势闸 + 熊市分支 + 净值计算 + 写盘 |
| `scripts/import_crsp_sp500.py` | CRSP CSV → parquet（成分区间 / 逐日行情 / 代码映射） |
| `scripts/compare_crsp_data.py` | CRSP 与本地四项对账，产出 `reports/data_audit/crsp_vs_local.md` |
| `src/quant/data/crsp.py` | CRSP 读取层：复权价 / 换手率 / 市值 / PIT 掩码 / SIC→板块 ETF |
| `docs/DATA_AUDIT_CRSP.md` | **数据审计**：收益虚高的来源、四项对账、归因实验、修正清单 |
| `scripts/sector_rsrs.py` | 单层板块 RSRS（`rsrs_panel` 参考实现、`anchors`、`perf` 被主脚本复用） |
| `scripts/build_web_report.py` | 主看板生成（`reports/web/index.html`） |
| `scripts/bear_etf_trends.py` | 熊市 ETF 副看板（`reports/web/bear_etfs.html`） |
| `scripts/backfill_turnover.py` | 用 SEC 股本回补 2021-01 ~ 2023-10 的换手率（`ws` 口径上线前的历史） |
| `src/quant/data/store.py` | `load_panel(field, symbols, start, end)` 读分区 parquet |
| `src/quant/data/universe.py` | `latest_file()` 市值快照路径 |
| `config/ref/us_sector_holdings.csv` | 板块 → 成分股映射（503 只） |
| `tests/test_rsrs.py` / `test_mcap.py` / `test_turnover.py` | 口径回归测试 |

`scripts/backfill_turnover.py` 用法：

```powershell
$env:PYTHONPATH = "src"
python -m scripts.backfill_turnover --dry-run                       # 先看覆盖率
python -m scripts.backfill_turnover --start 2021-01-01 --end 2023-10-01
```

（`--max-resid 0.10` 重叠期残差上限，超过整只不补；默认写盘前自动备份。）

---

## 11. 速查

```powershell
$env:PYTHONPATH = "src"

# 定稿回测（第 1 节完整命令）
python -m scripts.sector_stock_rsrs --data-source crsp --n 20 --m 40 --pick turnma ... --tag crsp

# 数据侧（CRSP 标准数据）
python -m scripts.import_crsp_sp500 --start 2020-01-01     # CSV → parquet
python -m scripts.compare_crsp_data                        # 四项对账报告

# 看板
python -m scripts.build_web_report --pattern "*_crsp" --out reports/web/index.html --title "…"
python -m scripts.bear_etf_trends
python -m http.server 8765 --directory reports/web

# 测试
python -m pytest -q

# 看某一期的决策
python -c "import pandas as pd;h=pd.read_csv(r'reports/etf/<参数串>/holds.csv');print(h[h['信号日']=='2026-09-30'].T)"
```

改口径前先做三件事：① `git log -p` 看这段是谁为什么改的；② 用第 1 节的基准跑一次确认没漂；
③ 把新数字写进本文第 1 节与 `docs/HANDOFF.md` 的对应行。**不留数字的结论等于没跑过。**

## 12. 数据源历史（为什么有两套口径）

| 时间 | 口径 | 同区间结果（2021-11 ~ 2025-12，50 期） |
|---|---|---|
| ~2026-11 之前 | `local`：yfinance 行情 + westock 换手率 + 今天 503 只快照 + 反推股本 | +637.9% / 夏普 1.52 |
| 2026-11 起 | `crsp`：标普 500 标准数据（PIT 成分 / 真实股本与市值 / 自带复权） | **+207.1% / 夏普 1.02（回撤 −41.6%）** |

差额约 2/3 来自数据错误：平均每期混入 58 只当时不在指数里的票、漏掉 58 只当时还在的票；
换手率对多股本结构（GOOG/GOOGL、FOX/FOXA、NWS、DELL）低估 2~3 倍；市值在 500 亿门槛上 1% 的票日判反。
完整证据见 `docs/DATA_AUDIT_CRSP.md`。
