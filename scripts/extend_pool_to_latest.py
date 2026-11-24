"""把池层延伸到本地行情的最新交易日（当前是 2026-11-13，CRSP 数据包只到 2025-12-31）。

为什么要单独一层而不是直接改 bars.parquet：
    CRSP 那段是「买来的标准数据」，必须可核对、可重建；2026 这段是「本地行情 + 公告回放」，
    两者来源不同、精度不同。所以延展段单独写 `*_ext.parquet`，`quant.data.pool` 读取时合并，
    回测仍然只从 pool 取数（一份数据源），但每行的 `src` 能区分 `crsp` / `external` / `local_ext`。

口径（务必和 CRSP 段对齐，否则接缝会跳）：
    * 价格：本地复权价按「最后 30 个交易日 CRSP adj_close ÷ 本地 adj_close」的中位数缩放，
      接进 CRSP 的价格空间；`close` 仍按池层定义反推（adj_close ÷ facpr），不是本地原始价。
    * 股本：优先沿用该证券 2025-12-31 的 CRSP 股本；没有的（2026 新纳入票）用 Nasdaq 快照
      的 market_cap/price 反推。全年用一个股本 → 拆股/增发会让市值和换手率有偏差。
    * 换手率：volume ÷ 股本（与 CRSP 段同口径）。
    * 成分：2025-12-31 在册的票延续；2026 公告剔除的按生效日截断（本地没行情，实际已无行）；
      2026 新纳入的 16 只建 `EXT-<TICKER>` 伪 security_id（它们没有 CRSP PERMNO，不能伪造）。
    * 板块：新票取 SPDR holdings 快照；快照没有的按 SIC/默认，`sector_src` 里标来源。

已知缺口（会打印在报告里）：
    2026 年被剔除的 17 只（MMC/DAY/PAYC/LW/MOH/MTCH/HOLX/CTRA/BK/EPAM/CPB/POOL/CAG/EA/AVB/EQR
    等）在剔除前仍在指数内，但本地行情只有「当前成分」，这些票没有 2026 行情 → 那几个月里
    它们不会出现在候选池，属于轻量幸存者偏差。补齐需要联网抓历史（Yahoo 当前被限流）。

用法：
    $env:PYTHONPATH='src'; python -m scripts.extend_pool_to_latest
    $env:PYTHONPATH='src'; python -m scripts.extend_pool_to_latest --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant.data import pool, store, universe as uni_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POOL_DIR = ROOT / "data" / "curated" / "sp500_pool"
CHANGES = ROOT / "data" / "curated" / "index" / "sp500_changes_since_2019.csv"
HOLDINGS = ROOT / "config" / "ref" / "us_sector_holdings.csv"
REPORT = POOL_DIR / "extend_report.csv"

SYM_ALIAS = {"BF": "BF-B", "BRK": "BRK-B"}
OVERLAP_DAYS = 35          # 接缝校准用的重叠交易日数
MIN_EXT_ROWS = 5           # 少于这么多个新交易日就不算延展成功
MIN_OVERLAP = 5


def norm(t: str) -> str:
    k = str(t).upper().replace(".", "-").strip()
    return SYM_ALIAS.get(k, k)


def load_changes(start: str = "2026-01-01") -> tuple[dict[str, pd.Timestamp],
                                                     dict[str, pd.Timestamp]]:
    """返回 (新纳入 {ticker: 生效日}, 剔除 {ticker: 生效日})。"""
    c = pd.read_csv(CHANGES)
    c["date"] = pd.to_datetime(c["date"])
    c = c[c["date"] >= pd.Timestamp(start)]
    adds, rems = {}, {}
    for r in c.itertuples():
        if isinstance(r.add, str):
            for t in r.add.split(","):
                if t.strip():
                    adds.setdefault(norm(t), r.date)
        if isinstance(r.remove, str):
            for t in r.remove.split(","):
                if t.strip():
                    rems.setdefault(norm(t), r.date)
    return adds, rems


def snapshot_shares() -> pd.Series:
    """ticker → 股本（Nasdaq 快照 market_cap ÷ price）。"""
    try:
        u = pd.read_parquet(uni_mod.latest_file())
    except Exception as exc:                     # 快照缺失不该让延展失败
        print(f"  （市值快照不可用：{exc}）")
        return pd.Series(dtype=float)
    u["_s"] = u["symbol"].astype(str).str.upper().str.replace(".", "-", regex=False)
    u = u[(u["price"] > 0) & (u["market_cap"] > 0)]
    return (u.set_index("_s")["market_cap"] / u.set_index("_s")["price"]).astype(float)


def sector_of(tickers: list[str]) -> dict[str, tuple[str, str]]:
    """ticker → (板块 ETF, 来源)。SPDR holdings 快照优先，缺失按 SIC 不可得则归 XLI。"""
    h = pd.read_csv(HOLDINGS)
    h["_s"] = h["symbol"].astype(str).str.upper().str.replace(".", "-", regex=False)
    known = dict(zip(h["_s"], h["sector_etf"]))
    return {t: (known.get(t, "XLI"), "holdings" if t in known else "default")
            for t in tickers}


def main() -> int:
    ap = argparse.ArgumentParser(description="池层延展到本地行情最新日（2026 段）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    b = pool.bars(src="crsp")                     # 只要 CRSP 原始行用于校准
    cutoff = pd.Timestamp(b["date"].max())
    u = pool.universe()
    tick = pool.display_tickers()

    # 往前多取一段用于「接缝校准」：必须与 CRSP 有 ≥20 个重叠交易日，
    # 否则拆股票（BKNG/KLAC/AMCR…）的复权比例算不出来，接缝会凭空跳 ±90%。
    spy = store.load_panel(field="adj_close", symbols=["SPY"],
                           start=str((cutoff - pd.Timedelta(days=120)).date()),
                           end="2027-01-01")
    if spy.empty:
        raise SystemExit("本地 SPY 行情为空，无法确定延展终点")
    end = pd.Timestamp(spy.index.max())
    if end <= cutoff:
        raise SystemExit(f"本地行情末日 {end.date()} 不晚于 CRSP 截止日 {cutoff.date()}，无需延展")
    print(f"CRSP 截止 {cutoff.date()} → 本地最新 {end.date()}"
          f"（{len(spy.loc[spy.index > cutoff])} 个交易日待延展）")

    adds, rems = load_changes(str(cutoff.date()))
    sh = snapshot_shares()

    # ---- 待延展证券：2025-12-31 仍在册 + 2026 新纳入 ----------------------
    # 只处理真 PERMNO 的延续票；上一轮延展出来的 EXT-* 伪 ID 不能重复延展
    act = u[(u["exit_type"] == "active")
            & (~u["security_id"].astype(str).str.startswith("EXT-"))].copy()
    act["ticker_n"] = [norm(tick.get(s, s)) for s in act["security_id"]]
    cont = {str(r.security_id): r.ticker_n for r in act.itertuples()}

    local_syms = set()
    for t in list(cont.values()) + list(adds):
        if t in set(str(x).upper() for x in store.list_symbols()):
            local_syms.add(t)
    syms = sorted(local_syms)
    print(f"候选证券 {len(cont)}（延续）+ {len(adds)}（新纳入）；"
          f"本地有行情的符号 {len(syms)} 个")

    cal = spy.index
    i_cut = int(cal.searchsorted(cutoff))
    win_start = cal[max(0, i_cut - OVERLAP_DAYS)]
    px = {f: store.load_panel(field=f, symbols=syms, start=str(win_start.date()),
                              end=str(end.date()))
          for f in ("adj_close", "high", "low", "open", "volume")}
    px = {k: v.sort_index() for k, v in px.items()}

    crsp_last = (b.sort_values("date").groupby("security_id")
                 .agg(adj_close=("adj_close", "last"), facpr=("facpr", "last"),
                      close=("close", "last"), shares=("shares", "last"),
                      ticker=("ticker", "last")).to_dict("index"))

    rows, mem, tk, uni, rep = [], [], [], [], []

    def build(sid: str, sym: str, start_member: pd.Timestamp,
              remove_on: pd.Timestamp | None, is_new: bool) -> None:
        if sym not in px["adj_close"].columns:
            rep.append({"security_id": sid, "ticker": sym, "status": "no_local",
                        "rows": 0, "ratio": np.nan, "overlap": 0})
            return
        s = px["adj_close"][sym].dropna()
        ext_idx = s.index[s.index > cutoff]
        if len(ext_idx) < MIN_EXT_ROWS:
            rep.append({"security_id": sid, "ticker": sym, "status": "too_few",
                        "rows": int(len(ext_idx)), "ratio": np.nan, "overlap": 0})
            return

        base = crsp_last.get(sid)
        ratio, overlap, cv = 1.0, 0, np.nan
        if base is not None and np.isfinite(base["adj_close"]):
            c = b[b["security_id"] == sid].set_index("date")["adj_close"].dropna()
            common = c.index.intersection(s.index)
            overlap = int(len(common))
            if overlap >= MIN_OVERLAP:
                r = (c.loc[common] / s.loc[common]).replace([np.inf, -np.inf], np.nan).dropna()
                if len(r):
                    ratio = float(r.median())
                    cv = float(r.std() / ratio) if ratio else np.inf
            # 拆股/合股会让比例尺变成 2/5/25 这种整数倍，这是正常的，不能当异常踢掉；
            # 真正要拒绝的是「比值不稳定」（cv 大）——说明本地价与 CRSP 不是同一只票/同一口径。
            if (not np.isfinite(ratio) or not (0.02 < ratio < 50.0)
                    or overlap < MIN_OVERLAP or (np.isfinite(cv) and cv > 0.01)):
                rep.append({"security_id": sid, "ticker": sym, "status": "ratio_bad",
                            "rows": int(len(ext_idx)), "ratio": round(ratio, 6),
                            "overlap": overlap, "cv": round(cv, 6) if np.isfinite(cv) else np.nan})
                return

        facpr = float(base["facpr"]) if base and np.isfinite(base["facpr"]) else 1.0
        shares = np.nan
        if base is not None and np.isfinite(base["shares"]):
            shares = float(base["shares"])
        if not np.isfinite(shares) and sym in sh.index:
            shares = float(sh[sym])

        idx = ext_idx
        if remove_on is not None:
            idx = idx[idx < remove_on]
        if len(idx) < MIN_EXT_ROWS:
            rep.append({"security_id": sid, "ticker": sym, "status": "removed_early",
                        "rows": int(len(idx)), "ratio": ratio, "overlap": overlap})
            return

        adj = s.loc[idx] * ratio
        # 接缝收益：第一行要用 CRSP 最后一天的复权价作分母，否则 2026-01 首日收益会缺
        prev = (float(base["adj_close"]) if base and np.isfinite(base["adj_close"])
                else float(adj.iloc[0]))
        ret = adj / adj.shift(1).fillna(prev) - 1.0
        vol = px["volume"][sym].reindex(idx)

        block = pd.DataFrame({
            "security_id": sid,
            "date": idx,
            "ticker": sym,
            "close": (adj / facpr).to_numpy(),
            "adj_open": (px["open"][sym].reindex(idx) * ratio).to_numpy(),
            "adj_high": (px["high"][sym].reindex(idx) * ratio).to_numpy(),
            "adj_low": (px["low"][sym].reindex(idx) * ratio).to_numpy(),
            "adj_close": adj.to_numpy(),
            "volume": vol.to_numpy(),
            "shares": shares,
            "mcap": ((adj / facpr) * shares).to_numpy(),
            "turnover": (vol / shares).to_numpy(),
            "facpr": facpr,
            "ret_adj": ret.to_numpy(),
            "ret_total": ret.to_numpy(),
            "is_member": True,
            "src": "local_ext",
        })
        rows.append(block)

        m_start = max(start_member, pd.Timestamp(idx.min()))
        m_end = (min(pd.Timestamp(idx.max()), remove_on - pd.Timedelta(days=1))
                 if remove_on is not None else pd.NaT)
        mem.append({"security_id": sid, "start": m_start, "end": m_end, "ticker": sym,
                    "cusip": None, "siccd": np.nan})
        tk.append({"security_id": sid, "ticker": sym, "start": m_start,
                   "end": (m_end if pd.notna(m_end) else pd.Timestamp(idx.max())),
                   "n_rows": int(len(idx))})
        rep.append({"security_id": sid, "ticker": sym,
                    "status": "ok" if not is_new else "ok_new",
                    "rows": int(len(idx)), "ratio": round(ratio, 6), "overlap": overlap})

    for sid, sym in cont.items():
        build(sid, sym, pd.Timestamp(cutoff) + pd.Timedelta(days=1), rems.get(sym), False)

    new_sec: list[tuple[str, str, pd.Timestamp]] = []
    for sym, d in adds.items():
        sid = f"EXT-{sym}"
        build(sid, sym, d, rems.get(sym), True)
        if any(m["security_id"] == sid for m in mem):
            new_sec.append((sid, sym, d))

    ext = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    ext = ext.sort_values(["security_id", "date"]).reset_index(drop=True)
    mem_df = pd.DataFrame(mem)
    tk_df = pd.DataFrame(tk)

    # ---- universe_ext：只放新纳入的伪 ID（延续票沿用原 universe 行）------
    sec_map = sector_of([s for _, s, _ in new_sec])
    uni_rows = []
    for sid, sym, d in new_sec:
        g = ext[ext["security_id"] == sid]
        etf, ssrc = sec_map[sym]
        uni_rows.append({
            "security_id": sid, "ticker": sym, "rows": int(len(g)),
            "first_date": g["date"].min().date(), "last_date": g["date"].max().date(),
            "member_days": int(len(g)), "non_member_rows": 0, "external_rows": 0,
            "days_before_pool_start": 0, "warmup_days": 0, "warmup_ok": False,
            "n_events": 0, "max_abs_ret": float(g["ret_adj"].abs().max()),
            "jump_gt_50pct": int((g["ret_adj"].abs() > 0.5).sum()),
            "max_gap_days": 0, "max_stale_run": 0,
            "ratio_adj_vs_DlyRet_median": np.nan, "mbr_start": d.date(),
            "mbr_end": g["date"].max().date(), "n_segments": 1, "exit_type": "active",
            "delisting_ret": np.nan, "trading_status": "A",
            "last_trade_date": g["date"].max().date(), "days_after_mbr_end": 0,
            "in_index": True, "cusip": None, "siccd": np.nan, "n_tickers": 1,
            "tickers": sym, "sector_etf": etf, "sector_src": ssrc,
        })
    uni_df = pd.DataFrame(uni_rows)

    rp = pd.DataFrame(rep).sort_values(["status", "security_id"])
    print(f"\n延展成功 {int((rp['status'].str.startswith('ok')).sum())} 只 / "
          f"{len(ext):,} 行（src=local_ext）")
    print("  状态分布：", rp["status"].value_counts().to_dict())
    bad = rp[~rp["status"].str.startswith("ok")]
    if len(bad):
        print("  未延展：")
        print(bad[["security_id", "ticker", "status", "overlap"]].to_string(index=False))
    if len(ext):
        jump = ext[ext["ret_adj"].abs() > 0.5]
        print(f"  单日 |收益| >50% 的行：{len(jump)}（接缝异常会在这里暴露）")
        print(f"  价格区间 {ext['date'].min().date()} ~ {ext['date'].max().date()}；"
              f"市值缺失 {int(ext['mcap'].isna().sum())} 行；"
              f"换手率缺失 {int(ext['turnover'].isna().sum())} 行")
        print(f"  新纳入伪 ID：{len(new_sec)} 只（{' '.join(s for _, s, _ in new_sec)}）")

    if args.dry_run:
        print("\n--dry-run：不写文件")
        return 0

    ext.to_parquet(POOL_DIR / "bars_ext.parquet", index=False)
    mem_df.to_parquet(POOL_DIR / "membership_ext.parquet", index=False)
    tk_df.to_parquet(POOL_DIR / "tickers_ext.parquet", index=False)
    uni_df.to_parquet(POOL_DIR / "universe_ext.parquet", index=False)
    rp.to_csv(REPORT, index=False, encoding="utf-8-sig")
    print(f"\n写出 bars_ext / membership_ext / tickers_ext / universe_ext → {POOL_DIR}")
    print(f"报告 → {REPORT}")
    print("\n下一步：python -m scripts.sector_stock_rsrs ... --end 2026-11-13")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
