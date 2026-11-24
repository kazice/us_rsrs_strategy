"""实盘数据来源体检：成分、变更表、换手率、行情覆盖是否都对得上。

为什么单独一个入口
------------------
实盘出错的多数不是策略，而是数据：快照没刷新（候选池还是两周前的）、变更表停在几个月前
（9 月之后的调入调出全缺）、换手率面板混进越界值（排序分被放大几十倍）。这些都不会报错，
只会让选股悄悄偏掉。收盘后跑一次，几秒钟看完全部来源。

用法
----
    python -m scripts.audit_live_data
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from quant.data import store
from quant.live import calendar
from quant.live import pool as live_pool
from quant.live import signal as live_signal

OK, WARN, BAD = "✓", "⚠", "✗"
MAX_SNAPSHOT_LAG = 10      # 快照落后超过这么多交易日就该报警
MAX_PRICE_LAG = 3          # 行情/换手率落后超过这么多交易日 = 抓取停了
MAX_CHANGES_LAG = 90       # 官方变更表落后超过这么多自然日就该补
MAX_MISSING_BARS = 20      # 成分股缺行情的只数上限


def trading_days_between(a: dt.date, b: dt.date) -> int:
    if b <= a:
        return 0
    n, d = 0, a + dt.timedelta(days=1)
    while d <= b:
        n += int(calendar.is_trading_day(d))
        d += dt.timedelta(days=1)
    return n


def main() -> int:
    today = dt.date.today()
    rows: list[tuple[str, str, str, str]] = []      # 项目 / 状态 / 现状 / 说明
    problems = 0

    # ---- 1. 成分快照 ----
    try:
        mem = live_pool.load()
        as_of = live_pool.snapshot_asof(mem)
        lag = trading_days_between(as_of.date(), today) if as_of is not None else 999
        bad = as_of is None or lag > MAX_SNAPSHOT_LAG
        problems += int(bad)
        rows.append(("成分快照", BAD if bad else OK, f"{len(mem)} 只 / as of {as_of.date() if as_of else '—'}",
                     f"落后 {lag} 个交易日" + ("，已超上限，候选池可能含已剔除票" if bad else "")))
    except SystemExit as e:
        problems += 1
        rows.append(("成分快照", BAD, "缺失", str(e)))
        mem = pd.DataFrame(columns=["symbol", "sector_etf"])

    # ---- 2. 历史存档 & 变更日志 ----
    snaps = live_pool.history_snapshots()
    rows.append(("历史快照存档", OK if snaps else WARN,
                 f"{len(snaps)} 份" + (f"，最近 {snaps[-1][0].date()}" if snaps else ""),
                 "每天成功 refresh 落一份，PIT 还原优先用它" if snaps else "没有存档，历史时点只能靠变更表倒推"))

    ch = live_pool.read_changes()
    rows.append(("index_changes 日志", OK if len(ch) else WARN,
                 f"{len(ch)} 行" + (f"，最近 {ch['date'].max().date()}" if len(ch) else ""),
                 "refresh 自己 diff 出来的板块移动/增减" if len(ch) else "从未成功记录过（refresh 没落盘？）"))

    # ---- 3. 官方变更表 ----
    off = live_pool.read_official_changes()
    if off.empty:
        problems += 1
        rows.append(("官方变更表", BAD, "缺失", "PIT 倒推/推进无法工作"))
    else:
        last = off["date"].max().date()
        gap = (today - last).days
        bad = gap > MAX_CHANGES_LAG
        problems += int(bad)
        rows.append(("官方变更表", BAD if bad else OK, f"最后 {last}（{len(off)} 条事件）",
                     f"落后 {gap} 天" + ("，之后的调入调出都还原不出来" if bad else "")))

    # ---- 4. PIT 还原结果 ----
    if not mem.empty:
        pit_mem = live_pool.members_asof(pd.Timestamp(today))
        pit = pit_mem.attrs.get("pit") or {}
        moved = int(pit.get("rollback", 0)) + int(pit.get("advance", 0))
        rows.append(("PIT 还原（今天）", OK if not moved else WARN,
                     f"{len(pit_mem)} 只 / {pit.get('src', '?')}",
                     f"回滚 {pit.get('rollback', 0)} 只、推进 {pit.get('advance', 0)} 只"
                     + (f"、板块未知 {pit['unknown_sector']} 只" if pit.get("unknown_sector") else "")))

    # ---- 5. 本地行情与换手率面板的新鲜度 ----
    # 两者是同一批抓取任务写的，一起停摆的概率很高；停 8 天就等于用两周前的数据出信号。
    try:
        spy = store.load_panel(field="adj_close", symbols=["SPY"],
                               start=str(today - dt.timedelta(days=40)), end=str(today))
        last_bar = pd.Timestamp(spy.index.max()).date() if len(spy) else None
    except Exception:                                  # noqa: BLE001
        last_bar = None
    if last_bar is None:
        problems += 1
        rows.append(("本地行情", BAD, "取不到 SPY", "数据抓取可能整段失败"))
    else:
        lag = trading_days_between(last_bar, today)
        bad = lag > MAX_PRICE_LAG
        problems += int(bad)
        rows.append(("本地行情", BAD if bad else OK, f"最新 {last_bar}",
                     f"落后 {lag} 个交易日" + ("，抓取大概率停了" if bad else "")))

    tp = store.data_root() / "curated" / "fundamentals" / "turnover_daily.parquet"
    if not tp.exists():
        problems += 1
        rows.append(("换手率面板", BAD, "缺失", "排序分没有分母，选股会退化"))
    else:
        tv = pd.read_parquet(tp)
        tv["date"] = pd.to_datetime(tv["date"])
        v = pd.to_numeric(tv["turnover"], errors="coerce")
        over = int((v <= 0).sum() + (v > 1.0).sum())
        last = tv["date"].max().date()
        lag = trading_days_between(last, today)
        bad = bool(over) or lag > MAX_PRICE_LAG
        problems += int(bad)
        why = []
        if over:
            why.append(f"越界 {over} 行，先跑 repair_turnover_panel")
        if lag > MAX_PRICE_LAG:
            why.append(f"落后 {lag} 个交易日，20 日均值已经掺旧值")
        rows.append(("换手率面板", BAD if bad else OK,
                     f"{len(tv):,} 行 / 最新 {last}", "；".join(why) or "无越界、时效正常"))

    # ---- 6. 成分股行情覆盖 ----
    if not mem.empty:
        have = set(store.list_symbols())
        miss = [s for s in mem["symbol"].astype(str) if s not in have]
        bad = len(miss) > MAX_MISSING_BARS
        problems += int(bad)
        rows.append(("成分股行情", BAD if bad else OK, f"缺 {len(miss)}/{len(mem)} 只",
                     ("；".join(miss[:8]) + (" …" if len(miss) > 8 else "")) if miss else "全覆盖"))

    # ---- 7. 池层延展段（回测/看板用）----
    ext_dir = store.data_root() / "curated" / "sp500_pool"
    bars_ext = ext_dir / "bars_ext.parquet"
    if bars_ext.exists():
        be = pd.read_parquet(bars_ext, columns=["date", "security_id", "src"])
        rep = ext_dir / "extend_report.csv"
        bad_rows = pd.read_csv(rep) if rep.exists() else pd.DataFrame()
        if len(bad_rows):
            bad_rows = bad_rows[~bad_rows["status"].astype(str).str.startswith("ok")]
        n_bad = len(bad_rows)
        names = " ".join(f"{r.ticker}({r.status})" for r in bad_rows.head(6).itertuples())
        rows.append(("池层延展段", WARN if n_bad else OK,
                     f"{len(be):,} 行 / 最新 {pd.to_datetime(be['date']).max().date()}",
                     f"未延展 {n_bad} 只：{names}" if n_bad else "全部延展成功"))

    # ---- 输出 ----
    width = max(len(r[0]) for r in rows)
    print(f"\n实盘数据来源体检  {today}\n" + "-" * 78)
    for name, mark, now, why in rows:
        print(f"{mark} {name.ljust(width)}  {now}")
        if why:
            print(f"  {' ' * width}  {why}")
    print("-" * 78)
    print(f"结论：{problems} 项需要处理" + ("" if problems else "，数据来源正常"))

    # 明天开盘前的提示：怎么补
    if problems:
        print("\n补充路径：")
        print("  快照过期      → 让 CI 跑 paper_close_update（内部调 pool.refresh()）")
        print("  变更表过期    → 接 FMP historical-sp-500 或买 PIT 成分数据集")
        print("  换手率越界    → python -m scripts.repair_turnover_panel")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
