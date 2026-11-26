"""一键更新：成分股 → 个股行情 → ETF 行情。

顺序不能乱：
1. `update_members.py`  抓 GitHub 的 S&P 500 名单，分配 permno，写入当日成员
2. `update.py`         按 permno_map 拉个股日线 + 股本，算全部指标
3. `update_etf.py`     拉 12 个 ETF/SPY 的日线与 RSRS

用法：
    python upload/update_all.py
    python upload/update_all.py --limit 20      # 小样本（透传给 update.py）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    ("成分股", "update_members.py", []),
    ("个股行情", "update.py", ["--limit"]),
    ("ETF 行情", "update_etf.py", []),
]


def main() -> int:
    extra = sys.argv[1:]
    for name, script, accepts in STEPS:
        cmd = [sys.executable, str(HERE / script)]
        if accepts:
            cmd += extra
        print("\n" + "=" * 70)
        print(f"[{name}] {script} {' '.join(extra) if accepts else ''}")
        print("=" * 70)
        r = subprocess.run(cmd, cwd=HERE.parent)
        if r.returncode != 0:
            print(f"[失败] {script} 退出码 {r.returncode}")
            return r.returncode
    print("\n" + "=" * 70)
    print("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
