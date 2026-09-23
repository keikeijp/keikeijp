"""指標候補を一括でふるいにかけ、一覧表にまとめる。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .evaluate import (
    benjamini_hochberg,
    distribution_report,
    forward_returns,
    information_coefficient,
    monotonicity,
    quantile_returns,
    subperiod_ic,
    zscore_strategy,
)
from .indicators import zscore

# |IC| の目安: 0.05 以上で「優秀」、0.1 以上で「非常に優秀」
IC_GOOD = 0.05
IC_EXCELLENT = 0.10


def grade(ic: float) -> str:
    if not np.isfinite(ic):
        return "-"
    a = abs(ic)
    return "非常に優秀" if a >= IC_EXCELLENT else "優秀" if a >= IC_GOOD else "弱い"


def screen(close: pd.Series, indicators: pd.DataFrame, horizon: int = 1,
           z_window: int = 250, threshold: float = 1.0, quantiles: int = 5,
           n_periods: int = 5, fdr: float = 0.05, periods_per_year: int = 252,
           cost: float = 0.0) -> pd.DataFrame:
    """各指標を Z スコア化し、IC・分位の単調性・期間ごとの安定性・資産曲線で評価する。

    列の意味:
      rank_ic / ic      … Z とリターンの順位相関 / Pearson 相関
      q_value           … 試した指標の数で補正した p 値 (Benjamini-Hochberg)
      monotonicity      … 分位別リターンの階段らしさ (±1 が理想)
      sign_stability    … 期間を分けたとき、全体と同じ符号の IC が出た割合
      sharpe            … Z が ±threshold を超えたときに建てた場合の年率シャープレシオ
      candidate         … 上の条件をすべて満たしたもの。最後は「なぜ効くのか」を必ず人が説明すること
    """
    fwd = forward_returns(close, horizon)
    rows = []
    for name in indicators.columns:
        z = zscore(indicators[name], window=z_window)
        res = information_coefficient(z, fwd, horizon=horizon)
        qr = quantile_returns(z, fwd, q=quantiles) if res.n >= quantiles * 10 else None
        mono = monotonicity(qr) if qr is not None else np.nan
        sub = subperiod_ic(z, fwd, n_periods=n_periods)
        sign = np.sign(res.rank_ic)
        stability = float((np.sign(sub.dropna()) == sign).mean()) if sub.notna().any() else np.nan
        direction = 1 if (res.rank_ic or 0) >= 0 else -1
        strat = zscore_strategy(z, close, threshold=threshold, direction=direction, cost=cost)
        pnl = strat["pnl"]
        sharpe = (float(pnl.mean() / pnl.std() * np.sqrt(periods_per_year))
                  if len(pnl) > 1 and pnl.std() > 0 else np.nan)
        dist = distribution_report(indicators[name])
        rows.append({
            "indicator": name, "rank_ic": res.rank_ic, "ic": res.ic,
            "ic_ci": (round(res.ci_low, 4), round(res.ci_high, 4)) if np.isfinite(res.ci_low) else None,
            "p_value": res.p_value, "grade": grade(res.rank_ic), "monotonicity": mono,
            "sign_stability": stability, "sharpe": sharpe,
            "total_return": float(pnl.sum()) if len(pnl) else np.nan,
            "time_in_market": float((strat["position"] != 0).mean()) if len(strat) else np.nan,
            "non_normal": dist["non_normal"], "n": res.n,
        })
    out = pd.DataFrame(rows).set_index("indicator")
    out["q_value"] = benjamini_hochberg(out["p_value"])
    out["candidate"] = (
        (out["rank_ic"].abs() >= IC_GOOD)
        & (out["q_value"] <= fdr)
        & (out["monotonicity"].abs() >= 0.8)
        & (out["sign_stability"] >= 0.8)
    )
    cols = ["rank_ic", "ic", "ic_ci", "grade", "p_value", "q_value", "monotonicity",
            "sign_stability", "sharpe", "total_return", "time_in_market", "non_normal", "n",
            "candidate"]
    return out[cols].sort_values("rank_ic", key=lambda s: s.abs(), ascending=False)
