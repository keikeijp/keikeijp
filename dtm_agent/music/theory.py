"""最小限の音楽理論ユーティリティ (キー → スケール → ノート番号)。"""

from __future__ import annotations

from ..analysis import PITCH_CLASSES

_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
}

ROMAN_TO_DEGREE = {"i": 0, "ii": 1, "iii": 2, "iv": 3, "v": 4, "vi": 5, "vii": 6}


def parse_key(key: str) -> tuple[int, str]:
    """'A minor' → (9, 'minor')。'Bb major', 'F#m' なども受け付ける。"""
    text = key.strip().replace("♭", "b").replace("♯", "#")
    parts = text.split()
    tonic_txt = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    if tonic_txt.endswith("m") and len(tonic_txt) > 1 and tonic_txt[-2] != "#":
        # 'Am' 形式
        if tonic_txt[:-1] in PITCH_CLASSES or tonic_txt[:-1] in _FLATS:
            tonic_txt, mode = tonic_txt[:-1], "minor"
    tonic_txt = tonic_txt[0].upper() + tonic_txt[1:]
    tonic_txt = _FLATS.get(tonic_txt, tonic_txt)
    if tonic_txt not in PITCH_CLASSES:
        raise ValueError(f"unknown tonic: {key}")
    mode = {"maj": "major", "min": "minor", "m": "minor"}.get(mode, mode)
    if mode not in SCALES:
        raise ValueError(f"unknown mode: {mode}")
    return PITCH_CLASSES.index(tonic_txt), mode


def scale_pitches(key: str, low: int = 0, high: int = 127) -> list[int]:
    tonic, mode = parse_key(key)
    classes = {(tonic + iv) % 12 for iv in SCALES[mode]}
    return [p for p in range(low, high + 1) if p % 12 in classes]


def snap_to_scale(pitch: int, key: str) -> int:
    """スケール外の音を最も近いスケール音に丸める (同距離なら下側)。"""
    allowed = scale_pitches(key)
    return min(allowed, key=lambda p: (abs(p - pitch), p))


def chord_pitches(key: str, degree: int, octave: int = 4, *, seventh: bool = False) -> list[int]:
    """ダイアトニックコード (度数は 0 始まり) のノート番号。"""
    tonic, mode = parse_key(key)
    scale = SCALES[mode]
    n = len(scale)
    root = 12 * (octave + 1) + tonic + scale[degree % n]
    idxs = [0, 2, 4] + ([6] if seventh else [])
    pitches = []
    for k in idxs:
        step = degree + k
        octave_shift = step // n
        pitches.append(12 * (octave + 1 + octave_shift) + tonic + scale[step % n])
    return [root] + pitches[1:]
