"""jevpilot (standardagents/jevpilot) の再実装。

2D 運動学シミュレータ上で「車線維持 / 車線変更 × 減速 / 維持 / 加速」の軌道候補を作り、
衝突・逸脱する候補を**決定的な安全フィルタで先に落としてから**、残りの中から Jev に選ばせる。
安全は Jev に委ねない。Jev は「安全な候補のどれが良いか」だけを判断する。

- `Track`            : 中心線 (waypoints) + 車線 + 静止障害物 + 制限速度
- `generate_candidates`: 5〜9 本の軌道候補と予測メトリクス
- `safety_filter`    : 衝突/逸脱候補を除去。全滅なら緊急ブレーキ候補だけ残す
- `choose`           : Jev に `candidate` Choice と `comfort` Score を聞く
- `write_html`       : ログを埋め込んだ自己完結の canvas 2D リプレイ HTML

使い方:
    jevlab pilot drive --steps 300 --html replay.html --backend mock
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from jevlab.core import Choice, Jev, Score

DT = 0.5  # 1 ステップの秒数
HORIZON = 8  # 予測点数
LANE_OFFSETS = {"left": -1, "keep": 0, "right": 1}
SPEED_MODES = {"slow": -2.0, "keep": 0.0, "fast": 2.0}
COMFORT_LEVELS = ["smooth", "moderate", "harsh"]


@dataclass
class Ego:
    x: float
    y: float
    heading: float  # rad
    speed: float  # m/s
    lane: int = 0  # 中心線からの車線オフセット (-1, 0, +1)


@dataclass
class Obstacle:
    x: float
    y: float
    radius: float = 1.2
    kind: str = "car"


@dataclass
class Candidate:
    id: str
    lane: int
    speed_mode: str
    target_speed: float
    points: list[tuple[float, float, float]]  # (x, y, heading)
    min_distance_to_obstacle: float
    off_track_ratio: float
    speed_delta: float
    progress: float
    lane_change: bool

    def describe(self) -> str:
        move = "車線維持" if not self.lane_change else ("左へ車線変更" if self.lane < 0 or self.id.startswith("left") else "右へ車線変更")
        return f"{move} / 速度{self.speed_mode} ({self.target_speed:.1f} m/s, Δ{self.speed_delta:+.1f}): 進行 {self.progress:.1f} m, 障害物との最小距離 {self.min_distance_to_obstacle:.1f} m"

    def metrics(self) -> dict[str, Any]:
        return {"lane": self.lane, "speed_mode": self.speed_mode, "min_distance_to_obstacle": round(self.min_distance_to_obstacle, 2), "off_track_ratio": round(self.off_track_ratio, 2), "speed_delta": round(self.speed_delta, 2), "progress": round(self.progress, 2)}


class Track:
    """折れ線の中心線。s (弧長) と横オフセット d で位置を表す Frenet 風の座標を持つ。"""

    def __init__(self, waypoints: list[tuple[float, float]], lane_width: float = 3.5, lanes: int = 3, speed_limit: float = 14.0, obstacles: list[Obstacle] | None = None):
        if len(waypoints) < 2:
            raise ValueError("waypoints は 2 点以上必要です")
        self.waypoints = waypoints
        self.lane_width = lane_width
        self.lanes = lanes
        self.speed_limit = speed_limit
        self.obstacles = list(obstacles or [])
        self.cum = [0.0]
        for (x0, y0), (x1, y1) in zip(waypoints, waypoints[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.cum[-1]

    @classmethod
    def demo(cls, seed: int = 0, length: float = 400.0, n_obstacles: int = 12) -> "Track":
        rng = random.Random(seed)
        waypoints = [(s, 12.0 * math.sin(s / 60.0)) for s in range(0, int(length) + 1, 10)]
        track = cls(waypoints)
        for i in range(n_obstacles):
            s = 40.0 + i * (length - 80.0) / max(1, n_obstacles - 1) + rng.uniform(-5, 5)
            lane = rng.choice([-1, 0, 1])
            x, y, _ = track.position(s, lane * track.lane_width)
            track.obstacles.append(Obstacle(x, y, 1.2, "car"))
        return track

    def position(self, s: float, d: float = 0.0) -> tuple[float, float, float]:
        """弧長 s と横オフセット d (左が負) から (x, y, heading)。"""
        s = max(0.0, min(self.length, s))
        i = max(0, min(len(self.cum) - 2, next((k for k in range(len(self.cum) - 1) if self.cum[k + 1] >= s), len(self.cum) - 2)))
        (x0, y0), (x1, y1) = self.waypoints[i], self.waypoints[i + 1]
        seg = self.cum[i + 1] - self.cum[i]
        t = (s - self.cum[i]) / seg if seg else 0.0
        heading = math.atan2(y1 - y0, x1 - x0)
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        return x + d * math.sin(heading), y - d * math.cos(heading), heading

    def project(self, x: float, y: float) -> tuple[float, float]:
        """(x, y) を中心線に射影して (s, d) を返す。"""
        best = (float("inf"), 0.0, 0.0)
        for i, ((x0, y0), (x1, y1)) in enumerate(zip(self.waypoints, self.waypoints[1:])):
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / seg2)) if seg2 else 0.0
            px, py = x0 + dx * t, y0 + dy * t
            dist = math.hypot(x - px, y - py)
            if dist < best[0]:
                side = math.copysign(1.0, (x - px) * dy - (y - py) * dx)  # 右側が正
                best = (dist, self.cum[i] + math.sqrt(seg2) * t, dist * (side if dist > 1e-9 else 1.0))
        return best[1], best[2]

    @property
    def half_width(self) -> float:
        return self.lanes * self.lane_width / 2.0

    def clearance(self, x: float, y: float) -> float:
        return min((math.hypot(x - o.x, y - o.y) - o.radius for o in self.obstacles), default=float("inf"))


# ---------------------------------------------------------------------------
# 候補生成と安全フィルタ
# ---------------------------------------------------------------------------


def generate_candidates(track: Track, ego: Ego, horizon: int = HORIZON, dt: float = DT) -> list[Candidate]:
    s0, _ = track.project(ego.x, ego.y)
    candidates: list[Candidate] = []
    max_lane = track.lanes // 2
    for lane_name, delta_lane in LANE_OFFSETS.items():
        lane = ego.lane + delta_lane
        if abs(lane) > max_lane:
            continue
        for speed_name, delta_v in SPEED_MODES.items():
            target = max(0.0, min(track.speed_limit, ego.speed + delta_v))
            points, min_dist, off = [], float("inf"), 0
            d_from, d_to = ego.lane * track.lane_width, lane * track.lane_width
            for k in range(1, horizon + 1):
                v = ego.speed + (target - ego.speed) * min(1.0, k / 3)
                s = s0 + sum(ego.speed + (target - ego.speed) * min(1.0, j / 3) for j in range(1, k + 1)) * dt
                blend = min(1.0, k / 4)
                d = d_from + (d_to - d_from) * (3 * blend**2 - 2 * blend**3)  # smoothstep で横移動
                x, y, heading = track.position(s, d)
                points.append((x, y, heading))
                min_dist = min(min_dist, track.clearance(x, y))
                if abs(d) + 0.9 > track.half_width:
                    off += 1
            progress = track.project(*points[-1][:2])[0] - s0
            candidates.append(Candidate(f"{lane_name}_{speed_name}", lane, speed_name, target, points, min_dist, off / horizon, target - ego.speed, progress, delta_lane != 0))
    return candidates


def emergency_candidate(track: Track, ego: Ego, horizon: int = HORIZON, dt: float = DT) -> Candidate:
    s0, _ = track.project(ego.x, ego.y)
    points, v, s = [], ego.speed, s0
    for _ in range(horizon):
        v = max(0.0, v - 4.0 * dt)
        s += v * dt
        points.append(track.position(s, ego.lane * track.lane_width))
    min_dist = min(track.clearance(x, y) for x, y, _ in points)
    return Candidate("emergency_brake", ego.lane, "brake", 0.0, points, min_dist, 0.0, -ego.speed, s - s0, False)


def safety_filter(candidates: list[Candidate], safety_margin: float = 1.0, max_off_track: float = 0.0) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """衝突 (障害物までの距離 < margin) と逸脱の候補を落とす。理由も返す。"""
    kept, rejected = [], []
    for c in candidates:
        if c.min_distance_to_obstacle < safety_margin:
            rejected.append({"id": c.id, "reason": "collision", "min_distance": round(c.min_distance_to_obstacle, 2)})
        elif c.off_track_ratio > max_off_track:
            rejected.append({"id": c.id, "reason": "off_track", "ratio": round(c.off_track_ratio, 2)})
        else:
            kept.append(c)
    return kept, rejected


# ---------------------------------------------------------------------------
# Jev による選択
# ---------------------------------------------------------------------------


def build_state(track: Track, ego: Ego, candidates: list[Candidate]) -> dict[str, Any]:
    s, d = track.project(ego.x, ego.y)
    ahead = sorted(((track.project(o.x, o.y)[0] - s, track.project(o.x, o.y)[1]) for o in track.obstacles if 0 <= track.project(o.x, o.y)[0] - s <= 60), key=lambda p: p[0])[:4]
    return {
        "ego": {"speed": round(ego.speed, 1), "lane": ego.lane, "lateral_offset": round(d, 1), "speed_limit": track.speed_limit, "progress_ratio": round(s / track.length, 2)},
        "obstacles_ahead": [{"distance": round(a, 1), "lane": int(round(b / track.lane_width))} for a, b in ahead],
        "candidates": {c.id: c.metrics() for c in candidates},
    }


def choose(jev: Jev, track: Track, ego: Ego, candidates: list[Candidate], min_confidence: float = 0.25) -> dict[str, Any]:
    """安全フィルタ済み候補から Jev が選ぶ。確信度が低ければ最も余裕のある候補にする。"""
    if not candidates:
        raise ValueError("候補が空です (safety_filter の後に emergency_candidate を足してください)")
    if len(candidates) == 1:
        return {"candidate": candidates[0], "comfort": "smooth", "source": "forced", "confidence": 1.0}
    questions = {
        "candidate": Choice({c.id: c.describe() for c in candidates}, "制限速度内で最も進みつつ、障害物から余裕を保ち、不要な車線変更を避ける候補を選ぶ"),
        "comfort": Score(["smooth: 速度変化も車線変更もない", "moderate: 緩やかな加減速か 1 回の車線変更", "harsh: 急減速や障害物すれすれの車線変更"], "選んだ候補の乗り心地は?"),
    }
    decision = jev.decide(build_state(track, ego, candidates), questions)
    answer = decision.choice("candidate")
    comfort = decision.score("comfort").level
    by_id = {c.id: c for c in candidates}
    if answer.confidence < min_confidence or answer.choice not in by_id:
        chosen, source = max(candidates, key=lambda c: (c.min_distance_to_obstacle, -abs(c.speed_delta))), "fallback"
    else:
        chosen, source = by_id[answer.choice], "jev"
    if comfort >= 2 and chosen.lane_change:  # 乗り心地が harsh と判定された車線変更は、安全な車線維持があればそちらへ
        keep = [c for c in candidates if not c.lane_change]
        if keep:
            chosen, source = max(keep, key=lambda c: c.min_distance_to_obstacle), "comfort_override"
    return {"candidate": chosen, "comfort": COMFORT_LEVELS[comfort], "source": source, "confidence": round(answer.confidence, 3), "latency_ms": round(decision.latency_ms, 2)}


def step(track: Track, ego: Ego, candidate: Candidate) -> Ego:
    x, y, heading = candidate.points[0]
    speed = ego.speed + (candidate.target_speed - ego.speed) * (1.0 if candidate.speed_mode == "brake" else 1 / 3)
    lane = candidate.lane if len(candidate.points) > 0 else ego.lane
    return Ego(x, y, heading, max(0.0, speed), lane)


def drive(jev: Jev, track: Track, steps: int = 300, safety_margin: float = 1.0, ego: Ego | None = None) -> dict[str, Any]:
    ego = ego or Ego(*track.position(0.0, 0.0)[:2], track.position(0.0)[2], 8.0, 0)
    log: list[dict[str, Any]] = []
    collisions = 0
    for t in range(steps):
        cands = generate_candidates(track, ego)
        safe, rejected = safety_filter(cands, safety_margin)
        if not safe:
            safe = [emergency_candidate(track, ego)]
        pick = choose(jev, track, ego, safe)
        chosen: Candidate = pick["candidate"]
        ego = step(track, ego, chosen)
        if track.clearance(ego.x, ego.y) < 0:
            collisions += 1
        s, _ = track.project(ego.x, ego.y)
        log.append({"t": t, "x": round(ego.x, 2), "y": round(ego.y, 2), "heading": round(ego.heading, 3), "speed": round(ego.speed, 2), "lane": ego.lane, "chosen": chosen.id, "source": pick["source"], "comfort": pick["comfort"], "n_safe": len(safe), "rejected": rejected, "path": [(round(px, 1), round(py, 1)) for px, py, _ in chosen.points]})
        if s >= track.length - 1.0:
            break
    return {"steps": len(log), "progress": round(track.project(ego.x, ego.y)[0], 1), "track_length": round(track.length, 1), "collisions": collisions, "finished": track.project(ego.x, ego.y)[0] >= track.length - 1.0, "jev_calls": jev.total_calls, "log": log}


# ---------------------------------------------------------------------------
# HTML リプレイ (canvas 2D, 依存なし)
# ---------------------------------------------------------------------------

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>jevpilot replay</title>
<style>body{font-family:system-ui,sans-serif;margin:16px;background:#111;color:#ddd}canvas{background:#1b1b1b;border:1px solid #444;max-width:100%}#info{font-size:13px;white-space:pre}</style></head>
<body><h3>jevpilot replay</h3>
<input id="t" type="range" min="0" value="0" style="width:60%"> <button id="play">play</button>
<canvas id="c" width="1000" height="400"></canvas><div id="info"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);const log=D.log,W=D.track.waypoints,O=D.track.obstacles,hw=D.track.half_width;
const c=document.getElementById('c'),g=c.getContext('2d'),sl=document.getElementById('t'),info=document.getElementById('info');sl.max=Math.max(0,log.length-1);
const xs=W.map(p=>p[0]),ys=W.map(p=>p[1]);const minX=Math.min(...xs)-10,maxX=Math.max(...xs)+10,minY=Math.min(...ys)-hw-10,maxY=Math.max(...ys)+hw+10;
const sc=Math.min(c.width/(maxX-minX),c.height/(maxY-minY));const X=x=>(x-minX)*sc,Y=y=>c.height-(y-minY)*sc;
function draw(i){g.clearRect(0,0,c.width,c.height);g.lineWidth=hw*2*sc;g.strokeStyle='#333';g.lineJoin='round';g.beginPath();W.forEach((p,k)=>k?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1])));g.stroke();
g.lineWidth=1;g.strokeStyle='#666';g.setLineDash([6,6]);g.beginPath();W.forEach((p,k)=>k?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1])));g.stroke();g.setLineDash([]);
O.forEach(o=>{g.fillStyle='#c0392b';g.beginPath();g.arc(X(o.x),Y(o.y),o.radius*sc,0,7);g.fill()});
g.strokeStyle='#2ecc71';g.lineWidth=2;g.beginPath();log.slice(0,i+1).forEach((e,k)=>k?g.lineTo(X(e.x),Y(e.y)):g.moveTo(X(e.x),Y(e.y)));g.stroke();
const e=log[i];if(!e)return;g.strokeStyle='#f1c40f';g.beginPath();e.path.forEach((p,k)=>k?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1])));g.stroke();
g.save();g.translate(X(e.x),Y(e.y));g.rotate(-e.heading);g.fillStyle='#3498db';g.fillRect(-2*sc,-1*sc,4*sc,2*sc);g.restore();
info.textContent=`t=${e.t} speed=${e.speed} lane=${e.lane} chosen=${e.chosen} (${e.source}, comfort=${e.comfort}) safe=${e.n_safe} rejected=${e.rejected.map(r=>r.id+':'+r.reason).join(' ')||'-'}`;}
sl.oninput=()=>draw(+sl.value);let timer=null;document.getElementById('play').onclick=()=>{if(timer){clearInterval(timer);timer=null;return}timer=setInterval(()=>{sl.value=(+sl.value+1)%log.length;draw(+sl.value)},120)};draw(0);
</script></body></html>
"""


def write_html(path: str, track: Track, result: dict[str, Any]) -> str:
    data = {"track": {"waypoints": track.waypoints, "obstacles": [asdict(o) for o in track.obstacles], "half_width": track.half_width, "lane_width": track.lane_width}, "log": result["log"], "summary": {k: v for k, v in result.items() if k != "log"}}
    html = _HTML.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab pilot", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("drive", help="内蔵トラックを走らせ、必要なら HTML リプレイを書き出す")
    p.add_argument("--steps", type=int, default=300)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--obstacles", type=int, default=12)
    p.add_argument("--safety-margin", type=float, default=1.0)
    p.add_argument("--html", default=None, help="リプレイ HTML の出力先")
    p.add_argument("--backend", default=None)
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    jev = Jev(args.backend, cache=True)
    track = Track.demo(seed=args.seed, n_obstacles=args.obstacles)
    result = drive(jev, track, steps=args.steps, safety_margin=args.safety_margin)
    if args.html:
        write_html(args.html, track, result)
        print(f"wrote {args.html}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps({**{k: v for k, v in result.items() if k != "log"}, "jev": jev.stats()}, ensure_ascii=False, indent=2))
    return 0


__all__ = ["Ego", "Obstacle", "Candidate", "Track", "generate_candidates", "emergency_candidate", "safety_filter", "build_state", "choose", "step", "drive", "write_html", "main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
