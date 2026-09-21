"""9. jev-rules: 依頼内容と編集対象ファイルに応じて、Claude Code に注入するルールファイルを選ぶ。

元ネタ: EliaAlberti/jev-rules

ルールは `.claude/rules/*.md`。先頭の front matter (任意) で

    ---
    description: FastAPI のエンドポイント規約
    paths: ["src/api/**/*.py", "tests/api/*"]
    tags: [api, python]
    always: false
    ---

選び方:
1. `paths` の glob が編集ファイルに当たれば決定的に採用 (`always: true` も採用)
2. 残りは Jev に `relevant` Noul を 1 ルール 1 decide で並列に聞き (decide_many)、閾値以上を採用
3. 合計文字数の上限で打ち切る

`--hook` を付けると Claude Code の UserPromptSubmit フックとして動き、stdin の JSON から prompt を読み
`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}` を出す。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jevlab.core import Jev, Noul

DEFAULT_RULES_DIR = ".claude/rules"
RELEVANT_Q = {
    "relevant": Noul(
        "Would including this rule file in the assistant's context help it handle the user's request correctly, "
        "given what the request is about and which files are being edited? Answer no for rules about unrelated areas.",
        {"true": "the rule governs the kind of work or files involved in the request", "false": "unrelated to the request and files"},
    )
}


# ---------------------------------------------------------------------------
# front matter
# ---------------------------------------------------------------------------


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        return [_parse_scalar(v) for v in _split_list(value[1:-1])]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.lower() in {"true", "yes", "on"}:
        return True
    if value.lower() in {"false", "no", "off"}:
        return False
    return value


def _split_list(text: str) -> list[str]:
    items, current, quote = [], "", None
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current)
    return [item for item in (i.strip() for i in items) if item]


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """`---` で囲まれた簡易 YAML (key: scalar / [list] / 続く `- item` 行) を読む。依存なし。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, Any] = {}
    key: str | None = None
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
        if re.match(r"^\s*-\s+", line) and key is not None:
            meta.setdefault(key, [])
            if not isinstance(meta[key], list):
                meta[key] = [meta[key]] if meta[key] != "" else []
            meta[key].append(_parse_scalar(re.sub(r"^\s*-\s+", "", line)))
            continue
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match:
            key = match.group(1)
            meta[key] = _parse_scalar(match.group(2))
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


@dataclass
class Rule:
    name: str
    path: str
    body: str
    description: str = ""
    paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    always: bool = False

    @property
    def chars(self) -> int:
        return len(self.body)

    @classmethod
    def from_file(cls, path: str) -> "Rule":
        with open(path, encoding="utf-8") as handle:
            meta, body = parse_front_matter(handle.read())
        as_list = lambda v: [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v not in (None, "") else [])  # noqa: E731
        return cls(
            name=os.path.splitext(os.path.basename(path))[0],
            path=path,
            body=body.strip(),
            description=str(meta.get("description", "") or ""),
            paths=as_list(meta.get("paths") or meta.get("globs")),
            tags=as_list(meta.get("tags")),
            always=bool(meta.get("always", False)),
        )


def load_rules(rules_dir: str) -> list[Rule]:
    if not os.path.isdir(rules_dir):
        return []
    rules = []
    for dirpath, _, filenames in os.walk(rules_dir):
        for name in sorted(filenames):
            if name.endswith((".md", ".mdc", ".txt")):
                rules.append(Rule.from_file(os.path.join(dirpath, name)))
    return sorted(rules, key=lambda r: r.name)


# ---------------------------------------------------------------------------
# glob マッチ
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if ch == "*":
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "{" and "}" in pattern[i:]:
            j = pattern.index("}", i)
            out += "(?:" + "|".join(re.escape(alt) for alt in pattern[i + 1 : j].split(",")) + ")"
            i = j
        else:
            out += re.escape(ch)
        i += 1
    return re.compile("^" + out + "$")


def glob_match(pattern: str, path: str) -> bool:
    path = path.replace("\\", "/").lstrip("./")
    pattern = pattern.strip().lstrip("./")
    if "/" not in pattern:  # ディレクトリ指定のないパターンはファイル名で判定
        return fnmatch.fnmatchcase(os.path.basename(path), pattern)
    return bool(_glob_to_regex(pattern).match(path))


def rule_matches_paths(rule: Rule, files: Sequence[str]) -> bool:
    return any(glob_match(pattern, f) for pattern in rule.paths for f in files)


# ---------------------------------------------------------------------------
# 選択
# ---------------------------------------------------------------------------


@dataclass
class Selection:
    selected: list[Rule]
    reasons: dict[str, str]
    skipped: dict[str, str]
    jev_calls: int = 0

    def render(self) -> str:
        return "\n\n".join(f"<!-- rule: {r.name} ({self.reasons.get(r.name, '')}) -->\n{r.body}" for r in self.selected)

    def to_dict(self) -> dict[str, Any]:
        return {"selected": [r.name for r in self.selected], "reasons": self.reasons, "skipped": self.skipped, "chars": sum(r.chars for r in self.selected), "jev_calls": self.jev_calls}


def select_rules(jev: Jev | None, rules: Sequence[Rule], request: str, edited_files: Sequence[str] = (), threshold: float = 0.5, max_chars: int = 12_000) -> Selection:
    reasons: dict[str, str] = {}
    skipped: dict[str, str] = {}
    chosen: list[tuple[int, float, Rule]] = []  # (優先度, スコア, rule)
    remaining: list[Rule] = []
    for rule in rules:
        if rule.always:
            chosen.append((0, 1.0, rule))
            reasons[rule.name] = "always"
        elif rule.paths and rule_matches_paths(rule, edited_files):
            chosen.append((1, 1.0, rule))
            reasons[rule.name] = "glob"
        else:
            remaining.append(rule)
    calls = 0
    if remaining and jev is not None and request.strip():
        states = [
            {
                "request": request[:2000],
                "edited_files": list(edited_files)[:30],
                "rule": {"name": r.name, "description": r.description, "tags": r.tags, "paths": r.paths, "preview": r.body[:300]},
            }
            for r in remaining
        ]
        decisions = jev.decide_many((s, RELEVANT_Q) for s in states)
        calls = len(decisions)
        for rule, decision in zip(remaining, decisions):
            p = decision.noul("relevant").noul
            if p >= threshold:
                chosen.append((2, p, rule))
                reasons[rule.name] = f"jev:{p:.2f}"
            else:
                skipped[rule.name] = f"jev:{p:.2f}"
    else:
        for rule in remaining:
            skipped[rule.name] = "no request / no jev"
    chosen.sort(key=lambda item: (item[0], -item[1], item[2].name))
    selected: list[Rule] = []
    total = 0
    for _, _, rule in chosen:
        if total + rule.chars > max_chars and selected:
            skipped[rule.name] = f"over max_chars ({reasons.pop(rule.name, '')})"
            continue
        selected.append(rule)
        total += rule.chars
    return Selection(selected, reasons, skipped, calls)


# ---------------------------------------------------------------------------
# Claude Code フック
# ---------------------------------------------------------------------------


def git_edited_files(cwd: str | None) -> list[str]:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line[3:].strip().split(" -> ")[-1] for line in out.stdout.splitlines() if line.strip()][:100]


def hook_output(selection: Selection) -> dict[str, Any]:
    context = selection.render()
    if not context:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def install_snippet(rules_dir: str, backend: str | None) -> dict[str, Any]:
    cmd = f"python -m jevlab.apps.rules --hook --git-files --rules-dir {rules_dir}"
    if backend:
        cmd += f" --backend {backend}"
    return {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd, "timeout": 20}]}]}}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab rules", description="依頼と編集ファイルに合う Claude Code ルールを Jev で選ぶ")
    parser.add_argument("--rules-dir", default=DEFAULT_RULES_DIR)
    parser.add_argument("--request", default=None, help="ユーザーの依頼文 (--hook なら stdin の JSON から読む)")
    parser.add_argument("--files", nargs="*", default=[], help="編集中のファイル (glob 判定用)")
    parser.add_argument("--git-files", action="store_true", help="git status の変更ファイルも編集ファイルとして使う")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--max-chars", type=int, default=12_000)
    parser.add_argument("--hook", action="store_true", help="UserPromptSubmit フックとして動く (stdin JSON → hookSpecificOutput JSON)")
    parser.add_argument("--install-hook", action="store_true", help="settings.json に貼るスニペットを表示")
    parser.add_argument("--json", dest="json_out", action="store_true", help="選択理由を JSON で出す")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    if args.install_hook:
        print(json.dumps(install_snippet(args.rules_dir, args.backend), ensure_ascii=False, indent=2))
        return 0
    request = args.request or ""
    cwd = None
    if args.hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        request = str(payload.get("prompt") or payload.get("user_prompt") or request)
        cwd = payload.get("cwd")
    files = list(args.files)
    if args.git_files:
        files += git_edited_files(cwd)
    rules_dir = args.rules_dir if os.path.isabs(args.rules_dir) or not cwd else os.path.join(cwd, args.rules_dir)
    rules = load_rules(rules_dir)
    try:
        jev: Jev | None = Jev(args.backend) if rules else None
    except Exception as error:  # フックはプロンプトを止めない
        print(f"[jev-rules] backend unavailable: {error}", file=sys.stderr)
        jev = None
    selection = select_rules(jev, rules, request, files, args.threshold, args.max_chars)
    if args.hook:
        print(json.dumps(hook_output(selection), ensure_ascii=False))
    elif args.json_out:
        print(json.dumps(selection.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(selection.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
