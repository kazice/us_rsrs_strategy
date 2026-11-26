"""生成 / 增量更新 upload 下的三个数据文件（与 latest_data 隔离）。

数据流
------
    预热段  latest_data[2024-06 : 末日]     ← 只读，提供指标所需的历史
    新增段  新浪[末日+1 : today]             ← 拉取
    拼接 → 重算指标 → 只输出 date >= OUTPUT_START(2025-12-31) 的行

口径
----
- **价格**：新浪返回拆股调整价（split-adjusted）。要接上 latest_data 的后复权尺度，
  每票算一次 `k = bars.close[末日] / sina.close[末日]`，新增行 `close = sina × k`。
- **市值**：`mcap = 实时股本 × 新浪当日收盘`。已验证与 latest_data 同日误差 ≤0.23%。
- **成交额**：新浪 `a` 字段（真实值）。
- **permno**：老票沿用 latest_data；新票从 1_000_001 起分配，写 permno_map.csv。

用法
----
    python upload/update.py --init          # 首次：从 latest_data 建立基线，并补最新
    python upload/update.py                 # 增量更新到最新
    python upload/update.py --limit 20      # 小样本试跑
    python upload/update.py --verify        # 只做质量校验，不写盘
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "latest_data"
OUT = ROOT / "upload"

OUTPUT_START = "2025-12-31"        # upload 只保留这一天之后
WARMUP_START = "2024-06-01"        # 指标预热起点
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLC", "XLRE"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://finance.sina.com.cn"}

MA_WIN = {"ma20": (20, 10), "ma50": (50, 50), "ma60": (60, 30), "ma200": (200, 200)}
SLOPES = {"slope20_5d": ("ma20", 5), "slope60_5d": ("ma60", 5), "slope20_10d": ("ma20", 10)}
ADX_N, RSRS_N, RSRS_M = 14, 20, 40
TURN_WIN = [(20, 10), (120, 60)]
# 当日换手率折算权重。
#
# 历史口径 0.48 的来源是「收盘前 30 分钟决策，当日成交额只走完约 48%」。
# 但现在 upload 拉的是**收盘后**新浪完整日线，当日成交额已经是全天值，
# 再乘 0.48 会系统性低估当日换手率（远端 us_rsrs_strategy 也在文档里
# 明确写了「实盘不再乘 0.48，乘两遍会重复折价」）。
# 因此这里改为 1.0，与 latest_data（回测）保持一致。
TODAY_W = 1.0


# ---------------------------------------------------------------- 抓取


def fetch_daily(symbol: str, retries: int = 3) -> pd.DataFrame | None:
    """新浪日线：返回 date/open/high/low/close/vol/amt（拆股调整价）。"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_x=/"
           f"US_MinKService.getDailyK?symbol={symbol}&___qn=3")
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="ignore")
            m = re.search(r"\((\[.*\])\)", raw, re.S)
            if not m:
                return None
            df = pd.DataFrame(json.loads(m.group(1)))
            if not len(df):
                return None
            df["date"] = pd.to_datetime(df["d"])
            for c in ["o", "h", "l", "c", "v", "a"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.rename(columns={"o": "open", "h": "high", "l": "low",
                                      "c": "close", "v": "vol", "a": "amt"})[
                ["date", "open", "high", "low", "close", "vol", "amt"]
            ]
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(1.0 * (i + 1))
    return None


def fetch_shares(symbols: list[str], chunk: int = 40) -> dict[str, float]:
    """实时快照，返回 {symbol: 总股本}（字段 [19]）。"""
    out: dict[str, float] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i: i + chunk]
        url = "https://hq.sinajs.cn/list=" + ",".join(f"gb_{s.lower()}" for s in batch)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                raw = r.read().decode("gbk", errors="ignore")
            for line in raw.strip().split("\n"):
                if '="' not in line:
                    continue
                k = line.split("=")[0].replace("var hq_str_gb_", "").strip().upper()
                p = line.split('="', 1)[1].rstrip('";').split(",")
                if len(p) > 19 and p[19]:
                    try:
                        out[k] = float(p[19])
                    except ValueError:
                        pass
        except Exception:
            pass
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- 指标


def compute_indicators(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").reset_index(drop=True)
    c, h, l = g.close, g.high, g.low

    for name, (w, mp) in MA_WIN.items():
        g[name] = c.rolling(w, min_periods=mp).mean()
    for name, (src, lag) in SLOPES.items():
        g[name] = g[src] - g[src].shift(lag)

    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=g.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=g.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / ADX_N, adjust=False, min_periods=ADX_N).mean()
    pdi = 100 * pdm.ewm(alpha=1 / ADX_N, adjust=False, min_periods=ADX_N).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / ADX_N, adjust=False, min_periods=ADX_N).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    g["plus_di14"], g["minus_di14"] = pdi, mdi
    g["adx14"] = dx.ewm(alpha=1 / ADX_N, adjust=False, min_periods=ADX_N).mean()

    hv, lv = h.to_numpy(), l.to_numpy()
    raw = np.full(len(g), np.nan)
    for i in range(RSRS_N - 1, len(g)):
        y, x = hv[i - RSRS_N + 1: i + 1], lv[i - RSRS_N + 1: i + 1]
        if np.isnan(y).any() or np.isnan(x).any():
            continue
        vx = np.var(x)
        if vx <= 0:
            continue
        b = np.cov(x, y, bias=True)[0, 1] / vx
        rr = np.corrcoef(x, y)[0, 1]
        if np.isnan(b) or np.isnan(rr) or abs(b) > 10 or rr ** 2 < 0.5:
            continue
        raw[i] = b * rr ** 2
    g["rsrs20_40_raw"] = raw
    s = pd.Series(raw, index=g.index)
    z = np.full(len(g), np.nan)
    for i in range(RSRS_M, len(g)):
        hist = s.iloc[i - RSRS_M: i].dropna()
        if len(hist) >= RSRS_M * 0.5 and hist.std() > 0:
            z[i] = (s.iloc[i] - hist.mean()) / hist.std()
    g["rsrs20_40_z"] = z

    tv = g.amount / g.mcap.where(g.mcap > 0)
    g["turnover"] = tv

    def _wmean(s, w, mp):
        """当日按 TODAY_W 折算的加权均值（实盘口径：收盘前下单，当日只走了 48%）。"""
        ssum = s.rolling(w, min_periods=1).sum()
        cnt = s.notna().rolling(w, min_periods=1).sum()
        return ((ssum - s + TODAY_W * s) / cnt).where(cnt >= mp)

    g["turn20"] = _wmean(tv, 20, 10)
    g["turn120"] = _wmean(tv, 120, 60)
    g["turn_score"] = g.turn20 ** 2 / g.turn120.where(g.turn120 > 0)

    for col, mp in [("ma20", 10), ("ma50", 50), ("ma60", 30), ("ma200", 200),
                    ("slope20_5d", 5), ("slope60_5d", 5), ("slope20_10d", 10),
                    ("adx14", ADX_N), ("rsrs20_40_raw", RSRS_N), ("rsrs20_40_z", RSRS_M),
                    ("turn20", 10), ("turn120", 60), ("turn_score", 60)]:
        idx = np.arange(len(g))
        st = np.zeros(len(g), dtype="int8")
        st[idx < mp] = 1
        st[g[col].isna().to_numpy() & (idx >= mp)] = 2
        g[f"{col}_state"] = st
    return g


# ---------------------------------------------------------------- 主流程


def load_baseline():
    bars = pd.read_parquet(SRC / "bars_daily.parquet")
    etf = pd.read_parquet(SRC / "etf_daily.parquet")
    mem = pd.read_parquet(SRC / "members_daily.parquet")
    return bars, etf, mem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="首次建立基线")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    bars0, etf0, mem0 = load_baseline()
    last = bars0.date.max()
    print(f"基线: latest_data 至 {last.date()}, {bars0.permno.nunique()} 只")

    # 1) 每票 k（后复权 / 新浪）
    kmap_file = OUT / "adjust_k.csv"
    base_last = (bars0[bars0.date == last]
                 .set_index("permno")[["symbol", "close", "mcap"]])
    # 票池来自 permno_map（由 update_members.py 维护，含新纳入的合成 permno）
    pm = pd.read_csv(OUT / "permno_map.csv", dtype={"symbol": str})
    permnos = pm.permno.astype("int64").tolist()
    syms = pm.symbol.tolist()
    if a.limit:
        permnos, syms = permnos[: a.limit], syms[: a.limit]

    print(f"拉取 {len(syms)} 只的末段数据用于算 k ...")
    sina_last = {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_daily, s): s for s in syms}
        for f in as_completed(futs):
            s = futs[f]
            try:
                df = f.result()
                if df is not None and len(df):
                    row = df[df.date == last]
                    if len(row):
                        sina_last[s] = float(row.close.iloc[0])
            except Exception:
                pass
    print(f"  成功 {len(sina_last)}/{len(syms)}")

    krows = []
    for p, s in zip(permnos, syms):
        if p in base_last.index:
            # 老票：用最后一天的 bars 后复权价 / 新浪价 求复权比例
            if s in sina_last and sina_last[s] > 0:
                k = float(base_last.loc[p, "close"]) / sina_last[s]
                krows.append({"permno": int(p), "symbol": s, "k": k,
                              "bars_close": float(base_last.loc[p, "close"]),
                              "sina_close": sina_last[s]})
        else:
            # 新纳入的票（合成 permno）：无历史后复权基准，k=1，
            # 即直接采用新浪的拆股调整价，并按实时股本 × 该价算市值
            krows.append({"permno": int(p), "symbol": s, "k": 1.0,
                          "bars_close": np.nan, "sina_close": np.nan})
    kdf = pd.DataFrame(krows)
    print(f"k 计算完成: {len(kdf)} 只 | 中位 {kdf.k.median():.4f}")

    if a.verify:
        print("\n=== 抽样 ===")
        print(kdf.head(10).to_string(index=False))
        return 0

    kdf.to_csv(kmap_file, index=False)
    print(f"已写 {kmap_file}")

    # 2) 拉新浪全段（2024-06 起），与 latest_data 拼接
    print("\n拉取全段日线 ...")
    frames = []
    fails = []
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_daily, s): (p, s) for p, s in zip(permnos, syms)}
        for f in as_completed(futs):
            p, s = futs[f]
            done += 1
            try:
                df = f.result()
                if df is None or not len(df):
                    fails.append(s)
                    continue
                k = kdf.loc[kdf.permno == p, "k"]
                if not len(k):
                    continue
                k = float(k.iloc[0])
                df = df[df.date >= pd.Timestamp(WARMUP_START)].copy()
                df["permno"] = int(p)
                df["symbol"] = s
                df["close"] = df.close * k
                df["open"] = df.open * k
                df["high"] = df.high * k
                df["low"] = df.low * k
                df["amount"] = df.amt
                df = df.rename(columns={"vol": "volume"})
                frames.append(df[["permno", "date", "symbol", "open", "high", "low",
                                  "close", "volume", "amount"]])
            except Exception:
                fails.append(s)
            if done % 100 == 0:
                print(f"  {done}/{len(syms)}")
    print(f"  成功 {len(frames)}, 失败 {len(fails)}")

    if not frames:
        print("无数据，退出")
        return 1

    sina_all = pd.concat(frames, ignore_index=True)

    # 3) 实时股本 -> 市值
    print("拉取实时股本 ...")
    shares = fetch_shares(sorted({s for s in syms if s in set(kdf.symbol)}))
    print(f"  成功 {len(shares)}/{len(syms)}")
    sina_all["shares"] = sina_all.symbol.map(shares)
    # close 已被 k 缩放为后复权价，除以 k 还原为新浪原始价，再乘股本得真实市值
    # 去掉 .OLD 后可能出现同名（如 SKYD 有两个 permno），去重后再做映射
    kmap_s = kdf.drop_duplicates("symbol", keep="first").set_index("symbol").k
    sina_all["mcap"] = sina_all.shares * (sina_all.close / sina_all.symbol.map(kmap_s))

    # 4) 拼上 latest_data 的预热段 + 计算指标
    print("合并并计算指标 ...")
    cols = ["permno", "date", "symbol", "open", "high", "low", "close",
            "volume", "amount", "mcap"]
    base = bars0[["permno", "date", "symbol", "open", "high", "low", "close",
                  "amount", "mcap"]].copy()
    base["volume"] = np.nan   # latest_data 无 volume 列，预热段留空（策略不用）
    base = base[cols]
    merged = pd.concat([base, sina_all[cols]], ignore_index=True)
    # keep="first"：重叠区优先用 latest_data 的原值（真实后复权价与真实市值），
    # 新浪数据只补 latest_data 之后的新增段。
    # 若用 keep="last"，等于拿「最新 k」和「最新股本」去反推历史，会造成
    # close 偏 0.2%、mcap 偏到 +15%（p95）。
    merged = merged.drop_duplicates(["permno", "date"], keep="first").sort_values(["permno", "date"])

    out_frames = []
    for p, g in merged.groupby("permno"):
        out_frames.append(compute_indicators(g))
    result = pd.concat(out_frames, ignore_index=True)
    result = result[result.date >= pd.Timestamp(OUTPUT_START)].sort_values(["permno", "date"])

    # 只保留信号日仍在册的成分股（约 503 只），剔除期内已退市的票
    alive = mem0.loc[mem0.date == last, "permno"].unique()
    before = result.permno.nunique()
    result = result[result.permno.isin(alive)].reset_index(drop=True)
    print(f"成分股过滤: {before} -> {result.permno.nunique()} 只")

    print(f"输出 {len(result):,} 行, {result.date.min().date()} ~ {result.date.max().date()}")

    # 5) 写盘
    result.to_parquet(OUT / "bars_daily.parquet", index=False, compression="zstd")
    print(f"已写 {OUT/'bars_daily.parquet'}")

    # 注意：etf_daily 由 update_etf.py 负责，members_daily 由 update_members.py 负责，
    # 这里不写这两张表，避免覆盖它们各自的增量结果。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
