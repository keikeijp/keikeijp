"""11 投稿診断: 書く → 採点 → 直す を回す。

投稿文に対して yes/no の質問群と冒頭の強さの採点を行い、0〜100 の合成スコアにする。
スコアは投稿の伸びを保証しない。書き直しのきっかけ用。
"""

from __future__ import annotations

from typing import Any

from ..client import Result
from ..questions import choice, noul, score
from .base import Outcome, UseCase

# (質問, 重み)。負の重みは減点。
SIGNALS: dict[str, tuple[str, float]] = {
    "specific_claim": ("Does the post make one specific, checkable claim (a number, a name, a result)?", 2.0),
    "hook_first_line": ("Would the first line alone make a stranger stop scrolling?", 2.0),
    "shows_evidence": ("Does the post show evidence (screenshot described, data, demo, before/after)?", 1.5),
    "first_person_experience": ("Is it written from direct first-person experience rather than general advice?", 1.0),
    "clear_takeaway": ("Can a reader state the takeaway in one sentence?", 1.0),
    "novel_angle": ("Does it say something the reader has probably not heard before?", 1.5),
    "reply_bait": ("Does it fish for replies with a lazy question or a controversial statement without substance?", -2.0),
    "engagement_bait": ("Does it ask for likes, reposts, follows, or say 'comment X to get Y'?", -2.0),
    "generic_advice": ("Is it generic advice that could apply to any topic?", -1.5),
    "wall_of_text": ("Is it hard to scan: long paragraphs, no line breaks, no structure?", -1.0),
    "too_many_hashtags": ("Does it use three or more hashtags?", -0.5),
}

HOOK = [
    "Flat: the first line could open any post.",
    "Mild: it names the topic but not why it matters.",
    "Strong: it contains a surprising number, claim, or contrast.",
    "Unmissable: a reader would feel they have to read the next line.",
]

POST_TYPE = {
    "build_in_public": "Shows something the author made and what happened.",
    "insight": "Explains a non-obvious idea with reasoning.",
    "news": "Reports an event, release, or result.",
    "opinion": "States a position on a debate.",
    "question": "Asks the audience something.",
    "promo": "Sells or announces a product or service.",
}


class ViralUseCase(UseCase):
    name = "viral"
    title = "投稿診断"
    summary = "投稿文を質問群で採点し、直すべき点と 0〜100 のスコアを返す"
    article_ref = "11 viral post classifier"
    label_field = "post_type"

    def from_text(self, text: str) -> Any:
        return {"post": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"post": item["post"]}
        if item.get("audience"):
            state["audience"] = item["audience"]
        questions: dict[str, dict] = {k: noul(q) for k, (q, _) in SIGNALS.items()}
        questions["hook"] = score("How strong is the first line as a hook?", HOOK)
        questions["post_type"] = choice("What kind of post is this?", POST_TYPE)
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        contributions = {}
        total = 0.0
        max_total = 0.0
        for key, (_, weight) in SIGNALS.items():
            p = result.noul(key)
            contributions[key] = round(p, 3)
            if weight > 0:
                total += weight * p
                max_total += weight
            else:
                total += weight * p  # 減点
        hook = result.score("hook")
        total += 2.0 * hook.normalized
        max_total += 2.0
        score_100 = max(0, min(100, round(100 * total / max_total)))
        fixes = [k for k, (_, w) in SIGNALS.items() if (w > 0 and contributions[k] < 0.4) or (w < 0 and contributions[k] > 0.6)]
        return {
            "score": score_100,
            "hook": hook.level,
            "hook_label": hook.label,
            "post_type": result.choice("post_type").choice,
            "signals": contributions,
            "fix_first": fixes[:3],
            "needs_human": False,
        }

    def example_items(self) -> list[Any]:
        return [
            {"post": "I spent 8 hours building a viral post classifier with Jev.\n\nIt picks the better post 2 in 3 times, costs $0.0004 per post, and never rewards reply bait.\n\nFree, no signup."},
            {"post": "Success is a journey, not a destination. Agree? Like and repost if you agree! #motivation #success #hustle"},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"score={d['score']:3d} hook={d['hook']} type={d['post_type']:16} fix_first={', '.join(d['fix_first']) or '-'}"
