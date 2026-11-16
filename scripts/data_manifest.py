"""数据说明：把 data 分支里各类数据的日期范围与规模写成一份 MANIFEST.md。

为什么要这个文件
----------------
data 分支里全是 parquet，在 GitHub 上点开只有二进制，看不出覆盖到哪一天。
跑信号前第一件事就是确认行情是不是最新，所以把「数据到哪天了」固化成文档，
每次更新数据后**原地重写**，跟着数据一起提交，永远和实际内容同步。

产物
----
``<data_root>/MANIFEST.md`` —— 落在 data 分支根目录，由 workflow 回写时一并提交。

实现约定
--------
只依赖 ``duckdb``（CI 里本来就装），不引 pandas / pyarrow：
所有 parquet 都当外部表直接用 SQL 聚合，1124 个文件一次扫完，不需要逐文件读。

用法
----
python -m scripts.data_manifest          # 扫描并写入 MANIFEST.md
python -m scripts.data_manifest --check  # 只打印，不落盘
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

OUT_NAME = "MANIFEST.md"
STALE_DAYS = 5          # 行情最新日距今超过这么多自然日就告警


# ------------------------------------------------------------------ 基础
def resolve_root() -> Path:
    """数据根目录：优先 TA_DATA_ROOT，否则走 settings。"""
    env = os.getenv("TA_DATA_ROOT", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else (Path.cwd() / p)
    from quant.config import get_settings          # 延迟导入，--help 时不碰配置
    return Path(get_settings().paths.data_path)


def _as_date(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 2][:19], fmt).date()
        except ValueError:
            continue
    return None


def _d(x) -> str:
    d = _as_date(x)
    return d.isoformat() if d else "—"


def _n(x) -> str:
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return "—"


def _one(sql: str, params: list | None = None) -> tuple | None:
    """跑一条聚合 SQL，返回第一行；表不存在或字段缺失时返回 None。"""
    con = duckdb.connect()
    try:
        return con.execute(sql, params or []).fetchone()
    except Exception:                              # noqa: BLE001
        return None
    finally:
        con.close()


def _mb(p: Path) -> float:
    try:
        return p.stat().st_size / 1024 / 1024
    except OSError:
        return 0.0


# ------------------------------------------------------------------ 各数据集
def scan_bars(root: Path) -> dict:
    """日线：逐 symbol 取 min/max 日期，顺带统计落后于最新日的标的。"""
    d = root / "curated" / "bars_daily"
    files = list(d.glob("symbol=*/part.parquet"))
    if not files:
        return {"ok": False, "why": "curated/bars_daily 不存在或为空"}

    pat = (d / "symbol=*" / "part.parquet").as_posix()
    row = _one(
        "SELECT count(DISTINCT symbol), count(*), min(date), max(date) "
        f"FROM read_parquet('{pat}', hive_partitioning=true)"
    )
    if not row or row[1] in (None, 0):
        return {"ok": False, "why": "读不出任何日线"}

    symbols, rows, mn, mx = row
    lag = _one(
        "SELECT count(*), string_agg(symbol || '(' || strftime(mx, '%Y-%m-%d') || ')', ', ') "
        "FROM (SELECT symbol, max(date) AS mx "
        f"      FROM read_parquet('{pat}', hive_partitioning=true) "
        "      GROUP BY symbol HAVING max(date) < (SELECT max(date) "
        f"      FROM read_parquet('{pat}', hive_partitioning=true))) t"
    )
    lag_n = lag[0] if lag else 0
    lag_head = (lag[1] or "") if lag else ""
    if lag_head and lag_head.count(",") >= 7:
        lag_head = ", ".join(lag_head.split(", ")[:8]) + " …"

    return {"ok": True, "symbols": symbols, "rows": rows, "start": mn, "end": mx,
            "files": len(files), "size_mb": sum(_mb(f) for f in files),
            "lagging": lag_n, "lagging_head": lag_head}


def scan_turnover(root: Path) -> dict:
    p = root / "curated" / "fundamentals" / "turnover_daily.parquet"
    if not p.exists():
        return {"ok": False, "why": "curated/fundamentals/turnover_daily.parquet 不存在"}
    row = _one("SELECT count(DISTINCT symbol), count(*), min(date), max(date) "
               f"FROM read_parquet('{p.as_posix()}')")
    if not row:
        return {"ok": False, "why": "换手率面板读取失败"}
    return {"ok": True, "symbols": row[0], "rows": row[1],
            "start": row[2], "end": row[3], "size_mb": _mb(p)}


def scan_shares(root: Path) -> dict:
    p = root / "curated" / "fundamentals" / "shares_outstanding.parquet"
    if not p.exists():
        return {"ok": False, "why": "curated/fundamentals/shares_outstanding.parquet 不存在"}
    # end / asof 都是 duckdb 保留字（ASOF JOIN），必须加双引号
    row = _one('SELECT count(DISTINCT symbol), count(*), min(filed), max(filed), max("end") '
               f"FROM read_parquet('{p.as_posix()}')")
    if not row:
        return {"ok": False, "why": "总股本表读取失败（缺 filed/end 列？）"}
    return {"ok": True, "symbols": row[0], "rows": row[1],
            "filed_start": row[2], "filed_end": row[3], "end_end": row[4],
            "size_mb": _mb(p)}


def scan_universe(root: Path) -> dict:
    p = root / "meta" / "universe" / "universe_latest.parquet"
    if not p.exists():
        return {"ok": False, "why": "meta/universe/universe_latest.parquet 不存在"}
    row = _one(f'SELECT count(*), max("asof") FROM read_parquet(\'{p.as_posix()}\')')
    if not row:
        return {"ok": False, "why": "全市场快照读取失败"}
    return {"ok": True, "rows": row[0], "asof": row[1], "size_mb": _mb(p)}


def scan_state(root: Path) -> dict:
    p = root / "meta" / "state.json"
    if not p.exists():
        return {"ok": False, "why": "meta/state.json 不存在"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "why": f"解析失败 {type(exc).__name__}"}
    n = len(data) if isinstance(data, (dict, list)) else 0
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return {"ok": True, "entries": n, "mtime": mtime, "size_mb": _mb(p)}


def scan_ref(root: Path) -> dict:
    p = root / "ref" / "us_sector_holdings.csv"
    if not p.exists():
        return {"ok": False, "why": "不存在（运行时退回 config/ref 的静态快照，不影响使用）"}
    row = _one(f"SELECT count(*), max(as_of) FROM read_csv_auto('{p.as_posix()}', header=true)")
    if not row:
        return {"ok": False, "why": "CSV 解析失败"}
    return {"ok": True, "rows": row[0], "asof": row[1]}


# ------------------------------------------------------------------ 渲染
def render(bars: dict, turn: dict, shares: dict, uni: dict,
           state: dict, ref: dict) -> str:
    now = datetime.now(timezone.utc)
    L: list[str] = []
    add = L.append

    add("# 数据说明（MANIFEST）")
    add("")
    add("> 本文件由 `scripts/data_manifest.py` 自动生成，每次更新数据后**原地重写**，")
    add("> 跟着数据一起提交，所以永远和实际内容一致。**不要手工编辑**，改了下次也会被覆盖。")
    add(">")
    add(f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M UTC')}")
    add("")

    # ---- 概况
    add("## 概况")
    add("")
    add("| 数据集 | 路径 | 规模 | 起始 | 结束 | 体积 |")
    add("|---|---|---|---|---|---|")
    if bars.get("ok"):
        add(f"| 日线 | `curated/bars_daily/` | {_n(bars['symbols'])} 只 / {_n(bars['rows'])} 行 "
            f"| {_d(bars['start'])} | **{_d(bars['end'])}** | {bars['size_mb']:.1f} MB |")
    else:
        add("| 日线 | `curated/bars_daily/` | 缺失 | — | — | — |")
    if turn.get("ok"):
        add(f"| 20 日换手率面板 | `curated/fundamentals/turnover_daily.parquet` "
            f"| {_n(turn['symbols'])} 只 / {_n(turn['rows'])} 行 "
            f"| {_d(turn['start'])} | **{_d(turn['end'])}** | {turn['size_mb']:.1f} MB |")
    else:
        add("| 20 日换手率面板 | `curated/fundamentals/turnover_daily.parquet` | 缺失 | — | — | — |")
    if shares.get("ok"):
        add(f"| 总股本（季报） | `curated/fundamentals/shares_outstanding.parquet` "
            f"| {_n(shares['symbols'])} 只 / {_n(shares['rows'])} 行 "
            f"| 披露 {_d(shares['filed_start'])} | 披露 **{_d(shares['filed_end'])}** "
            f"| {shares['size_mb']:.1f} MB |")
    else:
        add("| 总股本（季报） | `curated/fundamentals/shares_outstanding.parquet` | 缺失 | — | — | — |")
    if uni.get("ok"):
        add(f"| 全市场快照 | `meta/universe/universe_latest.parquet` | {_n(uni['rows'])} 行 "
            f"| — | as of **{_d(uni['asof'])}** | {uni['size_mb']:.1f} MB |")
    else:
        add("| 全市场快照 | `meta/universe/universe_latest.parquet` | 缺失 | — | — | — |")
    add("")

    # ---- 新鲜度
    add("## 数据新鲜度")
    add("")
    end_d = _as_date(bars.get("end")) if bars.get("ok") else None
    if end_d:
        gap = (now.date() - end_d).days
        flag = "" if gap <= STALE_DAYS else f" —— ⚠ 超过 {STALE_DAYS} 天阈值"
        add(f"- 行情最新交易日：**{end_d.isoformat()}**（距今 {gap} 天）{flag}")
        if bars["lagging"]:
            add(f"- 落后于最新日的标的：{bars['lagging']} 只"
                + (f"，如 {bars['lagging_head']}" if bars.get("lagging_head") else ""))
        else:
            add("- 所有标的都跟到了最新交易日")
    else:
        add("- 行情缺失，无法判断新鲜度")
    if turn.get("ok") and end_d:
        t_end = _as_date(turn["end"])
        if t_end and t_end < end_d:
            add(f"- ⚠ 换手率面板只到 {t_end.isoformat()}，比行情落后 "
                f"{(end_d - t_end).days} 天，20 日均值会偏旧")
        elif t_end:
            add(f"- 换手率面板到 {t_end.isoformat()}，不低于行情最新日")
    add("")

    # ---- 明细
    add("## 明细")
    add("")
    add("### 日线 `curated/bars_daily/`")
    add("")
    add("按 `symbol=<TICKER>/part.parquet` 分区，文件内不含 symbol 列，靠 hive 分区还原。")
    add("")
    if bars.get("ok"):
        add(f"- 文件数：{_n(bars['files'])}")
        add(f"- 标的数：{_n(bars['symbols'])}，合计 {_n(bars['rows'])} 行")
        add(f"- 日期范围：{_d(bars['start'])} ~ {_d(bars['end'])}")
    else:
        add(f"- ❌ {bars.get('why')}")
    add("")

    add("### 换手率 `curated/fundamentals/turnover_daily.parquet`")
    add("")
    add("列 `date / symbol / turnover`；市值门槛反推股本、20 日均换手率排序都靠它。")
    add("")
    if turn.get("ok"):
        add(f"- 标的数：{_n(turn['symbols'])}，合计 {_n(turn['rows'])} 行")
        add(f"- 日期范围：{_d(turn['start'])} ~ {_d(turn['end'])}")
    else:
        add(f"- ❌ {turn.get('why')}")
    add("")

    add("### 总股本 `curated/fundamentals/shares_outstanding.parquet`")
    add("")
    add("列 `symbol / end / val / filed / form / fy / fp`；按**披露日 filed** 取用，避免前视。")
    add("")
    if shares.get("ok"):
        add(f"- 标的数：{_n(shares['symbols'])}，合计 {_n(shares['rows'])} 行")
        add(f"- 报告期覆盖到：{_d(shares['end_end'])}")
        add(f"- 披露日范围：{_d(shares['filed_start'])} ~ {_d(shares['filed_end'])}")
    else:
        add(f"- ❌ {shares.get('why')}")
    add("")

    add("### 全市场快照 `meta/universe/universe_latest.parquet`")
    add("")
    add("市值 / 价格兜底来源（股本取不到时用 `market_cap / price` 反推）。")
    add("")
    if uni.get("ok"):
        add(f"- 行数：{_n(uni['rows'])}")
        add(f"- 快照日：{_d(uni['asof'])}")
    else:
        add(f"- ❌ {uni.get('why')}")
    add("")

    add("### 增量状态 `meta/state.json`")
    add("")
    add("每只股票上次抓到哪一天，增量更新靠它断点续抓。")
    add("")
    if state.get("ok"):
        add(f"- 条目数：{_n(state['entries'])}")
        add(f"- 最后写入：{state['mtime'].strftime('%Y-%m-%d %H:%M UTC')}")
    else:
        add(f"- ❌ {state.get('why')}")
    add("")

    add("### 成分股快照 `ref/us_sector_holdings.csv`")
    add("")
    if ref.get("ok"):
        add(f"- 行数：{_n(ref['rows'])}，as of {_d(ref['asof'])}")
    else:
        add(f"- {ref.get('why')}")
    add("")

    # ---- 没放进来的
    add("## 这个分支里没有的东西")
    add("")
    add("| 没放 | 原因 |")
    add("|---|---|")
    add("| `raw/`（yfinance 原始响应缓存） | 体积大，属于抓取中间产物，删了能重抓 |")
    add("| `curated/options/` | 期权链，势垒研究用，模拟盘不读 |")
    add("| `curated/factors/`、`curated/index/` | 因子面板与指数成分变动，研究用 |")
    add("| 代码 | 在 `main` 分支，workflow 会把两边 checkout 到一起 |")
    add("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 data 分支的数据说明 MANIFEST.md")
    ap.add_argument("--check", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    # Windows 控制台默认 GBK，打印 ⚠ 会炸；CI 是 UTF-8，这里统一兜住
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                              # noqa: BLE001
        pass

    root = resolve_root()
    if not root.exists():
        print(f"[data_manifest] 数据根目录不存在：{root}")
        return 1

    bars = scan_bars(root)
    turn = scan_turnover(root)
    shares = scan_shares(root)
    uni = scan_universe(root)
    state = scan_state(root)
    ref = scan_ref(root)

    text = render(bars, turn, shares, uni, state, ref)
    if args.check:
        print(text)
    else:
        out = root / OUT_NAME
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"[data_manifest] 已写入 {out}")

    bits = []
    if bars.get("ok"):
        bits.append(f"bars {bars['symbols']} 只 {_d(bars['start'])}~{_d(bars['end'])}")
    if turn.get("ok"):
        bits.append(f"turnover {_d(turn['start'])}~{_d(turn['end'])}")
    if uni.get("ok"):
        bits.append(f"universe asof {_d(uni['asof'])}")
    print("[data_manifest] " + (" | ".join(bits) if bits else "没有扫到任何数据"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
