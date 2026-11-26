"""workflow 闸门：判断现在能不能跑，避免空跑产生提交。

用法
----
    python live/gate.py signal    # 收盘前 30 分钟窗口内 → 0，否则 10
    python live/gate.py data      # 收盘满 SETTLE_MIN 分钟之后 → 0，否则 10

退出码
------
0 = 可以跑；10 = 跳过。workflow 用 ``== '10'`` 判定，不把跳过当成失败。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("market_calendar", HERE / "market_calendar.py")
cal = importlib.util.module_from_spec(_spec)
sys.modules["market_calendar"] = cal
_spec.loader.exec_module(cal)

SETTLE_MIN = 30          # 收盘后多少分钟才算日线稳定（新浪落盘比交易所结算慢）
LEAD_MIN = 30            # 收盘前多少分钟出信号（与 backtest 的收盘决策一致）
EARLY, LATE = 5, 40      # 信号窗口余量


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "data"
    now = dt.datetime.now(cal.UTC)
    today = now.astimezone(cal.ET).date()
    occ = cal.occasion_for(today)

    if not occ.is_trading:
        print(f"{today} 非交易日（{occ.reason}），跳过")
        return 10
    if occ.close_utc is None:
        print(f"{today} 拿不到收盘时间，跳过")
        return 10

    delta = (now - occ.close_utc).total_seconds() / 60
    print(f"{today} ET 收盘 {occ.close_et} = {occ.close_utc:%H:%M}Z，"
          f"现在距收盘 {delta:+.1f} 分钟，模式={mode}")

    if mode == "data":
        ok = delta >= SETTLE_MIN
    else:
        ok = -(LEAD_MIN + EARLY) <= delta <= -LEAD_MIN + LATE

    print("可以执行" if ok else "不在窗口内，跳过")
    return 0 if ok else 10


if __name__ == "__main__":
    raise SystemExit(main())
