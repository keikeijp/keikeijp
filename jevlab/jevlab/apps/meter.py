"""17. meter (ChetasLua/jevmeter の再実装): 動画の書き起こし (SRT/WebVTT/JSON) を発話単位に分け、
ユーザーが選んだ観点 (既定: 主張の断定度 / 感情の強さ / セールストーク度) で Jev の Score により採点し、
メーター表示のオーバーレイ字幕 (ASS) と ffmpeg の焼き込みコマンド、観点ごとの SVG スパークラインを出力する。

**ファクトチェックではない**。「どれくらい言い切っているか」「どれくらい売り込みか」といった話し方の観点を採点するだけで、
発言の真偽は判定しない (ドキュメント参照)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jevlab.core import Jev, Score

# 観点名 → 順序付きルーブリック (level 0 .. n-1)。--axes で選ぶ。
DEFAULT_AXES: dict[str, list[str]] = {
    "confidence_of_claim": ["強くぼかしている (かもしれない、たぶん)", "やや控えめ", "普通の断定", "かなり強い断定", "絶対的・例外なしの断言"],
    "emotional_intensity": ["淡々としている", "少し感情がこもる", "はっきり感情的", "非常に感情的", "激しく興奮/怒り/歓喜"],
    "salesmanship": ["売り込み要素なし", "軽い勧め", "はっきり勧めている", "強い売り込み", "煽り・限定・今すぐ買え"],
}
AXIS_INSTRUCTIONS: dict[str, str] = {
    "confidence_of_claim": "この発言はどれくらい言い切っているか (真偽ではなく、断定の強さ)",
    "emotional_intensity": "この発言の感情の強さは",
    "salesmanship": "この発言はどれくらい売り込み/勧誘か",
}

BAR_FILLED = "▰"
BAR_EMPTY = "▱"
BAR_WIDTH = 5


@dataclass
class Cue:
    start: float
    end: float
    text: str


# ---------------------------------------------------------------------------
# パーサ
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TIMING_RE = re.compile(r"(\S+)\s*-->\s*(\S+)")
_TAG_RE = re.compile(r"<[^>]+>")


def parse_timestamp(text: str) -> float:
    """`HH:MM:SS,mmm` / `HH:MM:SS.mmm` / `MM:SS.mmm` → 秒。"""
    match = _TS_RE.search(text.strip())
    if not match:
        raise ValueError(f"タイムスタンプを解釈できません: {text!r}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def _parse_blocks(text: str, skip_header: bool) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if skip_header and (lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE") or lines[0].startswith("STYLE")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = _TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        start, end = parse_timestamp(match.group(1)), parse_timestamp(match.group(2))
        body = " ".join(_TAG_RE.sub("", line).strip() for line in lines[timing_index + 1 :]).strip()
        if body:
            cues.append(Cue(start, end, body))
    return cues


def parse_srt(text: str) -> list[Cue]:
    return _parse_blocks(text, skip_header=False)


def parse_vtt(text: str) -> list[Cue]:
    if not text.lstrip().startswith("WEBVTT"):
        raise ValueError("WEBVTT ヘッダがありません")
    return _parse_blocks(text, skip_header=True)


def parse_json_transcript(data: Any) -> list[Cue]:
    items = data.get("segments", data.get("cues", [])) if isinstance(data, dict) else data
    return [Cue(float(item["start"]), float(item.get("end", item["start"] + item.get("duration", 0))), str(item["text"])) for item in items]


def load_transcript(path: str | Path) -> list[Cue]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        return parse_vtt(text)
    if suffix == ".json":
        return parse_json_transcript(json.loads(text))
    return parse_srt(text)


# ---------------------------------------------------------------------------
# 発話へのセグメント化と採点
# ---------------------------------------------------------------------------


def segment_utterances(cues: list[Cue], max_gap: float = 0.8, max_chars: int = 220, max_duration: float = 15.0) -> list[Cue]:
    """字幕キューを、文末や間 (gap) で区切った発話にまとめる。"""
    utterances: list[Cue] = []
    current: Cue | None = None
    for cue in cues:
        if current is None:
            current = Cue(cue.start, cue.end, cue.text)
            continue
        ends_sentence = bool(re.search(r"[.!?。！？]\s*$", current.text))
        too_long = len(current.text) + len(cue.text) > max_chars or cue.end - current.start > max_duration
        if cue.start - current.end > max_gap or ends_sentence or too_long:
            utterances.append(current)
            current = Cue(cue.start, cue.end, cue.text)
        else:
            current = Cue(current.start, cue.end, f"{current.text} {cue.text}")
    if current is not None:
        utterances.append(current)
    return utterances


def build_questions(axes: dict[str, list[str]]) -> dict[str, Score]:
    return {name: Score(rubric, AXIS_INSTRUCTIONS.get(name, f"{name} の度合いは?")) for name, rubric in axes.items()}


def score_utterances(jev: Jev, utterances: list[Cue], axes: dict[str, list[str]] | None = None, context_window: int = 1) -> list[dict[str, Any]]:
    """発話ごとに全観点を 1 回の Jev 呼び出しで採点し、タイムラインを返す。"""
    axes = axes or DEFAULT_AXES
    questions = build_questions(axes)
    items = []
    for i, utt in enumerate(utterances):
        previous = [u.text[:120] for u in utterances[max(0, i - context_window) : i]]
        items.append(({"utterance": utt.text, "previous": previous}, questions))
    decisions = jev.decide_many(items)
    timeline = []
    for utt, decision in zip(utterances, decisions):
        scores = {}
        for name in axes:
            answer = decision.score(name)
            scores[name] = {"score": round(answer.score, 3), "level": answer.level, "normalized": round(answer.normalized(), 3), "confidence": round(answer.confidence, 3)}
        timeline.append({**asdict(utt), "scores": scores})
    return timeline


# ---------------------------------------------------------------------------
# 出力: メーターバー / ASS / ffmpeg / SVG
# ---------------------------------------------------------------------------


def meter_bar(normalized: float, width: int = BAR_WIDTH) -> str:
    filled = int(max(0.0, min(1.0, normalized)) * width + 0.5)  # 0.5 → 半分以上を塗る (銀行丸めを避ける)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def ass_color(normalized: float) -> str:
    """0 → 緑, 0.5 → 黄, 1 → 赤 を ASS の &HBBGGRR& 形式で返す。"""
    n = max(0.0, min(1.0, normalized))
    red = int(255 * min(1.0, 2 * n))
    green = int(255 * min(1.0, 2 * (1 - n)))
    return f"&H00{0:02X}{green:02X}{red:02X}&"


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        centis, secs = 0, secs + 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


ASS_HEADER = """[Script Info]
Title: jevlab meter overlay
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Meter,Arial,28,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,2,0,7,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def to_ass(timeline: list[dict[str, Any]], axes: dict[str, list[str]] | None = None, width: int = BAR_WIDTH) -> str:
    axes = axes or DEFAULT_AXES
    lines = [ASS_HEADER]
    for entry in timeline:
        parts = []
        for name in axes:
            score = entry["scores"][name]
            parts.append(f"{{\\c&HFFFFFF&}}{name}: {{\\c{ass_color(score['normalized'])}}}{meter_bar(score['normalized'], width)}")
        text = "\\N".join(parts)
        lines.append(f"Dialogue: 0,{ass_time(entry['start'])},{ass_time(entry['end'])},Meter,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def ffmpeg_command(video: str, ass_path: str, output: str = "output_meter.mp4") -> str:
    escaped = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"ffmpeg -y -i '{video}' -vf \"ass='{escaped}'\" -c:a copy '{output}'"


def svg_sparkline(timeline: list[dict[str, Any]], axis: str, width: int = 480, height: int = 64) -> str:
    if not timeline:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'
    t0 = timeline[0]["start"]
    t1 = max(timeline[-1]["end"], t0 + 1e-6)
    points = []
    for entry in timeline:
        mid = (entry["start"] + entry["end"]) / 2
        x = (mid - t0) / (t1 - t0) * (width - 8) + 4
        y = height - 4 - entry["scores"][axis]["normalized"] * (height - 8)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<title>{axis}</title><rect width="{width}" height="{height}" fill="#111" rx="4"/>'
        f'<polyline fill="none" stroke="#f5a623" stroke-width="2" points="{" ".join(points)}"/>'
        f'<text x="6" y="14" fill="#ccc" font-size="11" font-family="sans-serif">{axis}</text></svg>'
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab meter", description="書き起こしの発言を観点別に採点してメーター字幕を作る (ファクトチェックではない)")
    parser.add_argument("--transcript", required=True, help="SRT / VTT / JSON")
    parser.add_argument("--axes", default=",".join(DEFAULT_AXES), help="カンマ区切りの観点名 (既定の 3 観点から選ぶ)")
    parser.add_argument("--axes-json", default=None, help="独自ルーブリック {name: [level...]} の JSON ファイル")
    parser.add_argument("--out-dir", default="meter_out")
    parser.add_argument("--video", default=None, help="焼き込み対象の動画 (--ffmpeg-cmd 用)")
    parser.add_argument("--ffmpeg-cmd", action="store_true", help="ffmpeg コマンドを表示する")
    parser.add_argument("--max-utterances", type=int, default=400)
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    if args.axes_json:
        with open(args.axes_json, encoding="utf-8") as handle:
            axes = {k: list(v) for k, v in json.load(handle).items()}
    else:
        axes = {}
        for name in [a.strip() for a in args.axes.split(",") if a.strip()]:
            if name not in DEFAULT_AXES:
                print(f"未知の観点: {name} (既定: {', '.join(DEFAULT_AXES)})", file=sys.stderr)
                return 2
            axes[name] = DEFAULT_AXES[name]

    cues = load_transcript(args.transcript)
    utterances = segment_utterances(cues)[: args.max_utterances]
    jev = Jev(args.backend)
    timeline = score_utterances(jev, utterances, axes)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timeline.json").write_text(json.dumps({"axes": axes, "timeline": timeline}, ensure_ascii=False, indent=2), encoding="utf-8")
    ass_path = out_dir / "meter.ass"
    ass_path.write_text(to_ass(timeline, axes), encoding="utf-8")
    for name in axes:
        (out_dir / f"sparkline_{name}.svg").write_text(svg_sparkline(timeline, name), encoding="utf-8")
    print(f"utterances={len(timeline)} axes={list(axes)} -> {out_dir}/ (timeline.json, meter.ass, sparkline_*.svg) jev={jev.stats()}")
    if args.ffmpeg_cmd:
        print(ffmpeg_command(args.video or "input.mp4", str(ass_path), str(out_dir / "output_meter.mp4")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
