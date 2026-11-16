"""候选池：11 个 SPDR 板块 ETF 的成分股快照 + 调入调出变更日志。

为什么必须有这一层
------------------
候选池原来是 ``config/ref/us_sector_holdings.csv`` 一张静态快照（2026-11-11）。
对回测它只是「幸存者偏差」的注脚，但对实盘是四个会直接吃亏的问题：

1. **新纳入的票进不来**：标普 500 每次调整都有新增，快照不更新就永远看不到它们，
   而新纳入往往伴随指数基金的被动买盘；
2. **被剔除的票出不去**：还留在池子里，就可能买到正在被指数基金抛售的票；
3. **新纳入的票没有本地行情**：``load_members`` 会按 ``store.list_symbols()`` 过滤，
   没有 bar 的新票会被**静默丢掉**——这比报错更危险，所以要显式补行情；
4. **持仓被调出**：调仓日必须强制换出，且推送里要说清原因，不能让人以为只是排名变化。

数据源与可及性
--------------
唯一权威源是 SSGA 官方每日 holdings（``build_sector_holdings.fetch``，11 个 xlsx）。
实测**国内网络不可达**（TLS 握手超时），Wikipedia / GitHub raw 也不可达，
所以刷新只在 GitHub Actions（美区）上做；本地或 CI 拉不到时一律沿用上一份快照，
把「快照几天没更新」显示在推送里，绝不因为刷新失败而中断信号。

产物（放 data 分支，跟着行情一起走）
------------------------------------
``ref/us_sector_holdings.csv``              最新快照（与 config/ref 同结构）
``ref/history/us_sector_holdings_<asof>.csv`` 每次刷新留一份，用于事后复盘
``ref/index_changes.csv``                   调入/调出/换板块的追加日志
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quant.data import store

log = logging.getLogger(__name__)

POOL_FILE = "us_sector_holdings.csv"
CHANGES_FILE = "index_changes.csv"
CHANGE_COLUMNS = ["date", "action", "symbol", "sector_etf", "sector_name", "note"]


def repo_pool_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "ref" / POOL_FILE


def data_ref_dir() -> Path:
    return store.data_root() / "ref"


def pool_path() -> Path:
    """优先用 data 分支里被刷新过的那份，没有才退回仓库里的静态快照。"""
    live = data_ref_dir() / POOL_FILE
    return live if live.exists() else repo_pool_path()


def normalize_symbol(sym: str) -> str:
    """把 SSGA 的点号代码换成行情源通用的横杠形式：BRK.B → BRK-B、BF.B → BF-B。

    不换的后果不是报错而是**静默消失**：本地行情文件按 yahoo 的 BRK-B 命名，
    拿 BRK.B 去查永远查不到，于是伯克希尔（标普 500 权重前 10）从来没进过候选池。
    """
    return str(sym).strip().upper().replace(".", "-")


def load() -> pd.DataFrame:
    path = pool_path()
    if not path.exists():
        raise SystemExit(f"成分股快照不存在：{path}")
    df = pd.read_csv(path)
    df["symbol"] = df["symbol"].map(normalize_symbol)
    return df.drop_duplicates("symbol").reset_index(drop=True)


def snapshot_age_days(today: dt.date | None = None) -> int | None:
    """快照的 as_of 距今几天。None = 表里没有 as_of。"""
    df = load()
    if "as_of" not in df.columns or df["as_of"].isna().all():
        return None
    as_of = pd.to_datetime(df["as_of"].astype(str), errors="coerce").max()
    if pd.isna(as_of):
        return None
    today = today or dt.date.today()
    return (pd.Timestamp(today) - as_of).days


def changes_path() -> Path:
    return data_ref_dir() / CHANGES_FILE


def read_changes() -> pd.DataFrame:
    path = changes_path()
    if not path.exists():
        return pd.DataFrame(columns=CHANGE_COLUMNS)
    df = pd.read_csv(path)
    for c in CHANGE_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df[CHANGE_COLUMNS]


def recent_changes(days: int = 45, today: dt.date | None = None) -> pd.DataFrame:
    df = read_changes()
    if df.empty:
        return df
    today = today or dt.date.today()
    cutoff = pd.Timestamp(today - dt.timedelta(days=days))
    return df[df["date"] >= cutoff].sort_values(["date", "action"], ascending=[False, True])


def _append_changes(rows: list[dict]) -> None:
    if not rows:
        return
    path = changes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).reindex(columns=CHANGE_COLUMNS)
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")


@dataclass
class RefreshResult:
    ok: bool
    as_of: str = ""
    n_symbols: int = 0
    added: list[str] = None          # type: ignore[assignment]
    removed: list[str] = None        # type: ignore[assignment]
    moved: list[str] = None          # type: ignore[assignment]
    new_symbols: list[str] = None    # type: ignore[assignment]  需要补行情的
    message: str = ""

    def __post_init__(self) -> None:
        self.added = self.added or []
        self.removed = self.removed or []
        self.moved = self.moved or []
        self.new_symbols = self.new_symbols or []

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.moved)


def diff(old: pd.DataFrame, new: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """返回 (新增, 剔除, 换板块)。换板块的票在集合层面没变，但归属变了，要单独记。"""
    o = old.set_index("symbol")["sector_etf"].to_dict()
    n = new.set_index("symbol")["sector_etf"].to_dict()
    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))
    moved = sorted(s for s in set(o) & set(n) if o[s] != n[s])
    return added, removed, moved


def refresh(today: dt.date | None = None, dry_run: bool = False) -> RefreshResult:
    """拉 SSGA 最新 holdings，落快照 + 记变更 + 返回需要补行情的票。

    任一板块拉取失败就整体放弃（半新半旧的池子会让板块归属自相矛盾），沿用旧快照。
    """
    from scripts.build_sector_holdings import SECTORS, fetch

    parts = []
    failed = []
    for code in SECTORS:
        try:
            parts.append(fetch(code))
        except Exception as exc:                     # noqa: BLE001
            failed.append(f"{code}({type(exc).__name__})")
    if failed:
        return RefreshResult(False, message=f"刷新失败，沿用旧快照：{' '.join(failed)}")
    if not parts:
        return RefreshResult(False, message="刷新失败：一个板块都没取到")

    new = pd.concat(parts, ignore_index=True)
    new["symbol"] = new["symbol"].map(normalize_symbol)
    new = new.sort_values("weight", ascending=False).drop_duplicates("symbol")
    new = new.sort_values(["sector_etf", "weight"], ascending=[True, False]).reset_index(drop=True)
    as_of = str(new["as_of"].dropna().iloc[0]) if new["as_of"].notna().any() else ""

    try:
        old = load()
        added, removed, moved = diff(old, new)
    except SystemExit:
        old = pd.DataFrame(columns=new.columns)
        added, removed, moved = sorted(new["symbol"]), [], []

    known = set(store.list_symbols())
    new_symbols = sorted(set(new["symbol"]) - known)

    if dry_run:
        return RefreshResult(True, as_of, len(new), added, removed, moved, new_symbols,
                             message="dry-run：未落盘")

    ref = data_ref_dir()
    ref.mkdir(parents=True, exist_ok=True)
    new.to_csv(ref / POOL_FILE, index=False, encoding="utf-8-sig")
    if as_of:
        hist = ref / "history" / f"us_sector_holdings_{as_of.replace('-', '')}.csv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        if not hist.exists():
            new.to_csv(hist, index=False, encoding="utf-8-sig")

    day = (today or dt.date.today()).isoformat()
    name_of = dict(zip(new["symbol"], new["sector_name"]))
    sec_of = dict(zip(new["symbol"], new["sector_etf"]))
    oldname = dict(zip(old["symbol"], old["sector_name"])) if len(old) else {}
    rows = []
    for s in added:
        rows.append({"date": day, "action": "add", "symbol": s,
                     "sector_etf": sec_of.get(s, ""), "sector_name": name_of.get(s, ""),
                     "note": "新纳入板块 ETF"})
    for s in removed:
        rows.append({"date": day, "action": "delete", "symbol": s,
                     "sector_etf": (old.set_index("symbol")["sector_etf"].get(s, "")
                                    if len(old) else ""),
                     "sector_name": oldname.get(s, ""), "note": "已移出板块 ETF"})
    for s in moved:
        rows.append({"date": day, "action": "move", "symbol": s,
                     "sector_etf": sec_of.get(s, ""), "sector_name": name_of.get(s, ""),
                     "note": "板块归属变更"})
    _append_changes(rows)

    msg = (f"快照 as of {as_of}，{len(new)} 只；新增 {len(added)}、剔除 {len(removed)}、"
           f"换板块 {len(moved)}；本地无行情需补 {len(new_symbols)}")
    log.info(msg)
    return RefreshResult(True, as_of, len(new), added, removed, moved, new_symbols, msg)
