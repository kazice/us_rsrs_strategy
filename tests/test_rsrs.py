"""RSRS 口径：z-score 的标准化窗口不含当日（shift(1)），strict 过滤后记 0 而不是 NaN。

用逐点 OLS 的实现做交叉验证 —— 这正是 ``rsrs_panel_fast`` 声称"已逐值比对"的那份口径。
"""
import numpy as np
import pandas as pd

N, M = 20, 40


def _panels(days: int = 160, seed: int = 7):
    idx = pd.bdate_range("2026-01-02", periods=days)
    rng = np.random.default_rng(seed)
    low = 100 + np.cumsum(rng.normal(0, 0.4, days))
    high = low + rng.uniform(0.05, 0.30, days)      # 噪声很小 → R² 基本都过 0.5
    return (pd.DataFrame({"A": high}, index=idx),
            pd.DataFrame({"A": low}, index=idx))


def _brute_z(high: pd.Series, low: pd.Series) -> pd.Series:
    """逐点 np.polyfit 的慢速实现：beta/r2 → 过滤 → shift(1) 标准化。"""
    idx = high.index
    raw = []
    for i in range(len(idx)):
        if i < N - 1:
            raw.append(np.nan)
            continue
        h = high.iloc[i - N + 1:i + 1].to_numpy(float)
        l = low.iloc[i - N + 1:i + 1].to_numpy(float)
        beta, alpha = np.polyfit(l, h, 1)
        ss_tot = float(((h - h.mean()) ** 2).sum())
        ss_res = float(((h - (alpha + beta * l)) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
        ok = np.isfinite(r2) and r2 >= 0.5 and abs(beta) <= 10.0
        raw.append(beta * r2 if ok else np.nan)
    raw = pd.Series(raw, index=idx)

    z = []
    for i in range(len(idx)):
        if i < N - 1 + M:
            z.append(np.nan)
            continue
        hist = raw.iloc[max(0, i - M):i].dropna()      # 不含当日
        if not np.isfinite(raw.iloc[i]) or len(hist) < 5 or hist.std(ddof=0) == 0:
            z.append(0.0)
            continue
        z.append((raw.iloc[i] - hist.mean()) / hist.std(ddof=0))
    return pd.Series(z, index=idx)


def test_matches_brute_force_ols():
    from scripts.sector_stock_rsrs import rsrs_panel_fast

    hi, lo = _panels()
    fast = rsrs_panel_fast(hi, lo, N, M, "strict")["A"]
    slow = _brute_z(hi["A"], lo["A"])

    both = fast.notna() & slow.notna()
    assert both.sum() > 50, "可比样本太少，测试无效"
    # 容差 1e-6：向量化版本用「协方差」算 β，价格量级 ~100 时会有 ~1e-9 的抵消误差，
    # 实测最大差 2.8e-9；口径（是否 shift、ddof、过滤条件）完全一致才是这里要钉的。
    diff = float(np.abs(fast[both].to_numpy() - slow[both].to_numpy()).max())
    assert diff < 1e-6, f"与逐点 OLS 的最大偏差 {diff:.2e} 过大"
    assert fast.isna().equals(slow.isna()), "预热段（前 N-1+M 行）应同为 NaN"


def test_appending_a_bar_does_not_rewrite_history():
    """追加一根新 bar 不能改变已有日期的分数（否则就是前视）。"""
    from scripts.sector_stock_rsrs import rsrs_panel_fast

    hi, lo = _panels()
    before = rsrs_panel_fast(hi, lo, N, M, "strict")

    new_idx = hi.index[-1] + pd.Timedelta(days=1)
    hi2 = pd.concat([hi, pd.DataFrame({"A": [hi["A"].iloc[-1] * 1.1]}, index=[new_idx])])
    lo2 = pd.concat([lo, pd.DataFrame({"A": [lo["A"].iloc[-1] * 1.05]}, index=[new_idx])])
    after = rsrs_panel_fast(hi2, lo2, N, M, "strict")

    assert after.loc[before.index].equals(before)


def test_strict_filter_masks_low_r2_to_zero():
    """high/low 完全不相关时 R²≈0，strict 口径应记 0（不是 NaN，也不是负分）。"""
    from scripts.sector_stock_rsrs import rsrs_panel_fast

    idx = pd.bdate_range("2026-01-02", periods=120)
    cycle = np.array([1.0, 1.0, -1.0, -1.0])
    l = 100 + cycle[np.arange(120) % 4]
    h = 100 + cycle[(np.arange(120) + 1) % 4]
    hi = pd.DataFrame({"A": h}, index=idx)
    lo = pd.DataFrame({"A": l}, index=idx)

    z = rsrs_panel_fast(hi, lo, N, M, "strict")["A"].dropna()
    assert len(z) > 10
    assert (z == 0.0).all()
