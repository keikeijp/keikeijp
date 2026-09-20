# Jev (TypeSafe) 導入の経済合理性と実装案 — DeepSeek V4.1 Flash 前提での dtm-agent 評価

調査日: 2026-09-20 / 対象: `keikeijp/keikeijp` (dtm-agent v0.1) / 再計算: `python scripts/jev_cost_model.py`

## 結論 (TL;DR)

**「Jev を今すぐ入れてコストを下げる」は成立しない。** 理由は 3 つ。

1. **この repo は DeepSeek をまだ使っていない。** LLM 呼び出しは `dtm_agent/agent/runner.py` の 1 箇所だけで、`claude-opus-5` + adaptive thinking + effort high + tool_runner (最大 40 反復)。1 run ≈ **$1.6** (推定、cache なし)。DeepSeek V4.1 Flash なら同じ run が **$0.007〜0.03**。50〜200 倍の差はモデル選択と cache であって Jev ではない。
2. **エージェントループ内に Jev を挟む (Case C) は損。** ループの各ターンは「引数付きの tool_use を生成する」仕事で、Jev は引数を書けない。DeepSeek call を 1 つも消せず、ターンあたり +4〜10% (オフピーク時は +8〜19%) のコストと +0.3〜0.6 秒 (直列 3 問なら実測 +4.6 秒) の遅延だけが乗る。**DeepSeek の cache hit 率が上がるほど Jev の相対コストは上がる** (hit 0% で 4.1% → hit 90% で 9.6%)。
3. **Jev なしで削れる分の方が大きい。** 決定的プレステップ (解析・URL 解決・scale_info を LLM の前にコードで実行) で 13 ターン → 9〜10 ターン、`search_samples` の返却を絞れば履歴が 1,500 tok/ターン軽くなる。これで DeepSeek 単独コストが 25〜35% 下がる。この最適化後を baseline にすると、Jev の余地はさらに小さい。

Jev が **理屈の上で** 得になる箇所は 2 つ + 将来 1 つ。いずれも **shadow mode で日本語精度を実測してから** に限る。

| # | 箇所 | Case | 期待効果 | 判断 |
|---|---|---|---|---|
| J1 | `dtm-agent run` 入口ゲート: 「解析だけ / 類似検索だけ」の依頼を LLM を呼ばずに既存の API 不要コマンドで完結 | **A** | 該当リクエスト 1 件につき DeepSeek run 丸ごと ($0.007〜0.03) を回避。Jev 代 $0.00003 | shadow で「該当比率」を測る。CLI はユーザーが subcommand を自分で選ぶので比率は小さい可能性が高い。規則ベース (正規表現) を先に置く |
| J2 | `search_samples` の 16 件を Jev Score で 4〜6 件に絞る | **B** | +$0.0004〜0.004 / run。金額は誤差、価値は品質と入力削減 (ROADMAP v1.0「安価モデルへの一部委譲」) | shadow で「LLM が選んだサンプルが Jev 上位 6 に入る率」を測る |
| J3 | (v1.0) 会話継続の差分編集「ベースを 8 分に」「半音下げて」を意図分類 → 決定的変換 | **A** | LLM 0 call | コードが無いので設計メモのみ |

**Jev 不要と断定する箇所**: 毎ターンのツール選択 (C)、ループ終了判定 (C)、メロディの数値評価 (Jev の公式 weakness)、スケール検証 (既に決定的)、thinking on/off 判定 (規則で足りる)、refusal 処理。詳細は §5。

---

## 0. 前提の訂正: このリポジトリの実態

| 項目 | 実態 (2026-09-20, `main`) |
|---|---|
| LLM 呼び出し箇所 | `dtm_agent/agent/runner.py:28` の `client.beta.messages.tool_runner(...)` のみ |
| モデル | `claude-opus-5` (`DTM_AGENT_MODEL` で差し替え可) |
| 設定 | `thinking={"type":"adaptive"}`, `output_config={"effort":"high"}`, `max_tokens=16000`, `max_iterations=40`, `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` |
| ツール | 10 個 (`tools.py`)。schema は毎 run 同じ順序・同じ内容 (prefix 安定) |
| system prompt | 固定文字列 (`prompt.py`)。可変値なし (prefix 安定) |
| user message | CLI が `参照曲 URL: …` / `参照音声ファイル: …` を先頭に付けてから依頼文 |
| cache | `cache_control` なし。usage の記録なし |
| user_id / metadata | なし |
| token / cost accounting | なし (`on_message` はツール名を stderr に出すだけ) |
| retry / fallback | SDK 既定の再試行 + Anthropic のサーバ側 refusal fallback |
| DeepSeek | **未使用** |

`keikeijp/emo` は README と動画のみ (Alibaba EMO のミラー) で LLM 呼び出しがないため対象外。

以下は「DeepSeek V4.1 Flash に移行した後」を主 baseline とし、現行 Opus 5 を参考値として併記する。

## 1. Jev 実利用情報 (2026-09-15 公開 → 09-20 までの 5 日間)

### 公式スペック (TypeSafe docs / OpenRouter / genai-prices)

- モデル: `jev-1.13.0` (`jev-latest` / `jev-preview` は別名)。2026-09-15 early access 公開。
- 価格: **$0.042 / 1M input tokens、output $0**。課金対象は state + questions の入力のみ。
- 質問型: **Noul** (真偽の確率)、**Choice** (選択肢ごとの確率 + confidence)、**Score** (順序尺度 2〜10 段階 + 分布 + confidence)。1 リクエストで最大 32 問を並列評価。Choice の選択肢は gateway 上限 64。
- 上限: 合計 64k tok、**state + 最長の質問 ≤ 32k tok** (超えると HTTP 400: octalide/sift#10 で実際に発生)。
- rate limit: 1,200 rpm / 250k tok/s (予告なく変動)。
- 公称レイテンシ 70〜500 ms。
- 公式 "jaggedness" (弱点) 一覧: 数値・算術・日付比較、多段推論、無関係な情報による distractor、二重否定・間接指示、**字義通りの読解**、**英語が最良で CJK は「扱えるが同等ではない」**、**state 内の指示文に動かされる (prompt injection 耐性なし)**。
- TypeSafe 自身の eval: 4 ワークフロー平均で参照解答との一致 **67.8%**。invoice 処理は 61.8% (Sol 79.1%)。structured output エラー率 0%。CEO は HN で「basically a zero-shot classifier」に「exactly right」と回答。

### 独立した実測 (実際に API を叩いた報告のみ)

| 報告 (日付) | 内容 | 結果 |
|---|---|---|
| **BillionsBobby/JevRouter #2 (09-18)** — **Jev vs DeepSeek V4.1 Flash** | Toolathlon 10 タスクで「最初の 5 ツール呼び出し」を予測。Jev は直列 5 call、DeepSeek は schema 付き 1 call (reasoning) | 位置一致 Jev 38% / DeepSeek 24%、順不同カバー率 Jev 50% / DeepSeek 64%、**latency 1,580 ms vs 8,645 ms、コスト $0.00058 vs $0.00407 / task (≈7 倍)**。ただし「ツール名だけを当てる」タスクで引数生成を含まない。n=10 |
| wotai-dev/typesafe-jev-tools (09-18) | 150 行で Jev vs Claude Haiku 4.5 | 正答率 **66.0% vs 66.0%**、中央値 455 ms vs 631 ms、ECE 0.121 vs 0.122、「不確か」を返した割合 **34.7% vs 2.7%** (Jev は迷いを表に出す) |
| AnshChoudhary/typesafe-ai-firewall | エージェントの tool call を実行前に 5 問の Noul で判定、600 件 shadow | 良性ブロック 0.55%、合成攻撃検出 100%、**p50 375 ms / p95 595 ms**、$0.0000365 / call、ECE 0.156 (目標 <0.10 未達)。1 問に圧縮すると hard negative の 39.2% を誤ブロック、文脈を外すと検出 61.6% |
| buberlo/dsh-jev (09-19) — DeepSeek Harness 向け決定層 | ツール選択 / 実行前判定 / ルーティング | fixture 25/25 一致、平均 483 ms。**live Jev は 3 判定でターンあたり +4.6 秒** |
| seanperkins/omp-jev-watchdog (09-17) | 指示遵守・検証の正直さを shadow で判定、11 ケース | 10/11 一致、誤検出 1 件 (confidence 0.26)、中央値 173 ms。「validated safety gate ではない」 |
| **mtane0412/hanatane #50 (09-20, 日本語)** | 記事 82 本のタグ・アイコン・関係候補を Jev で判定 | 明確なトピックのタグは precision 1.00 / recall 0.91〜1.00 (閾値 0.8)、技術系は 0.89 / 0.78。アイコン 84%。関係候補 top-8 に 86%。**関係「種別」は 48% で常に多数派を返す baseline 以下 → 採用中止**。不一致の主因は「本文に無い知識」で日本語起因ではない |
| zenn (acrosstudio, 日本語 48 call) | 顧客問い合わせ 16 文 | 中央値 286 ms、一部ラベル不一致 |
| ManatoYamashita/digicon-chan-ai #26 | Gemini の感情判定を Jev で置換するか | 「shadow で日本語精度を実測、明確に上回らない限り置換しない」で待機中 |
| dev.to reachjalil | Jev をログの pre-filter に | **pre-filter パイプラインの方が生ログを GPT-5.6 Luna に直送するより高くついた** (3,000 + 5,000 行)。前段を足すと逆に高くなる実例 |
| blablanumerodeux/model-router | Jev で分類 → tier 選択の proxy | tool loop が同じ会話を 5〜10 回再送するので **(system,user) キーで 15 分 cache し、ターンごとには Jev を呼ばない** 設計 |
| BerriAI/litellm #41615 (09-18 merge) | complexity router の分類器に Jev | simple 判定 confidence 1.0 / complex 0.52、Jev 代 $0.000018 / call |
| sseshachala/conductai #2139 が引用 | 2,000 通のフィッシング分類 | Jev 62.6% vs Haiku 81.3% (一部合成データ) |

統合: LangChain (harness blog)、Vercel AI SDK / AI Gateway、Netlify AI Gateway、Cloudflare、OpenRouter Decisions API、Pydantic AI、bifrost (#7278 提案)。

**Jev + DeepSeek V4.1 Flash の直接事例は JevRouter #2 の 1 件のみ** (比較であって併用ではない)。併用の経済性は以下の料金と上記の実測から推論している。

## 2. DeepSeek V4.1 Flash の最新価格・cache 仕様 (2026-09-10 公開)

| 項目 | 値 |
|---|---|
| API モデル名 | `deepseek-flash` (最新 V4.1 Flash)。Anthropic 互換エンドポイント `https://api.deepseek.com/anthropic` あり |
| 標準料金 (USD / 1M) | **cache hit $0.006 / cache miss $0.30 / output $1.20** |
| オフピーク | 上記の **1/2** ($0.003 / $0.15 / $0.60)。ピーク = 平日 01:00〜04:00 と 06:00〜10:00 UTC (JST 10〜13 時、15〜19 時)。それ以外は全てオフピーク |
| context | 1M tokens |
| thinking | thinking / non-thinking を切替可 (Anthropic 形式では `thinking` オブジェクト。`budget_tokens` と `cache_control` は無視) |
| tool calling / JSON | 対応 (OpenAI 形式 `tools`、Anthropic 形式 `tool_use`/`tool_result`) |
| 速度 (Artificial Analysis, DeepSeek 直) | 出力 ≈ 208 tok/s、TTFT ≈ 1.15 s (reasoning max) |
| context caching | **全ユーザー既定で有効、コード変更不要**。prefix 一致、**64 token 単位** (64 未満は cache されない)。usage に `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。未使用エントリは数時間〜数日で消える |
| `user_id` | `[a-zA-Z0-9\-_]{1,512}`、PII 禁止。**content safety / KV cache / スケジューリングの 3 つを user_id 単位で隔離**。OpenAI 形式は `extra_body.user_id`、Anthropic 形式は `metadata.user_id` |

hit と miss の比は **50 倍**。エージェントループのように毎ターン prefix を再送する用途では、hit 部分の課金はほぼゼロになる。

## 3. 現在の LLM call 一覧 (1 call = 1 run のループ)

`runner.py` の 1 箇所を、ループ内の「判断ポイント」に分解した。トークン数はコードから推定 (system ≈ 490 tok、tool schema 10 個 ≈ 2,060 tok、**prefix ≈ 2,600 tok**)。README の例「参照曲を解析してパッド/キック + 8 小節のメロディとコード」相当の典型 run を 13 ターンとして試算。

| ターン | ツール | assistant 出力 tok | tool_result tok | 判断の種類 |
|---|---|---|---|---|
| 1 | resolve_reference_url | 60 | 120 | 引数は CLI 引数そのまま → **コードで先にできる** |
| 2 | analyze_reference_audio | 60 | 350 | 同上 |
| 3 | set_project | 50 | 30 | BPM/キーは解析結果 → **コードで既定値を出せる** |
| 4 | search_samples | 60 | **2,500** | query は自然言語生成 (LLM) |
| 5 | scale_info | 40 | 400 | 引数はキー → **コードで先にできる** |
| 6-7 | add_audio_clip ×2 | 70 | 30 | 候補からの選択 (有限選択 + 配置引数) |
| 8-10 | add_midi_clip ×3 | 700〜1,000 | 60 | **生成** (ノート列)。スケール検証は既に決定的 |
| 11 | add_note_for_user | 150 | 15 | 生成 |
| 12 | realize_in_daw | 40 | 80 | 終了判定 |
| 13 | 最終テキスト | 300 | — | 生成 |

1 run: 入力合計 ≈ **82,550 tok** (thinking 込みで ≈ 199,550)、出力 ≈ 3,500 tok (thinking 込み ≈ 23,000)。ループ内で prefix caching が自然に効く割合 ≈ **88%** (前リクエスト全体が hit、増分だけ miss)。

### cache 観点の監査

| 観点 | 現状 | 評価 |
|---|---|---|
| system prompt の安定性 | 固定 | ○ |
| tool schema の固定性 | `build_tools` は毎回同じ順 | ○ |
| prompt の可変箇所 | user message 先頭の URL/パス、依頼文 | ○ (prefix の後ろ) |
| conversation history の並び | tool_runner が追記のみ | ○ |
| 並列ツール呼び出し | tool_runner は逐次 | ○ (cache が前リクエスト完了後に載る前提を満たす) |
| user_id | なし | 単一ユーザー CLI なら固定値で可 |
| cache hit / miss の記録 | なし | **× 最初にやること** |
| 可変の肥大化 | `search_samples` 2,500 tok、`show_plan` は全ノート JSON | **× 履歴に残り続ける** |

## 4. Jev 導入前に DeepSeek 側だけでできる削減 (= baseline B)

| # | 施策 | 効果 (推定) |
|---|---|---|
| D0 | **決定的プレステップ**: `--url` / `--audio` / `--segment` があれば `resolve_reference` / `analyze_file` / `scale_pitches` をコードで先に実行し、結果と `set_project` の既定値を初回 user message に埋め込む | 13 → 9〜10 ターン、入力 25〜30% 減、壁時計 10〜15 秒短縮。**Jev より大きい** |
| D1 | usage 記録: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` / output / ターン数 / 所要時間を run ごとに JSONL | コスト可視化 (すべての判断の前提) |
| D2 | `search_samples` の返却を絞る: path / score / duration / tags 上位 5 / license のみ、`feature_vector` は返さない、既定 limit 8→5 | 2,500 → ≈1,000 tok、以降 9 ターン分の履歴から消えるので 1 run ≈ 13k tok 減 |
| D3 | `show_plan` はサマリ (track / clip / note 数) のみ返し、全文は `--verbose` 時だけ | 肥大防止 |
| D4 | non-thinking 既定、`ok: false` が 2 回続いたときだけ thinking on | 出力コスト 1/3〜1/6 |
| D5 | `metadata.user_id` に固定値 (`dtm-agent`)、将来マルチテナント時はテナント ID | 隔離要件の準備 |
| D6 | オフピーク実行: バッチ的処理 (再ランク、複数曲の一括生成) は JST 10〜13 / 15〜19 時を避ける | 半額 |

## 5. 各判断ポイントの Jev 分類

凡例: **A** = Jev で完結し DeepSeek を呼ばない / **B** = Jev の結果で短い DeepSeek call だけ行う / **C** = Jev の後に同じ DeepSeek prompt を呼ぶ / **D** = DeepSeek 単独が極端に安い

| 判断 | Case | 判定 | 理由 |
|---|---|---|---|
| 入口: 「解析だけ」「類似サンプルを探すだけ」か「制作」か | **A** | **得 (要 shadow)** | high-confidence なら `analyze` / `match` 相当のコードで完結、LLM 0 call。ただし該当比率が未知 |
| サンプル候補の再ランク (16 → 4〜6) | **B** | **変わらない〜わずかに得** | 節約 $0.0004〜0.004 / run。品質と入力削減が主目的。Jev の Score は英語タグ照合に向く |
| 毎ターンのツール選択 | C | **損** | 引数生成は LLM 必須。DeepSeek call は 1 つも減らず、+4〜19% / ターン、+0.3〜4.6 秒 / ターン |
| ループ終了 / 完成度判定 | C | **損** | `end_turn` を置換できない。誤判定で追加ターンが増える |
| メロディの音域・跳躍・密度評価 (ROADMAP v0.3) | — | **不要** | 数値比較は Jev の公式 weakness。決定的コードで書く |
| スケール外ノートの検証 | — | **不要** | 既に `validate_notes` で決定的 |
| モデル階層ルーティング (Flash ↔ Pro / Opus) | B | **保留** | Flash 単一運用なら行き先がない。Opus 5 を残す運用なら Jev 代 $0.00003 は 1 件の Flash 化で回収できるが、品質判定の問題 |
| thinking on/off | B | **不要** | 効果 $0.01〜0.03 / run、誤判定コストが大きい。D4 の規則で足りる |
| refusal / 安全 | — | **不要** | Anthropic はサーバ側 fallback。DeepSeek に移っても Jev の役割ではない |
| Freesound 由来テキストの prompt injection 検査 | (安全策) | **補助のみ** | Jev 自身が state 内の指示に動かされる (公式)。tags / description の切り詰め + 命令文除去を決定的に先に行う。コスト削減にはならない |
| (v1.0) 差分編集の意図分類 → 決定的変換 | **A** | **将来の本命** | 「ベースを 8 分に」「半音下げて」「もっと暗く (velocity / 音域)」は `ProjectPlan` への決定的変換で LLM 0 call |

## 6. 損益分岐 (`scripts/jev_cost_model.py` の出力)

### 6.1 1 run のコスト

| 構成 | cache hit 0% | 25% | 50% | 75% | 90% |
|---|---|---|---|---|---|
| DeepSeek 単独 (標準, non-thinking) | $0.0290 | $0.0229 | $0.0168 | $0.0108 | **$0.0071** |
| DeepSeek 単独 (オフピーク) | $0.0145 | $0.0114 | $0.0084 | $0.0054 | $0.0036 |
| DeepSeek thinking on (標準) | $0.0875 | $0.0728 | $0.0581 | $0.0435 | $0.0347 |
| 参考: 現行 Claude Opus 5 (cache なし) | $1.573 | | | | |
| 参考: Claude Opus 5 (cache_control あり) | $0.731 | | | | |

ループ内の自然 hit 率 ≈ 88% なので、実効的な DeepSeek 単独コストは **$0.007〜0.01 / run** とみなす。

### 6.2 Case A (入口ゲート): Jev 1 call が run 全体を省く

Jev gate ≈ 800 tok = **$0.000034 / call**。100 リクエスト、DeepSeek 単独 = $1.076 (hit 75%) を基準。

| escalation 率 (Jev の後に DeepSeek へ進む割合) | Jev + DeepSeek / 100 req | 削減 |
|---|---|---|
| 10% | $0.111 | 89.7% |
| 25% | $0.272 | 74.7% |
| 50% | $0.541 | 49.7% |
| 75% | $0.811 | 24.7% |
| 90% | $0.972 | 9.7% |

損益分岐 escalation 率 (cache hit 率別): 0% → 99.88%、25% → 99.85%、50% → 99.80%、75% → 99.69%、90% → 99.53%。
つまり **100 件中 99.5 件を DeepSeek に送っても金額上は得**。金額の損益分岐は問題にならない。問題は (a) 該当リクエストが実際に何割あるか、(b) 「制作依頼を解析だけと誤判定して LLM を呼ばない」偽陽性のユーザー体験コスト。**判断基準は金額ではなく precision**。

### 6.3 Case C (ループ内で毎ターン Jev): DeepSeek は結局呼ぶ

Jev loop 判定 ≈ 3,000 tok = $0.000126 / call。ループ中盤 (8 ターン目、入力 ≈ 6,700 tok) の DeepSeek 1 ターンと比較。

| cache hit 率 | DeepSeek 1 ターン (標準 / オフピーク) | Jev の上乗せ | Jev が省くべきターン割合 (損益分岐) |
|---|---|---|---|
| 0% | $0.00307 / $0.00153 | +4.1% / +8.2% | 4.1% / 8.2% |
| 25% | $0.00258 / $0.00129 | +4.9% / +9.8% | 4.9% / 9.8% |
| 50% | $0.00209 / $0.00105 | +6.0% / +12.0% | 6.0% / 12.0% |
| 75% | $0.00161 / $0.00080 | +7.8% / +15.7% | 7.8% / 15.7% |
| 90% | $0.00131 / $0.00066 | +9.6% / +19.2% | 9.6% / 19.2% |

**cache が効くほど Jev を挟む合理性は下がる** (0% → 90% で必要な省略率が 2.3 倍)。ツール選択を Jev に任せても省けるターンは 0 なので、この構成は全ての hit 率で損。

### 6.4 Case B (再ランク): 履歴の削減効果

| cache hit 率 | 節約 | Jev 代 | 純効果 / run |
|---|---|---|---|
| 0% | $0.00389 | $0.00011 | +$0.00378 |
| 75% | $0.00103 | $0.00011 | +$0.00093 |
| 90% | $0.00046 | $0.00011 | **+$0.00035** |

得ではあるが 1 run の 5% 程度。D2 (返却を絞る) だけでほぼ同じ削減が Jev なしで得られる。

### 6.5 レイテンシ

| 構成 | 1 run の壁時計 (推定) |
|---|---|
| DeepSeek 単独 13 ターン (TTFT ≈ 1 s + 208 tok/s) | 30〜60 秒 |
| + D0 プレステップ (9〜10 ターン) | 20〜45 秒 |
| + Jev 入口ゲート 1 回 | +0.2〜0.6 秒 (≈1%) |
| + Jev 毎ターン (1 問) | +4〜8 秒 / run (≈10%) |
| + Jev 毎ターン (直列 3 問, dsh-jev 実測) | +60 秒 / run |

## 7. Jev → DeepSeek escalation 設計 (採用候補 J1 / J2 のみ)

```
dtm-agent run <prompt> [--url] [--audio] [--segment]
  │
  ├─ 規則: --audio も --url も無く、依頼文に「作って/置いて/MIDI/トラック」等が無い → analyze/match 候補
  ├─ 規則で確定しない場合のみ Jev (Choice: analyze_only | match_only | full_production | unclear,
  │                              Noul: needs_daw_output)
  │     ├─ analyze_only / match_only かつ confidence ≥ 0.85 かつ needs_daw_output ≤ 0.1
  │     │      → cmd_analyze / cmd_match 相当を実行して終了 (LLM 0 call)      … Case A
  │     └─ それ以外 / Jev 失敗 / timeout 1.5 s / 429 / 400 → 通常ルート
  │
  ├─ 決定的プレステップ (D0) → 初回 user message に解析結果と set_project 既定値を埋め込む
  │
  └─ DeepSeek V4.1 Flash tool loop (non-thinking)
        └─ search_samples の中で: 16 件 → Jev Score (fit: poor/ok/good, 質問 ≤ 32 問/回)
              → good 上位 4〜6 件だけ tool_result に返す。Jev 失敗時は従来通り全件 … Case B
```

Jev に渡す state は英語で組む (英語が最良、CJK は同等ではない)。依頼文は日本語のまま入れ、質問と選択肢の説明は英語にする。`AudioProfile.describe()` の英語版を用意する。

## 8. Shadow mode 設計

`DTM_AGENT_JEV=off|shadow|gate|rerank` (既定 `off`)。`shadow` では **現行の挙動を一切変えず**、同じ入力を Jev にも送って結果を捨て、ログだけ残す。Jev 呼び出しは別スレッドで行い、失敗しても run に影響させない。

記録項目 (run ごとに `dtm_agent_out/jev_shadow.jsonl`):

| 項目 | 取り方 |
|---|---|
| `jev_decision`, `jev_confidence`, `jev_probabilities`, `jev_latency_ms`, `jev_input_tokens`, `jev_cost` | Jev レスポンス |
| `deepseek_decision` | 実際に辿った経路 (`add_*` / `realize_in_daw` を呼んだか = full_production、呼ばなければ analyze/match 相当) |
| `agreement` | 上 2 つの一致 |
| `human_label` | 100〜200 件の日本語依頼文セットに人手ラベル (必須。TypeSafe の eval でも invoice 61.8% のように領域差が大きい) |
| `precision` / `recall` / `false_positive` / `false_negative` | 「LLM を省いた (skip)」を陽性として集計 |
| confidence bucket 別 accuracy | 0.5〜0.7 / 0.7〜0.85 / 0.85〜0.95 / 0.95+ |
| `deepseek_latency_ms`, `deepseek_turns`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `output_tokens`, `deepseek_cost` | usage から (D1) |
| `deepseek_avoided_calls` | gate が skip 判定した件数 (shadow では「skip したはず」の件数) |
| `escalation_rate` | 1 − skip 率 |
| `total_cost_per_request` | `jev_cost + deepseek_cost` (shadow では両方かかるので比較は「本番化した場合の推定」を別列で出す) |
| 再ランク: `chosen_in_jev_top6` | LLM が `add_audio_clip` に渡した path が Jev 上位 6 に含まれるか |

`dtm-agent eval-jev jev_shadow.jsonl` で上記の集計表を出す。

### KPI (本番化の条件)

| 候補 | 条件 |
|---|---|
| J1 gate | skip の **precision ≥ 0.98** (閾値 0.85 で)、recall ≥ 0.6、`run` 流入のうち skip 可能な依頼が **≥ 10%**、Jev p95 ≤ 600 ms、`total_cost_per_request` が baseline B より低い (実測) |
| J2 rerank | `chosen_in_jev_top6 ≥ 0.90`、人手評価で採用サンプルの適合度が下がらない、1 run コストが baseline B 以下 |
| 共通 | Jev 由来の失敗 (timeout / 429 / 400) で run が止まらない (0 件) |

満たさなければ **Jev 不要** と結論して閉じる。

## 9. Fallback

- Jev timeout 1.5 s / 429 / 5xx / 400 (state > 32k) → 判定を `unclear` 扱いにして通常ルート。**Jev の失敗が「許可」側に倒れる設計にしない** (gate は「skip しない」が安全側)。
- Jev の判定で skip した場合も、`--force-agent` で同じ依頼を LLM ルートに送れるようにする。
- 再ランクで Jev が全件 poor と返したら従来通り全件を返す。
- DeepSeek 側: SDK 既定の再試行。refusal 相当 (`stop_reason` 異常) は現行のメッセージを返す。

## 10. 実装変更箇所

| ファイル | 変更 |
|---|---|
| `dtm_agent/agent/runner.py` | provider 設定 (`DTM_AGENT_PROVIDER=anthropic|deepseek`, `base_url`, `metadata.user_id`)、`thinking` 切替、usage を `on_message` から集計して返す (`RunStats`)。DeepSeek の Anthropic 互換 endpoint が `client.beta.messages.tool_runner` の beta path / `betas` / `output_config` / `fallbacks` を受けるかは **未検証** → 受けなければ手動ループか OpenAI 形式に切替 |
| `dtm_agent/agent/prestep.py` (新規) | D0 決定的プレステップ。`analyze_file` / `resolve_reference` / `scale_pitches` を先に実行し、英語の要約と `set_project` 既定値を作る |
| `dtm_agent/agent/tools.py` | `search_samples` の返却を絞る (D2)、`show_plan` サマリ化 (D3)、再ランクフック (`rerank=callable`) |
| `dtm_agent/agent/jev.py` (新規) | TypeSafe client (直接 `api.typesafe.ai` または OpenRouter Decisions API)、`gate_question()`, `rerank_questions()`、shadow logger、mock provider (テスト用、ネットワーク不要) |
| `dtm_agent/cli.py` | `--jev {off,shadow,gate,rerank}`、`--force-agent`、`eval-jev` サブコマンド |
| `dtm_agent/models.py` | `AudioProfile.describe_en()` |
| `tests/` | mock Jev で gate / rerank / fallback の単体テスト。合成音声のみで完結させる |
| `docs/ARCHITECTURE.md` | プレステップと Jev の位置を追記 |
| `scripts/jev_cost_model.py` | 本 Issue の試算 (同梱済み)。実測トークンで `TURNS` / `PREFIX` を置き換える |

## 11. Migration plan / Rollback plan

| 段階 | 内容 | 判断 |
|---|---|---|
| 0 | D1 usage 記録を入れて現行 Opus 5 の実コストを 20 run 分測る | 本 Issue の推定 ($1.6 / run) を実測に置換 |
| 1 | DeepSeek V4.1 Flash へ切替 (`DTM_AGENT_PROVIDER=deepseek`)。同じ 20 依頼で品質を目視比較 | 品質が許容なら以降の baseline |
| 2 | D0 / D2 / D3 / D4 (Jev なしの削減) | ターン数・トークン・コストの前後比較 |
| 3 | Jev **shadow** を 2 週間 or 200 run | §8 KPI を集計 |
| 4 | KPI 達成なら J1 gate → J2 rerank の順に `DTM_AGENT_JEV` で段階有効化 | 1 週間ごとに `total_cost_per_request` を確認 |
| Rollback | `DTM_AGENT_JEV=off` (即時、コード変更不要)。DeepSeek → Anthropic は `DTM_AGENT_PROVIDER=anthropic`。Jev のログは残す | — |

段階 3 で KPI 未達なら **段階 2 で終了 = Jev 不要** が結論。現状の見立てでは、そうなる可能性の方が高い。

## 12. 参考リンク

Jev: OpenRouter `typesafe/jev-1.13` / docs.typesafe.ai (introduction, models, model-jaggedness/jev-1.13, cookbooks/llm_guardrails) / pydantic/genai-prices#704 / BillionsBobby/JevRouter#2 / wotai-dev/typesafe-jev-tools / AnshChoudhary/typesafe-ai-firewall (report.md) / buberlo/dsh-jev / PerryLink/jevcore / seanperkins/omp-jev-watchdog / mtane0412/hanatane#50 / uesgugikouhei-oss/jev-ja-eval / ManatoYamashita/digicon-chan-ai#26 / BerriAI/litellm#41615 / maximhq/bifrost#7278 / blablanumerodeux/model-router / rajdhakad9826/jev-router / cobusgreyling/loop-engineering#622 / sseshachala/conductai#2139 / octalide/sift#10 / HiQS-Labs/XYZ-forge#709 / latent.space AINews (HN 1,655 pt) / zenn acrosstudio / dev.to reachjalil

DeepSeek: api-docs.deepseek.com (news/news260910, quick_start/pricing, guides/kv_cache, quick_start/rate_limit, guides/anthropic_api) / VentureBeat 09-10 / Artificial Analysis deepseek-v4-1-flash / cohesion-org/deepseek-go examples/15_user_id

Anthropic (現行 baseline): Claude Opus 5 $5 / $25、cache read $0.50、cache write $6.25 (per 1M)
