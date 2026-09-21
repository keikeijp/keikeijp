import json

from jevlab.core import Jev, ScriptedBackend

from jevlab.apps import mobile
from jevlab.apps.mobile import SAMPLE_HIERARCHY, AdbDevice, Agent, FakeDevice, choose, parse_hierarchy


def test_parse_hierarchy_keeps_interactive_enabled_nodes():
    elements = parse_hierarchy(SAMPLE_HIERARCHY)
    labels = [(e["class"], e["text"] or e["content_desc"]) for e in elements]
    assert labels == [("EditText", "Search settings"), ("TextView", "Network & internet"), ("Switch", "Wi-Fi")]
    assert elements[0]["editable"] and elements[0]["resource_id"] == "search"
    assert elements[2]["bounds"] == [40, 600, 1040, 720] and elements[2]["clickable"] and not elements[2]["checked"]
    assert [e["index"] for e in elements] == [0, 1, 2]
    assert len(parse_hierarchy(SAMPLE_HIERARCHY, interactive_only=False)) == 5  # disabled は常に落とす


def test_choose_one_step_with_scripted_backend():
    backend = ScriptedBackend([{"action": "tap", "target": "2"}])
    elements = parse_hierarchy(SAMPLE_HIERARCHY)
    step = choose(Jev(backend), "Turn on Wi-Fi", elements, [])
    assert step.action == "tap" and step.target == 2 and "Wi-Fi" in step.description
    wire = backend.calls[0]["questions"]
    assert set(wire["action"]["criteria"]) == set(mobile.ACTIONS)
    assert set(wire["target"]["criteria"]) == {"0", "1", "2"}
    assert backend.calls[0]["state"]["task"] == "Turn on Wi-Fi"


def test_agent_executes_on_fake_device_and_writes_jsonl(tmp_path):
    on_screen = SAMPLE_HIERARCHY.replace('text="Wi-Fi" resource-id="com.android.settings:id/switch_text" class="android.widget.Switch" package="com.android.settings" content-desc="" checkable="true" checked="false"', 'text="Wi-Fi" resource-id="com.android.settings:id/switch_text" class="android.widget.Switch" package="com.android.settings" content-desc="" checkable="true" checked="true"')
    device = FakeDevice({"settings": SAMPLE_HIERARCHY, "wifi_on": on_screen}, on={"settings": {"tap:2": "wifi_on"}})
    backend = ScriptedBackend([{"action": "tap", "target": "2"}, {"action": "done", "target": "0"}])
    log_path = tmp_path / "run.jsonl"
    agent = Agent(Jev(backend), device, dry_run=False, log=lambda _: None, jsonl_path=str(log_path))
    result = agent.run("Turn on Wi-Fi", max_steps=5)
    assert result.status == "done"
    assert device.actions == [("tap", (540, 660))]
    assert device.current == "wifi_on"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [l["action"] for l in lines] == ["tap", "done"] and lines[0]["step"] == 1
    assert agent.status["status"] == "done"


def test_adb_device_builds_commands_with_fake_runner():
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(cmd)
        return SAMPLE_HIERARCHY if cmd[-1].endswith(".xml") and "cat" in cmd else "Physical size: 1080x2340\n"

    device = AdbDevice("emulator-5554", runner=runner)
    assert len(device.hierarchy()) == 3
    device.tap(10, 20)
    device.input_text("hello world")
    assert calls[0][:4] == ["adb", "-s", "emulator-5554", "shell"] and calls[0][4] == "uiautomator"
    assert calls[2][-3:] == ["tap", "10", "20"]
    assert calls[3][-1] == "hello%sworld"
    assert device.screen_size() == (1080, 2340)


def test_main_offline_dry_run(tmp_path, capsys):
    xml_path = tmp_path / "dump.xml"
    xml_path.write_text(SAMPLE_HIERARCHY, encoding="utf-8")
    code = mobile.main(["--task", "Turn on Wi-Fi", "--hierarchy-xml", str(xml_path), "--backend", "mock", "--max-steps", "2", "--log", str(tmp_path / "log.jsonl")])
    assert code in (0, 1)
    assert "dry_run=True" in capsys.readouterr().out
    assert (tmp_path / "log.jsonl").exists()
