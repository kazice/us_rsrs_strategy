"""Tiingo（第三备用源，需免费 token）。

免费档约 50 次/小时、1000 次/天、500 个唯一标的/月，且逐只请求，因此只适合
少量个股救急，不适合驱动 1000 只的批量回填。
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import pandas as pd
import requests

from quant.config import get_settings
from quant.data.sources.base import empty_bars

log = logging.getLogger(__name__)

BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"


class TiingoSource:
    name = "tiingo"

    def available(self) -> bool:
        cfg = get_settings().sources.tiingo
        return bool(cfg.enabled and cfg.token)

    def fetch(self, symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        cfg = get_settings()
        token = cfg.sources.tiingo.token
        proxies = {"http": cfg.proxy, "https": cfg.proxy} if getattr(cfg, "proxy", None) else None
        out: dict[str, pd.DataFrame] = {}

        for sym in symbols:
            params = {
                "startDate": pd.Timestamp(start).date().isoformat(),
                "endDate": pd.Timestamp(end).date().isoformat(),
                "format": "json",
                "token": token,
            }
            try:
                resp = requests.get(
                    BASE_URL.format(ticker=sym), params=params, timeout=60, proxies=proxies
                )
                if resp.status_code == 429:
                    log.warning("Tiingo 触发限速，停止本批剩余请求")
                    break
                resp.raise_for_status()
                rows = resp.json()
            except Exception as exc:  # 单只失败不影响整批
                log.debug("Tiingo %s 失败: %s", sym, exc)
                continue
            finally:
                time.sleep(0.3)

            if not rows:
                continue
            df = pd.DataFrame(rows)
            df = df.rename(
                columns={
                    "adjClose": "adj_close",
                    "adjOpen": "open",
                    "adjHigh": "high",
                    "adjLow": "low",
                    "divCash": "dividends",
                    "splitFactor": "stock_splits",
                }
            )
            df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None).dt.normalize()
            if "adj_close" not in df.columns:
                df["adj_close"] = df["close"]
            df["source"] = "tiingo"
            out[sym] = df if not df.empty else empty_bars()
        return out
