# 23. mario — ゲーム状態から Jev が Mario の行動を選ぶ

元ネタ: [fhshaik/typesafe-mario](https://github.com/fhshaik/typesafe-mario)

## 目的

エミュレータの RAM (または内蔵シミュレータ) から「Mario の位置・速度・近くの敵・穴・ブロック」だけを抜き出した
小さな JSON を Jev に渡し、1 手ごとに `action` (Choice) と `danger` (Score) を聞いて操作する制御実験。
LLM にフレームを読ませるのではなく、構造化した状態に対する高速な System One 判断で操作できるかを試す。

## 構成

| 部品 | 役割 |
| --- | --- |
| `GameState` | Jev に渡す状態 (`to_jev()` で相対座標の JSON に) |
| `MiniMario` | 依存ゼロの格子サイドスクローラ。重力・ジャンプ弧 (7 ステップ)・左へ歩く goomba・穴・ブロック |
| `controller(jev, state)` | `action` {left, right, jump, jump_right, run_right, wait} + `danger` [safe, caution, imminent] |
| `apply_danger_policy` | danger で速度を落とし (run→walk→jump)、次に Jev へ聞くまでのホールド数を決める |
| `decode_smb_ram(bytes)` | NES 版 SMB の 2KB RAM から GameState (0x006D/0x0086 = x, 0x00CE = y, 0x000F../0x0016.. = 敵) |
| `NesPyAdapter` / `PyBoyAdapter` | 実エミュレータ用の遅延スタブ (import は使うときだけ) |

確信度が `min_confidence` (既定 0.3) 未満なら Jev の答えを捨て、決定的な `reflex_action` に落とす。

## 使い方

```bash
jevlab mario play --steps 200 --render --backend mock      # ASCII で毎ステップ描画
jevlab mario play --steps 500 --jev-every 3 --seed 3       # 安全なときは 3 ステップに 1 回だけ聞く
jevlab mario play --json > run.json
```

Python から:

```python
from jevlab.core import Jev
from jevlab.apps.mario import MiniMario, play, decode_smb_ram

result = play(Jev("mock"), MiniMario(seed=1), steps=200)
state = decode_smb_ram(env.unwrapped.ram)   # nes_py の 2KB RAM
```

## 追加依存

- 内蔵シミュレータのみなら不要
- 実機: `pip install gym-super-mario-bros nes-py` (NesPyAdapter) / `pip install pyboy` (PyBoyAdapter)

## 課金・外部送信

Jev API を使う場合、1 ステップ = 1 リクエスト (`--jev-every` で削減可能)。state は数値だけの小さな JSON。

## 制限

- `decode_smb_ram` は穴とブロックを埋めない (地形バッファ 0x0500〜 の解釈が必要)。gym の `info['x_pos']` などと併用する
- PyBoy のアドレスは Super Mario Land の代表値で、作品ごとに `addresses` で上書きが必要
- MockBackend は語彙重なりで選ぶだけなので、実際にクリアするには本物の Jev か `FunctionBackend` を使う
