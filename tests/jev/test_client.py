import json

import pytest

from jev_usecases import (
    ChoiceAnswer,
    HttpBackend,
    JevAPIError,
    JevClient,
    JevError,
    MockBackend,
    NoulAnswer,
    ScoreAnswer,
    ScriptedBackend,
    choice,
    estimate_cost_usd,
    noul,
    score,
)
from jev_usecases.client import parse_answer


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = {
    "model": "jev-1.13.0",
    "answers": {
        "billing": {"type": "noul", "noul": 0.93},
        "dept": {"type": "choice", "choice": "billing", "confidence": 0.88, "probabilities": {"billing": 0.9, "bug": 0.1}},
        "urgency": {"type": "score", "score": 1.7, "confidence": 0.8, "legend": {"0": "wait", "1": "week", "2": "today"}, "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8}},
    },
    "usage": {"input_tokens": 120, "output_tokens": 3},
}


def test_question_helpers_match_wire_format():
    assert noul("q?") == {"type": "noul", "instructions": "q?"}
    assert noul("q?", true="yes when", false="no when") == {"type": "noul", "instructions": "q?", "criteria": {"true": "yes when", "false": "no when"}}
    assert choice("which?", {"a": "A", "b": None}) == {"type": "choice", "instructions": "which?", "criteria": {"a": "A", "b": None}}
    assert score("how?", ["low", "high"]) == {"type": "score", "instructions": "how?", "criteria": ["low", "high"]}
    with pytest.raises(ValueError):
        choice("x", {})
    with pytest.raises(ValueError):
        score("x", [])
    with pytest.raises(ValueError):
        score("x", [str(i) for i in range(11)])
    with pytest.raises(ValueError):
        choice("x", {str(i): None for i in range(256)})


def test_parse_answers_and_accessors():
    n = parse_answer(OK["answers"]["billing"])
    c = parse_answer(OK["answers"]["dept"])
    s = parse_answer(OK["answers"]["urgency"])
    assert isinstance(n, NoulAnswer) and n.yes
    assert isinstance(c, ChoiceAnswer) and c.ranked()[0] == ("billing", 0.9)
    assert isinstance(s, ScoreAnswer)
    assert s.max_level == 2 and s.level == 2 and s.label == "today"
    assert s.normalized == pytest.approx(0.85)
    assert s.to_dict()["legend"] == {"0": "wait", "1": "week", "2": "today"}
    with pytest.raises(JevError):
        parse_answer({"type": "future"})


def test_http_backend_sends_expected_body_and_parses():
    session = FakeSession([FakeResponse(200, OK, {"x-typesafe-request-id": "req_1"})])
    backend = HttpBackend("sk-test", session=session, sleep=lambda s: None)
    client = JevClient(backend, model="jev-latest")
    result = client.ask("I was charged twice.", {"billing": noul("Is this about billing?"), "dept": choice("dept?", {"billing": None, "bug": None}), "urgency": score("urgent?", ["wait", "week", "today"])})

    req = session.requests[0]
    assert req["method"] == "POST" and req["url"] == "https://api.typesafe.ai/v1/systemone"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    assert req["json"] == {
        "state": "I was charged twice.",
        "model": "jev-latest",
        "questions": {
            "billing": {"type": "noul", "instructions": "Is this about billing?"},
            "dept": {"type": "choice", "instructions": "dept?", "criteria": {"billing": None, "bug": None}},
            "urgency": {"type": "score", "instructions": "urgent?", "criteria": ["wait", "week", "today"]},
        },
    }
    assert result.model == "jev-1.13.0"
    assert result.noul("billing") == 0.93
    assert result.choice("dept").choice == "billing"
    assert result.score("urgency").level == 2
    assert result.input_tokens == 120
    assert result.cost_usd == pytest.approx(estimate_cost_usd(120))
    assert client.total_input_tokens == 120 and client.total_calls == 1
    with pytest.raises(JevError):
        result.noul("dept")


def test_http_backend_retries_on_429_then_succeeds():
    sleeps = []
    session = FakeSession([FakeResponse(429, {"detail": "slow down"}, {"retry-after-ms": "250"}), FakeResponse(200, OK)])
    backend = HttpBackend("sk-test", session=session, sleep=sleeps.append)
    assert backend.system_one({"state": "x", "model": "jev-latest", "questions": {}})["model"] == "jev-1.13.0"
    assert sleeps == [0.25]
    assert len(session.requests) == 2


def test_http_backend_raises_api_error_after_retries():
    session = FakeSession([FakeResponse(503, {"detail": "down"})] * 4)
    backend = HttpBackend("sk-test", session=session, max_retries=3, sleep=lambda s: None)
    with pytest.raises(JevAPIError) as exc:
        backend.system_one({"state": "x", "model": "jev-latest", "questions": {}})
    assert exc.value.status == 503
    assert len(session.requests) == 4


def test_http_backend_422_is_not_retried():
    session = FakeSession([FakeResponse(422, {"detail": [{"loc": ["body", "questions"], "msg": "Field required"}]})])
    backend = HttpBackend("sk-test", session=session, sleep=lambda s: None)
    with pytest.raises(JevAPIError) as exc:
        backend.system_one({"state": "x", "model": "jev-latest", "questions": {}})
    assert exc.value.status == 422 and len(session.requests) == 1


def test_http_backend_requires_api_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError):
        HttpBackend()
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-env")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://example.test/")
    b = HttpBackend()
    assert b.base_url == "https://example.test"


def test_client_from_env_mock_and_unknown():
    assert isinstance(JevClient.from_env("mock").backend, MockBackend)
    with pytest.raises(JevError):
        JevClient.from_env("nope")


def test_mock_backend_is_deterministic_and_well_formed():
    client = JevClient(MockBackend())
    qs = {
        "danger": noul("Is it dangerous?", true="rm -rf, DROP TABLE, force push"),
        "kind": choice("kind?", {"read": "ls cat grep", "delete": "rm -rf remove"}),
        "risk": score("risk?", ["safe: ls", "risky: rm -rf"]),
    }
    a = client.ask("rm -rf build", qs)
    b = client.ask("rm -rf build", qs)
    assert a.answers_dict() == b.answers_dict()
    assert a.choice("kind").choice == "delete"
    assert sum(a.choice("kind").probabilities.values()) == pytest.approx(1.0, abs=1e-3)
    assert sum(a.score("risk").probabilities.values()) == pytest.approx(1.0, abs=1e-3)
    assert 0 <= a.noul("danger") <= 1
    assert a.score("risk").score > 0.5
    assert client.ask("ls -la", qs).choice("kind").choice == "read"


def test_ask_many_concurrent_matches_sequential():
    client = JevClient(MockBackend())
    reqs = [(f"state {i}", {"q": noul("Is it even?", true=f"state {i}")}) for i in range(6)]
    seq = [r.answers_dict() for r in client.ask_many(reqs)]
    par = [r.answers_dict() for r in client.ask_many(reqs, concurrency=3)]
    assert seq == par
    assert client.total_calls == 12


def test_scripted_backend_queue_and_callable():
    client = JevClient(ScriptedBackend([{"q": {"type": "noul", "noul": 0.2}}]))
    assert client.ask("s", {"q": noul("?")}).noul("q") == 0.2
    with pytest.raises(JevError):
        client.ask("s", {"q": noul("?")})
    fn = ScriptedBackend(lambda body: {"q": {"type": "noul", "noul": 1.0 if "yes" in body["state"] else 0.0}})
    client = JevClient(fn)
    assert client.ask("yes please", {"q": noul("?")}).noul("q") == 1.0
    assert client.ask("no", {"q": noul("?")}).noul("q") == 0.0


def test_empty_questions_rejected():
    with pytest.raises(JevError):
        JevClient(MockBackend()).ask("s", {})
