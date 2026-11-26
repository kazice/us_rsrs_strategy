"""实盘信号与动作（与 backtest 同一套口径）。

四种动作，由 ``--kind`` 决定，``auto`` 按账本 + 日历判断：

    rebalance  月末最后交易日：选标的、记账、推送
    week       周内最后交易日：只出周报，不动持仓
    stop       每个交易日收盘前：持仓票跌破 -10% 割掉，本月 11 号前可重选
    refill     空仓 / 止损后：逐日重挑，直到下一个调仓日

用法：

    python live/signal.py --check                 # 只判该不该跑（workflow 用，不读 parquet）
    python live/signal.py --kind auto --push      # 正常一轮
    python live/signal.py --date 2026-11-13       # 复盘：只算不记账
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
books = _load_local("live_books", HERE / "books.py")
quote = _load_local("live_quote", HERE / "quote.py")

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
STOP_REPICK_DOM = 11        # 止损后允许重选的截止日（自然月）

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


def load_k(data_dir: Path) -> dict[str, float]:
    """复权因子表：未复权报价 × k = 表里的复权口径。"""
    p = data_dir / "adjust_k.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    return {str(s).upper(): float(k) for s, k in zip(df.symbol, df.k)}


def occasion_kind(d: dt.date) -> str:
    """日历口径：rebalance（月末最后交易日）/ week（周内最后交易日）/ none。"""
    if not cal.is_trading_day(d):
        return "none"
    if cal.month_end_trading_day(d) == d:
        return "rebalance"
    if cal.week_end_trading_day(d) == d:
        return "week"
    return "none"


def resolve_kind(d: dt.date, pos, refill: dict) -> str:
    """结合账本的最终动作：持仓每天查止损，补选窗口内逐日重挑。"""
    if not cal.is_trading_day(d):
        return "none"
    if not pos.is_empty():
        return "stop"
    if refill.get("open") and str(refill.get("since", "")) <= str(d) <= str(refill.get("until", "")):
        return "refill"
    return occasion_kind(d)


def next_month_end(d: dt.date) -> dt.date:
    """下一个自然月的最后交易日，作为补选窗口的右边界。"""
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    for day in range(31, 0, -1):
        try:
            cand = dt.date(y, m, day)
        except ValueError:
            continue
        if cal.is_trading_day(cand):
            return cand
    return d


def _check(force: bool = False) -> int:
    """只判「今天该不该跑」，只读 json 账本，不读 parquet。

    退出码：0 = 该跑；10 = 不该跑（workflow 用 == '10' 跳过，不当作失败）。
    """
    now = dt.datetime.now(cal.UTC)
    today = now.astimezone(cal.ET).date()
    occ = cal.occasion_for(today)
    pos = books.load_position()
    refill = books.load_refill()
    kind = resolve_kind(today, pos, refill)
    print(f"{today} {occ.reason}" + (f"｜持仓 {pos.symbol}" if pos.symbol else "")
          + ("｜补选中" if refill.get("open") else ""))
    if kind == "none":
        print("非调仓日 / 非周末最后交易日且无持仓、无补选，跳过")
        return 10
    if not force and not cal.in_window(now, occ, 5, 40):
        print(f"不在收盘前 {cal.TRIGGER_LEAD_MIN} 分钟窗口内，跳过")
        return 10
    print(f"kind={kind} 收盘 {occ.close_et} ET = {occ.close_utc:%H:%M}Z "
          f"触发 {occ.trigger_utc:%H:%M}Z")
    return 0


def gate_detail(r) -> tuple[bool, list[str]]:
    fails = []
    vals = [r.ma20, r.ma50, r.ma60, r.ma200, r.slope20_10d, r.slope60_5d, r.adx14, r.close]
    if any(pd.isna(v) for v in vals):
        return False, ["指标缺失"]
    for name, fn in GATE_NAMES:
        if not fn(r):
            fails.append(name)
    return len(fails) == 0, fails


def compute_signal(bars, etf, mem, day: pd.Timestamp) -> dict:
    """当期信号：牛熊、板块排名、候选池、最终标的。"""
    snap = bars[bars.date == day]
    e = etf[etf.date == day].copy()
    m = mem[mem.date == day]

    spy_row = e[e.etf_symbol == BENCH]
    bear_ratio = float(spy_row.bear_ratio21.iloc[0]) if len(spy_row) else float("nan")
    is_bear = bool(bear_ratio > BEAR_RATIO) if pd.notna(bear_ratio) else False

    out = {
        "signal_date": str(day.date()),
        "kind": occasion_kind(day.date()),
        "regime": "熊市" if is_bear else "牛市",
        "is_bear": is_bear,
        "spy_bear_ratio21": None if pd.isna(bear_ratio) else round(bear_ratio, 4),
        "sector_rank": [],
        "candidates": [],
        "pick": None,
        "action": None,
    }

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
        top_r = set(sec.head(BEAR_TOPN).etf_symbol)
        s = sec.dropna(subset=["slope120_weighted"])
        top_s = set(s.nlargest(BEAR_TOPN, "slope120_weighted").etf_symbol) if len(s) else set()
        both = top_r & top_s
        if both:
            cand = sec[sec.etf_symbol.isin(both)].iloc[0]
            out["pick"] = {"kind": "etf", "symbol": str(cand.etf_symbol),
                           "sector": str(cand.etf_symbol),
                           "close": round(float(cand.close), 2),
                           "reason": "熊市买板块ETF（RSRS∩斜率 交集）"}
            out["action"] = f"买入 {cand.etf_symbol}"
        else:
            out["action"] = "空仓（熊市且 ETF 交集为空）"
    else:
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
            out["action"] = "无票通过趋势闸，空仓"
    return out


def _close_of(symbol: str, day: pd.Timestamp, bars, etf) -> float | None:
    """某标的当日的复权收盘（ETF 走 etf 表）。"""
    if symbol in SECTOR_ETFS or symbol == BENCH:
        r = etf[(etf.date == day) & (etf.etf_symbol == symbol)]
        return float(r.close.iloc[0]) if len(r) else None
    r = bars.loc[(bars.date == day) & (bars.symbol.astype(str) == symbol), "close"]
    return float(r.iloc[0]) if len(r) else None


def _push(out: dict, extra: list[str] | None = None) -> bool:
    notify = _load_local("live_notify", HERE / "notify.py")
    title, body = _render_push(out)
    if extra:
        body += "\n\n" + "\n".join(extra)
    return notify.send(title, body)


def _render_push(out: dict) -> tuple[str, str]:
    kind = {"rebalance": "调仓", "week": "周报", "stop": "止损",
            "refill": "补选"}.get(out.get("kind", ""), "信号")
    title = f"[实盘] {out['signal_date']} {kind}：{out['action']}"
    lines = [
        f"## {out['signal_date']} {kind}",
        "",
        f"**状态**：{out['regime']}（SPY 跌破 MA200 占比 {out['spy_bear_ratio21']}）",
        "",
        f"**结论**：{out['action']}",
        "",
    ]
    if out.get("position_line"):
        lines += [out["position_line"], ""]
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


# ---------------------------------------------------------------- 各 kind 的执行

def _run_rebalance(out: dict, pos, a, bars, etf, ktab) -> int:
    day = pd.Timestamp(out["signal_date"])
    target = out["pick"]["symbol"] if out["pick"] else ""
    target_kind = out["pick"]["kind"] if out["pick"] else "stock"

    ret = None
    if not pos.is_empty():
        old_close = _close_of(pos.symbol, day, bars, etf)
        if old_close and pos.entry_adj:
            ret = old_close / pos.entry_adj - 1.0
        out["position_line"] = f"**当前持仓**：{pos.symbol}（{pos.entry_date} 建仓，"
        out["position_line"] += f"持有期 {ret:+.2%}）" if ret is not None else "收益待算）"

    action, desc = books.plan_action(pos, target)
    out["action"] = desc if not a.replay else out["action"] + f" ｜ {desc}"

    if not a.replay:
        price = 0.0
        k = 1.0
        sector = ""
        if target:
            price = _close_of(target, day, bars, etf) or 0.0
            k = 1.0 if target_kind == "etf" else ktab.get(target.upper(), 1.0)
            sector = out["pick"].get("sector", "")
        books.record(pos, day=out["signal_date"], kind="rebalance", action=action,
                     target=target, target_kind=target_kind, sector=sector,
                     price=price, k=k, ret=ret, note=out["regime"])
        # 牛市调仓日一只都没过闸 → 开补选窗口，次一交易日起逐日重挑
        if not target and not out["is_bear"]:
            books.set_refill(since=out["signal_date"],
                             until=str(next_month_end(day.date())),
                             bull=True, origin="rebalance")
        elif not target:
            books.clear_refill("熊市空仓，不开补选")
        else:
            books.clear_refill("已建仓")
        # 先落幂等键再推送：推送失败时账本也不能被下一次重跑再记一遍
        books.mark_pushed("rebalance", out["signal_date"], {"action": action})

    if a.push and not _push(out):
        return 2
    return 0


def _run_week(out: dict, pos, a) -> int:
    out["kind"] = "week"
    if not pos.is_empty():
        out["position_line"] = f"**当前持仓**：{pos.symbol}（{pos.entry_date} 建仓）"
    out["action"] = f"周报｜{'持仓 ' + pos.symbol if pos.symbol else '空仓'}｜{out['action']}"
    if a.push and _push(out):
        books.mark_pushed("week", out["signal_date"], {"action": out["action"][:60]})
    return 0


def _run_stop(pos, a, data_dir: Path, ktab) -> int:
    """收盘前止损检查：只取持仓票一个实时价，不跑全池。ETF 不参与（与回测一致）。"""
    if pos.is_empty():
        print("无持仓，跳过止损检查")
        return 0
    if pos.kind == "etf":
        print(f"持仓 {pos.symbol} 是 ETF，不参与止损")
        return 0

    q = quote.fetch([pos.symbol]).get(pos.symbol.upper())
    if q is None:
        print(f"::warning::取不到 {pos.symbol} 实时价，本次不做止损判断（不凭旧价割肉）")
        return 0

    k = ktab.get(pos.symbol.upper(), pos.entry_k or 1.0)
    hit, ret = books.stop_triggered(pos, q, k, STOP_LOSS)
    ret_s = "n/a" if ret is None else f"{ret:+.2%}"
    print(f"{pos.symbol} 现价 {q:.2f} × k{k:.4f} = 复权 {q * k:.2f}｜"
          f"建仓 {pos.entry_adj:.2f}（{pos.entry_date}）｜持有期 {ret_s}")

    if not hit:
        print(f"未触及 -{STOP_LOSS:.0%}，继续持有")
        return 0

    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(cal.ET).date()
    if not a.replay and books.already_pushed("stop", str(today)):
        print(f"{today} 已做过止损检查，跳过")
        return 0
    sold = pos.symbol                     # record_stop 会清掉 pos.symbol，先留住
    action = f"止损卖出 {sold}（{ret_s}）"
    allow = today.day < STOP_REPICK_DOM and int(pos.stop_retry) == 0
    print(f"{action}｜{'本月可重选' if allow else '本月不再重选'}")

    if a.replay:
        return 0

    books.record_stop(pos, day=str(today), action=action, price=q, ret=ret,
                      note="止损", retry_used=1 if allow else pos.stop_retry)
    if allow:
        books.set_refill(since=str(today), until=str(next_month_end(today)),
                         bull=True, exclude=sold, origin="stop")

    out = {"signal_date": str(today), "kind": "stop", "regime": "-",
           "spy_bear_ratio21": None, "sector_rank": [], "candidates": [],
           "action": action,
           "position_line": f"**已清仓**：{sold}｜持有期 {ret_s}｜卖出价 {q:.2f}"}
    tail = f"补选：{'次一交易日起逐日重挑（排除 ' + sold + '）' if allow else '本月不再重选'}"
    books.mark_pushed("stop", str(today), {"action": action[:60]})
    if a.push and not _push(out, [tail]):
        return 2
    return 0


def _run_refill(out: dict, pos, refill: dict, a, bars, etf, ktab) -> int:
    """补选：用当期信号重挑，命中即建仓；不命中就安静退出（不推送、不记账）。"""
    day = pd.Timestamp(out["signal_date"])
    out["kind"] = "refill"
    exclude = str(refill.get("exclude") or "").upper()

    if not out["is_bear"] and bool(refill.get("bull", True)):
        cands = [c for c in out["candidates"] if c["pass"] and c["symbol"].upper() != exclude]
        pick = cands[0] if cands else None
        if pick:
            out["pick"] = {"kind": "stock", "symbol": pick["symbol"], "sector": pick["sector"],
                           "close": pick["close"], "reason": f"补选（排除 {exclude or '无'}）"}
    else:
        # 熊市补选窗口：沿用 ETF 通道
        if out["pick"] and out["pick"]["symbol"].upper() == exclude:
            out["pick"] = None

    target = out["pick"]["symbol"] if out["pick"] else ""
    if not target:
        print(f"补选窗口 {refill.get('since')} ~ {refill.get('until')}：今天仍无合格标的")
        return 0

    out["action"] = f"补选买入 {target}（{out['pick'].get('sector', '')}）"
    print(out["action"])
    if a.replay:
        return 0

    price = _close_of(target, day, bars, etf) or 0.0
    kind = out["pick"]["kind"]
    k = 1.0 if kind == "etf" else ktab.get(target.upper(), 1.0)
    books.record(pos, day=out["signal_date"], kind="refill", action="建仓",
                 target=target, target_kind=kind, sector=out["pick"].get("sector", ""),
                 price=price, k=k, ret=None, note=f"补选自 {refill.get('since')}")
    books.clear_refill("补选成功")
    books.mark_pushed("refill", out["signal_date"], {"action": out["action"][:60]})
    if a.push and not _push(out):
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="信号日，默认取最新交易日")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--hold", default=None, help="临时指定当前持仓（不复盘账本时用）")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA), help="数据目录，默认 upload")
    ap.add_argument("--kind", default="auto",
                    choices=["auto", "rebalance", "week", "stop", "refill"],
                    help="auto = 按账本+日历判断")
    ap.add_argument("--today", default=None, help="指定今天（YYYY-MM-DD），排错用")
    ap.add_argument("--check", action="store_true",
                    help="只判今天该不该跑（只读账本，不读 parquet），用于 workflow 提前退出")
    ap.add_argument("--push", action="store_true", help="推送到 pushplus（需 PUSHPLUS_TOKEN）")
    ap.add_argument("--force", action="store_true", help="配合 --check：无视窗口判定")
    a = ap.parse_args()
    a.replay = a.date is not None        # 指定日期 = 复盘，不写账本

    if a.check:
        return _check(a.force)

    data_dir = Path(a.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    pos = books.load_position() if not a.replay else books.Position(symbol=(a.hold or "").upper())
    refill = books.load_refill()

    # 止损只需要一个实时价，不必加载 82MB 的行情表
    ktab = load_k(data_dir)
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(cal.ET).date()
    kind = a.kind if a.kind != "auto" else (
        occasion_kind(today) if a.replay else resolve_kind(today, pos, refill))

    if kind == "stop":
        return _run_stop(pos, a, data_dir, ktab)

    bars, etf, mem = load(data_dir)
    all_dates = sorted(bars.date.unique())
    day = pd.Timestamp(a.date) if a.date else pd.Timestamp(all_dates[-1])
    if day not in bars.date.values:
        near = [d for d in all_dates if d <= day]
        if not near:
            print(f"日期 {day.date()} 早于数据起点")
            return 1
        day = pd.Timestamp(near[-1])

    out = compute_signal(bars, etf, mem, day)
    if a.hold:
        out["action"] = f"（指定持仓 {a.hold.upper()}）" + out["action"]

    # 同一天被 Cloudflare 和 GitHub schedule 各触发一次时，这里挡住第二次记账
    if not a.replay and kind in ("rebalance", "week", "refill") \
            and books.already_pushed(kind, out["signal_date"]):
        print(f"{kind} {out['signal_date']} 已处理过，跳过")
        return 0

    if kind == "rebalance":
        rc = _run_rebalance(out, pos, a, bars, etf, ktab)
    elif kind == "week":
        rc = _run_week(out, pos, a)
    elif kind == "refill":
        rc = _run_refill(out, pos, refill, a, bars, etf, ktab)
    else:
        out["action"] = out["action"] or "无动作"
        rc = 0

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return rc

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
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
