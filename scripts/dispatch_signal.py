"""准点触发器：到「收盘前 30 分钟」就调 GitHub API 手动触发 paper-trade workflow。

为什么需要它
------------
GitHub Actions 的 schedule 实测会延迟数小时甚至整段丢失（2026-11-17 的 5 次定时运行全部
落在 UTC 00:24~04:03，而 cron 设的是 16:30~21:30Z），根本赶不上 15:30 ET 的窗口。
workflow_dispatch 是 API 直触，没有 schedule 的排队，通常几十秒内起跑。

用法
----
由一个**准时的外部定时器**每 5 分钟跑一次本脚本（Windows 任务计划 / 云服务器 cron 均可），
脚本自己判断今天是不是推送日、现在是不是到点，不到点或非推送日直接退出：

    set GH_DISPATCH_TOKEN=github_pat_xxx      # 仅需该仓库 Actions: Read and write
    python -m scripts.dispatch_signal

重复触发由两层兜住：本脚本的本地标记文件、以及 live_signal 账本里的幂等键。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from quant.live import calendar

REPO = "kazice/us_rsrs_strategy"
WORKFLOW = "paper_trade.yml"
MARK = Path(os.getenv("TA_DISPATCH_MARK", "")
            or Path(__file__).resolve().parents[1] / "state" / "dispatch_mark.json")


def _dispatch(token: str, ref: str) -> None:
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    body = json.dumps({"ref": ref, "inputs": {"kind": "auto"}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paper-trade-dispatch",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 204:
            raise RuntimeError(f"dispatch 返回 {resp.status}")


def main() -> int:
    ap = argparse.ArgumentParser(description="到点触发 paper-trade workflow")
    ap.add_argument("--ref", default="main", help="workflow 所在分支")
    ap.add_argument("--now", default="", help="复现用 UTC 时刻，如 2026-11-30 20:31")
    ap.add_argument("--late-min", type=int, default=30,
                    help="过了触发时刻多少分钟内仍补触发（应小于 live_signal 的 --late-min）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = (dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc)
           if args.now else dt.datetime.now(dt.timezone.utc))
    today = now.astimezone(calendar.ET).date()
    occ = calendar.occasion_for(today)
    if occ.kind == "none" or occ.trigger_utc is None:
        print(f"{today} 无需推送：{occ.reason}")
        return 0

    delta = (now - occ.trigger_utc).total_seconds() / 60
    if not 0 <= delta <= args.late_min:
        print(f"未到点或已过期：距触发时刻 {delta:+.1f} 分钟（触发 {occ.trigger_utc:%H:%MZ}）")
        return 0

    try:
        done = json.loads(MARK.read_text(encoding="utf-8")) if MARK.exists() else {}
    except json.JSONDecodeError:
        done = {}
    if done.get("date") == today.isoformat():
        print(f"{today} 已触发过，跳过")
        return 0

    if args.dry_run:
        print(f"[dry-run] 将触发 {REPO}@{args.ref}（{occ.kind}，delta {delta:+.1f} 分钟）")
        return 0

    token = os.getenv("GH_DISPATCH_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 GH_DISPATCH_TOKEN", file=sys.stderr)
        return 2
    try:
        _dispatch(token, args.ref)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"触发失败：{exc}（下一轮会重试）", file=sys.stderr)
        return 1

    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps({"date": today.isoformat(), "at": now.isoformat()}),
                    encoding="utf-8")
    print(f"已触发 {occ.kind}：{today}，距触发时刻 {delta:+.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
