"""サンプル検索ソースの共通インターフェース。

ローカルライブラリ / Freesound / (将来) Splice などを同じ形で扱えるようにする。
"""

from __future__ import annotations

from typing import Optional, Protocol

from ..models import AudioProfile, SampleHit


class SampleSource(Protocol):
    name: str

    def search(
        self,
        *,
        query: Optional[str] = None,
        reference: Optional[AudioProfile] = None,
        limit: int = 10,
    ) -> list[SampleHit]:
        """テキストクエリと/または参照プロファイルで検索する。"""
        ...
