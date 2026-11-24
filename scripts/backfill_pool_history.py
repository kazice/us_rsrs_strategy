"""补池内证券的「成分期之前」历史行情，让 MA200 在买入日就够 200 个交易日。

为什么必须补：
    CRSP 这份指数成分文件只有「成分期内」的逐日行情——2021-06 才纳入的票，2021-01-01
    往前一根 K 线都没有；MA200 需要 200 个交易日，趋势闸会一路判成「数据缺失」静默跳过，
    池子里那 106 只后纳入的票就等于白买了。源本身没有，只能去外部补。

口径与对齐（实测确认，不要再乘拆股因子）：
    Yahoo 的 close 已经是拆股调整后的价格（AAPL 2020-01-02 close=75.0875 = 300.35/4，
    与 CRSP 的 DlyClose/DlyCumFacPr 逐日相等），所以它就是「仅拆分复权」口径。
    抓完用重叠区间 ratio = CRSP.adj_close ÷ 外部价 求中位数当比例尺：
        med ≈ 1 且 cv ≤ 0.5%  → 口径一致，按 med 缩放后并入
        cv 超限               → 多半是 ticker 被复用/改名没对上，标 review 不并入

产出：
    data/raw/external/{security_id}_{ticker}.parquet   外部原始日线（带对齐统计）
    data/curated/sp500_pool/backfill_report.csv        每只票的缺口、重叠天数、比值、状态

用法：
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_history --dry-run
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_history --limit 5
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_history
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant.data import pool  # noqa: E402
from quant.data.sources.registry import fetch_with_fallback  # noqa: E402

from scripts.ext_align import align, split_basis  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "external"
REPORT = ROOT / "data" / "curated" / "sp500_pool" / "backfill_report.csv"

WARMUP = 200        # MA200 需要的交易日数
OVERLAP_DAYS = 60   # 往后多抓几天，用来和 CRSP 比口径（≥20 个重叠交易日才判得准）
WINDOW_DAYS = 500   # 往前抓多少自然日（≈345 个交易日，够 200 天 + 余量）
# 比例尺只做「合理性、防呆」用：形状一致（cv 达标）就按中位数缩放，量级离谱才拒
# （SNDK 从 WDC 分拆出来，Yahoo 与 CRSP 差一个恒定 8 倍，cv=0，缩放后正好接得上）
MIN_MED, MAX_MED = 0.05, 20.0

# CRSP 只记「成分期内」的 ticker，改名前的旧代码它没有；这几只要手工补上才能抓到历史
EXTRA_ALIAS = {"PSKY": ["PARA"], "WBD": ["DISCA"]}


def targets(universe: pd.DataFrame, bars: pd.DataFrame,
            pool_start: str) -> pd.DataFrame:
    """挑出「买入日之前不足 WARMUP 个交易日」的证券。

    重叠段不能用固定天数：有的票刚纳入（CRSP 只有 7 行），有的票停过牌（PCG），
    所以改用「买入日之后前 25 根 K 线」定位重叠窗口，重叠天数按实际可得的收紧。
    """
    # 只按 CRSP 自己的行算「买入日 / 已预热天数」：外部补的行不能当起点，否则永远算 0
    if "src" in bars.columns:
        bars = bars[bars["src"] == "crsp"]
    data_end = bars["date"].max()
    start_ts = pd.Timestamp(pool_start)
    recs = []
    for sid, g in bars.groupby("security_id", sort=False):
        first_bar = g["date"].min()
        trade_start = max(first_bar, start_ts)
        have = int((g["date"] < trade_start).sum())
        need = max(0, WARMUP - have)
        if need == 0:
            continue
        after = g.loc[g["date"] >= trade_start, "date"].head(25)
        end = (after.iloc[-1] + pd.Timedelta(days=5)) if len(after) else trade_start
        recs.append({
            "security_id": sid, "first_bar": first_bar, "trade_start": trade_start,
            "have_days": have, "need_days": need,
            "window_start": trade_start - pd.Timedelta(days=WINDOW_DAYS),
            "window_end": min(end, data_end),
            "min_overlap": min(20, max(3, len(after))),
        })
    u = pd.DataFrame(recs).merge(universe[["security_id", "ticker"]], on="security_id",
                                 how="left")
    return u.sort_values("need_days", ascending=False)


def alias_at(tick: pd.DataFrame, sid: str, ts: pd.Timestamp) -> str | None:
    """取「在 ts 那天有效的 ticker」，没有就用最早的——改名票（PSKY←PARA）只能这样抓。"""
    t = tick[(tick["security_id"] == sid)].dropna(subset=["ticker"]).sort_values("start")
    if t.empty:
        return None
    hit = t[(t["start"] <= ts) & ((t["end"].isna()) | (t["end"] >= ts))]
    return str((hit if len(hit) else t.iloc[:1])["ticker"].iloc[0])


def aliases(tick: pd.DataFrame, sid: str) -> list[str]:
    t = tick[tick["security_id"] == sid].dropna(subset=["ticker"]).sort_values("start")
    names = list(dict.fromkeys(t["ticker"].astype(str)))
    for n in names:                     # 旧代码（改名前的 ticker）也要试
        names += [a for a in EXTRA_ALIAS.get(n, []) if a not in names]
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description="补池内证券的成分期前历史（给 MA200 预热）")
    ap.add_argument("--pool-start", default="2021-01-04")
    ap.add_argument("--symbols", default="", help="只补这些 ticker / security_id")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=40, help="每次向外部源请求的标的数")
    ap.add_argument("--sleep", type=float, default=2.0, help="批次之间等待秒数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    u = pool.universe()
    b = pool.bars()
    tick = pool.tickers()
    todo = targets(u, b, args.pool_start)
    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        keep = (todo["ticker"].astype(str).str.upper().isin(want)
                | todo["security_id"].isin(want))
        todo = todo[keep]
    if args.limit > 0:
        todo = todo.head(args.limit)
    # 改名票要用「当时那个 ticker」抓（PSKY 的前身是 PARA），用最新 ticker 会 404
    todo["fetch_ticker"] = [alias_at(tick, sid, ws) for sid, ws in
                            zip(todo["security_id"], todo["window_start"])]
    todo = todo.dropna(subset=["fetch_ticker"])
    print(f"需要补历史：{len(todo)} 只（买入日之前不足 {WARMUP} 个交易日）")
    if todo.empty:
        return 0
    show = todo[["security_id", "ticker", "first_bar", "have_days", "need_days"]]
    print(show.head(15).to_string(index=False))
    print(f"  合计缺 {int(todo['need_days'].sum()):,} 个交易日；"
          f"窗口 {todo['window_start'].min().date()} ~ {todo['window_end'].max().date()}")
    if args.dry_run:
        return 0

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    chunk = args.chunk
    t0 = time.time()
    for i in range(0, len(todo), chunk):
        group = todo.iloc[i:i + chunk]
        tickers = group["fetch_ticker"].tolist()
        stem = group["window_start"].min().date().isoformat()
        wend = group["window_end"].max().date().isoformat()
        print(f"\n[{i + len(group)}/{len(todo)}] 抓 {len(tickers)} 只 "
              f"（{stem} ~ {wend}）…", flush=True)
        got, failed, src_map = fetch_with_fallback(tickers, stem, wend)

        # 批量抓失败的逐个换别名重试（Yahoo 对批量里的个别票会误报 delisted）
        resolved: dict[str, tuple[str, pd.DataFrame, str]] = {}
        for r in group.itertuples():
            if r.fetch_ticker in got:
                resolved[r.security_id] = (r.fetch_ticker, got[r.fetch_ticker],
                                          src_map.get(r.fetch_ticker, ""))
                continue
            for alt in [r.fetch_ticker] + [a for a in aliases(tick, r.security_id)
                                           if a != r.fetch_ticker]:
                if alt not in got:
                    more, _, smore = fetch_with_fallback([alt], stem, wend)
                    if alt not in more:
                        continue
                    got[alt] = more[alt]
                    src_map[alt] = smore.get(alt, "")
                resolved[r.security_id] = (alt, got[alt], src_map.get(alt, ""))
                break
        if failed:
            print(f"  批量未拿到 {len(failed)} 只，逐个别名重试后仍缺 "
                  f"{len(group) - len(resolved)} 只")

        for r in group.itertuples():
            sid, tk = r.security_id, r.fetch_ticker
            hit = resolved.get(sid)
            if hit is None:
                rows.append({"security_id": sid, "ticker": tk, "source": "",
                             "rows": 0, "gained_days": 0, "overlap_days": 0,
                             "ratio_median": np.nan, "ratio_cv": np.nan,
                             "status": "fetch_failed"})
                continue
            tk, df, src = hit
            ext = df.copy()
            ext["date"] = pd.to_datetime(ext["date"])
            ext = ext.drop_duplicates("date").sort_values("date")
            ext = split_basis(ext, src)          # 压到「仅拆分复权」口径
            ext = ext.set_index("date")
            ext = ext.loc[(ext.index >= r.window_start) & (ext.index <= r.window_end)]
            c = b[b["security_id"] == sid][["date", "adj_close"]].set_index("date")
            _, st = align(ext, c, col="adj_close_crsp",
                          min_overlap=int(r.min_overlap))
            med = st.get("ratio_median", np.nan)
            # 重叠点少（刚纳入指数/刚 IPO）时 cs 判定更松，靠更严的 cv 兜住
            max_cv = 0.005 if st.get("overlap_days", 0) >= 20 else 0.002
            # 口径一致 + 比例尺合理才算通过；比值离谱说明 ticker 被复用，宁可不并
            ok = (st.get("status") == "ok" and pd.notna(med)
                  and MIN_MED <= med <= MAX_MED
                  and st.get("ratio_cv", np.inf) <= max_cv)
            status = "ok" if ok else ("review" if st.get("status") == "ok"
                                      else st.get("status", "review"))
            status = status or "review"
            pre = ext[ext.index < r.first_bar]
            st = {"overlap_days": st.get("overlap_days", 0),
                  "ratio_median": med, "ratio_cv": st.get("ratio_cv", np.nan),
                  "status": status, "rows": int(len(ext)),
                  "gained_days": int(len(pre)), "source": src}
            out = ext.copy()
            out["security_id"] = sid
            out["ticker"] = tk
            out["source"] = src
            out["ratio_median"] = med
            out["ratio_cv"] = st.get("ratio_cv", np.nan)
            out["align_status"] = st["status"]
            out.index.name = "date"
            out.to_parquet(RAW_DIR / f"{sid}_{tk}.parquet")
            rows.append({"security_id": sid, "ticker": tk, **st})

        if i + chunk < len(todo):
            time.sleep(args.sleep)

    rep = pd.DataFrame(rows)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(REPORT, dtype={"security_id": str}) if REPORT.exists() else pd.DataFrame()
    rep_all = (pd.concat([old, rep], ignore_index=True)
                 .drop_duplicates(["security_id", "ticker"], keep="last"))
    rep_all.to_csv(REPORT, index=False, encoding="utf-8-sig")

    print(f"\n完成 {len(rep)} 只（{time.time() - t0:.0f}s）")
    print(rep["status"].value_counts().to_string())
    bad = rep[rep["status"] != "ok"]
    if len(bad):
        print("\n未通过（不并入池）：")
        print(bad[["security_id", "ticker", "overlap_days", "ratio_median",
                   "ratio_cv", "status"]].to_string(index=False))
    print(f"合计可补入 {int(rep.loc[rep['status'] == 'ok', 'gained_days'].fillna(0).sum()):,} "
          f"个交易日 → {RAW_DIR}")
    print(f"报告 → {REPORT}")
    print("\n下一步：python -m scripts.build_sp500_pool  # 会把通过的票并进 bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
