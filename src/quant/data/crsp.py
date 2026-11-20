"""CRSP 标准标普 500 数据（2020 之后）读取层。

数据来源：买来的 CRSP CSV，由 `scripts/import_crsp_sp500.py` 转成三个 parquet
（`data/curated/crsp_sp500/`）：
    daily.parquet       逐日行情（仅成分期内）
    membership.parquet  每只 PERMNO 的成分区间（point-in-time）
    symbols.parquet     PERMNO → 最新 ticker

相对本地 yfinance + westock 的差别（这正是回测收益虚高的根源）：
    ① 成分股是**当期**名单，不是今天的快照 → 没有幸存者偏差
    ② 换手率 = 成交量 ÷ 总股本，口径天然一致，不需要用「成交量÷换手率」反推股本
    ③ 市值 = DlyCap（当期真实市值），没有滚动中位数/快照兜底
    ④ 价格自带累计复权因子，拆股、分红都能还原

复权口径（实测确认）：CRSP 的 `DlyCumFacPr` 是**除数**。
ORLY 15:1 拆股当天，收盘价 1348.10 → 91.71，同时 `DlyCumFacPr` 由 15.0 → 1.0；
`close / facpr` 两边分别得 89.87 与 91.71，连续；`close * facpr` 会差 220 倍。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
CRSP_DIR = ROOT / "data" / "curated" / "crsp_sp500"

# 手工 GICS 归属：SPDR 快照里没有、但 SIC 会分错的历史成分股。
# 这些名字的 GICS 板块是稳定的公开信息，逐只写死比按 SIC 猜更可靠；
# 抽样核对时发现 SIC 把 CTVA 分到 XLP（应 XLB）、WBD/PARA 分到 XLI（应 XLC）。
SECTOR_OVERRIDE: dict[str, str] = {
    # 材料
    "CTVA": "XLB",
    # 医疗
    "OGN": "XLV", "CTLT": "XLV", "KVUE": "XLV", "WBA": "XLV",
    # 房地产（SIC 6798 会把 REIT 全分进金融）
    "VNO": "XLRE", "DRE": "XLRE", "EQR": "XLRE", "AVB": "XLRE",
    # 通信服务（媒体 / 电信 / 互动平台 / 广告）
    "PARA": "XLC", "PSKY": "XLC", "DISCA": "XLC", "DISCK": "XLC", "DISH": "XLC",
    "WBD": "XLC", "LUMN": "XLC", "MTCH": "XLC", "NLSN": "XLC", "IPG": "XLC",
    "TTD": "XLC", "TWTR": "XLC",
    # 信息技术（软件 / 网络设备）
    "CTXS": "XLK", "ATVI": "XLK", "EA": "XLK", "ANSS": "XLK", "PAYC": "XLK",
    "EPAM": "XLK", "CERN": "XLK", "DAY": "XLK", "DXC": "XLK", "JNPR": "XLK",
    # 可选消费（家电 / 零售）
    "WHR": "XLY", "POOL": "XLY", "LKQ": "XLY",
}

# SIC 大类 → SPDR 板块 ETF。只用于 CRSP 独有的历史成分股（本地 holdings 快照里没有的票），
# 命中不了的一律归 XLI（工业），并在 `sector_map` 的返回里留来源标记，便于排查。
SIC_TO_ETF: list[tuple[int, int, str]] = [
    (100, 999, "XLP"), (1000, 1099, "XLB"), (1100, 1399, "XLE"),
    (1400, 1499, "XLB"), (1500, 1799, "XLI"), (2000, 2199, "XLP"),
    (2200, 2399, "XLY"), (2400, 2499, "XLB"), (2500, 2599, "XLY"),
    (2600, 2699, "XLB"), (2700, 2799, "XLY"), (2800, 2899, "XLB"),
    (2900, 2999, "XLE"), (3000, 3199, "XLB"), (3200, 3399, "XLB"),
    (3400, 3599, "XLI"), (3600, 3699, "XLK"), (3700, 3799, "XLI"),
    (3800, 3899, "XLV"), (3900, 3999, "XLY"), (4000, 4499, "XLI"),
    (4500, 4599, "XLI"), (4600, 4899, "XLI"), (4900, 4999, "XLU"),
    (5000, 5199, "XLI"), (5200, 5399, "XLP"), (5400, 5999, "XLY"),
    (6000, 6199, "XLF"), (6200, 6299, "XLF"), (6300, 6499, "XLF"),
    (6500, 6599, "XLRE"), (6700, 6799, "XLF"), (7000, 7299, "XLY"),
    (7300, 7399, "XLI"), (7370, 7379, "XLK"), (7500, 7999, "XLY"),
    (8000, 8099, "XLV"), (8100, 8999, "XLI"), (9000, 9999, "XLI"),
]


def available() -> bool:
    return (CRSP_DIR / "daily.parquet").exists()


def _sym(s: pd.Series) -> pd.Series:
    return s.astype(str).str.upper().str.replace(".", "-", regex=False).str.strip()


@lru_cache(maxsize=1)
def daily() -> pd.DataFrame:
    """逐日行情长表（已算好复权价格、换手率、市值）。"""
    d = pd.read_parquet(CRSP_DIR / "daily.parquet")
    d["date"] = pd.to_datetime(d["DlyCalDt"])
    for c in ["DlyClose", "DlyHigh", "DlyLow", "DlyOpen", "DlyVol", "DlyCap",
              "ShrOut", "DlyCumFacPr", "DlyCumFacShr", "SICCD"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["symbol"] = _sym(d["Ticker"])
    d = d[(d["symbol"] != "NAN") & d["date"].notna()]

    fac = d["DlyCumFacPr"].where(d["DlyCumFacPr"] > 0)
    d["close"] = d["DlyClose"]
    d["adj_close"] = d["DlyClose"] / fac
    d["high"] = d["DlyHigh"] / fac
    d["low"] = d["DlyLow"] / fac
    d["volume"] = d["DlyVol"]
    d["shares"] = d["ShrOut"] * 1e3
    d["mcap"] = d["DlyCap"] * 1e3
    # 同一口径的原始量相除，复权因子自动约掉
    d["turnover"] = d["DlyVol"] / d["shares"].where(d["shares"] > 0)
    return d.sort_values(["symbol", "date"]).reset_index(drop=True)


@lru_cache(maxsize=1)
def membership() -> pd.DataFrame:
    m = pd.read_parquet(CRSP_DIR / "membership.parquet")
    m["start"] = pd.to_datetime(m["MbrStartDt"])
    m["end"] = pd.to_datetime(m["MbrEndDt"])
    sym = pd.read_parquet(CRSP_DIR / "symbols.parquet")
    sym["symbol"] = _sym(sym["ticker"])
    m["symbol"] = m["PERMNO"].map(sym.set_index("PERMNO")["symbol"])
    return m.dropna(subset=["symbol"]).sort_values(["symbol", "start"]).reset_index(drop=True)


def symbols_in_window(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[str]:
    """窗口内当过成分股的所有代码（含后来被剔除/退市的，这是修幸存者偏差的关键）。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    m = membership()
    hit = m[(m["start"] <= e) & ((m["end"].isna()) | (m["end"] >= s))]
    return sorted(hit["symbol"].unique())


def panel(field: str, symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """宽表：index=date，columns=symbol。field ∈ close/adj_close/high/low/volume。"""
    d = daily()
    d = d[d["symbol"].isin(symbols) & d["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    if d.empty:
        return pd.DataFrame()
    p = d.pivot_table(index="date", columns="symbol", values=field, aggfunc="last")
    return p.reindex(columns=[s for s in symbols if s in p.columns]).sort_index()


def turnover_panel(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    return panel("turnover", symbols, start, end)


def mcap_panel(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    return panel("mcap", symbols, start, end)


def membership_mask(cal: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    """逐日 point-in-time 成分掩码（date × symbol，bool）。"""
    m = membership()
    per = np.zeros((len(cal), len(symbols)), dtype=bool)
    pos = {s: i for i, s in enumerate(symbols)}
    for r in m.itertuples():
        i = pos.get(r.symbol)
        if i is None:
            continue
        lo = cal.searchsorted(r.start, side="left")
        hi = len(cal) if pd.isna(r.end) else cal.searchsorted(r.end, side="right")
        if hi > lo:
            per[lo:hi, i] = True
    return pd.DataFrame(per, index=cal, columns=symbols)


def sector_map(symbols: list[str]) -> pd.DataFrame:
    """symbol → 板块 ETF。优先用 SPDR holdings 快照，缺的按 SIC 大类补。"""
    h = pd.read_csv(ROOT / "config" / "ref" / "us_sector_holdings.csv")
    h["symbol"] = _sym(h["symbol"])
    known = dict(zip(h["symbol"], h["sector_etf"]))

    sic = (daily().dropna(subset=["SICCD"])
           .sort_values("date").groupby("symbol")["SICCD"].last())
    rows = []
    for s in symbols:
        if s in known:
            rows.append((s, known[s], "holdings"))
            continue
        if s in SECTOR_OVERRIDE:
            rows.append((s, SECTOR_OVERRIDE[s], "gics"))
            continue
        code = sic.get(s)
        etf = None
        if code and not pd.isna(code):
            for lo, hi, e in SIC_TO_ETF:
                if lo <= int(code) <= hi:
                    etf = e
                    break
        rows.append((s, etf or "XLI", "sic" if etf else "default"))
    return pd.DataFrame(rows, columns=["symbol", "sector_etf", "src"])
