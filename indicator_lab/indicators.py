"""投資指標の候補を作る。

どの指標も時点 t までの価格だけで計算する (先読みしない)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def zscore(x: pd.Series, window: int = 250, min_periods: int | None = None) -> pd.Series:
    """指標の raw 値を過去 window 本の分布で正規化したスコア Z。

    全期間の平均・標準偏差で正規化すると未来の情報が混ざるため、ローリングで計算する。
    """
    min_periods = min_periods or max(20, window // 2)
    mean = x.rolling(window, min_periods=min_periods).mean()
    std = x.rolling(window, min_periods=min_periods).std()
    return (x - mean) / std.replace(0.0, np.nan)


def ma_deviation(close: pd.Series, window: int) -> pd.Series:
    """移動平均乖離率 (close / MA - 1)。"""
    return close / close.rolling(window).mean() - 1.0


def momentum(close: pd.Series, window: int) -> pd.Series:
    """window 本前からの騰落率。"""
    return close.pct_change(window)


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder の RSI (0-100)。"""
    diff = close.diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


def volatility_ratio(close: pd.Series, short: int = 5, long: int = 60) -> pd.Series:
    """短期ボラティリティ / 長期ボラティリティ。"""
    r = np.log(close).diff()
    return r.rolling(short).std() / r.rolling(long).std()


def candidate_indicators(close: pd.Series) -> pd.DataFrame:
    """よく使われるテクニカル指標を一式そろえる (raw 値)。

    候補はいくらでも増やせるが、試した数が多いほどデータ・スヌーピングの危険が増す。
    screen() は試した数に応じて p 値を補正するので、候補は正直に全部渡すこと。
    """
    cols = {}
    for w in (5, 15, 25, 75):
        cols[f"ma_dev_{w}"] = ma_deviation(close, w)
    for w in (5, 20, 60):
        cols[f"mom_{w}"] = momentum(close, w)
    cols["rsi_14"] = rsi(close, 14)
    cols["vol_ratio_5_60"] = volatility_ratio(close, 5, 60)
    return pd.DataFrame(cols, index=close.index)
