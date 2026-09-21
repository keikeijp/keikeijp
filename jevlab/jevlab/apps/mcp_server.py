"""11. typesafe-mcp (itsmostafa/typesafe-mcp の再実装): Jev の判断プリミティブを MCP ツールとして公開する。

Claude Code / Codex / Cursor などの MCP クライアントから、
「大きい LLM が自分で考える代わりに Jev に速く安く判定させる」ための入口。

ツール (すべて JSON テキストを返す):
- jev_choice(state, options{label: 説明}, instructions)
- jev_score(state, rubric[], instructions)
- jev_noul(state, instructions)
- jev_decide(state, questions{name: {type, criteria, instructions}})
- jev_rank(candidates[], instructions, rubric?, context?)

    python -m jevlab.apps.mcp_server serve                # stdio
    python -m jevlab.apps.mcp_server --print-config       # claude mcp add ... / JSON / TOML
    python -m jevlab.apps.mcp_server call jev_noul --args '{"state": "今日中に", "instructions": "急ぎか?"}' --backend mock
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from jevlab.apps.skillbox import JsonRpcServer, mcp_config_snippet
from jevlab.core import Choice, Jev, Noul, Score, questions_to_wire

SERVER_NAME = "jev"
MODULE = "jevlab.apps.mcp_server"

_STATE_SCHEMA = {"description": "判定対象の状態。文字列または JSON オブジェクト", "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "array"}]}


def _require_state(args: Mapping[str, Any]) -> Any:
    if "state" not in args:
        raise ValueError("state is required")
    return args["state"]


def build_server(jev: Jev) -> JsonRpcServer:
    server = JsonRpcServer(
        SERVER_NAME,
        instructions="Jev (TypeSafe System One) による高速・低コストな型付き判断。分類は jev_choice、採点は jev_score、はい/いいえは jev_noul、複合は jev_decide、並べ替えは jev_rank。",
    )

    def jev_choice(args: Mapping[str, Any]) -> Any:
        options = args.get("options")
        if isinstance(options, list):
            options = {str(o): None for o in options}
        if not isinstance(options, Mapping) or not options:
            raise ValueError("options must be a non-empty object {label: description}")
        answer = jev.choose(_require_state(args), dict(options), args.get("instructions"))
        return {**answer.to_dict(), "ranked": answer.ranked(), "margin": answer.margin(), "backend": jev.backend.name}

    server.add_tool(
        "jev_choice",
        "state を見て options の中から 1 つ選ぶ (分類 / ルーティング)。確率と confidence を返す",
        jev_choice,
        {
            "type": "object",
            "properties": {
                "state": _STATE_SCHEMA,
                "options": {"type": "object", "description": "{ラベル: 説明 (null 可)}", "additionalProperties": {"type": ["string", "null"]}},
                "instructions": {"type": "string", "description": "何を選ぶかの指示"},
            },
            "required": ["state", "options"],
        },
    )

    def jev_score(args: Mapping[str, Any]) -> Any:
        rubric = args.get("rubric")
        if not isinstance(rubric, list) or not rubric:
            raise ValueError("rubric must be a non-empty ordered list (level 0 .. n-1)")
        answer = jev.score(_require_state(args), rubric, args.get("instructions"))
        return {**answer.to_dict(), "normalized": answer.normalized(), "backend": jev.backend.name}

    server.add_tool(
        "jev_score",
        "順序付きルーブリック (低→高) で state を採点する。期待値 score と最頻 level を返す",
        jev_score,
        {
            "type": "object",
            "properties": {"state": _STATE_SCHEMA, "rubric": {"type": "array", "items": {"type": "string"}, "description": "低い順のレベル説明"}, "instructions": {"type": "string"}},
            "required": ["state", "rubric"],
        },
    )

    def jev_noul(args: Mapping[str, Any]) -> Any:
        instructions = args.get("instructions")
        if not instructions:
            raise ValueError("instructions is required")
        answer = jev.judge(_require_state(args), instructions, args.get("criteria"))
        return {**answer.to_dict(), "yes": answer.yes, "backend": jev.backend.name}

    server.add_tool(
        "jev_noul",
        "はい/いいえ の質問に 0..1 の確率で答える",
        jev_noul,
        {
            "type": "object",
            "properties": {"state": _STATE_SCHEMA, "instructions": {"type": "string"}, "criteria": {"type": "object", "description": "任意: {true: 説明, false: 説明}"}},
            "required": ["state", "instructions"],
        },
    )

    def jev_decide(args: Mapping[str, Any]) -> Any:
        questions = args.get("questions")
        if not isinstance(questions, Mapping) or not questions:
            raise ValueError("questions must be a non-empty object {name: {type, criteria, instructions}}")
        wire = questions_to_wire(dict(questions))  # 形式検証もここで行う
        decision = jev.decide(_require_state(args), wire)
        return decision.to_dict()

    server.add_tool(
        "jev_decide",
        "複数の質問 (choice / score / noul) を 1 回の呼び出しで同時に判定する",
        jev_decide,
        {
            "type": "object",
            "properties": {
                "state": _STATE_SCHEMA,
                "questions": {
                    "type": "object",
                    "description": '{name: {"type": "choice", "criteria": {label: desc}} | {"type": "score", "criteria": [..]} | {"type": "noul", "instructions": ".."}}',
                    "additionalProperties": {"type": "object"},
                },
            },
            "required": ["state", "questions"],
        },
    )

    def jev_rank(args: Mapping[str, Any]) -> Any:
        candidates = args.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("candidates must be a non-empty list")
        instructions = args.get("instructions")
        if not instructions:
            raise ValueError("instructions is required")
        ranked = jev.rank(candidates, instructions, rubric=args.get("rubric"), context=args.get("context"))
        return {"ranked": [{"candidate": c, "score": round(s, 4)} for c, s in ranked], "backend": jev.backend.name}

    server.add_tool(
        "jev_rank",
        "候補を instructions に従って採点し降順に並べる (並列評価)",
        jev_rank,
        {
            "type": "object",
            "properties": {
                "candidates": {"type": "array", "items": {}},
                "instructions": {"type": "string"},
                "rubric": {"type": "array", "items": {"type": "string"}, "description": "任意: 採点ルーブリック (既定 5 段階)"},
                "context": {"description": "任意: 全候補に共通する文脈 (質問文など)"},
            },
            "required": ["candidates", "instructions"],
        },
    )
    return server


def print_config(backend: str | None = None) -> str:
    env = {"JEV_BACKEND": backend} if backend else {"TYPESAFE_API_KEY": "<your key>"}
    return mcp_config_snippet(SERVER_NAME, MODULE, ["serve"], env)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    parser.add_argument("--print-config", action="store_true", help="MCP クライアント設定スニペットを表示して終了")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="stdio で MCP サーバを起動 (既定)")
    p_call = sub.add_parser("call", help="ツールを 1 回だけ直接呼ぶ (動作確認用)")
    p_call.add_argument("tool")
    p_call.add_argument("--args", default="{}", help="JSON の引数")
    sub.add_parser("list-tools", help="ツール定義を JSON で表示")
    args = parser.parse_args(argv)

    if args.print_config:
        print(print_config(args.backend))
        return 0
    jev = Jev(args.backend)
    server = build_server(jev)
    if args.command == "list-tools":
        print(json.dumps(server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), ensure_ascii=False, indent=2))
        return 0
    if args.command == "call":
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": args.tool, "arguments": json.loads(args.args)}})
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response and "error" not in response and not response["result"].get("isError") else 1
    return server.serve_stdio()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
