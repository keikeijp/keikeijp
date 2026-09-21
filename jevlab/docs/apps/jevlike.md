# 28. jevlike — 可変長の選択肢集合を一度に評価する小型モデル

## 目的

Jev の choice は「選択肢の**集合**に対する確率分布」を返す。生成 LLM に頼らず、
(state, options) → softmax over options を直接学習する小型モデルの学習パイプラインを、依存ゼロで再現する。

- 元ネタ: [vinnylarouge/jevlike](https://github.com/vinnylarouge/jevlike)
- モジュール: `jevlab/local/jevlike.py`

## 構成

1. `make_dataset(n, seed)` — テンプレート合成データ。意図ルーティング / 感情 / 一桁の足し算比較 / キーワード一致。
   選択肢数は 2〜5 で可変。同じ (n, seed) なら決定的
2. `HashedLinearScorer` — 純 Python の特徴ハッシュ線形モデル。
   特徴 = (state トークン × option トークン) ペア + option トークン単独 + 語彙重なり率。
   選択肢集合上の softmax 交差エントロピーを SGD で学習。`fit / predict_proba / evaluate / save / load`。
   未学習でも語彙重なりの事前重み (`prior_overlap`) で MockBackend 風に動く
3. `build_torch_set_scorer` / `train_torch` — torch 版 cross-encoder-lite (共有 EmbeddingBag → [s; o; s*o] → MLP → 集合 softmax)。torch は遅延 import
4. `JevlikeBackend` — core の Backend。choice = 集合スコアリング、score = ルーブリック段階を選択肢化して期待値、noul = ["yes","no"] を選択肢化 (instructions は state に前置)

合成データ 800 件・6 エポックで held-out 精度 ≈ 0.9 (足し算は 0.7 弱、他は 1.0)。学習は 0.1 秒程度。

## 使い方

```bash
jevlab jevlike gen --n 2000 --seed 0 --out data.jsonl
jevlab jevlike train --data data.jsonl --out model.json --epochs 5   # 末尾 20% を held-out として精度表示
jevlab jevlike train --data data.jsonl --out model.pt --torch         # torch 版
jevlab jevlike eval --data data.jsonl --model model.json
jevlab jevlike serve-as-backend                                        # Jev への組み込み方を表示
jevlab serve --engine jevlike --model model.json                       # HTTP API として公開 (30. server)
```

```python
from jevlab.core import Jev
from jevlab.local.jevlike import JevlikeBackend, HashedLinearScorer
jev = Jev(JevlikeBackend(HashedLinearScorer.load("model.json")))
jev.choose("route the ticket: charged twice", ["refund", "bug", "shipping"])
```

## 追加依存

なし (torch 版のみ `pip install 'jevlab[local]'`)。

## 課金・外部送信

なし。

## 制限

- 線形モデルなので語彙の組み合わせ以上の推論はできない (合成データ用のベースライン)。実データでは torch 版か semif / nanojev を使う
- 特徴ハッシュは衝突する (既定 2^18 次元)。語彙が大きいときは `n_features` を増やす
- JSON の state はフラットに文字列化してトークン化する
