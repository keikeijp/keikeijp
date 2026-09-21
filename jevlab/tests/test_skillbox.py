import json
from pathlib import Path

from jevlab.apps import skillbox
from jevlab.apps.skillbox import JsonRpcServer, SkillStore, build_server, parse_front_matter, recommend
from jevlab.core import FunctionBackend, Jev, MockBackend, ScriptedBackend


def _make_skill(root: Path, name: str, description: str, extra: dict[str, str] | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n本文\n", encoding="utf-8")
    for rel, text in (extra or {}).items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return directory


def test_front_matter_and_store_scan(tmp_path):
    meta, body = parse_front_matter("---\nname: x\ndescription: \"y z\"\ntags: [a, b]\n---\nbody here\n")
    assert meta == {"name": "x", "description": "y z", "tags": ["a", "b"]}
    assert body.strip() == "body here"
    assert parse_front_matter("no front matter") == ({}, "no front matter")

    _make_skill(tmp_path, "pdf-fill", "PDF フォームを埋める", {"scripts/fill.py": "print('hi')\n"})
    _make_skill(tmp_path, "git-commit", "良いコミットメッセージを書く")
    (tmp_path / "not-a-skill").mkdir()
    store = SkillStore(tmp_path)
    assert [s.name for s in store.list()] == ["git-commit", "pdf-fill"]
    skill = store.get("pdf-fill")
    assert skill is not None and skill.description == "PDF フォームを埋める"
    assert [p.name for p in skill.files()] == ["SKILL.md", "fill.py"]
    assert store.read_file("pdf-fill", "scripts/fill.py").startswith("print")


def test_store_add_copies_directory(tmp_path):
    source = _make_skill(tmp_path / "src", "new-skill", "新しいスキル")
    store = SkillStore(tmp_path / "root")
    assert store.list() == []
    added = store.add(source)
    assert added.name == "new-skill" and added.path == tmp_path / "root" / "new-skill"
    assert (tmp_path / "root" / "new-skill" / "SKILL.md").is_file()


def test_recommend_with_scripted_backend(tmp_path):
    _make_skill(tmp_path, "pdf-fill", "PDF フォームを埋める")
    _make_skill(tmp_path, "git-commit", "コミットメッセージ")
    store = SkillStore(tmp_path)
    backend = ScriptedBackend([{"applies": False, "usefulness": 0}, {"applies": True, "usefulness": 3}])
    jev = Jev(backend, max_workers=1)  # 並列だと script の順番が崩れるので 1 ワーカー
    result = recommend(jev, "PDF の申請書を埋めたい", store.list(), top_k=3)
    assert [r["name"] for r in result] == ["pdf-fill"]
    assert result[0]["usefulness_label"] == "不可欠" and result[0]["score"] > 0.8
    assert backend.calls[0]["state"]["skill"]["name"] == "git-commit"
    assert recommend(jev, "x", [], top_k=3) == []


def test_jsonrpc_server_in_process(tmp_path):
    _make_skill(tmp_path, "pdf-fill", "PDF フォームを埋める", {"scripts/fill.py": "print('hi')\n"})
    store = SkillStore(tmp_path)
    jev = Jev(FunctionBackend(lambda state, q: {"applies": True, "usefulness": 2}))
    server = build_server(store, jev)

    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "skillbox" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_skills", "get_skill", "recommend_skills"]

    listed = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_skills", "arguments": {}}})["result"]
    assert listed["isError"] is False and json.loads(listed["content"][0]["text"])[0]["name"] == "pdf-fill"

    got = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_skill", "arguments": {"name": "pdf-fill"}}})["result"]
    assert json.loads(got["content"][0]["text"])["files"] == ["SKILL.md", "scripts/fill.py"]

    missing = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_skill", "arguments": {"name": "nope"}}})["result"]
    assert missing["isError"] is True

    rec = server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "recommend_skills", "arguments": {"task": "PDF を埋める"}}})["result"]
    assert json.loads(rec["content"][0]["text"])[0]["name"] == "pdf-fill"

    resources = server.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})["result"]["resources"]
    assert {r["uri"] for r in resources} == {"skill://pdf-fill/SKILL.md", "skill://pdf-fill/scripts/fill.py"}
    read = server.handle({"jsonrpc": "2.0", "id": 8, "method": "resources/read", "params": {"uri": "skill://pdf-fill/scripts/fill.py"}})["result"]
    assert read["contents"][0]["text"].startswith("print")

    unknown = server.handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert unknown["error"]["code"] == skillbox.METHOD_NOT_FOUND
    bad_tool = server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "nope"}})
    assert bad_tool["error"]["code"] == skillbox.INVALID_PARAMS
    assert json.loads(server.handle_line("{not json"))["error"]["code"] == skillbox.PARSE_ERROR
    assert server.handle_line("") is None
    assert json.loads(server.handle_line('{"jsonrpc":"2.0","id":11,"method":"ping"}'))["result"] == {}


def test_serve_stdio_roundtrip(tmp_path):
    import io

    server = JsonRpcServer("t")
    server.add_tool("echo", "echo", lambda args: args.get("x", ""))
    out = io.StringIO()
    server.serve_stdio(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"x":"hi"}}}\n'), out)
    assert json.loads(out.getvalue())["result"]["content"] == [{"type": "text", "text": "hi"}]


def test_main_list_recommend_config(tmp_path, capsys):
    _make_skill(tmp_path, "pdf-fill", "PDF フォームを埋める")
    assert skillbox.main(["--root", str(tmp_path), "list"]) == 0
    assert "pdf-fill" in capsys.readouterr().out
    assert skillbox.main(["--root", str(tmp_path), "--backend", "mock", "recommend", "PDF フォームを埋める", "--json", "--threshold", "0"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "pdf-fill"
    assert skillbox.main(["--root", str(tmp_path), "config"]) == 0
    assert "claude mcp add skillbox" in capsys.readouterr().out
