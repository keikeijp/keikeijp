"""ローカル / 公開モデルで Jev を再現する研究モジュール群 (27〜30)。

- semif   : 公開モデルの logit から選択肢確率を 1 回の forward で読む (LocalLogitBackend)
- jevlike : 可変長選択肢集合を一度に評価する小型モデルの学習スキャフォールド
- nanojev : 複数質問を 1 パスで答える並列判断 + ゲーム制御デモ
- server  : TypeSafe / OpenRouter 互換のローカル HTTP API サーバ

どのモジュールも import 時に torch / transformers を要求しない (関数内で遅延 import)。
"""
