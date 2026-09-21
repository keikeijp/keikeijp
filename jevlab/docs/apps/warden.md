# 19. warden — エージェントのルール違反 / 同じ失敗 / 未検証完了を監視

## 目的

コーディングエージェント (Pi、Claude Code、その他) のイベントログを読み、
「プロジェクトルールに違反した」「同じエラーで同じ試行を繰り返している」「テストを走らせずに完了と言っている」
を検出して findings JSON を出す。critical があれば終了コード 1 (CI や親エージェントが止められる)。

元ネタ: `DevMortimer/pi-warden`

## 入力

- ルール Markdown: 箇条書き (`-`, `*`, `1.`) 1 行 = 1 ルール
- イベント JSONL: 1 行 1 イベント `{"ts": 1700000000, "kind": "tool_call" | "tool_result" | "message", "text": "..."}`
  (`ok: false` や `error` フィールドがあればエラー扱い)

## チェックの中身

| チェック | Jev の質問 | 呼び出しを抑える工夫 |
| --- | --- | --- |
| `rule_violation` | `violates_rule` Noul (イベント × ルール) | ルール語彙の 20% 以上がイベントに現れるペアだけ聞く |
| `repeated_failure` | `same_failure_again` Noul (直前のエラーと比較) | difflib の類似度 ≥ 0.85 なら Jev なしで検出、0.4 未満は無視、その間だけ聞く |
| `unverified_completion` | `claims_completion` Noul → `verified` Noul | 完了っぽい語 (done/fixed/完了 …) を含む message だけ聞く。後続のツール呼び出しが皆無なら Jev なしで critical |

severity: probability ≥ 0.9 (または difflib 一致、検証コマンド皆無) は `critical`、それ以外は `warning`。

## 使い方

```bash
jevlab warden --rules RULES.md --events agent.jsonl --json-out findings.json
echo $?   # critical があれば 1

# ログを tail し続けて新しい finding を逐次表示 (テスト対象外)
jevlab warden --rules RULES.md --events agent.jsonl --watch --interval 2
```

Claude Code の場合は hooks (PostToolUse / Stop) で `{"kind": ..., "text": ...}` を JSONL に追記すれば入力になる。

## 追加依存

なし。

## 課金・外部送信の注意

- Jev に送るのはイベントテキスト (600 文字以内) とルール文のみ。
- 呼び出し数の上限は「重なりのあるペア数 + 中間類似度のエラー数 + 完了宣言数」。

## 制限

- ルール違反の事前フィルタは語彙の重なりなので、言い換えられた違反は取りこぼす (min_overlap を下げると呼び出しが増える)。
- 「検証コマンド」の判定は宣言後のツール呼び出しの有無と Jev の判断であり、テストが実際に通ったかまでは見ない。
- `--watch` はファイル全体を毎回再読込する単純実装。巨大ログには向かない。
