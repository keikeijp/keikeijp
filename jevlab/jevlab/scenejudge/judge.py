"""SceneJudge 本体: 画像 → SAM 3.1 → シーングラフ → Jev の型付き判断 → ルール適用。

```python
from jevlab.scenejudge import SceneJudge
judge = SceneJudge()                       # 環境変数で SAM/Jev のバックエンドを選ぶ (無ければ両方 mock)
verdict = judge.judge_image("desk.jpg", "desk")
verdict.answers["tidiness"].level, verdict.triggered
```

動画は `judge_video(frames, scenario)` に PIL/ffmpeg で取り出したフレーム列を渡す (ゾーン侵入/退出のイベントも出す)。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from jevlab.core import Decision, Jev
from jevlab.sam import ImageInput, Sam, Scene, Segmentation, build_scene
from jevlab.scenejudge.scenarios import Scenario, get_scenario


@dataclass
class Verdict:
    scenario: str
    source: str
    scene_state: dict[str, Any]
    decision: Decision
    triggered: list[dict[str, Any]] = field(default_factory=list)
    segmentation: Segmentation | None = None
    frame: int = 0
    sam_ms: float = 0.0

    @property
    def answers(self) -> dict[str, Any]:
        return self.decision.answers

    def summary(self) -> str:
        lines = [f"[{self.scenario}] {self.source or 'image'} frame={self.frame}"]
        counts = self.scene_state.get("counts", {})
        lines.append("  objects: " + (", ".join(f"{k}x{v}" for k, v in counts.items()) or "none"))
        for name, answer in self.decision.answers.items():
            if answer.type == "choice":
                lines.append(f"  {name}: {answer.choice} (p={answer.probabilities.get(answer.choice, 0):.2f}, conf={answer.confidence:.2f})")
            elif answer.type == "score":
                lines.append(f"  {name}: level {answer.level} '{answer.legend.get(answer.level, '')}' (score={answer.score:.2f})")
            else:
                lines.append(f"  {name}: {'yes' if answer.yes else 'no'} (p={answer.noul:.2f})")
        for hit in self.triggered:
            lines.append(f"  !! {hit['action']}: {hit['message']}")
        lines.append(f"  timing: sam={self.sam_ms:.0f}ms jev={self.decision.latency_ms:.0f}ms")
        return "\n".join(lines)

    def to_dict(self, include_objects: bool = True) -> dict[str, Any]:
        data = {
            "scenario": self.scenario,
            "source": self.source,
            "frame": self.frame,
            "scene": self.scene_state,
            "decision": self.decision.to_dict(),
            "triggered": self.triggered,
            "timing_ms": {"sam": round(self.sam_ms, 1), "jev": round(self.decision.latency_ms, 1)},
        }
        if include_objects and self.segmentation is not None:
            data["objects"] = [obj.to_dict() for obj in self.segmentation.objects]
        return data


@dataclass
class ZoneEvent:
    frame: int
    object_id: str
    label: str
    zone: str
    kind: str  # "enter" | "leave"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class SceneJudge:
    def __init__(self, sam: Sam | None = None, jev: Jev | None = None, min_object_area_ratio: float = 0.0005):
        self.sam = sam or Sam()
        self.jev = jev or Jev(cache=True)
        self.min_object_area_ratio = min_object_area_ratio

    # -- 画像 ---------------------------------------------------------------

    def segment(self, image: ImageInput | str | Path | bytes, scenario: Scenario) -> Segmentation:
        return self.sam.segment(image, scenario.prompts)

    def build_scene(self, segmentation: Segmentation, scenario: Scenario, frame: int = 0) -> Scene:
        width, height = segmentation.width, segmentation.height
        area = (width * height) or 1
        objects = [obj for obj in segmentation.objects if obj.mask_area() / area >= self.min_object_area_ratio]
        return build_scene(objects, width, height, zones=scenario.zones_px(width, height), frame=frame)

    def decide(self, scene: Scene, scenario: Scenario) -> tuple[dict[str, Any], Decision]:
        state = {"scenario": scenario.description, "context": scenario.context, "scene": scene.to_state()}
        decision = self.jev.decide(state, scenario.questions)
        return state["scene"], decision

    def judge_image(self, image: ImageInput | str | Path | bytes, scenario: Scenario | str, frame: int = 0) -> Verdict:
        scenario = get_scenario(scenario) if isinstance(scenario, str) else scenario
        started = time.perf_counter()
        segmentation = self.segment(image, scenario)
        sam_ms = (time.perf_counter() - started) * 1000
        scene = self.build_scene(segmentation, scenario, frame)
        scene_state, decision = self.decide(scene, scenario)
        triggered = apply_rules(scenario, decision)
        return Verdict(scenario.name, segmentation.source, scene_state, decision, triggered, segmentation, frame, sam_ms)

    # -- 動画 (フレーム列) ----------------------------------------------------

    def judge_video(self, frames: Iterable[ImageInput | bytes | str | Path], scenario: Scenario | str, every: int = 1) -> tuple[list[Verdict], list[ZoneEvent]]:
        """フレーム列を順に判定し、ゾーンの入退出イベントも返す。`every` で間引く。"""
        scenario = get_scenario(scenario) if isinstance(scenario, str) else scenario
        verdicts: list[Verdict] = []
        events: list[ZoneEvent] = []
        previous: dict[str, set[str]] = {}  # label → zones occupied (前フレーム)
        for index, frame in enumerate(frames):
            if index % every:
                continue
            verdict = self.judge_image(frame, scenario, frame=index)
            verdicts.append(verdict)
            current: dict[str, set[str]] = {}
            for obj in verdict.scene_state.get("objects", []):
                current.setdefault(obj["label"], set()).update(obj["zones"])
            for label in set(previous) | set(current):
                before = previous.get(label, set())
                after = current.get(label, set())
                for zone in after - before:
                    events.append(ZoneEvent(index, label, label, zone, "enter"))
                for zone in before - after:
                    events.append(ZoneEvent(index, label, label, zone, "leave"))
            previous = current
        return verdicts, events


def apply_rules(scenario: Scenario, decision: Decision) -> list[dict[str, Any]]:
    triggered = []
    for rule in scenario.rules:
        answer = decision.answers.get(rule.question)
        if answer is None:
            continue
        if rule.matches(answer):
            triggered.append({"question": rule.question, "when": rule.when, "action": rule.action, "message": rule.message})
    return triggered


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

PALETTE = [(255, 80, 80), (80, 200, 120), (80, 140, 255), (255, 200, 60), (200, 100, 255), (60, 220, 220), (255, 140, 40), (160, 160, 160)]


def render_overlay(image_path: str | Path, verdict: Verdict, out_path: str | Path, draw_masks: bool = True) -> Path:
    """検出 box / mask とゾーン、判定結果を描いた PNG を書く (Pillow が必要)。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError("オーバーレイ出力には `pip install pillow` が必要です") from error
    image = Image.open(image_path).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    segmentation = verdict.segmentation
    width, height = image.size
    for name, zone in verdict.scene_state.get("zones", {}).items():
        box = (zone[0] * width, zone[1] * height, zone[2] * width, zone[3] * height)
        draw.rectangle(box, outline=(255, 255, 255, 180), width=2)
        draw.text((box[0] + 4, box[1] + 4), name, fill=(255, 255, 255, 220))
    if segmentation is not None:
        colors = {prompt: PALETTE[i % len(PALETTE)] for i, prompt in enumerate(segmentation.prompts)}
        for obj in segmentation.objects:
            color = colors.get(obj.prompt, PALETTE[-1])
            if draw_masks and obj.mask is not None:
                raster = obj.mask.raster()
                if raster is not None and obj.mask.width and obj.mask.height:
                    mask_image = Image.frombytes("L", (obj.mask.width, obj.mask.height), bytes(v * 110 for v in raster))
                    if mask_image.size != (obj.width, obj.height):
                        mask_image = mask_image.resize((max(1, obj.width), max(1, obj.height)))
                    tint = Image.new("RGBA", mask_image.size, color + (0,))
                    tint.putalpha(mask_image)
                    layer.alpha_composite(tint, (obj.x1, obj.y1))
            draw.rectangle((obj.x1, obj.y1, obj.x2 - 1, obj.y2 - 1), outline=color + (255,), width=2)
            draw.text((obj.x1 + 3, max(0, obj.y1 - 12)), obj.prompt, fill=color + (255,))
    y = 6
    for line in verdict.summary().splitlines()[1:]:
        draw.rectangle((4, y - 2, 4 + 7 * len(line), y + 12), fill=(0, 0, 0, 150))
        draw.text((6, y), line, fill=(255, 255, 255, 255))
        y += 14
    out_path = Path(out_path)
    Image.alpha_composite(image, layer).convert("RGB").save(out_path)
    return out_path


def write_report(verdicts: Sequence[Verdict], events: Sequence[ZoneEvent], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.write_text(
        json.dumps({"verdicts": [v.to_dict() for v in verdicts], "events": [e.to_dict() for e in events]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def extract_frames(video_path: str | Path, fps: float = 1.0, out_dir: str | Path | None = None) -> list[Path]:
    """ffmpeg でフレームを JPEG に切り出す (ffmpeg が PATH にあること)。"""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("動画入力には ffmpeg が必要です")
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="scenejudge_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video_path), "-vf", f"fps={fps}", str(out_dir / "frame_%05d.jpg")], check=True)
    return sorted(out_dir.glob("frame_*.jpg"))
