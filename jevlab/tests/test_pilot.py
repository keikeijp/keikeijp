import json
import math

from jevlab.apps import pilot
from jevlab.apps.pilot import Ego, Obstacle, Track, choose, drive, emergency_candidate, generate_candidates, safety_filter, write_html
from jevlab.core import Jev, ScriptedBackend


def straight_track(**kw) -> Track:
    return Track([(0.0, 0.0), (100.0, 0.0), (200.0, 0.0)], **kw)


def test_track_projection_and_position_roundtrip():
    track = straight_track()
    x, y, heading = track.position(50.0, 3.5)
    assert (round(x, 6), round(y, 6), heading) == (50.0, -3.5, 0.0)
    s, d = track.project(x, y)
    assert abs(s - 50.0) < 1e-6 and abs(d - 3.5) < 1e-6
    assert track.clearance(10.0, 0.0) == float("inf")


def test_candidate_generation_counts_and_metrics():
    track = straight_track()
    ego = Ego(0.0, 0.0, 0.0, 8.0, 0)
    cands = generate_candidates(track, ego)
    assert len(cands) == 9  # 中央車線: 3 車線 × 3 速度
    ids = {c.id for c in cands}
    assert {"keep_keep", "left_fast", "right_slow"} <= ids
    fast = next(c for c in cands if c.id == "keep_fast")
    slow = next(c for c in cands if c.id == "keep_slow")
    assert fast.progress > slow.progress and fast.speed_delta == 2.0 and slow.speed_delta == -2.0
    assert all(c.off_track_ratio == 0.0 for c in cands)
    edge = generate_candidates(track, Ego(0.0, -3.5, 0.0, 8.0, -1))
    assert len(edge) == 6 and not any(c.id.startswith("left") for c in edge)  # 左端の車線では左へ行けない
    assert all(c.describe() for c in cands)


def test_safety_filter_removes_collisions_and_off_track():
    track = straight_track(obstacles=[Obstacle(20.0, 0.0, 1.2)])
    ego = Ego(0.0, 0.0, 0.0, 8.0, 0)
    cands = generate_candidates(track, ego)
    safe, rejected = safety_filter(cands, safety_margin=1.0)
    assert {r["id"] for r in rejected if r["reason"] == "collision"} >= {"keep_keep", "keep_fast"}
    assert all(c.min_distance_to_obstacle >= 1.0 for c in safe)
    assert all(not c.id.startswith("keep") or c.id == "keep_slow" and c.min_distance_to_obstacle >= 1.0 for c in safe)
    wide = generate_candidates(Track([(0.0, 0.0), (200.0, 0.0)], lanes=1), Ego(0.0, 0.0, 0.0, 8.0, 0))
    safe2, rejected2 = safety_filter(wide)
    assert [c.id for c in safe2] == ["keep_slow", "keep_keep", "keep_fast"]
    assert all(r["reason"] == "off_track" for r in rejected2)
    brake = emergency_candidate(track, ego)
    assert brake.target_speed == 0.0 and brake.progress < 8.0 * pilot.DT * pilot.HORIZON


def test_choose_with_scripted_backend_and_fallbacks():
    track = straight_track()
    ego = Ego(0.0, 0.0, 0.0, 8.0, 0)
    safe, _ = safety_filter(generate_candidates(track, ego))
    backend = ScriptedBackend([{"candidate": "keep_fast", "comfort": 0}, {"candidate": "left_fast", "comfort": 2}])
    jev = Jev(backend)
    pick = choose(jev, track, ego, safe)
    assert pick["candidate"].id == "keep_fast" and pick["source"] == "jev" and pick["comfort"] == "smooth"
    state = backend.calls[0]["state"]
    assert set(state) == {"ego", "obstacles_ahead", "candidates"} and "keep_fast" in state["candidates"]
    # harsh な車線変更は車線維持へ差し替える
    pick = choose(jev, track, ego, safe)
    assert pick["source"] == "comfort_override" and not pick["candidate"].lane_change
    # 低確信度 → 最も余裕のある候補
    low = ScriptedBackend([{"candidate": {c.id: 1 / len(safe) for c in safe}, "comfort": 1}])
    pick = choose(Jev(low), track, ego, safe, min_confidence=0.5)
    assert pick["source"] == "fallback"
    nxt = pilot.step(track, ego, safe[0])
    assert nxt.x > ego.x


def test_drive_one_step_and_html_embed(tmp_path):
    track = straight_track(obstacles=[Obstacle(30.0, 0.0)])
    jev = Jev(ScriptedBackend(default={"candidate": "keep_fast", "comfort": 0}))
    result = drive(jev, track, steps=3)
    assert result["steps"] == 3 and result["collisions"] == 0 and len(result["log"]) == 3
    assert result["log"][0]["chosen"] in {"keep_fast", "left_fast", "right_fast", "keep_keep", "keep_slow", "left_keep", "right_keep", "left_slow", "right_slow"}
    out = tmp_path / "replay.html"
    html = write_html(str(out), track, result)
    assert out.exists() and "<canvas" in html and "cdnjs" not in html
    start = html.index('<script id="data" type="application/json">') + len('<script id="data" type="application/json">')
    data = json.loads(html[start : html.index("</script>", start)])
    assert len(data["log"]) == 3 and data["track"]["obstacles"][0]["x"] == 30.0


def test_main_drive_mock(tmp_path, capsys):
    out = tmp_path / "r.html"
    assert pilot.main(["drive", "--steps", "20", "--backend", "mock", "--html", str(out), "--seed", "1"]) == 0
    assert out.exists()
    printed = capsys.readouterr().out
    assert '"collisions": 0' in printed
    demo = Track.demo(seed=2)
    assert demo.length > 300 and len(demo.obstacles) == 12
    assert all(abs(demo.project(o.x, o.y)[1]) <= demo.half_width for o in demo.obstacles)
    assert math.isfinite(demo.position(demo.length + 5)[0])
