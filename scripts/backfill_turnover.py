"""回补历史换手率：用「成交量 ÷ SEC 申报股本」重建 ``turnover_daily.parquet`` 的早期区间。

为什么需要
----------
``turnover_daily.parquet`` 的 ws 口径来自当日实时报价，只能当天抓，历史抓不回来。
实测该文件最早只到 2023-04-03，把回测往前推到 2021 年时，换手率面板整段是空的，
「板块内按 20 日均换手率取前 3」这一步会退化成「谁有数据谁入选」。

口径怎么对齐
------------
1. **成交量去拆股调整**：本地 bars 的 volume 是前复权到最新基准的（NFLX 2025-11-17
   拆 10:1，拆股前后 volume 同在 4000 万股量级），所以先按累计拆股倍数还原成当期真实
   股数：``vol_true = volume × cum_split(t) / cum_split(末日)``。
2. **股本用 PIT**：SEC 申报按 ``filed ≤ 交易日`` 取最后一条，避免用到未来才披露的股本。
3. **逐票校准系数 k**：多类别股份公司（V/MA/CME 只申报 A 类股）与快照缺口会带来稳定
   倍数偏差，用重叠期（有 ws 的区间）的 ``median(ws / calc)`` 吸收。留出法验证：
   k 用 2023-10~2024-12 校准、2025-01~2026-11 检验，467 只里 97.9% 的票中位偏差 ≤5%。

写入约定
--------
- 只补 ``--start`` ~ ``--end`` 之间**尚无 ws 数据**的 (symbol, date)，src 记 ``sec_calc``；
- 重跑幂等：先删掉范围内已有的 ``sec_calc`` 行再写；
- 校准残差（重叠期 median 相对偏差）超过 ``--max-resid`` 的票整只不补 —— 宁可留空，
  也不要把错一个数量级的换手率塞进排序。

用法
----
    python -m scripts.backfill_turnover --dry-run          # 只看覆盖率
    python -m scripts.backfill_turnover --start 2021-01-01 --end 2023-10-01
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import store

log = logging.getLogger(__name__)

TURNOVER_FILE = Path("curated") / "fundamentals" / "turnover_daily.parquet"
SRC = "sec_calc"
MIN_OVERLAP_DAYS = 60      # 校准系数至少要有这么多天重叠样本，否则不予校准


def turnover_path() -> Path:
    return store.data_root() / TURNOVER_FILE


def _load_turnover() -> pd.DataFrame:
    path = turnover_path()
    if not path.exists():
        raise SystemExit(f"换手率面板不存在：{path}")
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _shares_pit(index: pd.DatetimeIndex) -> pd.DataFrame:
    """把 SEC 申报股本摊到每个交易日（filed ≤ 当日 的最后一条）。"""
    path = store.data_root() / "curated" / "fundamentals" / "shares_outstanding.parquet"
    sh = pd.read_parquet(path)
    sh["filed"] = pd.to_datetime(sh["filed"])
    sh = sh[sh["val"] > 0].sort_values("filed")

    cols: dict[str, np.ndarray] = {}
    base = pd.DataFrame({"date": index})
    for sym, g in sh.groupby("symbol"):
        s = pd.merge_asof(
            base, g[["filed", "val"]].rename(columns={"filed": "date"}),
            on="date", direction="backward",
        )
        cols[sym] = s["val"].to_numpy()
    return pd.DataFrame(cols, index=index)


def _volume_unjiggle(vol: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """把前复权成交量还原成当期真实股数。

    ``cum_split`` 是「从区间起点到该日」的累积拆股倍数，除以末日的累积值就得到
    「该日之后还会发生的拆股倍数」，乘回去即还原。
    """
    spl = store.load_panel(field="stock_splits", symbols=list(vol.columns),
                           start=start, end=end).reindex(vol.index)
    spl = spl.reindex(columns=vol.columns).fillna(1.0)
    spl = spl.where(spl > 0, 1.0)
    cum = spl.cumprod()
    return vol * cum.div(cum.iloc[-1], axis=1)


def build(start: str, end: str, max_resid: float = 0.10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (待写入的长表, 逐票诊断表)。"""
    tv = _load_turnover()
    ws = (tv[tv["src"].isin(["ws", "ws_new"])]
          .pivot(index="date", columns="symbol", values="turnover").sort_index())
    if ws.empty:
        raise SystemExit("重叠期没有 ws 口径数据，无法校准")

    symbols = [s for s in ws.columns if store.bars_file(s).exists()]
    # 取数区间必须早于 start：cum_split 要用到末日累积值，且 PIT 股本需要更早的申报
    load_start = min(pd.Timestamp(start), ws.index.min()) - pd.Timedelta(days=30)
    vol = store.load_panel(field="volume", symbols=symbols,
                           start=load_start.date().isoformat(),
                           end=max(pd.Timestamp(end), ws.index.max()).date().isoformat()).sort_index()
    if vol.empty:
        raise SystemExit("本地没有成交量面板")

    vol_true = _volume_unjiggle(vol, load_start.date().isoformat(), vol.index.max().date().isoformat())
    shares = _shares_pit(vol.index).reindex(columns=vol_true.columns)
    calc = vol_true / shares

    overlap = calc.index.intersection(ws.index)
    overlap = overlap[overlap >= ws.index.min()]
    a, b = calc.loc[overlap], ws.loc[overlap]
    both = a.notna() & b.notna() & (b > 0)

    k, resid, ndays = {}, {}, {}
    for sym in calc.columns:
        m = both[sym]
        if int(m.sum()) < MIN_OVERLAP_DAYS:
            continue
        ratio = (b.loc[m, sym] / a.loc[m, sym]).replace([np.inf, -np.inf], np.nan).dropna()
        if len(ratio) < MIN_OVERLAP_DAYS:
            continue
        k[sym] = float(ratio.median())
        ndays[sym] = int(len(ratio))
    diag = pd.DataFrame({"k": pd.Series(k), "overlap_days": pd.Series(ndays)})
    for sym in diag.index:
        adj = a[sym] * k[sym]
        m = both[sym] & adj.notna()
        resid[sym] = float(((adj[m] - b.loc[m, sym]).abs() / b.loc[m, sym]).median()) if m.sum() else np.nan
    diag["resid"] = pd.Series(resid)
    diag["usable"] = diag["resid"] <= max_resid

    keep = sorted(diag.index[diag["usable"]])
    win = calc.loc[(calc.index >= pd.Timestamp(start)) & (calc.index <= pd.Timestamp(end)), keep]
    filled = win.mul(pd.Series({s: k[s] for s in keep}), axis=1)

    exist = tv[["symbol", "date"]].drop_duplicates()
    exist_keys = set(zip(exist["symbol"], exist["date"]))
    long = filled.stack().rename("turnover").reset_index()
    long.columns = ["date", "symbol", "turnover"]
    long = long[long["turnover"] > 0]

    # volume 填「turnover × 股本」，保证下游用 volume / turnover 反推股本时自洽。
    # 用 merge 而不是两次 stack 后按位置相乘：stack 默认丢 NaN，两边的行数/顺序不一定对齐。
    share_long = (shares.reindex(index=filled.index, columns=keep)
                  .rename_axis(index="date", columns="symbol")
                  .stack().rename("shares").reset_index())
    long = long.merge(share_long, on=["date", "symbol"], how="left")
    long["volume"] = long["turnover"] * long["shares"]
    long = long.drop(columns="shares")

    long = long[[(s, d) not in exist_keys for s, d in zip(long["symbol"], long["date"])]]
    long["src"] = SRC
    return long[["symbol", "date", "turnover", "volume", "src"]], diag


def main() -> int:
    ap = argparse.ArgumentParser(description="用 SEC 股本回补历史换手率")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2023-10-01", help="补到 ws 口径开始之前")
    ap.add_argument("--max-resid", type=float, default=0.10, help="重叠期残差上限，超过整只不补")
    ap.add_argument("--dry-run", action="store_true", help="只打印覆盖率，不写盘")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    long, diag = build(args.start, args.end, args.max_resid)
    log.info("可校准 %d 只，其中可用 %d 只（残差 ≤ %.0f%%）",
             len(diag), int(diag["usable"].sum()), args.max_resid * 100)
    if not long.empty:
        log.info("待写入 %d 行，%d 只，%s ~ %s", len(long), long["symbol"].nunique(),
                 long["date"].min().date(), long["date"].max().date())
        log.info("每只票的平均行数 %.0f", len(long) / long["symbol"].nunique())

    dropped = diag[~diag["usable"]].sort_values("resid", ascending=False)
    if len(dropped):
        log.info("残差过大不补的 %d 只：%s", len(dropped),
                 ", ".join(f"{s}({r:.2f})" for s, r in dropped["resid"].head(12).items()))

    if args.dry_run or long.empty:
        return 0

    path = turnover_path()
    if not args.no_backup:
        bak = path.with_suffix(".parquet.bak")
        shutil.copy2(path, bak)
        log.info("已备份 %s", bak)

    old = _load_turnover()
    lo, hi = pd.Timestamp(args.start), pd.Timestamp(args.end)
    keep = ~((old["src"] == SRC) & (old["date"] >= lo) & (old["date"] <= hi))
    merged = (pd.concat([old[keep], long], ignore_index=True)
              .sort_values(["symbol", "date"]).reset_index(drop=True))
    tmp = path.with_suffix(".tmp")
    merged.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    log.info("写入完成：%d 行、%d 只，最新 %s", len(merged), merged["symbol"].nunique(),
             f"{merged['date'].max():%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
