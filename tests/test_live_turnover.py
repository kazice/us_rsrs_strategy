"""换手率面板的正确性护栏。

面板是排序分的唯一基础（``turn20²/turn120``），平方项会把数量级错误放大成选股错误。
历史上 ``sec_calc`` 段留下过 449,828 倍的垃圾（VTRS，股本字段取成 100 股），排序照样用，
所以这里钉住两道防线：读的时候越界值按缺失处理，写的时候异常值直接拒收。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.live import turnover as tmod
from quant.live.quotes import Quote


def _quote(sym: str, turnover_pct: float) -> Quote:
    return Quote(symbol=sym, price=10.0, prev_close=10.0, high=10.0, low=10.0,
                 volume=1e6, turnover_pct=turnover_pct, total_shares=1e9,
                 ts=None, src="test")


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    path = tmp_path / "turnover_daily.parquet"
    dates = pd.bdate_range("2026-01-01", periods=21)
    rows = []
    for i, d in enumerate(dates):
        rows.append({"symbol": "AAA", "date": d, "turnover": 0.01, "src": "ws"})
        # 第 10 天写一个 449,828 倍（4.5 万倍）的垃圾值
        rows.append({"symbol": "BAD", "date": d,
                     "turnover": 449828.0 if i == 10 else 0.01, "src": "sec_calc"})
    pd.DataFrame(rows).to_parquet(path, index=False)
    monkeypatch.setattr(tmod, "turnover_path", lambda: path)
    return path, dates


def test_load_history_treats_outliers_as_missing(panel):
    _, dates = panel
    asof = dates[-1] + pd.Timedelta(days=1)
    hist, _ = tmod.load_history(["AAA", "BAD"], asof, lookback=21)

    assert np.allclose(hist["AAA"].to_numpy(), 0.01)
    # 垃圾值那一天必须变成 NaN，而不是 449,828 参与均值
    assert int(hist["BAD"].isna().sum()) == 1
    assert np.nanmax(hist["BAD"].to_numpy()) == pytest.approx(0.01)


def test_build_rejects_absurd_quote(panel):
    _, dates = panel
    asof = dates[-1] + pd.Timedelta(days=1)
    hist, _ = tmod.load_history(["AAA"], asof, lookback=21)
    quotes = {
        "AAA": _quote("AAA", 1.0),
        "BAD": _quote("BAD", 44982800.0),        # 44,982,800%
    }
    res = tmod.build(["AAA", "BAD"], asof, quotes)

    assert "AAA" in res.mean.index
    assert "BAD" not in res.mean.index
    assert "异常" in res.notes.get("BAD", "")


def test_mean_n_window_matches_full_history(panel):
    """120 日窗口不足时不该硬算（与回测 min_periods 一致）。"""
    _, dates = panel
    asof = dates[-1] + pd.Timedelta(days=1)
    q = {"AAA": _quote("AAA", 1.0)}
    out = tmod.mean_n(["AAA"], asof, q, window=120, min_days=60)
    assert out.empty
    out20 = tmod.mean_n(["AAA"], asof, q, window=20, min_days=10)
    assert out20["AAA"] == pytest.approx(0.01, rel=1e-6)
