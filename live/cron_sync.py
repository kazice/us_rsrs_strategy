"""闭环定时：每次运行后把 Cloudflare Worker 的 Cron 改成「下一次该触发的时刻」，并每天自检。

为什么
------
cron 表达式是静态的，而收盘时间随夏令时/早收日/休市变化。所以不再写死时刻，而是：
  1. 每次 paper_trade 跑完，``sync`` 用 calendar 算出下一次「信号触发」和「收盘后数据更新」的
     UTC 时刻，写成一次性日期 cron（如 ``30 20 20 11 *``）推给 Cloudflare；
  2. Cloudflare 到点调 workflow_dispatch，workflow 跑完又 ``sync`` 一次，形成闭环；
  3. 闭环最怕断链，所以 Cloudflare 上永久保留一条每日自检 cron（CHECK_CRON），
     触发 cron_check.yml 跑 ``check``：核对 Cron 是否正确、过去 26 小时该触发的是否真触发了，
     不对就自动重设并推送 pushplus。

环境变量：CF_API_TOKEN、CF_ACCOUNT_ID（Workers Scripts: Edit）；check 另需 GH_TOKEN、
GITHUB_REPOSITORY；告警用 PUSHPLUS_TOKEN。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_local(mod_name: str, path: Path):
    """按文件加载同目录模块（不能前插 sys.path，会遮蔽标准库）。"""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


calendar = _load_local("market_calendar", _HERE / "market_calendar.py")
notify = _load_local("live_notify", _HERE / "notify.py")

WORKER = "paper-trade-trigger"
WORKFLOW = "paper_trade.yml"
CHECK_CRON = "15 12 * * *"          # 每日自检（UTC），Worker 里按此值路由到 cron_check.yml
DATA_DELAY_MIN = 30                 # 收盘后多少分钟跑数据更新（沿用原 21:30Z = 收盘 +30）
MIGRATED_AT = dt.datetime(2026, 11, 18, 5, 0, tzinfo=dt.timezone.utc)  # 新机制上线，此前不查
UTC = dt.timezone.utc
CF = "https://api.cloudflare.com/client/v4"


# --------------------------------------------------------------------------- #
# 触发时刻
# --------------------------------------------------------------------------- #
def cron_at(t: dt.datetime) -> str:
    t = t.astimezone(UTC)
    return f"{t.minute} {t.hour} {t.day} {t.month} *"


def day_triggers(d: dt.date) -> list[tuple[str, dt.datetime]]:
    """某个 ET 日期的触发点：**每个交易日**都在收盘前 30 分钟叫醒一次 signal，data 收盘后跑。

    为什么 signal 不再只挂在推送日：回测的 ``--bull-refill`` 会在「调仓日空仓」后逐日重挑，
    实盘只能在每个交易日都算一次才知道今天有没有补选信号。非推送日由 ``live_signal``
    自己判断（无持仓、无补选窗口就直接 exit 0），多出来的运行只是空跑。
    """
    if not calendar.is_trading_day(d):
        return []
    occ = calendar.occasion_for(d)
    out: list[tuple[str, dt.datetime]] = []
    if occ.trigger_utc is not None:
        out.append(("signal", occ.trigger_utc))
    if occ.close_et is not None:
        close = dt.datetime.combine(d, occ.close_et, tzinfo=calendar.ET).astimezone(UTC)
        out.append(("data", close + dt.timedelta(minutes=DATA_DELAY_MIN)))
    return out


def next_triggers(now: dt.datetime) -> dict[str, dt.datetime]:
    """now 之后（至少 1 分钟）最近的一次 signal 和一次 data 触发时刻。"""
    floor = now + dt.timedelta(minutes=1)
    found: dict[str, dt.datetime] = {}
    d = now.astimezone(calendar.ET).date() - dt.timedelta(days=1)
    for _ in range(21):
        for kind, t in day_triggers(d):
            if t >= floor and kind not in found:
                found[kind] = t
        if len(found) == 2:
            return found
        d += dt.timedelta(days=1)
    raise RuntimeError(f"21 天内找不到下一次触发时刻：{found}")


def past_triggers(start: dt.datetime, end: dt.datetime) -> list[tuple[str, dt.datetime]]:
    out = []
    d = start.astimezone(calendar.ET).date() - dt.timedelta(days=1)
    last = end.astimezone(calendar.ET).date()
    while d <= last:
        out += [(k, t) for k, t in day_triggers(d) if start < t <= end]
        d += dt.timedelta(days=1)
    return sorted(out, key=lambda x: x[1])


def desired_crons(now: dt.datetime) -> list[str]:
    nxt = next_triggers(now)
    crons = [CHECK_CRON] + [cron_at(t) for t in nxt.values()]
    return list(dict.fromkeys(crons))


# --------------------------------------------------------------------------- #
# Cloudflare
# --------------------------------------------------------------------------- #
def _env(name: str) -> str:
    """读环境变量并清掉粘贴时常见的污染：首尾空白、成对引号、``Bearer `` 前缀。"""
    v = os.environ.get(name, "").strip().strip('"').strip("'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


def _shape(v: str) -> str:
    """只描述形态，不泄露值：长度 + 是否像 32 位十六进制（token ID / account ID 的形状）。"""
    if not v:
        return "空"
    hex32 = len(v) == 32 and all(c in "0123456789abcdefABCDEF" for c in v)
    return f"长度 {len(v)}" + ("，形如 32 位十六进制 ID（很可能填成了 ID 而不是 token 值）" if hex32 else "")


def _diag() -> str:
    """Cloudflare 认证失败时，输出不含敏感值的诊断信息。"""
    raw_t = os.environ.get("CF_API_TOKEN", "")
    raw_a = os.environ.get("CF_ACCOUNT_ID", "")
    t, a = _env("CF_API_TOKEN"), _env("CF_ACCOUNT_ID")
    lines = [
        f"CF_API_TOKEN：{_shape(t)}",
        f"  - 原始值与清洗后不同：{'是（含有空白/引号/Bearer 前缀）' if raw_t != t else '否'}",
        f"CF_ACCOUNT_ID：{_shape(a)}",
        f"  - 原始值与清洗后不同：{'是' if raw_a != a else '否'}",
    ]
    try:
        req = urllib.request.Request(f"{CF}/accounts/{a}/tokens/verify",
                                     headers={"Authorization": f"Bearer {t}", "User-Agent": "cron-sync-diag"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode())
        lines.append(f"token 校验：{(d.get('result') or {}).get('status', d.get('success'))}")
    except urllib.error.HTTPError as exc:
        lines.append(f"token 校验：HTTP {exc.code} {exc.read()[:160]!r}")
    except Exception as exc:                                    # noqa: BLE001
        lines.append(f"token 校验：{type(exc).__name__}: {exc}")
    return "\n".join(lines)


def _cf(method: str, body: object | None = None) -> dict:
    url = f"{CF}/accounts/{_env('CF_ACCOUNT_ID')}/workers/scripts/{WORKER}/schedules"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_env('CF_API_TOKEN')}",
                 "Content-Type": "application/json", "User-Agent": "paper-trade-cron-sync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cloudflare {method} -> {exc.code} {exc.read()[:300]!r}") from exc
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare {method} 失败：{data.get('errors')}")
    return data


def current_crons() -> list[str]:
    return [s["cron"] for s in _cf("GET")["result"]["schedules"]]


def apply_crons(want: list[str]) -> bool:
    """与现状不同才改；返回是否做了修改。"""
    if sorted(current_crons()) == sorted(want):
        return False
    _cf("PUT", [{"cron": c} for c in want])
    if sorted(current_crons()) != sorted(want):
        raise RuntimeError(f"Cloudflare Cron 写入后回读不一致：期望 {want}")
    return True


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_sync(now: dt.datetime) -> int:
    want = desired_crons(now)
    changed = apply_crons(want)
    print(("已更新" if changed else "无需更新") + f" Cloudflare Cron：{want}")
    return 0


def _dispatch_runs(since: dt.datetime) -> list[dt.datetime]:
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    q = urllib.parse.quote(f">={since.date().isoformat()}")
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
           f"?event=workflow_dispatch&per_page=100&created={q}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "paper-trade-cron-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        runs = json.loads(resp.read().decode())["workflow_runs"]
    return [dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs]


def cmd_check(now: dt.datetime) -> int:
    problems: list[str] = []

    try:
        if apply_crons(desired_crons(now)):
            problems.append("Cloudflare 上的 Cron 与预期不一致，已自动重设（说明闭环曾断链）")
    except Exception as exc:                                    # noqa: BLE001
        print("::group::Cloudflare 认证诊断")
        print(_diag())
        print("::endgroup::")
        problems.append(f"无法核对/重设 Cloudflare Cron：{exc}")

    start = max(now - dt.timedelta(hours=26), MIGRATED_AT)
    end = now - dt.timedelta(minutes=15)
    try:
        expected = past_triggers(start, end)
        created = _dispatch_runs(start)
        for kind, t in expected:
            hit = any(t - dt.timedelta(seconds=60) <= c <= t + dt.timedelta(minutes=15)
                      for c in created)
            print(f"{'ok  ' if hit else 'MISS'} {kind} {t:%Y-%m-%d %H:%MZ}")
            if not hit:
                problems.append(f"{kind} 触发缺失：{t:%Y-%m-%d %H:%MZ} 附近没有 workflow_dispatch 运行")
    except Exception as exc:                                    # noqa: BLE001
        problems.append(f"无法核对过去的触发记录：{exc}")

    if not problems:
        print("自检通过")
        return 0
    body = "\n\n".join(f"- {p}" for p in problems)
    print("::error::定时触发自检发现问题\n" + body)
    notify.send("[模拟盘·异常] 定时触发自检失败", body)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 Cloudflare Cron / 每日自检")
    ap.add_argument("cmd", choices=["sync", "check"])
    ap.add_argument("--now", default="", help="复现用 UTC 时刻，如 2026-11-20 20:35")
    args = ap.parse_args()
    now = (dt.datetime.fromisoformat(args.now).replace(tzinfo=UTC)
           if args.now else dt.datetime.now(UTC))
    try:
        return cmd_sync(now) if args.cmd == "sync" else cmd_check(now)
    except Exception as exc:                                    # noqa: BLE001
        msg = f"{args.cmd} 失败：{type(exc).__name__}: {exc}"
        print(f"::error::{msg}", file=sys.stderr)
        if args.cmd == "sync":
            notify.send("[模拟盘·异常] 下一次触发时刻未能更新",
                        f"{msg}\n\n闭环可能断链，下次自检（每日 12:15Z）会再尝试修复。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
