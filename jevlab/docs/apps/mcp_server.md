# 11. typesafe-mcp — Jev の判断プリミティブを MCP ツールとして公開

## 目的

Claude Code / Codex / Cursor などの MCP クライアントから Jev を呼べるようにする。
大きい LLM が「分類・採点・はい/いいえ・並べ替え」を自分で考える代わりに、速くて安い Jev に委ねる。

## 元ネタ

- GitHub: `itsmostafa/typesafe-mcp`

## ツール (戻り値はすべて JSON テキスト)

| ツール | 引数 | 返すもの |
|---|---|---|
| `jev_choice` | `state`, `options{label: 説明}`, `instructions?` | choice / probabilities / confidence / ranked / margin |
| `jev_score` | `state`, `rubric[]` (低→高), `instructions?` | score (期待値) / level / probabilities / normalized |
| `jev_noul` | `state`, `instructions`, `criteria?` | noul (0..1) / yes |
| `jev_decide` | `state`, `questions{name: {type, criteria, instructions}}` | Decision 全体 (answers / model / latency) |
| `jev_rank` | `candidates[]`, `instructions`, `rubric?`, `context?` | ranked [{candidate, score}] 降順 |

## 使い方

```bash
python -m jevlab.apps.mcp_server --print-config          # claude mcp add / mcpServers JSON / Codex TOML を表示
claude mcp add jev -e TYPESAFE_API_KEY=... -- python -m jevlab.apps.mcp_server serve

python -m jevlab.apps.mcp_server serve                    # stdio (MCP クライアントが起動する)
python -m jevlab.apps.mcp_server list-tools --backend mock
python -m jevlab.apps.mcp_server call jev_noul --args '{"state": "今日中に対応して", "instructions": "急ぎか?"}' --backend mock
```

JSON-RPC の実装は `jevlab/apps/skillbox.py` の `JsonRpcServer` を再利用している。

## 必要な追加依存

なし。

## 課金・外部送信の注意

ツール呼び出し 1 回 = Jev 1 回 (`jev_rank` は候補数分の並列呼び出し)。MCP クライアントが渡した `state` がそのまま外部 API に送られるので、
クライアント側 (エージェントのプロンプト) で秘密情報を含めないよう注意する。`--backend mock` / `JEV_BACKEND=mock` なら送信しない。

## 制限

- `jev_decide` の questions は TypeSafe wire 形式 (`{"type": "choice", "criteria": {...}}` など) をそのまま受ける。形式不正は `isError: true` で返す。
- 引数の型検証は最小限 (JSON Schema はクライアント向けのヒント)。
