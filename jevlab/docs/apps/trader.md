# 26. trader — 売買判断の実験 (mock / dry-run)

元ネタ: [jarrodwatts/jev-trader](https://github.com/jarrodwatts/jev-trader) (Monad / Kuru 上の実験)

> **免責**: 実験用コードです。**収益性は一切検証されておらず、投資助言ではありません。**
> `live` (オンチェーン実行) は実装していません。実資金で使わないでください。

## 目的

直近ローソク足の要約 (リターン、ボラティリティ、トレンド傾き、RSI) とポジション・損益を Jev に渡し、
`action` Choice {buy, sell, hold} / `conviction` Score [none, low, medium, high] / `regime` Choice
{trending_up, trending_down, ranging, volatile} を聞く。**リスク管理は決定的**で、Jev の提案を毎回ゲートする。

## モード

| モード | データ | 約定 |
| --- | --- | --- |
| `mock` (既定) | レジームが切り替わるランダムウォーク (`MockMarket`) | シミュレーション |
| `dry-run` | `KuruFeed` (環境変数 `KURU_API_URL`) または `StaticFeed` (CSV/JSON) | シミュレーションのみ。**注文は出さない** |
| `live` | — | `NotImplementedError` (署名者・鍵管理・約定確認が必要で範囲外) |

## リスク管理 (`RiskConfig`)

- `max_position` / `unit`: 最大保有量と 1 回の売買量
- `stop_loss_pct` / `take_profit_pct`: Jev の意見に関係なく強制決済
- `cooldown_steps`: 直近の取引から N 本は新規注文しない
- `min_conviction`: この Score レベル未満の提案は hold (既定 1 = low。実運用相当なら 2 以上を推奨)

## 使い方

```bash
jevlab trader run --mode mock --steps 500 --seed 7
jevlab trader run --mode dry-run --csv candles.csv            # 実データ 1 本分の判断だけ
KURU_API_URL=https://... jevlab trader run --mode dry-run --market MON-USDC
jevlab trader backtest --csv candles.csv --warmup 20         # return / max drawdown / trades
```

CSV は `time/t, open, high, low, close, volume` (close だけでも可)。JSON は `{"candles": [[t,o,h,l,c,v], ...]}` か dict の配列。

## 追加依存

なし。`KuruFeed` は urllib のみ (エンドポイントのパスとキーは引数で調整可)。

## 課金・外部送信

- `dry-run` で `KuruFeed` を使うと `KURU_API_URL` へ GET する。API キーは扱わない
- Jev API は 1 本 1 リクエスト。state には価格の要約と損益だけ (鍵やアドレスは含めない)

## 制限

- 約定は終値で即時 (スリッページなし、手数料は `fee_bps`)
- RSI は Wilder 法。バックテストは先頭から順に流すので未来は見ないが、生存バイアス等は考慮しない
- **収益性未検証・投資助言ではない**
