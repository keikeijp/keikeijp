# 20. moderation — Discord のスパム/詐欺 URL 判定と処分

## 目的

Discord のメッセージを Jev に 1 回だけ判定させ (種類 / 深刻度 / 詐欺 URL か)、閾値ポリシーで
`none / warn / delete / timeout(分) / flag_for_admin` を決める。Jev の確信度が低いときは**必ず** `flag_for_admin`
(🚩 リアクションを付けて人間に回す) に落とし、自動で処分しない。

元ネタ: `brainstormity/Jev-Moderation-Bot`

## 判定の中身

`assess(jev, message)` は次の構造化 state だけを送る (ユーザー ID、フル URL、サーバー名は送らない):

```json
{"content": "...(500 文字以内)", "author_age_days": 2, "is_new_member": true, "has_links": true,
 "link_domains": ["discord-nitro.example"], "mentions_count": 5, "recent_messages_by_author": ["..."], "channel_topic": "..."}
```

質問: `kind` Choice `{ok, spam, scam_link, harassment, nsfw, self_promo, off_topic}` /
`severity` Score `[none, mild, moderate, severe]` / `is_scam_url` Noul。

`decide_action(assessment, policy)` の既定ポリシー (`--policy policy.json` で上書き):

| キー | 既定 | 意味 |
| --- | --- | --- |
| `min_confidence` | 0.6 | これ未満は必ず `flag_for_admin` |
| `scam_url_threshold` | 0.8 | `is_scam_url` がこれ以上 (または kind=scam_link) → `delete` (severe なら `timeout`) |
| `warn_level` / `delete_level` / `timeout_level` | 1 / 2 / 3 | severity レベルの閾値 |
| `timeout_minutes` | 60 | timeout の長さ |
| `escalate_new_members` / `new_member_days` | true / 3 | 参加 3 日以内のメンバーは 1 段階厳しくする |

## 使い方

```bash
# オフライン判定 (JSON 1 件または配列)
jevlab moderation --backend mock assess --json msg.json
jevlab moderation --audit audit.jsonl assess --json msgs.json

# Discord ボット (要 discord.py)。まず --dry-run で処分を表示だけにする
export DISCORD_TOKEN=...
jevlab moderation run --token-env DISCORD_TOKEN --dry-run
jevlab moderation run --token-env DISCORD_TOKEN --policy policy.json --audit moderation_audit.jsonl
```

ボットには Message Content / Server Members のインテントが必要。

### 管理者コマンド (メッセージ管理権限が必要)

```
!jev override <message_id> none|warn|delete|timeout   判定を上書きして実行 (監査ログに記録)
!jev policy                                            現在の閾値を表示
!jev dry-run on|off                                   処分の実行/表示だけを切り替え
```

## 監査ログ

`--audit` の JSONL に 1 行 1 イベント (`assess` / `override`) で、Jev の回答と決定を記録する。

## 追加依存

- `run` のみ `pip install 'discord.py>=2.3'`

## 課金・外部送信の注意

- メッセージ 1 件につき Jev 1 回。
- ボットトークンは環境変数からのみ読む。Jev には送らない。

## 制限

- ポリシーは severity レベルの閾値のみ。繰り返し違反者の累積 (ストライク) 管理はしていない。
- `recent_messages_by_author` はプロセス内メモリなので、再起動で消える。
- mock バックエンドは語彙の重なりで判定するため、デモ用途以外では使わないこと。
