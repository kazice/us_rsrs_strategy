"""把 CRSP 标准数据与本地行情/换手率/市值逐项对账，输出 markdown 报告。

对比四件事（都是「策略收益虚高」的嫌疑来源）：
    ① 成分股：本地用今天 503 只快照回测过去 → 有多少在历史上并不在标普 500 里（幸存者偏差）
    ② 换手率：本地 westock/SEC 反推 vs CRSP 的 成交量 ÷ 总股本
    ③ 市值：本地 (成交量 ÷ 换手率) × 价格 反推股本 vs CRSP 的 DlyCap
    ④ 价格：本地 yfinance adj_close vs CRSP 的 收盘 ÷ 累计复权因子

用法：
    $env:PYTHONPATH='src'; python -m scripts.compare_crsp_data
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CRSP = ROOT / "data" / "curated" / "crsp_sp500"
MIN_MCAP = 500e8


def norm(s: pd.Series) -> pd.Series:
    """CRSP 的 BRK.B / BF.B 对齐到 yfinance 的 BRK-B / BF-B。"""
    return s.astype(str).str.upper().str.replace(".", "-", regex=False).str.strip()


def load_crsp() -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.read_parquet(CRSP / "daily.parquet")
    d["date"] = pd.to_datetime(d["DlyCalDt"])
    d["symbol"] = norm(d["Ticker"])
    for c in ["DlyClose", "DlyOpen", "DlyHigh", "DlyLow", "DlyVol", "DlyCap", "ShrOut",
              "DlyRet", "DlyCumFacPr", "DlyCumFacShr"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d["symbol"] != "NAN") & d["date"].notna()]
    d["shares"] = d["ShrOut"] * 1e3          # 千股 → 股
    d["mcap"] = d["DlyCap"] * 1e3            # 千美元 → 美元
    # 换手率：同一口径的原始量相除，两边的复权因子会约掉
    d["turnover"] = d["DlyVol"] / d["shares"].where(d["shares"] > 0)
    # CRSP 的复权是「除以」累计价格因子：ORLY 15:1 拆股当天 facpr 由 15 掉到 1，
    # 收盘价同时由 1348 掉到 91.7。乘上去会错 200 倍，必须除。
    d["adj_close_crsp"] = d["DlyClose"] / d["DlyCumFacPr"].where(d["DlyCumFacPr"] > 0)

    sym = pd.read_parquet(CRSP / "symbols.parquet")
    sym["symbol"] = norm(sym["ticker"])
    return d, sym


def load_local(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """本地 bars（close/adj_close/volume）+ 换手率面板，长表。"""
    from quant.data import store

    frames = []
    for f in ("close", "adj_close", "volume"):
        p = store.load_panel(field=f, symbols=symbols, start=str(start.date()), end=str(end.date()))
        if not p.empty:
            frames.append(p.stack().rename(f))
    loc = pd.concat(frames, axis=1).reset_index()
    loc.columns = ["date", "symbol", "close", "adj_close", "volume"]
    tv = pd.read_parquet(ROOT / "data" / "curated" / "fundamentals" / "turnover_daily.parquet")
    tv = tv[["symbol", "date", "turnover"]].rename(columns={"turnover": "turnover_local"})
    tv["date"] = pd.to_datetime(tv["date"])
    loc["date"] = pd.to_datetime(loc["date"])
    return loc.merge(tv, on=["symbol", "date"], how="left")


def main() -> int:
    ap = argparse.ArgumentParser(description="CRSP 与本地数据对账")
    ap.add_argument("--start", default="2021-11-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--out", default="reports/data_audit/crsp_vs_local.md")
    args = ap.parse_args()

    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    crsp, sym = load_crsp()
    mem = pd.read_parquet(CRSP / "membership.parquet")
    mem["start"] = pd.to_datetime(mem["MbrStartDt"])
    mem["end"] = pd.to_datetime(mem["MbrEndDt"])
    # 区间上的 Ticker 是「当时」的代码，映射到最新代码才能和本地符号对齐
    latest = sym.set_index("PERMNO")["symbol"]
    mem["symbol"] = mem["PERMNO"].map(latest)
    sec = pd.read_csv(ROOT / "config" / "ref" / "us_sector_holdings.csv")
    sec["symbol"] = norm(sec["symbol"])
    snap = set(sec["symbol"])

    lines: list[str] = []
    add = lines.append
    add("# CRSP 标普 500 标准数据 vs 本地数据源 对账报告\n")
    add(f"- 对账区间：{start.date()} ~ {end.date()}")
    add(f"- CRSP 逐日行情：{len(crsp):,} 行 / {crsp['PERMNO'].nunique()} 只 PERMNO / "
        f"{crsp['date'].min().date()} ~ {crsp['date'].max().date()}")
    add(f"- 本地快照成分股：{len(snap)} 只\n")

    # ① 成分股 ---------------------------------------------------------------
    anchors = pd.date_range(start, end, freq="ME")
    rows, absent_ever = [], set()
    for a in anchors:
        m = mem[(mem["start"] <= a) & ((mem["end"].isna()) | (mem["end"] >= a))]
        cur = set(m["symbol"].dropna())
        absent_ever |= cur
        rows.append({"date": a, "members": len(cur),
                     "snap_not_member": len(snap - cur),
                     "member_not_snap": len(cur - snap)})
    cdf = pd.DataFrame(rows)
    add("## ① 成分股：用今天的 503 只快照回测过去，偏差有多大\n")
    add("| 月末 | CRSP 当期成分 | 快照里有·当时不是成分 | 当时是成分·快照里没有 |")
    add("|---|---|---|---|")
    for _, r in cdf.iloc[::8].iterrows():
        add(f"| {r['date'].date()} | {r['members']} | {r['snap_not_member']} | {r['member_not_snap']} |")
    add("")
    add(f"**均值**：当期成分 {cdf['members'].mean():.0f} 只；快照里混入非成分 "
        f"**{cdf['snap_not_member'].mean():.0f} 只/期**；当期成分被漏掉 "
        f"**{cdf['member_not_snap'].mean():.0f} 只/期**。\n")
    add(f"- 回测期内出现过、但当前快照里没有的成分股：**{len(absent_ever - snap)} 只**"
        f"（本地永远买不到，全部是幸存者偏差的收益来源）")
    miss_local = sorted(absent_ever - snap)[:60]
    add(f"- 例：{' '.join(miss_local)} …\n")

    # ②③ 换手率 / 市值 -------------------------------------------------------
    local = load_local(sorted(sym["symbol"].dropna().unique()), start, end)
    j = local.merge(
        crsp[["PERMNO", "symbol", "date", "turnover", "mcap", "shares", "adj_close_crsp", "DlyClose"]],
        on=["symbol", "date"], how="inner")
    j["year"] = j["date"].dt.year
    j["t_ratio"] = j["turnover_local"] / j["turnover"]
    j["shares_implied"] = j["volume"] / j["turnover_local"].where(j["turnover_local"] > 0)
    j["mcap_implied"] = j["shares_implied"] * j["close"]
    j["m_ratio"] = j["mcap_implied"] / j["mcap"]

    v = j.dropna(subset=["t_ratio"])
    v = v[(v["turnover"] > 0) & (v["turnover_local"] > 0)]
    add("## ② 换手率：本地 vs CRSP（成交量 ÷ 总股本）\n")
    add(f"可比样本 {len(v):,} 个（symbol×日），比值 = 本地 / CRSP。\n")
    add("| 年份 | 样本 | 中位比值 | ±10% 占比 | ±50% 占比 |")
    add("|---|---|---|---|---|")
    for y, g in v.groupby("year"):
        r = g["t_ratio"]
        add(f"| {y} | {len(g):,} | {r.median():.3f} | {r.between(0.9, 1.1).mean():.1%} | "
            f"{r.between(0.5, 1.5).mean():.1%} |")
    add("")
    per = v.groupby("symbol")["t_ratio"].median().sort_values()
    add("系统性偏低的票（中位比值；<0.75 说明本地把换手率算小了）：")
    add("```")
    add(per.head(15).round(3).to_string())
    add("```\n")

    m = j.dropna(subset=["m_ratio"])
    m = m[(m["m_ratio"] > 0) & (m["m_ratio"] < 100)]
    add("## ③ 市值：本地反推 (成交量 ÷ 换手率 × 价格) vs CRSP DlyCap\n")
    add(f"可比样本 {len(m):,} 个：中位比值 {m['m_ratio'].median():.3f}，"
        f"±10% 占比 {m['m_ratio'].between(0.9, 1.1).mean():.1%}，"
        f"±50% 占比 {m['m_ratio'].between(0.5, 1.5).mean():.1%}\n")
    thr = m[(m["mcap"].between(0.5 * MIN_MCAP, 2 * MIN_MCAP))]
    flip = thr[(thr["mcap"] > MIN_MCAP) != (thr["mcap_implied"] > MIN_MCAP)]
    add(f"500 亿美元门槛附近（0.5~2 倍）样本 {len(thr):,} 个，其中**判定翻转 {len(flip):,} 个"
        f"（{len(flip) / max(len(thr), 1):.1%}）**\n")
    if len(flip):
        top = flip.groupby("symbol").size().sort_values(ascending=False).head(12)
        add("翻转次数最多的票：```\n" + top.to_string() + "\n```\n")

    # ④ 价格 ---------------------------------------------------------------
    add("## ④ 价格：本地 adj_close vs CRSP 收盘 ÷ 累计复权因子\n")
    px = j.dropna(subset=["adj_close", "adj_close_crsp"]).sort_values(["PERMNO", "date"])
    px["r_loc"] = px.groupby("PERMNO")["adj_close"].pct_change()
    px["r_crsp"] = px.groupby("PERMNO")["adj_close_crsp"].pct_change()
    d = px.dropna(subset=["r_loc", "r_crsp"])
    d["r_diff"] = (d["r_loc"] - d["r_crsp"]).abs()
    add(f"可比日收益 {len(d):,} 个：中位绝对差 {d['r_diff'].median():.5f}，"
        f"P99 {d['r_diff'].quantile(0.99):.4f}，最大 {d['r_diff'].max():.3f}")
    per_sym = d.groupby("symbol").apply(lambda g: g["r_loc"].corr(g["r_crsp"]), include_groups=False)
    add(f"相关系数中位 {per_sym.median():.3f}；<0.95 的 {int((per_sym < 0.95).sum())} 只\n")
    add("相关性最差 10 只：```\n" + per_sym.sort_values().head(10).round(3).to_string() + "\n```\n")

    text = "\n".join(lines)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
