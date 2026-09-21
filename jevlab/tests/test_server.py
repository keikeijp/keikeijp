import json
import threading
import urllib.error
import urllib.request

import pytest

from jevlab.core import MockBackend
from jevlab.local import server as server_mod
from jevlab.local.server import SGLangLogitModel, Server, build_engine, make_http_server, parse_top_logprobs

SAMPLE = {"state": "charged twice, refund me today", "questions": {"intent": {"type": "choice", "criteria": {"refund": "money back", "bug": "crash"}}, "urgency": {"type": "score", "criteria": ["low", "high"]}, "today": {"type": "noul", "instructions": "today?"}}}


def test_handle_typesafe_and_openrouter_endpoints():
    server = Server(MockBackend(), token=None)
    status, body = server.handle("/v1/systemone", SAMPLE)
    assert status == 200
    assert set(body) == {"answers", "model", "usage"} and body["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert body["answers"]["intent"]["type"] == "choice"
    status, body = server.handle("/api/alpha/decisions", json.dumps(SAMPLE))
    assert status == 200
    assert body["answers"]["intent"]["choice"] == body["intent"]["winner"]
    assert body["urgency"]["levels"] == ["low", "high"] and "probability" in body["today"] and body["cost"] == "$0"
    assert server.handle("/healthz", None, "GET")[0] == 200
    status, models = server.handle("/v1/models", None, "GET")
    assert status == 200 and models["data"][0]["object"] == "model"
    assert server.handle("/nope", {})[0] == 404
    assert server.handle("/v1/systemone", "{not json")[0] == 400
    assert server.handle("/v1/systemone", {"state": "x"})[0] == 400
    assert server.handle("/v1/systemone", {"state": "x", "questions": {"q": {"type": "bogus"}}})[0] == 400
    assert server.handle("/v1/models", None, "POST")[0] == 405


def test_auth_failure_and_success(monkeypatch):
    monkeypatch.setenv("JEV_SERVER_TOKEN", "secret")
    server = Server(MockBackend())
    assert server.handle("/v1/systemone", SAMPLE)[0] == 401
    assert server.handle("/v1/systemone", SAMPLE, headers={"authorization": "Bearer wrong"})[0] == 401
    assert server.handle("/v1/systemone", SAMPLE, headers={"Authorization": "Bearer secret"})[0] == 200
    assert server.handle("/healthz", None, "GET")[0] == 200  # ヘルスチェックは認証不要


def test_parse_top_logprobs_fixture():
    fixture = {"choices": [{"message": {"content": "A"}, "logprobs": {"content": [{"token": "A", "logprob": -0.05, "top_logprobs": [{"token": "A", "logprob": -0.05}, {"token": " B", "logprob": -3.2}, {"token": "b", "logprob": -4.0}, {"token": "\n", "logprob": -9.0}]}]}}]}
    logprobs = parse_top_logprobs(fixture, ["A", "B", "C"])
    assert logprobs["A"] == -0.05 and logprobs["B"] == -3.2
    assert logprobs["C"] < logprobs["B"]  # 未観測は最小値未満
    with pytest.raises(server_mod.JevError):
        parse_top_logprobs({"choices": [{}]}, ["A"])
    body = SGLangLogitModel("http://localhost:30000/v1", "qwen").request_body("p")
    assert body["max_tokens"] == 1 and body["logprobs"] is True and body["top_logprobs"] == 20


def test_build_engine_variants():
    assert build_engine("mock").name == "mock"
    assert build_engine("fake").name == "local"
    assert build_engine("semif", fake=True).model == "fake-logit"
    assert build_engine("jevlike").name == "jevlike"
    assert build_engine("nanojev", fake=True).name == "nanojev"
    assert build_engine("sglang", model="qwen").model == "sglang:qwen"
    with pytest.raises(server_mod.JevError):
        build_engine("unknown")


def test_real_socket_roundtrip():
    server = Server(MockBackend(), token="tok")
    httpd = make_http_server(server, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/alpha/decisions", data=json.dumps(SAMPLE).encode(), method="POST", headers={"Authorization": "Bearer tok", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            body = json.loads(response.read())
        assert body["intent"]["winner"] in {"refund", "bug"} and body["answers"]["today"]["type"] == "noul"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/models"), timeout=2)
        assert error.value.code == 401
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            assert json.loads(response.read())["status"] == "ok"
        assert server.requests == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_main_dry_run(capsys):
    assert server_mod.main(["--engine", "fake", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "/v1/systemone 200" in out and "/api/alpha/decisions 200" in out
