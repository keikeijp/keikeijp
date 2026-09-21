import json

from jevlab.core import Jev, ScriptedBackend

from jevlab.apps import computer_use
from jevlab.apps.computer_use import DEMO_SCREEN, Agent, DryRunExecutor, MacAccessibilityReader, OcrReader, StaticReader, argument_for, choose

SCREEN = [
    {"id": "ok", "kind": "button", "text": "OK", "bbox": [100, 200, 80, 30]},
    {"id": "cancel", "kind": "button", "text": "Cancel", "bbox": [200, 200, 80, 30]},
    {"id": "name", "kind": "textfield", "text": "", "bbox": [100, 100, 300, 24]},
]


def test_static_reader_normalizes_elements(tmp_path):
    path = tmp_path / "screen.json"
    path.write_text(json.dumps(SCREEN), encoding="utf-8")
    elements = StaticReader.from_file(str(path)).read()
    assert elements[0] == {"id": "ok", "kind": "button", "text": "OK", "bbox": [100, 200, 80, 30]}
    assert elements[2]["text"] == ""


def test_choose_picks_action_and_target_with_scripted_backend():
    backend = ScriptedBackend([{"action": "click", "target": "ok"}])
    step = choose(Jev(backend), "Press the OK button", StaticReader(SCREEN).read(), [])
    assert step.action == "click" and step.target == "ok"
    assert "button 'OK'" in step.description
    wire = backend.calls[0]["questions"]
    assert set(wire["action"]["criteria"]) == set(computer_use.ACTIONS)
    assert set(wire["target"]["criteria"]) == {"ok", "cancel", "name"}


def test_argument_slots_from_task():
    assert argument_for(computer_use.Step("type", None, 0.9), 'type "hello there" in the field') == "hello there"
    assert argument_for(computer_use.Step("key", None, 0.9), "then press cmd+s to save") == "cmd+s"
    assert argument_for(computer_use.Step("key", None, 0.9), "confirm the dialog") == "enter"


def test_agent_loop_dry_run_executor_logs_clicks_at_center():
    backend = ScriptedBackend([{"action": "double_click", "target": "downloads"}, {"action": "done", "target": "title"}])
    executor = DryRunExecutor()
    reader = StaticReader(frames=DEMO_SCREEN["frames"])
    result = Agent(Jev(backend), reader, executor, log=lambda _: None).run("Open the Downloads folder", max_steps=4)
    assert result.status == "done"
    assert executor.log == [("double_click", (80, 160))]
    assert result.steps[0].outcome == "double_click at (80,160)"


def test_mac_and_ocr_parsers_work_offline():
    text = "AXButton\tOK\t10\t20\t30\t40\nAXStaticText\tHello\t1\t2\t3\t4\nbroken line\n"
    elements = MacAccessibilityReader.parse(text)
    assert [e["kind"] for e in elements] == ["button", "statictext"]
    assert elements[0]["bbox"] == [10, 20, 30, 40]
    data = {"text": ["Open", "File", "", "Save"], "block_num": [1, 1, 1, 2], "line_num": [1, 1, 1, 1], "left": [0, 40, 0, 0], "top": [0, 0, 0, 50], "width": [30, 30, 0, 30], "height": [10, 10, 0, 10]}
    grouped = OcrReader.group_words(data)
    assert [g["text"] for g in grouped] == ["Open File", "Save"]
    assert grouped[0]["bbox"] == [0, 0, 70, 10]


def test_main_offline_dry_run(tmp_path, capsys):
    path = tmp_path / "screen.json"
    path.write_text(json.dumps(SCREEN), encoding="utf-8")
    code = computer_use.main(["--task", "Press OK", "--screen-json", str(path), "--backend", "mock", "--max-steps", "2"])
    assert code in (0, 1)
    assert "dry_run=True" in capsys.readouterr().out
