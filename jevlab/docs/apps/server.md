# 30. server (serve) — TypeSafe / OpenRouter 互換のローカル Jev API サーバ

## 目的

`jevlab.core` の `TypeSafeBackend` / `OpenRouterBackend` がそのまま話せる HTTP API を、ローカルモデルで提供する。
`TYPESAFE_BASE_URL=http://localhost:8787` や `OPENROUTER_DECISIONS_URL=http://localhost:8787/api/alpha/decisions` を
向ければ既存アプリが無改造でローカル推論に切り替わる。

- 元ネタ: [ekzhang/openjev-sglang](https://github.com/ekzhang/openjev-sglang)
- モジュール: `jevlab/local/server.py` (レジストリ名 `serve`)

## エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/v1/systemone` | TypeSafe 形: `{state, model, questions}` → `{answers, model, usage}` |
| POST | `/api/alpha/decisions` | 同じ入力。`answers` に加え OpenRouter 形 (質問名をトップレベルに `winner` / `score`+`levels` / `probability`) と `cost: "$0"` |
| GET | `/v1/models` | `{"object": "list", "data": [{"id": ..., "object": "model"}]}` |
| GET | `/healthz` | 認証不要のヘルスチェック |

認証: `JEV_SERVER_TOKEN` (または `--token`) があれば `Authorization: Bearer <token>` を要求 (401)。
`usage` / `cost` は常に 0。B200 級 GPU 箱に常駐させ、社内で使う想定。

## エンジン

`--engine fake | semif | jevlike | nanojev | mock | sglang`

- `mock`: core `MockBackend`
- `semif`: `LocalLogitBackend` (`--fake` で FakeLogitModel、`--model` で HF モデル ID)
- `jevlike`: `JevlikeBackend` (`--model model.json`)
- `nanojev`: `NanoJevBackend` (`--fake` で FakeSlotModel)
- `sglang`: `SGLangLogitModel` — SGLang / vLLM の OpenAI 互換サーバ (`--sglang-url http://host:30000/v1`) に
  `max_tokens=1, logprobs=true, top_logprobs=20` の chat completion を投げ、`top_logprobs` から候補トークン (A/B/…, Yes/No) の
  log 確率を読む。解析は純関数 `parse_top_logprobs(response, candidates)`

## 使い方

```bash
jevlab serve --engine fake --port 8787 --dry-run      # 起動せずサンプル要求を通す
jevlab serve --engine fake --port 8787
JEV_SERVER_TOKEN=secret jevlab serve --engine sglang --sglang-url http://localhost:30000/v1 --model Qwen/Qwen2.5-7B-Instruct
curl -s localhost:8787/v1/systemone -H 'Authorization: Bearer secret' \
  -d '{"state": "二重に課金された", "questions": {"intent": {"type": "choice", "criteria": {"refund": "返金", "bug": null}}}}'
TYPESAFE_API_KEY=x TYPESAFE_BASE_URL=http://localhost:8787 python -c "from jevlab.core import Jev; print(Jev('typesafe').choose('...', ['a','b']))"
```

`Server.handle(path, body, method, headers) -> (status, json)` はソケット非依存で単体テストできる。

## 追加依存

なし (標準ライブラリの `http.server`)。実モデルは各エンジンの依存に従う。

## 課金・外部送信

なし。`sglang` エンジンは指定した URL にプロンプト (state を含む) を送るので、外部ホストを指すときは注意。

## 制限

- `ThreadingHTTPServer` はリクエストごとにスレッドを起こす。GPU モデルは実装側でロックまたはバッチ化が必要
- TLS なし。リバースプロキシの後ろで使う
- レート制限・リクエスト ID ヘッダなど TypeSafe の運用機能は未実装
