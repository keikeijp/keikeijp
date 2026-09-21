import json

from jevlab.core import Choice, Jev, MockBackend, Noul, Score, ScriptedBackend
from jevlab.sam import ImageInput, MockSam, Sam
from jevlab.scenejudge import BUILTIN, Rule, Scenario, SceneJudge, evaluate_condition, get_scenario, scenario_from_dict
from jevlab.scenejudge.cli import main
from jevlab.scenejudge.demo import DESK_OBJECTS, TIDY_OBJECTS, write_png


def make_image(tmp_path, name, objects):
    return ImageInput.from_path(write_png(tmp_path / f"{name}.png", objects))


def test_builtin_scenarios_roundtrip_json():
    for name, scenario in BUILTIN.items():
        data = json.loads(json.dumps(scenario.to_dict(), ensure_ascii=False))
        restored = scenario_from_dict(data)
        assert restored.name == name
        assert set(restored.questions) == set(scenario.questions)
        assert restored.zones == scenario.zones
    assert get_scenario("desk") is BUILTIN["desk"]


def test_evaluate_condition_forms():
    class A:
        noul = 0.9
        level = 2
        score = 1.4
        choice = "dirty"
        confidence = 0.6

    assert evaluate_condition("noul>=0.8", A)
    assert evaluate_condition("level<=2", A)
    assert not evaluate_condition("score>2", A)
    assert evaluate_condition("choice==dirty", A)
    assert evaluate_condition("choice!=clean", A)
    assert not evaluate_condition("confidence>=0.7", A)


def test_judge_image_with_scripted_answers(tmp_path):
    image = make_image(tmp_path, "messy", DESK_OBJECTS)
    backend = ScriptedBackend([{"tidiness": 3, "first_to_remove": "trash", "needs_cleanup": True}])
    judge = SceneJudge(sam=Sam(MockSam(DESK_OBJECTS)), jev=Jev(backend))
    verdict = judge.judge_image(image, "desk")
    assert verdict.answers["tidiness"].level == 3
    assert verdict.answers["first_to_remove"].choice == "trash"
    assert verdict.triggered and verdict.triggered[0]["action"] == "notify"
    state = backend.calls[0]["state"]
    assert state["scene"]["counts"]["paper"] == 2
    labels = {obj["label"] for obj in state["scene"]["objects"]}
    assert "laptop" in labels and all(0 <= v <= 1 for obj in state["scene"]["objects"] for v in obj["box"])
    assert "sam" in verdict.to_dict()["timing_ms"]
    assert "tidiness: level 3" in verdict.summary()


def test_mock_jev_distinguishes_messy_and_tidy(tmp_path):
    jev = Jev(MockBackend())
    messy = SceneJudge(sam=Sam(MockSam(DESK_OBJECTS)), jev=jev).judge_image(make_image(tmp_path, "m", DESK_OBJECTS), "desk")
    tidy = SceneJudge(sam=Sam(MockSam(TIDY_OBJECTS)), jev=jev).judge_image(make_image(tmp_path, "t", TIDY_OBJECTS), "desk")
    assert messy.scene_state["counts"] != tidy.scene_state["counts"]
    assert len(messy.scene_state["objects"]) > len(tidy.scene_state["objects"])


def test_video_zone_events(tmp_path):
    scenario = Scenario(
        name="zone",
        description="zone test",
        prompts=["dog"],
        zones={"kitchen": (0.0, 0.5, 0.5, 1.0)},
        questions={"present": Noul("Is a dog present?")},
        rules=[Rule("present", "noul>=0.5", "log", "dog")],
    )
    frames = [make_image(tmp_path, f"f{i}", {}) for i in range(3)]
    positions = [[(10, 10, 60, 60)], [(20, 220, 80, 300)], [(400, 20, 460, 80)]]  # outside → kitchen → outside
    calls = {"i": 0}

    def detector(image, prompt):
        boxes = positions[calls["i"]]
        calls["i"] += 1
        return boxes

    judge = SceneJudge(sam=Sam(MockSam(detector=detector)), jev=Jev(ScriptedBackend(default={"present": True})))
    verdicts, events = judge.judge_video(frames, scenario)
    assert len(verdicts) == 3
    kinds = [(e.frame, e.kind, e.zone) for e in events]
    assert kinds == [(1, "enter", "kitchen"), (2, "leave", "kitchen")]


def test_cli_demo_and_scenarios(tmp_path, capsys):
    assert main(["--backend", "mock", "demo", "--out-dir", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    assert "[desk]" in out and "report ->" in out
    assert (tmp_path / "demo" / "report.json").exists()
    assert main(["scenarios", "--export", str(tmp_path / "desk.json"), "--name", "desk"]) == 0
    assert json.loads((tmp_path / "desk.json").read_text())["name"] == "desk"
    assert main(["--backend", "mock", "--sam-backend", "mock", "image", str(tmp_path / "demo" / "messy_desk.png"), "--scenario", str(tmp_path / "desk.json")]) == 0


def test_cli_watch_once_with_file_notify(tmp_path, capsys):
    folder = tmp_path / "frames"
    folder.mkdir()
    write_png(folder / "a.png", DESK_OBJECTS)
    scenario = BUILTIN["desk"].to_dict()
    scenario["rules"] = [{"question": "needs_cleanup", "when": "noul>=0.0", "action": "notify", "message": "hi"}]
    (tmp_path / "s.json").write_text(json.dumps(scenario))
    log = tmp_path / "notify.jsonl"
    assert main(["--backend", "mock", "--sam-backend", "mock", "watch", str(folder), "--scenario", str(tmp_path / "s.json"), "--once", "--notify", str(log)]) == 0
    assert log.exists() and json.loads(log.read_text().splitlines()[0])["message"] == "hi"
