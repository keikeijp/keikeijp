import json

from jevlab.apps import compaction
from jevlab.core import FunctionBackend, Jev, ScriptedBackend


def _cc(role, blocks, uuid):
    return {"type": role, "uuid": uuid, "message": {"role": role, "content": blocks}}


def _transcript():
    """探索的な Read → 本命の Edit → テスト実行 → 末尾のやりとり、という合成トランスクリプト。"""
    big = "line\n" * 400
    return [
        _cc("user", [{"type": "text", "text": "fix the failing test in tests/test_x.py"}], "u1"),
        _cc("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/README.md", "note": "x" * 1500}}], "a1"),
        _cc("user", [{"type": "tool_result", "tool_use_id": "t1", "content": big}], "u2"),
        _cc("assistant", [{"type": "text", "text": "I found the bug."}, {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/repo/x.py"}}], "a2"),
        _cc("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "ok " * 300}], "u3"),
        _cc("assistant", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -q"}}], "a3"),
        _cc("user", [{"type": "tool_result", "tool_use_id": "t3", "content": [{"type": "text", "text": "1 passed"}]}], "u4"),
        _cc("assistant", [{"type": "text", "text": "Done, all green."}], "a4"),
        _cc("user", [{"type": "text", "text": "thanks, now add a docstring"}], "u5"),
    ]


def test_find_pairs_and_state_marks_candidate():
    messages = _transcript()
    pairs = compaction.find_tool_pairs(messages)
    assert [p.name for p in pairs] == ["Read", "Edit", "Bash"]
    assert pairs[0].result_msg == 2
    state = compaction.build_state(messages, pairs[0])
    joined = "\n".join(state["conversation"])
    assert joined.count("<<CANDIDATE>>") == 2
    assert state["candidate"]["tool"] == "Read"
    assert len(json.dumps(state)) < 4000  # 巨大な結果本文は state に入らない


def test_graduated_compression_with_scripted_backend():
    messages = _transcript()
    backend = ScriptedBackend(
        [
            {"keep_call": False, "keep_result_verbatim": False},  # Read → drop
            {"keep_call": True, "keep_result_verbatim": False},  # Edit → truncate
        ]
    )
    new, report = compaction.compact_messages(messages, Jev(backend), preserve_recent_messages=4)
    assert len(backend.calls) == 2  # 末尾 4 件 (Bash ペアを含む) は Jev に聞かない
    assert set(backend.calls[0]["questions"]) == {"keep_call", "keep_result_verbatim"}
    assert report.pairs_total == 3 and report.pairs_pinned == 1
    assert report.pairs_dropped == 1 and report.pairs_truncated == 1
    assert report.reduction_ratio > 0.5
    ids = [m["uuid"] for m in new]
    assert "a1" not in ids and "u2" not in ids  # Read ペアは丸ごと消えた
    edit_result = next(m for m in new if m["uuid"] == "u3")
    text = edit_result["message"]["content"][0]["content"]
    assert len(text) < 400 and "compacted" in text
    # user/assistant の本文は無変更、末尾は固定
    assert new[-1] == messages[-1]
    assert next(m for m in new if m["uuid"] == "a2")["message"]["content"][0]["text"] == "I found the bug."
    assert new[-4:] == messages[-4:]


def test_tool_input_shrinks_and_anthropic_format_supported():
    plain = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "Grep", "input": {"pattern": "p" * 900, "path": "/"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "match " * 200}]},
        {"role": "assistant", "content": "found it"},
    ]
    jev = Jev(FunctionBackend(lambda state, q: {"keep_call": True, "keep_result_verbatim": True}))
    new, report = compaction.compact_messages(plain, jev, preserve_recent_messages=1)
    assert report.pairs_kept == 1 and report.pairs_dropped == 0
    tool_use = new[1]["content"][0]
    assert len(json.dumps(tool_use["input"])) <= 1000
    assert new[0] == plain[0] and new[-1] == plain[-1]


def test_target_ratio_tightens_thresholds():
    messages = _transcript()
    jev = Jev(FunctionBackend(lambda state, q: {"keep_call": 0.65, "keep_result_verbatim": 0.65}))
    _, loose = compaction.compact_messages(messages, jev, preserve_recent_messages=2)
    _, tight = compaction.compact_messages(messages, jev, preserve_recent_messages=2, target_ratio=0.6)
    assert loose.pairs_kept > tight.pairs_kept
    assert tight.reduction_ratio >= loose.reduction_ratio


def test_main_roundtrip(tmp_path, capsys):
    src = tmp_path / "session.jsonl"
    src.write_text("".join(json.dumps(m) + "\n" for m in _transcript()), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    assert compaction.main(["--input", str(src), "--output", str(out), "--preserve", "2", "--stats", "--backend", "mock"]) == 0
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["uuid"] == "u5"
    assert "reduction_ratio" in capsys.readouterr().err
