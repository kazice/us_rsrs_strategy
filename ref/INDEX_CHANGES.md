# 候选池调入调出历史

> 由 `quant.live.pool.refresh()` 在每次成功刷新 SSGA holdings 后重新生成，
> 数据源是同目录的 `index_changes.csv`。**不要手工编辑**，改了下次会被覆盖。

> 当前快照：`us_sector_holdings.csv`，as of 2026-11-11，503 只

## 状态

data 分支还没有 `us_sector_holdings.csv` —— 说明 `pool.refresh()` 至今没有成功拉过
SSGA 的 11 份 holdings（国内网络不可达，只有在 GitHub Actions 美区才通）。现在实际
生效的是仓库里的 `config/ref/us_sector_holdings.csv`（静态快照，as of 2026-11-11）。
首次刷新成功后，本文件会被真正的变更历史覆盖。

## 汇总

| 动作 | 次数 |
|---|---|
| 纳入 | 0 |
| 调出 | 0 |
| 换板块 | 0 |

## 明细

_还没有记录到任何调入调出。_
