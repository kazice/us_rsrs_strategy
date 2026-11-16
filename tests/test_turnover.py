"""换手率合成口径：当天不乘 0.48、窗口含当天、有效天数不足要剔除。

这是全链路最容易错的一步（HANDOFF 口径陷阱 #3/#4）：回测里 ``--turn-tail 0.48``
是因为回测当天只能拿到盘中量；实盘 15:30 的报价本身就是 0.48 倍，再乘一次就折两遍。
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from fixtures import quote
from quant.live import turnover

ASOF = pd.Timestamp("2026-11-13")


def _panel_file(dirpath: Path, rows) -> Path:
    path = dirpath / "turnover.parquet"
    pd.DataFrame(rows, columns=["symbol", "date", "turnover", "src"]).to_parquet(path, index=False)
    return path


def _rows(sym: str, days: int, value: float, start: str = "2026-10-01") -> list[dict]:
    return [{"symbol": sym, "date": d, "turnover": value, "src": "calc"}
            for d in pd.bdate_range(start, periods=days)]


def test_mean_is_sum19_plus_today_and_tail_factor_is_one():
    """(19 天 × 1% + 当天 2%) / 20 = 1.05%；若乘了 0.48 会变成 0.998%。"""
    assert turnover.TAIL_FACTOR == 1.0, "实盘当天不能再折一次 0.48"

    with tempfile.TemporaryDirectory() as td:
        path = _panel_file(Path(td), _rows("AAA", 19, 0.01))
        with patch.object(turnover, "turnover_path", lambda: path):
            res = turnover.build(["AAA"], ASOF, {"AAA": quote("AAA", turnover_pct=2.0)})

    assert abs(float(res.mean["AAA"]) - 0.0105) < 1e-12
    assert int(res.days["AAA"]) == 20


def test_missing_quote_is_dropped_with_reason():
    with tempfile.TemporaryDirectory() as td:
        path = _panel_file(Path(td), _rows("AAA", 19, 0.01))
        with patch.object(turnover, "turnover_path", lambda: path):
            res = turnover.build(["AAA"], ASOF, {})

    assert res.mean.empty
    assert "无当日报价" in res.notes["AAA"]


def test_too_few_days_is_dropped():
    """有效天数 < min_days(10) 的票不进均值，否则新股会以极低换手混进榜单。"""
    with tempfile.TemporaryDirectory() as td:
        path = _panel_file(Path(td), _rows("AAA", 5, 0.01))
        with patch.object(turnover, "turnover_path", lambda: path):
            res = turnover.build(["AAA"], ASOF, {"AAA": quote("AAA", turnover_pct=1.0)})

    assert res.mean.empty
    assert "有效天数不足" in res.notes["AAA"]


def test_zero_turnover_quote_is_dropped():
    with tempfile.TemporaryDirectory() as td:
        path = _panel_file(Path(td), _rows("AAA", 19, 0.01))
        with patch.object(turnover, "turnover_path", lambda: path):
            res = turnover.build(["AAA"], ASOF, {"AAA": quote("AAA", turnover_pct=0.0)})

    assert res.mean.empty
    assert "换手率缺失" in res.notes["AAA"]
