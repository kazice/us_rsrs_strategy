"""把一次回测的产出打包成单文件 HTML 报告（策略参数 + 收益曲线 + 年度/逐期调仓）。

用法（在项目根目录）：
    $env:PYTHONPATH='src'; python -m scripts.build_web_report                     # 自动取最新一批产出
    $env:PYTHONPATH='src'; python -m scripts.build_web_report --pattern "*fullc50"
    $env:PYTHONPATH='src'; python -m scripts.build_web_report --dir reports/etf/xxx

产出：reports/web/index.html（数据全部内嵌，双击即可打开，无需联网）。
刷新流程：重跑回测 → 再执行本脚本 → 刷新浏览器即可。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0f172a; --panel:#16213b; --panel2:#1c2a48; --line:#2a3a5e;
    --txt:#e6edf7; --dim:#93a4c0; --blue:#4f8ef7; --amber:#f0a63c; --grey:#7b8aa5;
    --green:#3ec98a; --red:#ef5b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:22px;margin:0 0 6px}
  h2{font-size:16px;margin:34px 0 12px;padding-left:10px;border-left:3px solid var(--blue)}
  .sub{color:var(--dim);font-size:12.5px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
  .grid{display:grid;gap:12px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .kpi .k{color:var(--dim);font-size:12px}
  .kpi .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
  th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--dim);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--panel2)}
  td:first-child,th:first-child{text-align:left}
  td.l,th.l{text-align:left}
  tbody tr:hover{background:rgba(79,142,247,.07)}
  .scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}
  .pos{color:var(--green)} .neg{color:var(--red)} .dim{color:var(--dim)}
  .tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11.5px;border:1px solid}
  .bull{color:#9fd2ff;border-color:#2f5b8f;background:#16283f}
  .bear{color:#ffc9a3;border-color:#8a5230;background:#2b1d14}
  .cash{color:var(--dim)}
  .etf{color:var(--amber)}
  .params{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px 22px}
  .p{display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed var(--line);padding:4px 0}
  .p b{font-weight:500;color:var(--dim)}
  #chart{position:relative}
  svg{display:block;width:100%;height:auto;touch-action:none}
  .legend{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 0;font-size:12.5px}
  .legend span{cursor:pointer;user-select:none;color:var(--dim)}
  .legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;
            vertical-align:middle;background:currentColor}
  .legend .on{color:var(--txt)}
  .tip{position:absolute;pointer-events:none;background:#0b1322;border:1px solid var(--line);
       border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.5;opacity:0;
       transition:opacity .12s;box-shadow:0 6px 18px rgba(0,0,0,.4);z-index:9;white-space:nowrap}
  .tip b{font-variant-numeric:tabular-nums}
  .ctl{display:flex;gap:14px;align-items:center;margin:10px 0 0;font-size:12.5px;color:var(--dim)}
  .ctl label{cursor:pointer;user-select:none}
  select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
         border-radius:6px;padding:3px 8px;font-size:12.5px}
  code{background:#0b1322;border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:12.5px}
  .foot{margin-top:36px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
  .year-head{display:flex;align-items:baseline;gap:10px;margin:22px 0 8px}
  .year-head b{font-size:15px}
  .year-head em{font-style:normal;color:var(--dim);font-size:12.5px}
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub">回测区间 __RANGE__ ｜ 生成于 __GENERATED__ ｜ 数据源 <code>__DIR__</code></div>

  <h2>策略口径</h2>
  <div class="card"><div class="params" id="params"></div>
    <div class="sub" style="margin-top:12px">
      牛市：板块按 RSRS 排序取前 N 个，板块内按换手率降序逐只检查趋势闸，第一只通过即买入；全不通过则当期空仓。
      熊市：板块需同时进入「RSRS 前 N」与「加权斜率前 N」，命中后买该板块 ETF（动量门槛不过则空仓）。
    </div>
  </div>

  <h2>绩效</h2>
  <div class="grid kpis" id="kpis"></div>
  <div class="card" style="margin-top:12px">
    <table id="stats"></table>
  </div>

  <h2>净值曲线</h2>
  <div class="card">
    <div id="chart">
      <svg id="svg" viewBox="0 0 1000 430"></svg>
      <div class="tip" id="tip"></div>
    </div>
    <div class="legend" id="legend"></div>
    <div class="ctl">
      <label><input type="checkbox" id="logchk"> 对数坐标</label>
      <label><input type="checkbox" id="ddchk"> 显示回撤</label>
      <span id="ddinfo"></span>
    </div>
  </div>

  <h2>年度汇总</h2>
  <div class="card"><table id="yearly"></table></div>

  <h2>调仓明细</h2>
  <div class="ctl" style="margin-bottom:10px">
    <span>筛选：</span>
    <select id="freg"><option value="all">全部</option><option value="bull">仅牛市</option>
      <option value="bear">仅熊市</option><option value="cash">仅空仓</option>
      <option value="refill">仅补选</option></select>
    <span id="cnt" class="dim"></span>
  </div>
  <div id="trades"></div>

  <div class="foot">
    刷新方式：重跑回测后执行 <code>python -m scripts.build_web_report</code>（默认取最新一批产出），再刷新本页。<br>
    净值以首个持有期起点为 1.0，未计税费；ETF 对照 = 每期持有同一板块 ETF 的累计净值；SPY 为同期买入持有。
  </div>
</div>
<script>
const DATA = __DATA__;
const S = DATA.series, M = DATA.meta;
const COLORS = {strategy:'#4f8ef7', etf:'#f0a63c', spy:'#7b8aa5'};
const NAMES  = {strategy:'策略', etf:'ETF 对照', spy:'SPY'};
const pct = v => (v>=0?'+':'') + (v*100).toFixed(2) + '%';
const num = v => v.toFixed(2);
const cls = v => v>=0 ? 'pos' : 'neg';

/* ---------- 参数 ---------- */
document.getElementById('params').innerHTML = M.params.map(p =>
  `<div class="p"><b>${p[0]}</b><span>${p[1]}</span></div>`).join('');

/* ---------- KPI + 绩效表 ---------- */
const st = M.stats;
document.getElementById('kpis').innerHTML = [
  ['累计收益', pct(st.strategy.cum), cls(st.strategy.cum)],
  ['年化', pct(st.strategy.cagr), cls(st.strategy.cagr)],
  ['夏普', num(st.strategy.sharpe), ''],
  ['最大回撤', pct(st.strategy.mdd), 'neg'],
  ['年化波动', pct(st.strategy.vol), 'dim'],
  ['ETF 对照累计', pct(st.etf.cum), cls(st.etf.cum)],
  ['SPY 累计', pct(st.spy.cum), cls(st.spy.cum)],
].map(([k,v,c]) => `<div class="kpi"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');

document.getElementById('stats').innerHTML =
  '<thead><tr><th class="l">净值</th><th>累计</th><th>年化</th><th>波动</th><th>夏普</th><th>最大回撤</th></tr></thead><tbody>' +
  [['strategy','策略'],['etf','ETF 对照'],['spy','SPY']].map(([k,n]) => {
    const s = st[k];
    return `<tr><td class="l">${n}</td><td class="${cls(s.cum)}">${pct(s.cum)}</td>
      <td class="${cls(s.cagr)}">${pct(s.cagr)}</td><td class="dim">${pct(s.vol)}</td>
      <td>${num(s.sharpe)}</td><td class="neg">${pct(s.mdd)}</td></tr>`;
  }).join('') + '</tbody>';

/* ---------- 曲线 ---------- */
const W=1000, H=430, PL=58, PR=14, PT=16, PB=30;
const svg = document.getElementById('svg');
const tip = document.getElementById('tip');
const show = {strategy:true, etf:true, spy:true};
let logMode=false, ddMode=false, hoverI=-1;

function draw(){
  const n = S.dates.length;
  const act = Object.keys(show).filter(k=>show[k]);
  let vals = [];
  act.forEach(k => S[k].forEach(v => { if(v!=null&&isFinite(v)) vals.push(v); }));
  if(!vals.length) vals=[0,1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if(logMode){ lo = Math.log10(Math.max(lo, hi/1000)); hi = Math.log10(hi); }
  const pad = (hi-lo)*0.06 || 0.1; lo-=pad; hi+=pad;
  const X = i => PL + (n<=1?0:i/(n-1))*(W-PL-PR);
  const Y = v => {
    const t = logMode ? Math.log10(Math.max(v, Math.pow(10,lo))) : v;
    return PT + (1-(t-lo)/(hi-lo))*(H-PT-PB);
  };
  let g = '';
  // 水平网格 + y 轴刻度
  for(let i=0;i<=4;i++){
    const y = PT + i*(H-PT-PB)/4;
    const raw = hi - i*(hi-lo)/4;
    const lab = logMode ? Math.pow(10,raw).toFixed(2) : raw.toFixed(2);
    g += `<line x1="${PL}" y1="${y}" x2="${W-PR}" y2="${y}" stroke="#233254" stroke-width="1"/>`;
    g += `<text x="${PL-8}" y="${y+4}" fill="#7b8aa5" font-size="11" text-anchor="end">${lab}</text>`;
  }
  // x 轴年份
  let lastY='';
  S.dates.forEach((d,i)=>{
    const y = d.slice(0,4);
    if(y!==lastY && (i===0 || d.slice(5)<'01-08')){
      lastY=y;
      g += `<line x1="${X(i)}" y1="${PT}" x2="${X(i)}" y2="${H-PB}" stroke="#1b2947" stroke-width="1"/>`;
      g += `<text x="${X(i)+3}" y="${H-PB+16}" fill="#7b8aa5" font-size="11">${y}</text>`;
    }
  });
  // 折线
  act.forEach(k=>{
    let d='', open=false;
    S[k].forEach((v,i)=>{
      if(v==null||!isFinite(v)){ open=false; return; }
      d += (open?'L':'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' ';
      open=true;
    });
    g += `<path d="${d}" fill="none" stroke="${COLORS[k]}" stroke-width="1.8" stroke-linejoin="round"/>`;
  });
  // 回撤带（策略）
  if(ddMode){
    let peak=-Infinity, d='', started=false, base=PT;
    S.strategy.forEach((v,i)=>{
      if(v==null||!isFinite(v)) return;
      peak=Math.max(peak,v);
      const dd=(v/peak-1);
      const y = PT + 8 + (-dd)*90;
      d += (started?'L':'M') + X(i).toFixed(1)+' '+y.toFixed(1)+' ';
      started=true;
    });
    if(started){
      g += `<path d="${d}" fill="none" stroke="#ef5b6b" stroke-width="1.1" opacity="0.75"/>`;
      g += `<text x="${PL+4}" y="${PT+22}" fill="#ef5b6b" font-size="11" opacity="0.9">回撤（相对前高）</text>`;
    }
  }
  // hover
  if(hoverI>=0 && hoverI<n){
    g += `<line x1="${X(hoverI)}" y1="${PT}" x2="${X(hoverI)}" y2="${H-PB}" stroke="#5b7099" stroke-dasharray="3 3"/>`;
  }
  g += `<rect x="${PL}" y="${PT}" width="${W-PL-PR}" height="${H-PT-PB}" fill="transparent" id="hit"/>`;
  svg.innerHTML = g;
  svg._X = X; svg._Y = Y; svg._n = n;
  document.getElementById('hit').addEventListener('mousemove', onMove);
  document.getElementById('hit').addEventListener('mouseleave', ()=>{
    hoverI=-1; tip.style.opacity=0; draw();
  });
}

function onMove(e){
  const r = svg.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width * W;
  const n = S.dates.length;
  let i = Math.round((x-PL)/((W-PL-PR)/Math.max(n-1,1)));
  i = Math.max(0, Math.min(n-1, i));
  if(i===hoverI) return;
  hoverI = i;
  draw();
  const rows = ['strategy','etf','spy'].filter(k=>show[k]).map(k=>{
    const v=S[k][i];
    return `<div style="color:${COLORS[k]}">${NAMES[k]} <b>${v==null?'-':v.toFixed(3)}</b></div>`;
  }).join('');
  tip.innerHTML = `<div style="color:#93a4c0;margin-bottom:3px">${S.dates[i]}</div>${rows}`;
  const px = svg._X(i) / W * r.width;
  tip.style.left = Math.min(Math.max(px+14, 0), r.width-150)+'px';
  tip.style.top  = '18px';
  tip.style.opacity = 1;
}

document.getElementById('legend').innerHTML =
  Object.keys(COLORS).map(k=>`<span class="on" data-k="${k}" style="color:${COLORS[k]}">
    <i></i>${NAMES[k]}</span>`).join('');
document.querySelectorAll('.legend span').forEach(el=>{
  el.onclick = ()=>{
    const k = el.dataset.k;
    if(Object.values(show).filter(Boolean).length===1 && show[k]) return;
    show[k] = !show[k];
    el.classList.toggle('on', show[k]);
    draw();
  };
});
document.getElementById('logchk').onchange = e => { logMode = e.target.checked; draw(); };
document.getElementById('ddchk').onchange = e => { ddMode = e.target.checked; draw(); };
draw();

/* ---------- 年度汇总 ---------- */
document.getElementById('yearly').innerHTML =
  '<thead><tr><th class="l">年份</th><th>调仓期</th><th>熊市期</th><th>策略</th><th>ETF 对照</th>'
  + '<th>SPY</th><th>超额 vs SPY</th><th>年内回撤</th><th>期均选股差</th></tr></thead><tbody>' +
  M.yearly.map(y=>`<tr><td class="l">${y.year}${y.partial?' *':''}</td><td>${y.trades}</td>
    <td>${y.bear}</td><td class="${cls(y.ret)}">${pct(y.ret)}</td>
    <td class="${cls(y.etf)}">${pct(y.etf)}</td><td class="${cls(y.spy)}">${pct(y.spy)}</td>
    <td class="${cls(y.excess)}">${pct(y.excess)}</td><td class="neg">${pct(y.mdd)}</td>
    <td class="${cls(y.diff)}">${pct(y.diff)}</td></tr>`).join('') +
  '</tbody><tfoot><tr><td class="l dim" colspan="9" style="text-align:left;font-size:12px">' +
  '* 首末年非完整年度（起止于回测区间两端）　｜　调仓期按「持有期起始日」归属年份，' +
  '所以每年 1 月的持仓（上年末信号）算在本年，年初不会缺一个月<br>' +
  '策略/ETF/SPY 的年度收益统一取净值曲线「上年末收盘 → 本年末收盘」的区间收益，' +
  '与下方分年明细的「年度收益」是同一个数（同源，逐项对齐）。</td></tr></tfoot>';

/* ---------- 调仓明细 ---------- */
const YEARLY = {};
M.yearly.forEach(v => { YEARLY[v.year] = v; });
const HAS_REFILL = M.trades.some(t => t.refill);
const HEAD = `<tr><th class="l">信号日</th><th class="l">持有区间</th><th>市况</th>
  <th class="l">板块</th><th class="l">持仓</th><th>板块RSRS</th><th>斜率%/日</th><th>斜率序</th>`
  + (HAS_REFILL?'<th class="l">补选日</th>':'')
  + '<th>策略</th><th>ETF对照</th><th>选股差</th><th>SPY</th></tr>';

function rowHtml(t){
  const reg = t.bull ? '<span class="tag bull">牛</span>' : '<span class="tag bear">熊</span>';
  const hold = t.pick ? (t.pick.startsWith('ETF:')
      ? `<span class="etf">${t.pick}</span>` : t.pick)
    : '<span class="cash">空仓</span>';
  return `<tr><td class="l">${t.date}</td><td class="l dim">${t.from} ~ ${t.to}</td>
    <td>${reg}</td><td class="l">${t.sector}</td><td class="l">${hold}</td>
    <td>${t.rsrs==null?'-':t.rsrs.toFixed(2)}</td>
    <td>${t.slope==null?'-':t.slope.toFixed(3)}</td>
    <td>${t.slope_rank==null?'-':t.slope_rank}</td>`
    + (HAS_REFILL?`<td class="l dim">${t.refill||''}</td>`:'')
    + `<td class="${cls(t.ret)}">${pct(t.ret)}</td>
    <td class="${cls(t.etf_ret)}">${pct(t.etf_ret)}</td>
    <td class="${cls(t.diff)}">${pct(t.diff)}</td>
    <td class="${cls(t.spy)}">${pct(t.spy)}</td></tr>`;
}

function renderTrades(rows){
  const g = {};
  rows.forEach(t => { (g[t.year] = g[t.year] || []).push(t); });
  return Object.keys(g).map(y => {
    const rs = g[y];
    const comp = rs.reduce((a,b)=>a*(1+b.ret),1) - 1;
    // 未筛选时直接取年度汇总同一个数（净值口径），保证两张表肉眼一致；
    // 筛选后子集没有对应的年度含义，退回落在本组行上的逐期连乘。
    const full = rs.length === M.trades.filter(t=>t.year===+y).length;
    const yv = YEARLY[+y];
    const head = (full && yv) ? `年度收益 ${pct(yv.ret)} ｜ ETF 对照 ${pct(yv.etf)}`
      : `本组逐期连乘 ${pct(comp)}`;
    const refill = rs.filter(r=>r.refill).length;
    return `<div class="year-head"><b>${y} 年</b>
      <em>${rs.length} 期 ｜ 熊市 ${rs.filter(r=>!r.bull).length} 期 ｜ 空仓 ${rs.filter(r=>!r.pick).length} 期`
      + (refill?` ｜ 补选 ${refill} 期`:'')
      + ` ｜ ${head}</em></div>
      <div class="scroll"><table><thead>${HEAD}</thead>
      <tbody>${rs.map(rowHtml).join('')}</tbody></table></div>`;
  }).join('');
}

document.getElementById('trades').innerHTML = renderTrades(M.trades);
document.getElementById('freg').onchange = e => {
  const v = e.target.value;
  const rows = M.trades.filter(t => v==='all' || (v==='bull'&&t.bull)
    || (v==='bear'&&!t.bull) || (v==='cash'&&!t.pick) || (v==='refill'&&!!t.refill));
  document.getElementById('trades').innerHTML = renderTrades(rows);
  document.getElementById('cnt').textContent =
    `已选 ${rows.length} 期（逐期连乘 ${pct(rows.reduce((a,b)=>a*(1+b.ret),1)-1)}）`;
};
document.getElementById('cnt').textContent = `共 ${M.trades.length} 期`;
</script>
</body>
</html>
"""


def pick_dir(pattern: str | None, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if not (p / "holds.csv").exists():
            raise SystemExit(f"目录里没有 holds.csv：{p}")
        return p
    base = ROOT / "reports" / "etf"
    cands = [p for p in base.iterdir()
             if p.is_dir() and (p / "holds.csv").exists()
             and fnmatch.fnmatch(p.name, pattern or "*")]
    if not cands:
        raise SystemExit(f"reports/etf 下没有匹配 {pattern or '*'} 的产出")
    return max(cands, key=lambda p: p.stat().st_mtime)


def load_spy(index: pd.DatetimeIndex) -> pd.Series | None:
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from quant.data import store
        px = store.load_panel(field="adj_close", symbols=["SPY"],
                              start=index[0].strftime("%Y-%m-%d"),
                              end=index[-1].strftime("%Y-%m-%d")).sort_index().ffill()
        return px["SPY"].reindex(index).ffill()
    except Exception as exc:  # 没有行情库时降级，只是不画 SPY
        print(f"  [warn] SPY 曲线跳过：{exc}")
        return None


def maxdd(s: pd.Series) -> float:
    s = s.dropna()
    if s.empty:
        return 0.0
    return float((s / s.cummax() - 1).min())


def yearly(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty:
        return pd.Series(dtype=float)
    ye, yf = s.groupby(s.index.year).last(), s.groupby(s.index.year).first()
    base = ye.shift(1)
    base.iloc[0] = yf.iloc[0]
    return ye / base - 1


def parse_params(dirname: str) -> list[tuple[str, str]]:
    """从目录名反推参数（目录名是唯一可靠的参数记录）。"""
    m = re.match(r"rsrs_stock_n(\d+)_m(\d+)_top1(.*)", dirname)
    tail = m.group(3) if m else dirname
    known = {"turnma", "turn", "cvdown", "dvol", "dvolcv", "dvolcvh", "mcap", "mmin", "rsrs"}
    pick = "rsrs"
    pm = re.match(r"_([a-z]+)", tail or "")
    if pm and pm.group(1) in known:
        pick = pm.group(1)

    # `_crsp` 是数据源标签、不是趋势模式，先摘掉再解析后面的开关
    crsp_scope = None
    if "_crsp" in (tail or ""):
        sm = re.search(r"_crsp(?:_(pool|metrics))?", tail)
        crsp_scope = (sm.group(1) if sm and sm.group(1) else "all")
        tail = re.sub(r"_crsp(?:_(?:pool|metrics))?", "", tail, count=1)

    def grab(pat: str) -> str | None:
        mm = re.search(pat, tail or "")
        return mm.group(1) if mm else None

    out = [("RSRS 窗口", f"N={m.group(1)} / M={m.group(2)}" if m else "-")]
    if crsp_scope:
        out.append(("数据源", "CRSP 标普500 标准数据（PIT 成分 / 真实股本与市值）"
                              + {"pool": "，仅修成分股", "metrics": "，仅修换手率/市值/价格"}
                              .get(crsp_scope, "")))
    out.append(("板块内选股", {
        "turnma": "换手率降序 + 趋势闸（逐块下探）",
        "turn": "换手率排名", "cvdown": "成交额 CV 降序 + 趋势闸",
        "rsrs": "个股 RSRS 最高", "mcap": "市值最大", "mmin": "市值最小",
        "dvol": "成交额最大", "dvolcv": "CV 最低", "dvolcvh": "CV 最高",
    }.get(pick, pick)))
    ttl = grab(rf"_{pick}_([a-z]+)")
    if ttl:
        out.append(("趋势模式", {"strong": "强化趋势（多闸）", "ma_stack": "均线排列",
                                 "below_mas": "跌破均线组", "ma_down": "均线下行"}.get(ttl, ttl)))
        st = grab(r"_(\d+-\d+(?:-\d+)?)_sl\d")
        if st:
            out.append(("趋势排列", " > ".join(f"MA{x}" for x in st.split("-"))))
        sl = grab(r"_sl(\d+)")
        if sl:
            out.append(("斜率窗口", f"{sl} 个交易日"))
        sma = grab(r"_slma([\d-]+)")
        out.append(("斜率均线", "MA" + "/MA".join(sma.split("-")) if sma else "MA5/20/60（旧口径）"))
        a = grab(r"_a(\d+)")
        if a:
            out.append(("收盘价要求", f"收盘 > MA{a}"))
        x = grab(r"_x([\d-]+)")
        if x:
            p = x.split("-")
            out.append(("均线先后", f"MA{p[0]} > MA{p[1]}"))
        adx = grab(r"_adx([\d.]+)")
        out.append(("ADX 下限", adx if adx else "0（不检查）"))
    cap = grab(r"_cap(\d+)")
    if cap:
        out.append(("换手率上限", f"{cap}%"))
    if "_tratio" in (tail or ""):
        out.append(("个股排序", "turn20^2 / turn120（绝对换手率 × 相对自身半年常态的倍数）"))
    mc = grab(r"_mc(\d+)")
    if mc:
        out.append(("最小市值", f"{mc} 亿美元"))
    reg = grab(r"_reg(\d+)")
    if reg:
        rw = grab(r"_rw(\d+)")
        out.append(("牛熊判定", f"SPY vs MA{reg}" + (f"，{rw} 日跌破占比 ≥50%" if rw else "，当日跌破")))
    blb = grab(r"_blb(\d+)")
    if blb:
        out.append(("熊市空窗补救", f"信号日交集为空 → 回看 {blb} 个交易日取最近一次双榜命中"))
    if "_bbref" in (tail or ""):
        out.append(("熊市空窗补救", "交集为空 → 次日起逐日重挑，命中即买"))
    if "_betfonly" in tail:
        out.append(("熊市处理", "只买最强板块 ETF"))
    elif "_betf" in tail:
        out.append(("熊市处理", "选不到个股时买板块 ETF"))
    bmin = grab(r"_bmin(-?[\d.]+)")
    if bmin is not None:
        out.append(("熊市动量门槛", f"板块 RSRS > {bmin}"))
    bm = re.search(r"_brankBoth(\d+)x(\d+)", tail or "")
    if bm:
        out.append(("熊市板块", f"RSRS 前 {bm.group(1)} ∩ 斜率前 {bm.group(1)}"
                                f"（斜率窗口 {bm.group(2)} 日）"))
    else:
        bsl = grab(r"_brankSlope(\d+)")
        if bsl:
            out.append(("熊市板块", f"加权斜率排序（窗口 {bsl} 日）"))
    out.append(("调仓频率", "月度（月末信号，次月首个交易日买入）"))
    if "_bref" in (tail or ""):
        out.append(("牛市空仓处理", "当期选不出 → 次日起逐日重挑，买到为止"))
    if "_bspy" in (tail or ""):
        out.append(("熊市兜底", "双条件全不达标 → 买 SPY"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="把回测产出打包成单文件 HTML 报告")
    ap.add_argument("--dir", default=None, help="指定产出目录；默认取 reports/etf 下最新一批")
    ap.add_argument("--pattern", default=None, help="按目录名匹配，如 *fullc50")
    ap.add_argument("--out", default="reports/web/index.html")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    d = pick_dir(args.pattern, args.dir)
    print(f"读取：{d}")

    nav = pd.read_csv(d / "nav_daily.csv", index_col=0, parse_dates=True).iloc[:, 0]
    net = pd.read_csv(d / "nav_etf_daily.csv", index_col=0, parse_dates=True).iloc[:, 0]
    net = net.reindex(nav.index)
    holds = pd.read_csv(d / "holds.csv")
    stats = pd.read_csv(d / "stats.csv", index_col=0)
    spy_px = load_spy(nav.index)
    spy_nav = (spy_px / spy_px.iloc[0] * nav.iloc[0]) if spy_px is not None else pd.Series(dtype=float)
    if isinstance(spy_nav, pd.Series) and not spy_nav.empty:
        spy_nav = spy_nav.reindex(nav.index).ffill()
        stats.loc["SPY", "累计"] = float(spy_nav.iloc[-1] / spy_nav.iloc[0] - 1)

    def rec(name: str) -> dict:
        r = stats.loc[name]
        return {"cum": float(r["累计"]), "cagr": float(r["年化"]), "vol": float(r["波动"]),
                "sharpe": float(r["夏普"]), "mdd": float(r["最大回撤"])}

    # 按「持有期起始日」归属年份：月度策略的 1 月持有期由上年 12 月末的信号决定，
    # 按信号日归属会把每年 1 月的持仓算到上一年，看起来就像"从 2 月开始"。
    holds["年份"] = pd.to_datetime(holds["持有期起"]).dt.year
    trades = []
    for _, r in holds.iterrows():
        def f(v):
            return None if pd.isna(v) else float(v)
        trades.append({
            "date": str(r["信号日"])[:10],
            "from": str(r["持有期起"])[:10],
            "to": str(r["持有期止"])[:10],
            "year": int(r["年份"]),
            "bull": bool(int(r["牛市"]) == 1),
            "sector": str(r["板块"]),
            "pick": "" if pd.isna(r["个股"]) else str(r["个股"]),
            "rsrs": f(r["板块RSRS"]),
            "slope": f(r["板块斜率%"]),
            "slope_rank": None if pd.isna(r["斜率序"]) else int(r["斜率序"]),
            "ret": float(r["个股收益"]),
            "etf_ret": float(r["ETF收益"]),
            "diff": float(r["选股差"]),
            "spy": f(r["SPY"]) or 0.0,
            "refill": (str(r["补选日"])[:10]
                       if "补选日" in holds.columns and pd.notna(r["补选日"]) else ""),
        })

    yr_ret, yr_etf = yearly(nav), yearly(net)
    yr_spy = yearly(spy_nav) if len(spy_nav) else pd.Series(dtype=float)
    first_d, last_d = nav.index[0], nav.index[-1]
    yearly_rows = []
    for y, g in holds.groupby("年份"):
        sr = float(yr_ret.get(y, 0.0))
        pr = float(yr_spy.get(y, 0.0)) if len(yr_spy) else 0.0
        yearly_rows.append({
            "year": int(y), "trades": int(len(g)), "bear": int((g["牛市"] == 0).sum()),
            "ret": sr, "etf": float(yr_etf.get(y, 0.0)), "spy": pr, "excess": sr - pr,
            "mdd": maxdd(nav[nav.index.year == y]),
            "diff": float(g["选股差"].mean()),
            # 只有真正被区间两端切断的年份才是「非完整年度」；末年跑满 12-31 就不算
            "partial": bool((y == first_d.year and (first_d.month, first_d.day) != (1, 1))
                            or (y == last_d.year and (last_d.month, last_d.day) != (12, 31))),
        })

    # 对账：分年明细用「逐期收益连乘」，年度汇总用「净值曲线区间收益」，两者必须一致
    comp = holds.groupby("年份")["个股收益"].apply(lambda s: (1 + s).prod() - 1)
    for row in yearly_rows:
        c = comp.get(row["year"])
        if c is not None and abs(float(c) - row["ret"]) > 1e-3:
            print(f"  [warn] {row['year']} 年：逐期连乘 {float(c):+.4%} ≠ 净值口径 {row['ret']:+.4%}")

    meta = {
        "params": parse_params(d.name),
        "stats": {"strategy": rec("个股版"), "etf": rec("ETF版"), "spy": rec("SPY")},
        "yearly": yearly_rows,
        "trades": trades,
    }
    series = {
        "dates": [x.strftime("%Y-%m-%d") for x in nav.index],
        "strategy": [None if pd.isna(v) else round(float(v), 4) for v in nav],
        "etf": [None if pd.isna(v) else round(float(v), 4) for v in net],
        "spy": [None if pd.isna(v) else round(float(v), 4) for v in spy_nav] if len(spy_nav) else [],
    }

    title = args.title or "RSRS 板块→个股 轮动回测"
    html = (TEMPLATE
            .replace("__TITLE__", title)
            .replace("__RANGE__", f"{nav.index[0].date()} ~ {nav.index[-1].date()}（{len(nav)} 个交易日，{len(holds)} 次调仓）")
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__DIR__", d.name)
            .replace("__DATA__", json.dumps({"meta": meta, "series": series}, ensure_ascii=False)))

    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"产出：{out}  ({out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
