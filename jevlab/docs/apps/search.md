# 14. jev-search — 検索語選定・期間指定・結果の関連度ソート

## 目的

質問文から Jev が (1) 検索の期間 (`time_range`)、(2) 質問の種類 (`intent`)、(3) 決定的に生成した候補クエリからの最良クエリ
(`best_query`) を選び、検索プロバイダで取得した結果を relevance (Score) と is_spam (Noul) で並べ替える。

## 元ネタ

- GitHub: `superagents-la/jev-search`

## パイプライン

1. `plan(jev, question)`: `time_range` ∈ {any, day, week, month, year}、`intent` ∈ {news, howto, reference, product, code, local}。
   候補クエリは `candidate_queries()` が決定的に作る (original / keywords-only / quoted / intent 別の `site:` ヒント)。Jev は選ぶだけ。
2. `fetch(query, time_range, provider)`: `SearchProvider` を差し替え可能
   - `StaticProvider` (JSON ファイル、オフライン)
   - `DuckDuckGoProvider` (HTML 版を urllib で取得、キー不要)
   - `BraveProvider` (`BRAVE_API_KEY`) / `SerpAPIProvider` (`SERPAPI_API_KEY`)
3. `rerank(jev, question, results)`: relevance (0..1) x (1 - spam) で降順、spam >= 0.8 は除外。

## 使い方

```bash
python -m jevlab.apps.search --q "python で pdf を結合する方法" --provider static --results results.json --top 5 --json
python -m jevlab.apps.search --q "latest rust release" --provider duckduckgo
BRAVE_API_KEY=... python -m jevlab.apps.search --q "..." --provider brave
```

`results.json` は `[{"title", "url", "snippet"}]` または `{"results": [...]}`。

## 必要な追加依存

なし (urllib のみ)。

## 課金・外部送信の注意

Jev 呼び出しは plan で 2 回 + 結果件数分の並列採点。質問文・候補クエリ・結果のタイトル / URL / スニペット (400 文字まで) が送られる。
検索プロバイダにはクエリが送られる。DuckDuckGo HTML 版のスクレイピングは利用規約と頻度に注意。

## 制限

- DuckDuckGo のパーサは正規表現ベースで、HTML 構造の変更で壊れる可能性がある。
- `site:` ヒントは固定表 (`SITE_HINTS`)。
