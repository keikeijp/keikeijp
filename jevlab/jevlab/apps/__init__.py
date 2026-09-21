"""アプリのレジストリ。`jevlab <name> ...` で各アプリの `main(argv)` を呼ぶ。

各アプリは `jevlab.apps.<module>` に `main(argv: list[str]) -> int` を持つ。
ここでは import を遅延させ、重い依存 (playwright 等) を持つアプリが他を壊さないようにする。
"""

from __future__ import annotations

APPS: dict[str, tuple[str, str]] = {
    # name: (module, 説明)
    "ultrafast": ("jevlab.apps.ultrafast", "1. jev-ultrafast: Jev が操作と対象要素を選ぶ高速ブラウザエージェント"),
    "computer-use": ("jevlab.apps.computer_use", "2. typesafe-computer-use: 画面 OCR/アクセシビリティ木から操作を決める PC 操作"),
    "mobile": ("jevlab.apps.mobile", "3. mobile-jev: adb 接続の Android 端末を Jev が操作"),
    "voice-browser": ("jevlab.apps.voice_browser", "4. jev-voice-browser: 音声 → 意図抽出 → Playwright 操作"),
    "compaction": ("jevlab.apps.compaction", "5. fast-jev-compaction: Claude Code 履歴の不要ツール呼び出しを Jev で削る"),
    "foreman": ("jevlab.apps.foreman", "6. foreman: 自律コーディングエージェントの監督 (継続/検証/停止)"),
    "review": ("jevlab.apps.review", "7. jev-review: git 差分の段階的レビュー → HTML レポート"),
    "router": ("jevlab.apps.router", "8. jev-router: ターン毎にモデルを振り分ける OpenAI 互換プロキシ"),
    "rules": ("jevlab.apps.rules", "9. jev-rules: 依頼内容と編集対象に応じて Claude Code のルールを選ぶ"),
    "skillbox": ("jevlab.apps.skillbox", "10. skillbox: スキル管理 + MCP 配信 + Jev によるスキル推薦"),
    "mcp": ("jevlab.apps.mcp_server", "11. typesafe-mcp: choice/score/noul を MCP ツールとして公開"),
    "shell-history": ("jevlab.apps.shell_history", "12. jev-shell-history: 入力途中に合う過去コマンドを Jev が選ぶ zsh 補完"),
    "pg": ("jevlab.apps.pg", "13. pg-jev: PostgreSQL 行を自然言語条件で絞り込み/分類/順位付け"),
    "search": ("jevlab.apps.search", "14. jev-search: 検索語選定・期間指定・結果の関連度ソート"),
    "graph": ("jevlab.apps.graph", "15. neo4jev: グラフ探索で次にたどる関係を Jev に選ばせる"),
    "unclutter": ("jevlab.apps.unclutter", "16. unclutter: ページ内の広告/ポップアップを Jev で識別して非表示"),
    "meter": ("jevlab.apps.meter", "17. jevmeter: 動画内の発言を観点別に採点してメーター字幕を作る"),
    "sponsor-skip": ("jevlab.apps.sponsor_skip", "18. youtube-sponsor-detection: 字幕からスポンサー区間を判定"),
    "warden": ("jevlab.apps.warden", "19. pi-warden: エージェントのルール違反/同じ失敗/未検証完了を監視"),
    "moderation": ("jevlab.apps.moderation", "20. Jev-Moderation-Bot: Discord のスパム/詐欺 URL 判定と処分"),
    "geo": ("jevlab.apps.geo", "21. notra: AI 回答でのブランド言及を追跡する GEO ツール"),
    "home": ("jevlab.apps.home", "22. HA-Jev: Home Assistant の家の状態を Jev で判定して通知"),
    "mario": ("jevlab.apps.mario", "23. typesafe-mario: ゲーム状態から Jev が行動を選ぶ制御実験"),
    "pilot": ("jevlab.apps.pilot", "24. jevpilot: 走行シミュレータで進路/速度候補から Jev が選ぶ"),
    "drone": ("jevlab.apps.drone", "25. jev-drone: ドローン障害物回避の判断を Jev に委ねる"),
    "trader": ("jevlab.apps.trader", "26. jev-trader: 売買判断の実験 (mock / dry-run)"),
    "semif": ("jevlab.local.semif", "27. SemIf: 公開モデルの logit から選択肢確率を直接読む"),
    "jevlike": ("jevlab.local.jevlike", "28. jevlike: 可変長選択肢を一度に評価する小型モデル学習"),
    "nanojev": ("jevlab.local.nanojev", "29. NanoJev: 並列判断ヘッド付き小型モデル + ゲーム制御デモ"),
    "serve": ("jevlab.local.server", "30. openjev-sglang: TypeSafe/OpenRouter 互換のローカル API サーバ"),
    "scenejudge": ("jevlab.scenejudge.cli", "★ SceneJudge: SAM 3.1 x Jev。画像/動画 → シーングラフ → 型付き判断"),
}
