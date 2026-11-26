"""板块轮动 + 个股换手率排序 回测（benchmark）。

口径见同目录 README.md。核心规则：
1. 板块池 10 个 SPDR 行业 ETF（不含 XLRE 房地产）
2. 板块层按 RSRS 取前 3；每板块按 turn20^2/turn120 取前 3 → 9 只候选池
3. 趋势闸 6 条全 AND；MA20 只看 10 日斜率、MA60 看 5 日斜率；ADX > 10
4. **熊市判定**：SPY 收盘价低于 MA200 的天数，过去 21 个交易日中占比 > 50%
   （等价于 bear_ratio21 > 0.5，实测与 is_bull 字段 1474/1474 完全一致）
5. **熊市不买个股，改买板块 ETF**（RSRS 前 3 ∩ 120 日加权斜率前 3 的交集）
6. **牛市空仓补选**：调仓日选不出时，次一交易日起逐日重试，直到选出或到下一调仓日
7. 换手率折算：回测 1.0（实盘 0.48）
8. 个股止损 −10%；止损在每月 11 号前可补买 1 次且不得重复该票
9. 同标的跨期继续持有不重复收费
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "latest_data"      # 默认用基板数据；传 --data-dir upload 即用实盘数据
BASELINE_DIR = ROOT / "reports" / "baseline"   # 基线固定放这里
LOGS_DIR = ROOT / "logs"                       # 参数试验按时间戳归档

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLC"]
BENCH = "SPY"

ADX_MIN = 10.0
MCAP_MIN = 500e8
TURN_CAP = 0.20
TOPN_SECTOR = 3
TOPN_STOCK = 3
STOP_LOSS = 0.10
REFILL_DAY = 11
BEAR_TOPN = 3
BEAR_RATIO = 0.5      # 过去 21 日中跌破 MA200 的比例阈值

DEFAULT_START = "2021-12-31"   # 策略定义的回测起点

PARAM_KEYS = ["ADX_MIN", "MCAP_MIN", "TURN_CAP", "TOPN_SECTOR", "TOPN_STOCK",
              "STOP_LOSS", "REFILL_DAY", "BEAR_TOPN", "BEAR_RATIO", "DEFAULT_START"]


def run_dir(tag: str) -> Path:
    """一次回测 = 一个目录。

    baseline 是正式基线，固定落在 reports/baseline；其它 tag 视为改参数的试验，
    按时间戳落到 logs/<时间戳>_<tag>/，以免和基线混在一起。
    """
    tag = (tag or "baseline").strip()
    if tag == "baseline":
        return BASELINE_DIR
    return LOGS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{tag}"


def _dashboard_mod():
    spec = importlib.util.spec_from_file_location(
        "make_dashboard", ROOT / "backtest" / "make_dashboard.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["make_dashboard"] = m
    spec.loader.exec_module(m)
    return m


@dataclass
class Cfg:
    """start 默认策略起点（数据不足时自动退到数据首日）；end 为 None 时跟随数据末日。"""
    start: str | None = DEFAULT_START
    end: str | None = None
    cost: float = 0.0005
    data_dir: str | Path = DEFAULT_DATA


def load_data(data_dir):
    d = Path(data_dir)
    for f in ["bars_daily.parquet", "etf_daily.parquet", "members_daily.parquet"]:
        if not (d / f).exists():
            raise FileNotFoundError(f"缺少 {d / f}，请检查 --data-dir")
    bars = pd.read_parquet(d / "bars_daily.parquet")[
        ["permno", "date", "symbol", "close", "turn20", "turn120", "turn_score", "mcap",
         "ma20", "ma50", "ma60", "ma200", "slope20_10d", "slope60_5d", "adx14"]
    ]
    etf = pd.read_parquet(d / "etf_daily.parquet")[
        ["date", "etf_symbol", "close", "rsrs20_40_z", "slope120_weighted", "bear_ratio21"]
    ]
    mem = pd.read_parquet(d / "members_daily.parquet")
    return bars, etf, mem


def month_ends(cal: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(cal, index=cal)
    return pd.DatetimeIndex(s.groupby([cal.year, cal.month]).max().values)


def trend_ok(r) -> bool:
    v = (r.ma20, r.ma50, r.ma60, r.ma200, r.slope20_10d, r.slope60_5d, r.adx14, r.close)
    if any(pd.isna(x) for x in v):
        return False
    return bool(
        r.ma20 > r.ma60
        and r.slope20_10d > 0
        and r.slope60_5d > 0
        and r.close > r.ma50
        and r.ma50 > r.ma200
        and r.adx14 > ADX_MIN
    )


class Engine:
    def __init__(self, data_dir=DEFAULT_DATA):
        self.bars, self.etf, self.mem = load_data(data_dir)
        self.snaps = {d: g for d, g in self.bars.groupby("date")}
        self.etf_by_date = {d: g for d, g in self.etf.groupby("date")}
        self.mem_by_date = {d: g for d, g in self.mem.groupby("date")}
        self.sym = self.bars.drop_duplicates("permno").set_index("permno").symbol.to_dict()

    def bear(self, dt) -> bool:
        e = self.etf_by_date.get(dt)
        if e is None:
            return False
        r = e.loc[e.etf_symbol == BENCH, "bear_ratio21"]
        if not len(r) or pd.isna(r.iloc[0]):
            return False
        return bool(r.iloc[0] > BEAR_RATIO)

    def stock_candidates(self, dt):
        e = self.etf_by_date.get(dt)
        snap = self.snaps.get(dt)
        mem = self.mem_by_date.get(dt)
        if e is None or snap is None or mem is None:
            return []
        e = e[e.etf_symbol.isin(SECTOR_ETFS)].dropna(subset=["rsrs20_40_z"])
        if not len(e):
            return []
        e = e.sort_values("rsrs20_40_z", ascending=False)
        out = []
        for sec in e.etf_symbol.head(TOPN_SECTOR):
            codes = mem.loc[mem.sector_etf == sec, "permno"].unique()
            c = snap[snap.permno.isin(codes)]
            c = c[c.turn20.notna() & (c.turn20 <= TURN_CAP) & c.turn120.notna() & (c.turn120 > 0)]
            c = c[c.turn_score.notna() & c.mcap.notna() & (c.mcap > MCAP_MIN)]
            c = c.sort_values("turn_score", ascending=False).head(TOPN_STOCK)
            if len(c):
                out.append((sec, c.permno.tolist()))
        return out

    def pick_stock(self, cands, dt, exclude=None):
        snap = self.snaps.get(dt)
        if snap is None:
            return None
        for sec, plist in cands:
            for p in plist:
                if p == exclude:
                    continue
                row = snap[snap.permno == p]
                if not len(row) or not trend_ok(row.iloc[0]):
                    continue
                return sec, p, float(row.iloc[0].close)
        return None

    def pick_bear_etf(self, dt):
        e = self.etf_by_date.get(dt)
        if e is None:
            return None
        e = e[e.etf_symbol.isin(SECTOR_ETFS)].dropna(subset=["rsrs20_40_z"])
        if not len(e):
            return None
        top_r = set(e.nlargest(BEAR_TOPN, "rsrs20_40_z").etf_symbol)
        s = e.dropna(subset=["slope120_weighted"])
        if not len(s):
            return None
        both = top_r & set(s.nlargest(BEAR_TOPN, "slope120_weighted").etf_symbol)
        if not both:
            return None
        c = e[e.etf_symbol.isin(both)].sort_values("rsrs20_40_z", ascending=False).iloc[0]
        return str(c.etf_symbol), float(c.close)


def run(cfg: Cfg, dump: bool = False):
    eng = Engine(cfg.data_dir)
    cal = pd.DatetimeIndex(sorted(eng.bars.date.unique()))
    # 未指定区间时自动跟随数据实际范围，保证 upload 更新后回测能覆盖到最新
    if cfg.start:
        cal = cal[cal >= pd.Timestamp(cfg.start)]
    if cfg.end:
        cal = cal[cal <= pd.Timestamp(cfg.end)]
    if not len(cal):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    rebal = month_ends(cal)

    px = eng.bars.pivot_table(index="date", columns="permno", values="close")
    epx = eng.etf.pivot_table(index="date", columns="etf_symbol", values="close")

    nav_rows, trades, cand_rows = [], [], []
    capital = 1.0
    cur = None

    for i, dt in enumerate(rebal):
        nxt = rebal[i + 1] if i + 1 < len(rebal) else cal[-1]
        if dt not in px.index:
            continue
        hold_days = cal[(cal > dt) & (cal <= nxt)]
        if not len(hold_days):
            continue

        is_bear = eng.bear(dt)
        cands = eng.stock_candidates(dt) if not is_bear else []
        if dump and not is_bear:
            snap = eng.snaps[dt]
            for sec, pl in cands:
                for p in pl:
                    r = snap[snap.permno == p].iloc[0]
                    cand_rows.append(dict(date=dt, sector=sec, permno=p, symbol=eng.sym.get(p, ""),
                                          turn_score=r.turn_score, adx=r.adx14, ok=trend_ok(r)))

        # ---------- 调仓日决策
        target = None
        if is_bear:
            got = eng.pick_bear_etf(dt)
            if got:
                target = (got[0], "etf", got[1], got[0], got[0])
        else:
            got = eng.pick_stock(cands, dt)
            if got:
                sec, p, pxf = got
                target = (p, "stock", pxf, sec, eng.sym.get(p, ""))

        if cur is not None and target is not None and cur["key"] == target[0]:
            pass  # 同标的续持
        else:
            if cur is not None:
                pr = (px.at[dt, cur["key"]] if (cur["kind"] == "stock" and cur["key"] in px.columns and dt in px.index)
                      else (epx.at[dt, cur["key"]] if (cur["kind"] == "etf" and cur["key"] in epx.columns and dt in epx.index)
                            else np.nan))
                if pd.notna(pr):
                    capital = cur["shares"] * pr * (1 - cfg.cost)
                    trades.append(dict(buy=cur["buy_dt"], sell=dt, symbol=cur["name"], sector=cur["sector"],
                                       buy_px=cur["buy_px"], sell_px=pr,
                                       ret=pr / cur["buy_px"] - 1, reason="rebalance"))
                cur = None
            if target is not None and pd.notna(target[2]) and target[2] > 0:
                key, kind, pxf, sec, name = target
                capital *= 1 - cfg.cost
                cur = dict(key=key, kind=kind, shares=capital / pxf, buy_px=pxf,
                           buy_dt=dt, sector=sec, name=name)

        # ---------- 持有期（含牛市空仓补选、止损、止损补买）
        for d in hold_days:
            # 牛市空仓 -> 逐日补选（熊市不补选，空仓到下一调仓日）
            if cur is None and not is_bear:
                c2 = eng.stock_candidates(d)
                got2 = eng.pick_stock(c2, d)
                if got2:
                    sec2, q, qpx = got2
                    if pd.notna(qpx) and qpx > 0:
                        capital *= 1 - cfg.cost
                        cur = dict(key=q, kind="stock", shares=capital / qpx, buy_px=qpx,
                                   buy_dt=d, sector=sec2, name=eng.sym.get(q, ""))

            if cur is None:
                nav_rows.append(dict(date=d, nav=capital, hold="cash"))
                continue

            pr = (px.at[d, cur["key"]] if (cur["kind"] == "stock" and cur["key"] in px.columns and d in px.index)
                  else (epx.at[d, cur["key"]] if (cur["kind"] == "etf" and cur["key"] in epx.columns and d in epx.index)
                        else np.nan))
            if pd.isna(pr):
                continue

            # 个股止损
            if cur["kind"] == "stock" and pr <= cur["buy_px"] * (1 - STOP_LOSS):
                capital = cur["shares"] * pr * (1 - cfg.cost)
                trades.append(dict(buy=cur["buy_dt"], sell=d, symbol=cur["name"], sector=cur["sector"],
                                   buy_px=cur["buy_px"], sell_px=pr,
                                   ret=pr / cur["buy_px"] - 1, reason="stop_loss"))
                nav_rows.append(dict(date=d, nav=capital, hold="cash"))
                lost = cur["key"]
                cur = None
                # 11 号前补买一次（不同票）
                if d.day < REFILL_DAY:
                    # 必须用当天 d 的候选与报价：用 cands/dt 会把调仓日的收盘价
                    # 当成当天的买入价，Buy 记录的日期与价格对不上（差出好几天）。
                    c3 = eng.stock_candidates(d)
                    got3 = eng.pick_stock(c3, d, exclude=lost)
                    if got3:
                        sec3, q3, qpx3 = got3
                        if pd.notna(qpx3) and qpx3 > 0:
                            capital *= 1 - cfg.cost
                            cur = dict(key=q3, kind="stock", shares=capital / qpx3, buy_px=qpx3,
                                       buy_dt=d, sector=sec3, name=eng.sym.get(q3, ""))
                            continue
                continue

            nav_rows.append(dict(date=d, nav=cur["shares"] * pr, hold=cur["name"]))

    nav = pd.DataFrame(nav_rows).drop_duplicates("date").sort_values("date").set_index("date")
    return nav, pd.DataFrame(trades), pd.DataFrame(cand_rows)


def perf(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    if len(nav) < 2:
        return {}
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (1 + total) ** (1 / yrs) - 1 if yrs > 0 else np.nan
    vol = r.std() * np.sqrt(252)
    return dict(累计=total, 年化=cagr, 波动=vol, 夏普=cagr / vol if vol else np.nan,
                最大回撤=(nav / nav.cummax() - 1).min(), 天数=len(nav))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA),
                    help="数据目录：latest_data（默认，1.0 换手率口径）或 upload（0.48，与实盘一致）")
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"默认 {DEFAULT_START}；数据不足时自动退到数据首日")
    ap.add_argument("--end", default=None, help="默认取数据末日（跟随 upload 更新）")
    ap.add_argument("--cost", type=float, default=0.0005)
    ap.add_argument("--tag", default="baseline",
                    help="baseline = 正式基线（落 reports/baseline）；其它名字 = 参数试验，"
                         "按时间戳落 logs/<时间戳>_<tag>/")
    ap.add_argument("--dump-candidates", action="store_true")
    a = ap.parse_args()

    data_dir = Path(a.data_dir)

    nav, trades, cands = run(Cfg(a.start, a.end, a.cost, data_dir), a.dump_candidates)
    if not len(nav):
        print("no nav")
        return 1

    etf = pd.read_parquet(data_dir / "etf_daily.parquet")
    spy = etf[etf.etf_symbol == BENCH].set_index("date").close.reindex(nav.index).ffill().bfill()
    bench = spy / spy.iloc[0]

    out = run_dir(a.tag)
    out.mkdir(parents=True, exist_ok=True)
    nav["nav"].to_csv(out / "nav_daily.csv")
    nav["hold"].to_csv(out / "hold_daily.csv")
    bench.rename("spy").to_csv(out / "bench_spy.csv")
    if len(trades):
        trades.to_csv(out / "holds.csv", index=False)
    if len(cands):
        cands.to_csv(out / "candidates.csv", index=False)

    st = pd.DataFrame({"策略": perf(nav["nav"]), "SPY": perf(bench)})
    st.to_csv(out / "stats.csv")

    # 参数落盘：事后翻这个目录就知道这轮跑的是什么口径
    cash_ratio = float((nav["hold"] == "cash").mean())
    params = {
        "tag": a.tag,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": data_dir.name,
        "start": str(nav.index.min().date()),
        "end": str(nav.index.max().date()),
        "cost": a.cost,
        "strategy": {k: globals().get(k) for k in PARAM_KEYS},
        "summary": {
            "trades": int(len(trades)),
            "stops": int((trades.reason == "stop_loss").sum()) if len(trades) else 0,
            "win": round(float((trades.ret > 0).mean()), 4) if len(trades) else 0.0,
            "avg_ret": round(float(trades.ret.mean()), 6) if len(trades) else 0.0,
            "cash_ratio": round(cash_ratio, 4),
        },
    }
    (out / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 回测 benchmark  数据={data_dir.name}  "
          f"{nav.index.min().date()} ~ {nav.index.max().date()} ===")
    print(st.to_string(float_format=lambda x: f"{x:.4f}"))
    if len(trades):
        print(f"\n交易 {len(trades)} 笔 | 止损 {int((trades.reason == 'stop_loss').sum())} 次 | "
              f"胜率 {(trades.ret > 0).mean()*100:.1f}% | 平均单笔 {trades.ret.mean()*100:.2f}%")
    print(f"空仓天数占比 {cash_ratio*100:.1f}%")
    print(f"\n产出: {out}")

    # 出看板：baseline 写 reports/index.html，其它 run 写自己目录
    try:
        md = _dashboard_mod()
        md.write_run(out)
        print(f"看板: {md.dashboard_path(out)}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"看板生成失败（不影响回测）：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
