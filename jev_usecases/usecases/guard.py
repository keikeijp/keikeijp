"""13 コマンドの実行前チェック (Vercel fx の安全レビュアー相当)。

AI エージェントが選んだシェルコマンドを実行する前に、破壊的かどうか・種類・危険度を判定し、
allow / ask / block を返す。判定は補助であり、実行環境側のサンドボックスや権限の代わりにはならない。
"""

from __future__ import annotations

from typing import Any

from ..client import Result
from ..questions import choice, noul, score
from .base import Outcome, UseCase

CATEGORIES = {
    "read_only": "Only reads: ls, cat, grep, git status, git log, git diff, curl GET to a public URL.",
    "build_or_test": "Compiles, lints, or runs tests: npm test, pytest, cargo build, make.",
    "mutating_local": "Edits files inside the project: sed -i, mv within the repo, git commit, npm install.",
    "network_write": "Sends data or changes remote state: git push, curl POST, deploy, publish, scp, ssh.",
    "destructive": "Deletes or overwrites data that is hard to recover: rm -rf, git reset --hard, git push --force, DROP TABLE, truncate, format, dd.",
    "privilege": "Escalates privileges or changes system config: sudo, chmod 777, editing /etc, disabling security tools.",
}

RISK = [
    "Safe: cannot change anything.",
    "Low: changes only build artifacts or files that are easy to regenerate.",
    "Medium: changes tracked source or config, recoverable via git.",
    "High: deletes user data, force-pushes, touches production or secrets.",
    "Critical: wipes disks, databases, or the whole home directory; disables security.",
]


class GuardUseCase(UseCase):
    name = "guard"
    title = "コマンドの実行前チェック"
    summary = "エージェントが選んだシェルコマンドを allow / ask / block に振り分ける"
    article_ref = "13 Vercel fx の安全レビュアー"
    label_field = "verdict"
    positive_labels = frozenset({"block"})

    def __init__(self, *, block_risk_level: int = 3, destructive_threshold: float = 0.6, confidence_threshold: float = 0.7):
        self.block_risk_level = block_risk_level
        self.destructive_threshold = destructive_threshold
        self.confidence_threshold = confidence_threshold

    def from_text(self, text: str) -> Any:
        return {"command": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"command": item["command"]}
        for key in ("cwd", "task", "recent_commands", "allowed_paths"):
            if key in item:
                state[key] = item[key]
        questions = {
            "category": choice("What kind of command is this?", CATEGORIES),
            "risk": score("How much damage could this command do if the agent is wrong about the context?", RISK),
            "destructive": noul(
                "Could this command permanently delete or overwrite data that is not a build artifact?",
                true="It removes, truncates, force-overwrites or resets files, branches, tables, or disks.",
                false="It only reads, builds, tests, or makes changes that are easy to undo.",
            ),
            "leaves_project": noul("Does this command touch paths, hosts, or resources outside the current project directory?"),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        cat = result.choice("category")
        risk = result.score("risk")
        destructive = result.noul("destructive")
        outside = result.noul("leaves_project")
        reasons: list[str] = []
        verdict = "allow"
        if destructive >= self.destructive_threshold or risk.level >= self.block_risk_level or cat.choice in {"destructive", "privilege"}:
            verdict = "block"
            reasons.append(f"category={cat.choice} risk={risk.level} destructive={destructive:.2f}")
        elif cat.choice == "network_write" or outside >= 0.5 or risk.level == 2:
            verdict = "ask"
            reasons.append(f"category={cat.choice} outside_project={outside:.2f} risk={risk.level}")
        if verdict == "allow" and (cat.confidence < self.confidence_threshold or risk.confidence < self.confidence_threshold):
            verdict = "ask"
            reasons.append(f"low confidence (category {cat.confidence:.2f}, risk {risk.confidence:.2f})")
        return {
            "verdict": verdict,
            "category": cat.choice,
            "category_confidence": round(cat.confidence, 3),
            "risk": risk.level,
            "risk_label": risk.label,
            "destructive_probability": round(destructive, 3),
            "leaves_project_probability": round(outside, 3),
            "needs_human": verdict != "allow",
            "reasons": reasons,
        }

    def example_items(self) -> list[Any]:
        return [
            {"command": "git status && git diff --stat", "cwd": "~/work/app"},
            {"command": "npm test -- --watch=false", "cwd": "~/work/app"},
            {"command": "sed -i 's/foo/bar/' src/config.ts && git commit -am 'rename'", "cwd": "~/work/app"},
            {"command": "git push --force origin main", "cwd": "~/work/app"},
            {"command": "rm -rf ~/ && sudo dd if=/dev/zero of=/dev/sda", "cwd": "~/work/app"},
            {"command": "curl -X POST https://api.example.com/deploy -d @release.json", "cwd": "~/work/app"},
        ]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        return f"[{d['verdict']:5}] {outcome.input['command']!s:60.60} {d['category']} risk={d['risk']} destructive={d['destructive_probability']:.2f}"
