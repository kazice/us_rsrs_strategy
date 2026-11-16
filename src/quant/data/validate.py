"""行情质量校验：OHLC 逻辑、异常跳变、覆盖率。

单日涨跌幅超过 50% 除了真妖股，通常是拆股没调整干净或数据源错位，必须人工看一眼。
"""

from __future__ import annotations

import logging
from typing import Sequence

import pandas as pd

from quant.config import get_settings
from quant.data import store

log = logging.getLogger(__name__)

MAX_ABS_RETURN = 0.5


def check_bars(df: pd.DataFrame, symbol: str = "") -> list[str]:
    issues: list[str] = []
    if df.empty:
        return [f"{symbol}: 空数据"]

    if df[["open", "high", "low", "close"]].isna().any().any():
        issues.append(f"{symbol}: 存在空价格")
    if (df["close"] <= 0).any():
        issues.append(f"{symbol}: 存在非正收盘价")
    bad_high = (df["high"] < df[["open", "close"]].max(axis=1)).sum()
    bad_low = (df["low"] > df[["open", "close"]].min(axis=1)).sum()
    if bad_high:
        issues.append(f"{symbol}: high 低于 open/close {bad_high} 根")
    if bad_low:
        issues.append(f"{symbol}: low 高于 open/close {bad_low} 根")

    ret = df["adj_close"].pct_change()
    jumps = ret[ret.abs() > MAX_ABS_RETURN]
    if len(jumps):
        issues.append(f"{symbol}: {len(jumps)} 根单日涨跌超 {MAX_ABS_RETURN:.0%}（首根 {jumps.index[0]}）")
    return issues


def check_all(symbols: Sequence[str] | None = None) -> pd.DataFrame:
    symbols = list(symbols) if symbols else store.list_symbols()
    rows = []
    for sym in symbols:
        df = store.read_bars(sym)
        rows.append(
            {
                "symbol": sym,
                "rows": len(df),
                "first": df["date"].min() if len(df) else pd.NaT,
                "last": df["date"].max() if len(df) else pd.NaT,
                "issues": "; ".join(check_bars(df, sym)),
            }
        )
    report = pd.DataFrame(rows)
    if report.empty:
        return report

    # 覆盖率分母必须是「该股上市区间内的交易日数」，不是股票总数，也不是全历史交易日数
    # —— 2015 年上市的股票只有 10 年数据，用全历史当分母会把它误判成缺数据。
    calendar = store.trading_days()

    def _coverage(row) -> float:
        if pd.isna(row["first"]) or row["rows"] == 0:
            return 0.0
        span = ((calendar >= row["first"]) & (calendar <= row["last"])).sum()
        return float(row["rows"] / span) if span else 0.0

    report["coverage"] = report.apply(_coverage, axis=1)
    min_cov = 1 - get_settings().data.max_missing_ratio
    report["ok"] = (report["issues"] == "") & (report["coverage"] >= min_cov)
    return report.sort_values(["ok", "coverage"])


def summary(symbols: Sequence[str] | None = None) -> str:
    report = check_all(symbols)
    if report.empty:
        return "无数据"
    bad = report[~report["ok"]]
    latest = report["last"].max()
    stale = report[report["last"] < latest - pd.Timedelta(days=5)]
    return (
        f"共 {len(report)} 只，最新交易日 {pd.Timestamp(latest).date()}，"
        f"有问题 {len(bad)} 只，落后超过 5 个自然日 {len(stale)} 只"
    )
