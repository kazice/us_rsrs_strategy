"""逐期「板块内前 3 名候选」日 K 看板：信号日前 3 个月 → 持有期止。

用法（项目根目录）：
    $env:PYTHONPATH='src'; python -m scripts.build_picks_chart              # 取最新一批产出
    $env:PYTHONPATH='src'; python -m scripts.build_picks_chart --pattern "*m80_nobmin"
    $env:PYTHONPATH='src'; python -m scripts.build_picks_chart --pre 63 --out reports/web/picks.html

窗口口径：左边界 = 信号日往前 ``--pre`` 个交易日（默认 63 ≈ 3 个月），
右边界 = 该期「持有期止」（≈ 信号后 1 个月，正是策略实际持有的那段）。

每期最多 3 个板块 × 换手率前 3 只，每个板块配一张板块 ETF 对照，共最多 12 张独立日 K；
各用各的价格轴，不叠加。浅色底纹 = 持有区间，灰虚线 = 信号日，橙虚线 = 买入日，
橙圆点 = 买入价，浅蓝实线 + 虚线 = 布林带（中轨 MA20）。

数据依赖：``holds.csv`` 里的 ``候选前3`` 列（由 sector_stock_rsrs 写入），以及 ``data/curated``
下的日线。目录名带 ``_pool``（或历史的 ``_crsp``）时个股走 ``sp500_pool`` 的复权 OHLC
（security_id 主键，与回测同源）；ETF 不在池里，仍走本地行情池。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
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
  .wrap{max-width:1360px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:22px;margin:0 0 6px}
  .sub{color:var(--dim);font-size:12.5px}
  .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:18px 0 4px}
  .bar button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
       border-radius:7px;padding:6px 11px;font:13px inherit;cursor:pointer}
  .bar button.on{background:#2b4a86;border-color:#4f8ef7;color:#fff}
  .grid{display:grid;gap:14px;grid-template-columns:1fr;margin-top:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px 12px}
  .chead{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-bottom:2px}
  .chead b{font-size:15px}
  .chead .idx{color:var(--dim);font-size:12px}
  .tag{font-size:11.5px;padding:1px 7px;border-radius:20px;border:1px solid}
  .tag.bull{color:var(--green);border-color:#2b6b4d;background:#14301f}
  .tag.bear{color:var(--red);border-color:#6b2b31;background:#301418}
  .tag.cash{color:var(--dim);border-color:var(--line);background:var(--panel2)}
  .tag.etf{color:var(--blue);border-color:#2b4a86;background:#14213b}
  .sec{color:var(--amber)}
  .meta{color:var(--dim);font-size:12.5px;margin-bottom:8px}
  .charts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
  .grid.big .charts{grid-template-columns:repeat(2,minmax(0,1fr))}
  @media (max-width:1100px){.charts{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .sgroup{margin-top:16px}
  .ghead{font-size:12.5px;color:var(--dim);margin:0 0 7px;padding-left:8px;
         border-left:3px solid #2b4a86}
  .ghead b{color:var(--txt);font-size:13px}
  .pager{display:inline-flex;gap:8px;align-items:center;margin-left:auto}
  .pager button:disabled{opacity:.3;cursor:default}
  .pager select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
      border-radius:7px;padding:5px 8px;font-size:12.5px;max-width:230px;cursor:pointer}
  .cbox{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px 6px}
  .ctitle{display:flex;justify-content:space-between;align-items:baseline;gap:6px;font-size:12px}
  .ctitle b{font-size:13.5px}
  .ctitle .pk{color:var(--amber)}
  .cnum{color:var(--dim);font-size:11.5px}
  svg{display:block;width:100%;height:auto;overflow:visible}
  .pos{color:var(--green)} .neg{color:var(--red)} .dim{color:var(--dim)}
  .foot{margin-top:26px;color:var(--dim);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub">回测区间 __RANGE__ ｜ 每期最多 3 个板块 × 换手率前 3 名（≤9 只）｜ 窗口 = 信号日前 __PRE__ 个交易日 → 持有期止
    ｜ 生成于 __GENERATED__ ｜ 数据源 <code>__DIR__</code></div>

  <div class="bar">
    <span class="dim">筛选</span>
    <button data-f="all" class="on">全部</button>
    <button data-f="bull">仅牛市个股</button>
    <button data-f="bear">仅熊市</button>
    <button data-f="etf">仅买 ETF</button>
    <button data-f="cash">仅空仓</button>
    <button data-f="stop">仅止损期</button>
    <button id="zoom">放大 2×2</button>
    <span class="dim" id="cnt"></span>
    <span class="pager">
      <button class="prev">‹ 上一期</button>
      <span class="dim lbl">1 / 1</span>
      <button class="next">下一期 ›</button>
      <select class="jump" title="跳到某一期"></select>
    </span>
  </div>

  <div class="grid" id="grid"></div>

  <div class="bar" style="margin-top:16px">
    <span class="dim">← / → 键翻页</span>
    <span class="pager">
      <button class="prev">‹ 上一期</button>
      <span class="dim lbl">1 / 1</span>
      <button class="next">下一期 ›</button>
      <select class="jump" title="跳到某一期"></select>
    </span>
  </div>

  <div class="foot">
    每期四张独立的日 K（板块内前三 + 板块 ETF），各自独立价格轴，不叠加；红涨绿跌按美股习惯
    （收盘 ≥ 开盘为绿）。<br>
    浅色底纹 = 实际持有区间，<b>灰色虚线 = 信号日</b>（月末收盘定档、当日决定买谁），
    <b>橙色虚线 = 买入日</b>（新一期建仓那天；只有补选时才会和信号日分开），
    <b>橙色圆点 = 买入价</b>（买入日的前一个收盘，收益从次日开始算）。
    实际买入的那只在标题里带 ★。价格均为前复权 OHLC，跨拆股/分红连续。<br>
    <b>布林带：中轨 = __BOLL__</b>（实线），<b>上下轨 = 中轨 ± __BK__ 倍标准差</b>（虚线）。
    收盘贴上轨 = 短期偏热，跌破下轨 = 短期超卖，中轨就是这段时间的均价。<br>
    __BBDNOTE__
    「窗口」= 窗口首日到期末的涨跌；「持有」= 建仓日到期末的涨跌（纯价格，不含换手成本
    与止损规则），与卡片右侧的真实成交收益可能有差异。<br>
    卡片头部出现「<span style="color:#ffc9d2">止损</span>」标签 = 该期触发了止损，
    标签里写明止损日、止损类型（均线 / 固定比例）和再选股的票；持有收益按止损后的实际持仓计算。<br>
    候选图下方的「<span style="color:#e8a0a8">落选：xxx</span>」= 该票当期被检视过、但没过哪一道趋势闸
    （均线排列 / 斜率 / MA50 / ADX / 追高 / 位置分位 / 布林带），写的是**第一道**没过的闸。
    「<span style="color:#8fd18f">合格 · 未买</span>」= 九道闸全过，但同一期前面顺位已经买到票了
    （单票满仓，不会同时买两只）。两种标注都没有的，是换手率没进本板块前 3、根本没进候选。<br>
    <b>「累计试 N 次」</b>：为了买到一只票一共试了多少个候选——每轮最多下探 3 个板块
    （`--turnma-sectors 3`，按板块 RSRS 排序），每个板块只看换手率前 3 名（`--turnma-top 3`），
    逐只过趋势闸；当轮全不过就进补选（`--bull-refill`），从次日起每天重扫一轮。
    所以 52 次 = 6 个交易日 × 每轮最多 9 只（个别板块当天候选不足 3 只，故为 52），
    不是「板块内第 52 名」；板块内到底有多少只可挑，看「板块内可挑 N 只」。
  </div>
</div>
<script>
const DATA = __DATA__;
const P = DATA.periods;
const pct = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%';
const cls = v => v >= 0 ? 'pos' : 'neg';
const CW = 340, CH = 186, CL = 42, CR = 8, CT = 18, CB = 20;

function kchart(p, se) {
  const n = p.dates.length;
  // 布林带：中轨 = MA(N)，上下轨 = 中轨 ± K 倍标准差
  const BB = (DATA.meta && DATA.meta.boll) || [0, 0];
  const BDELTA = (DATA.meta && DATA.meta.bbdelta) || 0;
  let boll = null;
  if (BB[0] > 1) {
    const N = BB[0], K = BB[1] || 2, need = Math.min(N, 10);
    const ma = [], up = [], dn = [], upSafe = [], sdArr = [];
    for (let i = 0; i < n; i++) {
      let s = 0, c = 0;
      for (let j = Math.max(0, i - N + 1); j <= i; j++) {
        const d = se.ohlc[j];
        if (!d) continue;
        s += d[3]; c++;
      }
      if (c < need) {
        ma.push(null); up.push(null); dn.push(null); upSafe.push(null); sdArr.push(null);
        continue;
      }
      const m = s / c;
      let v = 0;
      for (let j = Math.max(0, i - N + 1); j <= i; j++) {
        const d = se.ohlc[j];
        if (!d) continue;
        v += (d[3] - m) * (d[3] - m);
      }
      const sd = Math.sqrt(v / c);
      ma.push(m); up.push(m + K * sd); dn.push(m - K * sd);
      upSafe.push(m + Math.max(0, K - BDELTA) * sd);
      sdArr.push(sd);
    }
    boll = {ma, up, dn, upSafe, sd: sdArr, k: K};
  }
  let lo = Infinity, hi = -Infinity;
  se.ohlc.forEach(d => {
    if (d) { lo = Math.min(lo, d[2]); hi = Math.max(hi, d[1]); }
  });
  if (boll) {
    boll.up.forEach(v => { if (v != null && v > hi) hi = v; });
    boll.dn.forEach(v => { if (v != null && v < lo) lo = v; });
  }
  if (!(hi >= lo) || !isFinite(lo)) return '<div class="cnum">无行情</div>';
  const pad = (hi - lo) * 0.07 || (hi * 0.02) || 1;
  lo -= pad; hi += pad;
  const span = hi - lo;
  const slot = (CW - CL - CR) / Math.max(1, n);
  const bw = Math.max(1.2, slot * 0.66);
  const X = i => CL + (i + 0.5) * slot;
  const Y = v => CT + (1 - (v - lo) / span) * (CH - CT - CB);

  let s = `<svg viewBox="0 0 ${CW} ${CH}">`;
  // 建仓点按图走：再选股那组用「再选日」，其余用本期统一建仓日
  const eIdx = (se.entryIdx != null) ? se.entryIdx
    : ((p.entryIdx != null) ? p.entryIdx : p.holdFrom);
  // 止损后空仓的期次：底纹只画到止损日，不把空仓那段也伪装成持仓
  const holdEnd = (p.stopIdx != null && p.stopIdx >= 0 && !p.repickSym
                   && eIdx != null && p.stopIdx >= eIdx) ? p.stopIdx : n - 1;
  if (eIdx != null && eIdx >= 0 && eIdx <= n - 1) {
    s += `<rect x="${(CL + eIdx * slot).toFixed(1)}" y="${CT}"
           width="${Math.max(1, (holdEnd - eIdx + 1) * slot).toFixed(1)}"
           height="${CH - CT - CB}" fill="#243354" opacity=".5"/>`;
  }
  for (let k = 0; k <= 3; k++) {
    const v = lo + span * k / 3, y = Y(v);
    s += `<line x1="${CL}" y1="${y.toFixed(1)}" x2="${CW - CR}" y2="${y.toFixed(1)}"
           stroke="#24314d" stroke-width="1"/>`;
    s += `<text x="${CL - 5}" y="${(y + 3.4).toFixed(1)}" fill="#93a4c0" font-size="9.5"
           text-anchor="end">${v >= 100 ? v.toFixed(0) : v.toFixed(2)}</text>`;
  }
  const hasSig = (p.sigIdx != null && p.sigIdx >= 0);
  const hasBuy = (p.holdFrom != null && p.holdFrom >= 0 && p.holdFrom <= n - 1);
  const split = hasBuy && hasSig && p.holdFrom !== p.sigIdx;
  const vline = (i, col, txt) => {
    const x = X(i).toFixed(1);
    return `<line x1="${x}" y1="${CT}" x2="${x}" y2="${CH - CB}" stroke="${col}"
             stroke-width="1" stroke-dasharray="3 3"/>`
      + `<text x="${x}" y="${CT - 5}" fill="${col}" font-size="8.5" text-anchor="middle">${txt}</text>`;
  };
  if (hasSig) s += vline(p.sigIdx, '#8ea3c4', split ? '信号' : '信号·买入');
  if (split) s += vline(p.holdFrom, '#f0a63c', '买入');
  if (p.stopIdx != null && p.stopIdx >= 0 && p.stopIdx < n) {
    s += vline(p.stopIdx, '#ef5b6b', '止损');
  }
  if (boll) {
    const seg = arr => {
      let d = '', on = false;
      arr.forEach((v, i) => {
        if (v == null) { on = false; return; }
        d += (on ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' ';
        on = true;
      });
      return d;
    };
    if (BDELTA > 0) {
      // 危险区：上轨与其下移 BDELTA 倍标准差的那条线之间（回测里的禁买带）
      const band = (top, bot) => {
        let d = '', run = [];
        const flush = () => {
          if (run.length >= 2) {
            let p = `M${run[0][0].toFixed(1)} ${run[0][1].toFixed(1)}`;
            for (let k = 1; k < run.length; k++)
              p += `L${run[k][0].toFixed(1)} ${run[k][1].toFixed(1)}`;
            for (let k = run.length - 1; k >= 0; k--)
              p += `L${run[k][0].toFixed(1)} ${run[k][2].toFixed(1)}`;
            d += p + 'Z';
          }
          run = [];
        };
        for (let i = 0; i < n; i++) {
          if (top[i] == null || bot[i] == null) { flush(); continue; }
          run.push([X(i), Y(top[i]), Y(bot[i])]);
        }
        flush();
        return d;
      };
      s += `<path d="${band(boll.up, boll.upSafe)}" fill="#ef5b6b"
             opacity=".16" stroke="none"/>`;
      s += `<path d="${seg(boll.upSafe)}" fill="none" stroke="#ef5b6b"
             stroke-width="1" opacity=".9" stroke-dasharray="4 2"/>`;
    }
    s += `<path d="${seg(boll.up)}" fill="none" stroke="#6f88d6" stroke-width="1"
           opacity=".75" stroke-dasharray="2 2"/>`;
    s += `<path d="${seg(boll.dn)}" fill="none" stroke="#6f88d6" stroke-width="1"
           opacity=".75" stroke-dasharray="2 2"/>`;
    s += `<path d="${seg(boll.ma)}" fill="none" stroke="#b3c2f5" stroke-width="1"
           opacity=".95"/>`;
  }
  se.ohlc.forEach((d, i) => {
    if (!d) return;
    const [o, h, l, c] = d;
    const up = c >= o;
    const col = up ? '#3ec98a' : '#ef5b6b';
    const x = X(i);
    s += `<line x1="${x.toFixed(1)}" y1="${Y(h).toFixed(1)}" x2="${x.toFixed(1)}"
           y2="${Y(l).toFixed(1)}" stroke="${col}" stroke-width="1"/>`;
    const y0 = Y(Math.max(o, c)), y1 = Y(Math.min(o, c));
    s += `<rect x="${(x - bw / 2).toFixed(1)}" y="${y0.toFixed(1)}" width="${bw.toFixed(1)}"
           height="${Math.max(0.9, y1 - y0).toFixed(1)}" fill="${col}" stroke="${col}"/>`;
  });
  if (eIdx != null && eIdx >= 0 && se.ohlc[eIdx]) {
    const c = se.ohlc[eIdx][3];
    const cx = X(eIdx), cy = Y(c);
    let zb = null;
    if (boll && boll.k && boll.sd[eIdx] != null && boll.sd[eIdx] > 0) {
      zb = (c - boll.ma[eIdx]) / boll.sd[eIdx];
    }
    // 买入价落进禁买区（z 高于上轨往下 BDELTA 倍标准差）就标红提示
    const hot = (zb != null && BDELTA > 0 && zb > (boll.k - BDELTA));
    const mcol = hot ? '#ef5b6b' : '#f0a63c';
    s += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="3" fill="${mcol}"
           stroke="#0f172a" stroke-width="1"/>`;
    const right = cx > CW - 88;
    const ty = (cy - 8 < CT + 8) ? cy + 13 : cy - 8;
    const lbl = `${hot ? '⚠' : ''}买 $${c.toFixed(2)}${zb == null ? '' : ' z' + zb.toFixed(1)}`;
    s += `<text x="${(cx + (right ? -5 : 5)).toFixed(1)}" y="${ty.toFixed(1)}"
           fill="${mcol}" font-size="9" text-anchor="${right ? 'end' : 'start'}">${lbl}</text>`;
  }
  const marks = n <= 2 ? [0] : [0, Math.round((n - 1) / 2), n - 1];
  marks.forEach(i => {
    s += `<text x="${X(i).toFixed(1)}" y="${CH - 6}" fill="#93a4c0" font-size="9.5"
           text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}">${p.dates[i]}</text>`;
  });
  return s + '</svg>';
}

function box(p, se) {
  const isBuy = se.pick;
  // 止损期：原票一律标「止损卖出」（换股或空仓都一样，它确实被卖了），新票标「买入」
  const isStoppedOut = p.stop && se.sym === p.pick;
  const star = isBuy ? ' ★' : '';
  const badge = isBuy
    ? '<span class="tag" style="background:#8a5a12;color:#ffe6b6">买入</span>'
    : (isStoppedOut
      ? '<span class="tag" style="background:#4a4a55;color:#d8d8e0">止损卖出</span>' : '');
  const hl = isBuy ? ' style="outline:2px solid #f0a63c;outline-offset:1px"' : '';
  // 落选原因 / 合格未买：只给没买的个股显示，ETF 没有闸门
  const raw = (!isBuy && !se.etf && p.gates) ? (p.gates[se.sym] || '') : '';
  const isPass = raw === 'PASS';
  const gf = (raw && raw !== 'OK' && raw !== 'PASS') ? raw : '';
  return `<div class="cbox"${hl}>
    <div class="ctitle">
      <span><b class="${isBuy ? 'pk' : ''}">${se.sym}${star}</b>${badge}
        ${se.etf ? '<span class="dim">板块ETF</span>' : ''}</span>
      <span class="cnum">窗口 <span class="${cls(se.winRet)}">${pct(se.winRet)}</span></span>
    </div>
    <div class="ctitle" style="margin-bottom:2px">
      <span class="cnum">持有 <span class="${cls(se.holdRet || 0)}">${pct(se.holdRet)}</span></span>
      <span class="cnum">信号 ${p.sig.slice(5)} → ${p.to.slice(5)}</span>
    </div>
    ${gf ? `<div style="font-size:11px;line-height:1.35;color:#e8a0a8;margin:0 0 3px 2px">落选：${gf}</div>` : ''}
    ${isPass ? '<div style="font-size:11px;line-height:1.35;color:#8fd18f;margin:0 0 3px 2px">合格 · 未买（前序顺位已买入）</div>' : ''}
    ${kchart(p, se)}
  </div>`;
}

function card(p) {
  const reg = p.etfOnly ? '<span class="tag etf">买 ETF</span>'
    : p.cash ? '<span class="tag cash">空仓</span>'
    : (p.bull ? '<span class="tag bull">牛市</span>' : '<span class="tag bear">熊市</span>');
  const pickTxt = (p.cash ? '<span class="dim">空仓</span>'
    : (p.etfOnly ? `买入 ETF <b>${p.pick}</b>` : `买入 <b>${p.pick}</b>`))
    + (p.repickSym ? ` <span class="dim">→ 止损后换</span> <b>${p.repickSym}</b>` : '')
    + (p.stop && !p.repickSym ? ' <span class="dim">→ 止损后空仓</span>' : '');
  const groups = [];
  p.series.forEach(se => {
    const last = groups[groups.length - 1];
    if (!last || last.sec !== se.sec) groups.push({sec: se.sec || '-', sr: se.sr, items: []});
    groups[groups.length - 1].items.push(se);
  });
  const charts = groups.map(g => {
    const isRepick = g.sec === '止损后再选';
    const sub = isRepick
      ? '<span class="dim">止损后次日按同一套规则重选（原仓已卖出）</span>'
      : `<span class="dim">板块 RSRS 第 ${g.sr} 顺位 ｜ 换手率前 `
        + `${g.items.filter(x => !x.etf).length} 只 + 板块 ETF</span>`;
    return `<div class="sgroup">
      <div class="ghead"><b>${g.sec}</b>${sub}</div>
      <div class="charts">${g.items.map(se => box(p, se)).join('')}</div>
    </div>`;
  }).join('');
  const stopTag = p.stop
    ? `<span class="tag" style="background:#5b2330;color:#ffc9d2">止损 ${p.stop.slice(5)}`
      + `${p.stopKind ? ' · ' + p.stopKind : ''}`
      + `${p.repick ? ' → 再选 ' + p.repick.split(' ').pop() : ''}</span>`
    : '';
  return `<div class="card">
    <div class="chead">
      <span class="idx">#${p.i}</span><b>${p.sig}</b>${reg}${stopTag}
      <span class="sec">${p.sector}</span>
      <span class="dim">买入 ${p.from} ｜ 持有至 ${p.to}</span>
      <span>${pickTxt}</span>
      <span class="${cls(p.ret)}"><b>${pct(p.ret)}</b></span>
    </div>
    <div class="meta">板块 RSRS ${p.rsrs == null ? '-' : p.rsrs.toFixed(2)}
      ｜ 全板块排第 ${p.pickRank ?? '-'} ｜ ${p.etfOnly || p.cash
        ? '候选仅作对照（当期没买个股）' : `下探 ${p.nsec} 个板块`} / 共 ${p.nsym} 只候选
      ｜ 累计试 ${p.tried} 次${p.refill ? ` ｜ 补选 ${p.refill.slice(5)}` : ''}
      ｜ 板块 ETF 同期 ${p.holdRet == null ? '-' : pct(p.holdRet)}</div>
    ${charts || '<div class="dim">当期没有候选（空仓或直接买 ETF）</div>'}
  </div>`;
}

let FILTER = 'all', IDX = 0;
const rowsOf = f => P.filter(p => f === 'all'
  || (f === 'bull' && p.bull && !p.etfOnly) || (f === 'bear' && !p.bull)
  || (f === 'etf' && p.etfOnly) || (f === 'cash' && p.cash)
  || (f === 'stop' && !!p.stop));

function render() {
  const rows = rowsOf(FILTER);
  document.getElementById('cnt').textContent = `共 ${rows.length} / ${P.length} 期`;
  if (!rows.length) {
    document.getElementById('grid').innerHTML = '<div class="card dim">没有符合条件的期次</div>';
    document.querySelectorAll('.pager .lbl').forEach(e => (e.textContent = '0 / 0'));
    document.querySelectorAll('.pager .jump').forEach(e => (e.innerHTML = ''));
    return;
  }
  IDX = Math.min(Math.max(0, IDX), rows.length - 1);
  document.getElementById('grid').innerHTML = card(rows[IDX]);
  document.querySelectorAll('.pager .lbl')
    .forEach(e => (e.textContent = `${IDX + 1} / ${rows.length}`));
  document.querySelectorAll('.pager .jump').forEach(sel => {
    sel.innerHTML = rows.map((p, i) =>
      `<option value="${i}">#${p.i} ${p.sig} ${p.sector}${p.pick ? ' ' + p.pick : ''}</option>`
    ).join('');
    sel.value = IDX;
  });
  document.querySelectorAll('.pager .prev').forEach(b => (b.disabled = IDX === 0));
  document.querySelectorAll('.pager .next')
    .forEach(b => (b.disabled = IDX === rows.length - 1));
}

document.querySelectorAll('.bar button[data-f]').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    FILTER = b.dataset.f; IDX = 0; render();
  };
});
document.querySelectorAll('.pager .prev').forEach(b => (b.onclick = () => {
  IDX -= 1; render(); window.scrollTo({top: 0});
}));
document.querySelectorAll('.pager .next').forEach(b => (b.onclick = () => {
  IDX += 1; render(); window.scrollTo({top: 0});
}));
document.querySelectorAll('.pager .jump').forEach(sel => (sel.onchange = e => {
  const v = Number(e.target.value);
  if (!Number.isNaN(v)) { IDX = v; render(); window.scrollTo({top: 0}); }
}));
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { IDX -= 1; render(); window.scrollTo({top: 0}); }
  else if (e.key === 'ArrowRight') { IDX += 1; render(); window.scrollTo({top: 0}); }
});
document.getElementById('zoom').onclick = e => {
  document.getElementById('grid').classList.toggle('big');
  e.target.classList.toggle('on');
};
render();
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


def load_ohlc_store(syms: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """本地行情：open/high/low 是原始价，close 原始、adj_close 复权 → 统一乘到复权空间。"""
    sys.path.insert(0, str(ROOT / "src"))
    from quant.data import store
    if not syms:
        return {}
    raw = {f: store.load_panel(field=f, symbols=syms, start=start, end=end).sort_index()
           for f in ("open", "high", "low", "close", "adj_close")}
    adj = raw["adj_close"]
    fac = adj / raw["close"].where(raw["close"] > 0)
    out = {}
    for s in syms:
        if s not in adj.columns:
            continue
        f = fac[s]
        df = pd.DataFrame({
            "open": raw["open"][s] * f, "high": raw["high"][s] * f,
            "low": raw["low"][s] * f, "close": adj[s],
        })
        out[s] = df
    return out


def load_ohlc_pool(syms: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """池层复权 OHLC（security_id 主键），按 ticker 返回给绘图。

    ticker 只用来这里对齐「图上要画哪只」，取数按 security_id：同一证券改过名时
    （FB→META）整段历史都能画，不会像以前那样在改名日断掉。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from quant.data import pool
    if not syms:
        return {}
    th = pool.tickers()
    th["ticker"] = th["ticker"].astype(str).str.upper()
    sid_of: dict[str, str] = {}
    for s in syms:
        hit = th[th["ticker"] == s.upper()]
        if len(hit):
            sid_of[s] = str(hit.sort_values("end").iloc[-1]["security_id"])
    ids = sorted(set(sid_of.values()))
    if not ids:
        return {}
    p = {f: pool.panel(f, ids, start, end).sort_index()
         for f in ("adj_open", "adj_high", "adj_low", "adj_close")}
    out = {}
    for s, sid in sid_of.items():
        if sid not in p["adj_close"].columns:
            continue
        df = pd.DataFrame({"open": p["adj_open"][sid], "high": p["adj_high"][sid],
                           "low": p["adj_low"][sid], "close": p["adj_close"][sid]})
        df = df.dropna(subset=["close"])
        if len(df):
            out[s] = df
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="逐期板块前三候选日 K 看板")
    ap.add_argument("--dir", default=None, help="指定产出目录；默认取 reports/etf 下最新一批")
    ap.add_argument("--pattern", default=None, help="按目录名匹配，如 *m80_nobmin")
    ap.add_argument("--pre", type=int, default=63, help="信号日往前画多少个交易日（默认 63 ≈ 3 个月）")
    ap.add_argument("--boll", default="20,2",
                    help="布林带：周期,标准差倍数（默认 20,2）；0 或空 = 不画")
    ap.add_argument("--bb-delta", type=float, default=0.0,
                    help="布林带追高闸的 delta（与回测 --bb-delta 一致）："
                         "上轨往下多少倍标准差算「禁买区」，0 = 不画")
    ap.add_argument("--out", default="reports/web/picks.html")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    boll_n, boll_k = 0, 2.0
    if str(args.boll).strip() not in ("", "0"):
        _b = [x for x in str(args.boll).replace("~", ",").split(",") if x.strip()]
        if _b:
            boll_n = int(float(_b[0]))
            boll_k = float(_b[1]) if len(_b) > 1 else 2.0

    d = pick_dir(args.pattern, args.dir)
    print(f"读取：{d}")
    holds = pd.read_csv(d / "holds.csv")
    if "候选前3" not in holds.columns:
        raise SystemExit("holds.csv 没有「候选前3」列：请用新版 sector_stock_rsrs 重跑这批回测")

    for c in ("信号日", "持有期起", "持有期止"):
        holds[c] = pd.to_datetime(holds[c])

    def cands(v) -> list[str]:
        if not isinstance(v, str) or not v.strip():
            return []
        return [x.strip() for x in v.split(",") if x.strip()]

    def bare(v) -> str:
        s = "" if pd.isna(v) else str(v)
        return s[4:] if s.startswith("ETF:") else s

    stock_syms, etf_syms = set(), set()
    for _, r in holds.iterrows():
        stock_syms.update(cands(r["候选前3"]))
        if "候选明细" in holds.columns:
            for part in str(r["候选明细"] or "").split("|"):
                head, sep, names = part.partition(":")
                stock_syms.update(cands(names))
                # 明细里每个顺位板块自己的 ETF 也要加载行情，否则该板块的对照图会缺
                if sep and head.strip():
                    etf_syms.add(head.partition("#")[0].strip())
        if pd.notna(r["个股"]) and not str(r["个股"]).startswith("ETF:"):
            stock_syms.add(bare(r["个股"]))
        if pd.notna(r["板块"]):
            etf_syms.add(str(r["板块"]))
        if pd.notna(r["个股"]) and str(r["个股"]).startswith("ETF:"):
            etf_syms.add(bare(r["个股"]))
    etf_syms -= stock_syms
    stock_syms -= etf_syms
    stock_syms, etf_syms = sorted(stock_syms), sorted(etf_syms)

    start = (holds["信号日"].min() - pd.Timedelta(days=160)).strftime("%Y-%m-%d")
    end = holds["持有期止"].max().strftime("%Y-%m-%d")
    use_pool = ("_pool" in d.name) or ("_crsp" in d.name)
    print(f"个股 {len(stock_syms)} 只（{'池层' if use_pool else '本地'}复权 OHLC） / ETF {len(etf_syms)} 只"
          f" / {start} ~ {end}")

    ohlc = load_ohlc_store(etf_syms, start, end)
    if use_pool:
        try:
            ohlc.update(load_ohlc_pool(stock_syms, start, end))
        except Exception as exc:  # 池层不可用就退回本地行情（旧产出的目录）
            print(f"  [warn] 池层行情不可用，改用本地行情：{exc}")
            ohlc.update(load_ohlc_store(stock_syms, start, end))
    else:
        ohlc.update(load_ohlc_store(stock_syms, start, end))
    if not ohlc:
        raise SystemExit("没取到任何行情")

    # 交易日历取所有标的的并集
    idx = sorted(set().union(*[set(df.index) for df in ohlc.values()]))
    idx = pd.DatetimeIndex(idx)
    print(f"交易日历 {len(idx)} 天，标的 {len(ohlc)} 个")

    bad = 0
    for s, df in ohlc.items():
        hi_bad = (df["high"] < df[["open", "close"]].max(axis=1) - 1e-6).sum()
        lo_bad = (df["low"] > df[["open", "close"]].min(axis=1) + 1e-6).sum()
        bad += int(hi_bad + lo_bad)
    if bad:
        print(f"  [warn] OHLC 关系异常 {bad} 处（复权口径可能不一致，图上会出现穿头破脚）")

    def series_of(sym: str, dates: pd.DatetimeIndex, entry: pd.Timestamp) -> dict | None:
        if sym not in ohlc:
            return None
        df = ohlc[sym].reindex(dates)
        if df["close"].notna().sum() < 5:
            return None
        base = float(df["close"].dropna().iloc[0])
        if not (base > 0):
            return None
        rows = []
        for o, h, l, c in df[["open", "high", "low", "close"]].itertuples(index=False):
            if pd.isna(c) or pd.isna(h) or pd.isna(l):
                rows.append(None)
            else:
                o = c if pd.isna(o) else o
                rows.append([round(float(o), 2), round(float(h), 2),
                             round(float(l), 2), round(float(c), 2)])
        upto = df["close"].loc[:entry].dropna()
        return {
            "sym": sym,
            "ohlc": rows,
            "winRet": float(df["close"].dropna().iloc[-1] / base - 1),
            "holdRet": float(df["close"].dropna().iloc[-1] / float(upto.iloc[-1]) - 1) if len(upto) else 0.0,
        }

    periods = []
    for i, r in holds.reset_index(drop=True).iterrows():
        sig, e = r["信号日"], r["持有期止"]
        pos = int(idx.searchsorted(sig, side="right")) - 1
        if pos < 0:
            continue
        dates = idx[max(0, pos - args.pre):int(idx.searchsorted(e, side="right"))]
        if len(dates) == 0:
            continue
        sig_i = int(dates.searchsorted(sig, side="right")) - 1
        hold_i = max(0, int(dates.searchsorted(r["持有期起"], side="right")) - 1)
        # 建仓价取持有期起的前一个收盘（信号日收盘定档、次日开始吃涨跌）；
        # 首期没有前一日，退化为持有期起当天，与净值曲线起点一致。
        entry_i = hold_i - 1 if (i > 0 and hold_i > 0) else hold_i
        entry = dates[entry_i]

        pick_sym = bare(r["个股"]) if pd.notna(r["个股"]) else ""
        is_stock = bool(pick_sym) and not str(r["个股"]).startswith("ETF:")
        picked = pick_sym if is_stock else ""

        # 候选明细格式：板块#顺位:票1,票2,票3|板块#顺位:...（最多 3 板块 × 前 3）
        sections = []
        detail = str(r.get("候选明细") or "") if "候选明细" in holds.columns else ""
        for part in detail.split("|"):
            head, sep, names = part.partition(":")
            if not sep or not head.strip():
                continue
            sec_k, _, sr_k = head.partition("#")
            sections.append({"sec": sec_k.strip(),
                             "sr": int(sr_k) if str(sr_k).strip().isdigit() else 1,
                             "syms": [x for x in names.split(",") if x.strip()]})
        if not sections:
            # 新产出里明细为空 = 当期没进选股流程（熊市直接买 ETF / 空仓），只画板块 ETF；
            # 老产出没有明细列，退回「当前板块前三 + ETF」
            etf0 = str(r["板块"]) if pd.notna(r["板块"]) else ""
            legacy = [] if "候选明细" in holds.columns else cands(r["候选前3"])
            sections = [{"sec": etf0, "sr": 1, "syms": legacy}]
        def _txt(col: str) -> str:
            """取可空的文本列；CSV 里的空值可能是 NaN / 空串，统一成空串。"""
            if col not in holds.columns or pd.isna(r[col]):
                return ""
            s = str(r[col]).strip()
            return "" if s.lower() in ("nan", "nat", "none") else s

        stop_s, stop_kind = _txt("止损日"), _txt("止损类型")
        repick_s = _txt("再选股")                       # 格式 "YYYY-MM-DD SYM"
        repick_sym = repick_s.split(" ")[-1].strip() if repick_s else ""
        # 止损日在本期横轴上的位置：-1 = 不在窗口内（期末触发或数据缺失）
        stop_idx = -1
        if stop_s:
            _p = int(dates.searchsorted(pd.Timestamp(stop_s), side="right")) - 1
            stop_idx = _p if 0 <= _p < len(dates) else -1

        # 候选落选原因：回测写的是 "SYM=原因|SYM=OK|..."，解析成图上的小字
        gates: dict[str, str] = {}
        for _kv in _txt("候选闸门").split("|"):
            if "=" in _kv:
                _k, _v = _kv.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _v:
                    gates[_k] = _v          # OK=已买入 / PASS=合格但没买 / 其余=落选原因

        if picked and not any(picked in s["syms"] for s in sections):
            sections[0]["syms"] = [picked] + sections[0]["syms"]
        # 上旬止损后再选股：新票单独成组，图上标「止损后再选」，不混进原板块
        if repick_sym and not any(repick_sym in s["syms"] for s in sections):
            sections.append({"sec": "止损后再选", "sr": 1, "syms": [repick_sym]})

        bought_sec = str(r["板块"]) if pd.notna(r["板块"]) else ""
        # 止损后没再选股 = 当期剩余时间空仓，不能把原票标成「买入」（它已经被卖掉了）
        picked_set = (set() if (stop_s and not repick_sym)
                      else {picked} | ({repick_sym} if repick_sym else set()))
        # 再选股那组以「再选日」建仓，不能用本期的原建仓日（否则买入点、持有收益全偏）
        repick_entry, repick_i = entry, entry_i
        if repick_s:
            _ri = int(dates.searchsorted(pd.Timestamp(repick_s.split(" ")[0]),
                                         side="right")) - 1
            if 0 <= _ri < len(dates):
                repick_i, repick_entry = _ri, dates[_ri]

        series, etf_hold, seen = [], None, set()
        for sc in sections:
            for sym in sc["syms"][:3]:
                if sym in seen:
                    continue
                # 按代码本身判断，不能靠板块名——再选票可能正好也是候选之一
                is_rp_sym = bool(repick_sym) and sym == repick_sym
                e_use = repick_entry if is_rp_sym else entry
                i_use = repick_i if is_rp_sym else entry_i
                se = series_of(sym, dates, e_use)
                if se:
                    seen.add(sym)
                    se.update({"pick": sym in picked_set, "etf": False,
                               "sec": sc["sec"], "sr": sc["sr"], "entryIdx": i_use})
                    series.append(se)
            # 每个板块都挂一张自己的板块 ETF 做对照
            se = series_of(sc["sec"], dates, entry)
            if se and sc["sec"] not in seen:
                se.update({"pick": False, "etf": True,
                           "sec": sc["sec"], "sr": sc["sr"], "entryIdx": entry_i})
                if sc["sec"] == bought_sec:
                    etf_hold = se["holdRet"]
                series.append(se)

        periods.append({
            "i": i + 1,
            "year": int(sig.year),
            "sig": sig.strftime("%Y-%m-%d"),
            "from": r["持有期起"].strftime("%Y-%m-%d"),
            "to": e.strftime("%Y-%m-%d"),
            "sector": bought_sec or "-",
            "stop": stop_s,
            "stopKind": stop_kind,
            "repick": repick_s,
            "repickSym": repick_sym,
            "stopIdx": stop_idx,
            "gates": gates,
            "pick": pick_sym,
            "bull": bool(int(r["牛市"]) == 1),
            "etfOnly": bool(pd.notna(r["个股"]) and str(r["个股"]).startswith("ETF:")),
            "cash": bool(pd.isna(r["个股"])),
            "ret": float(r["个股收益"]),
            "rsrs": None if pd.isna(r["板块RSRS"]) else float(r["板块RSRS"]),
            "slopeRank": None if pd.isna(r["斜率序"]) else int(r["斜率序"]),
            "pickRank": (int(r["板块RSRS序"]) if "板块RSRS序" in holds.columns
                         and pd.notna(r["板块RSRS序"]) else None),
            "refill": (pd.Timestamp(r["补选日"]).strftime("%Y-%m-%d")
                       if "补选日" in holds.columns and pd.notna(r["补选日"]) else ""),
            "cand": 0 if pd.isna(r["同板块候选"]) else int(r["同板块候选"]),
            "tried": 0 if pd.isna(r["候选尝试"]) else int(r["候选尝试"]),
            "holdRet": etf_hold,
            "nsec": len(sections),
            "nsym": sum(len(s["syms"]) for s in sections),
            "dates": [x.strftime("%Y-%m-%d") for x in dates],
            "sigIdx": sig_i,
            "holdFrom": hold_i,
            "entryIdx": entry_i,
            "series": series,
        })

    print(f"成图 {len(periods)} 期，共 {sum(len(p['series']) for p in periods)} 张日 K")
    title = args.title or "RSRS 逐期板块前三候选日 K"
    bbd_note = (
        f'<span style="color:#ef5b6b"><b>红色阴影带 = 禁买区</b></span>：上轨往下 '
        f'{args.bb_delta:g} 倍标准差（即 z &gt; {boll_k - args.bb_delta:g}）。'
        f'买入点的 z 是按当日 MA20 和标准差实算的：'
        f'<b>落在红带内的会标红并加 ⚠</b>，说明这笔就是贴轨建仓。<br>'
        if args.bb_delta > 0 else "")
    html = (TEMPLATE
            .replace("__TITLE__", title)
            .replace("__RANGE__", f"{holds['信号日'].min().date()} ~ {holds['持有期止'].max().date()}"
                                  f"（{len(holds)} 期）")
            .replace("__PRE__", str(args.pre))
            .replace("__BOLL__", f"MA{boll_n}" if boll_n else "MA20")
            .replace("__BK__", f"{boll_k:g}")
            .replace("__BBDNOTE__", bbd_note)
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__DIR__", d.name)
            .replace("__DATA__", json.dumps({"meta": {"dir": d.name, "pre": args.pre,
                                                      "boll": [boll_n, boll_k],
                                                      "bbdelta": float(args.bb_delta)},
                                             "periods": periods}, ensure_ascii=False)))

    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"产出：{out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
