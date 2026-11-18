"""板块 → 个股 双层 RSRS：先选板块 ETF，再在该板块成分股里选 1 只个股。

第一层：11 个板块 ETF 的 RSRS（口径与 scripts.sector_rsrs 完全一致，N/M/锚点同源）
第二层：在该板块的成分股里，用同一套 RSRS（默认同样 N/M）选分数最高的一只

成分股来源：config/ref/us_sector_holdings.csv（SPDR 官方 holdings 快照，503 只，
本地行情覆盖 492 只）。**这是当前快照，回测过去存在幸存者偏差**。

产出同时记录「同一板块直接买 ETF」的对照净值，用来判断选股这一层是加分还是减分。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import store, universe
from scripts.sector_rsrs import anchors, perf

ROOT = Path(__file__).resolve().parents[1]
BENCH = "SPY"
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"]


def rsrs_panel_fast(high: pd.DataFrame, low: pd.DataFrame, n: int, m: int,
                    spec: str = "strict", r2_th: float = 0.5,
                    slope_lim: float = 10.0) -> pd.DataFrame:
    """与 sector_rsrs.rsrs_panel 等价的向量化实现（已逐值比对）。

    逐列循环的版本在 500 只股票上要几分钟，这里改成 rolling 均值算协方差：
    beta = (E[LH] − E[L]E[H]) / (E[L²] − E[L]²)，r2 = 1 − 残差方差 / E[H²] − E[H]²。
    """
    H, L = high.astype(float), low.astype(float)
    r = lambda df: df.rolling(n, min_periods=n)  # noqa: E731
    e_h, e_l = r(H).mean(), r(L).mean()
    e_lh, e_ll, e_hh = r(H * L).mean(), r(L * L).mean(), r(H * H).mean()
    var_l = e_ll - e_l ** 2
    var_h = e_hh - e_h ** 2
    beta = (e_lh - e_l * e_h) / var_l.where(var_l > 0)
    r2 = 1.0 - (var_h - beta ** 2 * var_l) / var_h.where(var_h > 0)
    raw = beta * r2

    if spec == "strict":
        raw = raw.where((r2 >= r2_th) & (beta.abs() <= slope_lim))
        hist = raw.shift(1)                                    # 不含当前
        rm = hist.rolling(m, min_periods=1)
        mean_m, std_m, cnt = rm.mean(), rm.std(ddof=0), rm.count()
        ok = (cnt >= 5) & raw.notna() & (std_m > 0)
        z = ((raw - mean_m) / std_m).where(ok, 0.0)
        z.iloc[: n - 1 + m] = np.nan
        return z
    s = raw
    return (s - s.rolling(m).mean()) / s.rolling(m).std(ddof=0)


def mcap_pool_mask(stocks: list[str], cal: pd.DatetimeIndex,
                   start: str, end: str, top_n: int = 500,
                   shares_src: str = "current") -> pd.DataFrame:
    """逐期「假设成分股」掩码：每个交易日按当时的市值排名，前 top_n 只在池内。

    为什么这么近似
    --------------
    标普 500 没有公开的历史成分股名单可抓（SSGA 只给当前快照、Wikipedia 在本机不可达），
    但成分股变动的主要驱动就是市值排名：涨上去的进、跌下去的出，且调整只在季度
    rebalance 生效。用「当时的市值排名」做代理，能挡掉一半前视——今天在池里、当时还
    排不进前 500 的票。

    排名宇宙必须够大
    ----------------
    只在 492 只成分股内部排名毫无分辨力（本来就都是大盘股，取 top500 等于全取）。
    所以宇宙用本地全部有行情的票（约 1050 只）。但**PIT 股本只覆盖那 501 只成分股**，
    其余 550 只没有申报股本，只能用 ``shares_src=current`` 的口径补：

    - ``current``：股本 = universe 快照的 市值/价格（当前值，全市场覆盖），
      市值序列 = **复权收盘价 × 当前股本**。复权价已经把拆股历史折算到今天，
      与当前股本配套，量级正确；分红会造成高股息票的历史市值被系统性低估（约 2%~3%/年），
      对 400~500 名边界有影响，对头部无影响。
    - ``sec``：只在有 SEC 申报股本的 501 只里排名（等于没有过滤效果，仅作对照）。

    ⚠ 残留偏差：宇宙本身是「今天的 top-1000」，当年大、现在小的票不在里面，
    这部分幸存者偏差改不掉；被剔除出指数的票仍然一个都没有。
    """
    from quant.data import universe as uni_mod

    px = store.load_panel(field="adj_close", symbols=list(store.list_symbols()),
                          start=start, end=end).sort_index()
    if shares_src == "sec":
        so = pd.read_parquet(
            ROOT / "data" / "curated" / "fundamentals" / "shares_outstanding.parquet")
        so["filed"] = pd.to_datetime(so["filed"])
        so["symbol"] = so["symbol"].astype(str).str.upper()
        so = so[so["val"] > 0].sort_values("filed")
        # 与 build_turnover_ws 同一条占位值判据：SPAC 期会填 1000 股这种数
        med = so.groupby("symbol")["val"].transform("median")
        so = so[~((so["val"] < 0.01 * med) | (so["val"] < 1e6))]
        left = pd.MultiIndex.from_product([px.index, so["symbol"].unique()],
                                          names=["date", "symbol"]).to_frame(index=False)
        merged = pd.merge_asof(left.sort_values("date"), so, left_on="date",
                               right_on="filed", by="symbol", direction="backward")
        shares = merged.pivot(index="date", columns="symbol", values="val")
        shares = shares.reindex(px.index).ffill().bfill()
        mcap = px.reindex(columns=shares.columns) * shares
    else:
        uni = pd.read_parquet(uni_mod.latest_file())
        uni["symbol"] = uni["symbol"].astype(str).str.upper()
        sh = (uni["market_cap"] / uni["price"]).replace([np.inf, -np.inf], np.nan)
        sh = pd.Series(sh.values, index=uni["symbol"]).dropna()
        sh = sh[sh > 0]
        cols = [c for c in px.columns if c in sh.index]
        mcap = px[cols].mul(sh[cols], axis=1)

    rank = mcap.rank(axis=1, ascending=False, method="min")
    mask = (rank <= top_n).reindex(columns=stocks, fill_value=False).fillna(False)
    keep = int(mask.sum(axis=1).mean())
    print(f"市值池：宇宙 {mcap.shape[1]} 只（股本口径 {shares_src}），top {top_n}，"
          f"成分股日均在池 {keep} 只 / {len(stocks)}")
    if keep < 0.9 * len(stocks):
        # 真实的标普 500 成分股绝大多数都应该在前 500 名市值里。保留率掉到 90% 以下，
        # 说明这套排名口径在误伤真成员（宇宙是按今天的市值筛的，会系统性地压低"今天小、
        # 当年在"的票），此时的结果不能当结论用。
        print(f"⚠ 只保留 {keep}/{len(stocks)}（{keep / len(stocks):.0%}）成分股，"
              f"低于 90%：这是排名口径的误伤，不是真实的调入调出，结果不可作结论")
    for d in [cal[0], cal[len(cal) // 2], cal[-1]]:
        if d in mask.index:
            print(f"  {d.date()} 在池 {int(mask.loc[d].sum())} 只")
    return mask


GICS_TO_ETF = {
    "Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU", "Communication Services": "XLC",
}


def load_sp500_change_events() -> pd.DataFrame:
    """S&P 官方公告里的标普 500 调入调出事件（scripts.fetch_sp_index_changes 的产物）。

    只保留带代码的行：2020-09 之后的公告才有 ticker，更早的行只有公司名，无法回放。
    """
    ch = pd.read_csv(ROOT / "data" / "curated" / "index" / "sp_constituent_changes.csv",
                     encoding="utf-8-sig")
    sp = ch[ch["index_name"] == "S&P 500"].copy()
    sp["effective_date"] = pd.to_datetime(sp["effective_date"], errors="coerce")
    sp = sp[sp["effective_date"].notna() & sp["ticker"].notna()].copy()
    sym = sp["ticker"].astype(str).str.strip().str.upper()
    sym = sym.str.split("/").str[0].str.rstrip("*").str.replace(".", "-", regex=False)
    sp["symbol"] = sym
    sp = sp[sp["symbol"].str.match(r"^[A-Z][A-Z0-9\-]{0,5}$")]
    return sp.drop_duplicates(["effective_date", "action", "symbol"]).sort_values("effective_date")


def pit_pool_mask(stocks: list[str], cal: pd.DatetimeIndex,
                  ev: pd.DataFrame, anchor: set[str]) -> pd.DataFrame:
    """逐日 point-in-time 成分股掩码：从今天的 anchor 名单出发，倒着撤销官方公告里的变动。

    倒放规则：当年「加入」的票，倒推到该生效日之前就该从名单里摘掉；当年「删除」的票要放回来。
    """
    idx = {s: i for i, s in enumerate(stocks)}
    vec = np.zeros(len(stocks), dtype=bool)
    for s in anchor:
        if s in idx:
            vec[idx[s]] = True
    rows = [(r.symbol, r.action, r.effective_date)
            for r in ev.itertuples() if r.symbol in idx]
    rows.reverse()
    mat = np.zeros((len(cal), len(stocks)), dtype=bool)
    j = 0
    for i in range(len(cal) - 1, -1, -1):
        d = cal[i]
        while j < len(rows) and rows[j][2] > d:
            sym, act, _ = rows[j]
            vec[idx[sym]] = (act == "Delete")
            j += 1
        mat[i] = vec
    mask = pd.DataFrame(mat, index=cal, columns=stocks)
    for d in [cal[0], cal[len(cal) // 2], cal[-1]]:
        print(f"  {d.date()} 在池 {int(mask.loc[d].sum())} 只")
    return mask


def load_members() -> pd.DataFrame:
    h = pd.read_csv(ROOT / "config" / "ref" / "us_sector_holdings.csv")
    h["symbol"] = h["symbol"].astype(str).str.strip().str.upper()
    pool = set(store.list_symbols())
    h = h[h["symbol"].isin(pool)].drop_duplicates("symbol").reset_index(drop=True)
    # 市值取自 universe 快照（asof 2026-10-27），只有当前值，无历史序列
    uni = pd.read_parquet(universe.latest_file())
    mcap = dict(zip(uni["symbol"].astype(str).str.upper(), uni["market_cap"]))
    h["mcap"] = h["symbol"].map(mcap)
    return h


def main() -> int:
    ap = argparse.ArgumentParser(description="板块→个股 双层 RSRS")
    ap.add_argument("--n", type=int, default=20, help="板块层回归窗口")
    ap.add_argument("--m", type=int, default=40, help="板块层标准化窗口")
    ap.add_argument("--sn", type=int, default=0, help="个股层回归窗口，0 = 同 --n")
    ap.add_argument("--sm", type=int, default=0, help="个股层标准化窗口，0 = 同 --m")
    ap.add_argument("--cost", type=float, default=0.0005)
    ap.add_argument("--spec", default="strict", choices=["strict", "simple"])
    ap.add_argument("--start", default="2021-11-13")
    ap.add_argument("--end", default="2026-10-26")
    ap.add_argument("--freq", default="M", choices=["M", "W", "H", "M2"])
    ap.add_argument("--pick", default="rsrs",
                    choices=["rsrs", "mcap", "mmin", "turn", "dvol", "dvolcv", "dvolcvh",
                             "cvdown", "turnma"],
                    help="板块内选股规则：rsrs 分数最高 / mcap 市值最大 / mmin 市值最小 / "
                         "turn 近 20 日平均换手率排名 / "
                         "dvol 近 20 日平均成交额最高 / dvolcv 近 20 日成交额变异系数最低 / "
                         "dvolcvh 近 20 日成交额变异系数最高 / "
                         "cvdown 按板块 RSRS 顺序逐个找「变异系数前 N 且满足均线排列」的个股 / "
                         "turnma 同上但按换手率降序找（换手率最大 + 均线排列）")
    ap.add_argument("--cvdown-top", type=int, default=3, help="--pick cvdown 时每个板块看 CV 前几名")
    ap.add_argument("--cvdown-sectors", type=int, default=3, help="--pick cvdown 时最多下探几个板块")
    ap.add_argument("--turn-shares", default="ws",
                    choices=["ws", "snapshot"],
                    help="换手率口径：ws = 直接用 westock 日 K 换手率（美股为成交量/总股本，默认，"
                         "定稿口径）；snapshot = 旧口径（当前快照市值/价格推算股本，含前视，仅对照）")
    ap.add_argument("--turn-noise", type=float, default=0.0,
                    help="给换手率注入对数正态噪声的标准差（0 = 不扰动），用于稳健性检验")
    ap.add_argument("--seed", type=int, default=0, help="--turn-noise 的随机种子")
    ap.add_argument("--turnma-top", type=int, default=3, help="--pick turnma 时每个板块看换手率前几名")
    ap.add_argument("--turnma-sectors", type=int, default=3, help="--pick turnma 时最多下探几个板块")
    ap.add_argument("--trend-ma", type=int, default=20, help="趋势判定用的均线长度")
    ap.add_argument("--trend-slope", type=int, default=5, help="均线在几个交易日内向下算下跌趋势")
    ap.add_argument("--trend-mode", default="ma_down",
                    choices=["ma_down", "below_mas", "ma_stack"],
                    help="下跌趋势判定：ma_down = 收盘 < MA(trend-ma) 且 MA 下行；"
                         "below_mas = 收盘价低于 trend-mas 列出的每一条均线；"
                         "ma_stack = 均线空头排列（trend-stack 依次严格递减）")
    ap.add_argument("--trend-mas", default="5,10,20,30",
                    help="--trend-mode below_mas 时使用的均线组，逗号分隔")
    ap.add_argument("--trend-stack", default="10,20,50",
                    help="--trend-mode ma_stack 时的均线组")
    ap.add_argument("--stack-order", default="asc", choices=["asc", "desc"],
                    help="ma_stack 的排列方向：asc = MA10<MA20<MA50（空头排列，默认）；"
                         "desc = MA10>MA20>MA50（多头排列）")
    ap.add_argument("--anchor-lag", type=int, default=0,
                    help="调仓日相对锚点（默认月末最后交易日）前移几个交易日；"
                         "1 = 用月末倒数第二个交易日调仓（RSRS、CV 窗口、买入价全部随之移动）")
    ap.add_argument("--regime-ma", type=int, default=0,
                    help="用 SPY 与 N 日均线判定牛熊：线上牛市、线下熊市（0 = 不启用）。"
                         "启用后牛市用多头排列、熊市改用空头排列（或空仓，见 --regime-bear）")
    ap.add_argument("--regime-bear", default="nofilter",
                    choices=["nofilter", "downtrend", "cash"],
                    help="熊市中的处理：nofilter = 不做多头筛选，直接取 CV 第一名（默认）；"
                         "downtrend = 改买空头排列的票；cash = 空仓")
    ap.add_argument("--turn-rank", type=int, default=1,
                    help="--pick turn 时取第几名（1 = 换手率最高）")
    ap.add_argument("--cv-lag", type=int, default=0,
                    help="--pick dvolcv/dvolcvh 时，CV 窗口截止日相对信号日滞后的交易日数；"
                         "0 = 含信号日当天（默认），1 = 只用信号日之前的数据")
    ap.add_argument("--stop-loss", type=float, default=0.0,
                    help="个股止损阈值（如 0.3 = 相对买入价跌 30%% 收盘割肉），0 = 不止损；"
                         "止损后当期空仓到下一个调仓日")
    ap.add_argument("--turn-cap", type=float, default=0.0,
                    help="20 日均换手率上限，超过的票当期不参与排序（0 = 不限）。"
                         "用于剔除分母异常导致的高换手票（如 ECHO 2025 年后出现 100%%+ 的日换手）")
    ap.add_argument("--turn-tail", type=float, default=0.0,
                    help="信号日当天换手率的折算系数（0 = 不折算）。调仓在收盘前 30 分钟内完成，"
                         "当天量能只走完一部分，实测 15:30 时点约为日线口径的 0.46，"
                         "推荐 0.48；只作用于 20 日窗口里的信号日当天那一项")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔的代码，整只从候选池剔除（如 ECHO 这种换手率序列不可信的票）")
    ap.add_argument("--pool-cutoff", default="",
                    help="只允许该日期之前已有行情的票进候选池（YYYY-MM-DD）。用于把"
                         "「回测期内才出现/才被纳入指数」的票挡在外面，与全池结果一起构成"
                         "指数调入调出影响的区间")
    ap.add_argument("--pool-mode", default="snapshot", choices=["snapshot", "mcap", "pit"],
                    help="候选池口径：snapshot = 今天的成分股名单回测过去（默认，含幸存者偏差）；"
                         "mcap = 每个调仓日按 point-in-time 市值排名重建 top N 池，近似还原"
                         "当时的成分股；pit = 用 S&P 官方公告的调入调出记录重建每期真实名单"
                         "（2020-09 之后才完整，更早无代码）")
    ap.add_argument("--min-mcap", type=float, default=0.0,
                    help="最小市值门槛（亿美元），0 = 不限。剔除指数尾部小票：它们的"
                         "换手率分母小、噪声大，且天然高换手，会让按换手率排序的策略"
                         "系统性偏向小市值票")
    ap.add_argument("--mcap-shares", default="ws", choices=["ws", "static"],
                    help="--min-mcap 的股本口径：ws = 与实盘一致（5 日反推→SEC，逐日无前视，默认）；"
                         "static = 旧口径（全样本中位数+今日快照，含前视，仅作对照）")
    ap.add_argument("--pool-top", type=int, default=500,
                    help="--pool-mode mcap 时的池子容量（标普 500 约 500 只）")
    ap.add_argument("--pool-shares", default="current", choices=["current", "sec"],
                    help="--pool-mode mcap 算市值用的股本：current = universe 快照的当前股本"
                         "（全市场覆盖，复权价 × 当前股本）；sec = 只在有 SEC 申报股本的"
                         "501 只里排名（无过滤效果，仅对照）")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    sn = args.sn or args.n
    sm = args.sm or args.m
    end = pd.Timestamp(args.end)
    start = pd.Timestamp(args.start)
    warm = max(args.n + args.m, sn + sm) * 2 + 40
    load_start = (start - pd.DateOffset(days=warm)).strftime("%Y-%m-%d")

    members = load_members()
    sec_of = dict(zip(members["symbol"], members["sector_etf"]))
    mcap_of = dict(zip(members["symbol"], members["mcap"]))
    stocks = list(members["symbol"])
    pit_ev = None
    pit_anchor: set[str] = set()
    if args.pool_mode == "pit":
        pit_ev = load_sp500_change_events()
        pit_anchor = set(stocks)
        # 已被剔除出指数的票不在当前 holdings 里，没有板块归属；用公告里的 GICS 板块补。
        gics = (pit_ev.sort_values("effective_date")
                .drop_duplicates("symbol", keep="last").set_index("symbol")["gics_sector"])
        filled = 0
        for sym, g in gics.items():
            if sym not in sec_of:
                etf = GICS_TO_ETF.get(str(g).strip())
                if etf:
                    sec_of[sym] = etf
                    filled += 1
        # 面板里必须包含「回测期内被剔除」的票，否则它永远买不到（这正是要修的偏差）
        inwin = set(pit_ev.loc[pit_ev["effective_date"] >= start, "symbol"])
        extra = sorted((inwin | pit_anchor) - set(stocks))
        stocks = list(dict.fromkeys(stocks + extra))
        print(f"--pool-mode pit：锚点 {len(pit_anchor)} 只，补入期内被剔除/新增 {len(extra)} 只"
              f"（{filled} 只用公告 GICS 板块补齐归属）")
    if args.exclude:
        drop = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
        hit = sorted(set(stocks) & drop)
        stocks = [s for s in stocks if s not in drop]
        print(f"--exclude 剔除 {len(hit)} 只：{' '.join(hit)}")
    if args.pool_cutoff:
        # 只在 cutoff 之前就已有行情的票才进候选池。用途：把「回测期内才出现/才被纳入」的票
        # 挡在外面，得到「期初固定池」的保守结果，用来量指数调入调出的影响上界。
        cutoff = pd.Timestamp(args.pool_cutoff)
        con = store.duckdb_conn()
        first = con.execute(
            "SELECT symbol, min(date) AS first_date FROM bars GROUP BY 1").fetch_df()
        con.close()
        first["first_date"] = pd.to_datetime(first["first_date"])
        early = set(first.loc[first["first_date"] <= cutoff, "symbol"])
        late = sorted(set(stocks) - early)
        stocks = [s for s in stocks if s in early]
        print(f"--pool-cutoff {cutoff.date()}：剔除 {len(late)} 只在期后才出现的票："
              f"{' '.join(late[:30])}{' ...' if len(late) > 30 else ''}")
    print(f"成分股 {len(stocks)} 只在本地行情池内（11 板块，快照 2026-11-11）")

    syms = SECTOR_ETFS + [BENCH] + stocks
    hi = store.load_panel(field="high", symbols=syms, start=load_start,
                          end=end.strftime("%Y-%m-%d")).sort_index().ffill()
    lo = store.load_panel(field="low", symbols=syms, start=load_start,
                          end=end.strftime("%Y-%m-%d")).sort_index().ffill()
    px = store.load_panel(field="adj_close", symbols=syms, start=load_start,
                          end=end.strftime("%Y-%m-%d")).sort_index().ffill()

    # 区间内完全没有行情的成分股（多为区间之后才上市/拆分出来的票）直接剔除，
    # 否则 px[stocks] / hi[stocks] 会因列缺失报 KeyError。
    have = [s for s in stocks if s in px.columns and s in hi.columns and s in lo.columns]
    if len(have) < len(stocks):
        print(f"区间内无行情的成分股 {len(stocks) - len(have)} 只已剔除："
              f"{' '.join(sorted(set(stocks) - set(have)))}")
    stocks = have

    etf_rsrs = rsrs_panel_fast(hi[SECTOR_ETFS], lo[SECTOR_ETFS], args.n, args.m, args.spec)
    print(f"板块层 RSRS 完成 {etf_rsrs.shape}")
    stock_rsrs = rsrs_panel_fast(hi[stocks], lo[stocks], sn, sm, args.spec)
    print(f"个股层 RSRS 完成 {stock_rsrs.shape}（N={sn}, M={sm}）")

    cal = px.index
    bench = px[BENCH]
    ret_etf = px[SECTOR_ETFS].pct_change()
    ret_stk = px[stocks].pct_change()

    # 逐期成分股池（--pool-mode mcap 时启用）。默认 snapshot = 用今天的名单回测过去。
    pool_mask = None
    if args.pool_mode == "mcap":
        pool_mask = mcap_pool_mask(stocks, cal, load_start, end.strftime("%Y-%m-%d"),
                                   args.pool_top, args.pool_shares)
    elif args.pool_mode == "pit":
        print(f"--pool-mode pit：按官方公告重建逐日名单（{len(pit_ev)} 条事件）")
        pool_mask = pit_pool_mask(stocks, cal, pit_ev, pit_anchor)

    # 最小市值过滤。小市值票的换手率分母（股本）小、噪声大，而且这类票天然高换手，
    # 会让「按换手率排序」系统性偏向指数尾部的小票（NCLH 市值 65~100 亿美元、
    # 日换手 5%，长期排在 XLY 第一）。这是口径修正，不是调参。
    if args.min_mcap > 0:
        thr = args.min_mcap * 1e8
        vol_m = store.load_panel(field="volume", symbols=stocks, start=load_start,
                                 end=end.strftime("%Y-%m-%d")).sort_index()
        px_c = store.load_panel(field="close", symbols=stocks, start=load_start,
                                end=end.strftime("%Y-%m-%d")).sort_index()
        tv_all = pd.read_parquet(ROOT / "data" / "curated" / "fundamentals"
                                 / "turnover_daily.parquet")
        tv_p = tv_all.pivot(index="date", columns="symbol", values="turnover").sort_index()
        cols_m = [c for c in stocks if c in vol_m.columns and c in px_c.columns]
        implied = (vol_m[cols_m] / tv_p.reindex(index=vol_m.index, columns=cols_m)
                   .where(lambda x: x > 0)).replace([np.inf, -np.inf], np.nan)
        if args.mcap_shares == "static":
            # 旧口径（含前视，仅作对照）：全样本中位数 + universe 快照兜底
            sh = implied.median()
            uni_m = pd.read_parquet(universe.latest_file())
            uni_m["symbol"] = uni_m["symbol"].astype(str).str.upper()
            snap = (uni_m["market_cap"] / uni_m["price"]).replace([np.inf, -np.inf], np.nan)
            snap.index = uni_m["symbol"].values
            sh = sh.fillna(snap)
            mcap = px_c[cols_m].mul(sh[cols_m], axis=1).reindex(cal).ffill()
        else:
            # 与实盘 quant.live.mcap 同口径（逐日、无前视）：
            # ① 最近 5 个交易日 成交量÷换手率 的中位数（至少 3 个有效点）
            # ② 缺失时用 SEC 申报股本（filed ≤ 当日，且不超过 400 天）
            # 快照那一级历史上不可得（只有今天一份），回测不用
            # 回测的 pit 池里有已调出/退市的票，换手率面板对它们有缺口（实盘池全是现成分股，无此问题）。
            # 缺口处沿用「最近一次可信股本」最多 250 个交易日（只用过去，无前视），再退到 SEC，
            # 否则这些小票会因「取不到市值→保留」而混进候选池
            sh_ws = implied.rolling(5, min_periods=3).median().ffill(limit=250)
            so_path = ROOT / "data" / "curated" / "fundamentals" / "shares_outstanding.parquet"
            sh_t = sh_ws
            if so_path.exists():
                so = pd.read_parquet(so_path)
                so["symbol"] = so["symbol"].astype(str).str.upper().str.replace(".", "-", regex=False)
                so["filed"] = pd.to_datetime(so["filed"], errors="coerce")
                so = so[(so["val"] > 1000) & so["filed"].notna() & so["symbol"].isin(cols_m)]
                so = so.sort_values("filed").groupby(["filed", "symbol"]).tail(1)
                idx = vol_m.index.union(pd.DatetimeIndex(so["filed"].unique())).sort_values()
                val = so.pivot(index="filed", columns="symbol", values="val").reindex(idx).ffill()
                fdate = so.assign(f=so["filed"]).pivot(index="filed", columns="symbol", values="f")
                fdate = fdate.reindex(idx).ffill()
                age = pd.DataFrame(idx.values[:, None] - fdate.values, index=idx, columns=fdate.columns)
                val = val.where(age <= pd.Timedelta(days=400))
                val = val.reindex(index=vol_m.index, columns=cols_m)
                sh_t = sh_ws.where(sh_ws.notna(), val)
            # ④ 仍缺（换手率面板整段缺失，如 MTCH 2023-10~2025-09 不在面板内）：退到全样本中位数。
            #    这一级含前视，但只补数据缺口，股本变化慢，且比「取不到→保留」更接近实盘
            sh_t = sh_t.fillna(implied.median())
            mcap = (px_c[cols_m] * sh_t).reindex(cal)     # 不 ffill：当日取不到就是未知（保留）
        ok = (mcap > thr) | mcap.isna()      # 拿不到市值的保留，靠换手率那一步自然淘汰
        ok = ok.reindex(columns=stocks).fillna(True).astype(bool)
        gone = sorted(s for s in stocks if not bool(ok[s].any()))
        pool_mask = ok if pool_mask is None else (pool_mask & ok)
        print(f"--min-mcap {args.min_mcap:.0f} 亿美元（股本口径 {args.mcap_shares}）："
              f"全程不达标的 {len(gone)} 只移出候选 "
              f"({' '.join(gone[:16])}{' ...' if len(gone) > 16 else ''})")

    def in_pool(sym: str, dt_: pd.Timestamp) -> bool:
        if pool_mask is None:
            return True
        try:
            return bool(pool_mask.at[dt_, sym])
        except KeyError:
            return False

    # 换手率 = 成交量 / 流通股本。本地无股本序列，用 universe 快照的 市值/价格 推算，
    # 即假设股本在回测期内不变（前视 + 近似，见 PROGRESS 记录）。
    turn_rate = None
    dvol = None
    dvol_cv = None
    if args.pick in ("turn", "dvol", "dvolcv", "dvolcvh", "cvdown", "turnma"):
        vol = store.load_panel(field="volume", symbols=stocks, start=load_start,
                               end=end.strftime("%Y-%m-%d")).sort_index()
        if args.pick in ("turn", "turnma"):
            if args.turn_shares == "snapshot":
                # 旧口径：用 universe 快照的 市值/价格 推算股本（前视，仅作对照）
                uni = pd.read_parquet(universe.latest_file())
                uni["symbol"] = uni["symbol"].astype(str).str.upper()
                sh = (uni["market_cap"] / uni["price"]).replace([np.inf, -np.inf], np.nan)
                shares = pd.Series(sh.values, index=uni["symbol"]).dropna()
                shares = shares[shares > 0]
                cols = [c for c in stocks if c in shares.index and c in vol.columns]
                turn_rate = (vol[cols].rolling(20, min_periods=10).mean()
                             .div(shares[cols], axis=1).reindex(cal).ffill())
                print(f"换手率面板完成 {turn_rate.shape}（旧口径：股本由当前市值/价格推算，"
                      f"共 {len(cols)} 只）")
            else:
                # 数据源口径：腾讯自选股日 K 的换手率字段。注意它对美股是「成交量/总股本」
                # 口径（实测 WMT/COIN/RDDT 全部对得上总股本），保留作对照，不是流通股本。
                path = ROOT / "data" / "curated" / "fundamentals" / "turnover_daily.parquet"
                if not path.exists():
                    raise SystemExit(f"换手率面板不存在（应由 data 分支提供）：{path}")
                tv = pd.read_parquet(path)
                panel = tv.pivot(index="date", columns="symbol", values="turnover").sort_index()
                cols = [c for c in stocks if c in panel.columns]
                missing = sorted(set(stocks) - set(cols))
                raw = panel[cols].reindex(cal).ffill()
                if args.turn_tail > 0:
                    # 调仓在收盘前 30 分钟内完成，信号日当天的成交量此时只走完一部分
                    # （实测 15:30 时点约完成日线口径的 45.6%），把当天这一项按系数折算，
                    # 其余 19 天仍用完整日值：sum(20) - today + c × today。
                    cnt = raw.notna().rolling(20, min_periods=1).sum()
                    s20 = raw.rolling(20, min_periods=1).sum()
                    turn_rate = ((s20 - raw + args.turn_tail * raw) / cnt).where(cnt >= 10)
                    print(f"信号日换手率按尾盘系数 {args.turn_tail:.2f} 折算")
                else:
                    turn_rate = raw.rolling(20, min_periods=10).mean()
                print(f"换手率面板完成 {turn_rate.shape}（数据源口径：westock 日 K 换手率，"
                      f"美股为成交量/总股本，20 日均值，共 {len(cols)} 只；无数据 {len(missing)} 只）")
                if missing:
                    print(f"  无换手率数据已剔除：{' '.join(missing[:20])}"
                          f"{' ...' if len(missing) > 20 else ''}")
            if args.turn_cap > 0:
                # 20 日均换手率超过上限的票，当期直接不参与排序（分母异常 / 极端事件）。
                n_cap = int((turn_rate > args.turn_cap).sum().sum())
                turn_rate = turn_rate.mask(turn_rate > args.turn_cap)
                print(f"换手率上限 {args.turn_cap:.0%}：剔除 {n_cap} 个票日")
            if args.turn_noise > 0:
                # 稳健性扰动：给每只票每天的换手率乘一个对数正态噪声（sigma = turn_noise），
                # 模拟「分母有 ±sigma 量级误差」，看选股与收益对这个扰动的敏感度。
                rng = np.random.default_rng(args.seed)
                noise = rng.normal(0.0, args.turn_noise, size=turn_rate.shape)
                turn_rate = turn_rate * np.exp(pd.DataFrame(
                    noise, index=turn_rate.index, columns=turn_rate.columns))
                print(f"换手率已注入噪声 sigma={args.turn_noise:.2f}（seed={args.seed}）")
        else:
            cols = [c for c in stocks if c in vol.columns]
            amt = vol[cols] * px[cols]
            if args.pick == "dvol":
                dvol = (amt.rolling(20, min_periods=10).mean()
                        .reindex(cal).ffill())
                print(f"成交额面板完成 {dvol.shape}（20 日均值，volume × adj_close，共 {len(cols)} 只）")
            else:
                # 变异系数 = 20 日成交额标准差 / 20 日均值，衡量成交额稳定度（越低越稳）
                mean_ = amt.rolling(20, min_periods=10).mean()
                std_ = amt.rolling(20, min_periods=10).std()
                dvol_cv = (std_ / mean_.where(mean_ > 0)).reindex(cal).ffill()
                if args.cv_lag:
                    dvol_cv = dvol_cv.shift(args.cv_lag)
                print(f"成交额变异系数面板完成 {dvol_cv.shape}"
                      f"（20 日 std/mean，volume × adj_close，窗口滞后 {args.cv_lag} 个交易日，"
                      f"共 {len(cols)} 只）")

    # 下跌趋势判定（cvdown 用）：收盘价低于 N 日均线，且均线本身在 M 个交易日内下行。
    # 只用信号日及之前的数据，不含未来。
    ma_t = px[stocks].rolling(args.trend_ma, min_periods=max(5, args.trend_ma // 2)).mean()
    ma_t_prev = ma_t.shift(args.trend_slope)
    ma_group = [int(x) for x in str(args.trend_mas).split(",") if str(x).strip()]
    ma_panels = {w: px[stocks].rolling(w, min_periods=max(3, w // 2)).mean() for w in ma_group}
    stack_group = [int(x) for x in str(args.trend_stack).split(",") if str(x).strip()]
    stack_panels = {w: px[stocks].rolling(w, min_periods=max(3, w // 2)).mean() for w in stack_group}

    # 牛熊判定：SPY 收盘与 120 日（可配）均线比较，线上 = 牛、线下 = 熊
    spy_ma = (px[BENCH].rolling(args.regime_ma, min_periods=max(20, args.regime_ma // 2)).mean()
              if args.regime_ma > 0 else None)

    def is_bull(dt: pd.Timestamp) -> bool:
        if spy_ma is None:
            return True
        a, m = px.at[dt, BENCH], spy_ma.at[dt]
        return bool(np.isfinite(a) and np.isfinite(m) and a > m)

    def is_down(sym: str, dt: pd.Timestamp, desc: bool | None = None) -> bool:
        if sym not in ma_t.columns:
            return False
        a = px.at[dt, sym]
        if not np.isfinite(a):
            return False
        if args.trend_mode == "ma_stack":
            # asc：MA10 < MA20 < MA50 空头排列；desc：MA10 > MA20 > MA50 多头排列
            use_desc = (args.stack_order == "desc") if desc is None else bool(desc)
            prev = None
            for w in stack_group:
                m = stack_panels[w].at[dt, sym]
                if not np.isfinite(m):
                    return False
                if prev is not None:
                    if not use_desc and not (prev < m):
                        return False
                    if use_desc and not (prev > m):
                        return False
                prev = m
            return True
        if args.trend_mode == "below_mas":
            # 收盘价必须低于每一条均线（5/10/20/30 全在上方）
            for w in ma_group:
                m = ma_panels[w].at[dt, sym]
                if not np.isfinite(m) or a >= m:
                    return False
            return True
        m, mp = ma_t.at[dt, sym], ma_t_prev.at[dt, sym]
        if not (np.isfinite(m) and np.isfinite(mp)):
            return False
        return bool(a < m and m < mp)

    me = anchors(cal, args.freq)
    if args.anchor_lag:
        # 调仓日整体前移：RSRS 评分日、CV 窗口截止日、买入价与持有区间一起移动
        pos = np.clip(cal.get_indexer(me) - args.anchor_lag, 0, None)
        me = pd.DatetimeIndex(cal[np.unique(pos)])
        print(f"调仓日前移 {args.anchor_lag} 个交易日（月末倒数第 {args.anchor_lag + 1} 个交易日）")
    scored = etf_rsrs.dropna(how="any").index
    cand = [i for i, d in enumerate(me) if d <= start and d in scored]
    if not cand:
        raise SystemExit("预热不足")
    k0 = cand[-1]

    nav_etf = pd.Series(np.nan, index=cal[cal >= start], dtype=float)   # 对照：直接买 ETF
    nav = nav_etf.copy()
    cur_s, cur_e, prev = 1.0, 1.0, None
    rows = []
    for k in range(k0, len(me) - 1):
        sig = me[k]
        if sig not in scored:
            continue
        sec_score = etf_rsrs.loc[sig].dropna().sort_values(ascending=False)
        if sec_score.empty:
            continue
        sector = str(sec_score.index[0])

        pool = [s for s in stocks if sec_of.get(s) == sector and s in stock_rsrs.columns
                and in_pool(s, sig)]
        ss = stock_rsrs.loc[sig, pool].dropna().sort_values(ascending=False) if pool else pd.Series(dtype=float)
        tried, sec_rank = 0, 0
        bull = is_bull(sig)
        if args.pick in ("mcap", "mmin"):
            ranked = sorted([s for s in pool if np.isfinite(mcap_of.get(s, np.nan))],
                            key=lambda s: float(mcap_of[s]), reverse=(args.pick == "mcap"))
            pick = ranked[0] if ranked else ""
        elif args.pick == "turn":
            cand = [s for s in pool if s in turn_rate.columns]
            t = turn_rate.loc[sig, cand].dropna().sort_values(ascending=False) \
                if cand else pd.Series(dtype=float)
            ri = min(max(1, args.turn_rank), len(t)) - 1
            pick = str(t.index[ri]) if len(t) else ""
        elif args.pick == "turnma":
            # 换手率降序 + 均线排列：按板块 RSRS 顺序下探，板块内按换手率取前 N 只，
            # 买第一只满足排列方向的（默认 desc = 多头排列，即上升趋势）。
            use_desc = (args.stack_order == "desc") if args.regime_ma == 0 else bull
            pick, tried, sec_rank = "", 0, 0
            for rank_s, sec_k in enumerate(sec_score.index[: args.turnma_sectors], 1):
                pool_k = [s for s in stocks if sec_of.get(s) == sec_k
                          and s in turn_rate.columns and in_pool(s, sig)]
                if not pool_k:
                    continue
                tt = (turn_rate.loc[sig, pool_k].dropna().sort_values(ascending=False)
                      .head(args.turnma_top))
                for s in tt.index:
                    tried += 1
                    if is_down(str(s), sig, use_desc):
                        pick, sector, sec_rank = str(s), str(sec_k), rank_s
                        break
                if pick:
                    break
        elif args.pick == "dvol":
            cand = [s for s in pool if s in dvol.columns]
            t = dvol.loc[sig, cand].dropna().sort_values(ascending=False) \
                if cand else pd.Series(dtype=float)
            pick = str(t.index[0]) if len(t) else ""
        elif args.pick in ("dvolcv", "dvolcvh"):
            cand = [s for s in pool if s in dvol_cv.columns]
            t = dvol_cv.loc[sig, cand].dropna().sort_values(
                ascending=(args.pick == "dvolcv")) if cand else pd.Series(dtype=float)
            pick = str(t.index[0]) if len(t) else ""
        elif args.pick == "cvdown":
            # 按板块 RSRS 顺序逐个下探：板块内取 CV 最高的前 N 只，买第一只满足均线排列的；
            # 整个板块都不合格就换下一个板块；全部候选都不合格则当期空仓。
            # 启用 --regime-ma 时：牛市（SPY 在线上）要多头排列；
            # 熊市按 --regime-bear 处理：nofilter 不做多头筛选（直接取 CV 第一名）、
            # downtrend 改要空头排列、cash 空仓。
            use_desc = (args.stack_order == "desc") if args.regime_ma == 0 else bull
            bear = args.regime_ma > 0 and not bull
            skip_all = (bear and args.regime_bear == "cash")
            pick, tried, sec_rank = "", 0, 0
            if bear and args.regime_bear == "nofilter":
                pool1 = [s for s in stocks
                         if sec_of.get(s) == sec_score.index[0] and s in dvol_cv.columns
                         and in_pool(s, sig)]
                t1 = (dvol_cv.loc[sig, pool1].dropna().sort_values(ascending=False)
                      if pool1 else pd.Series(dtype=float))
                if len(t1):
                    pick, tried, sec_rank = str(t1.index[0]), 1, 1
            elif not skip_all:
                for rank_s, sec_k in enumerate(sec_score.index[: args.cvdown_sectors], 1):
                    pool_k = [s for s in stocks if sec_of.get(s) == sec_k
                              and s in dvol_cv.columns and in_pool(s, sig)]
                    if not pool_k:
                        continue
                    tt = dvol_cv.loc[sig, pool_k].dropna().sort_values(ascending=False).head(args.cvdown_top)
                    for s in tt.index:
                        tried += 1
                        if is_down(str(s), sig, use_desc):
                            pick, sector, sec_rank = str(s), str(sec_k), rank_s
                            break
                    if pick:
                        break
        else:
            pick = str(ss.index[0]) if len(ss) else ""

        nxt = me[k + 1]
        seg = cal[(cal > sig) & (cal <= nxt) & (cal >= start)]
        if len(seg) == 0:
            prev = pick
            continue

        turnover = 0.0 if pick == prev else (2.0 if prev else 1.0)
        base_px = float(px.at[sig, pick]) if pick else np.nan   # 买入价（信号日收盘）
        stopped, stop_dt = False, None
        for j, dt in enumerate(seg):
            if j == 0:
                cur_s *= (1.0 - args.cost * turnover)
                cur_e *= (1.0 - args.cost * turnover)
            if pick and not stopped:
                a = ret_stk.loc[dt, pick]
                cur_s *= (1.0 + (0.0 if pd.isna(a) else a))
                # 收盘价比买入价跌破阈值 → 当日收盘割肉，当期剩余时间空仓
                if args.stop_loss > 0 and np.isfinite(base_px) and base_px > 0:
                    if px.at[dt, pick] / base_px - 1.0 <= -args.stop_loss:
                        stopped, stop_dt = True, dt.date()
            b = ret_etf.loc[dt, sector]
            cur_e *= (1.0 + (0.0 if pd.isna(b) else b))
            nav.loc[dt] = cur_s
            nav_etf.loc[dt] = cur_e

        seg_s = nav.loc[seg].iloc[-1] / nav.loc[seg].iloc[0] - 1
        seg_e = nav_etf.loc[seg].iloc[-1] / nav_etf.loc[seg].iloc[0] - 1
        sp = bench.reindex(seg).ffill()
        rows.append({
            "信号日": sig.date(), "持有期起": seg[0].date(), "持有期止": seg[-1].date(),
            "板块": sector,
            "板块RSRS": round(float(sec_score[sector]) if sector in sec_score.index
                            else float(sec_score.iloc[0]), 2),
            "板块RSRS序": sec_rank or 1,
            "牛市": int(bull),
            "个股": pick,
            "候选尝试": tried,
            "个股RSRS": (round(float(stock_rsrs.at[sig, pick]), 2)
                       if pick and pick in stock_rsrs.columns else np.nan),
            "同板块候选": len(ss),
            "个股收益": round(float(seg_s), 4), "ETF收益": round(float(seg_e), 4),
            "选股差": round(float(seg_s - seg_e), 4), "止损日": stop_dt,
            "SPY": round(float(sp.iloc[-1] / sp.iloc[0] - 1), 4) if len(sp) > 1 else np.nan,
        })
        prev = pick

    nav = nav.dropna()
    nav_etf = nav_etf.reindex(nav.index)
    st, st_e = perf(nav), perf(nav_etf)
    spy_px = bench.reindex(nav.index).ffill()
    st_spy = perf(spy_px / spy_px.iloc[0] * nav.iloc[0])

    hdf = pd.DataFrame(rows)
    print(f"\n区间 {nav.index[0].date()} ~ {nav.index[-1].date()}  "
          f"{len(nav)} 交易日  {len(hdf)} 次调仓")
    print(hdf.to_string(index=False))

    cmp = pd.DataFrame({"个股版": st, "ETF版": st_e, "SPY": st_spy}).T
    print("\n" + cmp.round(4).to_string())
    print(f"\n选股跑赢 ETF {int((hdf['选股差'] > 0).sum())}/{len(hdf)} 期，"
          f"累计选股差 {hdf['选股差'].sum():+.2%}")

    tag = f"_{args.tag}" if args.tag else ""
    ftag = "" if args.freq == "M" else f"_{args.freq}"
    ptag = "" if args.pick == "rsrs" else f"_{args.pick}"
    if args.pick == "turn" and args.turn_rank > 1:
        ptag += f"_r{args.turn_rank}"
    if args.stop_loss > 0:
        ptag += f"_sl{round(args.stop_loss * 100)}"
    if args.cv_lag:
        ptag += f"_lag{args.cv_lag}"
    if args.pick in ("cvdown", "turnma") and args.trend_mode != "ma_down":
        ptag += f"_{args.trend_mode}"
        if args.trend_mode == "ma_stack":
            ptag += f"_{args.stack_order}"
    if args.pick in ("turn", "turnma"):
        ptag += {"ws": "_turnws", "snapshot": "_shsnap"}[args.turn_shares]
    if args.turn_cap > 0:
        ptag += f"_cap{round(args.turn_cap * 100)}"
    if args.turn_tail > 0:
        ptag += f"_tail{round(args.turn_tail * 100)}"
    if args.exclude:
        ptag += "_ex" + args.exclude.upper().replace(",", "")
    if args.pool_cutoff:
        ptag += "_pc" + pd.Timestamp(args.pool_cutoff).strftime("%y%m%d")
    if args.pool_mode == "mcap":
        ptag += f"_pmcap{args.pool_top}" + ("" if args.pool_shares == "current"
                                            else f"_{args.pool_shares}")
    if args.pool_mode == "pit":
        ptag += "_ppit"
    if args.min_mcap > 0:
        ptag += f"_mc{int(args.min_mcap)}" + ("_mcstat" if args.mcap_shares == "static" else "")
    if args.anchor_lag:
        ptag += f"_al{args.anchor_lag}"
    if args.regime_ma > 0:
        ptag += f"_reg{args.regime_ma}"
        if args.regime_bear != "downtrend":
            ptag += f"_bear{args.regime_bear}"
    out = ROOT / "reports" / "etf" / f"rsrs_stock_n{sn}_m{sm}_top1{ptag}{ftag}{tag}"
    out.mkdir(parents=True, exist_ok=True)
    nav.rename("nav").to_csv(out / "nav_daily.csv", encoding="utf-8-sig")
    nav_etf.rename("nav_etf").to_csv(out / "nav_etf_daily.csv", encoding="utf-8-sig")
    hdf.to_csv(out / "holds.csv", index=False, encoding="utf-8-sig")
    cmp.round(6).to_csv(out / "stats.csv", encoding="utf-8-sig")
    print(f"\n产出 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
