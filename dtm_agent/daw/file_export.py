"""どの DAW でも読める汎用出力: トラックごとの .mid + サンプル一覧 + plan.json。"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import ProjectPlan
from ..music.melody import notes_to_midi_file


class FileBridge:
    name = "file"

    def __init__(self, out_dir: str | Path, *, copy_samples: bool = True) -> None:
        self.out_dir = Path(out_dir)
        self.copy_samples = copy_samples

    def realize(self, plan: ProjectPlan) -> list[Path]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        plan_path = self.out_dir / "plan.json"
        plan.save(plan_path)
        written.append(plan_path)

        for track in plan.tracks:
            for clip in track.midi_clips:
                path = self.out_dir / "midi" / f"{_safe(track.name)}__{_safe(clip.name)}.mid"
                notes_to_midi_file(clip.notes, path, bpm=plan.bpm, name=clip.name)
                written.append(path)
            for clip in track.audio_clips:
                src = Path(clip.path)
                if self.copy_samples and src.exists():
                    dst = self.out_dir / "samples" / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(src, dst)
                    written.append(dst)

        readme = self.out_dir / "README.txt"
        readme.write_text(_readme(plan), encoding="utf-8")
        written.append(readme)
        return written


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "untitled"


def _readme(plan: ProjectPlan) -> str:
    lines = [f"{plan.title}  ({plan.bpm:g} BPM, {plan.key})", ""]
    for t in plan.tracks:
        lines.append(f"[{t.kind}] {t.name}" + (f"  -- {t.instrument_hint}" if t.instrument_hint else ""))
        for c in t.midi_clips:
            lines.append(f"    midi/{_safe(t.name)}__{_safe(c.name)}.mid  ({len(c.notes)} notes, {c.length_beats:g} beats)")
        for c in t.audio_clips:
            lines.append(f"    samples/{Path(c.path).name}  @ beat {c.start_beat:g}")
    if plan.notes_for_user:
        lines += ["", "Notes:"] + [f"  - {n}" for n in plan.notes_for_user]
    return "\n".join(lines) + "\n"
