"""12/17 フィードの仕分け: 「見たくない投稿」を言葉で指定し、好みの例を渡す。

投稿ごとに keep / skim / hide を決める。ルールは自然言語で、例は保存済み投稿など。
モデルを追加学習させるのではなく、state に例と規則を同梱して判断させる。
"""

from __future__ import annotations

from typing import Any

from ..client import Result
from ..questions import choice, noul
from .base import Outcome, UseCase

VERDICTS = {
    "keep": "Worth reading now: relevant to the reader's interests and substantive.",
    "skim": "Maybe: on-topic but low information, or off-topic but interesting.",
    "hide": "Not worth showing: matches a hide rule, is an ad, ragebait, or low effort.",
}

FLAGS = {
    "ragebait": "The post exists to provoke anger or outrage rather than inform.",
    "ad": "The post promotes a product, course, newsletter, or service.",
    "low_effort": "The post is a platitude, a one-line hot take, or recycled content with no substance.",
    "hostile": "The post attacks a person or group.",
    "useful": "The reader would learn something concrete or find a link worth opening.",
}


class FeedUseCase(UseCase):
    name = "feed"
    title = "フィードの仕分け"
    summary = "自然言語の非表示ルールと好みの例を渡し、投稿を keep / skim / hide に分ける"
    article_ref = "12 X のフィルター / 17 ブックマークで仕分け"
    label_field = "verdict"
    positive_labels = frozenset({"hide"})

    def __init__(self, *, rules: list[str] | None = None, liked_examples: list[str] | None = None, max_examples: int = 16):
        self.rules = rules or []
        self.liked_examples = (liked_examples or [])[:max_examples]

    def from_text(self, text: str) -> Any:
        return {"post": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        rules = item.get("rules", self.rules)
        examples = item.get("liked_examples", self.liked_examples)
        state: dict[str, Any] = {"post": item["post"]}
        if item.get("author"):
            state["author"] = item["author"]
        if rules:
            state["hide_rules"] = rules
        if examples:
            state["posts_the_reader_saved"] = examples
        questions: dict[str, dict] = {
            "verdict": choice("Given the reader's hide rules and the posts they saved, what should happen to this post?", VERDICTS),
            **{k: noul(f"Is this true of the post? {desc}") for k, desc in FLAGS.items()},
        }
        if rules:
            questions["matches_rule"] = noul("Does the post match at least one of the reader's hide rules?")
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        verdict = result.choice("verdict")
        flags = {k: round(result.noul(k), 3) for k in FLAGS}
        matches_rule = result.noul("matches_rule") if "matches_rule" in result.answers else 0.0
        final = verdict.choice
        why = []
        if matches_rule >= 0.6:
            final = "hide"
            why.append("matches a hide rule")
        if flags["ad"] >= 0.7 or flags["ragebait"] >= 0.7 or flags["hostile"] >= 0.7:
            final = "hide"
            why.append("ad/ragebait/hostile")
        if final == "hide" and flags["useful"] >= 0.8 and matches_rule < 0.6:
            final = "skim"
            why.append("but looks useful")
        return {
            "verdict": final,
            "model_verdict": verdict.choice,
            "confidence": round(verdict.confidence, 3),
            "matches_rule": round(matches_rule, 3),
            "flags": flags,
            "why": why,
            "needs_human": verdict.confidence < 0.5,
        }

    def example_items(self) -> list[Any]:
        rules = ["crypto price predictions", "engagement bait asking to like or repost", "celebrity gossip"]
        liked = ["Benchmarked three vector DBs on 10M rows; pgvector was 2x slower but 10x simpler to operate.", "How we cut CI time from 40 to 12 minutes: test sharding and a warm cache."]
        return [
            {"post": "Bitcoin to 500k by December. Screenshot this.", "rules": rules, "liked_examples": liked},
            {"post": "We profiled our Rails app and found 60% of request time was one N+1 query. Fix and numbers in the thread.", "rules": rules, "liked_examples": liked},
            {"post": "Repost if you love Mondays! Like for a follow back!", "rules": rules, "liked_examples": liked},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"[{d['verdict']:4}] {outcome.input['post']!s:70.70} conf={d['confidence']:.2f} {' '.join(d['why'])}"
