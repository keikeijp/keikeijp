"""8. jev-router: ターン毎に Jev がタスクの種類と難易度を判定し、モデルを振り分ける。

元ネタ: gargpratyush/jev-router

- `route(jev, messages, tools, policy)` : state = {last_user_message, n_turns, has_tools, code_blocks_count, files_mentioned}
    task_kind   Choice {trivial_chat, code_edit_small, code_edit_large, debugging, architecture, research, long_context}
    difficulty  Score  [trivial, easy, moderate, hard, expert]
    needs_vision Noul
  → ポリシー表 (JSON / YAML、既定は下記) でモデル名に写す
- HTTP プロキシ: Anthropic 互換 `/v1/messages` と OpenAI 互換 `/v1/chat/completions` を受け、
  `model` を書き換えて上流 (ANTHROPIC_BASE_URL / OPENAI_BASE_URL) に urllib で転送し、ストリームをそのまま返す
- `--print-only "prompt"` : ルーティング結果だけ表示 (ネットワーク不要)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jevlab.core import Choice, Jev, Noul, Score

TASK_KINDS = {
    "trivial_chat": "greeting, thanks, yes/no, a one-line factual question",
    "code_edit_small": "a small, well-specified change to one place in the code",
    "code_edit_large": "a feature or refactor touching several files",
    "debugging": "finding the cause of a failure, error message, or wrong behavior",
    "architecture": "design decisions, trade-offs, system structure, planning",
    "research": "exploring a codebase or documentation to answer a question",
    "long_context": "summarizing or reasoning over a very long document or transcript",
}
DIFFICULTY = ["trivial", "easy", "moderate", "hard", "expert"]
QUESTIONS = {
    "task_kind": Choice(TASK_KINDS, "What kind of task is the user's latest message asking for?"),
    "difficulty": Score(["trivial: no thinking needed", "easy: routine", "moderate: needs some reasoning", "hard: multi-step reasoning, subtle", "expert: deep expertise, high stakes"], "How difficult is the request for an AI coding assistant?"),
    "needs_vision": Noul("Does answering require looking at an image, screenshot, or diagram?"),
}

DEFAULT_POLICY: dict[str, Any] = {
    "models": {
        "trivial": "claude-haiku-4-5-20251001",
        "easy": "claude-haiku-4-5-20251001",
        "moderate": "claude-sonnet-5",
        "hard": "claude-opus-5",
        "expert": "claude-opus-5",
    },
    "task_overrides": {"architecture": "claude-opus-5", "long_context": "claude-sonnet-5"},
    "vision_model": "claude-sonnet-5",
    "fallback": "claude-sonnet-5",
    "min_confidence": 0.35,
    "respect_client_model": False,  # True ならクライアントが既に指定したモデルを尊重して書き換えない
}

FILE_RE = re.compile(r"(?<![\w/])(?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|md|json|yaml|yml|toml|sql|sh|c|cc|cpp|h|cs)\b")
CODE_BLOCK_RE = re.compile(r"```")


# ---------------------------------------------------------------------------
# ポリシー
# ---------------------------------------------------------------------------


def load_policy(path: str | None) -> dict[str, Any]:
    policy = copy.deepcopy(DEFAULT_POLICY)
    if not path:
        return policy
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as error:  # pragma: no cover
            raise SystemExit("YAML ポリシーには pyyaml が必要です: pip install pyyaml (または JSON で書いてください)") from error
        loaded = yaml.safe_load(text) or {}
    else:
        loaded = json.loads(text)
    return merge_policy(policy, loaded)


def merge_policy(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# メッセージの正規化 (Anthropic / OpenAI 両形式)
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in {"text", "input_text"}:
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(_text_of(block.get("content")))
        return "\n".join(parts)
    return ""


def _has_image(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in {"image", "image_url", "input_image"} for b in content)


def build_state(messages: Sequence[Mapping[str, Any]], tools: Sequence[Any] | None = None, max_chars: int = 2000) -> dict[str, Any]:
    user_messages = [m for m in messages if m.get("role") == "user"]
    last = user_messages[-1] if user_messages else (messages[-1] if messages else {})
    last_text = _text_of(last.get("content"))
    all_text = "\n".join(_text_of(m.get("content")) for m in messages)
    files = sorted(set(FILE_RE.findall(all_text)))[:20]
    return {
        "last_user_message": last_text[-max_chars:] if len(last_text) > max_chars else last_text,
        "n_turns": len(messages),
        "has_tools": bool(tools),
        "code_blocks_count": len(CODE_BLOCK_RE.findall(all_text)) // 2,
        "files_mentioned": files,
        "has_images": any(_has_image(m.get("content")) for m in messages),
        "total_chars": len(all_text),
    }


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------


@dataclass
class RoutingDecision:
    model: str
    task_kind: str
    difficulty: str
    difficulty_level: int
    difficulty_score: float
    needs_vision: bool
    confidence: float
    reason: str
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route(jev: Jev, messages: Sequence[Mapping[str, Any]], tools: Sequence[Any] | None = None, policy: Mapping[str, Any] | None = None) -> RoutingDecision:
    policy = merge_policy(DEFAULT_POLICY, policy or {})
    state = build_state(messages, tools)
    if state["total_chars"] > 60_000:
        state["hint"] = "conversation is very long"
    decision = jev.decide(state, QUESTIONS)
    kind = decision.choice("task_kind")
    difficulty = decision.score("difficulty")
    vision = decision.noul("needs_vision").yes or state["has_images"]
    level = difficulty.level
    label = DIFFICULTY[min(level, len(DIFFICULTY) - 1)]
    confidence = min(kind.confidence, difficulty.confidence)
    if confidence < float(policy.get("min_confidence", 0.0)):
        model, reason = str(policy["fallback"]), f"low confidence ({confidence:.2f}) → fallback"
    elif vision and policy.get("vision_model"):
        model, reason = str(policy["vision_model"]), "needs vision"
    elif kind.choice in policy.get("task_overrides", {}):
        model, reason = str(policy["task_overrides"][kind.choice]), f"task override for {kind.choice}"
    else:
        model = str(policy["models"].get(label, policy["fallback"]))
        reason = f"difficulty {label}"
    return RoutingDecision(model, kind.choice, label, level, difficulty.score, vision, round(confidence, 3), reason, state)


def _openai_messages_to_common(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI の messages を build_state が読める形に (role と content だけ使う)。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role in {"user", "assistant"}:
            out.append({"role": role, "content": m.get("content")})
    return out


def rewrite_request(body: Mapping[str, Any], jev: Jev, policy: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], RoutingDecision]:
    """/v1/messages または /v1/chat/completions のリクエスト本文を受け取り、model を書き換えた本文と判定を返す。"""
    policy = merge_policy(DEFAULT_POLICY, policy or {})
    messages = body.get("messages") or []
    if "system" in body and isinstance(body.get("system"), str):
        pass  # system は判定に使わない
    if messages and messages[0].get("role") == "system":  # OpenAI 形式
        messages = _openai_messages_to_common(messages[1:])
    else:
        messages = _openai_messages_to_common(messages)
    decision = route(jev, messages, body.get("tools"), policy)
    new_body = dict(body)
    original = body.get("model")
    if policy.get("respect_client_model") and original and original not in {"auto", "jev-router", "router"}:
        decision.reason += f" (kept client model {original})"
        decision.model = str(original)
    new_body["model"] = decision.model
    return new_body, decision


# ---------------------------------------------------------------------------
# HTTP プロキシ
# ---------------------------------------------------------------------------

FORWARD_HEADERS = {"authorization", "x-api-key", "anthropic-version", "anthropic-beta", "content-type", "accept", "openai-organization", "openai-beta"}


def upstream_for(path: str, anthropic_base: str | None = None, openai_base: str | None = None) -> str:
    if path.startswith("/v1/messages"):
        base = anthropic_base or os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com"
    else:
        base = openai_base or os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com"
    return base.rstrip("/") + path


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], jev: Jev, policy: Mapping[str, Any], anthropic_base: str | None = None, openai_base: str | None = None, log: bool = True):
        super().__init__(address, RouterHandler)
        self.jev = jev
        self.policy = dict(policy)
        self.anthropic_base = anthropic_base
        self.openai_base = openai_base
        self.log_enabled = log
        self.decisions: list[dict[str, Any]] = []


class RouterHandler(BaseHTTPRequestHandler):
    server: RouterServer

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.log_enabled:
            sys.stderr.write(f"[jev-router] {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/health", "/"}:
            self._json(200, {"ok": True, "decisions": len(self.server.decisions), "jev": self.server.jev.stats()})
        elif self.path == "/decisions":
            self._json(200, self.server.decisions[-100:])
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if not (self.path.startswith("/v1/messages") or self.path.startswith("/v1/chat/completions")):
            self._json(404, {"error": "unsupported path; use /v1/messages or /v1/chat/completions"})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
            new_body, decision = rewrite_request(body, self.server.jev, self.server.policy)
        except (ValueError, KeyError) as error:
            self._json(400, {"error": f"bad request: {error}"})
            return
        record = {"path": self.path, "model": decision.model, "task_kind": decision.task_kind, "difficulty": decision.difficulty, "reason": decision.reason}
        self.server.decisions.append(record)
        self.log_message("route %s → %s (%s, %s)", self.path, decision.model, decision.task_kind, decision.difficulty)
        self._forward(new_body, decision)

    def _forward(self, body: dict[str, Any], decision: RoutingDecision) -> None:
        url = upstream_for(self.path, self.server.anthropic_base, self.server.openai_base)
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        for key, value in self.headers.items():
            if key.lower() in FORWARD_HEADERS:
                request.add_header(key, value)
        request.add_header("Content-Type", "application/json")
        try:
            response = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Jev-Router-Model", decision.model)
            self.end_headers()
            self.wfile.write(payload)
            return
        except (urllib.error.URLError, OSError) as error:
            self._json(502, {"error": f"upstream unreachable: {error}"})
            return
        with response:
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in {"content-type", "cache-control", "x-request-id", "request-id"}:
                    self.send_header(key, value)
            self.send_header("X-Jev-Router-Model", decision.model)
            self.send_header("X-Jev-Router-Task", decision.task_kind)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab router", description="Jev でターン毎にモデルを振り分けるプロキシ")
    parser.add_argument("--print-only", metavar="PROMPT", help="このプロンプトに対する判定だけ表示して終了")
    parser.add_argument("--policy", default=None, help="ポリシー JSON / YAML")
    parser.add_argument("--serve", action="store_true", help="HTTP プロキシを起動")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--anthropic-base", default=None, help="上流 (既定: $ANTHROPIC_BASE_URL or https://api.anthropic.com)")
    parser.add_argument("--openai-base", default=None, help="上流 (既定: $OPENAI_BASE_URL or https://api.openai.com)")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    policy = load_policy(args.policy)
    jev = Jev(args.backend, cache=True)
    if args.print_only is not None:
        decision = route(jev, [{"role": "user", "content": args.print_only}], None, policy)
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.serve:
        server = RouterServer((args.host, args.port), jev, policy, args.anthropic_base, args.openai_base)
        print(f"jev-router listening on http://{args.host}:{args.port}/ (ANTHROPIC_BASE_URL / OPENAI_BASE_URL をここに向ける)", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    parser.error("--print-only PROMPT か --serve を指定してください")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
