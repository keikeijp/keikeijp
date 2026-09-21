# ★ SceneJudge — SAM 3.1 × Jev (CV × Jev のフラッグシップ)

「画像を見て、型付きの判断を返す」パイプライン。

```
画像 / 動画フレーム
   │  SAM 3.1 (Meta Model API, テキストプロンプトで物体検出 + マスク)
   ▼
物体リスト (box / mask 面積 / ラベル)
   │  jevlab.sam.scene: 正規化座標・面積比・位置・ゾーン所属・空間関係 (inside / overlaps / near / above ...)
   ▼
シーングラフ JSON  ← Jev はテキスト専用なので、画像を「構造化 state」に落とす
   │  Jev (choice / score / noul を 1 回の呼び出しでまとめて)
   ▼
型付きの判断  → ルール (閾値) → 通知 / ログ
```

SAM は「何がどこにあるか」だけを担当し、「それで何をすべきか」は Jev が 70〜500ms で決める。
生成 LLM を一切呼ばないので、カメラのフレーム毎に回してもコストが小さい (Jev は入力 100 万トークンで $0.042)。

## 使い方

```bash
export META_API_KEY=...        # SAM 3.1 (無ければ SAM_BACKEND=mock)
export TYPESAFE_API_KEY=...    # または OPENROUTER_API_KEY (無ければ JEV_BACKEND=mock)
pip install "jevlab[sam]"      # meta-sam-parser (マスク復号) + pillow (オーバーレイ)

jevlab scenejudge demo                                     # API 無しで一通り動く
jevlab scenejudge scenarios                                # 内蔵シナリオ一覧
jevlab scenejudge image desk.jpg -s desk --overlay out.png # 机の散らかり採点
jevlab scenejudge video clip.mp4 -s pet --fps 1 --report r.json   # ffmpeg でフレーム化 → ゾーン入退出イベント
jevlab scenejudge watch ./snapshots -s parking --notify https://hooks.example/...   # カメラ連携
```

## 内蔵シナリオ

| 名前 | SAM に探させる物 | Jev の判断 |
|---|---|---|
| desk | cup, bottle, paper, book, cable, laptop, phone, trash | tidiness (score 4 段階) / first_to_remove (choice) / needs_cleanup (noul) |
| pet | dog, cat, couch, bed, food bowl | pet_on_furniture (noul) / pet_location (choice) / activity_level (score) |
| parking | car, motorcycle, bicycle, person | free_spots (score) / recommended_spot (choice) / pedestrian_present (noul) |
| kitchen | pot, pan, knife, child, stove, ... | attention (score) / reason (choice) / child_present (noul) |
| shelf | bottle, box, can, empty shelf space | stock_level (score) / restock (noul) / dominant_item (choice) |

自分のシナリオは `jevlab scenejudge scenarios --export my.json` で雛形を出して編集する。
`zones` は比率座標 (0..1)、`rules` は `noul>=0.8` / `level>=2` / `choice==xxx` の簡易式。

## 実装メモ

- SAM 3.1 は `POST https://api.meta.ai/v1/responses` (Responses API 互換) に `model: sam-3.1`、`input_image` (data URL) + `input_text` (プロンプト) で呼ぶ。
  戻りの output_text は `<0f>id<|box;...|><|mask;...|>` 形式で、マスクは base85 のアリスメティック符号。復号は Meta 公式の `meta-sam-parser` を使う (無ければ box だけで動く)
- プロンプトごとに 1 リクエスト (並列 4)。SAM 3.1 の Object Multiplex により 1 プロンプトで最大 16 物体を追跡できる
- Jev には画像を渡せない (テキスト専用)。座標は 0..1 に正規化し、面積の大きい順に最大 40 物体だけ渡す
- 同じ scene state には LRU キャッシュが効くので、静止しているカメラでは Jev 課金が増えない

## 注意

- 画像は Meta のサーバへ、シーングラフ (物体名と座標) は TypeSafe/OpenRouter へ送られる
- `kitchen` などのシナリオは「気付きの補助」であり、安全設備の代替にはならない (HA-Jev と同じ方針)
- SAM のプロンプトは短い名詞句 1 つ。抽象語 ("mess") は検出できないので、具体物に分解して Jev 側で抽象化する
