"""美股交易日历与当日收盘时间。

为什么不能把时间写死
--------------------
1. **夏令时**：16:00 ET 在 EDT 是 20:00 UTC、在 EST 是 21:00 UTC，一年切两次；
2. **早收日**：感恩节次日、7/3、平安夜都是 13:00 ET 收盘；
3. **假日**：NYE、MLK、总统日、Good Friday、Memorial、Juneteenth、独立日、Labor、感恩节、圣诞。

因此每次都按「日期 → 当日收盘时间 → 转 UTC」现算，不缓存跨日的结论。

三级来源（优先级从高到低）
--------------------------
1. 环境变量 `TA_MARKET_CLOSE_ET`（如 `15:00`），人工兜底；
2. 内置规则（本文件，覆盖假日与早收日）；
3. 联网源（可选，`--online` 时才查，失败自动退回内置）。

对外接口
--------
    is_trading_day(d)            -> bool
    session_close_et(d)          -> (time, source_str)
    close_utc(d)                 -> datetime | None
    occasion_for(d)              -> Occasion
    in_window(now, occ, ...)     -> bool

`Occasion.trigger_utc` = 收盘时间 − `TRIGGER_LEAD_MIN`（默认 30 分钟）。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
TRIGGER_LEAD_MIN = 30       # 收盘前多少分钟决策


# ---------------------------------------------------------------- 假日


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """某月第 n 个星期 weekday（weekday: 0=Mon）。"""
    d = dt.date(year, month, 1)
    off = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=off + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(d: dt.date) -> dt.date:
    """周六顺延到周五、周日顺延到周一。"""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def holidays(year: int) -> frozenset[dt.date]:
    out = {
        _observed(dt.date(year, 1, 1)),                    # New Year
        _nth_weekday(year, 1, 0, 3),                       # MLK
        _nth_weekday(year, 2, 0, 3),                       # Presidents
        _easter(year) - dt.timedelta(days=2),              # Good Friday
        _last_weekday(year, 5, 0),                         # Memorial
        _observed(dt.date(year, 6, 19)),                   # Juneteenth
        _observed(dt.date(year, 7, 4)),                    # Independence
        _nth_weekday(year, 9, 0, 1),                       # Labor
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(dt.date(year, 12, 25)),                  # Christmas
    }
    return frozenset(out)


def early_closes(year: int) -> frozenset[dt.date]:
    """13:00 ET 收盘的日子。"""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out = {thanksgiving + dt.timedelta(days=1)}            # 黑色星期五
    july4 = _observed(dt.date(year, 7, 4))
    if july4.weekday() < 5 and july4.month == 7 and july4.day <= 4:
        eve = july4 - dt.timedelta(days=1)
        if eve.weekday() < 5:
            out.add(eve)                                   # 7/3
    xmas = _observed(dt.date(year, 12, 25))
    eve = xmas - dt.timedelta(days=1)
    if eve.weekday() < 5:
        out.add(eve)                                       # 12/24
    return frozenset(d for d in out if d.weekday() < 5)


# ---------------------------------------------------------------- 基础判定


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def session_close_et(d: dt.date) -> tuple[dt.time | None, str]:
    """当日收盘时间（ET）与来源说明。非交易日返回 (None, 原因)。"""
    ov = os.getenv("TA_MARKET_CLOSE_ET", "").strip()
    if not is_trading_day(d):
        return None, "holiday/weekend"
    if ov:
        try:
            hh, mm = ov.split(":")[:2]
            return dt.time(int(hh), int(mm)), f"override TA_MARKET_CLOSE_ET={ov}"
        except (ValueError, IndexError):
            pass
    if d in early_closes(d.year):
        return EARLY_CLOSE, "early_close"
    return REGULAR_CLOSE, "regular"


def close_utc(d: dt.date) -> dt.datetime | None:
    t, _ = session_close_et(d)
    if t is None:
        return None
    return dt.datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


# ---------------------------------------------------------------- 事件


@dataclass
class Occasion:
    day: dt.date
    is_trading: bool
    close_et: dt.time | None
    close_utc: dt.datetime | None
    trigger_utc: dt.datetime | None
    reason: str


def occasion_for(d: dt.date, lead_min: int = TRIGGER_LEAD_MIN) -> Occasion:
    """给出某日的收盘时间与触发时刻（收盘 − lead_min）。"""
    t, why = session_close_et(d)
    if t is None:
        return Occasion(d, False, None, None, None, why)
    cu = dt.datetime.combine(d, t, tzinfo=ET).astimezone(UTC)
    tu = cu - dt.timedelta(minutes=lead_min)
    return Occasion(d, True, t, cu, tu, why)


def now_et() -> dt.datetime:
    return dt.datetime.now(UTC).astimezone(ET)


def prev_trading_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def month_end_trading_day(d: dt.date) -> dt.date:
    """d 所在自然月的最后一个交易日。"""
    nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    cur = nxt - dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= dt.timedelta(days=1)
    return cur


def week_end_trading_day(d: dt.date) -> dt.date:
    """d 所在自然周的最后一个交易日（通常周五，假日则提前）。"""
    cur = d
    while cur.weekday() != 4:
        cur += dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= dt.timedelta(days=1)
    return cur


def in_window(now: dt.datetime, occ: Occasion, early_min: int = 5, late_min: int = 40) -> bool:
    """现在是否在「触发时刻 −early / +late」窗口内。早到会让换手率被低估，故余量小。"""
    if occ.trigger_utc is None:
        return False
    delta = (now.astimezone(UTC) - occ.trigger_utc).total_seconds() / 60
    return -early_min <= delta <= late_min
