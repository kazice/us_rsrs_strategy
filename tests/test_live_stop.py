"""实盘止损与回测口径的对齐测试。

三件事必须钉住，否则实盘会悄悄跑偏：
1. 参数与回测定稿一致（10 个板块、ADX>10、20:10 / 60:5 斜率、10% 止损、11 号）；
2. 止损判定用复权口径——拆股不能被当成暴跌；
3. 止损后的重选状态要带上「排除原票」，且本期只给一次额度。
"""
import pandas as pd
import pytest

from quant.live import books
from quant.live import signal as signal_mod
from fixtures import quote


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TA_STATE_DIR", str(tmp_path))
    return tmp_path


def _pos(**kw) -> books.Position:
    base = dict(symbol="AAA", sector="XLK", entry_date="2026-03-02",
                entry_price=100.0, entry_adj=100.0, nav=1.0)
    base.update(kw)
    return books.Position(**base)


def test_params_match_backtest():
    assert len(signal_mod.SECTOR_ETFS) == 10
    assert "XLRE" not in signal_mod.SECTOR_ETFS
    assert signal_mod.EXCLUDE_SECTORS == ("XLRE",)
    assert signal_mod.ADX_MIN == 10.0
    assert signal_mod.SLOPE_WINDOWS == {20: 10, 60: 5}
    assert signal_mod.STOP_LOSS == 0.10
    assert signal_mod.STOP_REPICK_DOM == 11


@pytest.mark.parametrize("price,factor,hit", [
    (91.0, 1.0, False),     # −9%
    (90.0, 1.0, True),      # 正好 −10%，回测是 `<= -threshold`
    (85.0, 1.0, True),
])
def test_stop_threshold(price, factor, hit):
    pos = _pos()
    q = {pos.symbol: quote(pos.symbol, price)}
    got, ret, _ = books.stop_triggered(pos, q, {pos.symbol: factor}, 0.10)
    assert got is hit
    assert ret == pytest.approx(price * factor / pos.entry_adj - 1.0)


def test_stop_ignores_split():
    """2:1 拆股：未复权价从 100 掉到 50，但复权因子同时变成 2.0，跌幅应为 0。"""
    pos = _pos()
    q = {pos.symbol: quote(pos.symbol, 50.0)}
    got, ret, _ = books.stop_triggered(pos, q, {pos.symbol: 2.0}, 0.10)
    assert got is False
    assert ret == pytest.approx(0.0)


def test_stop_without_factor_does_nothing():
    """拿不到复权因子就判不了，宁可漏检也不能凭未复权价割肉。"""
    pos = _pos()
    q = {pos.symbol: quote(pos.symbol, 50.0)}
    got, ret, _ = books.stop_triggered(pos, q, {}, 0.10)
    assert got is False
    assert ret is None


def test_record_stop_clears_position_and_rolls_nav(state):
    pos = _pos(nav=1.2, stop_retry=0)
    books.save_position(pos)
    books.record_stop(pos, day="2026-03-05", action="止损卖出 AAA", price=88.0,
                      holding_return=-0.12, note="−12% 触及 −10%", retry_used=1)

    back = books.load_position()
    assert back.symbol == ""
    assert back.nav == pytest.approx(1.2 * 0.88)
    assert back.stop_retry == 1

    led = pd.read_csv(state / books.LEDGER)
    row = led.iloc[-1]
    assert row["kind"] == "stop"
    assert row["from_symbol"] == "AAA"
    assert pd.isna(row["to_symbol"])          # 空字符串读回来是 NaN
    assert float(row["ret_since_entry"]) == pytest.approx(-0.12)


def test_refill_carries_exclude_and_origin(state):
    books.set_refill(since="2026-03-05", until="2026-03-31",
                     bull=True, exclude="AAA", origin="stop")
    rf = books.load_refill()
    assert rf["open"] is True
    assert rf["exclude"] == "AAA"
    assert rf["origin"] == "stop"

    books.clear_refill("done")
    assert books.load_refill()["open"] is False


def test_rebalance_resets_stop_retry(state):
    pos = _pos(stop_retry=1)

    class _Sig:
        pick = "BBB"
        sector = "XLK"
        price = 50.0
        factors = {"BBB": 1.0}

    books.record(pos, day="2026-03-31", kind="rebalance", action="换仓",
                 signal=_Sig(), holding_return=-0.05, note="换仓")
    assert books.load_position().stop_retry == 0


def test_refill_keeps_stop_retry(state):
    """止损重选成交后额度不能清零，否则「再跌 10% 还能再选一次」。"""
    pos = _pos(stop_retry=1)

    class _Sig:
        pick = "CCC"
        sector = "XLK"
        price = 60.0
        factors = {"CCC": 1.0}

    books.record(pos, day="2026-03-06", kind="refill", action="建仓",
                 signal=_Sig(), holding_return=None, note="止损重选")
    assert books.load_position().stop_retry == 1
