"""18. sponsor_skip (trungdq88/youtube-sponsor-detection の再実装): 字幕からスポンサー区間を判定する。

入力: `--video-id` (youtube-transcript-api を遅延 import) / SRT / JSON (youtube-transcript-api 形式 [{text,start,duration}])。
約 20 秒のチャンクを 50% 重ねたスライディングウィンドウにし、チャンク毎に Jev の decide_many で
`segment_kind` Choice {content, sponsor, self_promotion, intro, outro, interaction_reminder} と `is_paid_promotion` Noul を聞く。
隣接する陽性ウィンドウをヒステリシス (開始閾値 > 継続閾値) で 1 区間にまとめ、SponsorBlock 互換 JSON
[{segment:[start,end], category}] を出す。`--emit-userscript` で Tampermonkey 用スクリプトを書き出し、
`serve` のローカルエンドポイントから区間を取得して video.currentTime で自動スキップする。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jevlab.core import Choice, Jev, Noul

SEGMENT_KINDS: dict[str, str] = {
    "content": "動画本来の内容",
    "sponsor": "第三者スポンサーの宣伝 (『この動画は〜の提供でお送りします』、プロモコード、リンクは概要欄)",
    "self_promotion": "自分の商品/グッズ/Patreon/別チャンネルの宣伝",
    "intro": "オープニング・挨拶・今日やることの前振り",
    "outro": "エンディング・締めの挨拶・次回予告",
    "interaction_reminder": "高評価・チャンネル登録・通知オンのお願い",
}
# SponsorBlock のカテゴリ名
SPONSORBLOCK_CATEGORY = {"sponsor": "sponsor", "self_promotion": "selfpromo", "intro": "intro", "outro": "outro", "interaction_reminder": "interaction"}
DEFAULT_WINDOW = 20.0
DEFAULT_OVERLAP = 0.5
ENTER_THRESHOLD = 0.6  # 区間開始に必要な「content でない」確率
EXIT_THRESHOLD = 0.4  # 区間継続に必要な確率 (ヒステリシス)


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(text: str) -> float:
    match = _TS_RE.search(text)
    if not match:
        raise ValueError(f"タイムスタンプを解釈できません: {text!r}")
    h, m, s, ms = match.groups()
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        start_text, end_text = timing.split("-->")
        body = " ".join(re.sub(r"<[^>]+>", "", line).strip() for line in lines[lines.index(timing) + 1 :])
        if body:
            cues.append(Cue(_ts(start_text), _ts(end_text), body))
    return cues


def parse_json_transcript(data: Any) -> list[Cue]:
    items = data.get("segments", data.get("transcript", [])) if isinstance(data, dict) else data
    cues = []
    for item in items:
        start = float(item["start"])
        end = float(item["end"]) if "end" in item else start + float(item.get("duration", 0.0))
        cues.append(Cue(start, end, str(item["text"])))
    return cues


def fetch_youtube_transcript(video_id: str, languages: tuple[str, ...] = ("ja", "en")) -> list[Cue]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError as error:  # pragma: no cover
        raise SystemExit("youtube-transcript-api が必要です: pip install youtube-transcript-api") from error
    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=list(languages))
    raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
    return parse_json_transcript(raw)


# ---------------------------------------------------------------------------
# ウィンドウ化 / 判定 / マージ
# ---------------------------------------------------------------------------


def make_windows(cues: list[Cue], size: float = DEFAULT_WINDOW, overlap: float = DEFAULT_OVERLAP) -> list[dict[str, Any]]:
    """size 秒のウィンドウを (1-overlap)*size 秒ずつずらして作る。テキストはウィンドウに重なるキューを連結。"""
    if not cues:
        return []
    step = max(0.5, size * (1.0 - overlap))
    t0, t_end = cues[0].start, max(c.end for c in cues)
    windows = []
    start = t0
    while start < t_end:
        end = start + size
        text = " ".join(c.text for c in cues if c.end > start and c.start < end).strip()
        if text:
            windows.append({"index": len(windows), "start": round(start, 2), "end": round(min(end, t_end), 2), "text": text[:600]})
        start += step
    return windows


def classify_windows(jev: Jev, windows: list[dict[str, Any]], context: str = "") -> list[dict[str, Any]]:
    questions = {
        "segment_kind": Choice(SEGMENT_KINDS, "この字幕チャンクは動画内のどの種類の区間か?"),
        "is_paid_promotion": Noul("第三者から対価を受けた宣伝 (スポンサー) を含むか?"),
    }
    items = [({"video": context, "chunk_start_sec": w["start"], "text": w["text"]}, questions) for w in windows]
    decisions = jev.decide_many(items)
    results = []
    for window, decision in zip(windows, decisions):
        kind = decision.choice("segment_kind")
        results.append(
            {
                **window,
                "kind": kind.choice,
                "kind_probability": round(kind.probabilities.get(kind.choice, 0.0), 4),
                "non_content": round(1.0 - kind.probabilities.get("content", 0.0), 4),
                "confidence": round(kind.confidence, 4),
                "paid": round(decision.noul("is_paid_promotion").noul, 4),
            }
        )
    return results


def merge_segments(results: list[dict[str, Any]], enter: float = ENTER_THRESHOLD, exit: float = EXIT_THRESHOLD, min_duration: float = 3.0) -> list[dict[str, Any]]:
    """陽性ウィンドウをヒステリシスでまとめる。開始は non_content >= enter、継続は >= exit。"""
    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    active = False
    for result in results:
        score = result["non_content"]
        if not active and score >= enter:
            active, current = True, [result]
        elif active and score >= exit:
            current.append(result)
        elif active:
            segments.append(_finalize(current))
            active, current = False, []
    if active and current:
        segments.append(_finalize(current))
    return [s for s in segments if s["segment"][1] - s["segment"][0] >= min_duration]


def _finalize(windows: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(w["kind"] for w in windows if w["kind"] != "content")
    kind = kinds.most_common(1)[0][0] if kinds else "sponsor"
    start = min(w["start"] for w in windows)
    end = max(w["end"] for w in windows)
    return {
        "segment": [round(start, 2), round(end, 2)],
        "category": SPONSORBLOCK_CATEGORY.get(kind, "sponsor"),
        "kind": kind,
        "confidence": round(sum(w["confidence"] for w in windows) / len(windows), 3),
        "paid": round(max(w["paid"] for w in windows), 3),
        "windows": [w["index"] for w in windows],
    }


def detect(jev: Jev, cues: list[Cue], size: float = DEFAULT_WINDOW, overlap: float = DEFAULT_OVERLAP, context: str = "") -> dict[str, Any]:
    windows = make_windows(cues, size, overlap)
    results = classify_windows(jev, windows, context)
    segments = merge_segments(results)
    return {"windows": results, "segments": [{"segment": s["segment"], "category": s["category"], "confidence": s["confidence"]} for s in segments]}


# ---------------------------------------------------------------------------
# ユーザースクリプト / ローカルサーバ
# ---------------------------------------------------------------------------

USERSCRIPT = r"""// ==UserScript==
// @name         jevlab sponsor skip
// @namespace    jevlab
// @version      0.1
// @description  Fetch sponsor segments from a local `jevlab sponsor-skip serve` endpoint and auto-skip them.
// @match        https://www.youtube.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==
(() => {
  const ENDPOINT = "http://127.0.0.1:8766/segments?videoID=";
  const CATEGORIES = new Set(["sponsor", "selfpromo", "interaction"]);
  let segments = [], loadedFor = null;
  function load(videoId) {
    if (videoId === loadedFor) return;
    loadedFor = videoId; segments = [];
    GM_xmlhttpRequest({ method: "GET", url: ENDPOINT + encodeURIComponent(videoId), onload: (r) => {
      try { segments = (JSON.parse(r.responseText).segments || []).filter((s) => CATEGORIES.has(s.category)); } catch (e) { segments = []; }
    }});
  }
  setInterval(() => {
    const id = new URLSearchParams(location.search).get("v");
    if (!id) return;
    load(id);
    const video = document.querySelector("video");
    if (!video || video.paused) return;
    for (const s of segments) {
      const [start, end] = s.segment;
      if (video.currentTime >= start && video.currentTime < end - 0.5) { video.currentTime = end; break; }
    }
  }, 500);
})();
"""


def serve(store_dir: str | Path, port: int = 8766) -> None:
    """GET /segments?videoID=X → store_dir/X.json を返す。POST /segments で保存。テストでは起動しない。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            video_id = re.sub(r"[^A-Za-z0-9_-]", "", (query.get("videoID") or [""])[0])
            path = store / f"{video_id}.json"
            if video_id and path.exists():
                self._send(200, json.loads(path.read_text(encoding="utf-8")))
            else:
                self._send(200, {"segments": []})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            video_id = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("videoID", "")))
            if not video_id:
                self._send(400, {"error": "videoID required"})
                return
            (store / f"{video_id}.json").write_text(json.dumps({"segments": payload.get("segments", [])}), encoding="utf-8")
            self._send(200, {"ok": True})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[sponsor-skip serve] {fmt % args}", file=sys.stderr)

    print(f"sponsor-skip serve: http://127.0.0.1:{port}/segments?videoID=... (store={store})")
    try:
        HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab sponsor-skip", description="字幕からスポンサー区間を判定して SponsorBlock 互換 JSON を出す")
    parser.add_argument("--backend", default=None)
    sub = parser.add_subparsers(dest="command")
    det = sub.add_parser("detect", help="区間を検出 (既定)")
    det.add_argument("--video-id", default=None, help="YouTube 動画 ID (youtube-transcript-api を使う)")
    det.add_argument("--srt", default=None)
    det.add_argument("--json", default=None, help="youtube-transcript-api 形式の JSON")
    det.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    det.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP)
    det.add_argument("--out", default=None, help="結果 JSON の保存先")
    det.add_argument("--store-dir", default=None, help="serve 用ストアにも保存する (<video-id>.json)")
    srv = sub.add_parser("serve", help="ユーザースクリプト向けローカルサーバ")
    srv.add_argument("--port", type=int, default=8766)
    srv.add_argument("--store-dir", default="sponsor_segments")
    emit = sub.add_parser("emit-userscript", help="Tampermonkey 用スクリプトを書き出す")
    emit.add_argument("path")
    parser.add_argument("--emit-userscript", default=None, metavar="PATH", help="emit-userscript サブコマンドの別名")
    args = parser.parse_args(argv)

    if args.emit_userscript or args.command == "emit-userscript":
        path = Path(args.emit_userscript or args.path)
        path.write_text(USERSCRIPT, encoding="utf-8")
        print(f"userscript -> {path}")
        return 0
    if args.command == "serve":
        serve(args.store_dir, args.port)
        return 0
    if args.command != "detect":
        parser.print_help()
        return 2

    if args.video_id:
        cues = fetch_youtube_transcript(args.video_id)
    elif args.srt:
        cues = parse_srt(Path(args.srt).read_text(encoding="utf-8"))
    elif args.json:
        cues = parse_json_transcript(json.loads(Path(args.json).read_text(encoding="utf-8")))
    else:
        print("--video-id / --srt / --json のいずれかが必要です", file=sys.stderr)
        return 2
    jev = Jev(args.backend)
    result = detect(jev, cues, args.window, args.overlap, context=args.video_id or "")
    output = {"videoID": args.video_id or "", "segments": result["segments"], "windows": len(result["windows"]), "jev": jev.stats()}
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.store_dir and args.video_id:
        store = Path(args.store_dir)
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{args.video_id}.json").write_text(json.dumps({"segments": result["segments"]}), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
