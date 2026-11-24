"""外部行情 → 池层的口径对齐（补预热段时用）。

两步，缺一不可：

1. `split_basis(df, source)`：把外部价压到「仅拆分复权」口径。
   Yahoo 的 `close` 本身就是拆股调整后、不含分红的价格（实测 AAPL 2020-01-02
   close=75.0875 = 300.35/4，与 CRSP `DlyClose/DlyCumFacPr` 同口径），直接拿来用；
   再乘一次拆股因子就等于把拆股做了两遍，2021 年后有拆股的票会整体错 4~40 倍。
   tiingo 的 `close` 是未拆股调整的原始价，得自己按 `stock_splits` 补因子。
   腾讯 qfq / alpaca 只有总收益口径，拆股口径拿不到——保持原样交给下面的 cv 闸挡。

2. `align(ext, c, col)`：用重叠区间的「CRSP 复权价 ÷ 外部价」把外部价缩进 CRSP 的价格空间。
   只做整体缩放（取中位数），不逐日抹平——逐日对齐会把真实走势差异也一起吃掉，
   反而更难发现口径问题；比值离散度（cv）超阈值就标 review，不自动并入。

这里只依赖 pandas，不碰任何主数据层（`master` / `crsp` 那两层已经删掉）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_RATIO_CV = 0.005      # 重叠 ≥20 天时的比值离散度上限
MIN_OVERLAP = 20          # 低于这个重叠天数判 insufficient_overlap


def split_basis(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """压到「仅拆分复权」口径，新增一列 `adj_close_crsp` 供对齐使用。"""
    out = df.copy()
    if "close" not in out.columns and "adj_close" not in out.columns:
        raise ValueError("外部行情既没有 close 也没有 adj_close，无法对齐")
    basis = out["close"] if "close" in out.columns else out["adj_close"]

    if source == "tiingo" and "stock_splits" in out.columns:
        sp = pd.to_numeric(out["stock_splits"], errors="coerce").fillna(1.0)
        if bool((sp != 1.0).any()):
            # 某天之前的拆股因子 = 该天之后所有拆股的连乘（不含当天）
            fut = sp[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
            basis = pd.to_numeric(out["close"], errors="coerce") * fut

    out["adj_close_crsp"] = pd.to_numeric(basis, errors="coerce")
    return out


def align(ext: pd.DataFrame, c: pd.DataFrame, col: str = "adj_close",
          min_overlap: int = MIN_OVERLAP) -> tuple[pd.Series | None, dict]:
    """返回 (缩放系数 Series 或 None, 统计字典)。

    `ext` 是外部行情（`col` 指定要比的那一列），`c` 是池层行情，必须含 `adj_close`。
    """
    a = pd.to_numeric(ext[col], errors="coerce").dropna()
    b = pd.to_numeric(c["adj_close"], errors="coerce").dropna()
    common = a.index.intersection(b.index)
    ov = int(len(common))
    if ov < max(2, int(min_overlap)):
        return None, {"overlap_days": ov, "ratio_median": np.nan,
                      "ratio_cv": np.nan, "status": "insufficient_overlap"}

    r = (b.loc[common] / a.loc[common]).replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return None, {"overlap_days": ov, "ratio_median": np.nan,
                      "ratio_cv": np.nan, "status": "ratio_empty"}

    med = float(r.median())
    cv = float(r.std() / med) if med != 0 else np.inf
    status = "ok" if cv <= MAX_RATIO_CV else "review"
    scale = pd.Series(med, index=ext.index)
    return scale, {"overlap_days": ov, "ratio_median": round(med, 6),
                   "ratio_cv": round(cv, 6), "status": status}
