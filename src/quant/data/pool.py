"""标普 500 成分股池（2021-01-01 起），security_id = CRSP PERMNO 为唯一主键。

数据由 `scripts/build_sp500_pool.py` 产出到 `data/curated/sp500_pool/`：
    universe.parquet     每只证券一行：ticker/板块/成分区间/退出方式/体检结论
    membership.parquet   security_id × [start, end] 成分区间（point-in-time）
    tickers.parquet      security_id × ticker × 区间（补外部历史时按区间换别名）
    bars.parquet         security_id × date：复权 OHLC + 原始 close + 量/股本/市值/换手 + src
    events.parquet       拆股/合股/分立（复权因子跳变）
    exits.parquet        退出方式与退市收益
    qc_issues.csv        逐票体检明细（warmup_days / external_rows 等）

`features.parquet`（security_id × date 长表）由 `scripts/build_pool_data_board.py` 另外产出，
行键与 bars 一一对应（早期预热段整行全 NaN 也保留），用 features() / feature_panel() 读取。

这是仓库里唯一的回测数据源（不再有 ticker 口径的第二套视图）：
    取数一律按 security_id，ticker 只是某一时点的别名、仅用于展示与外部行情对接；
    复权价已经用 CRSP 的累计因子除好，拆股/合股/分立、退市收益分别在 events / exits 里
    单独可查，不混在价格列里。

bars 的 `src` 列：
    crsp      CRSP 原始行（成分期内，权威）
    external  外部补来的「成分期之前」历史，只用于 MA200 等指标预热（is_member 恒 False），
              已按重叠区间缩放到 CRSP 的价格空间，比值中位≈1.0000
    MA200 这类指标直接用 panel()：按各证券自己的交易日序列算，预热段自然接在前面；
    能不能买仍然只看 membership_mask()，别拿 is_member=False 的行当持仓。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
POOL_DIR = ROOT / "data" / "curated" / "sp500_pool"

ADJ_FIELDS = ("adj_close", "adj_open", "adj_high", "adj_low",
              "close", "volume", "turnover", "mcap", "shares")


def available() -> bool:
    return (POOL_DIR / "bars.parquet").exists()


def _read(name: str) -> pd.DataFrame:
    """读主表；若存在 `<name>_ext.parquet`（2026 段延展）则一并合并。

    延展段由 `scripts/extend_pool_to_latest.py` 从本地行情 + 成分公告生成，src='local_ext'。
    主表保持 CRSP 原始内容不动，想只看标准数据时用 `bars(src='crsp')`。
    """
    df = pd.read_parquet(POOL_DIR / f"{name}.parquet")
    ext = POOL_DIR / f"{name}_ext.parquet"
    if ext.exists():
        e = pd.read_parquet(ext)
        cols = [c for c in df.columns if c in e.columns]
        df = pd.concat([df[cols], e[cols]], ignore_index=True)
    return df


@lru_cache(maxsize=1)
def universe() -> pd.DataFrame:
    u = _read("universe")
    u["security_id"] = u["security_id"].astype(str)
    for c in ("mbr_start", "mbr_end", "first_date", "last_date", "last_trade_date"):
        if c in u.columns:
            u[c] = pd.to_datetime(u[c])
    return u.sort_values(["security_id"]).reset_index(drop=True)


@lru_cache(maxsize=1)
def membership() -> pd.DataFrame:
    m = _read("membership")
    m["security_id"] = m["security_id"].astype(str)
    m["start"] = pd.to_datetime(m["start"])
    m["end"] = pd.to_datetime(m["end"])
    return m.sort_values(["security_id", "start"]).reset_index(drop=True)


@lru_cache(maxsize=1)
def tickers() -> pd.DataFrame:
    t = _read("tickers")
    t["security_id"] = t["security_id"].astype(str)
    t["start"] = pd.to_datetime(t["start"])
    t["end"] = pd.to_datetime(t["end"])
    return t.sort_values(["security_id", "start"]).reset_index(drop=True)


@lru_cache(maxsize=3)
def bars(src: str | None = None) -> pd.DataFrame:
    """逐日行情。

    src='crsp'     买来的标准数据（到 2025-12-31）
    src='external' 成分期之前补的预热段（外部源，缩放到 CRSP 价格空间）
    src='local_ext' 2025-12-31 之后用本地行情延展的段（scripts/extend_pool_to_latest.py）
    """
    b = _read("bars")
    b["security_id"] = b["security_id"].astype(str)
    b["date"] = pd.to_datetime(b["date"])
    if src:
        b = b[b["src"] == src]
    return b.sort_values(["security_id", "date"]).reset_index(drop=True)


@lru_cache(maxsize=1)
def events() -> pd.DataFrame:
    e = _read("events")
    e["security_id"] = e["security_id"].astype(str)
    e["date"] = pd.to_datetime(e["date"])
    return e.sort_values(["security_id", "date"]).reset_index(drop=True)


@lru_cache(maxsize=1)
def exits() -> pd.DataFrame:
    x = _read("exits")
    x["security_id"] = x["security_id"].astype(str)
    for c in ("last_date", "last_trade_date", "mbr_end"):
        if c in x.columns:
            x[c] = pd.to_datetime(x[c])
    return x.sort_values("last_date").reset_index(drop=True)


def ids(active_only: bool = False) -> list[str]:
    """池内 security_id；active_only=True 只取期末仍在指数内的。"""
    u = universe()
    if active_only:
        u = u[u["in_index"]]
    return u["security_id"].tolist()


def members_on(day: str | pd.Timestamp) -> list[str]:
    """某一天的 point-in-time 成分名单（security_id）。"""
    ts = pd.Timestamp(day)
    m = membership()
    hit = m[(m["start"] <= ts) & ((m["end"].isna()) | (m["end"] >= ts))]
    return sorted(hit["security_id"].unique())


def panel(field: str = "adj_close", security_ids: list[str] | None = None,
          start: str | pd.Timestamp | None = None,
          end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """宽表：index=date，columns=security_id。

    默认用复权收盘价。field 取 close 时是原始价（拆股日会看到 -50% 的台阶，别直接算收益）。
    index 是**全池交易日并集**（不同证券停牌/未上市的日子都在这条日历上），
    所以算 MA200 这类指标要按列各自 rolling，或者先 dropna 再 rolling，
    不要假设每一列在并集日历上是连续的——否则会把「别人独有的交易日」算成缺数据。
    """
    if field not in ADJ_FIELDS:
        raise ValueError(f"field 只能是 {ADJ_FIELDS}")
    b = bars()
    if security_ids is not None:
        b = b[b["security_id"].isin([str(s) for s in security_ids])]
    if start is not None:
        b = b[b["date"] >= pd.Timestamp(start)]
    if end is not None:
        b = b[b["date"] <= pd.Timestamp(end)]
    if b.empty:
        return pd.DataFrame()
    p = b.pivot_table(index="date", columns="security_id", values=field, aggfunc="last")
    return p.sort_index()


def membership_mask(cal: pd.DatetimeIndex, security_ids: list[str]) -> pd.DataFrame:
    """逐日 point-in-time 成分掩码（date × security_id，bool）。"""
    m = membership()
    ids_ = [str(s) for s in security_ids]
    per = np.zeros((len(cal), len(ids_)), dtype=bool)
    pos = {s: i for i, s in enumerate(ids_)}
    for r in m.itertuples():
        i = pos.get(r.security_id)
        if i is None:
            continue
        lo = cal.searchsorted(r.start, side="left")
        hi = len(cal) if pd.isna(r.end) else cal.searchsorted(r.end, side="right")
        if hi > lo:
            per[lo:hi, i] = True
    return pd.DataFrame(per, index=cal, columns=ids_)


def ids_in_window(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[str]:
    """窗口内当过成分股的所有 security_id（含后来被剔除/退市的，这是修幸存者偏差的关键）。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    m = membership()
    hit = m[(m["start"] <= e) & ((m["end"].isna()) | (m["end"] >= s))]
    return sorted(hit["security_id"].unique())


@lru_cache(maxsize=1)
def display_tickers() -> pd.Series:
    """security_id → 最新 ticker（只用于输出/排查；选股和持仓一律按 security_id）。"""
    t = tickers().sort_values("end").groupby("security_id").tail(1)
    return t.set_index("security_id")["ticker"]


def last_date() -> pd.Timestamp:
    """池内最后一天行情（CRSP 数据边界，回测区间不能超过它）。"""
    return pd.Timestamp(bars()["date"].max())


@lru_cache(maxsize=1)
def features() -> pd.DataFrame:
    """量化面板（security_id × date 长表），由 scripts/build_pool_data_board.py 生成。

    MA20/50/60/200、斜率、ADX14、±DI、turn20/turn120/turn_score、vol20、mom63/126。
    全部按各证券自己的交易日序列算，预热不足的前 N 根是 NaN。
    """
    f = pd.read_parquet(POOL_DIR / "features.parquet")
    f["security_id"] = f["security_id"].astype(str)
    f["date"] = pd.to_datetime(f["date"])
    return f.sort_values(["security_id", "date"]).reset_index(drop=True)


def feature_panel(field: str, security_ids: list[str] | None = None,
                  start: str | pd.Timestamp | None = None,
                  end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """量化字段的宽表：index=date，columns=security_id。

    index 与 panel() 一致，是所选证券的交易日并集；预热不足的日子整列都是 NaN，
    这些行不会被丢掉（mom126 的头 126 个交易日就是全 NaN），方便和其它面板按行对齐。
    """
    f = features()
    if field not in f.columns:
        raise ValueError(f"没有这个字段：{field}；可选 {[c for c in f.columns if c not in ('date', 'security_id')]}")
    if security_ids is not None:
        f = f[f["security_id"].isin([str(s) for s in security_ids])]
    if start is not None:
        f = f[f["date"] >= pd.Timestamp(start)]
    if end is not None:
        f = f[f["date"] <= pd.Timestamp(end)]
    if f.empty:
        return pd.DataFrame()
    p = f.pivot_table(index="date", columns="security_id", values=field, aggfunc="last")
    # pivot_table 会把「该字段全 NaN」的日期整行删掉，这里按交易日并集补回来
    return p.reindex(pd.DatetimeIndex(f["date"].drop_duplicates().sort_values())).sort_index()


def exit_returns() -> pd.Series:
    """退市收益（security_id → 末行 DlyRet），回测平仓时叠加，别让破产票按 0 算。"""
    x = exits()
    return x.set_index("security_id")["delisting_ret"].dropna()
