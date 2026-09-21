"""21. geo (usenotra/notra の再実装): GEO (generative engine optimization) トラッカー。
AI アシスタントの回答の中で自社ブランドと競合がどう言及されるかを追跡する。

構成:
- `AnswerProvider`: 質問 → 回答テキスト。`AnthropicProvider` (Messages API を urllib で遅延呼び出し、ANTHROPIC_API_KEY)、
  `OpenAICompatibleProvider` (OPENAI_API_KEY / OPENAI_BASE_URL)、`StaticProvider` (JSON からオフライン)
- `prompts.json`: {"brands": [...], "competitors": [...], "prompts": [...]}
- 回答ごとにブランド名を含む文を正規表現で抜き出し、Jev に `sentiment` Choice と `recommendation_strength` Score を聞く。
  `position_rank` は回答内での出現順から計算
- sqlite にラン結果を保存し、share-of-voice / 平均センチメント / 順位の推移を JSON と Markdown 表で出す
- `--ab` で 2 つのブランド説明 (プロンプト内の {brand_description}) を比較
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from jevlab.core import Choice, Jev, Score

SENTIMENTS: dict[str, str] = {"positive": "好意的に言及", "neutral": "中立的に言及", "negative": "否定的に言及"}
STRENGTH = ["not recommended: 勧めていない/避けるべきと言っている", "mentioned: 名前が出ただけ", "suggested: 選択肢として勧めている", "top pick: 一番の推薦"]
SENTIMENT_VALUE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
MAX_SENTENCE = 300


# ---------------------------------------------------------------------------
# 回答プロバイダ
# ---------------------------------------------------------------------------


class AnswerProvider(Protocol):
    name: str

    def answer(self, prompt: str) -> str: ...


class StaticProvider:
    """JSON ({prompt: answer} または [{prompt, answer}]) から回答を返すオフライン用。"""

    name = "static"

    def __init__(self, answers: dict[str, str] | list[dict[str, str]]):
        self.answers = {item["prompt"]: item["answer"] for item in answers} if isinstance(answers, list) else dict(answers)

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticProvider":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def answer(self, prompt: str) -> str:
        return self.answers.get(prompt, "")


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float = 60.0) -> dict[str, Any]:
    import urllib.request

    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class AnthropicProvider:
    """Anthropic Messages API (`POST /v1/messages`) を標準ライブラリだけで呼ぶ。"""

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None, max_tokens: int = 1024):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
        self.model = model or os.environ.get("GEO_ANTHROPIC_MODEL", "").strip() or "claude-opus-5"
        self.max_tokens = max_tokens
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com"

    def answer(self, prompt: str) -> str:
        body = {"model": self.model, "max_tokens": self.max_tokens, "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        response = _post_json(f"{self.base_url}/v1/messages", body, headers)
        if response.get("stop_reason") == "refusal":
            return ""
        return "".join(block.get("text", "") for block in response.get("content", []) if block.get("type") == "text")


class OpenAICompatibleProvider:
    """OpenAI 互換 (`/chat/completions`) のエンドポイント。OPENAI_BASE_URL で他社 API にも向けられる。"""

    name = "openai-compatible"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY が設定されていません")
        self.model = model or os.environ.get("GEO_OPENAI_MODEL", "").strip() or "gpt-4o-mini"
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")

    def answer(self, prompt: str) -> str:
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        response = _post_json(f"{self.base_url}/chat/completions", body, {"Authorization": f"Bearer {self.api_key}"})
        choices = response.get("choices") or []
        return str(choices[0]["message"]["content"]) if choices else ""


def provider_from_args(static_path: str | None, provider: str | None) -> AnswerProvider:
    if static_path:
        return StaticProvider.from_file(static_path)
    choice = (provider or "").lower() or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "openai" if os.environ.get("OPENAI_API_KEY") else "")
    if choice == "anthropic":
        return AnthropicProvider()
    if choice in ("openai", "openai-compatible"):
        return OpenAICompatibleProvider()
    raise SystemExit("回答プロバイダがありません: --static answers.json か ANTHROPIC_API_KEY / OPENAI_API_KEY を設定してください")


# ---------------------------------------------------------------------------
# 言及抽出と分類
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def extract_mentions(answer: str, brands: list[str]) -> list[dict[str, Any]]:
    """ブランド名を含む文を抜き出す。order は回答内の出現順、position_rank はブランド初出の順位 (1 始まり)。"""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(answer) if s.strip()]
    mentions: list[dict[str, Any]] = []
    first_seen: dict[str, int] = {}
    order = 0
    for index, sentence in enumerate(sentences):
        for brand in brands:
            if re.search(r"(?<![\w-])" + re.escape(brand) + r"(?![\w-])", sentence, re.IGNORECASE):
                order += 1
                if brand not in first_seen:
                    first_seen[brand] = len(first_seen) + 1
                mentions.append({"brand": brand, "sentence": sentence[:MAX_SENTENCE], "sentence_index": index, "order": order, "position_rank": first_seen[brand]})
    return mentions


def classify_mentions(jev: Jev, mentions: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    if not mentions:
        return []
    questions = {
        "sentiment": Choice(SENTIMENTS, "この文でブランドはどう言及されているか?"),
        "recommendation_strength": Score(STRENGTH, "この文はブランドをどれくらい強く勧めているか?"),
    }
    items = [({"query": query[:200], "brand": m["brand"], "sentence": m["sentence"]}, questions) for m in mentions]
    decisions = jev.decide_many(items)
    out = []
    for mention, decision in zip(mentions, decisions):
        sentiment = decision.choice("sentiment")
        strength = decision.score("recommendation_strength")
        out.append({**mention, "sentiment": sentiment.choice, "sentiment_value": SENTIMENT_VALUE[sentiment.choice], "strength_level": strength.level, "strength_score": round(strength.score, 3), "confidence": round(min(sentiment.confidence, strength.confidence), 3)})
    return out


# ---------------------------------------------------------------------------
# sqlite 保存とレポート
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts REAL, variant TEXT, provider TEXT, prompt TEXT, answer_len INTEGER);
CREATE TABLE IF NOT EXISTS mentions (id INTEGER PRIMARY KEY, run_id INTEGER, brand TEXT, sentence TEXT, sentiment TEXT, sentiment_value REAL,
  strength_level INTEGER, strength_score REAL, position_rank INTEGER, confidence REAL);
"""


def open_db(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def store_run(conn: sqlite3.Connection, variant: str, provider: str, prompt: str, answer: str, mentions: list[dict[str, Any]], ts: float | None = None) -> int:
    cursor = conn.execute("INSERT INTO runs (ts, variant, provider, prompt, answer_len) VALUES (?, ?, ?, ?, ?)", (ts or time.time(), variant, provider, prompt, len(answer)))
    run_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO mentions (run_id, brand, sentence, sentiment, sentiment_value, strength_level, strength_score, position_rank, confidence) VALUES (?,?,?,?,?,?,?,?,?)",
        [(run_id, m["brand"], m["sentence"], m["sentiment"], m["sentiment_value"], m["strength_level"], m["strength_score"], m["position_rank"], m["confidence"]) for m in mentions],
    )
    conn.commit()
    return run_id


def track(jev: Jev, provider: AnswerProvider, prompts: list[str], brands: list[str], conn: sqlite3.Connection, variant: str = "default", brand_description: str = "") -> list[dict[str, Any]]:
    """各プロンプトを回答させ、言及を分類して保存。"""
    results = []
    for template in prompts:
        prompt = template.replace("{brand_description}", brand_description)
        answer = provider.answer(prompt)
        mentions = classify_mentions(jev, extract_mentions(answer, brands), prompt)
        run_id = store_run(conn, variant, provider.name, prompt, answer, mentions)
        results.append({"run_id": run_id, "prompt": prompt, "answer_len": len(answer), "mentions": mentions})
    return results


def report(conn: sqlite3.Connection, brands: list[str], variant: str | None = None) -> dict[str, Any]:
    where, params = ("WHERE r.variant = ?", [variant]) if variant else ("", [])
    rows = conn.execute(f"SELECT m.brand, m.sentiment_value, m.strength_score, m.position_rank, r.ts, r.id FROM mentions m JOIN runs r ON m.run_id = r.id {where} ORDER BY r.ts, r.id", params).fetchall()
    total_runs = conn.execute(f"SELECT COUNT(*) FROM runs r {where}", params).fetchone()[0]
    total_mentions = len(rows)
    per_brand: dict[str, dict[str, Any]] = {}
    for brand in brands:
        brand_rows = [row for row in rows if row[0] == brand]
        runs_with = {row[5] for row in brand_rows}
        ranks_over_time = [{"ts": row[4], "rank": row[3]} for row in brand_rows]
        per_brand[brand] = {
            "mentions": len(brand_rows),
            "runs_mentioned": len(runs_with),
            "share_of_voice": round(len(brand_rows) / total_mentions, 3) if total_mentions else 0.0,
            "avg_sentiment": round(sum(r[1] for r in brand_rows) / len(brand_rows), 3) if brand_rows else None,
            "avg_strength": round(sum(r[2] for r in brand_rows) / len(brand_rows), 3) if brand_rows else None,
            "avg_rank": round(sum(r[3] for r in brand_rows) / len(brand_rows), 2) if brand_rows else None,
            "rank_over_time": ranks_over_time,
        }
    return {"variant": variant, "runs": total_runs, "mentions": total_mentions, "brands": per_brand}


def report_markdown(rep: dict[str, Any]) -> str:
    lines = [f"| brand | mentions | share of voice | avg sentiment | avg strength (0-3) | avg rank |", "| --- | --- | --- | --- | --- | --- |"]
    for brand, stats in rep["brands"].items():
        fmt = lambda v: "-" if v is None else v  # noqa: E731
        lines.append(f"| {brand} | {stats['mentions']} | {stats['share_of_voice']:.0%} | {fmt(stats['avg_sentiment'])} | {fmt(stats['avg_strength'])} | {fmt(stats['avg_rank'])} |")
    head = f"variant={rep['variant'] or 'all'} runs={rep['runs']} mentions={rep['mentions']}"
    return head + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_prompts(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("competitors", [])
    return data


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab geo", description="AI 回答でのブランド言及を追跡する GEO トラッカー")
    parser.add_argument("--prompts", required=True, help='{"brands": [...], "competitors": [...], "prompts": [...]} の JSON')
    parser.add_argument("--db", default="geo.sqlite")
    parser.add_argument("--static", default=None, help="オフライン用の回答 JSON ({prompt: answer})")
    parser.add_argument("--provider", default=None, help="anthropic | openai (未指定なら API キーの有無で選ぶ)")
    parser.add_argument("--variant", default="default")
    parser.add_argument("--ab", nargs=2, metavar=("DESC_A", "DESC_B"), default=None, help="プロンプト内の {brand_description} に入れる 2 つの説明を比較")
    parser.add_argument("--report-only", action="store_true", help="新しいランを作らずレポートだけ出す")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    config = load_prompts(args.prompts)
    brands = list(config["brands"]) + list(config["competitors"])
    conn = open_db(args.db)
    if not args.report_only:
        provider = provider_from_args(args.static, args.provider)
        jev = Jev(args.backend)
        variants = [("A", args.ab[0]), ("B", args.ab[1])] if args.ab else [(args.variant, "")]
        for name, description in variants:
            track(jev, provider, config["prompts"], brands, conn, variant=name, brand_description=description)
    variants_to_report = ["A", "B"] if args.ab else [None]
    reports = [report(conn, brands, v) for v in variants_to_report]
    for rep in reports:
        print(report_markdown(rep))
        print()
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
