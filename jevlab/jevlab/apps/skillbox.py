"""10. skillbox (kitze/skillbox の再実装): エージェント用スキルの管理 + MCP 配信 + Jev によるスキル推薦。

スキル = `SKILL.md` (front matter に name / description) を含むディレクトリ。

    skills/
      git-commit/SKILL.md
      pdf-fill/SKILL.md
      pdf-fill/scripts/fill.py

- `SkillStore(root)`          : root 直下のスキルを走査 / 取得 / 追加
- `recommend(jev, task, ...)` : スキルごとに `applies` (Noul) + `usefulness` (Score) を並列判定して上位を返す
- `JsonRpcServer`             : 依存ゼロの最小 MCP (JSON-RPC 2.0 / stdio) サーバ。`mcp_server.py` からも再利用する
- `build_server(store, jev)`  : list_skills / get_skill / recommend_skills ツールと skill:// リソースを公開

    python -m jevlab.apps.skillbox list --root ./skills
    python -m jevlab.apps.skillbox recommend "PDF のフォームを埋める" --root ./skills --backend mock
    python -m jevlab.apps.skillbox serve --root ./skills     # MCP stdio
    python -m jevlab.apps.skillbox add ./my-skill --root ./skills
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from jevlab.core import Jev, Noul, Score

DEFAULT_ROOT = os.environ.get("SKILLBOX_ROOT") or os.path.join(os.path.expanduser("~"), ".skillbox", "skills")
USEFULNESS = ["役に立たない", "少し役立つ", "役立つ", "不可欠"]

# ---------------------------------------------------------------------------
# スキル
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def files(self) -> list[Path]:
        """スキルディレクトリ配下のテキストファイル (相対パス順)。"""
        return sorted(p for p in self.path.rglob("*") if p.is_file() and not p.name.startswith("."))

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": str(self.path)}


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """`---\\nkey: value\\n---\\n本文` を (meta, body) に分ける。YAML 依存なしの最小実装 (スカラーとフラットなリストのみ)。"""
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith(" ") or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("\"'")
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value
    return meta, match.group(2)


def load_skill(directory: Path) -> Skill | None:
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        return None
    meta, body = parse_front_matter(skill_md.read_text(encoding="utf-8"))
    name = str(meta.get("name") or directory.name)
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    description = str(meta.get("description") or first_line)
    return Skill(name=name, description=description, path=directory, body=body, meta=meta)


class SkillStore:
    """root 直下の各ディレクトリを 1 スキルとして走査する。"""

    def __init__(self, root: str | os.PathLike[str] = DEFAULT_ROOT):
        self.root = Path(root)
        self._skills: dict[str, Skill] = {}
        self.scan()

    def scan(self) -> list[Skill]:
        self._skills = {}
        if self.root.is_dir():
            for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
                skill = load_skill(directory)
                if skill is not None:
                    self._skills[skill.name] = skill
        return self.list()

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def add(self, source: str | os.PathLike[str]) -> Skill:
        """スキルディレクトリ (SKILL.md を含む) を root にコピーして登録する。"""
        source_path = Path(source)
        skill = load_skill(source_path)
        if skill is None:
            raise ValueError(f"{source_path} に SKILL.md がありません")
        target = self.root / source_path.name
        if target.resolve() != source_path.resolve():
            self.root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source_path, target)
        self.scan()
        return self._skills[skill.name]

    def read_file(self, name: str, relative: str) -> str:
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"スキル {name!r} はありません")
        target = (skill.path / relative).resolve()
        if skill.path.resolve() not in target.parents and target != skill.path.resolve():
            raise PermissionError("スキルディレクトリの外は読めません")
        return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 推薦
# ---------------------------------------------------------------------------


def recommend(jev: Jev, task: str, skills: list[Skill], top_k: int = 3, threshold: float = 0.5) -> list[dict[str, Any]]:
    """各スキルについて「このタスクに適用できるか」(Noul) と「どれくらい役立つか」(Score) を並列判定。

    applies >= threshold のものだけを、applies x usefulness(0..1) の降順で top_k 件返す。
    """
    if not skills:
        return []
    questions = {
        "applies": Noul("このスキルはタスクの遂行に適用できるか?"),
        "usefulness": Score(USEFULNESS, "このスキルはタスクにどれくらい役立つか?"),
    }
    states = [{"task": task, "skill": {"name": s.name, "description": s.description}} for s in skills]
    decisions = jev.decide_many((state, questions) for state in states)
    ranked = []
    for skill, decision in zip(skills, decisions):
        applies = decision.noul("applies").noul
        usefulness = decision.score("usefulness")
        ranked.append(
            {
                "name": skill.name,
                "description": skill.description,
                "applies": round(applies, 3),
                "usefulness": round(usefulness.score, 3),
                "usefulness_label": usefulness.legend.get(usefulness.level, ""),
                "score": round(applies * usefulness.normalized(), 4),
            }
        )
    ranked = [r for r in ranked if r["applies"] >= threshold]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# 最小 MCP サーバ (JSON-RPC 2.0, stdio)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

ToolHandler = Callable[[Mapping[str, Any]], Any]


class JsonRpcServer:
    """MCP の最小サブセット: initialize / ping / tools/list / tools/call / resources/list / resources/read。

    `handle(message)` は 1 メッセージを処理して応答 dict (通知なら None) を返すので、stdio なしにテストできる。
    ツールの戻り値は str / dict / list のどれでもよく、JSON テキストの content に包む。
    """

    def __init__(self, name: str, version: str = "0.1.0", instructions: str | None = None):
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: dict[str, tuple[dict[str, Any], ToolHandler]] = {}
        self._resources: Callable[[], list[dict[str, Any]]] | None = None
        self._resource_reader: Callable[[str], dict[str, Any]] | None = None
        self._methods: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": lambda params: {"tools": [spec for spec, _ in self._tools.values()]},
            "tools/call": self._tools_call,
            "resources/list": lambda params: {"resources": self._resources() if self._resources else []},
            "resources/read": self._resources_read,
        }

    # -- 登録 --
    def tool(self, name: str, description: str, input_schema: Mapping[str, Any] | None = None) -> Callable[[ToolHandler], ToolHandler]:
        def register(fn: ToolHandler) -> ToolHandler:
            self.add_tool(name, description, fn, input_schema)
            return fn

        return register

    def add_tool(self, name: str, description: str, fn: ToolHandler, input_schema: Mapping[str, Any] | None = None) -> None:
        schema = dict(input_schema or {"type": "object", "properties": {}})
        self._tools[name] = ({"name": name, "description": description, "inputSchema": schema}, fn)

    def set_resources(self, lister: Callable[[], list[dict[str, Any]]], reader: Callable[[str], dict[str, Any]]) -> None:
        self._resources, self._resource_reader = lister, reader

    def add_method(self, method: str, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        self._methods[method] = fn

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    # -- 処理 --
    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _tools_call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in self._tools:
            raise _RpcError(INVALID_PARAMS, f"unknown tool: {name}")
        _, fn = self._tools[name]
        try:
            result = fn(params.get("arguments") or {})
        except (ValueError, TypeError, KeyError, LookupError, PermissionError, OSError) as error:
            return {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}], "isError": True}
        return {"content": as_content(result), "isError": False}

    def _resources_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._resource_reader is None:
            raise _RpcError(METHOD_NOT_FOUND, "resources are not supported")
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise _RpcError(INVALID_PARAMS, "uri is required")
        try:
            return {"contents": [self._resource_reader(uri)]}
        except (KeyError, FileNotFoundError, PermissionError) as error:
            raise _RpcError(INVALID_PARAMS, f"resource not found: {uri} ({error})") from error

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """1 リクエストを処理。通知 (id なし) には None を返す。"""
        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return None if msg_id is None else _error(msg_id, INVALID_REQUEST, "method is required")
        if method.startswith("notifications/"):
            return None
        fn = self._methods.get(method)
        if fn is None:
            return None if msg_id is None else _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
        try:
            result = fn(message.get("params") or {})
        except _RpcError as error:
            return _error(msg_id, error.code, str(error))
        except Exception as error:  # noqa: BLE001 - サーバを落とさない
            return _error(msg_id, INTERNAL_ERROR, f"{type(error).__name__}: {error}")
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def handle_line(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            return json.dumps(_error(None, PARSE_ERROR, f"parse error: {error}"), ensure_ascii=False)
        if isinstance(message, list):  # バッチ
            responses = [r for r in (self.handle(m) for m in message if isinstance(m, Mapping)) if r is not None]
            return json.dumps(responses, ensure_ascii=False) if responses else None
        if not isinstance(message, Mapping):
            return json.dumps(_error(None, INVALID_REQUEST, "invalid request"), ensure_ascii=False)
        response = self.handle(message)
        return None if response is None else json.dumps(response, ensure_ascii=False)

    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """改行区切り JSON-RPC を stdin から読み stdout に返す (MCP stdio トランスポート)。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            response = self.handle_line(line)
            if response is not None:
                stdout.write(response + "\n")
                stdout.flush()
        return 0


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def as_content(result: Any) -> list[dict[str, Any]]:
    """ツールの戻り値を MCP の content 配列にする。"""
    if isinstance(result, list) and all(isinstance(item, Mapping) and "type" in item for item in result):
        return [dict(item) for item in result]
    if isinstance(result, str):
        return [{"type": "text", "text": result}]
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2, default=str)}]


def mcp_config_snippet(server_name: str, module: str, extra_args: list[str] | None = None, env: Mapping[str, str] | None = None) -> str:
    """Claude Code (`claude mcp add`) と JSON 設定 (Claude Desktop / Codex) のスニペットを返す。"""
    args = ["-m", module, *(extra_args or [])]
    env = dict(env or {})
    env_flags = " ".join(f"-e {key}={value}" for key, value in env.items())
    lines = [
        "# Claude Code:",
        f"claude mcp add {server_name} {env_flags + ' ' if env_flags else ''}-- {sys.executable} {' '.join(args)}",
        "",
        "# Claude Desktop / Cursor (mcpServers JSON):",
        json.dumps({"mcpServers": {server_name: {"command": sys.executable, "args": args, "env": env}}}, indent=2),
        "",
        "# Codex (~/.codex/config.toml):",
        f"[mcp_servers.{server_name}]",
        f'command = "{sys.executable}"',
        "args = " + json.dumps(args),
    ]
    if env:
        lines.append(f"[mcp_servers.{server_name}.env]")
        lines.extend(f'{key} = "{value}"' for key, value in env.items())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# skillbox の MCP サーバ
# ---------------------------------------------------------------------------


def build_server(store: SkillStore, jev: Jev) -> JsonRpcServer:
    server = JsonRpcServer("skillbox", instructions="スキル (SKILL.md) の一覧・取得・タスクに合うスキルの推薦。")

    server.add_tool(
        "list_skills",
        "登録済みスキルの名前と説明を一覧する",
        lambda args: [s.summary() for s in store.scan()],
    )

    def get_skill(args: Mapping[str, Any]) -> Any:
        skill = store.get(str(args.get("name", "")))
        if skill is None:
            raise KeyError(f"スキル {args.get('name')!r} はありません")
        return {**skill.summary(), "meta": skill.meta, "body": skill.body, "files": [str(p.relative_to(skill.path)) for p in skill.files()]}

    server.add_tool("get_skill", "スキルの SKILL.md 本文と同梱ファイル一覧を返す", get_skill, {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})

    def recommend_skills(args: Mapping[str, Any]) -> Any:
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task is required")
        return recommend(jev, task, store.scan(), top_k=int(args.get("top_k", 3)), threshold=float(args.get("threshold", 0.5)))

    server.add_tool(
        "recommend_skills",
        "タスク説明に対して Jev が役立つスキルを推薦する",
        recommend_skills,
        {"type": "object", "properties": {"task": {"type": "string"}, "top_k": {"type": "integer", "default": 3}, "threshold": {"type": "number", "default": 0.5}}, "required": ["task"]},
    )

    def list_resources() -> list[dict[str, Any]]:
        resources = []
        for skill in store.scan():
            for file in skill.files():
                rel = file.relative_to(skill.path).as_posix()
                resources.append({"uri": f"skill://{skill.name}/{rel}", "name": f"{skill.name}/{rel}", "description": skill.description, "mimeType": "text/markdown" if file.suffix == ".md" else "text/plain"})
        return resources

    def read_resource(uri: str) -> dict[str, Any]:
        if not uri.startswith("skill://"):
            raise KeyError(uri)
        name, _, rel = uri[len("skill://") :].partition("/")
        return {"uri": uri, "mimeType": "text/markdown" if rel.endswith(".md") else "text/plain", "text": store.read_file(name, rel)}

    server.set_resources(list_resources, read_resource)
    return server


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab skillbox", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help="スキルのルートディレクトリ (SKILLBOX_ROOT)")
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="スキル一覧")
    p_rec = sub.add_parser("recommend", help="タスクに合うスキルを推薦")
    p_rec.add_argument("task")
    p_rec.add_argument("--top", type=int, default=3)
    p_rec.add_argument("--threshold", type=float, default=0.5)
    p_rec.add_argument("--json", action="store_true")
    sub.add_parser("serve", help="MCP サーバを stdio で起動")
    p_add = sub.add_parser("add", help="スキルディレクトリを root にコピー")
    p_add.add_argument("path")
    sub.add_parser("config", help="MCP クライアント設定スニペットを表示")
    args = parser.parse_args(argv)

    store = SkillStore(args.root)
    if args.command == "list":
        for skill in store.list():
            print(f"{skill.name:24} {skill.description}")
        if not store.list():
            print(f"(スキルなし: {store.root})")
        return 0
    if args.command == "add":
        skill = store.add(args.path)
        print(f"added {skill.name} -> {skill.path}")
        return 0
    if args.command == "config":
        print(mcp_config_snippet("skillbox", "jevlab.apps.skillbox", ["serve", "--root", str(store.root)]))
        return 0
    jev = Jev(args.backend)
    if args.command == "recommend":
        result = recommend(jev, args.task, store.list(), top_k=args.top, threshold=args.threshold)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for item in result:
                print(f"{item['score']:.2f}  {item['name']:24} applies={item['applies']:.2f} {item['usefulness_label']}  {item['description']}")
            if not result:
                print("(該当スキルなし)")
        return 0
    if args.command == "serve":
        return build_server(store, jev).serve_stdio()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
