"""19 検索・天気・タスク管理を 1 つのチャットから呼ぶ (ツールルーター)。

Jev が呼ぶツールと、候補が決まっている引数を選ぶ。自由入力の引数は生成できないので、
発話全体を渡すか、別の生成モデルに任せる。ツールの実行そのものは行わない。
"""

from __future__ import annotations

from typing import Any

from ..client import JevClient, Result
from ..questions import choice, noul
from .base import Outcome, UseCase, band

DEFAULT_TOOLS: list[dict[str, Any]] = [
    {"name": "web_search", "description": "Search the web for current information.", "args": {"query": {"type": "text"}}},
    {"name": "wikipedia", "description": "Look up an encyclopedia article about a person, place, or concept.", "args": {"title": {"type": "text"}}},
    {"name": "weather", "description": "Current weather or forecast for a city.", "args": {"city": {"type": "text"}, "when": {"type": "enum", "candidates": {"now": "right now", "today": "the rest of today", "tomorrow": "tomorrow", "week": "the coming week"}}}},
    {"name": "todo_add", "description": "Add a task to the user's to-do list.", "args": {"text": {"type": "text"}, "priority": {"type": "enum", "candidates": {"low": "no rush", "normal": "default", "high": "urgent or with a deadline"}}}},
    {"name": "home_light", "description": "Turn a smart light on or off.", "args": {"room": {"type": "enum", "candidates": {"living_room": None, "bedroom": None, "kitchen": None, "studio": None}}, "action": {"type": "enum", "candidates": {"on": None, "off": None}}}},
    {"name": "none", "description": "No tool is needed; answer directly or ask for clarification.", "args": {}},
]


class RouteUseCase(UseCase):
    name = "route"
    title = "ツールルーター"
    summary = "発話からツールと候補付き引数を選ぶ。自由入力の引数は生成しない"
    article_ref = "19 Coding Garden のチャットボット"
    label_field = "tool"

    def __init__(self, tools: list[dict[str, Any]] | None = None, *, act_threshold: float = 0.7):
        self.tools = tools or DEFAULT_TOOLS
        self.act = act_threshold

    def from_text(self, text: str) -> Any:
        return {"message": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        tools = item.get("tools", self.tools)
        # ツールの説明は choice の criteria に入るので state には重複させない
        state: dict[str, Any] = {"user_message": item["message"]}
        if item.get("history"):
            state["recent_conversation"] = item["history"][-6:]
        questions = {
            "tool": choice("Which tool should handle the user's message?", {t["name"]: t["description"] for t in tools}),
            "needs_clarification": noul("Is the message too ambiguous to act on without asking the user a question first?"),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        tool = result.choice("tool")
        clarify = result.noul("needs_clarification")
        return {
            "tool": tool.choice,
            "confidence": round(tool.confidence, 3),
            "band": band(tool.confidence, act=self.act, review=0.45),
            "needs_clarification": clarify >= 0.5,
            "args": {},
            "needs_human": clarify >= 0.5 or tool.confidence < self.act,
        }

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        state, questions = self.build(item)
        result = client.ask(state, questions)
        decision = self.decide(item, result)
        results = [result]
        tools = {t["name"]: t for t in item.get("tools", self.tools)}
        spec = tools.get(decision["tool"], {})
        enum_args = {name: a for name, a in spec.get("args", {}).items() if a.get("type") == "enum"}
        text_args = [name for name, a in spec.get("args", {}).items() if a.get("type") == "text"]
        if enum_args and decision["tool"] != "none":
            arg_questions = {name: choice(f"Which value of '{name}' does the user mean for tool {decision['tool']}?", a["candidates"]) for name, a in enum_args.items()}
            arg_result = client.ask({"user_message": item["message"], "tool": decision["tool"]}, arg_questions)
            results.append(arg_result)
            for name in enum_args:
                ans = arg_result.choice(name)
                decision["args"][name] = ans.choice
                decision.setdefault("arg_confidence", {})[name] = round(ans.confidence, 3)
        for name in text_args:
            decision["args"][name] = item["message"]  # 自由入力は生成できないので発話を渡す
        decision["free_text_args"] = text_args
        return Outcome(input=item, decision=decision, results=results)

    def example_items(self) -> list[Any]:
        return [
            {"message": "What's the weather in Osaka tomorrow?"},
            {"message": "Remind me to send the invoice, it's urgent"},
            {"message": "Turn off the studio lights"},
            {"message": "Who was Ryuichi Sakamoto?"},
            {"message": "hmm"},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"{outcome.input['message']!s:45.45} -> {d['tool']:12} ({d['confidence']:.2f}, {d['band']}) args={d['args']}{' CLARIFY' if d['needs_clarification'] else ''}"
