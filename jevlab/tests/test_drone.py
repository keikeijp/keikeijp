import csv
import math

from jevlab.apps import drone
from jevlab.apps.drone import KinematicDrone, Sphere, build_mjcf, decide, fly, greedy_action, ray_sphere, reflex_override
from jevlab.core import FunctionBackend, Jev, ScriptedBackend


def test_ray_sphere_and_sensor_directions():
    sphere = Sphere(5.0, 0.0, 0.0, 1.0)
    assert ray_sphere((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), sphere) == 4.0
    assert ray_sphere((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), sphere) is None
    assert ray_sphere((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), sphere) is None
    d = KinematicDrone([sphere, Sphere(0.0, 0.0, 6.0, 1.0)], goal=(20.0, 0.0, 2.0), start=(0.0, 0.0, 2.0), bounds=(40.0, 40.0, 12.0))
    d.yaw = 0.0
    obs = d.sense()
    assert obs["rays"]["forward"] == 10.0  # 球は z=0 にあり z=2 のレイは 半径 1 の球に当たらない
    assert obs["rays"]["up"] == 3.0  # 上の球 (中心 z=6, r=1) まで
    assert obs["rays"]["down"] == 2.0  # 地面
    assert obs["rays"]["back"] == 0.0  # 外壁 (x=0)
    assert obs["goal"] == {"forward": 20.0, "right": 0.0, "up": 0.0, "distance": 20.0, "bearing_deg": 0.0}
    d.yaw = math.pi / 2  # 北を向く: ゴールは右 90 度
    assert d.sense()["goal"]["bearing_deg"] == 90.0
    assert d.sense()["rays"]["right"] == 10.0  # +x 方向: z=2 のレイは z=0 の球 (r=1) に当たらない
    assert d.sense()["nearest"]["direction"] == "down"  # 外壁 (x=0, y=0) は最寄り判定に含めない


def test_reflex_override_only_below_margin_and_moves_away():
    clear = {"rays": {"forward": 5, "back": 5, "left": 5, "right": 5, "up": 5, "down": 5}, "nearest": {"direction": "down", "distance": 5}, "goal": {}}
    assert reflex_override(clear, 1.5) is None
    low = {"rays": {"forward": 5, "back": 5, "left": 5, "right": 5, "up": 5, "down": 0.8}, "nearest": {"direction": "down", "distance": 0.8}, "goal": {}}
    assert reflex_override(low, 1.5)["action"] == "climb"
    wall = {"rays": {"forward": 1.0, "back": 6, "left": 5, "right": 5, "up": 2.0, "down": 5}, "nearest": {"direction": "forward", "distance": 1.0}, "goal": {}}
    assert reflex_override(wall, 1.5)["action"] == "back"
    side = {"rays": {"forward": 4, "back": 4, "left": 0.5, "right": 5, "up": 9, "down": 5}, "nearest": {"direction": "left", "distance": 0.5}, "goal": {}}
    assert reflex_override(side, 1.5)["action"] == "climb"
    # Jev が forward と言っても、リフレックス層が上書きする
    backend = ScriptedBackend(default={"maneuver": "forward", "risk": 0})
    pick = decide(Jev(backend), low, hard_margin=1.5)
    assert pick["source"] == "reflex" and pick["action"] == "climb" and backend.calls == []
    pick = decide(Jev(backend), {**clear, "goal": {"forward": 5, "right": 0, "up": 0, "distance": 5, "bearing_deg": 0}}, hard_margin=1.5)
    assert pick["source"] == "jev" and pick["action"] == "forward" and pick["scale"] == 1.0
    imminent = ScriptedBackend(default={"maneuver": "forward", "risk": 2})
    obs = {"rays": {"forward": 2.0, "back": 5, "left": 5, "right": 5, "up": 5, "down": 5}, "nearest": {"direction": "forward", "distance": 2.0}, "goal": {"forward": 5, "right": 0, "up": 0, "distance": 5, "bearing_deg": 0}}
    assert decide(Jev(imminent), obs, hard_margin=1.5)["action"] == "hover"


def test_scripted_flight_reaches_goal_in_empty_world():
    d = KinematicDrone([], goal=(10.0, 0.0, 2.0), start=(0.0, 0.0, 2.0))
    d.yaw = 0.0
    jev = Jev(ScriptedBackend(default={"maneuver": "forward", "risk": 0}))
    result = fly(jev, d, steps=50)
    assert result["reached"] and not result["crashed"] and result["steps"] == 9
    assert result["log"][-1]["event"] == "goal" and result["final_distance"] <= 1.5
    # 前が塞がっていれば greedy は climb を選び、ゴールが右なら yaw_right
    blocked = {"rays": {"forward": 1.0, "back": 5, "left": 5, "right": 5, "up": 5, "down": 5}, "goal": {"forward": 5, "right": 0, "up": 0, "distance": 5, "bearing_deg": 0}}
    assert greedy_action(blocked) == "climb"
    assert greedy_action({"rays": {k: 9 for k in drone.RAY_DIRS}, "goal": {"forward": 5, "right": 5, "up": 0, "distance": 7, "bearing_deg": 45}}) == "yaw_right"


def test_greedy_flight_in_demo_world_approaches_goal_and_writes_csv(tmp_path):
    d = KinematicDrone.demo(seed=1)
    jev = Jev(FunctionBackend(lambda state, q: {"maneuver": greedy_action(state), "risk": 0}))
    result = fly(jev, d, steps=200)
    assert not result["crashed"]
    assert result["reached"] or result["final_distance"] < result["start_distance"] / 2
    path = tmp_path / "log.csv"
    drone.write_csv(str(path), result["log"])
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == result["steps"] and set(rows[0]) >= {"t", "x", "y", "z", "action", "source", "risk"}
    render = d.render()
    assert "D" in render and "G" in render
    assert "<mujoco" in build_mjcf(d.obstacles, d.goal) and 'name="drone"' in build_mjcf(d.obstacles, d.goal)


def test_main_fly_mock(tmp_path, capsys):
    csv_path = tmp_path / "t.csv"
    assert drone.main(["fly", "--steps", "15", "--seed", "1", "--backend", "mock", "--csv", str(csv_path), "--render"]) == 0
    assert csv_path.exists()
    out = capsys.readouterr().out
    assert '"steps": 15' in out and "+----" in out
