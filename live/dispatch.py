"""准点触发器：由外部定时器每 5 分钟调用一次，到「收盘前 30 分钟」才真正干活。

为什么需要它
------------
cron / 计划任务本身的时刻是**固定的 UTC**，但美股收盘时刻在变：
夏令时 16:00 ET = 20:00Z、冬令时 = 21:00Z，早收日是 13:00 ET = 17:00Z/18:00Z。
把 `20:30Z` 写死在定时器里，一年有半年会错一小时。

所以定时器只负责**高频唤醒**（每 5 分钟），
"今天是否交易日、收盘几点、现在到没到点" 全部由 `live/calendar.py` 现算。

设计约束
--------
- **幂等**：同一天只触发一次，靠 `state/dispatch_mark.json` + 账本幂等键双保险；
- **可回放**：`--now "2026-11-27 17:30"` 可复现任意时刻的判定；
- **不吞错**：调用下游失败会打印并返回非 0，下一轮定时器会重试。

用法
----
    python live/dispatch.py                 # 定时器调用这一条
    python live/dispatch.py --dry-run       # 只看会不会触发
    python live/dispatch.py --now "2026-11-27 17:31" --dry-run

Windows 任务计划：每 5 分钟运行
    python.exe d:\\work\\project\\trade_agent_paper\\live\\dispatch.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import market_calendar as cal  # noqa: E402

MARK = ROOT / "state" / "dispatch_mark.json"


def _load_mark() -> dict:
    try:
        return json.loads(MARK.read_text(encoding="utf-8")) if MARK.exists() else {}
    except json.JSONDecodeError:
        return {}


def _save_mark(day: dt.date, now: dt.datetime) -> None:
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps({"date": day.isoformat(), "at": now.isoformat()}, ensure_ascii=False),
                    encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="到「收盘前 30 分钟」则触发信号计算")
    ap.add_argument("--now", default="", help="复现用 UTC 时刻，如 '2026-11-27 17:31'")
    ap.add_argument("--early-min", type=int, default=5, help="允许早于触发时刻的分钟数")
    ap.add_argument("--late-min", type=int, default=30, help="过了触发时刻仍补触发的分钟数")
    ap.add_argument("--force", action="store_true", help="无视窗口与去重，强制跑一次")
    ap.add_argument("--dry-run", action="store_true", help="只判定，不执行下游")
    a = ap.parse_args()

    now = (dt.datetime.fromisoformat(a.now).replace(tzinfo=cal.UTC)
           if a.now else dt.datetime.now(cal.UTC))
    today = now.astimezone(cal.ET).date()
    occ = cal.occasion_for(today)

    if not occ.is_trading:
        print(f"{today} 非交易日（{occ.reason}），跳过")
        return 0

    delta = (now - occ.trigger_utc).total_seconds() / 60
    print(f"{today}  收盘 {occ.close_et} ET = {occ.close_utc:%H:%M}Z  "
          f"触发 {occ.trigger_utc:%H:%M}Z  当前距触发 {delta:+.1f} 分钟  [{occ.reason}]")

    if not a.force and not cal.in_window(now, occ, a.early_min, a.late_min):
        print("  不在窗口内，跳过")
        return 0

    if not a.force and _load_mark().get("date") == today.isoformat():
        print("  今天已触发过，跳过")
        return 0

    cmd = [sys.executable, str(HERE / "signal.py")]
    if a.dry_run:
        print(f"  [dry-run] 将执行：{' '.join(cmd)}")
        return 0

    print(f"  执行：{' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print(f"  下游失败（exit {r.returncode}），下一轮会重试", file=sys.stderr)
        return r.returncode

    _save_mark(today, now)
    print("  完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
