"""股票池：美股市值前 N 的选股清单，按日期快照留档。

数据源优先级：
1. nasdaq_marketcap（默认）：Nasdaq 官方 screener 接口，全市场约 7000 只，按 marketCap
   降序取前 N。免费、无需 key，实测可直接拉取。
2. ishares_iwb：iShares 罗素1000 官方持仓 CSV。口径更正统（真正的罗素1000成分），
   但 2026-10 起该 ajax 端点稳定返回 HTML 反爬页，解析会失败并自动降级到方案 1。

两个源的差异：Nasdaq 全市场包含 ADR（TSM/BABA 等），罗素1000 不含。除此之外前 1000
名高度重叠，实际差异在几十只量级。universe 表里保留 country 字段便于事后过滤。
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from quant.config import get_settings

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

UNIVERSE_COLUMNS = [
    "rank",
    "symbol",
    "name",
    "sector",
    "market_cap",
    "weight",
    "price",
    "exchange",
    "country",
    "asof",
]


def _universe_dir() -> Path:
    path = get_settings().paths.data_path / "meta" / "universe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_file() -> Path:
    return _universe_dir() / "universe_latest.parquet"


def _session() -> requests.Session:
    cfg = get_settings()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*"})
    if getattr(cfg, "proxy", None):
        session.proxies = {"http": cfg.proxy, "https": cfg.proxy}
    return session


def _normalize_ticker(raw) -> str:
    """iShares/Nasdaq 用 BRK.B，yfinance 用 BRK-B；其余原样保留。"""
    return str(raw).strip().strip('"').upper().replace(".", "-")


def _to_number(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(r"[$,%\s]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def _drop_non_stock(df: pd.DataFrame) -> pd.DataFrame:
    cfg = get_settings()
    keywords = getattr(cfg.universe, "exclude_name_keywords", []) or []
    mask = pd.Series(True, index=df.index)

    for kw in keywords:
        mask &= ~df["name"].astype(str).str.contains(rf"\b{kw}\b", case=False, regex=True, na=False)

    mask &= df["symbol"].astype(str).str.match(r"^[A-Z][A-Z0-9\-]{0,9}$")
    return df[mask]


# ---------------------------------------------------------------- Nasdaq screener
def fetch_nasdaq_marketcap(top_n: int = 1000) -> tuple[pd.DataFrame, str]:
    cfg = get_settings()
    session = _session()
    url_tpl = cfg.universe.nasdaq_url

    rows: list[dict] = []
    offset = 0
    total = None
    for _ in range(12):
        url = url_tpl.format(offset=offset)
        resp = session.get(url, timeout=90)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        table = data.get("table") or {}
        batch = table.get("rows") or []
        if total is None:
            total = int(data.get("totalrecords") or 0)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if total and offset >= total:
            break
        time.sleep(0.4)

    if not rows:
        raise RuntimeError("Nasdaq screener 没有返回任何数据")

    raw = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "symbol": raw.get("symbol", pd.Series(dtype=str)).map(_normalize_ticker),
            "name": raw.get("name", pd.Series(dtype=str)).astype(str),
            "sector": raw.get("sector", pd.Series(dtype=str)),
            "market_cap": _to_number(raw.get("marketCap", pd.Series(dtype=str))),
            "weight": pd.NA,
            "price": _to_number(raw.get("lastsale", pd.Series(dtype=str))),
            "exchange": raw.get("exchange", pd.Series(dtype=str)),
            "country": raw.get("country", pd.Series(dtype=str)),
        }
    )

    out = out[(out["market_cap"].fillna(0) > 0) & out["symbol"].ne("")]
    out = _drop_non_stock(out)
    out = out.drop_duplicates("symbol").sort_values("market_cap", ascending=False).head(top_n)
    out.insert(0, "rank", range(1, len(out) + 1))
    out["asof"] = datetime.today().date().isoformat()

    log.info("Nasdaq 全市场 %d 条，取市值前 %d", len(rows), len(out))
    return out.reset_index(drop=True), out["asof"].iloc[0]


# ---------------------------------------------------------------- iShares IWB
def _split_meta(text: str) -> tuple[list[str], str | None]:
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines[:60]):
        if line.split(",")[0].strip().strip('"') == "Ticker":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("响应里找不到 Ticker 表头，疑似被反爬成 HTML")

    asof = None
    for line in lines[:header_idx]:
        match = re.search(r"(\w{3} \d{2}, \d{4})", line)
        if match:
            asof = datetime.strptime(match.group(1), "%b %d, %Y").date().isoformat()
            break
    return lines[header_idx:], asof


def parse_holdings(text: str, top_n: int = 1000) -> tuple[pd.DataFrame, str | None]:
    body, asof = _split_meta(text)
    df = pd.read_csv(io.StringIO("\n".join(body)), dtype=str, on_bad_lines="skip")
    df.columns = [str(c).strip() for c in df.columns]

    def pick(*names: str) -> pd.Series:
        for name in names:
            for col in df.columns:
                if col.lower() == name.lower():
                    return df[col]
        return pd.Series([pd.NA] * len(df), index=df.index)

    out = pd.DataFrame(
        {
            "symbol": pick("Ticker").map(_normalize_ticker),
            "name": pick("Name").astype(str),
            "sector": pick("Sector"),
            "market_cap": _to_number(pick("Market Value")),
            "weight": _to_number(pick("Weight (%)")),
            "price": _to_number(pick("Price")),
            "exchange": pick("Exchange"),
            "country": pd.NA,
        }
    )

    out = out[out["symbol"].ne("") & out["symbol"].ne("-")]
    asset_class = pick("Asset Class").fillna("").astype(str).str.lower()
    out = out[asset_class.str.contains("equity") | asset_class.eq("")]
    out = out.drop_duplicates("symbol").sort_values("market_cap", ascending=False, na_position="last")
    out = out.head(top_n)
    out.insert(0, "rank", range(1, len(out) + 1))
    out["asof"] = asof or datetime.today().date().isoformat()
    return out.reset_index(drop=True), asof


# ---------------------------------------------------------------- entry
def build(top_n: int | None = None, force: bool = False) -> pd.DataFrame:
    cfg = get_settings()
    top_n = top_n or cfg.universe.top_n
    target = latest_file()

    if target.exists() and not force:
        age_h = (time.time() - target.stat().st_mtime) / 3600
        if age_h < cfg.universe.refresh_hours:
            return pd.read_parquet(target)

    df: pd.DataFrame | None = None
    asof: str | None = None

    if cfg.universe.source == "ishares_iwb":
        try:
            resp = _session().get(cfg.universe.ishares_url, timeout=60)
            resp.raise_for_status()
            df, asof = parse_holdings(resp.text, top_n=top_n)
            if len(df) < min(200, top_n):
                raise ValueError(f"只解析出 {len(df)} 行，判为反爬页")
        except Exception as exc:
            log.warning("iShares 源不可用(%s)，降级为 Nasdaq 市值前 %d", exc, top_n)
            df = None

    if df is None:
        df, asof = fetch_nasdaq_marketcap(top_n)

    df = df.reindex(columns=UNIVERSE_COLUMNS)
    df.to_parquet(target, index=False)
    if asof:
        df.to_parquet(_universe_dir() / f"asof={asof}.parquet", index=False)
    return df


def load_latest() -> pd.DataFrame:
    path = latest_file()
    if not path.exists():
        raise FileNotFoundError("全市场快照尚未构建（应由 data job 的 paper_close_update 生成）")
    return pd.read_parquet(path)


def get_symbols(top_n: int | None = None) -> list[str]:
    df = load_latest()
    if top_n:
        df = df.head(top_n)
    return df["symbol"].tolist()
