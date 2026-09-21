"""7. jev-review: git 差分 (またはリポジトリ全体) を段階的にレビューして JSON / HTML レポートを作る。

元ネタ: devagrawal09/jev-review

Stage 1  各ハンクについて 1 回の decide で
           risk        Score  [none, low, medium, high, critical]
           category    Choice {logic, security, performance, error_handling, api_change, test, style, docs, other}
           needs_test  Noul
           breaking    Noul
Stage 2  risk >= medium のハンクについて、周辺 (関数レベル) のコンテキストを添えて
           has_bug / security_issue / needs_human_review (Noul)
         Jev は文章を生成できないので、説明はルールベース (カテゴリ + 行範囲 + 確率) で組み立てる
Stage 3  ファイル別に集計し、PR 全体の merge_readiness Score を 1 回で聞く
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from jevlab.core import Choice, Jev, Noul, Score

RISK_LEVELS = ["none", "low", "medium", "high", "critical"]
CATEGORIES = {
    "logic": "control flow, data handling, business rules",
    "security": "auth, input validation, secrets, injection, permissions",
    "performance": "complexity, memory, I/O, caching",
    "error_handling": "exceptions, retries, edge cases, error messages",
    "api_change": "public function/endpoint/schema signature changed",
    "test": "test code only",
    "style": "formatting, naming, comments without behavior change",
    "docs": "documentation only",
    "other": None,
}
READINESS = ["not mergeable: serious problems", "needs work: several issues to fix first", "mergeable with small fixes", "mergeable as is"]

STAGE1 = {
    "risk": Score(
        ["none: no behavioral impact", "low: trivial, obviously safe", "medium: could introduce a bug, needs a careful look", "high: likely to break something or touches sensitive code", "critical: security or data-loss risk"],
        "How risky is this change hunk?",
    ),
    "category": Choice(CATEGORIES, "What kind of change is this hunk mainly?"),
    "needs_test": Noul("Should this change be covered by a (new or updated) test?"),
    "breaking": Noul("Does this hunk change a public interface or behavior in a way that could break callers?"),
}
STAGE2 = {
    "has_bug": Noul("Looking at the change together with its surrounding function, is there a concrete bug (wrong logic, off-by-one, missing null check, wrong error path)?"),
    "security_issue": Noul("Does this change introduce a security problem (injection, missing auth/validation, secret exposure)?"),
    "needs_human_review": Noul("Should a human reviewer look at this change carefully before merging?"),
}
READINESS_Q = {"merge_readiness": Score(READINESS, "Given the per-file review summary, how ready is this change set to merge?")}

SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".cs", ".swift", ".sh", ".sql", ".yaml", ".yml", ".toml", ".json", ".md"}
FUNC_RE = re.compile(r"^\s*(async\s+def |def |class |function\s|func |fn |pub fn |public |private |protected |static |export )")


# ---------------------------------------------------------------------------
# diff 解析
# ---------------------------------------------------------------------------


@dataclass
class Hunk:
    file: str
    index: int
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # 先頭 1 文字が +/-/space
    # 判定結果
    risk: float = 0.0
    risk_level: int = 0
    category: str = "other"
    needs_test: float = 0.0
    breaking: float = 0.0
    deep: dict[str, Any] | None = None
    note: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def added(self) -> list[tuple[int, str]]:
        out, n = [], self.new_start
        for line in self.lines:
            if line.startswith("+"):
                out.append((n, line[1:]))
            if not line.startswith("-"):
                n += 1
        return out

    @property
    def removed(self) -> list[tuple[int, str]]:
        out, n = [], self.old_start
        for line in self.lines:
            if line.startswith("-"):
                out.append((n, line[1:]))
            if not line.startswith("+"):
                n += 1
        return out

    @property
    def new_range(self) -> tuple[int, int]:
        added = self.added
        return (added[0][0], added[-1][0]) if added else (self.new_start, self.new_start + max(0, self.new_count - 1))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risk_label"] = RISK_LEVELS[self.risk_level]
        data["new_range"] = list(self.new_range)
        return data


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(text: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current_file = ""
    current: Hunk | None = None
    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            current, current_file = None, ""
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            continue
        if raw.startswith("--- ") and current is None:
            continue
        match = _HUNK_RE.match(raw)
        if match:
            o, oc, n, nc, ctx = match.groups()
            current = Hunk(current_file or "?", len(hunks), ctx.strip(), int(o), int(oc or 1), int(n), int(nc or 1))
            hunks.append(current)
            continue
        if current is not None and raw[:1] in {"+", "-", " "} and not raw.startswith("\\"):
            current.lines.append(raw)
        elif current is not None and raw == "":
            current.lines.append(" ")
    return hunks


def repo_to_hunks(root: str, chunk_lines: int = 80, max_files: int = 200) -> list[Hunk]:
    """リポジトリのソースを chunk_lines 行ごとの「追加ハンク」として扱う。"""
    hunks: list[Hunk] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"})
        for name in sorted(filenames):
            if os.path.splitext(name)[1] not in SOURCE_EXTS:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for start in range(0, len(lines), chunk_lines):
                chunk = lines[start : start + chunk_lines]
                hunks.append(Hunk(rel, len(hunks), f"{rel}:{start + 1}", start + 1, 0, start + 1, len(chunk), ["+" + line for line in chunk]))
            if len({h.file for h in hunks}) >= max_files:
                return hunks
    return hunks


def function_context(root: str | None, hunk: Hunk, radius: int = 40) -> str:
    """変更行を含む関数 (見つからなければ ±radius 行) をファイルから切り出す。ファイルが無ければハンク本文。"""
    if not root:
        return hunk.text
    path = os.path.join(root, hunk.file)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return hunk.text
    lo, hi = hunk.new_range
    start = max(0, lo - 1)
    for i in range(min(start, len(lines) - 1), max(-1, start - radius), -1):
        if FUNC_RE.match(lines[i]):
            start = i
            break
    else:
        start = max(0, lo - 1 - radius // 2)
    end = min(len(lines), hi + radius // 2)
    return "\n".join(f"{i + 1:5d} {line}" for i, line in enumerate(lines[start:end], start=start))


# ---------------------------------------------------------------------------
# レビュー本体
# ---------------------------------------------------------------------------


@dataclass
class ReviewReport:
    hunks: list[Hunk]
    files: dict[str, dict[str, Any]]
    merge_readiness: float
    merge_readiness_level: int
    jev_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "merge_readiness": round(self.merge_readiness, 3),
            "merge_readiness_level": self.merge_readiness_level,
            "merge_readiness_label": READINESS[self.merge_readiness_level],
            "files": self.files,
            "hunks": [h.to_dict() for h in self.hunks],
            "jev_calls": self.jev_calls,
        }


def _hunk_state(hunk: Hunk, max_chars: int = 3000) -> dict[str, Any]:
    text = hunk.text
    if len(text) > max_chars:
        text = text[: max_chars // 2] + "\n...[truncated]...\n" + text[-max_chars // 2 :]
    return {"file": hunk.file, "hunk_header": hunk.header, "diff": text, "added_lines": len(hunk.added), "removed_lines": len(hunk.removed)}


def review_hunks(hunks: Sequence[Hunk], jev: Jev, root: str | None = None, deep_min_level: int = 2, min_confidence: float = 0.3) -> ReviewReport:
    hunks = list(hunks)
    calls = 0
    # Stage 1
    if hunks:
        decisions = jev.decide_many((_hunk_state(h), STAGE1) for h in hunks)
        calls += len(decisions)
        for hunk, decision in zip(hunks, decisions):
            risk = decision.score("risk")
            hunk.risk, hunk.risk_level = risk.score, risk.level
            category = decision.choice("category")
            hunk.category = category.choice if category.confidence >= min_confidence else "other"
            hunk.needs_test = decision.noul("needs_test").noul
            hunk.breaking = decision.noul("breaking").noul
    # Stage 2
    deep_targets = [h for h in hunks if h.risk_level >= deep_min_level]
    if deep_targets:
        states = [{"file": h.file, "hunk": _hunk_state(h)["diff"], "function_context": function_context(root, h)[:6000]} for h in deep_targets]
        decisions = jev.decide_many((s, STAGE2) for s in states)
        calls += len(decisions)
        for hunk, decision in zip(deep_targets, decisions):
            hunk.deep = {name: round(decision.noul(name).noul, 3) for name in STAGE2}
    for hunk in hunks:
        hunk.note = _summarize(hunk)
    # Stage 3
    files: dict[str, dict[str, Any]] = {}
    for hunk in hunks:
        entry = files.setdefault(hunk.file, {"hunks": 0, "max_risk_level": 0, "categories": [], "needs_test": False, "breaking": False, "possible_bugs": 0, "security": 0})
        entry["hunks"] += 1
        entry["max_risk_level"] = max(entry["max_risk_level"], hunk.risk_level)
        if hunk.category not in entry["categories"]:
            entry["categories"].append(hunk.category)
        entry["needs_test"] = entry["needs_test"] or hunk.needs_test >= 0.5
        entry["breaking"] = entry["breaking"] or hunk.breaking >= 0.5
        if hunk.deep:
            entry["possible_bugs"] += int(hunk.deep["has_bug"] >= 0.5)
            entry["security"] += int(hunk.deep["security_issue"] >= 0.5)
    for entry in files.values():
        entry["max_risk"] = RISK_LEVELS[entry["max_risk_level"]]
    readiness, level = 3.0, 3
    if hunks:
        state = {"files": files, "total_hunks": len(hunks), "high_risk_hunks": sum(1 for h in hunks if h.risk_level >= 3), "possible_bugs": sum(e["possible_bugs"] for e in files.values())}
        answer = jev.decide(state, READINESS_Q).score("merge_readiness")
        calls += 1
        readiness, level = answer.score, answer.level
    return ReviewReport(hunks, files, readiness, level, calls)


def _summarize(hunk: Hunk) -> str:
    lo, hi = hunk.new_range
    where = f"line {lo}" if lo == hi else f"lines {lo}-{hi}"
    parts = [f"{hunk.category} change in {hunk.file} ({where}), risk {RISK_LEVELS[hunk.risk_level]}"]
    if hunk.breaking >= 0.5:
        parts.append(f"possibly breaking ({hunk.breaking:.0%})")
    if hunk.needs_test >= 0.5:
        parts.append(f"needs test ({hunk.needs_test:.0%})")
    if hunk.deep:
        if hunk.deep["has_bug"] >= 0.5:
            parts.append(f"possible bug ({hunk.deep['has_bug']:.0%})")
        if hunk.deep["security_issue"] >= 0.5:
            parts.append(f"security concern ({hunk.deep['security_issue']:.0%})")
        if hunk.deep["needs_human_review"] >= 0.5:
            parts.append("human review recommended")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_RISK_COLORS = ["#8a8f98", "#3a8f5c", "#c48a10", "#d9531e", "#b3261e"]


def render_html(report: ReviewReport, title: str = "jev-review") -> str:
    esc = html.escape
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>", esc(title), "</title><style>",
        "body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:0 auto;max-width:1100px;padding:16px;background:#f7f7f8;color:#1e2128}",
        "h1{font-size:20px}.badge{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;margin-right:6px}",
        ".hunk{background:#fff;border:1px solid #dcdfe4;border-radius:6px;margin:12px 0;padding:10px}",
        "pre{margin:8px 0 0;overflow-x:auto;font:12px/1.4 ui-monospace,Menlo,monospace;background:#fafbfc;padding:8px;border-radius:4px}",
        ".add{background:#e6ffec;display:block}.del{background:#ffebe9;display:block}.ctx{display:block;color:#57606a}",
        "table{border-collapse:collapse;background:#fff}td,th{border:1px solid #dcdfe4;padding:4px 10px;text-align:left}",
        "</style></head><body>",
        f"<h1>{esc(title)}</h1>",
        f"<p><b>Merge readiness:</b> {esc(READINESS[report.merge_readiness_level])} (score {report.merge_readiness:.2f}/3) · {len(report.hunks)} hunks · {report.jev_calls} Jev calls</p>",
        "<table><tr><th>file</th><th>hunks</th><th>max risk</th><th>categories</th><th>needs test</th><th>breaking</th><th>bugs?</th></tr>",
    ]
    for name, entry in report.files.items():
        parts.append(
            f"<tr><td>{esc(name)}</td><td>{entry['hunks']}</td><td><span class='badge' style='background:{_RISK_COLORS[entry['max_risk_level']]}'>{esc(entry['max_risk'])}</span></td>"
            f"<td>{esc(', '.join(entry['categories']))}</td><td>{'yes' if entry['needs_test'] else ''}</td><td>{'yes' if entry['breaking'] else ''}</td><td>{entry['possible_bugs'] or ''}</td></tr>"
        )
    parts.append("</table>")
    for hunk in sorted(report.hunks, key=lambda h: (-h.risk_level, h.file, h.index)):
        parts.append(f"<div class='hunk' id='hunk-{hunk.index}'><span class='badge' style='background:{_RISK_COLORS[hunk.risk_level]}'>{esc(RISK_LEVELS[hunk.risk_level])}</span>")
        parts.append(f"<span class='badge' style='background:#4b5563'>{esc(hunk.category)}</span><b>{esc(hunk.file)}</b> <code>@@ {esc(hunk.header)}</code><div>{esc(hunk.note)}</div><pre>")
        for line in hunk.lines:
            cls = "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
            parts.append(f"<span class='{cls}'>{esc(line)}</span>")
        parts.append("</pre></div>")
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _git_diff(root: str | None, staged: bool, ref: str | None) -> str:
    cmd = ["git", "diff", "--no-color"]
    if staged:
        cmd.append("--cached")
    if ref:
        cmd.append(ref)
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"git diff に失敗: {proc.stderr.strip()}")
    return proc.stdout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab review", description="git 差分を Jev で段階的にレビューする")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--diff-file", help="unified diff ファイル (- で stdin)")
    source.add_argument("--repo", help="差分ではなくディレクトリ内のソース全体を見る")
    parser.add_argument("--root", default=None, help="関数コンテキストを読むためのリポジトリルート (既定: cwd)")
    parser.add_argument("--staged", action="store_true", help="git diff --cached")
    parser.add_argument("--ref", default=None, help="git diff <ref> (例: main...HEAD)")
    parser.add_argument("--json", dest="json_out", default=None, help="JSON レポートの書き出し先 (- で stdout)")
    parser.add_argument("--html", default=None, help="HTML レポートの書き出し先")
    parser.add_argument("--serve", type=int, default=None, help="HTML を http.server で配信するポート")
    parser.add_argument("--deep-min-risk", choices=RISK_LEVELS, default="medium", help="Stage 2 に進める最小リスク")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    root = args.root or (args.repo if args.repo else os.getcwd())
    if args.repo:
        hunks = repo_to_hunks(args.repo)
    else:
        if args.diff_file == "-":
            text = sys.stdin.read()
        elif args.diff_file:
            with open(args.diff_file, encoding="utf-8") as handle:
                text = handle.read()
        else:
            text = _git_diff(root, args.staged, args.ref)
        hunks = parse_unified_diff(text)
    report = review_hunks(hunks, Jev(args.backend), root=root, deep_min_level=RISK_LEVELS.index(args.deep_min_risk))
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.json_out and args.json_out != "-":
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(payload)
    elif args.json_out == "-" or not (args.html or args.serve):
        print(payload)
    page = render_html(report)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as handle:
            handle.write(page)
        print(f"HTML report: {args.html}", file=sys.stderr)
    if args.serve:
        _serve(page, args.serve)
    return 0


def _serve(page: str, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    body = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    print(f"serving review at http://127.0.0.1:{port}/ (Ctrl-C で終了)", file=sys.stderr)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
