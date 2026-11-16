"""Alpaca Markets Data API v2（备用源，需免费 key）。

限制与口径提醒：
- 免费账户只允许 feed=iex（成交量偏低、部分小盘股无数据），sip 需付费。
- adjustment=all 返回的已经是复权价，因此 close 与 adj_close 同值；无法同时拿到
  原始价。只作为 Yahoo 失败时的兜底，不建议当主源。
"""

from __future__ import annotations

import logging
from typing import Sequence

import pandas as pd
import requests

from quant.config import get_settings
from quant.data.sources.base import empty_bars

log = logging.getLogger(__name__)

BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"


class AlpacaSource:
    name = "alpaca"

    def available(self) -> bool:
        cfg = get_settings().sources.alpaca
        return bool(cfg.enabled and cfg.key_id and cfg.secret)

    def _headers(self) -> dict:
        cfg = get_settings().sources.alpaca
        return {"APCA-API-KEY-ID": cfg.key_id, "APCA-API-SECRET-KEY": cfg.secret}

    def fetch(self, symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        cfg = get_settings()
        symbols = list(symbols)
        if not symbols:
            return {}

        proxies = {"http": cfg.proxy, "https": cfg.proxy} if getattr(cfg, "proxy", None) else None
        collected: dict[str, list[dict]] = {s: [] for s in symbols}
        page_token: str | None = None

        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": f"{pd.Timestamp(start).date().isoformat()}T00:00:00Z",
                "end": f"{pd.Timestamp(end).date().isoformat()}T23:59:59Z",
                "adjustment": "all",
                "feed": cfg.sources.alpaca.feed,
                "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                BASE_URL, headers=self._headers(), params=params, timeout=60, proxies=proxies
            )
            resp.raise_for_status()
            payload = resp.json()
            for sym, bars in (payload.get("bars") or {}).items():
                collected.setdefault(sym, []).extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        out: dict[str, pd.DataFrame] = {}
        for sym, bars in collected.items():
            if not bars:
                continue
            df = pd.DataFrame(bars).rename(
                columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
            )
            df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("America/New_York")
            df["date"] = df["date"].dt.tz_localize(None).dt.normalize()
            df["adj_close"] = df["close"]
            df["dividends"] = 0.0
            df["stock_splits"] = 0.0
            df["source"] = "alpaca"
            out[sym] = df if not df.empty else empty_bars()
        return out
