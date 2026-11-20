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

from quant.data import crsp, store, universe
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


def adx_panel(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
              n: int = 14) -> pd.DataFrame:
    """Wilder ADX（趋势强度，不区分方向）。

    公式链：TR / ±DM → RMA 平滑 → ±DI → DX → 再平滑一次得 ADX。
    平滑用 ``ewm(alpha=1/n, adjust=False)``，等价于 Wilder 的递归均值；``min_periods=n``
    保证前 n-1 根是 NaN，不给「刚上市几天就判强趋势」的机会。
    """
    h, l, c = high.astype(float), low.astype(float), close.astype(float)
    pc = c.shift(1)
    tr = np.maximum(np.maximum(h - l, (h - pc).abs()), (l - pc).abs())

    up, dn = h.diff(), -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)

    smooth = lambda df: df.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()  # noqa: E731
    atr = smooth(tr)
    pdi = 100.0 * smooth(plus_dm) / atr.where(atr > 0)
    mdi = 100.0 * smooth(minus_dm) / atr.where(atr > 0)
    den = (pdi + mdi).where((pdi + mdi) > 0)
    dx = 100.0 * (pdi - mdi).abs() / den
    return smooth(dx)


def weighted_slope_panel(px: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """加权线性回归斜率，按价格归一化成「每日涨幅」（≈ %/交易日）。

    为什么不用等权回归：等权斜率对窗口首尾同样敏感，一个旧的跳空会拖住整段判断。
    这里给近期更大的权重（w = 1..n，最新一天权重最大），得到的斜率更接近「现在在不在涨」。

    为什么除以均价：XLK 两百多块、XLF 五十块，绝对斜率不可比；归一化后是「每天涨百分之几」，
    11 个板块才能放在一起排序。

    这是给熊市用的：RSRS 是 high~low 回归的 z-score，衡量的是「日内强弱关系」的相对位置，
    在单边下跌里会频繁换手（2022 年就在 XLU/XLK/XLC/XLY 之间来回跳，而全年最强是 XLE）。
    """
    t = np.arange(n, dtype=float)
    w = np.arange(1.0, n + 1.0)
    w = w / w.sum()
    t_bar = float((w * t).sum())
    denom = float((w * (t - t_bar) ** 2).sum())
    coef = w * (t - t_bar) / denom          # slope = Σ coef_i · y_i，一次滚动点积搞定

    slope = px.rolling(n, min_periods=n).apply(lambda a: float(np.dot(coef, a)), raw=True)
    level = px.rolling(n, min_periods=n).mean()
    return slope / level.where(level > 0)


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


def mcap_above(mcap: pd.DataFrame | None, sym: str, dt: pd.Timestamp, thr: float) -> bool:
    """当日市值是否严格大于 thr（单位：美元）。面板缺失 / 不设门槛 / 取不到市值时放行。

    口径与 ``--min-mcap`` 那道闸一致：市值 = 未复权收盘价 × 股本（股本优先用
    成交量 ÷ 换手率 的逐日中位数，缺换手率的票用 universe 快照的 市值/价格 兜底）。
    """
    if mcap is None or thr <= 0:
        return True
    if sym not in mcap.columns or dt not in mcap.index:
        return True
    v = float(mcap.at[dt, sym])
    return bool(np.isfinite(v) and v > thr)


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
    ap.add_argument("--bull-refill", action="store_true",
                    help="牛市当期选不出个股时，从下一个交易日起逐日重挑（当日 RSRS / 换手率 / "
                         "趋势闸全部重算），买到为止；一直找不到才空仓到下一个调仓日")
    ap.add_argument("--trend-ma", type=int, default=20, help="趋势判定用的均线长度")
    ap.add_argument("--trend-slope", type=int, default=5, help="均线在几个交易日内向下算下跌趋势")
    ap.add_argument("--trend-mode", default="ma_down",
                    choices=["ma_down", "below_mas", "ma_stack", "strong"],
                    help="下跌趋势判定：ma_down = 收盘 < MA(trend-ma) 且 MA 下行；"
                         "below_mas = 收盘价低于 trend-mas 列出的每一条均线；"
                         "ma_stack = 均线空头排列（trend-stack 依次严格递减）；"
                         "strong = 强化趋势过滤（多头排列 + 各均线上行 + 站上长期均线 + ADX 下限）")
    ap.add_argument("--trend-mas", default="5,10,20,30",
                    help="--trend-mode below_mas 时使用的均线组，逗号分隔")
    ap.add_argument("--trend-stack", default="10,20,50",
                    help="--trend-mode ma_stack/strong 时的均线组（strong 下要求依次严格递减）")
    ap.add_argument("--stack-order", default="asc", choices=["asc", "desc"],
                    help="ma_stack 的排列方向：asc = MA10<MA20<MA50（空头排列，默认）；"
                         "desc = MA10>MA20>MA50（多头排列）")
    ap.add_argument("--stack-slope", type=int, default=5,
                    help="--trend-mode strong：要求每条均线 MA(t) > MA(t-该值)，0 = 不检查")
    ap.add_argument("--slope-mas", default="20,60",
                    help="--trend-mode strong：哪几条均线必须斜率为正（默认 20,60）。"
                         "MA5 斜率翻转太快，单日噪声就能否决一只原本多头排列的票，"
                         "默认不再要求它；传 5,20,60 可回到旧口径，空字符串 = 不检查")
    ap.add_argument("--trend-above", type=int, default=200,
                    help="--trend-mode strong：收盘价必须站上的均线长度，0 = 不检查")
    ap.add_argument("--trend-cross", default="50,200",
                    help="--trend-mode strong：均线先后关系「快,慢」，要求 MA(快) > MA(慢)；"
                         "空字符串 = 不检查")
    ap.add_argument("--adx-min", type=float, default=20.0,
                    help="--trend-mode strong：ADX 下限（严格大于），0 = 不检查")
    ap.add_argument("--adx-len", type=int, default=14, help="ADX 的 Wilder 平滑窗口")
    ap.add_argument("--bear-sector-ma", type=int, default=0,
                    help="熊市（--regime-ma 判定为线下）时只保留站上该均线的板块参与 RSRS 排序；"
                         "0 = 不过滤。逻辑：熊市里资金会抱团避险，掉在均线下方的板块不该占排名")
    ap.add_argument("--bear-etf", action="store_true",
                    help="熊市里个股选不到时改买最强板块 ETF，而不是空仓")
    ap.add_argument("--bear-etf-only", action="store_true",
                    help="熊市直接买最强板块 ETF，完全不碰个股（对 --bear-etf 的更强版本）")
    ap.add_argument("--bear-spy", action="store_true",
                    help="熊市里双条件全不达标（或动量门槛不过）时买 SPY，而不是空仓")
    ap.add_argument("--bear-pick", default="sector", choices=["sector", "turn5"],
                    help="熊市选股方式：sector = 板块双条件（RSRS∩斜率）+ 板块 ETF（默认）；"
                         "turn5 = 不分板块，全市场（已过市值门槛）按换手率取前 N 只，"
                         "在其中找第一只满足多头趋势的个股")
    ap.add_argument("--bear-turn-top", type=int, default=5,
                    help="--bear-pick turn5：全市场按换手率取前几名")
    ap.add_argument("--bear-turn-mcap", type=float, default=500.0,
                    help="--bear-pick turn5：只保留当日市值严格大于该值（亿美元）的个股，"
                         "默认 500（与实盘市值门槛同口径）；0 = 不额外过滤")
    ap.add_argument("--bear-min-score", type=float, default=None,
                    help="熊市买 ETF 时要求最强板块的 RSRS 严格大于该值；默认不检查。"
                         "传 0 = 动量必须为正，否则当期空仓（熊市里动量都向下的板块不值得买）")
    ap.add_argument("--bear-rank", default="rsrs", choices=["rsrs", "slope", "both"],
                    help="熊市里板块排序依据：rsrs = RSRS 分数（默认）；slope = 加权回归斜率"
                         "（近期权重更高，归一化成 %/交易日），直接衡量板块本身在不在涨；"
                         "both = 双条件，必须同时进两个榜单的前 --bear-intersect-top 名")
    ap.add_argument("--bear-slope-window", type=int, default=20,
                    help="--bear-rank slope/both 的回归窗口（交易日）")
    ap.add_argument("--bear-intersect-top", type=int, default=3,
                    help="--bear-rank both：RSRS 榜与斜率榜各取前 N 名，交集才进候选；"
                         "交集为空则当期空仓")
    ap.add_argument("--bear-lookback", type=int, default=0,
                    help="--bear-rank both：信号日交集为空时，回看过去 N 个交易日，取最近一次"
                         "双榜同时满足的板块（仍按信号日收盘买入、持有整月）；0 = 不回看")
    ap.add_argument("--bear-refill", action="store_true",
                    help="熊市双榜（--bear-rank both）当期交集为空或动量不达标时，从下一个交易日"
                         "起逐日重算双榜，第一天同时满足两榜就按当日收盘买入该板块 ETF；"
                         "到下一个调仓日仍没出现则空仓（与 --bull-refill 同一套语义）")
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
    ap.add_argument("--regime-window", type=int, default=0,
                    help="牛熊判定用「过去 N 个交易日」的统计而非当日单点：0/1 = 只看当日（旧口径）；"
                         "21 = 过去一个月。单日跌破均线不再直接判熊，避免在均线附近反复横跳")
    ap.add_argument("--regime-bear-ratio", type=float, default=0.5,
                    help="配合 --regime-window：过去 N 日里 SPY 低于均线的天数占比 ≥ 该值才算熊市"
                         "（默认 0.5）。占比越高越迟钝，1.0 = 整段时间都在线下才判熊")
    ap.add_argument("--turn-rank", type=int, default=1,
                    help="--pick turn 时取第几名（1 = 换手率最高）")
    ap.add_argument("--turn-rank-mode", default="abs", choices=["abs", "ratio"],
                    help="个股（板块内）排序口径：abs = 20 日均换手率（默认，旧口径）；"
                         "ratio = turn20 × (turn20 / turn120) = turn20^2/turn120，"
                         "即绝对换手率乘以「相对自身半年常态的放大倍数」，降序取前 N")
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
    ap.add_argument("--data-source", default="local", choices=["local", "crsp"],
                    help="行情/换手率/市值/成分股的数据源：local = yfinance 行情 + westock 换手率 + "
                         "快照市值（旧口径）；crsp = 买来的标普 500 标准数据（PIT 成分、真实股本与市值）")
    ap.add_argument("--crsp-scope", default="all", choices=["all", "pool", "metrics"],
                    help="只用 crsp 数据源的哪一部分（做归因实验用）：all = 成分股+换手率+市值+价格；"
                         "pool = 只把成分股换成 PIT；metrics = 只换换手率/市值/价格，池子仍用快照")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    sn = args.sn or args.n
    sm = args.sm or args.m
    # 熊市「不分板块」模式：全市场按换手率取前 N，只在这 N 只里找第一只多头个股
    bear_turn5 = args.regime_ma > 0 and args.bear_pick == "turn5"
    end = pd.Timestamp(args.end)
    start = pd.Timestamp(args.start)
    warm = max(args.n + args.m, sn + sm) * 2 + 40
    # 强化趋势要看 MA200，按「日历日 ≈ 交易日 × 1.6」放预热，否则启动初期整段是 NaN，
    # 会被判成「都不满足趋势」而全程空仓——那是参数不足，不是策略结论。
    if args.trend_mode == "strong":
        trend_w = max([args.trend_above, args.stack_slope]
                      + [int(x) for x in str(args.trend_stack).split(",") if x.strip()]
                      + [int(x) for x in str(args.trend_cross).split(",") if x.strip()])
        warm = max(warm, int(trend_w * 1.6) + 60)
    load_start = (start - pd.DateOffset(days=warm)).strftime("%Y-%m-%d")
    use_crsp = args.data_source == "crsp"
    use_crsp_pool = use_crsp and args.crsp_scope in ("all", "pool")
    use_crsp_metrics = use_crsp and args.crsp_scope in ("all", "metrics")
    if use_crsp and not crsp.available():
        raise SystemExit("CRSP 数据不存在：先跑 python -m scripts.import_crsp_sp500")

    members = load_members()
    sec_of = dict(zip(members["symbol"], members["sector_etf"]))
    mcap_of = dict(zip(members["symbol"], members["mcap"]))
    stocks = list(members["symbol"])
    pit_ev = None
    pit_anchor: set[str] = set()
    if use_crsp_pool:
        # 成分股改用 CRSP 的当期名单：窗口内当过成分的所有票（含后来被剔除、退市的），
        # 这才是「当时能买到」的池子；今天的 503 只快照含 58 只当时还没进指数的票。
        crsp_win = crsp.symbols_in_window(start, end)
        smap = crsp.sector_map(crsp_win)
        sec_of.update(dict(zip(smap["symbol"], smap["sector_etf"])))
        stocks = list(crsp_win)
        n_sic = int((smap["src"] == "sic").sum())
        print(f"--data-source crsp：窗口内成分 {len(stocks)} 只（{n_sic} 只板块归属按 SIC 补齐）")
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

    etf_syms = SECTOR_ETFS + [BENCH]
    end_s = end.strftime("%Y-%m-%d")
    if use_crsp_metrics:
        # ETF 仍用本地行情（CRSP 只覆盖标普 500 成分股），个股用 CRSP 自己的复权价
        def _both(field: str, crsp_field: str) -> pd.DataFrame:
            left = store.load_panel(field=field, symbols=etf_syms, start=load_start, end=end_s)
            right = crsp.panel(crsp_field, stocks, load_start, end_s)
            return pd.concat([left, right], axis=1).sort_index().ffill()

        hi, lo, px = _both("high", "high"), _both("low", "low"), _both("adj_close", "adj_close")
    else:
        syms = etf_syms + stocks
        hi = store.load_panel(field="high", symbols=syms, start=load_start,
                              end=end_s).sort_index().ffill()
        lo = store.load_panel(field="low", symbols=syms, start=load_start,
                              end=end_s).sort_index().ffill()
        px = store.load_panel(field="adj_close", symbols=syms, start=load_start,
                              end=end_s).sort_index().ffill()

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
    # 带上 SPY：熊市兜底买 SPY 时策略收益和对照都用它这一列
    ret_etf = px[SECTOR_ETFS + [BENCH]].pct_change()
    ret_stk = px[stocks].pct_change()

    # 逐期成分股池（--pool-mode mcap 时启用）。默认 snapshot = 用今天的名单回测过去。
    pool_mask = None
    if use_crsp_pool:
        # CRSP 自带成分区间，直接给逐日 PIT 掩码；不需要再按公告回放
        pool_mask = crsp.membership_mask(cal, stocks)
        for d in (cal[0], cal[len(cal) // 2], cal[-1]):
            print(f"  {d.date()} 在池 {int(pool_mask.loc[d].sum())} 只（CRSP PIT）")
    elif args.pool_mode == "mcap":
        pool_mask = mcap_pool_mask(stocks, cal, load_start, end.strftime("%Y-%m-%d"),
                                   args.pool_top, args.pool_shares)
    elif args.pool_mode == "pit":
        print(f"--pool-mode pit：按官方公告重建逐日名单（{len(pit_ev)} 条事件）")
        pool_mask = pit_pool_mask(stocks, cal, pit_ev, pit_anchor)

    # 最小市值过滤。小市值票的换手率分母（股本）小、噪声大，而且这类票天然高换手，
    # 会让「按换手率排序」系统性偏向指数尾部的小票（NCLH 市值 65~100 亿美元、
    # 日换手 5%，长期排在 XLY 第一）。这是口径修正，不是调参。
    mcap_panel = None
    if use_crsp_metrics and (args.min_mcap > 0 or bear_turn5):
        # CRSP 直接给当期市值（DlyCap），不需要反推股本，也没有前视
        mcap = crsp.mcap_panel(stocks, load_start, end.strftime("%Y-%m-%d")).reindex(cal)
        mcap_panel = mcap
        if args.min_mcap > 0:
            ok = (mcap > args.min_mcap * 1e8) | mcap.isna()
            ok = ok.reindex(columns=stocks).fillna(True).astype(bool)
            gone = sorted(s for s in stocks if not bool(ok[s].any()))
            pool_mask = ok if pool_mask is None else (pool_mask & ok)
            print(f"--min-mcap {args.min_mcap:.0f} 亿美元（口径 crsp）："
                  f"全程不达标的 {len(gone)} 只移出候选 "
                  f"({' '.join(gone[:16])}{' ...' if len(gone) > 16 else ''})")
    elif args.min_mcap > 0 or bear_turn5:
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
        thr = args.min_mcap * 1e8
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
        mcap_panel = mcap        # 熊市 turn5 复核市值时复用同一份面板
        if args.min_mcap > 0:
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
    turn_rate_120 = None      # 120 日均换手率：--turn-rank-mode ratio 用的「自身常态」
    turn_score = None         # 实际参与排序的面板（abs = turn20；ratio = turn20²/turn120）
    dvol = None
    dvol_cv = None
    if (args.pick in ("turn", "dvol", "dvolcv", "dvolcvh", "cvdown", "turnma")
            or bear_turn5):
        vol = (crsp.panel("volume", stocks, load_start, end.strftime("%Y-%m-%d")).sort_index()
               if use_crsp_metrics else
               store.load_panel(field="volume", symbols=stocks, start=load_start,
                                end=end.strftime("%Y-%m-%d")).sort_index())
        if args.pick in ("turn", "turnma") or bear_turn5:
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
                if args.turn_rank_mode == "ratio":
                    turn_rate_120 = (vol[cols].rolling(120, min_periods=60).mean()
                                     .div(shares[cols], axis=1).reindex(cal).ffill())
                print(f"换手率面板完成 {turn_rate.shape}（旧口径：股本由当前市值/价格推算，"
                      f"共 {len(cols)} 只）")
            else:
                # 数据源口径：腾讯自选股日 K 的换手率字段。注意它对美股是「成交量/总股本」
                # 口径（实测 WMT/COIN/RDDT 全部对得上总股本），保留作对照，不是流通股本。
                if use_crsp_metrics:
                    # CRSP 口径：成交量 ÷ 当日总股本，逐日真实值
                    panel = crsp.turnover_panel(stocks, load_start, end.strftime("%Y-%m-%d"))
                else:
                    path = (ROOT / "data" / "curated" / "fundamentals"
                            / "turnover_daily.parquet")
                    if not path.exists():
                        raise SystemExit(f"换手率面板不存在（应由 data 分支提供）：{path}")
                    tv = pd.read_parquet(path)
                    panel = tv.pivot(index="date", columns="symbol",
                                     values="turnover").sort_index()
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
                if args.turn_rank_mode == "ratio":
                    # 120 日的「常态」基准：同一个尾盘折算口径（当日那一项乘系数），
                    # 120 天窗口里当日只占 1/120，实际影响 < 0.5%，但口径统一更好解释。
                    cnt120 = raw.notna().rolling(120, min_periods=1).sum()
                    s120 = raw.rolling(120, min_periods=1).sum()
                    if args.turn_tail > 0:
                        turn_rate_120 = ((s120 - raw + args.turn_tail * raw)
                                         / cnt120).where(cnt120 >= 60)
                    else:
                        turn_rate_120 = raw.rolling(120, min_periods=60).mean()
                    print(f"120 日均换手率面板完成 {turn_rate_120.shape}（自身常态基准）")
                src_name = "CRSP 成交量/总股本" if use_crsp_metrics else "westock 日 K 换手率"
                print(f"换手率面板完成 {turn_rate.shape}（{src_name}，20 日均值，"
                      f"共 {len(cols)} 只；无数据 {len(missing)} 只）")
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
            if args.turn_rank_mode == "ratio":
                # 排序分 = 绝对换手率 × 相对自身常态的放大倍数
                #        = turn20 × (turn20 / turn120) = turn20² / turn120
                # 分子要求「本身就够活跃」，倍数要求「比它自己过去半年更活跃」：
                # 常年高换手的票分母大、倍数小，会被压低；一次性脉冲把 120 日均值拉高后，
                # 倍数也会回落，避免把「已经放完量」的票继续排在前面。
                if turn_rate_120 is None:
                    raise SystemExit("--turn-rank-mode ratio 缺少 120 日均换手率面板")
                turn_score = turn_rate ** 2 / turn_rate_120.where(turn_rate_120 > 0)
                r = (turn_rate / turn_rate_120).stack()
                print(f"个股排序：turn20^2/turn120（turn20 均值 {turn_rate.stack().mean():.2%}，"
                      f"倍数中位数 {r.median():.2f}x，P90 {r.quantile(0.9):.2f}x）")
            else:
                turn_score = turn_rate
        elif args.pick in ("dvol", "dvolcv", "dvolcvh", "cvdown"):
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

    # 强化趋势（--trend-mode strong）专用面板。均线斜率用「与 N 个交易日前比较」，
    # 而不是拟合回归斜率：口径直观、不受窗口内跳空影响，也不会引入未来的点。
    # 长期均线（MA200）用完整窗口 min_periods=w，不足 200 天一律 NaN——不做「半截均线」判定。
    strong = args.trend_mode == "strong"
    slope_panels, above_panel, cross_panels, adx_p = {}, None, None, None
    cross_group = [int(x) for x in str(args.trend_cross).split(",") if x.strip()]
    # 斜率闸只查 --slope-mas 列出的均线（默认 20,60）。MA5 的斜率一个交易日就能翻负，
    # 会把「刚回踩、排列仍完好」的票全部否决——实测这是牛市空仓的主要来源。
    slope_group = [int(x) for x in str(args.slope_mas).split(",") if str(x).strip()]
    if strong:
        if args.stack_slope > 0:
            for w in slope_group:
                base = stack_panels.get(w)
                if base is None:
                    base = px[stocks].rolling(w, min_periods=max(3, w // 2)).mean()
                slope_panels[w] = base - base.shift(args.stack_slope)
        if args.trend_above > 0:
            above_panel = px[stocks].rolling(args.trend_above,
                                             min_periods=args.trend_above).mean()
        if len(cross_group) == 2:
            cross_panels = [px[stocks].rolling(w, min_periods=w).mean() for w in cross_group]
        if args.adx_min > 0:
            adx_p = adx_panel(hi[stocks], lo[stocks], px[stocks], args.adx_len)
        slope_txt = (f'，MA{"/".join(str(w) for w in slope_group)} 的 '
                     f'{args.stack_slope} 日斜率为正'
                     if args.stack_slope > 0 and slope_group else '')
        print(f"强化趋势过滤：排列 {' > '.join(f'MA{w}' for w in stack_group)}"
              f"{slope_txt}"
              f"{f'，收盘 > MA{args.trend_above}' if args.trend_above > 0 else ''}"
              f"{f'，MA{cross_group[0]} > MA{cross_group[1]}' if cross_panels else ''}"
              f"{f'，ADX({args.adx_len}) > {args.adx_min:g}' if adx_p is not None else ''}")

    # 牛熊判定：SPY 收盘与 120 日（可配）均线比较，线上 = 牛、线下 = 熊。
    # --regime-window > 1 时改成「过去 N 日里跌破均线的天数占比」：均线附近单日穿越
    # 不再直接翻转牛熊（2026-03 就是单日跌破被判熊，把后面 +54% 的那期让给了 ETF）。
    spy_ma, bear_ratio = None, None
    if args.regime_ma > 0:
        spy_ma = px[BENCH].rolling(args.regime_ma,
                                   min_periods=max(20, args.regime_ma // 2)).mean()
        below = (px[BENCH] < spy_ma).astype(float).where(spy_ma.notna())
        win = max(1, args.regime_window)
        bear_ratio = (below if win == 1
                      else below.rolling(win, min_periods=win).mean())
        print(f"牛熊判定：SPY vs MA{args.regime_ma}，"
              + (f"过去 {win} 日跌破占比 ≥ {args.regime_bear_ratio:.0%} 判熊"
                 if win > 1 else "当日跌破即判熊（旧口径）"))
        if bear_turn5:
            print(f"熊市选股：不分板块，全市场市值 > {args.bear_turn_mcap:g} 亿美元的票按换手率"
                  f"降序取前 {args.bear_turn_top} 只，买其中第一只满足多头趋势的")
        if args.bear_refill:
            print("熊市双榜补选：当期交集为空/动量不达标时，从下一个交易日起逐日重算双榜，"
                  "命中即按当日收盘买入该板块 ETF；到下一个调仓日仍未命中则空仓")
        if args.bear_lookback > 0:
            print(f"熊市双榜回看：信号日交集为空时回看过去 {args.bear_lookback} 个交易日，"
                  "取最近一次双榜同时满足的板块（仍按信号日收盘买入）")

    # 熊市板块闸门：只留站上 MA(N) 的板块。熊市里板块普跌，但总有一两个在避险抱团
    # （2022 的 XLE、2023 的 XLK），要求它们站在中期均线上方，才让它们进 RSRS 排序。
    # 用完整窗口 min_periods=N，不足 N 天一律 NaN（不许「半截均线」放行）。
    sec_ma = (px[SECTOR_ETFS].rolling(args.bear_sector_ma, min_periods=args.bear_sector_ma).mean()
              if args.bear_sector_ma > 0 else None)

    # 熊市板块排序可换成加权斜率：RSRS 是 high~low 回归的 z-score，衡量「日内强弱关系」，
    # 在单边下跌里会频繁换手；斜率直接看价格趋势本身，更贴合「哪个板块真的在涨」。
    etf_slope = (weighted_slope_panel(px[SECTOR_ETFS], args.bear_slope_window)
                 if args.regime_ma > 0 and args.bear_rank in ("slope", "both") else None)
    if etf_slope is not None:
        print(f"熊市板块排序：加权斜率（窗口 {args.bear_slope_window} 日，%/交易日）"
              + (f"，且必须与 RSRS 前 {args.bear_intersect_top} 名取交集"
                 if args.bear_rank == "both" else ""))

    def is_bull(dt: pd.Timestamp) -> bool:
        if spy_ma is None:
            return True
        r = bear_ratio.at[dt] if dt in bear_ratio.index else np.nan
        if not np.isfinite(r):
            # 样本不足（预热没铺满均线+统计窗口）：按牛市走常规链路，不当成熊市去乱买 ETF
            return True
        return bool(r < args.regime_bear_ratio)

    def pick_bear_etf(sec_pool: pd.Series) -> str:
        """熊市里该买哪只板块 ETF：RSRS 最强、且（若设了下限）动量必须过关。

        返回空串 = 当期空仓。熊市里最强板块的 RSRS 都是负的，说明连抱团的都没有，
        这时候买 ETF 只是跟着大盘阴跌，不如空仓等信号。
        """
        if sec_pool.empty:
            return ""
        top = str(sec_pool.index[0])
        if args.bear_min_score is not None and not (float(sec_pool.iloc[0]) > args.bear_min_score):
            return ""
        return top

    def double_scores(dt_: pd.Timestamp) -> pd.Series:
        """某一天的「RSRS 前 N ∩ 斜率前 N」序列（按 RSRS 降序），并套用 --bear-sector-ma 闸。

        与信号日那段逻辑同口径，抽出来是为了给 --bear-refill 逐日重算用：交易日 dt_
        的 RSRS、斜率、均线闸全部取当日值，不掺未来数据。
        """
        if dt_ not in etf_rsrs.index:
            return pd.Series(dtype=float)
        zs = etf_rsrs.loc[dt_].dropna().sort_values(ascending=False)
        if zs.empty or etf_slope is None or dt_ not in etf_slope.index:
            return zs
        sl = etf_slope.loc[dt_].reindex(zs.index).dropna()
        if sl.empty:
            return pd.Series(dtype=float)
        top_n = max(1, args.bear_intersect_top)
        rsrs_top = set(zs.index[:top_n])
        slope_top = set(sl.sort_values(ascending=False).index[:top_n])
        out = zs[[s_ for s_ in zs.index if s_ in rsrs_top and s_ in slope_top]]
        if sec_ma is not None:
            out = out[[s_ for s_ in out.index
                       if s_ in sec_ma.columns and np.isfinite(sec_ma.at[dt_, s_])
                       and np.isfinite(px.at[dt_, s_]) and px.at[dt_, s_] > sec_ma.at[dt_, s_]]]
        return out

    def is_down(sym: str, dt: pd.Timestamp, desc: bool | None = None) -> bool:
        if sym not in ma_t.columns:
            return False
        a = px.at[dt, sym]
        if not np.isfinite(a):
            return False
        if args.trend_mode == "strong":
            # 强化趋势：排列、斜率、长期均线、趋势强度四道，任何一道不过就不买。
            # 1) 均线多头排列：trend-stack 给定顺序严格递减（MA5 > MA20 > MA60）
            prev = None
            for w in stack_group:
                m = stack_panels[w].at[dt, sym]
                if not np.isfinite(m):
                    return False
                if prev is not None and not (prev > m):
                    return False
                prev = m
            # 2) --slope-mas 列出的均线斜率为正（当前值高于 stack-slope 个交易日前）。
            # 默认只查 MA20/MA60：MA5 斜率噪声太大，一票否决过多。
            for w in slope_group:
                dlt = slope_panels[w].at[dt, sym] if args.stack_slope > 0 else 1.0
                if not np.isfinite(dlt) or dlt <= 0:
                    return False
            # 3) 收盘价站上长期均线
            if above_panel is not None:
                m_long = above_panel.at[dt, sym]
                if not np.isfinite(m_long) or not (a > m_long):
                    return False
            # 4) 中期均线在长期均线之上（MA50 > MA200）
            if cross_panels is not None:
                fast_m, slow_m = cross_panels[0].at[dt, sym], cross_panels[1].at[dt, sym]
                if not (np.isfinite(fast_m) and np.isfinite(slow_m)) or not (fast_m > slow_m):
                    return False
            # 5) 趋势强度：ADX 高于阈值（横盘/无趋势时不进场）
            if adx_p is not None:
                v = adx_p.at[dt, sym]
                if not np.isfinite(v) or not (v > args.adx_min):
                    return False
            return True
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
        sec_all = etf_rsrs.loc[sig].dropna().sort_values(ascending=False)
        if sec_all.empty:
            continue
        bull = is_bull(sig)
        bear = args.regime_ma > 0 and not bull
        nxt = me[k + 1]          # 下一个调仓日：既是持有区间的右端点，也是牛市补选的截止日

        # 熊市板块排序：默认沿用 RSRS；--bear-rank slope 时改用加权斜率（值 = %/交易日）；
        # both = 取「RSRS 前 N ∩ 斜率前 N」的交集，排序仍按 RSRS。
        # 牛市始终用 RSRS，不受这个开关影响。
        sec_score = sec_all
        sec_ref = sec_all         # 板块 RSRS 展示/参照系：默认信号日，回看命中时换成命中日
        slope_now = (etf_slope.loc[sig].reindex(sec_all.index).dropna()
                     if etf_slope is not None else None)
        if bear and slope_now is not None and args.bear_rank == "slope":
            sec_score = slope_now.sort_values(ascending=False)
        elif bear and slope_now is not None and args.bear_rank == "both":
            # 注意：不能用 k，那是外层调仓循环的下标（会被覆盖掉，导致 nxt 取错、
            # 熊市期 seg 为空被跳过——2022 年整段熊市消失就是这么来的）
            top_n = max(1, args.bear_intersect_top)
            rsrs_top = set(sec_all.index[:top_n])
            slope_top = set(slope_now.sort_values(ascending=False).index[:top_n])
            # 交集内按 RSRS 排：斜率负责「真的在涨」，RSRS 负责「不是一次性脉冲」
            sec_score = sec_all[[s_ for s_ in sec_all.index
                                 if s_ in rsrs_top and s_ in slope_top]]
            if sec_score.empty and args.bear_lookback > 0:
                # 月末那一天恰好交集为空时的补救：回看过去 N 个交易日，取最近一次「双榜都
                # 满足」的那天及其排名（只用滞后信息，不含未来），仍然为空才算真的没信号。
                # 与 --bear-refill 的区别：不提前买入，仍然在信号日收盘买、持有整月。
                i0 = int(cal.get_indexer([sig])[0])
                for back in range(1, min(args.bear_lookback, i0) + 1):
                    dt_b = cal[i0 - back]
                    sc_b = double_scores(dt_b)
                    if sc_b.empty:
                        continue
                    sec_score, sec_ref = sc_b, etf_rsrs.loc[dt_b].dropna()
                    print(f"  熊市回看：{sig.date()} 双榜为空 → 用 {dt_b.date()} 的 "
                          f"{sc_b.index[0]}（回看 {back} 个交易日）", flush=True)
                    break

        # 熊市板块闸门：只让站上 MA(--bear-sector-ma) 的板块进排序。
        # 全部跌破时就当期不持仓——熊市里没有抱团板块，硬买只是接落刀。
        if bear and sec_ma is not None:
            keep = [s_ for s_ in sec_score.index
                    if s_ in sec_ma.columns and np.isfinite(sec_ma.at[sig, s_])
                    and np.isfinite(px.at[sig, s_]) and px.at[sig, s_] > sec_ma.at[sig, s_]]
            sec_score = sec_score[keep]
        no_sector = sec_score.empty
        sector = str(sec_all.index[0]) if no_sector else str(sec_score.index[0])

        pool = [s for s in stocks if sec_of.get(s) == sector and s in stock_rsrs.columns
                and in_pool(s, sig)]
        ss = stock_rsrs.loc[sig, pool].dropna().sort_values(ascending=False) if pool else pd.Series(dtype=float)
        pick, buy_etf = "", False
        tried, sec_rank = 0, 0
        buy_dt, rsrs_ref = sig, sec_ref   # 实际买入日（补选会往后推）与 RSRS 参照系

        # 熊市双榜补选：当期「RSRS 前 N ∩ 斜率前 N」为空（或动量不达标）时，从下一个交易日起
        # 逐日重算两榜，第一天同时满足就按当日收盘买入该板块 ETF；到下一个调仓日仍没出现则空仓。
        # 语义与牛市 --bull-refill 一致：不追已经走完的行情，只在信号真正出现的那天进场。
        if (bear and args.bear_refill and args.bear_rank == "both" and not bear_turn5
                and not pick_bear_etf(sec_score)):
            n_try = 0
            for dt_try in cal[(cal > sig) & (cal < nxt)]:
                n_try += 1
                sym_try = pick_bear_etf(double_scores(dt_try))
                if not sym_try:
                    continue
                sector, pick, buy_etf = sym_try, f"ETF:{sym_try}", True
                buy_dt, rsrs_ref, sec_rank = dt_try, etf_rsrs.loc[dt_try].dropna(), 1
                tried = n_try
                print(f"  熊市补选：{sig.date()} 双榜为空 → {dt_try.date()} 命中 {sym_try}"
                      f"（扫了 {n_try} 个交易日）", flush=True)
                break
            if not pick:
                print(f"  熊市补选：{sig.date()} ~ {nxt.date()} 双榜始终为空 → 当期空仓",
                      flush=True)

        if pick:
            pass          # 熊市补选已买入，不再走后面的选股链路
        elif bear and bear_turn5:
            # 熊市不分板块：在全市场（本地行情池，已过 --min-mcap）里按当日市值 > 门槛筛一遍，
            # 再按 20 日均换手率降序取前 N 只，买其中第一只满足「多头」趋势的；一只都没有就
            # 交给后面的熊市兜底（--bear-etf / --bear-spy）。判定口径与牛市 turnma 一致，
            # 只是候选池从「最强板块的成分股」换成「全市场」。
            cand_t = [s for s in stocks
                      if s in turn_rate.columns and s in px.columns and in_pool(s, sig)
                      and mcap_above(mcap_panel, s, sig, args.bear_turn_mcap * 1e8)]
            tt = (turn_rate.loc[sig, cand_t].dropna().sort_values(ascending=False)
                  .head(max(1, args.bear_turn_top)) if cand_t else pd.Series(dtype=float))
            for s in tt.index:
                tried += 1
                if is_down(str(s), sig, True):
                    pick = str(s)
                    sector = sec_of.get(pick, BENCH)
                    if sector not in SECTOR_ETFS:
                        sector = BENCH
                    sec_rank = 1
                    break
            print(f"  熊市 turn5 {sig.date()}：TOP{len(tt)} "
                  f"{' '.join(f'{s}({v:.2%})' for s, v in tt.items())} "
                  f"→ {pick or '空仓（交给熊市兜底）'}", flush=True)
        elif no_sector:
            pass          # 熊市里 11 个板块全在均线下方：当期空仓
        elif bear and args.bear_etf_only:
            # 熊市完全不碰个股：直接持有最强板块的 ETF（动量不达标则空仓）
            etf_sym = pick_bear_etf(sec_score)
            if etf_sym:
                sector = etf_sym
                pick, buy_etf, sec_rank = f"ETF:{etf_sym}", True, 1
        elif args.pick in ("mcap", "mmin"):
            ranked = sorted([s for s in pool if np.isfinite(mcap_of.get(s, np.nan))],
                            key=lambda s: float(mcap_of[s]), reverse=(args.pick == "mcap"))
            pick = ranked[0] if ranked else ""
        elif args.pick == "turn":
            rp = turn_score if turn_score is not None else turn_rate
            cand = [s for s in pool if s in rp.columns]
            t = rp.loc[sig, cand].dropna().sort_values(ascending=False) \
                if cand else pd.Series(dtype=float)
            ri = min(max(1, args.turn_rank), len(t)) - 1
            pick = str(t.index[ri]) if len(t) else ""
        elif args.pick == "turnma":
            # 换手率降序 + 均线排列：按板块 RSRS 顺序下探，板块内按换手率取前 N 只，
            # 买第一只满足排列方向的（默认 desc = 多头排列，即上升趋势）。
            # 排序口径见 --turn-rank-mode：abs = turn20（旧口径）；
            # ratio = turn20²/turn120（绝对换手率 × 相对自身半年常态的放大倍数）。
            # --bull-refill：牛市当期一只都没过时，从下一个交易日起逐日重挑（板块 RSRS、
            # 换手率、趋势闸全部换成当日数据），买到为止；到下一个调仓日仍找不到才空仓。
            use_desc = (args.stack_order == "desc") if args.regime_ma == 0 else bull
            rp = turn_score if turn_score is not None else turn_rate
            pick, tried, sec_rank = "", 0, 0
            retry = (list(cal[(cal > sig) & (cal < nxt)])
                     if (args.bull_refill and bull) else [])
            for dt_try in [sig] + retry:
                if dt_try != sig:
                    if dt_try not in etf_rsrs.index:
                        continue
                    zs = etf_rsrs.loc[dt_try].dropna().sort_values(ascending=False)
                    if zs.empty:
                        continue
                    sec_pool = zs
                else:
                    sec_pool = sec_score
                for rank_s, sec_k in enumerate(sec_pool.index[: args.turnma_sectors], 1):
                    pool_k = [s for s in stocks if sec_of.get(s) == sec_k
                              and s in rp.columns and in_pool(s, dt_try)]
                    if not pool_k:
                        continue
                    tt = (rp.loc[dt_try, pool_k].dropna()
                          .sort_values(ascending=False).head(args.turnma_top))
                    for s in tt.index:
                        tried += 1
                        if is_down(str(s), dt_try, use_desc):
                            pick, sector, sec_rank = str(s), str(sec_k), rank_s
                            buy_dt = dt_try
                            rsrs_ref = zs if dt_try != sig else sec_all
                            break
                    if pick:
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

        # 熊市里趋势选股一个都没过：买最强板块的 ETF。逻辑是「资金抱团的那个板块本身」
        # 在熊市里相对抗跌，空仓等于放弃这块相对收益。牛市不兜底，选不到就空仓。
        if not pick and bear and args.bear_etf and not no_sector:
            etf_sym = pick_bear_etf(sec_score)
            if etf_sym:
                sector, pick, buy_etf = etf_sym, f"ETF:{etf_sym}", True
                sec_rank = sec_rank or 1

        # 熊市双条件（或动量门槛）全不达标：与其空仓一个月，不如买 SPY 跟住大盘。
        # 双条件太严时交集经常为空，空仓在 2025-03/2026-03 这类「指数已经修复」的窗口
        # 会白丢整月涨幅；代价是真正单边下跌的月份要跟着 SPY 一起吃跌幅。
        if not pick and bear and args.bear_spy:
            sector, pick, buy_etf = BENCH, f"ETF:{BENCH}", True
            sec_rank = sec_rank or 1

        seg = cal[(cal > buy_dt) & (cal <= nxt) & (cal >= start)]
        if len(seg) == 0:
            prev = pick
            continue

        # 补选的等待期：旧仓已在信号日卖出，这段是空仓，净值平推以保持曲线连续
        if buy_dt != sig:
            for dt in cal[(cal > sig) & (cal <= buy_dt) & (cal >= start)]:
                nav.loc[dt] = cur_s
                nav_etf.loc[dt] = cur_e
            if pick:
                print(f"  {'牛市' if bull else '熊市'}补选：{sig.date()} 无票 → "
                      f"{buy_dt.date()} 买入 {pick}（{sector}）", flush=True)

        turnover = 0.0 if pick == prev else (2.0 if prev else 1.0)
        # 买 ETF 时买卖价用板块 ETF 的列，个股时用个股列；补选时用实际买入日收盘
        base_px = float(px.at[buy_dt, sector if buy_etf else pick]) if pick else np.nan
        stopped, stop_dt = False, None
        for j, dt in enumerate(seg):
            if j == 0:
                cur_s *= (1.0 - args.cost * turnover)
                cur_e *= (1.0 - args.cost * turnover)
            if pick and not stopped:
                # 买 ETF 时策略收益即该板块 ETF 收益（与对照同源，选股差为 0）
                a = ret_etf.loc[dt, sector] if buy_etf else ret_stk.loc[dt, pick]
                cur_s *= (1.0 + (0.0 if pd.isna(a) else a))
                # 收盘价比买入价跌破阈值 → 当日收盘割肉，当期剩余时间空仓
                if args.stop_loss > 0 and not buy_etf and np.isfinite(base_px) and base_px > 0:
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
            "板块RSRS": (round(float(rsrs_ref[sector]), 2) if sector in rsrs_ref.index
                        else (np.nan if sector == BENCH else round(float(rsrs_ref.iloc[0]), 2))),
            "板块斜率%": (round(float(etf_slope.at[buy_dt, sector]) * 100, 3)
                       if etf_slope is not None and sector in etf_slope.columns
                       and np.isfinite(etf_slope.at[buy_dt, sector]) else np.nan),
            "斜率序": (int(etf_slope.loc[buy_dt].rank(ascending=False)[sector])
                    if etf_slope is not None and sector in etf_slope.columns
                    and np.isfinite(etf_slope.at[buy_dt, sector]) else np.nan),
            "补选日": (buy_dt.date() if buy_dt != sig else None),
            "板块RSRS序": sec_rank or 1,
            "牛市": int(bull),
            "个股": pick,
            "候选尝试": tried,
            "个股RSRS": (round(float(stock_rsrs.at[buy_dt, pick]), 2)
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
    if args.regime_ma > 0:
        bear_hdf = hdf[hdf["牛市"] == 0]
        n_etf = int(bear_hdf["个股"].astype(str).str.startswith("ETF:").sum())
        n_cash = int((bear_hdf["个股"].astype(str).str.strip() == "").sum())
        print(f"熊市 {len(bear_hdf)} 期：买 ETF {n_etf} 期 / 空仓 {n_cash} 期 / "
              f"个股 {len(bear_hdf) - n_etf - n_cash} 期")

    tag = f"_{args.tag}" if args.tag else ""
    ftag = "" if args.freq == "M" else f"_{args.freq}"
    ptag = "" if args.pick == "rsrs" else f"_{args.pick}"
    if use_crsp:
        ptag += "_crsp" + ("" if args.crsp_scope == "all" else f"_{args.crsp_scope}")
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
        elif args.trend_mode == "strong":
            # 目录名带上全部过滤条件，避免不同口径互相覆盖
            ptag += "_" + "-".join(x for x in str(args.trend_stack).split(",") if x.strip())
            if args.stack_slope > 0:
                ptag += f"_sl{args.stack_slope}"
            slope_tag = [x for x in str(args.slope_mas).split(",") if x.strip()]
            if args.stack_slope > 0 and slope_tag != ["5", "20", "60"]:
                ptag += "_slma" + "-".join(slope_tag)
            if args.trend_above > 0:
                ptag += f"_a{args.trend_above}"
            cross_tag = [x for x in str(args.trend_cross).split(",") if x.strip()]
            if len(cross_tag) == 2:
                ptag += "_x" + "-".join(cross_tag)
            if args.adx_min > 0:
                ptag += f"_adx{args.adx_min:g}"
    if args.pick in ("turn", "turnma"):
        ptag += {"ws": "_turnws", "snapshot": "_shsnap"}[args.turn_shares]
    if args.turn_rank_mode == "ratio":
        ptag += "_tratio"
    if args.bull_refill:
        ptag += "_bref"
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
        if args.regime_window > 1:
            ptag += f"_rw{args.regime_window}"
            if args.regime_bear_ratio != 0.5:
                ptag += f"_br{args.regime_bear_ratio:g}"
    if args.bear_sector_ma > 0:
        ptag += f"_bsec{args.bear_sector_ma}"
    if args.bear_etf:
        ptag += "_betf"
    if args.bear_etf_only:
        ptag += "_betfonly"
    if args.bear_spy:
        ptag += "_bspy"
    if args.bear_min_score is not None:
        ptag += f"_bmin{args.bear_min_score:g}"
    if args.bear_rank == "slope":
        ptag += f"_brankSlope{args.bear_slope_window}"
    elif args.bear_rank == "both":
        ptag += f"_brankBoth{args.bear_intersect_top}x{args.bear_slope_window}"
    if bear_turn5:
        ptag += f"_bT{args.bear_turn_top}" + ("" if args.bear_turn_mcap == 500
                                             else f"mc{args.bear_turn_mcap:g}")
    if args.bear_refill:
        ptag += "_bbref"
    if args.bear_lookback > 0:
        ptag += f"_blb{args.bear_lookback}"
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
