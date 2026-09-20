"""ユースケースの共通骨格。

各ユースケースは「入力 → state と questions を組む → Jev に聞く → 閾値と方針で決める」
の 1 往復 (または数往復) を実装する。判断だけを返し、送信・削除・返金などの実行は行わない。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..client import JevClient, Result


@dataclass
class Outcome:
    input: Any
    decision: dict[str, Any]
    results: list[Result] = field(default_factory=list)

    @property
    def result(self) -> Result:
        return self.results[-1]

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.results)

    @property
    def latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.results)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "decision": self.decision,
            "answers": [r.answers_dict() for r in self.results],
            "usage": {"calls": len(self.results), "input_tokens": self.input_tokens, "latency_ms": round(self.latency_ms, 1), "cost_usd": round(self.cost_usd, 6)},
        }


class UseCase(ABC):
    name: str = ""
    title: str = ""
    summary: str = ""
    article_ref: str = ""
    #: 評価ハーネスが正解ラベルと比べる decision のキー
    label_field: str = ""
    #: 見逃し率を出すときに「通してはいけない」とみなすラベル
    positive_labels: frozenset[str] = frozenset()
    #: 人が確認すべきかを示す decision のキー (bool)
    human_field: str = "needs_human"

    @abstractmethod
    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        """入力 1 件から (state, questions) を作る。"""

    @abstractmethod
    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        """回答から判断を作る。副作用なし。"""

    @abstractmethod
    def example_items(self) -> list[Any]:
        """デモ用の入力。"""

    def from_text(self, text: str) -> Any:
        return {"text": text}

    def expand(self, item: Any) -> list[Any]:
        """複合入力 (diff 全体、原稿全体、記憶の一覧など) を build() に渡せる単位に分ける。"""
        return [item]

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        state, questions = self.build(item)
        result = client.ask(state, questions)
        return Outcome(input=item, decision=self.decide(item, result), results=[result])

    def run(self, client: JevClient, items: Iterable[Any], *, concurrency: int = 1) -> list[Outcome]:
        items = list(items)
        if concurrency <= 1:
            return [self.run_one(client, it) for it in items]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(lambda it: self.run_one(client, it), items))

    def dry_run(self, items: Iterable[Any], model: str = "jev-latest") -> list[dict]:
        """API を呼ばずに送信するリクエスト本文を返す。"""
        return [JevClient.build_body(*self.build(sub), model) for it in items for sub in self.expand(it)]

    def format(self, outcome: Outcome) -> str:
        """人が読む 1 行〜数行の要約。"""
        return json.dumps(outcome.decision, ensure_ascii=False)


def band(confidence: float, *, act: float = 0.8, review: float = 0.55) -> str:
    """confidence を 3 段階に。act: 自動処理してよい / review: 人が確認 / abstain: 判断しない。"""
    if confidence >= act:
        return "act"
    if confidence >= review:
        return "review"
    return "abstain"


def state_text(item: Mapping[str, Any] | str, *keys: str) -> Any:
    """item が文字列ならそのまま、辞書なら指定キーだけを抜いた state を返す。"""
    if isinstance(item, str):
        return item
    if not keys:
        return dict(item)
    return {k: item[k] for k in keys if k in item}
