"""股池数据看板：原始数据 / 量化数据分开列出覆盖率、缺口与逐票明细。

为什么要这个：
    池子建好之后最容易出的事不是「算错」，而是「某只票某个字段根本没数据」——
    股本缺 → 换手率算不出来 → turn20 全 NaN → 排序被静默跳过。这些只能靠覆盖率查，
    不可能靠翻 parquet 发现。

三层口径（越往下越接近策略实际用法）：
    逐日成分（point-in-time）
        每个交易日只看当天在指数里的那 ~503 只，逐日验收 K线 / 股本换手 / 入闸四指标。
        596 只是 2021 年以来的历史并集（有进有出），拿它当分母会永远补不齐。
    原始数据（OHLC / 成交量 / 股本 / 成交额 / 市值 / 换手率）
        价格与成交量在全部行都有；股本/市值/换手率只有 CRSP 行才有
        （外部补来的预热段没有股本，这是源的限制，不是 bug）。
    量化数据（MA/斜率/ADX/DI/turn20/turn120/turn_score/波动率/动量）
        按各证券自己的交易日序列滚动计算，预热不足的前 N 根天然是 NaN，
        所以看的是「首个有效日」和「指标齐全日」，不只看覆盖率。

产出：
    data/curated/sp500_pool/features.parquet      量化面板（security_id × date 长表）
    data/curated/sp500_pool/field_coverage.csv    逐字段覆盖率
    data/curated/sp500_pool/coverage_by_year.csv  字段 × 年份覆盖率
    reports/data_audit/pool_data_board.html       可视化看板（自包含，双击即开）

用法：
    $env:PYTHONPATH='src'; python -m scripts.build_pool_data_board
    $env:PYTHONPATH='src'; python -m scripts.build_pool_data_board --open
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant.data import pool  # noqa: E402
from scripts.sector_stock_rsrs import dmi_panels  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "curated" / "sp500_pool"
HTML = ROOT / "reports" / "data_audit" / "pool_data_board.html"

# 原始字段：(键, 中文名, 源列, 数据范围说明, 分母口径 all=全部行 / crsp=只有 CRSP 行)
RAW_FIELDS = [
    ("open", "开盘价（复权）", "adj_open", "全部行", "all"),
    ("high", "最高价（复权）", "adj_high", "全部行", "all"),
    ("low", "最低价（复权）", "adj_low", "全部行", "all"),
    ("adj_close", "收盘价（复权）", "adj_close", "全部行", "all"),
    ("close", "收盘价（原始，拆股日有台阶）", "close", "全部行", "all"),
    ("volume", "成交量（股）", "volume", "全部行", "all"),
    ("amount", "成交额（美元，收盘×成交量估算）", "amount", "全部行", "all"),
    ("shares", "总股本（股）", "shares", "仅 CRSP 行", "crsp"),
    ("mcap", "总市值（美元）", "mcap", "仅 CRSP 行", "crsp"),
    ("turnover", "换手率（成交量÷总股本）", "turnover", "仅 CRSP 行", "crsp"),
]

# 量化字段：(键, 中文名, 预热窗口说明)
QUANT_FIELDS = [
    ("ma20", "MA20", "20 根"),
    ("ma50", "MA50", "50 根"),
    ("ma60", "MA60", "60 根"),
    ("ma200", "MA200", "200 根"),
    ("slope_ma20", "MA20 斜率（5 日差）", "25 根"),
    ("slope_ma60", "MA60 斜率（5 日差）", "65 根"),
    ("adx14", "ADX(14)", "28 根"),
    ("pdi14", "+DI(14)", "14 根"),
    ("mdi14", "-DI(14)", "14 根"),
    ("turn20", "turn20（20 日均换手）", "20 根"),
    ("turn120", "turn120（120 日均换手）", "120 根"),
    ("turn_score", "turn20²/turn120", "120 根"),
    ("vol20", "20 日波动率", "21 根"),
    ("mom63", "63 日动量", "64 根"),
    ("mom126", "126 日动量", "127 根"),
]


def wide(b: pd.DataFrame, col: str) -> pd.DataFrame:
    return b.pivot_table(index="date", columns="security_id", values=col, aggfunc="last")


def raw_panels(b: pd.DataFrame) -> dict[str, pd.DataFrame]:
    b = b.copy()
    b["amount"] = b["close"] * b["volume"]
    return {k: wide(b, col) for k, _, col, _, _ in RAW_FIELDS}


def _features_one(g: pd.DataFrame) -> pd.DataFrame:
    """单只证券：按**它自己的交易日序列**算全部指标（g 需按 date 升序）。

    不能在全池日历的宽表上 rolling：那是以「全池交易日并集」为步长，
    该票停牌/未上市的日子是一根 NaN，rolling 会把这条 NaN 一路传染下去——
    停一天，后面 200 根的 MA200 全报废。按自己的序列滚，缺的那天只是少一根。
    """
    px = g["adj_close"].astype(float)
    hi = g["adj_high"].astype(float)
    lo = g["adj_low"].astype(float)
    turn = g["turnover"].astype(float)
    d = pd.DataFrame(index=g.index)
    for w in (20, 50, 60, 200):
        d[f"ma{w}"] = px.rolling(w, min_periods=w).mean()
    d["slope_ma20"] = d["ma20"] - d["ma20"].shift(5)
    d["slope_ma60"] = d["ma60"] - d["ma60"].shift(5)
    one = "x"
    adx, pdi, mdi = dmi_panels(hi.to_frame(one), lo.to_frame(one), px.to_frame(one), 14)
    # Wilder 平滑用的是 ewm(adjust=False)，NaN 会被跳过并把上一个值带下去，
    # 于是「根本没这根 K 线」的日子也会算出 ADX——按收盘价的存在与否掩掉
    live = px.notna()
    d["adx14"] = adx[one].where(live)
    d["pdi14"] = pdi[one].where(live)
    d["mdi14"] = mdi[one].where(live)
    d["turn20"] = turn.rolling(20, min_periods=15).mean()
    d["turn120"] = turn.rolling(120, min_periods=60).mean()
    d["turn_score"] = d["turn20"] ** 2 / d["turn120"].where(d["turn120"] > 0)
    ret = px.pct_change(fill_method=None)
    d["vol20"] = ret.rolling(20, min_periods=15).std()
    d["mom63"] = px.pct_change(63, fill_method=None)
    d["mom126"] = px.pct_change(126, fill_method=None)
    return d


def build_features(b: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """量化面板：逐票按自己的交易日序列算，再摊回 index=date / columns=security_id。"""
    keys = [k for k, *_ in QUANT_FIELDS]
    parts = []
    for sid, g in b.sort_values(["security_id", "date"]).groupby("security_id", sort=False):
        d = _features_one(g)
        d["security_id"] = sid
        d["date"] = g["date"].to_numpy()
        parts.append(d)
    long = pd.concat(parts, ignore_index=True)
    return {k: long.pivot(index="date", columns="security_id", values=k) for k in keys}


def field_table(panels: dict[str, pd.DataFrame], labels: dict[str, str],
                denom_all: pd.Series, denom_crsp: pd.Series,
                crsp_only: set[str]) -> pd.DataFrame:
    """逐字段：非空行数、覆盖率、有值证券、首个有效日、预热消耗中位。"""
    rows = []
    for key, p in panels.items():
        den = denom_crsp if key in crsp_only else denom_all
        nn = p.notna().sum(axis=1)
        tot = den.reindex(p.index).fillna(0)
        cov = float(nn.sum() / tot.sum()) if tot.sum() else np.nan
        first = p.apply(lambda s: s.first_valid_index()).dropna()
        warm = []
        for sid in p.columns:
            s = p[sid].dropna()
            if len(s):
                warm.append(int((p.index < s.index[0]).sum()))
        rows.append({
            "字段": key, "含义": labels.get(key, key),
            "非空行数": int(p.notna().sum().sum()),
            "应有行数": int(tot.sum()),
            "覆盖率": cov,
            "有值证券": int((p.notna().sum() > 0).sum()),
            "全池首个有效日": str(first.min().date()) if len(first) else "—",
            "预热消耗中位(根)": int(np.median(warm)) if warm else 0,
        })
    return pd.DataFrame(rows)


def coverage_by_year(panels: dict[str, pd.DataFrame], denom: pd.Series) -> pd.DataFrame:
    out = {}
    for key, p in panels.items():
        s = p.notna().sum(axis=1) / denom.reindex(p.index).replace(0, np.nan)
        out[key] = s.groupby(s.index.year).mean()
    return pd.DataFrame(out).T.round(4)


def per_security(b: pd.DataFrame, panels: dict[str, pd.DataFrame],
                 u: pd.DataFrame) -> pd.DataFrame:
    """逐票：行数、CRSP/外部分布、预热、指标齐全日、分层缺失天数。"""
    meta = u.set_index("security_id")
    idx_all = panels["adj_close"].index
    quant_keys = [k for k, *_ in QUANT_FIELDS]
    rows = []
    for sid, g in b.groupby("security_id", sort=False):
        n_all, n_crsp = len(g), int((g["src"] == "crsp").sum())
        # 策略真正会读的字段：价格、量、股本、换手 + 四个入闸指标
        miss_raw = sum(int(panels[k][sid].reindex(g["date"]).isna().sum())
                       for k in ("adj_close", "volume", "shares", "turnover")
                       if sid in panels[k].columns)
        miss_q = sum(int(panels[k][sid].reindex(g["date"]).isna().sum())
                     for k in quant_keys if sid in panels[k].columns)
        ready = None
        for k in ("ma200", "adx14", "turn20", "turn120"):
            col = panels[k].get(sid)
            if col is None:
                ready = None
                break
            ok = col.dropna().index
            ready = ok if ready is None else ready.intersection(ok)
        first_ready = ready.min() if ready is not None and len(ready) else None
        rows.append({
            "security_id": sid,
            "ticker": meta.at[sid, "ticker"] if sid in meta.index else "",
            "sector": meta.at[sid, "sector_etf"] if sid in meta.index else "",
            "exit_type": meta.at[sid, "exit_type"] if sid in meta.index else "",
            "first": str(g["date"].min().date()), "last": str(g["date"].max().date()),
            "rows": n_all, "crsp_rows": n_crsp, "ext_rows": n_all - n_crsp,
            "warmup_days": int(meta.at[sid, "warmup_days"]) if sid in meta.index else 0,
            "ready_date": str(first_ready.date()) if first_ready is not None else "",
            # 从该票自己第一根 K 线算起，攒够多少根指标才齐（不是从池子起点算）
            "ready_bars": (int((g["date"] <= first_ready).sum())
                           if first_ready is not None else 0),
            "miss_raw": miss_raw, "miss_quant": miss_q,
        })
    df = pd.DataFrame(rows)
    buy = df["first"].map(lambda s: max(pd.Timestamp(s), pd.Timestamp("2021-01-04")))
    df["usable_buy"] = df["ready_date"].map(lambda s: bool(s)) & (
        df["ready_date"].replace("", "9999-12-31") <= buy.astype(str))
    return df.sort_values(["usable_buy", "miss_quant"], ascending=[False, True])


GATE_FIELDS = ("ma200", "adx14", "turn20", "turn120")


def daily_membership_check(b: pd.DataFrame, panels: dict[str, pd.DataFrame],
                           u: pd.DataFrame, start: str = "2021-01-04") -> dict:
    """point-in-time 口径：只看**当天真在指数里**的那些票，数据齐不齐。

    为什么不能拿 596 当分母：596 是 2021 年以来的历史并集（有进有出），
    任何一天实际只有 ~503 只在指数内（GOOG/GOOGL、FOX/FOXA、NWS/NWSA 这类多类别股算多只证券）。
    策略只买当天成分股，所以验收必须逐日取名单；拿并集当分母，会把「这只票还没进指数 / 已经退出」
    的日子也算成缺口，看起来永远补不齐。
    """
    cal = panels["adj_close"].index
    cal = cal[cal >= pd.Timestamp(start)]
    ids = u["security_id"].tolist()
    M = pool.membership_mask(cal, ids).to_numpy()

    def mat(keys: tuple[str, ...]) -> np.ndarray:
        m = np.ones((len(cal), len(ids)), dtype=bool)
        for k in keys:
            m &= panels[k].reindex(index=cal, columns=ids).notna().to_numpy()
        return m

    kline = mat(("adj_close", "volume"))
    share = mat(("shares", "turnover"))
    gate = mat(GATE_FIELDS)
    daily = pd.DataFrame({
        "date": cal,
        "mbr": M.sum(1),
        "kline": (M & kline).sum(1),
        "share": (M & share).sum(1),
        "gate": (M & gate).sum(1),
    })
    for c in ("kline", "share", "gate"):
        daily[c + "_rate"] = daily[c] / daily["mbr"]

    # 缺口明细：逐票找出「在成分期内但四指标不齐」的连续交易日段
    meta = u.set_index("security_id")
    seq = {sid: g["date"].to_numpy() for sid, g in b.groupby("security_id")}
    ext = b[b["src"] == "external"].groupby("security_id").size()
    miss_mat = {k: ~panels[k].reindex(index=cal, columns=ids).notna().to_numpy()
                for k in GATE_FIELDS}
    gaps = []
    for j, sid in enumerate(ids):
        inm = M[:, j]
        if not inm.any():
            continue
        bad = inm & ~gate[:, j]
        if not bad.any():
            continue
        miss = {k: int((inm & miss_mat[k][:, j]).sum()) for k in GATE_FIELDS}
        bars_before = int(np.searchsorted(
            seq.get(sid, np.array([], dtype="datetime64[ns]")),
            np.datetime64(cal[inm][0]), side="right"))
        why = []
        if miss["ma200"] and bars_before < 200:
            why.append(f"上市/历史不足200根（纳入时仅{bars_before}根）")
        if miss["turn120"] and int(ext.get(sid, 0)) > 0:
            why.append("外部预热段无股本→turn120攒不够120根")
        if not why:
            why.append("K线断档")
        pos = np.flatnonzero(bad)
        for seg in np.split(pos, np.flatnonzero(np.diff(pos) > 1) + 1):
            gaps.append({
                "security_id": sid,
                "ticker": str(meta.at[sid, "ticker"]) if sid in meta.index else "",
                "start": str(cal[seg[0]].date()), "end": str(cal[seg[-1]].date()),
                "days": int(len(seg)),
                "fields": ",".join(k for k in GATE_FIELDS if miss[k] > 0),
                "why": " / ".join(why),
            })
    gaps = (pd.DataFrame(gaps).sort_values(["days", "start"], ascending=[False, True])
            if gaps else pd.DataFrame(columns=["security_id", "ticker", "start", "end",
                                               "days", "fields", "why"]))

    g = daily.set_index("date")
    mon = g.groupby(g.index.to_period("M")).agg(
        days=("mbr", "size"), mbr=("mbr", "sum"), kline=("kline", "sum"),
        share=("share", "sum"), gate=("gate", "sum"))
    mon["gate_rate"] = mon["gate"] / mon["mbr"]
    bym = g.groupby(g.index.to_period("M"))
    mon["min_rate"] = bym["gate_rate"].min()
    mon["worst_day"] = bym["gate_rate"].idxmin().dt.strftime("%Y-%m-%d").values
    mon["miss_days"] = bym.apply(lambda x: int((x["gate"] < x["mbr"]).sum())).values
    mon = mon.reset_index().rename(columns={"date": "month"})
    mon["month"] = mon["month"].astype(str)
    return {"daily": daily, "monthly": mon, "gaps": gaps}


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>标普500 股池数据看板</title>
<style>
  :root{--bg:#0f172a;--panel:#16213b;--panel2:#1c2a48;--line:#2a3a5e;--txt:#e6edf7;
        --dim:#93a4c0;--blue:#4f8ef7;--green:#3ec98a;--amber:#f0a63c;--red:#ef5b6b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}
  .wrap{max-width:1280px;margin:0 auto;padding:26px 20px 60px}
  h1{font-size:22px;margin:0 0 4px}
  h2{font-size:16px;margin:30px 0 10px;padding-left:10px;border-left:3px solid var(--blue)}
  .sub{color:var(--dim);font-size:12.5px}
  .kpis{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-top:14px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 14px}
  .kpi .k{color:var(--dim);font-size:12px}
  .kpi .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
  .kpi .h{color:var(--dim);font-size:11.5px}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
  th,td{padding:6px 9px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--dim);font-weight:500;font-size:12px;position:sticky;top:0;
     background:var(--panel2);cursor:pointer;user-select:none}
  th:hover{color:var(--txt)}
  td:first-child,th:first-child,td.l,th.l{text-align:left}
  tbody tr:hover{background:rgba(79,142,247,.07)}
  .scroll{max-height:620px;overflow:auto;border:1px solid var(--line);border-radius:10px}
  .ok{color:var(--green)} .warn{color:var(--amber)} .bad{color:var(--red)} .dim{color:var(--dim)}
  .bar{display:inline-block;height:8px;border-radius:4px;vertical-align:middle;margin-right:6px}
  .tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11.5px;
       border:1px solid var(--line)}
  .ctl{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}
  input[type=search],select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
       border-radius:6px;padding:5px 9px;font-size:12.5px}
  label{font-size:12.5px;color:var(--dim);cursor:pointer;user-select:none}
  .foot{margin-top:30px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);
        padding-top:12px}
</style></head><body><div class="wrap">
<h1>标普 500 股池数据看板</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>

<h2>每日成分股齐全度（point-in-time 口径，策略真正用的就是这个）</h2>
<div class="sub">596 只是 2021 年以来的<b>历史并集</b>——有进有出，任何一天真正在指数里的只有 ~503 只。
多出来的几只来自 GOOG/GOOGL、FOX/FOXA、NWS/NWSA 这类多类别股：标普 500 的「成分证券数」本来就不等于 500。
下表按<b>当天成分名单</b>逐日验收（按月汇总）：K线 = 当日有 adj_close+volume；
股本/换手 = shares+turnover（外部预热段没有股本，但换手率已由东财日线补上）；
四指标 = MA200+ADX14+turn20+turn120 同时非空。带「缺口」的月份看最差日与缺口天数。</div>
<div class="scroll" style="margin-top:10px"><table id="t-daily"></table></div>

<h2>成分期内指标缺口（逐票逐段）</h2>
<div class="ctl">
  <input type="search" id="gq" placeholder="搜索 ticker / security_id / 原因" size="30">
  <span class="dim" id="gcnt"></span>
</div>
<div class="scroll"><table id="t-gaps"></table></div>

<h2>原始数据（价格 / 成交量 / 股本 / 成交额 / 市值 / 换手率）</h2>
<div class="sub">分母口径：价格与成交量 = 全部行（含外部补的预热段）；股本 / 市值 / 换手率 = 只有 CRSP 行才有。</div>
<div class="scroll" style="margin-top:10px"><table id="t-raw"></table></div>

<h2>量化数据（均线 / ADX / 换手 / 波动 / 动量）</h2>
<div class="sub">按各证券<b>自己的</b>交易日序列滚动（停牌一天只少一根，不会把后面 200 根 MA 全污染），
预热不足的前 N 根是 NaN。注意这里的覆盖率是「全池并集」口径（分母含该票未上市/已退出的日子），
不等于策略可用率——策略口径看最上面的逐日成分表。
turn20 / turn120 依赖换手率，换手率要股本，而股本只有 CRSP 行（成分期内）才有：
后纳入指数的票在纳入后的 60~120 根内算不出 turn120，这是源的口径限制。</div>
<div class="scroll" style="margin-top:10px"><table id="t-quant"></table></div>

<h2>字段 × 年份 覆盖率</h2>
<div class="sub">分母同样是全池并集行数，用于定位「某一年整段缺数据」（源断档、股本缺失、预热没到位）。</div>
<div class="scroll" style="margin-top:10px"><table id="t-year"></table></div>

<h2>逐票明细</h2>
<div class="ctl">
  <input type="search" id="q" placeholder="搜索 ticker / security_id" size="24">
  <select id="f-sector"><option value="">全部板块</option></select>
  <select id="f-exit"><option value="">全部状态</option></select>
  <label><input type="checkbox" id="f-bad"> 只看指标不齐 / 有缺失</label>
  <span class="dim" id="cnt"></span>
</div>
<div class="scroll"><table id="t-sec"></table></div>

<div class="foot" id="foot"></div>
</div>
<script id="DATA" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('DATA').textContent);
const fmtPct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
const covColor = v => v == null ? 'dim' : (v >= 0.99 ? 'ok' : v >= 0.6 ? 'warn' : 'bad');
const barW = v => v == null ? 0 : Math.max(2, Math.round(v * 90));

document.getElementById('sub').textContent =
  '池内 ' + D.meta.secs + ' 只 ｜ ' + D.meta.first + ' ~ ' + D.meta.last + ' ｜ '
  + D.meta.rows.toLocaleString() + ' 行（CRSP ' + D.meta.crsp_rows.toLocaleString()
  + ' / 外部预热 ' + D.meta.ext_rows.toLocaleString() + '）｜ 生成于 ' + D.meta.built;

const kpi = [
  ['每日成分股（中位）', D.meta.mbr_median, '任何一天真在指数里的只数（不是 500）'],
  ['成分股指标齐全率', fmtPct(D.meta.gate_rate), '当天成分股 MA200+ADX+turn20/120 齐'],
  ['成分股K线齐全率', fmtPct(D.meta.kline_rate), '当日有复权 OHLC + 成交量'],
  ['成分股缺口行数', D.meta.gate_miss.toLocaleString(), '成分期内四指标不齐的 证券×日'],
  ['池内证券（历史并集）', D.meta.secs, '2021 以来当过成分股的全部证券'],
  ['交易日', D.meta.days, D.meta.first + ' 起'],
];
document.getElementById('kpis').innerHTML = kpi.map(function (r) {
  return '<div class="kpi"><div class="k">' + r[0] + '</div><div class="v">' + r[1]
    + '</div><div class="h">' + r[2] + '</div></div>';
}).join('');

function fieldTable(el, rows) {
  const head = ['字段', '含义', '非空行数', '应有行数', '覆盖率', '有值证券', '全池首个有效日', '预热消耗中位(根)'];
  el.innerHTML = '<thead><tr>' + head.map(function (h) {
    return '<th class="' + (h === '字段' || h === '含义' ? 'l' : '') + '">' + h + '</th>';
  }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
    const cov = r['覆盖率'];
    return '<tr><td class="l"><code>' + r['字段'] + '</code></td>'
      + '<td class="l">' + r['含义'] + '</td>'
      + '<td>' + (r['非空行数'] || 0).toLocaleString() + '</td>'
      + '<td>' + (r['应有行数'] || 0).toLocaleString() + '</td>'
      + '<td class="' + covColor(cov) + '"><span class="bar" style="width:' + barW(cov)
      + 'px;background:currentColor"></span>' + fmtPct(cov) + '</td>'
      + '<td>' + r['有值证券'] + '</td><td class="l">' + r['全池首个有效日'] + '</td>'
      + '<td>' + r['预热消耗中位(根)'] + '</td></tr>';
  }).join('') + '</tbody>';
}
fieldTable(document.getElementById('t-raw'), D.raw);
fieldTable(document.getElementById('t-quant'), D.quant);

(function yearsTable() {
  const cols = D.years.cols, rows = D.years.rows, el = document.getElementById('t-year');
  el.innerHTML = '<thead><tr><th class="l">字段</th>' + cols.map(function (c) {
    return '<th>' + c + '</th>';
  }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
    return '<tr><td class="l"><code>' + r['字段'] + '</code></td>' + cols.map(function (c) {
      const v = r[c];
      return '<td class="' + covColor(v) + '">' + (v == null ? '—' : (v * 100).toFixed(0) + '%') + '</td>';
    }).join('') + '</tr>';
  }).join('') + '</tbody>';
})();

const SEC_HEAD = [
  ['security_id', 'ID', 'l'], ['ticker', 'Ticker', 'l'], ['sector', '板块', 'l'],
  ['exit_type', '状态', 'l'], ['first', '首日', 'l'], ['last', '末日', 'l'],
  ['rows', '行数', ''], ['crsp_rows', 'CRSP', ''], ['ext_rows', '外部', ''],
  ['warmup_days', '预热', ''], ['ready_date', '指标齐全日', 'l'],
  ['ready_bars', '齐全后K线', ''], ['miss_raw', '原始缺', ''], ['miss_quant', '量化缺', ''],
];
const selSector = document.getElementById('f-sector');
const selExit = document.getElementById('f-exit');
Array.from(new Set(D.secs.map(function (r) { return r.sector; }))).sort().forEach(function (v) {
  selSector.insertAdjacentHTML('beforeend', '<option>' + v + '</option>');
});
Array.from(new Set(D.secs.map(function (r) { return r.exit_type; }))).sort().forEach(function (v) {
  selExit.insertAdjacentHTML('beforeend', '<option>' + v + '</option>');
});

let sortKey = 'miss_quant', sortDir = 1;
function renderSec() {
  const q = document.getElementById('q').value.trim().toUpperCase();
  const fs = selSector.value, fe = selExit.value;
  const badOnly = document.getElementById('f-bad').checked;
  const rows = D.secs.filter(function (r) {
    const hit = !q || (r.ticker || '').toUpperCase().indexOf(q) >= 0 || r.security_id.indexOf(q) >= 0;
    return hit && (!fs || r.sector === fs) && (!fe || r.exit_type === fe)
      && (!badOnly || !r.usable_buy || r.miss_raw > 0 || r.miss_quant > 0);
  });
  rows.sort(function (a, b) {
    const x = a[sortKey], y = b[sortKey];
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * sortDir;
    return String(x).localeCompare(String(y)) * sortDir;
  });
  const el = document.getElementById('t-sec');
  el.innerHTML = '<thead><tr>' + SEC_HEAD.map(function (h) {
    return '<th class="' + h[2] + '" data-k="' + h[0] + '">' + h[1]
      + (sortKey === h[0] ? (sortDir > 0 ? ' ▲' : ' ▼') : '') + '</th>';
  }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
    const badge = r.exit_type === 'active' ? 'ok' : (r.exit_type === 'delisted' ? 'bad' : 'dim');
    const warm = r.warmup_days >= 200 ? 'ok' : (r.warmup_days > 0 ? 'warn' : 'bad');
    return '<tr><td class="l">' + r.security_id + '</td>'
      + '<td class="l"><b>' + (r.ticker || '—') + '</b></td>'
      + '<td class="l">' + (r.sector || '—') + '</td>'
      + '<td class="l"><span class="tag ' + badge + '">' + r.exit_type + '</span></td>'
      + '<td class="l dim">' + r.first + '</td><td class="l dim">' + r.last + '</td>'
      + '<td>' + r.rows + '</td><td>' + r.crsp_rows + '</td><td>' + (r.ext_rows || '—') + '</td>'
      + '<td class="' + warm + '">' + r.warmup_days + '</td>'
      + '<td class="l">' + (r.ready_date || '<span class="bad">未齐</span>') + '</td>'
      + '<td>' + (r.ready_bars || '—') + '</td>'
      + '<td class="' + (r.miss_raw > 0 ? 'warn' : 'dim') + '">' + r.miss_raw + '</td>'
      + '<td class="' + (r.miss_quant > 0 ? 'warn' : 'dim') + '">' + r.miss_quant + '</td></tr>';
  }).join('') + '</tbody>';
  el.querySelectorAll('th').forEach(function (th) {
    th.onclick = function () {
      const k = th.dataset.k;
      if (k === sortKey) sortDir = -sortDir; else { sortKey = k; sortDir = 1; }
      renderSec();
    };
  });
  document.getElementById('cnt').textContent = rows.length + ' / ' + D.secs.length + ' 只';
}
['q', 'f-sector', 'f-exit', 'f-bad'].forEach(function (id) {
  const e = document.getElementById(id);
  e.addEventListener(id === 'q' ? 'input' : 'change', renderSec);
});
renderSec();

(function dailyTable() {
  const HEAD = [['month', '月份', 'l'], ['days', '交易日', ''], ['mbr', '成分股行数', ''],
    ['kline', 'K线齐', ''], ['share', '股本换手齐', ''], ['gate', '四指标齐', ''],
    ['gate_rate', '齐全率', ''], ['min_rate', '当月最低', ''],
    ['worst_day', '最差日', 'l'], ['miss_days', '有缺口天数', '']];
  const el = document.getElementById('t-daily');
  el.innerHTML = '<thead><tr>' + HEAD.map(function (h) {
    return '<th class="' + h[2] + '">' + h[1] + '</th>';
  }).join('') + '</tr></thead><tbody>' + D.daily.rows.map(function (r) {
    return '<tr><td class="l">' + r.month + '</td><td>' + r.days + '</td>'
      + '<td>' + r.mbr.toLocaleString() + '</td>'
      + '<td>' + r.kline.toLocaleString() + '</td>'
      + '<td class="' + (r.share === r.mbr ? 'ok' : 'warn') + '">' + r.share.toLocaleString() + '</td>'
      + '<td class="' + (r.gate === r.mbr ? 'ok' : 'warn') + '">' + r.gate.toLocaleString() + '</td>'
      + '<td class="' + covColor(r.gate_rate) + '"><span class="bar" style="width:'
      + barW(r.gate_rate) + 'px;background:currentColor"></span>' + fmtPct(r.gate_rate) + '</td>'
      + '<td class="' + covColor(r.min_rate) + '">' + fmtPct(r.min_rate) + '</td>'
      + '<td class="l dim">' + (r.worst_day || '—') + '</td>'
      + '<td class="' + (r.miss_days > 0 ? 'warn' : 'ok') + '">' + r.miss_days + '</td></tr>';
  }).join('') + '</tbody>';
})();

(function gapsTable() {
  const HEAD = [['ticker', 'Ticker', 'l'], ['security_id', 'ID', 'l'],
    ['start', '起', 'l'], ['end', '止', 'l'], ['days', '交易日', ''],
    ['fields', '缺的字段', 'l'], ['why', '原因', 'l']];
  const el = document.getElementById('t-gaps'), q = document.getElementById('gq');
  function render() {
    const s = q.value.trim().toUpperCase();
    const rows = D.gaps.filter(function (r) {
      return !s || ((r.ticker || '') + ' ' + r.security_id + ' ' + r.why).toUpperCase().indexOf(s) >= 0;
    });
    document.getElementById('gcnt').textContent =
      rows.length + ' / ' + D.gaps.length + ' 段（按连续交易日切分）';
    el.innerHTML = '<thead><tr>' + HEAD.map(function (h) {
      return '<th class="' + h[2] + '">' + h[1] + '</th>';
    }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
      return '<tr><td class="l"><b>' + (r.ticker || '—') + '</b></td>'
        + '<td class="l dim">' + r.security_id + '</td>'
        + '<td class="l">' + r.start + '</td><td class="l">' + r.end + '</td>'
        + '<td class="' + (r.days > 200 ? 'bad' : 'warn') + '">' + r.days + '</td>'
        + '<td class="l"><code>' + r.fields + '</code></td>'
        + '<td class="l dim">' + r.why + '</td></tr>';
    }).join('') + '</tbody>';
  }
  q.addEventListener('input', render);
  render();
})();

document.getElementById('foot').innerHTML =
  '数据源：CRSP 标普500 成分文件（src=crsp）+ 外部补的预热段（src=external，对齐比值中位 ≈ 1.0000）。'
  + '<br>覆盖率为空表示该字段没有任何有效值；「预热消耗」= 从该票第一根 K 线到第一个有效值之间的根数。';
</script>
</body></html>"""


def render_html(meta: dict, fields_raw: pd.DataFrame, fields_q: pd.DataFrame,
                years: pd.DataFrame, secs: pd.DataFrame, daily: dict) -> str:
    def jf(x):
        if isinstance(x, (bool, np.bool_)):
            return bool(x)
        if isinstance(x, (int, np.integer)):
            return int(x)
        if isinstance(x, (float, np.floating)):
            return None if not np.isfinite(x) else round(float(x), 4)
        return x

    data = {
        "meta": meta,
        "raw": [{k: jf(v) for k, v in r.items()} for r in fields_raw.to_dict("records")],
        "quant": [{k: jf(v) for k, v in r.items()} for r in fields_q.to_dict("records")],
        "years": {"cols": [str(c) for c in years.columns],
                  "rows": [{"字段": k, **{str(c): jf(v) for c, v in row.items()}}
                           for k, row in years.iterrows()]},
        "secs": [{k: jf(v) for k, v in r.items()} for r in secs.to_dict("records")],
        "daily": {"rows": [{k: jf(v) for k, v in r.items()}
                           for r in daily["monthly"].to_dict("records")]},
        "gaps": [{k: jf(v) for k, v in r.items()} for r in daily["gaps"].to_dict("records")],
    }
    blob = json.dumps(data, ensure_ascii=False, default=str)
    return TEMPLATE.replace("__DATA__", blob)


def main() -> int:
    ap = argparse.ArgumentParser(description="股池数据看板（原始 / 量化分层）")
    ap.add_argument("--out", default=str(HTML))
    ap.add_argument("--open", action="store_true", help="生成后直接用浏览器打开")
    args = ap.parse_args()

    u, b = pool.universe(), pool.bars()
    b = b.sort_values(["security_id", "date"])
    print(f"池：{len(u)} 只 / {len(b):,} 行（CRSP {int((b['src'] == 'crsp').sum()):,} / "
          f"external {int((b['src'] == 'external').sum()):,}）")

    rp = raw_panels(b)
    qf = build_features(b)
    qp = {k: qf[k] for k, *_ in QUANT_FIELDS}
    panels = {**rp, **qp}
    denom_all = b.groupby("date").size()
    denom_crsp = b[b["src"] == "crsp"].groupby("date").size()

    fr = field_table(rp, {k: lab for k, lab, *_ in RAW_FIELDS}, denom_all, denom_crsp,
                     crsp_only={k for k, _, _, _, d in RAW_FIELDS if d == "crsp"})
    fq = field_table(qp, {k: lab for k, lab, _ in QUANT_FIELDS}, denom_all, denom_crsp, set())
    years = coverage_by_year(panels, denom_all)
    secs = per_security(b, panels, u)
    # point-in-time：逐日按当天成分名单验收（596 是历史并集，单日只有 ~503 只）
    dchk = daily_membership_check(b, panels, u)
    dd = dchk["daily"]

    # 量化面板落盘（长表 security_id × date）
    # 以 bars 的实际 (date, security_id) 为行键，而不是 stack() 后的结果：
    # stack 默认丢 NaN，预热早期（15 个字段全空）的整行会被吃掉，导致落盘行数 < bars 行数
    idx = pd.MultiIndex.from_frame(b[["date", "security_id"]])
    long = pd.DataFrame(index=idx)
    long.index.names = ["date", "security_id"]
    for k in qp:
        long[k] = qp[k].stack(dropna=False).reindex(idx).to_numpy()
    long = long.reset_index()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    long.to_parquet(OUT_DIR / "features.parquet", index=False)
    fr.to_csv(OUT_DIR / "field_coverage.csv", index=False, encoding="utf-8-sig")
    years.to_csv(OUT_DIR / "coverage_by_year.csv", encoding="utf-8-sig")

    meta = {
        "secs": int(len(u)), "rows": int(len(b)),
        "crsp_rows": int((b["src"] == "crsp").sum()),
        "ext_rows": int((b["src"] == "external").sum()),
        "days": int(b["date"].nunique()),
        "first": str(b["date"].min().date()), "last": str(b["date"].max().date()),
        "raw_cov": float(fr["覆盖率"].mean()), "quant_cov": float(fq["覆盖率"].mean()),
        "usable": int(secs["usable_buy"].sum()),
        # 外部补的预热段没有股本/换手（源限制）→ 这段 turn20/turn120 一定是 NaN
        "no_shares": int((b["src"] == "external").sum()),
        # point-in-time 口径：分母是「当天成分股」，不是全池并集
        "mbr_rows": int(dd["mbr"].sum()),
        "mbr_median": int(dd["mbr"].median()),
        "gate_rate": float(dd["gate"].sum() / dd["mbr"].sum()),
        "kline_rate": float(dd["kline"].sum() / dd["mbr"].sum()),
        "gate_miss": int((dd["mbr"] - dd["gate"]).sum()),
        "gap_segs": int(len(dchk["gaps"])),
        "built": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(meta, fr, fq, years, secs, dchk), encoding="utf-8")

    print("\n原始数据字段覆盖率：")
    print(fr[["字段", "覆盖率", "有值证券", "全池首个有效日"]].to_string(index=False))
    print("\n量化数据字段覆盖率：")
    print(fq[["字段", "覆盖率", "有值证券", "预热消耗中位(根)"]].to_string(index=False))
    print(f"\n逐日成分股（point-in-time）：中位 {meta['mbr_median']} 只/日 ｜ "
          f"K线齐 {meta['kline_rate']:.2%} ｜ 四指标齐 {meta['gate_rate']:.2%} ｜ "
          f"缺口 {meta['gate_miss']:,} 行 / {meta['gap_segs']} 段")
    print(f"全池并集口径：指标齐全可买 {meta['usable']}/{meta['secs']} 只 | "
          f"预热段无股本行数 {meta['no_shares']:,} 行")
    print(f"面板 → {OUT_DIR / 'features.parquet'}（{len(long):,} 行 / {len(qp)} 个字段）")
    print(f"看板 → {out}")

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
