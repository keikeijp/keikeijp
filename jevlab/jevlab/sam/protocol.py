"""SAM 3.1 (Meta Model API) の出力プロトコル。

API は Responses API の output_text レーンに、フレームごとに 1 行:

    <Nf>id<|box;x1=..;y1=..;x2=..;y2=..;w=<frameW>;h=<frameH>|><|mask;x=0;y=0;data=<H>,<W>,<enc>payload|>,id<|box...

を返す。box は inclusive 座標、mask は box ローカルの H×W ラスタ、payload は base85 のアリスメティック符号
(`~` = lossless, `!` = one_bit)。マスクの復号は公式の `meta-sam-parser` (依存なしの純 Python) に任せ、
未インストール時は box だけを返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_HEADER = re.compile(r"^<([0-9]+)f>(.*)$")
_RECORD = re.compile(
    r"^(?:,)?([0-9]+)"
    r"<\|box;x1=(-?[0-9]+);y1=(-?[0-9]+);x2=(-?[0-9]+);y2=(-?[0-9]+);w=([0-9]+);h=([0-9]+)\|>"
    r"<\|mask;x=0;y=0;data=([0-9]+),([0-9]+),([!~][^|]+)\|>"
)


@dataclass
class Mask:
    encoding: str  # "lossless" | "one_bit"
    payload: str
    width: int
    height: int
    _raster: bytes | None = field(default=None, repr=False)

    def raster(self) -> bytes | None:
        """行優先の 0/1 バイト列 (height*width)。meta-sam-parser が無ければ None。"""
        if self._raster is not None:
            return self._raster
        try:
            from meta_sam_parser import SegmentationMask, decode_mask_to_raster
        except ImportError:
            return None
        self._raster = decode_mask_to_raster(SegmentationMask(encoding=self.encoding, payload=self.payload, width=self.width, height=self.height))  # type: ignore[arg-type]
        return self._raster

    def area(self) -> int | None:
        raster = self.raster()
        return sum(raster) if raster is not None else None


@dataclass
class SamObject:
    object_id: str
    frame: int
    x1: int
    y1: int
    x2: int  # exclusive
    y2: int  # exclusive
    frame_width: int
    frame_height: int
    mask: Mask | None = None
    prompt: str = ""
    confidence: float | None = None

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def box_area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def mask_area(self) -> int:
        if self.mask is not None:
            area = self.mask.area()
            if area is not None:
                return area
        return self.box_area

    def box(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    def to_dict(self, include_mask: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.object_id,
            "prompt": self.prompt,
            "frame": self.frame,
            "box": [self.x1, self.y1, self.x2, self.y2],
            "frame_size": [self.frame_width, self.frame_height],
            "area": self.mask_area(),
        }
        if self.confidence is not None:
            data["confidence"] = self.confidence
        if include_mask and self.mask is not None:
            data["mask"] = {"encoding": self.mask.encoding, "width": self.mask.width, "height": self.mask.height, "payload": self.mask.payload}
        return data


@dataclass
class ParseResult:
    objects: list[SamObject]
    text_lines: list[str]
    diagnostics: list[str]

    def by_frame(self) -> dict[int, list[SamObject]]:
        frames: dict[int, list[SamObject]] = {}
        for obj in self.objects:
            frames.setdefault(obj.frame, []).append(obj)
        return frames


def parse_output_text(output_text: str, prompt: str = "") -> ParseResult:
    """SAM 3.1 の output_text をオブジェクトのリストに変換する。"""
    objects: list[SamObject] = []
    texts: list[str] = []
    diagnostics: list[str] = []
    for line_no, raw in enumerate(output_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        header = _HEADER.match(line)
        if header is None:
            texts.append(raw)
            continue
        frame = int(header.group(1))
        remainder = header.group(2)
        while remainder:
            match = _RECORD.match(remainder)
            if match is None:
                diagnostics.append(f"line {line_no}: 解釈できないレコード: {remainder[:60]!r}")
                break
            object_id, x1, y1, x2, y2, w, h, mh, mw, payload = match.groups()
            mask = Mask(encoding="lossless" if payload.startswith("~") else "one_bit", payload=payload, width=int(mw), height=int(mh))
            objects.append(
                SamObject(
                    object_id=object_id,
                    frame=frame,
                    x1=int(x1),
                    y1=int(y1),
                    x2=int(x2) + 1,
                    y2=int(y2) + 1,
                    frame_width=int(w),
                    frame_height=int(h),
                    mask=mask,
                    prompt=prompt,
                )
            )
            remainder = remainder[match.end() :]
    return ParseResult(objects=objects, text_lines=texts, diagnostics=diagnostics)


def format_output_text(objects: Iterable[SamObject]) -> str:
    """テストやモック用: オブジェクト → プロトコル文字列 (mask payload はそのまま埋め込む)。"""
    frames: dict[int, list[SamObject]] = {}
    for obj in objects:
        frames.setdefault(obj.frame, []).append(obj)
    lines = []
    for frame in sorted(frames):
        records = []
        for obj in frames[frame]:
            mask = obj.mask or Mask("one_bit", "!", obj.width, obj.height)
            records.append(
                f"{obj.object_id}<|box;x1={obj.x1};y1={obj.y1};x2={obj.x2 - 1};y2={obj.y2 - 1};w={obj.frame_width};h={obj.frame_height}|>"
                f"<|mask;x=0;y=0;data={mask.height},{mask.width},{mask.payload}|>"
            )
        lines.append(f"<{frame}f>" + ",".join(records))
    return "\n".join(lines) + ("\n" if lines else "")
