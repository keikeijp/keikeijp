import pytest

from dtm_agent.models import Note
from dtm_agent.music import chord_pitches, notes_to_midi_file, parse_key, scale_pitches, snap_to_scale, validate_notes
from dtm_agent.music.melody import midi_file_to_notes


def test_parse_key_variants():
    assert parse_key("A minor") == (9, "minor")
    assert parse_key("Am") == (9, "minor")
    assert parse_key("Bb major") == (10, "major")
    assert parse_key("F#") == (6, "major")
    with pytest.raises(ValueError):
        parse_key("H major")


def test_scale_and_snap():
    assert scale_pitches("C major", 60, 71) == [60, 62, 64, 65, 67, 69, 71]
    assert snap_to_scale(61, "C major") == 60
    assert snap_to_scale(70, "A minor") in (69, 71)


def test_chords():
    assert chord_pitches("C major", 0, octave=4) == [60, 64, 67]
    assert chord_pitches("A minor", 0, octave=3) == [57, 60, 64]
    assert chord_pitches("C major", 6, octave=4) == [71, 74, 77]  # B dim


def test_validate_and_midi_roundtrip(tmp_path):
    notes = [Note(pitch=69, start_beat=0, duration_beats=1), Note(pitch=72, start_beat=1, duration_beats=0.5, velocity=90)]
    assert validate_notes(notes, "A minor") == []
    assert validate_notes([Note(pitch=70, start_beat=0, duration_beats=1)], "A minor")
    path = notes_to_midi_file(notes, tmp_path / "x.mid", bpm=100)
    back = midi_file_to_notes(path)
    assert [(n.pitch, n.start_beat, n.duration_beats, n.velocity) for n in back] == [(69, 0.0, 1.0, 100), (72, 1.0, 0.5, 90)]
