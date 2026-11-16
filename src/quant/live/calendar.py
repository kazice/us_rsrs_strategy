"""美股交易日历与「当日收盘时间」（ET）。

为什么不能把收盘时间写死
------------------------
调度要落在「收盘前 30 分钟」，但收盘时间不是常量：

- 夏令时：16:00 ET = 20:00 UTC（EDT）/ 21:00 UTC（EST），一年切两次；
- 早收日：感恩节次日、7/3、平安夜是 13:00 ET（提前 3 小时）；
- 节假日根本不开市；偶发休市（国葬等）无法用规则预知；
- 交易所若延长/缩短交易时段，规则表当天就过期。

因此收盘时间按三层取，优先级从高到低：

1. ``TA_MARKET_CLOSE_ET`` 环境变量（如 ``15:00``）——人工兜底，改时长时不用改代码；
2. 联网日历接口（GitHub Actions 在美区才通，8 秒超时即放弃）；
3. 内置规则（NYSE 假日 + 早收日，2024~2030 适用）。

三者冲突时**以更早的收盘时间为准**（宁可早跑一次、不可错过窗口），并打印告警。
所有判断都基于 ET 日期，UTC 换算交给 zoneinfo 处理，调用方不要自己做偏移。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from quant.config import get_settings

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
OPEN_TIME = dt.time(9, 30)

_ONLINE_TIMEOUT = 8.0


# --------------------------------------------------------------------------- #
# 内置规则
# --------------------------------------------------------------------------- #
def _observed(d: dt.date) -> dt.date:
    """美股惯例：落在周六的假日提前到周五，落在周日的推后到周一。"""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """某月第 n 个星期 weekday（weekday: 周一=0）。"""
    d = dt.date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm。只用来反推耶稣受难日（Good Friday）。"""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset[dt.date]:
    """该年 NYSE 全天休市日。Juneteenth 自 2022 年起。"""
    out = {
        _observed(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),      # MLK
        _nth_weekday(year, 2, 0, 3),      # 华盛顿诞辰
        _easter(year) - dt.timedelta(days=2),   # Good Friday
        _last_weekday(year, 5, 0),        # 阵亡将士
        _observed(dt.date(year, 6, 19)),  # Juneteenth
        _observed(dt.date(year, 7, 4)),   # 独立日
        _nth_weekday(year, 9, 0, 1),      # 劳工节
        _nth_weekday(year, 11, 3, 4),     # 感恩节
        _observed(dt.date(year, 12, 25)),  # 圣诞
    }
    return frozenset(out)


@lru_cache(maxsize=32)
def early_closes(year: int) -> frozenset[dt.date]:
    """13:00 ET 提前收盘日：感恩节次日、7/3（7/4 为工作日时）、平安夜（工作日时）。"""
    out: set[dt.date] = set()
    # 感恩节次日（周五）
    thanks = _nth_weekday(year, 11, 3, 4)
    fri = thanks + dt.timedelta(days=1)
    if fri.weekday() == 4 and fri not in holidays(year):
        out.add(fri)
    # 7/3：仅当 7/4 本身是工作日（即独立日未被挪到 7/3 或 7/5）
    jul3 = dt.date(year, 7, 3)
    if jul3.weekday() < 5 and _observed(dt.date(year, 7, 4)) == dt.date(year, 7, 4):
        out.add(jul3)
    # 平安夜：仅当 12/25 是工作日（未被挪走）
    dec24 = dt.date(year, 12, 24)
    if dec24.weekday() < 5 and _observed(dt.date(year, 12, 25)) == dt.date(year, 12, 25):
        out.add(dec24)
    return frozenset(out)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def builtin_close_et(d: dt.date) -> dt.time | None:
    """内置规则给出的收盘时间；非交易日返回 None。"""
    if not is_trading_day(d):
        return None
    return EARLY_CLOSE if d in early_closes(d.year) else REGULAR_CLOSE


def prev_trading_day(d: dt.date) -> dt.date:
    cur = d - dt.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur -= dt.timedelta(days=1)
    raise RuntimeError(f"{d} 前 30 天内找不到交易日，日历数据异常")


def next_trading_day(d: dt.date) -> dt.date:
    cur = d + dt.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur += dt.timedelta(days=1)
    raise RuntimeError(f"{d} 后 30 天内找不到交易日，日历数据异常")


# --------------------------------------------------------------------------- #
# 联网校验
# --------------------------------------------------------------------------- #
def _fetch_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nasdaq.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=_ONLINE_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:                        # 网络/接口结构变化都不许中断主流程
        # 带出状态码：403 是封 UA/IP，404 是端点搬家，处理方式完全不同
        code = getattr(exc, "code", None)
        log.info("日历接口不可用 %s：%s%s", url, type(exc).__name__,
                 f" HTTP {code}" if code else "")
        return None


def _parse_nasdaq_hours(payload: dict | None) -> dict[str, dict]:
    """把 Nasdaq 日历返回整理成 {'2026-11-27': {'status': 'closed'|'early', ...}}。

    接口字段可能随版本变化，这里只做「能认出来就用、认不出来就忽略」的宽松解析，
    绝不因为解析失败而影响主流程。
    """
    if not payload:
        return {}
    rows = payload.get("data") or payload.get("calendar") or {}
    if isinstance(rows, dict):
        rows = rows.get("holidays") or rows.get("tradingHours") or []
    out: dict[str, dict] = {}
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict):
            continue
        day = (item.get("date") or item.get("tradeDate") or "")[:10]
        if not day:
            continue
        status = str(item.get("status") or item.get("marketStatus") or "").lower()
        hours = item.get("tradingHours") or item.get("hours") or {}
        close = ""
        if isinstance(hours, dict):
            close = str(hours.get("close") or hours.get("marketClose") or "")
        if "closed" in status or "holiday" in status:
            out[day] = {"status": "closed"}
        elif close:
            out[day] = {"status": "early" if close[:5] not in ("16:00", "4:00") else "regular",
                        "close": close}
    return out


def _alpaca_close_et(d: dt.date) -> tuple[dt.time | None, str] | None:
    """Alpaca 交易日历取当日收盘。没配密钥返回 None（= 这个源不可用）。

    为什么加这个源：Nasdaq 的 calendar 接口从 2026-11 起对非常规请求整体 403，
    本地和 Actions 都拿不到，早收日就只能靠内置规则（只覆盖感恩节次日/7-3/平安夜）。
    Alpaca 的 /v2/calendar 直接给每天的 close，早收日就是 13:00，正好是我们需要的。
    """
    try:
        cfg = get_settings().sources.alpaca
        key, secret = (cfg.key_id or "").strip(), (cfg.secret or "").strip()
    except Exception:                               # noqa: BLE001  配置缺失不该影响主流程
        return None
    if not key or not secret:
        return None

    # 免费账户的密钥一般配在 paper 环境，两个域名都试一次
    for host in ("https://api.alpaca.markets", "https://paper-api.alpaca.markets"):
        url = f"{host}/v2/calendar?start={d.isoformat()}&end={d.isoformat()}"
        try:
            req = urllib.request.Request(url, headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=_ONLINE_TIMEOUT) as resp:
                rows = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:                    # noqa: BLE001
            log.info("Alpaca 日历不可用 %s：%s", host,
                     getattr(exc, "code", type(exc).__name__))
            continue
        if not isinstance(rows, list):
            continue
        if not rows:
            return None, "alpaca 判定当日休市"
        close = str((rows[0] or {}).get("close") or "")
        if len(close) >= 5 and close[:2].isdigit():
            return dt.time(int(close[:2]), int(close[3:5])), f"alpaca 收盘 {close} ET"
        return None
    return None


def online_close_et(d: dt.date) -> tuple[dt.time | None, str]:
    """联网取当日收盘时间。返回 (收盘时间 or None, 来源说明)。

    只有拿到「这一天」的确定信息才返回时间：接口说休市、或日期对不上，一律返回 None，
    由调用方与内置规则合并。
    """
    urls = [
        f"https://api.nasdaq.com/api/calendar/holidays?dates={d:%Y-%m}",
        f"https://api.nasdaq.com/api/calendar/holidays?year={d.year}",
    ]
    for url in urls:
        table = _parse_nasdaq_hours(_fetch_json(url))
        hit = table.get(d.isoformat())
        if not hit:
            continue
        if hit["status"] == "closed":
            return None, f"nasdaq:{url.split('/')[-1]} 判定休市"
        close = hit.get("close", "")
        if len(close) >= 5 and close[:2].isdigit():
            hh, mm = int(close[:2]), int(close[3:5])
            return dt.time(hh, mm), f"nasdaq:{close} ET"

    # Nasdaq 拿不到时退到 Alpaca（需密钥），再不行由内置规则兜底
    hit = _alpaca_close_et(d)
    if hit is not None:
        return hit
    return None, "联网日历不可用"


def session_close_et(d: dt.date) -> tuple[dt.time | None, str]:
    """当日收盘时间（ET）+ 依据。非交易日返回 (None, 原因)。

    合并规则：人工覆盖 > 联网 > 内置；联网与内置不一致时取**更早**的那个。
    """
    override = os.getenv("TA_MARKET_CLOSE_ET", "").strip()
    builtin = builtin_close_et(d)

    online, why = online_close_et(d)
    chosen = builtin
    source = "内置规则" if builtin else "内置规则判定休市"

    if online is not None:
        if builtin is None:
            chosen, source = online, why
            log.warning("联网日历说 %s 开市（%s），内置规则说休市 —— 采用联网结果", d, why)
        elif online < builtin:
            chosen, source = online, why
            log.warning("联网日历收盘 %s 早于内置 %s，采用更早的 —— %s", online, builtin, why)
        else:
            source = f"{why}（与内置一致）"
    elif "休市" in why and builtin is not None:
        log.warning("联网日历说 %s 休市，内置规则说开市 —— 保守起见按休市处理", d)
        chosen, source = None, why

    if override:
        try:
            hh, mm = override.split(":")
            chosen, source = dt.time(int(hh), int(mm)), f"人工覆盖 TA_MARKET_CLOSE_ET={override}"
        except ValueError:
            log.warning("TA_MARKET_CLOSE_ET=%r 无法解析，忽略", override)

    return chosen, source


def close_utc(d: dt.date) -> dt.datetime | None:
    """当日收盘对应的 UTC 时刻（带时区）。"""
    t, _ = session_close_et(d)
    if t is None:
        return None
    return dt.datetime.combine(d, t, tzinfo=ET).astimezone(dt.timezone.utc)


def now_et() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(ET)


# --------------------------------------------------------------------------- #
# 调仓/推送日历
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Occasion:
    """一次推送的场合。

    kind: ``rebalance`` = 月末最后交易日（要换仓）；``week`` = 周内最后交易日（只报持仓）；
          ``none`` = 今天不推。
    """

    kind: str
    date: dt.date
    close_et: dt.time | None
    trigger_utc: dt.datetime | None
    reason: str

    @property
    def should_push(self) -> bool:
        return self.kind in ("rebalance", "week")


def occasion_for(d: dt.date) -> Occasion:
    """判断某天是不是调仓日 / 周末最后交易日，并给出「收盘前 30 分钟」的 UTC 触发时刻。"""
    close, why = session_close_et(d)
    if close is None:
        return Occasion("none", d, None, None, f"{d} 非交易日（{why}）")

    trigger = (dt.datetime.combine(d, close, tzinfo=ET)
               - dt.timedelta(minutes=30)).astimezone(dt.timezone.utc)

    # 月末最后交易日：下一个交易日的月份与今天不同
    nxt = next_trading_day(d)
    if nxt.month != d.month:
        return Occasion("rebalance", d, close, trigger, f"{d} 是 {d.month} 月最后一个交易日")

    # 周内最后交易日：下一个交易日的 ISO 周与今天不同
    if nxt.isocalendar()[:2] != d.isocalendar()[:2]:
        return Occasion("week", d, close, trigger, f"{d} 是本周最后一个交易日")

    # close 要透传：原来这里塞 None，日志就打成「收盘 None；触发 20:30」自相矛盾
    return Occasion("none", d, close, trigger, f"{d} 既非月末也非周末最后交易日")


def in_execution_window(now: dt.datetime, occasion: Occasion,
                        early_min: int = 5, late_min: int = 40) -> bool:
    """现在是否落在「触发时刻 −early / +late 分钟」的窗口内。

    窗口必须不对称：
    - 早到是**有害**的（还没到 15:30，当天成交量没走完，换手率会被系统性低估），所以只给
      5 分钟余量；
    - 迟到是**无害**的（只是晚几分钟知道买什么），给到 40 分钟，用来吃掉 GitHub Actions
      schedule 普遍存在的 5~30 分钟排队延迟。
    窗口内重复执行由账本的幂等键拦住，不会重复推送。
    """
    if occasion.trigger_utc is None:
        return False
    delta = (now.astimezone(dt.timezone.utc) - occasion.trigger_utc).total_seconds() / 60
    return -early_min <= delta <= late_min
