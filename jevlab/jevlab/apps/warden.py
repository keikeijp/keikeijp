"""19. warden (DevMortimer/pi-warden の再実装): エージェント (Pi / Claude Code / 何でも) のイベントログを監視し、
プロジェクトルール違反・同じ失敗の繰り返し・未検証の完了宣言を検出する。

入力:
- ルール Markdown (箇条書き 1 行 = 1 ルール)
- イベント JSONL: {"ts": ..., "kind": "tool_call" | "tool_result" | "message", "text": "..."}

チェック:
- `violates_rule`     Noul (イベント, ルール) 毎。キーワード重なりで事前フィルタして呼び出し数を抑える
- `same_failure_again` Noul 直近のエラー結果と比較。difflib で完全/近似重複ならJev を呼ばずに検出
- `claims_completion` Noul + `verified` Noul (宣言の後に検証コマンドが走ったか)

出力: findings JSON。critical があれば終了コード非 0。`--watch` でファイルを tail する (テストでは使わない)。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from jevlab.core import Jev, Noul, tokenize

MAX_TEXT = 600
ERROR_MARKERS = ("error", "exception", "traceback", "failed", "failure", "exit code", "non-zero", "エラー", "失敗", "panic:", "fatal")
COMPLETION_MARKERS = ("done", "complete", "finished", "fixed", "implemented", "passes", "passing", "works now", "resolved", "完了", "できました", "直しました", "実装しました", "通りました")
VERIFY_MARKERS = ("pytest", "npm test", "yarn test", "go test", "cargo test", "make test", "mvn test", "gradle test", "jest", "vitest", "unittest", "tox", "ruff", "eslint", "mypy", "tsc", "curl", "python -m", "build", "lint", "check")
NEAR_DUP_RATIO = 0.85  # これ以上は Jev を呼ばずに「同じ失敗」
ASK_RATIO = 0.4  # この間は Jev に聞く
MIN_KEYWORD_OVERLAP = 0.2


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------


def parse_rules(markdown: str) -> list[str]:
    rules = []
    for line in markdown.splitlines():
        match = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$", line)
        if match:
            rules.append(re.sub(r"\*\*|`", "", match.group(1)).strip())
    return rules


def load_events(path: str | Path) -> list[dict[str, Any]]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _text(event: dict[str, Any]) -> str:
    return str(event.get("text", ""))[:MAX_TEXT]


def _tokens(text: str) -> set[str]:
    return {t for t in tokenize(text) if len(t) >= 2 or not t.isascii()}


def keyword_overlap(text: str, rule: str) -> float:
    """ルール語彙のうちイベントに現れる割合 (0..1)。Jev 呼び出しの事前フィルタ。"""
    rule_tokens = _tokens(rule)
    if not rule_tokens:
        return 0.0
    return len(rule_tokens & _tokens(text)) / len(rule_tokens)


def is_error(event: dict[str, Any]) -> bool:
    if event.get("ok") is False or event.get("error"):
        return True
    text = _text(event).lower()
    return event.get("kind") == "tool_result" and any(marker in text for marker in ERROR_MARKERS)


# ---------------------------------------------------------------------------
# チェック
# ---------------------------------------------------------------------------


def check_rule_violations(jev: Jev, events: list[dict[str, Any]], rules: list[str], min_overlap: float = MIN_KEYWORD_OVERLAP, threshold: float = 0.7) -> list[dict[str, Any]]:
    pairs = []
    for index, event in enumerate(events):
        if event.get("kind") not in ("tool_call", "message"):
            continue
        for rule in rules:
            if keyword_overlap(_text(event), rule) >= min_overlap:
                pairs.append((index, rule))
    if not pairs:
        return []
    question = {"violates_rule": Noul("エージェントのこの行動/発言はプロジェクトルールに違反しているか?")}
    items = [({"rule": rule, "event_kind": events[i]["kind"], "text": _text(events[i])}, question) for i, rule in pairs]
    decisions = jev.decide_many(items)
    findings = []
    for (index, rule), decision in zip(pairs, decisions):
        p = decision.noul("violates_rule").noul
        if p >= threshold:
            findings.append({"check": "rule_violation", "severity": "critical" if p >= 0.9 else "warning", "event_index": index, "rule": rule, "probability": round(p, 3), "message": f"ルール違反の疑い: {rule}"})
    return findings


def check_repeated_failures(jev: Jev, events: list[dict[str, Any]], near_ratio: float = NEAR_DUP_RATIO, ask_ratio: float = ASK_RATIO, threshold: float = 0.7) -> list[dict[str, Any]]:
    """直近のエラーと今回のエラーを比較。近似重複は difflib だけで検出、微妙なものは Jev に聞く。"""
    findings = []
    previous: tuple[int, str] | None = None
    question = {"same_failure_again": Noul("2 つのエラーは同じ原因の同じ失敗か (エージェントが同じ試行を繰り返しているか)?")}
    for index, event in enumerate(events):
        if not is_error(event):
            continue
        text = _text(event)
        if previous is not None:
            ratio = difflib.SequenceMatcher(None, previous[1], text).ratio()
            same, how = False, ""
            if ratio >= near_ratio:
                same, how = True, "exact" if ratio >= 0.999 else "near_duplicate"
            elif ratio >= ask_ratio:
                p = jev.decide({"previous_error": previous[1], "current_error": text}, question).noul("same_failure_again").noul
                same, how = p >= threshold, f"jev:{p:.2f}"
            if same:
                findings.append({"check": "repeated_failure", "severity": "critical", "event_index": index, "previous_index": previous[0], "ratio": round(ratio, 3), "how": how, "message": "同じ失敗の繰り返し (前回のエラーと同一/近似)"})
        previous = (index, text)
    return findings


def check_completion_claims(jev: Jev, events: list[dict[str, Any]], threshold: float = 0.7) -> list[dict[str, Any]]:
    """完了宣言 (message) の後に検証コマンドが走ったかを確認する。"""
    candidates = [i for i, e in enumerate(events) if e.get("kind") == "message" and any(m in _text(e).lower() for m in COMPLETION_MARKERS)]
    if not candidates:
        return []
    claim_q = {"claims_completion": Noul("この発言はタスクが完了した/直った/動くと主張しているか?")}
    claim_decisions = jev.decide_many(({"message": _text(events[i])}, claim_q) for i in candidates)
    findings = []
    verify_items: list[tuple[int, Any]] = []
    for index, decision in zip(candidates, claim_decisions):
        if decision.noul("claims_completion").noul < threshold:
            continue
        # 宣言の後 (次の完了宣言まで) に走ったツール呼び出し
        later_calls = [_text(e)[:200] for e in events[index + 1 :] if e.get("kind") == "tool_call"]
        earlier_calls = [_text(e)[:200] for e in events[max(0, index - 6) : index] if e.get("kind") == "tool_call"]
        if not later_calls and not any(any(m in c.lower() for m in VERIFY_MARKERS) for c in earlier_calls):
            findings.append({"check": "unverified_completion", "severity": "critical", "event_index": index, "message": "完了を宣言したが検証コマンドが一度も走っていない"})
            continue
        verify_items.append((index, {"claim": _text(events[index])[:300], "commands_before": earlier_calls[-6:], "commands_after": later_calls[:10]}))
    if verify_items:
        verify_q = {"verified": Noul("この完了宣言はテスト/検証コマンドの実行によって裏付けられているか?")}
        decisions = jev.decide_many((state, verify_q) for _, state in verify_items)
        for (index, _), decision in zip(verify_items, decisions):
            p = decision.noul("verified").noul
            if p < threshold:
                findings.append({"check": "unverified_completion", "severity": "critical" if p < 0.3 else "warning", "event_index": index, "probability": round(p, 3), "message": "完了宣言に対する検証が不十分"})
    return findings


def run_checks(jev: Jev, events: list[dict[str, Any]], rules: list[str]) -> list[dict[str, Any]]:
    findings = check_rule_violations(jev, events, rules) + check_repeated_failures(jev, events) + check_completion_claims(jev, events)
    return sorted(findings, key=lambda f: (f["event_index"], f["check"]))


def exit_code(findings: list[dict[str, Any]]) -> int:
    return 1 if any(f["severity"] == "critical" for f in findings) else 0


# ---------------------------------------------------------------------------
# watch / CLI
# ---------------------------------------------------------------------------


def watch(jev: Jev, events_path: str | Path, rules: list[str], interval: float = 2.0) -> None:  # pragma: no cover - 対話用
    """ファイルを tail し、新しい行が来るたびに全体を再チェックして新規 finding だけ表示する。"""
    seen: set[str] = set()
    path = Path(events_path)
    print(f"warden watch: {path} ({len(rules)} rules) Ctrl-C で終了")
    try:
        while True:
            if path.exists():
                events = load_events(path)
                for finding in run_checks(jev, events, rules):
                    key = json.dumps(finding, sort_keys=True, ensure_ascii=False)
                    if key not in seen:
                        seen.add(key)
                        print(json.dumps(finding, ensure_ascii=False))
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab warden", description="エージェントのログからルール違反/同じ失敗/未検証完了を検出する")
    parser.add_argument("--rules", default=None, help="ルール Markdown (箇条書き)")
    parser.add_argument("--events", required=True, help="イベント JSONL")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--watch", action="store_true", help="ファイルを tail し続ける")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)
    rules = parse_rules(Path(args.rules).read_text(encoding="utf-8")) if args.rules else []
    jev = Jev(args.backend)
    if args.watch:
        watch(jev, args.events, rules, args.interval)
        return 0
    findings = run_checks(jev, load_events(args.events), rules)
    report = {"findings": findings, "critical": sum(f["severity"] == "critical" for f in findings), "jev": jev.stats()}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return exit_code(findings)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
