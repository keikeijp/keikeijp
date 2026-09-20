import pytest

from jev_usecases import JevClient, MockBackend, ScriptedBackend
from jev_usecases.usecases import USECASES, get_usecase
from jev_usecases.usecases.review import split_diff
from jev_usecases.usecases.sniff import TELLS, split_paragraphs


def N(p):
    return {"type": "noul", "noul": p}


def C(label, conf, probs=None):
    return {"type": "choice", "choice": label, "confidence": conf, "probabilities": probs or {label: conf}}


def S(levels, value, conf=0.9):
    legend = {str(i): l for i, l in enumerate(levels)}
    probs = {str(i): (1.0 if i == round(value) else 0.0) for i in range(len(levels))}
    return {"type": "score", "score": value, "confidence": conf, "legend": legend, "probabilities": probs}


@pytest.mark.parametrize("name", list(USECASES))
def test_every_usecase_runs_end_to_end_on_mock(name):
    uc = get_usecase(name)
    client = JevClient(MockBackend())
    outcomes = uc.run(client, uc.example_items())
    assert outcomes
    for o in outcomes:
        d = o.to_dict()
        assert "needs_human" in o.decision
        assert d["usage"]["calls"] >= 1
        assert isinstance(uc.format(o), str)
    bodies = uc.dry_run(uc.example_items())
    for body in bodies:
        assert set(body) == {"state", "model", "questions"}
        for q in body["questions"].values():
            assert q["type"] in {"noul", "choice", "score"}


@pytest.mark.parametrize("name", list(USECASES))
def test_from_text_produces_runnable_item(name):
    uc = get_usecase(name)
    if name == "sample_pick":
        pytest.skip("sample_pick には候補が必要")
    item = uc.from_text("git status\nsecond line")
    uc.run(JevClient(MockBackend()), [item])


def test_get_usecase_unknown():
    with pytest.raises(KeyError):
        get_usecase("nope")


def test_triage_routes_low_confidence_to_human():
    uc = get_usecase("triage")
    levels = ["wait", "week", "today", "now"]
    confident = {"department": C("billing", 0.92), "urgency": S(levels, 1.0), "refund_requested": N(0.9), "needs_human": N(0.1)}
    unsure = {"department": C("billing", 0.5), "urgency": S(levels, 1.0), "refund_requested": N(0.2), "needs_human": N(0.1)}
    outage = {"department": C("bug", 0.95), "urgency": S(levels, 3.0), "refund_requested": N(0.0), "needs_human": N(0.1)}
    client = JevClient(ScriptedBackend([confident, unsure, outage]))
    a, b, c = uc.run(client, [{"body": "x"}] * 3)
    assert a.decision["queue"] == "auto:billing" and a.decision["refund_requested"] is True and not a.decision["needs_human"]
    assert b.decision["queue"] == "human_review" and b.decision["band"] == "abstain"
    assert c.decision["needs_human"] and c.decision["urgency"] == 3


def test_guard_verdicts():
    uc = get_usecase("guard")
    risk = ["safe", "low", "medium", "high", "critical"]
    safe = {"category": C("read_only", 0.95), "risk": S(risk, 0.0), "destructive": N(0.02), "leaves_project": N(0.05)}
    push = {"category": C("network_write", 0.9), "risk": S(risk, 1.0), "destructive": N(0.1), "leaves_project": N(0.8)}
    wipe = {"category": C("destructive", 0.97), "risk": S(risk, 4.0), "destructive": N(0.99), "leaves_project": N(0.9)}
    lowconf = {"category": C("read_only", 0.4), "risk": S(risk, 0.0, conf=0.4), "destructive": N(0.1), "leaves_project": N(0.1)}
    client = JevClient(ScriptedBackend([safe, push, wipe, lowconf]))
    verdicts = [o.decision["verdict"] for o in uc.run(client, [{"command": c} for c in ("ls", "git push", "rm -rf /", "??")])]
    assert verdicts == ["allow", "ask", "block", "ask"]


def test_split_diff_and_review_two_stage():
    diff = get_usecase("review").example_items()[0]["diff"]
    files = split_diff(diff)
    assert [f["path"] for f in files] == ["app/auth.py", "README.md"]
    assert len(files[0]["hunks"]) == 2 and len(files[1]["hunks"]) == 1
    assert split_diff("no header")[0]["path"] == "(unknown)"

    sev = ["info", "low", "medium", "high", "critical"]
    auth = {"risky": N(0.95), "kind": C("security", 0.9), "severity": S(sev, 4.0), "needs_test": N(0.8)}
    evidence = {"evidence": C("hunk_1", 0.85, {"hunk_0": 0.15, "hunk_1": 0.85})}
    readme = {"risky": N(0.02), "kind": C("style_only", 0.99), "severity": S(sev, 0.0), "needs_test": N(0.0)}
    backend = ScriptedBackend([auth, evidence, readme])
    out = get_usecase("review").run_one(JevClient(backend), {"diff": diff})
    d = out.decision
    assert d["review_order"] == ["app/auth.py", "README.md"]
    assert d["files"][0]["evidence_hunk"] == 1 and "'%s'" in d["files"][0]["evidence_excerpt"]
    assert d["files"][1]["risky"] is False and "evidence_hunk" not in d["files"][1]
    assert d["needs_human"] and len(out.results) == 3
    # 段階 2 の state にはハンクが候補として入る
    assert set(backend.calls[1]["questions"]["evidence"]["criteria"]) == {"hunk_0", "hunk_1"}


def test_sniff_flags_paragraphs():
    text = "para one\n\npara two\n\n\npara three"
    assert split_paragraphs(text) == ["para one", "para two", "para three"]
    clean = {k: N(0.05) for k in TELLS}
    bad = dict(clean, generic_opener=N(0.9), vague_claim=N(0.7))
    client = JevClient(ScriptedBackend([clean, bad, clean]))
    out = get_usecase("sniff").run_one(client, {"text": text})
    assert out.decision["paragraphs"] == 3
    assert out.decision["flagged_paragraphs"] == [{"index": 1, "flags": ["vague_claim", "generic_opener"], "excerpt": "para two"}]
    assert out.decision["worst_tell"] == "generic_opener"


def test_viral_scoring_penalizes_bait():
    uc = get_usecase("viral")
    hook = ["flat", "mild", "strong", "unmissable"]
    good = {k: N(0.9 if w > 0 else 0.05) for k, (_, w) in __import__("jev_usecases.usecases.viral", fromlist=["SIGNALS"]).SIGNALS.items()}
    good.update(hook=S(hook, 3.0), post_type=C("build_in_public", 0.9))
    bait = {k: N(0.1 if w > 0 else 0.95) for k, (_, w) in __import__("jev_usecases.usecases.viral", fromlist=["SIGNALS"]).SIGNALS.items()}
    bait.update(hook=S(hook, 0.0), post_type=C("promo", 0.9))
    a, b = uc.run(JevClient(ScriptedBackend([good, bait])), [{"post": "x"}, {"post": "y"}])
    assert a.decision["score"] > 80 and b.decision["score"] == 0
    assert "reply_bait" in b.decision["fix_first"] or "specific_claim" in b.decision["fix_first"]


def test_feed_rules_override_model_verdict():
    uc = get_usecase("feed")
    base = {"verdict": C("keep", 0.9), "ragebait": N(0.1), "ad": N(0.1), "low_effort": N(0.1), "hostile": N(0.1), "useful": N(0.9)}
    matches = dict(base, matches_rule=N(0.95))
    ad = dict(base, ad=N(0.9), matches_rule=N(0.1))
    client = JevClient(ScriptedBackend([matches, ad, base]))
    items = [{"post": "a", "rules": ["crypto"]}, {"post": "b", "rules": ["crypto"]}, {"post": "c"}]
    a, b, c = uc.run(client, items)
    assert a.decision["verdict"] == "hide" and "matches a hide rule" in a.decision["why"]
    assert b.decision["verdict"] == "skim"  # 広告だが useful が高いので skim に格下げ
    assert c.decision["verdict"] == "keep" and c.decision["matches_rule"] == 0.0


def test_rank_caches_scores_and_reranks_without_api():
    from jev_usecases.usecases.rank import AXES

    uc = get_usecase("rank")

    def answers(body):
        title = body["state"]["title"]
        depth = 3.0 if "deep" in title else 0.0
        drama = 3.0 if "drama" in title else 0.0
        return {axis: S(levels, depth if axis == "technical_depth" else drama if axis == "drama" else 1.0) for axis, (_, levels) in AXES.items()}

    backend = ScriptedBackend(answers)
    client = JevClient(backend)
    items = [{"id": "d", "title": "deep dive"}, {"id": "x", "title": "drama"}]
    out = uc.run_one(client, {"items": items})
    assert [r["id"] for r in out.decision["ranked"]] == ["d", "x"]
    assert len(backend.calls) == 2
    # 重みだけ変えて再実行 → キャッシュが効いて API は呼ばれない
    out2 = uc.run_one(client, {"items": items, "weights": {"drama": 5.0, "technical_depth": 0.0}})
    assert [r["id"] for r in out2.decision["ranked"]] == ["x", "d"]
    assert len(backend.calls) == 2 and out2.results == []
    assert uc.rerank([{"id": "a", "axes": {"drama": 1.0}}], {"drama": -1.0})[0]["weighted"] == -1.0


def test_compact_keeps_critical_and_enforces_minimum():
    from jev_usecases.usecases.compact import RELEVANCE

    uc = get_usecase("compact", keep_min=2)

    def answers(body):
        text = body["state"]["memory"]["text"]
        rel = 3.0 if "bug" in text else 0.0
        return {"relevance": S(RELEVANCE, rel), "stale": N(0.9 if "old" in text else 0.1), "safety_critical": N(0.9 if "never" in text else 0.1)}

    client = JevClient(ScriptedBackend(answers))
    mems = [{"id": "a", "text": "the bug is in reaper.py"}, {"id": "b", "text": "never push to main"}, {"id": "c", "text": "weather"}, {"id": "d", "text": "old note"}]
    out = uc.run_one(client, {"task": "fix bug", "memories": mems})
    assert out.decision["kept"] == ["a", "b"] and out.decision["dropped"] == ["c", "d"]

    uc_min = get_usecase("compact", keep_min=3)
    out = uc_min.run_one(JevClient(ScriptedBackend(answers)), {"task": "fix bug", "memories": mems})
    assert len(out.decision["kept"]) == 3
    assert any(m.get("kept_by_minimum") for m in out.decision["memories"])


def test_route_picks_tool_and_enum_args_only():
    uc = get_usecase("route")
    tool = {"tool": C("weather", 0.9), "needs_clarification": N(0.05)}
    args = {"when": C("tomorrow", 0.8)}
    backend = ScriptedBackend([tool, args])
    out = uc.run_one(JevClient(backend), {"message": "weather in Osaka tomorrow?"})
    assert out.decision["tool"] == "weather" and out.decision["args"] == {"when": "tomorrow", "city": "weather in Osaka tomorrow?"}
    assert out.decision["free_text_args"] == ["city"] and out.decision["band"] == "act"
    assert "available_tools" not in backend.calls[0]["state"]
    assert set(backend.calls[0]["questions"]["tool"]["criteria"]) == {t["name"] for t in uc.tools}

    none = {"tool": C("none", 0.6), "needs_clarification": N(0.9)}
    out = uc.run_one(JevClient(ScriptedBackend([none])), {"message": "hmm"})
    assert out.decision["needs_clarification"] and out.decision["needs_human"] and out.decision["args"] == {}


def test_sample_pick_shortlist_and_limits():
    uc = get_usecase("sample_pick", max_candidates=2)
    item = uc.example_items()[0]
    state, questions = uc.build(item)
    assert set(questions["pick"]["criteria"]) == {"c1", "c2"}
    assert "JunoPad_Am_dark.wav" in questions["pick"]["criteria"]["c1"]
    fit = ["wrong", "usable", "good", "exact"]
    out = uc.run_one(JevClient(ScriptedBackend([{"pick": C("c1", 0.8, {"c1": 0.8, "c2": 0.2}), "fit": S(fit, 3.0)}])), item)
    assert out.decision["pick"] == "c1" and out.decision["shortlist"] == ["c1", "c2"] and not out.decision["needs_human"]


def test_complexity_decision():
    from jev_usecases.usecases.complexity import COMPLEXITY

    uc = get_usecase("complexity")
    out = uc.run_one(JevClient(ScriptedBackend([{"overengineered": N(0.9), "pattern": C("unnecessary_abstraction", 0.8), "complexity": S(COMPLEXITY, 3.0)}])), {"path": "a.py", "source": "class X: ..."})
    assert out.decision["overengineered"] and out.decision["needs_human"] and out.decision["complexity"] == 3
