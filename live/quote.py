"""实时报价（新浪美股）。

两个用途
--------
1. **止损**：``bars_daily`` 是收盘后才更新的，15:30 ET 判断止损时表里最新一行
   还是昨天，必须拿实时价；
2. **合成当日 bar**：收盘前 30 分钟决策，当天这根 bar 还没生成，但信号必须用
   「今天」的价格算（回测信号日 d 用的是 d 的收盘价），所以用实时价拼一根。

字段布局（``hq.sinajs.cn/list=gb_aapl``，38 段）
------------------------------------------------
    1 现价 | 3 时间戳 | 5 开盘 | 6 最高 | 7 最低
    10 当日累计成交量(股) | 12 总市值(美元) | 19 总股本(股)

返回的是**未复权**价，要乘 ``upload/adjust_k.csv`` 的 k 才是表里的复权口径。
"""
from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

URL = "https://hq.sinajs.cn/list={}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}
CHUNK = 40          # 一次请求多少只，太多会被截断


@dataclass
class Quote:
    symbol: str
    price: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0        # 当日累计成交量（股）
    mcap: float = 0.0          # 总市值（美元）
    shares: float = 0.0        # 总股本（股）
    ts: str = ""               # 数据源时间戳 "YYYY-MM-DD HH:MM:SS"（美东）

    @property
    def session_date(self) -> str:
        return self.ts[:10]

    @property
    def has_range(self) -> bool:
        """有真实日内区间。开盘前数据源会返回 high == low == 昨收。"""
        return self.high > self.low > 0


def _num(parts: list[str], i: int) -> float:
    try:
        return float(parts[i])
    except (IndexError, ValueError):
        return 0.0


def fetch_full(symbols: list[str], timeout: float = 12.0) -> dict[str, Quote]:
    """批量取完整报价。取不到的票不出现在返回值里。"""
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    out: dict[str, Quote] = {}
    for i in range(0, len(syms), CHUNK):
        batch = syms[i: i + CHUNK]
        req = urllib.request.Request(
            URL.format(",".join(f"gb_{s.lower()}" for s in batch)), headers=HEADERS)
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
        except Exception as exc:                              # noqa: BLE001
            log.warning("新浪报价请求失败（%s）：%s", batch[0], type(exc).__name__)
            continue
        out.update(_parse(raw))
    return out


def _parse(raw: str) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        head, _, body = line.partition('="')
        key = head.rsplit("_", 1)[-1].upper()          # hq_str_gb_aapl -> AAPL
        parts = body.rstrip('";').split(",")
        if len(parts) < 11:
            continue
        px = _num(parts, 1)
        if px <= 0:
            continue
        out[key] = Quote(
            symbol=key, price=px,
            open=_num(parts, 5), high=_num(parts, 6), low=_num(parts, 7),
            volume=_num(parts, 10), mcap=_num(parts, 12), shares=_num(parts, 19),
            ts=parts[3].strip(),
        )
    return out


def fetch(symbols: list[str], timeout: float = 12.0) -> dict[str, float]:
    """只要现价的轻量版（止损用）。"""
    return {k: v.price for k, v in fetch_full(symbols, timeout).items()}
