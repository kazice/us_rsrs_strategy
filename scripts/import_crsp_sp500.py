"""把买入的标普 500 标准数据（CRSP 口径 CSV）转成本地可用的 parquet。

源文件：`data/raw/crsp_sp500/*.csv`（由微信收到的 zip 解压得到，2.5 GB，44 列）。
关键列：
    INDNO            指数编号，1000500 = S&P 500
    MbrStartDt/MbrEndDt  成分股区间（逐行重复，可据此还原 point-in-time 名单）
    PERMNO           证券永久编号（跨 ticker 变更不变）
    DlyCalDt         交易日
    DlyPrc/DlyClose/DlyOpen/DlyHigh/DlyLow  未复权价（DlyPrc 为负表示无成交、按买卖中价）
    DlyCap           市值（千美元）
    ShrOut           总股本（千股）
    DlyVol           成交量（股）
    DlyRet           日总收益（含分红）
    DlyCumFacPr/DlyCumFacShr  累计复权因子

产出（`data/curated/crsp_sp500/`）：
    membership.parquet   PERMNO × 成分区间的去重清单
    daily.parquet        start 之后、处于成分期内的逐日行情（含市值/股本/换手率原料）
    symbols.parquet      PERMNO → 最新 Ticker 映射，用来和 bars_daily 对齐

用法：
    $env:PYTHONPATH='src'; python -m scripts.import_crsp_sp500 --start 2020-01-01
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SP500_INDNO = 1000500

DAILY_COLUMNS = [
    "PERMNO", "Ticker", "CUSIP", "SICCD", "DlyCalDt",
    "DlyPrc", "DlyPrcFlg", "DlyClose", "DlyOpen", "DlyHigh", "DlyLow",
    "DlyVol", "DlyCap", "ShrOut", "DlyRet",
    "DlyCumFacPr", "DlyCumFacShr", "TradingStatusFlg",
    "MbrStartDt", "MbrEndDt",
]


def find_csv(src: Path) -> Path:
    if src.is_file():
        return src
    hits = sorted(src.glob("*.csv"))
    if not hits:
        raise SystemExit(f"没找到 CSV：{src}")
    return hits[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="CRSP 标普500 标准数据 → parquet")
    ap.add_argument("--src", default=str(ROOT / "data" / "raw" / "crsp_sp500"))
    ap.add_argument("--out", default=str(ROOT / "data" / "curated" / "crsp_sp500"))
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    csv = find_csv(Path(args.src))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={args.threads}")
    read = (f"read_csv('{csv.as_posix()}', header=true, ignore_errors=true, "
            f"sample_size=200000)")

    t0 = time.time()
    # ① 成分区间去重清单（每只票可能有多段：调入→调出→再调入）
    # 同一区间会出现多行（Ticker/CUSIP 在部分行是空的），按区间去重后取非空值，
    # 否则同一天会被算成 800 多个「成分」（实测 2021-11-30 是 505 只 / 840 段）
    con.execute(f"""
        CREATE OR REPLACE TABLE mem AS
        SELECT PERMNO,
               max(Ticker) AS Ticker,
               max(CUSIP)  AS CUSIP,
               max(SICCD)  AS SICCD,
               MbrStartDt, MbrEndDt
        FROM {read}
        WHERE INDNO = {SP500_INDNO}
          AND (MbrEndDt IS NULL OR MbrEndDt >= DATE '{args.start}')
        GROUP BY PERMNO, MbrStartDt, MbrEndDt
    """)
    mem_n = con.execute("SELECT count(*), count(DISTINCT PERMNO) FROM mem").fetchone()
    print(f"成分区间：{mem_n[0]} 段 / {mem_n[1]} 只 PERMNO（{time.time() - t0:.0f}s）")
    con.execute(f"COPY (SELECT * FROM mem ORDER BY PERMNO, MbrStartDt) "
                f"TO '{out.joinpath('membership.parquet').as_posix()}' (FORMAT parquet)")

    # ② 逐日行情：只取 start 之后、且当天处于成分期内的行
    t1 = time.time()
    cols = ", ".join(DAILY_COLUMNS)
    con.execute(f"""
        CREATE OR REPLACE TABLE daily AS
        SELECT {cols}
        FROM {read}
        WHERE INDNO = {SP500_INDNO}
          AND DlyCalDt >= DATE '{args.start}'
          AND MbrStartDt <= DlyCalDt
          AND (MbrEndDt IS NULL OR MbrEndDt >= DlyCalDt)
          AND PERMNO IN (SELECT DISTINCT PERMNO FROM mem)
    """)
    n, permnos, dmin, dmax = con.execute(
        "SELECT count(*), count(DISTINCT PERMNO), min(DlyCalDt), max(DlyCalDt) FROM daily"
    ).fetchone()
    print(f"逐日行情：{n:,} 行 / {permnos} 只 / {dmin} ~ {dmax}（{time.time() - t1:.0f}s）")
    con.execute(f"COPY (SELECT * FROM daily ORDER BY PERMNO, DlyCalDt) "
                f"TO '{out.joinpath('daily.parquet').as_posix()}' (FORMAT parquet)")

    # ③ PERMNO → 最新 Ticker（ticker 会变，取最后一个非空）
    con.execute(f"""
        CREATE OR REPLACE TABLE sym AS
        SELECT PERMNO, arg_max(Ticker, DlyCalDt) AS ticker,
               min(DlyCalDt) AS first_date, max(DlyCalDt) AS last_date, count(*) AS n_rows
        FROM daily WHERE Ticker IS NOT NULL AND TRIM(Ticker) <> ''
        GROUP BY 1 ORDER BY 1
    """)
    con.execute(f"COPY (SELECT * FROM sym) TO '{out.joinpath('symbols.parquet').as_posix()}' (FORMAT parquet)")

    # ④ 体检：数据指纹
    q = con.execute("""
        SELECT
          count(*) FILTER (WHERE DlyCap IS NULL) AS cap_null,
          count(*) FILTER (WHERE ShrOut IS NULL) AS shr_null,
          count(*) FILTER (WHERE DlyVol IS NULL) AS vol_null,
          count(*) FILTER (WHERE DlyClose IS NULL) AS close_null,
          count(*) FILTER (WHERE DlyPrc < 0) AS price_neg,
          count(*) FILTER (WHERE TradingStatusFlg <> 'A') AS not_active
        FROM daily
    """).fetchone()
    print("空值/异常：市值缺 %d、股本缺 %d、成交量缺 %d、收盘缺 %d、价格为负(无成交) %d、非正常交易 %d"
          % q)

    # ⑤ 与本地 bars_daily 的 ticker 交集
    local = sorted(p.name.split("=", 1)[1] for p in (ROOT / "data" / "curated" / "bars_daily").glob("symbol=*"))
    sym = pd.read_parquet(out / "symbols.parquet")
    crsp = set(sym["ticker"].dropna().astype(str))
    inter = sorted(set(local) & crsp)
    print(f"本地 bars {len(local)} 只 / CRSP {len(crsp)} 只 / 交集 {len(inter)} 只")
    print(f"CRSP 有、本地没有：{len(crsp - set(local))} 只 → 用 CRSP 自己的价格即可覆盖")
    print(f"产出 → {out}（{time.time() - t0:.0f}s）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
