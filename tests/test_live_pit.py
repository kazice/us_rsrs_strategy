"""实盘成分的 point-in-time 还原。

钉住四件事：
1. 回放历史日时，快照里「那天之后才纳入」的票必须被撤掉
   （否则 2026-04-30 会选出 6 月才纳入的 MRVL，等于前视）；
2. 「当时还在指数里、现在已被剔除」的票必须加回，板块归属从历史来源补；
3. 快照比信号日旧时，把已公告的调整推进去（快照刷不出来时不至于用过期成分）；
4. 有历史快照存档时优先用存档——那时的板块归属是原样的，比倒推可靠。
"""
from pathlib import Path

import pandas as pd
import pytest

from quant.live import pool

SNAP_ASOF = "2026-11-12"
SECTORS = {"AAA": "XLK", "BBB": "XLF", "CCC": "XLV", "MRVL": "XLK"}


def _write_snapshot(path: Path, rows: list[tuple[str, str]], as_of: str) -> None:
    pd.DataFrame({
        "symbol": [s for s, _ in rows],
        "sector_etf": [sec for _, sec in rows],
        "sector_name": [f"{sec} name" for _, sec in rows],
        "weight": [1.0] * len(rows),
        "as_of": [as_of] * len(rows),
    }).to_csv(path, index=False, encoding="utf-8-sig")


def _write_changes(path: Path, rows: list[tuple[str, str, str]]) -> None:
    pd.DataFrame({"date": [d for d, _, _ in rows],
                  "add": [a for _, a, _ in rows],
                  "remove": [r for _, _, r in rows]}
                 ).to_csv(path, index=False, encoding="utf-8-sig")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """铺一套最小环境：当前快照 + 官方变更表 + 空的 history 目录。"""
    snap = tmp_path / "us_sector_holdings.csv"
    # 当前快照：BBB 已被剔除（6 月）、MRVL 已纳入
    _write_snapshot(snap, [("AAA", "XLK"), ("CCC", "XLV"), ("MRVL", "XLK")], SNAP_ASOF)
    ref = tmp_path / "ref"
    ref.mkdir()
    changes = tmp_path / "sp500_changes_since_2019.csv"

    monkeypatch.setattr(pool, "pool_path", lambda: snap)
    monkeypatch.setattr(pool, "data_ref_dir", lambda: ref)
    monkeypatch.setattr(pool, "OFFICIAL_CHANGES", changes)
    monkeypatch.setattr(pool, "sector_lookup", lambda base=None: dict(SECTORS))
    return {"snap": snap, "ref": ref, "changes": changes}


def test_rollback_drops_future_adds_and_restores_deletes(env):
    """2026-04-30：MRVL 是 6 月才纳入的，不能出现；BBB 那天还在，要加回。"""
    _write_changes(env["changes"], [("2026-06-22", "MRVL", "BBB")])

    df = pool.members_asof("2026-04-30")

    assert set(df["symbol"]) == {"AAA", "BBB", "CCC"}
    assert "MRVL" not in set(df["symbol"])
    pit = df.attrs["pit"]
    assert pit["rollback"] == 2 and pit["advance"] == 0
    assert pit["asof"] == "2026-04-30"


def test_advance_when_snapshot_is_stale(env):
    """快照停在 11-12 而信号日更晚：已公告的调整要推进去。"""
    _write_changes(env["changes"], [("2026-11-20", "DDD", "CCC")])

    df = pool.members_asof("2026-12-01")

    syms = set(df["symbol"])
    assert "DDD" in syms and "CCC" not in syms
    pit = df.attrs["pit"]
    assert pit["advance"] == 2 and pit["rollback"] == 0
    assert pit["unknown_sector"] == 1          # DDD 没有板块来源，如实报出来


def test_archive_wins_over_rollback(env):
    """有 ≤ 信号日的历史快照就用它，板块归属也是当时的。"""
    hist = env["ref"] / "history"
    hist.mkdir()
    _write_snapshot(hist / "us_sector_holdings_20260430.csv",
                    [("AAA", "XLK"), ("BBB", "XLF")], "2026-04-30")
    _write_changes(env["changes"], [("2026-06-22", "MRVL", "BBB")])

    df = pool.members_asof("2026-05-08")

    assert set(df["symbol"]) == {"AAA", "BBB"}
    assert df.attrs["pit"]["src"].startswith("archive")


def test_no_changes_table_keeps_snapshot(env):
    """没有官方变更表时不能瞎猜：原样返回快照，并在 src 里标出来。"""
    df = pool.members_asof("2026-04-30")

    assert set(df["symbol"]) == {"AAA", "CCC", "MRVL"}
    assert "no_changes" in df.attrs["pit"]["src"]


def test_normalize_symbol_handles_class_suffixes():
    """官方变更表写 BRK/BF，快照与行情是 BRK-B/BF-B；不归一就会多出没有行情的幽灵票。"""
    assert pool.normalize_symbol("BRK.B") == "BRK-B"
    assert pool.normalize_symbol("BRK") == "BRK-B"
    assert pool.normalize_symbol("bf") == "BF-B"
    assert pool.normalize_symbol("aapl") == "AAPL"


def test_signal_load_members_is_pit_and_filters_excluded_sector(monkeypatch):
    """signal.load_members(asof) 必须把 asof 传下去，并照旧剔掉 XLRE。"""
    from quant.live import signal

    seen = {}

    def fake_members_asof(asof, **kw):
        seen["asof"] = asof
        df = pd.DataFrame({"symbol": ["AAA", "REIT"],
                           "sector_etf": ["XLK", "XLRE"]})
        df.attrs["pit"] = {"asof": str(pd.Timestamp(asof).date()), "src": "snapshot:noop",
                           "rollback": 0, "advance": 0}
        return df

    monkeypatch.setattr(pool, "members_asof", fake_members_asof)

    df = signal.load_members("2026-04-30")

    assert pd.Timestamp(seen["asof"]) == pd.Timestamp("2026-04-30")
    assert list(df["symbol"]) == ["AAA"]                 # XLRE 被剔除
    assert df.attrs["pit"]["asof"] == "2026-04-30"        # attrs 不因 reset_index 丢失
