# 21. geo — AI 回答でのブランド言及を追跡する GEO ツール

元ネタ: [usenotra/notra](https://github.com/usenotra/notra)

## 目的

「おすすめのノートアプリは?」のような質問を AI アシスタントに投げ、回答の中で自社ブランドと競合が
**どの順番で・どんな感情で・どれくらい強く**言及されるかを継続的に記録する (GEO = Generative Engine Optimization)。
分類は Jev が担当するので、1 回答あたり数十 ms・ほぼ無料で回せる。

## 流れ

1. `prompts.json` の質問を回答プロバイダに投げる (Anthropic Messages API / OpenAI 互換 / オフラインの静的 JSON)
2. 回答をセンテンスに分け、ブランド名を含む文だけを正規表現で抜く (`extract_mentions`)。`position_rank` は初出順
3. 文ごとに Jev へ 1 回: `sentiment` Choice {positive, neutral, negative} / `recommendation_strength` Score
   [not recommended, mentioned, suggested, top pick]。`decide_many` で並列
4. sqlite に保存し、share-of-voice / 平均センチメント / 平均順位 / 順位の推移を出す

## 使い方

```bash
cat > prompts.json <<'EOF'
{"brands": ["Notion"], "competitors": ["Obsidian", "Evernote"],
 "prompts": ["おすすめのノートアプリを 3 つ教えて", "チーム向けのドキュメントツールは? {brand_description}"]}
EOF

# オフライン (回答を JSON で渡す)
jevlab geo --prompts prompts.json --static answers.json --db geo.sqlite

# 実際に AI に聞く
export ANTHROPIC_API_KEY=...          # または OPENAI_API_KEY (+ OPENAI_BASE_URL)
jevlab geo --prompts prompts.json --provider anthropic --db geo.sqlite

# A/B: プロンプト内の {brand_description} に 2 種類の自社説明を入れて比べる (事前 A/B テストの代わり)
jevlab geo --prompts prompts.json --ab "Notion は無料枠が広い" "Notion は AI 機能が強い" --json-out ab.json

# 蓄積分のレポートだけ
jevlab geo --prompts prompts.json --db geo.sqlite --report-only
```

出力は Markdown 表 (`| brand | mentions | share of voice | avg sentiment | avg strength (0-3) | avg rank |`) と、
`--json-out` で `rank_over_time` を含む JSON。

## 追加依存

なし (すべて stdlib)。

## 課金・外部送信

- 回答生成は Anthropic / OpenAI 互換 API の課金。`GEO_ANTHROPIC_MODEL` / `GEO_OPENAI_MODEL` でモデルを変えられる
- Jev には「質問 (200 文字)・ブランド名・該当する 1 文 (300 文字)」だけを送る

## 制限

- ブランド名の単語境界マッチなので、略称や表記揺れは `brands` に列挙する
- Jev は事実確認をしない。「言及の仕方」の分類だけ
