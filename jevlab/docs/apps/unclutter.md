# 16. unclutter — ページの広告/ポップアップを Jev で識別して非表示

## 目的

ページ内の広告・宣伝モーダル・ニュースレター登録・SNS 浮遊バー・Cookie 同意バナーを、
Jev の Choice 判定で識別し、**可逆的に** (拡張機能専用の `<style>` を消せば元に戻る) 隠す。

元ネタ: `kitze/unclutter`

## 仕組み

1. ブラウザ側で候補要素 (fixed/sticky/absolute、高い z-index、iframe、閉じるボタン付き) を最大 60 件集め、
   要素に `data-jevlab-unclutter="<idx>"` を付ける。Jev に送るのは **短い記述だけ**
   (`tag`, `id_hint`/`class_hint` (40 文字以内), `role`, `text` (80 文字以内), `position`, `size_ratio`, `z_index`,
   `has_close_button`, `iframe_ad_domain_hint`)。フル URL や生 HTML は送らない。
2. 候補ごとに `kind` Choice `{keep, ad, promotion, newsletter, social, cookie, uncertain}` を `decide_many` で並列判定。
3. **厳格な受け入れ**: 選ばれたラベルの probability ≥ 0.9 かつ confidence ≥ 0.9 のときだけ非表示。それ以外は `keep`
   (`uncertain` はどれだけ確信があっても隠さない)。
4. `[data-jevlab-unclutter="N"] { display: none !important; }` の属性セレクタ CSS を `<style id="jevlab-unclutter-style">` に入れる。
   ルールはホスト毎に `unclutter_rules/<host>.json` に保存 (id があれば `[id="..."]` セレクタで永続化)。

## 使い方

```bash
# オフライン: 候補 JSON を分類 (テストや拡張のデバッグ用)
jevlab unclutter --backend mock classify --json candidates.json --host example.com

# Playwright で実ページを処理 (要 playwright)
jevlab unclutter run --url https://example.com --headless
jevlab unclutter run --url https://example.com --dry-run   # 判定だけ、注入・保存しない

# ブラウザ拡張用ローカルサーバ (POST /classify, GET /rules/<host>)
jevlab unclutter serve --port 8765
```

ブラウザ拡張 (`jevlab/apps/unclutter_extension/`, Manifest V3) は Chrome の「パッケージ化されていない拡張機能を読み込む」で
読み込む。`content.js` が候補を集め、`background.js` が `http://127.0.0.1:8765/classify` に POST、返ってきた CSS を適用する。
ツールバーのアイコンで適用/解除をトグルできる。

## 追加依存

- `run` のみ `pip install playwright && playwright install chromium`
- `serve` / `classify` は標準ライブラリだけで動く

## 課金・外部送信の注意

- Jev へ送るのは要素の短い記述のみ。ページ URL、Cookie、HTML は送らない。
- 候補は 1 ページあたり最大 60 件 → 最大 60 回の Jev 呼び出し (並列)。

## 制限

- 候補収集は position/z-index/iframe ベースのヒューリスティックなので、インライン広告 (static 配置) は候補に上がらないことがある。
- 閾値が厳しいため、mock バックエンドではほとんど何も隠れない (安全側)。
- 非表示は CSS のみ。スクロールロック (`body { overflow:hidden }`) の解除などは行わない。
