"""模拟盘候选池的市值门槛：用**当日真实市值**，不再是股本估算。

为什么要有这一步
----------------
按换手率排序会系统性偏向指数尾部的小票：换手率的分母是股本，小票股本小、噪声大，
而且天然高换手，于是这类票会长期占据换手率榜首（NCLH 市值 65~100 亿美元、日换手 5%，
2023-11 ~ 2026-10 一直排在 XLY 第一）。500 亿美元门槛是**剔除分母失真的样本**，不是调参：
阈值扫描（100/200/300/500）显示收益对阈值没有单调关系，所以阈值不能按收益挑，
只能按「小盘换手率不可信」这条经济理由定。

市值怎么算（2026-11 改口径）
---------------------------
旧实现：股本 = ``成交量 ÷ 当日换手率`` 在**3 年窗口上的中位数**，再乘当日未复权价。
它是估算，实测有三处系统性误差（数据分支 2026-11-13 全池 503 只实测）：

1. **股本在变，3 年中位数是过时的**：回购/增发/分拆会让当期中位数偏离当期股本，
   500 亿门槛附近有 14 只判定与真实市值相反（DD 中位数口径 510 亿 / 实际 204 亿，
   CVNA 487 亿 / 实际 798 亿，KMB 363 亿 / 实际 672 亿）。
2. **单日反推噪声大**：近 20 日反推股本的变异系数中位数 2.8%、95 分位 9.3%、最坏 61%，
   拿当日值直接判门槛会让判定来回跳。
3. **估算无法审计**：只有一个数字，事后答不出「这个市值是怎么来的」。

现在按优先级取「当时能拿到的真实股本」，每条都留来源标记供审计：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | ``snapshot`` | Nasdaq 官方 screener 的 ``market_cap / price``（每日刷新，前 1000 名）|
| 2 | ``ws``       | ``成交量 ÷ 当日换手率`` 在**最近 20 个交易日**的中位数（贴近当期股本，抗单日噪声）|
| 3 | ``sec``      | SEC 申报股本，取 ``filed ≤ asof`` 的最后一条；超过 400 天视为过期不用 |
| 4 | 空           | 三个来源都没有 → 不剔除（与回测「拿不到市值不保留剔除」一致），并在推送里露出 |

用快照的 ``market_cap / price`` 反推股本而不是直接用 ``market_cap``，是为了让市值跟着**当天
的价格**走：快照是收盘后刷的，第二天盘中决策时价格已经变了，直接用快照市值会滞后一天。

口径说明（与回测的关系）
------------------------
- 估值价一律用**未复权**价：复权价在拆股后会被缩放，市值会算错；
- 判定用**严格大于**：等于 500 亿不算达标；
- 取不到市值的票**不剔除**（交给换手率那一步自然淘汰），与回测一致。
- ⚠ 回测的 ``--min-mcap`` 用的是「全样本中位数」（含未来数据）或当日快照，与这里的
  分层取数不完全等价。改口径后门槛附近的判定会与旧回测有差异（实测 500 亿档 503 只里
  有 14 只翻转，主要是拆股/股本变动导致的估算偏差），引用历史收益时要说明这一点。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant.data import store, universe
from quant.live import turnover as turnover_mod

log = logging.getLogger(__name__)

MIN_MCAP = 500e8            # 500 亿美元，与回测 --min-mcap 500 对齐
WS_DAYS = 20               # 反推股本的窗口（交易日）
WS_MIN_DAYS = 3            # 窗口内至少几个有效点才算可信
WS_LOOKBACK_DAYS = 90      # 取成交量面板的自然日跨度（覆盖 20 个交易日 + 长假）
SEC_MAX_AGE_DAYS = 400     # SEC 申报股本的有效期，超期视为过期（缺季报的票会踩到）
SNAPSHOT_DAYS_AHEAD = 2    # 快照多新算「可用」：容器时区为 UTC+8，asof 可能比 ET 交易日早 1~2 天

SRC_SNAPSHOT = "snapshot"
SRC_WS = "ws"
SRC_SEC = "sec"


@dataclass
class Gate:
    """市值过滤结果。kept 交给后续换手率排序，其余只用于解释与排查。"""

    kept: list[str]
    dropped: list[str] = field(default_factory=list)   # 市值不达标
    unknown: list[str] = field(default_factory=list)   # 取不到市值（保留）
    caps: pd.Series = field(default_factory=pd.Series)  # 每只的市值（美元）
    srcs: pd.Series = field(default_factory=pd.Series)  # 每只的市值来源（snapshot/ws/sec/空）
    notes: list[str] = field(default_factory=list)
    snapshot_as_of: str = ""

    @property
    def active(self) -> bool:
        return bool(self.dropped or self.unknown)

    def summary(self) -> str:
        """「快照 494 / 反推 9 / SEC 0 / 缺失 0」这类来源分布，写进推送正文。"""
        return source_summary(self.srcs, self.snapshot_as_of)


def source_summary(srcs: pd.Series, snapshot_as_of: str = "") -> str:
    if srcs is None or srcs.empty:
        return "无"
    counts = srcs.fillna("").replace("", "missing").value_counts()
    label = {SRC_SNAPSHOT: "快照", SRC_WS: "反推", SRC_SEC: "SEC", "missing": "缺失"}
    parts = [f"{label.get(k, k)} {v}" for k, v in counts.items()]
    tail = f"（快照 as of {snapshot_as_of}）" if snapshot_as_of else ""
    return " / ".join(parts) + tail


def _empty() -> pd.Series:
    return pd.Series(dtype="float64")


def _snapshot_shares(symbols: list[str], asof: pd.Timestamp,
                     allow_ahead: bool = False) -> tuple[pd.Series, str]:
    """从全市场快照反推股本（市值 ÷ 价格）。返回 (股本, 快照日期)。

    快照日期比 asof 超前 1~2 天属正常（容器时区 UTC+8 会把「抓取日」标成次日），
    但超前太多说明这份快照来自未来（用 --now 复现历史时点会踩到），此时弃用，避免前视。
    ``allow_ahead=True`` 只给 ``live_audit --replay`` 用：它要验的是「同输入同输出」，
    用今天的快照重算历史时点属于已知偏差，比退回到反推口径更接近当时的原值。
    """
    path = universe.latest_file()
    if not path.exists():
        log.info("全市场快照不存在，本轮市值跳过快照口径：%s", path)
        return _empty(), ""
    try:
        uni = pd.read_parquet(path)
    except Exception as exc:                        # noqa: BLE001  快照坏了不该中断信号
        log.warning("读取全市场快照失败（%s），本轮跳过快照口径", type(exc).__name__)
        return _empty(), ""

    if uni.empty or "market_cap" not in uni.columns or "price" not in uni.columns:
        return _empty(), ""
    uni = uni.copy()
    uni["symbol"] = uni["symbol"].astype(str).str.upper().str.replace(".", "-", regex=False)
    snap_asof = (pd.to_datetime(uni["asof"].astype(str), errors="coerce").max()
                 if "asof" in uni.columns else pd.NaT)
    label = f"{snap_asof:%Y-%m-%d}" if pd.notna(snap_asof) else ""
    if (not allow_ahead and pd.notna(snap_asof)
            and snap_asof > asof + pd.Timedelta(days=SNAPSHOT_DAYS_AHEAD)):
        log.info("全市场快照 as of %s 晚于信号日 %s，弃用以免前视", label, f"{asof:%Y-%m-%d}")
        return _empty(), ""

    shares = (uni["market_cap"] / uni["price"]).replace([np.inf, -np.inf], np.nan)
    shares.index = pd.Index(uni["symbol"].values, name="symbol")
    shares = shares[shares > 0]
    shares = shares[~shares.index.duplicated(keep="first")]
    return shares.reindex(list(symbols)).astype("float64"), label


def _ws_shares(symbols: list[str], asof: pd.Timestamp) -> pd.Series:
    """用「成交量 ÷ 当日换手率」反推股本，取最近 ``WS_DAYS`` 个交易日的中位数。

    只取最近 20 天而不是 3 年：股本会随回购/增发/分拆变化，窗口越长越偏离当期值。
    取中位数而不是当日值：反推值日间波动大（近 20 日变异系数中位数 2.8%、95 分位 9.3%），
    而换手率面板里由实时报价写入的那几天又整体偏低约一成，单日值会让门槛判定来回跳。
    """
    if not symbols:
        return _empty()
    path = turnover_mod.turnover_path()
    if not path.exists():
        log.warning("换手率面板不存在：%s，本轮跳过反推口径", path)
        return _empty()

    asof = pd.Timestamp(asof)
    start = (asof - pd.Timedelta(days=WS_LOOKBACK_DAYS)).date().isoformat()
    vol = store.load_panel(field="volume", symbols=list(symbols),
                           start=start, end=asof.date().isoformat()).sort_index()
    if vol.empty:
        log.warning("取不到成交量面板，本轮跳过反推口径")
        return _empty()

    tv = pd.read_parquet(path)
    if tv.empty:
        return _empty()
    tvp = (tv.pivot(index="date", columns="symbol", values="turnover").sort_index()
           .reindex(index=vol.index, columns=vol.columns))
    implied = (vol / tvp.where(tvp > 0)).replace([np.inf, -np.inf], np.nan)
    window = implied.tail(WS_DAYS)
    enough = window.notna().sum() >= WS_MIN_DAYS
    return window.median().where(enough).astype("float64")


def _sec_shares(symbols: list[str], asof: pd.Timestamp) -> pd.Series:
    """SEC 申报股本（``filed ≤ asof`` 的最后一条）。只作最后兜底。

    这份表有洞（部分标的的最后一条停在多年前，个别值还是 1 股这种占位），
    所以①只在快照与反推都取不到时才用；②``filed`` 超过 ``SEC_MAX_AGE_DAYS`` 视为过期。
    """
    path = store.data_root() / "curated" / "fundamentals" / "shares_outstanding.parquet"
    if not path.exists():
        return _empty()
    try:
        so = pd.read_parquet(path)
    except Exception as exc:                        # noqa: BLE001
        log.warning("读取 SEC 股本失败（%s）", type(exc).__name__)
        return _empty()
    if so.empty:
        return _empty()

    so = so.copy()
    so["symbol"] = so["symbol"].astype(str).str.upper().str.replace(".", "-", regex=False)
    so["filed"] = pd.to_datetime(so["filed"], errors="coerce")
    so = so[(so["val"] > 1000) & so["filed"].notna() & (so["filed"] <= asof)]
    if so.empty:
        return _empty()
    last = so.sort_values("filed").groupby("symbol").tail(1).set_index("symbol")
    fresh = last["filed"] >= asof - pd.Timedelta(days=SEC_MAX_AGE_DAYS)
    return last.loc[fresh, "val"].reindex(list(symbols)).astype("float64")


def share_sources(symbols: list[str], asof: pd.Timestamp,
                  allow_snapshot_ahead: bool = False) -> tuple[pd.Series, pd.Series, str]:
    """按优先级装配股本。返回 (股本, 来源, 快照日期)。"""
    syms = list(dict.fromkeys(str(s) for s in symbols))
    shares = pd.Series(np.nan, index=pd.Index(syms, name="symbol"), dtype="float64")
    srcs = pd.Series("", index=shares.index, dtype="object")

    def fill(values: pd.Series, tag: str) -> None:
        """只用非空值回填，并在来源列打标签（values 的索引可以是子集）。"""
        vals = values.reindex(shares.index)
        hits = vals.dropna().index
        if len(hits):
            shares[hits] = vals[hits]
            srcs[hits] = tag

    snap, snap_asof = _snapshot_shares(syms, asof, allow_snapshot_ahead)
    if len(snap):
        fill(snap, SRC_SNAPSHOT)

    todo = shares[shares.isna()].index.tolist()
    if todo:
        fill(_ws_shares(todo, asof), SRC_WS)

    todo = shares[shares.isna()].index.tolist()
    if todo:
        fill(_sec_shares(todo, asof), SRC_SEC)

    return shares, srcs, snap_asof


def shares_outstanding(symbols: list[str], asof: pd.Timestamp) -> pd.Series:
    """总股本（股）。保留旧接口：只要股本、不要来源时用它。"""
    shares, _, _ = share_sources(symbols, asof)
    return shares


def gate(symbols: list[str], prices: pd.Series, asof: pd.Timestamp,
         min_mcap: float = MIN_MCAP, allow_snapshot_ahead: bool = False) -> Gate:
    """按市值门槛过一遍候选池。``prices`` 是未复权价（盘中实时价或收盘价）。"""
    syms = [s for s in dict.fromkeys(str(s) for s in symbols)]
    if min_mcap <= 0 or not syms:
        return Gate(kept=syms)

    shares, srcs, snap_asof = share_sources(syms, asof, allow_snapshot_ahead)
    px = prices.reindex(syms).astype(float)
    caps = (px * shares).replace([np.inf, -np.inf], np.nan).dropna()

    dropped = sorted(c for c in caps.index if caps[c] <= min_mcap)
    unknown = [s for s in syms if s not in caps.index]
    kept = [s for s in syms if s not in set(dropped)]

    notes = [f"市值来源：{source_summary(srcs, snap_asof)}"]
    if dropped:
        head = " ".join(f"{s}({caps[s] / 1e8:.0f}亿)" for s in dropped[:12])
        notes.append(f"市值不足 {min_mcap / 1e8:.0f} 亿美元被剔除 {len(dropped)} 只：{head}"
                     f"{' ...' if len(dropped) > 12 else ''}")
    if unknown:
        notes.append(f"{len(unknown)} 只取不到市值（无报价或无股本），按回测口径保留："
                     f"{' '.join(unknown[:10])}{' ...' if len(unknown) > 10 else ''}")

    return Gate(kept=kept, dropped=dropped, unknown=unknown, caps=caps, srcs=srcs,
                notes=notes, snapshot_as_of=snap_asof)
