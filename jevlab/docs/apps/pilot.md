# 24. pilot — 走行シミュレータで進路/速度候補から Jev が選ぶ

元ネタ: [standardagents/jevpilot](https://github.com/standardagents/jevpilot)

## 目的

自動運転風の 2D 運動学シミュレータで「車線維持 / 左右へ車線変更 × 減速 / 維持 / 加速」の軌道候補 (5〜9 本) を作り、
**衝突・逸脱する候補を決定的な安全フィルタで先に落としてから**、残りを Jev に選ばせる。
安全は Jev に委ねず、Jev は「安全な候補のうちどれが良いか」だけを判断する、という役割分担の実験。

## 構成

- `Track` : 中心線 (waypoints) + 車線幅 + 車線数 + 静止障害物 (車) + 制限速度。`project()` で Frenet 風 (s, d) に射影
- `generate_candidates()` : 各候補に {min_distance_to_obstacle, off_track_ratio, speed_delta, progress}
- `safety_filter()` : 障害物距離 < `safety_margin` または逸脱の候補を除去。全滅なら `emergency_candidate` (急ブレーキ) のみ
- `choose()` : `candidate` Choice (候補 id → メトリクスの説明) + `comfort` Score [smooth, moderate, harsh]
  - 確信度 < `min_confidence` → 最も余裕のある候補にフォールバック
  - comfort が harsh の車線変更 → 安全な車線維持候補に差し替え
- `write_html()` : ログ JSON を埋め込んだ自己完結の canvas 2D リプレイ (外部 CDN 不要、オフラインで開ける)

## 使い方

```bash
jevlab pilot drive --steps 300 --html replay.html --backend mock
jevlab pilot drive --steps 300 --seed 3 --obstacles 20 --safety-margin 1.5 --json > log.json
```

`replay.html` をブラウザで開くとスライダー/再生ボタンで軌跡 (緑)・選んだ候補 (黄)・障害物 (赤) を確認できる。

## 追加依存

なし (標準ライブラリのみ)。

## 課金・外部送信

Jev API を使う場合は 1 ステップ 1 リクエスト。state は ego の速度/車線、前方の障害物 4 件、候補メトリクスのみ。

## 制限

- 障害物は静止、運動学は簡略 (横移動は smoothstep、加速は 3 ステップで目標へ)
- 車線は 3 本固定 (`Track(lanes=...)` で変更可)。急カーブでの内輪差などは扱わない
