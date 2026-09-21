import json

import pytest

from jevlab.apps import home
from jevlab.apps.home import StaticHA, compact_state, evaluate, load_rules, validate_rule
from jevlab.core import Jev, ScriptedBackend

STATES = {
    "states": [
        {"entity_id": "sensor.washer_power", "state": "2.1", "attributes": {"unit_of_measurement": "W", "friendly_name": "Washer power", "icon": "mdi:washing"}, "last_changed": "2026-09-21T10:00:00+00:00"},
        {"entity_id": "binary_sensor.washer_door", "state": "off", "attributes": {"device_class": "door"}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": 200}},
    ],
    "history": {"sensor.washer_power": [{"state": "450", "last_changed": "t1"}, {"state": "380", "last_changed": "t2"}, {"state": "2.1", "last_changed": "t3"}]},
}
RULES = [
    {"name": "laundry_done", "entities": ["sensor.washer_power", "binary_sensor.washer_door"], "question": "Has the washing machine finished its cycle (power dropped to idle after running)?", "threshold": 0.7, "cooldown_minutes": 60, "history_hours": 2, "notify": {"service": "notify.mobile_app_phone", "message": "洗濯が終わったよ ({probability})"}},
    {"name": "lights_left_on", "entities": ["light.kitchen"], "question": "Is the kitchen light on while nobody is around?", "threshold": 0.9, "notify": {"service": "notify.mobile_app_phone", "message": "キッチンの電気がつけっぱなし"}},
]


def test_compact_state_keeps_only_listed_entities_and_history():
    ha = StaticHA(STATES)
    state = compact_state(ha, RULES[0])
    assert set(state["entities"]) == {"sensor.washer_power", "binary_sensor.washer_door"}
    washer = state["entities"]["sensor.washer_power"]
    assert washer["state"] == "2.1" and washer["attributes"] == {"unit_of_measurement": "W", "friendly_name": "Washer power"}
    assert [h["state"] for h in washer["recent_history"]] == ["450", "380", "2.1"]
    assert state["entities"]["binary_sensor.washer_door"]["recent_history"] == []  # 履歴が無い entity は空リスト
    assert compact_state(ha, {"name": "x", "entities": ["sensor.missing"]})["entities"]["sensor.missing"] == {"state": "unavailable"}


def test_evaluate_thresholds_cooldown_and_dry_run(capsys):
    ha = StaticHA(STATES)
    backend = ScriptedBackend(default={"answer": 0.85})
    memory: dict = {}
    results = evaluate(Jev(backend), ha, RULES, memory, now=1000.0, dry_run=True)
    assert results[0]["fired"] and results[0]["notified"] and results[0]["message"] == "洗濯が終わったよ (85%)"
    assert not results[1]["fired"]  # 0.85 < 0.9
    assert ha.sent == [] and "[dry-run] notify notify.mobile_app_phone" in capsys.readouterr().out
    assert memory["laundry_done"] == 1000.0
    # cooldown 中は通知しない
    results = evaluate(Jev(backend), ha, RULES, memory, now=1000.0 + 30 * 60, dry_run=False)
    assert results[0]["fired"] and results[0]["in_cooldown"] and not results[0]["notified"] and ha.sent == []
    # cooldown を過ぎれば実通知 (StaticHA は記録するだけ)
    results = evaluate(Jev(backend), ha, RULES, memory, now=1000.0 + 61 * 60, dry_run=False)
    assert results[0]["notified"] and ha.sent[0]["service"] == "notify.mobile_app_phone"
    assert backend.calls[0]["state"]["rule"] == "laundry_done"


def test_validate_rule_rejects_non_notify_services(capsys):
    with pytest.raises(ValueError):
        validate_rule({"name": "x", "entities": [], "question": "?", "notify": {"service": "lock.unlock"}})
    with pytest.raises(ValueError):
        validate_rule({"name": "x", "entities": [], "question": "?"})
    validate_rule({"name": "front door lock check", "entities": [], "question": "?", "notify": {"service": "notify.me"}})
    assert "安全" in capsys.readouterr().err


def test_main_with_static_states_and_memory(tmp_path, capsys):
    (tmp_path / "rules.json").write_text(json.dumps({"rules": RULES}), encoding="utf-8")
    (tmp_path / "states.json").write_text(json.dumps(STATES), encoding="utf-8")
    memory = tmp_path / "memory.json"
    assert home.main(["--rules", str(tmp_path / "rules.json"), "--states", str(tmp_path / "states.json"), "--memory", str(memory), "--dry-run", "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert '"results"' in out and memory.exists()
    assert load_rules(tmp_path / "rules.json")[1]["name"] == "lights_left_on"
    # トークンもオフライン JSON も無ければ 2 で終了
    assert home.main(["--rules", str(tmp_path / "rules.json"), "--token-env", "JEVLAB_NO_SUCH_TOKEN"]) == 2
