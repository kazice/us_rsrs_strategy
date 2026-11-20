"""模拟盘日志的查询入口：事后回答「那天为什么这么选 / 为什么没选 X / 哪天漏跑了」。

为什么需要它
------------
日志写得再全，没有查询入口就不会有人看。GitHub Actions 的 run log 90 天就过期，
真正的证据在 ``state/live/`` 里（CI 上是 data 分支的 ``state/live``）。

用法
----
python -m scripts.live_audit --list                     # 列出所有决策快照
python -m scripts.live_audit --date 2026-11-30          # 当天的完整决策
python -m scripts.live_audit --why AIG --date 2026-11-30  # 某只票为什么没入选
python -m scripts.live_audit --runs --since 2026-11-01  # 运行流水
python -m scripts.live_audit --gaps --since 2026-06-01  # 该跑却没跑的调仓日
python -m scripts.live_audit --replay --date 2026-11-30 # 用快照重算，验证可重现
python -m scripts.live_audit --stats 2026-11              # 当月运行统计

退出码：0 正常；1 发现问题（replay 不一致 / 有 gap）；2 参数或数据问题。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

from quant.live import journal
from quant.live.books import state_dir
from quant.live.quotes import Quote

REASON_TXT = {
    "": "入选",
    "small_mcap": "市值不达标",
    "turn_cap": "换手率超上限",
    "no_turnover": "无 20 日换手率",
    "no_turn120": "无 120 日换手率（不足 60 天）",
    "no_quote": "无当日报价",
    "no_local_data": "本地无行情",
    "not_in_top3": "不在板块排序分前 3",
    "gate_fail": "未过趋势五闸",
    "not_selected": "合格但被更靠前的板块抢先",
    "bear_regime": "熊市：只做板块 ETF，个股不参与",
    "selected_bear": "熊市 ETF 入选",
}


# ---------------------------------------------------------------- 快照定位
def _snapshot_dirs(day: str | None = None) -> list[Path]:
    root = state_dir() / journal.DECISIONS_DIR
    if not root.exists():
        return []
    # 快照是 decisions/<asof>_<run_id>/ 扁平一层；旧版按月分层的数据已迁移
    dirs = sorted(p for p in root.glob("*") if p.is_dir() and not p.name.endswith(".tmp"))
    if day:
        dirs = [p for p in dirs if p.name.startswith(day)]
    return dirs


def _latest_rebalance_dir() -> Path | None:
    for r in reversed(journal.all_runs()):
        if r.get("kind") == "rebalance" and r.get("decision_dir"):
            p = state_dir() / r["decision_dir"]
            if p.exists():
                return p
    dirs = _snapshot_dirs()
    return dirs[-1] if dirs else None


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------------------------------------------------------- 各子命令
def cmd_list(limit: int) -> int:
    dirs = _snapshot_dirs()
    if not dirs:
        print("还没有任何决策快照。")
        return 0
    print(f"{'快照目录':<44s} {'动作':<8s} {'标的':<7s} {'板块':<6s} {'推送'}")
    for p in dirs[-limit:]:
        d = _read_json(p / "decision.json")
        print(f"{p.relative_to(state_dir()).as_posix():<44s} "
              f"{str(d.get('action', ''))[:6]:<8s} {str(d.get('pick', '') or '空仓'):<7s} "
              f"{str(d.get('sector', '')):<6s} {d.get('notify_ok')}")
    print(f"\n共 {len(dirs)} 份，显示最近 {min(limit, len(dirs))} 份。")
    return 0


def cmd_date(day: str) -> int:
    dirs = _snapshot_dirs(day)
    if not dirs:
        print(f"{day} 没有决策快照。用 --runs --since {day} 看那天有没有跑、为什么没留快照。")
        return 2
    for p in dirs:
        meta = _read_json(p / "meta.json")
        dec = _read_json(p / "decision.json")
        print("=" * 76)
        print(f"快照：{p.relative_to(state_dir()).as_posix()}")
        print(f"运行：run_id={meta.get('run_id')} kind={meta.get('kind')} "
              f"dry_run={meta.get('dry_run')} sha={meta.get('git_sha') or '-'}")
        print(f"参数：{meta.get('params')}")
        print(f"池：{meta.get('pool')}  报价：{meta.get('quotes')}")
        print("-" * 76)
        print(f"动作：{dec.get('action')} —— {dec.get('detail')}")
        print(f"标的：{dec.get('pick') or '空仓'}  板块：{dec.get('sector')}  "
              f"价：{dec.get('price')}")
        hold = dec.get("holding")
        if hold:
            print(f"上期持仓：{hold.get('symbol')}（{hold.get('sector')}，"
                  f"{hold.get('entry_date')} @ {hold.get('entry_price')}），"
                  f"持有期收益 {dec.get('holding_return')}")
        else:
            print("上期持仓：无（空仓或首期）")
        print(f"推送：{dec.get('notify_ok')}")

        sfile = p / "sectors.csv"
        if sfile.exists():
            s = pd.read_csv(sfile, encoding="utf-8-sig").head(5)
            print("\n板块 RSRS 前 5：" + " / ".join(
                f"{r.sector} {r.rsrs:.2f}" for r in s.itertuples()))

        cfile = p / "candidates.csv"
        if cfile.exists():
            c = pd.read_csv(cfile, encoding="utf-8-sig")
            print(f"\n候选池 {len(c)} 只，入选 1 只。板块前 3 的下探过程：")
            by = "score" if "score" in c.columns else "turnover20"
            top = c[c["in_top3"] == True].sort_values(["sector_rank", by],
                                                      ascending=[True, False])
            if len(top):
                show = pd.DataFrame({
                    "板块": top["sector"],
                    "排名": top["sector_rank"].astype("Int64"),
                    "代码": top["symbol"],
                    "排序分": top[by].map(
                        lambda v: f"{v:.4f}" if pd.notna(v) else "-"),
                    "换手": top["turnover20"].map(
                        lambda v: f"{v:.2%}" if pd.notna(v) else "-"),
                    "市值(亿)": top["mcap"].map(
                        lambda v: f"{v / 1e8:,.0f}" if pd.notna(v) else "-"),
                    "五闸": top["gate_ok"] if "gate_ok" in top.columns else "",
                    "结论": top["reason"].fillna("").map(
                        lambda r: REASON_TXT.get(str(r), str(r))),
                })
                print(show.to_string(index=False))
            print("\n各原因计数：")
            cnt = c["reason"].fillna("").map(lambda r: REASON_TXT.get(str(r), str(r))).value_counts()
            for k, v in cnt.items():
                print(f"  {k:<22s} {v}")
            small = c[c["reason"] == "small_mcap"].sort_values("mcap", ascending=False)
            if len(small):
                print("\n离门槛最近的被剔除票（按市值降序，注意 500 亿附近的边缘票）：")
                print(small.head(8).assign(
                    市值_亿=lambda x: (x["mcap"] / 1e8).round(0))[
                    ["symbol", "市值_亿", "price"]].to_string(index=False))
        print()
    return 0


def cmd_why(symbol: str, day: str | None) -> int:
    sym = symbol.strip().upper()
    dirs = _snapshot_dirs(day) if day else ([_latest_rebalance_dir()] if _latest_rebalance_dir() else [])
    if not dirs:
        print("找不到可用的决策快照。")
        return 2
    hits = 0
    for p in dirs:
        cfile = p / "candidates.csv"
        if not cfile.exists():
            continue
        c = pd.read_csv(cfile, encoding="utf-8-sig")
        row = c[c["symbol"].astype(str).str.upper() == sym]
        if row.empty:
            continue
        hits += 1
        r = row.iloc[0]
        print(f"快照：{p.relative_to(state_dir()).as_posix()}")
        to = f"{float(r['turnover20']):.2%}" if pd.notna(r["turnover20"]) else "无"
        cap = f"{float(r['mcap']) / 1e8:.0f} 亿美元" if pd.notna(r["mcap"]) else "未知"
        src = str(r.get("mcap_src") or "")
        if cap != "未知" and src:
            cap += f"（来源 {src}）"
        rank = f"{int(r['sector_rank'])}" if pd.notna(r["sector_rank"]) else "-"
        t120 = (f"{float(r['turnover120']):.4f}"
                if "turnover120" in r.index and pd.notna(r.get("turnover120")) else "无")
        print(f"  {sym}  板块={r['sector']}  价={r['price']}  20日均换手={to}  "
              f"120日均换手={t120}  市值={cap}")
        if "gate_ok" in r.index:
            print(f"  趋势五闸 MA20={r.get('ma20')} 斜率={r.get('s20')} "
                  f"MA60={r.get('ma60')} 斜率={r.get('s60')} MA50={r.get('ma50')} "
                  f"MA200={r.get('ma200')} ADX={r.get('adx')} → 通过={r['gate_ok']}"
                  f"（{r.get('gate_why') or '-'}）")
        print(f"  板块排名={rank}  进入前3={r['in_top3']}  最终入选={r['selected']}")
        print(f"  结论：{REASON_TXT.get(str(r['reason']), r['reason'])}")
        if str(r["reason"]) == "small_mcap" and pd.notna(r["mcap"]):
            print(f"  距门槛还差 {(500e8 - float(r['mcap'])) / 1e8:.0f} 亿美元")
        print()
    if not hits:
        print(f"{sym} 不在这些快照的候选池里（可能是当日无行情、或已不在成分股名单）。")
        return 1
    return 0


def cmd_runs(since: str | None, limit: int) -> int:
    rows = journal.all_runs()
    if since:
        rows = [r for r in rows if r.get("ts_et", "")[:10] >= since]
    if not rows:
        print("没有运行流水。")
        return 0
    print(f"{'ts_ET':<26s} {'kind':<9s} {'status':<7s} {'标的':<7s} {'notify':<8s} reason")
    for r in rows[-limit:]:
        print(f"{str(r.get('ts_et', ''))[:25]:<26s} {str(r.get('kind', '')):<9s} "
              f"{str(r.get('status', '')):<7s} {str(r.get('pick', '') or '-'):<7s} "
              f"{str(r.get('notify', '')):<8s} {r.get('reason', '')}")
    bad = [r for r in rows if r.get("status") in ("abort", "error")]
    if bad:
        print(f"\n⚠ 中止/失败 {len(bad)} 次：")
        for r in bad[-10:]:
            print(f"  {r.get('ts_et')} {r.get('kind')} {r.get('status')} {r.get('reason')}")
    return 0


def cmd_gaps(since: str) -> int:
    """该有调仓记录的月末交易日却没有 ok 记录 → 静默失败。只查调仓（月频），周报不查。"""
    rows = journal.all_runs()
    ok_days = {r.get("asof") for r in rows
               if r.get("kind") == "rebalance" and r.get("status") == "ok"
               and not r.get("dry_run")}
    from quant.data import store
    cal = store.load_panel(field="close", symbols=["SPY"], start=since,
                           end=str(dt.date.today())).sort_index()
    if cal.empty:
        print("取不到交易日历，无法判断缺口。")
        return 2
    dates = list(cal.index)
    cutoff = pd.Timestamp(dt.date.today()) - pd.Timedelta(days=1)
    month_end = [d for i, d in enumerate(dates)
                 if d <= cutoff and (i == len(dates) - 1 or dates[i + 1].month != d.month)]
    missing = [d for d in month_end if d.strftime("%Y-%m-%d") not in ok_days]
    print(f"检查区间 {since} 起，共 {len(month_end)} 个月末交易日。")
    if missing:
        print(f"\n⚠ {len(missing)} 个调仓日没有成功记录（要么没跑，要么跑了但中止）：")
        for d in missing:
            day = d.strftime("%Y-%m-%d")
            same = [r for r in rows if r.get("asof") == day]
            why = "；".join(f"{r.get('status')}:{r.get('reason')}" for r in same) or "无任何运行记录"
            print(f"  {day}  {why}")
        return 1
    print("没有缺口。")
    return 0


def _quotes_from_csv(path: Path) -> dict[str, Quote]:
    df = pd.read_csv(path, encoding="utf-8-sig")
    out: dict[str, Quote] = {}
    for r in df.itertuples():
        ts = None
        if isinstance(r.ts, str) and r.ts:
            try:
                ts = dt.datetime.fromisoformat(r.ts)
            except ValueError:
                ts = None
        out[str(r.symbol)] = Quote(
            symbol=str(r.symbol), price=float(r.price), prev_close=float(r.prev_close),
            high=float(r.high), low=float(r.low), volume=float(r.volume),
            turnover_pct=float(r.turnover_pct), total_shares=float(r.total_shares),
            ts=ts, src=str(r.src))
    return out


def cmd_replay(day: str | None) -> int:
    """用快照里的报价重算一遍，与当时的 decision.json 比对。不一致就是口径漂移。"""
    dirs = _snapshot_dirs(day) if day else ([_latest_rebalance_dir()] if _latest_rebalance_dir() else [])
    if not dirs:
        print("找不到可回放的快照。")
        return 2
    from quant.live import signal as signal_mod

    bad = 0
    for p in dirs:
        qf = p / "quotes.csv"
        if not qf.exists():
            print(f"{p.name}：没有 quotes.csv，无法回放")
            bad += 1
            continue
        dec = _read_json(p / "decision.json")
        asof = pd.Timestamp(dec.get("asof"))
        quotes = _quotes_from_csv(qf)
        print(f"回放 {p.name}：报价 {len(quotes)} 只，asof {asof.date()} ……")
        try:
            # 允许用今天的全市场快照取股本：快照是逐日覆盖的，历史那天的已经没留档；
            # 用今天这份比退回到换手率反推更接近当时的真实股本（已知偏差，不算口径漂移）
            sig = signal_mod.compute(asof, quotes, allow_snapshot_ahead=True)
        except SystemExit as exc:
            print(f"  ✗ 重算失败：{exc}")
            bad += 1
            continue
        same = (sig.pick == (dec.get("pick") or ""))
        print(f"  当时 {dec.get('pick') or '空仓'} / 现在 {sig.pick or '空仓'}"
              f"　{'✓ 一致' if same else '✗ 不一致'}")
        if not same:
            bad += 1
            c = pd.read_csv(p / "candidates.csv", encoding="utf-8-sig") \
                if (p / "candidates.csv").exists() else pd.DataFrame()
            for s in {dec.get("pick"), sig.pick}:
                if not s or c.empty:
                    continue
                row = c[c["symbol"] == s]
                if len(row):
                    r = row.iloc[0]
                    print(f"    {s}: reason={r['reason']} 换手={r['turnover20']} "
                          f"市值={r['mcap'] / 1e8 if pd.notna(r['mcap']) else float('nan'):.0f}亿")
    return 0 if bad == 0 else 1


def cmd_stats(month: str) -> int:
    rows = [r for r in journal.all_runs() if r.get("ts_et", "").startswith(month)]
    if not rows:
        print(f"{month} 没有运行记录。")
        return 0
    print(f"{month} 共 {len(rows)} 次运行")
    st = pd.Series([r.get("status", "") for r in rows]).value_counts()
    for k, v in st.items():
        print(f"  status={k:<8s} {v}")
    nt = pd.Series([r.get("notify", "") for r in rows]).value_counts()
    for k, v in nt.items():
        print(f"  notify={k:<8s} {v}")
    bad = [r for r in rows if r.get("status") in ("abort", "error")]
    for r in bad:
        print(f"  ⚠ {r.get('ts_et')} {r.get('kind')} {r.get('reason')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘日志查询")
    ap.add_argument("--list", action="store_true", help="列出所有决策快照")
    ap.add_argument("--date", default="", help="某天的决策（YYYY-MM-DD）")
    ap.add_argument("--why", default="", help="某只票为什么没入选")
    ap.add_argument("--runs", action="store_true", help="运行流水")
    ap.add_argument("--gaps", action="store_true", help="该跑却没跑的调仓日")
    ap.add_argument("--replay", action="store_true", help="用快照重算并比对")
    ap.add_argument("--stats", default="", help="某月统计（YYYY-MM）")
    ap.add_argument("--since", default="2026-01-01", help="runs/gaps 的起始日")
    ap.add_argument("--limit", type=int, default=30, help="最多显示多少条")
    args = ap.parse_args()

    if args.list:
        return cmd_list(args.limit)
    if args.date and args.why:
        return cmd_why(args.why, args.date)
    if args.date and args.replay:
        return cmd_replay(args.date)
    if args.date:
        return cmd_date(args.date)
    if args.why:
        return cmd_why(args.why, None)
    if args.replay:
        return cmd_replay(None)
    if args.gaps:
        return cmd_gaps(args.since)
    if args.stats:
        return cmd_stats(args.stats)
    if args.runs:
        return cmd_runs(args.since, args.limit)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
