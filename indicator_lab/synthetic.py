"""動作確認用の合成データ。答え (本当に効く指標) が分かっているので、道具の検証に使う。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_market(n: int = 3000, ic: float = 0.1, n_noise: int = 20, vol: float = 0.01,
                seed: int = 0) -> tuple[pd.Series, pd.DataFrame]:
    """翌日リターンと相関 ic を持つ指標 'signal' と、無関係な指標 noise_* を作る。

    signal は AR(1) で緩やかに動く (実際の指標のように自己相関を持つ)。
    戻り値は (終値, 指標の DataFrame)。
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-01", periods=n)
    s = np.zeros(n)
    for t in range(1, n):
        s[t] = 0.9 * s[t - 1] + np.sqrt(1 - 0.81) * rng.standard_normal()
    # 翌日リターン r[t+1] = ic * vol * s[t] + ノイズ  (相関がちょうど ic になるよう調整)
    noise = rng.standard_normal(n) * vol * np.sqrt(1 - ic * ic)
    r = np.zeros(n)
    r[1:] = ic * vol * s[:-1] + noise[1:]
    close = pd.Series(100 * np.exp(np.cumsum(r)), index=idx, name="close")
    cols = {"signal": s}
    for i in range(n_noise):
        e = np.zeros(n)
        for t in range(1, n):
            e[t] = 0.9 * e[t - 1] + np.sqrt(1 - 0.81) * rng.standard_normal()
        cols[f"noise_{i:02d}"] = e
    return close, pd.DataFrame(cols, index=idx)
