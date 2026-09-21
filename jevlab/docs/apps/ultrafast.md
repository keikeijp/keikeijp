# ultrafast — jev-ultrafast の再実装

## 目的

Playwright で開いたページの対話要素を小さな「索引付き行動空間」にし、**Jev が操作 (click / type / scroll / go_back / done / give_up) と対象要素を 1 回の判断で決める**高速ブラウザエージェント。
生成 LLM が要るのは「入力欄に何を打つか」だけで、それも goal からのヒューリスティック抽出で済むことが多い。

元ネタ: `browser-use/jev-ultrafast`

## 仕組み

1. `snapshot(page)` — `page.evaluate(SNAPSHOT_JS)` 1 回で `a, button, input, select, textarea, [role=button] ...` を列挙し、各要素に `data-jev-index` を振って `{index, tag, role, text(<=60字), name, href_domain, visible}` を返す。フル URL は state に入れずドメインだけ残す
2. `choose(jev, goal, snapshot, history)` — state = `{goal, page{title,host}, elements[{i,d}], history(直近5)}` に対し `operation` (Choice) と `target` (Choice: 要素番号 → 1 行説明、可視要素優先で最大 60 件) を同時に聞く
3. `text_for_field()` — `type` のときだけ、goal から引用符・"search for X"・メール・日付を正規表現で抜く。`ANTHROPIC_API_KEY` があれば `AnthropicTextGenerator` (urllib で Messages API、既定モデル `claude-haiku-4-5`、`JEVLAB_TEXT_MODEL` で変更) を使い、失敗時はヒューリスティックへ戻る。`TextGenerator` プロトコルで差し替え可
4. `Agent.run(goal, start_url, max_steps)` — 履歴 `(operation, target, outcome)` を持ち、`done` / `give_up` で停止。confidence が閾値 (`--min-confidence`, 既定 0.3) 未満の手は実行せず、連続 3 回で `low_confidence` 停止
5. `FakePage` — Playwright なしで同じループを回すための極小 Page (evaluate / click / fill / press / goto / go_back / mouse.wheel / url / title)。`scenes = {url: {title, elements, on: {"click:2": next_url}}}` で遷移を書く

## 使い方

```bash
# 実ブラウザ (playwright が必要)
jevlab ultrafast --goal "Search for jev typesafe" --url https://duckduckgo.com --headed
jevlab ultrafast --goal "..." --url https://example.com --max-steps 20 --json

# 決定を表示するだけ。playwright が無ければ内蔵 FakePage (DEMO_SCENES) で動く
jevlab ultrafast --goal "Search for jev" --dry-run --backend mock
```

Python から:

```python
from jevlab.core import Jev
from jevlab.apps.ultrafast import Agent, FakePage, DEMO_SCENES
agent = Agent(Jev("mock"), FakePage(DEMO_SCENES))
result = agent.run("Search for jev typesafe", max_steps=5)
print(result.to_dict())
```

## 追加依存

- 実ブラウザ操作: `pip install 'jevlab[browser]'` + `playwright install chromium`
- 入力テキストの LLM 生成 (任意): `ANTHROPIC_API_KEY` (依存追加なし、urllib 直叩き)

## 課金・外部送信の注意

- Jev バックエンド (TypeSafe / OpenRouter) には goal・ページタイトル・ホスト名・要素の短い説明・直近履歴のみ送る。フル URL や入力済みの値は送らない
- `ANTHROPIC_API_KEY` を設定すると `type` 操作のたびに Anthropic API へ goal と対象欄の説明を送る (少量課金)。未設定ならネットワークに出ない
- `--dry-run` 以外ではクリック・入力を実際に行う。ログイン済みブラウザでは使わないこと

## 制限

- 対話要素の列挙は簡易セレクタベース。Shadow DOM / iframe 内は見ない
- `type` は fill + Enter 固定。複数フィールドのフォームは goal の書き方 (引用符) に依存する
- MockBackend では語彙一致で選ぶため、実運用の品質は再現しない
