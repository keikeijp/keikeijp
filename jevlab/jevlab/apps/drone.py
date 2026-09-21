"""jev-drone (RomanSlack/jev-drone) の再実装。

3D 空間の球状障害物を避けてゴールへ向かうドローンの「次の機動」を Jev に選ばせる。
6 方向レイキャストと相対ゴールベクトルを小さな JSON にして渡し、
`maneuver` Choice と `risk` Score を聞く。最も近い障害物が hard margin を切ったら
決定的なリフレックス層が Jev を上書きする (衝突回避は Jev に委ねない)。

- `KinematicDrone` : 依存ゼロの 3D 質点シミュレータ (球状障害物, 地面, ゴール)
- `MuJoCoDrone`    : mujoco があれば同じ API で動く最小 MJCF (遅延 import)
- `reflex_override`: 近接時の決定的回避
- `fly`            : Jev 制御ループ。軌跡ログ / ASCII 俯瞰描画 / CSV

使い方:
    jevlab drone fly --steps 200 --seed 1 --render --csv path.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import dataclass
from typing import Any

from jevlab.core import Choice, Jev, Score

MANEUVERS = {
    "forward": "機首方向へ 1 単位進む。前方が開けていてゴールが前方にあるとき",
    "back": "機首と逆方向へ 1 単位下がる。前方が塞がり、後方に余裕があるとき",
    "climb": "高度を 1 単位上げる。前方の障害物を上から越える/ゴールが上にあるとき",
    "descend": "高度を 1 単位下げる。ゴールが下にあり、下方に余裕があるとき",
    "yaw_left": "その場で左へ 30 度旋回。ゴールが左にある/左側が開けているとき",
    "yaw_right": "その場で右へ 30 度旋回。ゴールが右にある/右側が開けているとき",
    "hover": "その場で静止。判断がつかない/様子見",
}
RISK_LEVELS = ["clear", "moderate", "collision_imminent"]
RAY_DIRS = ("forward", "back", "left", "right", "up", "down")
YAW_STEP = math.radians(30)


@dataclass
class Sphere:
    x: float
    y: float
    z: float
    r: float


def ray_sphere(origin: tuple[float, float, float], direction: tuple[float, float, float], sphere: Sphere) -> float | None:
    """原点から単位方向ベクトルで飛ばしたレイと球の最初の交点までの距離。当たらなければ None。"""
    ox, oy, oz = origin[0] - sphere.x, origin[1] - sphere.y, origin[2] - sphere.z
    b = 2.0 * (ox * direction[0] + oy * direction[1] + oz * direction[2])
    c = ox * ox + oy * oy + oz * oz - sphere.r * sphere.r
    disc = b * b - 4.0 * c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    t = (-b - root) / 2.0
    if t < 0:
        t = (-b + root) / 2.0
    return t if t >= 0 else None


class KinematicDrone:
    """3D 質点ドローン。位置 (x, y, z) と yaw だけを持つ。"""

    def __init__(self, obstacles: list[Sphere], goal: tuple[float, float, float], start: tuple[float, float, float] = (0.0, 0.0, 2.0), bounds: tuple[float, float, float] = (40.0, 40.0, 12.0), step_size: float = 1.0, max_range: float = 10.0, goal_radius: float = 1.5):
        self.obstacles = list(obstacles)
        self.goal = goal
        self.start = start
        self.bounds = bounds
        self.step_size = step_size
        self.max_range = max_range
        self.goal_radius = goal_radius
        self.reset()

    @classmethod
    def demo(cls, seed: int = 1, n_obstacles: int = 14) -> "KinematicDrone":
        rng = random.Random(seed)
        start, goal = (2.0, 2.0, 2.0), (34.0, 30.0, 4.0)
        obstacles: list[Sphere] = []
        while len(obstacles) < n_obstacles:
            s = Sphere(rng.uniform(6, 32), rng.uniform(4, 30), rng.uniform(1, 6), rng.uniform(1.0, 2.5))
            if math.dist((s.x, s.y, s.z), start) > s.r + 3 and math.dist((s.x, s.y, s.z), goal) > s.r + 3:
                obstacles.append(s)
        return cls(obstacles, goal, start)

    def reset(self) -> dict[str, Any]:
        self.x, self.y, self.z = self.start
        self.yaw = math.atan2(self.goal[1] - self.y, self.goal[0] - self.x)
        self.t = 0
        self.done = False
        self.reached = False
        self.crashed = False
        self.path: list[tuple[float, float, float]] = [self.start]
        return self.sense()

    # -- センサ ------------------------------------------------------------

    def directions(self) -> dict[str, tuple[float, float, float]]:
        f = (math.cos(self.yaw), math.sin(self.yaw), 0.0)
        left = (-math.sin(self.yaw), math.cos(self.yaw), 0.0)
        return {"forward": f, "back": (-f[0], -f[1], 0.0), "left": left, "right": (-left[0], -left[1], 0.0), "up": (0.0, 0.0, 1.0), "down": (0.0, 0.0, -1.0)}

    def raycast(self, direction: tuple[float, float, float], include_walls: bool = True) -> float:
        origin = (self.x, self.y, self.z)
        best = self.max_range
        for sphere in self.obstacles:
            t = ray_sphere(origin, direction, sphere)
            if t is not None and t < best:
                best = t
        if direction[2] < 0:  # 地面
            best = min(best, self.z)
        if not include_walls:
            return max(0.0, best)
        for axis, size in enumerate(self.bounds):  # 外壁
            if direction[axis] > 1e-9:
                best = min(best, (size - origin[axis]) / direction[axis])
            elif direction[axis] < -1e-9:
                best = min(best, origin[axis] / -direction[axis])
        return max(0.0, best)

    def sense(self) -> dict[str, Any]:
        directions = self.directions()
        rays = {name: round(self.raycast(vec), 2) for name, vec in directions.items()}
        # 反射層用の「最寄り」は障害物と地面だけで決める。外壁は step() で位置がクランプされ衝突しないので含めない
        hazards = {name: round(self.raycast(vec, include_walls=False), 2) for name, vec in directions.items()}
        gx, gy, gz = self.goal[0] - self.x, self.goal[1] - self.y, self.goal[2] - self.z
        forward = gx * math.cos(self.yaw) + gy * math.sin(self.yaw)
        right = gx * math.sin(self.yaw) - gy * math.cos(self.yaw)
        nearest = min(hazards, key=hazards.get)
        return {
            "rays": rays,
            "goal": {"forward": round(forward, 2), "right": round(right, 2), "up": round(gz, 2), "distance": round(math.sqrt(gx * gx + gy * gy + gz * gz), 2), "bearing_deg": round(math.degrees(math.atan2(right, forward)), 1)},
            "nearest": {"direction": nearest, "distance": hazards[nearest]},
            "altitude": round(self.z, 2),
        }

    # -- 物理 --------------------------------------------------------------

    def step(self, action: str, scale: float = 1.0) -> tuple[dict[str, Any], float, bool, dict[str, Any]]:
        if action not in MANEUVERS:
            raise ValueError(f"unknown maneuver: {action}")
        if self.done:
            return self.sense(), 0.0, True, {"event": "done"}
        before = math.dist((self.x, self.y, self.z), self.goal)
        move = self.step_size * scale
        dirs = self.directions()
        if action == "yaw_left":
            self.yaw += YAW_STEP
        elif action == "yaw_right":
            self.yaw -= YAW_STEP
        elif action != "hover":
            vec = dirs[{"forward": "forward", "back": "back", "climb": "up", "descend": "down"}[action]]
            self.x = min(self.bounds[0], max(0.0, self.x + vec[0] * move))
            self.y = min(self.bounds[1], max(0.0, self.y + vec[1] * move))
            self.z = min(self.bounds[2], max(0.0, self.z + vec[2] * move))
        self.t += 1
        self.path.append((self.x, self.y, self.z))
        event = ""
        after = math.dist((self.x, self.y, self.z), self.goal)
        reward = before - after
        if self.z <= 0.0 or any(math.dist((self.x, self.y, self.z), (s.x, s.y, s.z)) < s.r for s in self.obstacles):
            self.crashed = self.done = True
            reward -= 100.0
            event = "crash"
        elif after <= self.goal_radius:
            self.reached = self.done = True
            reward += 100.0
            event = "goal"
        return self.sense(), reward, self.done, {"event": event, "distance": round(after, 2)}

    def render(self, cols: int = 40, rows: int = 20) -> str:
        sx, sy = cols / self.bounds[0], rows / self.bounds[1]
        grid = [[" "] * cols for _ in range(rows)]

        def put(x: float, y: float, ch: str) -> None:
            c, r = int(x * sx), rows - 1 - int(y * sy)
            if 0 <= c < cols and 0 <= r < rows:
                grid[r][c] = ch

        for s in self.obstacles:
            for dx in range(-int(s.r * sx) - 1, int(s.r * sx) + 2):
                for dy in range(-int(s.r * sy) - 1, int(s.r * sy) + 2):
                    px, py = s.x + dx / sx, s.y + dy / sy
                    if math.hypot(px - s.x, py - s.y) <= s.r:
                        put(px, py, "O" if abs(s.z - self.z) < s.r else "o")
        for px, py, _ in self.path:
            put(px, py, ".")
        put(self.goal[0], self.goal[1], "G")
        put(self.x, self.y, "D")
        border = "+" + "-" * cols + "+"
        body = "\n".join("|" + "".join(r) + "|" for r in grid)
        return f"{border}\n{body}\n{border}\nt={self.t} pos=({self.x:.1f},{self.y:.1f},{self.z:.1f}) yaw={math.degrees(self.yaw):.0f}° goal_dist={math.dist((self.x, self.y, self.z), self.goal):.1f}"


# ---------------------------------------------------------------------------
# MuJoCo 版 (任意)
# ---------------------------------------------------------------------------


def build_mjcf(obstacles: list[Sphere], goal: tuple[float, float, float], start: tuple[float, float, float] = (0.0, 0.0, 2.0)) -> str:
    geoms = "\n".join(f'    <geom name="obs{i}" type="sphere" pos="{s.x} {s.y} {s.z}" size="{s.r}" rgba="0.8 0.2 0.2 0.6"/>' for i, s in enumerate(obstacles))
    return f"""<mujoco model="jev-drone">
  <option gravity="0 0 0" timestep="0.01"/>
  <worldbody>
    <geom name="floor" type="plane" size="50 50 0.1" rgba="0.3 0.3 0.3 1"/>
    <site name="goal" pos="{goal[0]} {goal[1]} {goal[2]}" size="0.3" rgba="0.2 0.9 0.2 1"/>
{geoms}
    <body name="drone" pos="{start[0]} {start[1]} {start[2]}">
      <freejoint name="root"/>
      <geom name="frame" type="box" size="0.25 0.25 0.05" mass="1" rgba="0.2 0.4 0.9 1"/>
      <geom name="nose" type="box" pos="0.3 0 0" size="0.08 0.03 0.03" rgba="1 1 0 1"/>
    </body>
  </worldbody>
</mujoco>"""


class MuJoCoDrone(KinematicDrone):
    """`pip install mujoco` があれば MJCF 上で同じ API を提供する。速度指令で自由剛体を動かす。

    センサは `mujoco.mj_ray` による本物のレイキャスト。位置と yaw は qpos から読む。
    """

    def __init__(self, obstacles: list[Sphere], goal: tuple[float, float, float], start: tuple[float, float, float] = (0.0, 0.0, 2.0), **kw: Any):
        try:
            import mujoco  # type: ignore
        except ImportError as error:
            raise ImportError("MuJoCoDrone には mujoco が必要です: pip install mujoco numpy  (無ければ KinematicDrone を使ってください)") from error
        self._mj = mujoco
        self.model = mujoco.MjModel.from_xml_string(build_mjcf(obstacles, goal, start))
        self.data = mujoco.MjData(self.model)
        super().__init__(obstacles, goal, start, **kw)

    def _sync_from_sim(self) -> None:  # pragma: no cover - mujoco 依存
        self.x, self.y, self.z = (float(v) for v in self.data.qpos[:3])

    def raycast(self, direction: tuple[float, float, float]) -> float:  # pragma: no cover - mujoco 依存
        import numpy as np  # type: ignore

        geomid = np.array([-1], dtype=np.int32)
        dist = self._mj.mj_ray(self.model, self.data, np.array([self.x, self.y, self.z]), np.array(direction), None, 1, -1, geomid)
        return float(min(self.max_range, dist if dist >= 0 else self.max_range))

    def step(self, action: str, scale: float = 1.0) -> tuple[dict[str, Any], float, bool, dict[str, Any]]:  # pragma: no cover - mujoco 依存
        obs, reward, done, info = super().step(action, scale)
        self.data.qpos[:3] = [self.x, self.y, self.z]
        self._mj.mj_forward(self.model, self.data)
        self._sync_from_sim()
        return obs, reward, done, info


# ---------------------------------------------------------------------------
# 制御
# ---------------------------------------------------------------------------

QUESTIONS = {
    "maneuver": Choice(MANEUVERS, "ゴールへ最短で近づきつつ、レイ距離が短い方向へは進まない機動を 1 つ選ぶ"),
    "risk": Score(
        ["clear: すべてのレイ距離が 4 以上", "moderate: いずれかのレイ距離が 2〜4", "collision_imminent: いずれかのレイ距離が 2 未満"],
        "現在の衝突リスクは?",
    ),
}
RISK_SCALE = [1.0, 0.6, 0.3]


def reflex_override(obs: dict[str, Any], hard_margin: float = 1.5) -> dict[str, Any] | None:
    """最も近い障害物が hard_margin 未満なら、最も余裕のある方向へ逃げる決定的な回避を返す。"""
    rays = obs["rays"]
    nearest = obs["nearest"]
    if nearest["distance"] >= hard_margin:
        return None
    # 旋回は位置を変えないので使わない (左右で振動する)。必ず最も近い障害物から離れる並進を選ぶ
    escape = {"forward": rays["forward"], "back": rays["back"], "climb": rays["up"], "descend": rays["down"]}
    opposite = {"down": "climb", "up": "descend", "forward": "back", "back": "forward"}.get(nearest["direction"])
    if nearest["direction"] != "up" and escape["climb"] >= 2 * hard_margin:
        # 球は有限なので、上へ逃げるのが最も確実で、前後に振動しない (back → forward → back ... を避ける)
        action = "climb"
    elif opposite is not None and escape[opposite] >= 2 * hard_margin:
        action = opposite
    else:  # 挟まれた場合: 最も余裕のある並進 (同点なら climb)
        action = max(escape, key=lambda k: (escape[k], k == "climb"))
    return {"action": action, "reason": f"{nearest['direction']} {nearest['distance']:.1f} < {hard_margin}"}


def greedy_action(obs: dict[str, Any], margin: float = 2.0) -> str:
    """Jev の確信度が低いときのフォールバック: ゴール方向へ素直に向く。

    優先順: 前が塞がっていれば上へ抜ける → 進行方向をゴールへ合わせる → 水平距離が残っていれば前進 → 最後に高度を合わせる。
    高度合わせを最後にするのは、障害物を越えた直後に descend → 再び塞がる → climb と振動するのを防ぐため。
    """
    goal, rays = obs["goal"], obs["rays"]
    if rays["forward"] <= margin:  # 前が塞がっている: 上に抜けるのが最も確実 (球は有限)。無理なら開いている側へ
        if rays["up"] > margin:
            return "climb"
        return "yaw_left" if rays["left"] >= rays["right"] else "yaw_right"
    if abs(goal["bearing_deg"]) > 20:
        return "yaw_right" if goal["bearing_deg"] > 0 else "yaw_left"
    horizontal = math.hypot(goal.get("forward", 0.0), goal.get("right", 0.0))
    if horizontal > 2.0:
        return "forward"
    if goal["up"] > 1.0 and rays["up"] > margin:
        return "climb"
    if goal["up"] < -1.0 and rays["down"] > margin:
        return "descend"
    return "forward"


def decide(jev: Jev, obs: dict[str, Any], hard_margin: float = 1.5, min_confidence: float = 0.3) -> dict[str, Any]:
    override = reflex_override(obs, hard_margin)
    if override:
        return {"action": override["action"], "risk": "collision_imminent", "scale": 1.0, "source": "reflex", "reason": override["reason"], "confidence": 1.0}
    decision = jev.decide(obs, QUESTIONS)
    answer = decision.choice("maneuver")
    risk = decision.score("risk").level
    if answer.confidence < min_confidence:
        action, source = greedy_action(obs), "greedy"
    else:
        action, source = answer.choice, "jev"
    if risk >= 2 and action == "forward" and obs["rays"]["forward"] < 2 * hard_margin:
        action = "hover"  # imminent なのに前進は止める
    return {"action": action, "risk": RISK_LEVELS[risk], "scale": RISK_SCALE[risk], "source": source, "reason": "", "confidence": round(answer.confidence, 3)}


def fly(jev: Jev, drone: KinematicDrone, steps: int = 200, hard_margin: float = 1.5, render: bool = False, out=None) -> dict[str, Any]:
    out = out or sys.stdout
    obs = drone.sense()
    log: list[dict[str, Any]] = []
    start_dist = obs["goal"]["distance"]
    for t in range(steps):
        pick = decide(jev, obs, hard_margin)
        obs, reward, done, info = drone.step(pick["action"], pick["scale"])
        log.append({"t": t, "x": round(drone.x, 2), "y": round(drone.y, 2), "z": round(drone.z, 2), "yaw_deg": round(math.degrees(drone.yaw), 1), "action": pick["action"], "source": pick["source"], "risk": pick["risk"], "reward": round(reward, 2), "goal_distance": info["distance"], "event": info["event"]})
        if render:
            print(f"[{t}] {pick['action']} ({pick['source']}, {pick['risk']}) {pick['reason']}\n{drone.render()}\n", file=out)
        if done:
            break
    return {"steps": len(log), "reached": drone.reached, "crashed": drone.crashed, "start_distance": start_dist, "final_distance": log[-1]["goal_distance"] if log else start_dist, "reflex_overrides": sum(1 for e in log if e["source"] == "reflex"), "jev_calls": jev.total_calls, "log": log}


def write_csv(path: str, log: list[dict[str, Any]]) -> None:
    fields = ["t", "x", "y", "z", "yaw_deg", "action", "source", "risk", "reward", "goal_distance", "event"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: e[k] for k in fields} for e in log)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab drone", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fly", help="障害物のある空間をゴールまで飛ぶ")
    p.add_argument("--steps", type=int, default=200)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--obstacles", type=int, default=14)
    p.add_argument("--hard-margin", type=float, default=1.5, help="この距離未満はリフレックス層が Jev を上書き")
    p.add_argument("--render", action="store_true", help="ASCII 俯瞰図を毎ステップ出力")
    p.add_argument("--csv", default=None, help="軌跡ログの CSV 出力先")
    p.add_argument("--sim", choices=["kinematic", "mujoco"], default="kinematic")
    p.add_argument("--backend", default=None)
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    demo = KinematicDrone.demo(seed=args.seed, n_obstacles=args.obstacles)
    drone = MuJoCoDrone(demo.obstacles, demo.goal, demo.start) if args.sim == "mujoco" else demo
    jev = Jev(args.backend, cache=True)
    result = fly(jev, drone, steps=args.steps, hard_margin=args.hard_margin, render=args.render)
    if args.csv:
        write_csv(args.csv, result["log"])
        print(f"wrote {args.csv}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps({**{k: v for k, v in result.items() if k != "log"}, "jev": jev.stats()}, ensure_ascii=False, indent=2))
        if not args.render:
            print(drone.render())
    return 0


__all__ = ["Sphere", "ray_sphere", "KinematicDrone", "MuJoCoDrone", "build_mjcf", "reflex_override", "greedy_action", "decide", "fly", "write_csv", "MANEUVERS", "RISK_LEVELS", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
