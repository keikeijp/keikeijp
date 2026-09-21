import json

from jevlab.apps import foreman
from jevlab.core import Jev, ScriptedBackend


def _config(**overrides):
    base = dict(task="fix the bug", verify_cmd="pytest -q", interval_lines=3, idle_seconds=0, max_minutes=1, dry_run=False)
    base.update(overrides)
    return foreman.ForemanConfig(**base)


def test_regexes_detect_tests_and_completion_claims():
    assert foreman.TESTS_RE.search("3 passed in 0.1s")
    assert foreman.TESTS_RE.search("running pytest -q")
    assert not foreman.TESTS_RE.search("editing file")
    assert foreman.DONE_RE.search("The task is complete.")
    assert foreman.DONE_RE.search("Implemented the feature")
    assert not foreman.DONE_RE.search("still working on it")
    assert foreman.repeat_ratio(["a", "a", "a", "a", "b"]) > 0.5
    assert foreman.repeat_ratio(["a", "b", "c", "d"]) == 0.0


def test_verify_verdict_runs_command_and_feeds_back_then_stops():
    agent = foreman.FakeAgentProcess(["edit a.py", "run tests", "1 passed", "done"])
    backend = ScriptedBackend([{"verdict": "verify", "progress": 2, "claims_completion": True, "looks_looping": False}])
    runs = []

    def runner(cmd):
        runs.append(cmd)
        return 0, "1 passed in 0.01s"

    fm = foreman.Foreman(Jev(backend), agent, _config(), files_changed_fn=lambda: ["a.py"], verify_runner=runner)
    session = fm.run()
    assert runs == ["pytest -q"]
    assert session.verified is True
    assert session.stopped_by == "verified"
    assert agent.terminated
    assert any("PASSED" in msg for msg in agent.inbox)
    state = backend.calls[0]["state"]
    assert state["task"] == "fix the bug" and state["files_changed"] == ["a.py"]
    assert set(backend.calls[0]["questions"]) == {"verdict", "progress", "claims_completion", "looks_looping"}


def test_stop_verdict_terminates_and_low_confidence_falls_back_to_continue():
    agent = foreman.FakeAgentProcess(["x"] * 9)
    backend = ScriptedBackend(
        [
            {"verdict": {"stop": 0.3, "continue": 0.25, "verify": 0.25, "intervene": 0.2}, "progress": 1, "claims_completion": False, "looks_looping": False},
            {"verdict": "continue", "progress": 2, "claims_completion": False, "looks_looping": False},
            {"verdict": "stop", "progress": 0, "claims_completion": False, "looks_looping": False},
        ]
    )
    fm = foreman.Foreman(Jev(backend), agent, _config(min_confidence=0.5), files_changed_fn=lambda: [])
    session = fm.run()
    assert session.stopped_by == "jev_stop" and agent.terminated
    checks = [e for e in session.events if e["kind"] == "check"]
    assert checks[0].get("low_confidence") is True and checks[0]["verdict"] == "continue"
    assert session.verdicts["stop"] == 1


def test_looping_detection_intervenes_then_stops():
    agent = foreman.FakeAgentProcess(["retrying..."] * 12)
    backend = ScriptedBackend(default={"verdict": "continue", "progress": 0, "claims_completion": False, "looks_looping": True})
    fm = foreman.Foreman(Jev(backend), agent, _config(max_interventions=2), files_changed_fn=lambda: [])
    session = fm.run()
    assert len(agent.inbox) == 2  # 2 回促してから
    assert session.stopped_by == "too_many_interventions"
    assert all(e["looks_looping"] for e in session.events if e["kind"] == "check")


def test_idle_triggers_check_and_dry_run_skips_verify(tmp_path):
    agent = foreman.FakeAgentProcess(["working", None, "more"])
    backend = ScriptedBackend(default={"verdict": "verify", "progress": 2, "claims_completion": False, "looks_looping": False})
    log = tmp_path / "session.jsonl"
    session = foreman.Session(task="t", log_path=str(log))
    fm = foreman.Foreman(Jev(backend), agent, _config(dry_run=True, interval_lines=100), session, files_changed_fn=lambda: [])
    fm.run()
    kinds = [e["kind"] for e in session.events]
    assert "idle" in kinds and "verify" in kinds
    assert session.verified is None  # dry_run では検証コマンドを実行しない
    assert session.stopped_by == "agent_exited"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "start" and lines[-1]["kind"] == "end"


def test_main_dry_run(capsys):
    assert foreman.main(["--task", "demo", "--verify", "pytest -q", "--dry-run", "--interval", "2", "--backend", "mock"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task"] == "demo" and out["checks"] >= 1
