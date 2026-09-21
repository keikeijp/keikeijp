"""Jev クライアント本体。

```python
from jevlab import Jev, Choice, Score, Noul

jev = Jev()  # 環境変数からバックエンドを選ぶ (TYPESAFE_API_KEY / OPENROUTER_API_KEY / なければ mock)
d = jev.decide(
    state={"ticket": "二重に課金された。今日中に返金して"},
    questions={
        "intent": Choice({"refund": "返金要求", "bug": "不具合報告", "other": None}, "主な要求は?"),
        "urgency": Score(["急がない", "今週中", "今日中"], "緊急度は?"),
        "angry": Noul("怒っているか?"),
    },
)
d.choice("intent").choice, d.score("urgency").level, d.noul("angry").noul
```
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from jevlab.core.answers import ChoiceAnswer, Decision, NoulAnswer, ScoreAnswer, parse_decision
from jevlab.core.backends import Backend, JevError, MockBackend, backend_from_env
from jevlab.core.questions import Choice, Noul, Question, Score, questions_to_wire

MAX_STATE_CHARS = 100_000  # 32k トークンの目安。超えたら末尾を落として警告


class DecisionCache:
    """(state, questions) → Decision の LRU キャッシュ。同じ状態を何度も判定する UI 系で効く。"""

    def __init__(self, capacity: int = 512):
        self.capacity = capacity
        self._items: OrderedDict[str, Decision] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(state: Any, questions: Mapping[str, Any], model: str) -> str:
        payload = json.dumps({"s": state, "q": questions, "m": model}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Decision | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: str, decision: Decision) -> None:
        with self._lock:
            self._items[key] = decision
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)


class Jev:
    def __init__(self, backend: Backend | str | None = None, cache: DecisionCache | bool | None = None, max_workers: int = 8, **backend_kwargs: Any):
        if backend is None or isinstance(backend, str):
            backend = backend_from_env(backend, **backend_kwargs)
        self.backend = backend
        if cache is True:
            cache = DecisionCache()
        self.cache = cache if isinstance(cache, DecisionCache) else None
        self.max_workers = max_workers
        self.total_calls = 0
        self.total_latency_ms = 0.0
        self._lock = threading.Lock()

    # -- 基本呼び出し -------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return isinstance(self.backend, MockBackend)

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        wire = questions_to_wire(questions)
        state = _bound_state(state)
        model_hint = getattr(self.backend, "model", getattr(self.backend, "name", ""))
        key = DecisionCache.key(state, wire, str(model_hint)) if self.cache else None
        if key is not None:
            cached = self.cache.get(key)  # type: ignore[union-attr]
            if cached is not None:
                return cached
        started = time.perf_counter()
        body, model = self.backend.decide(state, wire)
        latency_ms = (time.perf_counter() - started) * 1000
        decision = parse_decision(body, wire, self.backend.name, latency_ms)
        if not decision.model:
            decision.model = model
        with self._lock:
            self.total_calls += 1
            self.total_latency_ms += latency_ms
        if key is not None:
            self.cache.put(key, decision)  # type: ignore[union-attr]
        return decision

    def decide_many(self, items: Iterable[tuple[Any, Mapping[str, Question]]]) -> list[Decision]:
        """複数の (state, questions) を並列に判定。順序は入力通り。"""
        items = list(items)
        if len(items) <= 1:
            return [self.decide(state, questions) for state, questions in items]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(lambda item: self.decide(item[0], item[1]), items))

    # -- 便利メソッド -------------------------------------------------------

    def choose(self, state: Any, options: Mapping[str, Any] | Sequence[str], instructions: Any = None) -> ChoiceAnswer:
        if isinstance(options, Mapping):
            question = Choice(options, instructions)
        else:
            question = Choice.of(*options, instructions=instructions)
        return self.decide(state, {"answer": question}).choice("answer")

    def score(self, state: Any, rubric: Sequence[Any], instructions: Any = None) -> ScoreAnswer:
        return self.decide(state, {"answer": Score(rubric, instructions)}).score("answer")

    def judge(self, state: Any, instructions: Any, criteria: Mapping[str, Any] | None = None) -> NoulAnswer:
        return self.decide(state, {"answer": Noul(instructions, criteria)}).noul("answer")

    def judge_many(self, states: Iterable[Any], instructions: Any, criteria: Mapping[str, Any] | None = None) -> list[NoulAnswer]:
        question = {"answer": Noul(instructions, criteria)}
        return [decision.noul("answer") for decision in self.decide_many((state, question) for state in states)]

    def rank(self, candidates: Sequence[Any], instructions: Any, rubric: Sequence[Any] | None = None, context: Any = None) -> list[tuple[Any, float]]:
        """候補を Jev の score で並べ替える。rubric 省略時は 5 段階。"""
        rubric = list(rubric or ["全く関係ない", "少し関係する", "関係する", "とても関係する", "完全に一致する"])
        question = {"answer": Score(rubric, instructions)}
        states = [({"context": context, "candidate": candidate} if context is not None else candidate) for candidate in candidates]
        decisions = self.decide_many((state, question) for state in states)
        scored = [(candidate, decision.score("answer").score) for candidate, decision in zip(candidates, decisions)]
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def filter(self, candidates: Sequence[Any], instructions: Any, threshold: float = 0.5, context: Any = None) -> list[Any]:
        """noul で候補を絞り込む。"""
        states = [({"context": context, "candidate": candidate} if context is not None else candidate) for candidate in candidates]
        answers = self.judge_many(states, instructions)
        return [candidate for candidate, answer in zip(candidates, answers) if answer.noul >= threshold]

    def classify(self, state: Any, categories: Mapping[str, Any] | Sequence[str], instructions: Any = None, min_confidence: float = 0.0) -> str | None:
        """confidence が閾値未満なら None (人間や大きい LLM に回す)。"""
        answer = self.choose(state, categories, instructions)
        if answer.confidence < min_confidence:
            return None
        return answer.choice

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.backend.name,
            "calls": self.total_calls,
            "avg_latency_ms": round(self.total_latency_ms / self.total_calls, 1) if self.total_calls else 0.0,
            "cache_hits": self.cache.hits if self.cache else 0,
            "cache_misses": self.cache.misses if self.cache else 0,
        }


def _bound_state(state: Any) -> Any:
    """state が長すぎる場合に末尾を切る。構造化 state はそのまま (呼び出し側で削る)。"""
    if isinstance(state, str) and len(state) > MAX_STATE_CHARS:
        return state[:MAX_STATE_CHARS] + "\n...[truncated]"
    return state


__all__ = ["Jev", "DecisionCache", "JevError"]
