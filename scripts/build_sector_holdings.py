"""拉取 11 个 SPDR 板块 ETF 的当前成分股，建立「个股 → 板块」归属表。

为什么不用行业分类表：本项目要的是「这个板块 ETF 里到底装了什么」，直接取 SPDR 官方
holdings 最贴近回测实际；26 类行业分类与 11 个 GICS 板块不是一一对应（例如房地产板块
在行业表里没有独立类别）。

产物
----
config/ref/us_sector_holdings.csv   symbol / sector_etf / sector_name / weight / as_of

⚠ 这是**当前快照**。历史成分股变动无法还原：用今天的成分股回测过去几年，
被剔除的股票不在表里、新纳入的一直存在，存在幸存者偏差与前视偏差。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "ref" / "us_sector_holdings.csv"
URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
       "etfs/us/holdings-daily-us-en-{code}.xlsx")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

SECTORS = {
    "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费", "XLP": "必需消费",
    "XLE": "能源", "XLI": "工业", "XLB": "材料", "XLRE": "房地产", "XLU": "公用事业",
    "XLC": "通信服务",
}


def fetch(code: str) -> pd.DataFrame:
    resp = requests.get(URL.format(code=code.lower()), timeout=60, headers=HEADERS)
    resp.raise_for_status()
    raw = pd.read_excel(io.BytesIO(resp.content), header=None)

    # 头部三行是 Fund Name / Ticker Symbol / Holdings: As of <date>
    meta = " ".join(str(x) for x in raw.iloc[0:3, 1].tolist())
    m = re.search(r"As of\s+(\d{1,2}-\w{3}-\d{4})", meta)
    as_of = pd.to_datetime(m.group(1), format="%d-%b-%Y").date().isoformat() if m else ""

    hdr = raw.index[raw[0].astype(str).str.strip().str.lower() == "name"]
    if len(hdr) == 0:
        raise ValueError(f"{code} 找不到表头行")
    df = pd.read_excel(io.BytesIO(resp.content), header=int(hdr[0]))
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    df = df.dropna(subset=["Ticker"])
    df["Ticker"] = df["Ticker"].astype(str).str.strip().str.upper()
    df = df[df["Ticker"].str.fullmatch(r"[A-Z][A-Z.\-]{0,5}")]
    out = pd.DataFrame({
        "symbol": df["Ticker"],
        "sector_etf": code,
        "sector_name": SECTORS[code],
        "weight": pd.to_numeric(df.get("Weight"), errors="coerce"),
        "as_of": as_of,
    })
    return out.drop_duplicates("symbol")


def main() -> int:
    parts = []
    for code in SECTORS:
        try:
            df = fetch(code)
            parts.append(df)
            print(f"{code} {SECTORS[code]:5s} {len(df):4d} 只（as of {df['as_of'].iloc[0]}）")
        except Exception as e:  # noqa: BLE001
            print(f"{code} 失败：{type(e).__name__}: {str(e)[:100]}")

    allx = pd.concat(parts, ignore_index=True)
    dup = allx[allx.duplicated("symbol", keep=False)]
    if len(dup):
        print(f"\n⚠ {dup['symbol'].nunique()} 只出现在多个板块（保留权重最大的那个）：")
        print("  " + " ".join(sorted(dup["symbol"].unique())[:20]))
    allx = allx.sort_values("weight", ascending=False).drop_duplicates("symbol")
    allx = allx.sort_values(["sector_etf", "weight"], ascending=[True, False])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    allx.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n合计 {len(allx)} 只（去重后），各板块：")
    print(allx.groupby(["sector_etf", "sector_name"]).size().to_string())
    print(f"\n产出 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
