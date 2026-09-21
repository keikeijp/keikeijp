# computer-use — typesafe-computer-use の再実装 (クロスプラットフォーム)

## 目的

画面上の UI 要素一覧 `{id, kind, text, bbox}` を state にして、**Jev が action (click / double_click / type / key / scroll / wait / done) と対象要素 id を 1 回で決める** PC 操作エージェント。
元ネタは macOS のアクセシビリティ木専用だったので、読み取りと実行を Protocol で分離して他 OS・オフラインでも動くようにした。

元ネタ: `awlevin/typesafe-computer-use`

## 構成

| 役割 | 実装 | 依存 |
|---|---|---|
| `ScreenReader` | `StaticReader` — JSON ファイル / リスト (`{frames: [...]}` で画面遷移も再現) | なし |
| | `MacAccessibilityReader` — `osascript` で最前面ウィンドウの UI 要素 (macOS のみ) | macOS + アクセシビリティ権限 |
| | `OcrReader` — `pyautogui.screenshot()` + `pytesseract` で行単位の文字要素 | pyautogui, pytesseract, tesseract |
| `Executor` | `DryRunExecutor` — 記録のみ (**既定**) | なし |
| | `PyAutoGuiExecutor` — 実クリック/入力 (FAILSAFE 有効) | pyautogui |

`Agent.run(task, max_steps)` は毎ステップ画面を読み直し、`choose()` で action+target を決め、`argument_for()` が type/key の引数を task の引用符・"press cmd+s" から抜く。confidence が `--min-confidence` (既定 0.3) 未満の手は実行せず、連続 3 回で停止。

## 使い方

```bash
# オフライン (既定 dry-run)。画面 JSON は [{id,kind,text,bbox:[x,y,w,h]}] か {frames:[[...],[...]]}
jevlab computer-use --task "Press the OK button" --screen-json screen.json --backend mock
jevlab computer-use --task "Open the Downloads folder" --json         # 内蔵デモ画面

# 実機 (macOS)。--execute を付けたときだけ pyautogui で操作する
jevlab computer-use --task "Open the Downloads folder" --reader mac --execute
jevlab computer-use --task "..." --reader ocr --execute               # 他 OS: OCR
```

## 追加依存

- 実操作: `pip install pyautogui`
- OCR 読み取り: `pip install pyautogui pytesseract pillow` + tesseract 本体
- macOS 読み取り: システム設定 → プライバシー → アクセシビリティでターミナルを許可

## 課金・外部送信の注意

- Jev には task・要素の短い説明 (kind/text/座標)・直近履歴のみ送る。スクリーンショット画像は送らない
- OCR は端末内で完結 (tesseract)。ネットワークには出ない
- `--execute` はマウス・キーボードを実際に動かす。誤動作時はマウスを画面左上隅へ (pyautogui FAILSAFE)

## 制限

- `type` の文字列は task 内の引用符から取る単純ルール。複雑な入力は task を分けて書く
- OCR 要素は「文字の行」なので、アイコンだけのボタンは対象にできない
- Windows/Linux のアクセシビリティ木 (UIA / AT-SPI) は未実装。`--screen-json` か OCR を使う
