"""把 ``turnover_daily.parquet`` 修成「能直接信」的面板。

为什么需要
----------
实盘换手率面板是三段拼出来的：``ws``（当日实时报价，权威）、``ws_new``、``sec_calc``
（用 SEC 申报股本回补的历史段）。前两段基本可信，``sec_calc`` 有硬错误：

* 151 行 ``turnover > 100%``（VTRS 单日 449,828 倍，股本字段取成了 100 股量级）；
* 33 只票的 20 日均换手与 CRSP 口径（池层 ``turnover``）中位偏差超过 10%
  （多类别股份、回购房本、XBRL 单位错误都会造成）。

排序用的是 ``turn20²/turn120``，平方项会把 0.6 倍的股本误差放大成 2.8 倍的分差，
直接决定同板块前 3 是谁。所以口径漂移不是"好看不好看"的问题，是选股错误。

修法（只动有权威对照的区间）
--------------------------
1. **物理约束**：``0 < turnover ≤ 1``，否则置空（留 NaN 比留错值安全——整列算不出来的票
   会被显式剔出排序，而不是拿垃圾值参与排名）。
2. **与池层对账**：2021-01-04 ~ 2025-12-31 池层 ``turnover`` 来自 CRSP（成交量 ÷ 当日总股本），
   是权威口径。逐票算 ``median(pool / live)``：
   - 样本 ≥60 天且中位偏离 > 5% → 该票整段改用池层值，``src='pool_crsp'``；
   - 偏离 ≤5% → 保留原值（两套口径等价，不必覆盖）。
3. **2026 段不动**：池层 2026 是延展段，股本按 2025-12-31 常数处理，回购房本的票会偏；
   实盘 ``ws`` 拿的是当日真实换手，反而更准，只做物理约束。

用法
----
    python -m scripts.repair_turnover_panel --dry-run
    python -m scripts.repair_turnover_panel
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import pool, store
from quant.live.pool import normalize_symbol

log = logging.getLogger(__name__)

TURNOVER_FILE = Path("curated") / "fundamentals" / "turnover_daily.parquet"
AUDIT_FILE = Path("reports") / "data_audit" / "turnover_repair.csv"
POOL_END = pd.Timestamp("2025-12-31")     # 池层 CRSP 段末日
MAX_TURNOVER = 1.0                        # 单日 100% 换手，超过必错
RECAL_TOL = 0.05                          # 允许的口径偏差
MIN_SAMPLE = 60


def turnover_path() -> Path:
    return store.data_root() / TURNOVER_FILE


def _pool_turnover_long() -> pd.DataFrame:
    """池层 turnover 摊成 (symbol, date, pool) 长表，按当时 ticker 对齐。"""
    u = pool.universe()
    ids = [str(i) for i in u["security_id"]]
    panel = pool.panel("turnover", ids, "2021-01-01", "2026-12-31")
    if panel.empty:
        return pd.DataFrame(columns=["symbol", "date", "pool"])

    tk = pool.tickers()[["security_id", "ticker", "start", "end"]].copy()
    tk["security_id"] = tk["security_id"].astype(str)
    long = (panel.rename_axis("date").reset_index()
            .melt(id_vars="date", var_name="security_id", value_name="pool"))
    long = long.merge(tk, on="security_id", how="inner")
    long = long[(long["date"] >= long["start"])
                & (long["end"].isna() | (long["date"] <= long["end"]))]
    long["symbol"] = long["ticker"].map(normalize_symbol)
    return (long.dropna(subset=["pool"])
            .groupby(["symbol", "date"], as_index=False)["pool"].max())


def repair(dry_run: bool = False) -> pd.DataFrame:
    path = turnover_path()
    if not path.exists():
        raise SystemExit(f"换手率面板不存在：{path}")
    tv = pd.read_parquet(path)
    tv["date"] = pd.to_datetime(tv["date"])
    raw_rows = len(tv)

    # ---- 1. 物理约束 ----
    bad = (tv["turnover"] <= 0) | (tv["turnover"] > MAX_TURNOVER)
    n_bad = int(bad.sum())
    tv.loc[bad, "turnover"] = np.nan

    # ---- 2. 与池层对账（只覆盖有权威对照的区间）----
    pl = _pool_turnover_long()
    pl = pl[pl["date"] <= POOL_END]
    merged = tv.merge(pl, on=["symbol", "date"], how="inner")
    merged = merged.dropna(subset=["turnover", "pool"])
    ratio = merged["pool"] / merged["turnover"]

    per = (ratio.groupby(merged["symbol"]).agg(n="size", med="median")
           .query("n >= @MIN_SAMPLE"))
    off = per[(per["med"] - 1).abs() > RECAL_TOL].sort_values("med")
    log.info("对账样本 %d 条，偏离超 %.0f%% 的票 %d 只", len(ratio), RECAL_TOL * 100, len(off))

    use = set(off.index)
    cov = tv.merge(pl, on=["symbol", "date"], how="left")
    hit = cov["symbol"].isin(use) & cov["pool"].notna()
    n_cover = int(hit.sum())
    cov.loc[hit, "turnover"] = cov.loc[hit, "pool"]
    if "src" in cov.columns:
        cov.loc[hit, "src"] = "pool_crsp"
    out = cov.drop(columns=["pool"])[tv.columns]
    out["turnover"] = out["turnover"].clip(lower=0, upper=MAX_TURNOVER)

    audit = pd.DataFrame({
        "symbol": off.index, "overlap_days": per.loc[off.index, "n"].to_numpy(),
        "ratio_median": off["med"].round(4).to_numpy(),
        "covered_rows": [int((hit & (cov["symbol"] == s)).sum()) for s in off.index],
        "action": "replaced_by_pool",
    }).sort_values("ratio_median")

    print(f"原 {raw_rows:,} 行；物理剔除 {n_bad} 行；票级口径覆盖 {n_cover:,} 行 "
          f"（{len(use)} 只）")
    print(f"覆盖后空值 {int(out['turnover'].isna().sum()):,} 行")

    if dry_run:
        print("--dry-run：不写盘")
        return audit

    audit_dir = Path(store.data_root()) / AUDIT_FILE.parent
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit.to_csv(Path(store.data_root()) / AUDIT_FILE, index=False, encoding="utf-8-sig")
    shutil.copy2(path, path.with_suffix(".parquet.bak"))
    out.to_parquet(path, index=False)
    print(f"已写盘（备份 {path.name}.bak），审计表 {AUDIT_FILE}")
    return audit


def main() -> int:
    ap = argparse.ArgumentParser(description="修复实盘换手率面板")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    audit = repair(dry_run=args.dry_run)
    if not audit.empty:
        print("\n偏差最大的 15 只（ratio_median < 1 表示实盘原本偏高）：")
        print(audit.head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
