import json

from jevlab.apps import router
from jevlab.core import Jev, ScriptedBackend


def _messages(text):
    return [{"role": "user", "content": text}]


def test_build_state_extracts_features():
    messages = [
        {"role": "user", "content": "please fix src/app.py and tests/test_app.py\n```py\nprint(1)\n```"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "x" * 3000}, {"type": "image", "source": {}}]},
    ]
    state = router.build_state(messages, tools=[{"name": "bash"}])
    assert state["n_turns"] == 3 and state["has_tools"] is True
    assert state["files_mentioned"] == ["src/app.py", "tests/test_app.py"]
    assert state["code_blocks_count"] == 1
    assert len(state["last_user_message"]) == 2000
    assert state["has_images"] is True


def test_routing_maps_difficulty_and_task_kind_to_models():
    backend = ScriptedBackend(
        [
            {"task_kind": "trivial_chat", "difficulty": 0, "needs_vision": False},
            {"task_kind": "code_edit_small", "difficulty": 2, "needs_vision": False},
            {"task_kind": "debugging", "difficulty": 4, "needs_vision": False},
            {"task_kind": "architecture", "difficulty": 1, "needs_vision": False},
            {"task_kind": "code_edit_small", "difficulty": 1, "needs_vision": True},
        ]
    )
    jev = Jev(backend)
    assert router.route(jev, _messages("hi")).model == "claude-haiku-4-5-20251001"
    assert router.route(jev, _messages("rename a var")).model == "claude-sonnet-5"
    d = router.route(jev, _messages("why does it crash"))
    assert d.model == "claude-opus-5" and d.difficulty == "expert" and d.task_kind == "debugging"
    d = router.route(jev, _messages("design the system"))
    assert d.model == "claude-opus-5" and "override" in d.reason
    d = router.route(jev, _messages("what is in this screenshot"))
    assert d.needs_vision and d.model == "claude-sonnet-5" and d.reason == "needs vision"
    assert set(backend.calls[0]["questions"]) == {"task_kind", "difficulty", "needs_vision"}


def test_policy_override_and_low_confidence_fallback(tmp_path):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"models": {"trivial": "gpt-5-mini"}, "task_overrides": {"debugging": "o4-mini"}, "fallback": "gpt-5", "min_confidence": 0.5}), encoding="utf-8")
    policy = router.load_policy(str(policy_file))
    assert policy["models"]["moderate"] == "claude-sonnet-5"  # マージされる
    backend = ScriptedBackend(
        [
            {"task_kind": "trivial_chat", "difficulty": 0, "needs_vision": False},
            {"task_kind": "debugging", "difficulty": 3, "needs_vision": False},
            {"task_kind": {"trivial_chat": 0.4, "research": 0.35, "debugging": 0.25}, "difficulty": 0, "needs_vision": False},
        ]
    )
    jev = Jev(backend)
    assert router.route(jev, _messages("hi"), policy=policy).model == "gpt-5-mini"
    assert router.route(jev, _messages("bug"), policy=policy).model == "o4-mini"
    d = router.route(jev, _messages("?"), policy=policy)
    assert d.model == "gpt-5" and "fallback" in d.reason


def test_rewrite_request_for_both_api_shapes():
    backend = ScriptedBackend(default={"task_kind": "code_edit_large", "difficulty": 3, "needs_vision": False})
    jev = Jev(backend)
    anthropic_body = {"model": "auto", "max_tokens": 100, "system": "be brief", "messages": [{"role": "user", "content": [{"type": "text", "text": "refactor"}]}], "stream": True}
    new, decision = router.rewrite_request(anthropic_body, jev)
    assert new["model"] == "claude-opus-5" and new["stream"] is True and new["messages"] == anthropic_body["messages"]
    assert anthropic_body["model"] == "auto"  # 元の本文は変更しない
    openai_body = {"model": "gpt-4o", "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "refactor"}]}
    new, decision = router.rewrite_request(openai_body, jev, {"models": {"hard": "gpt-5"}})
    assert new["model"] == "gpt-5"
    assert backend.calls[-1]["state"]["last_user_message"] == "refactor" and backend.calls[-1]["state"]["n_turns"] == 1
    new, decision = router.rewrite_request(openai_body, jev, {"respect_client_model": True})
    assert new["model"] == "gpt-4o"


def test_upstream_for_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:9999/")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert router.upstream_for("/v1/messages") == "http://localhost:9999/v1/messages"
    assert router.upstream_for("/v1/chat/completions") == "https://api.openai.com/v1/chat/completions"
    assert router.upstream_for("/v1/chat/completions", openai_base="http://x:1") == "http://x:1/v1/chat/completions"


def test_main_print_only(capsys):
    assert router.main(["--print-only", "hello there", "--backend", "mock"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] and out["task_kind"] in router.TASK_KINDS and out["difficulty"] in router.DIFFICULTY
