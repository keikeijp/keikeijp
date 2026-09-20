"""Jev の 3 つの質問型を辞書として組み立てるヘルパー。

公式 SDK (`typesafe-sdk`) の `Noul` / `Choice` / `Score` と同じワイヤ形式を返すので、
そのまま `POST /v1/systemone` の `questions` に入れられる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

JSONContent = Any  # str | dict | list

MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10


def noul(instructions: JSONContent, *, true: JSONContent | None = None, false: JSONContent | None = None) -> dict:
    """はい / いいえ で答える質問。回答は `noul` (真である確率 0〜1)。"""
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        criteria: dict[str, Any] = {}
        if true is not None:
            criteria["true"] = true
        if false is not None:
            criteria["false"] = false
        q["criteria"] = criteria
    return q


def choice(instructions: JSONContent, criteria: Mapping[str, JSONContent | None]) -> dict:
    """候補から 1 つ選ぶ質問。回答は `choice` / `confidence` / `probabilities`。"""
    if not criteria:
        raise ValueError("choice には 1 つ以上の候補が必要です")
    if len(criteria) > MAX_CHOICE_OPTIONS:
        raise ValueError(f"choice の候補は最大 {MAX_CHOICE_OPTIONS} 件です (指定: {len(criteria)})")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: JSONContent, levels: Sequence[JSONContent]) -> dict:
    """順序付きの基準で段階を当てる質問。回答は `score` (0 〜 len-1 の期待値) / `legend` / `probabilities`。"""
    if not levels:
        raise ValueError("score には 1 つ以上の段階が必要です")
    if len(levels) > MAX_SCORE_LEVELS:
        raise ValueError(f"score の段階は最大 {MAX_SCORE_LEVELS} 件です (指定: {len(levels)})")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}
