"""typesafe-mario (fhshaik/typesafe-mario) の再実装。

ゲーム状態 (Mario の位置・敵・穴・ブロック) を小さな構造化 JSON にして Jev に渡し、
1 つの Choice (`action`) と 1 つの Score (`danger`) で次の行動を決める。

- `GameState`     : Jev に渡す状態。実機 (NES RAM) でも内蔵シミュレータでも同じ形にする
- `MiniMario`     : 依存ゼロの決定的サイドスクローラ (格子, 重力, ジャンプ弧, 歩く goomba, 穴)
- `controller`    : Jev に action / danger を聞き、danger で速度とホールド (再質問間隔) を調整する
- `decode_smb_ram`: Super Mario Bros. (NES) の RAM スナップショット (bytes) から GameState を作る
- `NesPyAdapter` / `PyBoyAdapter` : 実エミュレータへの遅延スタブ

使い方:
    jevlab mario play --steps 200 --render --jev-every 1 --backend mock
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from jevlab.core import Choice, Jev, Score

ACTIONS: dict[str, str] = {
    "left": "1 マス左へ戻る。敵や穴から距離を取りたいときだけ",
    "right": "1 マス右へ歩く。安全に前進する基本行動",
    "jump": "その場で垂直ジャンプ。真上のブロックを叩く/頭上の敵をやり過ごす",
    "jump_right": "右へ 1 マス進みながらジャンプ。穴 (幅 5 以下) や目前の敵を飛び越える",
    "run_right": "右へ 2 マス走る。前方 6 マス以内に穴や敵がないときだけ速い",
    "wait": "その場で待つ。敵が通り過ぎるのを待つ/落下を待つ",
}
DANGER_LEVELS = ["safe", "caution", "imminent"]
LOOKAHEAD = 12
JUMP_VELOCITY = 3


# ---------------------------------------------------------------------------
# 状態
# ---------------------------------------------------------------------------


@dataclass
class GameState:
    """Jev に渡す最小限の状態。座標は Mario 基準の相対値 (dx, dy)。"""

    mario_x: int
    mario_y: int
    on_ground: bool
    velocity: dict[str, int] = field(default_factory=lambda: {"vx": 0, "vy": 0})
    enemies: list[dict[str, Any]] = field(default_factory=list)  # {dx, dy, kind}
    holes: list[int] = field(default_factory=list)  # dx (右方向の穴の列)
    blocks: list[dict[str, int]] = field(default_factory=list)  # {dx, dy}
    score: int = 0
    time_left: int = 400
    lives: int = 3

    def to_jev(self) -> dict[str, Any]:
        nearest_enemy = min((e["dx"] for e in self.enemies if e["dx"] >= 0), default=None)
        nearest_hole = min((h for h in self.holes if h >= 0), default=None)
        return {
            "mario": {"x": self.mario_x, "y": self.mario_y, "on_ground": self.on_ground, **self.velocity},
            "enemies_ahead": self.enemies,
            "holes_ahead_dx": self.holes,
            "blocks": self.blocks,
            "nearest_enemy_dx": nearest_enemy,
            "nearest_hole_dx": nearest_hole,
            "score": self.score,
            "time_left": self.time_left,
            "lives": self.lives,
        }


# ---------------------------------------------------------------------------
# 内蔵シミュレータ
# ---------------------------------------------------------------------------


class MiniMario:
    """格子ベースの決定的サイドスクローラ。

    - 地面は y=0。`holes` の列には地面がない
    - 重力: 空中では毎ステップ vy -= 1。ジャンプは vy = 3 (上昇 3,2,1 → 下降 1,2,3 で 7 ステップ)
    - goomba は毎ステップ 1 マス左へ歩き、穴に落ちると消える
    - Mario が落下中に goomba と同じマスに入れば踏みつけ (+100)。それ以外の接触は 1 ミス
    - y <= -3 まで落ちたら穴に落ちたとみなし 1 ミス。ミス後は直前の安全な地面に戻る
    """

    def __init__(self, width: int = 120, seed: int = 0, holes: list[int] | None = None, enemies: list[int] | None = None, blocks: list[tuple[int, int]] | None = None, time_limit: int = 400, lives: int = 3):
        self.width = width
        self.time_limit = time_limit
        rng = random.Random(seed)
        if holes is None:
            holes = []
            x = 14
            while x < width - 10:
                span = rng.choice([1, 2, 2, 3])
                holes.extend(range(x, x + span))
                x += span + rng.randint(8, 16)
        if enemies is None:
            enemies = [x for x in range(10, width - 6, rng.randint(9, 14)) if x not in holes and (x + 1) not in holes]
        if blocks is None:
            blocks = [(x, 3) for x in range(20, width - 10, 23) if x not in holes]
        self.holes = set(holes)
        self.blocks = set(blocks)
        self.initial_enemies = list(enemies)
        self.lives_start = lives
        self.reset()

    def reset(self) -> GameState:
        self.x, self.y, self.vx, self.vy = 2, 0, 0, 0
        self.goombas = [{"x": x, "y": 0, "alive": True} for x in self.initial_enemies]
        self.score = 0
        self.time_left = self.time_limit
        self.lives = self.lives_start
        self.safe_x = 2
        self.done = False
        self.won = False
        self.events: list[str] = []
        return self.observe()

    # -- 物理 --------------------------------------------------------------

    def _support(self, x: int, y: int) -> bool:
        if y == 0:
            return x not in self.holes
        return (x, y - 1) in self.blocks

    @property
    def on_ground(self) -> bool:
        return self.y >= 0 and self._support(self.x, self.y)

    def step(self, action: str) -> tuple[GameState, float, bool]:
        if self.done:
            return self.observe(), 0.0, True
        if action not in ACTIONS:
            raise ValueError(f"unknown action: {action}")
        self.events = []
        reward = 0.0
        old_x = self.x
        vx = {"left": -1, "right": 1, "run_right": 2, "jump_right": 1}.get(action, 0)
        grounded = self.on_ground
        if grounded and action in ("jump", "jump_right"):
            self.vy = JUMP_VELOCITY
        elif grounded:
            self.vy = 0
        self.vx = vx
        new_x = max(0, min(self.width, self.x + vx))
        # 縦移動: 上昇中は頭上ブロックで止まり、下降中は足元の支えで着地する
        new_y = self.y + self.vy
        if self.vy > 0:
            for ty in range(self.y + 1, new_y + 1):
                if (new_x, ty) in self.blocks:
                    new_y = ty - 1
                    self.vy = 0
                    self.events.append("bonk")
                    break
        else:
            for ty in range(self.y, new_y - 1, -1):
                if self._support(new_x, ty):
                    new_y = ty
                    self.vy = 0
                    break
        descended = new_y < self.y  # このステップで下に動いた = 踏みつけ判定に使う
        self.x, self.y = new_x, new_y
        if not self.on_ground:
            self.vy -= 1
        else:
            self.vy = 0
            self.safe_x = self.x
        # 敵の移動と接触
        for goomba in self.goombas:
            if not goomba["alive"]:
                continue
            goomba["x"] -= 1
            if goomba["x"] in self.holes or goomba["x"] < 0:
                goomba["alive"] = False
                continue
            if goomba["x"] == self.x and goomba["y"] == self.y:
                if descended:
                    goomba["alive"] = False
                    self.score += 100
                    reward += 100
                    self.events.append("stomp")
                else:
                    reward += self._lose_life("hit")
        if self.y <= -3:
            reward += self._lose_life("fell")
        reward += (self.x - old_x) * 1.0
        self.time_left -= 1
        if self.x >= self.width:
            self.won = True
            self.done = True
            self.score += self.time_left
            reward += 1000
            self.events.append("goal")
        if self.time_left <= 0 and not self.done:
            self.done = True
            self.events.append("timeout")
        return self.observe(), reward, self.done

    def _lose_life(self, why: str) -> float:
        self.lives -= 1
        self.events.append(why)
        self.x, self.y, self.vx, self.vy = self.safe_x, 0, 0, 0
        if self.lives <= 0:
            self.done = True
        return -50.0

    # -- 観測と描画 --------------------------------------------------------

    def observe(self) -> GameState:
        holes = sorted(h - self.x for h in self.holes if -2 <= h - self.x <= LOOKAHEAD)
        enemies = [{"dx": g["x"] - self.x, "dy": g["y"] - self.y, "kind": "goomba"} for g in self.goombas if g["alive"] and -3 <= g["x"] - self.x <= LOOKAHEAD]
        blocks = [{"dx": bx - self.x, "dy": by - self.y} for bx, by in sorted(self.blocks) if -2 <= bx - self.x <= LOOKAHEAD]
        return GameState(self.x, self.y, self.on_ground, {"vx": self.vx, "vy": self.vy}, enemies, holes, blocks, self.score, self.time_left, self.lives)

    def render(self, window: int = 32) -> str:
        left = max(0, self.x - 8)
        rows = []
        for y in range(6, -1, -1):
            row = []
            for x in range(left, left + window):
                if x == self.x and y == self.y:
                    row.append("M")
                elif any(g["alive"] and g["x"] == x and g["y"] == y for g in self.goombas):
                    row.append("g")
                elif (x, y) in self.blocks:
                    row.append("#")
                elif x == self.width and y <= 4:
                    row.append("F")
                else:
                    row.append(" ")
            rows.append("".join(row))
        rows.append("".join(" " if x in self.holes else "=" for x in range(left, left + window)))
        rows.append(f"x={self.x} y={self.y} lives={self.lives} score={self.score} t={self.time_left} {' '.join(self.events)}")
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Jev コントローラ
# ---------------------------------------------------------------------------

QUESTIONS = {
    "action": Choice(ACTIONS, "Mario の次の 1 手を選ぶ。右に進むほど良いが、穴に落ちる/敵に正面から当たるのは最悪"),
    "danger": Score(
        ["safe: 前方 6 マス以内に穴も敵もない", "caution: 4〜6 マス先に穴か敵がある", "imminent: 3 マス以内に穴か敵があり、今すぐ跳ぶか止まる必要がある"],
        "現在の危険度は?",
    ),
}


def reflex_action(state: GameState) -> str:
    """Jev の確信度が低いときの決定的フォールバック。"""
    hole = min((h for h in state.holes if h >= 0), default=None)
    enemy = min((e["dx"] for e in state.enemies if e["dx"] >= 0 and e["dy"] == 0), default=None)
    if not state.on_ground:
        return "right"
    if hole is not None and hole <= 2:
        return "jump_right"
    if enemy is not None and enemy <= 3:
        return "jump_right"
    if (hole is None or hole > 6) and (enemy is None or enemy > 6):
        return "run_right"
    return "right"


def apply_danger_policy(action: str, danger_level: int, jev_every: int) -> tuple[str, int]:
    """danger で速度 (run → walk → jump) と、次に Jev へ聞くまでのホールド数を調整する。"""
    if danger_level >= 2:
        if action in ("run_right", "right"):
            action = "jump_right"
        return action, 1
    if danger_level == 1:
        if action == "run_right":
            action = "right"
        return action, max(1, min(jev_every, 2))
    return action, max(1, jev_every)


def controller(jev: Jev, state: GameState, jev_every: int = 1, min_confidence: float = 0.3) -> dict[str, Any]:
    """1 回 Jev に聞いて {action, danger, hold, source} を返す。"""
    decision = jev.decide(state.to_jev(), QUESTIONS)
    choice = decision.choice("action")
    danger = decision.score("danger").level
    if choice.confidence < min_confidence:
        action, source = reflex_action(state), "reflex"
    else:
        action, source = choice.choice, "jev"
    action, hold = apply_danger_policy(action, danger, jev_every)
    return {"action": action, "danger": DANGER_LEVELS[danger], "hold": hold, "source": source, "confidence": round(choice.confidence, 3), "latency_ms": round(decision.latency_ms, 2)}


def play(jev: Jev, sim: MiniMario, steps: int = 200, jev_every: int = 1, render: bool = False, out=None) -> dict[str, Any]:
    out = out or sys.stdout
    state = sim.observe()
    log: list[dict[str, Any]] = []
    hold, current = 0, None
    total_reward = 0.0
    for t in range(steps):
        if hold <= 0 or current is None:
            current = controller(jev, state, jev_every)
            hold = current["hold"]
        state, reward, done = sim.step(current["action"])
        hold -= 1
        total_reward += reward
        log.append({"t": t, "action": current["action"], "danger": current["danger"], "source": current["source"], "x": sim.x, "y": sim.y, "reward": reward, "events": list(sim.events)})
        if render:
            print(f"[{t}] {current['action']} ({current['danger']}, {current['source']})\n{sim.render()}\n", file=out)
        if done:
            break
    return {"steps": len(log), "x": sim.x, "score": sim.score, "lives": sim.lives, "won": sim.won, "total_reward": total_reward, "jev_calls": jev.total_calls, "log": log}


# ---------------------------------------------------------------------------
# 実機アダプタ (RAM デコード)
# ---------------------------------------------------------------------------

SMB_RAM = {
    "player_page": 0x006D,  # 画面ページ (x = page*256 + screen_x)
    "player_screen_x": 0x0086,
    "player_y": 0x00CE,  # 画面上の y (下向き正)。地面は概ね 0xB0
    "player_float": 0x001D,  # 0 なら接地
    "player_vx": 0x0057,  # 符号付き
    "player_vy": 0x009F,  # 符号付き (下向き正)
    "enemy_drawn": 0x000F,  # 5 スロット 0x000F..0x0013
    "enemy_type": 0x0016,  # 5 スロット 0x0016..0x001A
    "enemy_page": 0x006E,
    "enemy_screen_x": 0x0087,
    "enemy_y": 0x00CF,
    "lives": 0x075A,
    "score": 0x07DD,  # 6 桁 BCD (1 バイト 1 桁)
    "time": 0x07F8,  # 3 桁
}
SMB_ENEMY_KINDS = {0x00: "green_koopa", 0x01: "red_koopa", 0x02: "buzzy_beetle", 0x03: "hammer_bro", 0x06: "goomba", 0x0E: "piranha_plant", 0x12: "bullet_bill"}
SMB_GROUND_Y = 0xB0
SMB_TILE = 16


def _signed(b: int) -> int:
    return b - 256 if b > 127 else b


def decode_smb_ram(ram: bytes | bytearray | memoryview, ground_y: int = SMB_GROUND_Y) -> GameState:
    """SMB (NES) の 2KB RAM スナップショットから GameState を作る。座標は 16px タイル単位。

    穴の位置は RAM の地形バッファ (0x0500〜) の解釈が要るため、ここでは空にしている。
    実機で使うときは gym-super-mario-bros の info['x_pos'] と併用するか、タイルバッファを別途読むこと。
    """
    ram = bytes(ram)
    if len(ram) < 0x800:
        raise ValueError(f"SMB RAM は 2048 バイト必要です (got {len(ram)})")
    px = ram[SMB_RAM["player_page"]] * 256 + ram[SMB_RAM["player_screen_x"]]
    py_px = ram[SMB_RAM["player_y"]]
    mario_x, mario_y = px // SMB_TILE, max(0, (ground_y - py_px) // SMB_TILE)
    enemies = []
    for slot in range(5):
        if ram[SMB_RAM["enemy_drawn"] + slot] == 0:
            continue
        ex = ram[SMB_RAM["enemy_page"] + slot] * 256 + ram[SMB_RAM["enemy_screen_x"] + slot]
        ey = max(0, (ground_y - ram[SMB_RAM["enemy_y"] + slot]) // SMB_TILE)
        kind = SMB_ENEMY_KINDS.get(ram[SMB_RAM["enemy_type"] + slot], f"enemy_{ram[SMB_RAM['enemy_type'] + slot]:02x}")
        enemies.append({"dx": ex // SMB_TILE - mario_x, "dy": ey - mario_y, "kind": kind})
    score = int("".join(str(d) for d in ram[SMB_RAM["score"] : SMB_RAM["score"] + 6]) or 0)
    time_left = int("".join(str(d) for d in ram[SMB_RAM["time"] : SMB_RAM["time"] + 3]) or 0)
    return GameState(
        mario_x=mario_x,
        mario_y=mario_y,
        on_ground=ram[SMB_RAM["player_float"]] == 0,
        velocity={"vx": _signed(ram[SMB_RAM["player_vx"]]), "vy": -_signed(ram[SMB_RAM["player_vy"]])},
        enemies=enemies,
        holes=[],
        blocks=[],
        score=score,
        time_left=time_left,
        lives=ram[SMB_RAM["lives"]] + 1,
    )


class NesPyAdapter:
    """gym-super-mario-bros (nes_py) 用。`pip install gym-super-mario-bros nes-py` が必要。

    env.ram (2KB) を `decode_smb_ram` に通す。行動は SIMPLE_MOVEMENT のインデックスへ写像する。
    """

    ACTION_MAP = {"wait": 0, "right": 1, "jump_right": 2, "run_right": 3, "jump": 5, "left": 6}

    def __init__(self, level: str = "SuperMarioBros-1-1-v0"):
        try:
            import gym_super_mario_bros  # type: ignore
            from gym_super_mario_bros.actions import SIMPLE_MOVEMENT  # type: ignore
            from nes_py.wrappers import JoypadSpace  # type: ignore
        except ImportError as error:  # pragma: no cover - 実機依存
            raise ImportError("NesPyAdapter には gym-super-mario-bros と nes-py が必要です: pip install gym-super-mario-bros nes-py") from error
        self.env = JoypadSpace(gym_super_mario_bros.make(level), SIMPLE_MOVEMENT)
        self.env.reset()

    def read_state(self) -> GameState:  # pragma: no cover
        return decode_smb_ram(self.env.unwrapped.ram)

    def step(self, action: str, repeat: int = 4) -> tuple[GameState, float, bool]:  # pragma: no cover
        total, done = 0.0, False
        for _ in range(repeat):
            _, reward, done, _ = self.env.step(self.ACTION_MAP.get(action, 0))
            total += reward
            if done:
                break
        return self.read_state(), total, done


class PyBoyAdapter:
    """PyBoy (Game Boy: Super Mario Land) 用スタブ。`pip install pyboy` が必要。

    Super Mario Land の代表的アドレス (WRAM): 0xC202 = Mario X (画面内), 0xC201 = Mario Y,
    0xDA15 = 残機, 0xC0AB = 空中フラグ。作品ごとに違うので `addresses` で上書きできるようにしてある。
    """

    DEFAULT_ADDRESSES = {"x": 0xC202, "y": 0xC201, "lives": 0xDA15, "airborne": 0xC0AB}

    def __init__(self, rom_path: str, addresses: dict[str, int] | None = None):
        try:
            from pyboy import PyBoy  # type: ignore
        except ImportError as error:  # pragma: no cover
            raise ImportError("PyBoyAdapter には pyboy が必要です: pip install pyboy") from error
        self.pyboy = PyBoy(rom_path, window="null")
        self.addresses = {**self.DEFAULT_ADDRESSES, **(addresses or {})}

    def read_state(self) -> GameState:  # pragma: no cover
        mem = self.pyboy.memory
        a = self.addresses
        return GameState(mario_x=mem[a["x"]] // 8, mario_y=max(0, (0x90 - mem[a["y"]]) // 8), on_ground=mem[a["airborne"]] == 0, lives=mem[a["lives"]])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab mario", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("play", help="内蔵シミュレータで Jev に Mario を操作させる")
    p.add_argument("--steps", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jev-every", type=int, default=1, help="安全なときは N ステップに 1 回だけ Jev に聞く")
    p.add_argument("--render", action="store_true", help="ASCII で毎ステップ描画")
    p.add_argument("--backend", default=None)
    p.add_argument("--json", action="store_true", help="ログを JSON で出力")
    args = parser.parse_args(argv)
    jev = Jev(args.backend, cache=True)
    sim = MiniMario(seed=args.seed)
    result = play(jev, sim, steps=args.steps, jev_every=args.jev_every, render=args.render)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        summary = {k: v for k, v in result.items() if k != "log"}
        print(json.dumps({**summary, "jev": jev.stats()}, ensure_ascii=False, indent=2))
    return 0


__all__ = ["GameState", "MiniMario", "controller", "play", "decode_smb_ram", "reflex_action", "apply_danger_policy", "NesPyAdapter", "PyBoyAdapter", "ACTIONS", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
