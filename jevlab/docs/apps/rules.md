# 9. rules — 依頼に合う Claude Code ルールの選択

## 目的

`.claude/rules/` に置いた多数のルール (コーディング規約、レビュー観点、モジュール固有の注意) を毎回
すべて注入するとコンテキストを圧迫する。`rules` は、依頼文と編集中のファイルから
「今回のターンに関係するルールだけ」を選んで結合し、Claude Code の `UserPromptSubmit` フックとして
`additionalContext` に流し込む。

元ネタ: `EliaAlberti/jev-rules`

## ルールファイルの形式

```markdown
---
description: FastAPI のエンドポイント規約
paths: ["src/api/**/*.py", "tests/api/*"]   # glob (省略可)
tags: [api, python]
always: false                                # true なら常に含める
---
ルール本文 (Markdown)
```

front matter は依存なしの簡易パーサで読む (`key: 値`, `[a, b]`, 続く `- item` 行)。

## 選び方

1. `always: true` → 採用。`paths` の glob が編集ファイルに当たれば採用 (決定的)
2. 残りのルールは Jev に `relevant` (Noul) を 1 ルール 1 decide で並列に聞く。
   state = `{request (2000 字), edited_files (30 件), rule: {name, description, tags, paths, preview 300 字}}`。
   `--threshold` (0.5) 以上を採用
3. `always` → glob → Jev の確率降順に並べ、`--max-chars` (12000) を超えるルールは落とす

## 使い方

```bash
# 手で試す
jevlab rules --rules-dir .claude/rules --request "add a unit test for the login endpoint" --files src/api/login.py --json

# フックとして (stdin の JSON から prompt / cwd を読み、git status の変更ファイルも見る)
echo '{"prompt": "fix the api", "cwd": "."}' | jevlab rules --hook --git-files

# settings.json に貼るスニペット
jevlab rules --install-hook --rules-dir .claude/rules
```

`--hook` の出力:

```json
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<!-- rule: api (glob) -->\n..."}}
```

該当ルールがなければ `{}` を出す (何も注入しない)。Jev バックエンドが使えない場合は glob / always の
分だけを返してプロンプトを止めない。

## 追加依存

なし。

## 課金・外部送信の注意

Jev には依頼文 (2000 字まで) と編集ファイル名、ルールの説明と先頭 300 字が送られる。ルール数ぶんの
呼び出しが毎ターン発生するので、`paths` や `always` を書いておくと Jev 呼び出しを減らせる。

## 制限

- front matter はネストした YAML には対応しない
- 編集ファイルは `--files` と `git status --porcelain` (`--git-files`) から得る。Claude Code のフック JSON には
  編集ファイルが含まれないため、実際に触る予定のファイルは依頼文からしか推測できない
