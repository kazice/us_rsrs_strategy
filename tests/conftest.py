"""把仓库根、src、tests 都放进 sys.path，测试不依赖安装包、不依赖 PYTHONPATH。

这样 `pytest` 在仓库根目录直接跑就能找到 ``quant.*`` 与 ``scripts.*``；
所有测试只用 stdlib + pandas，不碰 data 分支、不联网。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = _HERE.parents[1]

for p in (ROOT / "tests", ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
