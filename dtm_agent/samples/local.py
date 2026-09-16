"""PC 内のサンプルライブラリをインデックス化し、音響類似 + キーワードで検索する。

- 各ファイルの特徴ベクトル (analysis.FEATURE_NAMES 参照) を JSON に保存
- 検索時はライブラリ統計で標準化した cosine 類似度 + ファイル名/フォルダ名のキーワード一致
- CLAP 等の埋め込みモデルは後から `embedder` として差し替え可能な設計
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from ..analysis import analyze_file
from ..models import AudioProfile, SampleHit

AUDIO_EXTS = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a"}
INDEX_FILENAME = ".dtm_agent_index.json"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class LocalLibrary:
    name = "local"

    def __init__(self, root: str | Path, *, max_seconds: float = 30.0,
                 embedder: Optional[Callable[[Path], list[float]]] = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_seconds = max_seconds
        self.embedder = embedder
        self.index_path = self.root / INDEX_FILENAME
        self.entries: list[dict] = []
        self._mean: Optional[np.ndarray] = None
        self._std: Optional[np.ndarray] = None
        if self.index_path.exists():
            self.load()

    # ---------- インデックス ----------

    def iter_audio_files(self) -> Iterable[Path]:
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                yield p

    def build(self, *, progress: Optional[Callable[[int, int, Path], None]] = None) -> int:
        files = list(self.iter_audio_files())
        entries = []
        for i, path in enumerate(files):
            try:
                profile = analyze_file(path, 0.0, self.max_seconds)
            except Exception as exc:  # 壊れたファイルはスキップ
                entries.append({"path": str(path), "error": str(exc)})
                continue
            entry = {
                "path": str(path),
                "name": path.stem,
                "duration_sec": profile.duration_sec,
                "bpm": profile.bpm,
                "key": profile.key,
                "tags": sorted(set(tokenize(str(path.relative_to(self.root))))),
                "feature_vector": profile.feature_vector,
                "brightness": profile.brightness,
                "percussiveness": profile.percussiveness,
                "tonalness": profile.tonalness,
            }
            if self.embedder is not None:
                entry["embedding"] = self.embedder(path)
            entries.append(entry)
            if progress:
                progress(i + 1, len(files), path)
        self.entries = entries
        self._fit_stats()
        self.save()
        return len([e for e in entries if "feature_vector" in e])

    def save(self) -> None:
        self.index_path.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")

    def load(self) -> None:
        self.entries = json.loads(self.index_path.read_text(encoding="utf-8"))
        self._fit_stats()

    def _fit_stats(self) -> None:
        vecs = [e["feature_vector"] for e in self.entries if "feature_vector" in e]
        if not vecs:
            self._mean = self._std = None
            return
        arr = np.asarray(vecs, dtype=float)
        self._mean = arr.mean(axis=0)
        self._std = arr.std(axis=0) + 1e-6

    def _standardize(self, vec: list[float]) -> np.ndarray:
        v = np.asarray(vec, dtype=float)
        if self._mean is None:
            return v
        return (v - self._mean) / self._std

    # ---------- 検索 ----------

    def search(self, *, query: Optional[str] = None, reference: Optional[AudioProfile] = None,
               limit: int = 10, embedding: Optional[list[float]] = None) -> list[SampleHit]:
        if not self.entries:
            return []
        q_tokens = set(tokenize(query)) if query else set()
        ref_vec = self._standardize(reference.feature_vector) if reference else None
        emb = np.asarray(embedding, dtype=float) if embedding is not None else None

        hits: list[SampleHit] = []
        for e in self.entries:
            if "feature_vector" not in e:
                continue
            score = 0.0
            if ref_vec is not None:
                v = self._standardize(e["feature_vector"])
                denom = (np.linalg.norm(v) * np.linalg.norm(ref_vec)) + 1e-9
                score += float(v @ ref_vec / denom)  # -1..1
            if emb is not None and "embedding" in e:
                ev = np.asarray(e["embedding"], dtype=float)
                score += float(ev @ emb / ((np.linalg.norm(ev) * np.linalg.norm(emb)) + 1e-9))
            if q_tokens:
                overlap = len(q_tokens & set(e["tags"]))
                if overlap == 0 and ref_vec is None and emb is None:
                    continue  # キーワードのみの検索で不一致なら除外
                score += 0.5 * overlap
            hits.append(
                SampleHit(
                    path=e["path"],
                    name=e["name"],
                    source="local",
                    score=round(score, 4),
                    duration_sec=e.get("duration_sec"),
                    tags=e.get("tags", []),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]
