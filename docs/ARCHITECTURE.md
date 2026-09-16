# アーキテクチャ

## 設計方針

1. **LLM は「判断」、ツールは「事実と実体化」を担当する。**
   BPM やキーの推定、サンプルの類似度、ノートがスケール内かの検証、DAW への書き込みは
   すべて決定的なコードで行い、Claude はそれらの結果をもとに何を選び何を置くかを決める。
   LLM に音響解析や MIDI バイナリを生成させない。
2. **`ProjectPlan` を唯一の中間表現にする。**
   エージェントは設計図 (トラック / クリップ / ノート / サンプル) を組み立てるだけで、
   DAW ごとの違いは `DawBridge` 実装に閉じ込める。新しい DAW を足すときは bridge を 1 つ書く。
3. **サンプルソースはプラグイン。** `SampleSource` プロトコルを満たせばローカル / Freesound / Splice /
   自前 CLAP インデックスを同じ検索ツールから横断できる。
4. **オフラインで検証できる。** `index` / `analyze` / `match` は API キーなしで動き、テストも合成音声で完結する。

## モジュール

| パス | 役割 |
|---|---|
| `dtm_agent/models.py` | `AudioProfile`, `SampleHit`, `Note`, `MidiClip`, `AudioClip`, `Track`, `ProjectPlan` |
| `dtm_agent/analysis.py` | librosa による解析。テンポ (beat_track)、キー (Krumhansl-Schmuckler)、HPSS による tonal 比率、MFCC + スペクトル特徴の 20 次元ベクトル |
| `dtm_agent/reference.py` | Spotify URL の解決 (Client Credentials)。音声は取れないので metadata のみ |
| `dtm_agent/samples/local.py` | フォルダを走査してインデックス (JSON)。ライブラリ統計で標準化した cosine 類似 + パスのトークン一致 |
| `dtm_agent/samples/freesound.py` | Freesound text search API |
| `dtm_agent/music/theory.py` | キー解析、スケール構成音、ダイアトニックコード、スナップ |
| `dtm_agent/music/melody.py` | `Note[]` ↔ `.mid` (mido)、スケール検証 |
| `dtm_agent/daw/file_export.py` | 汎用書き出し |
| `dtm_agent/daw/reaper.py` | `.rpp` 生成 (`<SOURCE MIDI>` / `<SOURCE WAVE>`) |
| `dtm_agent/daw/ableton.py` | AbletonOSC (`/live/song/create_midi_track`, `/live/clip/add/notes` など) |
| `dtm_agent/agent/tools.py` | Claude に公開する 10 個のツール (`@beta_tool`) |
| `dtm_agent/agent/runner.py` | `client.beta.messages.tool_runner` によるループ |
| `dtm_agent/cli.py` | `index` / `analyze` / `match` / `run` |

## エージェントのツール

| ツール | 入力 → 出力 |
|---|---|
| `resolve_reference_url` | Spotify URL → 曲名 / アーティスト / 長さ |
| `analyze_reference_audio` | 音声パス + 区間 → BPM / キー / 質感指標 / chroma |
| `search_samples` | キーワード + 参照区間の有無 → ローカル / Freesound の候補 (path 付き) |
| `scale_info` | キー → スケール構成音とダイアトニックコードの MIDI 番号 |
| `set_project` | タイトル / BPM / キー |
| `add_midi_clip` | トラック名 + ノート列 → スケール検証。キー外なら `ok: false` で差し戻す |
| `add_audio_clip` | トラック名 + サンプルパス + 位置 |
| `add_note_for_user` | 音色やエフェクトなど人がやる作業の申し送り |
| `realize_in_daw` | 設計図を選択した bridge で実体化 |
| `show_plan` | 現在の設計図の確認 |

Claude API 側の設定: モデル `claude-opus-5`、adaptive thinking、effort `high`、
安全分類器による refusal 時のサーバサイドフォールバック (`fallbacks: "default"`) を既定で有効化
(`DTM_AGENT_FALLBACKS=0` で無効化)。モデルは `DTM_AGENT_MODEL` で差し替え可能。

## 類似検索の特徴ベクトル

`analysis.FEATURE_NAMES` の順に 20 次元:
MFCC 平均 13 + log スペクトル重心 / 帯域幅 / ロールオフ + ZCR + RMS + オンセット密度 + tonal 比率。
インデックス作成時にライブラリ全体の平均 / 標準偏差を求め、検索時はクエリも同じ統計で標準化して cosine 類似を取る。
テキスト → 音の検索 (「きらきらしたベル」など) を強くするには CLAP 埋め込みを `LocalLibrary(embedder=...)` に渡し、
`search(embedding=...)` で加算する (インターフェースは用意済み、モデルは未同梱)。

## DAW ブリッジの到達点

| DAW | MIDI | オーディオ | 方法 |
|---|---|---|---|
| REAPER | ✅ | ✅ | `.rpp` を生成 (テキスト) |
| Ableton Live | ✅ | ⚠️ 手動 | AbletonOSC。Live API にファイル読み込みが無いためサンプルはコピーのみ |
| その他 (Logic / FL / Cubase / Studio One) | ✅ | ⚠️ 手動 | `.mid` 書き出し + サンプルフォルダ |
