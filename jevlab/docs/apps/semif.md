# 27. semif — 公開モデルの logit から選択肢確率を直接読む

## 目的

Jev (System One) の「サンプリングせずに型付き回答の確率分布を返す」性質を、手元の公開モデルで再現する。
プロンプト末尾 `Answer:` の**次トークン分布**から候補トークン (A/B/C… または Yes/No) の log 確率を 1 回の forward で読み、
softmax して確率にする。トークン生成は行わないので、生成 LLM と比べて 1 質問 = 1 forward の固定コスト。

- 元ネタ: [TheoLeeCJ/SemIf](https://github.com/TheoLeeCJ/SemIf) (旧 OpenJev)
- モジュール: `jevlab/local/semif.py`
- `JEV_BACKEND=local` で core の `backend_from_env` から自動選択される (`LocalLogitBackend.from_env`)

## 仕組み

| 質問 | プロンプト | 候補トークン | 回答 |
|---|---|---|---|
| choice | 選択肢を `A. label: 説明` で列挙 + `Answer:` | 英字 | softmax 確率、`confidence` = 1 位 − 2 位 |
| score | ルーブリック段階を英字で列挙 | 英字 | 確率分布の期待値が `score` |
| noul | `Answer (Yes or No):` | `Yes` / `No` | `P(Yes)` |

- `LogitModel` プロトコル: `next_token_logprobs(prompt, candidate_tokens) -> {token: logprob}` を満たせば何でも差し込める
- `HFLogitModel`: transformers の CausalLM。候補トークンは先頭スペース有無の変種を試し、単一トークンになる ID の最大を採る
- `FakeLogitModel`: 語彙重なりで logit を作るテスト用 (torch 不要)
- `temperature_scale(probs, T)`: 温度スケーリングによる較正。検証セットで T を選び `LocalLogitBackend(temperature=T)` に渡す

## 使い方

```bash
jevlab semif --fake ask --state "二重に課金された。返金して" --choice refund,bug,other --noul "急ぎか?"
jevlab semif --model Qwen/Qwen2.5-0.5B-Instruct ask --state "..." --score "低,中,高"
jevlab semif --fake bench --jsonl cases.jsonl     # {"state","options","expected","instructions"} の JSONL で精度
JEV_BACKEND=local JEV_LOCAL_MODEL=Qwen/Qwen2.5-0.5B-Instruct JEV_LOCAL_DEVICE=cuda python -c "from jevlab.core import Jev; print(Jev().choose('...', ['a','b']))"
```

```python
from jevlab.core import Jev
from jevlab.local.semif import LocalLogitBackend, HFLogitModel, FakeLogitModel
jev = Jev(LocalLogitBackend(model=FakeLogitModel()))          # テスト
jev = Jev(LocalLogitBackend(model=HFLogitModel("Qwen/Qwen2.5-1.5B-Instruct"), temperature=1.3))
```

## 追加依存

`pip install 'jevlab[local]'` (torch, transformers)。import 時には不要で、`HFLogitModel` を初めて使うときに遅延 import する。
`JEV_LOCAL_MODEL=fake` で依存なしの FakeLogitModel になる。

## 課金・外部送信

なし。モデルの重みは HF Hub から初回ダウンロードされる (以降ローカル)。

## 制限

- 選択肢は最大 26 個 (英字 1 トークン)。多いときは 2 段階に分けるかサーバ側で分割する
- 小型モデルの素の確率は較正されていない (過信しがち)。`temperature_scale` で較正してから閾値を切る
- 複数質問は質問数ぶんの forward (バッチ化は `LogitModel` 実装側で行える)。1 パスで全質問を答えるのは nanojev を参照
- 日本語の選択肢ラベルも英字に写像するので、ラベルの意味は説明文に書く
