"""ノート列 ↔ MIDI ファイルの相互変換と検証。"""

from __future__ import annotations

from pathlib import Path

import mido

from ..models import Note
from .theory import scale_pitches

TICKS_PER_BEAT = 480


def validate_notes(notes: list[Note], key: str | None = None) -> list[str]:
    """問題点を日本語で列挙する (空なら OK)。"""
    problems = []
    allowed = set(scale_pitches(key)) if key else None
    for i, n in enumerate(notes):
        if allowed is not None and n.pitch not in allowed:
            problems.append(f"note[{i}] pitch={n.pitch} は {key} のスケール外")
    return problems


def notes_to_midi_file(notes: list[Note], path: str | Path, *, bpm: float = 120.0,
                       name: str = "clip", channel: int = 0) -> Path:
    mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))

    events: list[tuple[int, int, mido.Message]] = []  # (tick, order, msg)
    for n in notes:
        on = int(round(n.start_beat * TICKS_PER_BEAT))
        off = int(round((n.start_beat + n.duration_beats) * TICKS_PER_BEAT))
        events.append((on, 1, mido.Message("note_on", note=n.pitch, velocity=n.velocity, channel=channel)))
        events.append((off, 0, mido.Message("note_off", note=n.pitch, velocity=0, channel=channel)))
    events.sort(key=lambda e: (e[0], e[1]))

    last = 0
    for tick, _, msg in events:
        msg.time = tick - last
        track.append(msg)
        last = tick
    track.append(mido.MetaMessage("end_of_track", time=0))

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(out))
    return out


def midi_file_to_notes(path: str | Path) -> list[Note]:
    mid = mido.MidiFile(str(path))
    tpb = mid.ticks_per_beat
    notes: list[Note] = []
    pending: dict[int, tuple[int, int]] = {}
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                pending[msg.note] = (tick, msg.velocity)
            elif msg.type in ("note_off", "note_on") and msg.note in pending:
                start, vel = pending.pop(msg.note)
                notes.append(Note(pitch=msg.note, start_beat=start / tpb,
                                  duration_beats=max((tick - start) / tpb, 1 / tpb), velocity=vel))
    return sorted(notes, key=lambda n: (n.start_beat, n.pitch))
