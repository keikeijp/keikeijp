import json

from jevlab.apps import geo
from jevlab.apps.geo import StaticProvider, classify_mentions, extract_mentions, open_db, report, report_markdown, track
from jevlab.core import FunctionBackend, Jev, ScriptedBackend

ANSWER = (
    "For note-taking I would recommend Notion as the top pick. "
    "Obsidian is also a solid choice for local files! Evernote has fallen behind and I would avoid it.\n"
    "Notion's pricing is reasonable."
)


def test_extract_mentions_order_and_rank():
    mentions = extract_mentions(ANSWER, ["Notion", "Obsidian", "Evernote", "Roam"])
    brands = [m["brand"] for m in mentions]
    assert brands == ["Notion", "Obsidian", "Evernote", "Notion"]
    assert [m["position_rank"] for m in mentions] == [1, 2, 3, 1]
    assert mentions[0]["order"] == 1 and mentions[-1]["order"] == 4
    assert "Notion's pricing" in mentions[-1]["sentence"]
    # 部分一致はしない (Notional など)
    assert extract_mentions("Notional value is high.", ["Notion"]) == []


def test_classify_mentions_with_scripted_backend():
    mentions = extract_mentions(ANSWER, ["Notion", "Evernote"])
    backend = ScriptedBackend([
        {"sentiment": "positive", "recommendation_strength": 3},
        {"sentiment": "negative", "recommendation_strength": 0},
        {"sentiment": "neutral", "recommendation_strength": 1},
    ])
    out = classify_mentions(Jev(backend), mentions, query="best note app")
    assert [m["sentiment"] for m in out] == ["positive", "negative", "neutral"]
    assert [m["strength_level"] for m in out] == [3, 0, 1]
    assert out[0]["sentiment_value"] == 1.0 and out[1]["sentiment_value"] == -1.0
    assert backend.calls[0]["state"] == {"query": "best note app", "brand": "Notion", "sentence": mentions[0]["sentence"]}
    assert classify_mentions(Jev(backend), [], "q") == []


def test_track_report_and_ab_variants():
    provider = StaticProvider({"best note app?": ANSWER, "cheap note app?": "Obsidian is free. Notion has a free tier too."})
    jev = Jev(FunctionBackend(lambda s, q: {"sentiment": "negative" if "avoid" in s["sentence"] else "positive", "recommendation_strength": 3 if "top pick" in s["sentence"] else 2}))
    conn = open_db(":memory:")
    results = track(jev, provider, ["best note app?", "cheap note app?"], ["Notion", "Obsidian", "Evernote"], conn, variant="A")
    assert len(results) == 2 and results[0]["mentions"][0]["brand"] == "Notion"
    rep = report(conn, ["Notion", "Obsidian", "Evernote"], variant="A")
    assert rep["runs"] == 2 and rep["mentions"] == 6
    assert rep["brands"]["Notion"]["mentions"] == 3 and rep["brands"]["Notion"]["runs_mentioned"] == 2
    assert rep["brands"]["Evernote"]["avg_sentiment"] == -1.0
    assert abs(sum(b["share_of_voice"] for b in rep["brands"].values()) - 1.0) < 0.01
    assert rep["brands"]["Notion"]["rank_over_time"][0]["rank"] == 1
    md = report_markdown(rep)
    assert "| Notion | 3 |" in md and "variant=A runs=2" in md
    # 別バリアントは分けて集計される
    track(jev, provider, ["best note app?"], ["Notion"], conn, variant="B")
    assert report(conn, ["Notion"], variant="B")["runs"] == 1
    assert report(conn, ["Notion"])["runs"] == 3


def test_main_static_provider(tmp_path, capsys):
    (tmp_path / "prompts.json").write_text(json.dumps({"brands": ["Notion"], "competitors": ["Obsidian"], "prompts": ["best note app? {brand_description}"]}), encoding="utf-8")
    (tmp_path / "answers.json").write_text(json.dumps({"best note app? ": ANSWER, "best note app? fast": ANSWER, "best note app? cheap": "Obsidian wins."}), encoding="utf-8")
    db = tmp_path / "geo.sqlite"
    assert geo.main(["--prompts", str(tmp_path / "prompts.json"), "--static", str(tmp_path / "answers.json"), "--db", str(db), "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert "| Notion |" in out and "| Obsidian |" in out
    assert geo.main(["--prompts", str(tmp_path / "prompts.json"), "--static", str(tmp_path / "answers.json"), "--db", str(db), "--backend", "mock", "--ab", "fast", "cheap", "--json-out", str(tmp_path / "r.json")]) == 0
    reports = json.loads((tmp_path / "r.json").read_text())
    assert [r["variant"] for r in reports] == ["A", "B"]
    assert reports[1]["brands"]["Obsidian"]["mentions"] == 1
    assert geo.main(["--prompts", str(tmp_path / "prompts.json"), "--db", str(db), "--report-only"]) == 0
