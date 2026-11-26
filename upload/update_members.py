"""自动更新 upload/members_daily.parquet（逐日成分股与板块归属）。

数据源
------
GitHub `datasets/s-and-p-500-companies` 的 `constituents.csv`：
    Symbol, Security, GICS Sector, GICS Sub-Industry, Headquarters, Date added, CIK, Founded

- Wikipedia 直连超时（被墙），GitHub raw 可访问，字段更规整。
- `GICS Sector` 静态映射到 11 个 SPDR 板块 ETF。
- 符号规范化：GitHub 用 `BF.B`，CRSP 用 `BF-B`，统一转成连字符形式。

permno 分配
-----------
- 老票（能按 symbol 在 latest_data 或 permno_map 里找到）：沿用原 permno
- 新票：从 `1_000_001` 递增分配，写入 `upload/permno_map.csv` 持久化
  （CRSP 的 permno 不会到 7 位，不会冲突）

自动化
------
每次运行都会抓最新名单、写入当日成员、并把新票清单交给 `update.py` 拉行情。
不再依赖人工确认。
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "latest_data"
OUT = ROOT / "upload"
OUTPUT_START = "2025-12-31"
PERMNO_BASE = 1_000_001

URL = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
       "main/data/constituents.csv")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

GICS2ETF = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
}


def norm_symbol(s: str) -> str:
    """BF.B -> BF-B；去空格、转大写。"""
    return str(s).strip().upper().replace(".", "-")


def fetch_sp500(retries: int = 4) -> pd.DataFrame:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(URL, headers=UA, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df["symbol"] = df["Symbol"].map(norm_symbol)
            df["sector_etf"] = df["GICS Sector"].map(GICS2ETF)
            df["date_added"] = pd.to_datetime(df["Date added"], errors="coerce")
            miss = df[df.sector_etf.isna()]
            if len(miss):
                print(f"  [警告] {len(miss)} 只无 GICS 映射: {miss.Symbol.tolist()[:5]}")
            return df[["symbol", "sector_etf", "date_added"]]
        except Exception as e:
            last_err = e
            print(f"  [重试 {i+1}/{retries}] {type(e).__name__}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"无法获取名单: {last_err}")


def build_permno_map(cur_symbols: set[str]) -> pd.DataFrame:
    """维护 symbol -> permno 映射。老票沿用，新票从 PERMNO_BASE 起分配。"""
    mp = OUT / "permno_map.csv"
    base = pd.read_parquet(SRC / "bars_daily.parquet")[["permno", "symbol"]].drop_duplicates("permno")
    base["symbol"] = base.symbol.map(lambda s: norm_symbol(str(s).replace(".OLD", "")))
    known = dict(zip(base.symbol, base.permno))

    if mp.exists():
        old = pd.read_csv(mp, dtype={"symbol": str})
        for s, p in zip(old.symbol, old.permno):
            known.setdefault(norm_symbol(s), int(p))

    rows = []
    next_id = max([PERMNO_BASE - 1, *known.values()]) + 1
    for s in sorted(cur_symbols):
        if s in known:
            rows.append({"symbol": s, "permno": int(known[s]), "source": "crsp"})
        else:
            rows.append({"symbol": s, "permno": int(next_id), "source": "synthetic"})
            print(f"  [新增] {s} -> permno {next_id}")
            next_id += 1
    out = pd.DataFrame(rows).drop_duplicates("symbol", keep="first")
    out.to_csv(mp, index=False)
    return out


def main() -> int:
    OUT.mkdir(exist_ok=True)
    print("抓取 S&P 500 名单 ...")
    try:
        cur = fetch_sp500()
        print(f"  当前 {len(cur)} 只，GICS 映射成功 {int(cur.sector_etf.notna().sum())}")
    except Exception as e:
        print(f"  [降级] 抓取失败（{type(e).__name__}），回退到上次的名单")
        src_mem = pd.read_parquet(SRC / "members_daily.parquet")
        last_day = src_mem[src_mem.date == src_mem.date.max()]
        pm_old = pd.read_csv(OUT / "permno_map.csv", dtype={"symbol": str})
        cur = last_day[["symbol", "sector_etf"]].copy()
        cur["symbol"] = cur.symbol.map(norm_symbol)
        cur = cur.drop_duplicates("symbol")
        print(f"  回退到 {len(cur)} 只")

    pm = build_permno_map(set(cur.symbol))
    cur = cur.merge(pm, on="symbol", how="left")
    print(f"permno 映射: {len(pm)} 条（合成 {int((pm.source=='synthetic').sum())} 条）")

    # 已有成员表
    mem_path = OUT / "members_daily.parquet"
    if mem_path.exists():
        mem = pd.read_parquet(mem_path)
    else:
        mem = pd.read_parquet(SRC / "members_daily.parquet")
        mem = mem[mem.date >= pd.Timestamp(OUTPUT_START)]

    # 目标日期：补齐 bars_daily 里有、但 members 里还没有的所有交易日
    bars = pd.read_parquet(OUT / "bars_daily.parquet")
    all_dates = sorted(pd.Timestamp(d) for d in bars.date.unique())
    # 基板段（latest_data 覆盖的区间）保持原样，之后全部按 PIT 重建，
    # 这样每次运行都会用 Date added 重新校准，不会残留旧的非 PIT 行。
    src_last = pd.read_parquet(SRC / "members_daily.parquet").date.max()
    mem = mem[mem.date <= src_last]
    missing = [d for d in all_dates if d > src_last]
    print(f"重建区间 {len(missing)} 个交易日"
          + (f": {missing[0].date()} ~ {missing[-1].date()}" if missing else "（基板已是最新）"))

    # PIT（point-in-time）：某日 d 的成员 = GitHub 名单里 `Date added <= d` 的票。
    #
    # 为什么必须这样做：直接拿「最新快照」填历史日期会产生**前视偏差**——
    # 远端 us_rsrs_strategy 踩过同样的坑（「6 月才纳入的 MRVL 在 4 月被选出来」）。
    # 这里用 `Date added` 把「当天还没进指数」的票剔除掉。
    #
    # 已知局限：被**剔除**的票没有移除日期（GitHub 只给 Date added），
    # 所以这部分只能沿用，不做剔除。影响面远小于前视，且可由 latest_data 的历史 PIT 兜住。
    has_da = "date_added" in cur.columns and cur.date_added.notna().any()
    if not has_da:
        print("  [警告] 无 Date added，退回用当前名单填（存在前视风险）")

    frames = [mem]
    for d in missing:
        sub = cur
        if has_da:
            sub = cur[cur.date_added.isna() | (cur.date_added <= d)]
        if not len(sub):
            continue
        chunk = sub[["permno", "sector_etf", "symbol"]].copy()
        chunk["permno"] = chunk.permno.astype("int64")
        chunk["date"] = d
        frames.append(chunk)

    out = pd.concat(frames, ignore_index=True)
    out = out[["date", "permno", "sector_etf", "symbol"]]
    out = out.drop_duplicates(["date", "permno"], keep="last").sort_values(["date", "permno"])
    out.to_parquet(mem_path, index=False, compression="zstd")

    print(f"输出 {len(out):,} 行  {out.date.min().date()} ~ {out.date.max().date()}")
    last_n = int((out.date == out.date.max()).sum())
    print(f"末日成分股: {last_n} 只")
    if missing:
        # 抽查最早与最晚两个补的日期，确认 PIT 生效（数量应逐步增加到最新）
        for d in [missing[0], missing[-1]]:
            print(f"  {d.date()}  {int((out.date == d).sum())} 只")
    print(f"已写 {mem_path}")

    synth = pm[pm.source == "synthetic"]
    if len(synth):
        print(f"\n[待拉行情] {len(synth)} 只新票: {synth.symbol.tolist()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
