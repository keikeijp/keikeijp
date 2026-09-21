import json

from jevlab.core import Choice, Jev, Noul, Score
from jevlab.local import jevlike
from jevlab.local.jevlike import HashedLinearScorer, JevlikeBackend, make_dataset


def test_make_dataset_is_deterministic_and_variable_length():
    a = make_dataset(200, seed=7)
    b = make_dataset(200, seed=7)
    assert a == b
    assert make_dataset(50, seed=7) == a[:50]
    assert a != make_dataset(200, seed=8)
    lengths = {len(ex["options"]) for ex in a}
    assert len(lengths) > 1 and min(lengths) >= 2
    assert all(ex["options"][ex["gold"]] for ex in a)
    assert {ex["task"] for ex in a} == {"intent", "sentiment", "math", "keyword"}


def test_hashed_scorer_learns_above_chance():
    data = make_dataset(1000, seed=1)
    train, held = data[:800], data[800:]
    scorer = HashedLinearScorer().fit(train, epochs=6)
    before = HashedLinearScorer(prior_overlap=0.0).evaluate(held)["accuracy"]
    after = scorer.evaluate(held)["accuracy"]
    assert after > 0.7 and after > before
    probs = scorer.predict_proba("route the ticket: refund the duplicate payment", ["shipping", "refund", "bug"])
    assert len(probs) == 3 and abs(sum(probs) - 1) < 1e-9 and probs[1] == max(probs)
    restored = HashedLinearScorer.from_dict(json.loads(json.dumps(scorer.to_dict())))
    assert restored.predict_proba("what is 2 plus 3", ["5", "7"]) == scorer.predict_proba("what is 2 plus 3", ["5", "7"])


def test_backend_roundtrip_via_jev():
    scorer = HashedLinearScorer().fit(make_dataset(400, seed=2), epochs=4)
    jev = Jev(JevlikeBackend(scorer))
    decision = jev.decide(
        "route the ticket: I was charged twice, please refund me",
        {"intent": Choice.of("shipping", "refund", "bug"), "tone": Score(["calm", "angry"]), "money": Noul("Is it about money?")},
    )
    assert decision.backend == "jevlike"
    assert decision.choice("intent").choice == "refund"
    assert set(decision.score("tone").probabilities) == {0, 1}
    assert 0 <= decision.noul("money").noul <= 1
    untrained = Jev(JevlikeBackend())  # 未学習でも語彙重なりの事前重みで動く
    assert untrained.choose("find the document about chess", ["notes about chess", "cooking digest"]).choice == "notes about chess"


def test_torch_scorer_requires_torch_or_builds():
    try:
        import torch  # noqa: F401
    except ImportError:
        import pytest

        with pytest.raises(jevlike.JevError):
            jevlike.build_torch_set_scorer()
    else:  # pragma: no cover - torch がある環境
        assert jevlike.build_torch_set_scorer() is not None


def test_main_gen_train_eval(tmp_path, capsys):
    data = tmp_path / "data.jsonl"
    model = tmp_path / "model.json"
    assert jevlike.main(["gen", "--n", "300", "--seed", "3", "--out", str(data)]) == 0
    assert jevlike.main(["train", "--data", str(data), "--out", str(model), "--epochs", "3"]) == 0
    assert jevlike.main(["eval", "--data", str(data), "--model", str(model)]) == 0
    assert jevlike.main(["serve-as-backend"]) == 0
    out = capsys.readouterr().out
    assert "accuracy" in out and "JevlikeBackend" in out
