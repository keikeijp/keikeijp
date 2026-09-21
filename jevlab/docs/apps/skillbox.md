# 10. skillbox — スキル管理 + MCP 配信 + Jev によるスキル推薦

## 目的

エージェント用の「スキル」(front matter に `name` / `description` を持つ `SKILL.md` を含むディレクトリ) を 1 か所で管理し、
MCP サーバとして Claude Code / Codex などに配信する。タスク説明を渡すと、Jev が各スキルについて
「適用できるか」(Noul `applies`) と「どれくらい役立つか」(Score `usefulness`) を並列判定して上位を推薦する。

## 元ネタ

- GitHub: `kitze/skillbox`

## 使い方

```bash
python -m jevlab.apps.skillbox list --root ./skills
python -m jevlab.apps.skillbox recommend "PDF の申請書を埋めたい" --root ./skills --top 3 --json
python -m jevlab.apps.skillbox add ./my-skill --root ./skills        # SKILL.md を含むディレクトリをコピー
python -m jevlab.apps.skillbox serve --root ./skills                 # MCP サーバ (stdio)
python -m jevlab.apps.skillbox config --root ./skills                # claude mcp add ... のスニペット
jevlab skillbox recommend "..." --backend mock                        # API なしで動作確認
```

`--root` 省略時は `$SKILLBOX_ROOT`、無ければ `~/.skillbox/skills`。

MCP ツール: `list_skills` / `get_skill(name)` / `recommend_skills(task, top_k, threshold)`。
リソース: `skill://<name>/<相対パス>` で各スキルのファイルを読める。

スキルの形:

```
skills/pdf-fill/SKILL.md
---
name: pdf-fill
description: PDF フォームを埋める
---
# 本文 ...
```

### 再利用できる MCP ヘルパ

`JsonRpcServer` は依存ゼロの最小 MCP 実装 (`initialize` / `ping` / `tools/list` / `tools/call` / `resources/list` / `resources/read`、
改行区切り JSON-RPC 2.0 の stdio)。`handle(message)` で 1 メッセージずつ処理できるので stdio なしでテストできる。
`jevlab/apps/mcp_server.py` (11) もこれを使う。

## 必要な追加依存

なし (標準ライブラリのみ)。

## 課金・外部送信の注意

`recommend` はスキルごとに 1 回 Jev を呼ぶ (N スキル = N 判定、並列)。state はタスク文とスキルの name / description だけで、
本文やスクリプトは送らない。`list` / `add` / `serve` の一覧・取得は Jev を呼ばない。

## 制限

- front matter は YAML のサブセット (スカラーと `[a, b]` 形式のリスト) のみ。
- 推薦は `applies >= threshold` (既定 0.5) で足切り。0 件なら「該当スキルなし」。
- MCP の prompts / sampling / 通知には未対応。
