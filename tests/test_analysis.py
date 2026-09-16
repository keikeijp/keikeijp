from dtm_agent.analysis import analyze_file, parse_segment


def test_analyze_detects_key_and_tempo(reference_wav):
    p = analyze_file(reference_wav)
    assert p.key == "A minor"
    assert p.key_confidence > 0.6
    assert 110 <= p.bpm <= 130
    assert abs(sum(p.chroma) - 1.0) < 1e-3
    assert len(p.feature_vector) == 20


def test_analyze_segment(reference_wav):
    p = analyze_file(reference_wav, 2.0, 5.0)
    assert abs(p.duration_sec - 3.0) < 0.05
    assert p.start_sec == 2.0 and p.end_sec == 5.0


def test_parse_segment():
    assert parse_segment("1:05-1:21") == (65.0, 81.0)
    assert parse_segment("32-48") == (32.0, 48.0)
    assert parse_segment("10") == (10.0, None)
    assert parse_segment(None) == (0.0, None)
