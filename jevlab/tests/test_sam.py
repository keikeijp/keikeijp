import struct
import zlib

import pytest

from jevlab.sam import ImageInput, MockSam, Sam, build_scene, extract_output_text, format_output_text, parse_output_text, probe_size
from jevlab.sam.protocol import Mask, SamObject

SAMPLE = (
    "<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>"
    ",1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`=o3?X{emCgO$y@|>\n"
)


def make_png(width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_parse_official_sample():
    result = parse_output_text(SAMPLE, prompt="person")
    assert len(result.objects) == 2
    first = result.objects[0]
    assert (first.x1, first.y1, first.x2, first.y2) == (211, 228, 271, 255)
    assert first.frame_width == 320 and first.frame_height == 334
    assert first.mask.encoding == "lossless" and first.mask.height == 27 and first.mask.width == 60
    assert first.width == 60 and first.height == 27
    assert result.diagnostics == []


def test_mask_decodes_when_parser_installed():
    pytest.importorskip("meta_sam_parser")
    result = parse_output_text(SAMPLE)
    raster = result.objects[0].mask.raster()
    assert raster is not None and len(raster) == 27 * 60
    assert 0 < sum(raster) <= 27 * 60


def test_roundtrip_format_and_diagnostics():
    parsed = parse_output_text(SAMPLE)
    assert parse_output_text(format_output_text(parsed.objects)).objects[1].object_id == "1"
    broken = parse_output_text("<0f>0<|box;x1=1|>garbage\nplain text\n")
    assert broken.diagnostics and broken.text_lines == ["plain text"]


def test_probe_size_and_data_url():
    image = ImageInput.from_bytes(make_png(12, 7), mime="image/png")
    assert (image.width, image.height) == (12, 7)
    assert image.data_url().startswith("data:image/png;base64,")
    assert probe_size(b"not an image") == (0, 0)


def test_extract_output_text_shapes():
    assert extract_output_text({"output_text": "x"}) == "x"
    assert extract_output_text({"output": [{"content": [{"type": "output_text", "text": "a"}, {"type": "output_text", "text": "b"}]}]}) == "ab"


def test_mock_sam_and_scene_relations():
    sam = Sam(MockSam({"dog": [(10, 10, 50, 50)], "couch": [(0, 0, 100, 80)], "ball": [(60, 10, 70, 20)]}))
    seg = sam.segment(ImageInput.from_bytes(make_png(100, 100), "image/png"), ["dog", "couch", "ball", ""])
    assert seg.prompts == ["dog", "couch", "ball"]
    assert seg.by_prompt()["dog"][0].object_id == "dog#0"
    scene = build_scene(seg.objects, seg.width, seg.height, zones={"floor": (0, 80, 100, 100)})
    predicates = {(r.subject, r.predicate, r.object) for r in scene.relations}
    assert ("dog#0", "inside", "couch#0") in predicates
    state = scene.to_state()
    assert state["counts"] == {"dog": 1, "couch": 1, "ball": 1}
    assert state["objects"][0]["label"] == "couch"
    assert "Objects:" in scene.describe()


def test_meta_backend_request_shape(monkeypatch):
    import io
    import json
    import urllib.request

    from jevlab.sam import client as sam_client

    captured = {}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"output": [{"content": [{"type": "output_text", "text": SAMPLE}]}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("META_API_KEY", "meta-key")
    seg = Sam(sam_client.MetaSamBackend()).segment(ImageInput.from_bytes(make_png(320, 334), "image/png"), ["person"])
    assert captured["url"] == "https://api.meta.ai/v1/responses"
    assert captured["body"]["model"] == "sam-3.1"
    content = captured["body"]["input"][0]["content"]
    assert content[0]["type"] == "input_image" and content[0]["image_url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "input_text", "text": "person"}
    assert captured["auth"] == "Bearer meta-key"
    assert len(seg.objects) == 2 and seg.objects[0].prompt == "person"


def test_sam_object_helpers():
    obj = SamObject("a", 0, 0, 0, 10, 20, 100, 100, mask=Mask("one_bit", "!", 10, 20, _raster=bytes([1, 0] * 100)), prompt="x")
    assert obj.center == (5.0, 10.0)
    assert obj.mask_area() == 100
    assert obj.to_dict(include_mask=True)["mask"]["payload"] == "!"
