"""ツール層のテスト。Claude API は呼ばず、runner が行うのと同じ形で tool.call() する。"""

import json

from dtm_agent.agent import Session
from dtm_agent.agent.tools import build_tools
from dtm_agent.samples import LocalLibrary


def tools_for(session):
    return {t.name: t for t in build_tools(session)}


def call(tools, name, **kwargs):
    return json.loads(tools[name].call(kwargs))


def test_full_offline_flow(tmp_path, sample_library, reference_wav):
    LocalLibrary(sample_library).build()
    session = Session(out_dir=tmp_path / "out", library_dir=sample_library, daw="reaper")
    tools = tools_for(session)

    ref = call(tools, "analyze_reference_audio", audio_path=str(reference_wav), segment="0-8")
    assert ref["key"] == "A minor"

    hits = call(tools, "search_samples", query="kick", use_reference=False, sources="local")
    assert len(hits["local"]) == 2
    assert "error" not in hits["local"][0]

    assert call(tools, "set_project", title="lofi_demo", bpm=ref["bpm"], key=ref["key"])["ok"]
    info = call(tools, "scale_info", key="A minor")
    assert 69 in info["scale_pitches_C3_C6"]

    bad = call(tools, "add_midi_clip", track_name="Lead", clip_name="a", length_beats=4,
               notes=[{"pitch": 70, "start_beat": 0, "duration_beats": 1}])
    assert bad["ok"] is False and bad["problems"]

    good = call(tools, "add_midi_clip", track_name="Lead", clip_name="a", length_beats=4, instrument_hint="pluck",
                notes=[{"pitch": 69, "start_beat": 0, "duration_beats": 1},
                       {"pitch": 72, "start_beat": 1, "duration_beats": 1}])
    assert good["ok"] and good["notes"] == 2

    too_long = call(tools, "add_midi_clip", track_name="Lead", clip_name="b", length_beats=2,
                    notes=[{"pitch": 69, "start_beat": 1.5, "duration_beats": 1}])
    assert too_long["ok"] is False

    assert call(tools, "add_audio_clip", track_name="Drums", sample_path=hits["local"][0]["path"])["ok"]
    assert call(tools, "add_note_for_user", text="Lead は Serum の pluck 系に")["ok"]

    out = call(tools, "realize_in_daw")
    assert out["daw"] == "reaper" and any(f.endswith(".rpp") for f in out["files"])

    plan = json.loads(tools["show_plan"].call({}))
    assert [t["name"] for t in plan["tracks"]] == ["Lead", "Drums"]
    assert plan["notes_for_user"] == ["Lead は Serum の pluck 系に"]


def test_search_without_library_reports_error(tmp_path):
    tools = tools_for(Session(out_dir=tmp_path))
    res = call(tools, "search_samples", query="kick", sources="local")
    assert "error" in res["local"]


def test_resolve_reference_url_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    tools = tools_for(Session(out_dir=tmp_path))
    res = call(tools, "resolve_reference_url", url="https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp?si=x")
    assert res["provider"] == "spotify" and res["id"] == "3n3Ppam7vgaVa1iaRUc9Lp"
    assert "未設定" in res["note"]


def test_tool_schemas_are_valid_json_schema():
    for tool in build_tools(Session(out_dir="/tmp/x")):
        d = tool.to_dict()
        assert d["name"] and d["description"]
        assert d["input_schema"]["type"] == "object"
