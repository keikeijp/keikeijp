"""18 AI の記憶整理: 不要なメモやツール結果を絞り込む。

現在のタスクに対して、記憶 (メモ / ツール呼び出しの結果) ごとに関連度を採点し、
閾値未満を落とす。要約は生成しない。落としすぎを防ぐため最低保持件数を持つ。
"""

from __future__ import annotations

from typing import Any

from ..client import JevClient, Result
from ..questions import noul, score
from .base import Outcome, UseCase

RELEVANCE = [
    "Irrelevant: has nothing to do with the current task.",
    "Background: loosely related; could be reconstructed if needed.",
    "Useful: the agent would probably consult it while doing the task.",
    "Essential: the task cannot be completed correctly without it.",
]


class CompactUseCase(UseCase):
    name = "compact"
    title = "記憶の整理"
    summary = "タスクに対する関連度で記憶やツール結果を採点し、不要なものを落とす"
    article_ref = "18 Jev compaction"
    label_field = "keep"

    def __init__(self, *, keep_level: float = 1.5, keep_min: int = 3, never_drop_essential: bool = True):
        self.keep_level = keep_level
        self.keep_min = keep_min
        self.never_drop_essential = never_drop_essential

    def from_text(self, text: str) -> Any:
        lines = [l for l in text.splitlines() if l.strip()]
        return {"task": lines[0] if lines else "", "memories": [{"id": f"m{i}", "text": l} for i, l in enumerate(lines[1:])]}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"current_task": item["task"], "memory": item["memory"]}
        questions = {
            "relevance": score("How relevant is this memory to the current task?", RELEVANCE),
            "stale": noul("Is this memory outdated or superseded by something more recent?"),
            "safety_critical": noul("Does this memory contain a constraint, permission, or warning the agent must not forget (e.g. 'never push to main', a user preference, a credential location)?"),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        rel = result.score("relevance")
        stale = result.noul("stale")
        critical = result.noul("safety_critical")
        keep = rel.score >= self.keep_level or (self.never_drop_essential and critical >= 0.5)
        if stale >= 0.7 and critical < 0.5 and rel.score < 2.5:
            keep = False
        return {
            "id": item["memory"].get("id") if isinstance(item["memory"], dict) else None,
            "keep": keep,
            "relevance": round(rel.score, 3),
            "relevance_label": rel.label,
            "stale": round(stale, 3),
            "safety_critical": round(critical, 3),
            "needs_human": False,
        }

    def expand(self, item: Any) -> list[Any]:
        if "memory" in item:
            return [item]
        return [{"task": item["task"], "memory": m} for m in item["memories"]]

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        if "memory" in item:
            return super().run_one(client, item)
        subs = self.expand(item)
        outcomes = [super(CompactUseCase, self).run_one(client, s) for s in subs]
        decisions = [o.decision for o in outcomes]
        kept = [d for d in decisions if d["keep"]]
        if len(kept) < self.keep_min:
            for d in sorted(decisions, key=lambda d: -d["relevance"]):
                if len(kept) >= self.keep_min:
                    break
                if not d["keep"]:
                    d["keep"] = True
                    d["kept_by_minimum"] = True
                    kept.append(d)
        return Outcome(
            input=item,
            decision={
                "kept": [d["id"] for d in decisions if d["keep"]],
                "dropped": [d["id"] for d in decisions if not d["keep"]],
                "memories": decisions,
                "needs_human": False,
            },
            results=[r for o in outcomes for r in o.results],
        )

    def example_items(self) -> list[Any]:
        return [
            {
                "task": "Fix the failing test in tests/test_daw.py about REAPER project export",
                "memories": [
                    {"id": "m0", "text": "User prefers pytest -q and hates verbose output."},
                    {"id": "m1", "text": "Never push directly to main; always open a PR."},
                    {"id": "m2", "text": "Ran `ls ~/Music`: 3 wav files, 1 mp3."},
                    {"id": "m3", "text": "tests/test_daw.py::test_reaper_export fails: expected 2 tracks, got 3 (extra empty track from add_audio_clip)."},
                    {"id": "m4", "text": "Weather in Tokyo today is sunny, 24C."},
                    {"id": "m5", "text": "dtm_agent/daw/reaper.py builds tracks in ReaperProjectBridge.write(); add_audio_clip appends a track even when the clip list is empty."},
                ],
            }
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        if "memories" not in d:
            return str(d)
        lines = [f"kept {len(d['kept'])}, dropped {len(d['dropped'])}"]
        for m in d["memories"]:
            lines.append(f"  {'KEEP' if m['keep'] else 'drop'} {m['id']} rel={m['relevance']:.2f} critical={m['safety_critical']:.2f}")
        return "\n".join(lines)
