# 25. drone — ドローン障害物回避の判断を Jev に委ねる

元ネタ: [RomanSlack/jev-drone](https://github.com/RomanSlack/jev-drone)

## 目的

3D 空間の球状障害物を避けてゴールへ向かうドローンの「次の機動」を Jev に選ばせる。
センサは 6 方向 (前後左右上下) のレイキャスト距離と、機体座標系での相対ゴールベクトルだけ。
最も近い障害物が `hard_margin` を切ったら**決定的なリフレックス層が Jev を上書き**する (衝突回避は委ねない)。

## 構成

- `KinematicDrone` : 依存ゼロの 3D 質点シミュレータ (球状障害物、地面、外壁、ゴール半径)。`demo(seed)` でランダム配置
- `MuJoCoDrone` : `pip install mujoco numpy` があれば同じ API。内蔵の最小 MJCF (`build_mjcf`) で自由剛体 + 球ジオム、`mj_ray` でレイキャスト
- `decide()` : `maneuver` Choice {forward, back, climb, descend, yaw_left, yaw_right, hover} + `risk` Score [clear, moderate, collision_imminent]
  - risk で移動量をスケール (1.0 / 0.6 / 0.3)。imminent なのに forward なら hover
  - 確信度 < `min_confidence` → `greedy_action` (ゴール方向へ素直に向く) にフォールバック
- `reflex_override()` : nearest < hard_margin なら最も近い障害物と反対方向へ全速で並進 (旋回は振動するので使わない)
- `fly()` : 制御ループ。軌跡ログ、ASCII 俯瞰描画 (`--render`)、CSV (`--csv`)

## 使い方

```bash
jevlab drone fly --steps 200 --seed 1 --backend mock
jevlab drone fly --steps 200 --seed 1 --render --csv flight.csv
jevlab drone fly --sim mujoco --seed 2          # mujoco がある場合
```

## 追加依存

- 既定 (`--sim kinematic`) は不要
- `--sim mujoco`: `pip install mujoco numpy` (`pyproject` の extras `drone`)

## 課金・外部送信

Jev API を使う場合、リフレックスが効いたステップは Jev を呼ばない。state は距離と角度の数値のみ。

## 制限

- 質点モデルで姿勢ダイナミクスはない。MuJoCo 版も速度指令で位置を書き換える簡略版
- MockBackend は語彙重なりで選ぶため、実際にゴールへ着くには本物の Jev か `FunctionBackend(greedy_action)` を使う
