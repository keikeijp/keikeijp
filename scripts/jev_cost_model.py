"""Jev + DeepSeek V4.1 Flash の損益分岐を dtm-agent の 1 run プロファイルで試算する。

python scripts/jev_cost_model.py

前提 (2026-09-20 時点の公開料金、USD / 1M tokens):
  DeepSeek V4.1 Flash (deepseek-flash) 標準: cache hit 0.006 / cache miss 0.30 / output 1.20
                                     オフピーク: 上記の 1/2 (平日 01:00-04:00, 06:00-10:00 UTC 以外)
  Jev 1.13 (TypeSafe): input 0.042 / output 0 (state + question の入力トークンのみ課金)
  Claude Opus 5 (現行): input 5 / output 25 / cache write 6.25 / cache read 0.50
トークン数はコードから推定した概算 (実測値は shadow mode で置き換えること)。
"""

from __future__ import annotations

M = 1_000_000

# --- 1 run の典型ターン列 (assistant 出力トークン, tool_result トークン) ---
# README の例「参照曲を解析してパッド/キック + 8 小節のメロディとコード」相当。
TURNS = [
    ("resolve_reference_url", 60, 120),
    ("analyze_reference_audio", 60, 350),
    ("set_project", 50, 30),
    ("search_samples", 60, 2500),
    ("scale_info", 40, 400),
    ("add_audio_clip", 70, 30),
    ("add_audio_clip", 70, 30),
    ("add_midi_clip", 900, 60),
    ("add_midi_clip", 700, 60),
    ("add_midi_clip", 1000, 60),
    ("add_note_for_user", 150, 15),
    ("realize_in_daw", 40, 80),
    ("final_text", 300, 0),
]
PREFIX = 2_600          # SYSTEM_PROMPT (~490) + 10 ツールの schema (~2,060)
USER_PROMPT = 150
THINK_PER_TURN = 1_500  # thinking 有効時の 1 ターンあたり平均 (adaptive/high 相当の概算)


def run_profile(thinking: bool):
    """(入力トークン総計, 出力トークン総計, 各ターンの入力長, 各ターンの新規分) を返す。"""
    history = USER_PROMPT
    inputs, fresh, out_total = [], [], 0
    for _, out, result in TURNS:
        inputs.append(PREFIX + history)
        fresh.append(out + result)
        think = THINK_PER_TURN if thinking else 0
        out_total += out + think
        history += out + result + (think if thinking else 0)
    return sum(inputs), out_total, inputs, fresh


def deepseek_cost(hit_rate: float, *, off_peak=False, thinking=False):
    inp, out, _, _ = run_profile(thinking)
    hit, miss, o = (0.006, 0.30, 1.20)
    if off_peak:
        hit, miss, o = hit / 2, miss / 2, o / 2
    return (inp * (1 - hit_rate) * miss + inp * hit_rate * hit + out * o) / M


def deepseek_turn_cost(hit_rate: float, *, off_peak=False):
    """ループ中盤 (8 ターン目) の 1 ターン分。"""
    _, _, inputs, _ = run_profile(False)
    inp = inputs[7]
    out = TURNS[7][1]
    hit, miss, o = (0.006, 0.30, 1.20)
    if off_peak:
        hit, miss, o = hit / 2, miss / 2, o / 2
    return (inp * (1 - hit_rate) * miss + inp * hit_rate * hit + out * o) / M, inp


def natural_hit_rate():
    """ループ内で prefix caching が自然に効く割合 (前リクエスト全体が hit、増分が miss)。"""
    _, _, inputs, fresh = run_profile(False)
    hits = sum(inputs[i] - fresh[i - 1] if i else 0 for i in range(len(inputs)))
    return hits / sum(inputs)


def claude_cost(cached: bool):
    inp, out, inputs, fresh = run_profile(True)
    if not cached:
        return (inp * 5 + out * 25) / M
    # tools+system+最新メッセージに cache_control。各ターン: 前回分は read、増分は write。
    read = sum(inputs[i] - (fresh[i - 1] if i else 0) - (PREFIX if i == 0 else 0) for i in range(len(inputs)))
    write = sum((fresh[i - 1] if i else PREFIX + USER_PROMPT) for i in range(len(inputs)))
    return (read * 0.50 + write * 6.25 + out * 25) / M


JEV_PRICE = 0.042
JEV_GATE_TOKENS = 800      # 入口ゲート: prompt + CLI context + 質問
JEV_TURN_TOKENS = 3_000    # ループ内: 設計図要約 + 直近 tool_result + 候補ツール
JEV_RERANK_TOKENS = 2_500  # サンプル再ランク: 16 件 × ~120 + 参照プロファイル


def jev(tokens):
    return tokens * JEV_PRICE / M


def main():
    inp_nt, out_nt, _, _ = run_profile(False)
    inp_t, out_t, _, _ = run_profile(True)
    print(f"1 run 推定: ターン数={len(TURNS)}  入力合計={inp_nt:,} tok (thinking 込み {inp_t:,})  出力={out_nt:,} tok (thinking 込み {out_t:,})")
    print(f"ループ内で自然に得られる DeepSeek cache hit 率 ≈ {natural_hit_rate():.0%}\n")

    print("== 現行 Claude Opus 5 (adaptive thinking, effort high) 1 run ==")
    print(f"  cache_control なし (現状): ${claude_cost(False):.3f}")
    print(f"  cache_control あり:        ${claude_cost(True):.3f}\n")

    print("== DeepSeek V4.1 Flash 1 run (non-thinking) : cache hit 率別 ==")
    print("  hit率   標準      オフピーク   標準(thinking)")
    for h in (0, .25, .5, .75, .9):
        print(f"  {h:>4.0%}  ${deepseek_cost(h):.4f}   ${deepseek_cost(h, off_peak=True):.4f}     ${deepseek_cost(h, thinking=True):.4f}")

    print(f"\n== Jev 1 call ==  gate ${jev(JEV_GATE_TOKENS):.6f} / loop ${jev(JEV_TURN_TOKENS):.6f} / rerank ${jev(JEV_RERANK_TOKENS):.6f}")

    print("\n== Case A: 入口ゲート (Jev が run 全体を省略) 100 リクエストあたり ==")
    print("  escalation = Jev の後に DeepSeek run へ進む割合。基準 = DeepSeek 単独 (hit 75%, 標準)")
    base = deepseek_cost(.75)
    print(f"  DeepSeek 単独: ${base*100:.3f} / 100 req")
    for e in (.1, .25, .5, .75, .9):
        c = (jev(JEV_GATE_TOKENS) + e * base) * 100
        print(f"  escalation {e:>3.0%}: Jev+DeepSeek ${c:.3f}  (削減 {1 - c/(base*100):.1%})")
    for h in (0, .25, .5, .75, .9):
        d = deepseek_cost(h)
        print(f"  hit {h:>3.0%}: 損益分岐 escalation = {1 - jev(JEV_GATE_TOKENS)/d:.2%}  (100 件中 {100*(1 - jev(JEV_GATE_TOKENS)/d):.1f} 件まで DeepSeek に送っても得)")

    print("\n== Case C: ループ内で毎ターン Jev を挟む (DeepSeek は結局呼ぶ) ==")
    print("  hit率   DeepSeek 1ターン   Jev/ターン比   Jev が省くべきターン割合(損益分岐)")
    for h in (0, .25, .5, .75, .9):
        t, inp = deepseek_turn_cost(h)
        to, _ = deepseek_turn_cost(h, off_peak=True)
        print(f"  {h:>4.0%}  ${t:.5f} (off-peak ${to:.5f})   {jev(JEV_TURN_TOKENS)/t:.1%} (off-peak {jev(JEV_TURN_TOKENS)/to:.1%})   {jev(JEV_TURN_TOKENS)/t:.1%} / {jev(JEV_TURN_TOKENS)/to:.1%}")

    print("\n== Case B: サンプル再ランク (Jev で 16 件→4 件に絞り、以降のターンの入力を減らす) ==")
    saved_tokens = 12 * 120 * (len(TURNS) - 4)   # 削れる 12 件分が残り 9 ターンの履歴から消える
    for h in (0, .75, .9):
        hit, miss = 0.006, 0.30
        saved = saved_tokens * ((1 - h) * miss + h * hit) / M
        print(f"  hit {h:>3.0%}: 節約 ${saved:.5f} − Jev ${jev(JEV_RERANK_TOKENS):.5f} = 純 ${saved - jev(JEV_RERANK_TOKENS):+.5f} / run")


if __name__ == "__main__":
    main()
