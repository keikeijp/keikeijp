from dtm_agent.reference import parse_spotify_track_id, resolve_spotify


def test_parse_ids():
    assert parse_spotify_track_id("https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp?si=abc") == "3n3Ppam7vgaVa1iaRUc9Lp"
    assert parse_spotify_track_id("https://open.spotify.com/intl-ja/track/3n3Ppam7vgaVa1iaRUc9Lp") == "3n3Ppam7vgaVa1iaRUc9Lp"
    assert parse_spotify_track_id("spotify:track:3n3Ppam7vgaVa1iaRUc9Lp") == "3n3Ppam7vgaVa1iaRUc9Lp"
    assert parse_spotify_track_id("https://example.com") is None


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    def post(self, url, **kw):
        return FakeResp({"access_token": "tok"})

    def get(self, url, headers=None, **kw):
        assert headers["Authorization"] == "Bearer tok"
        return FakeResp({"name": "Song", "artists": [{"name": "Artist"}], "album": {"name": "Alb"}, "duration_ms": 201000})


def test_resolve_with_credentials(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    meta = resolve_spotify("spotify:track:3n3Ppam7vgaVa1iaRUc9Lp", session=FakeSession())
    assert meta.title == "Song" and meta.artists == ["Artist"] and meta.duration_sec == 201.0
