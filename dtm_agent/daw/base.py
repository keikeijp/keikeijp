"""DAW ブリッジの共通インターフェース。

ProjectPlan (設計図) を受け取り、DAW 上 (またはプロジェクトファイル上) に実体化する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import ProjectPlan


class DawBridge(Protocol):
    name: str

    def realize(self, plan: ProjectPlan) -> list[Path]:
        """設計図を DAW に反映し、生成したファイル一覧を返す。"""
        ...
