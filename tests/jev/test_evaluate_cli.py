import json

import pytest

from jev_usecases import JevClient, ScriptedBackend
from jev_usecases.cli import main
from jev_usecases.evaluate import estimate_batch_cost, evaluate, load_jsonl
from jev_usecases.usecases import get_usecase


def C(label, conf):
    return {"type": "choice", "choice": label, "confidence": conf, "probabilities": {label: conf}}


def S(levels, value):
    return {"type": "score", "score": value, "confidence": 0.9, "legend": {str(i): l for i, l in enumerate(levels)}, "probabilities": {str(i): 1.0 if i == round(value) else 0.0 for i in range(len(levels))}}


def N(p):
    return {"type": "noul", "noul": p}


def test_evaluate_metrics():
    uc = get_usecase("guard")
    risk = ["safe", "low", "medium", "high", "critical"]
    allow = {"category": C("read_only", 0.95), "risk": S(risk, 0.0), "destructive": N(0.02), "leaves_project": N(0.05)}
    block = {"category": C("destructive", 0.95), "risk": S(risk, 4.0), "destructive": N(0.98), "leaves_project": N(0.9)}
    # 3 件目: 本当は block だが allow と判定 (見逃し)
    backend = ScriptedBackend([allow, block, allow, allow])
    dataset = [
        {"input": {"command": "ls"}, "label": "allow"},
        {"input": {"command": "rm -rf /"}, "label": "block"},
        {"input": {"command": "git push --force"}, "label": "block"},
        {"input": {"command": "cat x"}, "label": "allow"},
    ]
    report = evaluate(uc, JevClient(backend), dataset)
    assert report.n == 4
    assert report.accuracy == pytest.approx(0.75)
    assert report.false_pass_rate == pytest.approx(0.5)
    assert report.human_rate == pytest.approx(0.25)
    assert report.input_tokens == 400 and report.total_cost_usd == pytest.approx(400 / 1e6 * 0.042)
    assert report.confusion == {"allow": {"allow": 2}, "block": {"block": 1, "allow": 1}}
    assert report.errors[0]["label"] == "block" and report.errors[0]["predicted"] == "allow"
    assert "accuracy=75.0%" in report.summary()
    assert json.dumps(report.to_dict())


def test_evaluate_without_labels():
    uc = get_usecase("viral")
    report = evaluate(uc, JevClient.from_env("mock"), [{"input": {"post": "hello"}}])
    assert report.accuracy is None and report.false_pass_rate is None and report.n == 1


def test_estimate_batch_cost_matches_article():
    assert estimate_batch_cost(10_000, 2_000) == {"items": 10_000, "tokens_per_item": 2_000, "total_tokens": 20_000_000, "usd": 0.84}


def test_load_jsonl_skips_comments(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('# comment\n{"input": {"command": "ls"}, "label": "allow"}\n\n', encoding="utf-8")
    assert load_jsonl(p) == [{"input": {"command": "ls"}, "label": "allow"}]


def test_cli_list_run_dry_run_and_eval(tmp_path, capsys):
    assert main(["list"]) == 0
    assert "triage" in capsys.readouterr().out

    assert main(["run", "guard", "--text", "git status", "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert "git status" in out

    assert main(["run", "triage", "--demo", "--dry-run"]) == 0
    body = json.loads("[" + capsys.readouterr().out.replace("}\n{", "},{") + "]")[0]
    assert body["model"] == "jev-latest" and "department" in body["questions"]

    assert main(["run", "sniff", "--demo", "--backend", "mock", "--json"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[0]
    assert "decision" in json.loads(line)

    p = tmp_path / "d.jsonl"
    p.write_text('{"input": {"command": "ls"}, "label": "allow"}\n', encoding="utf-8")
    assert main(["eval", "guard", "--dataset", str(p), "--backend", "mock", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 1

    assert main(["cost", "--items", "10", "--tokens", "100"]) == 0
    assert json.loads(capsys.readouterr().out)["usd"] == pytest.approx(0.000042)


def test_cli_stdin_jsonl(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO('{"post": "hello world"}\n'))
    assert main(["run", "feed", "--input", "-", "--backend", "mock"]) == 0
    assert "hello world" in capsys.readouterr().out


def test_cli_http_without_key_reports_error(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["run", "guard", "--text", "ls"]) == 1
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err
