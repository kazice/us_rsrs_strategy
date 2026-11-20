"""模拟盘当期信号：把回测里「月末最后交易日那一期」单独拎出来算。

与回测（``scripts/sector_stock_rsrs.py``）的唯一区别是**不需要下一期**：
回测循环是 ``for k in range(k0, len(me)-1)``，必须知道下一个调仓日才能算持有期收益，
所以最后一期永远不出信号；实盘只要「现在该买哪只」，没有这段依赖。

口径逐项对齐（任何一项不同都会让信号偏离定稿）：
- 板块层 RSRS：同一份 ``rsrs_panel_fast``，N=20 / M=40 / strict；
- 牛熊：SPY 收盘 vs MA200，过去 21 个交易日跌破占比 ≥50% 判熊（``--regime-ma 200
  --regime-window 21``）；样本不足按牛市处理，与回测一致；
- 熊市：不碰个股，买「板块 RSRS 前 3 ∩ 120 日加权斜率前 3」里 RSRS 最强的 ETF，
  RSRS 不 >0 就空仓（``--bear-etf-only --bear-rank both --bear-min-score 0
  --bear-intersect-top 3 --bear-slope-window 120``）；
- 市值门槛：候选池先按 **500 亿美元**过滤（``--min-mcap 500``，见 ``quant.live.mcap``），
  市值用**当日真实股本**（全市场快照 → 换手率反推 → SEC 申报，逐级兜底并留来源标记）；
- 个股层排序：``turn20² / turn120`` 降序（``--turn-rank-mode ratio``），turn20 超 20%
  上限的票当期剔除；turn120 至少 60 天有效值；
- 趋势闸（``--trend-mode strong``）五条全 AND：MA20 > MA60；MA20 与 MA60 的 5 日斜率为正；
  收盘 > MA50；MA50 > MA200；ADX(14) > 20。任一 NaN 或不过即判不合格；
- 下探：按板块 RSRS 顺序最多看 3 个板块，每板块看排序分前 3 只；
- 全部不合格 → 当期空仓（牛市由 ``live_signal`` 的逐日补选继续找，见 docs/PAPER_TRADING.md）。

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

# 趋势闸（回测 --trend-mode strong 的口径）
STACK = (20, 60)            # 均线多头排列：MA20 > MA60
SLOPE_MAS = (20, 60)        # 只查这两条均线的斜率（回测 --slope-mas 20,60）
STACK_SLOPE = 5             # 斜率 = 当前均线 − 5 个交易日前同一条均线（回测 --stack-slope 5）
ABOVE_MA = 50               # 收盘必须站上 MA50（--trend-above 50）
CROSS = (50, 200)           # MA50 > MA200（--trend-cross 50,200，只是大小关系，不是交叉事件）
ADX_LEN = 14                # --adx-len 默认 14
ADX_MIN = 20.0              # 严格大于（--adx-min 20）

# 牛熊（--regime-ma 200 --regime-window 21）
REGIME_MA = 200
REGIME_WINDOW = 21
REGIME_BEAR_RATIO = 0.5

# 熊市 ETF（--bear-etf-only --bear-rank both --bear-intersect-top 3
#           --bear-slope-window 120 --bear-min-score 0）
BEAR_INTERSECT_TOP = 3
BEAR_SLOPE_WINDOW = 120
BEAR_MIN_SCORE = 0.0

# 换手率（--turn-cap 0.20 --turn-rank-mode ratio）
TURN_CAP = 0.20
TURN_RATIO_WINDOW = 120     # turn120 的窗口
TURN_RATIO_MIN_DAYS = 60    # turn120 的 min_periods（回测 rolling(120, min_periods=60)）
TURNMA_TOP = 3
TURNMA_SECTORS = 3

# 预热：MA200 要 200 个交易日，约 290 个自然日；留到 520 自然日（≈345 个交易日）才够斜率+交叉
HISTORY_DAYS = 520
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
    """一个个股候选：排序分、两个换手率、趋势闸的每一项实际取值。"""

    symbol: str
    sector: str
    sector_rank: int
    score: float                                   # turn20² / turn120，回测的排序键
    turnover: float                                # turn20（已过 20% 上限）
    turnover120: float = float("nan")
    gate: dict[str, float] = field(default_factory=dict)   # ma20/s20/ma60/s60/ma50/ma200/adx
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
    requested_asof: pd.Timestamp | None = None   # 调用方请求的日期；asof 是实际口径日
    pool_table: pd.DataFrame = field(default_factory=pd.DataFrame)   # 全池候选表，供日志落盘
    bull: bool = True              # 牛熊判定结果（False = 熊市分支）
    bear_ratio: float = float("nan")   # 过去 N 日 SPY 跌破 MA200 的占比
    pick_kind: str = ""            # "stock" / "etf" / ""（空仓）
    bear_scores: pd.Series | None = None   # 熊市双榜交集（RSRS ∩ 120 日斜率），供推送展示

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
                     ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, float], str, pd.Timestamp]:
    """把信号日那根 bar 补齐，返回 (px, hi, lo, 复权因子, 口径来源, 实际口径日)。

    以数据自身为准，三级降级，不猜、不中止：

    1. ``daily``：日线里已经有请求日这根 bar（收盘后跑、或历史时点复现）→ 直接用日线。
       报价是盘中快照，拿它覆盖已确定的收盘数据只会把 bar 改小。
    2. ``quote``：日线只到前一交易日，且报价确实是请求日这个交易日的（取 ``Quote.ts`` 的
       ET 日期），并且带真实日内区间（high > low，排除开盘前的 `high=low=昨收`）→ 用报价
       合成当日 bar。这是盘中 15:30 决策的正常路径。
    3. ``fallback``：请求日还没有任何当日数据（周末、盘前、数据源没翻页、请求的是未来日期）
       → 退回**最近一根真实日线**，口径日改成那一天，并在推送里写明。宁可报旧口径的信号，
       也不拿一份不属于当日的报价去造 bar——那会在面板尾部复制一根前值，20 日回归的 R² 被
       拉向 1，板块排序整体改写（实测 11-13 的 XLP 1.06 会被算成 0.44，MNST 变 APP）。

    复权因子取最近一个交易日的 ``adj_close / close``：报价是未复权价，乘上它才和面板里的
    adj_close 同口径。除权当天因子会跳变，但一天之内不影响排序。
    """
    req = pd.Timestamp(asof).normalize()
    last_bar = pd.Timestamp(px.index.max()).normalize() if len(px.index) else None
    if last_bar is None:
        raise SystemExit(f"{req} 之前没有历史行情，无法计算信号")

    session = _quote_session(quotes)
    range_cover, checked = _quote_range_cover(quotes)

    if last_bar >= req:
        eff, src = req, "daily"
    elif (session == req.date()
          and (checked < 20 or range_cover >= QUOTE_RANGE_MIN_COVER)):
        eff, src = req, "quote"
    else:
        eff, src = last_bar, "fallback"

    px = px[px.index <= eff].copy()
    hi = hi[hi.index <= eff].copy()
    lo = lo[lo.index <= eff].copy()
    last_close = store.load_panel(field="close", symbols=list(px.columns),
                                  start=str((eff - pd.Timedelta(days=20)).date()),
                                  end=str(eff.date())).ffill()
    if last_close.empty:
        raise SystemExit("取不到最近收盘价，无法计算复权因子")
    factor = (px.iloc[-1] / last_close.iloc[-1]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    factors = {s: float(factor[s]) for s in factor.index}

    if src != "quote":
        return px, hi, lo, factors, src, eff

    row_px, row_hi, row_lo = {}, {}, {}
    for s, q in quotes.items():
        if s not in factor.index:
            continue
        f = float(factor[s])
        row_px[s] = q.price * f
        row_hi[s] = max(q.high, q.price) * f
        row_lo[s] = min(q.low or q.price, q.price) * f

    idx = px.index.append(pd.DatetimeIndex([eff]))
    px = px.reindex(idx).ffill()
    hi = hi.reindex(idx).ffill()
    lo = lo.reindex(idx).ffill()
    for s, v in row_px.items():
        px.at[eff, s] = v
    for s, v in row_hi.items():
        hi.at[eff, s] = v
    for s, v in row_lo.items():
        lo.at[eff, s] = v
    return px, hi, lo, factors, src, eff


def _gate_panels(px: pd.DataFrame, hi: pd.DataFrame, lo: pd.DataFrame,
                 cols: list[str]) -> dict[str, object]:
    """预先算好 strong 趋势闸要用的全部面板，逐票查表即可。

    逐票现算（500 只 × 每条均线一次 rolling）会把一次信号拖到几十秒，且无法保证与回测
    完全同口径；这里一次性向量化算完，min_periods 全部照抄回测：
    排列/斜率的均线用 ``max(3, w//2)``，MA50/MA200/交叉均线用**完整窗口**（不做半截均线）。
    """
    from scripts.sector_stock_rsrs import adx_panel

    sub = px[cols]
    stack = {w: sub.rolling(w, min_periods=max(3, w // 2)).mean() for w in STACK}
    slope = {w: stack[w] - stack[w].shift(STACK_SLOPE) for w in SLOPE_MAS}
    return {
        "stack": stack,
        "slope": slope,
        "above": sub.rolling(ABOVE_MA, min_periods=ABOVE_MA).mean(),
        "cross_fast": sub.rolling(CROSS[0], min_periods=CROSS[0]).mean(),
        "cross_slow": sub.rolling(CROSS[1], min_periods=CROSS[1]).mean(),
        "adx": adx_panel(hi[cols], lo[cols], sub, ADX_LEN),
    }


def _gate(gp: dict[str, object], asof: pd.Timestamp, sym: str,
          px: pd.DataFrame) -> tuple[bool, dict[str, float], str]:
    """按回测 ``--trend-mode strong`` 逐条判定，返回 (通过?, 各指标值, 首个不过的原因)。"""
    def val(panel, key) -> float:
        if panel is None or sym not in panel.columns or asof not in panel.index:
            return float("nan")
        return float(panel.at[asof, sym])

    v: dict[str, float] = {}
    stack = gp["stack"]
    slope = gp["slope"]
    for w in STACK:
        v[f"ma{w}"] = val(stack[w], w)
    for w in SLOPE_MAS:
        v[f"s{w}"] = val(slope[w], w)
    v[f"ma{ABOVE_MA}"] = val(gp["above"], ABOVE_MA)
    v[f"ma{CROSS[0]}"] = val(gp["cross_fast"], CROSS[0])
    v[f"ma{CROSS[1]}"] = val(gp["cross_slow"], CROSS[1])
    v["adx"] = val(gp["adx"], "adx")
    v["price"] = float(px.at[asof, sym]) if sym in px.columns and asof in px.index else float("nan")

    if not np.isfinite(v["price"]):
        return False, v, "当日无收盘价"
    prev, prev_w, name = None, None, ""
    for w in STACK:
        m = v[f"ma{w}"]
        if not np.isfinite(m):
            return False, v, f"MA{w} 预热不足"
        if prev is not None and not (prev > m):
            return False, v, f"MA{prev_w} ≤ MA{w}（非多头排列）"
        prev, prev_w = m, w
    for w in SLOPE_MAS:
        if not np.isfinite(v[f"s{w}"]) or v[f"s{w}"] <= 0:
            return False, v, f"MA{w} 的 {STACK_SLOPE} 日斜率 ≤ 0"
    if not np.isfinite(v[f"ma{ABOVE_MA}"]) or not (v["price"] > v[f"ma{ABOVE_MA}"]):
        return False, v, f"收盘未站上 MA{ABOVE_MA}"
    if not (np.isfinite(v[f"ma{CROSS[0]}"]) and np.isfinite(v[f"ma{CROSS[1]}"])):
        return False, v, f"MA{CROSS[0]}/MA{CROSS[1]} 预热不足"
    if not (v[f"ma{CROSS[0]}"] > v[f"ma{CROSS[1]}"]):
        return False, v, f"MA{CROSS[0]} ≤ MA{CROSS[1]}"
    if not np.isfinite(v["adx"]) or not (v["adx"] > ADX_MIN):
        return False, v, f"ADX {v['adx']:.1f} ≤ {ADX_MIN:g}" if np.isfinite(v["adx"]) else "ADX 预热不足"
    return True, v, "通过全部趋势闸"


def compute(asof: pd.Timestamp, quotes: dict[str, Quote],
            quote_notes: dict[str, str] | None = None,
            allow_snapshot_ahead: bool = False,
            today_turnover: pd.Series | None = None,
            regime: bool | None = None,
            ) -> Signal:
    """算当期目标持仓。``quotes`` 至少要覆盖 11 只板块 ETF + 候选个股。

    ``allow_snapshot_ahead`` 只给 ``live_audit --replay`` 用（历史时点复算时允许用
    今天的全市场快照取股本），正常出信号必须保持 False，否则会引入前视。

    ``today_turnover`` 同样只给重放用：历史时点的当日换手率直接取面板值，不从报价合成。

    ``regime`` 只给牛市补选用：补选窗口内的分支冻结在调仓日那一天（True = 牛市），
    中途牛熊翻转也不改——回测的 ``--bull-refill`` 就是在信号日判定的牛市里逐日重挑的。
    """
    from scripts.sector_stock_rsrs import rsrs_panel_fast, weighted_slope_panel

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
    requested_asof = asof
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

    px, hi, lo, factors, bar_src, eff_asof = _append_live_row(px, hi, lo, quotes, asof)
    if eff_asof != requested_asof:
        notes.append(
            f"请求日 {requested_asof:%Y-%m-%d} 还没有当日数据（日线最新 {last_bar:%Y-%m-%d}，"
            f"报价交易日 {_quote_session(quotes)}）：本次按 {eff_asof:%Y-%m-%d} 的口径计算"
        )
    elif bar_src == "daily":
        notes.append(f"信号日 {eff_asof:%Y-%m-%d} 已有实际日线，按日线口径计算（未用盘中报价覆盖）")
    asof = eff_asof
    lag_days = trading_lag_days(last_bar, asof)
    if lag_days:
        notes.append(f"⚠ 行情面板最新 {last_bar:%Y-%m-%d}，落后信号日 {lag_days} 个交易日："
                     f"板块 RSRS 与均线用的是旧收盘，本期信号仅供参考")

    etf_rsrs = rsrs_panel_fast(hi[SECTOR_ETFS], lo[SECTOR_ETFS], RSRS_N, RSRS_M, "strict")
    if asof not in etf_rsrs.index or etf_rsrs.loc[asof].dropna().empty:
        raise SystemExit(f"{asof} 板块层 RSRS 全为 NaN，预热不足或今天是数据空洞")

    sec_all = etf_rsrs.loc[asof].dropna().sort_values(ascending=False)
    lead_sector = str(sec_all.index[0])

    # 牛熊：SPY 站上 MA200 = 牛；用「过去 21 个交易日跌破占比 ≥50%」判熊，避免单日穿越就翻转。
    # 统计量 NaN（预热不足）按牛市处理——回测 is_bull 同口径，数据少不能成为买 ETF 的理由。
    if BENCH not in px.columns:
        raise SystemExit(f"本地没有 {BENCH} 的行情，无法判定牛熊")
    spy_ma = px[BENCH].rolling(REGIME_MA, min_periods=max(20, REGIME_MA // 2)).mean()
    below = (px[BENCH] < spy_ma).astype(float).where(spy_ma.notna())
    bear_ratio = below.rolling(REGIME_WINDOW, min_periods=REGIME_WINDOW).mean()
    ratio = float(bear_ratio.at[asof]) if asof in bear_ratio.index else float("nan")
    bull = True if not np.isfinite(ratio) else bool(ratio < REGIME_BEAR_RATIO)
    notes.append(f"{'牛市' if bull else '熊市'}：SPY 过去 {REGIME_WINDOW} 个交易日跌破 "
                 f"MA{REGIME_MA} 的占比 "
                 + (f"{ratio:.0%}" if np.isfinite(ratio) else "不可算（预热不足，按牛市）")
                 + f"，判熊阈值 {REGIME_BEAR_RATIO:.0%}")
    if regime is not None and bool(regime) != bull:
        notes.append(f"补选窗口沿用它开启时的分支（{'牛市' if regime else '熊市'}），"
                     f"今日口径已翻转为{'牛市' if bull else '熊市'}，本期不改分支")
        bull = bool(regime)

    # 换手率先按**全池**算，再做市值过滤。顺序不能反：被市值剔掉的票也要留下换手率，
    # 否则日志里答不出「它当时换手率排第几、离入选差多远」。
    turn = turnover_mod.build(stocks, asof, quotes, quote_notes or {},
                              today_panel=today_turnover)
    turn120 = turnover_mod.mean_n(stocks, asof, quotes,
                                  window=TURN_RATIO_WINDOW,
                                  min_days=TURN_RATIO_MIN_DAYS,
                                  today_panel=today_turnover)
    capped = turn.mean[turn.mean > TURN_CAP]
    if len(capped):
        notes.append(f"换手率超 {TURN_CAP:.0%} 上限被剔除："
                     + "、".join(f"{s} {v:.2%}" for s, v in capped.items()))
    turn_ok = turn.mean[turn.mean <= TURN_CAP]

    # 排序键 = turn20² / turn120（回测 --turn-rank-mode ratio）：分子要求「本身就够活跃」，
    # 分子/分母要求「比它自己的半年常态更活跃」。turn120 有效天数不到的票直接不参与排序。
    base120 = turn120.reindex(turn_ok.index)
    score = (turn_ok ** 2 / base120.where(base120 > 0)).dropna()
    if len(score) < len(turn_ok):
        short = [s for s in turn_ok.index if s not in score.index]
        notes.append(f"{len(short)} 只票 turn120 不足 {TURN_RATIO_MIN_DAYS} 天或为 0，"
                     f"不参与排序：{' '.join(short[:10])}{' ...' if len(short) > 10 else ''}")

    # 市值门槛：候选池先过 500 亿美元（与回测 --min-mcap 500 同口径）。
    # 价用盘中实时价（未复权），股本由 成交量/换手率 反推；取不到市值的票保留，
    # 与回测「拿不到市值不剔除」保持一致。没有当日报价的票没有价，自然落在保留侧。
    px_now = pd.Series({s: q.price for s, q in quotes.items()}, dtype="float64")
    mgate = mcap_mod.gate(stocks, px_now, asof,
                          allow_snapshot_ahead=allow_snapshot_ahead)
    if mgate.active:
        notes += mgate.notes
    eligible = [s for s in mgate.kept if s in score.index]
    if not eligible:
        notes.append("市值/换手率过滤后候选池为空，本期必然空仓")

    cols = [s for s in stocks if s in px.columns]
    gp = _gate_panels(px, hi, lo, cols) if (bull and cols) else None

    candidates: list[Candidate] = []
    pick, picked_sector, picked_price, pick_kind = "", lead_sector, float("nan"), ""
    rank_of: dict[str, int] = {}
    gate_of: dict[str, tuple[bool, dict[str, float], str]] = {}
    bear_scores: pd.Series | None = None

    if bull:
        for rank_s, sec in enumerate(sec_all.index[:TURNMA_SECTORS], 1):
            pool = [s for s in eligible if sec_of.get(s) == sec]
            if not pool:
                notes.append(f"板块 {sec}（第 {rank_s} 名）无可排序个股，跳过")
                continue
            top = score.reindex(pool).dropna().sort_values(ascending=False).head(TURNMA_TOP)
            for raw_s in top.index:
                sym = str(raw_s)
                rank_of[sym] = rank_s
                ok, vals, why = _gate(gp, asof, sym, px)
                gate_of[sym] = (ok, vals, why)
                candidates.append(Candidate(
                    sym, str(sec), rank_s, float(score[sym]), float(turn_ok[sym]),
                    float(base120.get(sym, np.nan)), vals, ok, why))
                if ok and not pick:
                    pick, picked_sector, pick_kind = sym, str(sec), "stock"
            if pick:
                break
    else:
        # 熊市：完全不碰个股。买「板块 RSRS 前 3 ∩ 120 日加权斜率前 3」里 RSRS 最强的 ETF；
        # 交集为空、或最强板块 RSRS 不 >0 → 当期空仓（连抱团的板块都没有，买 ETF 只是跟着阴跌）。
        etf_slope = weighted_slope_panel(px[SECTOR_ETFS], BEAR_SLOPE_WINDOW)
        slope_now = (etf_slope.loc[asof].reindex(sec_all.index).dropna()
                     if asof in etf_slope.index else pd.Series(dtype="float64"))
        if len(slope_now):
            rsrs_top = set(sec_all.index[:BEAR_INTERSECT_TOP])
            slope_top = set(slope_now.sort_values(ascending=False).index[:BEAR_INTERSECT_TOP])
            bear_scores = sec_all[[s for s in sec_all.index
                                   if s in rsrs_top and s in slope_top]]
        else:
            bear_scores = sec_all.iloc[0:0]
        if bear_scores.empty:
            notes.append(f"熊市：RSRS 前 {BEAR_INTERSECT_TOP} 与 {BEAR_SLOPE_WINDOW} 日斜率前 "
                         f"{BEAR_INTERSECT_TOP} 无交集，本期空仓")
        elif not (float(bear_scores.iloc[0]) > BEAR_MIN_SCORE):
            notes.append(f"熊市：最强板块 {bear_scores.index[0]} 的 RSRS "
                         f"{float(bear_scores.iloc[0]):.2f} 未超过 {BEAR_MIN_SCORE:g}，本期空仓")
        else:
            etf = str(bear_scores.index[0])
            pick, picked_sector, pick_kind = etf, etf, "etf"
            notes.append(f"熊市：买板块 ETF {etf}（RSRS {float(bear_scores.iloc[0]):.2f}，"
                         f"同时在 RSRS 前 {BEAR_INTERSECT_TOP} 与 "
                         f"{BEAR_SLOPE_WINDOW} 日斜率前 {BEAR_INTERSECT_TOP}）")

    if pick:
        q = quotes.get(pick)
        if q is not None:
            picked_price = float(q.price)
        elif pick in px.columns and asof in px.index and np.isfinite(px.at[asof, pick]):
            picked_price = float(px.at[asof, pick])      # 重放/无报价：用当日复权收盘兜底
            notes.append(f"{pick} 无当日报价，参考价改用当日收盘（复权口径）")
        else:
            notes.append(f"{pick} 无当日报价，价格未知")

    # 全池候选表：每只票当时的价、两个换手率、排序分、市值、趋势闸每一项，以及**没入选的
    # 确切原因**。这张表是审计的主要证据——推送里只有结论，事后追问「为什么不是 X」只能靠它。
    small = set(mgate.dropped)
    capped_set = set(capped.index)
    tried = set(rank_of)
    no_data = set(stocks_missing)
    rows = []
    for raw in members["symbol"]:
        sym = str(raw)
        price = float(px_now.get(sym, np.nan)) if sym in px_now.index else float("nan")
        if not np.isfinite(price) and sym in px.columns and asof in px.index:
            price = float(px.at[asof, sym])
        t20 = float(turn.mean.get(sym, np.nan)) if sym in turn.mean.index else float("nan")
        t120 = float(base120.get(sym, np.nan)) if sym in base120.index else float("nan")
        tsc = float(score.get(sym, np.nan)) if sym in score.index else float("nan")
        cap = float(mgate.caps.get(sym, np.nan)) if sym in mgate.caps.index else float("nan")
        ok_gate, gvals, gwhy = gate_of.get(sym, (False, {}, ""))
        if not bull:
            reason = "selected_bear" if (pick and sym == pick) else "bear_regime"
        elif sym in no_data:
            reason = "no_local_data"
        elif not np.isfinite(price):
            reason = "no_quote"
        elif sym in small:
            reason = "small_mcap"
        elif not np.isfinite(t20):
            reason = "no_turnover"
        elif sym in capped_set:
            reason = "turn_cap"
        elif not np.isfinite(tsc):
            reason = "no_turn120"
        elif sym not in tried:
            reason = "not_in_top3"
        elif not ok_gate:
            reason = "gate_fail"
        elif sym == pick:
            reason = ""
        else:
            reason = "not_selected"
        rows.append({
            "symbol": sym, "sector": sec_of.get(sym, ""),
            "price": price, "turnover20": t20, "turnover120": t120, "score": tsc,
            "mcap": cap,
            "mcap_src": str(mgate.srcs.get(sym, "")) if len(mgate.srcs) else "",
            "mcap_ok": sym in mgate.kept, "turn_ok": sym in turn_ok.index,
            "ma20": gvals.get("ma20", np.nan), "s20": gvals.get("s20", np.nan),
            "ma60": gvals.get("ma60", np.nan), "s60": gvals.get("s60", np.nan),
            "ma50": gvals.get("ma50", np.nan), "ma200": gvals.get("ma200", np.nan),
            "adx": gvals.get("adx", np.nan), "gate_ok": ok_gate, "gate_why": gwhy,
            "sector_rank": rank_of.get(sym, np.nan), "in_top3": sym in tried,
            "selected": sym == pick, "reason": reason,
        })
    pool_table = pd.DataFrame(rows)

    return Signal(asof=asof, sector=picked_sector, pick=pick, price=picked_price,
                  adj_price=float(px.at[asof, pick]) if pick and pick in px.columns else float("nan"),
                  sector_scores=sec_all, candidates=candidates,
                  turnover_median=turnover_mod.median_of(turn), notes=notes,
                  data_asof=asof, factors=factors,
                  pool_size=len(members),
                  pool_as_of=str(members["as_of"].dropna().iloc[0])
                  if "as_of" in members.columns and members["as_of"].notna().any() else "",
                  min_mcap=mcap_mod.MIN_MCAP, pool_table=pool_table,
                  data_lag_days=lag_days, data_last_bar=last_bar,
                  requested_asof=requested_asof,
                  bull=bull, bear_ratio=ratio, pick_kind=pick_kind,
                  bear_scores=bear_scores)


def top_sectors(signal: Signal, n: int = 3) -> list[tuple[str, float]]:
    return [(str(k), float(v)) for k, v in signal.sector_scores.head(n).items()]
