"""参照曲の解決。

Spotify URL → トラックのメタデータ (曲名 / アーティスト / 長さ)。
音声そのものは Spotify から取得できない (規約 + 2024 年 11 月の audio-features / preview 廃止) ため、
解析には手元の音声ファイル (--audio) を使う。メタデータはエージェントが検索語を作る材料になる。
"""

from __future__ import annotations

import base64
import os
import re
from typing import Optional

from pydantic import BaseModel

_SPOTIFY_TRACK_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([A-Za-z0-9]{22})")


class TrackMeta(BaseModel):
    provider: str
    id: str
    title: Optional[str] = None
    artists: list[str] = []
    album: Optional[str] = None
    duration_sec: Optional[float] = None
    url: Optional[str] = None
    note: Optional[str] = None


def parse_spotify_track_id(text: str) -> Optional[str]:
    m = _SPOTIFY_TRACK_RE.search(text or "")
    return m.group(1) if m else None


def _spotify_token(client_id: str, client_secret: str, session) -> str:
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = session.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def resolve_spotify(url_or_id: str, *, session=None) -> TrackMeta:
    """Client Credentials フローで Spotify Web API からメタデータを取る。

    環境変数 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET が無ければ ID だけ返す。
    """
    track_id = parse_spotify_track_id(url_or_id) or url_or_id
    meta = TrackMeta(provider="spotify", id=track_id, url=f"https://open.spotify.com/track/{track_id}")
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        meta.note = "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET 未設定のためメタデータ未取得"
        return meta
    import requests

    session = session or requests.Session()
    token = _spotify_token(cid, secret, session)
    resp = session.get(
        f"https://api.spotify.com/v1/tracks/{track_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    meta.title = data.get("name")
    meta.artists = [a["name"] for a in data.get("artists", [])]
    meta.album = (data.get("album") or {}).get("name")
    meta.duration_sec = (data.get("duration_ms") or 0) / 1000.0 or None
    return meta


def resolve_reference(text: str) -> Optional[TrackMeta]:
    """URL らしき文字列から対応プロバイダを判定して解決する。未対応なら None。"""
    if parse_spotify_track_id(text):
        return resolve_spotify(text)
    return None
