"""收盘后数据更新：行情（yahoo）+ 当日换手率（腾讯报价），一步到位。

为什么合成一个入口
------------------
两件事有同一个前提——**必须收盘之后**：

- 盘中写 yahoo 日线会把当天的半根 bar 落进 bars_daily（虽然 live 信号会丢弃当日行，
  但回测/周报读到它就是脏数据）；
- 盘中写换手率会拿到「累计到此刻」的部分值，后续 20 日均值全被拉低。

所以闸门只有一道：``now ≥ 当日收盘 + 8 分钟``。冬令/夏令/早收日的收盘时间由
``quant.live.calendar`` 现算，workflow 里挂多个 cron 让它多唤几次、只有一次会通过。

跑什么
------
1. 行情：只更新策略用得到的 503 只（11 板块成分股 + 11 只板块 ETF + SPY），
   不走全市场 1000 只，省掉一半时间；
2. 换手率：腾讯报价的换手率字段就是「成交量 ÷ 当日总股本」，与 ws 面板同源同口径，
   按 (symbol, date) 幂等覆盖写回 parquet；
3. 全市场快照：Nasdaq 市值前 1000，给「股本取不到时用 market_cap / price 反推」兜底。
   build() 内部按 refresh_hours（默认 24h）判新旧，重复调用直接读缓存。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import pandas as pd

from quant.data import fetch, store, universe
from quant.data.sources.registry import build_chain
from quant.live import calendar, pool, quotes
from quant.live.signal import BENCH, SECTOR_ETFS

log = logging.getLogger(__name__)

TURNOVER_FILE = "curated/fundamentals/turnover_daily.parquet"
SETTLE_MIN = 8
FRESH_MIN_COVER = 0.95     # 成分股/换手率里「已更新到当日」的占比下限，低于此判本次运行失败
FRESH_LOOKBACK_DAYS = 10   # 找「应有交易日」时最多往回翻几天


def members() -> list[str]:
    """策略用到的全部标的：成分股 ∩ 本地行情池，再加 ETF 与基准。"""
    root = Path(__file__).resolve().parents[1]
    path = root / "config" / "ref" / "us_sector_holdings.csv"
    if not path.exists():
        raise SystemExit(f"成分股快照不存在：{path}")
    hold = pd.read_csv(path)
    syms = {s.strip().upper() for s in hold["symbol"].astype(str)}
    syms |= set(SECTOR_ETFS) | {BENCH}
    pool = set(store.list_symbols())
    return sorted(syms & pool) if pool else sorted(syms)


def append_turnover(day: dt.date, quote_map: dict[str, quotes.Quote]) -> int:
    rows = [(s, day, q.turnover_pct / 100.0)
            for s, q in quote_map.items() if q.turnover_pct > 0]
    if not rows:
        return 0
    path = store.data_root() / TURNOVER_FILE
    old = (pd.read_parquet(path) if path.exists()
           else pd.DataFrame(columns=["symbol", "date", "turnover", "src"]))
    new = pd.DataFrame(rows, columns=["symbol", "date", "turnover"])
    new["src"] = "ws"
    new["date"] = pd.to_datetime(new["date"])
    merged = (pd.concat([old, new], ignore_index=True)
              .drop_duplicates(["symbol", "date"], keep="last")
              .sort_values(["symbol", "date"]).reset_index(drop=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    merged.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    log.info("换手率面板 %d 行、%d 只，最新 %s", len(merged),
             merged["symbol"].nunique(), f"{merged['date'].max():%Y-%m-%d}")
    return len(new)


def expected_bar_date(day: dt.date) -> dt.date:
    """收盘更新之后，日线里应该已经有的最后一个交易日。"""
    d = day
    for _ in range(FRESH_LOOKBACK_DAYS):
        if calendar.is_trading_day(d):
            return d
        d -= dt.timedelta(days=1)
    return day


def check_freshness(day: dt.date, syms: list[str]) -> list[str]:
    """自检：日线与换手率是否真的更新到了 ``day``。返回问题清单（空 = 通过）。

    为什么必须自检：``fetch.update`` 对单只标的失败只记进 ``stats['failed']``，进程照样退 0；
    yahoo 也可能对整个市场返回上一交易日的 bar（或被限流）。这两种情况下 Actions 都是绿的、
    data 分支照常回写，直到下一个调仓日（最长 20 天后）才被 ``signal.data_lag_days`` 发现——
    那时已经错过了修复窗口。所以这里按标的逐个核对"最后一天有没有 bar"，不达标就把运行标红。

    只读，不改数据：即使自检失败，已抓到的部分仍然回写（半份数据比没有好），
    但退出码为 1，GitHub Actions 会把这次运行标成失败。
    """
    problems: list[str] = []
    want = expected_bar_date(day)
    if want != day:
        problems.append(f"{day} 不是交易日，按 {want} 核对")

    etfs = list(SECTOR_ETFS) + [BENCH]
    start = (want - dt.timedelta(days=FRESH_LOOKBACK_DAYS)).isoformat()
    panel = store.load_panel(field="close", symbols=syms, start=start, end=want.isoformat())
    stamp = pd.Timestamp(want)
    if panel.empty or stamp not in panel.index:
        last = f"{panel.index.max():%Y-%m-%d}" if len(panel) else "无"
        problems.append(f"日线面板里没有 {want} 这一行（面板最新 {last}）")
        have = pd.Series(dtype="bool")
    else:
        have = panel.loc[stamp].notna()

    etf_stale = [s for s in etfs if not bool(have.get(s, False))]
    if etf_stale:
        # ETF 是板块层的全部输入，缺一只整张板块排序就不可信，必须严格
        problems.append(f"板块 ETF 未更新到 {want}：{' '.join(etf_stale)}")

    stocks = [s for s in syms if s not in set(etfs)]
    cover = float(have.reindex(stocks).eq(True).mean()) if stocks else 1.0
    if cover < FRESH_MIN_COVER:
        stale = [s for s in stocks if not bool(have.get(s, False))]
        problems.append(f"成分股只有 {cover:.1%} 更新到 {want}（下限 {FRESH_MIN_COVER:.0%}），"
                        f"未更新 {len(stale)} 只：{' '.join(stale[:15])}"
                        f"{' ...' if len(stale) > 15 else ''}")

    path = store.data_root() / TURNOVER_FILE
    if path.exists():
        tv = pd.read_parquet(path)
        if len(tv):
            tv["date"] = pd.to_datetime(tv["date"])
            got = set(tv.loc[tv["date"] == stamp, "symbol"]) & set(syms)
            tv_cover = len(got) / len(syms) if syms else 1.0
            if tv_cover < FRESH_MIN_COVER:
                problems.append(f"换手率只有 {tv_cover:.1%} 的标的写到 {want}"
                                f"（面板最新 {tv['date'].max():%Y-%m-%d}）")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="收盘后更新行情与换手率")
    ap.add_argument("--now", default="", help="模拟当前时刻（测试用），缺省时区按 UTC")
    ap.add_argument("--date", default="", help="交易日（默认当前 ET 日期）")
    ap.add_argument("--force", action="store_true", help="跳过收盘闸门（仅测试）")
    ap.add_argument("--skip-bars", action="store_true", help="只更新换手率")
    ap.add_argument("--skip-pool", action="store_true",
                    help="不刷新成分股快照（SSGA 只在美区可达，本地跑就加上这个）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # 先把「实际启用了哪些数据源」打出来。缺密钥时降级链会静默少一层（只剩 yahoo），
    # 而 run log 只留 90 天，等出问题再回头查已经晚了。放在闸门之前，空跑也能看见。
    try:
        print(f"数据源链：{' → '.join(s.name for s in build_chain())}")
    except Exception as exc:                        # noqa: BLE001
        print(f"::warning::数据源链构建失败：{type(exc).__name__}（{exc}）")

    now = (dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))
    day = dt.date.fromisoformat(args.date) if args.date else now.astimezone(calendar.ET).date()

    if not calendar.is_trading_day(day):
        print(f"{day} 非交易日，退出。")
        return 0
    close_utc = calendar.close_utc(day)
    if close_utc is None:
        print(f"{day} 取不到收盘时间，退出。")
        return 0
    ready = close_utc + dt.timedelta(minutes=SETTLE_MIN)
    if not args.force and now < ready:
        print(f"未到收盘结算时刻（收盘 {close_utc:%H:%M}Z，可用 {ready:%H:%M}Z，"
              f"当前 {now:%H:%M}Z），退出。")
        return 0
    print(f"收盘更新：{day}（收盘 {close_utc:%H:%M}Z）")

    # --- 1. 成分股快照：标普 500 的调入调出 ---
    fresh: list[str] = []
    if args.skip_pool:
        print("跳过成分股刷新（--skip-pool）")
    else:
        res = pool.refresh(today=day)
        if not res.ok:
            print(f"::warning::候选池刷新失败：{res.message}（沿用旧快照，"
                  f"推送里会显示快照年龄）")
        else:
            print(res.message)
            if res.changed:
                print(f"  纳入 {' '.join(res.added[:20])}")
                print(f"  调出 {' '.join(res.removed[:20])}")
                print(f"  换板块 {' '.join(res.moved[:20])}")
            fresh = res.new_symbols

    syms = members()
    print(f"标的 {len(syms)} 只")

    # --- 2. 新纳入的票补历史行情（不补就进不了候选，会被静默丢掉） ---
    if fresh:
        start = (day - dt.timedelta(days=520)).isoformat()
        print(f"新纳入 {len(fresh)} 只缺本地行情，回补 {start} 起的历史：{' '.join(fresh[:20])}")
        try:
            bf = fetch.backfill(symbols=fresh, start=start, end=day.isoformat())
            print(f"  补行情：成功 {bf['symbols']} 只，新增 {bf['new_rows']} 行，"
                  f"失败 {len(bf['failed'])} 只")
        except Exception as exc:                    # noqa: BLE001
            print(f"::warning::新票补行情失败：{type(exc).__name__}（下个调仓日它们仍是缺数据）")
        syms = members()

    if not args.skip_bars:
        # yahoo 日线：只补最近几天，避免每天全量拉
        stats = fetch.update(symbols=syms, end=day.isoformat())
        print(f"行情：更新 {stats['symbols']} 只，新增 {stats['new_rows']} 行，"
              f"失败 {len(stats['failed'])} 只")
        if stats["failed"]:
            print(f"  失败：{' '.join(stats['failed'][:20])}")

    # --- 3. 全市场快照：市值 / 价格兜底（股本取不到时用 market_cap / price 反推） ---
    # 失败不中断：快照只是兜底，旧的那份继续用，mcap 门槛会自动退化到只靠换手率反推
    try:
        snap = universe.build()
        asof = str(snap["asof"].iloc[0]) if "asof" in snap.columns and len(snap) else "?"
        print(f"全市场快照：{len(snap)} 行，as of {asof}")
    except Exception as exc:                        # noqa: BLE001
        print(f"::warning::全市场快照刷新失败：{type(exc).__name__}（沿用旧快照）")

    quote_map, notes = quotes.fetch_quotes(syms)
    if len(quote_map) < len(syms) * 0.5:
        print(f"::error::报价只拿到 {len(quote_map)}/{len(syms)}，换手率未写入")
        return 2
    n = append_turnover(day, quote_map)
    print(f"换手率：写入 {n} 行（{day}）")
    if notes:
        print(f"  来源异常 {len(notes)} 只：{list(notes)[:8]}")

    # --- 4. 自检：数据是不是真的更新到了今天 ---
    # 这一步是给「静默失败」兜底的：抓取环节失败不会中断进程，只有这里会把运行标红。
    problems = check_freshness(day, syms)
    if problems:
        for p in problems:
            print(f"::error::{p}")
        print(f"::error::数据完整性自检未通过（{len(problems)} 项）：已抓到的部分照常回写，"
              f"但请**当天**补齐，别等下一个调仓日才发现（迟了最长 20 天）")
        return 1
    print(f"自检通过：日线与换手率均已覆盖 {expected_bar_date(day)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
