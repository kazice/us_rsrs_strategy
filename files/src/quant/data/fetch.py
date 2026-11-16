"""回填与每日增量。

增量策略：
1. 每只股票从 state 里取 last_date，往前多看 `incremental_lookback_days` 天，
   覆盖交易所迟到修正（Yahoo 偶尔会补昨天的数据）。
2. 拉回来做 upsert（按 date 去重、新值覆盖旧值）。
3. 若重叠区间收盘价相对偏差 > revision_tolerance，判定为拆股/分红导致的历史
   复权漂移，收集起来做单只全量重刷——否则增量拼出来的序列会断层。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Sequence

import pandas as pd

from quant.config import get_settings
from quant.data import store
from quant.data.sources.base import BarSource
from quant.data.sources.registry import build_chain, fetch_with_fallback

log = logging.getLogger(__name__)

SLACK_DAYS = 3


def _chunks(seq: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _group_by_start(
    plan: list[tuple[str, pd.Timestamp]], batch_size: int, slack_days: int = SLACK_DAYS
) -> list[list[tuple[str, pd.Timestamp]]]:
    """把起始日期相近的股票合到一批，减少请求数又不漏数据。"""
    plan = sorted(plan, key=lambda x: x[1])
    batches: list[list[tuple[str, pd.Timestamp]]] = []
    current: list[tuple[str, pd.Timestamp]] = []
    current_min: pd.Timestamp | None = None

    for item in plan:
        symbol, start = item
        too_late = current_min is not None and (start - current_min).days > slack_days
        if current and (len(current) >= batch_size or too_late):
            batches.append(current)
            current = []
            current_min = None
        if not current:
            current_min = start
        current.append(item)

    if current:
        batches.append(current)
    return batches


def _run(
    plan: list[tuple[str, pd.Timestamp]],
    end: pd.Timestamp,
    chain: list[BarSource],
    state: dict,
    batch_size: int,
    sleep_s: float,
) -> dict:
    import time

    stats = {"symbols": 0, "new_rows": 0, "failed": [], "revised": []}
    batches = _group_by_start(plan, batch_size)
    total = len(batches)

    for idx, batch in enumerate(batches, start=1):
        symbols = [s for s, _ in batch]
        start = min(s for _, s in batch)
        log.info("[%d/%d] 抓取 %d 只  %s ~ %s", idx, total, len(symbols), start.date(), end.date())

        frames, failed, source_map = fetch_with_fallback(
            symbols, start.date().isoformat(), end.date().isoformat(), chain=chain
        )

        for sym, df in frames.items():
            src_name = source_map.get(sym, "?")
            res = store.upsert_bars(
                sym, df, tolerance=get_settings().data.revision_tolerance, source=src_name
            )
            state[sym] = {
                "last_date": res.last_date,
                "last_close": res.last_close,
                "last_adj_close": res.last_adj_close,
                "rows": res.total_rows,
                "source": src_name,
                "status": "ok",
                "error": "",
                "updated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            }
            stats["new_rows"] += max(res.new_rows, 0)
            stats["symbols"] += 1
            if res.revised:
                stats["revised"].append(sym)
                log.warning("%s 检测到历史价格漂移 %.4f%%，需全量重刷", sym, res.max_rel_diff * 100)

        for sym in failed:
            prev = state.get(sym, {})
            state[sym] = {
                **prev,
                "status": "failed",
                "updated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            }
            stats["failed"].append(sym)

        store.write_state(state)
        if idx < total and sleep_s:
            time.sleep(sleep_s)

    return stats


def backfill(
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
    limit: int | None = None,
) -> dict:
    """全量回填指定区间。force=True 时先删掉已有文件重刷。"""
    cfg = get_settings()
    if symbols is None:
        from quant.data import universe

        symbols = universe.get_symbols()
    if limit:
        symbols = list(symbols)[:limit]

    start_ts = pd.Timestamp(start or cfg.data.start_date).normalize()
    end_ts = pd.Timestamp(end or date.today()).normalize()

    if force:
        for sym in symbols:
            path = store.bars_file(sym)
            if path.exists():
                path.unlink()

    plan = [(sym, start_ts) for sym in symbols]
    chain = build_chain()
    state = store.read_state()
    log.info("回填 %d 只，%s ~ %s，数据源链: %s", len(symbols), start_ts.date(), end_ts.date(),
             [s.name for s in chain])

    stats = _run(
        plan, end_ts, chain, state,
        batch_size=cfg.data.batch_size, sleep_s=cfg.data.sleep_between_batches,
    )
    log.info(
        "完成：成功 %d 只，新增 %d 行，失败 %d 只，需重刷 %d 只",
        stats["symbols"], stats["new_rows"], len(stats["failed"]), len(stats["revised"]),
    )
    return stats


def update(symbols: Sequence[str] | None = None, end: str | None = None) -> dict:
    """每日增量。可重复执行，结果幂等。"""
    cfg = get_settings()
    if symbols is None:
        from quant.data import universe

        symbols = universe.get_symbols()

    state = store.read_state()
    last = store.last_dates()
    end_ts = pd.Timestamp(end or date.today()).normalize()
    lookback = pd.Timedelta(days=cfg.data.incremental_lookback_days)

    plan: list[tuple[str, pd.Timestamp]] = []
    for sym in symbols:
        if sym in last.index and pd.notna(last[sym]):
            plan.append((sym, max(pd.Timestamp(last[sym]) - lookback, pd.Timestamp(cfg.data.start_date))))
        else:
            plan.append((sym, pd.Timestamp(cfg.data.start_date)))

    chain = build_chain()
    log.info("增量 %d 只，截止 %s", len(plan), end_ts.date())
    stats = _run(
        plan, end_ts, chain, state,
        batch_size=cfg.data.batch_size, sleep_s=cfg.data.sleep_between_batches,
    )

    if stats["revised"]:
        log.info("对 %d 只做全量重刷", len(stats["revised"]))
        refresh = backfill(symbols=stats["revised"], start=cfg.data.start_date, end=end, force=True)
        stats["refresh_failed"] = refresh["failed"]

    return stats
