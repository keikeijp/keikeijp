import json

from jevlab.apps import meter
from jevlab.core import Jev, ScriptedBackend

SRT = """1
00:00:01,000 --> 00:00:03,500
This is absolutely the best product ever.

2
00:00:03,600 --> 00:00:05,000
Buy it now, only today!

3
00:00:09,000 --> 00:00:11,000
Maybe it works, I am not sure.
"""

VTT = """WEBVTT

NOTE this is a note

00:01.000 --> 00:03.500
<v Speaker>This is <b>absolutely</b> the best.</v>

cue-2
00:00:03.600 --> 00:00:05.000
Buy it now.
"""


def test_parse_srt_and_vtt():
    cues = meter.parse_srt(SRT)
    assert len(cues) == 3
    assert cues[0].start == 1.0 and cues[0].end == 3.5
    assert cues[2].text.startswith("Maybe")
    vtt = meter.parse_vtt(VTT)
    assert len(vtt) == 2
    assert vtt[0].text == "This is absolutely the best."
    assert vtt[1].start == 3.6
    assert meter.parse_timestamp("01:02:03.004") == 3723.004


def test_segment_utterances_splits_on_gap_and_sentence_end():
    cues = meter.parse_srt(SRT)
    utterances = meter.segment_utterances(cues)
    # 1 は文末で区切れる、2 と 3 の間は 4 秒の gap → 3 発話
    assert [u.text for u in utterances] == [c.text for c in cues]
    joined = meter.segment_utterances([meter.Cue(0, 1, "hello"), meter.Cue(1.2, 2, "world.")])
    assert len(joined) == 1 and joined[0].text == "hello world."


def test_scoring_and_ass_generation():
    backend = ScriptedBackend([{"confidence_of_claim": 4, "salesmanship": 3}, {"confidence_of_claim": 3, "salesmanship": 4}, {"confidence_of_claim": 0, "salesmanship": 0}])
    axes = {k: meter.DEFAULT_AXES[k] for k in ("confidence_of_claim", "salesmanship")}
    utterances = meter.segment_utterances(meter.parse_srt(SRT))
    timeline = meter.score_utterances(Jev(backend, max_workers=1), utterances, axes)
    assert timeline[0]["scores"]["confidence_of_claim"]["level"] == 4
    assert timeline[2]["scores"]["salesmanship"]["level"] == 0
    assert backend.calls[1]["state"]["previous"] == [utterances[0].text]
    ass = meter.to_ass(timeline, axes)
    assert "[V4+ Styles]" in ass and ass.count("Dialogue:") == 3
    assert "Dialogue: 0,0:00:01.00,0:00:03.50,Meter" in ass
    assert meter.meter_bar(1.0) == "▰▰▰▰▰" and meter.meter_bar(0.0) == "▱▱▱▱▱" and meter.meter_bar(0.5) == "▰▰▰▱▱"
    assert meter.ass_color(0.0) == "&H0000FF00&" and meter.ass_color(1.0) == "&H000000FF&"
    svg = meter.svg_sparkline(timeline, "salesmanship")
    assert svg.startswith("<svg") and "polyline" in svg


def test_main_writes_outputs(tmp_path, capsys):
    srt = tmp_path / "t.srt"
    srt.write_text(SRT, encoding="utf-8")
    out = tmp_path / "out"
    code = meter.main(["--transcript", str(srt), "--out-dir", str(out), "--backend", "mock", "--ffmpeg-cmd", "--video", "v.mp4"])
    assert code == 0
    assert (out / "meter.ass").exists() and (out / "sparkline_salesmanship.svg").exists()
    data = json.loads((out / "timeline.json").read_text(encoding="utf-8"))
    assert len(data["timeline"]) == 3
    assert "ffmpeg" in capsys.readouterr().out
