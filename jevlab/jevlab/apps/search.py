"""14. jev-search (superagents-la/jev-search の再実装): 検索語の選定・期間指定・結果の関連度ソートを Jev がやる検索パイプライン。

    plan(jev, question)      : time_range (Choice) と intent (Choice) を決め、決定的に生成した候補クエリから best_query を選ぶ
    fetch(query, time_range) : SearchProvider (DuckDuckGo HTML / Brave / SerpAPI / StaticProvider) で取得
    rerank(jev, question, results) : relevance (Score) + is_spam (Noul) を並列採点して並べ替え

    python -m jevlab.apps.search --q "python で pdf を結合する方法" --provider static --results results.json --top 5 --json
    python -m jevlab.apps.search --q "latest rust release" --provider duckduckgo --backend mock

外部送信: 質問文・候補クエリ・検索結果のタイトルとスニペットが Jev API に送られる。検索プロバイダにはクエリが送られる。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from jevlab.core import Choice, Jev, Noul, Score

TIME_RANGES = {"any": "期間を限定しない", "day": "24 時間以内 (速報・障害・今日の出来事)", "week": "1 週間以内", "month": "1 か月以内", "year": "1 年以内 (最近のリリース・価格)"}
INTENTS = {
    "news": "ニュース・時事・最新の出来事",
    "howto": "やり方・手順・トラブルシューティング",
    "reference": "定義・仕様・百科事典的な事実",
    "product": "製品・価格・比較・レビュー",
    "code": "ライブラリ・API・ソースコード・エラーメッセージ",
    "local": "近くの店・場所・営業時間",
}
SITE_HINTS = {"news": "site:reuters.com OR site:apnews.com", "howto": "site:stackoverflow.com", "reference": "site:wikipedia.org", "product": "review", "code": "site:github.com", "local": "near me"}
RELEVANCE = ["無関係", "少し関係する", "関係する", "とても関係する", "質問にそのまま答えている"]
_STOPWORDS = set("a an the of to for in on with and or is are how what why when where do does can i my me を に の は が で と へ や も する ください 方法 教えて".split())


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published: str | None = None
    relevance: float | None = None
    spam: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchPlan:
    question: str
    time_range: str
    intent: str
    query: str
    candidates: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 計画 (クエリ候補は決定的に作り、Jev は選ぶだけ)
# ---------------------------------------------------------------------------


def keywords(question: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_.+#-]+|[぀-ヿ一-鿿]+", question)
    return [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 1]


def candidate_queries(question: str, intent: str) -> dict[str, str]:
    """original / keywords / quoted / site-hint の候補を {ラベル: クエリ} で返す (Jev の Choice の criteria)。"""
    q = " ".join(question.split())
    kw = " ".join(keywords(q)) or q
    cands = {"original": q, "keywords": kw}
    if len(kw.split()) >= 2:
        cands["quoted"] = f'"{kw}"'
    hint = SITE_HINTS.get(intent)
    if hint:
        cands["hinted"] = f"{kw} {hint}"
    return {label: query for label, query in cands.items() if query}


def plan(jev: Jev, question: str) -> SearchPlan:
    """1 回目: time_range + intent。2 回目: intent に応じた候補クエリから best_query。"""
    first = jev.decide({"question": question}, {"time_range": Choice(TIME_RANGES, "この質問に答えるのに結果を絞るべき期間は?"), "intent": Choice(INTENTS, "質問の種類は?")})
    time_range = first.choice("time_range").choice
    intent = first.choice("intent").choice
    cands = candidate_queries(question, intent)
    labels = {label: f"{query}  (候補: {label})" for label, query in cands.items()}
    second = jev.decide({"question": question, "intent": intent, "candidates": cands}, {"best_query": Choice(labels, "検索エンジンに投げると最も良い結果が返りそうなクエリは?")})
    answer = second.choice("best_query")
    best = cands.get(answer.choice, cands["original"])
    return SearchPlan(question, time_range, intent, best, cands, round(answer.confidence, 4))


# ---------------------------------------------------------------------------
# 取得 (プロバイダ差し替え可)
# ---------------------------------------------------------------------------


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, time_range: str = "any", limit: int = 10) -> list[SearchResult]: ...


class StaticProvider:
    """JSON ファイル / リストから返すオフライン用プロバイダ。クエリの語を含む結果を優先するだけ。"""

    name = "static"

    def __init__(self, results: Sequence[SearchResult | dict[str, Any]] | None = None, path: str | None = None):
        items: list[Any] = list(results or [])
        if path:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            items.extend(data.get("results", data) if isinstance(data, dict) else data)
        self.results = [item if isinstance(item, SearchResult) else SearchResult(**{k: v for k, v in item.items() if k in SearchResult.__dataclass_fields__}) for item in items]

    def search(self, query: str, time_range: str = "any", limit: int = 10) -> list[SearchResult]:
        terms = [t.lower() for t in keywords(query)]

        def hits(result: SearchResult) -> int:
            text = f"{result.title} {result.snippet}".lower()
            return sum(1 for t in terms if t in text)

        ordered = sorted(self.results, key=hits, reverse=True) if terms else list(self.results)
        return [SearchResult(**r.to_dict()) for r in ordered[:limit]]


class DuckDuckGoProvider:
    """DuckDuckGo の HTML 版を urllib で叩いて正規表現で拾う (API キー不要、規約と頻度に注意)。"""

    name = "duckduckgo"
    _DF = {"day": "d", "week": "w", "month": "m", "year": "y"}
    _ITEM_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def search(self, query: str, time_range: str = "any", limit: int = 10) -> list[SearchResult]:
        params = {"q": query}
        if time_range in self._DF:
            params["df"] = self._DF[time_range]
        request = urllib.request.Request("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(params), headers={"User-Agent": "Mozilla/5.0 jevlab/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        return self.parse(body)[:limit]

    @classmethod
    def parse(cls, body: str) -> list[SearchResult]:
        results = []
        for href, title, snippet in cls._ITEM_RE.findall(body):
            url = href
            if "uddg=" in href:  # リダイレクトラッパを剥がす
                url = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
            results.append(SearchResult(html.unescape(re.sub(r"<[^>]+>", "", title)).strip(), url, html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()))
        return results


class BraveProvider:
    name = "brave"
    _FRESH = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY が必要です")
        self.timeout = timeout

    def search(self, query: str, time_range: str = "any", limit: int = 10) -> list[SearchResult]:
        params = {"q": query, "count": limit}
        if time_range in self._FRESH:
            params["freshness"] = self._FRESH[time_range]
        request = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params), headers={"Accept": "application/json", "X-Subscription-Token": self.api_key})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [SearchResult(r.get("title", ""), r.get("url", ""), r.get("description", ""), r.get("age")) for r in data.get("web", {}).get("results", [])][:limit]


class SerpAPIProvider:
    name = "serpapi"
    _TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("SERPAPI_API_KEY が必要です")
        self.timeout = timeout

    def search(self, query: str, time_range: str = "any", limit: int = 10) -> list[SearchResult]:
        params = {"q": query, "api_key": self.api_key, "num": limit, "engine": "google"}
        if time_range in self._TBS:
            params["tbs"] = self._TBS[time_range]
        with urllib.request.urlopen("https://serpapi.com/search.json?" + urllib.parse.urlencode(params), timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [SearchResult(r.get("title", ""), r.get("link", ""), r.get("snippet", ""), r.get("date")) for r in data.get("organic_results", [])][:limit]


def provider_from_name(name: str, results_path: str | None = None) -> SearchProvider:
    name = name.lower()
    if name == "static":
        if not results_path:
            raise ValueError("--provider static には --results results.json が必要です")
        return StaticProvider(path=results_path)
    if name in {"duckduckgo", "ddg"}:
        return DuckDuckGoProvider()
    if name == "brave":
        return BraveProvider()
    if name == "serpapi":
        return SerpAPIProvider()
    raise ValueError(f"不明なプロバイダ: {name}")


def fetch(query: str, time_range: str = "any", provider: SearchProvider | None = None, limit: int = 10) -> list[SearchResult]:
    return (provider or DuckDuckGoProvider()).search(query, time_range, limit)


# ---------------------------------------------------------------------------
# 再ランク
# ---------------------------------------------------------------------------


def rerank(jev: Jev, question: str, results: Sequence[SearchResult], spam_threshold: float = 0.8) -> list[SearchResult]:
    """relevance (0..1) x (1 - spam) で降順。spam 確率が閾値以上のものは落とす。"""
    if not results:
        return []
    questions = {"relevance": Score(RELEVANCE, "この検索結果は質問にどれくらい関係するか?"), "is_spam": Noul("この結果はスパム・SEO 目的の薄い内容・広告か?")}
    states = [{"question": question, "result": {"title": r.title, "url": r.url, "snippet": r.snippet[:400]}} for r in results]
    decisions = jev.decide_many((state, questions) for state in states)
    scored: list[SearchResult] = []
    for result, decision in zip(results, decisions):
        item = SearchResult(**result.to_dict())
        item.relevance = round(decision.score("relevance").normalized(), 4)
        item.spam = round(decision.noul("is_spam").noul, 4)
        if item.spam < spam_threshold:
            scored.append(item)
    scored.sort(key=lambda r: (r.relevance or 0.0) * (1.0 - (r.spam or 0.0)), reverse=True)
    return scored


def run(jev: Jev, question: str, provider: SearchProvider, top: int = 5, limit: int = 10) -> dict[str, Any]:
    search_plan = plan(jev, question)
    results = fetch(search_plan.query, search_plan.time_range, provider, limit)
    ranked = rerank(jev, question, results)[:top]
    return {"plan": search_plan.to_dict(), "results": [r.to_dict() for r in ranked], "provider": provider.name, "jev": jev.stats()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab search", description=__doc__.splitlines()[0])
    parser.add_argument("--q", required=True, help="質問文")
    parser.add_argument("--provider", default="static", help="static / duckduckgo / brave / serpapi")
    parser.add_argument("--results", default=None, help="static 用の結果 JSON")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10, help="プロバイダから取る件数")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    args = parser.parse_args(argv)

    jev = Jev(args.backend, cache=True)
    output = run(jev, args.q, provider_from_name(args.provider, args.results), args.top, args.limit)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    p = output["plan"]
    print(f"query: {p['query']}  [intent={p['intent']} time={p['time_range']} conf={p['confidence']:.2f}]")
    for i, r in enumerate(output["results"], 1):
        print(f"{i}. ({r['relevance']:.2f}, spam={r['spam']:.2f}) {r['title']}\n   {r['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
