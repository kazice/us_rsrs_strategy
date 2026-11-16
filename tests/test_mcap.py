"""市值门槛的三级降级：全市场快照 → 5 日换手率反推 → SEC 申报股本。

钉住三件事：
1. 优先级不能乱（快照最准，SEC 表有洞只配兜底）；
2. 快照的 asof 比信号日超前太多时弃用（用 --now 复现历史时点会踩到，属于前视）；
3. 门槛是严格大于，且取不到市值的票**不剔除**（与回测一致）。
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from quant.live import mcap

ASOF = pd.Timestamp("2026-11-16")
DAYS = pd.bdate_range("2026-11-09", periods=5)


def _write_env(root: Path, *, universe_asof: str = "2026-11-16") -> None:
    """铺一套最小数据：快照 / 换手率 / SEC 股本。"""
    uni = pd.DataFrame({
        "symbol": ["AAA"],
        "name": ["A Corp"],
        "price": [100.0],
        "market_cap": [100e9],          # 1e9 股
        "asof": [universe_asof],
    })
    (root / "meta" / "universe").mkdir(parents=True, exist_ok=True)
    uni.to_parquet(root / "meta" / "universe" / "universe_latest.parquet", index=False)

    # BBB：不在快照里，用成交量 ÷ 换手率反推 → 10e6 / 0.02 = 5e8 股
    (root / "curated" / "fundamentals").mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"symbol": "BBB", "date": d, "turnover": 0.02, "src": "calc"} for d in DAYS]
                 ).to_parquet(root / "curated" / "fundamentals" / "turnover_daily.parquet",
                              index=False)

    # CCC：两个来源都没有，退回 SEC 申报
    pd.DataFrame([{"symbol": "CCC", "end": "2026-09-30", "val": 8e8,
                   "filed": "2026-11-01", "form": "10-Q"}]
                 ).to_parquet(root / "curated" / "fundamentals" / "shares_outstanding.parquet",
                              index=False)


def _fake_volume(symbols):
    return pd.DataFrame(10e6, index=DAYS, columns=list(symbols), dtype="float64")


def _run(root: Path, symbols, asof=ASOF):
    with patch.object(mcap.universe, "latest_file",
                      lambda: root / "meta" / "universe" / "universe_latest.parquet"):
        with patch.object(mcap.turnover_mod, "turnover_path",
                          lambda: root / "curated" / "fundamentals" / "turnover_daily.parquet"):
            with patch.object(mcap.store, "data_root", lambda: root):
                with patch.object(mcap.store, "load_panel",
                                  lambda **kw: _fake_volume(kw.get("symbols") or [])):
                    return mcap.share_sources(symbols, asof)


def test_priority_snapshot_then_ws_then_sec():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_env(root)
        shares, srcs, snap_asof = _run(root, ["AAA", "BBB", "CCC", "DDD"])

    assert snap_asof == "2026-11-16"
    assert srcs["AAA"] == mcap.SRC_SNAPSHOT and abs(shares["AAA"] - 1e9) < 1
    assert srcs["BBB"] == mcap.SRC_WS and abs(shares["BBB"] - 5e8) < 1e-3
    assert srcs["CCC"] == mcap.SRC_SEC and abs(shares["CCC"] - 8e8) < 1
    assert srcs["DDD"] == "" and np.isnan(shares["DDD"])


def test_future_snapshot_is_rejected():
    """快照 as_of 晚于信号日太多 → 弃用（否则 --now 复现历史时会拿到未来股本）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_env(root, universe_asof="2026-12-31")
        shares, srcs, snap_asof = _run(root, ["AAA"])

    assert snap_asof == ""
    assert srcs["AAA"] != mcap.SRC_SNAPSHOT
    assert np.isnan(shares["AAA"])


def test_gate_is_strictly_greater_and_keeps_unknown():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_env(root)
        with patch.object(mcap.universe, "latest_file",
                          lambda: root / "meta" / "universe" / "universe_latest.parquet"):
            with patch.object(mcap.turnover_mod, "turnover_path",
                              lambda: root / "curated" / "fundamentals" / "turnover_daily.parquet"):
                with patch.object(mcap.store, "data_root", lambda: root):
                    with patch.object(mcap.store, "load_panel",
                                      lambda **kw: _fake_volume(kw.get("symbols") or [])):
                        g = mcap.gate(["AAA", "BBB", "CCC", "DDD"],
                                      pd.Series({s: 100.0 for s in ["AAA", "BBB", "CCC"]}),
                                      ASOF, min_mcap=500e8)

    assert abs(g.caps["BBB"] - 500e8) < 1e6, "BBB 应正好等于门槛"
    assert "BBB" in g.dropped, "等于门槛不算达标（严格大于）"
    assert "AAA" in g.kept and "CCC" in g.kept
    assert g.unknown == ["DDD"] and "DDD" in g.kept, "取不到市值的不剔除"
    assert any("市值来源" in n for n in g.notes)
