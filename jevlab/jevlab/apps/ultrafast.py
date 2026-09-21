"""1. jev-ultrafast: Jev が「操作」と「対象要素」を決める高速ブラウザエージェント。

元ネタ: browser-use/jev-ultrafast

- ページの対話要素を `snapshot()` で小さな索引付き行動空間にする (1 回の page.evaluate)
- Jev に 1 回の呼び出しで `operation` (click/type/scroll/...) と `target` (要素番号) を同時に聞く
- 生成が要るのは「入力欄に何を打つか」だけ。goal からの抽出ヒューリスティック → 無ければ小さな LLM (任意)
- Playwright が無くても `FakePage` で同じループをオフライン実行できる

```
jevlab ultrafast --goal "Search for jev typesafe" --url https://duckduckgo.com --headed
jevlab ultrafast --goal "..." --dry-run            # 決定を表示するだけ (Playwright 無しなら FakePage)
```
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from jevlab.core import Choice, Jev

# ---------------------------------------------------------------------------
# スナップショット (行動空間)
# ---------------------------------------------------------------------------

INTERACTIVE_SELECTOR = "a[href], button, input, select, textarea, [role=button], [role=link], [role=textbox], [role=menuitem], [role=tab], [contenteditable=true]"
MAX_CANDIDATES = 60
TEXT_LIMIT = 60

# 対話要素を列挙して data-jev-index を振り、軽い dict のリストで返す JS。
SNAPSHOT_JS = r"""
() => {
  const selector = %s;
  const out = [];
  const nodes = Array.from(document.querySelectorAll(selector));
  nodes.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
      && rect.bottom > 0 && rect.top < window.innerHeight;
    el.setAttribute('data-jev-index', String(i));
    const text = (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    out.push({
      index: i,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || (el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text') : ''),
      text: text,
      name: el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '',
      href: el.getAttribute('href') || '',
      visible: visible,
    });
  });
  return out;
}
""" % json.dumps(INTERACTIVE_SELECTOR)


def _domain(href: str) -> str:
    """フル URL は state に入れない。ドメインだけ残す。"""
    if not href or href.startswith(("#", "javascript:")):
        return ""
    parsed = urlparse(href)
    return parsed.netloc or "(same site)"


def normalize_element(raw: dict[str, Any], index: int | None = None) -> dict[str, Any]:
    text = str(raw.get("text") or "")[:TEXT_LIMIT]
    return {
        "index": int(raw.get("index", index if index is not None else 0)),
        "tag": str(raw.get("tag") or "").lower(),
        "role": str(raw.get("role") or ""),
        "text": text,
        "name": str(raw.get("name") or "")[:TEXT_LIMIT],
        "href_domain": _domain(str(raw.get("href") or "")),
        "visible": bool(raw.get("visible", True)),
    }


def snapshot(page: Any) -> list[dict[str, Any]]:
    """ページの対話要素を索引付きの小さな辞書リストにする (page.evaluate 1 回)。"""
    raw = page.evaluate(SNAPSHOT_JS) or []
    return [normalize_element(item, i) for i, item in enumerate(raw)]


def describe(element: dict[str, Any]) -> str:
    """Jev の criteria に載せる 1 行説明。"""
    label = element["text"] or element["name"] or "(no text)"
    parts = [element["tag"]]
    if element["role"] and element["role"] != element["tag"]:
        parts.append(element["role"])
    desc = f"{' '.join(parts)} '{label}'"
    if element["name"] and element["name"] != label:
        desc += f" [{element['name']}]"
    if element["href_domain"]:
        desc += f" -> {element['href_domain']}"
    if not element["visible"]:
        desc += " (offscreen)"
    return desc


def candidates(elements: list[dict[str, Any]], limit: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
    """見えている要素を優先して上限件数まで絞る。"""
    visible = [e for e in elements if e["visible"]]
    hidden = [e for e in elements if not e["visible"]]
    return (visible + hidden)[:limit]


# ---------------------------------------------------------------------------
# Jev による判断
# ---------------------------------------------------------------------------

OPERATIONS: dict[str, str] = {
    "click": "対象要素をクリックする (リンク・ボタン・チェックボックス)",
    "type": "対象の入力欄に文字を打ち込む (検索語・メール・フォーム)",
    "scroll_down": "下にスクロールして続きを見る",
    "scroll_up": "上にスクロールして戻る",
    "go_back": "ブラウザの戻るで前のページへ",
    "done": "目標はすでに達成された",
    "give_up": "このページからは目標を達成できない",
}
NEEDS_TARGET = {"click", "type"}


@dataclass
class Step:
    operation: str
    target: int | None
    confidence: float
    target_confidence: float
    description: str = ""
    text: str | None = None
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target": self.target,
            "description": self.description,
            "text": self.text,
            "outcome": self.outcome,
            "confidence": round(self.confidence, 3),
        }


def choose(jev: Jev, goal: str, elements: list[dict[str, Any]], history: list[dict[str, Any]], page_title: str = "", page_host: str = "") -> Step:
    """1 回の Jev 呼び出しで operation と target を同時に決める。"""
    cands = candidates(elements)
    state = {
        "goal": goal,
        "page": {"title": page_title[:80], "host": page_host},
        "elements": [{"i": e["index"], "d": describe(e)} for e in cands],
        "history": history[-5:],
    }
    target_criteria = {str(e["index"]): describe(e) for e in cands} or {"none": "対象要素なし"}
    decision = jev.decide(
        state,
        {
            "operation": Choice(OPERATIONS, "目標に最も近づく次の 1 手は? 履歴で同じ手を繰り返しているなら別の手を選ぶ"),
            "target": Choice(target_criteria, "その操作の対象として最も適切な要素は? (click/type 以外なら無視される)"),
        },
    )
    op = decision.choice("operation")
    tgt = decision.choice("target")
    target = int(tgt.choice) if tgt.choice.isdigit() else None
    by_index = {e["index"]: e for e in cands}
    element = by_index.get(target) if target is not None else None
    return Step(
        operation=op.choice,
        target=target if op.choice in NEEDS_TARGET else None,
        confidence=op.confidence,
        target_confidence=tgt.confidence,
        description=describe(element) if element else "",
    )


# ---------------------------------------------------------------------------
# 入力テキストの生成 (唯一 LLM が要りうる箇所)
# ---------------------------------------------------------------------------


class TextGenerator(Protocol):
    def __call__(self, goal: str, element: dict[str, Any], history: list[dict[str, Any]]) -> str: ...


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_QUOTED_RE = re.compile(r"[\"“「']([^\"”」']{1,80})[\"”」']")
_SEARCH_RE = re.compile(r"(?:search(?: for)?|look up|find|type|enter|input)\s+(.+?)(?:\s+(?:on|in|at|using|into)\s+\S+)?\s*$", re.IGNORECASE)
_JA_SEARCH_RE = re.compile(r"(.+?)(?:を|で)\s*(?:検索|入力|探)")


def heuristic_text(goal: str, element: dict[str, Any], history: list[dict[str, Any]] | None = None) -> str:
    """LLM なしで goal から打ち込む文字列を推定する。"""
    hint = f"{element.get('name', '')} {element.get('role', '')} {element.get('text', '')}".lower()
    if "mail" in hint:
        found = _EMAIL_RE.search(goal)
        if found:
            return found.group(0)
    if "date" in hint or element.get("role") == "date":
        found = _DATE_RE.search(goal)
        if found:
            return found.group(0)
    quoted = _QUOTED_RE.search(goal)
    if quoted:
        return quoted.group(1).strip()
    ja = _JA_SEARCH_RE.search(goal)
    if ja:
        return ja.group(1).strip()
    found = _SEARCH_RE.search(goal)
    if found:
        return found.group(1).strip().rstrip(".。")
    return goal.strip()


class AnthropicTextGenerator:
    """ANTHROPIC_API_KEY があれば Messages API (urllib, 遅延) で短い入力文字列を作る。失敗時はヒューリスティックへ。"""

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("JEVLAB_TEXT_MODEL", "claude-haiku-4-5")
        self.timeout = timeout

    def __call__(self, goal: str, element: dict[str, Any], history: list[dict[str, Any]]) -> str:
        if not self.api_key:
            return heuristic_text(goal, element, history)
        import urllib.request

        prompt = (
            "You control a web browser. Reply with ONLY the exact text to type into the field, no quotes.\n"
            f"Goal: {goal}\nField: {describe(element)}\nRecent steps: {json.dumps(history[-3:], ensure_ascii=False)}"
        )
        body = json.dumps({"model": self.model, "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        request = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("x-api-key", self.api_key)
        request.add_header("anthropic-version", "2023-06-01")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
            return text.strip().strip('"') or heuristic_text(goal, element, history)
        except Exception as error:  # noqa: BLE001 - ネットワーク失敗は必ずヒューリスティックへ落とす
            print(f"[ultrafast] text LLM failed, falling back to heuristics: {error}", file=sys.stderr)
            return heuristic_text(goal, element, history)


def default_text_generator() -> TextGenerator:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicTextGenerator()
    return heuristic_text


def text_for_field(goal: str, element: dict[str, Any], history: list[dict[str, Any]] | None = None, generator: TextGenerator | None = None) -> str:
    generator = generator or default_text_generator()
    return generator(goal, element, list(history or []))


# ---------------------------------------------------------------------------
# オフライン用 FakePage (Playwright Page の極小サブセット)
# ---------------------------------------------------------------------------

_INDEX_SELECTOR_RE = re.compile(r"data-jev-index=['\"]?(\d+)")


class _FakeMouse:
    def __init__(self, page: "FakePage"):
        self.page = page

    def wheel(self, dx: float, dy: float) -> None:
        self.page.scroll_y = max(0, self.page.scroll_y + dy)
        self.page.actions.append(("wheel", dy))


class FakePage:
    """`scenes` = {url: {"title": str, "elements": [...], "on": {"click:3": url, "fill:1": url}}}。

    要素は snapshot と同じ dict (index/tag/role/text/name/href/visible)。`on` で遷移先を書く。
    """

    def __init__(self, scenes: dict[str, dict[str, Any]], start_url: str | None = None):
        self.scenes = scenes
        self.url = start_url or next(iter(scenes))
        self.history: list[str] = []
        self.actions: list[tuple[str, Any]] = []
        self.scroll_y = 0
        self.mouse = _FakeMouse(self)
        self.filled: dict[int, str] = {}

    @property
    def scene(self) -> dict[str, Any]:
        return self.scenes.get(self.url, {"title": "(missing)", "elements": []})

    def title(self) -> str:
        return str(self.scene.get("title", ""))

    def evaluate(self, script: str) -> list[dict[str, Any]]:
        return [dict(e, index=i) for i, e in enumerate(self.scene.get("elements", []))]

    def goto(self, url: str) -> None:
        self.history.append(self.url)
        self.url = url
        self.actions.append(("goto", url))

    def go_back(self) -> None:
        if self.history:
            self.url = self.history.pop()
        self.actions.append(("back", self.url))

    def _transition(self, key: str) -> None:
        target = self.scene.get("on", {}).get(key)
        if target:
            self.goto(target)

    def click(self, selector: str) -> None:
        index = self._index(selector)
        self.actions.append(("click", index))
        self._transition(f"click:{index}")

    def fill(self, selector: str, text: str) -> None:
        index = self._index(selector)
        self.filled[index] = text
        self.actions.append(("fill", (index, text)))
        self._transition(f"fill:{index}")

    def press(self, selector: str, key: str) -> None:
        index = self._index(selector)
        self.actions.append(("press", (index, key)))
        self._transition(f"press:{index}")

    @staticmethod
    def _index(selector: str) -> int:
        found = _INDEX_SELECTOR_RE.search(selector)
        if not found:
            raise ValueError(f"FakePage は data-jev-index セレクタのみ対応: {selector}")
        return int(found.group(1))


DEMO_SCENES: dict[str, dict[str, Any]] = {
    "https://example.test/": {
        "title": "Example Search",
        "elements": [
            {"tag": "a", "text": "Home", "href": "/"},
            {"tag": "input", "role": "search", "name": "Search the web"},
            {"tag": "button", "text": "Search"},
            {"tag": "a", "text": "Sign in", "href": "https://accounts.example.test/login"},
        ],
        "on": {"press:1": "https://example.test/results", "click:2": "https://example.test/results"},
    },
    "https://example.test/results": {
        "title": "Results",
        "elements": [
            {"tag": "a", "text": "Jev: typed decisions for agents", "href": "https://typesafe.ai/jev"},
            {"tag": "a", "text": "Unrelated ad", "href": "https://ads.example.test/x"},
            {"tag": "a", "text": "Next page", "href": "/results?p=2", "visible": False},
        ],
        "on": {"click:0": "https://typesafe.ai/jev"},
    },
    "https://typesafe.ai/jev": {"title": "Jev - TypeSafe", "elements": [{"tag": "a", "text": "Docs", "href": "/docs"}]},
}


# ---------------------------------------------------------------------------
# エージェント本体
# ---------------------------------------------------------------------------


def selector_for(index: int) -> str:
    return f"[data-jev-index='{index}']"


@dataclass
class RunResult:
    status: str  # done | give_up | max_steps | low_confidence
    steps: list[Step] = field(default_factory=list)
    final_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "final_url": self.final_url, "steps": [s.to_dict() for s in self.steps]}


class Agent:
    def __init__(
        self,
        jev: Jev,
        page: Any,
        text_generator: TextGenerator | None = None,
        min_confidence: float = 0.3,
        max_low_confidence: int = 3,
        dry_run: bool = False,
        log: Callable[[str], None] | None = None,
    ):
        self.jev = jev
        self.page = page
        self.text_generator = text_generator or default_text_generator()
        self.min_confidence = min_confidence
        self.max_low_confidence = max_low_confidence
        self.dry_run = dry_run
        self.log = log or (lambda line: print(line))
        self.history: list[dict[str, Any]] = []

    def execute(self, step: Step, elements: list[dict[str, Any]], goal: str) -> str:
        """1 手を実行して結果の短い文字列を返す。dry_run では何もしない。"""
        by_index = {e["index"]: e for e in elements}
        if step.operation in NEEDS_TARGET and (step.target is None or step.target not in by_index):
            return "no target element"
        if self.dry_run:
            if step.operation == "type":
                step.text = text_for_field(goal, by_index[step.target], self.history, self.text_generator)  # type: ignore[index]
            return "dry-run"
        page = self.page
        try:
            if step.operation == "click":
                page.click(selector_for(step.target))  # type: ignore[arg-type]
                return "clicked"
            if step.operation == "type":
                element = by_index[step.target]  # type: ignore[index]
                step.text = text_for_field(goal, element, self.history, self.text_generator)
                page.fill(selector_for(step.target), step.text)  # type: ignore[arg-type]
                page.press(selector_for(step.target), "Enter")  # type: ignore[arg-type]
                return f"typed {step.text[:40]!r}"
            if step.operation == "scroll_down":
                page.mouse.wheel(0, 600)
                return "scrolled down"
            if step.operation == "scroll_up":
                page.mouse.wheel(0, -600)
                return "scrolled up"
            if step.operation == "go_back":
                page.go_back()
                return "went back"
        except Exception as error:  # noqa: BLE001 - 失敗は履歴に残して次の手に活かす
            return f"error: {type(error).__name__}"
        return "noop"

    def run(self, goal: str, start_url: str | None = None, max_steps: int = 15) -> RunResult:
        if start_url and not self.dry_run:
            self.page.goto(start_url)
        result = RunResult(status="max_steps")
        low_streak = 0
        for number in range(1, max_steps + 1):
            elements = snapshot(self.page)
            host = urlparse(str(getattr(self.page, "url", ""))).netloc
            step = choose(self.jev, goal, elements, self.history, self.page.title(), host)
            if step.operation in {"done", "give_up"}:
                step.outcome = step.operation
                result.steps.append(step)
                self.log(f"[{number}] {step.operation} (conf {step.confidence:.2f})")
                result.status = step.operation
                break
            if step.confidence < self.min_confidence:
                low_streak += 1
                step.outcome = "skipped: low confidence"
            else:
                low_streak = 0
                step.outcome = self.execute(step, elements, goal)
            result.steps.append(step)
            self.history.append({"op": step.operation, "target": step.description, "outcome": step.outcome})
            self.log(f"[{number}] {step.operation} {step.description} -> {step.outcome} (conf {step.confidence:.2f})")
            if low_streak >= self.max_low_confidence:
                result.status = "low_confidence"
                break
        result.final_url = str(getattr(self.page, "url", ""))
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def open_playwright_page(url: str | None, headless: bool) -> tuple[Any, Callable[[], None]]:
    """Playwright を遅延 import。無ければ分かりやすいエラー。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("playwright が必要です: pip install 'jevlab[browser]' && playwright install chromium") from error
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    page = browser.new_page()
    if url:
        page.goto(url)

    def close() -> None:
        browser.close()
        pw.stop()

    return page, close


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab ultrafast", description="Jev が操作と対象要素を選ぶブラウザエージェント")
    parser.add_argument("--goal", required=True, help="達成したいこと (例: 'Search for jev typesafe')")
    parser.add_argument("--url", default=None, help="開始 URL")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe/openrouter/mock)")
    parser.add_argument("--dry-run", action="store_true", help="決定を表示するだけ。Playwright が無ければ FakePage を使う")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力")
    args = parser.parse_args(argv)

    jev = Jev(args.backend, cache=True)
    close: Callable[[], None] = lambda: None
    try:
        page, close = open_playwright_page(args.url, args.headless)
    except RuntimeError as error:
        if not args.dry_run:
            print(str(error), file=sys.stderr)
            return 2
        print(f"[ultrafast] {error}\n[ultrafast] dry-run: 内蔵 FakePage を使います", file=sys.stderr)
        page = FakePage(DEMO_SCENES, args.url if args.url in DEMO_SCENES else None)
    try:
        agent = Agent(jev, page, dry_run=args.dry_run, min_confidence=args.min_confidence)
        result = agent.run(args.goal, start_url=None, max_steps=args.max_steps)
    finally:
        close()
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"status={result.status} steps={len(result.steps)} url={result.final_url} jev={jev.stats()}")
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
