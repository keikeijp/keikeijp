import json

from jevlab.apps import sponsor_skip as ss
from jevlab.core import FunctionBackend, Jev, ScriptedBackend


def _cues(n: int = 12, step: float = 10.0) -> list[ss.Cue]:
    return [ss.Cue(i * step, (i + 1) * step, f"line {i}") for i in range(n)]


def test_windowing_20s_with_50_percent_overlap():
    windows = ss.make_windows(_cues(), size=20.0, overlap=0.5)
    assert windows[0]["start"] == 0.0 and windows[0]["end"] == 20.0
    assert windows[1]["start"] == 10.0  # 50% 重なり → 10 秒ステップ
    assert windows[0]["text"] == "line 0 line 1"
    assert windows[-1]["end"] == 120.0
    assert len(windows) == 12


def test_merge_with_hysteresis_and_scripted_backend():
    # 12 ウィンドウ: 3〜5 がスポンサー (5 は継続閾値ギリギリ)、8 は単発で弱い → 無視
    script = []
    for i in range(12):
        if i in (3, 4):
            script.append({"segment_kind": "sponsor", "is_paid_promotion": True})
        elif i == 5:
            script.append({"segment_kind": {"content": 0.55, "sponsor": 0.45}, "is_paid_promotion": 0.5})
        elif i == 8:
            script.append({"segment_kind": {"content": 0.5, "self_promotion": 0.5}, "is_paid_promotion": False})
        else:
            script.append({"segment_kind": "content", "is_paid_promotion": False})
    backend = ScriptedBackend(script)
    result = ss.detect(Jev(backend, max_workers=1), _cues(), size=20.0, overlap=0.5)
    assert len(result["segments"]) == 1
    seg = result["segments"][0]
    assert seg["category"] == "sponsor"
    assert seg["segment"] == [30.0, 70.0]  # 3 で開始、5 (non_content 0.45 >= 0.4) まで継続
    assert len(backend.calls) == 12
    assert backend.calls[0]["state"]["text"] == "line 0 line 1"


def test_merge_categories_map_to_sponsorblock():
    results = [
        {"index": 0, "start": 0.0, "end": 20.0, "kind": "interaction_reminder", "non_content": 0.9, "confidence": 0.9, "paid": 0.1},
        {"index": 1, "start": 10.0, "end": 30.0, "kind": "content", "non_content": 0.2, "confidence": 0.8, "paid": 0.0},
        {"index": 2, "start": 20.0, "end": 40.0, "kind": "self_promotion", "non_content": 0.7, "confidence": 0.9, "paid": 0.2},
    ]
    segments = ss.merge_segments(results)
    assert [s["category"] for s in segments] == ["interaction", "selfpromo"]


def test_srt_input_and_main(tmp_path, capsys):
    srt = tmp_path / "t.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:15,000\nToday's video is sponsored by Acme VPN, use code JEV.\n\n2\n00:00:15,000 --> 00:00:40,000\nNow let's get into the tutorial.\n", encoding="utf-8")
    cues = ss.parse_srt(srt.read_text(encoding="utf-8"))
    assert len(cues) == 2 and cues[1].end == 40.0
    out = tmp_path / "segments.json"
    assert ss.main(["--backend", "mock", "detect", "--srt", str(srt), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "segments" in data and data["windows"] >= 2

    jev = Jev(FunctionBackend(lambda s, q: {"segment_kind": "sponsor" if "sponsored" in s["text"] else "content", "is_paid_promotion": "sponsored" in s["text"]}))
    result = ss.detect(jev, cues, size=20.0, overlap=0.5)
    assert result["segments"][0]["segment"][0] == 0.0 and result["segments"][0]["category"] == "sponsor"

    script = tmp_path / "skip.user.js"
    assert ss.main(["--emit-userscript", str(script)]) == 0
    assert "==UserScript==" in script.read_text(encoding="utf-8")
