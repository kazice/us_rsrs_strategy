"""修复 etf_daily 的 rsrs20_40_raw / rsrs20_40_z 不自洽。

缺陷
----
表里的 ``rsrs20_40_z`` 不是用同表的 ``rsrs20_40_raw`` 归一化出来的：

    只有 1 行自洽（17,688 行里 1 行）
    反解出的标准差 0.12，实际 40 日标准差 0.141
    试遍 M=20/40/60/120/250 各种窗口都复现不出原值

后果很直接：板块排序用的是 z，而实盘无论怎么算都复现不了回测里的名次。
用「表内 raw + M=40 不含当日」重算，2026 年有 13 天（5.9%）的 top3 板块会变。

修法
----
raw 由 high/low 推出（与表内原值几乎一致，最大差 0.002），z 用同表 raw 的
最近 40 个归一化，两列从此共用一条序列。

用法：
    python upload/fix_etf_rsrs.py --target upload          # 只报告
    python upload/fix_etf_rsrs.py --target upload --write
    python upload/fix_etf_rsrs.py --target both --write
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "latest_data"
OUT = ROOT / "upload"
M = 40


def _load_etf_mod():
    spec = importlib.util.spec_from_file_location("update_etf", OUT / "update_etf.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["update_etf"] = mod
    spec.loader.exec_module(mod)
    return mod


def recompute(df: pd.DataFrame, warm: pd.DataFrame | None, ue):
    """按「high/low → raw → 40 日标准化」重算，返回同结构的新表。"""
    out = []
    for s, g in df.groupby("etf_symbol", observed=True):
        g = g.sort_values("date").reset_index(drop=True)
        w = (warm[warm.etf_symbol == s].sort_values("date")
             if warm is not None and len(warm) else None)
        cols = ["date", "high", "low"]
        full = pd.concat([w[cols], g[cols]], ignore_index=True) if w is not None else g[cols]
        raw, z = ue.rsrs_z(full.high.to_numpy(float), full.low.to_numpy(float))
        if w is not None:
            raw, z = raw[len(w):], z[len(w):]
        new = g.copy()
        new["rsrs20_40_raw"] = raw
        new["rsrs20_40_z"] = z
        if "rsrs20_40_raw_state" in new.columns:
            new["rsrs20_40_raw_state"] = np.where(np.isnan(raw), 2, 0).astype("int8")
        if "rsrs20_40_z_state" in new.columns:
            new["rsrs20_40_z_state"] = np.where(np.isnan(z), 2, 0).astype("int8")
        out.append(new)
    return pd.concat(out, ignore_index=True)


def selfcheck(df: pd.DataFrame) -> float:
    worst = 0.0
    for _, g in df.groupby("etf_symbol", observed=True):
        g = g.sort_values("date").reset_index(drop=True)
        r = g.rsrs20_40_raw.to_numpy(float)
        z = g.rsrs20_40_z.to_numpy(float)
        for i in range(M, len(g)):
            h = pd.Series(r[i - M:i]).dropna()
            if len(h) < 20 or h.std() == 0 or np.isnan(r[i]):
                continue
            worst = max(worst, abs((r[i] - h.mean()) / h.std() - z[i]))
    return worst


def report_moved(old: pd.DataFrame, new: pd.DataFrame) -> None:
    last = old.date.max()
    a = old[old.date == last].set_index("etf_symbol").rsrs20_40_z
    b = new[new.date == last].set_index("etf_symbol").rsrs20_40_z
    both = pd.concat([a.rename("old"), b.rename("new")], axis=1).dropna()
    both["r_old"] = both.old.rank(ascending=False)
    both["r_new"] = both["new"].rank(ascending=False)
    moved = both[both.r_old != both.r_new]
    print(f"  {last.date()} 名次变化 {len(moved)}/{len(both)}")
    if len(moved):
        print(moved.sort_values("r_new").to_string(float_format=lambda v: f"{v:.3f}"))


def run(target: str, write: bool) -> None:
    ue = _load_etf_mod()
    if target in ("upload", "both"):
        up = pd.read_parquet(OUT / "etf_daily.parquet")
        base = pd.read_parquet(SRC / "etf_daily.parquet")
        warm = base[base.date < up.date.min()].copy()
        new = recompute(up, warm, ue)
        print(f"[upload] 行 {len(up):,} {up.date.min().date()}~{up.date.max().date()}"
              f"  自洽误差 {selfcheck(new):.2e}")
        report_moved(up, new)
        if write:
            shutil.copy2(OUT / "etf_daily.parquet", OUT / "etf_daily.bak.parquet")
            new[list(up.columns)].sort_values(["etf_symbol", "date"]).reset_index(drop=True) \
                .to_parquet(OUT / "etf_daily.parquet", index=False, compression="zstd")
            print("  已写回 upload/etf_daily.parquet")

    if target in ("latest", "both"):
        lt = pd.read_parquet(SRC / "etf_daily.parquet")
        new = recompute(lt, None, ue)
        print(f"[latest] 行 {len(lt):,} {lt.date.min().date()}~{lt.date.max().date()}"
              f"  自洽误差 {selfcheck(new):.2e}")
        report_moved(lt, new)
        if write:
            shutil.copy2(SRC / "etf_daily.parquet", SRC / "etf_daily.bak.parquet")
            new[list(lt.columns)].sort_values(["etf_symbol", "date"]).reset_index(drop=True) \
                .to_parquet(SRC / "etf_daily.parquet", index=False, compression="zstd")
            print("  已写回 latest_data/etf_daily.parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="upload", choices=["upload", "latest", "both"])
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    run(a.target, a.write)
    if not a.write:
        print("\n（dry-run，未写入；加 --write 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
