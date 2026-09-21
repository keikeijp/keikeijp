"""API キー無しで SceneJudge を一通り動かすデモ。

合成画像 (色付き矩形) を Pillow で描き、MockSam に「どこに何があるか」を教えて、
Jev (mock か実 API) に机の散らかり具合を判定させる。Pillow が無ければ PNG を stdlib で書く。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from jevlab.core import Jev
from jevlab.sam import MockSam, Sam
from jevlab.scenejudge.judge import SceneJudge, render_overlay, write_report
from jevlab.scenejudge.scenarios import BUILTIN

W, H = 640, 400

DESK_OBJECTS = {
    "cup": [(60, 220, 120, 300)],
    "bottle": [(140, 150, 180, 300)],
    "paper": [(200, 240, 420, 330), (380, 260, 560, 340)],
    "book": [(430, 120, 600, 240)],
    "cable": [(20, 330, 300, 350)],
    "laptop": [(220, 60, 460, 230)],
    "phone": [(500, 300, 560, 380)],
    "trash": [(600, 320, 630, 380)],
}
TIDY_OBJECTS = {"laptop": [(220, 60, 460, 230)], "cup": [(520, 120, 580, 200)]}

COLORS = {"cup": (230, 90, 90), "bottle": (90, 150, 230), "paper": (240, 240, 210), "book": (120, 200, 120), "cable": (40, 40, 40), "laptop": (90, 90, 110), "phone": (20, 20, 20), "trash": (150, 120, 60)}


def write_png(path: Path, objects: dict[str, list[tuple[int, int, int, int]]]) -> Path:
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (W, H), (200, 170, 130))
        draw = ImageDraw.Draw(image)
        for label, boxes in objects.items():
            for box in boxes:
                draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=COLORS.get(label, (255, 255, 255)))
        image.save(path)
        return path
    except ImportError:
        pass
    # stdlib で最低限の PNG
    rows = []
    for y in range(H):
        row = bytearray([200, 170, 130] * W)
        for label, boxes in objects.items():
            color = COLORS.get(label, (255, 255, 255))
            for x1, y1, x2, y2 in boxes:
                if y1 <= y < y2:
                    for x in range(x1, x2):
                        row[x * 3 : x * 3 + 3] = bytes(color)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    return path


def run_demo(out_dir: Path, backend: str | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    jev = Jev(backend, cache=True)
    scenario = BUILTIN["desk"]
    verdicts = []
    for name, objects in (("messy_desk", DESK_OBJECTS), ("tidy_desk", TIDY_OBJECTS)):
        image_path = write_png(out_dir / f"{name}.png", objects)
        judge = SceneJudge(sam=Sam(MockSam(objects)), jev=jev)
        verdict = judge.judge_image(image_path, scenario)
        verdicts.append(verdict)
        print(verdict.summary())
        try:
            print(f"overlay -> {render_overlay(image_path, verdict, out_dir / f'{name}_judged.png')}")
        except RuntimeError as error:
            print(f"(overlay skipped: {error})")
    print(f"report -> {write_report(verdicts, [], out_dir / 'report.json')}")
    print(f"jev stats: {jev.stats()}")
    return 0
