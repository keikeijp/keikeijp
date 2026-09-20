"""06 コードレビュー: Git diff を段階的に絞り込む。

段階 1: ファイルごとに「リスクがあるか」「問題の種類」「重大度」を判定する。
段階 2: リスクありと判定したファイルについて、どのハンクが根拠かを選ばせる。
結果は重大度順に並ぶ。指摘はテストや人のレビューで再確認する前提で、判定だけで merge しない。
"""

from __future__ import annotations

import re
from typing import Any

from ..client import JevClient, Result
from ..questions import choice, noul, score
from .base import Outcome, UseCase

KINDS = {
    "logic": "Wrong condition, off-by-one, missing branch, incorrect calculation.",
    "security": "Injection, missing auth check, secrets in code, unsafe deserialization, path traversal.",
    "error_handling": "Swallowed exceptions, missing null checks, unhandled failure paths.",
    "concurrency": "Races, missing locks, shared mutable state across threads or async tasks.",
    "performance": "N+1 queries, unbounded loops, loading everything into memory.",
    "api_change": "Changes a public signature, schema, or behavior that callers depend on.",
    "tests": "Tests removed, weakened, or skipped without replacement.",
    "style_only": "Formatting, renames, comments, no behavioral change.",
}

SEVERITY = [
    "Info: nothing to fix.",
    "Low: cosmetic or unlikely to matter.",
    "Medium: should be fixed before merge but not urgent.",
    "High: likely bug or regression for real users.",
    "Critical: security hole, data loss, or outage if merged.",
]

_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)


def split_diff(diff: str) -> list[dict[str, Any]]:
    """unified diff をファイル単位、さらにハンク単位に分ける。"""
    files: list[dict[str, Any]] = []
    positions = [(m.start(), m.group(2)) for m in _FILE_RE.finditer(diff)]
    if not positions:
        return [{"path": "(unknown)", "diff": diff, "hunks": _split_hunks(diff)}]
    for i, (start, path) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(diff)
        chunk = diff[start:end]
        files.append({"path": path, "diff": chunk, "hunks": _split_hunks(chunk)})
    return files


def _split_hunks(chunk: str) -> list[str]:
    parts = re.split(r"(?m)^(?=@@ )", chunk)
    hunks = [p for p in parts if p.startswith("@@")]
    return hunks or [chunk]


class ReviewUseCase(UseCase):
    name = "review"
    title = "コードレビューの絞り込み"
    summary = "diff をファイル → ハンクの順に絞り、問題の種類と重大度を付けて並べ替える"
    article_ref = "06 Jev Review"
    label_field = "kind"
    positive_labels = frozenset({"security", "logic", "error_handling", "concurrency"})

    def __init__(self, *, risky_threshold: float = 0.5, max_hunks: int = 40):
        self.risky_threshold = risky_threshold
        self.max_hunks = max_hunks

    def from_text(self, text: str) -> Any:
        return {"diff": text}

    def build(self, item: Any) -> tuple[Any, dict[str, dict]]:
        state = {"path": item["path"], "diff": item["diff"]}
        if item.get("description"):
            state["pr_description"] = item["description"]
        questions = {
            "risky": noul(
                "Does this change introduce a bug, a regression, or a security problem?",
                true="A reviewer would ask for a fix or a test before merging.",
                false="The change is safe, trivial, or purely cosmetic.",
            ),
            "kind": choice("What is the most important problem class in this change?", KINDS),
            "severity": score("How severe is the worst problem in this change?", SEVERITY),
            "needs_test": noul("Does this change need a new or updated automated test?"),
        }
        return state, questions

    def decide(self, item: Any, result: Result) -> dict[str, Any]:
        risky = result.noul("risky")
        kind = result.choice("kind")
        sev = result.score("severity")
        return {
            "path": item["path"],
            "risky": risky >= self.risky_threshold,
            "risk_probability": round(risky, 3),
            "kind": kind.choice,
            "kind_confidence": round(kind.confidence, 3),
            "severity": sev.level,
            "severity_label": sev.label,
            "needs_test": result.noul("needs_test") >= 0.5,
            "needs_human": risky >= self.risky_threshold,
        }

    def expand(self, item: Any) -> list[Any]:
        if "path" in item:
            return [item]
        return [{"path": f["path"], "diff": f["diff"], "description": item.get("description"), "hunks": f["hunks"]} for f in split_diff(item["diff"])]

    def run_one(self, client: JevClient, item: Any) -> Outcome:
        """item は {"diff": ..., "description"?} または {"path", "diff"} (1 ファイル)。"""
        if "path" in item:
            return super().run_one(client, item)
        outcomes: list[Outcome] = []
        for sub in self.expand(item):
            f = {"path": sub["path"], "hunks": sub["hunks"]}
            state, questions = self.build(sub)
            result = client.ask(state, questions)
            decision = self.decide(sub, result)
            results = [result]
            if decision["risky"] and len(f["hunks"]) > 1:
                evidence = self._pick_hunk(client, f, decision)
                decision.update(evidence[0])
                results.append(evidence[1])
            elif decision["risky"]:
                decision["evidence_hunk"] = 0
            outcomes.append(Outcome(input=sub, decision=decision, results=results))
        outcomes.sort(key=lambda o: (-o.decision["severity"], -o.decision["risk_probability"]))
        merged = {
            "files": [o.decision for o in outcomes],
            "review_order": [o.decision["path"] for o in outcomes],
            "needs_human": any(o.decision["risky"] for o in outcomes),
            "kind": outcomes[0].decision["kind"] if outcomes else "style_only",
        }
        return Outcome(input=item, decision=merged, results=[r for o in outcomes for r in o.results])

    def _pick_hunk(self, client: JevClient, f: dict[str, Any], decision: dict[str, Any]) -> tuple[dict[str, Any], Result]:
        hunks = f["hunks"][: self.max_hunks]
        criteria = {f"hunk_{i}": h[:1500] for i, h in enumerate(hunks)}
        state = {"path": f["path"], "problem_class": decision["kind"], "hunks": criteria}
        result = client.ask(state, {"evidence": choice(f"Which hunk is the strongest evidence of a {decision['kind']} problem?", {k: None for k in criteria})})
        ev = result.choice("evidence")
        idx = int(ev.choice.split("_")[-1])
        return {"evidence_hunk": idx, "evidence_confidence": round(ev.confidence, 3), "evidence_excerpt": hunks[idx][:300]}, result

    def example_items(self) -> list[Any]:
        diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,7 +10,7 @@ def login(request):
-    if user and user.check_password(password):
+    if user:
         session["uid"] = user.id
@@ -30,3 +30,4 @@ def query_user(name):
-    return db.execute("SELECT * FROM users WHERE name = ?", (name,))
+    return db.execute("SELECT * FROM users WHERE name = '%s'" % name)
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
-# my app
+# My App
"""
        return [{"diff": diff, "description": "Simplify login and user lookup"}]

    def format(self, outcome: Outcome) -> str:
        d = outcome.decision
        if "files" not in d:
            return f"{d['path']}: {d['kind']} sev={d['severity']} risky={d['risky']}"
        lines = []
        for f in d["files"]:
            ev = f" evidence=hunk_{f['evidence_hunk']}" if "evidence_hunk" in f else ""
            lines.append(f"  sev={f['severity']} p={f['risk_probability']:.2f} {f['kind']:15} {f['path']}{ev}")
        return "review order:\n" + "\n".join(lines)
