"""校验外部行情与 CRSP 的复权口径是否一致（只看重叠区间，不合并数据）。

判定方式：
    在 CRSP 有行情的那些交易日上，算 r = CRSP.adj_close ÷ 外部价，两种口径各算一次：
        adj_total_return  外部自带 Adj Close（含分红再投）—— 差值是累计分红因子，属正常
        adj_split_only    外部 close（Yahoo 已做拆股调整、未含分红）—— 差值是数据问题
    只用「两边都在」的重叠区间，避开外部源自己前复权基准变化带来的干扰。
        ratio_median  整体比例尺（1.0000 = 完全同一基准）
        ratio_cv      r 的相对离散度；<0.5% 认为口径一致，可以直接乘常数；
                      更大说明分红/拆股处理方式不同，必须逐日对齐或弃用该源。
        max_dev       最大单日偏离，用来抓「某一天突然跳 50%」的拆股漏处理。
    源若只提供总收益复权价（腾讯 qfq / alpaca 的 close 就是复权价），
    basis=adj_split_only 一行记 n/a_degenerate，不应据此判数据有问题。

用法：
    python -m scripts.align_check --symbols MPWR,ENPH
    python -m scripts.align_check --symbols MPWR --sources tencent,yahoo
    python -m scripts.align_check --symbols AAPL,NVDA --sources yahoo

池层是唯一的数据源（`data/curated/sp500_pool`），这里只校验「外部行情补进来的那一段」
和池层的复权口径是否一致，不产生第二份数据。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant.data.sources.registry import build_chain  # noqa: E402


def check(sym: str, src, start: str, end: str, c: pd.DataFrame) -> list[dict]:
    """同一份外部数据按两种复权口径各比一次，好看出差的是数据还是口径。

    A（adj_total_return）：外部自带的 Adj Close，总收益口径（含分红再投）。
    B（adj_split_only） ：外部 close 自身，已含拆股调整、不含分红，与 CRSP 同口径。
    源若只给总收益价（腾讯/alpaca 的 close 本身就是复权价），B 与 A 等价，记 n/a。
    """
    got = src.fetch([sym], start, end)
    df = got.get(sym)
    if df is None or df.empty:
        return [{"ticker": sym, "source": src.name, "basis": "-", "rows": 0,
                 "status": "no_data"}]
    ext = df.copy()
    ext["date"] = pd.to_datetime(ext["date"])
    ext["close"] = pd.to_numeric(ext["close"], errors="coerce")
    ext = ext.dropna(subset=["close"]).sort_values("date")
    if "adj_close" not in ext.columns:
        ext["adj_close"] = np.nan
    ext["adj_close"] = pd.to_numeric(ext["adj_close"], errors="coerce")

    # 口径 A：外部自带的 Adj Close（总收益，含分红）
    ext["adj_total_return"] = ext["adj_close"]
    # 口径 B：外部 close 直接对 CRSP.adj_close。
    # 实测 Yahoo 的 OHLC 本来就做过拆股调整（AAPL 2020-01-02 close=75.09=300.35/4、
    # NVDA 2020-01-02 close=5.998，均与 CRSP adj_close 逐日相等），所以 close 自身就是
    # 「仅拆分」口径，不能再乘拆股因子；腾讯 qfq 的 close 是总收益口径，close≡adj_close，
    # B 与 A 等价、无信息量，记 n/a 免重复判定。
    has_split_free_close = bool((ext["close"] != ext["adj_close"]).any())
    bases = ["adj_total_return"]
    if has_split_free_close:
        ext["adj_split_only"] = ext["close"]
        bases.append("adj_split_only")
    ext = ext.set_index("date").sort_index()

    out = []
    for basis in bases:
        v = ext[basis].dropna()
        common = v.index.intersection(c.index)
        if len(common) < 20:
            out.append({"ticker": sym, "source": src.name, "basis": basis,
                        "rows": int(v.notna().sum()), "overlap": len(common),
                        "status": "insufficient_overlap"})
            continue
        r = (c.loc[common, "adj_close"] / v.loc[common]).replace(
            [np.inf, -np.inf], np.nan).dropna()
        med = float(r.median()) if len(r) else np.nan
        if not np.isfinite(med) or med == 0:
            out.append({"ticker": sym, "source": src.name, "basis": basis,
                        "rows": int(v.notna().sum()),
                        "first": v.index.min().date(), "last": v.index.max().date(),
                        "overlap": int(len(common)), "status": "ratio_empty"})
            continue
        cv = float(r.std() / med)
        out.append({
            "ticker": sym, "source": src.name, "basis": basis,
            "rows": int(v.notna().sum()),
            "first": v.index.min().date(), "last": v.index.max().date(),
            "overlap": int(len(common)),
            "ratio_median": round(med, 6),
            "ratio_cv": round(cv, 6) if np.isfinite(cv) else np.nan,
            "max_dev": round(float((r / med - 1).abs().max()), 6),
            "status": "ok" if cv <= 0.005 else "review",
        })
    if "adj_split_only" not in bases:
        out.append({"ticker": sym, "source": src.name, "basis": "adj_split_only",
                    "rows": int(len(ext)), "status": "n/a_degenerate"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="外部行情 vs 池层复权口径校验")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--sources", default="tencent,yahoo")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2025-12-31")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    chain = build_chain([s.strip() for s in args.sources.split(",")])
    from quant.data import pool as pool_mod

    bar_pool = pool_mod.bars()
    th = pool_mod.tickers()
    th["ticker"] = th["ticker"].astype(str).str.upper()

    def crsp_side(sid: str) -> pd.DataFrame:
        return (bar_pool[bar_pool["security_id"] == sid][["date", "adj_close"]]
                .set_index("date").sort_index())

    rows = []
    for sym in syms:
        hit = th[th["ticker"] == sym]
        if hit.empty:
            print(f"{sym}: 主数据里没有这个 ticker")
            continue
        sid = str(hit.sort_values("end").iloc[-1]["security_id"])
        c = crsp_side(sid)
        for src in chain:
            for r in check(sym, src, args.start, args.end, c):
                r["security_id"] = sid
                rows.append(r)

    rep = pd.DataFrame(rows)
    cols = ["ticker", "security_id", "source", "basis", "rows", "first", "last",
            "overlap", "ratio_median", "ratio_cv", "max_dev", "status"]
    print(rep[[c for c in cols if c in rep.columns]].to_string(index=False))
    out = Path("data/curated/securities/align_check.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
