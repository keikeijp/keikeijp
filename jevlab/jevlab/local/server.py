"""30. openjev-sglang: TypeSafe 互換 + OpenRouter Decisions 互換の HTTP API をローカルモデルで提供する。

元ネタ: ekzhang/openjev-sglang

エンドポイント:
  POST /v1/systemone          TypeSafe 形: {state, model, questions} → {answers, model, usage}
  POST /api/alpha/decisions   同じ入力 → TypeSafe 形 `answers` + OpenRouter 形 (質問名をトップレベルに) を両方返す
  GET  /v1/models             {"object": "list", "data": [...]}
  GET  /healthz               {"status": "ok", "engine": ...}
認証: 環境変数 JEV_SERVER_TOKEN があれば `Authorization: Bearer <token>` を要求。

エンジン (--engine): fake | semif | jevlike | nanojev | mock。
  mock   = core MockBackend、semif = LocalLogitBackend (--fake で FakeLogitModel)、
  jevlike = JevlikeBackend、nanojev = NanoJevBackend (--fake で FakeSlotModel)、fake = semif --fake と同じ。
  sglang = SGLangLogitModel: SGLang / vLLM の OpenAI 互換 chat completion に max_tokens=1, logprobs=True,
           top_logprobs=20 で問い合わせ、`top_logprobs` から候補トークンの確率を読む。

usage / cost は常に 0 (ローカル推論。B200 級 GPU 箱で常駐させる想定)。

```
jevlab serve --engine fake --port 8787
curl -X POST localhost:8787/v1/systemone -d '{"state": "...", "questions": {"q": {"type": "noul", "instructions": "?"}}}'
```
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jevlab.core.backends import Backend, JevError, MockBackend
from jevlab.core.questions import questions_to_wire

SERVER_NAME = "openjev-sglang/jevlab"
ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0}

# ---------------------------------------------------------------------------
# top_logprobs の解析 (純関数) と SGLang / vLLM エンジン
# ---------------------------------------------------------------------------


def parse_top_logprobs(response: Mapping[str, Any], candidates: list[str], floor_gap: float = 5.0) -> dict[str, float]:
    """OpenAI 互換 chat completion レスポンスの choices[0].logprobs.content[0].top_logprobs から候補の log 確率を読む。

    トークンは前後の空白を落として大文字小文字を無視して照合。見つからない候補は (観測最小値 - floor_gap)。
    """
    try:
        content = response["choices"][0]["logprobs"]["content"]
        top = content[0].get("top_logprobs") or []
    except (KeyError, IndexError, TypeError) as error:
        raise JevError(f"logprobs がレスポンスにありません: {error}") from error
    seen: dict[str, float] = {}
    for entry in top:
        token = str(entry.get("token", "")).strip().lower()
        value = float(entry.get("logprob", -math.inf))
        if token and (token not in seen or value > seen[token]):
            seen[token] = value
    lowest = min(seen.values()) if seen else -20.0
    return {c: seen.get(c.strip().lower(), lowest - floor_gap) for c in candidates}


class SGLangLogitModel:
    """SGLang / vLLM の OpenAI 互換サーバを semif の LogitModel として使う。`LocalLogitBackend(model=SGLangLogitModel(...))`。"""

    def __init__(self, base_url: str = "http://localhost:30000/v1", model: str = "default", timeout: float = 10.0, api_key: str | None = None, top_logprobs: int = 20):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"sglang:{model}"
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("SGLANG_API_KEY", "").strip() or None
        self.top_logprobs = top_logprobs

    def request_body(self, prompt: str) -> dict[str, Any]:
        return {"model": self.model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": self.top_logprobs}

    def next_token_logprobs(self, prompt: str, candidate_tokens: list[str]) -> dict[str, float]:
        data = json.dumps(self.request_body(prompt)).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise JevError(f"SGLang エラー {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from error
        except (urllib.error.URLError, OSError) as error:
            raise JevError(f"SGLang に接続できません ({self.base_url}): {error}") from error
        return parse_top_logprobs(body, candidate_tokens)


# ---------------------------------------------------------------------------
# エンジン選択
# ---------------------------------------------------------------------------


def build_engine(name: str, fake: bool = False, model: str | None = None, sglang_url: str | None = None) -> Backend:
    name = name.lower()
    if name == "mock":
        return MockBackend()
    if name in {"fake", "semif"}:
        from jevlab.local.semif import FakeLogitModel, HFLogitModel, LocalLogitBackend

        if fake or name == "fake":
            return LocalLogitBackend(model=FakeLogitModel())
        return LocalLogitBackend(model=HFLogitModel(model) if model else None)
    if name == "sglang":
        from jevlab.local.semif import LocalLogitBackend

        return LocalLogitBackend(model=SGLangLogitModel(sglang_url or "http://localhost:30000/v1", model or "default"))
    if name == "jevlike":
        from jevlab.local.jevlike import HashedLinearScorer, JevlikeBackend

        return JevlikeBackend(HashedLinearScorer.load(model) if model else None)
    if name == "nanojev":
        from jevlab.local.nanojev import FakeSlotModel, HFSlotModel, NanoJevBackend

        return NanoJevBackend(FakeSlotModel() if fake else HFSlotModel(model) if model else None)
    raise JevError(f"不明なエンジン: {name}")


# ---------------------------------------------------------------------------
# リクエスト処理 (ソケット非依存)
# ---------------------------------------------------------------------------


def _openrouter_answer(answer: Mapping[str, Any]) -> dict[str, Any]:
    """TypeSafe 形の回答 → OpenRouter 形 (winner / score+levels / probability)。"""
    qtype = answer.get("type")
    if qtype == "choice":
        return {"winner": answer["choice"], "confidence": answer.get("confidence", 0.0), "probabilities": dict(answer.get("probabilities", {}))}
    if qtype == "score":
        legend = answer.get("legend") or {}
        levels = [legend[k] for k in sorted(legend, key=int)] if legend else []
        return {"score": answer["score"], "confidence": answer.get("confidence", 0.0), "probabilities": dict(answer.get("probabilities", {})), "levels": levels}
    return {"probability": answer["noul"]}


class Server:
    """エンジンと認証トークンを持ち、`handle(path, body, method, headers)` で JSON 応答を作る。"""

    def __init__(self, engine: Backend, token: str | None = None, model_name: str | None = None):
        self.engine = engine
        self.token = token if token is not None else (os.environ.get("JEV_SERVER_TOKEN", "").strip() or None)
        self.model_name = model_name or str(getattr(engine, "model", None) or f"local/{engine.name}")
        self.requests = 0
        self._lock = threading.Lock()

    def authorized(self, headers: Mapping[str, str] | None) -> bool:
        if not self.token:
            return True
        auth = ""
        for key, value in (headers or {}).items():
            if key.lower() == "authorization":
                auth = value
        return auth == f"Bearer {self.token}"

    def handle(self, path: str, body: Any = None, method: str = "POST", headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            return 200, {"status": "ok", "engine": self.engine.name, "model": self.model_name, "server": SERVER_NAME}
        if not self.authorized(headers):
            return 401, {"error": {"type": "unauthorized", "message": "Bearer token required"}}
        if path == "/v1/models":
            if method != "GET":
                return 405, {"error": {"type": "method_not_allowed", "message": "GET only"}}
            return 200, {"object": "list", "data": [{"id": self.model_name, "object": "model", "owned_by": "local", "engine": self.engine.name}]}
        if path in {"/v1/systemone", "/api/alpha/decisions"}:
            if method != "POST":
                return 405, {"error": {"type": "method_not_allowed", "message": "POST only"}}
            if isinstance(body, (bytes, str)):
                try:
                    body = json.loads(body or "{}")
                except json.JSONDecodeError as error:
                    return 400, {"error": {"type": "invalid_json", "message": str(error)}}
            if not isinstance(body, Mapping):
                return 400, {"error": {"type": "invalid_request", "message": "JSON object expected"}}
            return self._decide(path, body)
        return 404, {"error": {"type": "not_found", "message": f"unknown path {path}"}}

    def _decide(self, path: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if "state" not in body:
            return 400, {"error": {"type": "invalid_request", "message": "state is required"}}
        questions = body.get("questions")
        if not isinstance(questions, Mapping) or not questions:
            return 400, {"error": {"type": "invalid_request", "message": "questions must be a non-empty object"}}
        try:
            wire = questions_to_wire(questions)
        except (ValueError, TypeError) as error:
            return 400, {"error": {"type": "invalid_question", "message": str(error)}}
        try:
            result, model = self.engine.decide(body["state"], wire)
        except JevError as error:
            return 502, {"error": {"type": "engine_error", "message": str(error)}}
        with self._lock:
            self.requests += 1
        answers = result.get("answers") or {name: result[name] for name in wire if isinstance(result.get(name), Mapping)}
        response: dict[str, Any] = {"answers": answers, "model": str(body.get("model") or model or self.model_name), "usage": dict(ZERO_USAGE)}
        if path == "/api/alpha/decisions":
            for name, answer in answers.items():
                response[name] = _openrouter_answer(answer)
            response["cost"] = "$0"
        return 200, response


def make_handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = SERVER_NAME

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 静かにする
            pass

        def _respond(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(*server.handle(self.path, None, "GET", dict(self.headers.items())))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self._respond(*server.handle(self.path, raw.decode("utf-8", "replace"), "POST", dict(self.headers.items())))

    return Handler


def make_http_server(server: Server, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """port=0 で空きポートを取る。`.server_address[1]` で実ポートが分かる。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(server))
    httpd.daemon_threads = True
    return httpd


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab serve", description="TypeSafe / OpenRouter 互換のローカル Jev API サーバ")
    parser.add_argument("--backend", default=None, help="--engine の別名 (規約互換)")
    parser.add_argument("--engine", default="fake", help="fake | semif | jevlike | nanojev | mock | sglang")
    parser.add_argument("--fake", action="store_true", help="semif / nanojev で実モデルを読まない")
    parser.add_argument("--model", default=None, help="HF モデル ID / jevlike の model.json / sglang のモデル名")
    parser.add_argument("--sglang-url", default=None, help="SGLang / vLLM の OpenAI 互換 URL (例 http://localhost:30000/v1)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None, help="Bearer トークン (既定 JEV_SERVER_TOKEN)")
    parser.add_argument("--dry-run", action="store_true", help="起動せず、サンプルリクエストを handle() に通して表示")
    args = parser.parse_args(argv)
    try:
        server = Server(build_engine(args.backend or args.engine, args.fake, args.model, args.sglang_url), args.token)
        if args.dry_run:
            sample = {"state": "二重に課金された。今日中に返金して", "questions": {"intent": {"type": "choice", "criteria": {"refund": "返金要求", "bug": "不具合", "other": None}}, "urgent": {"type": "noul", "instructions": "今日中か?"}}}
            for path in ("/healthz", "/v1/systemone", "/api/alpha/decisions"):
                status, payload = server.handle(path, sample, "GET" if path == "/healthz" else "POST", {"Authorization": f"Bearer {server.token}"} if server.token else None)
                print(path, status, json.dumps(payload, ensure_ascii=False))
            return 0
        httpd = make_http_server(server, args.host, args.port)
        print(f"listening on http://{args.host}:{httpd.server_address[1]} engine={server.engine.name} auth={'on' if server.token else 'off'}", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover
            pass
        finally:
            httpd.server_close()
    except JevError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
