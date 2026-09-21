"""jev-trader (jarrodwatts/jev-trader, Monad/Kuru 実験) の再実装。

**免責: 実験用コードです。収益性は検証されておらず、投資助言ではありません。**
`live` モードは実装していません (オンチェーン実行は利用者自身の署名者が必要で、範囲外)。

- `mock`    : 合成ランダムウォーク相場 + 約定シミュレーション (既定)
- `dry-run` : `MarketFeed` (KuruFeed / StaticFeed) から実データを読むが**注文は一切出さない**
- `live`    : NotImplementedError

Jev には {直近ローソク足の要約 (リターン, ボラ, トレンド傾き, RSI), ポジション, 損益} を渡し、
`action` Choice {buy, sell, hold} / `conviction` Score / `regime` Choice を聞く。
`RiskManager` が最大ポジション・損切り・クールダウン・最低確信度で**毎回決定的に**ゲートする。

使い方:
    jevlab trader run --mode mock --steps 500 --seed 7
    jevlab trader backtest --csv candles.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol

from jevlab.core import Choice, Jev, Score

DISCLAIMER = "実験用: 収益性は未検証。投資助言ではありません。live 実行は未実装です。"


@dataclass
class Candle:
    t: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------


def returns(closes: list[float]) -> list[float]:
    return [(b - a) / a if a else 0.0 for a, b in zip(closes, closes[1:])]


def volatility(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def trend_slope(closes: list[float]) -> float:
    """最小二乗の傾きを平均価格で正規化 (1 本あたりの相対変化)。"""
    n = len(closes)
    if n < 2:
        return 0.0
    xm, ym = (n - 1) / 2, sum(closes) / n
    num = sum((i - xm) * (c - ym) for i, c in enumerate(closes))
    den = sum((i - xm) ** 2 for i in range(n))
    return (num / den) / ym if den and ym else 0.0


def rsi(closes: list[float], period: int = 14) -> float:
    """Wilder の RSI。最初の平均は単純平均、以後は指数平滑。データ不足なら 50。"""
    if len(closes) <= period:
        return 50.0
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def summarize(candles: list[Candle], window: int = 20) -> dict[str, Any]:
    closes = [c.close for c in candles[-window:]]
    rets = returns(closes)
    return {
        "bars": len(closes),
        "last_close": round(closes[-1], 4) if closes else None,
        "return_window_pct": round((closes[-1] / closes[0] - 1) * 100, 3) if len(closes) > 1 else 0.0,
        "last_return_pct": round(rets[-1] * 100, 3) if rets else 0.0,
        "volatility_pct": round(volatility(rets) * 100, 3),
        "trend_slope_pct": round(trend_slope(closes) * 100, 4),
        "rsi": round(rsi([c.close for c in candles]), 1),
        "high": round(max(c.high for c in candles[-window:]), 4) if candles else None,
        "low": round(min(c.low for c in candles[-window:]), 4) if candles else None,
    }


# ---------------------------------------------------------------------------
# 相場データ
# ---------------------------------------------------------------------------


class MarketFeed(Protocol):
    def candles(self, limit: int | None = None) -> list[Candle]: ...


class StaticFeed:
    """CSV / JSON から読むフィード。バックテストとテスト用。"""

    def __init__(self, candles: Iterable[Candle]):
        self._candles = list(candles)

    @staticmethod
    def parse_rows(rows: Iterable[dict[str, Any]]) -> list[Candle]:
        out = []
        for i, row in enumerate(rows):
            if isinstance(row, (list, tuple)):  # [t, open, high, low, close, volume] の配列行
                values = list(row) + [None] * 6
                close = float(values[4] if values[4] is not None else values[1])
                out.append(Candle(int(float(values[0] if values[0] is not None else i)), float(values[1] if values[1] is not None else close), float(values[2] if values[2] is not None else close), float(values[3] if values[3] is not None else close), close, float(values[5] or 0.0)))
                continue
            keys = {k.lower(): k for k in row}
            get = lambda *names, default=None: next((row[keys[n]] for n in names if n in keys), default)  # noqa: E731
            close = float(get("close", "c", "price"))
            out.append(Candle(int(float(get("t", "time", "timestamp", "ts", default=i))), float(get("open", "o", default=close)), float(get("high", "h", default=close)), float(get("low", "l", default=close)), close, float(get("volume", "v", "vol", default=0.0))))
        return out

    @classmethod
    def from_csv(cls, path: str) -> "StaticFeed":
        with open(path, newline="", encoding="utf-8") as handle:
            return cls(cls.parse_rows(csv.DictReader(handle)))

    @classmethod
    def from_json(cls, path: str) -> "StaticFeed":
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data.get("candles", data.get("data", [])) if isinstance(data, dict) else data
        return cls(cls.parse_rows(rows))

    def candles(self, limit: int | None = None) -> list[Candle]:
        return self._candles[-limit:] if limit else list(self._candles)


class KuruFeed:
    """Kuru (Monad) の公開 API からローソク足を取る。base URL は環境変数 KURU_API_URL。

    エンドポイント形は `{base}/{path}?market=...&interval=...&limit=...` で、`path` と
    レスポンスのキーは API 側の変更に追従できるよう引数にしてある。ネットワークは `candles()` 呼び出し時のみ。
    """

    def __init__(self, market: str, interval: str = "1m", base_url: str | None = None, path: str = "candles", timeout: float = 10.0):
        self.base_url = (base_url or os.environ.get("KURU_API_URL", "")).rstrip("/")
        if not self.base_url:
            raise ValueError("KURU_API_URL が設定されていません (例: export KURU_API_URL=https://api.example/kuru)")
        self.market, self.interval, self.path, self.timeout = market, interval, path, timeout

    def candles(self, limit: int | None = 200) -> list[Candle]:
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({"market": self.market, "interval": self.interval, "limit": limit or 200})
        request = urllib.request.Request(f"{self.base_url}/{self.path}?{query}", headers={"Accept": "application/json", "User-Agent": "jevlab/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        rows = data.get("candles", data.get("data", [])) if isinstance(data, dict) else data
        if rows and isinstance(rows[0], list):  # [t, o, h, l, c, v] 形式
            rows = [dict(zip(["t", "open", "high", "low", "close", "volume"], r)) for r in rows]
        return StaticFeed.parse_rows(rows)


class MockMarket:
    """レジームが切り替わるランダムウォーク相場。1 ステップ 1 本生成する。"""

    def __init__(self, seed: int = 7, price: float = 100.0, warmup: int = 40):
        self.rng = random.Random(seed)
        self.price = price
        self.drift, self.vol = 0.0, 0.01
        self.t = 0
        self._candles: list[Candle] = []
        for _ in range(warmup):
            self.next()

    def next(self) -> Candle:
        if self.t % 50 == 0:
            self.drift = self.rng.choice([-0.003, 0.0, 0.0, 0.003])
            self.vol = self.rng.choice([0.005, 0.01, 0.02])
        open_ = self.price
        close = max(1.0, open_ * (1 + self.drift + self.rng.gauss(0, self.vol)))
        wick = abs(self.rng.gauss(0, self.vol)) * open_
        candle = Candle(self.t, open_, max(open_, close) + wick, min(open_, close) - wick, close, self.rng.uniform(10, 100))
        self.price, self.t = close, self.t + 1
        self._candles.append(candle)
        return candle

    def candles(self, limit: int | None = None) -> list[Candle]:
        return self._candles[-limit:] if limit else list(self._candles)


# ---------------------------------------------------------------------------
# ポートフォリオとリスク管理
# ---------------------------------------------------------------------------


@dataclass
class Portfolio:
    cash: float = 10_000.0
    position: float = 0.0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    fee_bps: float = 10.0
    trades: list[dict[str, Any]] = field(default_factory=list)

    def fill(self, side: str, price: float, units: float, t: int) -> None:
        fee = price * units * self.fee_bps / 10_000
        if side == "buy":
            total = self.position + units
            self.entry_price = (self.entry_price * self.position + price * units) / total if total else 0.0
            self.position = total
            self.cash -= price * units + fee
            pnl = -fee
        else:
            pnl = (price - self.entry_price) * units - fee
            self.realized_pnl += pnl
            self.position -= units
            self.cash += price * units - fee
            if self.position <= 1e-12:
                self.position, self.entry_price = 0.0, 0.0
        self.trades.append({"t": t, "side": side, "price": round(price, 4), "units": units, "pnl": round(pnl, 4)})

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.position

    def equity(self, price: float) -> float:
        return self.cash + self.position * price


@dataclass
class RiskConfig:
    max_position: float = 1.0  # 単位数
    unit: float = 0.5  # 1 回の売買量
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 8.0
    cooldown_steps: int = 5
    min_conviction: int = 1  # Score level: 0 none, 1 low, 2 medium, 3 high。本番相当なら 2 以上を推奨


class RiskManager:
    """Jev の提案を決定的にゲートする。損切りは Jev の意見に関係なく発動する。"""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self.last_trade_step = -10**9

    def gate(self, action: str, conviction: int, portfolio: Portfolio, price: float, step: int) -> tuple[str, str]:
        cfg = self.config
        if portfolio.position > 0 and portfolio.entry_price:
            change = (price / portfolio.entry_price - 1) * 100
            if change <= -cfg.stop_loss_pct:
                return "sell", f"stop_loss {change:.2f}%"
            if change >= cfg.take_profit_pct:
                return "sell", f"take_profit {change:.2f}%"
        if action == "hold":
            return "hold", "jev_hold"
        if conviction < cfg.min_conviction:
            return "hold", f"conviction {conviction} < {cfg.min_conviction}"
        if action == "buy" and portfolio.position + cfg.unit > cfg.max_position + 1e-9:
            return "hold", "max_position"
        if action == "sell" and portfolio.position <= 0:
            return "hold", "no_position"
        if step - self.last_trade_step < cfg.cooldown_steps:
            return "hold", "cooldown"
        return action, "ok"

    def note_trade(self, step: int) -> None:
        self.last_trade_step = step


# ---------------------------------------------------------------------------
# Jev 戦略
# ---------------------------------------------------------------------------

QUESTIONS = {
    "action": Choice({"buy": "上昇が続く/売られ過ぎ (RSI < 30) からの反発が見込め、ポジションに余裕がある", "sell": "下落が続く/買われ過ぎ (RSI > 70) で保有中、または含み損が拡大", "hold": "方向感がない、ボラが高すぎる、または既に適切なポジション"}, "次の 1 本での売買判断は?"),
    "conviction": Score(["none: 根拠がない", "low: 弱い根拠", "medium: 複数の指標が一致", "high: トレンド・RSI・直近リターンが強く一致"], "判断の確信度は?"),
    "regime": Choice({"trending_up": "傾きが正でリターンも正が続く", "trending_down": "傾きが負で下落が続く", "ranging": "傾きがほぼ 0 でボラが低い", "volatile": "ボラが高く方向が定まらない"}, "現在の相場レジームは?"),
}
CONVICTION_LEVELS = ["none", "low", "medium", "high"]


def build_state(candles: list[Candle], portfolio: Portfolio, window: int = 20) -> dict[str, Any]:
    price = candles[-1].close
    return {
        "market": summarize(candles, window),
        "position": {"units": portfolio.position, "entry_price": round(portfolio.entry_price, 4), "unrealized_pct": round((price / portfolio.entry_price - 1) * 100, 3) if portfolio.position and portfolio.entry_price else 0.0},
        "pnl": {"realized": round(portfolio.realized_pnl, 4), "unrealized": round(portfolio.unrealized(price), 4), "trades": len(portfolio.trades)},
    }


class Trader:
    def __init__(self, jev: Jev, feed: MarketFeed, mode: str = "mock", risk: RiskManager | None = None, portfolio: Portfolio | None = None, window: int = 20):
        if mode == "live":
            raise NotImplementedError("live モードは未実装です: オンチェーン実行 (Kuru/Monad) には利用者自身の署名者・鍵管理・約定確認が必要で、このリポジトリの範囲外です。--mode mock か --mode dry-run を使ってください。")
        if mode not in ("mock", "dry-run"):
            raise ValueError(f"unknown mode: {mode}")
        self.jev, self.feed, self.mode, self.window = jev, feed, mode, window
        self.risk = risk or RiskManager()
        self.portfolio = portfolio or Portfolio()
        self.log: list[dict[str, Any]] = []
        self.equity_curve: list[float] = []
        self.step_index = 0

    def step(self, candles: list[Candle]) -> dict[str, Any]:
        price = candles[-1].close
        state = build_state(candles, self.portfolio, self.window)
        decision = self.jev.decide(state, QUESTIONS)
        proposed = decision.choice("action").choice
        conviction = decision.score("conviction").level
        regime = decision.choice("regime").choice
        action, reason = self.risk.gate(proposed, conviction, self.portfolio, price, self.step_index)
        if action == "buy":
            self.portfolio.fill("buy", price, self.risk.config.unit, candles[-1].t)
            self.risk.note_trade(self.step_index)
        elif action == "sell":
            self.portfolio.fill("sell", price, self.portfolio.position if reason != "ok" else min(self.portfolio.position, self.risk.config.unit), candles[-1].t)
            self.risk.note_trade(self.step_index)
        equity = self.portfolio.equity(price)
        self.equity_curve.append(equity)
        entry = {"step": self.step_index, "t": candles[-1].t, "price": round(price, 4), "proposed": proposed, "conviction": CONVICTION_LEVELS[conviction], "regime": regime, "action": action, "reason": reason, "position": self.portfolio.position, "equity": round(equity, 4), "rsi": state["market"]["rsi"]}
        self.log.append(entry)
        self.step_index += 1
        return entry

    def run(self, steps: int) -> dict[str, Any]:
        for _ in range(steps):
            if isinstance(self.feed, MockMarket):
                self.feed.next()
            candles = self.feed.candles(limit=max(self.window, 15) + 1)
            if len(candles) < 2:
                break
            self.step(candles)
        return self.metrics()

    def metrics(self) -> dict[str, Any]:
        m = equity_metrics(self.equity_curve)
        return {**m, "trades": len(self.portfolio.trades), "win_rate": round(sum(1 for t in self.portfolio.trades if t["side"] == "sell" and t["pnl"] > 0) / max(1, sum(1 for t in self.portfolio.trades if t["side"] == "sell")), 3), "realized_pnl": round(self.portfolio.realized_pnl, 4), "mode": self.mode, "disclaimer": DISCLAIMER}


def equity_metrics(curve: list[float]) -> dict[str, Any]:
    if not curve:
        return {"return_pct": 0.0, "max_drawdown_pct": 0.0, "steps": 0}
    peak, max_dd = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak * 100 if peak else 0.0)
    return {"return_pct": round((curve[-1] / curve[0] - 1) * 100, 4), "max_drawdown_pct": round(max_dd, 4), "steps": len(curve), "final_equity": round(curve[-1], 4)}


def backtest(jev: Jev, feed: MarketFeed, warmup: int = 20, risk: RiskManager | None = None, window: int = 20) -> dict[str, Any]:
    """StaticFeed の全ローソク足を先頭から順に流す (未来は見ない)。"""
    candles = feed.candles()
    trader = Trader(jev, feed, "dry-run", risk=risk, window=window)
    for i in range(max(2, warmup), len(candles) + 1):
        trader.step(candles[:i])
    result = trader.metrics()
    result["log"] = trader.log
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab trader", description=__doc__.splitlines()[0], epilog=DISCLAIMER)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="mock / dry-run で売買ループを回す")
    p.add_argument("--mode", choices=["mock", "dry-run", "live"], default="mock")
    p.add_argument("--steps", type=int, default=500)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--market", default="MON-USDC", help="dry-run で KuruFeed に渡す市場名")
    p.add_argument("--csv", default=None, help="dry-run で KuruFeed の代わりに使う CSV")
    p.add_argument("--backend", default=None)
    p.add_argument("--json", action="store_true")
    b = sub.add_parser("backtest", help="CSV / JSON のローソク足でバックテスト")
    b.add_argument("--csv", default=None)
    b.add_argument("--json-file", default=None)
    b.add_argument("--warmup", type=int, default=20)
    b.add_argument("--backend", default=None)
    b.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    jev = Jev(args.backend)
    print(DISCLAIMER, file=sys.stderr)
    try:
        if args.command == "run":
            if args.mode == "live":
                Trader(jev, StaticFeed([]), mode="live")  # NotImplementedError を投げる (フィードを作る前に止める)
            if args.mode == "mock":
                feed: MarketFeed = MockMarket(seed=args.seed)
            elif args.csv:
                feed = StaticFeed.from_csv(args.csv)
            else:
                feed = KuruFeed(args.market)
            trader = Trader(jev, feed, mode=args.mode)
            result = trader.run(args.steps if args.mode == "mock" else 1)
            result["log_tail"] = trader.log[-5:]
        else:
            if not (args.csv or args.json_file):
                parser.error("backtest には --csv か --json-file が必要です")
            feed = StaticFeed.from_csv(args.csv) if args.csv else StaticFeed.from_json(args.json_file)
            result = backtest(jev, feed, warmup=args.warmup)
            result.pop("log")
    except NotImplementedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({**result, "jev": jev.stats()}, ensure_ascii=False, indent=None if args.json else 2))
    return 0


__all__ = ["Candle", "returns", "volatility", "trend_slope", "rsi", "summarize", "MarketFeed", "StaticFeed", "KuruFeed", "MockMarket", "Portfolio", "RiskConfig", "RiskManager", "build_state", "Trader", "backtest", "equity_metrics", "DISCLAIMER", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
