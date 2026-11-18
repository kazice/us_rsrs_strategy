"""异常告警与报价重试：该出信号的运行失败必须推送；网络抖动要重试而不是直接丢整批。"""
import argparse
import urllib.error
from unittest.mock import MagicMock, patch

from quant.live import quotes
from scripts import live_signal


def _args(dry_run=False):
    return argparse.Namespace(dry_run=dry_run)


def test_alert_only_when_armed():
    j = MagicMock(run_id="r1", armed=False)
    with patch.object(live_signal.notify, "send") as send:
        live_signal._alert(j, _args(), "t", "b")
        send.assert_not_called()                 # 判定前的正常退出不打扰人
        j.armed = True
        live_signal._alert(j, _args(), "t", "b")
        send.assert_called_once()
        send.reset_mock()
        live_signal._alert(j, _args(dry_run=True), "t", "b")
        send.assert_not_called()                 # dry-run 不推


def test_alert_failure_does_not_raise():
    j = MagicMock(run_id="r1", armed=True)
    with patch.object(live_signal.notify, "send", side_effect=RuntimeError("x")):
        live_signal._alert(j, _args(), "t", "b")


def test_http_retries_then_succeeds():
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return "ok".encode()

    def fake_open(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("boom")
        return _Resp()

    with patch.object(quotes.urllib.request, "urlopen", fake_open), \
            patch.object(quotes.time, "sleep"):
        assert quotes._http("http://x", {}) == "ok"
    assert calls["n"] == 3


def test_http_raises_after_retries():
    with patch.object(quotes.urllib.request, "urlopen",
                      side_effect=urllib.error.URLError("boom")), \
            patch.object(quotes.time, "sleep"):
        try:
            quotes._http("http://x", {})
        except urllib.error.URLError:
            return
    raise AssertionError("应该在重试用尽后抛出")
