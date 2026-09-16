"""パイプライン全体で共有するデータモデル。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class AudioProfile(BaseModel):
    """音声区間の特徴量。参照曲の "この部分" を数値で表したもの。"""

    source: str = Field(description="ファイルパスまたは URL")
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    duration_sec: float
    sample_rate: int

    bpm: float
    key: str = Field(description="例: 'A minor', 'F# major'")
    key_confidence: float = Field(ge=0.0, le=1.0)

    # 0..1 に正規化した知覚的な指標
    energy: float = Field(description="RMS ラウドネス (0..1)")
    brightness: float = Field(description="スペクトル重心ベース (0..1)")
    percussiveness: float = Field(description="オンセット密度ベース (0..1)")
    tonalness: float = Field(description="HPSS の harmonic 比率 (0..1)")

    chroma: list[float] = Field(description="12 次元 pitch-class 分布 (合計 1)")
    feature_vector: list[float] = Field(description="類似検索用の特徴ベクトル")

    def describe(self) -> str:
        """LLM に渡すための短い日本語説明。"""
        return (
            f"{self.bpm:.1f} BPM, {self.key} (確信度 {self.key_confidence:.2f}), "
            f"energy={self.energy:.2f}, brightness={self.brightness:.2f}, "
            f"percussive={self.percussiveness:.2f}, tonal={self.tonalness:.2f}, "
            f"区間 {self.start_sec:.1f}s-{(self.end_sec or self.duration_sec):.1f}s"
        )


class SampleHit(BaseModel):
    """サンプル検索の 1 件の結果。"""

    path: str
    name: str
    source: Literal["local", "freesound", "splice"] = "local"
    score: float = Field(description="高いほど類似 (cosine similarity など)")
    duration_sec: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    license: Optional[str] = None


class Note(BaseModel):
    """拍単位の MIDI ノート。"""

    pitch: int = Field(ge=0, le=127, description="MIDI ノート番号 (60 = C4)")
    start_beat: float = Field(ge=0.0)
    duration_beats: float = Field(gt=0.0)
    velocity: int = Field(default=100, ge=1, le=127)


class MidiClip(BaseModel):
    name: str
    notes: list[Note]
    length_beats: float


class AudioClip(BaseModel):
    name: str
    path: str
    start_beat: float = 0.0
    gain_db: float = 0.0


class Track(BaseModel):
    name: str
    kind: Literal["midi", "audio"]
    midi_clips: list[MidiClip] = Field(default_factory=list)
    audio_clips: list[AudioClip] = Field(default_factory=list)
    instrument_hint: Optional[str] = Field(
        default=None, description="例: 'analog pad', '808 kick' (DAW 側で音色を選ぶ手掛かり)"
    )


class ProjectPlan(BaseModel):
    """エージェントが組み立て、DAW ブリッジが実体化する曲の設計図。"""

    title: str = "untitled"
    bpm: float = 120.0
    key: str = "C major"
    time_signature: tuple[int, int] = (4, 4)
    tracks: list[Track] = Field(default_factory=list)
    notes_for_user: list[str] = Field(default_factory=list)

    def add_track(self, track: Track) -> Track:
        self.tracks.append(track)
        return track

    def find_track(self, name: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.name == name), None)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ProjectPlan":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
