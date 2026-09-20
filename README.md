# dtm-agent

DTM (デスクトップミュージック) に特化した AI エージェントの土台です。

「この曲のこの部分に合うサンプルを探して、メロディまで DAW に置いてほしい」
「この曲みたいなのを作りたい」
といった依頼を、Claude がツールを使って **参照曲の解析 → サンプル探索 → MIDI 生成 → DAW への配置** まで一貫して実行します。

```
参照曲 (Spotify URL + 手元の音声)            サンプルライブラリ (PC 内 / Freesound)
        │                                           │
        ▼                                           ▼
 analysis: BPM / キー / 質感 / 特徴ベクトル    samples: 音響類似 + キーワード検索
        │                                           │
        └──────────────► Claude (tool_runner) ◄──────┘
                              │  set_project / add_midi_clip / add_audio_clip ...
                              ▼
                        ProjectPlan (設計図)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        FileBridge      ReaperProjectBridge   AbletonOscBridge
     (.mid + samples)      (.rpp 生成)      (AbletonOSC で即配置)
```

## できること (v0.1)

| 機能 | 状態 | 備考 |
|---|---|---|
| 音声区間の解析 (BPM / キー / energy / brightness / percussiveness) | ✅ | librosa。区間は `1:05-1:21` 形式 |
| Spotify URL → 曲名・アーティスト等のメタデータ | ✅ | 音声は取得不可 (下記の制約参照) |
| PC 内サンプルのインデックス化と参照区間との音響類似検索 | ✅ | MFCC + スペクトル特徴の cosine 類似 + ファイル名キーワード |
| Freesound (世界中の CC サンプル) のキーワード検索 | ✅ | `FREESOUND_API_KEY` が必要 |
| メロディ / コード / ベースの MIDI 生成 (スケール検証付き) | ✅ | Claude がノートを決め、ツールがキー外の音を拒否 |
| 汎用書き出し (.mid + サンプルコピー + README) | ✅ | どの DAW でも読める |
| REAPER プロジェクト (.rpp) の直接生成 | ✅ | 開くだけでトラック / MIDI / オーディオが並ぶ |
| Ableton Live へのリアルタイム配置 | ✅ MIDI のみ | [AbletonOSC](https://github.com/ideoforms/AbletonOSC) 経由。オーディオはブラウザからドラッグ |
| CLAP 等の埋め込みモデルによるテキスト→音の検索 | 🔜 | `LocalLibrary(embedder=...)` に差し込める設計 |
| Splice 連携 | 🔜 | 公開 API が無いため MCP / ブラウザ自動化を検討 |

## セットアップ

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...          # Claude
export FREESOUND_API_KEY=...         # 任意: Freesound 検索
export SPOTIFY_CLIENT_ID=...         # 任意: Spotify メタデータ
export SPOTIFY_CLIENT_SECRET=...
```

## 使い方

### 1. API 不要のコマンドで動作確認

```bash
# 手元のサンプルフォルダをインデックス化 (初回のみ、以後は .dtm_agent_index.json を再利用)
dtm-agent index ~/Samples

# 参照曲の 1:05-1:21 を解析
dtm-agent analyze ~/Music/ref.wav --segment 1:05-1:21

# その区間の音に近いサンプルを PC 内から探す (キーワードで絞り込みも可)
dtm-agent match ~/Music/ref.wav --segment 1:05-1:21 --library ~/Samples --query "pad"
```

### 2. エージェントに任せる

```bash
dtm-agent run "1:05-1:21 の雰囲気に合うパッドとキックを探して、同じキーで 8 小節のメロディとコードを作って" \
  --url https://open.spotify.com/track/xxxx \
  --audio ~/Music/ref.wav --segment 1:05-1:21 \
  --library ~/Samples \
  --daw reaper --out ./out
```

- `--daw file`    : `out/midi/*.mid` と `out/samples/` を書き出す (既定)
- `--daw reaper`  : `out/<title>.rpp` を生成。REAPER で開く
- `--daw ableton` : Live + AbletonOSC (ポート 11000) に MIDI トラックを直接作る

Python から:

```python
from dtm_agent.agent import Session, run_agent

session = Session(out_dir="./out", library_dir="~/Samples", daw="reaper")
print(run_agent("この曲みたいなローファイを 8 小節", session))
```

## 重要な制約 (正直に)

- **Spotify から音声は取れません。** 規約上ダウンロード不可で、2024 年 11 月に audio-features / 30 秒プレビュー API も新規アプリ向けに廃止されました。URL からはメタデータのみ取得し、解析には手元の音声ファイル (購入した音源、自分の録音など) を使います。
- **DAW への "完全自動実装" は DAW ごとに到達点が違います。** REAPER はプロジェクトファイルがテキストなので最も深く自動化できます。Ableton は MIDI は API で置けますがオーディオファイルの読み込みは公開されていません。Logic / FL / Cubase は MIDI 書き出し経由になります。
- **音色 (シンセのプリセット) は自動では決まりません。** `instrument_hint` と申し送り (`notes_for_user`) として残し、人が選ぶ前提です。

詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) と [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。

## 開発

```bash
python -m pytest -q
```

テストは合成音声 (numpy で生成した A minor アルペジオ + クリック) を使うので、外部 API もサンプル素材も不要です。

## その他: Claude Code Mod

- [`claude-mods/jev-model-router/`](claude-mods/jev-model-router/) — Claude Code へのリクエストごとに TypeSafe の Jev でサブエージェントのモデル・メインのモデル (セッション開始時のみ)・effort を自動選択する Claude Code Mod (function hooks プラグイン)。詳細はそのディレクトリの README を参照。
