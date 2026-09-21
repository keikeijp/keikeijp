import os

from jevlab.core import Jev, ScriptedBackend

from jevlab.apps import ultrafast
from jevlab.apps.ultrafast import DEMO_SCENES, Agent, FakePage, choose, heuristic_text, snapshot


def test_snapshot_builds_compact_indexed_action_space():
    page = FakePage(DEMO_SCENES)
    elements = snapshot(page)
    assert [e["index"] for e in elements] == [0, 1, 2, 3]
    assert elements[1]["tag"] == "input" and elements[1]["role"] == "search" and elements[1]["name"] == "Search the web"
    assert elements[3]["href_domain"] == "accounts.example.test"  # フル URL は残さない
    assert "href" not in elements[3]
    assert all(len(e["text"]) <= 60 for e in elements)


def test_choose_maps_scripted_answers_to_operation_and_target():
    backend = ScriptedBackend([{"operation": "type", "target": "1"}])
    jev = Jev(backend)
    elements = snapshot(FakePage(DEMO_SCENES))
    step = choose(jev, "Search for jev", elements, [], "Example Search", "example.test")
    assert step.operation == "type" and step.target == 1
    assert "Search the web" in step.description
    wire = backend.calls[0]["questions"]
    assert set(wire["operation"]["criteria"]) == set(ultrafast.OPERATIONS)
    assert set(wire["target"]["criteria"]) == {"0", "1", "2", "3"}
    assert backend.calls[0]["state"]["page"]["host"] == "example.test"


def test_candidates_prefers_visible_and_caps_at_60():
    elements = [{"index": i, "tag": "a", "role": "", "text": f"t{i}", "name": "", "href_domain": "", "visible": i % 2 == 0} for i in range(100)]
    cands = ultrafast.candidates(elements)
    assert len(cands) == 60
    assert all(c["visible"] for c in cands[:50])


def test_heuristic_text_extracts_search_terms_and_emails():
    search_box = {"tag": "input", "role": "search", "name": "Search", "text": ""}
    assert heuristic_text("Search for jev typesafe on duckduckgo", search_box) == "jev typesafe"
    assert heuristic_text('type "hello world" into the box', search_box) == "hello world"
    assert heuristic_text("「猫の写真」を検索して", search_box) == "猫の写真"
    email_box = {"tag": "input", "role": "email", "name": "Email address", "text": ""}
    assert heuristic_text("sign up with alice@example.com", email_box) == "alice@example.com"


def test_agent_loop_on_fake_page_reaches_done():
    backend = ScriptedBackend(
        [
            {"operation": "type", "target": "1"},  # 検索欄に入力 → 結果ページへ
            {"operation": "click", "target": "0"},  # 先頭の結果をクリック
            {"operation": "done", "target": "0"},
        ]
    )
    page = FakePage(DEMO_SCENES)
    agent = Agent(Jev(backend), page, text_generator=heuristic_text, log=lambda _: None)
    result = agent.run("Search for jev typesafe", max_steps=5)
    assert result.status == "done"
    assert [s.operation for s in result.steps] == ["type", "click", "done"]
    assert page.filled[1] == "jev typesafe"
    assert result.final_url == "https://typesafe.ai/jev"
    assert agent.history[0]["outcome"].startswith("typed")


def test_agent_stops_after_repeated_low_confidence():
    # "type" は操作ラベルでもあるので、完全な回答 dict で渡す
    probs = {"click": 0.2, "type": 0.2, "scroll_down": 0.2, "scroll_up": 0.2, "go_back": 0.2, "done": 0.0, "give_up": 0.0}
    low = {"operation": {"type": "choice", "choice": "click", "probabilities": probs, "confidence": 0.2}, "target": "0"}
    backend = ScriptedBackend([low, low, low, low])
    page = FakePage(DEMO_SCENES)
    result = Agent(Jev(backend), page, min_confidence=0.5, max_low_confidence=2, log=lambda _: None).run("x", max_steps=10)
    assert result.status == "low_confidence"
    assert page.actions == []  # 閾値未満は実行しない


def test_main_dry_run_offline(capsys):
    os.environ.pop("ANTHROPIC_API_KEY", None)
    code = ultrafast.main(["--goal", "Search for jev", "--dry-run", "--backend", "mock", "--max-steps", "2"])
    assert code in (0, 1)
    out = capsys.readouterr().out
    assert "status=" in out
