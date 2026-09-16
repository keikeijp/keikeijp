from dtm_agent.analysis import analyze_file
from dtm_agent.samples import LocalLibrary


def test_index_and_keyword_search(sample_library):
    lib = LocalLibrary(sample_library)
    assert lib.build() == 5
    assert lib.index_path.exists()

    reloaded = LocalLibrary(sample_library)
    assert len(reloaded.entries) == 5
    hits = reloaded.search(query="kick", limit=5)
    assert {h.name for h in hits} == {"kick_808_deep", "kick_punchy"}
    assert all("kicks" in h.tags for h in hits)


def test_reference_similarity_prefers_matching_timbre(sample_library, reference_wav):
    lib = LocalLibrary(sample_library)
    lib.build()
    ref = analyze_file(reference_wav)
    hits = lib.search(reference=ref, limit=5)
    # 同じ構成音の正弦波 pad が最も近く、ノイズ系ドラムはすべてそれより遠いはず
    assert hits[0].name == "pad_warm_Am" and hits[0].score > 0.5
    drum_scores = [h.score for h in hits if h.name.startswith(("kick_", "hat_"))]
    assert len(drum_scores) == 3 and max(drum_scores) < hits[0].score


def test_keyword_plus_reference(sample_library, reference_wav):
    lib = LocalLibrary(sample_library)
    lib.build()
    hits = lib.search(query="pad", reference=analyze_file(reference_wav), limit=2)
    assert [h.name for h in hits] == ["pad_warm_Am", "pad_bright_C"]
