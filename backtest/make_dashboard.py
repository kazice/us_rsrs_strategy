"""调仓看板：reports/index.html

明细列表 + 点行展开该期的日线走势，不翻页。

每期图表窗口 = 买入前 3 个月 ~ 卖出后 1 个月，叠加 MA20 / MA60，
买入 ▲、卖出 ▼ 都打在当日收盘价上。图表在展开时才创建，
不依赖"先切页再画"的时机（之前画不出来就是这个原因）。

看板位置：baseline 统一写到 reports/index.html（不放进 baseline 目录，避免重复）；
参数试验随 run 一起写到 logs/<时间戳>_<tag>/index.html。回测跑完会自动调一次。

用法：
    python backtest/make_dashboard.py                      # 用 reports/baseline
    python backtest/make_dashboard.py --run <run 目录>      # 指定某轮产出
    python backtest/make_dashboard.py --out <路径>          # 另存
    python backtest/make_dashboard.py --open               # 生成后自动打开
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RUN_DEFAULT = ROOT / "reports" / "baseline"     # 基线产出目录
OUT_DEFAULT = ROOT / "reports" / "index.html"   # 基线看板（不放在 baseline 里，避免重复）

ETFS = {"SPY", "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI",
        "XLB", "XLU", "XLC", "XLRE"}
PRE_MONTHS = 3      # 买入前画 3 个月
POST_MONTHS = 1     # 卖出后再留 1 个月
MA_WARMUP = 70      # 多带 70 根，MA60 从窗口第一根就有值
REASON_CN = {"rebalance": "调仓卖出", "stop_loss": "止损卖出"}
def read_params(run: Path) -> dict:
    """参数由回测脚本写在 run 目录里，看板只读不反向依赖。"""
    f = run / "params.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _load(run: Path, data_dir: Path):
    d = run
    holds = pd.read_csv(d / "holds.csv", parse_dates=["buy", "sell"])
    nav = pd.read_csv(d / "nav_daily.csv", parse_dates=["date"], index_col="date")
    hold = pd.read_csv(d / "hold_daily.csv", parse_dates=["date"], index_col="date")
    nav = nav.join(hold, how="left")
    spy = pd.read_csv(d / "bench_spy.csv", parse_dates=["date"], index_col="date")
    stats = pd.read_csv(d / "stats.csv", index_col=0)
    bars = pd.read_parquet(data_dir / "bars_daily.parquet")
    etf = pd.read_parquet(data_dir / "etf_daily.parquet")
    return holds, nav, spy, stats, bars, etf


def _window(bars, etf, sym: str, buy, sell):
    """买入前 3 个月 ~ 卖出后 1 个月，多带 MA_WARMUP 根做均线预热。"""
    if sym in ETFS:
        g = etf[etf.etf_symbol == sym][["date", "open", "high", "low", "close"]]
    else:
        g = bars[bars.symbol.astype(str) == sym][["date", "open", "high", "low", "close"]]
    if not len(g):
        return None
    g = g.sort_values("date").reset_index(drop=True)
    dates = g.date.to_numpy()

    start = buy - pd.DateOffset(months=PRE_MONTHS)
    end = sell + pd.DateOffset(months=POST_MONTHS)
    i0 = int(dates.searchsorted(pd.Timestamp(start), side="left"))
    i1 = int(dates.searchsorted(pd.Timestamp(end), side="right") - 1)
    i0 = max(0, min(i0, len(g) - 1))
    i1 = max(i0, min(i1, len(g) - 1))

    lo = max(0, i0 - MA_WARMUP)
    seg = g.iloc[lo:i1 + 1].reset_index(drop=True)
    i_buy = int(dates.searchsorted(pd.Timestamp(buy), side="right") - 1) - lo
    i_sell = int(dates.searchsorted(pd.Timestamp(sell), side="right") - 1) - lo
    rows = [[r.date.strftime("%Y-%m-%d"), round(float(r.open), 4), round(float(r.high), 4),
             round(float(r.low), 4), round(float(r.close), 4)] for r in seg.itertuples()]
    return rows, i0 - lo, i_buy, i_sell


def build(run: Path) -> dict:
    params = read_params(run)
    data_dir = ROOT / str(params.get("data_dir", "latest_data"))
    holds, nav, spy, stats, bars, etf = _load(run, data_dir)

    periods = []
    for i, r in enumerate(holds.itertuples(), 1):
        res = _window(bars, etf, str(r.symbol), r.buy, r.sell)
        ohlc, show_from, i_buy, i_sell = res if res else (None, 0, None, None)
        periods.append({
            "i": i,
            "symbol": str(r.symbol),
            "sector": str(r.sector),
            "kind": "ETF" if str(r.symbol) in ETFS else "个股",
            "buy": r.buy.strftime("%Y-%m-%d"),
            "sell": r.sell.strftime("%Y-%m-%d"),
            "hold_days": int((r.sell - r.buy).days),
            "buy_px": round(float(r.buy_px), 4),
            "sell_px": round(float(r.sell_px), 4),
            "ret": round(float(r.ret), 6),
            "reason": REASON_CN.get(str(r.reason), str(r.reason)),
            "ohlc": ohlc,
            "show_from": show_from,
            "buy_i": i_buy,
            "sell_i": i_sell,
        })

    n = len(holds)
    sv = spy.reindex(nav.index).ffill().bfill()["spy"]
    return {
        "tag": params.get("tag") or run.name,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": [nav.index.min().strftime("%Y-%m-%d"), nav.index.max().strftime("%Y-%m-%d")],
        "data_dir": data_dir.name,
        "cost": params.get("cost"),
        "params": params.get("strategy", {}),
        "stats": {str(k): [round(float(v), 6) for v in stats.loc[k]] for k in stats.index},
        "summary": {
            "trades": n,
            "stops": int((holds.reason == "stop_loss").sum()),
            "win": round(float((holds.ret > 0).mean()), 4) if n else 0.0,
            "avg_ret": round(float(holds.ret.mean()), 6) if n else 0.0,
            "cash_ratio": round(float((nav["hold"] == "cash").mean()), 4),
        },
        "nav": {
            "dates": [d.strftime("%Y-%m-%d") for d in nav.index],
            "nav": [round(float(v), 6) for v in nav["nav"]],
            "spy": [round(float(v), 6) for v in (sv / sv.iloc[0])],
        },
        "periods": periods,
    }


def render(data: dict) -> str:
    return TEMPLATE.replace("__DATA__",
                            json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def dashboard_path(run: Path) -> Path:
    """看板落在哪：baseline 统一放 reports/index.html，其它 run 放自己目录里。"""
    return OUT_DEFAULT if run.resolve() == RUN_DEFAULT.resolve() else run / "index.html"


def write_run(run: Path, out_html: Path | None = None) -> dict:
    """给一个 run 目录出看板。回测脚本直接调这个。"""
    data = build(run)
    target = Path(out_html) if out_html else dashboard_path(run)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(data), encoding="utf-8")
    return data


def _report(data: dict, run: Path, out: Path) -> None:
    p = data["params"]
    print(f"看板: {out}")
    print(f"  目录 {run}")
    print(f"  期数 {len(data['periods'])} | 区间 {data['range'][0]} ~ {data['range'][1]}"
          f" | 数据 {data['data_dir']}")
    if p:
        print(f"  参数 ADX>{p.get('ADX_MIN')} MCAP>{p.get('MCAP_MIN', 0)/1e8:.0f}亿 "
              f"turn<={p.get('TURN_CAP')} 板块{p.get('TOPN_SECTOR')}×个股{p.get('TOPN_STOCK')} "
              f"止损{p.get('STOP_LOSS')} 补选日<{p.get('REFILL_DAY')} 熊市阈值{p.get('BEAR_RATIO')}")
    miss = sum(1 for x in data["periods"] if not x["ohlc"])
    if miss:
        print(f"  警告：{miss} 期没有 K 线数据")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=str(RUN_DEFAULT), help="回测产出目录")
    ap.add_argument("--out", default=None,
                    help="看板输出路径，默认 baseline → reports/index.html，其它 → <run>/index.html")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    a = ap.parse_args()

    run = Path(a.run)
    if not (run / "holds.csv").exists():
        print(f"找不到 {run/'holds.csv'}，先跑 backtest_benchmark.py")
        return 1
    out = Path(a.out) if a.out else dashboard_path(run)
    data = write_run(run, out)
    _report(data, run, out)
    if a.open:
        webbrowser.open(out.as_uri())
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>调仓看板</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --line:#262c36; --fg:#e6edf3; --dim:#8b949e;
    --up:#f85149; --down:#3fb950; --accent:#4493f8; --mark:#f0883e;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);overflow-anchor:none;
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:18px 20px 60px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}
  h1{font-size:17px;margin:0;font-weight:600}
  .meta{color:var(--dim);font-size:12.5px}
  .tools{margin-left:auto;display:flex;gap:8px}
  button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
         border-radius:7px;padding:7px 13px;font-size:13px;cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.on{background:#1f6feb22;border-color:var(--accent);color:var(--accent)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px 18px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(116px,1fr));gap:11px;margin:14px 0}
  .kpi .lab{color:var(--dim);font-size:12px}
  .kpi .val{font-size:19px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
  .up{color:var(--up)} .down{color:var(--down)} .dim{color:var(--dim)}
  table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
  th{color:var(--dim);font-weight:500;text-align:left;padding:9px 10px;
     border-bottom:1px solid var(--line);font-size:12px;position:sticky;top:0;background:var(--panel)}
  td{padding:9px 10px;border-bottom:1px solid #1b2027}
  tr.row{cursor:pointer}
  tr.row:hover{background:#1c2330}
  tr.row.open{background:#1b2432}
  tr.row.open td{border-bottom-color:transparent}
  .num{text-align:right}
  .caret{display:inline-block;width:13px;color:var(--dim);transition:transform .15s}
  tr.row.open .caret{transform:rotate(90deg);color:var(--accent)}
  .exp>td{padding:0;border-bottom:1px solid #1b2027;background:#11161d}
  .expbox{padding:14px 16px 18px}
  .exphdr{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}
  .tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
       border:1px solid var(--line);color:var(--dim)}
  canvas{width:100%;display:block}
  .chartbox{position:relative}
  .tip{position:absolute;pointer-events:none;background:#0d1117f2;border:1px solid var(--line);
       border-radius:7px;padding:7px 10px;font-size:12px;line-height:1.5;display:none;
       white-space:nowrap;z-index:9}
  .legend{display:flex;gap:16px;color:var(--dim);font-size:12px;margin-top:8px;flex-wrap:wrap}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:1px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>调仓看板</h1>
    <span class="meta" id="meta"></span>
    <div class="tools">
      <button id="lock">锁定展开</button>
      <button id="alloff">全部收起</button>
    </div>
  </header>

  <div class="grid" id="kpis"></div>

  <div class="card chartbox" style="margin-bottom:14px">
    <canvas id="navchart" height="280"></canvas>
    <div class="legend">
      <span><i class="dot" style="background:var(--accent)"></i>策略净值（对数轴）</span>
      <span><i class="dot" style="background:var(--dim)"></i>SPY（同轴）</span>
      <span style="margin-left:auto" class="dim">点下面任意一行展开该期日线</span>
    </div>
    <div class="tip" id="navtip"></div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table>
      <thead><tr>
        <th style="width:34px"></th><th>#</th><th>买入</th><th>卖出</th><th>代码</th><th>板块</th>
        <th class="num">买入价</th><th class="num">卖出价</th><th class="num">收益</th>
        <th class="num">持有天</th><th>原因</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<script>
const D = __DATA__;
const P = D.periods;

const el = id => document.getElementById(id);
const pct = v => (v * 100).toFixed(2) + '%';
const cls = v => v >= 0 ? 'up' : 'down';
let locked = false;
const openSet = new Map();      // i -> {tr, box, canvas}

/* ---------------- 通用绘图 ---------------- */
function setup(cv, cssH){
  const dpr = window.devicePixelRatio || 1;
  // 用画布自身的 CSS 宽度，位图与显示 1:1，箭头和鼠标位置才不会偏
  const w = cv.clientWidth;
  if (w <= 0) return null;
  cv.style.height = cssH + 'px';
  cv.width = Math.round(w * dpr); cv.height = Math.round(cssH * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, cssH);
  return {ctx, w, h: cssH};
}

/* 刻度取「整数感」的值：对数轴走 1/2/5×10^n，线性轴走 1/2/2.5/5 步长 */
function niceTicks(lo, hi, log){
  const out = [];
  if (log){
    const e0 = Math.floor(Math.log10(lo)) - 1, e1 = Math.ceil(Math.log10(hi));
    for (let e = e0; e <= e1; e++)
      for (const m of [1, 2, 5]){
        const v = m * Math.pow(10, e);
        if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v);
      }
  } else {
    // 步长要「不大于」目标间隔，否则刻度会稀到只剩两三条
    const raw = (hi - lo) / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const opts = [1, 2, 2.5, 5, 10].map(s => s * mag).filter(s => s <= raw * 1.0001);
    const step = opts.length ? opts[opts.length - 1] : mag;
    const i0 = Math.ceil(lo / step - 1e-9);
    for (let i = i0; i * step <= hi + 1e-9; i++) out.push(i * step);
  }
  return out.length ? out : [lo, hi];
}

function fmtTick(v){
  if (v >= 1000) return v.toFixed(0);
  if (v >= 10) return v.toFixed(v % 1 ? 1 : 0);
  if (v >= 1) return v.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  return v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

function axes(ctx, w, h, lo, hi, dates, padL, padR, padT, padB, log){
  const n = dates.length;
  // 对数轴把值域撑到相邻刻度，避免出现 0.76 / 17.10 这种贴边的不整齐端点
  let d0 = lo, d1 = hi;
  if (log){
    const t = niceTicks(lo, hi, true);
    if (t.length){ d0 = Math.min(lo, t[0]); d1 = Math.max(hi, t[t.length - 1]); }
  }
  const px = i => padL + (w - padL - padR) * (n <= 1 ? .5 : i / (n - 1));
  const py = log
    ? v => padT + (h - padT - padB) * (1 - (Math.log(v) - Math.log(d0)) / (Math.log(d1) - Math.log(d0) || 1))
    : v => padT + (h - padT - padB) * (1 - (v - d0) / (d1 - d0 || 1));

  ctx.font = '11px system-ui'; ctx.textAlign = 'left';
  niceTicks(d0, d1, log).forEach(v => {
    const y = py(v);
    if (y < padT - 1 || y > h - padB + 1) return;
    ctx.strokeStyle = '#262c36'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = '#8b949e';
    ctx.fillText(fmtTick(v), 3, y + 4);
  });

  const step = Math.max(1, Math.round(n / 7));
  ctx.textAlign = 'center';
  for (let i = 0; i < n; i += step) ctx.fillText(dates[i].slice(5), px(i), h - padB + 18);
  ctx.textAlign = 'left';
  return {px, py};
}

/* ---------------- 净值曲线 ---------------- */
function drawNav(){
  const cv = el('navchart');
  const c = setup(cv, 280); if (!c) return;
  const {ctx, w, h} = c;
  const a = D.nav.nav, b = D.nav.spy, ds = D.nav.dates;
  const vals = a.concat(b).filter(v => isFinite(v) && v > 0);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const {px, py} = axes(ctx, w, h, lo, hi, ds, 52, 12, 14, 30, true);
  const line = (arr, color, width) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = width;
    arr.forEach((v, i) => i ? ctx.lineTo(px(i), py(v)) : ctx.moveTo(px(i), py(v)));
    ctx.stroke();
  };
  line(b, '#6e7681', 1.2);
  line(a, '#4493f8', 1.9);
  cv._hit = {px, py, n: a.length};
}

/* ---------------- 单期 K 线 ---------------- */
function ma(rows, m){
  const out = new Array(rows.length).fill(null);
  let s = 0;
  for (let i = 0; i < rows.length; i++){
    s += rows[i][4];
    if (i >= m) s -= rows[i - m][4];
    if (i >= m - 1) out[i] = s / m;
  }
  return out;
}

function drawK(cv, p){
  const c = setup(cv, 330); if (!c) return;
  const {ctx, w, h} = c;
  const all = p.ohlc, from = p.show_from;
  const rows = all.slice(from);
  const n = rows.length;
  const padL = 52, padR = 14, padT = 14, padB = 30;

  const vis = rows;
  const lo = Math.min(...vis.map(r => r[3]));
  const hi = Math.max(...vis.map(r => r[2]));
  const pad = (hi - lo) * 0.09 || 1;   // 留出画箭头的空隙
  const {px, py} = axes(ctx, w, h, lo - pad, hi + pad,
                        rows.map(r => r[0]), padL, padR, padT, padB, false);

  const bw = Math.max(1.2, (w - padL - padR) / n * 0.62);
  const cov = (arr, i) => arr[i] === null || arr[i] === undefined ? null : arr[i];

  // MA20 / MA60：在完整数组上算，再按窗口切片
  const m20 = ma(all, 20), m60 = ma(all, 60);
  const drawMA = (arr, color) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.25;
    let started = false;
    for (let i = 0; i < n; i++){
      const v = cov(arr, from + i);
      if (v === null) continue;
      i && started ? ctx.lineTo(px(i), py(v)) : ctx.moveTo(px(i), py(v));
      started = true;
    }
    ctx.stroke();
  };

  rows.forEach((r, i) => {
    const up = r[4] >= r[1];
    ctx.strokeStyle = ctx.fillStyle = up ? '#f85149' : '#3fb950';
    const x = px(i), y1 = py(Math.max(r[1], r[4])), y2 = py(Math.min(r[1], r[4]));
    ctx.beginPath(); ctx.moveTo(x, py(r[2])); ctx.lineTo(x, py(r[3]));
    ctx.lineWidth = 1; ctx.stroke();
    ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
  });

  drawMA(m20, '#e3b341');
  drawMA(m60, '#a371f7');

  // 箭头画在 K 线外侧：买入在最低价下方尖端朝上，卖出在最高价上方尖端朝下，
  // 这样一眼就能看出它属于哪一根，不会和当天实体糊在一起。
  const mark = (idx, up) => {
    if (idx === null || idx === undefined || idx < from || idx >= all.length) return;
    const bar = all[idx];
    const x = px(idx - from), s = 6;
    ctx.beginPath();
    if (up){
      const y0 = py(bar[3]) + 5;
      ctx.moveTo(x, y0); ctx.lineTo(x - s, y0 + s + 2); ctx.lineTo(x + s, y0 + s + 2);
    } else {
      const y0 = py(bar[2]) - 5;
      ctx.moveTo(x, y0); ctx.lineTo(x - s, y0 - s - 2); ctx.lineTo(x + s, y0 - s - 2);
    }
    ctx.closePath();
    ctx.fillStyle = up ? '#f0883e' : '#4493f8'; ctx.fill();
    ctx.strokeStyle = '#0d1117'; ctx.lineWidth = 1.5; ctx.stroke();
  };
  mark(p.buy_i, true);
  mark(p.sell_i, false);

  cv._hit = {px, py, n, all, from, p};
}

/* ---------------- tooltip ---------------- */
function bindTip(cv, tip, fmt){
  cv.onmousemove = e => {
    const hit = cv._hit; if (!hit) return;
    const rect = cv.getBoundingClientRect(), x = e.clientX - rect.left;
    let best = 0, bd = 1e9;
    for (let i = 0; i < hit.n; i++){ const d = Math.abs(hit.px(i) - x); if (d < bd){ bd = d; best = i; } }
    const s = fmt(best);
    if (!s){ tip.style.display = 'none'; return; }
    tip.innerHTML = s; tip.style.display = 'block';
    tip.style.left = Math.min(rect.width - 160, Math.max(0, hit.px(best) + 14)) + 'px';
    tip.style.top = Math.max(0, e.clientY - rect.top - 12) + 'px';
  };
  cv.onmouseleave = () => tip.style.display = 'none';
}

/* ---------------- 表 + 展开 ---------------- */
function kpis(){
  const s = D.summary, st = D.stats;
  const cells = [
    ['累计收益', pct(st['累计'][0]), cls(st['累计'][0])],
    ['年化', pct(st['年化'][0]), cls(st['年化'][0])],
    ['夏普', st['夏普'][0].toFixed(2), ''],
    ['最大回撤', pct(st['最大回撤'][0]), 'down'],
    ['交易笔数', s.trades, ''],
    ['胜率', pct(s.win), ''],
    ['止损次数', s.stops, ''],
    ['平均单笔', pct(s.avg_ret), cls(s.avg_ret)],
    ['空仓占比', pct(s.cash_ratio), 'dim'],
  ];
  el('kpis').innerHTML = cells.map(([k, v, c]) =>
    `<div class="card kpi"><div class="lab">${k}</div><div class="val ${c}">${v}</div></div>`).join('');
  el('meta').textContent = `${D.range[0]} ~ ${D.range[1]}｜${D.tag}｜${D.data_dir}｜生成于 ${D.generated}`;
}

function expBody(p){
  if (!p.ohlc) return '<div class="dim" style="padding:18px">该期没有日线数据（不在当天成分表里）</div>';
  return `
  <div class="exphdr">
    <b style="font-size:16px">${p.symbol}</b>
    <span class="tag">${p.sector}</span><span class="tag">${p.kind}</span>
    <span class="tag">买入 ${p.buy} @ ${p.buy_px.toFixed(2)}</span>
    <span class="tag">卖出 ${p.sell} @ ${p.sell_px.toFixed(2)}</span>
    <span class="tag">持有 ${p.hold_days} 天</span>
    <span class="tag">${p.reason}</span>
    <span style="margin-left:auto" class="${cls(p.ret)}"><b>${pct(p.ret)}</b></span>
  </div>
  <div class="chartbox"><canvas></canvas><div class="tip"></div></div>
  <div class="legend">
    <span><i class="dot" style="background:var(--up)"></i>涨</span>
    <span><i class="dot" style="background:var(--down)"></i>跌</span>
    <span><i class="dot" style="background:#e3b341"></i>MA20</span>
    <span><i class="dot" style="background:#a371f7"></i>MA60</span>
    <span style="margin-left:auto" class="dim">▲ 买入　▼ 卖出　窗口：买入前 3 个月 ~ 卖出后 1 个月</span>
  </div>`;
}

function collapse(i){
  const o = openSet.get(i); if (!o) return;
  o.tr.remove(); openSet.delete(i);
}

function expand(i){
  if (openSet.has(i)) return;
  const p = P[i - 1];
  const tr = document.createElement('tr');
  tr.className = 'exp';
  const td = document.createElement('td');
  td.colSpan = 11;
  const box = document.createElement('div');
  box.className = 'expbox';
  box.innerHTML = expBody(p);
  td.appendChild(box); tr.appendChild(td);
  const row = el('tbody').querySelector(`tr[data-i="${i}"]`);
  row.after(tr);
  row.classList.add('open');
  openSet.set(i, {tr, row, box});

  if (!p.ohlc) return;
  const cv = box.querySelector('canvas'), tip = box.querySelector('.tip');
  // 展开后再画：此时布局已完成，clientWidth 才有值
  requestAnimationFrame(() => {
    drawK(cv, p);
    bindTip(cv, tip, k => {
      const r = cv._hit.all[cv._hit.from + k];
      const gi = cv._hit.from + k;
      const tag = gi === p.buy_i ? '　<b style="color:#f0883e">买入</b>'
                : gi === p.sell_i ? '　<b style="color:#4493f8">卖出</b>' : '';
      return `${r[0]}${tag}<br>开 ${r[1].toFixed(2)}　高 ${r[2].toFixed(2)}`
           + `<br>低 ${r[3].toFixed(2)}　收 ${r[4].toFixed(2)}`;
    });
  });
}

function toggle(i){
  // 锁定视角：展开/收起会让上方内容增减，浏览器锚定会把视口带跑。
  // 这里先记住被点那一行在屏幕上的位置，插完再滚回去，行看着不动。
  const row = el('tbody').querySelector(`tr[data-i="${i}"]`);
  const before = row ? row.getBoundingClientRect().top : 0;
  if (openSet.has(i)) collapse(i);
  else {
    if (!locked) [...openSet.keys()].forEach(collapse);
    expand(i);
  }
  requestAnimationFrame(() => {
    if (!row) return;
    const dy = row.getBoundingClientRect().top - before;
    if (Math.abs(dy) > 0.5) window.scrollBy(0, dy);
  });
}

function table(){
  el('tbody').innerHTML = P.map(p => `<tr class="row" data-i="${p.i}">
    <td><span class="caret">▸</span></td>
    <td class="dim">${p.i}</td><td>${p.buy}</td><td>${p.sell}</td>
    <td><b>${p.symbol}</b></td><td class="dim">${p.sector}</td>
    <td class="num">${p.buy_px.toFixed(2)}</td><td class="num">${p.sell_px.toFixed(2)}</td>
    <td class="num ${cls(p.ret)}">${pct(p.ret)}</td>
    <td class="num dim">${p.hold_days}</td><td class="dim">${p.reason}</td></tr>`).join('');
  el('tbody').querySelectorAll('tr.row').forEach(tr => {
    tr.onclick = e => { e.stopPropagation(); toggle(+tr.dataset.i); };
  });
}

el('lock').onclick = () => {
  locked = !locked;
  el('lock').classList.toggle('on', locked);
  el('lock').textContent = locked ? '已锁定（可同时展开多期）' : '锁定展开';
};
el('alloff').onclick = () => { [...openSet.keys()].forEach(collapse); };

kpis(); table();
requestAnimationFrame(() => {
  drawNav();
  bindTip(el('navchart'), el('navtip'), i =>
    `${D.nav.dates[i]}<br>策略 ${D.nav.nav[i].toFixed(3)}<br>SPY ${D.nav.spy[i].toFixed(3)}`);
});

let rt; window.addEventListener('resize', () => {
  clearTimeout(rt);
  rt = setTimeout(() => {
    drawNav();
    openSet.forEach((o, i) => { const cv = o.box.querySelector('canvas'); if (cv) drawK(cv, P[i - 1]); });
  }, 150);
});

// index.html#12 直接展开第 12 期
(function(){
  const h = parseInt((location.hash || '').slice(1), 10);
  if (!isFinite(h) || h < 1 || h > P.length) return;
  expand(h);
  requestAnimationFrame(() => {
    const r = el('tbody').querySelector(`tr[data-i="${h}"]`);
    if (r) r.scrollIntoView({block: 'center'});
  });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
