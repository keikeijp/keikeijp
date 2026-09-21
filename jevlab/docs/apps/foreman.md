# 6. foreman — 自律コーディングエージェントの監督

## 目的

`codex exec` や `claude -p` のような自律コーディングエージェントを子プロセスとして起動し、出力を
見張りながら「続けさせる / 検証する / 止める / 口を挟む」を Jev に判断させる。
「完了しました」と言いながらテストを走らせていない、同じ出力をループしている、といった
典型的な失敗を検知して、検証コマンドの結果をエージェントに突き返す。

元ネタ: `thruwire/foreman`

## 使い方

```bash
# Codex を監督。20 行ごと / 30 秒アイドルごとに判定し、verify 判定なら pytest を回して結果を stdin に返す
jevlab foreman --cmd 'codex exec --full-auto "fix the failing tests"' \
  --task "fix the failing tests" --verify "pytest -q" --interval 20 --idle 30 --max-minutes 30 --log session.jsonl

# サブプロセスも検証コマンドも動かさず、内蔵デモ出力で流れだけ確認
jevlab foreman --task demo --verify "pytest -q" --dry-run --backend mock
```

Python から:

```python
from jevlab.core import Jev
from jevlab.apps.foreman import Foreman, ForemanConfig, SubprocessAgent

config = ForemanConfig(task="...", verify_cmd="pytest -q", interval_lines=20, idle_seconds=30, dry_run=False)
session = Foreman(Jev(), SubprocessAgent("codex exec ...", cwd="."), config).run()
print(session.summary())
```

## 判定

判定ごとに 1 回の `decide` で 4 問を聞く。

| 質問 | 型 | 意味 |
| --- | --- | --- |
| `verdict` | Choice {continue, verify, stop, intervene} | 監督者の次の行動 |
| `progress` | Score [stuck, slow, steady, fast] | 進捗 |
| `claims_completion` | Noul | 完了を主張しているか |
| `looks_looping` | Noul | 同じ出力の繰り返しか (末尾行の重複率 ≥ 0.6 でも真とみなす) |

state は `{task, recent_output (末尾 40 行、各 300 字), elapsed_seconds, files_changed (git status --porcelain), tests_ran (正規表現), claims_done (正規表現), repeat_ratio, ...}`。

- `verify`: 検証コマンドを実行し、`[foreman] verification ... PASSED/FAILED` + 出力末尾 30 行を stdin に送る。
  通れば既定で停止 (`stop_when_verified`)。stdin を受け付けないエージェントならログに記録するだけ
- `intervene` (または looping): 促しメッセージを送る。`--max-interventions` (既定 3) を超えたら停止
- `stop`: プロセスを terminate
- `verdict` の confidence が `min_confidence` (0.4) 未満なら安全側の `continue` に落とす

`--log` を付けると start / line / check / verify / send / stop / end のイベントを JSONL に追記する。

## 追加依存

なし。`git` があれば変更ファイル一覧を state に入れる。

## 課金・外部送信の注意

Jev にはエージェントの出力末尾 (最大 40 行 × 300 字) とタスク説明が送られる。出力にトークンや
秘密情報が流れる場合は注意。判定回数は「行数 / interval + アイドル回数」程度。

## 制限

- `--dry-run` は内蔵デモ出力 (`FakeAgentProcess`) を使う。実コマンドの起動は `--cmd` 指定時のみ
- 検証コマンドは shell 経由で実行される (`--dry-run` ではスキップ)。信頼できるコマンドだけ渡すこと
- stdin での対話に対応していないエージェント (一方向の `-p` 実行など) には結果を返せず、記録だけになる
