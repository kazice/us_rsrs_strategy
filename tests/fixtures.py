"""测试用的合成数据工厂（不叫 test_*.py，不会被当成测试收集）。"""
import datetime as dt

import pandas as pd

from quant.live.quotes import Quote

ET = dt.timezone(dt.timedelta(hours=-5))
DEFAULT_END = "2026-11-13"


def quote(sym: str, price: float = 100.0, *, high: float | None = None,
          low: float | None = None, turnover_pct: float = 1.0,
          ts: dt.datetime | None = None) -> Quote:
    """一条当日快照。high/low 默认给 ±1%，ts 默认给 end 那天 15:30 ET。"""
    return Quote(
        symbol=sym, price=price, prev_close=price,
        high=price * 1.01 if high is None else high,
        low=price * 0.99 if low is None else low,
        volume=1e7, turnover_pct=turnover_pct, total_shares=1e9,
        ts=ts if ts is not None else dt.datetime(2026, 11, 13, 15, 30, tzinfo=ET),
        src="test",
    )


def ohlc_panel(symbols: list[str], *, end: str = DEFAULT_END, days: int = 80,
               price: float = 100.0) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """造 (px, hi, lo) 三个面板：px=复权收盘，hi/lo 为当日高低点。"""
    idx = pd.bdate_range(end=end, periods=days)
    px = pd.DataFrame(price, index=idx, columns=symbols, dtype="float64")
    hi = pd.DataFrame(price * 1.01, index=idx, columns=symbols, dtype="float64")
    lo = pd.DataFrame(price * 0.99, index=idx, columns=symbols, dtype="float64")
    return px, hi, lo
