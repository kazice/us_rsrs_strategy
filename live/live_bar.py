"""收盘前 30 分钟：用实时报价合成「当日」这根 bar。

为什么必须合成
--------------
``upload/bars_daily.parquet`` 每天**收盘后**才更新，15:30 ET 做决策时表里最新
一行还是昨天。回测的信号日 d 用的是 d 的收盘价（含 d 当天价格），如果实盘拿
d-1 的数据算，等于整体提前一天决策，选出来的票和回测完全不是一回事。

所以这里照回测口径补一根当日 bar：

    close = 现价 × k      high = 盘中最高 × k      low = 盘中最低 × k
    amount = 累计成交量 × 现价 ÷ PART_TIME        （折成全天的量，见下）

PART_TIME
---------
15:30 ET 还剩 30 分钟，累计成交量只走完全天的约 48%。换手率要还原成「全天」
口径才能和回测的日线数据比，所以把 amount 除以 0.48 再进 ``compute_indicators``。

已知口径差（会写进推送）
------------------------
盘中最高/最低比真实全天振幅窄一点；用 15:30 的价格代替收盘价成交，相当于把
最后 30 分钟的波动当成滑点。两条都不改变选股逻辑，只是执行价的近似。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent


def _load_local(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cal = _load_local("market_calendar", HERE / "market_calendar.py")
quote = _load_local("live_quote", HERE / "quote.py")
upd = _load_local("upload_update", ROOT / "upload" / "update.py")
upd_etf = _load_local("upload_update_etf", ROOT / "upload" / "update_etf.py")

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLC"]
BENCH = "SPY"

PART_TIME = 0.48        # 15:30 ET 时当日成交量占全天的比例
HIST = 300              # 每只票取多少历史行做指标预热（MA200 + RSRS 60）
MIN_COVER = 0.5         # 报价里带真实日内区间的比例下限


def session_now() -> dt.datetime:
    return dt.datetime.now(cal.UTC).astimezone(cal.ET)


def build(bars: pd.DataFrame, etf: pd.DataFrame, mem: pd.DataFrame,
          ktab: dict[str, float], today: dt.date | None = None):
    """合成当日快照。

    返回 ``(bars_day, etf_day, mem_day, day, notes)``；不该合成时返回 ``None``，
    调用方退回「数据里最新一天」的口径。
    """
    now = session_now()
    today = today or now.date()

    if not cal.is_trading_day(today):
        return None
    # 当天日线已经落库（收盘后重跑）就用真数据，不再用报价覆盖
    if (bars.date == pd.Timestamp(today)).any():
        return None

    last_mem = mem.date.max()
    m = mem[mem.date == last_mem]
    sym_of = (bars.sort_values("date").drop_duplicates("permno")
              .set_index("permno")["symbol"].astype(str).to_dict())
    sym_of = {int(k): v for k, v in sym_of.items()}
    permnos = [p for p in m.permno.unique().tolist() if int(p) in sym_of]
    stock_syms = [sym_of[int(p)] for p in permnos]
    etf_syms = SECTOR_ETFS + [BENCH]

    quotes = quote.fetch_full(stock_syms + etf_syms)
    if not quotes:
        return None

    # 会话校验：报价必须就是今天，且不是开盘前 high==low 的退化快照
    sample = quotes.get(BENCH) or next(iter(quotes.values()))
    if sample.session_date != str(today):
        return None
    cover = sum(1 for q in quotes.values() if q.has_range) / len(quotes)
    if cover < MIN_COVER:
        return None

    bars_day = _stock_rows(bars, quotes, ktab, permnos, sym_of, today)
    etf_day = _etf_rows(etf, quotes, ktab, today)
    if bars_day is None or etf_day is None:
        return None

    mem_day = m.copy()
    mem_day["date"] = pd.Timestamp(today)

    notes = [
        f"口径：用 15:30 前实时报价合成 {today} 当日 bar"
        f"（{cover:.0%} 有真实区间，成交量按 {PART_TIME:.0%} 折全天）",
        "已知偏差：盘中高低比全天振幅略窄；成交价按 15:30 计，与收盘价有最后 30 分钟的差",
        "板块 RSRS 用表内 raw 重算归一化：表内 rsrs20_40_z 与 raw 不自洽，不能直接沿用",
    ]
    return bars_day, etf_day, mem_day, today, notes


def _stock_rows(bars, quotes, ktab, permnos, sym_of, today) -> pd.DataFrame | None:
    cols = ["permno", "date", "symbol", "open", "high", "low", "close",
            "volume", "amount", "mcap"]
    rows = []
    keep = {"permno", "date", "symbol", "close", "turn20", "turn120", "turn_score",
            "turnover", "mcap", "ma20", "ma50", "ma60", "ma200",
            "slope20_10d", "slope60_5d", "adx14", "rsrs20_40_raw", "rsrs20_40_z"}
    for p in permnos:
        p = int(p)
        s = sym_of[p]
        q = quotes.get(s)
        if q is None or not q.has_range:
            continue
        k = ktab.get(s, 1.0)
        g = bars[bars.permno == p].tail(HIST)
        if len(g) < 210:                       # MA200 预热不够，宁可不给信号
            continue
        row = {
            "permno": p, "date": pd.Timestamp(today), "symbol": s,
            "open": q.open * k,
            "high": max(q.high, q.price) * k,
            "low": min(q.low or q.price, q.price) * k,
            "close": q.price * k,
            "volume": q.volume,
            "amount": q.volume * q.price / PART_TIME,
            "mcap": q.mcap,
        }
        gg = pd.concat([g[cols], pd.DataFrame([row])], ignore_index=True)
        out = upd.compute_indicators(gg)
        rows.append(out.iloc[-1][list(keep)])
    if not rows:
        return None
    return pd.DataFrame(rows)


def _rsrs_raw(y, x) -> float:
    """单点 RSRS：β × R²，与 update.py 的 compute_indicators 同一公式。"""
    if len(y) != 20 or len(x) != 20:
        return float("nan")
    if pd.isna(y).any() or pd.isna(x).any():
        return float("nan")
    vx = float(pd.Series(x).var(ddof=0))
    if vx <= 0:
        return float("nan")
    b = float(pd.Series(x).cov(pd.Series(y), ddof=0) / vx)
    r = float(pd.Series(x).corr(pd.Series(y)))
    if pd.isna(b) or pd.isna(r) or abs(b) > 10 or r ** 2 < 0.5:
        return float("nan")
    return b * r ** 2


def _etf_rows(etf, quotes, ktab, today) -> pd.DataFrame | None:
    rows = []
    spy_close = None
    for s in SECTOR_ETFS + [BENCH]:
        q = quotes.get(s)
        if q is None or not q.has_range:
            continue
        k = ktab.get(s, 1.0)
        g = etf[etf.etf_symbol == s].sort_values("date")
        if len(g) < 220:
            continue
        c = pd.concat([g.close.reset_index(drop=True),
                       pd.Series([q.price * k])], ignore_index=True).astype(float)
        slope = upd_etf.log_slope120(c)

        # RSRS 不整段重算：表里的 raw 是 float32 存的，重算会因 40 日标准差窗口
        # 偏移把 z 拉偏 0.1~0.2，足以改变 11 个板块的相对名次。改成
        # 「今日 raw 增量算 + 用表内最近 40 个 raw 归一化」，与表内口径连续。
        y = list(g.high.to_numpy(float)[-19:]) + [max(q.high, q.price) * k]
        x = list(g.low.to_numpy(float)[-19:]) + [min(q.low or q.price, q.price) * k]
        raw_now = _rsrs_raw(y, x)
        # 归一化窗口是「今天之前的 40 个 raw」，必须排除当天，口径与 rsrs_z 一致
        hist = g.rsrs20_40_raw.iloc[:-1].tail(40).dropna()
        z = float("nan")
        if len(hist) >= 20 and float(hist.std()) > 0 and pd.notna(raw_now):
            z = (raw_now - float(hist.mean())) / float(hist.std())

        rows.append({
            "date": pd.Timestamp(today), "etf_symbol": s, "close": float(c.iloc[-1]),
            "rsrs20_40_z": z,
            "slope120_weighted": float(slope.iloc[-1]) if slope.iloc[-1] == slope.iloc[-1]
            else float("nan"),
            "bear_ratio21": float("nan"),
        })
        if s == BENCH:
            spy_close = c

    if not rows:
        return None

    df = pd.DataFrame(rows)
    if spy_close is not None and len(spy_close) >= 220:
        ma200 = spy_close.rolling(200, min_periods=200).mean()
        below = (spy_close < ma200).astype(float).where(ma200.notna())
        df.loc[df.etf_symbol == BENCH, "bear_ratio21"] = float(below.tail(21).mean())
    return df
