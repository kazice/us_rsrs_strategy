"""RSRS（阻力支撑相对强度）板块动量：用 high/low 回归斜率做横截面排序。

口径（按用户指定）
------------------
1. 对最近 **N** 天的 high 与 low 做 OLS 回归：high = α + β·low
2. 取 **β × R²** 作为原始强度（R² 用于惩罚拟合差的斜率）
3. 对 β×R² 序列做 **M** 天 z-score 标准化（M 默认 2N，即"需要 3×N 天数据"）
4. 月末取各板块最新 RSRS，横截面降序取前 N 只，等权，次日起持有到下一月末

与研报版的差别：研报是「先 z(β) 再乘 R²×β」，本脚本是「先乘 R²×β 再 z」。
后者的结果尺度恒定（均值 0、标准差 1），0.7/−0.7 这类阈值才有可比性。

注意
----
- high/low 是未复权价，ETF 无拆股，影响可忽略。
- M 越小 z-score 越噪；研报用 M=600（约 34N），这里默认 2N 是数据不足时的低配。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import store

ROOT = Path(__file__).resolve().parents[1]

SECTORS = {
    "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费", "XLP": "必需消费",
    "XLE": "能源", "XLI": "工业", "XLB": "材料", "XLRE": "房地产", "XLU": "公用事业",
    "XLC": "通信服务",
}
BENCH = "SPY"


def anchors(cal: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    """取每个调仓周期的最后一个交易日作为信号日。

    freq: M = 月末；W = 周末；H = 半月（每月 1-15 日、16-月末各一次）；
          M2 = 月中（每月第 2 个 ISO 交易周的最后一个交易日）
    """
    idx = cal.to_series()
    f = freq.upper()
    if f == "W":
        key = idx.dt.to_period("W")
    elif f == "H":
        # 上半月 / 下半月分别取最后一天
        key = idx.dt.strftime("%Y-%m-") + np.where(idx.dt.day <= 15, "1", "2")
    elif f == "M2":
        # 按 (ISO 年, ISO 周) 识别周，在该月内出现的第 2 个周里取最后一个交易日
        iso = cal.isocalendar()
        week = iso["year"].astype(str) + "-" + iso["week"].astype(str)
        tmp = pd.DataFrame({"d": cal, "ym": cal.to_period("M"), "w": week.values})
        rank = tmp.groupby("ym")["w"].transform(lambda s: s.rank(method="dense"))
        sel = tmp.loc[rank == 2]
        return pd.DatetimeIndex(sel.groupby("ym")["d"].max().values)
    else:
        key = idx.dt.to_period("M")
    return pd.DatetimeIndex(idx.groupby(key).max().values)


def rsrs_panel(high: pd.DataFrame, low: pd.DataFrame, n: int, m: int,
               spec: str = "strict", r2_th: float = 0.5,
               slope_lim: float = 10.0) -> pd.DataFrame:
    """逐板块滚动回归，返回标准化后的 RSRS。

    spec="strict"（对齐研报/常见实现）：
        ① 质量过滤：|β| ≤ slope_lim 且 R² ≥ r2_th，不通过则该日原始值记 NaN
        ② z-score 用**过去 m 个、不含当前**的原始值
        ③ 当前位为 NaN 或历史有效值不足时，z 记 0（中性），仍参与横截面排序
    spec="simple"：仅跳过 var=0，z 用 rolling(m) 含当前值，NaN 直接丢弃。
    """
    out = {}
    for col in high.columns:
        h = high[col].astype(float).values
        l = low[col].astype(float).values
        raw = np.full(len(h), np.nan)
        for i in range(n - 1, len(h)):
            yw = h[i - n + 1:i + 1]
            xw = l[i - n + 1:i + 1]
            if not np.isfinite(yw).all() or not np.isfinite(xw).all():
                continue
            vx = xw.var()
            if not np.isfinite(vx) or vx <= 0:
                continue
            b = np.cov(xw, yw, bias=True)[0, 1] / vx
            a = yw.mean() - b * xw.mean()
            res = yw - (a + b * xw)
            ss_tot = ((yw - yw.mean()) ** 2).sum()
            r2 = 1.0 - (res ** 2).sum() / ss_tot if ss_tot > 0 else np.nan
            if spec == "strict":
                if not (np.isfinite(r2) and r2 >= r2_th) or abs(b) > slope_lim:
                    continue
            raw[i] = b * r2

        if spec == "strict":
            z = np.full(len(raw), np.nan)
            for i in range(max(n - 1 + m, m), len(raw)):
                hist = raw[i - m:i]                       # 不含当前
                valid = hist[np.isfinite(hist)]
                cur = raw[i]
                if not np.isfinite(cur) or len(valid) < 5:
                    z[i] = 0.0
                    continue
                sd = valid.std()
                z[i] = 0.0 if sd == 0 else (cur - valid.mean()) / sd
        else:
            s = pd.Series(raw, index=high.index)
            z = ((s - s.rolling(m).mean()) / s.rolling(m).std(ddof=0)).values
        out[col] = z
    return pd.DataFrame(out, index=high.index)


def perf(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    n = max(len(r), 1)
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    ann = (1 + total) ** (252 / n) - 1
    vol = float(r.std() * np.sqrt(252))
    dd = float((nav / nav.cummax() - 1).min())
    return {"累计": total, "年化": ann, "波动": vol,
            "夏普": ann / vol if vol > 0 else np.nan, "最大回撤": dd}


def main() -> int:
    ap = argparse.ArgumentParser(description="RSRS 板块动量")
    ap.add_argument("--n", type=int, default=20, help="回归窗口")
    ap.add_argument("--m", type=int, default=0, help="标准化窗口，0 = 2N")
    ap.add_argument("--topn", type=int, default=3)
    ap.add_argument("--cost", type=float, default=0.0005, help="单边成本")
    ap.add_argument("--spec", default="strict", choices=["strict", "simple"],
                    help="strict = 质量过滤 + z 不含当前 + NaN 补 0；simple = 简化版")
    ap.add_argument("--r2-th", type=float, default=0.5, help="strict 下的 R² 下限")
    ap.add_argument("--slope-lim", type=float, default=10.0, help="strict 下的 |β| 上限")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--tag", default="", help="产物目录后缀，避免不同区间互相覆盖")
    ap.add_argument("--freq", default="M", choices=["M", "W", "H", "M2"],
                    help="调仓频率：M 月末 / W 周末 / H 半月 / M2 月中（每月第 2 周最后一个交易日）")
    args = ap.parse_args()

    m = args.m or 2 * args.n
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.today().normalize()
    start = pd.Timestamp(args.start) if args.start else end - pd.DateOffset(years=1)
    syms = list(SECTORS)

    # 预热：需要 N 天回归 + M 天标准化
    load_start = (start - pd.DateOffset(days=(args.n + m) * 2 + 30)).strftime("%Y-%m-%d")
    hi = store.load_panel(field="high", symbols=syms + [BENCH],
                          start=load_start, end=end.strftime("%Y-%m-%d")).sort_index().ffill()
    lo = store.load_panel(field="low", symbols=syms + [BENCH],
                          start=load_start, end=end.strftime("%Y-%m-%d")).sort_index().ffill()
    px = store.load_panel(field="adj_close", symbols=syms + [BENCH],
                          start=load_start, end=end.strftime("%Y-%m-%d")).sort_index().ffill()
    if hi.empty or lo.empty:
        raise SystemExit("high/low 为空")

    bench = px[BENCH]
    rsrs = rsrs_panel(hi[syms], lo[syms], args.n, m, spec=args.spec,
                      r2_th=args.r2_th, slope_lim=args.slope_lim)
    cal = px.index
    ret = px[syms].pct_change()

    me = anchors(cal, args.freq)
    scored = rsrs.dropna(how="any").index
    cand = [i for i, d in enumerate(me) if d <= start and d in scored]
    if not cand:
        raise SystemExit("预热不足，请把 --start 往后挪或减小 --n/--m")
    k0 = cand[-1]

    nav = pd.Series(np.nan, index=cal[cal >= start], dtype=float)
    prev, holds, cur = [], [], 1.0
    for k in range(k0, len(me) - 1):
        sig = me[k]
        if sig not in scored:
            continue
        score = rsrs.loc[sig].dropna().sort_values(ascending=False)
        hold = list(score.index[:args.topn])

        w_new = {s: 1.0 / len(hold) for s in hold} if hold else {}
        w_old = {s: 1.0 / len(prev) for s in prev} if prev else {}
        turnover = sum(abs(w_new.get(s, 0) - w_old.get(s, 0)) for s in set(w_new) | set(w_old))

        nxt = me[k + 1]
        seg = cal[(cal > sig) & (cal <= nxt) & (cal >= start)]
        if len(seg) == 0:
            prev = hold
            continue
        for j, dt in enumerate(seg):
            if j == 0:
                cur *= (1.0 - args.cost * turnover)
            if hold:
                r = ret.loc[dt, hold].mean()
                cur *= (1.0 + (0.0 if pd.isna(r) else r))
            nav.loc[dt] = cur

        seg_ret = nav.loc[seg].iloc[-1] / nav.loc[seg].iloc[0] - 1
        sp = bench.reindex(seg).ffill()
        seg_spy = float(sp.iloc[-1] / sp.iloc[0] - 1) if len(sp) > 1 else np.nan
        holds.append({
            "信号日": sig.date(), "持有期起": seg[0].date(), "持有期止": seg[-1].date(),
            "持仓": " ".join(hold),
            "RSRS": "  ".join(f"{s} {score[s]:+.2f}" for s in hold),
            "换手": round(turnover, 3), "区间收益": round(float(seg_ret), 4),
            "SPY": round(seg_spy, 4), "超额": round(float(seg_ret) - seg_spy, 4),
        })
        prev = hold

    nav = nav.dropna()
    st = perf(nav)
    spy_nav = bench.reindex(nav.index).ffill()
    st_spy = perf(spy_nav / spy_nav.iloc[0] * nav.iloc[0])
    eq = px[syms].reindex(nav.index).ffill()
    st_eq = perf((eq.pct_change().mean(axis=1).fillna(0) + 1).cumprod() * nav.iloc[0])

    hdf = pd.DataFrame(holds)
    spec_txt = (f"strict（|β|≤{args.slope_lim:g}, R²≥{args.r2_th:g}, z 不含当前, NaN→0）"
                if args.spec == "strict" else "simple（无过滤, z 含当前）")
    print(f"RSRS  N={args.n}  M={m}（{'2N' if not args.m else '手动'}）  top{args.topn}  "
          f"单边 {args.cost * 1e4:.1f}bp  {spec_txt}")
    print(f"区间 {nav.index[0].date()} ~ {nav.index[-1].date()}  "
          f"{len(nav)} 交易日  {len(hdf)} 次调仓\n")
    print(hdf.to_string(index=False))

    cmp = pd.DataFrame({"策略": st, "SPY": st_spy, "11板块等权": st_eq}).T
    print("\n" + cmp.round(4).to_string())
    print(f"\n跑赢 SPY {int((hdf['超额'] > 0).sum())}/{len(hdf)} 期，"
          f"换手合计 {hdf['换手'].sum():.2f}")

    suffix = "" if args.spec == "simple" else f"_{args.spec}"
    tag = f"_{args.tag}" if args.tag else ""
    ftag = "" if args.freq == "M" else f"_{args.freq}"
    out = ROOT / "reports" / "etf" / f"rsrs_n{args.n}_m{m}_top{args.topn}{suffix}{ftag}{tag}"
    out.mkdir(parents=True, exist_ok=True)
    nav.rename("nav").to_csv(out / "nav_daily.csv", encoding="utf-8-sig")
    hdf.to_csv(out / "monthly_holds.csv", index=False, encoding="utf-8-sig")
    rsrs.to_csv(out / "rsrs_scores.csv", encoding="utf-8-sig")
    cmp.round(6).to_csv(out / "stats.csv", encoding="utf-8-sig")
    print(f"\n产出 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
