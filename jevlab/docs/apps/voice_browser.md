# voice-browser — jev-voice-browser の再実装

## 目的

音声 (またはテキスト) の発話を **Jev で意図 (navigate / search / click_link / scroll / read_aloud / go_back / stop) と対象要素に変換**し、Playwright で実行する。URL や検索語などのスロットは正規表現で埋めるので、生成 LLM は不要。

元ネタ: `moritzkremb/jev-voice-browser`

## パイプライン

1. `transcribe(source)` — `mic` またはファイル → SpeechRecognition (Google Web Speech) → 無ければ openai-whisper (遅延 import)。`--text` ならこの段階を飛ばす
2. `extract_intent(jev, utterance, snapshot)` — state = `{utterance, page_title, elements[{i,d}]}` に `intent` (Choice) と `target` (Choice: 要素番号、click_link 時のみ使用) を同時に聞く。`fill_slots()` が URL (`open example.com` → `https://example.com`)、検索語 (`search for X on Y` / `Xを検索`)、スクロール方向を抽出
3. `execute(page, intent, elements)` — `jevlab.apps.ultrafast` の Page 抽象 (Playwright / `FakePage`) で goto / fill+Enter / click / wheel / go_back。`read_aloud` は `speak` コールバック (既定は標準出力)
4. `Session.run(utterances)` — 発話を順に処理。confidence が閾値未満なら実行せず聞き返す。`stop` で終了

## 使い方

```bash
# テキスト入力 (実ブラウザ、playwright が必要)
jevlab voice-browser --text "search for jev typesafe" --text "open the first result" --url https://duckduckgo.com --headed

# マイク / 音声ファイル
jevlab voice-browser --mic --url https://duckduckgo.com
jevlab voice-browser --audio command.wav --url https://example.com

# オフライン: 意図を表示するだけ。playwright が無ければ内蔵 FakePage
jevlab voice-browser --text "search for jev" --text "stop" --dry-run --backend mock --json
```

## 追加依存

- ブラウザ: `pip install 'jevlab[browser]'` + `playwright install chromium`
- 音声: `pip install 'jevlab[voice]'` (SpeechRecognition) + `pyaudio` (マイク)。代替: `pip install openai-whisper` (ファイルのみ)
- 読み上げは標準出力。音声合成が欲しければ `Session(speak=...)` に `pyttsx3` 等を渡す

## 課金・外部送信の注意

- SpeechRecognition の既定 (`recognize_google`) は音声を Google に送る。ローカル完結にしたければ whisper を使う
- Jev には発話テキスト・ページタイトル・要素の短い説明のみ送る。URL は送らない
- `--dry-run` 以外ではブラウザを実際に操作する

## 制限

- `navigate` のサイト名推定は「最後の単語 + .com」の単純ルール
- `search` はページ内の検索欄 (input[type=search|text] / placeholder に search) を先頭 1 つだけ使う
- `read_aloud` は要素テキストの連結 (300 字) で、本文の要約はしない
