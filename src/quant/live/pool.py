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
CHANGES_MD = "INDEX_CHANGES.md"          # 同一份变更历史的可读版，给人看
_LABEL = {"add": "纳入", "delete": "调出", "move": "换板块"}
CHANGE_COLUMNS = ["date", "action", "symbol", "sector_etf", "sector_name", "note"]


def repo_pool_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "ref" / POOL_FILE


def data_ref_dir() -> Path:
    return store.data_root() / "ref"


def pool_path() -> Path:
    """优先用 data 分支里被刷新过的那份，没有才退回仓库里的静态快照。"""
    live = data_ref_dir() / POOL_FILE
    return live if live.exists() else repo_pool_path()


# 不带类别后缀的写法：官方变更表写 "BRK"/"BF"，行情源是 "BRK-B"/"BF-B"。
# 不归一的话 PIT 回滚会凭空多出一只 "BRK"，与快照里的 "BRK-B" 并存且没有行情。
_CLASS_ALIAS = {"BRK": "BRK-B", "BF": "BF-B", "BRK.B": "BRK-B", "BF.B": "BF-B"}


def normalize_symbol(sym: str) -> str:
    """把 SSGA 的点号代码换成行情源通用的横杠形式：BRK.B → BRK-B、BF.B → BF-B。

    不换的后果不是报错而是**静默消失**：本地行情文件按 yahoo 的 BRK-B 命名，
    拿 BRK.B 去查永远查不到，于是伯克希尔（标普 500 权重前 10）从来没进过候选池。
    """
    k = str(sym).strip().upper().replace(".", "-")
    return _CLASS_ALIAS.get(k, k)


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


# ---------------------------------------------------------------- point-in-time 成分
# 为什么需要这一层：
#   `load()` 只给「最新快照」。拿它去算历史某天（live_audit --replay、回测对账、
#   事后复盘）会把「那天之后才纳入指数」的票当成当时可买——2026-04-30 选出 6 月才
#   纳入的 MRVL 就是这么来的。前视会同时污染选股和归因，比数据缺失更难发现。
#   所以凡是带 asof 的调用，一律走 `members_asof()`。
OFFICIAL_CHANGES = (Path(__file__).resolve().parents[3]
                    / "data" / "curated" / "index" / "sp500_changes_since_2019.csv")


def snapshot_asof(df: pd.DataFrame | None = None) -> pd.Timestamp | None:
    """快照自带的 as_of（SSGA 落盘日期），没有就 None。"""
    df = load() if df is None else df
    if "as_of" not in df.columns or df["as_of"].isna().all():
        return None
    ts = pd.to_datetime(df["as_of"].astype(str), errors="coerce").max()
    return None if pd.isna(ts) else pd.Timestamp(ts).normalize()


def history_snapshots() -> list[tuple[pd.Timestamp, Path]]:
    """``data/ref/history/us_sector_holdings_YYYYMMDD.csv`` 存档，按日期升序。

    refresh() 每次成功刷新都会留一份，所以历史日期优先用存档——它连「当时的板块归属」
    都是原样的，比从变更表倒推可靠。
    """
    root = data_ref_dir() / "history"
    if not root.exists():
        return []
    out: list[tuple[pd.Timestamp, Path]] = []
    for p in root.glob("us_sector_holdings_*.csv"):
        ts = pd.to_datetime(p.stem.rsplit("_", 1)[-1], format="%Y%m%d", errors="coerce")
        if pd.notna(ts):
            out.append((pd.Timestamp(ts), p))
    return sorted(out)


def _read_snapshot_file(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["symbol"] = df["symbol"].map(normalize_symbol)
    return df.drop_duplicates("symbol").reset_index(drop=True)


def read_official_changes(path: Path | None = None) -> pd.DataFrame:
    """官方成分变更表（date / add / remove）展开成长表：date / action / symbol。

    这是**公告层面**的权威变更（含 2019 年以来的每次调整），与 ``index_changes.csv``
    （本仓库 refresh 时自己 diff 出来的）互补：前者能倒推任意历史日，后者能记板块移动。
    """
    p = Path(path) if path is not None else OFFICIAL_CHANGES
    if not p.exists():
        return pd.DataFrame(columns=["date", "action", "symbol"])
    df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    rows = []
    for r in df.itertuples():
        for col, act in (("add", "add"), ("remove", "delete")):
            v = getattr(r, col, "")
            if isinstance(v, str):
                for t in v.split(","):
                    if t.strip():
                        rows.append({"date": r.date, "action": act,
                                     "symbol": normalize_symbol(t)})
    return pd.DataFrame(rows).dropna(subset=["date"]).sort_values("date")


def sector_lookup(base: pd.DataFrame | None = None) -> dict[str, str]:
    """symbol → sector_etf：当前快照 → 调仓日志 → 回测池历史成员。

    倒推「当时还在池里、现在已被剔除」的票需要它的板块归属，而最新快照里没有它。
    回测池（``data/curated/sp500_pool/universe.parquet``）是同一份 S&P 500 板块口径的
    历史并集，只取 ``sector_etf`` 这一列，不碰行情；没有这份数据时退化为空字符串
    （这些票仍会进池，但归不到板块、不参与板块内排序，并在推送里列出来）。
    """
    out: dict[str, str] = {}
    try:
        b = load() if base is None else base
        for s, x in zip(b["symbol"], b["sector_etf"]):
            if str(x) and str(x) != "nan":
                out[str(s)] = str(x)
    except SystemExit:
        pass
    ch = read_changes()
    for r in ch.itertuples():
        if str(r.symbol) and str(r.sector_etf) and str(r.sector_etf) != "nan":
            out.setdefault(normalize_symbol(r.symbol), str(r.sector_etf))
    try:
        from quant.data import pool as data_pool      # 只在实盘环境存在
        u = data_pool.universe()
        for t, s in zip(u["ticker"].astype(str), u["sector_etf"].astype(str)):
            if t and t != "nan" and s and s != "nan":
                out.setdefault(normalize_symbol(t), s)
    except Exception:                                  # noqa: BLE001 池层缺失不该影响出信号
        log.debug("回测池不可用，历史成员的板块归属只能靠快照/日志", exc_info=True)
    return out


def members_asof(asof, *, base: pd.DataFrame | None = None,
                 official: Path | None = None,
                 use_history: bool = True) -> pd.DataFrame:
    """as of 那一天的 point-in-time 成分股（列与 ``load()`` 一致）。

    三条路径，从准到糙：
    1. 有 ≤ asof 的历史快照存档 → 直接用它（板块归属也是当时的）；
    2. 快照比 asof 新 → 用官方变更表把 asof 之后发生的 add 撤掉、delete 加回；
    3. 快照比 asof 旧 → 把 asof 之前已公告的变更推进去（快照刷不出来时的兜底）。

    返回的 ``df.attrs["pit"]`` 记录走了哪条路径、动了几只，供推送正文与日志审计。
    """
    ts = pd.Timestamp(asof).normalize()
    snap = load() if base is None else base.copy()
    snap_day = snapshot_asof(snap)

    info = {"asof": str(ts.date()), "src": "snapshot", "rollback": 0, "advance": 0,
            "unknown_sector": 0}
    hist: list[tuple[pd.Timestamp, Path]] = []
    if use_history:
        snaps = [(d, p) for d, p in history_snapshots() if d <= ts]
        # 快照比 asof 新：用 ≤asof 的最近存档最准；快照不比 asof 新时，
        # 只有「正好那天的存档」才有意义（否则不如用变更表往前推进）
        if snaps and (snap_day is None or ts < snap_day or snaps[-1][0] == ts):
            hist = snaps[-1:]

    if hist:
        day, path = hist[-1]
        df = _read_snapshot_file(path)
        info["src"] = f"archive:{day.date()}"
        df.attrs["pit"] = info
        return df

    ev = read_official_changes(official)
    info["changes_through"] = str(ev["date"].max().date()) if len(ev) else ""
    if ev.empty:
        info["src"] = "snapshot:no_changes"
        snap.attrs["pit"] = info
        return snap

    if snap_day is not None and ts < snap_day:
        back = ev[(ev["date"] > ts) & (ev["date"] <= snap_day)]
    else:
        back = ev.iloc[:0]
    fwd = (ev[(ev["date"] > snap_day) & (ev["date"] <= ts)]
           if snap_day is not None else ev.iloc[:0])

    out = snap.copy()
    have = set(out["symbol"].astype(str))
    smap = sector_lookup(snap) if (len(back) or len(fwd)) else {}

    def _append(sym: str) -> None:
        nonlocal out
        sec = smap.get(sym, "")
        if not sec:
            info["unknown_sector"] += 1
        row = {c: "" for c in out.columns}
        row.update({"symbol": sym, "sector_etf": sec, "sector_name": "",
                    "weight": float("nan"), "as_of": str(ts.date())})
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)

    # 回滚：先撤最近发生的（同一只票可能反复进出，顺序反了会算错）
    for r in back.sort_values("date", ascending=False).itertuples():
        if r.action == "add":
            if r.symbol in have:
                out = out[out["symbol"] != r.symbol]
                have.discard(r.symbol)
                info["rollback"] += 1
        elif r.symbol not in have:                     # delete → 当时还在指数里
            _append(r.symbol)
            have.add(r.symbol)
            info["rollback"] += 1
    # 推进：快照落后于 asof 时，把已公告但还没进快照的调整补上
    for r in fwd.itertuples():
        if r.action == "add":
            if r.symbol not in have:
                _append(r.symbol)
                have.add(r.symbol)
                info["advance"] += 1
        elif r.symbol in have:
            out = out[out["symbol"] != r.symbol]
            have.discard(r.symbol)
            info["advance"] += 1

    kinds = []
    if info["rollback"]:
        kinds.append("rollback")
    if info["advance"]:
        kinds.append("advance")
    info["src"] = "+".join(kinds) or "snapshot:noop"
    out = out.reset_index(drop=True)
    out.attrs["pit"] = info
    log.info("PIT 成分 %s：%s（回滚 %d / 推进 %d / 板块未知 %d）", ts.date(), info["src"],
             info["rollback"], info["advance"], info["unknown_sector"])
    return out


def render_changes_md(snapshot: pd.DataFrame | None = None) -> str:
    """把 index_changes.csv 渲染成人看的 Markdown。

    为什么还要 md：csv 是给代码追加的，人是按「上个月调进来哪几只」来回忆的，
    直接用 csv 在 GitHub 上看是一堆重复的 date/action 行。同一份数据两种视图，
    每次刷新成功就整体重写，保证不会出现「md 和 csv 各说一套」。
    """
    df = read_changes()
    out = ["# 候选池调入调出历史", ""]
    out.append("> 由 `quant.live.pool.refresh()` 在每次成功刷新 SSGA holdings 后重新生成，")
    out.append("> 数据源是同目录的 `index_changes.csv`。**不要手工编辑**，改了下次会被覆盖。")

    if snapshot is not None and len(snapshot):
        as_of = ""
        if "as_of" in snapshot.columns and snapshot["as_of"].notna().any():
            as_of = str(snapshot["as_of"].dropna().iloc[0])
        out += ["", f"> 当前快照：`us_sector_holdings.csv`，as of {as_of or '未知'}，"
                    f"{len(snapshot)} 只"]

    out += ["", "## 汇总", "", "| 动作 | 次数 |", "|---|---|"]
    for key, label in _LABEL.items():
        out.append(f"| {label} | {int((df['action'] == key).sum())} |")

    out += ["", "## 明细", ""]
    if df.empty:
        out.append("_还没有记录到任何调入调出。_")
    else:
        out += ["| 日期 | 动作 | 代码 | 板块 ETF | 板块名 | 备注 |", "|---|---|---|---|---|---|"]
        for r in df.sort_values(["date", "action"], ascending=[False, True]).itertuples():
            day = "" if pd.isna(r.date) else pd.Timestamp(r.date).strftime("%Y-%m-%d")
            out.append(f"| {day} | {_LABEL.get(r.action, r.action)} | {r.symbol} | "
                       f"{r.sector_etf} | {r.sector_name} | {r.note} |")
    return "\n".join(out) + "\n"


def write_changes_md(snapshot: pd.DataFrame | None = None) -> Path:
    path = data_ref_dir() / CHANGES_MD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_changes_md(snapshot), encoding="utf-8")
    return path


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
    try:
        write_changes_md(new)
    except Exception:                               # noqa: BLE001  可读版写不出来不该影响刷新
        log.warning("写 %s 失败（csv 已落盘，历史没丢）", CHANGES_MD)

    msg = (f"快照 as of {as_of}，{len(new)} 只；新增 {len(added)}、剔除 {len(removed)}、"
           f"换板块 {len(moved)}；本地无行情需补 {len(new_symbols)}")
    log.info(msg)
    return RefreshResult(True, as_of, len(new), added, removed, moved, new_symbols, msg)
