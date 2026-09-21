import json

import pytest

from jevlab.core import (
    Choice,
    DecisionCache,
    FunctionBackend,
    Jev,
    MockBackend,
    Noul,
    Score,
    ScriptedBackend,
    parse_decision,
    questions_to_wire,
)


def test_questions_to_wire_matches_typesafe_schema():
    wire = questions_to_wire(
        {
            "intent": Choice({"refund": "返金", "bug": None}, "主な要求は?"),
            "urgency": Score(["low", "high"], "緊急度"),
            "angry": Noul("怒っているか", {"true": "怒り", "false": "冷静"}),
            "raw": {"type": "noul", "instructions": "raw dict"},
        }
    )
    assert wire["intent"] == {"type": "choice", "criteria": {"refund": "返金", "bug": None}, "instructions": "主な要求は?"}
    assert wire["urgency"] == {"type": "score", "criteria": ["low", "high"], "instructions": "緊急度"}
    assert wire["angry"]["criteria"] == {"true": "怒り", "false": "冷静"}
    assert wire["raw"]["type"] == "noul"
    json.dumps(wire)


def test_invalid_questions_rejected():
    with pytest.raises(ValueError):
        Choice({})
    with pytest.raises(ValueError):
        Score([])
    with pytest.raises(ValueError):
        questions_to_wire({})
    with pytest.raises(ValueError):
        questions_to_wire({"x": {"type": "choice"}})


def test_mock_backend_choice_prefers_lexical_overlap():
    jev = Jev(MockBackend())
    answer = jev.choose("My payouts have been failing for 3 days, refund me", {"billing": "payments, refunds, invoices", "technical": "bugs, crashes", "sales": None})
    assert answer.choice == "billing"
    assert abs(sum(answer.probabilities.values()) - 1.0) < 1e-6
    assert 0 <= answer.confidence <= 1


def test_mock_backend_score_and_noul():
    jev = Jev(MockBackend())
    score = jev.score("the customer is very angry and shouting", ["calm", "concerned", "very angry shouting"])
    assert score.level == 2
    assert score.legend[2] == "very angry shouting"
    yes = jev.judge("the deadline is today, urgent", "Does the text mention urgency or a deadline?")
    no = jev.judge("nice weather, no rush", "Does the text mention urgency or a deadline?")
    assert yes.noul > no.noul


def test_mock_hints_force_answers():
    jev = Jev(MockBackend(hints={"answer": "b"}))
    assert jev.choose("anything", ["a", "b", "c"]).choice == "b"


def test_scripted_backend_expands_short_answers():
    backend = ScriptedBackend([{"k": "b", "s": 1, "n": True}])
    jev = Jev(backend)
    decision = jev.decide("x", {"k": Choice.of("a", "b"), "s": Score(["0", "1", "2"]), "n": Noul("?")})
    assert decision.choice("k").choice == "b"
    assert decision.score("s").level == 1
    assert decision.noul("n").yes
    assert backend.calls[0]["state"] == "x"


def test_function_backend_and_parse_openrouter_shape():
    body = {
        "urgent": {"probability": 0.99},
        "team": {"winner": "technical", "confidence": 0.56, "probabilities": {"billing": 0.2, "technical": 0.56, "sales": 0.24}},
        "frustration": {"score": 2, "confidence": 1.0, "probabilities": {"0": 0, "1": 0, "2": 1.0}},
        "usage": {"input_tokens": 423, "output_tokens": 70},
        "cost": "$0.000018",
        "model": "typesafe/jev-1.13-20260917",
    }
    wire = questions_to_wire({"urgent": Noul("?"), "team": Choice.of("billing", "technical", "sales"), "frustration": Score(["Calm", "Concerned", "Angry"])})
    decision = parse_decision(body, wire, "openrouter", 12.0)
    assert decision.noul("urgent").noul == 0.99
    assert decision.choice("team").choice == "technical"
    assert decision.score("frustration").level == 2
    assert decision.score("frustration").legend[1] == "Concerned"
    assert decision.usage.input_tokens == 423 and decision.usage.cost == "$0.000018"

    fn = FunctionBackend(lambda state, q: {"answer": "b"})
    assert Jev(fn).choose("s", ["a", "b"]).choice == "b"


def test_cache_and_decide_many_and_rank():
    backend = MockBackend()
    jev = Jev(backend, cache=True)
    jev.judge("same state", "same question")
    jev.judge("same state", "same question")
    assert len(backend.calls) == 1
    assert jev.stats()["cache_hits"] == 1
    decisions = jev.decide_many([("a", {"q": Noul("?")}), ("b", {"q": Noul("?")})])
    assert len(decisions) == 2
    ranked = jev.rank(["python tutorial", "cooking recipe"], "How relevant is the candidate to the query?", context="learn python")
    assert ranked[0][0] == "python tutorial"
    kept = jev.filter(["python tutorial", "cooking recipe"], "Is the candidate about python?", threshold=0.3)
    assert "python tutorial" in kept


def test_cache_key_is_stable():
    assert DecisionCache.key({"a": 1}, {"q": {"type": "noul"}}, "m") == DecisionCache.key({"a": 1}, {"q": {"type": "noul"}}, "m")


def test_backend_from_env_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JEV_BACKEND", raising=False)
    assert Jev().is_mock


def test_typesafe_backend_request_shape(monkeypatch):
    from jevlab.core import backends

    captured = {}

    def fake_post(url, body, headers, timeout, retries=2):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return {"answers": {"answer": {"type": "noul", "noul": 0.7}}, "model": "jev-1.13", "usage": {"input_tokens": 3, "output_tokens": 0}}

    monkeypatch.setattr(backends, "_post_json", fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    jev = Jev("typesafe")
    answer = jev.judge("hello", "?")
    assert answer.noul == 0.7
    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["body"] == {"state": "hello", "model": "jev-latest", "questions": {"answer": {"type": "noul", "instructions": "?"}}}
    assert captured["headers"]["Authorization"] == "Bearer k"

    monkeypatch.setenv("OPENROUTER_API_KEY", "ork")
    Jev("openrouter").judge("hi", "?")
    assert captured["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert captured["body"]["model"] == "typesafe/jev-latest"
