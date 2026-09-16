"""エージェントの作業状態 (参照曲プロファイル / サンプルライブラリ / 設計図)。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..models import AudioProfile, ProjectPlan
from ..samples import FreesoundSource, LocalLibrary


class Session:
    def __init__(self, *, out_dir: str | Path = "./dtm_agent_out",
                 library_dir: Optional[str | Path] = None, daw: str = "file",
                 ableton_host: str = "127.0.0.1", ableton_port: int = 11000) -> None:
        self.out_dir = Path(out_dir)
        self.daw = daw
        self.ableton_host, self.ableton_port = ableton_host, ableton_port
        self.library: Optional[LocalLibrary] = LocalLibrary(library_dir) if library_dir else None
        self.freesound = FreesoundSource()
        self.plan = ProjectPlan()
        self.reference: Optional[AudioProfile] = None
        self.reference_meta: Optional[dict] = None
        self.log: list[str] = []
