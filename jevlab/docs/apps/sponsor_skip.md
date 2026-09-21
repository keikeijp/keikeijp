# 18. sponsor-skip — 字幕からスポンサー区間を判定して自動スキップ

## 目的

YouTube 等の字幕 (書き起こし) を約 20 秒のチャンクに切り、各チャンクが「本編 / スポンサー / 自己宣伝 / イントロ /
アウトロ / 高評価のお願い」のどれかを Jev に判定させ、隣接する陽性チャンクを 1 区間にまとめて
SponsorBlock 互換の JSON (`[{segment:[start,end], category}]`) を出す。ユーザースクリプトがローカルサーバから
区間を取得し、`video.currentTime` を進めてスキップする。

元ネタ: `trungdq88/youtube-sponsor-detection`

## 仕組み

1. ウィンドウ化: 20 秒幅、50% 重なり (10 秒ステップ)。ウィンドウに重なる字幕行を連結 (600 文字上限)。
2. チャンク毎に `decide_many` で
   - `segment_kind` Choice `{content, sponsor, self_promotion, intro, outro, interaction_reminder}`
   - `is_paid_promotion` Noul
3. マージ (ヒステリシス): `1 - P(content)` が **0.6 以上で区間開始**、**0.4 以上なら継続**、それ未満で終了。
   区間のカテゴリは陽性ウィンドウ中の多数決。3 秒未満の区間は捨てる。
4. カテゴリ対応: sponsor→`sponsor`, self_promotion→`selfpromo`, intro→`intro`, outro→`outro`, interaction_reminder→`interaction`

## 使い方

```bash
# オフライン (SRT / youtube-transcript-api 形式 JSON)
jevlab sponsor-skip --backend mock detect --srt video.srt --out segments.json
jevlab sponsor-skip detect --json transcript.json

# YouTube から字幕取得 (要 youtube-transcript-api)。--store-dir で serve 用に保存
jevlab sponsor-skip detect --video-id dQw4w9WgXcQ --store-dir sponsor_segments

# ユーザースクリプトを書き出し → Tampermonkey に登録
jevlab sponsor-skip --emit-userscript sponsor_skip.user.js

# ユーザースクリプトが読むローカルサーバ (GET /segments?videoID=..., POST /segments)
jevlab sponsor-skip serve --port 8766 --store-dir sponsor_segments
```

## 追加依存

- `--video-id` のみ `pip install youtube-transcript-api`
- それ以外は標準ライブラリのみ

## 課金・外部送信の注意

- 10 分の動画で約 60 ウィンドウ → 60 回の Jev 呼び出し (並列)。`--window` / `--overlap` で調整可能。
- Jev に送るのは字幕テキストと開始秒、動画 ID だけ。
- `serve` は 127.0.0.1 にのみバインドし、認証はない。ローカル利用専用。

## 制限

- 字幕の品質 (自動生成字幕の誤り) がそのまま判定に影響する。
- スポンサー区間の境界はウィンドウ幅 (既定 20 秒) の粒度でしか決まらない。ユーザースクリプトは区間終端の 0.5 秒手前までをスキップ対象にする。
- 既定でスキップするカテゴリは `sponsor`, `selfpromo`, `interaction` (スクリプト内の `CATEGORIES` で変更)。
