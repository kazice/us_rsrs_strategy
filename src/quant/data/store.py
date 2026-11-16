"""Parquet 行情存储 + DuckDB 查询视图 + 增量状态。

布局：
    data/curated/bars_daily/symbol=<TICKER>/part.parquet   文件内不含 symbol，靠 hive 分区列

设计取舍：每只股票一个 Parquet 全历史文件。1000 只 x 20 年约 500 万行，列裁剪后
单只读取 1~5ms；DuckDB 直接把整个目录当一张 `bars` 视图查，比 SQLite 快一个量级。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from quant.config import ensure_dirs, get_settings

BARS_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "stock_splits",
    "source",
]

_PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close"]


@dataclass
class UpsertStats:
    symbol: str
    new_rows: int = 0
    total_rows: int = 0
    revised: bool = False
    max_rel_diff: float = 0.0
    last_date: str | None = None
    last_close: float | None = None
    last_adj_close: float | None = None


def _fill_tail(stats: UpsertStats, df: pd.DataFrame) -> UpsertStats:
    """用最终序列的末行补全状态字段，避免调用方再读一次盘。"""
    stats.total_rows = len(df)
    if len(df):
        last = df.iloc[-1]
        stats.last_date = str(pd.Timestamp(last["date"]).date())
        stats.last_close = float(last["close"])
        stats.last_adj_close = float(last["adj_close"]) if pd.notna(last["adj_close"]) else None
    return stats


def data_root() -> Path:
    cfg = get_settings()
    ensure_dirs(cfg)
    return cfg.paths.data_path


def bars_dir() -> Path:
    return data_root() / "curated" / "bars_daily"


def bars_file(symbol: str) -> Path:
    return bars_dir() / f"symbol={symbol}" / "part.parquet"


def _normalize(df: pd.DataFrame, source: str | None = None) -> pd.DataFrame:
    """统一列名/类型/排序，供写入与合并使用。"""
    if df.empty:
        return pd.DataFrame(columns=BARS_COLUMNS)

    out = df.copy()
    if "date" not in out.columns:
        out = out.reset_index()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    out = out.rename(columns={"adjclose": "adj_close", "adj close": "adj_close", "split": "stock_splits"})

    for col in BARS_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA

    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    for col in _PRICE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0).astype("float64")
    out["dividends"] = pd.to_numeric(out["dividends"], errors="coerce").fillna(0.0).astype("float64")
    out["stock_splits"] = pd.to_numeric(out["stock_splits"], errors="coerce").fillna(0.0).astype("float64")
    out["source"] = source or out["source"].fillna("unknown")

    out = out[BARS_COLUMNS].dropna(subset=["date", "close"])
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return out


def read_bars(symbol: str) -> pd.DataFrame:
    path = bars_file(symbol)
    if not path.exists():
        return pd.DataFrame(columns=BARS_COLUMNS)
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, path)


def upsert_bars(
    symbol: str,
    new_df: pd.DataFrame,
    tolerance: float = 0.001,
    source: str = "unknown",
) -> UpsertStats:
    """合并新数据。返回是否检测到历史价格被修正（拆股/分红导致的复权漂移）。"""
    new_df = _normalize(new_df, source=source)
    old = read_bars(symbol)
    stats = UpsertStats(symbol=symbol)

    if new_df.empty:
        return _fill_tail(stats, old)

    if old.empty:
        _write_atomic(new_df, bars_file(symbol))
        stats.new_rows = len(new_df)
        return _fill_tail(stats, new_df)

    merged_check = old[["date", "close", "adj_close"]].merge(
        new_df[["date", "close", "adj_close"]], on="date", suffixes=("_old", "_new")
    )
    if not merged_check.empty:
        denominator = merged_check["close_old"].abs().replace(0, pd.NA)
        rel = ((merged_check["close_new"] - merged_check["close_old"]) / denominator).abs()
        stats.max_rel_diff = float(rel.max(skipna=True) or 0.0)
        stats.revised = stats.max_rel_diff > tolerance

    combined = pd.concat([old, new_df], ignore_index=True)
    combined = combined.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    _write_atomic(combined, bars_file(symbol))

    stats.new_rows = len(combined) - len(old)
    return _fill_tail(stats, combined)


def list_symbols() -> list[str]:
    root = bars_dir()
    if not root.exists():
        return []
    return sorted(p.name.split("=", 1)[1] for p in root.glob("symbol=*") if p.is_dir())


def duckdb_conn(read_only: bool = True):
    """返回挂了 `bars` 视图的 DuckDB 连接。目录为空时返回无视图连接。"""
    import duckdb

    con = duckdb.connect(database=":memory:", read_only=False)
    glob = (bars_dir() / "symbol=*" / "*.parquet").as_posix()
    if list_symbols():
        con.execute(
            f"CREATE OR REPLACE VIEW bars AS SELECT * FROM read_parquet('{glob}', "
            f"hive_partitioning=1, union_by_name=1)"
        )
    return con


def load_panel(
    field: str = "adj_close",
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读取宽表 panel：index=date，columns=symbol。"""
    if field not in BARS_COLUMNS:
        raise ValueError(f"未知字段: {field}")

    con = duckdb_conn()
    try:
        if not list_symbols():
            return pd.DataFrame()

        conditions = [f"{field} IS NOT NULL"]
        params: list = []
        if start:
            conditions.append("date >= ?")
            params.append(pd.Timestamp(start).date())
        if end:
            conditions.append("date <= ?")
            params.append(pd.Timestamp(end).date())
        if symbols:
            placeholders = ",".join(["?"] * len(symbols))
            conditions.append(f"symbol IN ({placeholders})")
            params.extend(list(symbols))

        sql = f"SELECT symbol, date, {field} AS value FROM bars WHERE " + " AND ".join(conditions)
        df = con.execute(sql, params).fetch_df()
    finally:
        con.close()

    if df.empty:
        return pd.DataFrame()
    panel = df.pivot(index="date", columns="symbol", values="value").sort_index()
    panel.index = pd.to_datetime(panel.index)
    return panel


def trading_days(start: str | None = None, end: str | None = None) -> pd.DatetimeIndex:
    """全市场交易日并集（去重）。用于决定因子取样日与回测日历。"""
    con = duckdb_conn()
    try:
        if not list_symbols():
            return pd.DatetimeIndex([])
        sql = "SELECT DISTINCT date FROM bars"
        conditions, params = [], []
        if start:
            conditions.append("date >= ?")
            params.append(pd.Timestamp(start).date())
        if end:
            conditions.append("date <= ?")
            params.append(pd.Timestamp(end).date())
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY date"
        df = con.execute(sql, params).fetch_df()
    finally:
        con.close()
    return pd.DatetimeIndex(pd.to_datetime(df["date"]))


def resample_dates(freq: str = "M", start: str | None = None) -> pd.DatetimeIndex:
    """取每个周期的最后一个交易日，作为调仓/取样的锚点。"""
    days = trading_days(start=start)
    if len(days) == 0:
        return days
    if freq.upper() == "D":
        return days
    key = days.to_period("W" if freq.upper() == "W" else "M")
    return pd.DatetimeIndex(pd.Series(days, index=key).groupby(level=0).max().values)


def last_dates() -> pd.Series:
    """每只股票的最新日期，用于增量。"""
    con = duckdb_conn()
    try:
        if not list_symbols():
            return pd.Series(dtype="datetime64[ns]")
        df = con.execute("SELECT symbol, max(date) AS last_date FROM bars GROUP BY symbol").fetch_df()
    finally:
        con.close()
    return pd.Series(pd.to_datetime(df["last_date"]).values, index=df["symbol"].values)


def missing_symbols(expected: Iterable[str]) -> list[str]:
    return sorted(set(expected) - set(list_symbols()))
