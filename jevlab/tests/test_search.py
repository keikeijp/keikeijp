import json

from jevlab.apps import search
from jevlab.apps.search import DuckDuckGoProvider, SearchResult, StaticProvider, candidate_queries, plan, rerank
from jevlab.core import FunctionBackend, Jev, ScriptedBackend

RESULTS = [
    {"title": "Merge PDF files with Python (pypdf)", "url": "https://example.org/pypdf", "snippet": "How to merge pdf files using pypdf in python"},
    {"title": "Best cheap flights", "url": "https://spam.example/flights", "snippet": "buy now cheap cheap"},
    {"title": "PDF specification", "url": "https://example.org/pdf-spec", "snippet": "ISO 32000 reference"},
]


def test_candidate_queries_are_deterministic():
    cands = candidate_queries("how to merge pdf files in python", "code")
    assert cands["original"] == "how to merge pdf files in python"
    assert cands["keywords"] == "merge pdf files python"
    assert cands["quoted"] == '"merge pdf files python"'
    assert cands["hinted"].endswith("site:github.com")
    assert "hinted" not in candidate_queries("x", "unknown") and candidate_queries("x", "unknown")["original"] == "x"


def test_plan_with_scripted_backend():
    backend = ScriptedBackend([{"time_range": "week", "intent": "code"}, {"best_query": "hinted"}])
    p = plan(Jev(backend), "how to merge pdf files in python")
    assert p.time_range == "week" and p.intent == "code" and p.query == "merge pdf files python site:github.com"
    assert set(backend.calls[0]["questions"]) == {"time_range", "intent"}
    assert list(backend.calls[1]["questions"]["best_query"]["criteria"]) == ["original", "keywords", "quoted", "hinted"]
    assert p.confidence == 0.9


def test_rerank_orders_by_relevance_and_drops_spam():
    def rule(state, questions):
        title = state["result"]["title"].lower()
        return {"relevance": 4 if "python" in title else 2 if "pdf" in title else 0, "is_spam": "cheap" in title}

    jev = Jev(FunctionBackend(rule))
    ranked = rerank(jev, "merge pdf in python", [SearchResult(**r) for r in RESULTS])
    assert [r.url for r in ranked] == ["https://example.org/pypdf", "https://example.org/pdf-spec"]
    assert ranked[0].relevance >= 0.9 and ranked[1].spam < 0.5
    assert rerank(jev, "q", []) == []


def test_static_provider_and_ddg_parse():
    provider = StaticProvider(RESULTS)
    hits = provider.search("python pdf", limit=2)
    assert [r.url for r in hits] == ["https://example.org/pypdf", "https://example.org/pdf-spec"]
    html = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=1">Title <b>A</b></a><a class="result__snippet" href="x">Snippet &amp; more</a>'
    parsed = DuckDuckGoProvider.parse(html)
    assert parsed == [SearchResult("Title A", "https://example.org/a", "Snippet & more")]


def test_main_static_end_to_end(tmp_path, capsys):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"results": RESULTS}), encoding="utf-8")
    assert search.main(["--q", "how to merge pdf files in python", "--provider", "static", "--results", str(path), "--top", "2", "--json", "--backend", "mock"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provider"] == "static" and out["plan"]["query"] and len(out["results"]) <= 2
    assert all("relevance" in r for r in out["results"])
    assert search.main(["--q", "pdf", "--provider", "static", "--results", str(path), "--backend", "mock"]) == 0
    assert "query:" in capsys.readouterr().out
