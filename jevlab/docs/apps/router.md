# 8. router — ターン毎のモデル振り分けプロキシ

## 目的

Claude Code / Codex の各ターンについて「どんな種類のタスクか」「どれくらい難しいか」「画像が要るか」を
Jev に判定させ、ポリシー表でモデル名に写す。小さな判定モデル (Jev) が数十 ms で振り分けるので、
雑談や小さな修正を安いモデルへ、設計や難しいデバッグを大きなモデルへ回せる。

元ネタ: `gargpratyush/jev-router`

## 使い方

```bash
# 判定だけ見る (ネットワーク不要)
jevlab router --print-only "why does pytest hang after the last test?" --backend mock

# プロキシを起動して Claude Code から使う (Anthropic 互換 /v1/messages)
jevlab router --serve --port 8787 --policy policy.json
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude

# OpenAI 互換 (/v1/chat/completions) も同じポートで受ける
OPENAI_BASE_URL=http://127.0.0.1:8787/v1 codex
```

プロキシは `model` を書き換えて上流 (`--anthropic-base` / `$ANTHROPIC_BASE_URL`、`--openai-base` /
`$OPENAI_BASE_URL`) に urllib で転送し、レスポンスを chunked でそのまま流す (SSE ストリーミング対応)。
認証ヘッダ (`x-api-key`, `authorization`, `anthropic-version` など) は透過する。レスポンスには
`X-Jev-Router-Model` / `X-Jev-Router-Task` が付く。`GET /health`, `GET /decisions` で状態を確認できる。

## 判定

state = `{last_user_message (末尾 2000 字), n_turns, has_tools, code_blocks_count, files_mentioned, has_images}`

| 質問 | 型 |
| --- | --- |
| `task_kind` | Choice {trivial_chat, code_edit_small, code_edit_large, debugging, architecture, research, long_context} |
| `difficulty` | Score [trivial, easy, moderate, hard, expert] |
| `needs_vision` | Noul |

## ポリシー (JSON / YAML、既定とマージ)

```json
{
  "models": {"trivial": "claude-haiku-4-5-20251001", "easy": "claude-haiku-4-5-20251001", "moderate": "claude-sonnet-5", "hard": "claude-opus-5", "expert": "claude-opus-5"},
  "task_overrides": {"architecture": "claude-opus-5", "long_context": "claude-sonnet-5"},
  "vision_model": "claude-sonnet-5",
  "fallback": "claude-sonnet-5",
  "min_confidence": 0.35,
  "respect_client_model": false
}
```

優先順: confidence が `min_confidence` 未満 → `fallback`; 画像あり / `needs_vision` → `vision_model`;
`task_overrides` に task_kind があればそれ; それ以外は difficulty → `models`。
`respect_client_model: true` にすると、クライアントが `auto` 以外のモデルを明示した場合は書き換えない。

## 追加依存

なし (YAML ポリシーを使う場合のみ `pip install pyyaml`)。

## 課金・外部送信の注意

Jev には最後のユーザーメッセージ (2000 字) と件数・ファイル名などのメタ情報だけを送る。
本文全体は上流 API にのみ転送される。上流の課金はそのまま発生する。

## 制限

- プロキシは `/v1/messages` と `/v1/chat/completions` の POST のみ。他のパス (models 一覧など) は 404
- OpenAI 形式の `system` メッセージ、Anthropic の `system` フィールドは判定に使わない
- 同じ state (会話) は `Jev(cache=True)` でキャッシュされる
