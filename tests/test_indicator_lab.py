import numpy as np
import pandas as pd
import pytest

from indicator_lab import (
    binned_expectation,
    candidate_indicators,
    extra_trees_importance,
    forward_returns,
    information_coefficient,
    lasso_select,
    quantile_returns,
    regime_ic,
    screen,
    zscore,
    zscore_strategy,
)
from indicator_lab.cli import main
from indicator_lab.evaluate import benjamini_hochberg, monotonicity
from indicator_lab.synthetic import make_market


@pytest.fixture(scope="module")
def market():
    return make_market(n=3000, ic=0.1, n_noise=20, seed=0)


def test_zscore_has_no_lookahead():
    x = pd.Series(np.arange(300, dtype=float))
    z1 = zscore(x, window=50)
    x2 = x.copy()
    x2.iloc[200:] = 1e6  # 未来を書き換えても過去の Z は変わらない
    z2 = zscore(x2, window=50)
    pd.testing.assert_series_equal(z1.iloc[:200], z2.iloc[:200])


def test_forward_returns():
    close = pd.Series([100.0, 110.0, 99.0])
    r = forward_returns(close, 1)
    assert r.iloc[0] == pytest.approx(0.1)
    assert np.isnan(r.iloc[-1])


def test_ic_recovers_planted_signal(market):
    close, ind = market
    res = information_coefficient(ind["signal"], forward_returns(close, 1))
    assert 0.07 < res.ic < 0.15
    assert res.ci_low < res.ic < res.ci_high
    assert res.p_value < 1e-4


def test_overlapping_horizon_is_penalized(market):
    close, ind = market
    fwd = forward_returns(close, 5)
    r1 = information_coefficient(ind["noise_00"], fwd, horizon=1)
    r5 = information_coefficient(ind["noise_00"], fwd, horizon=5)
    assert r5.n_eff == pytest.approx(r1.n / 5)
    assert r5.p_value > r1.p_value


def test_quantiles_are_monotone_for_signal(market):
    close, ind = market
    qr = quantile_returns(ind["signal"], forward_returns(close, 1), q=5)
    assert len(qr) == 5
    assert monotonicity(qr) > 0.8


def test_binned_expectation_has_t_stats(market):
    close, ind = market
    out = binned_expectation(ind["noise_00"], forward_returns(close, 1), bins=8)
    assert {"mean_return", "count", "t_stat"} <= set(out.columns)
    assert out["count"].sum() > 2500


def test_zscore_strategy_profits_on_signal_and_direction_flips(market):
    close, ind = market
    z = zscore(ind["signal"])
    long = zscore_strategy(z, close, direction=1)
    short = zscore_strategy(z, close, direction=-1)
    assert long["equity"].iloc[-1] > 0
    assert short["equity"].iloc[-1] == pytest.approx(-long["equity"].iloc[-1])
    costly = zscore_strategy(z, close, direction=1, cost=0.001)
    assert costly["equity"].iloc[-1] < long["equity"].iloc[-1]


def test_benjamini_hochberg():
    p = pd.Series([0.01, 0.04, 0.03, 0.5, np.nan])
    q = benjamini_hochberg(p)
    assert q.iloc[0] == pytest.approx(0.04)
    assert np.isnan(q.iloc[4])
    assert (q.dropna() >= p.dropna()).all()


def test_screen_keeps_signal_and_rejects_noise(market):
    close, ind = market
    table = screen(close, ind)
    assert table.index[0] == "signal"
    assert bool(table.loc["signal", "candidate"])
    assert table.loc["signal", "grade"] == "非常に優秀"
    assert not table.drop(index="signal")["candidate"].any()


def test_feature_selection_prefers_signal(market):
    close, ind = market
    feats = ind.apply(zscore)
    fwd = forward_returns(close, 1)
    coef = lasso_select(feats, fwd)
    assert coef.index[0] == "signal" and coef["signal"] > 0
    imp = extra_trees_importance(feats, fwd, n_estimators=100)
    assert imp.index[0] == "signal"


def test_regime_ic_finds_conditional_signal():
    rng = np.random.default_rng(1)
    n = 4000
    x = pd.Series(rng.standard_normal(n))
    regime = pd.Series(np.where(np.arange(n) % 2 == 0, "calm", "stress"))
    # calm では順張り、stress では逆張り → 全期間の IC はほぼ 0
    y = pd.Series(np.where(regime == "calm", 0.15, -0.15) * x + rng.standard_normal(n))
    assert abs(information_coefficient(x, y).ic) < 0.05
    out = regime_ic(x, y, regime)
    assert out.loc["calm", "ic"] > 0.1 and out.loc["stress", "ic"] < -0.1


def test_candidate_indicators_columns():
    close, _ = make_market(n=400, n_noise=0)
    ind = candidate_indicators(close)
    assert "ma_dev_15" in ind and "rsi_14" in ind
    assert ind["rsi_14"].dropna().between(0, 100).all()


def test_cli_screen_csv(tmp_path, capsys):
    close, ind = make_market(n=800, n_noise=2)
    df = pd.DataFrame({"Date": close.index, "Close": close.values, "my_signal": ind["signal"].values})
    path = tmp_path / "prices.csv"
    df.to_csv(path, index=False)
    main(["screen", str(path), "--z-window", "100"])
    out = capsys.readouterr().out
    assert "my_signal" in out and "ma_dev_15" in out
