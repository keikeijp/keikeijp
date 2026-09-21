"""2. typesafe-computer-use: 画面上の UI 要素一覧から Jev が「操作」と「対象」を決める PC 操作。

元ネタ: awlevin/typesafe-computer-use (macOS のアクセシビリティ木専用)。ここではクロスプラットフォームに:

- `ScreenReader` : 画面を {id, kind, text, bbox} の要素リストにする
    - `MacAccessibilityReader` : macOS の osascript (System Events) から取得 (遅延)
    - `OcrReader`              : pyautogui スクリーンショット + pytesseract OCR (遅延)
    - `StaticReader`           : JSON ファイル/リストから読む (オフライン・テスト用)
- Jev が `action` (click/double_click/type/key/scroll/wait/done) と `target` (要素 id) を 1 回で決める
- `Executor` : `PyAutoGuiExecutor` (遅延) / `DryRunExecutor` (ログのみ、既定)

```
jevlab computer-use --task "Open the Downloads folder" --screen-json screen.json          # dry-run
jevlab computer-use --task "..." --reader mac --execute                                   # 実操作 (mac)
```
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from jevlab.core import Choice, Jev

# ---------------------------------------------------------------------------
# 画面読み取り
# ---------------------------------------------------------------------------

Element = dict[str, Any]  # {id, kind, text, bbox: [x, y, w, h]}
MAX_CANDIDATES = 60
TEXT_LIMIT = 60


def normalize_element(raw: dict[str, Any], fallback_id: int) -> Element:
    bbox = raw.get("bbox") or [0, 0, 0, 0]
    return {
        "id": str(raw.get("id", fallback_id)),
        "kind": str(raw.get("kind") or raw.get("role") or "element"),
        "text": str(raw.get("text") or raw.get("title") or "")[:TEXT_LIMIT],
        "bbox": [int(v) for v in list(bbox)[:4]] + [0] * (4 - len(bbox)),
    }


class ScreenReader(Protocol):
    def read(self) -> list[Element]: ...


class StaticReader:
    """JSON ファイルまたはリストから要素を返す。`frames` を渡すと read() ごとに次の画面へ進む (遷移のシミュレーション)。"""

    def __init__(self, elements: list[dict[str, Any]] | None = None, frames: list[list[dict[str, Any]]] | None = None):
        self.frames = [list(f) for f in (frames or [elements or []])]
        self.cursor = 0

    @classmethod
    def from_file(cls, path: str) -> "StaticReader":
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "frames" in data:
            return cls(frames=data["frames"])
        return cls(elements=data if isinstance(data, list) else data.get("elements", []))

    def read(self) -> list[Element]:
        frame = self.frames[min(self.cursor, len(self.frames) - 1)]
        self.cursor += 1
        return [normalize_element(e, i) for i, e in enumerate(frame)]


_MAC_SCRIPT = """
tell application "System Events"
  set frontApp to first application process whose frontmost is true
  set out to ""
  repeat with el in (every UI element of front window of frontApp)
    try
      set {x, y} to position of el
      set {w, h} to size of el
      set out to out & (role of el) & tab & (name of el as text) & tab & x & tab & y & tab & w & tab & h & linefeed
    end try
  end repeat
  return out
end tell
"""


class MacAccessibilityReader:
    """macOS 専用。osascript で最前面ウィンドウの UI 要素を読む。"""

    def __init__(self, runner: Callable[[list[str]], str] | None = None):
        self.runner = runner or self._run

    @staticmethod
    def _run(cmd: list[str]) -> str:
        if sys.platform != "darwin":
            raise RuntimeError("MacAccessibilityReader は macOS 専用です。他 OS では --reader ocr か --screen-json を使ってください")
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True).stdout
        except FileNotFoundError as error:
            raise RuntimeError("osascript が見つかりません (macOS の System Events が必要)") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"osascript 失敗 (アクセシビリティ権限を確認): {error.stderr.strip()[:200]}") from error

    def read(self) -> list[Element]:
        text = self.runner(["osascript", "-e", _MAC_SCRIPT])
        return self.parse(text)

    @staticmethod
    def parse(text: str) -> list[Element]:
        elements: list[Element] = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            role, name = parts[0].replace("AX", "").lower(), parts[1]
            try:
                bbox = [int(float(v)) for v in parts[2:6]]
            except ValueError:
                continue
            elements.append({"id": str(len(elements)), "kind": role, "text": name[:TEXT_LIMIT], "bbox": bbox})
        return elements


class OcrReader:
    """pyautogui のスクリーンショットを pytesseract で OCR し、単語の塊を要素にする (遅延 import)。"""

    def read(self) -> list[Element]:
        try:
            import pyautogui
            import pytesseract
        except ImportError as error:
            raise RuntimeError("OCR 読み取りには pip install pyautogui pytesseract pillow と tesseract 本体が必要です") from error
        image = pyautogui.screenshot()
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        return self.group_words(data)

    @staticmethod
    def group_words(data: dict[str, list[Any]]) -> list[Element]:
        """tesseract の単語を (block, line) 単位で 1 要素にまとめる。"""
        lines: dict[tuple[int, int], dict[str, Any]] = {}
        for i, word in enumerate(data.get("text", [])):
            word = str(word).strip()
            if not word:
                continue
            key = (int(data["block_num"][i]), int(data["line_num"][i]))
            x, y, w, h = int(data["left"][i]), int(data["top"][i]), int(data["width"][i]), int(data["height"][i])
            entry = lines.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
            entry["words"].append(word)
            entry["x0"], entry["y0"] = min(entry["x0"], x), min(entry["y0"], y)
            entry["x1"], entry["y1"] = max(entry["x1"], x + w), max(entry["y1"], y + h)
        elements: list[Element] = []
        for entry in lines.values():
            elements.append({"id": str(len(elements)), "kind": "text", "text": " ".join(entry["words"])[:TEXT_LIMIT], "bbox": [entry["x0"], entry["y0"], entry["x1"] - entry["x0"], entry["y1"] - entry["y0"]]})
        return elements


# ---------------------------------------------------------------------------
# 実行器
# ---------------------------------------------------------------------------


class Executor(Protocol):
    def click(self, x: int, y: int, double: bool = False) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, combo: str) -> None: ...
    def scroll(self, amount: int) -> None: ...
    def wait(self, seconds: float) -> None: ...


class DryRunExecutor:
    """何もせず記録するだけ。既定。"""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    def click(self, x: int, y: int, double: bool = False) -> None:
        self.log.append(("double_click" if double else "click", (x, y)))

    def type_text(self, text: str) -> None:
        self.log.append(("type", text))

    def key(self, combo: str) -> None:
        self.log.append(("key", combo))

    def scroll(self, amount: int) -> None:
        self.log.append(("scroll", amount))

    def wait(self, seconds: float) -> None:
        self.log.append(("wait", seconds))


class PyAutoGuiExecutor:
    """pyautogui で実際に操作する (遅延 import)。"""

    def __init__(self) -> None:
        try:
            import pyautogui
        except ImportError as error:
            raise RuntimeError("実操作には pip install pyautogui が必要です") from error
        pyautogui.FAILSAFE = True  # マウスを左上隅に飛ばすと中断
        self.gui = pyautogui

    def click(self, x: int, y: int, double: bool = False) -> None:
        (self.gui.doubleClick if double else self.gui.click)(x, y)

    def type_text(self, text: str) -> None:
        self.gui.typewrite(text, interval=0.02)

    def key(self, combo: str) -> None:
        self.gui.hotkey(*[k.strip() for k in combo.split("+") if k.strip()])

    def scroll(self, amount: int) -> None:
        self.gui.scroll(amount)

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Jev による判断
# ---------------------------------------------------------------------------

ACTIONS: dict[str, str] = {
    "click": "対象要素を 1 回クリック (ボタン・リンク・入力欄にフォーカス)",
    "double_click": "対象要素をダブルクリック (ファイル/フォルダを開く)",
    "type": "フォーカス中の入力欄にテキストを打つ",
    "key": "キーボードショートカット (enter / cmd+space / ctrl+l など)",
    "scroll": "画面をスクロールして続きを見る",
    "wait": "画面の更新を待つ",
    "done": "タスクは完了した",
}
NEEDS_TARGET = {"click", "double_click"}


def describe(element: Element) -> str:
    x, y, w, h = element["bbox"]
    label = element["text"] or "(no text)"
    return f"{element['kind']} '{label}' @({x},{y}) {w}x{h}"


def center(element: Element) -> tuple[int, int]:
    x, y, w, h = element["bbox"]
    return x + w // 2, y + h // 2


@dataclass
class Step:
    action: str
    target: str | None
    confidence: float
    description: str = ""
    argument: str | None = None
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "target": self.target, "description": self.description, "argument": self.argument, "outcome": self.outcome, "confidence": round(self.confidence, 3)}


def choose(jev: Jev, task: str, elements: list[Element], history: list[dict[str, Any]]) -> Step:
    cands = elements[:MAX_CANDIDATES]
    state = {"task": task, "elements": [{"id": e["id"], "d": describe(e)} for e in cands], "history": history[-5:]}
    target_criteria = {e["id"]: describe(e) for e in cands} or {"none": "対象要素なし"}
    decision = jev.decide(
        state,
        {
            "action": Choice(ACTIONS, "タスクを進める次の 1 手は? 直前と同じ手が効いていなければ別の手を"),
            "target": Choice(target_criteria, "その操作の対象要素は? (click/double_click 以外なら無視)"),
        },
    )
    action, target = decision.choice("action"), decision.choice("target")
    by_id = {e["id"]: e for e in cands}
    element = by_id.get(target.choice)
    return Step(
        action=action.choice,
        target=target.choice if action.choice in NEEDS_TARGET and element else None,
        confidence=action.confidence,
        description=describe(element) if element and action.choice in NEEDS_TARGET else "",
    )


_QUOTED_RE = re.compile(r"[\"“「']([^\"”」']{1,120})[\"”」']")
_KEY_RE = re.compile(r"\b(?:press|hit|push)\s+([a-z0-9+]+)", re.IGNORECASE)


def argument_for(step: Step, task: str) -> str | None:
    """type/key/scroll/wait の引数を task から簡単に推定する (LLM 不要)。"""
    if step.action == "type":
        quoted = _QUOTED_RE.search(task)
        return quoted.group(1) if quoted else task
    if step.action == "key":
        found = _KEY_RE.search(task)
        return found.group(1).lower() if found else "enter"
    if step.action == "scroll":
        return "-5"
    if step.action == "wait":
        return "1.0"
    return None


# ---------------------------------------------------------------------------
# エージェント
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    status: str  # done | max_steps | low_confidence
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "steps": [s.to_dict() for s in self.steps]}


class Agent:
    def __init__(self, jev: Jev, reader: ScreenReader, executor: Executor | None = None, min_confidence: float = 0.3, max_low_confidence: int = 3, log: Callable[[str], None] | None = None):
        self.jev = jev
        self.reader = reader
        self.executor = executor or DryRunExecutor()
        self.min_confidence = min_confidence
        self.max_low_confidence = max_low_confidence
        self.log = log or (lambda line: print(line))
        self.history: list[dict[str, Any]] = []

    def execute(self, step: Step, elements: list[Element]) -> str:
        by_id = {e["id"]: e for e in elements}
        ex = self.executor
        try:
            if step.action in NEEDS_TARGET:
                element = by_id.get(step.target or "")
                if element is None:
                    return "no target element"
                x, y = center(element)
                ex.click(x, y, double=step.action == "double_click")
                return f"{step.action} at ({x},{y})"
            if step.action == "type":
                ex.type_text(step.argument or "")
                return f"typed {str(step.argument)[:40]!r}"
            if step.action == "key":
                ex.key(step.argument or "enter")
                return f"key {step.argument}"
            if step.action == "scroll":
                ex.scroll(int(step.argument or -5))
                return "scrolled"
            if step.action == "wait":
                ex.wait(float(step.argument or 1.0))
                return "waited"
        except Exception as error:  # noqa: BLE001
            return f"error: {type(error).__name__}"
        return "noop"

    def run(self, task: str, max_steps: int = 10) -> RunResult:
        result = RunResult(status="max_steps")
        low_streak = 0
        for number in range(1, max_steps + 1):
            elements = self.reader.read()
            step = choose(self.jev, task, elements, self.history)
            if step.action == "done":
                step.outcome = "done"
                result.steps.append(step)
                result.status = "done"
                self.log(f"[{number}] done (conf {step.confidence:.2f})")
                break
            step.argument = argument_for(step, task)
            if step.confidence < self.min_confidence:
                low_streak += 1
                step.outcome = "skipped: low confidence"
            else:
                low_streak = 0
                step.outcome = self.execute(step, elements)
            result.steps.append(step)
            self.history.append({"action": step.action, "target": step.description, "outcome": step.outcome})
            self.log(f"[{number}] {step.action} {step.description} -> {step.outcome} (conf {step.confidence:.2f})")
            if low_streak >= self.max_low_confidence:
                result.status = "low_confidence"
                break
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEMO_SCREEN = {
    "frames": [
        [
            {"id": "finder", "kind": "application", "text": "Finder", "bbox": [10, 5, 60, 20]},
            {"id": "downloads", "kind": "folder", "text": "Downloads", "bbox": [40, 120, 80, 80]},
            {"id": "documents", "kind": "folder", "text": "Documents", "bbox": [140, 120, 80, 80]},
            {"id": "search", "kind": "textfield", "text": "Search", "bbox": [600, 40, 200, 24]},
        ],
        [
            {"id": "title", "kind": "text", "text": "Downloads", "bbox": [10, 5, 120, 20]},
            {"id": "file1", "kind": "file", "text": "report.pdf", "bbox": [40, 120, 80, 80]},
        ],
    ]
}


def build_reader(name: str, screen_json: str | None) -> ScreenReader:
    if screen_json:
        return StaticReader.from_file(screen_json)
    if name == "mac":
        return MacAccessibilityReader()
    if name == "ocr":
        return OcrReader()
    return StaticReader(frames=DEMO_SCREEN["frames"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab computer-use", description="画面要素一覧から Jev が操作を決める PC 操作 (既定は dry-run)")
    parser.add_argument("--task", required=True)
    parser.add_argument("--screen-json", default=None, help="オフライン用の画面 JSON ([{id,kind,text,bbox}] または {frames: [...]})")
    parser.add_argument("--reader", choices=["static", "mac", "ocr"], default="static", help="画面の読み方 (--screen-json があればそれを優先)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="既定。操作を記録するだけ")
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="pyautogui で実際に操作する")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        reader = build_reader(args.reader, args.screen_json)
        executor: Executor = DryRunExecutor() if args.dry_run else PyAutoGuiExecutor()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    jev = Jev(args.backend, cache=True)
    agent = Agent(jev, reader, executor, min_confidence=args.min_confidence)
    try:
        result = agent.run(args.task, max_steps=args.max_steps)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"status={result.status} steps={len(result.steps)} dry_run={args.dry_run} jev={jev.stats()}")
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
