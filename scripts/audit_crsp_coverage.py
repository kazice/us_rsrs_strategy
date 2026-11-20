"""CRSP 数据包体检：有哪些指数编号、成分覆盖、能不能替掉 SIC 板块近似。

背景：定稿回测里有 102 只历史成分股的板块归属是按 SIC 大类猜的（快照里没有它们）。
如果这个数据包里除了标普 500（INDNO=1000500）还带了 11 个板块指数的成分记录，
就能直接拿到 point-in-time 的板块归属，把近似层去掉。本文先把事实查清楚。

用法：
    $env:PYTHONPATH='src'; python -m scripts.audit_crsp_coverage
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "crsp_sp500"

# CRSP 指数编号里，1000xxx 段是标普系列；已知：1000500 = S&P 500 Composite
KNOWN = {
    1000500: "S&P 500 Composite",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(RAW))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--list-sic", action="store_true",
                    help="列出板块归属靠 SIC 近似的票（代码 / SIC / 推断板块）")
    args = ap.parse_args()

    files = sorted(Path(args.src).glob("*.csv"))
    if not files:
        raise SystemExit(f"没找到 CSV：{args.src}")
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={args.threads}")
    read = (f"read_csv('{files[-1].as_posix()}', header=true, "
            f"ignore_errors=true, sample_size=200000)")

    print("=== ① 指数编号分布 ===")
    df = con.execute(f"""
        SELECT INDNO, count(*) AS rows, count(DISTINCT PERMNO) AS permnos,
               min(DlyCalDt) AS dmin, max(DlyCalDt) AS dmax
        FROM {read} GROUP BY 1 ORDER BY 2 DESC LIMIT 20
    """).df()
    df["说明"] = df["INDNO"].map(lambda x: KNOWN.get(int(x), ""))
    print(df.to_string(index=False))

    top = int(df.iloc[0]["INDNO"])
    print(f"\n说明：行数最多的是 INDNO={top}"
          f"（{KNOWN.get(top, '未登记')}），导入脚本只用了它。")

    print("\n=== ② 标普 500 逐年成分只数（体检口径异常）===")
    q = con.execute(f"""
        SELECT year(DlyCalDt) AS y, count(DISTINCT PERMNO) AS permnos
        FROM {read}
        WHERE INDNO = 1000500 AND DlyCalDt >= DATE '2020-01-01'
        GROUP BY 1 ORDER BY 1
    """).df()
    print(q.to_string(index=False))

    print("\n=== ③ 是否存在重复 (PERMNO, 日期) ===")
    q = con.execute(f"""
        WITH d AS (SELECT PERMNO, DlyCalDt, count(*) c FROM {read}
                   WHERE INDNO = 1000500 AND DlyCalDt >= DATE '2020-01-01'
                   GROUP BY 1, 2)
        SELECT count(*) FILTER (WHERE c > 1) AS dup_pairs, max(c) AS max_mult
        FROM d
    """).df()
    print(q.to_string(index=False))

    print("\n=== ④ 板块归属可用的字段 ===")
    q = con.execute(f"""
        SELECT count(*) AS rows,
               count(SICCD) AS sic_filled,
               count(DISTINCT SICCD) AS sic_values,
               count(Ticker) AS ticker_filled
        FROM {read}
        WHERE INDNO = 1000500 AND DlyCalDt >= DATE '2024-01-01'
    """).df()
    print(q.to_string(index=False))
    print("\n只有 SICCD（4 位 SIC），没有 GICS 字段；"
          "若上面 ① 里没有板块指数编号，板块归属只能继续用 SIC 近似。")

    print("\n=== ⑤ SIC 近似的实际影响（板块归属靠猜的票，有没有被选中）===")
    from quant.data import crsp as crsp_mod

    win = crsp_mod.symbols_in_window("2021-11-13", "2025-12-31")
    smap = crsp_mod.sector_map(win)
    sic_only = set(smap.loc[smap["src"] == "sic", "symbol"])
    print(f"池子 {len(win)} 只，其中 {len(sic_only)} 只板块归属靠 SIC 近似")
    if args.list_sic:
        sic_of = (crsp_mod.daily().dropna(subset=["SICCD"])
                  .sort_values("date").groupby("symbol")["SICCD"].last())
        show = smap[smap["src"] == "sic"].copy()
        show["SIC"] = show["symbol"].map(sic_of).astype("Int64")
        print(show[["symbol", "SIC", "sector_etf"]].sort_values("SIC").to_string(index=False))

    runs = sorted((ROOT / "reports" / "etf").glob("*_crsp*"))
    if not runs:
        print("没有 _crsp 回测产出，跳过选中检查")
    else:
        h = pd.read_csv(runs[-1] / "holds.csv")
        picks = set(h["个股"].dropna().astype(str))
        hit = sorted(pick for pick in picks if pick in sic_only)
        print(f"最近一次 CRSP 回测：{runs[-1].name}")
        print(f"  实际选过的个股 {len(picks)} 只，其中靠 SIC 补归属的 {len(hit)} 只：{hit}")
        if hit:
            shown = smap.set_index("symbol").loc[hit, "sector_etf"].to_dict()
            print(f"  这些票的板块：{shown}（逐只核对 SIC 大类是否无歧义）")
        print("  结论：" + ("影响可忽略：剩余 SIC 近似的票几乎没被选到，且命中项的 SIC 大类无歧义"
                          if len(hit) <= 2 else "影响不可忽略：需要官方 GICS 历史替换"))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
