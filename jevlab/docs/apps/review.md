# 7. review — git 差分の段階的レビュー

## 目的

`git diff` (または diff ファイル、リポジトリ全体) をハンク単位に分け、Jev に「リスク・種類・
テスト要否・破壊的変更か」を採点させ、危ないハンクだけ関数レベルのコンテキストを添えて
深掘りし、最後にファイル別集計と PR 全体のマージ可否スコアを出す。JSON と自己完結の HTML
レポートを生成する。

元ネタ: `devagrawal09/jev-review`

## 使い方

```bash
# 作業ツリーの差分をレビューして HTML に
jevlab review --html report.html

# ステージ済み / ブランチ差分 / diff ファイル / リポジトリ全体
jevlab review --staged --json review.json
jevlab review --ref main...HEAD --html report.html
jevlab review --diff-file pr.diff --json -
jevlab review --repo ./src --html repo.html

# ローカルで配信して眺める (テスト対象外)
jevlab review --html report.html --serve 8000
```

## 段階

| Stage | 対象 | 質問 (1 ハンク 1 回の decide) |
| --- | --- | --- |
| 1 | 全ハンク | `risk` Score [none, low, medium, high, critical] / `category` Choice {logic, security, performance, error_handling, api_change, test, style, docs, other} / `needs_test` Noul / `breaking` Noul |
| 2 | risk ≥ medium (`--deep-min-risk`) | `has_bug` / `security_issue` / `needs_human_review` Noul。state に変更行を含む関数 (`--root` 配下のファイルから切り出し) を添える |
| 3 | ファイル別集計 → PR | `merge_readiness` Score [not mergeable, needs work, mergeable with small fixes, mergeable] |

Jev は文章を生成できないので、各ハンクの説明 (`note`) は「カテゴリ + ファイル + 行範囲 + リスク +
確率付きフラグ」をルールで組み立てる。Stage 1・2 は `decide_many` で並列。

Stage 1 の state は `{file, hunk_header, diff (3000 字まで), added_lines, removed_lines}`、
Stage 2 は `{file, hunk, function_context (6000 字まで)}`。category の confidence が 0.3 未満なら `other` に落とす。

## 出力

- JSON: `merge_readiness(_label)`, `files{...max_risk, categories, needs_test, breaking, possible_bugs}`, `hunks[...]`
- HTML: ファイル別サマリ表 + リスク順に並べたハンク (追加/削除行の色分け、エスケープ済み)

## 追加依存

なし。

## 課金・外部送信の注意

差分本文 (ハンクごと最大 3000 字) と、Stage 2 では周辺の関数本体 (最大 6000 字) が Jev に送られる。
秘密情報を含む差分は `--backend mock` で確認するか除外すること。呼び出し回数は
「ハンク数 + medium 以上のハンク数 + 1」。

## 制限

- diff の解析は unified 形式のみ (バイナリ差分・rename ヘッダは無視)
- `--repo` は 80 行ごとの「全行追加」ハンクとして扱う粗い読み方
- `--serve` は単一ページを返すだけの簡易サーバ
