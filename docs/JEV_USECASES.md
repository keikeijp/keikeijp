# jev_usecases — TypeSafe Jev の業務ユースケース集

「海外で話題の Jev、仕事でどう使う？実演・API 活用 20 選」で紹介された使い方のうち、
ブラウザ・ゲーム・ハードウェアを伴わないものを、そのまま動く Python パイプラインとして実装したもの。

Jev は文章を生成しない。渡した状態 (state) に対して、あらかじめ決めた候補や基準に沿った判断を返す。

| 質問型 | 返るもの | 使いどころ |
|---|---|---|
| `noul` | ある条件が成り立つ確率 (0〜1) | 「返金を求めているか」「破壊的か」 |
| `choice` | 候補のうち 1 つ + 各候補の確率 + confidence | 担当部署、ツール、ハンク |
| `score` | 順序付き基準の期待値 (0〜段階数-1) + 分布 + confidence | 緊急度、重大度、関連度 |

文章を書くのは生成 AI、正確な計算はコード、候補の選択は Jev。このパッケージは「選択」の部分だけを担い、
返信の送信・返金処理・コマンドの実行・投稿の削除は行わない (判断を返すだけ)。

## ユースケース一覧

| 名前 | 記事の番号 | やること | 主な判断 |
|---|---|---|---|
| `triage` | API 最小例 / 08 | 問い合わせの仕分け | 部署 (choice)、緊急度 (score)、返金要望・人が見るべきか (noul) |
| `guard` | 13 | コマンドの実行前チェック | 種類 (choice)、危険度 (score)、破壊的か (noul) → allow / ask / block |
| `review` | 06 | コードレビューの絞り込み | ファイルごとにリスク・種類・重大度 → 根拠ハンクの選択 (2 段階) |
| `complexity` | 14 | 過剰設計チェック | パターン (choice)、複雑さ (score) |
| `sniff` | 15 | 文章のクセ検出 | 段落ごとに 10 個の noul |
| `viral` | 11 | 投稿診断 | 11 個の noul + 冒頭の強さ (score) → 0〜100 の合成スコア |
| `feed` | 12 / 17 | フィードの仕分け | 自然言語の非表示ルール + 保存済み投稿の例 → keep / skim / hide |
| `rank` | 16 | ニュースの並べ替え | 6 軸の score をキャッシュし、重みだけ変えて再ランク |
| `compact` | 18 | 記憶の整理 | 関連度 (score)、古さ・安全上重要か (noul) → 残す / 落とす |
| `route` | 19 | ツールルーター | ツール (choice) + 候補付き引数 (choice)。自由入力の引数は生成しない |
| `sample_pick` | (dtm-agent 連携) | サンプルの選択 | 検索候補から役割と雰囲気に合う 1 件 (choice) + 適合度 (score) |

## セットアップ

```bash
pip install -e .            # 依存は requests のみ (dtm_agent の音声系ライブラリは不要)
export TYPESAFE_API_KEY=...  # https://typesafe.ai のコンソールで発行
```

API キーなしでも `--backend mock` で配線を確認できる。モックは語彙一致で決定論的に答えるだけで、
Jev の判断精度は再現しない。

## 使い方

```bash
jev-usecases list                                       # 一覧
jev-usecases run triage --demo                          # 組み込みのデモ入力で実行
jev-usecases run guard --text "git push --force origin main"
jev-usecases run review --text "$(git diff HEAD~1)"
jev-usecases run sniff --text "$(cat draft.md)"
jev-usecases run feed --input examples/jev/posts.jsonl --json   # JSONL in / JSONL out
jev-usecases run rank --demo --dry-run                  # 送信するリクエスト本文だけ見る
jev-usecases models                                     # jev-latest / jev-preview / jev-1.13.0 など
jev-usecases cost --items 10000 --tokens 2000           # 費用の目安 ($0.84)
```

Python から:

```python
from jev_usecases import JevClient, choice, noul, score
from jev_usecases.usecases import get_usecase

client = JevClient.from_env()               # TYPESAFE_API_KEY を読む
guard = get_usecase("guard")
outcome = guard.run_one(client, {"command": "rm -rf ~/", "cwd": "~/work/app"})
print(outcome.decision["verdict"])          # "block"
print(outcome.result.answers_dict())        # 生の回答 (確率・confidence)
print(outcome.cost_usd, outcome.latency_ms)

# 自分の質問を直接投げる
r = client.ask(
    "請求が二重になっています。返金をお願いします。",
    {"refund_requested": noul("Is the customer asking for a refund?")},
)
print(r.noul("refund_requested"))
```

## 試すときは、速度・費用・正確さをセットで見る

記事の推奨どおり、まず匿名化した 50〜100 件に人が正解ラベルを付け、判定結果を記録するだけにする。

```bash
jev-usecases eval triage --dataset examples/jev/tickets_labeled.jsonl
jev-usecases eval guard  --dataset examples/jev/commands_labeled.jsonl --json
```

データ形式は 1 行 1 件の JSONL: `{"input": {...ユースケースの入力...}, "label": "billing"}`。

| 指標 | 意味 |
|---|---|
| `accuracy` | 主ラベル (`label_field`) の一致率 |
| `false_pass_rate` | 通してはいけないもの (`positive_labels`) を、人にも回さず通した割合 (見逃し) |
| `human_rate` | 人へ回した割合。高すぎれば自動化の効果が薄く、低すぎれば見逃しが増える |
| `latency_p50_ms` / `p95` | API 往復のみ。OCR やブラウザの待ち時間は含まない |
| `total_cost_usd` | 入力トークン × $0.042 / 100 万トークン。再試行や併用モデルの費用は含まない |

閾値 (confidence の act / review / abstain の境界、block とみなす危険度など) は各ユースケースのコンストラクタ引数で変えられる。
正解ラベルに対する `eval` の結果を見ながら決めるのが前提で、既定値は出発点にすぎない。

## Vercel AI Gateway で無料で試す (JavaScript)

`examples/jev/vercel-ai-gateway/` に AI SDK の `experimental_evaluate` (ai 7.0.105 以降) を使った最小例がある。
モデル ID は `typesafe-ai/jev`、認証は `AI_GATEWAY_API_KEY`。質問型は公式 API と名前が少し違う:

| TypeSafe 公式 / この Python 実装 | AI SDK |
|---|---|
| `{"type": "noul"}` → `noul` | `{ type: 'boolean' }` → `probability` |
| `{"type": "choice", "criteria": {...}}` → `choice` / `probabilities` | 同じ |
| `{"type": "score", "criteria": [...]}` → `score` / `probabilities` | 同じ |

無料期間や料金は利用時に公式ページで確認する。秘密キーをブラウザ側へ埋め込まない。

## 実装のメモ

- ワイヤ形式は公式 Python SDK (`typesafe-sdk` 0.7.0) の OpenAPI 生成モデルに合わせてある: `POST https://api.typesafe.ai/v1/systemone`、
  `{"state", "model", "questions"}`、回答は `answers.<name>.{noul|choice,confidence,probabilities|score,confidence,legend,probabilities}`。
- 1 リクエスト 1 state。複数件は `--concurrency N` で並列に投げる (公式レート制限は 1,200 req/min)。
- 429 / 5xx は指数バックオフで再試行し、`retry-after` を尊重する。422 (バリデーション) は再試行しない。
- 質問文と基準は英語にしてある (公式ドキュメントで英語が主対応言語)。state は日本語でも渡せるが、精度は `eval` で確認する。
- 公式の既知の弱点: 正確な計数、計算、日付の比較、複雑な間接推論、無関係な情報が多い入力。数値処理はコードへ、state は必要な分だけに絞る。
- 型が決まった答えが返ることと、内容が正しいことは別。`needs_human` と confidence の帯 (`band`) を使って人に回す経路を必ず残す。

## dtm-agent との連携 (`sample_pick`)

`LocalLibrary.search()` の上位候補 (パス・ファイル名・類似度) と参照区間の解析値 (BPM / キー / 明るさ) を文字にして渡し、
「この役割にはどれが合うか」を Jev に選ばせる。音声そのものは渡せない。

```python
from jev_usecases.usecases import get_usecase

hits = lib.search(query="pad", reference=profile, limit=10)
item = {
    "wanted": "a warm, dark analog pad for the 1:05-1:21 section",
    "reference": {"bpm": profile.bpm, "key": profile.key, "brightness": profile.brightness},
    "candidates": [{"id": f"c{i}", "name": h.path.name, "similarity": round(h.score, 2)} for i, h in enumerate(hits)],
}
print(get_usecase("sample_pick").run_one(client, item).decision)
```
