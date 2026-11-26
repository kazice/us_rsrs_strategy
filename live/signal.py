"""实盘信号生成（与 backtest 同一套口径）。

口径见 backtest/README.md。每月最后一个交易日收盘后运行：

    python live/signal.py                 # 用数据里最新交易日
    python live/signal.py --date 2026-11-13
    python live/signal.py --json          # 输出 JSON

输出：
    1. 当前牛熊状态
    2. 板块 RSRS 排名（前 3）
    3. 9 只候选池及每只是否过趋势闸
    4. 最终应买入的标的（个股或 ETF），或建议空仓
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "upload"    # 实盘默认读 upload（每日自动更新），可 --data-dir 覆盖
HERE = Path(__file__).resolve().parent


def _load_local(mod_name: str, path: Path):
    """按文件加载同目录模块。

    不能把 live/ 前插进 sys.path：目录里的本地模块会遮蔽同名的标准库
    （calendar.py 就让 pandas 的 _strptime 崩过一次）。
    """
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod          # dataclass 处理注解时需要模块已注册
    spec.loader.exec_module(mod)
    return mod


cal = _load_local("market_calendar", HERE / "market_calendar.py")

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLC"]
BENCH = "SPY"

ADX_MIN = 10.0
MCAP_MIN = 500e8
TURN_CAP = 0.20
TOPN_SECTOR = 3
TOPN_STOCK = 3
BEAR_TOPN = 3
BEAR_RATIO = 0.5
STOP_LOSS = 0.10

GATE_NAMES = [
    ("MA20>MA60", lambda r: r.ma20 > r.ma60),
    ("MA20 10日斜率>0", lambda r: r.slope20_10d > 0),
    ("MA60 5日斜率>0", lambda r: r.slope60_5d > 0),
    ("close>MA50", lambda r: r.close > r.ma50),
    ("MA50>MA200", lambda r: r.ma50 > r.ma200),
    ("ADX>10", lambda r: r.adx14 > ADX_MIN),
]


def load(data_dir: Path):
    bars = pd.read_parquet(data_dir / "bars_daily.parquet")[
        ["permno", "date", "symbol", "close", "turn20", "turn120", "turn_score", "mcap",
         "ma20", "ma50", "ma60", "ma200", "slope20_10d", "slope60_5d", "adx14"]
    ]
    etf = pd.read_parquet(data_dir / "etf_daily.parquet")[
        ["date", "etf_symbol", "close", "rsrs20_40_z", "slope120_weighted", "bear_ratio21"]
    ]
    mem = pd.read_parquet(data_dir / "members_daily.parquet")
    return bars, etf, mem


def occasion_kind(d: dt.date) -> str:
    """今天该做什么：rebalance（月末最后交易日）/ week（周内最后交易日）/ none。"""
    if not cal.is_trading_day(d):
        return "none"
    if cal.month_end_trading_day(d) == d:
        return "rebalance"
    if cal.week_end_trading_day(d) == d:
        return "week"
    return "none"


def _check(force: bool = False) -> int:
    """只判「今天该不该跑」，不读数据、不算信号。

    退出码：0 = 该跑；10 = 不该跑（workflow 用 == '10' 跳过，不当作失败）。
    窗口也要判：早到会用到未收盘的当日数据，晚到信号已经过时，两头都必须拒绝。
    """
    now = dt.datetime.now(cal.UTC)
    today = now.astimezone(cal.ET).date()
    occ = cal.occasion_for(today)
    kind = occasion_kind(today)
    print(f"{today} {occ.reason}")
    if kind == "none":
        print("非调仓日 / 非周末最后交易日，跳过")
        return 10
    if not force and not cal.in_window(now, occ, 5, 40):
        print(f"不在收盘前 {cal.TRIGGER_LEAD_MIN} 分钟窗口内，跳过")
        return 10
    print(f"kind={kind} 收盘 {occ.close_et} ET = {occ.close_utc:%H:%M}Z "
          f"触发 {occ.trigger_utc:%H:%M}Z")
    return 0


def _render_push(out: dict) -> tuple[str, str]:
    """把算出来的信号压成一条 pushplus 消息。"""
    kind = {"rebalance": "调仓", "week": "周报"}.get(out.get("kind", ""), "信号")
    title = f"[实盘] {out['signal_date']} {kind}：{out['action']}"
    lines = [
        f"## {out['signal_date']} {kind}",
        "",
        f"**状态**：{out['regime']}（SPY 跌破 MA200 占比 {out['spy_bear_ratio21']}）",
        "",
        f"**结论**：{out['action']}",
        "",
    ]
    if out["sector_rank"]:
        lines.append("**板块 RSRS**")
        for i, s in enumerate(out["sector_rank"][:5], 1):
            lines.append(f"{i}. {s['etf']} {s['rsrs_z']:+.3f}")
        lines.append("")
    if out["candidates"]:
        lines.append("**候选池**")
        for c in out["candidates"]:
            flag = "通过" if c["pass"] else "淘汰(" + ",".join(c["fail_on"]) + ")"
            lines.append(f"- {c['symbol']}（{c['sector']}）{flag}")
    return title[:100], "\n".join(lines)


def gate_detail(r) -> tuple[bool, list[str]]:
    fails = []
    vals = [r.ma20, r.ma50, r.ma60, r.ma200, r.slope20_10d, r.slope60_5d, r.adx14, r.close]
    if any(pd.isna(v) for v in vals):
        return False, ["指标缺失"]
    for name, fn in GATE_NAMES:
        if not fn(r):
            fails.append(name)
    return len(fails) == 0, fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="信号日，默认取最新交易日")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--hold", default=None, help="当前持仓代码，用于判断是否需要换仓")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA), help="数据目录，默认 upload")
    ap.add_argument("--check", action="store_true",
                    help="只判今天该不该跑（不读数据、不算信号），用于 workflow 提前退出")
    ap.add_argument("--push", action="store_true",
                    help="算完推送到 pushplus（需要 PUSHPLUS_TOKEN 环境变量）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --check：无视窗口判定（手工验证用）")
    a = ap.parse_args()

    if a.check:
        return _check(a.force)

    data_dir = Path(a.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    bars, etf, mem = load(data_dir)
    all_dates = sorted(bars.date.unique())
    dt = pd.Timestamp(a.date) if a.date else pd.Timestamp(all_dates[-1])
    if dt not in bars.date.values:
        near = [d for d in all_dates if d <= dt]
        if not near:
            print(f"日期 {dt.date()} 早于数据起点")
            return 1
        dt = pd.Timestamp(near[-1])

    snap = bars[bars.date == dt]
    e = etf[etf.date == dt].copy()
    m = mem[mem.date == dt]

    # 1) 牛熊
    spy_row = e[e.etf_symbol == BENCH]
    bear_ratio = float(spy_row.bear_ratio21.iloc[0]) if len(spy_row) else float("nan")
    is_bear = bool(bear_ratio > BEAR_RATIO) if pd.notna(bear_ratio) else False

    out = {
        "signal_date": str(dt.date()),
        "kind": occasion_kind(dt.date()),
        "regime": "熊市" if is_bear else "牛市",
        "spy_bear_ratio21": None if pd.isna(bear_ratio) else round(bear_ratio, 4),
        "sector_rank": [],
        "candidates": [],
        "pick": None,
        "action": None,
    }

    # 2) 板块排名
    sec = e[e.etf_symbol.isin(SECTOR_ETFS)].dropna(subset=["rsrs20_40_z"]).sort_values(
        "rsrs20_40_z", ascending=False
    )
    for _, r in sec.iterrows():
        out["sector_rank"].append({
            "etf": str(r.etf_symbol),
            "rsrs_z": round(float(r.rsrs20_40_z), 4),
            "slope120": None if pd.isna(r.slope120_weighted) else round(float(r.slope120_weighted), 4),
        })

    if is_bear:
        # 3a) 熊市：RSRS 前3 ∩ 斜率前3
        top_r = set(sec.head(BEAR_TOPN).etf_symbol)
        s = sec.dropna(subset=["slope120_weighted"])
        top_s = set(s.nlargest(BEAR_TOPN, "slope120_weighted").etf_symbol) if len(s) else set()
        both = top_r & top_s
        if both:
            cand = sec[sec.etf_symbol.isin(both)].iloc[0]
            out["pick"] = {"kind": "etf", "symbol": str(cand.etf_symbol),
                           "close": round(float(cand.close), 2),
                           "reason": "熊市买板块ETF（RSRS∩斜率 交集）"}
            out["action"] = f"买入 {cand.etf_symbol}"
        else:
            out["action"] = "空仓（熊市且 ETF 交集为空）"
    else:
        # 3b) 牛市：9 只候选 + 趋势闸
        for sec_sym in sec.head(TOPN_SECTOR).etf_symbol:
            codes = m.loc[m.sector_etf == sec_sym, "permno"].unique()
            c = snap[snap.permno.isin(codes)]
            c = c[c.turn20.notna() & (c.turn20 <= TURN_CAP) & c.turn120.notna() & (c.turn120 > 0)]
            c = c[c.turn_score.notna() & c.mcap.notna() & (c.mcap > MCAP_MIN)]
            c = c.sort_values("turn_score", ascending=False).head(TOPN_STOCK)
            for _, r in c.iterrows():
                ok, fails = gate_detail(r)
                out["candidates"].append({
                    "sector": str(sec_sym),
                    "symbol": str(r.symbol),
                    "close": round(float(r.close), 2),
                    "turn20": round(float(r.turn20), 4),
                    "turn_score": round(float(r.turn_score), 4),
                    "mcap_usd_bn": round(float(r.mcap) / 1e8, 1),
                    "adx14": None if pd.isna(r.adx14) else round(float(r.adx14), 2),
                    "pass": ok,
                    "fail_on": fails,
                })

        first = next((c for c in out["candidates"] if c["pass"]), None)
        if first:
            out["pick"] = {"kind": "stock", "symbol": first["symbol"], "sector": first["sector"],
                           "close": first["close"], "reason": "9只候选池中第一只通过趋势闸"}
            out["action"] = f"买入 {first['symbol']}（{first['sector']}）"
        else:
            out["action"] = "调仓日无票通过，次一交易日起逐日补选"

    # 换仓提示
    if a.hold:
        if out["pick"] and out["pick"]["symbol"] == a.hold.upper():
            out["action"] = f"继续持有 {a.hold.upper()}（无需交易）"
        elif out["pick"]:
            out["action"] = f"卖出 {a.hold.upper()} → 买入 {out['pick']['symbol']}"
        else:
            out["action"] = f"卖出 {a.hold.upper()} → 空仓"

    if a.push:
        notify = _load_local("live_notify", HERE / "notify.py")
        title, body = _render_push(out)
        if not notify.send(title, body):
            return 2
        print(f"已推送：{title}")

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"信号日: {out['signal_date']}   状态: {out['regime']}  "
          f"(SPY 跌破MA200占比 {out['spy_bear_ratio21']})")
    print("\n板块 RSRS 排名:")
    for i, s in enumerate(out["sector_rank"], 1):
        mark = " ←入选" if i <= TOPN_SECTOR else ""
        print(f"  {i:2}. {s['etf']:5} rsrs_z={s['rsrs_z']:7.4f}  slope120={s['slope120']}{mark}")

    if out["candidates"]:
        print(f"\n候选池 {len(out['candidates'])} 只:")
        for c in out["candidates"]:
            flag = "PASS" if c["pass"] else "FAIL " + ",".join(c["fail_on"])
            print(f"  {c['sector']:5}{c['symbol']:7} close={c['close']:8.2f} turn20={c['turn20']:.4f} "
                  f"mcap={c['mcap_usd_bn']:7.1f}亿USD adx={c['adx14']}  {flag}")

    print(f"\n>>> {out['action']}")
    if out["pick"]:
        print(f"    依据: {out['pick']['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
