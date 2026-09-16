"""REAPER プロジェクト (.RPP) を直接生成する。

.RPP はテキスト形式なので、テンポ・トラック・MIDI アイテム・オーディオアイテムを
まとめて書き出せる。REAPER で開くだけで "DAW 上に実装済み" の状態になる。
"""

from __future__ import annotations

from pathlib import Path

from ..models import MidiClip, Note, ProjectPlan

PPQ = 960


def _q(s: str) -> str:
    return '"' + s.replace('"', "'") + '"'


def _midi_events(notes: list[Note]) -> list[str]:
    events: list[tuple[int, int, str]] = []
    for n in notes:
        on = int(round(n.start_beat * PPQ))
        off = int(round((n.start_beat + n.duration_beats) * PPQ))
        events.append((on, 1, f"90 {n.pitch:02x} {n.velocity:02x}"))
        events.append((off, 0, f"80 {n.pitch:02x} 00"))
    events.sort(key=lambda e: (e[0], e[1]))
    lines, last = [], 0
    for tick, _, body in events:
        lines.append(f"E {tick - last} {body}")
        last = tick
    return lines


def _midi_item(clip: MidiClip, bpm: float, indent: str = "    ") -> list[str]:
    length_sec = clip.length_beats * 60.0 / bpm
    end_tick = int(round(clip.length_beats * PPQ))
    ev = _midi_events(clip.notes)
    last_tick = sum(int(l.split()[1]) for l in ev)
    lines = [
        f"{indent}<ITEM",
        f"{indent}  POSITION 0",
        f"{indent}  LENGTH {length_sec:.6f}",
        f"{indent}  NAME {_q(clip.name)}",
        f"{indent}  <SOURCE MIDI",
        f"{indent}    HASDATA 1 {PPQ} QN",
    ]
    lines += [f"{indent}    {l}" for l in ev]
    lines.append(f"{indent}    E {max(end_tick - last_tick, 0)} b0 7b 00")  # all notes off
    lines += [f"{indent}  >", f"{indent}>"]
    return lines


def _audio_item(name: str, path: str, start_beat: float, bpm: float, gain_db: float,
                indent: str = "    ") -> list[str]:
    import soundfile as sf

    try:
        info = sf.info(path)
        length = float(info.frames / info.samplerate)
    except Exception:
        length = 1.0
    vol = 10 ** (gain_db / 20.0)
    return [
        f"{indent}<ITEM",
        f"{indent}  POSITION {start_beat * 60.0 / bpm:.6f}",
        f"{indent}  LENGTH {length:.6f}",
        f"{indent}  NAME {_q(name)}",
        f"{indent}  VOLPAN {vol:.6f} 0 1 -1",
        f"{indent}  <SOURCE WAVE",
        f"{indent}    FILE {_q(str(Path(path).resolve()))}",
        f"{indent}  >",
        f"{indent}>",
    ]


def render_rpp(plan: ProjectPlan) -> str:
    num, den = plan.time_signature
    lines = [
        '<REAPER_PROJECT 0.1 "7.0/dtm-agent" 0',
        f"  TEMPO {plan.bpm:g} {num} {den}",
        f"  TITLE {_q(plan.title)}",
    ]
    for track in plan.tracks:
        lines += ["  <TRACK", f"    NAME {_q(track.name)}"]
        for clip in track.midi_clips:
            lines += _midi_item(clip, plan.bpm)
        for clip in track.audio_clips:
            lines += _audio_item(clip.name, clip.path, clip.start_beat, plan.bpm, clip.gain_db)
        lines.append("  >")
    lines.append(">")
    return "\n".join(lines) + "\n"


class ReaperProjectBridge:
    name = "reaper"

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)

    def realize(self, plan: ProjectPlan) -> list[Path]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{plan.title or 'untitled'}.rpp"
        path.write_text(render_rpp(plan), encoding="utf-8")
        plan.save(self.out_dir / "plan.json")
        return [path, self.out_dir / "plan.json"]
