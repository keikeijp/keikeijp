"""DTM 向け: 参照区間の説明に合うサンプルを候補から選ぶ。

dtm-agent の `LocalLibrary.search()` は音響類似で候補を出す。その上位候補を Jev に渡し、
「今欲しい役割 (パッド / キック など) と雰囲気に最も合うのはどれか」を選ばせる再ランクの例。
音声そのものは渡せないので、ファイル名・タグ・解析値 (BPM / キー / 明るさ) を文字で渡す。
"""

from __future__ import annotations

from typing import Any

from ..client import Result
from ..questions import choice, score
from .base import Outcome, UseCase, band

FIT = ["Wrong role or clashing key", "Usable with editing", "Good fit", "Exactly what was asked for"]


class SamplePickUseCase(UseCase):
    name = "sample_pick"
    title = "サンプルの選択 (DTM)"
    summary = "参照区間の説明と役割に対して、検索候補から最も合うサンプルを選ぶ"
    article_ref = "候補の選択 (dtm-agent との連携例)"
    label_field = "pick"

    def __init__(self, *, max_candidates: int = 50):
        self.max_candidates = max_candidates

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        cands = item["candidates"][: self.max_candidates]
        criteria = {c["id"]: self._describe(c) for c in cands}
        state = {"wanted": item["wanted"], "reference": item.get("reference", {}), "candidates": criteria}
        questions = {
            "pick": choice("Which candidate best matches what the producer wants for this part?", criteria),
            "fit": score("How well does the best candidate fit?", FIT),
        }
        return state, questions

    @staticmethod
    def _describe(c: dict[str, Any]) -> str:
        parts = [c.get("name") or c.get("path", "")]
        for k in ("tags", "bpm", "key", "brightness", "duration_s", "similarity"):
            if c.get(k) is not None:
                parts.append(f"{k}={c[k]}")
        return ", ".join(str(p) for p in parts if p)

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        pick = result.choice("pick")
        fit = result.score("fit")
        return {
            "pick": pick.choice,
            "confidence": round(pick.confidence, 3),
            "band": band(pick.confidence, act=0.7, review=0.45),
            "fit": fit.level,
            "fit_label": fit.label,
            "shortlist": [k for k, _ in pick.ranked()[:3]],
            "needs_human": pick.confidence < 0.7 or fit.level <= 1,
        }

    def example_items(self) -> list[Any]:
        return [
            {
                "wanted": "a warm, dark analog pad for the 1:05-1:21 section",
                "reference": {"bpm": 92, "key": "A minor", "brightness": "dark", "energy": "low"},
                "candidates": [
                    {"id": "c1", "name": "JunoPad_Am_dark.wav", "tags": ["pad", "analog", "warm"], "key": "A minor", "similarity": 0.81},
                    {"id": "c2", "name": "Kick_808_hard.wav", "tags": ["kick", "808"], "similarity": 0.79},
                    {"id": "c3", "name": "BrightSupersaw_C.wav", "tags": ["lead", "bright"], "key": "C major", "similarity": 0.74},
                    {"id": "c4", "name": "StringsSwell_Dm.wav", "tags": ["strings", "pad"], "key": "D minor", "similarity": 0.7},
                ],
            }
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"pick={d['pick']} ({d['confidence']:.2f}, {d['band']}) fit={d['fit']} shortlist={d['shortlist']}"
