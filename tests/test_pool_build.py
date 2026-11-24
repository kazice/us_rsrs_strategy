"""股池清洗口径：拆股/合股靠复权因子吸收、退市收益不丢、调出指数不等于退市。

这些是 ``scripts/build_sp500_pool.py`` 里最容易错的三处，全部用合成数据断言，
不读 data 分支、不联网（和仓库其它测试一致）。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_sp500_pool import build  # noqa: E402


def _row(permno, ticker, day, close, facpr, *, ret=0.0, status="A", shares=1000.0):
    return {
        "PERMNO": permno, "Ticker": ticker, "CUSIP": f"C{permno}", "SICCD": 7372,
        "DlyCalDt": pd.Timestamp(day), "DlyClose": close, "DlyOpen": close,
        "DlyHigh": close, "DlyLow": close, "DlyPrc": close, "DlyPrcFlg": "TR",
        "DlyVol": 1e6, "DlyCap": None if close is None else close * shares,
        "ShrOut": shares,
        "DlyRet": ret, "DlyCumFacPr": facpr, "DlyCumFacShr": 1.0,
        "TradingStatusFlg": status, "DlyRetMissFlg": None,
    }


def _mem(permno, ticker, start, end):
    return {"PERMNO": permno, "ticker": ticker, "cusip": f"C{permno}", "siccd": 7372,
            "MbrStartDt": pd.Timestamp(start), "MbrEndDt": pd.Timestamp(end)}


@pytest.fixture(scope="module")
def tables():
    raw = pd.DataFrame([
        # AAA：2:1 拆股（facpr 2→1，价格 200→101），复权后应连续
        _row(11111, "AAA", "2021-06-01", 200.0, 2.0),
        _row(11111, "AAA", "2021-06-02", 101.0, 1.0, ret=0.01),
        _row(11111, "AAA", "2021-06-03", 102.0, 1.0, ret=0.0099),
        # BBB：退市（末行没有收盘价，但有 -90% 的退市收益）
        _row(22222, "BBB", "2021-06-01", 50.0, 1.0),
        _row(22222, "BBB", "2021-06-02", 51.0, 1.0, ret=0.02),
        _row(22222, "BBB", "2021-06-03", None, 1.0, ret=-0.9, status="D"),
        # CCC：只是被调出指数，行情到调出日为止，状态仍是 A
        _row(33333, "CCC", "2021-06-01", 30.0, 1.0),
        _row(33333, "CCC", "2021-06-02", 30.5, 1.0, ret=0.0167),
    ])
    mem = pd.DataFrame([
        _mem(11111, "AAA", "2021-01-04", "2025-12-31"),
        _mem(22222, "BBB", "2021-01-04", "2021-06-05"),
        _mem(33333, "CCC", "2021-01-04", "2021-06-02"),
    ])
    return build(mem, raw, "2021-01-01", "2021-12-31")


def test_adj_price_absorbs_split(tables):
    bars = tables["bars"]
    b = bars[bars["security_id"] == "11111"].sort_values("date")
    assert b["close"].tolist() == [200.0, 101.0, 102.0]       # 原始价：拆股日有台阶
    r = b["adj_close"].pct_change().dropna()
    assert (r.abs() < 0.02).all(), "复权后拆股日不应还有 ±50% 的台阶"
    assert b["adj_close"].iloc[0] == pytest.approx(100.0)


def test_split_event_classified(tables):
    ev = tables["events"]
    assert len(ev) == 1
    row = ev.iloc[0]
    assert row["kind"] == "split"
    assert row["fac_ratio"] == pytest.approx(0.5)
    assert row["implied_ratio"] == pytest.approx(2.0)


def test_delisting_return_is_kept(tables):
    ex = tables["exits"].set_index("security_id")
    assert ex.loc["22222", "exit_type"] == "delisted"
    assert ex.loc["22222", "delisting_ret"] == pytest.approx(-0.9)
    assert ex.loc["22222", "ticker"] == "BBB"
    # 退市行没有收盘价，不能留在价表里污染价格序列
    assert not (tables["bars"]["security_id"] == "22222").pipe(
        lambda s: tables["bars"][s]["close"].isna()).any()


def test_index_removal_is_not_delisting(tables):
    ex = tables["exits"].set_index("security_id")
    assert ex.loc["33333", "exit_type"] == "index_removal"
    assert pd.isna(ex.loc["33333", "delisting_ret"])
    assert ex.loc["11111", "exit_type"] == "active"


def test_output_keys_are_unique_and_aligned(tables):
    u, b = tables["universe"], tables["bars"]
    assert u["security_id"].is_unique
    assert not b.duplicated(["security_id", "date"]).any()
    assert set(b["security_id"]) <= set(u["security_id"])
    assert b["adj_close"].notna().all()
    assert (b["close"] > 0).all()
