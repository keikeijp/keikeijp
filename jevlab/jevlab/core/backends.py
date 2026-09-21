"""Jev バックエンド。

- TypeSafeBackend   : https://api.typesafe.ai/v1/systemone (公式)
- OpenRouterBackend : https://openrouter.ai/api/alpha/decisions (ウェイトリスト不要)
- MockBackend       : API 不要の決定的ヒューリスティック。テストとオフライン開発用
- ScriptedBackend   : テスト用。あらかじめ用意した回答を順番に返す
- FunctionBackend   : 任意の関数で回答を作る (ルールベースやローカルモデルの差し込み口)

どのバックエンドも `decide(state, questions_wire) -> (body_dict, model_name)` を実装する。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

JSONContent = Any


class JevError(RuntimeError):
    pass


class JevAPIError(JevError):
    def __init__(self, status: int, body: str, request_id: str | None = None):
        super().__init__(f"Jev API error {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.request_id = request_id


class Backend(Protocol):
    name: str

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]: ...


# ---------------------------------------------------------------------------
# HTTP 共通
# ---------------------------------------------------------------------------


def _post_json(url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float, retries: int = 2) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="replace")
            if error.code in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < retries:
                retry_after = error.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 0.5 * (2**attempt))
                last_error = JevAPIError(error.code, text, error.headers.get("x-typesafe-request-id"))
                continue
            raise JevAPIError(error.code, text, error.headers.get("x-typesafe-request-id")) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
                continue
            raise JevError(f"Jev API に接続できません: {error}") from error
    raise JevError(f"Jev API 呼び出しに失敗しました: {last_error}")


class TypeSafeBackend:
    """TypeSafe 公式エンドポイント。"""

    name = "typesafe"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY が設定されていません")
        self.model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or "jev-latest"
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai").rstrip("/")
        self.timeout = timeout

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        body = {"state": state, "model": self.model, "questions": dict(questions)}
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "jevlab/0.1", "X-TypeSafe-SDK": "jevlab/0.1"}
        response = _post_json(f"{self.base_url}/v1/systemone", body, headers, self.timeout)
        return response, str(response.get("model", self.model))


class OpenRouterBackend:
    """OpenRouter の Decisions (alpha) エンドポイント。"""

    name = "openrouter"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 15.0, app_title: str = "jevlab"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self.api_key:
            raise JevError("OPENROUTER_API_KEY が設定されていません")
        self.model = model or os.environ.get("OPENROUTER_JEV_MODEL", "").strip() or "typesafe/jev-latest"
        self.url = os.environ.get("OPENROUTER_DECISIONS_URL", "").strip() or "https://openrouter.ai/api/alpha/decisions"
        self.timeout = timeout
        self.app_title = app_title

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        body = {"model": self.model, "state": state, "questions": dict(questions)}
        headers = {"Authorization": f"Bearer {self.api_key}", "X-OpenRouter-Title": self.app_title, "HTTP-Referer": "https://github.com/keikeijp/keikeijp"}
        response = _post_json(self.url, body, headers, self.timeout)
        return response, str(response.get("model", self.model))


# ---------------------------------------------------------------------------
# モック
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+|[぀-ヿ一-鿿]", re.IGNORECASE)


def tokenize(text: Any) -> list[str]:
    """雑だが多言語で動くトークナイザ。JSON はフラットに文字列化する。"""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False) if not isinstance(text, (int, float)) else str(text)
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _bigrams(tokens: list[str]) -> set[str]:
    return {a + b for a, b in zip(tokens, tokens[1:])}


def overlap_score(state_tokens: list[str], text: Any) -> float:
    """状態と説明文の語彙重なり。1 文字の日本語トークンはバイグラムでも比較する。"""
    if text is None:
        return 0.0
    tokens = tokenize(text)
    if not tokens or not state_tokens:
        return 0.0
    state_set = set(state_tokens)
    state_bi = _bigrams(state_tokens)
    hits = 0.0
    for token in tokens:
        if token in state_set:
            hits += 1.0
        elif len(token) >= 4 and any(len(s) >= 4 and (s.startswith(token[:4]) and token.startswith(s[:4])) for s in state_set):
            hits += 0.7  # refund / refunds, 走る / 走った のような語幹一致
    hits += sum(1 for bigram in _bigrams(tokens) if bigram in state_bi)
    return hits / (len(tokens) + len(tokens) - 1 if len(tokens) > 1 else 1)


def softmax(values: Iterable[float], temperature: float = 0.35) -> list[float]:
    xs = list(values)
    if not xs:
        return []
    top = max(xs)
    exps = [math.exp((x - top) / temperature) for x in xs]
    total = sum(exps)
    return [e / total for e in exps]


class MockBackend:
    """API なしで動く決定的な Jev もどき。

    - choice : ラベル名と説明文の語彙が state にどれだけ含まれるかで確率を作る
    - score  : 各レベル説明の語彙重なりで分布を作り、期待値をスコアにする
    - noul   : instructions と state の語彙重なり + 否定語の有無で確率にする

    `hints` に {質問名: 回答} を渡すと固定回答にできる (デモ用)。
    実運用の品質を再現するものではないので、テストとパイプライン開発専用。
    """

    name = "mock"

    def __init__(self, hints: Mapping[str, Any] | None = None, temperature: float = 0.35, seed_bias: float = 0.0):
        self.hints = dict(hints or {})
        self.temperature = temperature
        self.seed_bias = seed_bias
        self.calls: list[dict[str, Any]] = []

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        self.calls.append({"state": state, "questions": dict(questions)})
        state_tokens = tokenize(state)
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            qtype = question["type"]
            hint = self.hints.get(name)
            if qtype == "choice":
                answers[name] = self._choice(state_tokens, question, hint)
            elif qtype == "score":
                answers[name] = self._score(state_tokens, question, hint)
            else:
                answers[name] = self._noul(state_tokens, question, hint)
        input_tokens = len(state_tokens) + sum(len(tokenize(q)) for q in questions.values())
        return {"answers": answers, "model": "mock-jev", "usage": {"input_tokens": input_tokens, "output_tokens": 0}}, "mock-jev"

    def _choice(self, state_tokens: list[str], question: Mapping[str, Any], hint: Any) -> dict[str, Any]:
        criteria = question["criteria"]
        labels = list(criteria)
        if hint is not None and hint in criteria:
            probabilities = {label: (0.9 if label == hint else 0.1 / max(1, len(labels) - 1)) for label in labels}
            return {"type": "choice", "choice": hint, "probabilities": probabilities, "confidence": 0.9}
        raw = []
        for index, label in enumerate(labels):
            score = overlap_score(state_tokens, label.replace("_", " ")) * 1.5 + overlap_score(state_tokens, criteria[label])
            score += self.seed_bias * (len(labels) - index) * 1e-3  # 同点時の安定化
            raw.append(score)
        probs = softmax(raw, self.temperature)
        probabilities = {label: p for label, p in zip(labels, probs)}
        choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
        ranked = sorted(probs, reverse=True)
        confidence = min(1.0, 0.5 + (ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)))
        return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}

    def _score(self, state_tokens: list[str], question: Mapping[str, Any], hint: Any) -> dict[str, Any]:
        criteria = list(question["criteria"])
        legend = {str(i): level for i, level in enumerate(criteria)}
        if isinstance(hint, (int, float)):
            level = int(round(hint))
            probabilities = {str(i): (0.9 if i == level else 0.1 / max(1, len(criteria) - 1)) for i in range(len(criteria))}
            return {"type": "score", "score": float(level), "legend": legend, "probabilities": probabilities, "confidence": 0.9}
        raw = [overlap_score(state_tokens, level) for level in criteria]
        if max(raw) == 0:
            # 手がかりがなければ中央寄りの緩い分布
            middle = (len(criteria) - 1) / 2
            raw = [-abs(i - middle) * 0.1 for i in range(len(criteria))]
        probs = softmax(raw, self.temperature)
        expected = sum(i * p for i, p in enumerate(probs))
        return {
            "type": "score",
            "score": round(expected, 4),
            "legend": legend,
            "probabilities": {str(i): p for i, p in enumerate(probs)},
            "confidence": max(probs),
        }

    _NEGATIONS = ("not ", "no ", "never", "ない", "いいえ", "なし", "false", "off")

    def _noul(self, state_tokens: list[str], question: Mapping[str, Any], hint: Any) -> dict[str, Any]:
        if isinstance(hint, bool):
            return {"type": "noul", "noul": 0.95 if hint else 0.05}
        if isinstance(hint, (int, float)):
            return {"type": "noul", "noul": float(min(1.0, max(0.0, hint)))}
        instructions = question.get("instructions")
        criteria = question.get("criteria") or {}
        base = overlap_score(state_tokens, instructions)
        yes = overlap_score(state_tokens, criteria.get("true")) if isinstance(criteria, Mapping) else 0.0
        no = overlap_score(state_tokens, criteria.get("false")) if isinstance(criteria, Mapping) else 0.0
        negated = any(neg.strip() in state_tokens for neg in self._NEGATIONS)
        logit = (base + yes - no) * 4.0 - (1.5 if negated else 0.0) - 1.0
        probability = 1.0 / (1.0 + math.exp(-logit))
        return {"type": "noul", "noul": probability}


class ScriptedBackend:
    """テスト用: 与えた回答 (質問名 → 回答 dict または簡略値) を順に返す。"""

    name = "scripted"

    def __init__(self, script: Iterable[Mapping[str, Any]] | None = None, default: Mapping[str, Any] | None = None):
        self.script = list(script or [])
        self.default = dict(default or {})
        self.calls: list[dict[str, Any]] = []

    def push(self, answers: Mapping[str, Any]) -> None:
        self.script.append(dict(answers))

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        self.calls.append({"state": state, "questions": dict(questions)})
        planned = self.script.pop(0) if self.script else self.default
        answers = {name: _expand_answer(planned.get(name), question) for name, question in questions.items()}
        return {"answers": answers, "model": "scripted-jev", "usage": {"input_tokens": 0, "output_tokens": 0}}, "scripted-jev"


class FunctionBackend:
    """任意の関数 `fn(state, questions_wire) -> {質問名: 簡略回答}` をバックエンドにする。"""

    name = "function"

    def __init__(self, fn: Callable[[JSONContent, Mapping[str, Mapping[str, Any]]], Mapping[str, Any]], model_name: str = "function-jev"):
        self.fn = fn
        self.model_name = model_name

    def decide(self, state: JSONContent, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        planned = self.fn(state, questions)
        answers = {name: _expand_answer(planned.get(name), question) for name, question in questions.items()}
        return {"answers": answers, "model": self.model_name, "usage": {}}, self.model_name


def _expand_answer(value: Any, question: Mapping[str, Any]) -> dict[str, Any]:
    """簡略値 (ラベル文字列 / レベル整数 / bool / float) を完全な回答 dict に広げる。"""
    qtype = question["type"]
    if isinstance(value, Mapping) and "type" in value:
        return dict(value)
    if qtype == "choice":
        labels = list(question["criteria"])
        if isinstance(value, Mapping):  # {label: prob}
            probabilities = {label: float(value.get(label, 0.0)) for label in labels}
        else:
            chosen = value if value in labels else labels[0]
            probabilities = {label: (0.9 if label == chosen else 0.1 / max(1, len(labels) - 1)) for label in labels}
        choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
        return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": probabilities[choice]}
    if qtype == "score":
        levels = list(question["criteria"])
        legend = {str(i): level for i, level in enumerate(levels)}
        if isinstance(value, Mapping):
            probabilities = {str(i): float(value.get(i, value.get(str(i), 0.0))) for i in range(len(levels))}
        else:
            level = int(round(float(value))) if value is not None else 0
            level = min(max(level, 0), len(levels) - 1)
            probabilities = {str(i): (0.9 if i == level else 0.1 / max(1, len(levels) - 1)) for i in range(len(levels))}
        expected = sum(int(k) * p for k, p in probabilities.items())
        return {"type": "score", "score": expected, "legend": legend, "probabilities": probabilities, "confidence": max(probabilities.values())}
    if isinstance(value, bool):
        return {"type": "noul", "noul": 0.95 if value else 0.05}
    return {"type": "noul", "noul": float(value) if value is not None else 0.5}


def backend_from_env(prefer: str | None = None, **kwargs: Any) -> Backend:
    """環境変数からバックエンドを選ぶ。

    優先順: 引数 prefer > JEV_BACKEND > TYPESAFE_API_KEY があれば typesafe > OPENROUTER_API_KEY があれば openrouter > mock
    """
    choice = (prefer or os.environ.get("JEV_BACKEND", "")).strip().lower()
    if not choice:
        if os.environ.get("TYPESAFE_API_KEY", "").strip():
            choice = "typesafe"
        elif os.environ.get("OPENROUTER_API_KEY", "").strip():
            choice = "openrouter"
        else:
            choice = "mock"
    if choice == "typesafe":
        return TypeSafeBackend(**kwargs)
    if choice == "openrouter":
        return OpenRouterBackend(**kwargs)
    if choice == "mock":
        return MockBackend(**kwargs)
    if choice == "local":
        from jevlab.local.semif import LocalLogitBackend

        return LocalLogitBackend.from_env(**kwargs)
    raise JevError(f"不明な JEV_BACKEND: {choice}")
