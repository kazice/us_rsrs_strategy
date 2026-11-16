"""数据源协议。所有源返回的 DataFrame 必须含 store.BARS_COLUMNS 里的字段名。"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

import pandas as pd

BAR_FIELDS = ["date", "open", "high", "low", "close", "adj_close", "volume", "dividends", "stock_splits"]


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_FIELDS)


@runtime_checkable
class BarSource(Protocol):
    name: str

    def available(self) -> bool:
        """密钥/开关是否满足，False 时该源被跳过。"""
        ...

    def fetch(self, symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """返回 {symbol: bars}，拉不到的符号不要出现在结果里。"""
        ...
