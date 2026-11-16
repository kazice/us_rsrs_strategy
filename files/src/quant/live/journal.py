"""模拟盘运行日志：把每次运行的输入快照与判断落盘，供事后回溯与重放。

为什么不能用 GitHub Actions 的 run log
------------------------------------
Actions 的 stdout 默认只留 90 天（可调最长 400 天），只能按 job 翻页、不可查询，
过期即无从查证；runner 是一次性的，唯一能长期留住的只有 git。所以这里做两件事：

1. **运行流水** ``runs/YYYY-MM.jsonl``：每次运行一行，**包括中止、空跑、失败**。
   没有这一行，「那天为什么没收到信号」只能去翻 90 天就消失的 run log。
2. **决策快照** ``decisions/YYYY-MM/<asof>_<run_id>/``：完整输入与输出，可重放。
   推送里只有结论，事后追问「为什么不是 X」只能靠这张表。

写入纪律（都是踩过的坑）
------------------------
- **先写快照，再推送**：否则会出现「消息到了手机、仓库里没有痕迹」；
- 快照先写临时目录再 ``rename``，避免半截文件被当成完整记录；
- **append-only**：不修改历史文件，只追加；按月滚文件，避免单文件无限增长；
- ``run_id`` 撞车（同一天重跑）自动加序号，**绝不覆盖**已有快照；
- 落盘失败只记 warning，不中断信号流程——日志系统坏了不该让交易决策也坏。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from quant.live import calendar as cal_mod
from quant.live.books import state_dir

log = logging.getLogger(__name__)

RUNS_DIR = "runs"
DECISIONS_DIR = "decisions"

QUOTE_COLUMNS = ["symbol", "price", "prev_close", "high", "low", "volume",
                 "turnover_pct", "total_shares", "ts", "src"]


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def run_id() -> str:
    """优先用 CI 注入的 ``TA_RUN_ID``（GITHUB_RUN_ID），本地跑用时间戳。

    本地 id 以 ``L`` 开头，与 CI 的数字 id 一眼能区分——排查时不必猜这条记录从哪来。
    """
    env = os.getenv("TA_RUN_ID", "").strip()
    if env:
        return env
    return "L" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")


def git_sha() -> str:
    env = os.getenv("GITHUB_SHA", "").strip()
    if env:
        return env[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=project_root(), capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:                                   # noqa: BLE001
        return ""


class Journal:
    """一次运行一个实例。``finish`` 必须被调用，否则这次运行不会留下流水。"""

    def __init__(self, kind: str, asof: Any, *, dry_run: bool = False,
                 enabled: bool = True) -> None:
        self.kind = kind
        self.asof = pd.Timestamp(asof).normalize()
        self.dry_run = dry_run
        self.enabled = enabled

        self.run_id = run_id()
        self.t0 = dt.datetime.now(dt.timezone.utc)
        self.sha = git_sha()
        self.decision_dir: str = ""        # 相对 state_dir 的路径，写进 runs 行
        self.pick = ""
        self.sector = ""
        self.notify: str = "skipped"       # skipped / ok / failed
        self.extra: dict[str, Any] = {}

    # ------------------------------------------------------------------ 快照
    def snapshot(self, sig, quotes: dict, *, title: str = "", content: str = "",
                 action: str = "", detail: str = "", pos=None,
                 holding_ret: float | None = None) -> Path | None:
        """落一份决策快照。返回目录；失败返回 None（只记 warning，不抛）。"""
        if not self.enabled:
            return None
        try:
            root = state_dir() / DECISIONS_DIR / self.asof.strftime("%Y-%m")
            root.mkdir(parents=True, exist_ok=True)
            name = f"{self.asof.strftime('%Y-%m-%d')}_{self.run_id}"
            target = root / name
            seq = 1
            while target.exists():                      # 重跑不覆盖，加序号
                seq += 1
                target = root / f"{name}-{seq}"

            tmp = root / (target.name + ".tmp")
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)

            self._write_meta(tmp, sig, quotes)
            self._write_quotes(tmp, quotes)
            self._write_candidates(tmp, sig)
            self._write_sectors(tmp, sig)
            self._write_decision(tmp, sig, action=action, detail=detail,
                                 pos=pos, holding_ret=holding_ret)
            if content:
                (tmp / "push.md").write_text(
                    f"# {title}\n\n{content}\n", encoding="utf-8")

            tmp.rename(target)                          # 原子落位
            # 一律存 posix 路径：CI 在 Linux 写、本地 Windows 读，反斜杠跨平台不认
            self.decision_dir = target.relative_to(state_dir()).as_posix()
            self.pick = sig.pick
            self.sector = sig.sector
            return target
        except Exception:                               # noqa: BLE001
            log.exception("写决策快照失败（不影响信号）")
            return None

    def _write_meta(self, d: Path, sig, quotes: dict) -> None:
        from quant.live import mcap as mcap_mod
        from quant.live import signal as signal_mod
        lag = None
        ts = [q.ts for q in quotes.values() if q.ts is not None]
        if ts:
            ref = self.t0.astimezone(cal_mod.ET)
            lag = round(sorted((ref - t).total_seconds() / 60.0 for t in ts)[len(ts) // 2], 1)
        meta = {
            "run_id": self.run_id,
            "ts_utc": self.t0.isoformat(timespec="seconds"),
            "ts_et": self.t0.astimezone(cal_mod.ET).isoformat(timespec="seconds"),
            "asof": str(self.asof.date()),
            "kind": self.kind,
            "dry_run": self.dry_run,
            "git_sha": self.sha,
            "pool": {"size": sig.pool_size, "as_of": sig.pool_as_of},
            "quotes": {"count": len(quotes), "lag_min": lag},
            "params": {
                "rsrs_n": signal_mod.RSRS_N, "rsrs_m": signal_mod.RSRS_M,
                "stack": list(signal_mod.STACK), "turn_cap": signal_mod.TURN_CAP,
                "turnma_top": signal_mod.TURNMA_TOP,
                "turnma_sectors": signal_mod.TURNMA_SECTORS,
                "min_mcap": mcap_mod.MIN_MCAP,
            },
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_quotes(self, d: Path, quotes: dict) -> None:
        rows = []
        for q in quotes.values():
            rows.append({
                "symbol": q.symbol, "price": q.price, "prev_close": q.prev_close,
                "high": q.high, "low": q.low, "volume": q.volume,
                "turnover_pct": q.turnover_pct, "total_shares": q.total_shares,
                "ts": q.ts.isoformat() if q.ts else "", "src": q.src,
            })
        pd.DataFrame(rows, columns=QUOTE_COLUMNS).to_csv(
            d / "quotes.csv", index=False, encoding="utf-8-sig")

    def _write_candidates(self, d: Path, sig) -> None:
        tbl = getattr(sig, "pool_table", pd.DataFrame())
        if tbl is None or len(tbl) == 0:
            tbl = pd.DataFrame(columns=["symbol", "reason"])
        tbl.to_csv(d / "candidates.csv", index=False, encoding="utf-8-sig")

    def _write_sectors(self, d: Path, sig) -> None:
        s = sig.sector_scores
        pd.DataFrame({"sector": s.index, "rsrs": s.values,
                      "rank": range(1, len(s) + 1)}).to_csv(
            d / "sectors.csv", index=False, encoding="utf-8-sig")

    def _write_decision(self, d: Path, sig, *, action: str, detail: str,
                        pos, holding_ret: float | None) -> None:
        payload = {
            "run_id": self.run_id,
            "asof": str(self.asof.date()),
            "kind": self.kind,
            "dry_run": self.dry_run,
            "action": action,
            "detail": detail,
            "pick": sig.pick,
            "sector": sig.sector,
            "price": None if pd.isna(sig.price) else float(sig.price),
            "holding": None if pos is None or pos.is_empty() else {
                "symbol": pos.symbol, "sector": pos.sector,
                "entry_date": pos.entry_date, "entry_price": pos.entry_price,
                "nav": pos.nav,
            },
            "holding_return": None if holding_ret is None else round(float(holding_ret), 6),
            "turnover_median": None if pd.isna(sig.turnover_median) else float(sig.turnover_median),
            "notes": list(sig.notes),
            "notify_ok": self.notify,
        }
        (d / "decision.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def patch_decision(self, **kw: Any) -> None:
        """推送之后回填结果（notify_ok 等）。文件不存在就跳过。"""
        if not self.enabled or not self.decision_dir:
            return
        path = state_dir() / self.decision_dir / "decision.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            payload.update(kw)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception:                               # noqa: BLE001
            log.warning("回填 decision.json 失败：%s", path)

    # ------------------------------------------------------------------ 流水
    def finish(self, code: int, status: str, reason: str = "") -> int:
        """记一行运行流水并返回退出码。``status``: ok / noop / abort / error / dryrun。"""
        if not self.enabled:
            return code
        try:
            self._append_run(status, reason, code)
        except Exception:                               # noqa: BLE001
            log.exception("写运行流水失败（不影响退出码）")
        return code

    def _append_run(self, status: str, reason: str, code: int) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        row = {
            "run_id": self.run_id,
            "ts_utc": now.isoformat(timespec="seconds"),
            "ts_et": now.astimezone(cal_mod.ET).isoformat(timespec="seconds"),
            "asof": str(self.asof.date()),
            "kind": self.kind,
            "status": status,
            "reason": reason,
            "dry_run": self.dry_run,
            "git_sha": self.sha,
            "duration_s": round((now - self.t0).total_seconds(), 1),
            "notify": self.notify,
            "pick": self.pick,
            "sector": self.sector,
            "decision_dir": self.decision_dir,
            "code": code,
        }
        row.update(self.extra)
        path = state_dir() / RUNS_DIR / f"{now.strftime('%Y-%m')}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_runs(path: Path) -> list[dict]:
    """读一个月度 runs 文件（坏行跳过，不让一条脏数据毁掉整份审计）。"""
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("跳过坏行：%s", line[:80])
    return rows


def all_runs() -> list[dict]:
    root = state_dir() / RUNS_DIR
    if not root.exists():
        return []
    rows: list[dict] = []
    for p in sorted(root.glob("*.jsonl")):
        rows += read_runs(p)
    return sorted(rows, key=lambda r: r.get("ts_utc", ""))


def load_decision(decision_dir: str) -> dict:
    """读一份快照的 decision.json（``decision_dir`` 是相对 state_dir 的路径）。"""
    path = state_dir() / decision_dir / "decision.json"
    if not path.exists():
        raise SystemExit(f"找不到决策快照：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_dir(decision_dir: str) -> Path:
    return state_dir() / decision_dir
