"""当日 bar 从哪来：日线优先 → 当日盘中报价 → 退回最近日线。

这条逻辑是 2026-11 那次事故的根因：报价还停在上一交易日，却被当成当日 bar 追加，
面板尾部多一根"副本"，RSRS 的 R² 被拉向 1，板块排序整体改写（MNST 变 APP）。
"""
import datetime as dt
from unittest.mock import patch

import pandas as pd

from fixtures import ET, ohlc_panel, quote
from quant.live import signal

SYMS = [f"S{i:02d}" for i in range(30)]      # ≥20 只，才会触发报价覆盖率判定
LAST_BAR = pd.Timestamp("2026-11-13")
MONDAY = pd.Timestamp("2026-11-16")


def _quotes(ts: dt.datetime, *, flat: bool = False) -> dict:
    if flat:                                  # 开盘前：high=low=上一交易日收盘
        return {s: quote(s, high=100.0, low=100.0, ts=ts) for s in SYMS}
    return {s: quote(s, ts=ts) for s in SYMS}


def _call(asof: pd.Timestamp, quotes: dict):
    px, hi, lo = ohlc_panel(SYMS, end=str(LAST_BAR.date()), days=80)
    with patch.object(signal.store, "load_panel", lambda **kw: px):
        return signal._append_live_row(px, hi, lo, quotes, asof)


def test_uses_daily_bar_when_it_exists():
    """信号日已有日线 → 直接用日线，报价不覆盖（报价是盘中快照，会把确定的收盘改小）。"""
    px, _, _, _, src, eff = _call(LAST_BAR, _quotes(dt.datetime(2026, 11, 13, 15, 30, tzinfo=ET)))

    assert src == "daily"
    assert eff == LAST_BAR
    assert px.index.max() == LAST_BAR
    assert float(px.at[LAST_BAR, "S00"]) == 100.0


def test_synthesizes_only_when_quote_belongs_to_that_day():
    px, _, _, _, src, eff = _call(MONDAY, _quotes(dt.datetime(2026, 11, 16, 15, 30, tzinfo=ET)))

    assert src == "quote"
    assert eff == MONDAY
    assert px.index.max() == MONDAY
    assert float(px.at[MONDAY, "S00"]) == 100.0


def test_falls_back_when_quote_is_from_previous_session():
    """报价的交易日 ≠ 信号日 → 退回最近一根真实日线，口径日跟着数据走。"""
    px, _, _, _, src, eff = _call(MONDAY, _quotes(dt.datetime(2026, 11, 13, 16, 0, tzinfo=ET)))

    assert src == "fallback"
    assert eff == LAST_BAR, "口径日必须退回 11-13，不能伪造 11-16 的 bar"
    assert px.index.max() == LAST_BAR
    assert MONDAY not in px.index


def test_falls_back_when_no_intraday_range():
    """时间戳是今天但 high=low=昨收（开盘前）→ 不能合成一个"点"当 bar。"""
    px, _, _, _, src, eff = _call(
        MONDAY, _quotes(dt.datetime(2026, 11, 16, 9, 30, tzinfo=ET), flat=True))

    assert src == "fallback"
    assert eff == LAST_BAR
    assert px.index.max() == LAST_BAR
