"""临时页面：熊市里 11 个板块 ETF 怎么走 + 双条件每期选了谁。

用法（项目根目录）：
    $env:PYTHONPATH='src'; python -m scripts.bear_etf_trends
产出：reports/web/bear_etfs.html
"""
from __future__ import annotations

import fnmatch
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"]
COLORS = {
    "XLK": "#4f8ef7", "XLF": "#3ec98a", "XLV": "#ef5b6b", "XLY": "#f0a63c",
    "XLP": "#a78bfa", "XLE": "#f472b6", "XLI": "#22d3ee", "XLB": "#a3e635",
    "XLRE": "#fb923c", "XLU": "#38bdf8", "XLC": "#e879f9", "SPY": "#94a3b8",
}
# 与定稿回测同口径：SPY vs MA200、21 日跌破占比 ≥50%、区间截至 CRSP 数据末端
START, END = "2021-11-13", "2025-12-31"
REGIME_MA, REGIME_WIN, BEAR_RATIO = 200, 21, 0.5
RSRS_N, RSRS_M, SLOPE_WIN, INTERSECT = 20, 40, 120, 3


def latest_out() -> Path:
    """优先用 CRSP 定稿产出；没有就退回最新的任何一套。"""
    base = ROOT / "reports" / "etf"
    cands = [p for p in base.iterdir()
             if p.is_dir() and (p / "holds.csv").exists() and fnmatch.fnmatch(p.name, "*crsp*")]
    if not cands:
        cands = [p for p in base.iterdir() if p.is_dir() and (p / "holds.csv").exists()]
    return max(cands, key=lambda p: p.stat().st_mtime)


def svg_lines(sub: pd.DataFrame, w: int, h: int, title: str = "",
              marks: list[tuple[str, str]] | None = None) -> str:
    """归一化到段首 = 100 的多线图（Python 端直接算好坐标）。"""
    pl, pr, pt, pb = 46, 54, 22, 26
    norm = sub / sub.iloc[0] * 100.0
    lo, hi = float(norm.min().min()), float(norm.max().max())
    pad = (hi - lo) * 0.08 or 1.0
    lo -= pad
    hi += pad
    n = len(norm)
    X = lambda i: pl + (i / max(n - 1, 1)) * (w - pl - pr)
    Y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb)

    g = [f'<svg viewBox="0 0 {w} {h}" class="chart">']
    for k in range(5):
        y = pt + k * (h - pt - pb) / 4
        val = hi - k * (hi - lo) / 4
        g.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w-pr}" y2="{y:.1f}" stroke="#233254"/>')
        g.append(f'<text x="{pl-6}" y="{y+4:.1f}" fill="#7b8aa5" font-size="10.5" '
                 f'text-anchor="end">{val:.0f}</text>')
    if marks:
        for d, lab in marks:
            if d in sub.index:
                i = list(sub.index).index(d)
                x = X(i)
                g.append(f'<line x1="{x:.1f}" y1="{pt}" x2="{x:.1f}" y2="{h-pb}" '
                         f'stroke="#f0a63c" stroke-dasharray="4 3" opacity="0.75"/>')
                g.append(f'<text x="{x+3:.1f}" y="{pt+11}" fill="#f0a63c" font-size="10">{lab}</text>')
    for c in norm.columns:
        ys = [Y(v) for v in norm[c].values]
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f} {y:.1f}" for i, y in enumerate(ys))
        dash = ' stroke-dasharray="5 4"' if c == "SPY" else ""
        wide = 2.2 if c == "SPY" else 1.6
        g.append(f'<g class="line" data-c="{c}"><path d="{d}" fill="none" '
                 f'stroke="{COLORS[c]}" stroke-width="{wide}"{dash} stroke-linejoin="round"/>')
        g.append(f'<text x="{X(n-1)+4:.1f}" y="{ys[-1]+3.5:.1f}" fill="{COLORS[c]}" '
                 f'font-size="10.5">{c} {norm[c].iloc[-1]:.0f}</text></g>')
    # x 轴日期
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        i = int(round(frac * (n - 1)))
        g.append(f'<text x="{X(i):.1f}" y="{h-8}" fill="#7b8aa5" font-size="10" '
                 f'text-anchor="middle">{sub.index[i].strftime("%y-%m-%d")}</text>')
    if title:
        g.append(f'<text x="{pl}" y="{pt-8}" fill="#e6edf7" font-size="12">{title}</text>')
    g.append("</svg>")
    return "".join(g)


def main() -> int:
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from quant.data import store
    from scripts.sector_stock_rsrs import rsrs_panel_fast, weighted_slope_panel

    out = latest_out()
    holds = pd.read_csv(out / "holds.csv")

    end, start = pd.Timestamp(END), pd.Timestamp(START)
    load_start = (start - pd.DateOffset(days=380)).strftime("%Y-%m-%d")
    px = store.load_panel(field="adj_close", symbols=ETFS + ["SPY"], start=load_start,
                          end=END).sort_index().ffill()
    hi = store.load_panel(field="high", symbols=ETFS, start=load_start,
                          end=END).sort_index().ffill()
    lo = store.load_panel(field="low", symbols=ETFS, start=load_start,
                          end=END).sort_index().ffill()

    cal = px.index
    spy = px["SPY"]
    ma = spy.rolling(REGIME_MA, min_periods=max(20, REGIME_MA // 2)).mean()
    below = (spy < ma).astype(float).where(ma.notna())
    ratio = below.rolling(REGIME_WIN, min_periods=REGIME_WIN).mean()
    is_bear = (ratio >= BEAR_RATIO) & ratio.notna()
    is_bear = is_bear[is_bear.index >= start]

    # 连续熊市段
    segs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cur_s = None
    prev = None
    for d, v in is_bear.items():
        if v and cur_s is None:
            cur_s = d
        elif not v and cur_s is not None:
            segs.append((cur_s, prev))
            cur_s = None
        prev = d
    if cur_s is not None:
        segs.append((cur_s, prev))
    segs = [(s, e) for s, e in segs if (e - s).days >= 10]

    etf_rsrs = rsrs_panel_fast(hi[ETFS], lo[ETFS], RSRS_N, RSRS_M, "strict")
    etf_slope = weighted_slope_panel(px[ETFS], SLOPE_WIN)

    bear_rows = holds[holds["牛市"] == 0].copy()
    periods = []
    for _, r in bear_rows.iterrows():
        sig = pd.Timestamp(str(r["信号日"]))
        zs = etf_rsrs.loc[sig].dropna().sort_values(ascending=False) if sig in etf_rsrs.index else pd.Series(dtype=float)
        sl = etf_slope.loc[sig].reindex(zs.index).dropna().sort_values(ascending=False) if sig in etf_slope.index else pd.Series(dtype=float)
        rt, st = list(zs.index[:INTERSECT]), list(sl.index[:INTERSECT])
        periods.append({
            "date": str(r["信号日"])[:10],
            "from": str(r["持有期起"])[:10], "to": str(r["持有期止"])[:10],
            "pick": "" if pd.isna(r["个股"]) else str(r["个股"]),
            "sector": str(r["板块"]),
            "ret": float(r["ETF收益"]),
            "rsrs_top": ", ".join(f"{k}({zs[k]:+.2f})" for k in rt),
            "slope_top": ", ".join(f"{k}({sl[k]*100:+.3f}%)" for k in st),
            "inter": ", ".join([k for k in zs.index if k in rt and k in st]) or "∅",
        })

    # 总览：全区间归一化 + 熊市阴影
    W, H = 1000, 380
    pl, pr, pt, pb = 46, 54, 22, 26
    sub_all = px[ETFS + ["SPY"]]
    norm = sub_all / sub_all.iloc[0] * 100
    lo_v, hi_v = float(norm.min().min()), float(norm.max().max())
    pad = (hi_v - lo_v) * 0.06
    lo_v, hi_v = lo_v - pad, hi_v + pad
    n = len(norm)
    X = lambda i: pl + (i / max(n - 1, 1)) * (W - pl - pr)
    Y = lambda v: pt + (1 - (v - lo_v) / (hi_v - lo_v)) * (H - pt - pb)
    ov = [f'<svg viewBox="0 0 {W} {H}" class="chart">']
    for s, e in segs:
        i0, i1 = norm.index.get_indexer([s])[0], norm.index.get_indexer([e])[0]
        ov.append(f'<rect x="{X(i0):.1f}" y="{pt}" width="{max(X(i1)-X(i0),1):.1f}" '
                  f'height="{H-pt-pb}" fill="#ef5b6b" opacity="0.13"/>')
    for k in range(5):
        y = pt + k * (H - pt - pb) / 4
        val = hi_v - k * (hi_v - lo_v) / 4
        ov.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{W-pr}" y2="{y:.1f}" stroke="#233254"/>')
        ov.append(f'<text x="{pl-6}" y="{y+4:.1f}" fill="#7b8aa5" font-size="10.5" '
                  f'text-anchor="end">{val:.0f}</text>')
    for c in norm.columns:
        ys = [Y(v) for v in norm[c].values]
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f} {y:.1f}" for i, y in enumerate(ys))
        dash = ' stroke-dasharray="5 4"' if c == "SPY" else ""
        ov.append(f'<g class="line" data-c="{c}"><path d="{d}" fill="none" stroke="{COLORS[c]}" '
                  f'stroke-width="{2.4 if c == "SPY" else 1.5}"{dash}/></g>')
    for frac in np.linspace(0, 1, 6):
        i = int(round(frac * (n - 1)))
        ov.append(f'<text x="{X(i):.1f}" y="{H-8}" fill="#7b8aa5" font-size="10" '
                  f'text-anchor="middle">{norm.index[i].strftime("%Y-%m")}</text>')
    ov.append("</svg>")

    # 每段小图
    blocks = []
    for s, e in segs:
        win = px.loc[(px.index >= s - pd.Timedelta(days=5)) & (px.index <= e + pd.Timedelta(days=5)),
                     ETFS + ["SPY"]].dropna(how="all")
        if len(win) < 5:
            continue
        marks = [(pd.Timestamp(p["date"]), p["pick"] or "空仓")
                 for p in periods if s <= pd.Timestamp(p["date"]) <= e]
        rets = (win[ETFS].iloc[-1] / win[ETFS].iloc[0] - 1).sort_values(ascending=False)
        rank = "".join(
            f'<span class="rk"><i style="background:{COLORS[c]}"></i>{c} '
            f'<b class="{cls(v)}">{v*100:+.1f}%</b></span>' for c, v in rets.items())
        blocks.append(f"""
        <h3>{s.date()} ~ {e.date()}（{(e-s).days} 天，{len(win)} 个交易日）</h3>
        <div class="card">{svg_lines(win, 980, 320, "段内归一化（起点 = 100），灰虚线 = SPY", marks)}</div>
        <div class="rank">{rank}</div>""")

    legend = "".join(f'<span data-c="{c}" class="on"><i style="background:{COLORS[c]}"></i>{c}</span>'
                     for c in ETFS + ["SPY"])

    rows_html = "".join(f"""<tr><td class="l">{p['date']}</td>
      <td class="l dim">{p['from']} ~ {p['to']}</td>
      <td class="l">{p['rsrs_top']}</td><td class="l">{p['slope_top']}</td>
      <td class="l">{p['inter']}</td>
      <td class="l"><b>{p['pick'] or '空仓'}</b></td>
      <td class="{'pos' if p['ret']>=0 else 'neg'}">{p['ret']*100:+.2f}%</td></tr>"""
      for p in periods)

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>熊市里的板块 ETF 走势</title>
<style>
 :root{{--bg:#0f172a;--panel:#16213b;--line:#2a3a5e;--txt:#e6edf7;--dim:#93a4c0;
        --green:#3ec98a;--red:#ef5b6b}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--txt);
      font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}}
 .wrap{{max-width:1080px;margin:0 auto;padding:26px 18px 60px}}
 h1{{font-size:21px;margin:0 0 6px}} h2{{font-size:16px;margin:32px 0 10px;
      padding-left:10px;border-left:3px solid #4f8ef7}}
 h3{{font-size:14.5px;margin:26px 0 8px;color:#cfe0f7;font-weight:600}}
 .sub{{color:var(--dim);font-size:12.5px}}
 .card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px}}
 svg.chart{{display:block;width:100%;height:auto}}
 .legend{{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0 4px;font-size:12.5px}}
 .legend span{{cursor:pointer;color:var(--dim);user-select:none}}
 .legend span.on{{color:var(--txt)}}
 .legend i,.rk i{{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;
      vertical-align:middle}}
 .rank{{display:flex;flex-wrap:wrap;gap:10px 16px;margin:8px 0 0;font-size:12.5px;color:var(--dim)}}
 .rk{{white-space:nowrap}}
 table{{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}}
 th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
 th{{color:var(--dim);font-weight:500;font-size:12px;text-align:left}}
 td.l,th.l{{text-align:left}}
 .pos{{color:var(--green)}} .neg{{color:var(--red)}} .dim{{color:var(--dim)}}
 .note{{background:#16213b;border:1px solid var(--line);border-radius:10px;padding:12px 14px;
        font-size:12.8px;color:#c6d4ea}}
 code{{background:#0b1322;border:1px solid var(--line);border-radius:5px;padding:1px 6px}}
</style></head><body><div class="wrap">
<h1>熊市里的板块 ETF 怎么走</h1>
<div class="sub">判定口径：SPY vs MA{REGIME_MA}，过去 {REGIME_WIN} 日跌破占比 ≥ {BEAR_RATIO:.0%} 判熊
 ｜ 数据 {start.date()} ~ {end.date()} ｜ 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}
 ｜ 来源 <code>{out.name[:60]}…</code></div>

<div class="note" style="margin-top:14px">
  灰色虚线是 SPY。红色底纹 = 熊市区间。下方每段图都按「段内第一个交易日 = 100」归一化，
  所以看的是相对强弱，不是绝对点位。橙色竖虚线标的是当期调仓日买入的标的（双条件命中后买的是板块 ETF，
  交集为空时按 <code>--bear-spy</code> 买 SPY）。
</div>

<h2>全区间总览</h2>
<div class="card">{''.join(ov)}</div>
<div class="legend">{legend}</div>

<h2>逐段放大</h2>
{''.join(blocks) if blocks else '<div class="sub">没有识别到熊市段</div>'}

<h2>熊市每期双条件明细</h2>
<div class="card"><table>
<thead><tr><th class="l">信号日</th><th class="l">持有区间</th><th class="l">RSRS 前 {INTERSECT}</th>
<th class="l">斜率前 {INTERSECT}（%/日）</th><th class="l">交集</th><th class="l">实际买入</th>
<th>该期收益</th></tr></thead><tbody>{rows_html}</tbody></table></div>
<div class="sub" style="margin-top:8px">
  「该期收益」= 该期持有标的的区间收益（买 ETF/SPY 时与 ETF 对照同源）。
</div>
</div>
<script>
document.querySelectorAll('.legend span').forEach(el=>{{
  el.onclick=()=>{{
    const c=el.dataset.c;
    el.classList.toggle('on');
    document.querySelectorAll(`.line[data-c="${{c}}"]`).forEach(g=>{{
      g.style.display = el.classList.contains('on') ? '' : 'none';
    }});
  }};
}});
</script>
</body></html>"""

    path = ROOT / "reports" / "web" / "bear_etfs.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"熊市段 {len(segs)} 个：{[f'{s.date()}~{e.date()}' for s, e in segs]}")
    print(f"产出：{path}（{path.stat().st_size/1024:.0f} KB）")
    return 0


def cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


if __name__ == "__main__":
    raise SystemExit(main())
