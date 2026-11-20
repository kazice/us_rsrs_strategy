"""板块层 RSRS 动量因子检验：排序之后的 20 个交易日收益到底有没有用。

只研究**板块层**（11 个 SPDR ETF），不碰个股、不跑回测。回答三个问题：

    ① 按 RSRS 横截面排序后，第 1 名与第 11 名的未来 20 日收益差多少？单调吗？
    ② 这个排序的截面 IC（Spearman）是多少、稳不稳（ICIR / t 值 / 每年表现）？
    ③ 把标准化窗口 M 从 40 改到 200（或 600），因子是变好还是变钝？

口径与回测一致（`scripts.sector_stock_rsrs.rsrs_panel_fast`）：
    strict 过滤（R² ≥ 0.5、|β| ≤ 10），z 用**不含当日**的过去 M 天，有效点 <5 记 0。

用法：
    $env:PYTHONPATH='src'; python -m scripts.research_sector_momentum --n 20 --m 200 --horizon 20
    python -m scripts.research_sector_momentum --compare 40,120,200,600   # 一次比多个 M
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import store
from scripts.sector_rsrs import SECTORS, anchors
from scripts.sector_stock_rsrs import rsrs_panel_fast

ROOT = Path(__file__).resolve().parents[1]
BENCH = "SPY"


def load_panels(syms: list[str], load_start: str, end: str):
    hi = store.load_panel("high", syms, load_start, end).sort_index().ffill()
    lo = store.load_panel("low", syms, load_start, end).sort_index().ffill()
    px = store.load_panel("adj_close", syms, load_start, end).sort_index().ffill()
    return hi, lo, px


def ic_stats(series: pd.Series, label: str) -> dict:
    s = series.dropna()
    n = len(s)
    if n < 3:
        return {"因子": label, "期数": n}
    mean, sd = float(s.mean()), float(s.std(ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else np.nan
    return {"因子": label, "期数": n, "均值": mean, "标准差": sd,
            "ICIR": mean / sd if sd > 0 else np.nan, "t值": t,
            "IC>0 占比": float((s > 0).mean())}


def analyse(px: pd.DataFrame, rsrs: pd.DataFrame, sig_dates: pd.DatetimeIndex,
            horizon: int, label: str, bench_fwd: pd.Series | None = None) -> dict:
    """返回分档收益表、IC 序列、多空序列。"""
    cal = px.index
    pos = {d: i for i, d in enumerate(cal)}
    fwd = (px.shift(-horizon) / px - 1.0)

    rows, ics, ls1, ls3 = [], {}, {}, {}
    xs_ex, spy_ex = {}, {}
    for d in sig_dates:
        i = pos.get(d)
        if i is None or i + horizon >= len(cal):
            continue
        score = rsrs.loc[d].dropna()
        f = fwd.loc[d].reindex(score.index)
        ok = f.notna()
        score, f = score[ok], f[ok]
        if len(score) < 8:
            continue
        rank = score.rank(ascending=False)
        for sym in score.index:
            rows.append({"date": d, "symbol": sym, "score": float(score[sym]),
                         "rank": int(rank[sym]), "fwd": float(f[sym])})
        ics[d] = score.corr(f, method="spearman")
        top1 = score.idxmax()
        bot1 = score.idxmin()
        top3 = rank[rank <= 3].index
        bot3 = rank[rank > len(rank) - 3].index
        ls1[d] = float(f[top1] - f[bot1])
        ls3[d] = float(f[top3].mean() - f[bot3].mean())
        xs_ex[d] = float(f[top1] - f.mean())          # 第 1 名相对 11 板块等权
        if bench_fwd is not None and d in bench_fwd.index and pd.notna(bench_fwd[d]):
            spy_ex[d] = float(f[top1] - bench_fwd[d])

    obs = pd.DataFrame(rows)
    if obs.empty:
        return {"label": label, "obs": obs}
    bucket = (obs.groupby("rank")["fwd"]
              .agg(均值="mean", 中位="median", 期数="size",
                   胜率=lambda s: float((s > 0).mean())))
    bucket["均值%"] = (bucket["均值"] * 100).round(2)
    bucket["中位%"] = (bucket["中位"] * 100).round(2)
    bucket["胜率"] = (bucket["胜率"] * 100).round(1)

    mono = bucket.reset_index().pipe(
        lambda d: float(d["rank"].corr(d["均值"], method="spearman")))
    def _ser(d: dict) -> pd.Series:
        return pd.Series(d, dtype=float).sort_index()

    return {"label": label, "obs": obs, "bucket": bucket, "mono": mono,
            "ic": _ser(ics), "ls1": _ser(ls1), "ls3": _ser(ls3),
            "xs_ex": _ser(xs_ex), "spy_ex": _ser(spy_ex)}


def fmt_spread(s: pd.Series) -> str:
    s = s.dropna()
    if len(s) < 3:
        return "-"
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))
    return f"{s.mean() * 100:+.2f}% (t={t:+.2f}, 胜率 {(s > 0).mean():.0%})"


def main() -> int:
    ap = argparse.ArgumentParser(description="板块 RSRS 动量的 20 日前瞻收益检验")
    ap.add_argument("--n", type=int, default=20, help="回归窗口")
    ap.add_argument("--m", type=int, default=200, help="标准化窗口")
    ap.add_argument("--horizon", type=int, default=20, help="前瞻收益的交易日数")
    ap.add_argument("--freq", default="M", help="信号日频率：M 月末 / W 周末 / H 半月")
    ap.add_argument("--start", default="2021-11-13")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--compare", default="", help="逗号分隔的 M 列表，做参数对比，如 40,200,600")
    ap.add_argument("--out", default="reports/research/sector_momentum")
    args = ap.parse_args()

    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    ms = [int(x) for x in args.compare.split(",") if x.strip()] if args.compare else [args.m]
    syms = list(SECTORS) + [BENCH]

    warm = (args.n + max(ms)) * 2 + 30
    load_start = (start - pd.DateOffset(days=warm)).strftime("%Y-%m-%d")
    hi, lo, px = load_panels(syms, load_start, end.strftime("%Y-%m-%d"))
    cal = px.index
    sig_all = anchors(cal, args.freq)
    sig = sig_all[(sig_all >= start) & (sig_all <= end)]
    bench_fwd = (px[BENCH].shift(-args.horizon) / px[BENCH] - 1).reindex(sig)

    res = {}
    for m in ms:
        z = rsrs_panel_fast(hi[list(SECTORS)], lo[list(SECTORS)], args.n, m, "strict")
        res[m] = analyse(px[list(SECTORS)], z, sig, args.horizon, f"N{args.n}_M{m}",
                         bench_fwd=bench_fwd)

    lines = ["# 板块 RSRS 动量：排序后的 20 交易日收益检验", ""]
    add = lines.append
    add(f"- 标的：11 个 SPDR 板块 ETF（等权口径，不含个股层）")
    add(f"- 信号日：{args.freq} 频（{len(sig)} 个），区间 {start.date()} ~ {end.date()}")
    add(f"- 前瞻收益：信号日收盘起 {args.horizon} 个交易日")
    add(f"- 同期 SPY 的前瞻收益均值：**{bench_fwd.mean() * 100:+.2f}%**"
        f"（胜率 {(bench_fwd > 0).mean():.0%}）")
    add("")

    for m in ms:
        r = res[m]
        if r.get("obs") is None or r["obs"].empty:
            add(f"## N={args.n} / M={m}\n\n样本不足（预热不够或区间太短）。\n")
            continue
        add(f"## N={args.n} / M={m}（{len(r['obs']['date'].unique())} 期 × "
            f"{r['obs']['symbol'].nunique()} 板块 = {len(r['obs'])} 个观测）\n")
        add("| RSRS 排名 | 均值 | 中位 | 胜率 | 样本 |")
        add("|---|---|---|---|---|")
        for rank, row in r["bucket"].iterrows():
            add(f"| {rank} | {row['均值%']:+.2f}% | {row['中位%']:+.2f}% | "
                f"{row['胜率']:.0f}% | {int(row['期数'])} |")
        add("")
        st = ic_stats(r["ic"], f"N{args.n}_M{m}")
        add(f"- 截面 IC：均值 **{st['均值']:+.3f}**，ICIR {st['ICIR']:+.2f}，"
            f"t={st['t值']:+.2f}，IC>0 占 {st['IC>0 占比']:.0%}")
        add(f"- 单调性（排名 vs 收益的 Spearman，4 位）：{r['mono']:+.4f}")
        add(f"- 多空（第 1 − 第 11）：{fmt_spread(r['ls1'])}")
        add(f"- 多空（前 3 − 后 3）：{fmt_spread(r['ls3'])}")
        add(f"- 第 1 名 − 11 板块等权：{fmt_spread(r['xs_ex'])}")
        add(f"- 第 1 名 − SPY：{fmt_spread(r['spy_ex'])}（策略真正需要的超额）")

        by_year = r["ic"].groupby(r["ic"].index.year).mean()
        add(f"- 分年 IC：{'  '.join(f'{y}: {v:+.2f}' for y, v in by_year.items())}")
        add("")

    if len(ms) > 1:
        add("## M 参数对比\n")
        add("| M | IC 均值 | ICIR | t | IC>0 | 前3−后3 | 第1−第11 | 单调性 |")
        add("|---|---|---|---|---|---|---|---|")
        for m in ms:
            r = res[m]
            if r.get("obs") is None or r["obs"].empty:
                add(f"| {m} | - | - | - | - | - | - | - |")
                continue
            st = ic_stats(r["ic"], str(m))
            add(f"| {m} | {st['均值']:+.3f} | {st['ICIR']:+.2f} | {st['t值']:+.2f} | "
                f"{st['IC>0 占比']:.0%} | {r['ls3'].mean() * 100:+.2f}% | "
                f"{r['ls1'].mean() * 100:+.2f}% | {r['mono']:+.3f} |")
        add("")
        add("> IC = 每个信号日「RSRS 分数」与「未来 20 日收益」的截面 Spearman 相关。"
            "样本是重叠的前瞻窗口，t 值偏乐观，看方向与稳定性即可，不要当显著性检验。")

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    (out / "README.md").write_text(text, encoding="utf-8")
    for m in ms:
        r = res[m]
        if r.get("obs") is None or r["obs"].empty:
            continue
        r["obs"].to_csv(out / f"obs_m{m}.csv", index=False, encoding="utf-8-sig")
        r["bucket"].to_csv(out / f"bucket_m{m}.csv", encoding="utf-8-sig")
        r["ic"].rename("ic").to_csv(out / f"ic_m{m}.csv", encoding="utf-8-sig")
    print(text)
    print(f"\n产出 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
