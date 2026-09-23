"""indicator_lab: シストレ用の投資指標を探索・評価する道具箱。

情報係数 (IC) を軸に、
「散布図 → 分位別リターン → 時系列の累積リターン → 多重検定の補正 → 特徴選択」
の順で投資指標をふるいにかける。考え方は docs/INDICATOR_SEARCH.md を参照。
"""

from .evaluate import (
    binned_expectation,
    distribution_report,
    forward_returns,
    information_coefficient,
    quantile_returns,
    subperiod_ic,
    zscore_strategy,
)
from .indicators import candidate_indicators, zscore
from .screen import screen
from .select import extra_trees_importance, lasso_select, regime_ic

__all__ = [
    "binned_expectation",
    "candidate_indicators",
    "distribution_report",
    "extra_trees_importance",
    "forward_returns",
    "information_coefficient",
    "lasso_select",
    "quantile_returns",
    "regime_ic",
    "screen",
    "subperiod_ic",
    "zscore",
    "zscore_strategy",
]
