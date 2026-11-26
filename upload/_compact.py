"""把 latest_data/bars_daily.parquet 转成 float32 + category，压缩到 100MB 以内。

为什么需要：GitHub 单文件上限 100MB，原始 127.3 MB 会推不上去。
代价：float64 → float32（约 7 位有效数字）。
  - 市值量级 1e11，float32 的绝对精度约 1e4 美元，相对误差 1e-7；
  - turn20 量级 1e-3，绝对精度约 1e-10；
  对策略（排序 + 阈值比较）无影响。
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path("latest_data/bars_daily.parquet")
TMP = Path("latest_data/_compact_tmp.parquet")

df = pd.read_parquet(SRC)
print(f"原始: {len(df):,} 行 × {len(df.columns)} 列  {SRC.stat().st_size/1e6:.1f} MB")

out = df.copy()
for c in out.select_dtypes("float64").columns:
    out[c] = out[c].astype("float32")
for c in ("symbol", "sector_etf"):
    if c in out.columns:
        out[c] = out[c].astype("category")

out.to_parquet(TMP, index=False, compression="zstd", compression_level=9)
print(f"压缩: {TMP.stat().st_size/1e6:.1f} MB")

# 精度校验
print("\n=== 精度校验（float32 vs 原 float64）===")
for c in ["close", "mcap", "turn20", "turn_score", "ma200", "adx14"]:
    if c not in df.columns:
        continue
    a = df[c].astype("float64").to_numpy()
    b = out[c].astype("float64").to_numpy()
    m = np.isfinite(a) & np.isfinite(b) & (a != 0)
    if not m.any():
        continue
    rel = np.abs(b[m] / a[m] - 1)
    print(f"  {c:11} 相对误差 中位 {np.median(rel):.2e}  p99 {np.percentile(rel, 99):.2e}  最大 {rel.max():.2e}")

print("\n=== 行数/空值一致性 ===")
print(f"  行数 {len(df):,} -> {len(pd.read_parquet(TMP)):,}")
for c in ["mcap", "turn_score", "adx14"]:
    if c in df.columns:
        print(f"  {c:11} 空值 {int(df[c].isna().sum()):,} -> {int(out[c].isna().sum()):,}")

if TMP.stat().st_size < 100e6:
    os.replace(TMP, SRC)
    print(f"\n已覆盖 {SRC}  {SRC.stat().st_size/1e6:.1f} MB")
else:
    print("\n仍超 100MB，保留临时文件待处理")
