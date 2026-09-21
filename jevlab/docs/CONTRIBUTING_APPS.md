# jevlab アプリ実装規約 (サブエージェント向けブリーフ)

リポジトリ: `/home/user/keikeijp/jevlab` (Python 3.10+、依存ゼロのコアの上にアプリを載せる)。

## コア API (変更禁止。読むだけ)

```python
from jevlab.core import Jev, Choice, Score, Noul, Decision, MockBackend, ScriptedBackend, FunctionBackend

jev = Jev()                 # 環境変数で TypeSafe / OpenRouter / mock を自動選択。Jev("mock") / Jev(MockBackend()) も可
jev = Jev(backend, cache=True)   # 同じ (state, questions) を LRU キャッシュ

d = jev.decide(state, {                     # state は str / dict / list (JSON にできるもの)
    "intent": Choice({"refund": "返金要求", "bug": "不具合", "other": None}, instructions="主な要求は?"),
    "urgency": Score(["急がない", "今週中", "今日中"], "緊急度は?"),   # 順序付きルーブリック (level 0..n-1)
    "angry": Noul("怒っているか?"),                                    # yes 確率
})
d.choice("intent").choice / .probabilities / .confidence / .ranked() / .margin()
d.score("urgency").score (期待値 float) / .level (最頻レベル int) / .legend / .probabilities / .normalized()
d.noul("angry").noul (0..1) / .yes
d.latency_ms, d.model, d.backend, d.to_dict()

# 便利メソッド
jev.choose(state, ["a","b"] or {"a": "説明", ...}, instructions) -> ChoiceAnswer
jev.score(state, rubric_list, instructions) -> ScoreAnswer
jev.judge(state, instructions) -> NoulAnswer
jev.judge_many(states, instructions) -> list[NoulAnswer]     # 並列
jev.decide_many([(state, questions), ...]) -> list[Decision] # 並列
jev.rank(candidates, instructions, rubric=None, context=None) -> [(candidate, score), ...] 降順
jev.filter(candidates, instructions, threshold=0.5, context=None) -> list
jev.classify(state, categories, instructions, min_confidence=0.0) -> label | None
jev.stats()
```

テスト用バックエンド:

- `MockBackend(hints={"質問名": 回答})` : 語彙重なりのヒューリスティック。hints で固定回答
- `ScriptedBackend([{"質問名": "label" | level_int | bool | float}, ...])` : 呼び出し順に返す。`.calls` に記録
- `FunctionBackend(lambda state, questions_wire: {"質問名": 簡略回答})` : ルールで返す

SAM 3.1 (`jevlab.sam`) は SceneJudge 以外では使わない。

## アプリの置き場所と形

- 1 アプリ = `jevlab/apps/<module>.py` (大きければ `jevlab/apps/<module>/__init__.py` + サブモジュール)
- モジュール名は `jevlab/apps/__init__.py` の `APPS` に書いてある名前に**必ず**合わせる
- 必ず `def main(argv: list[str]) -> int` を持ち、argparse で `--backend` (Jev のバックエンド名) を受ける
- モジュールの import は追加依存なしで成功すること。playwright / psycopg / discord.py / torch などは**関数内で遅延 import** し、
  無ければ分かりやすいエラーメッセージ (何を pip install すればよいか) を出す
- 外部 API・ネットワーク・実機が要る部分は、必ず**オフラインで動く代替経路** (ファイル入力、内蔵シミュレータ、`--dry-run`) を用意する
- Jev に渡す state は「判断に必要な最小限の構造化 JSON」にする。生 HTML や巨大ログをそのまま渡さない (32k トークン制限)
- 判断の閾値 (confidence / probability) を設け、閾値未満は「保留・人間へ・大きい LLM へ」に落とすパターンを守る
- 副作用のある操作 (削除、送信、取引、クリック) は `dry_run` 既定 True か、明示フラグでのみ実行
- 秘密情報 (API キー、Cookie、フル URL) を state に含めない

## テスト

- `tests/test_<module>.py` に pytest。ネットワーク・実機・GPU なしで 1 秒以内に通ること
- Jev は `MockBackend` / `ScriptedBackend` / `FunctionBackend` を使う
- 少なくとも: 主要ロジック 2〜3 ケース + `main([...])` を dry-run で 1 回通すケース

## ドキュメント

- `docs/apps/<module>.md` に日本語で: 目的 / 元ネタの GitHub リポジトリ名 / 使い方 (コマンド例) / 必要な追加依存 / 課金・外部送信の注意 / 制限
- コードのコメントは日本語でよい。識別子は英語

## やらないこと

- `jevlab/core`, `jevlab/sam`, `jevlab/cli.py`, `pyproject.toml`, 他グループのアプリを編集しない
- git commit / push をしない (統合担当がまとめて行う)
- 実 API キーを使わない
