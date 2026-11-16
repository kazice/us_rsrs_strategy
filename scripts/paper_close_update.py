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
    return 0


if __name__ == "__main__":
    sys.exit(main())
