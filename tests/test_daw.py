import mido

from dtm_agent.daw import AbletonOscBridge, FileBridge, ReaperProjectBridge, get_bridge
from dtm_agent.daw.reaper import render_rpp
from dtm_agent.models import AudioClip, MidiClip, Note, ProjectPlan, Track


def make_plan(sample_path=None):
    plan = ProjectPlan(title="demo", bpm=100, key="A minor")
    plan.add_track(Track(name="Lead", kind="midi", instrument_hint="pluck", midi_clips=[
        MidiClip(name="m", length_beats=4, notes=[
            Note(pitch=69, start_beat=0, duration_beats=1),
            Note(pitch=72, start_beat=1, duration_beats=1),
        ])]))
    if sample_path:
        plan.add_track(Track(name="Drums", kind="audio", audio_clips=[
            AudioClip(name="kick", path=str(sample_path), start_beat=0)]))
    return plan


def test_file_bridge(tmp_path, sample_library):
    kick = sample_library / "drums" / "kicks" / "kick_808_deep.wav"
    out = tmp_path / "out"
    written = FileBridge(out).realize(make_plan(kick))
    assert (out / "plan.json").exists()
    assert (out / "samples" / "kick_808_deep.wav").exists()
    mid = mido.MidiFile(out / "midi" / "Lead__m.mid")
    assert any(m.type == "note_on" and m.note == 69 for t in mid.tracks for m in t)
    assert "pluck" in (out / "README.txt").read_text()
    assert ProjectPlan.load(out / "plan.json").title == "demo"
    assert len(written) == 4


def test_reaper_rpp(tmp_path, sample_library):
    kick = sample_library / "drums" / "kicks" / "kick_808_deep.wav"
    text = render_rpp(make_plan(kick))
    assert text.startswith("<REAPER_PROJECT")
    assert "TEMPO 100 4 4" in text
    assert "<SOURCE MIDI" in text and "HASDATA 1 960 QN" in text
    assert "E 0 90 45 64" in text and "E 960 80 45 00" in text
    assert "<SOURCE WAVE" in text and "kick_808_deep.wav" in text
    # 括弧の対応
    assert text.count("<") == text.count(">") - text.count("->")
    files = ReaperProjectBridge(tmp_path).realize(make_plan())
    assert files[0].suffix == ".rpp" and files[0].exists()


def test_ableton_osc_messages(tmp_path):
    sent = []
    bridge = AbletonOscBridge(tmp_path, sender=lambda a, args: sent.append((a, args)), start_track_index=3)
    bridge.realize(make_plan())
    addrs = [a for a, _ in sent]
    assert addrs[0] == "/live/song/set/tempo" and sent[0][1] == [100.0]
    assert ("/live/song/create_midi_track", [3]) in sent
    assert ("/live/track/set/name", [3, "Lead"]) in sent
    assert ("/live/clip_slot/create_clip", [3, 0, 4.0]) in sent
    assert ("/live/clip/add/notes", [3, 0, 69, 0.0, 1.0, 100, 0]) in sent
    assert (tmp_path / "midi" / "Lead__m.mid").exists()  # フォールバック出力も残す


def test_get_bridge_unknown():
    import pytest

    with pytest.raises(ValueError):
        get_bridge("logic", "/tmp")
