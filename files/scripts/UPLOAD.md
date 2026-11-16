# 模拟盘仓库 · 上传指南

本文件夹只装**模拟盘运行需要的主仓文件**（38 个），不含因子研究、回测、期权势垒那些模块，
也不含行情数据（行情在 `data` 分支，见第 8 节）。

## 0. 文件夹结构

```
trade_agent_paper/
├── UPLOAD.md      ← 本文件，不用上传
├── README.md      ← 要放到仓库根
├── .gitignore     ← 要放到仓库根
└── files/         ← 38 个待上传文件，路径即仓库路径
    ├── pyproject.toml
    ├── .github/workflows/paper_trade.yml
    ├── config/...
    ├── docs/...
    ├── scripts/*.py.txt
    └── src/quant/...
```

`files/` 里的目录层级就是仓库里的层级；`.py` 后面多一个 `.txt`（原因见第 1 节）。
**根目录的 `README.md` 和 `.gitignore` 也要一起上传**，它们不在 `files/` 里。

---

## 1. 先看这一节：为什么代码文件是 `.py.txt`

公司电脑上装了透明加密软件（文件头 `%TSD-Header-###%`），**`.py` 落盘即被加密**。
实测结论（就是在这个目录里测的）：

| 操作 | 结果 |
|---|---|
| 复制 `mcap.py` → `mcap.py` | 仍是 `%TSD-Header-###%` 乱码，**上传到 GitHub 会是乱码** |
| 复制 `mcap.py` → `mcap.py.txt` | **明文**，可以正常复制粘贴/上传 |

所以 `files/` 里：

- **`.py` 一律保存为 `<原名>.py.txt`** —— 内容是明文，给「新建文件时粘贴」用；
- **其他扩展名**（`.md` `.yml` `.yaml` `.csv` `.toml`）本来就是明文，按原路径原样放。

⚠ **不要把本地的 `.py` 直接拖到 GitHub**。你没装加密软件的机器上没问题，但这台机器上
拖上去就是乱码。

---

## 2. 方式 A：GitHub 网页逐个新建（不用 push，推荐）

需要粘贴的只有 31 个 `.py`，另外 9 个文件可以直接拖拽上传。

**批量部分（9 个文件，直接上传）**

GitHub 仓库页 → `Add file` → `Upload files`，把下列文件按对应路径拖进去：

```
README.md                            # 在本文件夹根，不在 files/ 里
.gitignore                           # 同上；拖不上去就 Create new file 建同名文件再粘贴
pyproject.toml
config/settings.yaml
config/ref/us_sector_holdings.csv
docs/HANDOFF.md                      # 交接文档，接手的人/AI 先读这个
docs/PAPER_TRADING.md
docs/LIVE_LOG.md
.github/workflows/paper_trade.yml
```

拖拽后 GitHub 会保留相对路径；如果路径丢了，用 `/` 在文件名里手动分段也可以。

**粘贴部分（31 个 `.py`）**

对每个 `files/**/xxx.py.txt`：

1. 仓库页 → `Add file` → `Create new file`
2. 文件名框填**去掉 `.txt` 的完整路径**，例如 `src/quant/live/mcap.py`
   （输入 `/` 会自动变成目录层级）
3. 在 VSCode 里打开对应的 `xxx.py.txt`，全选（Ctrl+A）复制，粘贴到网页编辑框
4. `Commit changes`

31 个文件大约 30~40 分钟。建议按这个顺序，出错时容易定位：

1. `src/quant/__init__.py`、`src/quant/config.py`
2. `src/quant/data/**`（12 个）
3. `src/quant/live/**`（10 个）
4. `scripts/**`（8 个）
5. 最后放 `.github/workflows/paper_trade.yml`

---

## 3. 方式 B：在能上外网的机器上还原后 `git push`

前提：那台机器**没有装同一套加密客户端**（家里电脑/云主机）。

1. 把整个 `trade_agent_paper` 拷过去（U 盘、网盘都行）；
2. 在 `files/` 目录里执行还原（PowerShell）：

```powershell
# .py.txt → .py
Get-ChildItem -Recurse -Filter "*.py.txt" | ForEach-Object {
    Rename-Item -Path $_.FullName -NewName ($_.Name -replace '\.py\.txt$', '.py')
}
```

3. 把 `files/` 里的内容挪到仓库根，再把本文件夹根的 `README.md` 和 `.gitignore` 一起放进去，然后：

```bash
git init
git add -A
git commit -m "paper trade: 模拟盘 + 市值门槛 + 日志系统"
git remote add origin https://github.com/<你>/<repo>.git
git push -u origin main
```

4. 还原后的目录应该长这样（`files/` 这一层不要带进去）：

```
.github/workflows/paper_trade.yml
config/settings.yaml
config/ref/us_sector_holdings.csv
docs/LIVE_LOG.md
docs/PAPER_TRADING.md
pyproject.toml
README.md
.gitignore
scripts/*.py
src/quant/*.py
src/quant/data/**/*.py
src/quant/live/*.py
```

---

## 4. 文件清单

「改动」列是相对原来那个研究仓：**新增** = 这次新写的，**修改** = 这次改过的，
**沿用** = 原样搬过来（模拟盘的依赖闭包）。

### 根目录与 CI

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `pyproject.toml` | 沿用 | 依赖声明；CI 里不装本项目，只用 `PYTHONPATH=src` |
| `.github/workflows/paper_trade.yml` | 修改 | 加了 `TA_RUN_ID`、`always()` 提交日志、push 重试、artifact |
| `README.md` | 新增 | 本仓说明（见同目录） |
| `.gitignore` | 新增 | 排除 data/reports/state/.venv/.env |

### 配置

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `config/settings.yaml` | 沿用 | 数据源降级链、路径。`paths.data` 被 `TA_DATA_ROOT` 覆盖 |
| `config/ref/us_sector_holdings.csv` | 沿用 | 成分股兜底快照（data 分支有刷新版时优先用那份） |

### 文档

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `docs/HANDOFF.md` | 新增 | **交接文档**：策略核心与公式、逐文件职责、数据结构、数据契约、12 条口径陷阱、改需求去哪改、禁止事项 |
| `docs/PAPER_TRADING.md` | 修改 | 同步 500 亿门槛、口径差异、文件职责、日志一节 |
| `docs/LIVE_LOG.md` | 新增 | 日志系统的完整说明：目录、字段、命令、13 条注意事项、排查顺序 |

### 入口脚本

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `scripts/__init__.py` | 沿用 | 让 `scripts` 成为包（`from scripts.xxx import` 需要） |
| `scripts/live_signal.py` | 修改 | 信号入口；接入 journal，所有出口都写流水 |
| `scripts/live_audit.py` | 新增 | 日志查询：`--date / --why / --runs / --gaps / --replay / --stats` |
| `scripts/paper_close_update.py` | 沿用 | 收盘后更新行情 + 换手率 + 刷新成分股 |
| `scripts/sector_stock_rsrs.py` | 沿用 | **间接依赖**：`quant.live.signal` 从这里取 `rsrs_panel_fast` |
| `scripts/sector_rsrs.py` | 沿用 | 上一行的依赖（`anchors` / `perf`） |
| `scripts/build_sector_holdings.py` | 沿用 | `pool.refresh()` 调用它抓 SSGA 官方 holdings |
| `scripts/archive_data_year.py` | 沿用 | data 分支按年归档，控制仓库体积 |

### 核心包

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `src/quant/__init__.py` | 沿用 | 包标记 |
| `src/quant/config.py` | 沿用 | yaml + 环境变量 + `.env`，`TA_DATA_ROOT` 覆盖 data 路径 |

### 数据层（`src/quant/data/`）

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `__init__.py` | 沿用 | 包标记 |
| `store.py` | 沿用 | parquet 读写、`load_panel`、交易日历、符号列表 |
| `fetch.py` | 沿用 | 回填与增量（yahoo → alpaca → tiingo 降级链） |
| `universe.py` | 沿用 | 全市场快照（首页市值/价格的兜底来源） |
| `validate.py` | 沿用 | 数据校验 |
| `sources/__init__.py`、`base.py`、`registry.py`、`yahoo.py`、`alpaca.py`、`tiingo.py` | 沿用 | 数据源适配层 |

### 模拟盘本体（`src/quant/live/`）

| 仓库路径 | 改动 | 说明 |
|---|---|---|
| `__init__.py` | 沿用 | 包标记 |
| `calendar.py` | 沿用 | 交易日历、当日收盘时间、执行窗口 |
| `quotes.py` | 沿用 | 腾讯/新浪批量报价（价格、量、换手率、时间戳） |
| `turnover.py` | 沿用 | 20 日均换手率（历史面板 + 当日累计值） |
| `pool.py` | 沿用 | 候选池：成分股快照、调入调出日志、代码归一 |
| `signal.py` | 修改 | 换手率先全池算再过滤；新增 `pool_table` 全池候选表（日志用） |
| `mcap.py` | 新增 | 500 亿美元市值门槛，与回测 `--min-mcap` 同口径 |
| `books.py` | 沿用 | 持仓/流水/幂等标记 |
| `notify.py` | 沿用 | pushplus 推送 |
| `journal.py` | 新增 | 运行流水 + 决策快照写入 |

---

## 5. 上线前必须做的三件事

1. **配 Secret**：`PUSHPLUS_TOKEN`（必填）。
   `Settings → Secrets and variables → Actions → New repository secret`。
   不配的话信号会 `abort`，不会推送。
2. **开写权限**：`Settings → Actions → General → Workflow permissions` 选
   **Read and write**，否则回写 data 分支会 403。
3. **确认 `data` 分支存在**（见第 8 节），workflow 第一步就要 checkout 它。

可选 Secret：`ALPACA_KEY_ID` / `ALPACA_SECRET` / `TIINGO_TOKEN`（yahoo 限流时的降级源）。

---

## 6. 上传后怎么校验

1. 仓库里 `.py` 文件数应为 **31**（若有 `.py.txt` 残留，说明漏改了文件名）；
2. 打开 `src/quant/live/mcap.py`，第一行应是
   `"""模拟盘候选池的市值门槛：与回测 ``--min-mcap 500`` 严格同口径。`
   如果看到 `%TSD-Header-###%`，说明传的是密文，删掉重传；
3. 打开 `.github/workflows/paper_trade.yml`，确认有 `TA_RUN_ID` 和 `if: always()`；
4. 手动跑一次：`Actions → paper-trade → Run workflow`，`kind=rebalance`，`dry_run=true`。
   绿了说明代码、依赖、data 分支都没问题（dry_run 不会推送、不会提交）。

---

## 7. 本包不含什么

| 不含 | 原因 | 怎么补 |
|---|---|---|
| 因子/回测/共振/期权势垒模块 | 与模拟盘无关 | 不需要 |
| `data` 分支（行情 parquet） | 约 150 MB，无法逐个粘贴 | 从现有 data 分支 `git clone`，或用能 push 的机器 |
| `reports/` 回测产物 | 研究用 | 不需要 |
| `state/live/` 日志 | 由 CI 运行后自动产生 | 不用手工创建，目录会自动建 |
| `.env` / 密钥 | 安全 | 用 GitHub Secrets |

---

## 8. 关于 `data` 分支（首次部署必看）

主仓只放代码，行情和历史数据在 `data` 分支，workflow 会把它 checkout 到 `./data`。
这个分支**不在本文件夹里**，因为它是 150 MB 级的 parquet，没法逐个文件交上去。

`data` 分支最小内容（缺了就跑不起来）：

```
curated/bars_daily/symbol=*/xxx.parquet     # 503 只成分股 + 11 只 ETF + SPY，约 3 年日线
curated/fundamentals/turnover_daily.parquet # 换手率面板（20 日均值、市值门槛都要它）
ref/us_sector_holdings.csv                  # 成分股快照（config/ref 那份的刷新版）
meta/universe/universe_latest.parquet       # 市值/价格快照（股本兜底要用）
```

获取方式（任选）：

- **能访问 GitHub 的机器**：`git clone -b data <repo>` 后把整个目录压成 zip 传过来；
- **从本机现有 data 目录**：本机 `d:\work\project\trade_agent\data` 下就是这份内容，
  压成 zip 后在同一台能 push 的机器上解压进 data 分支；
- 如果 data 分支已经存在且是最新的，**什么都不用做**，workflow 每次都会拉最新。

体积参考：`curated/bars_daily` 约 143 MB，`turnover_daily.parquet` 约 1.1 MB。
