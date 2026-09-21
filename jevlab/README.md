# jevlab — Jev × SAM 3.1 の実験場

TypeSafe の **Jev** (System One モデル: 文章を生成せず `choice / score / noul` の型付き判断だけを 70〜500ms で返す) と、
Meta の **SAM 3.1** (テキストプロンプトで検出・セグメント・追跡) を組み合わせるための Python モノレポ。

- 依存ゼロのコア (`jevlab.core`, `jevlab.sam`)
- 記事で紹介された **30 リポジトリの再実装** (`jevlab.apps`, `jevlab.local`)
- CV × Jev のフラッグシップ **SceneJudge** (`jevlab.scenejudge`)

すべて **API キー無し (mock) でテストが通る**。キーを入れると本物の Jev / SAM 3.1 に切り替わる。

```bash
cd jevlab
pip install -e ".[dev,sam]"
python -m pytest -q

export TYPESAFE_API_KEY=...       # 公式 https://api.typesafe.ai/v1/systemone
# または
export OPENROUTER_API_KEY=...     # https://openrouter.ai/api/alpha/decisions (ウェイトリスト不要)
export META_API_KEY=...           # SAM 3.1 (https://api.meta.ai/v1/responses, model=sam-3.1)

jevlab list                                   # アプリ一覧
jevlab ask "二重に課金された。今日中に返金して" --choice refund,bug,other --noul "怒っているか?"
jevlab scenejudge demo                        # SAM x Jev をキー無しで体験
```

## コア API

```python
from jevlab import Jev, Choice, Score, Noul

jev = Jev()   # JEV_BACKEND=typesafe|openrouter|mock|local。未指定ならキーの有無で自動
d = jev.decide(
    state={"ticket": "I was charged twice, refund me today"},
    questions={
        "intent": Choice({"refund": "money back", "bug": "something broken", "other": None}, "Main request?"),
        "urgency": Score(["no rush", "this week", "today"], "How urgent?"),
        "angry": Noul("Is the customer angry?"),
    },
)
d.choice("intent").choice        # "refund"   + .probabilities / .confidence
d.score("urgency").level         # 2         + .score (期待値) / .legend
d.noul("angry").noul             # 0.31
jev.rank(candidates, "How relevant?", context=query)   # 並列で採点して並べ替え
jev.filter(candidates, "Is it spam?", threshold=0.9)   # noul で絞り込み
```

バックエンド: `TypeSafeBackend` / `OpenRouterBackend` (どちらも stdlib のみ) / `MockBackend` (語彙重なりのヒューリスティック) /
`ScriptedBackend`, `FunctionBackend` (テスト) / `LocalLogitBackend` (公開モデルの logit を直接読む、`jevlab.local.semif`)。

## ★ SceneJudge (CV × Jev)

```
画像/動画 → SAM 3.1 (物体 box + mask) → シーングラフ JSON (正規化座標・面積比・ゾーン・空間関係) → Jev の型付き判断 → ルール → 通知
```

```bash
jevlab scenejudge image desk.jpg -s desk --overlay out.png     # 机の散らかり: tidiness(score) / first_to_remove(choice) / needs_cleanup(noul)
jevlab scenejudge video clip.mp4 -s pet --fps 1                 # ペット監視: ゾーン入退出イベント付き
jevlab scenejudge watch ./cam -s parking --notify https://...   # 駐車場の空き
```

詳細: [docs/apps/scenejudge.md](docs/apps/scenejudge.md)

## 30 本のアプリ

`jevlab <name> --help`。各アプリの詳細は `docs/apps/<module>.md`。

| # | name | 元ネタ | 一言 |
|---|---|---|---|
| 1 | `ultrafast` | browser-use/jev-ultrafast | Jev が操作と対象要素を選ぶブラウザエージェント |
| 2 | `computer-use` | awlevin/typesafe-computer-use | 画面 OCR / アクセシビリティ木 → 操作 |
| 3 | `mobile` | droidrun/mobile-jev | adb の UI 階層から Android を操作 |
| 4 | `voice-browser` | moritzkremb/jev-voice-browser | 音声 → 意図 → Playwright |
| 5 | `compaction` | tamaratran/fast-jev-compaction | Claude Code 履歴の不要ツール結果を Jev で削る |
| 6 | `foreman` | thruwire/foreman | 自律コーディングエージェントの監督 |
| 7 | `review` | devagrawal09/jev-review | 差分の段階的レビュー → HTML |
| 8 | `router` | gargpratyush/jev-router | ターン毎のモデル振り分けプロキシ |
| 9 | `rules` | EliaAlberti/jev-rules | 依頼と編集ファイルでルールを選ぶ hook |
| 10 | `skillbox` | kitze/skillbox | スキル管理 + MCP 配信 + 推薦 |
| 11 | `mcp` | itsmostafa/typesafe-mcp | choice/score/noul を MCP ツールに |
| 12 | `shell-history` | mrnugget/jev-shell-history | zsh の履歴補完 |
| 13 | `pg` | realZachi/pg-jev | SQL の自然言語 WHERE / 分類 / 順位 |
| 14 | `search` | superagents-la/jev-search | 検索語・期間・再ランク |
| 15 | `graph` | jexp/neo4jev | グラフ探索の次の一手 |
| 16 | `unclutter` | kitze/unclutter | 広告/ポップアップの識別と非表示 |
| 17 | `meter` | ChetasLua/jevmeter | 発言をメーター化する字幕 |
| 18 | `sponsor-skip` | trungdq88/youtube-sponsor-detection | 字幕からスポンサー区間 |
| 19 | `warden` | DevMortimer/pi-warden | ルール違反・同じ失敗・未検証完了の監視 |
| 20 | `moderation` | brainstormity/Jev-Moderation-Bot | Discord のスパム/詐欺 URL |
| 21 | `geo` | usenotra/notra | AI 回答のブランド言及トラッカー |
| 22 | `home` | AboveColin/HA-Jev | Home Assistant の日常判定 |
| 23 | `mario` | fhshaik/typesafe-mario | ゲーム状態 → 行動 |
| 24 | `pilot` | standardagents/jevpilot | 走行シミュレータの進路選択 |
| 25 | `drone` | RomanSlack/jev-drone | ドローンの障害物回避 |
| 26 | `trader` | jarrodwatts/jev-trader | 売買判断の実験 (mock / dry-run のみ) |
| 27 | `semif` | TheoLeeCJ/SemIf | 公開モデルの logit から選択肢確率 |
| 28 | `jevlike` | vinnylarouge/jevlike | 可変長選択肢を一度に評価する小型モデル |
| 29 | `nanojev` | TianyuCodings/NanoJev | 並列判断ヘッド + ゲーム制御デモ |
| 30 | `serve` | ekzhang/openjev-sglang | TypeSafe / OpenRouter 互換のローカル API |

## 設計方針 (全アプリ共通)

- **Jev に渡す state は最小限の構造化 JSON**。生 HTML・巨大ログ・秘密情報は渡さない (32k トークン / 外部送信)
- **confidence / probability に閾値**を置き、閾値未満は「保留 → 人間 or 大きい LLM」へ落とす
- **安全に関わる判断は Jev に委ねない** (走行の衝突回避、ドローンの反射層、取引のリスク管理はすべて決定的なコード)
- **副作用は dry-run 既定**。削除・送信・取引・クリックは明示フラグでのみ実行
- 実 API はすべて stdlib (`urllib`) で呼び、SDK は任意

## 注意

- Jev / OpenRouter / Meta は従量課金。`jev.stats()` で呼び出し回数と平均レイテンシを確認できる
- state はそれぞれの API 提供者へ送られる。各アプリの docs に「何が送られるか」を書いてある
- 30 本の再実装は元リポジトリの**設計思想を再現したもの**で、コードの移植ではない。動作確認は mock バックエンドのテストのみ。実 API での品質は各自で確認を
