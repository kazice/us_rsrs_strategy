"""模拟盘当期信号：把回测里「月末最后交易日那一期」单独拎出来算。

与回测（``scripts/sector_stock_rsrs.py``）的唯一区别是**不需要下一期**：
回测循环是 ``for k in range(k0, len(me)-1)``，必须知道下一个调仓日才能算持有期收益，
所以最后一期永远不出信号；实盘只要「现在该买哪只」，没有这段依赖。

口径逐项对齐（任何一项不同都会让信号偏离定稿）：
- 板块层 RSRS：同一份 ``rsrs_panel_fast``，N=20 / M=40 / strict；
- 市值门槛：候选池先按 **500 亿美元**过滤（``--min-mcap 500``，见 ``quant.live.mcap``），
  市值用**当日真实股本**（全市场快照 → 换手率反推 → SEC 申报，逐级兜底并留来源标记）；
- 个股层排序：20 日均换手率降序（ws 口径），超 20% 上限的票当期剔除；
- 多头排列：MA10 > MA20 > MA50（``--trend-mode ma_stack --stack-order desc``）；
- 下探：按板块 RSRS 顺序最多看 3 个板块，每板块看换手率前 3 只；
- 全部不合格 → 当期空仓（回测同样处理）。

今日这一根 bar 用**实时报价**合成：现价 → adj_close（乘最近一日的复权因子），
盘中最高/最低 → high/low，累计成交量 → 换手率（见 ``quant.live.turnover``）。
这意味着盘中高/低点比回测的完整日振幅略窄，属于已知口径差，会写进推送正文。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import store
from quant.live import calendar as calendar_mod
from quant.live import mcap as mcap_mod
from quant.live import pool as pool_mod
from quant.live import turnover as turnover_mod
from quant.live.quotes import Quote

log = logging.getLogger(__name__)

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"]
BENCH = "SPY"
RSRS_N, RSRS_M = 20, 40
STACK = (10, 20, 50)
TURN_CAP = 0.20
TURNMA_TOP = 3
TURNMA_SECTORS = 3
HISTORY_DAYS = 400          # 预热：60 个交易日窗口 + 节假日余量，取 400 自然日足够
QUOTE_RANGE_MIN_COVER = 0.5  # 这个比例的报价要有 high>low，否则判为「当日 bar 还没形成」


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_members() -> pd.DataFrame:
    """成分股快照（data 分支刷新过的那份优先）。

    这里**不再**按 ``store.list_symbols()`` 过滤：新纳入标普 500 的票本地往往还没有行情，
    过滤掉就等于静默丢弃，而它恰恰是可能带来被动买盘的那只。没有行情的票交给
    ``compute`` 显式剔出并写进推送备注，人能看到「它进池了但没数据，去补」。
    """
    return pool_mod.load()


def trading_lag_days(last_bar, asof) -> int:
    """``last_bar`` 与信号日**之间**还有几个交易日的数据没到。

    只数中间缺口，不含信号日本身：当天的 bar 本来就要用实时报价合成，所以
    「面板停在上一交易日」= 0（正常），缺 1 个交易日 = 1，以此类推。

    为什么要单独算这个：``_append_live_row`` 只会把「今天」拼到面板尾部，中间的缺口
    不补也不报错；data job 连挂几天时，板块 RSRS 与均线会静默用几天前的收盘价，
    而推送正文里看不出任何异常。所以这里按内置交易日历数缺口，交给调用方决定要不要中止。
    """
    if last_bar is None or pd.isna(last_bar):
        return 0
    last = pd.Timestamp(last_bar).date()
    end = pd.Timestamp(asof).date()
    if end <= last:
        return 0
    n, d = 0, last + dt.timedelta(days=1)
    for _ in range(400):
        if d >= end:                    # 信号日本身不算缺口
            break
        if calendar_mod.is_trading_day(d):
            n += 1
        d += dt.timedelta(days=1)
    return n


@dataclass
class Candidate:
    symbol: str
    sector: str
    sector_rank: int
    turnover: float
    ma: dict[int, float] = field(default_factory=dict)
    ok: bool = False
    why: str = ""


@dataclass
class Signal:
    asof: pd.Timestamp
    sector: str
    pick: str
    price: float
    adj_price: float
    sector_scores: pd.Series
    candidates: list[Candidate]
    turnover_median: float
    notes: list[str] = field(default_factory=list)
    data_asof: pd.Timestamp | None = None
    factors: dict[str, float] = field(default_factory=dict)   # 未复权价 → adj_close 的因子
    pool_size: int = 0
    pool_as_of: str = ""
    min_mcap: float = 0.0          # 候选池市值门槛（美元），0 = 不限
    data_lag_days: int = 0         # 行情面板落后信号日几个交易日（0 = 到上一交易日，正常）
    data_last_bar: pd.Timestamp | None = None
    pool_table: pd.DataFrame = field(default_factory=pd.DataFrame)   # 全池候选表，供日志落盘

    @property
    def is_cash(self) -> bool:
        return not self.pick


def _quote_session(quotes: dict[str, Quote]) -> dt.date | None:
    """报价所属的交易日（ET 日期），取时间戳最新的那条。没时间戳就返回 None。"""
    stamps = [q.ts for q in quotes.values() if getattr(q, "ts", None) is not None]
    if not stamps:
        return None
    latest = max(stamps)
    if latest.tzinfo is not None:
        latest = latest.astimezone(calendar_mod.ET)
    return latest.date()


def _quote_range_cover(quotes: dict[str, Quote]) -> tuple[float, int]:
    """报价里有多少比例带真实日内区间（high > low）。

    数据源在开盘前会返回 high == low == 上一交易日收盘（时间戳还标成 09:30），这时
    "今日 bar" 退化成一个点：RSRS 的 20 日回归里多一个点，R² 被拉向 1、β 变形，
    板块排序整体改写。看区间比看时间戳可靠——时间戳在开盘前也是 09:30。
    """
    covered = checked = 0
    for q in quotes.values():
        if not (q.price > 0 and q.high and q.low):
            continue
        checked += 1
        if q.high > q.low:
            covered += 1
    return (covered / checked if checked else 0.0), checked


def _append_live_row(px: pd.DataFrame, hi: pd.DataFrame, lo: pd.DataFrame,
                     quotes: dict[str, Quote], asof: pd.Timestamp,
                     ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, float], str]:
    """把信号日那根 bar 补齐到面板尾部，返回 (px, hi, lo, 复权因子, 该 bar 的来源)。

    以日线实际数据为准，分两种情形：

    - **日线里已经有这一天的 bar**（决策在收盘后跑，或历史时点复现）→ 直接用日线，
      不拿报价去覆盖它。报价是盘中快照，覆盖过去等于把已确定的收盘数据改小。
    - **日线只到前一交易日**（盘中 15:30 决策的正常情形）→ 用报价合成当日 bar，但前提是
      报价确实属于信号日这个交易日。报价的交易日取自它自己的时间戳；对不上就说明数据源
      还没翻页（拿到的是上一交易日的收盘快照），此时合成等于在面板尾部复制一根昨天的 bar，
      20 日回归的 R² 会被拉向 1，板块排序整体改写（实测 11-13 的 XLP 1.06 会被算成 0.44，
      选出的票从 MNST 变成 APP）。这种情形一律拒绝，不做相似度猜测。

    复权因子取最近一个交易日的 ``adj_close / close``：报价是未复权价，乘上它才和面板里的
    adj_close 同口径。除权当天因子会跳变，但一天之内不影响排序。
    """
    last_bar = px.index.max() if len(px.index) else None
    asof = pd.Timestamp(asof).normalize()

    if last_bar is not None and pd.Timestamp(last_bar).normalize() >= asof:
        # 日线已覆盖信号日：以实际日线为准
        px = px[px.index <= asof].copy()
        hi = hi[hi.index <= asof].copy()
        lo = lo[lo.index <= asof].copy()
        last_close = store.load_panel(field="close", symbols=list(px.columns),
                                      start=str((asof - pd.Timedelta(days=20)).date()),
                                      end=str(asof.date()))
        factor = (px.iloc[-1] / last_close.iloc[-1]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
        return px, hi, lo, {s: float(factor[s]) for s in factor.index}, "daily"

    px = px[px.index < asof].copy()
    hi = hi[hi.index < asof].copy()
    lo = lo[lo.index < asof].copy()
    if px.empty:
        raise SystemExit(f"{asof} 之前没有历史行情，无法计算信号")

    last_close = store.load_panel(field="close", symbols=list(px.columns),
                                  start=str((asof - pd.Timedelta(days=20)).date()),
                                  end=str(asof.date()))
    last_close = last_close[last_close.index < asof]
    if last_close.empty:
        raise SystemExit("取不到最近收盘价，无法计算复权因子")
    factor = (px.iloc[-1] / last_close.iloc[-1]).replace([np.inf, -np.inf], np.nan).fillna(1.0)

    session = _quote_session(quotes)
    if session is None:
        raise SystemExit("报价没有时间戳，无法确认它属于哪个交易日，拒绝合成当日 bar")
    if pd.Timestamp(session) != asof:
        raise SystemExit(
            f"报价的交易日是 {session}，信号日是 {asof:%Y-%m-%d}，日线最新 {px.index[-1]:%Y-%m-%d}："
            f"信号日这根 bar 没有任何真实数据，用旧报价合成只会复制前一根、改写板块 RSRS。"
            f"等数据源更新到 {asof:%Y-%m-%d} 再出信号"
        )

    range_cover, checked = _quote_range_cover(quotes)
    if checked >= 20 and range_cover < QUOTE_RANGE_MIN_COVER:
        raise SystemExit(
            f"只有 {range_cover:.0%} 的报价（{checked} 只）带日内区间（high>low），其余是 "
            f"high=low=收盘：{asof:%Y-%m-%d} 的当日 bar 还没形成（开盘前/集合竞价阶段，"
            f"数据源会把 high/low 填成上一交易日收盘）。合成的 bar 是一个点，会让 RSRS 回归退化，"
            f"拒绝出信号"
        )

    row_px, row_hi, row_lo = {}, {}, {}
    for s, q in quotes.items():
        if s not in factor.index:
            continue
        f = float(factor[s])
        row_px[s] = q.price * f
        row_hi[s] = max(q.high, q.price) * f
        row_lo[s] = min(q.low or q.price, q.price) * f

    idx = px.index.append(pd.DatetimeIndex([asof]))
    px = px.reindex(idx).ffill()
    hi = hi.reindex(idx).ffill()
    lo = lo.reindex(idx).ffill()
    for s, v in row_px.items():
        px.at[asof, s] = v
    for s, v in row_hi.items():
        hi.at[asof, s] = v
    for s, v in row_lo.items():
        lo.at[asof, s] = v
    return px, hi, lo, {s: float(factor[s]) for s in factor.index}, "quote"


def _stack_ok(px: pd.DataFrame, asof: pd.Timestamp, sym: str) -> tuple[bool, dict[int, float]]:
    """MA10 > MA20 > MA50（多头排列）。任一均线缺失即判不合格。"""
    mas: dict[int, float] = {}
    for w in STACK:
        series = px[sym].rolling(w, min_periods=max(3, w // 2)).mean()
        val = series.at[asof] if asof in series.index else np.nan
        if not np.isfinite(val):
            return False, mas
        mas[w] = float(val)
    ok = mas[10] > mas[20] > mas[50]
    return ok, mas


def compute(asof: pd.Timestamp, quotes: dict[str, Quote],
            quote_notes: dict[str, str] | None = None,
            allow_snapshot_ahead: bool = False,
            ) -> Signal:
    """算当期目标持仓。``quotes`` 至少要覆盖 11 只板块 ETF + 候选个股。

    ``allow_snapshot_ahead`` 只给 ``live_audit --replay`` 用（历史时点复算时允许用
    今天的全市场快照取股本），正常出信号必须保持 False，否则会引入前视。
    """
    from scripts.sector_stock_rsrs import rsrs_panel_fast

    asof = pd.Timestamp(asof).normalize()
    members = load_members()
    if members.empty:
        raise SystemExit("成分股表为空：data 分支的 ref/us_sector_holdings.csv 缺失或损坏")

    sec_of = dict(zip(members["symbol"], members["sector_etf"]))
    stocks = list(members["symbol"])
    syms = list(dict.fromkeys(SECTOR_ETFS + [BENCH] + stocks))

    start = str((asof - pd.Timedelta(days=HISTORY_DAYS)).date())
    hi = store.load_panel(field="high", symbols=syms, start=start, end=str(asof.date())).sort_index()
    lo = store.load_panel(field="low", symbols=syms, start=start, end=str(asof.date())).sort_index()
    px = store.load_panel(field="adj_close", symbols=syms, start=start, end=str(asof.date())).sort_index()

    notes: list[str] = []
    last_bar = px.index.max() if len(px.index) else None
    lag_days = trading_lag_days(last_bar, asof)
    if lag_days:
        notes.append(f"⚠ 行情面板最新 {last_bar:%Y-%m-%d}，落后信号日 {lag_days} 个交易日："
                     f"板块 RSRS 与均线用的是旧收盘，本期信号仅供参考")
    stocks_missing: list[str] = []          # 有成分没本地行情，审计时要单独标出来
    missing = [s for s in syms if s not in px.columns]
    if missing:
        log.info("%d 只成分股在本地无行情，已忽略：%s", len(missing),
                 " ".join(missing[:15]))
        stocks_missing = [s for s in missing if s not in (SECTOR_ETFS + [BENCH])]
        if stocks_missing:
            # 这些可能是刚纳入标普 500、还没补过行情的票，也可能是从来就没抓过的小票。
            # 无论哪种，候选池都不完整，必须在推送里露出来——静默丢弃比报错更危险。
            notes.append(f"{len(stocks_missing)}/{len(members)} 只成分股无本地行情，"
                         f"本期不参与排序（新纳入需补数据）："
                         f"{' '.join(stocks_missing[:10])}"
                         f"{' ...' if len(stocks_missing) > 10 else ''}")
        syms = [s for s in syms if s not in missing]
        stocks = [s for s in stocks if s in syms]

    no_quote = [s for s in syms if s not in quotes]
    if no_quote:
        notes.append(f"{len(no_quote)} 只无当日报价（今日 bar 用前值 ffill）："
                     f"{' '.join(no_quote[:10])}{' ...' if len(no_quote) > 10 else ''}")

    px, hi, lo, factors, bar_src = _append_live_row(px, hi, lo, quotes, asof)
    if bar_src == "daily":
        notes.append(f"信号日 {asof:%Y-%m-%d} 已有实际日线，按日线口径计算（未用盘中报价覆盖）")

    etf_rsrs = rsrs_panel_fast(hi[SECTOR_ETFS], lo[SECTOR_ETFS], RSRS_N, RSRS_M, "strict")
    if asof not in etf_rsrs.index or etf_rsrs.loc[asof].dropna().empty:
        raise SystemExit(f"{asof} 板块层 RSRS 全为 NaN，预热不足或今天是数据空洞")

    sec_score = etf_rsrs.loc[asof].dropna().sort_values(ascending=False)
    lead_sector = str(sec_score.index[0])

    # 换手率先按**全池**算，再做市值过滤。顺序不能反：被市值剔掉的票也要留下换手率，
    # 否则日志里答不出「它当时换手率排第几、离入选差多远」。
    turn = turnover_mod.build(stocks, asof, quotes, quote_notes or {})
    capped = turn.mean[turn.mean > TURN_CAP]
    if len(capped):
        notes.append(f"换手率超 {TURN_CAP:.0%} 上限被剔除："
                     + "、".join(f"{s} {v:.2%}" for s, v in capped.items()))
    turn_ok = turn.mean[turn.mean <= TURN_CAP]

    # 市值门槛：候选池先过 500 亿美元（与回测 --min-mcap 500 同口径）。
    # 价用盘中实时价（未复权），股本由 成交量/换手率 反推；取不到市值的票保留，
    # 与回测「拿不到市值不剔除」保持一致。没有当日报价的票没有价，自然落在保留侧。
    px_now = pd.Series({s: q.price for s, q in quotes.items()}, dtype="float64")
    mgate = mcap_mod.gate(stocks, px_now, asof,
                          allow_snapshot_ahead=allow_snapshot_ahead)
    if mgate.active:
        notes += mgate.notes
    eligible = [s for s in mgate.kept if s in turn_ok.index]
    if not eligible:
        notes.append("市值/换手率过滤后候选池为空，本期必然空仓")

    # 全池均线（复权价）。min_periods 与 _stack_ok 一致，任一均线缺失即判不合格。
    ma = {w: px.rolling(w, min_periods=max(3, w // 2)).mean() for w in STACK}

    def ma_of(sym: str) -> tuple[bool, dict[int, float]]:
        if sym not in px.columns:
            return False, {}
        vals = {w: float(ma[w].at[asof, sym]) if sym in ma[w].columns else float("nan")
                for w in STACK}
        ok = all(np.isfinite(v) for v in vals.values()) and vals[10] > vals[20] > vals[50]
        return ok, vals

    candidates: list[Candidate] = []
    pick, picked_sector, picked_price = "", lead_sector, float("nan")
    rank_of: dict[str, int] = {}

    for rank_s, sec in enumerate(sec_score.index[:TURNMA_SECTORS], 1):
        pool = [s for s in eligible if sec_of.get(s) == sec]
        if not pool:
            notes.append(f"板块 {sec}（第 {rank_s} 名）无可排序个股，跳过")
            continue
        top = turn_ok[pool].sort_values(ascending=False).head(TURNMA_TOP)
        for s in top.index:
            rank_of[str(s)] = rank_s
            ok, mas = ma_of(str(s))
            candidates.append(Candidate(str(s), str(sec), rank_s, float(turn_ok[s]), mas, ok,
                                        "多头排列" if ok else "均线未多头排列"))
            if ok and not pick:
                pick, picked_sector = str(s), str(sec)
        if pick:
            break

    if pick:
        q = quotes.get(pick)
        picked_price = float(q.price) if q else float("nan")
        if q is None:
            notes.append(f"{pick} 无当日报价，价格未知")

    # 全池候选表：每只票当时的价、换手率、市值、均线，以及**没入选的确切原因**。
    # 这张表是审计的主要证据——推送里只有结论，事后追问「为什么不是 X」只能靠它。
    small = set(mgate.dropped)
    capped_set = set(capped.index)
    tried = {c.symbol for c in candidates}
    no_data = set(stocks_missing)
    rows = []
    for raw in members["symbol"]:
        sym = str(raw)
        price = float(px_now.get(sym, np.nan)) if sym in px_now.index else float("nan")
        t20 = float(turn.mean.get(sym, np.nan)) if sym in turn.mean.index else float("nan")
        cap = float(mgate.caps.get(sym, np.nan)) if sym in mgate.caps.index else float("nan")
        ok_stack, mas = ma_of(sym)
        if sym in no_data:
            reason = "no_local_data"
        elif not np.isfinite(price):
            reason = "no_quote"
        elif sym in small:
            reason = "small_mcap"
        elif not np.isfinite(t20):
            reason = "no_turnover"
        elif sym in capped_set:
            reason = "turn_cap"
        elif sym not in tried:
            reason = "not_in_top3"
        elif not ok_stack:
            reason = "stack_fail"
        elif sym == pick:
            reason = ""
        else:
            reason = "not_selected"
        rows.append({
            "symbol": sym, "sector": sec_of.get(sym, ""),
            "price": price, "turnover20": t20, "mcap": cap,
            "mcap_src": str(mgate.srcs.get(sym, "")) if len(mgate.srcs) else "",
            "mcap_ok": sym in mgate.kept, "turn_ok": sym in turn_ok.index,
            "ma10": mas.get(10, np.nan), "ma20": mas.get(20, np.nan),
            "ma50": mas.get(50, np.nan), "stack_ok": ok_stack,
            "sector_rank": rank_of.get(sym, np.nan), "in_top3": sym in tried,
            "selected": sym == pick, "reason": reason,
        })
    pool_table = pd.DataFrame(rows)

    return Signal(asof=asof, sector=picked_sector, pick=pick, price=picked_price,
                  adj_price=float(px.at[asof, pick]) if pick and pick in px.columns else float("nan"),
                  sector_scores=sec_score, candidates=candidates,
                  turnover_median=turnover_mod.median_of(turn), notes=notes,
                  data_asof=asof, factors=factors,
                  pool_size=len(members),
                  pool_as_of=str(members["as_of"].dropna().iloc[0])
                  if "as_of" in members.columns and members["as_of"].notna().any() else "",
                  min_mcap=mcap_mod.MIN_MCAP, pool_table=pool_table,
                  data_lag_days=lag_days, data_last_bar=last_bar)


def top_sectors(signal: Signal, n: int = 3) -> list[tuple[str, float]]:
    return [(str(k), float(v)) for k, v in signal.sector_scores.head(n).items()]
