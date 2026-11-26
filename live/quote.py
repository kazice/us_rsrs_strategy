"""实时报价（新浪美股），只用于收盘前的止损检查。

为什么不能用日线
----------------
``upload/bars_daily.parquet`` 是**收盘后**更新的，15:30 ET 做止损判断时，表里最新
一行还是昨天。用昨收判止损会整整晚一天，所以持仓票必须拿实时价。

返回值是**未复权**价，要乘 ``upload/adjust_k.csv`` 里的 k 才等于表里的复权口径。
"""
from __future__ import annotations

import logging
import urllib.request

log = logging.getLogger(__name__)

URL = "https://hq.sinajs.cn/list={}"
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}


def fetch(symbols: list[str], timeout: float = 10.0) -> dict[str, float]:
    """批量取现价。取不到的票不出现在返回值里，由调用方决定降级策略。"""
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    req = urllib.request.Request(
        URL.format(",".join(f"gb_{s.lower()}" for s in syms)), headers=HEADERS)
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("新浪报价请求失败：%s", type(exc).__name__)
        return {}

    out: dict[str, float] = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        head, _, body = line.partition('="')
        key = head.rsplit("_", 1)[-1].upper()          # hq_str_gb_aapl -> AAPL
        body = body.rstrip('";')
        parts = body.split(",")
        if len(parts) < 2:
            continue
        try:
            px = float(parts[1])
        except ValueError:
            continue
        if px > 0:
            out[key] = px
    if not out:
        log.warning("新浪报价解析为空：%s", raw[:200])
    return out
