import json

from jevlab.apps import unclutter
from jevlab.core import FunctionBackend, Jev, ScriptedBackend

CANDIDATES = [
    {"idx": 0, "tag": "div", "id_hint": "cookie-banner", "text": "We use cookies. Accept all", "position": "fixed", "size_ratio": 0.1, "z_index": 9999, "has_close_button": True},
    {"idx": 1, "tag": "iframe", "iframe_ad_domain_hint": "doubleclick", "position": "static", "size_ratio": 0.05},
    {"idx": 2, "tag": "nav", "text": "Home News Sports", "position": "sticky", "size_ratio": 0.08},
]


def test_strict_acceptance_thresholds():
    # 0: cookie 高確率 → 採用 / 1: ad だが確率 0.7 → keep / 2: keep
    backend = ScriptedBackend(
        [
            {"kind": "cookie"},  # 0.9 / 0.9 → 境界値で採用
            {"kind": {"ad": 0.7, "keep": 0.3}},
            {"kind": "keep"},
        ]
    )
    results = unclutter.classify_candidates(Jev(backend, max_workers=1), CANDIDATES)
    by_idx = {r["idx"]: r for r in results}
    assert by_idx[0]["accepted"] and by_idx[0]["label"] == "cookie"
    assert not by_idx[1]["accepted"] and by_idx[1]["label"] == "keep" and by_idx[1]["raw_label"] == "ad"
    assert not by_idx[2]["accepted"]
    # state に URL や生 HTML が入っていない (上限付きの記述のみ)
    state = backend.calls[0]["state"]
    assert set(state) == set(unclutter.normalize_candidate({}))
    assert len(state["text"]) <= unclutter.MAX_TEXT


def test_uncertain_never_hidden_even_when_confident():
    jev = Jev(FunctionBackend(lambda s, q: {"kind": "uncertain"}))
    results = unclutter.classify_candidates(jev, CANDIDATES)
    assert all(not r["accepted"] for r in results)
    assert unclutter.build_css(results) == ""


def test_css_rule_generation_and_rules_roundtrip(tmp_path):
    jev = Jev(FunctionBackend(lambda s, q: {"kind": "ad" if s["tag"] == "iframe" else "keep"}))
    results = unclutter.classify_candidates(jev, CANDIDATES)
    css = unclutter.build_css(results)
    assert css == '[data-jevlab-unclutter="1"] { display: none !important; }'
    rules = unclutter.build_rules("example.com", results)
    path = unclutter.save_rules(tmp_path, rules)
    assert path.name == "example.com.json"
    loaded = unclutter.load_rules(tmp_path, "example.com")
    assert loaded["rules"][0]["label"] == "ad"
    assert "display: none" in unclutter.css_from_rules(loaded)
    assert unclutter.selector_for(unclutter.normalize_candidate(CANDIDATES[0])) == '[id="cookie-banner"]'


def test_main_classify(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(CANDIDATES), encoding="utf-8")
    assert unclutter.main(["--backend", "mock", "classify", "--json", str(path), "--host", "example.com"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["results"]) == 3 and "css" in out and out["rules"]["host"] == "example.com"
