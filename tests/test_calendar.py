"""交易日历与行情缺口计数。

``trading_lag_days`` 只数「最后一根 bar 与信号日之间」的缺口，不含信号日本身——
当天的 bar 本来就要用实时报价合成，所以「面板停在上一交易日」= 0，不是 1。
"""
import datetime as dt

import pandas as pd

from quant.live import calendar
from quant.live.signal import trading_lag_days


def test_lag_zero_when_panel_stops_at_previous_session():
    assert trading_lag_days(pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-16")) == 0
    assert trading_lag_days(pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-13")) == 0


def test_lag_counts_missing_trading_days_only():
    # 11-13(五) → 11-17(二)：中间缺 11-16(一)，算 1 天
    assert trading_lag_days(pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-17")) == 1
    # 11-13(五) → 11-30(一)：11-16..11-27 共 9 个交易日，其中 11-26 感恩节休市
    assert trading_lag_days(pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-30")) == 9


def test_holiday_is_not_counted_as_missing():
    """感恩节 11-26 休市：11-25 的 bar 拿到 11-27 用，缺口是 0。"""
    assert not calendar.is_trading_day(dt.date(2026, 11, 26))
    assert trading_lag_days(pd.Timestamp("2026-11-25"), pd.Timestamp("2026-11-27")) == 0


def test_early_close_day_detected():
    """感恩节次日早收 13:00 ET，收盘闸门不能写死 16:00。"""
    assert calendar.builtin_close_et(dt.date(2026, 11, 27)) == dt.time(13, 0)
    assert calendar.builtin_close_et(dt.date(2026, 11, 25)) == dt.time(16, 0)
    assert calendar.builtin_close_et(dt.date(2026, 11, 26)) is None      # 休市
