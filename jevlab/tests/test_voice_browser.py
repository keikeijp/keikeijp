from jevlab.core import Jev, ScriptedBackend

from jevlab.apps import voice_browser
from jevlab.apps.ultrafast import DEMO_SCENES, FakePage, snapshot
from jevlab.apps.voice_browser import Session, execute, extract_intent, fill_slots


def test_fill_slots_regexes():
    assert fill_slots("navigate", "open example.com please") == {"url": "https://example.com"}
    assert fill_slots("navigate", "go to https://typesafe.ai/jev") == {"url": "https://typesafe.ai/jev"}
    assert fill_slots("navigate", "open wikipedia") == {"url": "https://wikipedia.com"}
    assert fill_slots("search", "search for jev typesafe on duckduckgo") == {"query": "jev typesafe"}
    assert fill_slots("search", "猫の写真を検索して") == {"query": "猫の写真"}
    assert fill_slots("scroll", "scroll up a bit") == {"direction": "up"}
    assert fill_slots("scroll", "scroll down") == {"direction": "down"}
    assert fill_slots("stop", "stop") == {}


def test_extract_intent_with_scripted_backend_picks_target_for_click():
    backend = ScriptedBackend([{"intent": "click_link", "target": "3"}, {"intent": "search", "target": "0"}])
    jev = Jev(backend)
    elements = snapshot(FakePage(DEMO_SCENES))
    intent = extract_intent(jev, "click sign in", elements, "Example Search")
    assert intent.name == "click_link" and intent.target == 3 and "Sign in" in intent.target_description
    wire = backend.calls[0]["questions"]
    assert set(wire["intent"]["criteria"]) == set(voice_browser.INTENTS)
    assert set(wire["target"]["criteria"]) == {"0", "1", "2", "3"}
    search = extract_intent(jev, "search for jev typesafe", elements)
    assert search.name == "search" and search.slots == {"query": "jev typesafe"} and search.target is None


def test_session_loop_on_fake_page():
    backend = ScriptedBackend(
        [
            {"intent": "search", "target": "0"},
            {"intent": "click_link", "target": "0"},
            {"intent": "read_aloud", "target": "0"},
            {"intent": "stop", "target": "0"},
        ]
    )
    page = FakePage(DEMO_SCENES)
    spoken: list[str] = []
    session = Session(Jev(backend), page, speak=spoken.append, log=lambda _: None)
    transcript = session.run(["search for jev typesafe", "open the first result", "read the page", "stop"])
    assert [t["intent"] for t in transcript] == ["search", "click_link", "read_aloud", "stop"]
    assert page.filled[1] == "jev typesafe"
    assert page.url == "https://typesafe.ai/jev"
    assert spoken and spoken[0].startswith("Jev - TypeSafe")
    assert transcript[1]["outcome"].startswith("clicked")


def test_low_confidence_asks_to_repeat_instead_of_executing():
    probs = {name: (0.2 if name in {"navigate", "search"} else 0.1) for name in voice_browser.INTENTS}
    backend = ScriptedBackend([{"intent": {"type": "choice", "choice": "navigate", "probabilities": probs, "confidence": 0.2}, "target": "0"}])
    page = FakePage(DEMO_SCENES)
    spoken: list[str] = []
    Session(Jev(backend), page, min_confidence=0.5, speak=spoken.append, log=lambda _: None).handle("open example.com")
    assert page.actions == [] and spoken == ["Sorry, could you say that again?"]


def test_execute_navigate_and_go_back():
    page = FakePage(DEMO_SCENES)
    elements = snapshot(page)
    assert execute(page, voice_browser.Intent("navigate", 0.9, {"url": "https://typesafe.ai/jev"}), elements) == "navigated to typesafe.ai"
    assert execute(page, voice_browser.Intent("go_back", 0.9), elements) == "went back"
    assert page.url == "https://example.test/"


def test_main_text_dry_run_offline(capsys):
    code = voice_browser.main(["--text", "search for jev", "--text", "stop", "--dry-run", "--backend", "mock"])
    assert code == 0
    assert "turns=" in capsys.readouterr().out
