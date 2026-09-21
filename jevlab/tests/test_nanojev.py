import json

from jevlab.core import Choice, Jev, Noul, Score
from jevlab.local import nanojev
from jevlab.local.nanojev import (
    DodgeGame,
    FakeSlotModel,
    MultiHeadDecider,
    NanoJevBackend,
    export_sft,
    pack_questions,
    parse_packed_prompt,
    run_demo,
)

QUESTIONS = {
    "intent": {"type": "choice", "criteria": {"refund": "money back", "bug": "crash"}, "instructions": "What does the user want?"},
    "urgency": {"type": "score", "criteria": ["low", "high"]},
    "angry": {"type": "noul", "instructions": "Is the user angry?"},
}


def test_pack_questions_has_one_slot_per_question():
    packed = pack_questions({"ticket": "charged twice, refund me"}, QUESTIONS)
    assert packed.prompt.count(nanojev.SLOT) == 3
    assert packed.slots == [["A", "B"], ["A", "B"], ["Yes", "No"]]
    assert packed.names == ["intent", "urgency", "angry"] and packed.kinds == ["choice", "score", "noul"]
    state, blocks = parse_packed_prompt(packed.prompt)
    assert json.loads(state)["ticket"] == "charged twice, refund me"
    assert [b["name"] for b in blocks] == packed.names
    assert blocks[0]["options"] == {"A": "refund: money back", "B": "bug: crash"}


def test_multi_slot_decision_with_fake_model_and_backend():
    model = FakeSlotModel(hints={"urgency": "B"})
    answers = MultiHeadDecider(model).decide({"ticket": "charged twice, refund me"}, QUESTIONS)
    assert answers["intent"]["choice"] == "refund"
    assert answers["urgency"]["score"] > 0.5 and answers["urgency"]["legend"] == {"0": "low", "1": "high"}
    assert 0 <= answers["angry"]["noul"] <= 1
    assert len(model.calls) == 1  # 3 質問で 1 forward

    jev = Jev(NanoJevBackend(model))
    decision = jev.decide("charged twice, refund me", {"intent": Choice({"refund": "money back", "bug": "crash"}), "urgency": Score(["low", "high"]), "angry": Noul("Is the user angry?")})
    assert decision.backend == "nanojev" and decision.choice("intent").choice == "refund" and decision.score("urgency").level == 1
    assert len(model.calls) == 2


def test_export_sft_format():
    logs = [{"state": "s", "questions": QUESTIONS, "answers": {"intent": {"type": "choice", "choice": "bug", "probabilities": {"refund": 0.2, "bug": 0.8}}, "urgency": {"type": "score", "score": 0.9, "probabilities": {"0": 0.1, "1": 0.9}}, "angry": {"type": "noul", "noul": 0.2}}}]
    records = export_sft(logs)
    assert len(records) == 1
    messages = records[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert "Q1 [intent]" in messages[1]["content"] and nanojev.SLOT not in messages[1]["content"]
    assert json.loads(messages[2]["content"]) == {"intent": "bug", "urgency": 1, "angry": False}
    assert records[0]["slots"] == {"intent": "choice", "urgency": "score", "angry": "noul"}


def test_game_and_demo_run():
    game = DodgeGame(seed=0)
    game.obstacles = [{"lane": 1, "distance": 1, "low": False}]
    state = game.state()
    assert state["threat"] == "imminent" and "stay" not in state["safe_moves"]
    game.step("left", False)
    assert game.alive and game.dodged == 1
    result = run_demo(steps=30, model=FakeSlotModel(), seed=1)
    assert result["ticks"] > 0 and result["alive"]


def test_main_demo_and_export(tmp_path, capsys):
    assert nanojev.main(["--fake", "demo", "--steps", "10"]) == 0
    assert json.loads(capsys.readouterr().out)["ticks"] == 10
    log = tmp_path / "decisions.jsonl"
    log.write_text(json.dumps({"state": "x", "questions": {"q": {"type": "noul", "instructions": "?"}}, "answers": {"q": {"type": "noul", "noul": 0.9}}}) + "\n")
    out = tmp_path / "sft.jsonl"
    assert nanojev.main(["export", "--log", str(log), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["messages"][2]["content"] == '{"q": true}'
    try:
        import peft  # noqa: F401
    except ImportError:
        import pytest

        with pytest.raises(nanojev.JevError):
            nanojev.train_lora([], "x", str(tmp_path))
