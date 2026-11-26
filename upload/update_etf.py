"""增量更新 upload/etf_daily.parquet（SPY + 11 个板块 ETF）。

口径
----
- **价格**：新浪拆股调整价 × k（k = latest_data 的 etf close / 新浪 close，逐票算）
- **成交额**：新浪 `a` 字段
- **rsrs20_40_raw/z**：与个股同算法（N=20 回归 β×R²，M=40 标准化，不含当日）
- **ma200**：简单均线
- **bear_ratio21**：过去 21 日中 close < ma200 的占比
- **is_bull**：bear_ratio21 <= 0.5
- **slope120_weighted**：原始算法未能反推（最近的近似仍差 1.7%）。
  重叠区沿用 latest_data 原值；新增行用「120 日对数回归斜率」近似，
  因该字段只用于熊市 ETF 的相对排序，绝对值偏差不影响名次。

只输出 date >= 2025-12-31 的行。
"""
from __future__ import annotations

import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "latest_data"
OUT = ROOT / "upload"

OUTPUT_START = "2025-12-31"
WARMUP_START = "2024-06-01"
ETFS = ["SPY", "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLC", "XLRE"]
RSRS_N, RSRS_M = 20, 40
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_daily(symbol: str, retries: int = 3):
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_x=/"
           f"US_MinKService.getDailyK?symbol={symbol}&___qn=3")
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="ignore")
            m = re.search(r"\((\[.*\])\)", raw, re.S)
            if not m:
                return None
            df = pd.DataFrame(json_load(m.group(1)))
            if not len(df):
                return None
            df["date"] = pd.to_datetime(df["d"])
            for c in ["o", "h", "l", "c", "v", "a"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.rename(columns={"o": "open", "h": "high", "l": "low",
                                      "c": "close", "v": "volume", "a": "amount"})[
                ["date", "open", "high", "low", "close", "volume", "amount"]]
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(1.0 * (i + 1))
    return None


def json_load(s: str):
    import json
    return json.loads(s)


def rsrs_z(h: np.ndarray, l: np.ndarray):
    n = len(h)
    raw = np.full(n, np.nan)
    for i in range(RSRS_N - 1, n):
        y, x = h[i - RSRS_N + 1: i + 1], l[i - RSRS_N + 1: i + 1]
        if np.isnan(y).any() or np.isnan(x).any():
            continue
        vx = np.var(x)
        if vx <= 0:
            continue
        b = np.cov(x, y, bias=True)[0, 1] / vx
        r = np.corrcoef(x, y)[0, 1]
        if np.isnan(b) or np.isnan(r) or abs(b) > 10 or r ** 2 < 0.5:
            continue
        raw[i] = b * r ** 2
    s = pd.Series(raw)
    z = np.full(n, np.nan)
    for i in range(RSRS_M, n):
        hist = s.iloc[i - RSRS_M: i].dropna()
        if len(hist) >= RSRS_M * 0.5 and hist.std() > 0:
            z[i] = (s.iloc[i] - hist.mean()) / hist.std()
    return raw, z


def log_slope120(c: pd.Series) -> pd.Series:
    """120 日对数价格线性回归斜率（期末对齐），仅用于新增段近似。"""
    lc = np.log(c.astype(float))
    w = 120
    x = np.arange(w) - (w - 1) / 2
    sxx = (x ** 2).sum()
    out = np.full(len(lc), np.nan)
    v = lc.to_numpy()
    for i in range(w - 1, len(v)):
        seg = v[i - w + 1: i + 1]
        if np.isnan(seg).any():
            continue
        out[i] = (x * (seg - seg.mean())).sum() / sxx
    return pd.Series(out, index=c.index)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    etf0 = pd.read_parquet(SRC / "etf_daily.parquet")
    last = etf0.date.max()
    print(f"基线: etf_daily 至 {last.date()}")

    # 1) 逐票 k
    base_last = etf0[etf0.date == last].set_index("etf_symbol").close
    kmap, sina_last = {}, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_daily, s): s for s in ETFS}
        for f in as_completed(futs):
            s = futs[f]
            try:
                df = f.result()
                if df is None or not len(df):
                    continue
                row = df[df.date == last]
                if len(row) and float(row.close.iloc[0]) > 0:
                    sina_last[s] = float(row.close.iloc[0])
                    if s in base_last.index:
                        kmap[s] = float(base_last.loc[s]) / sina_last[s]
            except Exception:
                pass
    print(f"k 计算: {len(kmap)}/{len(ETFS)} 只")

    # 2) 全段
    frames = []
    for s in ETFS:
        if s not in kmap:
            continue
        df = fetch_daily(s)
        if df is None or not len(df):
            continue
        df = df[df.date >= pd.Timestamp(WARMUP_START)].copy()
        k = kmap[s]
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c] * k
        df["etf_symbol"] = s
        frames.append(df)
        time.sleep(0.15)

    if not frames:
        print("无 ETF 数据")
        return 1
    sina = pd.concat(frames, ignore_index=True)

    # 3) 指标
    out_frames = []
    for s, g in sina.groupby("etf_symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        raw, z = rsrs_z(g.high.to_numpy(float), g.low.to_numpy(float))
        g["rsrs20_40_raw"] = raw
        g["rsrs20_40_z"] = z
        g["ma200"] = g.close.rolling(200, min_periods=200).mean()
        g["slope120_weighted"] = log_slope120(g.close)     # 近似
        below = (g.close < g.ma200).astype(float).where(g.ma200.notna())
        g["bear_ratio21"] = below.rolling(21, min_periods=21).mean()
        g["is_bull"] = (g.bear_ratio21 <= 0.5).astype("boolean")
        out_frames.append(g)
    cand = pd.concat(out_frames, ignore_index=True)

    # 4) 合并：重叠区优先用 latest_data 原值
    cols = ["date", "etf_symbol", "open", "high", "low", "close", "volume",
            "amount", "rsrs20_40_raw", "rsrs20_40_z", "slope120_weighted",
            "ma200", "bear_ratio21", "is_bull"]
    base = etf0[etf0.date >= pd.Timestamp(WARMUP_START)][cols].copy()
    cand = cand[cols]
    merged = pd.concat([base, cand], ignore_index=True)
    merged = merged.drop_duplicates(["etf_symbol", "date"], keep="first")

    # 合并后重算 raw/z。keep="first" 会让重叠区的 raw 来自 base、z 来自 cand，
    # 两列不是同一条序列（z 复现不出、实盘板块名次对不上回测）。
    # 这里用合并后的 high/low 统一重算一次，必须在裁剪到 OUTPUT_START 之前做，
    # 否则前 40 行没有预热窗口。
    fixed = []
    for s, g in merged.groupby("etf_symbol", observed=True):
        g = g.sort_values("date").reset_index(drop=True)
        raw, z = rsrs_z(g.high.to_numpy(float), g.low.to_numpy(float))
        g["rsrs20_40_raw"] = raw
        g["rsrs20_40_z"] = z
        fixed.append(g)
    merged = pd.concat(fixed, ignore_index=True)

    merged = merged[merged.date >= pd.Timestamp(OUTPUT_START)].sort_values(["etf_symbol", "date"])

    # 状态列
    for col, mp in [("rsrs20_40_raw", RSRS_N), ("rsrs20_40_z", RSRS_M),
                    ("slope120_weighted", 120), ("ma200", 200)]:
        st = np.zeros(len(merged), dtype="int8")
        st[merged[col].isna().to_numpy()] = 2
        merged[f"{col}_state"] = st

    merged = merged.reset_index(drop=True)
    merged.to_parquet(OUT / "etf_daily.parquet", index=False, compression="zstd")
    print(f"输出 {len(merged):,} 行  {merged.date.min().date()} ~ {merged.date.max().date()}")
    print(f"ETF 数 {merged.etf_symbol.nunique()}")
    print(f"已写 {OUT/'etf_daily.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
