"""Jev に投げる質問 (System One primitives)。

TypeSafe の wire 形式にそのまま変換できる軽量なデータクラス。

- Choice : 名前付き選択肢から 1 つ選ぶ。`criteria` は {label: 説明 | None}
- Score  : 順序付きルーブリックで採点。`criteria` は [level0 の説明, level1 の説明, ...]
- Noul   : はい/いいえ の確率。`criteria` は {"true": 説明, "false": 説明} (任意)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

JSONContent = Union[str, dict, list]


def _clean(value: Any) -> Any:
    """None を落として wire に載せる。"""
    return value


@dataclass(frozen=True)
class Choice:
    criteria: Mapping[str, JSONContent | None]
    instructions: JSONContent | None = None
    type: str = field(default="choice", init=False)

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValueError("Choice には 1 つ以上の選択肢が必要です")
        object.__setattr__(self, "criteria", dict(self.criteria))

    @classmethod
    def of(cls, *labels: str, instructions: JSONContent | None = None) -> "Choice":
        """説明なしの選択肢を並べる簡易コンストラクタ。"""
        return cls({label: None for label in labels}, instructions)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": "choice", "criteria": dict(self.criteria)}
        if self.instructions is not None:
            wire["instructions"] = self.instructions
        return wire

    @property
    def labels(self) -> list[str]:
        return list(self.criteria)


@dataclass(frozen=True)
class Score:
    criteria: Sequence[JSONContent]
    instructions: JSONContent | None = None
    type: str = field(default="score", init=False)

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValueError("Score には 1 つ以上のレベルが必要です")
        object.__setattr__(self, "criteria", list(self.criteria))

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": "score", "criteria": list(self.criteria)}
        if self.instructions is not None:
            wire["instructions"] = self.instructions
        return wire

    @property
    def levels(self) -> int:
        return len(self.criteria)


@dataclass(frozen=True)
class Noul:
    instructions: JSONContent | None = None
    criteria: Mapping[str, JSONContent | None] | None = None
    type: str = field(default="noul", init=False)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": "noul"}
        if self.instructions is not None:
            wire["instructions"] = self.instructions
        if self.criteria is not None:
            wire["criteria"] = dict(self.criteria)
        return wire


Question = Union[Choice, Score, Noul, Mapping[str, Any]]


def question_to_wire(question: Question) -> dict[str, Any]:
    if isinstance(question, (Choice, Score, Noul)):
        return question.to_wire()
    if isinstance(question, Mapping):
        wire = dict(question)
        if wire.get("type") not in {"choice", "score", "noul"}:
            raise ValueError(f"不明な質問タイプ: {wire.get('type')!r}")
        if wire["type"] in {"choice", "score"} and "criteria" not in wire:
            raise ValueError(f"{wire['type']} には criteria が必要です")
        return wire
    raise TypeError(f"質問として扱えません: {type(question).__name__}")


def questions_to_wire(questions: Mapping[str, Question]) -> dict[str, dict[str, Any]]:
    if not questions:
        raise ValueError("少なくとも 1 つの質問が必要です")
    return {name: question_to_wire(question) for name, question in questions.items()}
