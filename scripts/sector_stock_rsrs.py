"""板块 → 个股 双层 RSRS 回测（唯一数据源：`data/curated/sp500_pool`）。

数据口径
--------
* 个股一律按 **security_id（CRSP PERMNO）** 取数：价格、成交量、市值、换手率、
  成分区间、板块归属全部来自 `quant.data.pool`。ticker 只是某个时点的别名，
  在这里只用于输出展示，不参与选股、排序、持仓或去重。
* 成分股是 point-in-time 的：`pool.membership_mask()` 逐日给掩码，窗口内当过成分股的
  票都在候选池里（含后来被剔除、退市的），不用今天的名单回测过去。
* 复权价用池层算好的 `adj_close/adj_high/adj_low`（CRSP 累计因子做除数，含拆股/合股/分立），
  `close` 是原始价、只在需要展示时用。
* 池层自带 `src='external'` 的预热段（外部源按重叠区间缩放对齐到 CRSP 价格空间），
  所以新纳入指数的票在成分期之前也有 MA200 可算，不需要再从本地行情补前导。
* 板块 ETF 与 SPY 不在标普 500 成分股池里，只有这一处例外：从本地 data 分支读
  （`store.load_panel`）。除此之外没有第二套个股数据源。

已知边界
--------
* 池的行情到 CRSP 数据边界为止（`pool.last_date()`）；`--end` 超过它会被截断并提示。
  以前那套「2026 年用本地行情按截止日比值接续」属于第二份数据源，已删除。
* 退市收益在 `pool.exits()` 里单独存（如 FRC -90.5%）。本回测目前仍按「末行价格消失 →
  当期剩余天数收益记 0」处理，与历史口径保持一致；把它叠加进净值会改动收益，属于口径变更，
  需要单独评估后再做。

策略
----
第一层：11 个板块 ETF 的 RSRS（N/M 与 `scripts.sector_rsrs` 同源）。
第二层：在该板块的成分股里按 `--pick` 规则选 1 只（默认 RSRS 最高；定稿口径是
`turn20²/turn120` 降序 + strong 五闸逐块下探）。
牛熊：SPY 与 MA(`--regime-ma`) 比较；熊市按 `--bear-*` 分支买板块 ETF 或空仓。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import pool, store
from scripts.sector_rsrs import anchors, perf

ROOT = Path(__file__).resolve().parents[1]
BENCH = "SPY"
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"]


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def rsrs_panel_fast(high: pd.DataFrame, low: pd.DataFrame, n: int, m: int,
                    spec: str = "strict", r2_th: float = 0.5,
                    slope_lim: float = 10.0) -> pd.DataFrame:
    """与 sector_rsrs.rsrs_panel 等价的向量化实现（已逐值比对）。

    逐列循环的版本在 500 只股票上要几分钟，这里改成 rolling 均值算协方差：
    beta = (E[LH] − E[L]E[H]) / (E[L²] − E[L]²)，r2 = 1 − 残差方差 / (E[H²] − E[H]²)。
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


def dmi_panels(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
               n: int = 14) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Wilder DMI，返回 ``(ADX, +DI, -DI)``。

    公式链：TR / ±DM → RMA 平滑 → ±DI → DX → 再平滑一次得 ADX。
    平滑用 ``ewm(alpha=1/n, adjust=False)``，等价于 Wilder 的递归均值；``min_periods=n``
    保证前 n-1 根是 NaN，不给「刚上市几天就判强趋势」的机会。

    ADX 只量趋势强度、不分方向：一路阴跌也能把 ADX 抬到 40 以上。要判「多头趋势」，
    得再看 +DI 与 -DI 谁在上面（+DI > -DI 才说明这波趋势是买方主导）。
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
    return smooth(dx), pdi, mdi


def adx_panel(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
              n: int = 14) -> pd.DataFrame:
    """只要 ADX（保留旧签名，其它调用方无需改动）。"""
    return dmi_panels(high, low, close, n)[0]


def weighted_slope_panel(px: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """加权线性回归斜率，按价格归一化成「每日涨幅」（≈ %/交易日）。

    为什么不用等权回归：等权斜率对窗口首尾同样敏感，一个旧的跳空会拖住整段判断。
    这里给近期更大的权重（w = 1..n，最新一天权重最大），得到的斜率更接近「现在在不在涨」。
    为什么除以均价：XLK 两百多块、XLF 五十块，绝对斜率不可比；归一化后才是「每天涨百分之几」。
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


def parse_slope_spec(spec: str, default_win: int) -> dict[int, int]:
    """解析「均线:窗口」列表 → {均线: 窗口}（保持书写顺序）。

    支持每根均线用不同窗口，因为「MA20 的 10 日斜率」和「MA60 的 5 日斜率」是两个量：
    短线均线要用更长的观察窗才能滤掉噪声，长线均线本来变化就慢，5 日足够。
    写法：
        "20:10,60:5" → {20: 10, 60: 5}
        "20,60"      → 两者都用 default_win（= --stack-slope，兼容旧命令）
        ""           → {}
    """
    out: dict[int, int] = {}
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            w, win = tok.split(":", 1)
            out[int(w)] = int(win)
        else:
            out[int(tok)] = int(default_win)
    return out


def mcap_above(mcap: pd.DataFrame | None, sid: str, dt: pd.Timestamp, thr: float) -> bool:
    """当日市值是否严格大于 thr（单位：美元）。面板缺失 / 不设门槛 / 取不到市值时放行。"""
    if mcap is None or thr <= 0:
        return True
    if sid not in mcap.columns or dt not in mcap.index:
        return True
    v = float(mcap.at[dt, sid])
    return bool(np.isfinite(v) and v > thr)


# ---------------------------------------------------------------------------
# 取数：池层（个股）+ 本地（板块/基准 ETF，池里没有）
# ---------------------------------------------------------------------------
def load_etf(sectors: list[str], load_start: str, end_s: str) -> dict[str, pd.DataFrame]:
    """板块 ETF + SPY 的复权价/高/低。池层只覆盖标普 500 成分股，ETF 只能从这里取。"""
    syms = list(sectors) + [BENCH]
    out = {}
    for field in ("adj_close", "high", "low"):
        p = store.load_panel(field=field, symbols=syms, start=load_start, end=end_s)
        if p.empty:
            raise SystemExit(
                f"本地没有 ETF 行情（{field}）：回测需要 {len(syms)} 只板块/基准 ETF。"
                "池层只含标普 500 成分股，ETF 不在池内。")
        out[field] = p.sort_index().ffill().reindex(columns=syms)
    return out


def load_pool(start: pd.Timestamp, end: pd.Timestamp, warm_days: int,
              exclude: set[str], sectors: list[str]) -> dict:
    """按 security_id 取个股面板、成分掩码、市值、换手率、板块归属。"""
    load_start = (start - pd.DateOffset(days=warm_days)).strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")

    etf = load_etf(sectors, load_start, end_s)
    cal = etf["adj_close"].index

    u = pool.universe().set_index("security_id")
    ids = [i for i in pool.ids_in_window(start, end)
           if i in u.index and str(u.at[i, "sector_etf"]) in sectors]
    if exclude:
        ids = [i for i in ids if i not in exclude]
    if not ids:
        raise SystemExit("窗口内没有任何可用成分股")

    px_s = pool.panel("adj_close", ids, load_start, end_s).reindex(cal)
    hi_s = pool.panel("adj_high", ids, load_start, end_s).reindex(cal)
    lo_s = pool.panel("adj_low", ids, load_start, end_s).reindex(cal)
    mcap = pool.panel("mcap", ids, load_start, end_s).reindex(cal)
    turn = pool.panel("turnover", ids, load_start, end_s).reindex(cal)

    px = pd.concat([etf["adj_close"], px_s], axis=1).sort_index().ffill()
    hi = pd.concat([etf["high"], hi_s], axis=1).sort_index().ffill()
    lo = pd.concat([etf["low"], lo_s], axis=1).sort_index().ffill()

    # 区间内完全没有行情的票剔掉，否则 px[stocks] 会因列缺失报 KeyError
    have = [i for i in ids if i in px.columns and i in hi.columns and i in lo.columns]
    if len(have) < len(ids):
        gone = sorted(set(ids) - set(have))
        tick = pool.display_tickers()
        print(f"区间内无行情的成分股 {len(gone)} 只已剔除："
              f"{' '.join(str(tick.get(g, g)) for g in gone)}")
    ids = have

    mask = pool.membership_mask(cal, ids)
    sec_of = {i: str(u.at[i, "sector_etf"]) for i in ids}
    sec_src = {i: str(u.at[i, "sector_src"]) for i in ids}
    tick_of = {i: str(pool.display_tickers().get(i, i)) for i in ids}

    return {"ids": ids, "cal": cal, "px": px, "hi": hi, "lo": lo,
            "etf_px": etf["adj_close"], "mask": mask, "mcap": mcap, "turn": turn,
            "sec_of": sec_of, "sec_src": sec_src, "tick_of": tick_of,
            "load_start": load_start, "end": end}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="板块→个股 双层 RSRS（数据源：sp500_pool，security_id 主键）")
    ap.add_argument("--n", type=int, default=20, help="板块层回归窗口")
    ap.add_argument("--m", type=int, default=40, help="板块层标准化窗口")
    ap.add_argument("--sn", type=int, default=0, help="个股层回归窗口，0 = 同 --n")
    ap.add_argument("--sm", type=int, default=0, help="个股层标准化窗口，0 = 同 --m")
    ap.add_argument("--cost", type=float, default=0.0005, help="单边成本")
    ap.add_argument("--spec", default="strict", choices=["strict", "simple"])
    ap.add_argument("--start", default="2021-11-13")
    ap.add_argument("--end", default="", help="默认取池内最后一天（CRSP 数据边界）")
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
    ap.add_argument("--turn-noise", type=float, default=0.0,
                    help="给换手率注入对数正态噪声的标准差（0 = 不扰动），用于稳健性检验")
    ap.add_argument("--seed", type=int, default=0, help="--turn-noise 的随机种子")
    ap.add_argument("--turnma-top", type=int, default=3, help="--pick turnma 时每个板块看换手率前几名")
    ap.add_argument("--turnma-sectors", type=int, default=3, help="--pick turnma 时最多下探几个板块")
    ap.add_argument("--turn-trend-first", action="store_true",
                    help="--pick turnma 时先筛趋势再排序：板块内所有票先过趋势闸，"
                         "合格的再按换手率降序取第一只（此时 --turnma-top 不生效）。"
                         "默认口径相反——先取换手率前 N 只，再在里面找第一只趋势合格的")
    ap.add_argument("--debug-pool", action="store_true",
                    help="诊断用：信号日打印每个板块的候选数、趋势合格数、合格票的换手率名次")
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
                    help="--trend-mode strong：斜率窗口的默认值；--slope-mas 里没写 :窗口 时用它，"
                         "0 = 不检查")
    ap.add_argument("--slope-mas", default="20:10,60:5",
                    help="--trend-mode strong：哪几条均线必须斜率为正、各自用多长窗口，"
                         "写法「均线:窗口」逗号分隔。默认 20:10,60:5 = MA20 的 10 日斜率、"
                         "MA60 的 5 日斜率都为正。只写均线名则用 --stack-slope 的窗口；"
                         "空字符串 = 不检查")
    ap.add_argument("--trend-above", type=int, default=200,
                    help="--trend-mode strong：收盘价必须站上的均线长度，0 = 不检查")
    ap.add_argument("--trend-cross", default="50,200",
                    help="--trend-mode strong：均线先后关系「快,慢」，要求 MA(快) > MA(慢)；"
                         "空字符串 = 不检查")
    ap.add_argument("--adx-min", type=float, default=10.0,
                    help="--trend-mode strong：ADX 下限（严格大于），0 = 不检查")
    ap.add_argument("--adx-dir", action="store_true",
                    help="--trend-mode strong：ADX 达标之外还要求 +DI > -DI（趋势方向得是买方主导）。"
                         "ADX 只量强度不分方向，阴跌票的 ADX 一样能很高")
    ap.add_argument("--max-near-high", type=float, default=0.0,
                    help="追高闸：收盘价距最近 --near-high-len 日最高价不足该比例时跳过该候选"
                         "（0.01 = 离高点 1%% 以内算追高），0 = 不检查。"
                         "目的是不买在放量突破当天，改买回踩过、刚转强的")
    ap.add_argument("--near-high-len", type=int, default=63,
                    help="追高闸的回看窗口（交易日），默认 63 ≈ 3 个月")
    ap.add_argument("--pos-band", default="",
                    help="位置分位区间（如 0.3,0.75）：pos = (C - 最低Low) / (最高High - 最低Low)，"
                         "窗口见 --pos-len。只买 pos 落在区间内的候选——低端排除破位，高端排除追高。"
                         "空字符串 = 不检查")
    ap.add_argument("--pos-len", type=int, default=20,
                    help="--pos-band 的回看窗口（交易日），默认 20")
    ap.add_argument("--bb-delta", type=float, default=0.0,
                    help="布林带追高闸（以自身标准差为单位）：z = (C - MA) / 标准差，"
                         "上轨是 z = --bb-mult。要求 z <= --bb-mult - delta，即离上轨至少留 "
                         "delta 个标准差。0 = 不检查")
    ap.add_argument("--bb-window", type=int, default=20, help="布林带均线窗口，默认 20")
    ap.add_argument("--bb-mult", type=float, default=2.0, help="布林带标准差倍数，默认 2")
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
                         "（近期权重更高，归一化成 %%/交易日），直接衡量板块本身在不在涨；"
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
                    help="用 SPY 与 N 日均线判定牛熊：线上牛市、线下熊市（0 = 不启用）")
    ap.add_argument("--regime-bear", default="nofilter",
                    choices=["nofilter", "downtrend", "cash"],
                    help="熊市中的处理：nofilter = 不做多头筛选，直接取 CV 第一名（默认）；"
                         "downtrend = 改买空头排列的票；cash = 空仓")
    ap.add_argument("--regime-window", type=int, default=0,
                    help="牛熊判定用「过去 N 个交易日」的统计而非当日单点：0/1 = 只看当日（旧口径）；"
                         "21 = 过去一个月")
    ap.add_argument("--regime-bear-ratio", type=float, default=0.5,
                    help="配合 --regime-window：过去 N 日里 SPY 低于均线的天数占比 ≥ 该值才算熊市"
                         "（默认 0.5）")
    ap.add_argument("--turn-rank", type=int, default=1,
                    help="--pick turn 时取第几名（1 = 换手率最高）")
    ap.add_argument("--turn-rank-mode", default="abs", choices=["abs", "ratio"],
                    help="个股排序口径：abs = 20 日均换手率（默认）；"
                         "ratio = turn20 × (turn20 / turn120) = turn20^2/turn120")
    ap.add_argument("--cv-lag", type=int, default=0,
                    help="--pick dvolcv/dvolcvh 时，CV 窗口截止日相对信号日滞后的交易日数")
    ap.add_argument("--stop-loss", type=float, default=0.0,
                    help="个股止损阈值（如 0.3 = 相对买入价跌 30%% 收盘割肉），0 = 不止损")
    ap.add_argument("--stop-ma20", type=int, default=0,
                    help="均线止损（0 = 关闭；填 20 = 用 MA20）。买点收盘在均线上方时："
                         "前一日收盘已跌破、且当日收盘仍未收回 → 当日收盘止损；买点在线下时："
                         "止损价 = min(买入价 × (1 - stop_pct), 买入日前一交易日布林带下轨)")
    ap.add_argument("--stop-pct", type=float, default=0.07,
                    help="--stop-ma20 里「买点在均线下方」时的固定比例止损（默认 0.07 = 7%%）")
    ap.add_argument("--stop-repick-days", type=int, default=10,
                    help="--stop-ma20 的再选股窗口：止损发生在持有期前 N 个交易日（默认 10）"
                         "才允许次日重新选股；--stop-repick-dom > 0 时改用日历日判断")
    ap.add_argument("--stop-repick-dom", type=int, default=0,
                    help="止损日在该自然月第 N 日之前（day < N，如 11 = 11 号之前）才允许"
                         "割肉后继续选股；0 = 用 --stop-repick-days 的交易日口径。"
                         "重选一律排除刚止损的那只票")
    ap.add_argument("--turn-cap", type=float, default=0.0,
                    help="20 日均换手率上限，超过的票当期不参与排序（0 = 不限）")
    ap.add_argument("--turn-tail", type=float, default=0.0,
                    help="信号日当天换手率的折算系数（0 = 不折算），推荐 0.48")
    ap.add_argument("--min-mcap", type=float, default=0.0,
                    help="最小市值门槛（亿美元，池层 DlyCap 口径），0 = 不限")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔的 ticker 或 security_id，整只从候选池剔除")
    ap.add_argument("--exclude-sectors", default="XLRE",
                    help="从板块层整体剔除的 ETF，逗号分隔（默认 XLRE：房地产，与实盘不一致，"
                         "且 3 年里有两年跑输 SPY）。空字符串 = 11 个板块全用")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    sn = args.sn or args.n
    sm = args.sm or args.m
    bear_turn5 = args.regime_ma > 0 and args.bear_pick == "turn5"
    start = pd.Timestamp(args.start)
    pool_end = pool.last_date()
    end = pd.Timestamp(args.end) if args.end else pool_end
    if end > pool_end:
        print(f"⚠ --end {end.date()} 超过池内数据边界 {pool_end.date()}，已截断"
              f"（池层只到 CRSP 数据截止日，不再用本地行情接续）")
        end = pool_end

    # 板块层可整体剔除某些 ETF（默认去掉 XLRE）；被剔除板块的成分股一并移出候选池，
    # 否则会出现「板块榜里没有它、但个股仍可能被熊市 turn5 挑中」的口径漏洞。
    drop_sec = {s.strip().upper() for s in str(args.exclude_sectors).split(",") if s.strip()}
    bad_sec = sorted(drop_sec - set(SECTOR_ETFS))
    if bad_sec:
        raise SystemExit(f"--exclude-sectors 里有不认识的 ETF：{', '.join(bad_sec)}")
    active_etfs = [s for s in SECTOR_ETFS if s not in drop_sec]
    if not active_etfs:
        raise SystemExit("--exclude-sectors 把板块全删了")
    if drop_sec:
        print(f"板块层剔除 {'/'.join(sorted(drop_sec))}：只用 {len(active_etfs)} 个板块 "
              f"({' '.join(active_etfs)})，这些板块的成分股也一并移出候选池")

    # 预热：RSRS + 均线；strong 还要看 MA200
    slope_wins = parse_slope_spec(args.slope_mas, args.stack_slope)
    warm = max(args.n + args.m, sn + sm) * 2 + 40
    if args.trend_mode == "strong":
        trend_w = max([args.trend_above] + [v for v in slope_wins.values() if v > 0]
                      + [int(x) for x in str(args.trend_stack).split(",") if x.strip()]
                      + [int(x) for x in str(args.trend_cross).split(",") if x.strip()])
        warm = max(warm, int(trend_w * 1.6) + 60)

    # --exclude 同时接受 ticker 与 security_id（ticker 兼容 BRK / BRK-B 两种写法）
    tick_all = pool.display_tickers()
    _ALIAS = {"BRK": "BRK-B", "BF": "BF-B"}

    def _norm_tick(t: str) -> str:
        k = str(t).upper().replace(".", "-").strip()
        return _ALIAS.get(k, k)

    tick2id = {}
    for i, t in tick_all.items():
        tick2id[_norm_tick(t)] = str(i)
        tick2id[str(t).upper()] = str(i)
    id_set = set(tick_all.index.astype(str))
    excl: set[str] = set()
    for tok in (s.strip() for s in args.exclude.split(",")):
        if not tok:
            continue
        key = tok.upper()
        if key in tick2id:
            excl.add(tick2id[key])
        elif key in id_set:
            excl.add(key)
        else:
            print(f"⚠ --exclude {tok}：池里找不到这个 ticker / security_id，忽略")

    data = load_pool(start, end, warm, excl, active_etfs)
    ids, cal = data["ids"], data["cal"]
    px, hi, lo = data["px"], data["hi"], data["lo"]
    mask, mcap, turn = data["mask"], data["mcap"], data["turn"]
    sec_of, sec_src, tick_of = data["sec_of"], data["sec_src"], data["tick_of"]
    stocks = ids
    T = lambda s: tick_of.get(s, s)  # noqa: E731  security_id → 展示用 ticker

    n_src: dict[str, int] = {}
    for s in stocks:
        n_src[sec_src[s]] = n_src.get(sec_src[s], 0) + 1
    daily_n = mask.loc[cal >= start].sum(axis=1)   # 预热段的 cal 不算，只统计回测区间
    print(f"数据源 sp500_pool：窗口内**并集** {len(stocks)} 只 "
          f"（含期内新进/被剔除的，防幸存者偏差）/ {len(cal)} 个交易日 "
          f"({cal[0].date()} ~ {cal[-1].date()})")
    print(f"  任一交易日真正在指数内的只有 {int(daily_n.median())} 只"
          f"（min {int(daily_n.min())} / max {int(daily_n.max())}），选股只在当日成分里挑")
    print(f"  板块归属：{' / '.join(f'{k} {v}' for k, v in sorted(n_src.items()))}"
          f"（holdings 快照优先，缺的按 GICS/SIC 补）")
    print(f"  预热 {warm} 自然日（load_start {data['load_start']}）；"
          f"清洗层自带外部预热段，新纳入的票不再需要前端补价")

    etf_rsrs = rsrs_panel_fast(hi[active_etfs], lo[active_etfs], args.n, args.m, args.spec)
    print(f"板块层 RSRS 完成 {etf_rsrs.shape}")
    stock_rsrs = rsrs_panel_fast(hi[stocks], lo[stocks], sn, sm, args.spec)
    print(f"个股层 RSRS 完成 {stock_rsrs.shape}（N={sn}, M={sm}）")

    bench = px[BENCH]
    ret_etf = px[active_etfs + [BENCH]].pct_change()
    ret_stk = px[stocks].pct_change()

    # 最小市值门槛：池层 DlyCap 是当期真实市值，没有股本反推/快照兜底的前视问题
    pool_mask = mask
    if args.min_mcap > 0:
        thr = args.min_mcap * 1e8
        ok = ((mcap > thr) | mcap.isna()).reindex(columns=stocks).fillna(True).astype(bool)
        gone = sorted(s for s in stocks if not bool(ok[s].any()))
        pool_mask = ok if pool_mask is None else (pool_mask & ok)
        print(f"--min-mcap {args.min_mcap:.0f} 亿美元（池层 DlyCap）：全程不达标的 "
              f"{len(gone)} 只移出候选（{' '.join(T(s) for s in gone[:16])}"
              f"{' ...' if len(gone) > 16 else ''}）")

    for d in (cal[0], cal[len(cal) // 2], cal[-1]):
        print(f"  {d.date()} 在池 {int(pool_mask.loc[d].sum())} 只（PIT）")

    def in_pool(sid: str, dt_: pd.Timestamp) -> bool:
        if pool_mask is None:
            return True
        try:
            return bool(pool_mask.at[dt_, sid])
        except KeyError:
            return False

    # ------------------------------------------------------------------
    # 换手率 / 成交额面板（池层逐日 turnover = 成交量 ÷ 当日总股本）
    # ------------------------------------------------------------------
    turn_rate = turn_rate_120 = turn_score = None
    dvol = dvol_cv = None
    if (args.pick in ("turn", "dvol", "dvolcv", "dvolcvh", "cvdown", "turnma")
            or bear_turn5):
        vol = pool.panel("volume", stocks, data["load_start"],
                         end.strftime("%Y-%m-%d")).reindex(cal)
        if args.pick in ("turn", "turnma") or bear_turn5:
            raw = turn.reindex(cal).ffill()
            cols = [c for c in stocks if c in raw.columns]
            missing = sorted(set(stocks) - set(cols))
            raw = raw[cols]
            if args.turn_tail > 0:
                # 调仓在收盘前 30 分钟内完成，信号日当天的成交量只走完一部分
                cnt = raw.notna().rolling(20, min_periods=1).sum()
                s20 = raw.rolling(20, min_periods=1).sum()
                turn_rate = ((s20 - raw + args.turn_tail * raw) / cnt).where(cnt >= 10)
                print(f"信号日换手率按尾盘系数 {args.turn_tail:.2f} 折算")
            else:
                turn_rate = raw.rolling(20, min_periods=10).mean()
            if args.turn_rank_mode == "ratio":
                cnt120 = raw.notna().rolling(120, min_periods=1).sum()
                s120 = raw.rolling(120, min_periods=1).sum()
                if args.turn_tail > 0:
                    turn_rate_120 = ((s120 - raw + args.turn_tail * raw)
                                     / cnt120).where(cnt120 >= 60)
                else:
                    turn_rate_120 = raw.rolling(120, min_periods=60).mean()
                print(f"120 日均换手率面板完成 {turn_rate_120.shape}（自身常态基准）")
            print(f"换手率面板完成 {turn_rate.shape}（池层 成交量/总股本，20 日均值，"
                  f"共 {len(cols)} 只；无数据 {len(missing)} 只）")
            if missing:
                print(f"  无换手率数据已剔除：{' '.join(T(s) for s in missing[:20])}"
                      f"{' ...' if len(missing) > 20 else ''}")
            if args.turn_cap > 0:
                n_cap = int((turn_rate > args.turn_cap).sum().sum())
                turn_rate = turn_rate.mask(turn_rate > args.turn_cap)
                print(f"换手率上限 {args.turn_cap:.0%}：剔除 {n_cap} 个票日")
            if args.turn_noise > 0:
                rng = np.random.default_rng(args.seed)
                noise = rng.normal(0.0, args.turn_noise, size=turn_rate.shape)
                turn_rate = turn_rate * np.exp(pd.DataFrame(
                    noise, index=turn_rate.index, columns=turn_rate.columns))
                print(f"换手率已注入噪声 sigma={args.turn_noise:.2f}（seed={args.seed}）")
            if args.turn_rank_mode == "ratio":
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
                print(f"成交额面板完成 {dvol.shape}（20 日均值，volume × adj_close，"
                      f"共 {len(cols)} 只）")
            else:
                mean_ = amt.rolling(20, min_periods=10).mean()
                std_ = amt.rolling(20, min_periods=10).std()
                dvol_cv = (std_ / mean_.where(mean_ > 0)).reindex(cal).ffill()
                if args.cv_lag:
                    dvol_cv = dvol_cv.shift(args.cv_lag)
                print(f"成交额变异系数面板完成 {dvol_cv.shape}"
                      f"（20 日 std/mean，窗口滞后 {args.cv_lag} 个交易日，共 {len(cols)} 只）")

    # ------------------------------------------------------------------
    # 趋势面板
    # ------------------------------------------------------------------
    ma_t = px[stocks].rolling(args.trend_ma, min_periods=max(5, args.trend_ma // 2)).mean()
    ma_t_prev = ma_t.shift(args.trend_slope)
    ma_group = [int(x) for x in str(args.trend_mas).split(",") if str(x).strip()]
    ma_panels = {w: px[stocks].rolling(w, min_periods=max(3, w // 2)).mean() for w in ma_group}
    stack_group = [int(x) for x in str(args.trend_stack).split(",") if x.strip()]
    stack_panels = {w: px[stocks].rolling(w, min_periods=max(3, w // 2)).mean() for w in stack_group}

    # 均线止损专用面板：止损均线 + 买入日的布林带下轨
    stop_ma_p, stop_bb_low = None, None
    if args.stop_ma20 > 0:
        _sw = max(2, int(args.stop_ma20))
        stop_ma_p = px[stocks].rolling(_sw, min_periods=max(5, _sw // 2)).mean()
        _bw2 = max(2, int(args.bb_window))
        _bm2 = px[stocks].rolling(_bw2, min_periods=max(10, _bw2 // 2)).mean()
        _bs2 = px[stocks].rolling(_bw2, min_periods=max(10, _bw2 // 2)).std(ddof=0)
        stop_bb_low = _bm2 - float(args.bb_mult) * _bs2
        _repick_txt = (f"当月 {args.stop_repick_dom} 日之前止损 → 割肉后逐日再选股"
                       f"（排除原票）" if args.stop_repick_dom > 0
                       else f"前 {max(1, args.stop_repick_days)} 个交易日止损后次日再选股一次")
        print(f"均线止损：MA{_sw}（买点在线上 → 前一日跌破 + 当日未收回，两日确认）；"
              f"离线买入 → min(买入价 × {1.0 - args.stop_pct:.2f}, 前一日布林带下轨)；"
              f"{_repick_txt}")
    elif args.stop_loss > 0:
        _repick_txt = (f"当月 {args.stop_repick_dom} 日之前止损 → 割肉后逐日再选股"
                       f"（排除原票）" if args.stop_repick_dom > 0
                       else f"前 {max(1, args.stop_repick_days)} 个交易日止损后次日再选股一次")
        print(f"固定止损：收盘价相对买入价跌 {args.stop_loss:.1%} 当日收盘割；{_repick_txt}")

    strong = args.trend_mode == "strong"
    slope_panels, above_panel, cross_panels = {}, None, None
    adx_p, pdi_p, mdi_p = None, None, None
    near_high_p = pos_p = None
    pos_lo, pos_hi = None, None
    bb_ma, bb_sd, bb_zmax = None, None, None
    pos_len = max(2, int(args.pos_len))
    if str(args.pos_band).strip():
        _pb = [float(x) for x in str(args.pos_band).replace("~", ",").split(",") if str(x).strip()]
        if len(_pb) != 2 or not (0.0 <= _pb[0] < _pb[1] <= 1.0):
            raise SystemExit(f"--pos-band 需要两个递增值且在 0~1 之间，收到 {args.pos_band!r}")
        pos_lo, pos_hi = _pb
    cross_group = [int(x) for x in str(args.trend_cross).split(",") if x.strip()]
    if strong:
        for w, win in slope_wins.items():
            if win <= 0:
                continue
            base = stack_panels.get(w)
            if base is None:
                base = px[stocks].rolling(w, min_periods=max(3, w // 2)).mean()
            slope_panels[w] = base - base.shift(win)
        if args.trend_above > 0:
            above_panel = px[stocks].rolling(args.trend_above,
                                             min_periods=args.trend_above).mean()
        if len(cross_group) == 2:
            cross_panels = [px[stocks].rolling(w, min_periods=w).mean() for w in cross_group]
        if args.adx_min > 0 or args.adx_dir:
            adx_p, pdi_p, mdi_p = dmi_panels(hi[stocks], lo[stocks], px[stocks], args.adx_len)
        if args.max_near_high > 0:
            near_high_p = (hi[stocks].rolling(args.near_high_len,
                                              min_periods=max(10, args.near_high_len // 2))
                           .max())
        if pos_lo is not None:
            hi_n = hi[stocks].rolling(pos_len, min_periods=max(10, pos_len // 2)).max()
            lo_n = lo[stocks].rolling(pos_len, min_periods=max(10, pos_len // 2)).min()
            rng = (hi_n - lo_n).where(lambda d: d > 0)
            pos_p = (px[stocks] - lo_n) / rng
        if args.bb_delta > 0:
            _bw = max(2, int(args.bb_window))
            _bm = px[stocks].rolling(_bw, min_periods=max(10, _bw // 2)).mean()
            _bs = px[stocks].rolling(_bw, min_periods=max(10, _bw // 2)).std(ddof=0)
            bb_ma, bb_sd = _bm, _bs
            bb_zmax = float(args.bb_mult) - float(args.bb_delta)
        slope_txt = ("，" + "、".join(f"MA{w} 的 {win} 日斜率"
                                     for w, win in slope_wins.items() if win > 0) + "为正"
                     if any(v > 0 for v in slope_wins.values()) else "")
        print(f"强化趋势过滤：排列 {' > '.join(f'MA{w}' for w in stack_group)}{slope_txt}"
              f"{f'，收盘 > MA{args.trend_above}' if args.trend_above > 0 else ''}"
              f"{f'，MA{cross_group[0]} > MA{cross_group[1]}' if cross_panels else ''}"
              f"{f'，ADX({args.adx_len}) > {args.adx_min:g}' if adx_p is not None else ''}"
              f"{'，+DI > -DI' if args.adx_dir else ''}"
              f"{f'，距 {args.near_high_len} 日最高价 > {args.max_near_high:.1%}' if near_high_p is not None else ''}"
              f"{f'，位置分位({pos_len}) 在 {pos_lo:.0%}~{pos_hi:.0%}' if pos_lo is not None else ''}"
              f"{f'，布林 z <= {bb_zmax:g}' if bb_sd is not None else ''}")

    # ------------------------------------------------------------------
    # 牛熊 + 熊市板块排序
    # ------------------------------------------------------------------
    spy_ma, bear_ratio = None, None
    if args.regime_ma > 0:
        spy_ma = px[BENCH].rolling(args.regime_ma,
                                   min_periods=max(20, args.regime_ma // 2)).mean()
        below = (px[BENCH] < spy_ma).astype(float).where(spy_ma.notna())
        win = max(1, args.regime_window)
        bear_ratio = (below if win == 1 else below.rolling(win, min_periods=win).mean())
        print(f"牛熊判定：SPY vs MA{args.regime_ma}，"
              + (f"过去 {win} 日跌破占比 ≥ {args.regime_bear_ratio:.0%} 判熊"
                 if win > 1 else "当日跌破即判熊（旧口径）"))
        if bear_turn5:
            print(f"熊市选股：不分板块，市值 > {args.bear_turn_mcap:g} 亿美元的票按换手率"
                  f"降序取前 {args.bear_turn_top} 只")

    sec_ma = (px[active_etfs].rolling(args.bear_sector_ma, min_periods=args.bear_sector_ma).mean()
              if args.bear_sector_ma > 0 else None)
    etf_slope = (weighted_slope_panel(px[active_etfs], args.bear_slope_window)
                 if args.regime_ma > 0 and args.bear_rank in ("slope", "both") else None)

    def is_bull(dt: pd.Timestamp) -> bool:
        if spy_ma is None:
            return True
        r = bear_ratio.at[dt] if dt in bear_ratio.index else np.nan
        if not np.isfinite(r):
            return True
        return bool(r < args.regime_bear_ratio)

    def pick_bear_etf(sec_pool: pd.Series) -> str:
        """熊市里该买哪只板块 ETF：RSRS/斜率最强、且（若设了下限）动量必须过关。"""
        if sec_pool.empty:
            return ""
        top = str(sec_pool.index[0])
        if args.bear_min_score is not None and not (float(sec_pool.iloc[0]) > args.bear_min_score):
            return ""
        return top

    def double_scores(dt_: pd.Timestamp) -> pd.Series:
        """某一天的「RSRS 前 N ∩ 斜率前 N」（按 RSRS 降序），并套用 --bear-sector-ma 闸。"""
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

    def gate_fail(sid: str, dt: pd.Timestamp, desc: bool | None = None) -> str:
        """strong 模式：返回第一道没过闸的中文原因；空串 = 全过。"""
        if sid not in ma_t.columns:
            return "无均线数据"
        a = px.at[dt, sid]
        if not np.isfinite(a):
            return "当日无价格"
        prev, prev_w = None, None
        for w in stack_group:
            m = stack_panels[w].at[dt, sid]
            if not np.isfinite(m):
                return f"MA{w} 数据缺失"
            if prev is not None and not (prev > m):
                return f"均线未多头排列（MA{prev_w}≤MA{w}）"
            prev, prev_w = m, w
        for w, win in slope_wins.items():
            if win <= 0:
                continue
            dlt = slope_panels[w].at[dt, sid]
            if not np.isfinite(dlt):
                return f"MA{w} 的 {win} 日斜率数据缺失"
            if dlt <= 0:
                return f"MA{w} 的 {win} 日斜率为负"
        if above_panel is not None:
            m_long = above_panel.at[dt, sid]
            if not np.isfinite(m_long):
                return f"MA{args.trend_above} 数据缺失"
            if not (a > m_long):
                return f"收盘跌破 MA{args.trend_above}（{a / m_long - 1:+.1%}）"
        if cross_panels is not None:
            fast_m, slow_m = cross_panels[0].at[dt, sid], cross_panels[1].at[dt, sid]
            if not (np.isfinite(fast_m) and np.isfinite(slow_m)):
                return "交叉均线数据缺失"
            if not (fast_m > slow_m):
                return f"MA{cross_group[0]}<MA{cross_group[1]}"
        if adx_p is not None:
            v = adx_p.at[dt, sid]
            if not np.isfinite(v):
                return "ADX 数据缺失"
            if not (v > args.adx_min):
                return f"ADX {v:.1f}≤{args.adx_min:g}"
            if args.adx_dir:
                pdi, mdi = pdi_p.at[dt, sid], mdi_p.at[dt, sid]
                if not (np.isfinite(pdi) and np.isfinite(mdi)):
                    return "DI 数据缺失"
                if not (pdi > mdi):
                    return "+DI<-DI"
        if near_high_p is not None:
            nh = near_high_p.at[dt, sid]
            if not np.isfinite(nh) or nh <= 0:
                return "近期高点数据缺失"
            if (a / nh - 1.0) > -args.max_near_high:
                return f"贴近日高（距高点 {a / nh - 1:+.1%}）"
        if pos_p is not None:
            pv = pos_p.at[dt, sid]
            if not np.isfinite(pv):
                return "位置分位缺失"
            if not (pos_lo <= pv <= pos_hi):
                return f"位置分位 {pv:.2f} 不在 [{pos_lo:.2f}, {pos_hi:.2f}]"
        if bb_sd is not None:
            m_, s_ = bb_ma.at[dt, sid], bb_sd.at[dt, sid]
            if not (np.isfinite(m_) and np.isfinite(s_)) or s_ <= 0:
                return "布林带数据缺失"
            z = (a - m_) / s_
            if z > bb_zmax:
                return f"布林 z={z:.2f}>{bb_zmax:g}"
        return ""

    def is_down(sid: str, dt: pd.Timestamp, desc: bool | None = None) -> bool:
        if args.trend_mode == "strong":
            return gate_fail(sid, dt, desc) == ""
        if sid not in ma_t.columns:
            return False
        a = px.at[dt, sid]
        if not np.isfinite(a):
            return False
        if args.trend_mode == "ma_stack":
            use_desc = (args.stack_order == "desc") if desc is None else bool(desc)
            prev = None
            for w in stack_group:
                m = stack_panels[w].at[dt, sid]
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
            for w in ma_group:
                m = ma_panels[w].at[dt, sid]
                if not np.isfinite(m) or a >= m:
                    return False
            return True
        m, mp = ma_t.at[dt, sid], ma_t_prev.at[dt, sid]
        if not (np.isfinite(m) and np.isfinite(mp)):
            return False
        return bool(a < m and m < mp)

    def turnma_pick_on(dt_sel: pd.Timestamp,
                       exclude: set[str] | None = None) -> tuple[str, str]:
        """上旬止损后再选股：口径与主循环一致，返回 (security_id, 板块)。

        `exclude` 里的票（刚止损的那只）不参与，下一名顺补。
        """
        if dt_sel not in etf_rsrs.index:
            return "", ""
        zs = etf_rsrs.loc[dt_sel].dropna().sort_values(ascending=False)
        if zs.empty:
            return "", ""
        ex = {str(s) for s in (exclude or ())}
        rp2 = turn_score if turn_score is not None else turn_rate
        use_desc2 = (args.stack_order == "desc") if args.regime_ma == 0 else is_bull(dt_sel)
        for sec_k in zs.index[: args.turnma_sectors]:
            pool_k = [s for s in stocks if sec_of.get(s) == sec_k
                      and s in rp2.columns and in_pool(s, dt_sel)]
            if not pool_k:
                continue
            tt = (rp2.loc[dt_sel, pool_k].dropna().sort_values(ascending=False)
                  .head(max(1, args.turnma_top)))
            for s in tt.index:
                if str(s) in ex:
                    continue
                if is_down(str(s), dt_sel, use_desc2):
                    return str(s), str(sec_k)
        return "", ""

    # ------------------------------------------------------------------
    # 调仓日与主循环
    # ------------------------------------------------------------------
    me = anchors(cal, args.freq)
    if args.anchor_lag:
        pos = np.clip(cal.get_indexer(me) - args.anchor_lag, 0, None)
        me = pd.DatetimeIndex(cal[np.unique(pos)])
        print(f"调仓日前移 {args.anchor_lag} 个交易日")

    scored = etf_rsrs.dropna(how="any").index
    cand = [i for i, d in enumerate(me) if d <= start and d in scored]
    if not cand:
        raise SystemExit("预热不足：信号日之前没有可用的板块 RSRS")
    k0 = cand[-1]

    nav = pd.Series(np.nan, index=cal[cal >= start], dtype=float)
    nav_etf = nav.copy()
    cur_s, cur_e, prev = 1.0, 1.0, None
    prev_s, prev_e = None, None
    rows = []
    for k in range(k0, len(me) - 1):
        sig = me[k]
        if sig not in scored:
            continue
        probe, cand_gate = [], []
        sec_all = etf_rsrs.loc[sig].dropna().sort_values(ascending=False)
        if sec_all.empty:
            continue
        bull = is_bull(sig)
        bear = args.regime_ma > 0 and not bull
        nxt = me[k + 1]

        sec_score = sec_all
        sec_ref = sec_all
        slope_now = (etf_slope.loc[sig].reindex(sec_all.index).dropna()
                     if etf_slope is not None else None)
        if bear and slope_now is not None and args.bear_rank == "slope":
            sec_score = slope_now.sort_values(ascending=False)
        elif bear and slope_now is not None and args.bear_rank == "both":
            top_n = max(1, args.bear_intersect_top)
            rsrs_top = set(sec_all.index[:top_n])
            slope_top = set(slope_now.sort_values(ascending=False).index[:top_n])
            sec_score = sec_all[[s_ for s_ in sec_all.index
                                 if s_ in rsrs_top and s_ in slope_top]]
            if sec_score.empty and args.bear_lookback > 0:
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
        if bear and sec_ma is not None:
            keep = [s_ for s_ in sec_score.index
                    if s_ in sec_ma.columns and np.isfinite(sec_ma.at[sig, s_])
                    and np.isfinite(px.at[sig, s_]) and px.at[sig, s_] > sec_ma.at[sig, s_]]
            sec_score = sec_score[keep]

        no_sector = sec_score.empty
        sector = str(sec_all.index[0]) if no_sector else str(sec_score.index[0])
        pool_k = [s for s in stocks if sec_of.get(s) == sector
                  and s in stock_rsrs.columns and in_pool(s, sig)]
        ss = (stock_rsrs.loc[sig, pool_k].dropna().sort_values(ascending=False)
              if pool_k else pd.Series(dtype=float))
        pick, buy_etf = "", False
        tried, sec_rank = 0, 0
        buy_dt, rsrs_ref = sig, sec_ref

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
            pass
        elif bear and bear_turn5:
            cand_t = [s for s in stocks
                      if s in turn_rate.columns and s in px.columns and in_pool(s, sig)
                      and mcap_above(mcap, s, sig, args.bear_turn_mcap * 1e8)]
            tt = (turn_rate.loc[sig, cand_t].dropna().sort_values(ascending=False)
                  .head(max(1, args.bear_turn_top)) if cand_t else pd.Series(dtype=float))
            for s in tt.index:
                tried += 1
                if is_down(str(s), sig, True):
                    pick = str(s)
                    sector = sec_of.get(pick, BENCH)
                    if sector not in active_etfs:
                        sector = BENCH
                    sec_rank = 1
                    break
            print(f"  熊市 turn5 {sig.date()}：TOP{len(tt)} "
                  f"{' '.join(f'{T(s)}({v:.2%})' for s, v in tt.items())} "
                  f"→ {T(pick) if pick else '空仓（交给熊市兜底）'}", flush=True)
        elif no_sector:
            pass
        elif bear and args.bear_etf_only:
            etf_sym = pick_bear_etf(sec_score)
            if etf_sym:
                sector = etf_sym
                pick, buy_etf, sec_rank = f"ETF:{etf_sym}", True, 1
        elif args.pick in ("mcap", "mmin"):
            mcap_of = mcap.ffill().iloc[-1]
            ranked = sorted([s for s in pool_k if np.isfinite(mcap_of.get(s, np.nan))],
                            key=lambda s: float(mcap_of[s]), reverse=(args.pick == "mcap"))
            pick = ranked[0] if ranked else ""
        elif args.pick == "turn":
            rp = turn_score if turn_score is not None else turn_rate
            cand_t = [s for s in pool_k if s in rp.columns]
            t = (rp.loc[sig, cand_t].dropna().sort_values(ascending=False)
                 if cand_t else pd.Series(dtype=float))
            ri = min(max(1, args.turn_rank), len(t)) - 1
            pick = str(t.index[ri]) if len(t) else ""
        elif args.pick == "turnma":
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
                probe, cand_gate = [], []
                for rank_s, sec_k in enumerate(sec_pool.index[: args.turnma_sectors], 1):
                    pool_k2 = [s for s in stocks if sec_of.get(s) == sec_k
                               and s in rp.columns and in_pool(s, dt_try)]
                    if not pool_k2:
                        probe.append(f"{sec_k}#{rank_s}:")
                        continue
                    tt_all = (rp.loc[dt_try, pool_k2].dropna()
                              .sort_values(ascending=False))
                    probe.append(f"{sec_k}#{rank_s}:" + ",".join(
                        T(str(x)) for x in tt_all.head(max(1, args.turnma_top)).index))
                    if pick:
                        for s_ in tt_all.head(max(1, args.turnma_top)).index:
                            gf_ = (gate_fail(str(s_), dt_try, use_desc)
                                   if args.trend_mode == "strong" else "")
                            cand_gate.append(f"{T(str(s_))}={'PASS' if not gf_ else gf_}")
                        continue
                    if args.debug_pool:
                        _elig = [str(x) for x in tt_all.index
                                 if is_down(str(x), dt_try, use_desc)]
                        _rank = (list(tt_all.index).index(_elig[0]) + 1) if _elig else 0
                        print(f"[dbg] {dt_try.date()} {'sig' if dt_try == sig else 'try'}"
                              f"#r{rank_s} {sec_k} 候选{len(pool_k2)} "
                              f"合格{len(_elig)} 首位排名{_rank} "
                              f"{','.join(_elig[:6])}", flush=True)
                    if args.turn_trend_first:
                        for s in tt_all.index:
                            tried += 1
                            if is_down(str(s), dt_try, use_desc):
                                pick, sector, sec_rank = str(s), str(sec_k), rank_s
                                buy_dt = dt_try
                                rsrs_ref = zs if dt_try != sig else sec_all
                                break
                        continue
                    tt = tt_all.head(args.turnma_top)
                    for s in tt.index:
                        s_ = str(s)
                        gf = (gate_fail(s_, dt_try, use_desc)
                              if args.trend_mode == "strong" else "")
                        if pick:
                            cand_gate.append(f"{T(s_)}={'PASS' if not gf else gf}")
                            continue
                        tried += 1
                        cand_gate.append(f"{T(s_)}={gf or 'OK'}")
                        if is_down(s_, dt_try, use_desc):
                            pick, sector, sec_rank = s_, str(sec_k), rank_s
                            buy_dt = dt_try
                            rsrs_ref = zs if dt_try != sig else sec_all
                if pick:
                    break
        elif args.pick == "dvol":
            cand_t = [s for s in pool_k if s in dvol.columns]
            t = (dvol.loc[sig, cand_t].dropna().sort_values(ascending=False)
                 if cand_t else pd.Series(dtype=float))
            pick = str(t.index[0]) if len(t) else ""
        elif args.pick in ("dvolcv", "dvolcvh"):
            cand_t = [s for s in pool_k if s in dvol_cv.columns]
            t = (dvol_cv.loc[sig, cand_t].dropna().sort_values(
                ascending=(args.pick == "dvolcv")) if cand_t else pd.Series(dtype=float))
            pick = str(t.index[0]) if len(t) else ""
        elif args.pick == "cvdown":
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
                    pool_k2 = [s for s in stocks if sec_of.get(s) == sec_k
                               and s in dvol_cv.columns and in_pool(s, sig)]
                    if not pool_k2:
                        continue
                    tt = (dvol_cv.loc[sig, pool_k2].dropna().sort_values(ascending=False)
                          .head(args.cvdown_top))
                    for s in tt.index:
                        tried += 1
                        if is_down(str(s), sig, use_desc):
                            pick, sector, sec_rank = str(s), str(sec_k), rank_s
                            break
                    if pick:
                        break
        else:
            pick = str(ss.index[0]) if len(ss) else ""

        if not pick and bear and args.bear_etf and not no_sector:
            etf_sym = pick_bear_etf(sec_score)
            if etf_sym:
                sector, pick, buy_etf = etf_sym, f"ETF:{etf_sym}", True
                sec_rank = sec_rank or 1
        if not pick and bear and args.bear_spy:
            sector, pick, buy_etf = BENCH, f"ETF:{BENCH}", True
            sec_rank = sec_rank or 1

        # 本期板块内换手率前 3（看板画同期走势用，展示 ticker）
        cand3 = []
        rp_rank = turn_score if turn_score is not None else turn_rate
        if sector in active_etfs and rp_rank is not None:
            c_pool = [s for s in stocks if sec_of.get(s) == sector
                      and s in rp_rank.columns and in_pool(s, buy_dt)]
            if c_pool:
                cand3 = [T(str(x)) for x in rp_rank.loc[buy_dt, c_pool].dropna()
                         .sort_values(ascending=False).head(3).index]
        if not probe and args.pick == "turnma" and rp_rank is not None and buy_dt in etf_rsrs.index:
            sec_order = (etf_rsrs.loc[buy_dt].dropna()
                         .sort_values(ascending=False).index[: args.turnma_sectors])
            for rank_s, sec_k in enumerate(sec_order, 1):
                pool_k2 = [s for s in stocks if sec_of.get(s) == sec_k
                           and s in rp_rank.columns and in_pool(s, buy_dt)]
                if not pool_k2:
                    probe.append(f"{sec_k}#{rank_s}:")
                    continue
                tt_all = rp_rank.loc[buy_dt, pool_k2].dropna().sort_values(ascending=False)
                probe.append(f"{sec_k}#{rank_s}:" + ",".join(
                    T(str(x)) for x in tt_all.head(max(1, args.turnma_top)).index))

        seg = cal[(cal > buy_dt) & (cal <= nxt) & (cal >= start)]
        if len(seg) == 0:
            prev = pick
            continue
        if buy_dt != sig:
            for dt in cal[(cal > sig) & (cal <= buy_dt) & (cal >= start)]:
                nav.loc[dt] = cur_s
                nav_etf.loc[dt] = cur_e
            if pick:
                print(f"  {'牛市' if bull else '熊市'}补选：{sig.date()} 无票 → "
                      f"{buy_dt.date()} 买入 {T(pick)}（{sector}）", flush=True)

        turnover = 0.0 if pick == prev else (2.0 if prev else 1.0)
        pick0 = pick
        base_px = float(px.at[buy_dt, sector if buy_etf else pick]) if pick else np.nan
        stopped, stop_dt, stop_kind = False, None, ""
        repick_dt, repick_sym = None, ""
        n_stop = 0
        sector0, sector_e = sector, sector

        def _c_at(d: pd.Timestamp, s_: str) -> float:
            return float(px.at[d, s_]) if (d in px.index and s_ in px.columns) else np.nan

        def stop_setup(sid: str, d_buy: pd.Timestamp) -> tuple[bool, float]:
            if stop_ma_p is None or not sid or sid not in stop_ma_p.columns:
                return False, np.nan
            if d_buy not in px.index or d_buy not in stop_ma_p.index:
                return False, np.nan
            c_, m_ = _c_at(d_buy, sid), float(stop_ma_p.at[d_buy, sid])
            if not (np.isfinite(c_) and np.isfinite(m_)):
                return False, np.nan
            if c_ > m_:
                return True, np.nan
            fixed = c_ * (1.0 - args.stop_pct)
            # 下轨取「买入日前一交易日」：买入日的下轨要用当日收盘才能算出，
            # 拿它当买入时就已确定的止损位属于轻微前视
            i_b = int(cal.get_indexer([d_buy])[0])
            d_bb = cal[i_b - 1] if i_b > 0 else d_buy
            bb = (float(stop_bb_low.at[d_bb, sid])
                  if stop_bb_low is not None and d_bb in stop_bb_low.index
                  and sid in stop_bb_low.columns else np.nan)
            if np.isfinite(bb) and bb > 0:
                fixed = min(fixed, bb)
            return False, fixed

        above_ma, stop_px = (stop_setup(pick, buy_dt) if (pick and not buy_etf)
                             else (False, np.nan))

        j = 0
        while j < len(seg):
            dt = seg[j]
            j_step = 1
            if j == 0:
                cur_s *= (1.0 - args.cost * turnover)
                cur_e *= (1.0 - args.cost * turnover)
            if pick and not stopped:
                a = ret_etf.loc[dt, sector] if buy_etf else ret_stk.loc[dt, pick]
                cur_s *= (1.0 + (0.0 if pd.isna(a) else a))
                hit, kind = False, ""
                if args.stop_ma20 > 0 and not buy_etf:
                    if above_ma:
                        i_ = int(cal.get_indexer([dt])[0])
                        if i_ > 0:
                            d0 = cal[i_ - 1]
                            c0, c1 = _c_at(d0, pick), _c_at(dt, pick)
                            m0 = (float(stop_ma_p.at[d0, pick])
                                  if d0 in stop_ma_p.index else np.nan)
                            m1 = (float(stop_ma_p.at[dt, pick])
                                  if dt in stop_ma_p.index else np.nan)
                            if (np.isfinite(c0) and np.isfinite(c1)
                                    and np.isfinite(m0) and np.isfinite(m1)
                                    and c0 < m0 and c1 < m1):
                                hit, kind = True, f"MA{args.stop_ma20}"
                    elif np.isfinite(stop_px) and stop_px > 0:
                        if _c_at(dt, pick) <= stop_px:
                            hit, kind = True, f"固定{args.stop_pct:.0%}"
                if (not hit and args.stop_loss > 0 and not buy_etf
                        and np.isfinite(base_px) and base_px > 0):
                    if _c_at(dt, pick) / base_px - 1.0 <= -args.stop_loss:
                        hit, kind = True, f"{args.stop_loss:.0%}"
                if hit:
                    stopped = True
                    n_stop += 1
                    if n_stop == 1:
                        stop_dt, stop_kind = dt.date(), kind
                    if args.stop_repick_dom > 0:
                        allow_repick = dt.day < args.stop_repick_dom
                    else:
                        allow_repick = (j + 1) <= max(1, args.stop_repick_days)
                    if (args.stop_ma20 > 0 or args.stop_loss > 0) and n_stop == 1 and allow_repick:
                        for dt_try in cal[(cal > dt) & (cal <= nxt)]:
                            p2, s2 = turnma_pick_on(dt_try, exclude={pick0})
                            if not p2:
                                continue
                            repick_dt, repick_sym = dt_try.date(), T(p2)
                            pick, sector, buy_etf = p2, s2, False
                            base_px = _c_at(dt_try, p2)
                            above_ma, stop_px = stop_setup(p2, dt_try)
                            cur_s *= (1.0 - args.cost * 1.0)
                            stopped = False
                            j_step = (int(seg.get_loc(dt_try)) + 1 - j
                                      if dt_try in seg else len(seg) - j)
                            print(f"  上旬止损再选股：{dt.date()} 止损 {kind} → "
                                  f"{dt_try.date()} 买入 {T(p2)}（{s2}）", flush=True)
                            break
            b = ret_etf.loc[dt, sector_e]
            cur_e *= (1.0 + (0.0 if pd.isna(b) else b))
            nav.loc[dt] = cur_s
            nav_etf.loc[dt] = cur_e
            j += j_step

        base_s = nav.loc[seg].iloc[0] if prev_s is None else prev_s
        base_e = nav_etf.loc[seg].iloc[0] if prev_e is None else prev_e
        seg_s = cur_s / base_s - 1.0
        seg_e = cur_e / base_e - 1.0
        prev_s, prev_e = cur_s, cur_e
        sp = bench.reindex(seg).ffill()
        rows.append({
            "信号日": sig.date(), "持有期起": seg[0].date(), "持有期止": seg[-1].date(),
            "板块": sector0,
            "板块RSRS": (round(float(rsrs_ref[sector0]), 2) if sector0 in rsrs_ref.index
                        else (np.nan if sector0 == BENCH else round(float(rsrs_ref.iloc[0]), 2))),
            "板块斜率%": (round(float(etf_slope.at[buy_dt, sector0]) * 100, 3)
                       if etf_slope is not None and sector0 in etf_slope.columns
                       and np.isfinite(etf_slope.at[buy_dt, sector0]) else np.nan),
            "斜率序": (int(etf_slope.loc[buy_dt].rank(ascending=False)[sector0])
                    if etf_slope is not None and sector0 in etf_slope.columns
                    and np.isfinite(etf_slope.at[buy_dt, sector0]) else np.nan),
            "补选日": (buy_dt.date() if buy_dt != sig else None),
            "板块RSRS序": sec_rank or 1,
            "牛市": int(bull),
            "个股": T(pick0) if pick0 else "",
            "个股ID": (pick0 if pick0 and not buy_etf else ""),
            "候选尝试": tried,
            "个股RSRS": (round(float(stock_rsrs.at[buy_dt, pick0]), 2)
                       if pick0 and not buy_etf and pick0 in stock_rsrs.columns else np.nan),
            "同板块候选": len(ss),
            "位置分位": (round(float(pos_p.at[buy_dt, pick0]), 3)
                     if pos_p is not None and pick0 and pick0 in pos_p.columns
                     and np.isfinite(pos_p.at[buy_dt, pick0]) else np.nan),
            "个股收益": round(float(seg_s), 4), "ETF收益": round(float(seg_e), 4),
            "选股差": round(float(seg_s - seg_e), 4), "止损日": stop_dt,
            "止损类型": (stop_kind or None),
            "再选股": (f"{repick_dt} {repick_sym}" if repick_dt else None),
            "候选前3": ",".join(cand3),
            "候选明细": "|".join(probe),
            "候选闸门": ("|".join(cand_gate) if cand_gate else None),
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

    # 目录名：源固定 _pool，其余参数照旧
    tag = f"_{args.tag}" if args.tag else ""
    ftag = "" if args.freq == "M" else f"_{args.freq}"
    ptag = "" if args.pick == "rsrs" else f"_{args.pick}"
    ptag += "_pool"
    if drop_sec:
        ptag += "_no-" + "-".join(sorted(drop_sec))
    if args.pick == "turn" and args.turn_rank > 1:
        ptag += f"_r{args.turn_rank}"
    if args.stop_loss > 0:
        ptag += f"_sl{round(args.stop_loss * 100)}"
        if int(args.stop_repick_dom) > 0:
            ptag += f"_srdom{args.stop_repick_dom}"
    if args.stop_ma20 > 0:
        ptag += f"_stma{args.stop_ma20}"
        if abs(float(args.stop_pct) - 0.07) > 1e-9:
            ptag += f"_stp{round(args.stop_pct * 100)}"
        if int(args.stop_repick_days) != 10:
            ptag += f"_rp{args.stop_repick_days}"
        if int(args.stop_repick_dom) > 0:
            ptag += f"_srdom{args.stop_repick_dom}"
    if args.cv_lag:
        ptag += f"_lag{args.cv_lag}"
    if args.pick in ("cvdown", "turnma") and args.trend_mode != "ma_down":
        ptag += f"_{args.trend_mode}"
        if args.trend_mode == "ma_stack":
            ptag += f"_{args.stack_order}"
        elif args.trend_mode == "strong":
            ptag += "_" + "-".join(x for x in str(args.trend_stack).split(",") if x.strip())
            wins_tag = "-".join(f"{w}x{win}" for w, win in slope_wins.items() if win > 0)
            if wins_tag and wins_tag != "20x10-60x5":
                ptag += f"_slma{wins_tag}"
            if args.trend_above > 0:
                ptag += f"_a{args.trend_above}"
            cross_tag = [x for x in str(args.trend_cross).split(",") if x.strip()]
            if len(cross_tag) == 2:
                ptag += "_x" + "-".join(cross_tag)
            if args.adx_min > 0:
                ptag += f"_adx{args.adx_min:g}"
            if args.adx_dir:
                ptag += "_adxdi"
            if args.max_near_high > 0:
                ptag += f"_nh{args.near_high_len}x{round(args.max_near_high * 1000)}"
            if pos_lo is not None:
                ptag += f"_pos{pos_len}x{round(pos_lo * 100)}-{round(pos_hi * 100)}"
            if bb_sd is not None:
                ptag += f"_bbz{args.bb_window}x{round(bb_zmax * 10)}"
    if args.pick in ("turn", "turnma") and args.turn_trend_first:
        ptag += "_tfirst"
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
    if args.min_mcap > 0:
        ptag += f"_mc{int(args.min_mcap)}"
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
