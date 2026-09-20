"""評価ハーネス: 人が正解ラベルを付けたデータで、速度・費用・正確さをセットで見る。

記事の推奨どおり、まず判定結果を記録するだけにして、業務の更新や送信にはつなげない。

データ形式 (JSONL): 1 行 1 件、`input` にユースケースの入力、`label` に正解。
    {"input": {"subject": "...", "body": "..."}, "label": "billing"}

見る数字:
  - accuracy        : label_field の予測が正解と一致した割合
  - false_pass_rate : 「通してはいけない」ラベル (positive_labels) を通してしまった割合 (見逃し)
  - human_rate      : 人へ回した割合 (needs_human)
  - latency p50/p95 : 1 件あたりの API 往復時間 (ms)。OCR やブラウザの待ち時間は含まない
  - cost            : 入力トークン × 通常単価。再試行・併用モデルの費用は含まない
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import JevClient
from .usecases.base import UseCase


@dataclass
class EvalReport:
    usecase: str
    n: int
    accuracy: float | None
    false_pass_rate: float | None
    human_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    total_cost_usd: float
    cost_per_item_usd: float
    input_tokens: int
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "usecase": self.usecase,
            "n": self.n,
            "accuracy": self.accuracy,
            "false_pass_rate": self.false_pass_rate,
            "human_rate": self.human_rate,
            "latency_p50_ms": round(self.latency_p50_ms, 1),
            "latency_p95_ms": round(self.latency_p95_ms, 1),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "cost_per_item_usd": round(self.cost_per_item_usd, 8),
            "input_tokens": self.input_tokens,
            "confusion": self.confusion,
            "errors": self.errors,
        }

    def summary(self) -> str:
        acc = f"{self.accuracy:.1%}" if self.accuracy is not None else "n/a"
        fp = f"{self.false_pass_rate:.1%}" if self.false_pass_rate is not None else "n/a"
        return (
            f"{self.usecase}: n={self.n} accuracy={acc} false_pass={fp} human={self.human_rate:.1%} "
            f"latency p50={self.latency_p50_ms:.0f}ms p95={self.latency_p95_ms:.0f}ms "
            f"cost=${self.total_cost_usd:.4f} (${self.cost_per_item_usd:.6f}/item, {self.input_tokens} input tokens)"
        )


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def evaluate(usecase: UseCase, client: JevClient, dataset: Iterable[dict[str, Any]], *, concurrency: int = 1) -> EvalReport:
    rows = list(dataset)
    outcomes = usecase.run(client, [r["input"] for r in rows], concurrency=concurrency)
    field_name = usecase.label_field
    correct = 0
    labeled = 0
    positives = 0
    missed = 0
    human = 0
    confusion: dict[str, Counter] = {}
    errors: list[dict[str, Any]] = []
    latencies: list[float] = []
    cost = 0.0
    tokens = 0
    for row, outcome in zip(rows, outcomes):
        latencies.append(outcome.latency_ms)
        cost += outcome.cost_usd
        tokens += outcome.input_tokens
        d = outcome.decision
        if d.get(usecase.human_field):
            human += 1
        label = row.get("label")
        if label is None or not field_name:
            continue
        pred = d.get(field_name)
        labeled += 1
        confusion.setdefault(str(label), Counter())[str(pred)] += 1
        if _match(pred, label):
            correct += 1
        else:
            errors.append({"label": label, "predicted": pred, "needs_human": bool(d.get(usecase.human_field)), "input": row["input"]})
        if str(label) in usecase.positive_labels:
            positives += 1
            if str(pred) not in usecase.positive_labels and not d.get(usecase.human_field):
                missed += 1
    return EvalReport(
        usecase=usecase.name,
        n=len(rows),
        accuracy=(correct / labeled) if labeled else None,
        false_pass_rate=(missed / positives) if positives else None,
        human_rate=(human / len(rows)) if rows else 0.0,
        latency_p50_ms=statistics.median(latencies) if latencies else 0.0,
        latency_p95_ms=_percentile(latencies, 0.95),
        total_cost_usd=cost,
        cost_per_item_usd=(cost / len(rows)) if rows else 0.0,
        input_tokens=tokens,
        confusion={k: dict(v) for k, v in confusion.items()},
        errors=errors,
    )


def _match(pred: Any, label: Any) -> bool:
    if isinstance(label, bool) or isinstance(pred, bool):
        return bool(pred) == bool(label)
    if isinstance(label, (int, float)) and isinstance(pred, (int, float)):
        return round(pred) == round(label)
    return str(pred) == str(label)


def estimate_batch_cost(items: int, tokens_per_item: int, usd_per_mtoken: float = 0.042) -> dict[str, float]:
    """記事の目安計算: 1 件 2,000 トークン × 1 万件 = 2,000 万トークン → $0.84。"""
    total_tokens = items * tokens_per_item
    return {"items": items, "tokens_per_item": tokens_per_item, "total_tokens": total_tokens, "usd": round(total_tokens / 1_000_000 * usd_per_mtoken, 6)}
