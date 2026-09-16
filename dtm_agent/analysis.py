"""librosa による音声解析。

参照曲 (ローカルファイル) の任意区間から BPM / キー / 質感の指標 / 類似検索用ベクトルを求める。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from .models import AudioProfile

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler のキープロファイル
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# feature_vector の並び (LocalLibrary の標準化とドキュメントで参照する)
FEATURE_NAMES = (
    [f"mfcc{i}" for i in range(13)]
    + ["log_centroid", "log_bandwidth", "log_rolloff", "zcr", "rms", "onset_rate", "tonal_ratio"]
)


def load_audio(path: str | Path, start_sec: float = 0.0, end_sec: Optional[float] = None,
               sr: int = 22050) -> tuple[np.ndarray, int]:
    """モノラルで読み込み、区間を切り出す。"""
    import librosa

    duration = None if end_sec is None else max(0.0, end_sec - start_sec)
    y, sr_out = librosa.load(str(path), sr=sr, mono=True, offset=start_sec, duration=duration)
    if y.size == 0:
        raise ValueError(f"empty audio: {path} [{start_sec}, {end_sec}]")
    return y, int(sr_out)


def estimate_key(chroma_mean: np.ndarray) -> tuple[str, float]:
    """12 次元 chroma からキーを推定する。戻り値は ('A minor', 確信度)。"""
    best_name, best_corr, scores = "C major", -1.0, []
    for shift in range(12):
        for name, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
            rotated = np.roll(profile, shift)
            corr = float(np.corrcoef(chroma_mean, rotated)[0, 1])
            if np.isnan(corr):
                corr = 0.0
            scores.append(corr)
            if corr > best_corr:
                best_corr, best_name = corr, f"{PITCH_CLASSES[shift]} {name}"
    scores.sort(reverse=True)
    # 1 位と 2 位の差を確信度に変換 (0..1)
    margin = scores[0] - scores[1] if len(scores) > 1 else 0.0
    confidence = float(np.clip(0.5 + margin * 2.5, 0.0, 1.0))
    return best_name, confidence


def _sigmoid01(x: float, center: float, width: float) -> float:
    return float(1.0 / (1.0 + np.exp(-(x - center) / width)))


def analyze_signal(y: np.ndarray, sr: int, *, source: str = "<array>",
                   start_sec: float = 0.0, end_sec: Optional[float] = None) -> AudioProfile:
    import warnings

    import librosa

    # ワンショットの短いサンプルでは n_fft > 信号長の警告が大量に出るので抑制する
    warnings.filterwarnings("ignore", message="n_fft=.* is too large", category=UserWarning)

    duration = float(len(y) / sr)
    hop = 512

    tempo, _ = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    bpm = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0

    harmonic, percussive = librosa.effects.hpss(y)
    h_energy = float(np.sum(harmonic**2)) + 1e-9
    p_energy = float(np.sum(percussive**2)) + 1e-9
    tonal_ratio = h_energy / (h_energy + p_energy)

    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=hop)
    chroma_mean = chroma.mean(axis=1)
    chroma_norm = chroma_mean / (chroma_mean.sum() + 1e-9)
    key, key_conf = estimate_key(chroma_mean)

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
    centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop).mean())
    bandwidth = float(librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=hop).mean())
    rolloff = float(librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=hop).mean())
    zcr = float(librosa.feature.zero_crossing_rate(y, hop_length=hop).mean())
    rms = float(librosa.feature.rms(y=y, hop_length=hop).mean())
    onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=hop, units="time")
    onset_rate = float(len(onsets) / max(duration, 1e-3))

    feature_vector = (
        [float(v) for v in mfcc.mean(axis=1)]
        + [
            float(np.log1p(centroid)),
            float(np.log1p(bandwidth)),
            float(np.log1p(rolloff)),
            zcr,
            rms,
            onset_rate,
            tonal_ratio,
        ]
    )

    return AudioProfile(
        source=source,
        start_sec=start_sec,
        end_sec=end_sec if end_sec is not None else start_sec + duration,
        duration_sec=duration,
        sample_rate=sr,
        bpm=bpm,
        key=key,
        key_confidence=key_conf,
        energy=_sigmoid01(rms, center=0.1, width=0.05),
        brightness=_sigmoid01(centroid, center=2500.0, width=1200.0),
        percussiveness=_sigmoid01(onset_rate, center=4.0, width=2.0),
        tonalness=float(tonal_ratio),
        chroma=[float(v) for v in chroma_norm],
        feature_vector=feature_vector,
    )


def analyze_file(path: str | Path, start_sec: float = 0.0,
                 end_sec: Optional[float] = None) -> AudioProfile:
    """ファイルの指定区間を解析する。"""
    y, sr = load_audio(path, start_sec, end_sec)
    return analyze_signal(y, sr, source=str(path), start_sec=start_sec, end_sec=end_sec)


def parse_segment(text: Optional[str]) -> tuple[float, Optional[float]]:
    """'1:05-1:21' / '65-81' / '32' のような区間表記を秒に変換する。"""
    if not text:
        return 0.0, None

    def to_sec(part: str) -> float:
        part = part.strip()
        if ":" in part:
            m, s = part.split(":", 1)
            return int(m) * 60 + float(s)
        return float(part)

    if "-" in text:
        a, b = text.split("-", 1)
        return to_sec(a), to_sec(b)
    return to_sec(text), None
