"""主备降级链：按配置顺序尝试，前一个源没拿到的符号自动流向下一个。"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import pandas as pd

from quant.config import get_settings
from quant.data.sources.alpaca import AlpacaSource
from quant.data.sources.base import BarSource
from quant.data.sources.tencent import TencentSource
from quant.data.sources.tiingo import TiingoSource
from quant.data.sources.yahoo import YahooSource

log = logging.getLogger(__name__)

_FACTORY = {
    "tencent": TencentSource,
    "yahoo": YahooSource,
    "alpaca": AlpacaSource,
    "tiingo": TiingoSource,
}


def build_chain(source_chain: Sequence[str] | None = None) -> list[BarSource]:
    cfg = get_settings()
    names = source_chain or cfg.data.source_chain
    chain: list[BarSource] = []

    for name in names:
        factory = _FACTORY.get(name)
        if factory is None:
            log.warning("未知数据源 %s，跳过", name)
            continue
        src = factory()
        if not src.available():
            log.info("数据源 %s 未启用或缺密钥，跳过", name)
            continue
        chain.append(src)

    if not chain:
        raise RuntimeError("没有任何可用数据源，检查 config/settings.yaml 与密钥环境变量")
    return chain


def fetch_with_fallback(
    symbols: Sequence[str],
    start: str,
    end: str,
    chain: list[BarSource] | None = None,
    retries: int | None = None,
    backoff_base: float | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, str]]:
    """返回 (成功的数据, 仍失败的符号, {symbol: 数据来源})。"""
    cfg = get_settings()
    chain = chain or build_chain()
    retries = retries or cfg.data.retries
    backoff_base = backoff_base or cfg.data.backoff_base

    frames: dict[str, pd.DataFrame] = {}
    source_map: dict[str, str] = {}
    remaining = list(dict.fromkeys(symbols))

    for src in chain:
        if not remaining:
            break
        for attempt in range(retries):
            try:
                got = src.fetch(remaining, start, end)
            except Exception as exc:
                wait = backoff_base * (2**attempt)
                log.warning("%s 第 %d 次失败(%s)，%.1fs 后重试", src.name, attempt + 1, exc, wait)
                time.sleep(wait)
                continue

            for sym, df in got.items():
                if df is not None and not df.empty:
                    frames[sym] = df
                    source_map[sym] = src.name
            remaining = [s for s in remaining if s not in frames]
            if got:
                break
            time.sleep(backoff_base * (2**attempt))

        if remaining:
            log.info("%s 未覆盖 %d 只，降级到下一个源", src.name, len(remaining))

    return frames, remaining, source_map
