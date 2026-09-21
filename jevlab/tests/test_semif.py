import json

import pytest

from jevlab.core import Choice, Jev, Noul, Score, backend_from_env
from jevlab.local import semif
from jevlab.local.semif import (
    FakeLogitModel,
    LocalLogitBackend,
    build_choice_prompt,
    build_noul_prompt,
    logprobs_to_probs,
    parse_prompt,
    temperature_scale,
)


def test_choice_prompt_enumerates_letters_and_roundtrips():
    prompt, letters = build_choice_prompt({"ticket": "refund"}, {"refund": "返金要求", "bug": None}, "主な要求は?")
    assert letters == ["A", "B"]
    assert prompt.endswith("Answer:")
    state, question, options = parse_prompt(prompt)
    assert json.loads(state) == {"ticket": "refund"}
    assert question == "主な要求は?"
    assert options == {"A": "refund: 返金要求", "B": "bug"}
    _, yes_no = build_noul_prompt("x", "?")
    assert yes_no == ["Yes", "No"]


def test_fake_logit_backend_through_jev():
    model = FakeLogitModel()
    jev = Jev(LocalLogitBackend(model=model))
    decision = jev.decide(
        "My payouts have been failing for 3 days, refund me",
        {
            "team": Choice({"billing": "payments, refunds, invoices", "technical": "bugs, crashes", "sales": None}),
            "anger": Score(["calm", "concerned", "very angry shouting"]),
            "refund": Noul("Does the customer ask for a refund?"),
        },
    )
    assert decision.backend == "local" and decision.model == "fake-logit"
    team = decision.choice("team")
    assert team.choice == "billing"
    assert abs(sum(team.probabilities.values()) - 1.0) < 1e-6
    assert team.confidence == pytest.approx(team.margin())
    assert len(model.calls) == 3  # 質問ごとに 1 forward
    score = decision.score("anger")
    assert score.legend[2] == "very angry shouting" and 0 <= score.score <= 2
    yes = jev.judge("the deadline is today, urgent", "Does the text mention urgency or a deadline?")
    no = jev.judge("nice weather, no rush", "Does the text mention urgency or a deadline?")
    assert yes.noul > no.noul


def test_temperature_scale_and_probs():
    probs = logprobs_to_probs({"A": -0.1, "B": -3.0})
    assert probs["A"] > probs["B"] and abs(sum(probs.values()) - 1) < 1e-9
    flat = temperature_scale({"a": 0.9, "b": 0.1}, 2.0)
    sharp = temperature_scale({"a": 0.9, "b": 0.1}, 0.5)
    assert flat["a"] < 0.9 < sharp["a"]
    assert abs(sum(flat.values()) - 1) < 1e-9


def test_from_env_fake_and_backend_from_env(monkeypatch):
    monkeypatch.setenv("JEV_LOCAL_MODEL", "fake")
    backend = LocalLogitBackend.from_env()
    assert isinstance(backend.model_impl, FakeLogitModel)
    monkeypatch.setenv("JEV_BACKEND", "local")
    assert isinstance(backend_from_env(), LocalLogitBackend)
    monkeypatch.delenv("JEV_LOCAL_MODEL")
    lazy = LocalLogitBackend.from_env()  # HF モデルはまだ読まない
    assert lazy.model_impl.model_id == semif.DEFAULT_MODEL and lazy.model_impl._model is None


def test_main_ask_and_bench_offline(tmp_path, capsys):
    assert semif.main(["--fake", "ask", "--state", "charged twice, refund please", "--choice", "refund,bug,other", "--noul", "Is it about refund?"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["answers"]["choice"]["choice"] == "refund"
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps({"state": "the app crashes on login", "options": {"bug": "crash, error", "refund": "money back"}, "expected": "bug"}) + "\n"
        + json.dumps({"state": "give me my money back", "options": {"bug": "crash, error", "refund": "money back"}, "expected": "refund"}) + "\n"
    )
    assert semif.main(["--fake", "bench", "--jsonl", str(cases)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] == 2 and result["accuracy"] == 1.0
