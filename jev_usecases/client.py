"""TypeSafe AI System One API (`POST /v1/systemone`) の最小クライアント。

依存は `requests` のみ。公式 SDK (`pip install typesafe-sdk`) と同じワイヤ形式:

    request : {"state": str|dict|list, "model": "jev-latest", "questions": {name: question}}
    response: {"model": str, "answers": {name: answer}, "usage": {"input_tokens", "output_tokens"}}

    answer(noul)  : {"type": "noul",   "noul": 0.93}
    answer(choice): {"type": "choice", "choice": "billing", "confidence": 0.88, "probabilities": {...}}
    answer(score) : {"type": "score",  "score": 1.7, "confidence": 0.8, "legend": {"0": ...}, "probabilities": {"0": ...}}

バックエンドは差し替え可能:
  - HttpBackend     : 本物の API を呼ぶ (TYPESAFE_API_KEY が必要)
  - MockBackend     : キーワード一致で決定論的に答えるオフライン用の代替。Jev の精度は再現しない
  - ScriptedBackend : テスト用。用意した回答をそのまま返す
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
SYSTEM_ONE_PATH = "/v1/systemone"
MODELS_PATH = "/v1/models"
API_KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL_ENV = "TYPESAFE_BASE_URL"
MODEL_ENV = "TYPESAFE_DEFAULT_MODEL"

# 記事・公式ページ記載の通常単価 (入力 100 万トークンあたり USD)。出力は無料。
INPUT_USD_PER_MTOKEN = 0.042
RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})


class JevError(Exception):
    """クライアント側のエラー。"""


class JevAPIError(JevError):
    """API が 2xx 以外を返した。"""

    def __init__(self, status: int, body: Any, request_id: str | None = None):
        self.status = status
        self.body = body
        self.request_id = request_id
        super().__init__(f"TypeSafe API error {status}: {json.dumps(body, ensure_ascii=False)[:300]}")


def estimate_cost_usd(input_tokens: int, usd_per_mtoken: float = INPUT_USD_PER_MTOKEN) -> float:
    return input_tokens / 1_000_000 * usd_per_mtoken


# ---------------------------------------------------------------- answers


@dataclass(frozen=True)
class NoulAnswer:
    noul: float

    @property
    def yes(self) -> bool:
        return self.noul >= 0.5

    def to_dict(self) -> dict:
        return {"type": "noul", "noul": self.noul}


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])

    def to_dict(self) -> dict:
        return {"type": "choice", "choice": self.choice, "confidence": self.confidence, "probabilities": dict(self.probabilities)}


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    confidence: float
    legend: dict[int, Any]
    probabilities: dict[int, float]

    @property
    def max_level(self) -> int:
        return max(self.legend) if self.legend else 0

    @property
    def normalized(self) -> float:
        """0.0 〜 1.0 に正規化した期待値。"""
        return self.score / self.max_level if self.max_level else 0.0

    @property
    def level(self) -> int:
        return int(round(self.score))

    @property
    def label(self) -> Any:
        return self.legend.get(self.level)

    def to_dict(self) -> dict:
        return {
            "type": "score",
            "score": self.score,
            "confidence": self.confidence,
            "legend": {str(k): v for k, v in self.legend.items()},
            "probabilities": {str(k): v for k, v in self.probabilities.items()},
        }


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


def parse_answer(raw: Mapping[str, Any]) -> Answer:
    kind = raw.get("type")
    if kind == "noul":
        return NoulAnswer(float(raw["noul"]))
    if kind == "choice":
        return ChoiceAnswer(str(raw["choice"]), float(raw.get("confidence", 0.0)), {k: float(v) for k, v in raw.get("probabilities", {}).items()})
    if kind == "score":
        return ScoreAnswer(
            float(raw["score"]),
            float(raw.get("confidence", 0.0)),
            {int(k): v for k, v in raw.get("legend", {}).items()},
            {int(k): float(v) for k, v in raw.get("probabilities", {}).items()},
        )
    raise JevError(f"unknown answer type: {kind!r}")


@dataclass
class Result:
    answers: dict[str, Answer]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def cost_usd(self) -> float:
        return estimate_cost_usd(self.input_tokens)

    def noul(self, name: str) -> float:
        a = self.answers[name]
        if not isinstance(a, NoulAnswer):
            raise JevError(f"{name} is not a noul answer")
        return a.noul

    def choice(self, name: str) -> ChoiceAnswer:
        a = self.answers[name]
        if not isinstance(a, ChoiceAnswer):
            raise JevError(f"{name} is not a choice answer")
        return a

    def score(self, name: str) -> ScoreAnswer:
        a = self.answers[name]
        if not isinstance(a, ScoreAnswer):
            raise JevError(f"{name} is not a score answer")
        return a

    def answers_dict(self) -> dict[str, dict]:
        return {k: v.to_dict() for k, v in self.answers.items()}


# ---------------------------------------------------------------- backends


class Backend(Protocol):
    def system_one(self, body: dict) -> dict: ...


class HttpBackend:
    """本物の TypeSafe API。429 / 5xx / 接続エラーは指数バックオフで再試行する。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = api_key or os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            raise JevError(f"API キーがありません。api_key を渡すか環境変数 {API_KEY_ENV} を設定してください。")
        self._api_key = key
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "jev-usecases/0.1",
        }

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        url = self.base_url + path
        attempt = 0
        while True:
            try:
                resp = self._session.request(method, url, headers=self._headers(), json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise JevError(f"接続エラー: {exc}") from exc
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(_retry_after(resp) or _backoff(attempt))
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                raise JevAPIError(resp.status_code, payload, resp.headers.get("x-typesafe-request-id"))
            try:
                return resp.json()
            except ValueError as exc:
                raise JevError("API の応答が JSON ではありません") from exc

    def system_one(self, body: dict) -> dict:
        return self._request("POST", SYSTEM_ONE_PATH, body)

    def list_models(self) -> list[dict]:
        return list(self._request("GET", MODELS_PATH, None).get("models", []))


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * (2**attempt))


def _retry_after(resp: requests.Response) -> float | None:
    ms = resp.headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    s = resp.headers.get("retry-after")
    if s:
        try:
            return max(0.0, float(s))
        except ValueError:
            pass
    return None


_WORD_RE = re.compile(r"[a-zA-Z0-9_\-\.\+/]{2,}|[぀-ヿ一-鿿]{2,}")
_STOP = frozenset(
    "the a an of to in on for is are be this that it and or with does do did was were as at by from into than then "
    "not no yes any all one two about which what who how when where its their there here have has had can could should "
    "would will may might must if else message text state item post ticket customer user".split()
)


def _tokens(value: Any) -> set[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {t.lower() for t in _WORD_RE.findall(text)} - _STOP


class MockBackend:
    """オフラインで動く決定論的な代替。

    質問文・基準の説明と state の語彙一致で確率を作る。Jev の判断精度を再現するものではなく、
    パイプラインの配線確認・デモ・テストのためのもの。
    """

    def __init__(self, *, model_name: str = "mock-jev", tokens_per_char: float = 0.35):
        self.model_name = model_name
        self.tokens_per_char = tokens_per_char
        self.calls: list[dict] = []

    def system_one(self, body: dict) -> dict:
        self.calls.append(body)
        state_tokens = _tokens(body["state"])
        answers = {name: self._answer(q, state_tokens) for name, q in body["questions"].items()}
        n_chars = len(json.dumps(body, ensure_ascii=False))
        return {
            "model": self.model_name,
            "answers": answers,
            "usage": {"input_tokens": int(n_chars * self.tokens_per_char), "output_tokens": len(answers)},
        }

    @staticmethod
    def _overlap(desc: Any, state_tokens: set[str]) -> int:
        return len(_tokens(desc) & state_tokens)

    def _answer(self, q: Mapping[str, Any], state_tokens: set[str]) -> dict:
        kind = q["type"]
        if kind == "noul":
            crit = q.get("criteria") or {}
            words = _tokens(crit.get("true")) if crit.get("true") is not None else _tokens(q.get("instructions"))
            hit = len(words & state_tokens)
            denom = max(1, min(len(words), 6))
            p = min(0.97, 0.12 + 0.85 * hit / denom)
            return {"type": "noul", "noul": round(p, 4)}
        if kind == "choice":
            weights = {label: 0.5 + self._overlap(label, state_tokens) + self._overlap(desc, state_tokens) for label, desc in q["criteria"].items()}
            total = sum(weights.values())
            probs = {k: round(v / total, 4) for k, v in weights.items()}
            best = max(probs.items(), key=lambda kv: kv[1])
            second = sorted(probs.values())[-2] if len(probs) > 1 else 0.0
            return {"type": "choice", "choice": best[0], "confidence": round(min(0.99, best[1] - second + 0.5), 4), "probabilities": probs}
        if kind == "score":
            levels = q["criteria"]
            weights = [0.5 + self._overlap(desc, state_tokens) for desc in levels]
            total = sum(weights)
            probs = {str(i): round(w / total, 4) for i, w in enumerate(weights)}
            expected = sum(i * w / total for i, w in enumerate(weights))
            return {
                "type": "score",
                "score": round(expected, 4),
                "confidence": round(max(probs.values()), 4),
                "legend": {str(i): desc for i, desc in enumerate(levels)},
                "probabilities": probs,
            }
        raise JevError(f"unknown question type: {kind!r}")


class ScriptedBackend:
    """テスト用: 回答 (answers 辞書) の列、または body から answers を作る関数を渡す。"""

    def __init__(self, script: Sequence[Mapping[str, Any]] | Callable[[dict], Mapping[str, Any]], *, model_name: str = "scripted"):
        self._fn = script if callable(script) else None
        self._queue = list(script) if not callable(script) else []
        self.model_name = model_name
        self.calls: list[dict] = []

    def system_one(self, body: dict) -> dict:
        self.calls.append(body)
        if self._fn is not None:
            answers = self._fn(body)
        else:
            if not self._queue:
                raise JevError("ScriptedBackend: 用意した回答を使い切りました")
            answers = self._queue.pop(0)
        return {"model": self.model_name, "answers": dict(answers), "usage": {"input_tokens": 100, "output_tokens": len(answers)}}


# ---------------------------------------------------------------- client


class JevClient:
    def __init__(self, backend: Backend | None = None, *, model: str | None = None):
        self.backend: Backend = backend or HttpBackend()
        self.model = model or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self.total_input_tokens = 0
        self.total_calls = 0

    @classmethod
    def from_env(cls, backend: str = "http", *, model: str | None = None) -> "JevClient":
        if backend == "http":
            return cls(HttpBackend(), model=model)
        if backend == "mock":
            return cls(MockBackend(), model=model or "mock-jev")
        raise JevError(f"unknown backend: {backend!r} (http | mock)")

    @staticmethod
    def build_body(state: Any, questions: Mapping[str, Mapping[str, Any]], model: str) -> dict:
        if not questions:
            raise JevError("questions は 1 つ以上必要です")
        return {"state": state, "model": model, "questions": {k: dict(v) for k, v in questions.items()}}

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]], *, model: str | None = None) -> Result:
        body = self.build_body(state, questions, model or self.model)
        t0 = time.perf_counter()
        raw = self.backend.system_one(body)
        latency = (time.perf_counter() - t0) * 1000
        usage = raw.get("usage") or {}
        result = Result(
            answers={name: parse_answer(a) for name, a in raw.get("answers", {}).items()},
            model=str(raw.get("model", body["model"])),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency,
            raw=raw,
        )
        self.total_input_tokens += result.input_tokens
        self.total_calls += 1
        return result

    def ask_many(
        self,
        requests_: Iterable[tuple[Any, Mapping[str, Mapping[str, Any]]]],
        *,
        concurrency: int = 1,
        model: str | None = None,
    ) -> list[Result]:
        """(state, questions) の列を評価する。Jev は 1 リクエスト 1 state なので並列度で速度を稼ぐ。"""
        items = list(requests_)
        if concurrency <= 1 or len(items) <= 1:
            return [self.ask(s, q, model=model) for s, q in items]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(lambda sq: self.ask(sq[0], sq[1], model=model), items))

    @property
    def total_cost_usd(self) -> float:
        return estimate_cost_usd(self.total_input_tokens)
