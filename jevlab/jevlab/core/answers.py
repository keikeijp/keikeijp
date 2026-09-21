"""Jev の回答オブジェクト。TypeSafe / OpenRouter 双方のレスポンス形を正規化する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Union


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float
    type: str = field(default="choice", init=False)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)

    def margin(self) -> float:
        """1 位と 2 位の確率差。曖昧さの指標。"""
        ranked = self.ranked()
        if len(ranked) < 2:
            return 1.0
        return ranked[0][1] - ranked[1][1]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "choice", "choice": self.choice, "probabilities": dict(self.probabilities), "confidence": self.confidence}


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: dict[int, float]
    legend: dict[int, Any]
    confidence: float
    type: str = field(default="score", init=False)

    @property
    def level(self) -> int:
        """最も確率の高いレベル。"""
        if not self.probabilities:
            return int(round(self.score))
        return max(self.probabilities.items(), key=lambda kv: kv[1])[0]

    @property
    def max_level(self) -> int:
        return max(self.legend) if self.legend else max(self.probabilities) if self.probabilities else 0

    def normalized(self) -> float:
        """0..1 に正規化した期待スコア。"""
        top = self.max_level
        return self.score / top if top else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "score",
            "score": self.score,
            "level": self.level,
            "probabilities": {str(k): v for k, v in self.probabilities.items()},
            "legend": {str(k): v for k, v in self.legend.items()},
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class NoulAnswer:
    noul: float
    type: str = field(default="noul", init=False)

    @property
    def yes(self) -> bool:
        return self.noul >= 0.5

    def to_dict(self) -> dict[str, Any]:
        return {"type": "noul", "noul": self.noul}


Answer = Union[ChoiceAnswer, ScoreAnswer, NoulAnswer]


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: str | None = None


@dataclass
class Decision:
    """1 回の Jev 呼び出しの結果。質問名 → 回答。"""

    answers: dict[str, Answer]
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    backend: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]

    def __contains__(self, name: str) -> bool:
        return name in self.answers

    def choice(self, name: str) -> ChoiceAnswer:
        answer = self.answers[name]
        if not isinstance(answer, ChoiceAnswer):
            raise TypeError(f"{name} は choice ではありません: {answer.type}")
        return answer

    def score(self, name: str) -> ScoreAnswer:
        answer = self.answers[name]
        if not isinstance(answer, ScoreAnswer):
            raise TypeError(f"{name} は score ではありません: {answer.type}")
        return answer

    def noul(self, name: str) -> NoulAnswer:
        answer = self.answers[name]
        if not isinstance(answer, NoulAnswer):
            raise TypeError(f"{name} は noul ではありません: {answer.type}")
        return answer

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": {name: answer.to_dict() for name, answer in self.answers.items()},
            "model": self.model,
            "backend": self.backend,
            "latency_ms": round(self.latency_ms, 1),
            "usage": {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens, "cost": self.usage.cost},
        }


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def parse_answer(raw: Mapping[str, Any], question_wire: Mapping[str, Any] | None = None) -> Answer:
    """TypeSafe 形式 (`type`, `choice`/`score`/`noul`) と OpenRouter 形式 (`winner`/`probability`/`levels`) の両方を受ける。"""
    qtype = raw.get("type") or (question_wire or {}).get("type")
    if qtype is None:
        if "noul" in raw or "probability" in raw:
            qtype = "noul"
        elif "choice" in raw or "winner" in raw:
            qtype = "choice"
        elif "score" in raw:
            qtype = "score"
        else:
            raise ValueError(f"回答タイプを判別できません: {dict(raw)}")

    if qtype == "noul":
        value = raw.get("noul", raw.get("probability", raw.get("value")))
        return NoulAnswer(noul=min(1.0, max(0.0, _float(value))))

    if qtype == "choice":
        probabilities = {str(k): _float(v) for k, v in (raw.get("probabilities") or {}).items()}
        choice = raw.get("choice", raw.get("winner"))
        if choice is None and probabilities:
            choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
        if choice is None:
            raise ValueError("choice 回答に選択結果がありません")
        confidence = _float(raw.get("confidence"), probabilities.get(str(choice), 0.0))
        return ChoiceAnswer(choice=str(choice), probabilities=probabilities, confidence=confidence)

    if qtype == "score":
        legend_raw = raw.get("legend")
        if legend_raw is None:
            levels = raw.get("levels") or (question_wire or {}).get("criteria") or []
            legend_raw = {str(i): level for i, level in enumerate(levels)}
        legend = {int(k): v for k, v in dict(legend_raw).items()}
        probabilities = {int(k): _float(v) for k, v in (raw.get("probabilities") or {}).items()}
        score = raw.get("score")
        if score is None and probabilities:
            score = sum(k * v for k, v in probabilities.items())
        confidence = _float(raw.get("confidence"), max(probabilities.values()) if probabilities else 0.0)
        return ScoreAnswer(score=_float(score), probabilities=probabilities, legend=legend, confidence=confidence)

    raise ValueError(f"不明な回答タイプ: {qtype}")


def parse_decision(body: Mapping[str, Any], questions_wire: Mapping[str, Mapping[str, Any]], backend: str, latency_ms: float) -> Decision:
    """レスポンス本文 → Decision。`answers` キーがある形と、質問名がトップレベルに並ぶ形 (OpenRouter) の両方に対応。"""
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, Mapping):
        raw_answers = {name: body[name] for name in questions_wire if isinstance(body.get(name), Mapping)}
    answers: dict[str, Answer] = {}
    for name, question in questions_wire.items():
        if name not in raw_answers:
            raise ValueError(f"レスポンスに質問 {name!r} の回答がありません")
        answers[name] = parse_answer(raw_answers[name], question)
    usage_raw = body.get("usage") or {}
    usage = Usage(
        input_tokens=usage_raw.get("input_tokens") if isinstance(usage_raw, Mapping) else None,
        output_tokens=usage_raw.get("output_tokens") if isinstance(usage_raw, Mapping) else None,
        cost=str(body["cost"]) if "cost" in body else None,
    )
    return Decision(answers=answers, model=str(body.get("model", "")), usage=usage, latency_ms=latency_ms, backend=backend, raw=dict(body))
