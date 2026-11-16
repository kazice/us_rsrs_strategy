"""data 分支的「按年归档」：每年结算一次，把过期的历史切出来单独放。

data 分支的维护规则（约定）
--------------------------
- 日常：``data/curated/bars_daily`` 只保留**近两年**，每天增量覆盖，体积稳定在 20 MB 量级；
- 归档：每年年初跑一次本脚本，把已滚出窗口的年份整年导出成
  ``data/archive/bars_YYYY.parquet``（一个年度一个文件，只增不改）；
- 回测要更长的历史时，把 ``data/archive/`` 一并挂上即可，不用回头重抓。

为什么不用 duckdb 直接 PARTITION_BY 重写：它输出的文件名是 ``data_0.parquet``，
而 ``quant.data.store.bars_file`` 固定写 ``part.parquet``，两种文件名共存会让
``read_parquet('symbol=*/*.parquet')`` 读到重复行。所以裁剪逐票用 pandas 原地替换。
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from quant.data import store

log = logging.getLogger(__name__)


def archive_year(year: int) -> int:
    """把该年的全部分区导出成一个年度文件。已存在则跳过（只增不改）。"""
    src = store.bars_dir()
    if not src.exists():
        raise SystemExit(f"行情目录不存在：{src}")
    out = store.data_root() / "archive" / f"bars_{year}.parquet"
    if out.exists():
        print(f"{out} 已存在，跳过（归档只增不改，要重做请先删掉它）")
        return 0

    frames = []
    for path in sorted(src.glob("symbol=*/*.parquet")):
        sym = path.parent.name.split("=", 1)[1]
        df = pd.read_parquet(path)
        if df.empty or "date" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        part = df[df["date"].dt.year == year]
        if len(part):
            part = part.assign(symbol=sym)
            frames.append(part)
    if not frames:
        print(f"{year} 年没有数据，不生成归档文件")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    alldf = pd.concat(frames, ignore_index=True)
    cols = ["symbol"] + [c for c in store.BARS_COLUMNS if c in alldf.columns]
    tmp = out.with_suffix(".tmp")
    alldf[cols].to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(out)
    print(f"归档 {year} 年：{len(alldf)} 行、{alldf['symbol'].nunique()} 只 → "
          f"{out}（{out.stat().st_size / 1e6:.1f} MB）")
    return len(alldf)


def prune(keep_years: int = 2) -> int:
    """把主行情目录裁到最近 keep_years 年。返回删除的行数。"""
    src = store.bars_dir()
    latest = None
    files = sorted(src.glob("symbol=*/*.parquet"))
    for path in files:
        df = pd.read_parquet(path, columns=["date"])
        if len(df):
            m = pd.to_datetime(df["date"]).max()
            latest = m if latest is None else max(latest, m)
    if latest is None:
        print("行情目录为空，无需裁剪")
        return 0

    cutoff = pd.Timestamp(year=latest.year - keep_years + 1, month=1, day=1)
    removed = 0
    touched = 0
    for path in files:
        df = pd.read_parquet(path)
        if df.empty:
            continue
        d = pd.to_datetime(df["date"])
        keep = df[d >= cutoff]
        if len(keep) == len(df):
            continue
        removed += len(df) - len(keep)
        touched += 1
        tmp = path.with_suffix(".tmp")
        keep.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path)
    print(f"裁剪到 {cutoff:%Y-%m-%d} 之后：{touched} 只、删除 {removed} 行")
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="行情按年归档 + 裁剪")
    ap.add_argument("--year", type=int, action="append", default=None,
                    help="要归档的年份，可重复；不给则按 --keep-years 自动算")
    ap.add_argument("--keep-years", type=int, default=2, help="主目录保留最近几年")
    ap.add_argument("--no-prune", action="store_true", help="只归档不裁剪")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    years = args.year
    if not years:
        latest = None
        for path in store.bars_dir().glob("symbol=*/*.parquet"):
            df = pd.read_parquet(path, columns=["date"])
            if len(df):
                m = pd.to_datetime(df["date"]).max()
                latest = m if latest is None else max(latest, m)
        if latest is None:
            print("行情目录为空")
            return 0
        first_keep = latest.year - args.keep_years + 1
        years = list(range(2005, first_keep))

    total = 0
    for y in years:
        total += archive_year(y)
    if not args.no_prune:
        prune(args.keep_years)
    print(f"完成：归档 {total} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
