"""14 コードの過剰設計をチェックする。

ファイル単位で「過剰設計か」「どのパターンか」「複雑さの段階」を判定する。
何を削るかは仕様やテストと照らして人が決める。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..client import Result
from ..questions import choice, noul, score
from .base import Outcome, UseCase

PATTERNS = {
    "fine": "The abstraction level matches what the code does.",
    "unnecessary_abstraction": "Interfaces, base classes, factories, or strategy objects with a single implementation.",
    "premature_generalization": "Options, plugins, or config for cases nobody asked for; speculative flexibility.",
    "indirection_layers": "Wrappers that only forward calls; managers of managers; helpers for one-line operations.",
    "dead_or_duplicate": "Unused code paths, copy-pasted logic, or feature flags that are never toggled.",
    "config_sprawl": "Many knobs, environment variables, or nested settings for a small feature.",
}

COMPLEXITY = [
    "Minimal: as simple as the task allows.",
    "Reasonable: some structure, all of it earning its keep.",
    "Heavy: noticeably more structure than needed; a reviewer would ask to simplify.",
    "Overbuilt: most of the file is scaffolding around a small amount of real logic.",
]


class ComplexityUseCase(UseCase):
    name = "complexity"
    title = "過剰設計チェック"
    summary = "AI が書いたコードを、ファイル単位で過剰設計のパターンと複雑さで分類する"
    article_ref = "14 codebase classifier"
    label_field = "pattern"
    positive_labels = frozenset({"unnecessary_abstraction", "premature_generalization", "indirection_layers", "dead_or_duplicate", "config_sprawl"})

    def __init__(self, *, max_chars: int = 12000):
        self.max_chars = max_chars

    def from_text(self, text: str) -> Any:
        p = Path(text)
        if p.exists():
            return {"path": str(p), "source": p.read_text(encoding="utf-8", errors="replace")}
        return {"path": "(stdin)", "source": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"path": item.get("path", ""), "source": item["source"][: self.max_chars]}
        if item.get("purpose"):
            state["purpose"] = item["purpose"]
        questions = {
            "overengineered": noul(
                "Is this file more complex than its purpose requires?",
                true="A senior engineer would remove layers, classes, or options without losing behavior.",
                false="The structure is proportionate to what the code has to do.",
            ),
            "pattern": choice("Which over-engineering pattern best describes this file?", PATTERNS),
            "complexity": score("How much structure does this file carry relative to its real logic?", COMPLEXITY),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        over = result.noul("overengineered")
        pattern = result.choice("pattern")
        cx = result.score("complexity")
        return {
            "path": item.get("path", ""),
            "overengineered": over >= 0.5,
            "overengineered_probability": round(over, 3),
            "pattern": pattern.choice,
            "pattern_confidence": round(pattern.confidence, 3),
            "complexity": cx.level,
            "complexity_label": cx.label,
            "needs_human": over >= 0.5 and cx.level >= 2,
        }

    def example_items(self) -> list[Any]:
        return [
            {
                "path": "greeting.py",
                "purpose": "Print a greeting",
                "source": (
                    "from abc import ABC, abstractmethod\n\nclass GreetingStrategy(ABC):\n    @abstractmethod\n    def greet(self, name: str) -> str: ...\n\n"
                    "class DefaultGreetingStrategy(GreetingStrategy):\n    def greet(self, name):\n        return f'Hello, {name}'\n\n"
                    "class GreetingStrategyFactory:\n    _registry = {'default': DefaultGreetingStrategy}\n    @classmethod\n    def create(cls, kind='default'):\n        return cls._registry[kind]()\n\n"
                    "class GreetingService:\n    def __init__(self, factory=GreetingStrategyFactory):\n        self._factory = factory\n    def run(self, name, kind='default'):\n        return self._factory.create(kind).greet(name)\n\n"
                    "if __name__ == '__main__':\n    print(GreetingService().run('world'))\n"
                ),
            },
            {"path": "add.py", "purpose": "Add two numbers", "source": "def add(a, b):\n    return a + b\n"},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"{d['path']:30.30} over={d['overengineered']!s:5} p={d['overengineered_probability']:.2f} {d['pattern']:25} complexity={d['complexity']}"
