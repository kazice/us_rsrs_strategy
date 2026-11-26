"""检查 upload 产物与 latest_data 的重叠区一致性。"""
from pathlib import Path

import numpy as np
import pandas as pd

U = Path("upload")
D = Path("latest_data")

print("=== 文件 ===")
for f in sorted(U.glob("*")):
    print(f"  {f.name:26} {f.stat().st_size/1e6:8.3f} MB")

up = pd.read_parquet(U / "bars_daily.parquet")
lc = pd.read_parquet(D / "bars_daily.parquet")

print(f"\n=== upload/bars_daily ===")
print(f"  shape {up.shape}   {up.date.min().date()} ~ {up.date.max().date()}   permno {up.permno.nunique()}")
print(f"  列: {list(up.columns)[:12]}...")

# 重叠区
ov = up.merge(lc, on=["permno", "date"], suffixes=("", "_lc"))
print(f"\n=== 与 latest_data 重叠 {len(ov):,} 行 ===")
if len(ov):
    for col in ["close", "amount", "mcap", "ma20", "adx14", "turn20", "turn_score"]:
        a, b = ov[col], ov.get(col + "_lc")
        if b is None or a.notna().sum() == 0:
            continue
        m = a.notna() & b.notna() & (b != 0)
        if m.sum() == 0:
            continue
        r = (a[m] / b[m]).replace([np.inf, -np.inf], np.nan).dropna()
        print(f"  {col:11} n={m.sum():>7,}  比值中位 {r.median():.6f}  "
              f"p05 {r.quantile(.05):.6f}  p95 {r.quantile(.95):.6f}  "
              f"±1%内 {(r.between(.99,1.01)).mean()*100:5.1f}%")

# 接点检查
print("\n=== 接点检查（2026-11-13）===")
last = lc.date.max()
a = up[up.date == last].set_index("permno").close
b = lc[lc.date == last].set_index("permno").close
j = pd.concat([a.rename("upload"), b.rename("latest")], axis=1).dropna()
if len(j):
    dev = (j.upload / j.latest - 1) * 100
    print(f"  可比 {len(j)} 只 | 偏差中位 {dev.median():+.4f}% | 最大 {dev.abs().max():.4f}%")

# 新增行
print(f"\n=== 新增行（latest_data 之后）===")
new = up[up.date > last]
print(f"  {len(new):,} 行  {new.date.min().date() if len(new) else '-'} ~ {new.date.max().date() if len(new) else '-'}")
if len(new):
    print(f"  mcap 空 {int(new.mcap.isna().sum())}  amount 空 {int(new.amount.isna().sum())}")
    print(new.groupby("date").size().head(8).to_string())
