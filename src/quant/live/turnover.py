"""盘中换手率：历史 19 天用 ws 面板，第 20 天用实时报价，合成与回测同口径的 20 日均值。

口径对齐（这一步是整条链路最容易错的地方）
------------------------------------------
回测里 ``--turn-tail 0.48`` 的作用是：信号日当天用的是**完整日**换手率，而调仓发生在
收盘前 30 分钟，当天量能只走完约一半，于是把当天那一项乘 0.48：

    (sum19_full + 0.48 × today_full) / 20

实盘在 15:30 拿到的报价本来就是「累计到此刻的成交量 ÷ 总股本」，即
``today_1530 ≈ 0.48 × today_full``。所以**实盘不能再乘一次 0.48**，否则等于折了两遍：

    (sum19_full + today_1530) / 20
    ^ 与回测的 (sum19_full + 0.48 × today_full) / 20 等价

模块对外只暴露「合成后的 20 日均值」，折算系数固定为 1.0 并在日志里写明，
避免有人把回测的 0.48 又抄进来。

多源兜底
--------
腾讯与 ws 面板同源（都是成交量 ÷ 当日总股本），作为主源；新浪按 市值/价格 反推股本，
口径一致但市值可能含多股份类别，作为校验与兜底。两源成交量比值超阈值时只在备注里标出，
不改数值——数值一律以主源为准，异常交给推送提醒人工确认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant.data import store
from quant.live.quotes import Quote

log = logging.getLogger(__name__)

TURNOVER_FILE = "curated/fundamentals/turnover_daily.parquet"
WINDOW = 20
MIN_DAYS = 10          # 与回测 rolling(20, min_periods=10) 一致
TAIL_FACTOR = 1.0      # 实盘当天已是「截至此刻」口径，见模块 docstring
MAX_TURNOVER = 1.0     # 单日换手上限：超过 100% 必是股本/成交量字段错了


def turnover_path():
    return store.data_root() / TURNOVER_FILE


def load_history(symbols: list[str], asof: pd.Timestamp,
                 lookback: int = WINDOW) -> tuple[pd.DataFrame, dict[str, pd.Timestamp]]:
    """取 asof 之前（不含）的最近 lookback-1 个交易日的换手率，index=date、columns=symbol。"""
    path = turnover_path()
    if not path.exists():
        raise SystemExit(f"换手率面板不存在：{path}（应由 data 分支提供，见 docs/HANDOFF.md 第 5 节）")
    tv = pd.read_parquet(path)
    tv = tv[tv["symbol"].isin(symbols)].copy()
    # 落盘防御：面板是 data 分支多段拼出来的，历史段曾经出现过 449,828 倍的垃圾值
    # （股本字段取成 100 股量级）。越界值按缺失处理，宁可这只票少一天，也不让
    # turn20²/turn120 的排序被一个错数量级的数带偏。
    tv["turnover"] = pd.to_numeric(tv["turnover"], errors="coerce")
    bad = (tv["turnover"] <= 0) | (tv["turnover"] > MAX_TURNOVER)
    if bool(bad.any()):
        log.warning("换手率面板 %d 行越界（要求 0 < x ≤ %.0f%%），按缺失处理",
                    int(bad.sum()), MAX_TURNOVER * 100)
        tv.loc[bad, "turnover"] = np.nan
    panel = (tv.pivot(index="date", columns="symbol", values="turnover")
             .sort_index())
    panel = panel[panel.index < asof].tail(max(lookback - 1, 1))
    if panel.empty:
        raise SystemExit(f"{asof} 之前没有任何换手率数据")
    return panel.reindex(columns=symbols), {c: panel.index[-1] for c in panel.columns}


@dataclass
class TurnoverResult:
    """20 日均换手率面板 + 每只票的数据来源说明。"""

    mean: pd.Series                     # index=symbol，20 日均值（小数，0.25 = 25%）
    today: pd.Series                    # 当日（截至此刻）换手率
    days: pd.Series                     # 参与均值的有效天数
    notes: dict[str, str] = field(default_factory=dict)
    asof: pd.Timestamp | None = None


def _today_from_panel(today_panel: pd.Series, symbols: list[str]) -> pd.Series:
    """重放专用：asof 当天的换手率直接取面板值（已是小数，与报价除以 100 同口径）。

    只给历史时点复算用——实盘当天面板里还没有这一行，必须用报价合成。
    """
    want = [s for s in symbols if s in today_panel.index]
    vals = today_panel.reindex(want).astype("float64")
    return vals.where(np.isfinite(vals) & (vals > 0)).dropna()


def mean_n(symbols: list[str], asof: pd.Timestamp, quotes: dict[str, Quote],
           window: int = WINDOW, min_days: int = MIN_DAYS,
           today_panel: pd.Series | None = None) -> pd.Series:
    """任意窗口的均换手率（含当天那一项，口径与回测 rolling(window) 一致）。"""
    symbols = [s for s in dict.fromkeys(symbols)]
    hist, _ = load_history(symbols, asof, window)
    hist = hist.sort_index().ffill()
    if today_panel is not None:
        today = _today_from_panel(today_panel, symbols)
    else:
        today_vals = {}
        for s in symbols:
            q = quotes.get(s)
            if q is None or q.turnover_pct <= 0:
                continue
            if q.turnover_pct > MAX_TURNOVER * 100:
                continue
            today_vals[s] = q.turnover_pct / 100.0
        today = pd.Series(today_vals, dtype="float64")
    have = [s for s in symbols if s in today.index]
    sub = hist.reindex(columns=have)
    cnt = sub.notna().sum() + 1
    total = sub.fillna(0.0).sum() + today.reindex(have) * TAIL_FACTOR
    return (total / cnt).where(cnt >= min_days).dropna()


def build(symbols: list[str], asof: pd.Timestamp,
          quotes: dict[str, Quote],
          quote_notes: dict[str, str] | None = None,
          window: int = WINDOW, min_days: int = MIN_DAYS,
          today_panel: pd.Series | None = None) -> TurnoverResult:
    """合成 20 日均换手率。缺当日报价的票会被剔除并在 notes 里写明原因。"""
    symbols = [s for s in dict.fromkeys(symbols)]
    hist, _ = load_history(symbols, asof, window)
    # 与回测一致：先按交易日索引 ffill，再算均值。停牌/缺数日沿用前值，而不是把该日当空。
    hist = hist.sort_index().ffill()
    notes: dict[str, str] = dict(quote_notes or {})

    if today_panel is not None:
        today = _today_from_panel(today_panel, symbols)
    else:
        today_vals: dict[str, float] = {}
        for s in symbols:
            q = quotes.get(s)
            if q is None:
                notes[s] = notes.get(s, "无当日报价")
                continue
            if q.turnover_pct <= 0:
                notes[s] = "当日换手率缺失（报价无成交量或股本字段异常）"
                continue
            if q.turnover_pct > MAX_TURNOVER * 100:
                notes[s] = f"当日换手率异常（{q.turnover_pct:.0f}% > 100%），按缺失处理"
                continue
            today_vals[s] = q.turnover_pct / 100.0
        today = pd.Series(today_vals, dtype="float64")
    have = [s for s in symbols if s in today.index]
    sub = hist.reindex(columns=have)

    cnt = sub.notna().sum() + 1                     # 含当天
    total = sub.fillna(0.0).sum() + today.reindex(have) * TAIL_FACTOR
    mean = (total / cnt).where(cnt >= min_days)

    dropped = mean[mean.isna()].index.tolist()
    for s in dropped:
        notes.setdefault(s, f"有效天数不足 {min_days} 天")
    mean = mean.dropna()

    if len(mean) < len(symbols):
        log.info("换手率覆盖 %d/%d 只，缺 %d 只（多为新股或数据源缺口）",
                 len(mean), len(symbols), len(symbols) - len(mean))

    return TurnoverResult(mean=mean, today=today, days=cnt.reindex(mean.index),
                          notes=notes, asof=asof)


def sanity_flags(result: TurnoverResult, cap: float = 0.20) -> list[str]:
    """把「离群/被上限挡掉」的票列出来，供推送正文解释为什么没选它。"""
    if result.mean.empty:
        return []
    out = []
    over = result.mean[result.mean > cap].sort_values(ascending=False)
    for s, v in over.items():
        out.append(f"{s} 20日均换手 {v:.2%} > 上限 {cap:.0%}")
    return out


def median_of(result: TurnoverResult) -> float:
    return float(np.nanmedian(result.mean.to_numpy())) if len(result.mean) else float("nan")
