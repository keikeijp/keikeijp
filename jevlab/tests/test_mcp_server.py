import json

from jevlab.apps import mcp_server
from jevlab.apps.mcp_server import build_server, print_config
from jevlab.core import Jev, MockBackend


def _call(server, name, arguments):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"]) if not result["isError"] else result["content"][0]["text"]


def test_tools_list_exposes_five_jev_tools():
    server = build_server(Jev(MockBackend()))
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["jev_choice", "jev_score", "jev_noul", "jev_decide", "jev_rank"]
    assert all("inputSchema" in t and t["inputSchema"]["type"] == "object" for t in tools)
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})["result"]["serverInfo"]["name"] == "jev"


def test_each_tool_via_in_process_dispatch():
    server = build_server(Jev(MockBackend(hints={"answer": "refund"})))
    err, out = _call(server, "jev_choice", {"state": "二重に課金された、返金して", "options": {"refund": "返金", "bug": "不具合"}, "instructions": "主な要求は?"})
    assert not err and out["choice"] == "refund" and out["ranked"][0][0] == "refund"

    server = build_server(Jev(MockBackend()))
    err, out = _call(server, "jev_score", {"state": "the customer is very angry", "rubric": ["calm", "concerned", "very angry"]})
    assert not err and out["level"] == 2 and 0 <= out["normalized"] <= 1

    err, out = _call(server, "jev_noul", {"state": "deadline is today, urgent", "instructions": "Does the text mention urgency or a deadline?"})
    assert not err and 0 <= out["noul"] <= 1 and isinstance(out["yes"], bool)

    err, out = _call(
        server,
        "jev_decide",
        {"state": "refund me today", "questions": {"intent": {"type": "choice", "criteria": {"refund": "返金", "bug": None}}, "urgent": {"type": "noul", "instructions": "urgent?"}, "level": {"type": "score", "criteria": ["low", "high"]}}},
    )
    assert not err and set(out["answers"]) == {"intent", "urgent", "level"} and out["answers"]["intent"]["type"] == "choice"

    err, out = _call(server, "jev_rank", {"candidates": ["python tutorial", "cooking recipe"], "instructions": "How relevant is the candidate to the query?", "context": "learn python"})
    assert not err and out["ranked"][0]["candidate"] == "python tutorial"


def test_tool_errors_are_reported_not_raised():
    server = build_server(Jev(MockBackend()))
    err, text = _call(server, "jev_choice", {"state": "x", "options": {}})
    assert err and "options" in text
    err, text = _call(server, "jev_decide", {"state": "x", "questions": {"q": {"type": "banana"}}})
    assert err and "banana" in text
    err, text = _call(server, "jev_noul", {"instructions": "?"})
    assert err and "state" in text


def test_print_config_and_main(capsys):
    text = print_config("mock")
    assert "claude mcp add jev" in text and "jevlab.apps.mcp_server" in text and '"mcpServers"' in text and "[mcp_servers.jev]" in text
    assert mcp_server.main(["--print-config"]) == 0
    assert "claude mcp add" in capsys.readouterr().out
    assert mcp_server.main(["--backend", "mock", "call", "jev_noul", "--args", json.dumps({"state": "urgent today", "instructions": "urgent?"})]) == 0
    assert "noul" in json.loads(capsys.readouterr().out)["result"]["content"][0]["text"]
    assert mcp_server.main(["--backend", "mock", "list-tools"]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]["tools"]) == 5
