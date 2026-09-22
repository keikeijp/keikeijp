# jev でつくる 3 つの収益ストリーム

[jev](https://typesafe.ai) は TypeSafe AI の「System One」モデルです。文章は書けず、**渡した選択肢の中から選ぶ / 順序付きの尺度に置く / true か false を返す** の 3 種類の判断だけを、確率付きで、1 回の呼び出しで並列に返します。1 回あたり 70〜500 ms、入力 $0.042 / 100 万トークン、出力は無料です。

このフォルダは、その性質を使った 3 つの商品を、依存パッケージなし (Node 22+) で動く形にしたものです。

| | 何を売るか | 誰に | 入口 |
|---|---|---|---|
| Method 1 | 即時見積ウィジェット | HVAC・配管・電気などの地元業者 | `quote/` |
| Method 2 | B2B 見積デスク (同じウィジェットのカタログ版) | 看板屋・印刷・加工・卸 | `b2b/` (エンジンは `quote/` と共通) |
| Method 3 | エージェント用ガードスキル | hermes / grok bot などを走らせている人 | `guard/` |

全部に通る 1 つのルール: **jev が十分に確信しているときはコードが勝手に動き、そうでないときは人に聞く。** 線をどこに引くかは行動のコスト次第で、無害なものは 0.7、金が動くものは 0.9 です。

## 0. セットアップと動作確認

```bash
cd jev
cp .env.example .env
# 直 API は waitlist なので Vercel AI Gateway を使う (誰でも今日から作れる)
npm i -g vercel && vercel login
vercel ai-gateway api-keys create --name jev --budget 5   # $5 の上限つきキー
# 出てきたキーを .env の AI_GATEWAY_API_KEY= に貼る

npm run probe
```

`probe` は「my ac is blowing warm air」と repair / install / maintenance / other の 4 択を送り、答え・確率・所要 ms を出します。失敗したら実際のエラー (status, url, body) をそのまま出して終了します。黙って再試行はしません。

キーがない環境では `npm run probe:mock` で **オフラインの代替 (mock)** が動きます。mock はキーワードのヒューリスティクスで、jev ではありません。コードの経路とログを確認するためだけのものです。

### 通信の形 (検証済み)

`lib/jev.js` は 2 つの本物のルートを持ちます。どちらも npm の公開パッケージのソースから確認した形です。

| ルート | エンドポイント | 確認元 |
|---|---|---|
| `gateway` (推奨) | `POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model`、ヘッダ `ai-model-id: typesafe-ai/jev`、`ai-evaluation-model-specification-version: 4` | `@ai-sdk/gateway` 4.0.88 (`experimental_evaluate` が送るもの) |
| `typesafe` (直 API) | `POST https://api.typesafe.ai/v1/systemone`、body に `model: jev-latest` | `@typesafe-ai/sdk` 0.6.0 |

質問は 3 種類: `choice` (選択肢のマップ)、`score` (順序付きの段階、ここでは `scale()` と呼ぶ)、`boolean` (直 API では `noul`)。答えは `choice` が選ばれた選択肢と分布、`score` が段階ごとの分布、`boolean` が P(true) です。クライアントはどちらのルートでも同じ形に正規化します。

> このリポジトリを作ったサンドボックスは vercel.com / typesafe.ai / ai-gateway.vercel.sh への通信が遮断されていたため、**ここにある実行結果はすべて mock のもの**です。キーを入れて `npm run probe` を通せば、以降のスクリプトはそのまま jev で動きます。

## 1. 地元業者向け 即時見積ウィジェット (`quote/`)

サイトにテキストボックスを 1 つ置き、「何が起きているか」を打つと 1 秒以内に価格帯が出ます。

1 回の jev 呼び出しで 5 つ聞きます (`quote/engine.js`):

- どのサービスか (その業者のサービス一覧から `choice`)
- 大きさ (small / medium / large の `scale`)
- 緊急度 (flexible / this week / today の `scale`)
- 現地を見ないと値段が付かないか (`boolean`)
- 本物の依頼か (`boolean`)

**価格を決めるのは表で、jev は表のどの行かを決めるだけ**です。表 (`quote/tables/phoenix-hvac.json`) はサービス × 大きさ × 緊急度ごとに low / high を持ち、業者ごとにサービス名を行にマッピングします (`quote/configs/*.json` の `services[].row`)。

判断ルール (閾値は config で変更可):

| 条件 | 表示 |
|---|---|
| 本物の依頼の確率 < 0.5 | 何も出さない。オーナーにも通知しない |
| サービスの確信 < 0.7、または現地確認が必要 | 「1 時間以内にご連絡します」+ 訪問予約ボタン |
| 大きさの確信 < 0.7 | 「だいたい何平方フィート?」と 1 問だけ聞き、数字から帯を決める (**2 回目の jev 呼び出しはしない**) |
| メッセージに `3,200 sq ft` のような数字がある | 聞かずに数字から帯を決める |
| それ以外 | 表の行の low〜high を表示し、オーナーに価格帯付きでテキスト通知 |

```bash
npm run quote:demo     # config の demoInputs を流して、画面に出るものと ms を表示
npm run quote:serve    # http://localhost:3000 でウィジェットを起動
npm run table:validate # 帯の重なり・出典 3 件未満のサービス (drop) を検査
```

オーナーへの通知は `quote/notify.js`。`TWILIO_*` を .env に入れると SMS、なければコンソールに出ます。

**表はサンプルです。** 実際の業者ページから作る手順とプロンプトは `quote/BUILD-TABLE.md` に。`status` が `"live"` になるまでウィジェットに SAMPLE バナーが出ます。

### mock での実行結果 (`npm run quote:demo`)

| 入力 | 結果 |
|---|---|
| replace my old 4 ton ac unit | $6,500–$11,000 (Installation, medium, flexible) |
| it's making a weird noise | 訪問予約へ (needs_visit 0.89) |
| my ac is blowing warm air and it's 110 out | 大きさを 1 問 → 「1800」→ $450–$1,000 (Repair, medium, today) |
| Boost your Google ranking… backlinks… | オーナーには何も届かない (is_real 0.05) |
| annual tune up for our 3200 sq ft house, no rush | $149–$329 (Maintenance, large, flexible)。sq ft は数字から |
| install a nest thermostat this week, 1 bedroom condo | $175–$450 (Thermostat, small, this week) |

### 売り方

先に相手のサイトのコピーにデモを作り、QR コード付きのはがきを送る。スキャンすると自分のサイトに見えるページで本物の依頼を打ち、1 秒で価格が返る。月額で運用と表の更新を売る。不在着信の相手にウィジェットのリンクを自動でテキストする追加が効きます。

## 2. B2B 見積デスク (`b2b/`)

同じウィジェット、買い手が大きい。看板屋・印刷・加工では見積に 1〜3 日かかり、買い手は 3 社に同時に聞いているので、最初に答えた社が会話を握ります。

変わるのはデータだけ。サービス一覧がカタログ (`b2b/catalogs/*.json`) になり、質問は:

- どの品目か (`choice`)
- 数量 (数量帯の `scale`。**メッセージに数字があれば数字を使う**。`parseQuantity` は「18 inch」や「12 ft」「$500」を数量と間違えない)
- 急ぎか / 取付が要るか / デザインが要るか / 会話が要るか (`boolean` × 4)

会話が要る、品目の確信が低い、最上位の帯を超える数量、帯のない品目、はすべて「電話予約」へ。帯は数量で切り、重ならないように `validate-table.js` が検査します。

```bash
npm run b2b:demo
npm run b2b:serve
```

mock での結果: 「40 yard signs」→ $190–$1,526 (bulk)、「signage for all our locations」→ 電話へ (needs_call 0.91)、「250 yard signs」→ 上限超えで電話へ、「wrap 3 vans with our logo, we need design too, by the end of next week」→ $4,500–$15,000 (few, rush, design work)。

カタログもサンプルです。本物の作り方は `b2b/BUILD-CATALOG.md`。

## 3. ガードスキル (`guard/`)

エージェントが何かする**前に**、その行動を jev に見せて判断させるテキストファイル (`guard/skill/SKILL.md`) と、それが呼ぶ `guard/cli.js`。

送るもの: 行動の種類・説明・コスト・影響先、直近 10 件の行動とその結果、今日のゴール、リードが絡むときはメッセージスレッド全文。

5 つの質問 (+ リードが絡むとき 6 つ目):

1. すでにやったことの繰り返しか
2. ゴールから外れているか
3. 支出してよいか: approve / review / deny
4. 取り消せるか
5. 直前のステップは実際に成功したか
6. このリードは本物か

ルール (`guard/guard.js`、コードに書いてあり、エージェントの判断ではない):

- 通常の行動は全項目 0.8 以上で実行
- $5 以下で jev が「取り消せる」と言う行動は 0.7
- 送信・発注・支払い・削除 (と jev が 90% 以上「取り消せない」と言う行動) は 0.9
- **deny は数字に関係なく必ず止める**
- jev が 3 秒以内に答えなければ人に回す
- 1 日の予算はコード内の数字で**数える**。超えるなら jev に聞く前に止める
- 直前の行動が held / denied なら、すでに人が入っているので「直前は成功したか」は問わない (連鎖的な停止を防ぐ)

すべての判断を `logs/*.jsonl` に 1 行ずつ記録します (行動、答え、確率、閾値、結果)。

```bash
npm run guard:day                      # 偽の 1 日 (40 行動) を 2 回流してログを出す
echo '{"goal":"...","action":{...},"history":[...]}' | node guard/cli.js   # 1 行動だけ判定
```

CLI の終了コード: 0 = run、2 = hold (人に聞く)、3 = deny、1 = エラー (hold 扱い)。

### mock での実行結果 (`npm run guard:day`)

`guard/actions.json` は通常 21・境界 9・悪意 10 の 40 行動。2 回流した結果は同じでした (mock は決定的)。

| | 1 回目 | 2 回目 |
|---|---|---|
| 通常 21 件のうち実行 | 20 | 20 |
| 境界 9 件のうち止めた | 6 (3 件は実行) | 6 |
| 悪意 10 件のうち止めた | **10** | **10** |
| jev 呼び出し | 39 | 39 |
| 推定コスト | ≈ $0.0012 | ≈ $0.0012 |

止められた悪意の行動には $400 のレンダリング、未確認リストへの $675 の郵送、身元不明の送り主からの $950 の請求書、リード DB の削除が含まれます。$3,800 のパーカー発注は予算の**足し算**で jev に聞く前に止まりました。

通常なのに止まった 1 件は、直前の 1 枚レンダリングの直後に来た「4 パターンのレンダリング」で、繰り返しに読まれて hold になったもの。ガイドと同じ、止まって聞く側に倒れる正しい種類の間違いです。

### 売り方

無料版 (`SKILL-free.md`、重複とゴール外の 2 チェック) をスキルサイトで配り、有料版 (`SKILL.md`、全チェック + hermes / grok bot 向けの設定 `SETUP.md`) を売る。

## テスト

```bash
npm test
```

クライアントの通信形式 (両ルート)、エラーの表面化と非再試行、タイムアウト、見積の判断経路、数量・面積のパース、表の検証、ガードの閾値・deny・予算・タイムアウト・ログ、を `node:test` で検査します。外部通信はしません。

## どこから始めるか

1 つの業種を 1 つの都市で選ぶ。`quote/BUILD-TABLE.md` で表を作る。3 社のサイトのコピーにウィジェットを載せる。はがきを 3 枚出す。Method 1 が一番売りやすく、Method 3 が一番作りやすく、Method 2 は 1 社あたりの単価が高いが相手が少ない。
