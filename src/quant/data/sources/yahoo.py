"""Yahoo Finance（yfinance）。

注意：Yahoo 的 chart 接口对裸请求返回 429，必须带浏览器 UA。yfinance 内部自己会
拿 cookie + 伪装 UA，因此直接用 yfinance，不要自己 requests 拼。仍要限速，
批量按 50~60 只一批，批间 sleep。
"""

from __future__ import annotations

import logging
from typing import Sequence

import pandas as pd

from quant.config import get_settings
from quant.data.sources.base import empty_bars

log = logging.getLogger(__name__)

_COLUMN_MAP = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "adj close": "adj_close",
    "adj_close": "adj_close",
    "volume": "volume",
    "dividends": "dividends",
    "stock splits": "stock_splits",
    "stock_splits": "stock_splits",
}


def _extract(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """从 yf.download 的返回里切出单只股票，兼容单层/MultiIndex 两种列结构。"""
    if raw is None or len(raw) == 0:
        return empty_bars()

    sub: pd.DataFrame | None = None
    if isinstance(raw.columns, pd.MultiIndex):
        first = raw.columns.get_level_values(0)
        second = raw.columns.get_level_values(1)
        if symbol in first:
            sub = raw[symbol]
        elif symbol in second:
            sub = raw.xs(symbol, axis=1, level=1)
    else:
        sub = raw

    if sub is None or len(sub) == 0:
        return empty_bars()

    sub = sub.copy()
    sub.columns = [str(c).strip().lower() for c in sub.columns]
    sub = sub.rename(columns=_COLUMN_MAP)
    sub = sub.loc[:, [c for c in sub.columns if c in set(_COLUMN_MAP.values())]]
    sub = sub.dropna(how="all")
    if sub.empty or "close" not in sub.columns:
        return empty_bars()

    sub = sub.reset_index()
    sub.columns = [str(c).strip().lower() for c in sub.columns]
    if "date" not in sub.columns:
        sub = sub.rename(columns={sub.columns[0]: "date"})
    for col in ("open", "high", "low", "close", "volume"):
        if col not in sub.columns:
            sub[col] = pd.NA
    if "adj_close" not in sub.columns:
        sub["adj_close"] = sub["close"]
    for col in ("dividends", "stock_splits"):
        if col not in sub.columns:
            sub[col] = 0.0
    sub["source"] = "yahoo"
    return sub


class YahooSource:
    name = "yahoo"

    def available(self) -> bool:
        return bool(get_settings().sources.yahoo.enabled)

    def fetch(self, symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        symbols = list(symbols)
        if not symbols:
            return {}

        start_ts = pd.Timestamp(start).normalize()
        end_ts = pd.Timestamp(end).normalize() + pd.Timedelta(days=1)  # yfinance end 为开区间

        raw = yf.download(
            tickers=symbols,
            start=start_ts.date().isoformat(),
            end=end_ts.date().isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )

        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = _extract(raw, sym)
            if not df.empty:
                out[sym] = df
        return out
