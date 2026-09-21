# 29. nanojev — 並列判断ヘッド: K 個の質問を 1 パスで答える

## 目的

Jev は 1 回の呼び出しで複数の型付き質問 (choice / score / noul) に同時に答える。
これを小型モデル (Qwen3-0.6B 級) で再現する「並列判断」の推論契約と学習データ整備、ゲーム制御デモを実装する。

- 元ネタ: [TianyuCodings/NanoJev](https://github.com/TianyuCodings/NanoJev)
- モジュール: `jevlab/local/nanojev.py`

## 推論契約 (ParallelJudgmentHead)

- `pack_questions(state, questions)` が K 質問を 1 つのプロンプトに詰める。末尾は
  `Answers (JSON): {"q1": "<slot>", "q2": "<slot>", ...}` で、各 `<slot>` が質問の回答スロット
- `SlotModel.slot_logprobs(prompt, slots)` がスロットごとに候補トークン (英字 / Yes/No) の log 確率を返す
- `MultiHeadDecider` がそれを choice / score / noul の回答 dict に直す。`NanoJevBackend` は core の Backend
- `HFSlotModel` (遅延 import): スロットに中立プレースホルダを置いて **1 回 forward** し、各スロット直前位置の logits を読む。
  後続スロットは前のスロットの真の回答ではなくプレースホルダに条件付く = 並列ヘッドの近似。
  本家はこのギャップを LoRA 学習で埋める
- `FakeSlotModel`: 語彙重なりで採点するテスト用 (`hints={質問名: 候補}` で固定可)

## 学習データ

```bash
jevlab nanojev export --log decisions.jsonl --out sft.jsonl   # {state, questions, answers} → chat 形式 SFT
jevlab nanojev --model Qwen/Qwen3-0.6B train --sft sft.jsonl --out-dir nanojev-lora   # peft 必須
```

`export_sft` は Decision ログ (`Decision.to_dict()` に state / questions を添えた JSONL) を
system / user (詰めたプロンプト) / assistant (`{"q1": "label", "q2": level, "q3": true}`) の chat 形式にする。
`train_lora(records, base_model, out_dir)` は peft + transformers による LoRA SFT (遅延 import、無ければ pip の案内)。

## ゲーム制御デモ

3 レーンの避けゲー `DodgeGame` (自前実装、apps からは import しない)。毎ティック
`{action: choice(stay/left/right), danger: score(none/near/imminent), should_jump: noul}` を 1 パスで判断する。
state には障害物と「安全な移動候補」だけを入れ、Jev はその中から選ぶ。

```bash
jevlab nanojev --fake demo --steps 50 --verbose
jevlab nanojev --model Qwen/Qwen3-0.6B demo --steps 50
```

## 追加依存

なし (Fake)。実モデルは `pip install 'jevlab[local]'`、LoRA 学習は加えて `pip install peft`。

## 課金・外部送信

なし。

## 制限

- プレースホルダ近似のため、未学習の素のモデルでは後続スロットの精度が落ちる (学習前提)
- 選択肢は質問ごとに最大 26 個
- 質問名は `[name]` として プロンプトに載るので `]` を含めない
