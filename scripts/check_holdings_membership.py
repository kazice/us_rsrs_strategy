# -*- coding: utf-8 -*-
"""逐期持仓反向校验：每期买入的个股在决策日/买入日是否真的在标普 500 内。

为什么需要单独校验：
    回测选股走的是 `pool.membership_mask()`，理论上不可能买到非成分票；
    但 2025-12-31 之后的价格 / 成分来自本地延展（`src='local_ext'`），
    如果延展脚本把「公告生效日」写早了，回测就会提前买入而不自知。
    所以这里做两层判定，第二层刻意绕开 membership_ext。

    A 层：`pool.membership()`（CRSP 权威段 + 延展段合并）
    B 层：基底 = CRSP 在 2025-12-31 的在册名单（第三方数据），
          再按 `data/curated/index/sp500_changes_since_2019.csv` 逐条 apply
          add / remove，完全不读 membership_ext。

判决（任一不过 = 提前买入）：
    信号日（月末最后交易日，收盘前决策）不在名单；
    补选日（牛市空仓后逐日重挑的成交日，若有）不在名单；
    持有期起始日不在名单。

用法：
    $env:PYTHONPATH='src'; python -m scripts.check_holdings_membership
    $env:PYTHONPATH='src'; python -m scripts.check_holdings_membership --dir reports/etf/xxx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from quant.data import pool  # noqa: E402

CHANGES = ROOT / "data" / "curated" / "index" / "sp500_changes_since_2019.csv"
OUT_CSV = ROOT / "reports" / "data_audit" / "holdings_membership_check.csv"
ALIAS = {"BF": "BF-B", "BRK": "BRK-B"}
CRSP_END = pd.Timestamp("2025-12-31")


def norm(t: str) -> str:
    k = str(t).upper().replace(".", "-").strip()
    return ALIAS.get(k, k)


def pick_dir(pattern: str | None, given: str | None) -> Path:
    base = ROOT / "reports" / "etf"
    if given:
        return ROOT / given if not Path(given).is_absolute() else Path(given)
    cands = [d for d in base.iterdir() if d.is_dir() and (pattern is None or pattern.strip("*") in d.name)]
    if not cands:
        raise SystemExit("reports/etf 下没有匹配的产出目录")
    return max(cands, key=lambda d: (d / "holds.csv").stat().st_mtime if (d / "holds.csv").exists() else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="校验每期持仓在买入时点是否为标普 500 成分股")
    ap.add_argument("--dir", default=None, help="产出目录；默认取 reports/etf 下最新的 holds.csv")
    ap.add_argument("--pattern", default="pool2026", help="默认按名字匹配定稿产出")
    ap.add_argument("--out", default=str(OUT_CSV))
    args = ap.parse_args()

    d0 = pick_dir(args.pattern, args.dir)
    holds = d0 / "holds.csv"
    if not holds.exists():
        raise SystemExit(f"{holds} 不存在")

    h = pd.read_csv(holds)
    h.columns = [c.strip() for c in h.columns]
    for c in ("信号日", "持有期起", "持有期止"):
        h[c] = pd.to_datetime(h[c])
    if "补选日" in h.columns:
        h["补选日"] = pd.to_datetime(h["补选日"])

    m = pool.membership()
    tk = pool.tickers()

    def sid_of(v):
        return None if pd.isna(v) else str(int(float(v)))

    def member_on(sid: str, ts: pd.Timestamp) -> bool:
        s = m[m["security_id"] == sid]
        return bool(((s["start"] <= ts) & (s["end"].isna() | (s["end"] >= ts))).any())

    def ticker_at(sid: str, ts: pd.Timestamp) -> str | None:
        s = tk[(tk["security_id"] == sid) & (tk["start"] <= ts)
               & (tk["end"].isna() | (tk["end"] >= ts))]
        return str(s["ticker"].iloc[-1]) if len(s) else None

    # ---- B 层：独立回放 2026 段（基底 = CRSP 2025-12-31 在册）----
    base = {norm(t) for t in (ticker_at(s, CRSP_END) for s in pool.members_on(CRSP_END)) if t}
    ch = pd.read_csv(CHANGES)
    ch["date"] = pd.to_datetime(ch["date"])
    ch = ch[ch["date"] > CRSP_END].sort_values("date")
    timeline, cur = [], set(base)
    for r in ch.itertuples():
        if isinstance(r.add, str):
            for t in r.add.split(","):
                if t.strip():
                    cur.add(norm(t))
        if isinstance(r.remove, str):
            for t in r.remove.split(","):
                if t.strip():
                    cur.discard(norm(t))
        timeline.append((r.date, set(cur)))

    def replay_on(ts: pd.Timestamp) -> set[str]:
        snap = set(base)
        for d, st in timeline:
            if d <= ts:
                snap = st
            else:
                break
        return snap

    rows = []
    for i, r in h.iterrows():
        sid = sid_of(r["个股ID"])
        rec = {
            "期序": i + 1, "信号日": r["信号日"].date(), "持有期起": r["持有期起"].date(),
            "持有期止": r["持有期止"].date(), "代码": r["个股"], "security_id": sid or "",
            "补选日": "", "成分判定": "", "B层回放": "", "结论": "",
        }
        if not sid:
            rec["代码"] = f"ETF:{r['板块']}"
            rec["结论"] = "板块 ETF（非成分股），跳过"
            rows.append(rec)
            continue
        sig_ok = member_on(sid, r["信号日"])
        st_ok = member_on(sid, r["持有期起"])
        rf = r["补选日"] if "补选日" in r.index else pd.NaT
        rf_ok = True if pd.isna(rf) else member_on(sid, rf)
        rec["补选日"] = "" if pd.isna(rf) else rf.date()
        rec["成分判定"] = (
            f"信号日{'Y' if sig_ok else 'N'}/"
            f"补选{'Y' if rf_ok else 'N'}/"
            f"起日{'Y' if st_ok else 'N'}")
        if r["信号日"] > CRSP_END:
            b_ok = norm(ticker_at(sid, r["信号日"]) or str(r["个股"])) in replay_on(r["信号日"])
            rec["B层回放"] = "Y" if b_ok else "N"
        else:
            b_ok = True
            rec["B层回放"] = "n/a"
        rec["结论"] = "OK" if (sig_ok and st_ok and rf_ok and b_ok) else "违规：买入时未列入"
        rows.append(rec)

    res = pd.DataFrame(rows)
    bad = res[res["结论"].str.startswith("违规")]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False, encoding="utf-8-sig")

    n_stock = int((res["security_id"] != "").sum())
    print(f"目录：{d0.name}")
    print(f"  期数 {len(res)}（个股 {n_stock} / ETF {len(res) - n_stock}）")
    print(f"  违规 {len(bad)} 期")
    if len(bad):
        for r in bad.itertuples():
            print(f"  ! 第{r.期序}期 {r.信号日} {r.代码} id={r.security_id} "
                  f"{r.成分判定} B层={r.B层回放}")
    print(f"  明细 → {out.relative_to(ROOT)}")
    return 1 if len(bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
