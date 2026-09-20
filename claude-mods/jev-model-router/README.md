# jev-model-router — Claude Code Mod

Claude Code に送る**すべてのリクエスト**を、TypeSafe の判断モデル **Jev** に一瞬だけ見せて、

- **サブエージェントのモデル** (`agent.spawn`)
- **メインのモデル** (セッション開始時の 1 回だけ。プロンプトキャッシュを壊さないため)
- **推論の effort** (メイン会話の毎ターン)

を自動で選ぶ Claude Code Mod (function hooks プラグイン) です。
Jev には **TypeSafe の直接 API** か **Vercel AI Gateway** のどちらかで接続します。キーが無ければ Claude Code 内蔵の分類器にフォールバックします。

```
[jev-model-router] ready on typesafe (https://api.typesafe.ai/v1/systemone); routing subagent model, main effort, main model (session-start)
[jev-model-router] jev: tier fast (1.00) · effort 0.1 → low (0.95) · risky 0.03 · 404ms
> print the contents of package.json
[jev-model-router] main loop → claude-haiku-4-5-20251001, effort low: fast (confidence 1.00)
```

## 仕組み

| イベント | 何をするか | スイッチ |
|---|---|---|
| `prompt.submit` | プロンプト本文を Jev に送り、`tier` / `effort` / `risk` を 1 リクエストで取得 | — |
| `turn.start` | プロンプト本文でその判断をターン ID に紐付ける | — |
| `turn.step` (index 0) | メイン会話の `effort` を書き換える。モデルはセッション最初のリクエストのみ書き換える | `routeMainEffort`, `mainModelRouting` |
| `turn.step` (index > 0) | 同じターンのツールループ中は最初の判断を使い回す | — |
| `agent.spawn` | サブエージェントの prompt / description / type を Jev に送り、`model` を書き換える | `routeSubagentModel` |

Jev は文章を生成せず、型付きの質問に確率付きで答えます。聞いているのは 3 つだけです。

- `tier` (choice): `fast` / `balanced` / `deep` のどれが「この作業を十分こなせる最も安い層」か。モデル名は一切見せません
- `effort` (score): どれだけ段階的な推論が要るか (0..3 → `low` / `medium` / `high` / `xhigh`)
- `risk` (noul / boolean): 実行そのものが本番環境・実際のお金・復元不能なデータに触れるか

### 判断ルール

- **上げる** (大きいモデル・多い effort) には `minUpgradeConfidence` (既定 0.3)。間違えても損するのはお金だけなので低め
- **下げる** には `minDowngradeConfidence` (既定 0.6)。間違えるとタスクが足りないモデルに落ちるので高め
- `risk` が `riskThreshold` (既定 0.7) を超えたら、確信度に関係なく `deep` 層と `high` 以上の effort
- 確信度を返さないバックエンド (Gateway で分布が無い場合、内蔵分類器) は **上げる方向にしか動かせない**
- すでに同じ層にいるなら変更なし (`claude-opus-5[1m]` は 1M コンテキストのまま)
- 数値の effort や未知のモデル ID など、方向が判定できないものは触らないか、緩い方の閾値で扱う
- タイムアウト・非 2xx・壊れたレスポンス・例外はすべて **fail-open**: リクエストはエンジンが組んだまま通ります

### なぜメインモデルはセッション開始時だけなのか

`turn.step` の `model` は「そのリクエスト」のパラメータなので、途中で変えるとプロンプトキャッシュが無効になり、長いコンテキストでは安いモデルに切り替える節約より再キャッシュのほうが高くつきます。そこで既定 (`mainModelRouting: "session-start"`) では、**プロセス起動後の最初のメインリクエスト** (まだキャッシュが無い) でだけモデルを決め、以降はそのまま固定します。`--resume` した (メッセージ数が多い) セッションでは触りません。

effort はキャッシュに影響しないので毎ターン動かします。サブエージェントは毎回コンテキストが新しいので、こちらも毎回選びます。

| `mainModelRouting` | 挙動 |
|---|---|
| `session-start` (既定) | 最初のプロンプトの判断でメインモデルを決めて固定 |
| `every-turn` | 毎ターン判断し直す (キャッシュ無効化のコストを自分で測ってから) |
| `off` | メインモデルには触らない |

## バックエンド

| `provider` | エンドポイント | モデル | 確信度 |
|---|---|---|---|
| `typesafe` | `POST https://api.typesafe.ai/v1/systemone` | `jev-latest` | 回答ごとに `confidence` を返す |
| `gateway` | `POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model` | `typesafe-ai/jev` | 確率分布の最大値から導出 (分布が無ければ不明) |
| `builtin` | Claude Code 内蔵の `$.model.classify` | 小さい高速モデル | なし (上げる方向のみ) |

`auto` (既定) は TypeSafe キー → Gateway キー → 内蔵、の順に選びます。両方のキーがあるときは、較正された確信度を返す TypeSafe を優先します。バックエンドを指定したのにキーが無い場合は内蔵分類器に落ち、ログに 1 回だけそう書きます。

**プライバシー:** キーを設定すると、プロンプト本文 (サブエージェントでは prompt / description / agent type) がそのキーのバックエンドに送られます。それ以外は送りません。キーが無ければ何も外に出ません。

## インストール

前提: Claude Code **2.1.259 以上** と `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`。

### 1. プラグインとして読み込む

一番手軽なのは `--plugin-dir` です (ホットリロード付き、`jev-model-router` という ID で読み込まれます)。

```sh
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir /path/to/claude-mods/jev-model-router
```

常用するなら `~/.claude/skills/` にコピー (またはシンボリックリンク) すると、次のセッションから `jev-model-router@skills-dir` として自動で読み込まれます。

```sh
ln -s /path/to/claude-mods/jev-model-router ~/.claude/skills/jev-model-router
```

### 2. キーを設定する

`/config` から設定するか、ユーザー設定 (`~/.claude/settings.json`) の `pluginConfigs` に書きます。キーは **読み込み方によって変わるプラグイン ID** です。

```json
{
  "pluginConfigs": {
    "jev-model-router": {
      "options": { "typesafeApiKey": "..." }
    }
  }
}
```

- `--plugin-dir` で読んだとき: `"jev-model-router"`
- `~/.claude/skills/` から自動で読んだとき: `"jev-model-router@skills-dir"`

キーの場所が違うと全オプションが既定値のままになり、`ready on the built-in classifier, no key set` と表示されます。

### 3. 動作確認

```sh
claude plugin validate /path/to/claude-mods/jev-model-router
```

フックしているイベントと `$` の呼び出し一覧が出れば OK です。セッション中はトランスクリプトの `[jev-model-router] ready on ...` 行と、プロンプト下のステータス行 (`jev · fast 1.00 → claude-haiku-4-5-20251001/low`) で動いていることが分かります。

何も出ないときは順に確認してください。

1. `claude -p` (ヘッドレス) にはトランスクリプトが無い → `~/.claude/debug/<session-id>.txt` を見る
2. プラグインが読み込まれていない → `claude --debug` で `hooks module jev-model-router@inline loaded ... events: session.start,prompt.submit,turn.start,turn.step,agent.spawn` を探す
3. `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` が無い → debug ログに `hooks modules not loaded: rollout flag (tengu_plugin_hooks_modules) is off`

## オプション

`.claude-plugin/plugin.json` の `userConfig` で宣言しています。

```
typesafeApiKey          string   TypeSafe API キー (優先)
gatewayApiKey           string   Vercel AI Gateway キー
provider                string   auto | typesafe | gateway | builtin        (auto)
typesafeBaseUrl         string   空なら https://api.typesafe.ai
typesafeModel           string   空なら jev-latest
gatewayBaseUrl          string   空なら https://ai-gateway.vercel.sh/v4/ai
gatewayModel            string   空なら typesafe-ai/jev
fastModel               string   fast 層 (エイリアスか完全な ID)            (haiku)
balancedModel           string   balanced 層                                  (sonnet)
deepModel               string   deep 層                                      (opus)
minUpgradeConfidence    number   上げるのに必要な確信度                       (0.3)
minDowngradeConfidence  number   下げるのに必要な確信度                       (0.6)
riskThreshold           number   これを超える risk は deep + high 以上に強制 (0.7)
routeSubagentModel      boolean  サブエージェントのモデルを選ぶ               (true)
routeMainEffort         boolean  メイン会話の effort を動かす                 (true)
mainModelRouting        string   session-start | every-turn | off            (session-start)
timeoutMs               number   Jev を待つ上限。超えたらそのまま通す         (800)
logDecisions            boolean  判断をトランスクリプトとステータス行に出す   (true)
```

層にはエイリアス (`haiku` / `sonnet` / `opus` / `fable`) か完全な ID を指定できます。サブエージェントはエイリアスのまま渡され (Agent ツールと同じ)、メイン会話のリクエストは ID が必要なので `haiku → claude-haiku-4-5-20251001`、`sonnet → claude-sonnet-5`、`opus → claude-opus-5`、`fable → claude-fable-5-1` に解決します。特定バージョンに固定したければ完全な ID を書いてください。

## 開発

```sh
bun test tests              # 純粋ロジックと、疑似エンジンで駆動するフックのテスト
sh scripts/fetch-types.sh   # types/claude-code.d.ts を取得 (セッション内なら /plugin-types でも可)
tsc -p tsconfig.json        # hooks/ を Anthropic の型宣言に対して型検査
claude plugin validate .    # マニフェストとフックモジュールの検証
```

```
.claude-plugin/plugin.json  マニフェストと userConfig
hooks/hooks.json            modules: ["./register.ts"]
hooks/register.ts           フック本体 (session.start / prompt.submit / turn.start / turn.step / agent.spawn)
hooks/jev.ts                Jev のワイヤ形式: バックエンド選択、リクエスト組み立て、レスポンス読み取り
hooks/policy.ts             判断ルール: 閾値、risk、effort の段、ターンへの紐付け
hooks/report.ts             ログ行とステータス行の文言
tests/                      bun:test
```

Mod は `node_modules` 無しで動くため、SDK は使わず `$.http.fetch` で直接 HTTP を話します。TypeSafe のワイヤ形式は `@typesafe-ai/sdk`、Gateway のものは AI SDK の `experimental_evaluate` (`@ai-sdk/gateway`) に合わせています。どちらも変わる可能性があります。

**Early access:** function hooks は Claude Code 2.1.259+ で `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` のときだけ読み込まれ、`$` の API はリリース間で変わることがあります。型は https://github.com/anthropics/claude-code/tree/main/mods の宣言に合わせています。

このモッドは [claude-code-templates](https://github.com/davila7/claude-code-templates) の `productivity/jev-model-router` (MIT) の設計を参考に、メインモデルを「セッション開始時だけ」決める方針で書き直したものです。
