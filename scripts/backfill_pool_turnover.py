"""补池内证券「成分期之前」的换手率，让 turn20/turn120 在买入日就算得出来。

为什么必须补：
    turn20 / turn120 是趋势闸的入闸条件，而换手率 = 成交量 ÷ 股本。
    股本（ShrOut）只有 CRSP 的「成分期内」行才有，外部补的预热段（src=external）
    只有价格和成交量 —— 于是后纳入指数的票在纳入后 60~120 个交易日里 turn120 全是 NaN，
    这几十只票在回测里等于白买。价格补得再全，缺换手率一样进不了闸。

数据源（实测可用，2026-11）：
    东方财富美股日线接口 push2his.eastmoney.com，fields2 里 f61 就是**每日换手率**（百分数）。
    IBKR/APP/CVNA/EME/CRH 与 CRSP 的 turnover（DlyVol/ShrOut）对比：
        ratio 中位 0.99~1.00、cv ≤ 2%、相关系数 0.999  → 口径一致，直接 /100 即可用。
    注意 requests 的 TLS 指纹被封（RemoteDisconnected），必须用 curl_cffi 模拟 chrome。
    腾讯 web.ifzq.gtimg.cn 的 usfqkline 也带换手率字段，作为备用。

口径与校验：
    只用重叠区间（CRSP 有 turnover 的日子）算 ratio = CRSP.turnover ÷ 外部换手率，
    要求 ratio 中位在 [0.5, 2.0]、cv ≤ 0.20、相关 ≥ 0.90；不过关的不并入，记 review。
    与价格回补一样，external 段的 shares / mcap 仍然是 NaN —— 那段不可交易，只喂指标。

产出：
    data/raw/external/{security_id}_{ticker}_turnover.parquet   date / turnover / volume / amount
    data/curated/sp500_pool/turnover_backfill_report.csv        逐票校验结论

用法：
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_turnover --dry-run
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_turnover --limit 5
    $env:PYTHONPATH='src'; python -m scripts.backfill_pool_turnover
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

ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "data" / "raw" / "external"
REPORT = ROOT / "data" / "curated" / "sp500_pool" / "turnover_backfill_report.csv"

PREFIXES = ("105", "106", "107")     # 纳斯达克 / 纽交所 / 美交所
BEG = "20180101"                     # 池子 bars 起点是 2019-01-02，往前留一年余量
MIN_OVERLAP = 5
RATIO_RANGE = (0.5, 2.0)
MAX_CV = 0.20
MIN_CORR = 0.90
EXTRA_ALIAS = {"PSKY": ["PARA"], "WBD": ["DISCA"]}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}

_BANNED_UNTIL = 0.0     # 被东财封 IP 时的冷却截止（time.time()）


SINA_URL = ("https://stock.finance.sina.com.cn/usstock/api/json_v2.php/"
            "US_MinKService.getDailyK?symbol={tk}&___qn=3")
SINA_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def sina_daily(tk: str, tries: int = 2) -> pd.DataFrame:
    """新浪美股日线（close 是**未复权**价，volume 是原始量）。"""
    import requests

    for i in range(tries):
        try:
            r = requests.get(SINA_URL.format(tk=tk), headers=SINA_UA, timeout=25,
                             proxies={"http": None, "https": None})
            rows = r.json()
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame([{"date": x["d"], "close": float(x["c"]),
                                "vol": float(x["v"])} for x in rows])
            if df.empty:
                return pd.DataFrame()
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date").sort_index()
        except Exception:
            time.sleep(2.0 * (i + 1))
    return pd.DataFrame()


def sina_pack(sid: str, tk: str, crsp: pd.DataFrame,
              ext: pd.DataFrame) -> tuple[pd.DataFrame | None, dict]:
    """用「新浪成交量 ÷ CRSP 股本」近似外部段换手率，并做两项自检。

    自检 1（量口径）：新浪量与 CRSP 段 DlyVol 在重叠期比，得 k。k≈1 说明两边都是原始量，
        可以拿 CRSP 的 ShrOut 直接除；k≠1 就按 k 缩放，避免拿复权量除原始股本。
    自检 2（拆股）：股本取的是「纳入日」那个时点的股数，外部段中间若拆过股，量翻倍而股本
        没跟着变，换手率会整体差一个倍数。判据用**价格比值的跳变**（新浪原始 close ÷ 复权
        adj_close）：拆股时新浪价减半而复权价连续，比值阶跃；真实暴涨暴跌两者同步，比值不动。
        量级台阶不能当判据——疫情那种正常放量会误杀。
    """
    d = sina_daily(tk)
    if d.empty:
        return None, {"status": "sina_no_data", "overlap": 0, "ratio": np.nan, "cv": np.nan}
    v, px = d["vol"], d["close"]
    cvol = crsp.set_index("date")["volume"].dropna()
    common = cvol.index.intersection(v.index)
    if len(common) < MIN_OVERLAP:
        return None, {"status": "overlap_too_small", "overlap": len(common),
                      "ratio": np.nan, "cv": np.nan}
    k = float((cvol.loc[common] / v.loc[common].replace(0, np.nan)).median())
    shares = pd.to_numeric(crsp["shares"], errors="coerce").dropna()
    if not len(shares) or k <= 0:
        return None, {"status": "no_shares", "overlap": len(common), "ratio": k, "cv": np.nan}

    # 拆股检测：只在外部段内看，比值常数的部分不敏感
    adj = ext.set_index("date")["adj_close"].reindex(px.index)
    pr = (px / adj).replace([np.inf, -np.inf], np.nan).dropna()
    jump = float((pr / pr.shift(1)).dropna().apply(np.log).abs().max()) if len(pr) > 5 else 0.0

    sub = (v.reindex(pd.DatetimeIndex(ext["date"])) * k).dropna()
    turn = (sub / float(shares.iloc[0])).rename("turnover").dropna()
    ok = (0.5 <= k <= 2.0) and jump < float(np.log(1.30)) and len(turn) >= 20
    if ok:
        status = "ok"
    elif jump >= float(np.log(1.30)):
        status = "split_suspect"
    elif len(turn) < 20:
        status = "ext_too_short"
    else:
        status = "ratio_mismatch"
    return (turn.to_frame() if ok else None,
            {"status": status, "overlap": len(common), "ratio": round(k, 4),
             "cv": round(jump, 4)})


def local_pack(sid: str, tk: str, crsp: pd.DataFrame,
               ext: pd.DataFrame) -> tuple[pd.DataFrame | None, dict]:
    """离线兜底：外部段成交量本来就有（backfill_pool_history 抓的 Yahoo 量），缺的只是股本。

    turnover = 外部量 × k ÷ CRSP 股本，k 用「重叠区 CRSP 量 ÷ 外部量」的中位数标定 ——
    Yahoo 的成交量是拆股调整后的，CRSP 的 DlyVol 是原始量，两者差一个累积拆股倍数，
    k 正好把它折回来（PANW 的 k≈0.5 就是 2024-12 那次 2:1 拆股）。
    精度用东财真换手率对过 10 只：turn20 偏离中位 0.4%~3.4%，日频 ratio 中位 0.99~1.03。

    拆股自检：外部量乘 k 之后是「当时口径」，但股本取的是纳入日的股数；若外部段内部
    自己拆过股，两边就差倍数。用 raw 文件里的 原始close ÷ 复权close 看跳变即可识别。
    """
    files = sorted(p for p in EXT_DIR.glob(f"{sid}_*.parquet") if "_turnover" not in p.name)
    if not files:
        return None, {"status": "no_raw_file", "overlap": 0, "ratio": np.nan, "cv": np.nan}
    raw = pd.read_parquet(files[-1])
    raw.index = pd.to_datetime(raw.index)
    if "volume" not in raw.columns or ext.empty:
        return None, {"status": "no_raw_volume", "overlap": 0, "ratio": np.nan, "cv": np.nan}
    c = crsp.set_index("date")
    ov = c.index.intersection(raw.index)
    ov = ov[c.loc[ov, "volume"].notna() & raw.loc[ov, "volume"].notna()]
    if len(ov) < MIN_OVERLAP:
        return None, {"status": "overlap_too_small", "overlap": len(ov),
                      "ratio": np.nan, "cv": np.nan}
    k = float((c.loc[ov, "volume"] / raw.loc[ov, "volume"].replace(0, np.nan)).median())
    shares = pd.to_numeric(c["shares"], errors="coerce").dropna()
    if not len(shares) or not np.isfinite(k) or k <= 0:
        return None, {"status": "no_shares", "overlap": len(ov), "ratio": k, "cv": np.nan}

    d = pd.DatetimeIndex(ext["date"])
    sub = raw["volume"].reindex(d)
    if "adj_close_crsp" in raw.columns:
        pr = (raw["close"] / raw["adj_close_crsp"]).replace([np.inf, -np.inf], np.nan)
        pr = pr.reindex(d).dropna()
        jump = float((pr / pr.shift(1)).dropna().apply(np.log).abs().max()) if len(pr) > 5 else 0.0
    else:
        jump = 0.0
    turn = (sub * k / float(shares.iloc[0])).rename("turnover").dropna()
    # k 可以很小：SMCI 10:1、SNDK 8:1 拆股后 Yahoo 量是原始量的 10 倍/8 倍，
    # k≈0.1 正是把复权量折回原始量的比例，不是数据错误
    ok = (0.05 <= k <= 20.0) and jump < float(np.log(1.30)) and len(turn) >= 20
    if ok:
        status = "ok"
    elif jump >= float(np.log(1.30)):
        status = "split_suspect"
    elif len(turn) < 20:
        status = "ext_too_short"
    else:
        status = "ratio_mismatch"
    return (turn.to_frame() if ok else None,
            {"status": status, "overlap": len(ov), "ratio": round(k, 4), "cv": round(jump, 4)})


def resolve(tk: str) -> list[str]:
    """用东财搜索接口把 ticker 换成 secid（105=纳斯达克 / 106=纽交所 / 107=美交所）。

    比盲试三个前缀快一倍多，也顺手挡掉同名的不相关品种（只认代码完全相同的）。
    """
    from curl_cffi import requests as cr

    u = ("https://searchapi.eastmoney.com/api/suggest/get"
         f"?input={tk}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=8")
    try:
        r = cr.get(u, headers=UA, impersonate="chrome", timeout=15)
        data = ((r.json() or {}).get("QuotationCodeTable") or {}).get("Data") or []
    except Exception:
        return []
    out = []
    for it in data:
        qid = it.get("QuoteID") or ""
        parts = qid.split(".")
        if len(parts) == 2 and parts[0] in PREFIXES and parts[1].upper() == tk.upper():
            out.append(qid)
    return out


def kline(secid: str, tries: int = 1) -> tuple[str, pd.DataFrame]:
    """东财日线：date / close / volume / amount / turn_pct（换手率，百分数）。

    默认**只请求一次**：东财对高频请求会做 IP 级封禁（整族域名直接断连，不是 403），
    重试只会加重封禁。逐只排查时一只一只来，失败就记原因、跳过，不要连打。
    """
    from curl_cffi import requests as cr

    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
         f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f56,f57,f61"
         f"&klt=101&fqt=1&beg={BEG}&end=20500101")
    kline.last_err = ""
    for i in range(tries):
        try:
            r = cr.get(u, headers=UA, impersonate="chrome", timeout=25)
            d = (r.json() or {}).get("data") or {}
            rows = d.get("klines") or []
            if not rows:
                return "", pd.DataFrame()
            recs = []
            for s in rows:
                p = s.split(",")
                try:
                    recs.append({"date": p[0], "close": float(p[2]), "volume": float(p[3]),
                                 "amount": float(p[4]), "turn_pct": float(p[5])})
                except (ValueError, IndexError):
                    continue
            df = pd.DataFrame(recs)
            if df.empty:
                return d.get("name", ""), df
            df["date"] = pd.to_datetime(df["date"])
            return d.get("name", ""), df.set_index("date").sort_index()
        except Exception as exc:
            global _BANNED_UNTIL
            kline.last_err = f"{type(exc).__name__}: {str(exc)[:70]}"
            if "Connection" in type(exc).__name__:
                _BANNED_UNTIL = time.time() + 120
            if i + 1 < tries:
                time.sleep(2.0 * (i + 1))
    return "", pd.DataFrame()


def targets(force: bool) -> pd.DataFrame:
    """external 段没换手率、且还没补过的证券。"""
    b = pool.bars()
    ext = b[b["src"] == "external"]
    recs = []
    u = pool.universe().set_index("security_id")
    for sid, g in ext.groupby("security_id", sort=False):
        if not force and list(EXT_DIR.glob(f"{sid}_*_turnover.parquet")):
            continue
        if g["turnover"].notna().any():
            continue
        first = g["date"].min()
        recs.append({"security_id": sid, "first_ext": first, "ext_rows": len(g),
                     "ticker": u.at[sid, "ticker"] if sid in u.index else ""})
    return pd.DataFrame(recs)


def candidates(sid: str, tk: str) -> list[str]:
    """该票可能用过的 ticker：CRSP 的当前代码 + 改名前的旧代码。"""
    t = pool.tickers()
    hist = t[t["security_id"] == sid]["ticker"].dropna().tolist()
    out = list(dict.fromkeys([tk, *hist, *EXTRA_ALIAS.get(tk, [])]))
    return [x for x in out if x]


def verify(ext: pd.Series, crsp: pd.Series) -> dict:
    common = ext.index.intersection(crsp.index)
    common = common[ext.loc[common].notna() & crsp.loc[common].notna()]
    if len(common) < MIN_OVERLAP:
        return {"overlap": len(common), "ratio": np.nan, "cv": np.nan, "corr": np.nan,
                "status": "overlap_too_small"}
    a, b = crsp.loc[common], ext.loc[common]
    ratio = (a / b.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
    if ratio.empty:
        return {"overlap": len(common), "ratio": np.nan, "cv": np.nan, "corr": np.nan,
                "status": "ratio_empty"}
    med, cv = float(ratio.median()), float(ratio.std() / abs(ratio.median())) if ratio.median() else np.inf
    corr = float(a.corr(b))
    ok = (RATIO_RANGE[0] <= med <= RATIO_RANGE[1]) and cv <= MAX_CV and corr >= MIN_CORR
    return {"overlap": len(common), "ratio": round(med, 4), "cv": round(cv, 4),
            "corr": round(corr, 4), "status": "ok" if ok else "ratio_mismatch"}


def main() -> int:
    ap = argparse.ArgumentParser(description="补外部预热段的换手率（东财美股日线）")
    ap.add_argument("--one", default="", help="只试这些 ticker（逗号分隔），逐只排查用")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只")
    ap.add_argument("--dry-run", action="store_true", help="只列目标，不抓")
    ap.add_argument("--force", action="store_true", help="已补过的也重抓")
    ap.add_argument("--source", choices=("local", "em", "sina"), default="local",
                    help="local=外部量×k÷股本（离线，推荐）；em=东财真换手率（会封 IP）；"
                         "sina=新浪量÷股本（只覆盖 2021 后）")
    ap.add_argument("--keep-going", action="store_true",
                    help="某只失败后继续试下一只（默认失败即停，避免连打被封）")
    ap.add_argument("--sleep", type=float, default=6.0, help="每只之间的间隔秒数")
    args = ap.parse_args()

    want = [x.strip().upper() for x in args.one.split(",") if x.strip()]
    tg = targets(args.force or bool(want))
    if want:
        tg = tg[tg["ticker"].str.upper().isin(want)]
    if args.limit:
        tg = tg.head(args.limit)
    print(f"待试：{len(tg)} 只")
    if len(tg):
        print(tg.to_string(index=False))
    if args.dry_run or tg.empty:
        return 0

    bars = pool.bars(src="crsp")
    crsp_turn = {}
    for sid, g in bars.groupby("security_id", sort=False):
        crsp_turn[sid] = g.set_index("date")["turnover"].dropna()

    EXT_DIR.mkdir(parents=True, exist_ok=True)
    allb = pool.bars()
    rep = []
    for i, row in enumerate(tg.itertuples(), 1):
        wait = _BANNED_UNTIL - time.time()
        if wait > 0:
            print(f"  冷却 {wait:.0f}s 后继续（东财限流）")
            time.sleep(wait)
        sid, tk = row.security_id, row.ticker
        if args.source in ("sina", "local"):
            extg = allb[(allb["security_id"] == sid) & (allb["src"] == "external")]
            fn = sina_pack if args.source == "sina" else local_pack
            pack, chk = fn(sid, tk, bars[bars["security_id"] == sid], extg)
            if pack is not None:
                pack.to_parquet(EXT_DIR / f"{sid}_{tk}_turnover.parquet")
            rep.append({"security_id": sid, "ticker": tk, "status": chk["status"],
                        "rows": 0 if pack is None else len(pack),
                        "overlap": chk["overlap"], "ratio": chk["ratio"], "cv": chk["cv"],
                        "corr": np.nan, "source": f"{args.source}:{tk}", "error": ""})
            print(f"  [{i}/{len(tg)}] {'OK ' if pack is not None else '!! '}{tk:6s} "
                  f"重叠={chk['overlap']:3d} k={chk['ratio']} 台阶={chk['cv']} {chk['status']}")
            if pack is None and not args.keep_going:
                print("  失败即停。加 --keep-going 可连续试下一只。")
                break
            time.sleep(args.sleep)
            continue
        got = None
        tried = []
        for cand in candidates(sid, tk):
            secids = resolve(cand) or [f"{pre}.{cand}" for pre in PREFIXES]
            for secid in secids:
                name, df = kline(secid)
                tried.append(secid)
                if len(df) > MIN_OVERLAP:
                    got = (secid, cand, name, df)
                    break
                time.sleep(0.3)
            if got:
                break
        if not got:
            err = kline.last_err or "no_klines"
            rep.append({"security_id": sid, "ticker": tk, "status": "no_data",
                        "rows": 0, "overlap": 0, "ratio": np.nan, "cv": np.nan,
                        "corr": np.nan, "source": ",".join(tried), "error": err})
            print(f"  [{i}/{len(tg)}] FAIL {tk:6s} 试过 {tried} → {err}")
            if not args.keep_going:
                print("  失败即停（避免连打被封）。确认已解封后可用 --one 单只重试，"
                      "或加 --keep-going 连续试。")
                break
            time.sleep(args.sleep)
            continue

        pre, cand, name, df = got
        ext_turn = (df["turn_pct"] / 100.0).rename("turnover")
        chk = verify(ext_turn, crsp_turn.get(sid, pd.Series(dtype=float)))
        out = df.assign(turnover=ext_turn).dropna(subset=["turnover"])
        out.attrs["security_id"] = sid
        status = chk["status"]
        if status == "ok":
            out.to_parquet(EXT_DIR / f"{sid}_{cand}_turnover.parquet")
        rep.append({"security_id": sid, "ticker": tk, "status": status, "rows": len(out),
                    "overlap": chk["overlap"], "ratio": chk["ratio"], "cv": chk["cv"],
                    "corr": chk["corr"], "source": f"em:{pre}", "error": ""})
        mark = "OK " if status == "ok" else "!! "
        print(f"  [{i}/{len(tg)}] {mark}{tk:6s} {name[:10]:10s} secid={pre} 行={len(out):4d} "
              f"重叠={chk['overlap']:3d} ratio={chk['ratio']} cv={chk['cv']} "
              f"corr={chk['corr']} {status}")
        time.sleep(args.sleep)

    rpt = pd.DataFrame(rep)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    rpt.to_csv(REPORT, index=False, encoding="utf-8-sig")
    ok = int((rpt["status"] == "ok").sum())
    print(f"\n成功 {ok}/{len(rpt)} 只 → {EXT_DIR}")
    print(f"报告 → {REPORT}")
    if len(rpt) and ok < len(rpt):
        print("\n未通过（需人工看）：")
        cols = [c for c in ("ticker", "status", "source", "error", "overlap", "ratio", "cv")
                if c in rpt.columns]
        print(rpt[rpt["status"] != "ok"][cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
