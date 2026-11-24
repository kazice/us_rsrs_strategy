"""腾讯自选股：美股日 K（前复权）。

为什么加这个源：
    yfinance 对已退市/改名的票经常拿不到，而且复权口径与 CRSP 会有 1% 级漂移；
    腾讯国内可直连、无 key、单次最多 640 条，按 2 年一段分段就能拿全 2015→今天。

接口要点（实测，2026-11）：
    URL  https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param=us{SYM}.{EX},day,{start},{end},{n},qfq
    ① 必须带交易所后缀：.OQ=Nasdaq / .N=NYSE / .A=AMEX。
       不带后缀只返回 2 条（等于不可用），带错后缀返回空。
    ② 单次 n 上限 640，超了返回 {"code":-1,"msg":"limit error"}。
       返回的是「区间内最后 640 条」，所以按 2 年一段切不会丢数据。
    ③ 返回 key = qfqday，每行 [日期, 开, 收, 高, 低, 成交量]（注意是「开收高低」，不是 OHLC）。
    ④ 复权：qfqday 是前复权（以最新价为基准），与 CRSP 的 DlyClose/DlyCumFacPr 同向。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Sequence

import pandas as pd

log = logging.getLogger(__name__)

URL = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://gu.qq.com/",
}
MAX_BARS = 640
EXCHANGES = (".OQ", ".N", ".A", "")     # Nasdaq / NYSE / AMEX / 兜底
SLEEP = 0.35                            # 批间限速，避免被限流

_EX_CACHE: dict[str, str] = {}


def _get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def _probe_exchange(sym: str, start: str, end: str) -> str:
    """找一个能返回数据的交易所后缀（结果缓存）。"""
    if sym in _EX_CACHE:
        return _EX_CACHE[sym]
    for ex in EXCHANGES:
        code = f"us{sym}{ex}"
        url = f"{URL}?param={code},day,{start},{end},5,qfq"
        try:
            j = json.loads(_get(url))
        except Exception:
            continue
        if j.get("code") != 0:
            continue
        d = j.get("data", {}).get(code) or {}
        for k in ("qfqday", "day"):
            if isinstance(d.get(k), list) and len(d[k]) >= 1:
                _EX_CACHE[sym] = ex
                return ex
    _EX_CACHE[sym] = ""
    return ""


def _fetch_one(sym: str, start: str, end: str) -> pd.DataFrame:
    ex = _probe_exchange(sym, start, end)
    if ex is None:
        return pd.DataFrame()
    code = f"us{sym}{ex}"
    rows: list[list] = []
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    guard = 0
    while s < e and guard < 30:
        guard += 1
        seg_end = min(s + pd.DateOffset(years=2), e)
        url = (f"{URL}?param={code},day,{s.date()},{seg_end.date()},{MAX_BARS},qfq")
        try:
            j = json.loads(_get(url))
        except Exception as exc:
            log.warning("tencent %s %s~%s 失败：%s", sym, s.date(), seg_end.date(), exc)
            s = seg_end
            time.sleep(SLEEP)
            continue
        d = j.get("data", {}).get(code) or {}
        arr = d.get("qfqday") or d.get("day") or []
        rows.extend(arr)
        s = seg_end
        time.sleep(SLEEP)

    if not rows:
        return pd.DataFrame()
    # [日期, 开, 收, 高, 低, 成交量]
    df = pd.DataFrame([r[:6] for r in rows if len(r) >= 6],
                      columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = (df.dropna(subset=["date", "close"])
            .drop_duplicates("date", keep="last")
            .sort_values("date"))
    df["adj_close"] = df["close"]       # qfq = 前复权，close 已经是复权价
    df["dividends"] = 0.0
    df["stock_splits"] = 0.0
    df["source"] = f"tencent{ex or ''}"
    return df.reset_index(drop=True)


class TencentSource:
    name = "tencent"

    def available(self) -> bool:
        return True

    def fetch(self, symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for s in symbols:
            df = _fetch_one(str(s).upper(), start, end)
            if not df.empty:
                out[str(s).upper()] = df
        return out
