"""实时报价（腾讯自选股 + 新浪财经），给盘中决策提供「截至此刻」的价格与累计成交量。

为什么是这两家
--------------
- **腾讯** `qt.gtimg.cn` 的报价里直接带 ``换手率`` 字段，分母是当日总股本——这与回测用的
  ``turnover_daily.parquet``（westock 日 K 的 exchange 列）**同源同口径**，无需再拿 SEC
  股本去凑分母，也不用校准系数。批量上限约 60 只/请求。
- **新浪** `hq.sinajs.cn` 给价格/最高/最低/累计成交量/总市值，用来交叉校验腾讯的成交量，
  并在腾讯个别票缺失时兜底（此时用 市值/价格 反推股本算换手率，口径略差，会被标注）。

两者都是免费、无 key、国内可直连；在 GitHub Actions（美区）上也能通。但它们对美股
都有**服务端延迟**（早期手测约 1~1.5 小时，**未经 runs.jsonl 验证**），所以每个报价都带
数据源时间戳，调用方必须用 ``lag_minutes`` 判断这份数据能不能支撑 15:30 的决策——本项目
不做静默处理。实际滞后会写进 runs.jsonl 的 ``lag_min``，阈值见 ``live_signal --lag-refuse-min``。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import urllib.request
from dataclasses import dataclass

from zoneinfo import ZoneInfo

from quant.live.calendar import ET

_BJ = ZoneInfo("Asia/Shanghai")

log = logging.getLogger(__name__)

TENCENT_QUOTE = "https://qt.gtimg.cn/q={codes}"
SINA_QUOTE = "https://hq.sinajs.cn/list={codes}"
TENCENT_BATCH = 60
SINA_BATCH = 80
HTTP_RETRIES = 3          # 每批请求的尝试次数（含首次）；退避 1s、2s
# 换手率自校验容差。腾讯换手率只给两位小数，真正同源的数据误差远小于 2%；
# 原来的 6% 足以让「20% 换手上限」这类硬阈值的判定翻转。
TURN_ABS_TOL = 0.02
TURN_REL_TOL = 0.02
# 两源成交量比值容差：超出只写 notes（推送里可见），不改数值也不中止
RECONCILE_RATIO = (0.93, 1.075)

_RE_TENCENT = re.compile(r'v_us([A-Za-z0-9.\-]+)="([^"]*)"')
_RE_SINA = re.compile(r'hq_str_gb_([A-Za-z0-9.\-]+)="([^"]*)"')
_RE_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")

_HEADERS_TENCENT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://gu.qq.com/",
}
_HEADERS_SINA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn",
}


@dataclass
class Quote:
    """一条当日快照。金额单位：美元；成交量单位：股（**未复权**原始量）。"""

    symbol: str
    price: float
    prev_close: float
    high: float
    low: float
    volume: float
    turnover_pct: float          # 当日累计换手率（%），分母 = 当日总股本
    total_shares: float
    ts: dt.datetime | None       # 数据源时间戳（ET）
    src: str

    def lag_minutes(self, ref: dt.datetime) -> float | None:
        if self.ts is None:
            return None
        return (ref - self.ts).total_seconds() / 60.0


def _variants(sym: str) -> list[str]:
    """同一个代码在报价源的几种写法：BRK-B ↔ BRK.B。

    库里统一用横杠（yahoo / Tiingo 口径），但腾讯、新浪的美股代码用点号，
    拿 ``usBRK-B`` 去问只会返回空——不是报错，是**静默缺这一只**，所以两个都试。
    """
    out = [sym]
    if "-" in sym:
        out.append(sym.replace("-", "."))
    return out


def _http(url: str, headers: dict[str, str], timeout: float = 20.0,
          retries: int = HTTP_RETRIES) -> str:
    """GET 并解码；网络层失败重试（限流/抖动常是瞬时的），重试用尽才抛出。"""
    req = urllib.request.Request(url, headers=headers)
    raw = b""
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except Exception as exc:
            if attempt >= retries:
                raise
            log.warning("报价请求第 %d 次失败（%s），%ds 后重试",
                        attempt, type(exc).__name__, attempt)
            time.sleep(float(attempt))
    for enc in ("gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _parse_ts(text: str) -> dt.datetime | None:
    m = _RE_DATETIME.search(text)
    if not m:
        return None
    try:
        naive = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=ET)


def _as_float(cell: str) -> float | None:
    try:
        val = float(cell)
    except (TypeError, ValueError):
        return None
    return val if val == val else None          # 排除 nan


def _resolve_turnover(fields: list[str], idx: int, volume: float) -> tuple[float, float]:
    """在报价字段里找出 (换手率%, 总股本)。

    腾讯报价的字段序号会随品种/版本浮动：个股的换手率在时间戳后第 8 格，ETF 因为中间几个
    字段为空会整体前移，硬编码下标就会把空串当数字、把市值当股本。这里用恒等式自校验：
    找到一对 (turn, shares) 使 ``成交量 ÷ shares × 100 ≈ turn``，谁成立用谁。

    ETF 找不到一致对时退回「成交量 ÷ 末段最大数字」，这个数对个股是流通股本、对 ETF 是
    份额，量级可能偏，但 ETF 的换手率不参与本策略决策（板块层只用 RSRS），仅作展示。
    """
    if volume <= 0:
        return 0.0, 0.0
    tail = [v for v in (_as_float(c) for c in fields[idx + 5: idx + 25]) if v and v > 0]
    for turn in tail:
        if turn > 100:                      # 换手率不可能是三位数，那是成交量/成交额
            continue
        for k in range(len(fields) - 1, idx, -1):
            sh = _as_float(fields[k])
            if sh is None or sh <= volume * 1.2:
                continue
            implied = volume / sh * 100.0
            if abs(implied - turn) <= max(TURN_ABS_TOL, TURN_REL_TOL * turn):
                return turn, sh
            break                           # 只认末段最大的那个数，继续换 turn 试
    best = 0.0
    for k in range(len(fields) - 1, idx, -1):
        sh = _as_float(fields[k])
        if sh is not None and sh > volume * 1.2:
            best = sh
            break
    return (volume / best * 100.0 if best > 0 else 0.0), best


def tencent_quotes(symbols: list[str], pause: float = 0.15) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for i in range(0, len(symbols), TENCENT_BATCH):
        chunk = symbols[i: i + TENCENT_BATCH]
        codes = [f"us{v}" for s in chunk for v in _variants(s)]
        url = TENCENT_QUOTE.format(codes=",".join(codes))
        try:
            text = _http(url, _HEADERS_TENCENT)
        except Exception as exc:
            log.warning("腾讯报价失败（%d 只）：%s", len(chunk), type(exc).__name__)
            continue
        for sym, body in _RE_TENCENT.findall(text):
            fields = body.split("~")
            if len(fields) < 40:
                continue
            try:
                price = float(fields[3])
                prev = float(fields[4])
                volume = float(fields[6])
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            ts = _parse_ts(body)
            high, low, turnover_pct, shares = price, price, 0.0, 0.0
            try:
                idx = next(j for j, c in enumerate(fields) if _RE_DATETIME.search(c))
            except StopIteration:
                idx = -1
            if idx >= 0 and len(fields) > idx + 5:
                # 时间戳之后依次是 涨跌 / 涨跌幅 / 最高 / 最低 / 币种 / 成交量 ...
                h, lo_ = _as_float(fields[idx + 3]), _as_float(fields[idx + 4])
                if h and h > 0:
                    high = h
                if lo_ and lo_ > 0:
                    low = lo_
                turnover_pct, shares = _resolve_turnover(fields, idx, volume)
            key = sym.upper().replace(".", "-")     # 源用点号，本项目统一横杠
            out[key] = Quote(key, price, prev, high, low, volume,
                             turnover_pct, shares, ts, "tencent")
        if i + TENCENT_BATCH < len(symbols):
            time.sleep(pause)
    return out


def sina_quotes(symbols: list[str], pause: float = 0.15) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for i in range(0, len(symbols), SINA_BATCH):
        chunk = symbols[i: i + SINA_BATCH]
        codes = [f"gb_{v.lower()}" for s in chunk for v in _variants(s)]
        url = SINA_QUOTE.format(codes=",".join(codes))
        try:
            text = _http(url, _HEADERS_SINA)
        except Exception as exc:
            log.warning("新浪报价失败（%d 只）：%s", len(chunk), type(exc).__name__)
            continue
        for sym, body in _RE_SINA.findall(text):
            fields = body.split(",")
            if len(fields) < 21:
                continue
            try:
                price = float(fields[1])
                chg = float(fields[4])
                high = float(fields[6])
                low = float(fields[7])
                volume = float(fields[10])
                mktcap = float(fields[12])
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            # 新浪 fields[3] 是北京时间：必须带上 Asia/Shanghai 时区再转 ET。
            # 旧写法「减 8 小时再贴 ET 标签」会把时刻偏到未来约 5 小时，滞后变成负数，
            # 陈旧报价反而能通过「滞后超过 45 分钟」的闸门。
            # 注意：盘前/盘后该字段是新浪的刷新时刻（实测 = 当前墙钟），并非最后成交时刻，
            # 所以新浪兜底的报价无法靠这个时间戳识别陈旧；交给 _quote_session 等闸门。
            ts = None
            try:
                bj = dt.datetime.strptime(fields[3], "%Y-%m-%d %H:%M:%S")
                ts = bj.replace(tzinfo=_BJ).astimezone(ET)
            except (ValueError, IndexError):
                pass
            shares = mktcap / price if price > 0 else 0.0
            key = sym.upper().replace(".", "-")
            out[key] = Quote(
                key, price, price - chg, high or price, low or price, volume,
                (volume / shares * 100.0) if shares > 0 else 0.0, shares, ts, "sina")
        if i + SINA_BATCH < len(symbols):
            time.sleep(pause)
    return out


def fetch_quotes(symbols: list[str],
                 reconcile_ratio: tuple[float, float] = RECONCILE_RATIO,
                 ) -> tuple[dict[str, Quote], dict[str, str]]:
    """腾讯为主、新浪校验/兜底。

    返回 (报价表, 每只票的来源与告警说明)。成交量两源比值超出 ``reconcile_ratio``
    时保留腾讯值但记一条告警——不一致通常意味着其中一家的报价不是同一天的。
    """
    symbols = [s.upper() for s in dict.fromkeys(symbols)]
    primary = tencent_quotes(symbols)
    notes: dict[str, str] = {}

    missing = [s for s in symbols if s not in primary]
    backup = sina_quotes(missing + [s for s in symbols if s in primary]) if symbols else {}

    for s in list(primary):
        alt = backup.get(s)
        if alt is None or alt.volume <= 0 or primary[s].volume <= 0:
            continue
        ratio = primary[s].volume / alt.volume
        if not (reconcile_ratio[0] <= ratio <= reconcile_ratio[1]):
            notes[s] = f"两源成交量比值 {ratio:.2f}，可能非同一天"

    for s in missing:
        alt = backup.get(s)
        if alt is None:
            notes[s] = "两源均无报价"
            continue
        primary[s] = alt
        notes.setdefault(s, "腾讯缺失，用新浪兜底（换手率由市值反推股本，口径略差）")

    return primary, notes


def describe_lag(quotes: dict[str, Quote], ref: dt.datetime) -> tuple[float | None, int]:
    """返回 (中位滞后分钟数, 样本数)。滞后过大时决策不应被当作实时信号。"""
    lags = [q.lag_minutes(ref) for q in quotes.values() if q.ts is not None]
    lags = sorted(x for x in lags if x is not None)
    if not lags:
        return None, 0
    mid = lags[len(lags) // 2]
    return float(mid), len(lags)
