import json

from jevlab.apps import warden
from jevlab.core import FunctionBackend, Jev, ScriptedBackend

RULES_MD = """# Project rules

Some intro text.

- Never run `rm -rf` on the repo directory
- Always run pytest before claiming a task is done
* Do not edit files under vendor/
"""


def test_parse_rules():
    rules = warden.parse_rules(RULES_MD)
    assert len(rules) == 3
    assert rules[0] == "Never run rm -rf on the repo directory"
    assert rules[2].startswith("Do not edit")


def test_duplicate_failure_detection_without_jev_and_with_jev():
    err = "Traceback (most recent call last):\n  File 'a.py', line 10\nAssertionError: expected 1 got 2"
    events = [
        {"ts": 1, "kind": "tool_call", "text": "pytest tests/"},
        {"ts": 2, "kind": "tool_result", "text": err},
        {"ts": 3, "kind": "tool_call", "text": "pytest tests/"},
        {"ts": 4, "kind": "tool_result", "text": err},  # 完全一致
        {"ts": 5, "kind": "tool_result", "text": err.replace("line 10", "line 11")},  # 近似
        {"ts": 6, "kind": "tool_result", "text": "Error: connection refused while calling AssertionError endpoint got 2 File"},  # 中間 → Jev
    ]
    backend = ScriptedBackend([{"same_failure_again": True}])
    findings = warden.check_repeated_failures(Jev(backend), events)
    assert [f["event_index"] for f in findings] == [3, 4, 5]
    assert findings[0]["how"] == "exact" and findings[1]["how"] == "near_duplicate" and findings[2]["how"].startswith("jev:")
    assert len(backend.calls) == 1  # 完全/近似一致は Jev を呼ばない
    assert all(f["severity"] == "critical" for f in findings)


def test_unverified_completion_claim():
    backend = FunctionBackend(lambda s, q: {"claims_completion": True, "verified": "pytest" in " ".join(s.get("commands_after", []))})
    jev = Jev(backend)
    unverified = [
        {"ts": 1, "kind": "tool_call", "text": "edit src/app.py"},
        {"ts": 2, "kind": "message", "text": "Done! The bug is fixed and everything works now."},
    ]
    findings = warden.check_completion_claims(jev, unverified)
    assert len(findings) == 1 and findings[0]["check"] == "unverified_completion" and findings[0]["severity"] == "critical"
    verified = unverified + [{"ts": 3, "kind": "tool_call", "text": "pytest -q"}, {"ts": 4, "kind": "tool_result", "text": "3 passed"}]
    assert warden.check_completion_claims(jev, verified) == []
    assert warden.exit_code(findings) == 1


def test_rule_violation_prefiltered_and_scripted():
    rules = warden.parse_rules(RULES_MD)
    events = [
        {"ts": 1, "kind": "tool_call", "text": "rm -rf the repo directory build cache"},
        {"ts": 2, "kind": "tool_call", "text": "ls"},  # どのルールとも重ならない → Jev を呼ばない
    ]
    backend = ScriptedBackend(default={"violates_rule": 0.95})
    findings = warden.check_rule_violations(Jev(backend, max_workers=1), events, rules)
    assert len(backend.calls) >= 1
    assert all(call["state"]["text"] != "ls" for call in backend.calls)
    assert findings and findings[0]["rule"].startswith("Never run rm -rf") and findings[0]["severity"] == "critical"
    assert findings[0]["event_index"] == 0


def test_main_reports_zero_findings(tmp_path, capsys):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"ts": 1, "kind": "tool_call", "text": "ls"}) + "\n", encoding="utf-8")
    rules = tmp_path / "rules.md"
    rules.write_text(RULES_MD, encoding="utf-8")
    out = tmp_path / "findings.json"
    code = warden.main(["--rules", str(rules), "--events", str(events), "--json-out", str(out), "--backend", "mock"])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["findings"] == []
