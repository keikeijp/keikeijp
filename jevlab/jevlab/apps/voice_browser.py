"""4. jev-voice-browser: 音声 → 意図抽出 (Jev) → Playwright 操作。

元ネタ: moritzkremb/jev-voice-browser

パイプライン:
1. `transcribe(source)` : マイク/音声ファイル → 文字起こし (SpeechRecognition / whisper を遅延 import)。`--text` ならそのまま
2. `extract_intent(jev, utterance, snapshot)` : Jev が intent (navigate/search/click_link/scroll/read_aloud/go_back/stop) と
   `target` (ページ要素) を 1 回で決め、URL / 検索語は正規表現のスロット抽出で埋める (生成 LLM 不要)
3. `execute(page, intent)` : `jevlab.apps.ultrafast` と同じ Page 抽象 (Playwright / FakePage) で実行

```
jevlab voice-browser --text "search for jev typesafe" --url https://duckduckgo.com --headed
jevlab voice-browser --mic --dry-run
jevlab voice-browser --text "open example.com" --dry-run          # Playwright 無しなら FakePage
```
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from jevlab.core import Choice, Jev

from jevlab.apps.ultrafast import DEMO_SCENES, FakePage, candidates, describe, open_playwright_page, selector_for, snapshot

# ---------------------------------------------------------------------------
# 文字起こし
# ---------------------------------------------------------------------------


def transcribe(source: str = "mic", language: str = "en-US") -> str:
    """`mic` ならマイク、それ以外は音声ファイルのパス。SpeechRecognition → whisper の順に試す (遅延 import)。"""
    try:
        import speech_recognition as sr
    except ImportError:
        sr = None  # type: ignore[assignment]
    if sr is not None:
        recognizer = sr.Recognizer()
        if source == "mic":
            with sr.Microphone() as mic:
                recognizer.adjust_for_ambient_noise(mic, duration=0.5)
                print("[voice] listening...", file=sys.stderr)
                audio = recognizer.listen(mic, timeout=10, phrase_time_limit=15)
        else:
            with sr.AudioFile(source) as handle:
                audio = recognizer.record(handle)
        return str(recognizer.recognize_google(audio, language=language))  # 外部送信あり (docs 参照)
    try:
        import whisper  # openai-whisper
    except ImportError as error:
        raise RuntimeError("音声入力には pip install 'jevlab[voice]' pyaudio (または openai-whisper) が必要です。オフラインなら --text を使ってください") from error
    if source == "mic":
        raise RuntimeError("whisper ではマイク入力を扱えません。音声ファイルを渡すか SpeechRecognition を入れてください")
    model = whisper.load_model("base")
    return str(model.transcribe(source)["text"])


# ---------------------------------------------------------------------------
# 意図抽出
# ---------------------------------------------------------------------------

INTENTS: dict[str, str] = {
    "navigate": "指定された URL やサイト名のページを開く (open / go to / ...を開いて)",
    "search": "検索語を検索欄に入れて検索する (search for / look up / ...を検索)",
    "click_link": "ページ上のリンクやボタンを押す (click / press / open the first result / ...を押して)",
    "scroll": "スクロールする (scroll down / up / 下へ)",
    "read_aloud": "ページの内容を読み上げる (read / what does it say / 読んで)",
    "go_back": "前のページに戻る (go back / 戻って)",
    "stop": "終了する (stop / quit / 終わり)",
}
NEEDS_TARGET = {"click_link"}

_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", re.IGNORECASE)
_SEARCH_RE = re.compile(r"(?:search(?: for)?|look up|google|find)\s+(.+?)(?:\s+(?:on|in|at)\s+\S+)?\s*[.。]?$", re.IGNORECASE)
_JA_SEARCH_RE = re.compile(r"(.+?)(?:を|で)\s*(?:検索|調べ)")
_SCROLL_UP_RE = re.compile(r"\b(?:up|top)\b|上", re.IGNORECASE)
_SITE_WORDS = {"open", "go", "to", "navigate", "visit", "the", "site", "website", "please", "を", "開いて", "に", "行って"}


def fill_slots(intent: str, utterance: str) -> dict[str, Any]:
    """意図ごとの引数を正規表現で埋める。LLM は使わない。"""
    slots: dict[str, Any] = {}
    if intent == "navigate":
        found = _URL_RE.search(utterance)
        if found:
            url = found.group(0)
            slots["url"] = url if url.lower().startswith("http") else f"https://{url}"
        else:
            words = [w for w in re.findall(r"[\w.-]+", utterance) if w.lower() not in _SITE_WORDS]
            if words:
                slots["url"] = f"https://{words[-1].lower()}.com"
    elif intent == "search":
        ja = _JA_SEARCH_RE.search(utterance)
        en = _SEARCH_RE.search(utterance)
        if ja:
            slots["query"] = ja.group(1).strip()
        elif en:
            slots["query"] = en.group(1).strip()
        else:
            slots["query"] = utterance.strip()
    elif intent == "scroll":
        slots["direction"] = "up" if _SCROLL_UP_RE.search(utterance) else "down"
    return slots


@dataclass
class Intent:
    name: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    target: int | None = None
    target_description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.name, "confidence": round(self.confidence, 3), "slots": self.slots, "target": self.target, "target_description": self.target_description}


def extract_intent(jev: Jev, utterance: str, elements: list[dict[str, Any]], page_title: str = "") -> Intent:
    cands = candidates(elements)
    state = {"utterance": utterance, "page_title": page_title[:80], "elements": [{"i": e["index"], "d": describe(e)} for e in cands]}
    target_criteria = {str(e["index"]): describe(e) for e in cands} or {"none": "対象なし"}
    decision = jev.decide(
        state,
        {
            "intent": Choice(INTENTS, "発話が求めているブラウザ操作は?"),
            "target": Choice(target_criteria, "click_link の場合、発話が指している要素は? (それ以外は無視)"),
        },
    )
    intent, target = decision.choice("intent"), decision.choice("target")
    index = int(target.choice) if target.choice.isdigit() else None
    element = next((e for e in cands if e["index"] == index), None)
    result = Intent(name=intent.choice, confidence=intent.confidence, slots=fill_slots(intent.choice, utterance))
    if intent.choice in NEEDS_TARGET and element is not None:
        result.target, result.target_description = index, describe(element)
    return result


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------


def _search_box(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    for element in elements:
        hint = f"{element['role']} {element['name']} {element['text']}".lower()
        if element["tag"] in {"input", "textarea"} and ("search" in hint or "検索" in hint or element["role"] in {"text", "search", "textbox"}):
            return element
    return None


def execute(page: Any, intent: Intent, elements: list[dict[str, Any]], speak: Callable[[str], None] | None = None) -> str:
    speak = speak or (lambda text: print(f"[speak] {text}"))
    try:
        if intent.name == "navigate":
            url = intent.slots.get("url")
            if not url:
                return "no url"
            page.goto(url)
            return f"navigated to {urlparse(url).netloc}"
        if intent.name == "search":
            box = _search_box(elements)
            if box is None:
                return "no search box"
            page.fill(selector_for(box["index"]), intent.slots.get("query", ""))
            page.press(selector_for(box["index"]), "Enter")
            return f"searched {intent.slots.get('query', '')!r}"
        if intent.name == "click_link":
            if intent.target is None:
                return "no target"
            page.click(selector_for(intent.target))
            return f"clicked {intent.target_description}"
        if intent.name == "scroll":
            page.mouse.wheel(0, -600 if intent.slots.get("direction") == "up" else 600)
            return f"scrolled {intent.slots.get('direction')}"
        if intent.name == "read_aloud":
            summary = "; ".join(e["text"] for e in elements if e["text"])[:300] or page.title()
            speak(f"{page.title()}. {summary}")
            return "read aloud"
        if intent.name == "go_back":
            page.go_back()
            return "went back"
        if intent.name == "stop":
            return "stop"
    except Exception as error:  # noqa: BLE001
        return f"error: {type(error).__name__}"
    return "noop"


class Session:
    """発話を 1 つずつ処理するループ。confidence が閾値未満なら聞き返す (実行しない)。"""

    def __init__(self, jev: Jev, page: Any, min_confidence: float = 0.3, dry_run: bool = False, speak: Callable[[str], None] | None = None, log: Callable[[str], None] | None = None):
        self.jev = jev
        self.page = page
        self.min_confidence = min_confidence
        self.dry_run = dry_run
        self.speak = speak or (lambda text: print(f"[speak] {text}"))
        self.log = log or (lambda line: print(line))
        self.transcript: list[dict[str, Any]] = []

    def handle(self, utterance: str) -> Intent:
        elements = snapshot(self.page)
        intent = extract_intent(self.jev, utterance, elements, self.page.title())
        if intent.confidence < self.min_confidence:
            outcome = "unclear: please repeat"
            self.speak("Sorry, could you say that again?")
        elif self.dry_run:
            outcome = "dry-run"
        else:
            outcome = execute(self.page, intent, elements, self.speak)
        self.transcript.append({"utterance": utterance, **intent.to_dict(), "outcome": outcome})
        self.log(f"{utterance!r} -> {intent.name} {intent.slots} {intent.target_description} => {outcome} (conf {intent.confidence:.2f})")
        return intent

    def run(self, utterances: list[str] | Callable[[], str], max_turns: int = 20) -> list[dict[str, Any]]:
        for _ in range(max_turns):
            if callable(utterances):
                utterance = utterances()
            elif utterances:
                utterance = utterances.pop(0)
            else:
                break
            intent = self.handle(utterance)
            if intent.name == "stop" and intent.confidence >= self.min_confidence:
                break
        return self.transcript


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab voice-browser", description="音声 → 意図抽出 → ブラウザ操作")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", action="append", help="発話テキスト (複数可、順に処理)")
    source.add_argument("--mic", action="store_true", help="マイクから聞き取る (SpeechRecognition が必要)")
    source.add_argument("--audio", help="音声ファイルを文字起こしして使う")
    parser.add_argument("--url", default=None, help="開始 URL")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="意図を表示するだけ。Playwright 無しなら FakePage")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    jev = Jev(args.backend, cache=True)
    close: Callable[[], None] = lambda: None
    try:
        page, close = open_playwright_page(args.url, args.headless)
    except RuntimeError as error:
        if not args.dry_run:
            print(str(error), file=sys.stderr)
            return 2
        print(f"[voice-browser] {error}\n[voice-browser] dry-run: 内蔵 FakePage を使います", file=sys.stderr)
        page = FakePage(DEMO_SCENES, args.url if args.url in DEMO_SCENES else None)
    session = Session(jev, page, min_confidence=args.min_confidence, dry_run=args.dry_run)
    try:
        if args.text:
            transcript = session.run(list(args.text), max_turns=args.max_turns)
        elif args.audio:
            transcript = session.run([transcribe(args.audio)], max_turns=1)
        else:
            transcript = session.run(lambda: transcribe("mic"), max_turns=args.max_turns)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        close()
    if args.json:
        print(json.dumps(transcript, ensure_ascii=False, indent=2))
    else:
        print(f"turns={len(transcript)} url={getattr(page, 'url', '')} jev={jev.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
