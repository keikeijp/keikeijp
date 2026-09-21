"""3. mobile-jev: adb 接続の Android 端末を Jev が操作する。

元ネタ: droidrun/mobil-jev

- `AdbDevice` : `adb shell uiautomator dump` → XML 階層を xml.etree で要素リストにし、`adb shell input` で tap/swipe/text/keyevent
- `FakeDevice` : XML 文字列/ファイルから階層を作るオフライン実装 (実機・adb 不要)
- Jev が `action` (tap/type/swipe_up/swipe_down/back/home/wait/done/give_up) と `target` (要素番号) を 1 回で決める
- `--log` で JSONL 実行ログ、`--web` で http.server の小さな状態ページ (フラグ指定時のみ起動)

```
jevlab mobile --task "Open settings and turn on Wi-Fi" --serial emulator-5554 --execute
jevlab mobile --task "..." --hierarchy-xml dump.xml --log run.jsonl     # オフライン dry-run
```
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from jevlab.core import Choice, Jev

Element = dict[str, Any]
MAX_CANDIDATES = 60
TEXT_LIMIT = 60
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# ---------------------------------------------------------------------------
# uiautomator XML の解析
# ---------------------------------------------------------------------------


def parse_bounds(text: str) -> list[int]:
    found = _BOUNDS_RE.search(text or "")
    if not found:
        return [0, 0, 0, 0]
    return [int(v) for v in found.groups()]


def parse_hierarchy(xml_text: str, interactive_only: bool = True) -> list[Element]:
    """uiautomator dump の XML → {index, class, text, content_desc, resource_id, bounds, clickable} のリスト。

    interactive_only なら clickable / 入力可能 / テキスト付き要素だけ残して行動空間を小さくする。
    """
    root = ET.fromstring(xml_text)
    elements: list[Element] = []
    for node in root.iter("node"):
        attrs = node.attrib
        clickable = attrs.get("clickable") == "true"
        editable = "Edit" in attrs.get("class", "")
        text = (attrs.get("text") or "")[:TEXT_LIMIT]
        desc = (attrs.get("content-desc") or "")[:TEXT_LIMIT]
        if interactive_only and not (clickable or editable or attrs.get("scrollable") == "true" or attrs.get("checkable") == "true"):
            continue
        if attrs.get("enabled") == "false":
            continue
        elements.append(
            {
                "index": len(elements),
                "class": attrs.get("class", "").rsplit(".", 1)[-1],
                "text": text,
                "content_desc": desc,
                "resource_id": attrs.get("resource-id", "").rsplit("/", 1)[-1],
                "bounds": parse_bounds(attrs.get("bounds", "")),
                "clickable": clickable,
                "editable": editable,
                "checked": attrs.get("checked") == "true",
            }
        )
    return elements


def describe(element: Element) -> str:
    label = element["text"] or element["content_desc"] or element["resource_id"] or "(no label)"
    desc = f"{element['class']} '{label}'"
    if element["resource_id"] and element["resource_id"] != label:
        desc += f" #{element['resource_id']}"
    if element.get("checked"):
        desc += " [checked]"
    return desc


def center(element: Element) -> tuple[int, int]:
    x0, y0, x1, y1 = element["bounds"]
    return (x0 + x1) // 2, (y0 + y1) // 2


# ---------------------------------------------------------------------------
# デバイス
# ---------------------------------------------------------------------------


class Device(Protocol):
    def hierarchy(self) -> list[Element]: ...
    def tap(self, x: int, y: int) -> None: ...
    def swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int = 300) -> None: ...
    def input_text(self, text: str) -> None: ...
    def keyevent(self, code: str) -> None: ...
    def screen_size(self) -> tuple[int, int]: ...


class AdbDevice:
    """adb をサブプロセスで叩く。`runner` を差し替えるとテストで adb 無しにできる。"""

    def __init__(self, serial: str | None = None, adb: str = "adb", runner: Callable[[list[str]], str] | None = None):
        self.serial = serial
        self.adb = adb
        self.runner = runner or self._run

    def _cmd(self, *args: str) -> list[str]:
        base = [self.adb] + (["-s", self.serial] if self.serial else [])
        return base + list(args)

    def _run(self, cmd: list[str]) -> str:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        except FileNotFoundError as error:
            raise RuntimeError("adb が見つかりません。Android SDK platform-tools を入れて PATH に通してください (オフラインなら --hierarchy-xml)") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"adb 失敗: {' '.join(cmd)}: {error.stderr.strip()[:200]}") from error
        return done.stdout

    def hierarchy(self) -> list[Element]:
        self.runner(self._cmd("shell", "uiautomator", "dump", "/sdcard/jevlab_ui.xml"))
        xml_text = self.runner(self._cmd("shell", "cat", "/sdcard/jevlab_ui.xml"))
        return parse_hierarchy(xml_text)

    def tap(self, x: int, y: int) -> None:
        self.runner(self._cmd("shell", "input", "tap", str(x), str(y)))

    def swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int = 300) -> None:
        self.runner(self._cmd("shell", "input", "swipe", str(x0), str(y0), str(x1), str(y1), str(duration_ms)))

    def input_text(self, text: str) -> None:
        escaped = text.replace(" ", "%s").replace("'", "\\'")  # adb input text の空白エスケープ
        self.runner(self._cmd("shell", "input", "text", escaped))

    def keyevent(self, code: str) -> None:
        self.runner(self._cmd("shell", "input", "keyevent", code))

    def screen_size(self) -> tuple[int, int]:
        out = self.runner(self._cmd("shell", "wm", "size"))
        found = re.search(r"(\d+)x(\d+)", out)
        return (int(found.group(1)), int(found.group(2))) if found else (1080, 1920)


class FakeDevice:
    """XML 文字列 (または {キー: XML} の複数画面) で動くオフライン端末。`on` で遷移: {"tap:2": "screen_b", "text": "screen_c"}。"""

    def __init__(self, screens: dict[str, str] | str, start: str | None = None, on: dict[str, dict[str, str]] | None = None, size: tuple[int, int] = (1080, 1920)):
        self.screens = {"main": screens} if isinstance(screens, str) else dict(screens)
        self.current = start or next(iter(self.screens))
        self.on = on or {}
        self.size = size
        self.actions: list[tuple[str, Any]] = []
        self.history: list[str] = []

    @classmethod
    def from_file(cls, path: str) -> "FakeDevice":
        with open(path, encoding="utf-8") as handle:
            return cls(handle.read())

    def _transition(self, key: str) -> None:
        target = self.on.get(self.current, {}).get(key)
        if target and target in self.screens:
            self.history.append(self.current)
            self.current = target

    def hierarchy(self) -> list[Element]:
        return parse_hierarchy(self.screens[self.current])

    def tap(self, x: int, y: int) -> None:
        self.actions.append(("tap", (x, y)))
        for element in self.hierarchy():
            x0, y0, x1, y1 = element["bounds"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                self._transition(f"tap:{element['index']}")
                break

    def swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int = 300) -> None:
        self.actions.append(("swipe", (x0, y0, x1, y1)))
        self._transition("swipe_up" if y1 < y0 else "swipe_down")

    def input_text(self, text: str) -> None:
        self.actions.append(("text", text))
        self._transition("text")

    def keyevent(self, code: str) -> None:
        self.actions.append(("key", code))
        if code == "KEYCODE_BACK" and self.history:
            self.current = self.history.pop()
        else:
            self._transition(f"key:{code}")

    def screen_size(self) -> tuple[int, int]:
        return self.size


SAMPLE_HIERARCHY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.android.settings" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,1920]">
    <node index="0" text="Settings" resource-id="android:id/title" class="android.widget.TextView" package="com.android.settings" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[40,80][400,160]"/>
    <node index="1" text="" resource-id="com.android.settings:id/search" class="android.widget.EditText" package="com.android.settings" content-desc="Search settings" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[40,200][1040,300]"/>
    <node index="2" text="Network &amp; internet" resource-id="android:id/title" class="android.widget.TextView" package="com.android.settings" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[40,400][1040,520]"/>
    <node index="3" text="Wi-Fi" resource-id="com.android.settings:id/switch_text" class="android.widget.Switch" package="com.android.settings" content-desc="" checkable="true" checked="false" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[40,600][1040,720]"/>
    <node index="4" text="Disabled item" resource-id="" class="android.widget.Button" package="com.android.settings" content-desc="" checkable="false" checked="false" clickable="true" enabled="false" focusable="false" scrollable="false" bounds="[40,800][1040,900]"/>
  </node>
</hierarchy>
"""

# ---------------------------------------------------------------------------
# Jev による判断とエージェント
# ---------------------------------------------------------------------------

ACTIONS: dict[str, str] = {
    "tap": "対象要素をタップ",
    "type": "対象の入力欄をタップしてテキストを入力",
    "swipe_up": "上にスワイプして下の内容を見る",
    "swipe_down": "下にスワイプして上に戻る",
    "back": "戻るキー",
    "home": "ホームキー",
    "wait": "画面の更新を待つ",
    "done": "タスクは完了した",
    "give_up": "この画面からは達成できない",
}
NEEDS_TARGET = {"tap", "type"}
_QUOTED_RE = re.compile(r"[\"“「']([^\"”」']{1,120})[\"”」']")


@dataclass
class Step:
    action: str
    target: int | None
    confidence: float
    description: str = ""
    text: str | None = None
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "target": self.target, "description": self.description, "text": self.text, "outcome": self.outcome, "confidence": round(self.confidence, 3)}


def choose(jev: Jev, task: str, elements: list[Element], history: list[dict[str, Any]]) -> Step:
    cands = elements[:MAX_CANDIDATES]
    state = {"task": task, "screen": [{"i": e["index"], "d": describe(e)} for e in cands], "history": history[-5:]}
    target_criteria = {str(e["index"]): describe(e) for e in cands} or {"none": "対象なし"}
    decision = jev.decide(
        state,
        {
            "action": Choice(ACTIONS, "タスクを進める次の 1 手は? 同じ手を繰り返して効いていなければ別の手を"),
            "target": Choice(target_criteria, "その操作の対象要素は? (tap/type 以外なら無視)"),
        },
    )
    action, target = decision.choice("action"), decision.choice("target")
    index = int(target.choice) if target.choice.isdigit() else None
    element = next((e for e in cands if e["index"] == index), None)
    return Step(
        action=action.choice,
        target=index if action.choice in NEEDS_TARGET and element else None,
        confidence=action.confidence,
        description=describe(element) if element and action.choice in NEEDS_TARGET else "",
    )


def text_for_task(task: str) -> str:
    quoted = _QUOTED_RE.search(task)
    return quoted.group(1) if quoted else task


@dataclass
class RunResult:
    status: str
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "steps": [s.to_dict() for s in self.steps]}


class Agent:
    def __init__(self, jev: Jev, device: Device, dry_run: bool = True, min_confidence: float = 0.3, max_low_confidence: int = 3, log: Callable[[str], None] | None = None, jsonl_path: str | None = None):
        self.jev = jev
        self.device = device
        self.dry_run = dry_run
        self.min_confidence = min_confidence
        self.max_low_confidence = max_low_confidence
        self.log = log or (lambda line: print(line))
        self.jsonl_path = jsonl_path
        self.history: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {"task": "", "step": 0, "last": None, "status": "idle"}

    def _record(self, step: Step, number: int) -> None:
        self.status.update({"step": number, "last": step.to_dict()})
        if self.jsonl_path:
            with open(self.jsonl_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": time.time(), "step": number, **step.to_dict()}, ensure_ascii=False) + "\n")

    def execute(self, step: Step, elements: list[Element], task: str) -> str:
        by_index = {e["index"]: e for e in elements}
        element = by_index.get(step.target) if step.target is not None else None
        if step.action in NEEDS_TARGET and element is None:
            return "no target element"
        if step.action == "type":
            step.text = text_for_task(task)
        if self.dry_run:
            return "dry-run"
        width, height = self.device.screen_size()
        try:
            if step.action == "tap":
                self.device.tap(*center(element))  # type: ignore[arg-type]
                return "tapped"
            if step.action == "type":
                self.device.tap(*center(element))  # type: ignore[arg-type]
                self.device.input_text(step.text or "")
                return f"typed {str(step.text)[:40]!r}"
            if step.action == "swipe_up":
                self.device.swipe(width // 2, int(height * 0.7), width // 2, int(height * 0.3))
                return "swiped up"
            if step.action == "swipe_down":
                self.device.swipe(width // 2, int(height * 0.3), width // 2, int(height * 0.7))
                return "swiped down"
            if step.action == "back":
                self.device.keyevent("KEYCODE_BACK")
                return "back"
            if step.action == "home":
                self.device.keyevent("KEYCODE_HOME")
                return "home"
            if step.action == "wait":
                time.sleep(1.0)
                return "waited"
        except Exception as error:  # noqa: BLE001
            return f"error: {type(error).__name__}"
        return "noop"

    def run(self, task: str, max_steps: int = 10) -> RunResult:
        result = RunResult(status="max_steps")
        self.status.update({"task": task, "status": "running"})
        low_streak = 0
        for number in range(1, max_steps + 1):
            elements = self.device.hierarchy()
            step = choose(self.jev, task, elements, self.history)
            if step.action in {"done", "give_up"}:
                step.outcome = step.action
                result.steps.append(step)
                result.status = step.action
                self._record(step, number)
                self.log(f"[{number}] {step.action} (conf {step.confidence:.2f})")
                break
            if step.confidence < self.min_confidence:
                low_streak += 1
                step.outcome = "skipped: low confidence"
            else:
                low_streak = 0
                step.outcome = self.execute(step, elements, task)
            result.steps.append(step)
            self.history.append({"action": step.action, "target": step.description, "outcome": step.outcome})
            self._record(step, number)
            self.log(f"[{number}] {step.action} {step.description} -> {step.outcome} (conf {step.confidence:.2f})")
            if low_streak >= self.max_low_confidence:
                result.status = "low_confidence"
                break
        self.status["status"] = result.status
        return result


# ---------------------------------------------------------------------------
# 状態ページ (フラグ指定時のみ)
# ---------------------------------------------------------------------------


def start_status_server(agent: Agent, port: int) -> threading.Thread:
    """`--web` 指定時だけ呼ぶ。GET / で agent.status を JSON で返す小さな http.server。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(agent.status, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # 標準出力を汚さない
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[mobile] status page: http://127.0.0.1:{port}/", file=sys.stderr)
    return thread


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab mobile", description="adb 経由で Android 端末を Jev が操作 (既定は dry-run)")
    parser.add_argument("--task", required=True)
    parser.add_argument("--serial", default=None, help="adb -s に渡す端末シリアル")
    parser.add_argument("--hierarchy-xml", default=None, help="オフライン用: uiautomator dump の XML ファイル (無指定でも adb 不在なら内蔵サンプル)")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="既定。操作せず決定だけ記録")
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="実際に adb shell input を送る")
    parser.add_argument("--log", default=None, help="JSONL 実行ログの出力先")
    parser.add_argument("--web", type=int, default=None, metavar="PORT", help="状態ページを http://127.0.0.1:PORT で公開")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    device: Device
    if args.hierarchy_xml:
        device = FakeDevice.from_file(args.hierarchy_xml)
    elif args.dry_run and not args.serial:
        print("[mobile] --serial も --hierarchy-xml も無いので内蔵サンプル画面で dry-run します", file=sys.stderr)
        device = FakeDevice(SAMPLE_HIERARCHY)
    else:
        device = AdbDevice(args.serial)
    jev = Jev(args.backend, cache=True)
    agent = Agent(jev, device, dry_run=args.dry_run, min_confidence=args.min_confidence, jsonl_path=args.log)
    if args.web:
        start_status_server(agent, args.web)
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
