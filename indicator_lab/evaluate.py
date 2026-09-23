"""投資指標とリターンの関係を評価する。

アクティブ運用の基本式 E[r | Z] ≈ IC · σr · Z に沿って、
IC (情報係数) とその確からしさ、時系列での安定性、分位別リターンの形を調べる。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats


def forward_returns(close: pd.Series, horizon: int = 1) -> pd.Series:
    """時点 t から t+horizon までの騰落率。時点 t の指標と対にして評価する。"""
    return close.shift(-horizon) / close - 1.0


def _aligned(indicator: pd.Series, returns: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    df = pd.concat([indicator, returns], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    return df.iloc[:, 0].to_numpy(float), df.iloc[:, 1].to_numpy(float)


@dataclass
class ICResult:
    ic: float            # Pearson 相関 (記事の IC)
    rank_ic: float       # Spearman 相関 (分布の歪みや外れ値に強い)
    n: int               # 観測数
    n_eff: float         # 重複リターンを考慮した実効サンプル数
    p_value: float       # rank_ic = 0 の両側検定
    ci_low: float        # IC の 95% 信頼区間 (Fisher z)
    ci_high: float
    r2: float            # 決定係数 (= IC^2)


def information_coefficient(indicator: pd.Series, returns: pd.Series, horizon: int = 1) -> ICResult:
    """指標とリターンの相関 (IC) と、その確からしさ。

    horizon > 1 のリターンは期間が重なっていて観測が独立でないため、
    実効サンプル数を n / horizon として p 値と信頼区間を計算する (甘い判定を防ぐ)。
    """
    x, y = _aligned(indicator, returns)
    n = len(x)
    if n < 10 or np.std(x) == 0 or np.std(y) == 0:
        return ICResult(np.nan, np.nan, n, np.nan, np.nan, np.nan, np.nan, np.nan)
    ic = float(np.corrcoef(x, y)[0, 1])
    rank_ic = float(stats.spearmanr(x, y).statistic)
    n_eff = n / max(1, horizon)
    if n_eff > 3:
        r = np.clip(rank_ic, -0.999999, 0.999999)
        t = r * np.sqrt((n_eff - 2) / (1 - r * r))
        p = float(2 * stats.t.sf(abs(t), df=n_eff - 2))
        z, se = np.arctanh(np.clip(ic, -0.999999, 0.999999)), 1 / np.sqrt(n_eff - 3)
        lo, hi = float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se))
    else:
        p, lo, hi = np.nan, np.nan, np.nan
    return ICResult(ic, rank_ic, n, n_eff, p, lo, hi, ic * ic)


def quantile_returns(indicator: pd.Series, returns: pd.Series, q: int = 5) -> pd.DataFrame:
    """指標の大きさで q 個のグループに分け、グループ毎の平均リターンを出す。

    素性の良い指標なら平均リターンが階段状 (単調) に並ぶ。
    """
    x, y = _aligned(indicator, returns)
    df = pd.DataFrame({"x": x, "y": y})
    df["bucket"] = pd.qcut(df["x"].rank(method="first"), q, labels=False) + 1
    out = df.groupby("bucket").agg(x_mean=("x", "mean"), mean_return=("y", "mean"),
                                    std_return=("y", "std"), count=("y", "size"))
    return out


def monotonicity(qr: pd.DataFrame) -> float:
    """分位番号と平均リターンの順位相関。±1 に近いほど階段状。"""
    if len(qr) < 3:
        return np.nan
    return float(stats.spearmanr(qr.index, qr["mean_return"]).statistic)


def binned_expectation(indicator: pd.Series, returns: pd.Series, bins: int = 10) -> pd.DataFrame:
    """指標の値の区間ごとのリターン期待値 (等間隔ビン)。

    IC の低い指標で「たまたま平均が高い区間」を切り出して条件にするのはカーブフィッティング。
    その区間の件数と t 値も併記するので、偶然かどうかの目安にする。
    """
    x, y = _aligned(indicator, returns)
    df = pd.DataFrame({"x": x, "y": y})
    df["bin"] = pd.cut(df["x"], bins)
    g = df.groupby("bin", observed=True)["y"]
    out = pd.DataFrame({"mean_return": g.mean(), "count": g.size(), "std": g.std()})
    out["t_stat"] = out["mean_return"] / (out["std"] / np.sqrt(out["count"]))
    return out


def zscore_strategy(z: pd.Series, close: pd.Series, threshold: float = 1.0,
                    direction: int = 1, cost: float = 0.0) -> pd.DataFrame:
    """Z > threshold でロング、Z < -threshold でショートした場合の資産曲線。

    direction=-1 で逆張り (IC が負の指標)。シグナルは時点 t の終値で建て、
    t→t+1 のリターンを受け取る。cost は建玉変更 1 単位あたりの片道コスト (比率)。
    """
    pos = pd.Series(0.0, index=z.index)
    pos[z > threshold] = 1.0
    pos[z < -threshold] = -1.0
    pos = pos * direction
    ret1 = forward_returns(close, 1)
    pnl = pos * ret1 - cost * pos.diff().abs().fillna(pos.abs())
    pnl = pnl.dropna()
    return pd.DataFrame({"position": pos.reindex(pnl.index), "pnl": pnl,
                         "equity": pnl.cumsum()})


def subperiod_ic(indicator: pd.Series, returns: pd.Series, n_periods: int = 5) -> pd.Series:
    """期間を n_periods 個に等分し、それぞれの rank IC を出す。

    全期間の IC が高くても、特定の期間だけで稼いでいる指標は危うい。
    """
    df = pd.concat([indicator, returns], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    out = {}
    for i, chunk in enumerate(np.array_split(np.arange(len(df)), n_periods)):
        part = df.iloc[chunk]
        label = f"{part.index[0]}〜{part.index[-1]}" if len(part) else str(i)
        out[label] = (float(stats.spearmanr(part.iloc[:, 0], part.iloc[:, 1]).statistic)
                      if len(part) >= 10 else np.nan)
    return pd.Series(out, name="rank_ic")


def distribution_report(x: pd.Series) -> dict:
    """分布の形。正規分布から大きく外れると Pearson IC や Z スコアが誤解を招く。"""
    v = x.replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
    if len(v) < 10:
        return {"skew": np.nan, "excess_kurtosis": np.nan, "jb_p": np.nan, "non_normal": False}
    skew, kurt = float(stats.skew(v)), float(stats.kurtosis(v))
    jb_p = float(stats.jarque_bera(v).pvalue)
    return {"skew": skew, "excess_kurtosis": kurt, "jb_p": jb_p,
            "non_normal": bool(abs(skew) > 1.0 or kurt > 3.0)}


def benjamini_hochberg(p_values: pd.Series) -> pd.Series:
    """多重検定の補正 (FDR)。たくさんの指標を試した分だけ p 値を厳しくする。"""
    p = p_values.astype(float)
    mask = p.notna()
    ranked = p[mask].sort_values()
    m = len(ranked)
    if m == 0:
        return p
    adj = ranked * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1].clip(upper=1.0)
    out = pd.Series(np.nan, index=p.index)
    out[adj.index] = adj
    return out
