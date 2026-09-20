"""16 ニュースを自分向けに並べ替える (Upweight)。

記事ごとに複数の軸で採点し、重み (スライダー) の加重和で並べ替える。
採点結果はキャッシュするので、重みを変えても再度 API を呼ばない。
"""

from __future__ import annotations

from typing import Any

from ..client import JevClient, Result
from ..questions import score
from .base import Outcome, UseCase

AXES: dict[str, tuple[str, list[str]]] = {
    "technical_depth": ("How technically deep is this item?", ["Surface-level", "Some detail", "Explains mechanisms", "Expert-level detail"]),
    "practical": ("How directly can a working engineer apply this?", ["Not applicable", "Background knowledge", "Useful soon", "Use it today"]),
    "novelty": ("How new is the information?", ["Widely known", "Somewhat known", "Fresh", "First time this is reported"]),
    "drama": ("How much drama, outrage, or personality conflict is in it?", ["None", "A little", "Considerable", "It is the whole story"]),
    "evidence": ("How well supported are the claims?", ["Unsupported", "Anecdotal", "Some data", "Rigorous or reproducible"]),
    "longevity": ("Will this still matter in a year?", ["Gone tomorrow", "Weeks", "Months", "Years"]),
}

DEFAULT_WEIGHTS = {"technical_depth": 1.0, "practical": 1.0, "novelty": 0.5, "drama": -1.0, "evidence": 1.0, "longevity": 0.5}


class RankUseCase(UseCase):
    name = "rank"
    title = "ニュースの並べ替え"
    summary = "記事を 6 つの軸で採点し、重みの加重和で自分向けに並べ替える (採点はキャッシュ)"
    article_ref = "16 Upweight"
    label_field = "rank"

    def __init__(self, *, weights: dict[str, float] | None = None):
        self.weights = dict(DEFAULT_WEIGHTS, **(weights or {}))
        self._cache: dict[str, dict[str, float]] = {}

    def from_text(self, text: str) -> Any:
        return {"items": [{"id": f"item_{i}", "title": line} for i, line in enumerate(l for l in text.splitlines() if l.strip())]}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {k: item[k] for k in ("title", "summary", "url", "source") if k in item}
        if item.get("interests"):
            state["reader_interests"] = item["interests"]
        return state, {axis: score(q, levels) for axis, (q, levels) in AXES.items()}

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        axes = {axis: round(result.score(axis).normalized, 3) for axis in AXES}
        return {"id": item.get("id"), "axes": axes, "needs_human": False}

    @staticmethod
    def weighted(axes: dict[str, float], weights: dict[str, float]) -> float:
        return round(sum(weights.get(a, 0.0) * v for a, v in axes.items()), 4)

    def rerank(self, scored: list[dict[str, Any]], weights: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """採点済みの一覧を重みだけ変えて並べ替える。API は呼ばない。"""
        w = dict(self.weights, **(weights or {}))
        ranked = sorted(scored, key=lambda d: -self.weighted(d["axes"], w))
        return [dict(d, weighted=self.weighted(d["axes"], w), rank=i + 1) for i, d in enumerate(ranked)]

    def expand(self, item: Any) -> list[Any]:
        if "items" not in item:
            return [item]
        entries = []
        for i, entry in enumerate(item["items"]):
            entry = dict(entry)
            entry.setdefault("id", f"item_{i}")
            if item.get("interests"):
                entry["interests"] = item["interests"]
            entries.append(entry)
        return entries

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        """item は {"items": [...], "weights"?, "interests"?}。"""
        if "items" not in item:
            return super().run_one(client, item)
        results: list[Result] = []
        scored: list[dict[str, Any]] = []
        for entry in self.expand(item):
            key = entry["id"] + "|" + entry.get("title", "")
            if key in self._cache:
                scored.append({"id": entry["id"], "title": entry.get("title"), "axes": self._cache[key]})
                continue
            state, questions = self.build(entry)
            result = client.ask(state, questions)
            results.append(result)
            d = self.decide(entry, result)
            self._cache[key] = d["axes"]
            scored.append({"id": entry["id"], "title": entry.get("title"), "axes": d["axes"]})
        ranked = self.rerank(scored, item.get("weights"))
        return Outcome(input=item, decision={"ranked": ranked, "weights": dict(self.weights, **(item.get("weights") or {})), "needs_human": False}, results=results)

    def example_items(self) -> list[Any]:
        return [
            {
                "items": [
                    {"id": "a", "title": "Show HN: I wrote a 400-line Rust allocator and benchmarked it against jemalloc (numbers inside)"},
                    {"id": "b", "title": "CEO of BigCo fires CTO in public spat on social media"},
                    {"id": "c", "title": "Postgres 18 released: async I/O, up to 3x faster sequential scans"},
                    {"id": "d", "title": "10 productivity hacks every developer should know"},
                ],
                "weights": {"drama": -1.5, "practical": 1.5},
            }
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        if "ranked" not in d:
            return str(d)
        return "\n".join(f"  {r['rank']}. {r['weighted']:+.2f}  {r['title']}" for r in d["ranked"])
