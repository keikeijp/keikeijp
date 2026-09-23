"""複数の指標から有効なものを選ぶ (特徴選択) と、レジーム別の評価。

金融データはノイズが大きく、表現力の高いモデルほど誤ったフィッティングに陥りやすい。
ここでは線形の LASSO と、深さ 2 に抑えた ExtraTrees だけを使う。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .evaluate import information_coefficient


def _xy(features: pd.DataFrame, returns: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    df = features.join(returns.rename("__y__"), how="inner")
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    return df.drop(columns="__y__"), df["__y__"]


def lasso_select(features: pd.DataFrame, returns: pd.Series, n_splits: int = 5,
                 random_state: int = 0) -> pd.Series:
    """LassoCV (時系列分割の交差検証) で係数が残った指標を返す。

    特徴量は標準化してから渡すので、係数の大きさはそのまま比較できる。
    係数が 0 の指標は「他の指標と合わせたときに説明力を持たない」とみなす。
    """
    from sklearn.linear_model import LassoCV
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler

    X, y = _xy(features, returns)
    Xs = StandardScaler().fit_transform(X)
    model = LassoCV(cv=TimeSeriesSplit(n_splits=n_splits), random_state=random_state,
                    max_iter=20000).fit(Xs, y.to_numpy())
    coef = pd.Series(model.coef_, index=X.columns, name="lasso_coef")
    return coef.reindex(coef.abs().sort_values(ascending=False).index)


def extra_trees_importance(features: pd.DataFrame, returns: pd.Series, max_depth: int = 2,
                           n_estimators: int = 500, random_state: int = 0) -> pd.Series:
    """ExtraTrees の特徴量重要度。

    max_depth=2 なので 2 指標の交互作用までは拾えるが、過剰なフィッティングは抑えられる。
    木はノンパラメトリックなので、指標の分布の形を気にしなくてよい。
    """
    from sklearn.ensemble import ExtraTreesRegressor

    X, y = _xy(features, returns)
    model = ExtraTreesRegressor(n_estimators=n_estimators, max_depth=max_depth,
                                min_samples_leaf=max(20, len(X) // 50),
                                random_state=random_state, n_jobs=-1).fit(X, y)
    imp = pd.Series(model.feature_importances_, index=X.columns, name="et_importance")
    return imp.sort_values(ascending=False)


def regime_ic(indicator: pd.Series, returns: pd.Series, regime: pd.Series,
              horizon: int = 1) -> pd.DataFrame:
    """レジーム (条件) ごとの IC。

    全期間では無効に見える指標が、ある条件の下でだけ効く (または符号が逆転する) ことがある。
    ただしレジームの定義もパラメータなので、増やすほどカーブフィッティングの危険が増す。
    """
    rows = {}
    for value in pd.unique(regime.dropna()):
        mask = regime == value
        res = information_coefficient(indicator[mask], returns[mask], horizon=horizon)
        rows[value] = {"ic": res.ic, "rank_ic": res.rank_ic, "p_value": res.p_value, "n": res.n}
    return pd.DataFrame(rows).T
