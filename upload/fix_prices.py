"""修复 bars_daily 的复权断层（在 inceptio 上直接改）。

问题
----
原表 close 在每个除息日把复权因子重置回 1.0（而非累积），除息当天凭空多出 +7%~12%
（VICI 2022-06-08 / CPT 2022-04-04 / INVH 2022-09-19，已用 yahoo adj_close 证实）；
反向地，个别特别股息又完全没计入（TDG 2025-09-02 的 6.4%）。

做法
----
1. 价格列改为 yahoo adj_close（总收益复权），OHLC 同比例缩放；
2. 指标用 upload/update.py 的 compute_indicators 重算，预热区间取 2019-01 起；
3. yahoo 缺少某行/某列时保留原值，绝不用 NaN 覆盖；
4. 未匹配 yahoo 的 permno（.OLD 占位行 / 已退市 / 无覆盖）原样保留；
5. 校验用「信号等价性」（趋势闸 6 条的布尔结果）而非绝对数值——复权基准不同，
   均线绝对值必然不同，但信号应当一致。

用法
----
    python upload/fix_prices.py --dry-run
    python upload/fix_prices.py
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "latest_data"
YAHOO = Path("c:/Users/12146/CodeBuddy/us_rsrs_strategy/data/curated/bars_daily")
WARMUP = pd.Timestamp("2019-01-01")

_spec = importlib.util.spec_from_file_location("inc_update", ROOT / "upload" / "update.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
compute_indicators = _mod.compute_indicators

PRICE_COLS = ["open", "high", "low", "close"]
IDX_COLS = ["ma20", "ma50", "ma60", "ma200", "slope20_5d", "slope60_5d", "slope20_10d",
            "adx14", "plus_di14", "minus_di14", "rsrs20_40_raw", "rsrs20_40_z"]
STATE_COLS = [c + "_state" for c in IDX_COLS]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    bars = pd.read_parquet(SRC / "bars_daily.parquet")
    syms = bars["symbol"].astype(str)
    print(f"原表 {len(bars):,} 行 | {bars.permno.nunique()} permno | "
          f"{bars.date.min().date()} ~ {bars.date.max().date()}")

    # 每票取一个代表 symbol（用于找 yahoo 历史做预热）
    rep = (bars[~syms.str.endswith(".OLD")]
           .sort_values("date").groupby("permno")["symbol"].last())

    cache: dict[int, pd.DataFrame] = {}
    for p, s in rep.items():
        f = YAHOO / f"symbol={s}" / "part.parquet"
        if not f.exists():
            continue
        d = pd.read_parquet(f, columns=["date", "open", "high", "low", "close", "adj_close"])
        d = d[(d.date >= WARMUP) & (d.date <= bars.date.max())].sort_values("date")
        d = d.dropna(subset=["close", "adj_close"])
        if len(d) < 30:
            continue
        fac = (d["adj_close"] / d["close"]).astype("float64")
        g = pd.DataFrame({
            "date": d["date"].values,
            "open": (d["open"] * fac).values,
            "high": (d["high"] * fac).values,
            "low": (d["low"] * fac).values,
            "close": d["adj_close"].astype("float64").values,
            "amount": np.nan,
            "mcap": np.nan,
        })
        cache[int(p)] = compute_indicators(g).set_index("date")

    print(f"可用 yahoo 预热 {len(cache)} 只 / 共 {len(rep)} 只")

    res = bars.copy()

    def fill_mask(cname, mask, series):
        """把 series 的非空值写入 res[cname] 的 mask 行，空值保留原值。"""
        col = res[cname].astype("float64")
        old = col[mask].values
        new = series.values
        col.loc[mask] = np.where(pd.isna(new), old, new)
        res[cname] = col

    for p, g in cache.items():
        mask = res.permno == p
        if not mask.any():
            continue
        dates = res.loc[mask, "date"]
        for cname in PRICE_COLS + IDX_COLS + STATE_COLS:
            if cname in g.columns:
                fill_mask(cname, mask, dates.map(g[cname]))

    # dtype 还原（state 的缺失按口径记 2 = 意外缺失）
    for cname in PRICE_COLS + IDX_COLS + STATE_COLS:
        if cname not in res.columns:
            continue
        if cname in STATE_COLS:
            res[cname] = res[cname].fillna(2).astype(bars[cname].dtype)
        else:
            res[cname] = res[cname].astype(bars[cname].dtype)

    # ---- 校验：趋势闸信号等价性 ----
    def gates(df):
        return pd.DataFrame({
            "ma20>ma60": df.ma20 > df.ma60,
            "slope20_10d>0": df.slope20_10d > 0,
            "slope60_5d>0": df.slope60_5d > 0,
            "close>ma50": df.close > df.ma50,
            "ma50>ma200": df.ma50 > df.ma200,
            "adx>10": df.adx14 > 10,
        })

    key = ["permno", "date"]
    b0 = bars.set_index(key).sort_index()
    b1 = res.set_index(key).sort_index()
    same_px = (b0[PRICE_COLS] - b1[PRICE_COLS]).abs().max(axis=1) < 1e-6
    sub0, sub1 = b0[same_px], b1[same_px]
    need = ["ma20", "ma60", "slope20_10d", "slope60_5d", "close", "ma50", "ma200", "adx14"]
    ok = sub0[need].notna().all(axis=1)
    dif = gates(sub0).ne(gates(sub1)) & ok.values[:, None]
    print("\n=== 校验：价格未变的行，趋势闸 6 条布尔结果是否一致 ===")
    print(f"  样本 {int(ok.sum()):,} 行")
    for c in dif.columns:
        n = int(dif[c].sum())
        print(f"  {c:<14} 不一致 {n:>6} 行 ({n / max(int(ok.sum()), 1):.4%})")

    print("\n=== 校验：列级 NaN 数量（新 / 原）===")
    for c in PRICE_COLS + IDX_COLS:
        print(f"  {c:<14}{int(res[c].isna().sum()):>8} /{int(bars[c].isna().sum()):>8}")

    print("\n=== 关键样本日收益 ===")
    for s in ["VICI", "CPT", "INVH", "TDG"]:
        g0s = bars[syms == s].set_index("date")["close"].sort_index()
        g1s = res[syms == s].set_index("date")["close"].sort_index()
        j = g0s.index.intersection(g1s.index)
        r0, r1 = g0s.loc[j].pct_change(), g1s.loc[j].pct_change()
        print(f"  {s}:")
        for dt in ["2022-06-08", "2022-04-04", "2022-09-19", "2025-09-02"]:
            k = pd.Timestamp(dt)
            if k in r0.index and pd.notna(r0.loc[k]):
                print(f"    {dt}  原 {r0.loc[k]:+.2%} -> 修复 {r1.loc[k]:+.2%}")

    if a.dry_run:
        print("\n[dry-run] 未写盘")
        return 0

    bak = SRC / "bars_daily.bak.parquet"
    if not bak.exists():
        shutil.copy2(SRC / "bars_daily.parquet", bak)
        print(f"\n已备份 -> {bak.name}")
    out = res[bars.columns.tolist()].sort_values(["permno", "date"]).reset_index(drop=True)
    out.to_parquet(SRC / "bars_daily.parquet", index=False)
    print(f"已写回 {SRC / 'bars_daily.parquet'}  {len(out):,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
