# 17. meter — 動画内の発言を観点別に採点してメーター字幕を作る

## 目的

動画の書き起こし (SRT / WebVTT / JSON) を発話単位に分け、ユーザーが選んだ観点で Jev の Score により採点し、
「今この人はどれくらい言い切っているか」「どれくらい売り込みか」を示すメーター (▰▰▰▱▱) のオーバーレイ字幕を作る。

元ネタ: `ChetasLua/jevmeter`

## これはファクトチェックではない

採点するのは **話し方の観点** (断定の強さ、感情の強さ、セールストーク度) であって、発言の真偽ではない。
「絶対的な断言」レベルが高くても、その主張が正しいか間違っているかは一切判定していない。
真偽の検証に使わないこと。

## 既定の観点 (`--axes` で選択)

| 観点 | level 0 | … | level 4 |
| --- | --- | --- | --- |
| `confidence_of_claim` | 強くぼかしている | | 絶対的・例外なしの断言 |
| `emotional_intensity` | 淡々としている | | 激しく興奮/怒り/歓喜 |
| `salesmanship` | 売り込み要素なし | | 煽り・限定・今すぐ買え |

独自ルーブリックは `--axes-json rubric.json` (`{"名前": ["level0", "level1", ...]}`) で渡せる。

## 使い方

```bash
jevlab meter --transcript talk.srt --out-dir meter_out
jevlab meter --transcript talk.vtt --axes confidence_of_claim,salesmanship --backend mock
jevlab meter --transcript talk.srt --video talk.mp4 --ffmpeg-cmd   # 焼き込み用 ffmpeg コマンドを表示
```

出力 (`--out-dir`):

- `timeline.json` : 発話ごとの {start, end, text, scores{axis: {score, level, normalized, confidence}}}
- `meter.ass`     : 画面左上に観点名 + 色付きバー (緑→黄→赤) を表示する ASS 字幕
- `sparkline_<axis>.svg` : 観点ごとの推移
- `--ffmpeg-cmd`  : `ffmpeg -i video -vf "ass=meter.ass" -c:a copy output_meter.mp4`

## 追加依存

なし (ffmpeg は焼き込み時のみ、外部コマンド)。

## 課金・外部送信の注意

- 発話 1 つにつき Jev 1 回 (全観点を同時に質問)。`--max-utterances` (既定 400) で上限。
- Jev に送るのは発話テキストと直前 1 発話 (120 文字以内) のみ。

## 制限

- 発話分割は「文末記号」「0.8 秒以上の間」「220 文字/15 秒」の単純ルール。
- ASS の描画は再生プレイヤー (mpv / VLC / ffmpeg libass) に依存する。
