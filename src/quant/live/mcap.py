"""模拟盘候选池的市值门槛：与回测 ``--min-mcap 500`` 严格同口径。

为什么必须有这一步
------------------
按换手率排序会系统性偏向指数尾部的小票：换手率的分母是股本，小票股本小、噪声大，
而且天然高换手，于是这类票会长期占据换手率榜首（NCLH 市值 65~100 亿美元、日换手 5%，
2023-11 ~ 2026-10 一直排在 XLY 第一）。500 亿美元门槛是**剔除分母失真的样本**，不是调参：
阈值扫描（100/200/300/500）显示收益对阈值没有单调关系，所以阈值不能按收益挑，
只能按「小盘换手率不可信」这条经济理由定。

口径对齐（任何一项不同都会让实盘信号偏离定稿回测）
--------------------------------------------------
- 市值 = 当日**未复权**价 × 股本。价用盘中实时价（回测用信号日收盘价，二者同义）；
- 股本**不读 SEC 表**，用「成交量 ÷ 当日换手率」在全样本上的中位数反推——回测就是这么
  算的（``scripts/sector_stock_rsrs.py`` 的 ``--min-mcap`` 分支），两边必须同一份数，
  否则会出现「回测里达标、实盘里不达标」的撕裂；
- 缺换手率的票用 universe 快照的 市值/价格 兜底；
- **取不到市值的票不剔除**（交给换手率那一步自然淘汰），与回测一致；
- 判定用**严格大于**：等于 500 亿不算达标。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant.data import store, universe
from quant.live import turnover as turnover_mod

log = logging.getLogger(__name__)

MIN_MCAP = 500e8          # 500 亿美元，与回测 --min-mcap 500 对齐
SHARES_DAYS = 1095        # 反推股本的回看窗口（3 年）。股本变化慢，窗口长短对中位数影响极小


@dataclass
class Gate:
    """市值过滤结果。kept 交给后续换手率排序，其余只用于解释与排查。"""

    kept: list[str]
    dropped: list[str] = field(default_factory=list)   # 市值不达标
    unknown: list[str] = field(default_factory=list)   # 取不到市值（保留）
    caps: pd.Series = field(default_factory=pd.Series)  # 每只的市值（美元）
    notes: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.dropped or self.unknown)


def shares_outstanding(symbols: list[str], asof: pd.Timestamp,
                       days: int = SHARES_DAYS) -> pd.Series:
    """反推总股本（股）：成交量 ÷ 当日换手率的中位数，缺换手率的用快照 市值/价格 兜底。"""
    if not symbols:
        return pd.Series(dtype="float64")
    asof = pd.Timestamp(asof)
    start = (asof - pd.Timedelta(days=days)).date().isoformat()
    end = asof.date().isoformat()

    vol = store.load_panel(field="volume", symbols=symbols, start=start, end=end).sort_index()
    if vol.empty:
        log.warning("取不到成交量面板，市值门槛本期跳过")
        return pd.Series(dtype="float64")

    path = turnover_mod.turnover_path()
    if not path.exists():
        log.warning("换手率面板不存在：%s，市值门槛本期跳过", path)
        return pd.Series(dtype="float64")
    tv = pd.read_parquet(path)
    tv = tv[tv["symbol"].isin(vol.columns)]
    tvp = tv.pivot(index="date", columns="symbol", values="turnover").sort_index()
    tvp = tvp.reindex(index=vol.index, columns=vol.columns)

    sh = (vol / tvp.where(tvp > 0)).median()

    # 快照兜底：与回测同一份 universe，缺换手率的票靠 市值/价格 补
    try:
        snap_path = universe.latest_file()
        if snap_path.exists():
            uni = pd.read_parquet(snap_path)
            uni["symbol"] = uni["symbol"].astype(str).str.upper()
            snap = (uni["market_cap"] / uni["price"]).replace([np.inf, -np.inf], np.nan)
            snap.index = uni["symbol"].values
            sh = sh.fillna(snap.reindex(sh.index))
    except Exception as exc:                        # noqa: BLE001
        log.warning("读取 universe 快照失败（%s），仅用换手率反推股本", type(exc).__name__)

    return sh.replace([np.inf, -np.inf], np.nan)


def gate(symbols: list[str], prices: pd.Series, asof: pd.Timestamp,
         min_mcap: float = MIN_MCAP) -> Gate:
    """按市值门槛过一遍候选池。``prices`` 是未复权价（盘中实时价或收盘价）。"""
    syms = [s for s in dict.fromkeys(symbols)]
    if min_mcap <= 0 or not syms:
        return Gate(kept=syms)

    sh = shares_outstanding(syms, asof)
    px = prices.reindex(syms).astype(float)
    caps = (px * sh.reindex(syms)).dropna()

    dropped = sorted(c for c in caps.index if caps[c] <= min_mcap)
    unknown = [s for s in syms if s not in caps.index]
    kept = [s for s in syms if s not in set(dropped)]

    notes: list[str] = []
    if dropped:
        head = " ".join(f"{s}({caps[s] / 1e8:.0f}亿)" for s in dropped[:12])
        notes.append(f"市值不足 {min_mcap / 1e8:.0f} 亿美元被剔除 {len(dropped)} 只：{head}"
                     f"{' ...' if len(dropped) > 12 else ''}")
    if unknown:
        notes.append(f"{len(unknown)} 只取不到市值（无报价或无换手率），按回测口径保留："
                     f"{' '.join(unknown[:10])}{' ...' if len(unknown) > 10 else ''}")

    return Gate(kept=kept, dropped=dropped, unknown=unknown, caps=caps, notes=notes)
