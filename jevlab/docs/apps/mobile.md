# mobile — mobil-jev の再実装 (adb で Android を操作)

## 目的

`adb shell uiautomator dump` の XML 階層を要素リストにし、**Jev が action (tap / type / swipe_up / swipe_down / back / home / wait / done / give_up) と対象要素を 1 回で決めて** `adb shell input` で実行する Android 操作エージェント。

元ネタ: `droidrun/mobil-jev`

## 構成

- `parse_hierarchy(xml)` — `xml.etree` で `<node>` を走査し `{index, class, text, content_desc, resource_id, bounds, clickable, editable, checked}` に。既定では clickable / EditText / scrollable / checkable の要素だけ残し、`enabled="false"` は落とす (行動空間を小さく保つ)
- `AdbDevice(serial)` — `uiautomator dump` → `cat`、`input tap/swipe/text/keyevent`、`wm size`。`runner` を差し替えれば adb 無しでテスト可
- `FakeDevice(xml | {name: xml}, on={screen: {"tap:2": next}})` — XML 文字列/ファイルで動くオフライン端末。tap 座標から要素を逆引きして遷移
- `Agent` — 毎ステップ階層を取り直し `choose()`。`type` の文字列は task の引用符から。confidence 閾値未満は実行せず、連続 3 回で停止
- `--log run.jsonl` — 1 ステップ 1 行の JSONL 実行ログ
- `--web PORT` — `http.server` で `agent.status` を JSON 返却する状態ページ (フラグ指定時のみ起動)

## 使い方

```bash
# オフライン (既定 dry-run)。実機で `adb shell uiautomator dump && adb pull /sdcard/window_dump.xml` した XML を渡す
jevlab mobile --task "Turn on Wi-Fi" --hierarchy-xml dump.xml --backend mock --log run.jsonl
jevlab mobile --task "Turn on Wi-Fi"                      # 引数なしなら内蔵サンプル画面で dry-run

# 実機。--execute を付けたときだけ input を送る
adb devices
jevlab mobile --task "Open settings and turn on Wi-Fi" --serial emulator-5554 --execute --web 8765 --log run.jsonl
```

## 追加依存

- Android SDK platform-tools (`adb`) が PATH にあること。Python の追加パッケージは不要
- 端末側: USB デバッグ有効化

## 課金・外部送信の注意

- Jev には task・要素の短い説明 (class / text / resource-id)・直近履歴のみ送る。画面キャプチャは送らない
- `--execute` はタップ・入力を実際に行う。決済アプリや個人情報の画面では使わない
- `--web` は `127.0.0.1` にのみバインドする

## 制限

- `input text` は ASCII 前提 (adb の制約)。日本語入力は IME 経由が必要で未対応
- WebView 内の要素は uiautomator の dump に出ないことがある
- スワイプは画面中央の縦方向のみ
