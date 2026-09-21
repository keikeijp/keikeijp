# 5. compaction — Claude Code 履歴の Jev 圧縮

## 目的

Claude Code のセッション履歴 (JSONL) は、探索的な `Read` / `Grep` / `Bash` の結果で膨らみ続ける。
`compaction` は tool_use / tool_result のペアごとに「この呼び出しは今も必要か」「結果を一字一句
残す必要があるか」を Jev に聞き、不要なものを段階的に削る。user / assistant の本文テキストは変更しない。

元ネタ: `tamaratran/fast-jev-compaction`

## 使い方

```bash
# セッション JSONL を圧縮して別ファイルに書く (末尾 4 メッセージは固定)
jevlab compaction --input ~/.claude/projects/<proj>/<session>.jsonl --output compact.jsonl --preserve 4 --stats

# 半分に減るまで閾値を上げて削る
jevlab compaction --input session.jsonl --output out.jsonl --target-ratio 0.5

# API なしで試す
jevlab compaction --input session.jsonl --backend mock --stats > /dev/null
```

Python から:

```python
from jevlab.core import Jev
from jevlab.apps.compaction import compact_messages, load_transcript

messages, fmt = load_transcript("session.jsonl")
new_messages, report = compact_messages(messages, Jev(), preserve_recent_messages=4)
print(report.to_dict())   # reduction_ratio, pairs_dropped, pairs_truncated, ...
```

入力は Claude Code の JSONL (`type: user|assistant`, `message.content` ブロック) と、
素の Anthropic messages 形式 (`[{role, content}]` の JSON 配列) の両方を受け付ける。`summary` 等の
その他の行はそのまま通す。

## アルゴリズム

1. 末尾 `--preserve` 件は固定 (Jev に聞かない、変更しない)
2. それより前の各ペアについて 1 回の `decide` で `keep_call` / `keep_result_verbatim` (Noul) を聞く。
   state は「会話の要約 (各メッセージ: 役割 + 本文 200 字 + ツール名と入力 60 字)」で、候補は
   `<<CANDIDATE>>` で印を付ける。候補の前後 8 件・冒頭 3 件・末尾 12 件だけ入れて state を小さく保つ
3. 段階的圧縮
   - 両方 no → ペア削除 (メッセージが tool ブロックだけなら丸ごと消す。本文が同居していれば結果を 1 行メモに)
   - 結果だけ no → 結果を 300 字 + メモに切り詰め
   - 残すペアでも古い `tool_use.input` は 1000 → 200 → 60 字に縮める (末尾からの距離で段階分け)
   - 3000 字を超える tool_result は head+tail
4. `target_ratio` を指定すると、達成するまで Noul 閾値を 0.6 → 0.7 → … と厳しくして再適用する

## 追加依存

なし。

## 課金・外部送信の注意

Jev にはツール呼び出しの要約 (ファイルパス、コマンド、結果の先頭 300 字) が送られる。秘密情報が
コマンドや結果に含まれる履歴は `--backend mock` で試すか、事前にマスクすること。
ペア数ぶんの Jev 呼び出しが発生する (並列実行)。

## 制限

- Jev は要約に基づいて判断するため、結果の細部 (行番号など) が後で必要になる場合は
  `keep_result_verbatim` が false でも困る可能性がある。`--preserve` を大きめにするのが安全
- 削除後のメッセージ列は user/assistant の交互性を保つよう配慮しているが、Claude Code の
  `parentUuid` チェーンは付け替えない (Claude Code 自身の resume には使わず、API に渡す用途向け)
