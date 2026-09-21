import json

import pytest

from jevlab.apps import trader
from jevlab.apps.trader import Candle, KuruFeed, MockMarket, Portfolio, RiskConfig, RiskManager, StaticFeed, Trader, backtest, equity_metrics, rsi, summarize, trend_slope, volatility
from jevlab.core import FunctionBackend, Jev, ScriptedBackend

STOCKCHARTS = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]


def test_rsi_on_known_series():
    assert abs(rsi(STOCKCHARTS, 14) - 70.5) < 1.0  # 定番の StockCharts 例 (70.46〜70.53)
    assert rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]) == 100.0
    assert rsi(list(range(16, 0, -1))) == 0.0
    assert rsi([1.0, 2.0, 3.0]) == 50.0  # データ不足
    assert rsi([5.0] * 20) == 50.0  # 変化なし
    assert trend_slope([100, 101, 102, 103]) > 0 > trend_slope([103, 102, 101, 100])
    assert volatility([0.01, -0.01, 0.01, -0.01]) > volatility([0.001, -0.001, 0.001, -0.001])
    candles = [Candle(i, p, p + 0.1, p - 0.1, p) for i, p in enumerate(STOCKCHARTS)]
    summary = summarize(candles, window=10)
    assert summary["bars"] == 10 and abs(summary["rsi"] - 70.5) < 1.0 and summary["last_close"] == 46.28


def test_risk_manager_gates_every_action_deterministically():
    risk = RiskManager(RiskConfig(max_position=1.0, unit=0.5, stop_loss_pct=3.0, take_profit_pct=8.0, cooldown_steps=5, min_conviction=2))
    empty = Portfolio()
    assert risk.gate("buy", 3, empty, 100.0, 0) == ("buy", "ok")
    assert risk.gate("buy", 1, empty, 100.0, 0)[1].startswith("conviction")
    assert risk.gate("sell", 3, empty, 100.0, 0) == ("hold", "no_position")
    assert risk.gate("hold", 3, empty, 100.0, 0) == ("hold", "jev_hold")
    risk.note_trade(0)
    assert risk.gate("buy", 3, empty, 100.0, 3) == ("hold", "cooldown")
    assert risk.gate("buy", 3, empty, 100.0, 5) == ("buy", "ok")
    full = Portfolio(position=1.0, entry_price=100.0)
    assert risk.gate("buy", 3, full, 100.0, 10) == ("hold", "max_position")
    assert risk.gate("hold", 0, full, 96.5, 10)[0] == "sell" and "stop_loss" in risk.gate("hold", 0, full, 96.5, 10)[1]
    assert risk.gate("buy", 3, full, 109.0, 10)[1].startswith("take_profit")
    assert risk.gate("sell", 3, full, 101.0, 10) == ("sell", "ok")


def test_portfolio_fills_and_pnl():
    p = Portfolio(cash=1000.0, fee_bps=0.0)
    p.fill("buy", 100.0, 1.0, 0)
    p.fill("buy", 110.0, 1.0, 1)
    assert p.position == 2.0 and p.entry_price == 105.0 and p.cash == 790.0
    assert p.unrealized(120.0) == 30.0 and p.equity(120.0) == 1030.0
    p.fill("sell", 120.0, 2.0, 2)
    assert p.position == 0.0 and p.realized_pnl == 30.0 and p.cash == 1030.0 and len(p.trades) == 3


def test_mock_run_with_scripted_backend_and_live_refused():
    backend = ScriptedBackend(default={"action": "buy", "conviction": 3, "regime": "trending_up"})
    t = Trader(Jev(backend), MockMarket(seed=3), mode="mock", risk=RiskManager(RiskConfig(cooldown_steps=5, unit=0.5, max_position=1.0, stop_loss_pct=99, take_profit_pct=999)))
    metrics = t.run(12)
    assert metrics["steps"] == 12 and metrics["trades"] == 2  # step 0 と 5 で買い、その後は max_position
    assert [e["reason"] for e in t.log[:2]] == ["ok", "cooldown"]
    assert t.log[6]["reason"] == "max_position" and t.portfolio.position == 1.0
    state = backend.calls[0]["state"]
    assert set(state) == {"market", "position", "pnl"} and "rsi" in state["market"]
    assert "投資助言ではありません" in metrics["disclaimer"]
    with pytest.raises(NotImplementedError, match="live"):
        Trader(Jev(backend), MockMarket(), mode="live")


def test_backtest_metrics_and_static_feeds(tmp_path):
    closes = [100 + i for i in range(40)]  # 一本調子の上昇
    rows = [{"time": i, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1} for i, c in enumerate(closes)]
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text("time,open,high,low,close,volume\n" + "\n".join(",".join(str(r[k]) for k in ("time", "open", "high", "low", "close", "volume")) for r in rows), encoding="utf-8")
    json_path = tmp_path / "candles.json"
    json_path.write_text(json.dumps({"candles": [[r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]] for r in rows]}), encoding="utf-8")
    feed = StaticFeed.from_csv(str(csv_path))
    assert len(feed.candles()) == 40 and feed.candles(limit=3)[0].close == 137
    feed_json = StaticFeed.from_json(str(json_path))
    assert len(feed_json.candles()) == 40 and feed_json.candles()[0].t == 0
    jev = Jev(FunctionBackend(lambda s, q: {"action": "buy" if s["market"]["rsi"] > 50 else "hold", "conviction": 3, "regime": "trending_up"}))
    result = backtest(jev, feed, warmup=20, risk=RiskManager(RiskConfig(take_profit_pct=999)))
    assert result["trades"] == 2 and result["return_pct"] > 0 and result["max_drawdown_pct"] >= 0
    assert len(result["log"]) == 21 and result["log"][0]["action"] == "buy"
    assert equity_metrics([100.0, 120.0, 90.0, 110.0]) == {"return_pct": 10.0, "max_drawdown_pct": 25.0, "steps": 4, "final_equity": 110.0}
    assert equity_metrics([])["steps"] == 0


def test_kuru_feed_requires_url_and_main(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("KURU_API_URL", raising=False)
    with pytest.raises(ValueError):
        KuruFeed("MON-USDC")
    monkeypatch.setenv("KURU_API_URL", "https://kuru.example/api/")
    feed = KuruFeed("MON-USDC")
    assert feed.base_url == "https://kuru.example/api"  # ネットワークは candles() まで触らない
    assert trader.main(["run", "--mode", "mock", "--steps", "20", "--seed", "7", "--backend", "mock", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["steps"] == 20 and out["mode"] == "mock"
    assert trader.main(["run", "--mode", "live"]) == 2
    assert "live" in capsys.readouterr().err
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("t,close\n" + "\n".join(f"{i},{100 + (i % 7)}" for i in range(30)), encoding="utf-8")
    assert trader.main(["backtest", "--csv", str(csv_path), "--backend", "mock"]) == 0
    assert "return_pct" in capsys.readouterr().out
