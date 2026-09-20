"""15 文章のクセを見つける (Sniff Test)。

原稿を段落ごとに読み、10 個の yes/no 質問で表現のクセを検出する。
書き直しはしない。AI が書いたかどうかを断定する検出器でもない。
"""

from __future__ import annotations

import re
from typing import Any

from ..client import JevClient, Result
from ..questions import noul
from .base import Outcome, UseCase

TELLS = {
    "hedged_repeatedly": "The paragraph hedges the same claim three or more times (may, might, could, perhaps, it seems).",
    "closer_restates": "The last sentence only restates what the paragraph already said.",
    "not_x_but_y": "It uses the 'not X, but Y' or 'it's not about X, it's about Y' construction.",
    "rule_of_three_padding": "It lists exactly three adjectives or items where one would do, for rhythm rather than content.",
    "rhetorical_question": "It opens with or leans on a rhetorical question instead of a claim.",
    "vague_claim": "It makes a claim with no specific example, number, or name to support it.",
    "empty_intensifier": "It uses intensifiers that add no information (truly, deeply, incredibly, game-changing, crucial).",
    "generic_opener": "It opens with a throat-clearing phrase (In today's world, It is important to note, As we all know).",
    "signposting": "It announces what it will say instead of saying it (In this section, Let's explore, Now we will look at).",
    "over_transition": "It stacks transition words that add no logic (Moreover, Furthermore, Additionally, In addition).",
}


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


class SniffUseCase(UseCase):
    name = "sniff"
    title = "文章のクセ検出"
    summary = "段落ごとに 10 個の yes/no 質問で表現のクセを見つけ、直す箇所を絞る"
    article_ref = "15 Sniff Test"
    label_field = "worst_tell"

    def __init__(self, *, flag_threshold: float = 0.6):
        self.flag_threshold = flag_threshold

    def from_text(self, text: str) -> Any:
        return {"text": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"paragraph": item["paragraph"]}
        if item.get("context"):
            state["document_context"] = item["context"]
        return state, {key: noul(f"Does this paragraph show this writing tell? {desc}") for key, desc in TELLS.items()}

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        probs = {k: round(result.noul(k), 3) for k in TELLS}
        flagged = [k for k, p in probs.items() if p >= self.flag_threshold]
        worst = max(probs.items(), key=lambda kv: kv[1])
        return {
            "index": item.get("index", 0),
            "flags": flagged,
            "worst_tell": worst[0] if worst[1] >= self.flag_threshold else "none",
            "tell_probabilities": probs,
            "needs_human": bool(flagged),
        }

    def expand(self, item: Any) -> list[Any]:
        if "paragraph" in item:
            return [item]
        return [{"index": i, "paragraph": p, "context": item.get("context")} for i, p in enumerate(split_paragraphs(item["text"]))]

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        if "paragraph" in item:
            return super().run_one(client, item)
        subs = self.expand(item)
        paragraphs = [s["paragraph"] for s in subs]
        outcomes = [super(SniffUseCase, self).run_one(client, s) for s in subs]
        flags = [o.decision for o in outcomes if o.decision["flags"]]
        return Outcome(
            input=item,
            decision={
                "paragraphs": len(paragraphs),
                "flagged_paragraphs": [{"index": d["index"], "flags": d["flags"], "excerpt": paragraphs[d["index"]][:80]} for d in flags],
                "worst_tell": max((d["worst_tell"] for d in flags), default="none"),
                "needs_human": bool(flags),
            },
            results=[r for o in outcomes for r in o.results],
        )

    def example_items(self) -> list[Any]:
        return [
            {
                "text": (
                    "In today's fast-paced world, it is important to note that writing may perhaps be one of the most crucial, "
                    "essential, and game-changing skills. It's not about talent, it's about practice. Writing matters a great deal.\n\n"
                    "The build took 41 seconds on the M2 laptop. Removing the unused lodash import cut it to 29 seconds.\n\n"
                    "Moreover, and furthermore, in this section we will explore why this is truly important. Is it not obvious?"
                )
            }
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        if "paragraphs" not in d:
            return f"paragraph {d['index']}: {', '.join(d['flags']) or 'clean'}"
        lines = [f"{d['paragraphs']} paragraphs, {len(d['flagged_paragraphs'])} flagged"]
        for f in d["flagged_paragraphs"]:
            lines.append(f"  #{f['index']} {', '.join(f['flags'])}  | {f['excerpt']}")
        return "\n".join(lines)
