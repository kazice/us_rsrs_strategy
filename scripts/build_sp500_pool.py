"""构建「2021-01-01 起」的标普 500 point-in-time 成分股池，security_id(PERMNO) 为唯一主键。

为什么单独做一层：
    买来的原始 CSV 是 ticker 视角、只留成分期内行，拆股/合股/分立的调整藏在 DlyCumFacPr 里
    没人看得见，「被调出指数」和「真退市」混在一起。这一层把主键收到 PERMNO，复权口径显式化，
    公司行为与退出方式拆成独立表，回测直接按 security_id 取数，不再拿 ticker 当键。
    产出就是仓库里**唯一**的个股数据源，旧的 ticker 口径派生视图已删除。

清洗口径（实测确认，不是猜的）：
    ① 复权：adj = DlyClose / DlyCumFacPr（CRSP 的因子是**除数**，不是乘数）。
       全池 82 次因子跳变里拆股 79、合股 3，复权后逐日收益与 CRSP 的 DlyRet 相关 0.9977；
       残留单日 |收益|>50% 只有 5 次且都是真实暴跌（SIVB/FRC/OXY/APA/GL），
       说明拆股、合股、分立都已被因子吸收，不需要再乘任何拆股比例。
    ② 拆股/合股/分立：只记录、不改价。用「因子跳变幅度 vs 当日价格变化」区分：
       两者接近 → 拆股/合股；明显不等 → 分立/分派（价格因子按分派比例调整）。
    ③ 退出：只有「成分期已结束（MbrEndDt 非空）」= 被调出指数，之后源里本就没有价格，
       不能当退市；MbrEndDt 为空但行情在中途终止，才是真退市/被并购/破产，
       末行 DlyRet 即退市收益（如 FRC 2023-05-01 的 -90.5%），单独存进 exits.parquet。
    ④ 缺价、缺因子、停牌：剔除并逐条记入 qc_issues.csv，不静默吞掉。
    ⑤ 成分期之前的历史：CRSP 这份文件只有「成分期内」行情，后纳入的票（106 只）没有预热段，
       由 scripts/backfill_pool_history.py 从外部补齐并按重叠区间缩放到 CRSP 价格空间，
       这里把它并成 src='external' 的行（is_member 恒 False，只喂 MA200 等指标）。
    ⑤b 换手率：外部段没有股本（ShrOut 只有 CRSP 行才有），turn120 会一直算不出来。
       由 scripts/backfill_pool_turnover.py 从东财美股日线补「每日换手率」写进 external 段，
       口径已用与 CRSP 的重叠区间校验（ratio≈1、corr≈0.999）；shares / mcap 仍是 NaN。

产出（data/curated/sp500_pool/）：
    universe.parquet     每只证券一行（security_id 唯一）：ticker/板块/成分区间/状态/体检结论
    membership.parquet   security_id × 成分区间（PIT，可多段）
    tickers.parquet      security_id × ticker × 区间（补外部历史时按区间换别名）
    bars.parquet         security_id × date：复权 OHLC + 原始 close + 量/股本/市值/换手 + src
    events.parquet       拆股/合股/分立事件（因子跳变 + 当日价格变化 + 复权连续性）
    exits.parquet        退出方式与退市收益
    qc_issues.csv        逐票体检明细（含 warmup_days / external_rows）
    backfill_merged.csv  并入的外部历史清单与对齐比值

用法：
    $env:PYTHONPATH='src'; python -m scripts.build_sp500_pool
    $env:PYTHONPATH='src'; python -m scripts.build_sp500_pool --start 2019-01-01 --pool-start 2021-01-01
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "crsp_sp500"
EXT_DIR = ROOT / "data" / "raw" / "external"     # backfill_pool_history 抓的外部历史
OUT_DIR = ROOT / "data" / "curated" / "sp500_pool"
SP500_INDNO = 1000500

# CRSP 的 Ticker 不带股份类别后缀：`BF` 实际是 BF.B、`BRK` 实际是 BRK.B。
# SSGA 快照与本地行情用 BF-B / BRK-B，不补齐这两只永远对不上板块与行情。
SYM_ALIAS = {"BF": "BF-B", "BRK": "BRK-B"}

# 手工 GICS 归属：SPDR 快照里没有、但 SIC 会分错的历史成分股。
# 这些名字的 GICS 板块是稳定的公开信息，逐只写死比按 SIC 猜更可靠；
# 抽样核对时发现 SIC 把 CTVA 分到 XLP（应 XLB）、WBD/PARA 分到 XLI（应 XLC）。
SECTOR_OVERRIDE: dict[str, str] = {
    "CTVA": "XLB",
    "OGN": "XLV", "CTLT": "XLV", "KVUE": "XLV", "WBA": "XLV",
    "VNO": "XLRE", "DRE": "XLRE", "EQR": "XLRE", "AVB": "XLRE",
    "PARA": "XLC", "PSKY": "XLC", "DISCA": "XLC", "DISCK": "XLC", "DISH": "XLC",
    "WBD": "XLC", "LUMN": "XLC", "MTCH": "XLC", "NLSN": "XLC", "IPG": "XLC",
    "TTD": "XLC", "TWTR": "XLC",
    "CTXS": "XLK", "ATVI": "XLK", "EA": "XLK", "ANSS": "XLK", "PAYC": "XLK",
    "EPAM": "XLK", "CERN": "XLK", "DAY": "XLK", "DXC": "XLK", "JNPR": "XLK",
    "WHR": "XLY", "POOL": "XLY", "LKQ": "XLY",
}

# SIC 大类 → SPDR 板块 ETF。只用于 CRSP 独有的历史成分股（本地 holdings 快照里没有的票），
# 命中不了的一律归 XLI（工业），并在 sector_src 里留来源标记，便于排查。
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


def norm_sym(tickers) -> pd.Series:
    """CRSP ticker → 对外代码（补股份类别后缀，. 统一成 -）。"""
    t = pd.Series(tickers).astype(str).str.upper().str.replace(".", "-", regex=False).str.strip()
    return t.replace(SYM_ALIAS)


def sector_map(latest_tick: pd.Series, sic_last: pd.Series) -> pd.DataFrame:
    """security_id → 板块 ETF。SPDR holdings 快照优先，其次手工 GICS，最后按 SIC 大类。

    latest_tick / sic_last 都以 security_id 为索引（ticker 是 CRSP 原始写法）。
    """
    h = pd.read_csv(ROOT / "config" / "ref" / "us_sector_holdings.csv")
    h["symbol"] = norm_sym(h["symbol"])
    known = dict(zip(h["symbol"], h["sector_etf"]))

    rows = []
    for sid, raw in latest_tick.items():
        sym = SYM_ALIAS.get(str(raw).upper().replace(".", "-").strip(),
                            str(raw).upper().replace(".", "-").strip())
        if sym in known:
            rows.append((sid, known[sym], "holdings"))
            continue
        if sym in SECTOR_OVERRIDE:
            rows.append((sid, SECTOR_OVERRIDE[sym], "gics"))
            continue
        code = sic_last.get(sid)
        etf = None
        if code is not None and not pd.isna(code):
            for lo, hi, e in SIC_TO_ETF:
                if lo <= int(code) <= hi:
                    etf = e
                    break
        rows.append((sid, etf or "XLI", "sic" if etf else "default"))
    return pd.DataFrame(rows, columns=["security_id", "sector_etf", "sector_src"])

FACTOR_JUMP = 0.02      # 复权因子单日变化超过 2% 视为公司行为
SPLIT_TOL = 0.15        # 价格变化与因子变化相差 <15% 视为纯拆股/合股，否则算分立/分派
CRASH = 0.50            # 复权后单日 |收益| 超过这个值单独列出来核对
STALE_RUN = 20          # 连续同样收盘价超过这么多天 = 长期无成交
WARMUP_DAYS = 200       # 2021-01-01 之前至少要有的交易日数


def find_csv(src: Path) -> Path:
    if src.is_file():
        return src
    hits = sorted(src.glob("*.csv"))
    if not hits:
        raise SystemExit(f"没找到 CRSP CSV：{src}")
    return hits[-1]


def extract(csv: Path, pool_start: str, start: str, end: str,
            threads: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从原始 CSV 里切出 ① 成分区间 ② 池内证券的逐日行情。"""
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={threads}")
    read = (f"read_csv('{csv.as_posix()}', header=true, ignore_errors=true, "
            f"sample_size=200000)")

    # 同一区间会有多行（部分行的 Ticker/CUSIP 为空），按区间去重后取非空值
    con.execute(f"""
        CREATE OR REPLACE TABLE mem AS
        SELECT PERMNO,
               max(Ticker) AS ticker,
               max(CUSIP)  AS cusip,
               max(SICCD)  AS siccd,
               MbrStartDt, MbrEndDt
        FROM {read}
        WHERE INDNO = {SP500_INDNO}
          AND (MbrEndDt IS NULL OR MbrEndDt >= DATE '{pool_start}')
          AND MbrStartDt <= DATE '{end}'
          AND PERMNO IS NOT NULL
        GROUP BY PERMNO, MbrStartDt, MbrEndDt
    """)
    mem = con.execute("SELECT * FROM mem ORDER BY PERMNO, MbrStartDt").df()

    con.execute(f"""
        CREATE OR REPLACE TABLE bars AS
        SELECT PERMNO, Ticker, CUSIP, SICCD, DlyCalDt,
               DlyClose, DlyOpen, DlyHigh, DlyLow, DlyPrc, DlyPrcFlg,
               DlyVol, DlyCap, ShrOut, DlyRet, DlyCumFacPr, DlyCumFacShr,
               TradingStatusFlg, DlyRetMissFlg
        FROM {read}
        WHERE INDNO = {SP500_INDNO}
          AND DlyCalDt >= DATE '{start}'
          AND DlyCalDt <= DATE '{end}'
          AND PERMNO IN (SELECT DISTINCT PERMNO FROM mem)
        ORDER BY PERMNO, DlyCalDt
    """)
    bars = con.execute("SELECT * FROM bars").df()
    con.close()
    return mem, bars


def ext_turnover(sid: str, dates: pd.DatetimeIndex) -> pd.Series | None:
    """外部段换手率（iFinD/东财美股日线补的，见 scripts/backfill_pool_turnover.py）。

    为什么单独补：换手率 = 成交量 ÷ 股本，而 ShrOut 只有 CRSP 的成分期内行才有。
    没有这段换手率，后纳入的票在纳入后 60~120 根里 turn120 全是 NaN，等于进不了闸。
    """
    files = sorted(EXT_DIR.glob(f"{sid}_*_turnover.parquet"))
    if not files:
        return None
    t = pd.read_parquet(files[-1])
    if "turnover" not in t.columns:
        return None
    if "date" in t.columns:
        t["date"] = pd.to_datetime(t["date"])
        t = t.set_index("date")
    else:
        t.index = pd.to_datetime(t.index)
    s = t["turnover"].reindex(dates)
    return s if s.notna().any() else None


def load_external(d: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把补来的「成分期之前」历史并进价表（src='external'）。

    外部价只用于指标预热：先按重叠区间把外部价缩进 CRSP 的价格空间（仅拆分口径，比值≈1），
    再只取第一根 CRSP K 线之前的行。原始 close 用 facpr(首日) 反推，
    保持与 CRSP 行同一语义（raw close + 复权因子），不要直接拿复权价当原始价用。
    """
    if not EXT_DIR.exists():
        return pd.DataFrame(), pd.DataFrame()
    rows, rep = [], []
    for sid, g in d.groupby("security_id", sort=False):
        files = sorted(EXT_DIR.glob(f"{sid}_*.parquet"))
        if not files:
            continue
        first = g["date"].min()
        f0 = float(g["facpr"].iloc[0])
        c = g[["date", "adj_close"]].set_index("date")
        for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            e = pd.read_parquet(p)
            if e.empty or "adj_close_crsp" not in e.columns:
                continue
            if "align_status" in e.columns and (e["align_status"] != "ok").all():
                continue
            e.index = pd.to_datetime(e.index)
            ext = e.dropna(subset=["adj_close_crsp"])
            common = ext.index.intersection(c.index)
            need = 20 if len(common) >= 20 else 3
            if len(common) < need:
                continue
            ratio = (c.loc[common, "adj_close"] / ext.loc[common, "adj_close_crsp"])
            med = float(ratio.median())
            if not np.isfinite(med) or med <= 0:
                continue
            cv = float(ratio.std() / med)
            if cv > (0.005 if need == 20 else 0.002):
                continue
            pre = ext[ext.index < first]
            if pre.empty:
                continue
            adj = pre["adj_close_crsp"] * med
            # 接缝检查：分拆出来的票（CARR/AMTM）在拆分前是「暂定发行价」，与 CRSP 的含权价
            # 差 20%+，这种段接上去会把 MA200 带歪，宁可不并
            crsp_first_adj = float(g["adj_close"].iloc[0])
            jump = crsp_first_adj / float(adj.iloc[-1]) - 1
            if abs(jump) > 0.20:
                rep.append({"security_id": sid, "ticker": p.stem.split("_", 1)[-1],
                            "rows": 0, "first": pre.index.min().date(),
                            "last": pre.index.max().date(),
                            "ratio_median": round(med, 6), "ratio_cv": round(cv, 6),
                            "overlap_days": int(len(common)),
                            "status": f"junction_break({jump:+.1%})"})
                break
            # 换手率：CRSP 只给成分期内，这里用外部补的日频换手率填上（shares 仍为 NaN）
            turn = ext_turnover(sid, pre.index)
            block = pd.DataFrame({
                "security_id": sid,
                "date": pre.index,
                "Ticker": pre["ticker"].to_numpy() if "ticker" in pre.columns else None,
                # 列名沿用 CRSP 的写法，后面统一改名；raw close 用 facpr(首日) 反推
                "DlyClose": adj.to_numpy() * f0,
                "DlyVol": pd.to_numeric(pre.get("volume"), errors="coerce").to_numpy(),
                "adj_open": pd.to_numeric(pre.get("open"), errors="coerce").to_numpy() * med,
                "adj_high": pd.to_numeric(pre.get("high"), errors="coerce").to_numpy() * med,
                "adj_low": pd.to_numeric(pre.get("low"), errors="coerce").to_numpy() * med,
                "adj_close": adj.to_numpy(),
                "shares": np.nan, "mcap": np.nan,
                "turnover": turn.to_numpy() if turn is not None else np.nan,
                "facpr": f0,
                "ret_adj": adj.pct_change(fill_method=None).to_numpy(),
                "ret_total": (pd.to_numeric(pre.get("adj_close"), errors="coerce")
                              .pct_change(fill_method=None).to_numpy()),
                "is_member": False,
                "src": "external",
            })
            rows.append(block)
            rep.append({"security_id": sid, "ticker": p.stem.split("_", 1)[-1],
                        "rows": int(len(block)), "first": block["date"].min().date(),
                        "last": block["date"].max().date(), "ratio_median": round(med, 6),
                        "ratio_cv": round(cv, 6), "overlap_days": int(len(common)),
                        "status": "merged"})
            break
    if not rows:
        return pd.DataFrame(), pd.DataFrame(rep)
    return pd.concat(rows, ignore_index=True), pd.DataFrame(rep)


def build(con_mem: pd.DataFrame, raw: pd.DataFrame, pool_start: str,
          end: str) -> dict[str, pd.DataFrame]:
    t0 = time.time()
    mem = con_mem.copy()
    mem["security_id"] = mem["PERMNO"].astype(str)
    mem["start"] = pd.to_datetime(mem["MbrStartDt"])
    mem["end"] = pd.to_datetime(mem["MbrEndDt"])
    mem = mem[["security_id", "start", "end", "ticker", "cusip", "siccd"]]

    d = raw.copy()
    d["security_id"] = d["PERMNO"].astype(str)
    d["date"] = pd.to_datetime(d["DlyCalDt"])
    for c in ["DlyClose", "DlyOpen", "DlyHigh", "DlyLow", "DlyVol", "DlyCap",
              "ShrOut", "DlyRet", "DlyCumFacPr", "DlyCumFacShr", "SICCD"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values(["security_id", "date"]).reset_index(drop=True)
    # 退出判定必须用「没剔过任何行」的原表：真退市那一行经常没有收盘价（FRC 2023-05-01），
    # 但它的 DlyRet = -90.5% 正是退市收益，先剔掉就再也补不回来了
    d_all = d[["security_id", "date", "Ticker", "DlyClose", "DlyRet", "DlyCumFacPr",
               "TradingStatusFlg"]].copy()

    # ---- ① 逐票体检：缺价 / 缺因子 / 停牌 -----------------------------------
    # 无成交日：DlyPrcFlg='BA'（买卖报价中价）、DlyVol=0、DlyClose 为空。
    # 直接剔掉等于让这只票凭空少一天（BIIB 2023-06-09 就这样缺的，全池 2019 年来只有 2 例）。
    # CRSP 对这两行都给了有效 DlyRet（BIIB +2.6%、SIVB 崩盘 -62.9%），说明价格是有效的，
    # 所以按 CRSP 的无成交日口径填：DlyPrc 当收盘价、OHLC 平推、量记 0（真实就是 0）。
    # 只填 BA：DA（价格 0）/ DP（破产后场外价）/ MP（连报价都没有）仍然剔除，补进去只会污染指标。
    no_trade = (d["DlyPrcFlg"].eq("BA") & d["DlyPrc"].notna() & (d["DlyPrc"] > 0))
    if no_trade.any():
        for c in ("DlyOpen", "DlyHigh", "DlyLow", "DlyClose"):
            d.loc[no_trade, c] = d.loc[no_trade, "DlyPrc"]
        d.loc[no_trade, "DlyVol"] = 0.0

    bad_close = d["DlyClose"].isna() | (d["DlyClose"] <= 0)
    d["facpr"] = d["DlyCumFacPr"].where(d["DlyCumFacPr"] > 0)
    bad_fac = d["facpr"].isna()
    # 因子缺失时用该证券最近的有效因子补（不跨证券，避免张冠李戴）
    d["facpr"] = d.groupby("security_id")["facpr"].ffill()
    d = d[~bad_close].copy()                       # 没有收盘价的行对回测无意义
    issues = {
        "dropped_no_close": int(bad_close.sum()),
        "filled_factor": int(bad_fac.sum()),
        "filled_no_trade": int(no_trade.sum()),
    }
    d = d.dropna(subset=["facpr"]).copy()

    # ---- ② 复权：CRSP 的因子是除数 -----------------------------------------
    d["adj_close"] = d["DlyClose"] / d["facpr"]
    d["adj_open"] = d["DlyOpen"] / d["facpr"]
    d["adj_high"] = d["DlyHigh"] / d["facpr"]
    d["adj_low"] = d["DlyLow"] / d["facpr"]
    d["shares"] = d["ShrOut"] * 1e3                # 千股 → 股
    d["mcap"] = d["DlyCap"] * 1e3                  # 千美元 → 美元
    d["turnover"] = d["DlyVol"] / d["shares"].where(d["shares"] > 0)
    d["ret_adj"] = d.groupby("security_id")["adj_close"].pct_change(fill_method=None)
    d["ret_total"] = d["DlyRet"]                   # CRSP 日总收益（含分红，含退市收益）
    d["src"] = "crsp"

    # ---- ③ 公司行为：因子跳变 ----------------------------------------------
    prev_fac = d.groupby("security_id")["facpr"].shift(1)
    prev_px = d.groupby("security_id")["DlyClose"].shift(1)
    ev = d.loc[prev_fac.notna() & ((d["facpr"] / prev_fac - 1).abs() > FACTOR_JUMP)].copy()
    ev["fac_ratio"] = (ev["facpr"] / prev_fac[ev.index]).round(6)
    ev["px_ratio"] = (ev["DlyClose"] / prev_px[ev.index]).round(6)
    ev["adj_ret"] = (ev["px_ratio"] / ev["fac_ratio"] - 1).round(6)
    ev["kind"] = np.where(
        ev["adj_ret"].abs() <= SPLIT_TOL,
        np.where(ev["fac_ratio"] < 1, "split", "reverse_split"),
        "distribution")
    ev["implied_ratio"] = (1 / ev["fac_ratio"]).round(6)   # 1 股拆成几股
    events = (ev[["security_id", "date", "Ticker", "DlyClose", "facpr", "fac_ratio",
                  "px_ratio", "adj_ret", "kind", "implied_ratio"]]
              .rename(columns={"Ticker": "ticker", "DlyClose": "close_raw"})
              .sort_values(["security_id", "date"]).reset_index(drop=True))

    # ---- ④ 成分掩码（PIT）与 ticker 区间 ------------------------------------
    d["is_member"] = False
    for sid, g in d.groupby("security_id", sort=False):
        idx = g.index.to_numpy()
        dates = g["date"].to_numpy()
        for span in mem.loc[mem["security_id"] == sid, ["start", "end"]].itertuples(index=False):
            lo = np.searchsorted(dates, np.datetime64(span.start), side="left")
            hi = (len(dates) if pd.isna(span.end)
                  else np.searchsorted(dates, np.datetime64(span.end), side="right"))
            if hi > lo:
                d.loc[idx[lo:hi], "is_member"] = True

    tick = (d.groupby(["security_id", "Ticker"], as_index=False)
              .agg(start=("date", "min"), end=("date", "max"), n_rows=("date", "size"))
              .rename(columns={"Ticker": "ticker"}))
    tick = tick[tick["ticker"].notna()].sort_values(["security_id", "start"])

    # ---- ④b 并入外部补的「成分期之前」历史（MA200 预热用，src=external）------
    ext_rows, ext_rep = load_external(d)
    if len(ext_rows):
        d = (pd.concat([d, ext_rows], ignore_index=True)
               .sort_values(["security_id", "date"], kind="stable")
               .drop_duplicates(["security_id", "date"], keep="first")
               .reset_index(drop=True))
        # 合并后重算收益：接缝那一行（CRSP 首根）原本是 NaN，现在跨源算得出来
        d["ret_adj"] = d.groupby("security_id")["adj_close"].pct_change(fill_method=None)
        merged = int((ext_rep["status"] == "merged").sum()) if len(ext_rep) else 0
        print(f"  并入外部历史：{merged} 只 / {len(ext_rows):,} 行（src=external，"
              f"只做指标预热，is_member=False）")

    # ---- ⑤ 退出方式 ---------------------------------------------------------
    # 注意：这份 CRSP 指数成分文件里 MbrEndDt 永远非空——期末仍在指数内的证券，
    # 其 MbrEndDt 就是文件的截止日（2025-12-31），所以「是否还在指数里」要拿它跟数据末日比，
    # 不能判空。真退市/被并购看 TradingStatusFlg='D'（末行），与成分期是否结束是两件事。
    data_end = d["date"].max()
    span = mem.groupby("security_id").agg(mbr_start=("start", "min"),
                                          mbr_end=("end", "max"),
                                          n_segments=("start", "size"))
    last_all = d_all.groupby("security_id").tail(1).set_index("security_id")
    last_px = d.groupby("security_id").tail(1).set_index("security_id")
    # 退市那几行的 Ticker 在 CRSP 里是空的，用该证券最后一个有效 ticker
    tick_last = (d.dropna(subset=["Ticker"]).sort_values("date")
                   .groupby("security_id")["Ticker"].last())
    exits = pd.DataFrame({
        "security_id": last_all.index,
        "ticker": last_all.index.map(tick_last),
        "last_date": last_all["date"].to_numpy(),
        "last_trade_date": last_px.reindex(last_all.index)["date"].to_numpy(),
        "last_close_raw": last_px.reindex(last_all.index)["DlyClose"].to_numpy(),
        "last_adj_close": last_px.reindex(last_all.index)["adj_close"].to_numpy(),
        "last_ret": last_all["DlyRet"].to_numpy(),
        "trading_status": last_all["TradingStatusFlg"].to_numpy(),
        "mbr_end": last_all.index.map(span["mbr_end"]).to_numpy(),
    })
    exits["last_date"] = pd.to_datetime(exits["last_date"])
    exits["mbr_end"] = pd.to_datetime(exits["mbr_end"])
    exits["delisting_ret"] = np.where(exits["trading_status"].eq("D"),
                                      exits["last_ret"], np.nan)
    exits["exit_type"] = np.select(
        [exits["delisting_ret"].notna(),            # 真摘牌：带退市收益的末行
         exits["mbr_end"] >= data_end,              # 期末仍在成分名单内
         exits["mbr_end"].notna()],
        ["delisted", "active", "index_removal"], default="unknown")
    exits["days_after_mbr_end"] = (exits["last_date"] - exits["mbr_end"]).dt.days

    # ---- ⑥ 逐票 QC ----------------------------------------------------------
    qc = []
    g_dates = d.groupby("security_id")["date"]
    for sid, g in d.groupby("security_id"):
        r = g["ret_adj"]
        member = g["is_member"].to_numpy()
        gaps = g_dates.get_group(sid).diff().dt.days.iloc[1:]
        if len(member) > 1 and len(gaps) == len(member) - 1:
            gaps = gaps[member[1:]]
        close = g["DlyClose"]
        stale = (close.diff() == 0)
        run = stale.groupby((~stale).cumsum()).cumsum().max() if len(close) else 0
        pre = int((g["date"] < pd.Timestamp(pool_start)).sum())
        # MA200 看的是「买入日之前」有多少根 K 线：后纳入的票按纳入日算，不是按 2021-01-01；
        # 买入日必须取「CRSP 第一根 K 线」，别拿补来的外部历史当起点，否则预热天数恒为 0
        crsp_start = g.loc[g["src"] == "crsp", "date"].min()
        trade_start = max(crsp_start, pd.Timestamp(pool_start))
        warm = int((g["date"] < trade_start).sum())
        qc.append({
            "security_id": sid,
            "ticker": g["Ticker"].iloc[-1],
            "rows": int(len(g)),
            "first_date": g["date"].min().date(),
            "last_date": g["date"].max().date(),
            "member_days": int(member.sum()),
            "non_member_rows": int(((~member) & (g["src"] == "crsp").to_numpy()).sum()),
            "external_rows": int((g["src"] == "external").sum()),
            "days_before_pool_start": pre,
            "warmup_days": warm,
            "warmup_ok": warm >= WARMUP_DAYS,
            "n_events": int((events["security_id"] == sid).sum()),
            "max_abs_ret": round(float(r.abs().max()), 6) if r.notna().any() else np.nan,
            "jump_gt_50pct": int((r.abs() > CRASH).sum()),
            "max_gap_days": int(gaps.max()) if len(gaps.dropna()) else 0,
            "max_stale_run": int(run) if pd.notna(run) else 0,
            "ratio_adj_vs_DlyRet_median": round(float((r - g["ret_total"]).median()), 6)
            if r.notna().any() else np.nan,
        })
    qc = pd.DataFrame(qc)

    # ---- ⑦ 主表 -------------------------------------------------------------
    universe = (qc.merge(span.reset_index(), on="security_id", how="left")
                  .merge(exits[["security_id", "exit_type", "delisting_ret",
                                "trading_status", "last_trade_date",
                                "days_after_mbr_end"]], on="security_id", how="left"))
    universe["in_index"] = universe["mbr_end"] >= data_end
    universe["cusip"] = universe["security_id"].map(
        d.dropna(subset=["CUSIP"]).sort_values("date").groupby("security_id")["CUSIP"].last())
    universe["siccd"] = universe["security_id"].map(
        d.dropna(subset=["SICCD"]).sort_values("date").groupby("security_id")["SICCD"].last())
    universe["n_tickers"] = universe["security_id"].map(tick.groupby("security_id").size())
    universe["tickers"] = universe["security_id"].map(
        tick.groupby("security_id")["ticker"].apply(lambda s: "/".join(dict.fromkeys(s))))

    # 板块归属：SPDR holdings 快照优先（ticker 先规范化，否则 BRK/BF 会落到 SIC → 误分 XLI），
    # 其次手工 GICS，最后按 SIC 大类补。全部以 security_id 为键落进 universe。
    latest_tick = (tick.sort_values("end").groupby("security_id").tail(1)
                    .set_index("security_id")["ticker"])
    sic_last = (d.dropna(subset=["SICCD"]).sort_values("date")
                  .groupby("security_id")["SICCD"].last())
    try:
        smap = sector_map(latest_tick.dropna(), sic_last).set_index("security_id")
        universe["sector_etf"] = universe["security_id"].map(smap["sector_etf"])
        universe["sector_src"] = universe["security_id"].map(smap["sector_src"])
    except Exception as exc:                       # 快照缺失不应拖垮整个构建
        print(f"  （板块映射跳过：{exc}）")
    universe = universe.sort_values(["exit_type", "last_date"], ascending=[True, False])

    bars = d[["security_id", "date", "Ticker", "DlyClose", "adj_open", "adj_high",
              "adj_low", "adj_close", "DlyVol", "shares", "mcap", "turnover",
              "facpr", "ret_adj", "ret_total", "is_member", "src"]].rename(
        columns={"Ticker": "ticker", "DlyClose": "close", "DlyVol": "volume"})

    return {"universe": universe, "membership": mem, "tickers": tick,
            "bars": bars, "events": events, "exits": exits, "qc": qc,
            "backfill": ext_rep,
            "_stats": pd.DataFrame([{**issues,
                                     "post_removal_rows": int(((~d["is_member"])
                                                               & (d["src"] == "crsp")).sum()),
                                     "external_rows": int((d["src"] == "external").sum()),
                                     "secs": d["security_id"].nunique(),
                                     "rows": len(d),
                                     "build_sec": round(time.time() - t0, 1)}])}


def report(t: dict[str, pd.DataFrame], pool_start: str) -> None:
    u, b, ev, ex, qc = t["universe"], t["bars"], t["events"], t["exits"], t["qc"]
    print(f"\n证券池：{len(u)} 只（2021-01-01 之后当过标普500成分股），主键 security_id")
    print("  退出方式：" + "、".join(
        f"{k} {v}" for k, v in ex["exit_type"].value_counts().items()))
    dead = ex[ex["exit_type"] == "delisted"].sort_values("last_date")
    if len(dead):
        print("  真退市/被并购（末行 DlyRet 即退市收益，已存进 exits.parquet）：")
        print(dead[["security_id", "ticker", "last_date", "mbr_end", "delisting_ret"]]
              .to_string(index=False))

    m = t["membership"]
    cal = pd.DatetimeIndex(sorted(b["date"].unique()))
    for day in ["2021-01-04", "2022-01-03", "2023-01-03", "2024-01-02", "2025-12-31"]:
        ts = pd.Timestamp(day)
        if ts not in cal:
            continue
        n = ((m["start"] <= ts) & (m["end"].isna() | (m["end"] >= ts))).sum()
        print(f"  {day} 成分只数（按区间）：{n}")

    print(f"\n公司行为 {len(ev)} 次："
          + "、".join(f"{k} {v}" for k, v in ev["kind"].value_counts().items()))
    for kind in ("reverse_split", "distribution"):
        sub = ev[ev["kind"] == kind]
        if len(sub):
            print(f"  {kind}:")
            print(sub[["security_id", "ticker", "date", "fac_ratio", "px_ratio", "adj_ret"]]
                  .to_string(index=False))

    crash = qc[qc["jump_gt_50pct"] > 0]
    print(f"\n复权后仍有 |日收益|>50% 的证券 {len(crash)} 只（应为真实暴跌，不是拆股漏处理）：")
    print(crash[["security_id", "ticker", "jump_gt_50pct", "max_abs_ret"]]
          .to_string(index=False))

    bf = t.get("backfill")
    if bf is not None and len(bf):
        print(f"\n外部补历史已并入：{len(bf)} 只 / {int(bf['rows'].sum()):,} 行 "
              f"（比值中位 {bf['ratio_median'].median():.4f}，cv 最大 {bf['ratio_cv'].max():.4f}）")
    ok = int(qc["warmup_ok"].sum())
    print(f"MA{WARMUP_DAYS} 预热：{ok}/{len(qc)} 只在买入日之前够 {WARMUP_DAYS} 个交易日")
    lack = qc[~qc["warmup_ok"]].sort_values("warmup_days")
    if len(lack):
        print("  仍不足的（外部源也没有这段历史，回测里这些票前 200 日会被跳过）：")
        print(lack[["security_id", "ticker", "first_date", "warmup_days", "external_rows"]]
              .to_string(index=False))
    print(f"\n逐票 QC → qc_issues.csv；最大停牌缺口 {int(qc['max_gap_days'].max())} 天；"
          f"最长横盘 {int(qc['max_stale_run'].max())} 天")
    print("\n构建统计：")
    print(t["_stats"].to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="构建标普500 PIT 成分股池（security_id 主键）")
    ap.add_argument("--src", default=str(RAW_DIR))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--pool-start", default="2021-01-01",
                    help="成分池的起点：这一天之后当过成分股才进池")
    ap.add_argument("--start", default="2019-01-01",
                    help="行情起点（比 pool-start 早，留出指标预热窗口）")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    csv = find_csv(Path(args.src))
    t0 = time.time()
    print(f"读 CRSP：{csv.name}")
    mem, raw = extract(csv, args.pool_start, args.start, args.end, args.threads)
    print(f"  成分区间 {len(mem)} 段 / {mem['PERMNO'].nunique()} 只；"
          f"行情 {len(raw):,} 行（{time.time() - t0:.0f}s）")

    t = build(mem, raw, args.pool_start, args.end)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("universe", "membership", "tickers", "bars", "events", "exits"):
        t[name].to_parquet(out / f"{name}.parquet", index=False)
    t["qc"].to_csv(out / "qc_issues.csv", index=False, encoding="utf-8-sig")
    if len(t.get("backfill", [])):
        t["backfill"].to_csv(out / "backfill_merged.csv", index=False, encoding="utf-8-sig")
    report(t, args.pool_start)
    print(f"\n产出 → {out}（{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
