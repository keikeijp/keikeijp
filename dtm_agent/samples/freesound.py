"""Freesound (https://freesound.org) のテキスト検索。世界中の CC ライセンスサンプルを探せる。

API キーは環境変数 FREESOUND_API_KEY で渡す。キーが無ければ空の結果を返す。
"""

from __future__ import annotations

import os
from typing import Optional

from ..models import AudioProfile, SampleHit

_API = "https://freesound.org/apiv2/search/text/"


class FreesoundSource:
    name = "freesound"

    def __init__(self, api_key: Optional[str] = None, *, session=None) -> None:
        self.api_key = api_key or os.environ.get("FREESOUND_API_KEY")
        self._session = session

    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, *, query: Optional[str] = None, reference: Optional[AudioProfile] = None,
               limit: int = 10) -> list[SampleHit]:
        if not self.available() or not query:
            return []
        import requests

        session = self._session or requests
        params = {
            "query": query,
            "token": self.api_key,
            "page_size": limit,
            "fields": "id,name,duration,tags,license,url,previews",
            "filter": "duration:[0.1 TO 30]",
        }
        resp = session.get(_API, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        hits = []
        for i, r in enumerate(results):
            preview = (r.get("previews") or {}).get("preview-hq-mp3") or ""
            hits.append(
                SampleHit(
                    path=preview,
                    name=r.get("name", str(r.get("id"))),
                    source="freesound",
                    score=round(1.0 - i / max(len(results), 1), 4),
                    duration_sec=r.get("duration"),
                    tags=list(r.get("tags") or []),
                    url=r.get("url"),
                    license=r.get("license"),
                )
            )
        return hits
