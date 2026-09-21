"""SAM の検出結果 → Jev に渡せる「シーングラフ」(構造化 state)。

Jev はテキスト専用なので、画像は「何がどこにどれくらいの大きさで、何と重なっているか」の JSON に落とす。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from jevlab.sam.protocol import SamObject


@dataclass
class Relation:
    subject: str
    predicate: str  # "inside" | "overlaps" | "above" | "below" | "left_of" | "right_of" | "touching" | "near"
    object: str
    value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "predicate": self.predicate, "object": self.object, "value": round(self.value, 3)}


@dataclass
class Scene:
    width: int
    height: int
    objects: list[SamObject]
    relations: list[Relation] = field(default_factory=list)
    zones: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    frame: int = 0

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for obj in self.objects:
            counts[obj.prompt] = counts.get(obj.prompt, 0) + 1
        return counts

    def find(self, prompt: str) -> list[SamObject]:
        return [obj for obj in self.objects if obj.prompt == prompt]

    def to_state(self, max_objects: int = 40) -> dict[str, Any]:
        """Jev に渡す state。座標はフレーム比率 (0..1) に正規化して、サイズ非依存にする。"""
        w = self.width or 1
        h = self.height or 1
        objects = sorted(self.objects, key=lambda o: o.mask_area(), reverse=True)[:max_objects]
        return {
            "frame": self.frame,
            "frame_size": [self.width, self.height],
            "counts": self.counts(),
            "objects": [
                {
                    "id": obj.object_id,
                    "label": obj.prompt,
                    "box": [round(obj.x1 / w, 3), round(obj.y1 / h, 3), round(obj.x2 / w, 3), round(obj.y2 / h, 3)],
                    "area_ratio": round(obj.mask_area() / (w * h), 4),
                    "position": describe_position(obj, w, h),
                    "zones": [name for name, zone in self.zones.items() if inside_ratio(obj.box(), zone) > 0.5],
                }
                for obj in objects
            ],
            "relations": [relation.to_dict() for relation in self.relations],
            "zones": {name: [round(z[0] / w, 3), round(z[1] / h, 3), round(z[2] / w, 3), round(z[3] / h, 3)] for name, z in self.zones.items()},
        }

    def describe(self) -> str:
        """人間向けの 1 段落説明 (Jev の state に文章として渡す時にも使う)。"""
        parts = [f"{count} x {label}" for label, count in self.counts().items()]
        lines = ["Objects: " + (", ".join(parts) if parts else "none")]
        for relation in self.relations:
            lines.append(f"{relation.subject} {relation.predicate} {relation.object}")
        return "\n".join(lines)


def inside_ratio(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> float:
    """inner の面積のうち outer に含まれる割合。"""
    ix1, iy1, ix2, iy2 = inner
    ox1, oy1, ox2, oy2 = outer
    area = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if area == 0:
        return 0.0
    overlap = max(0, min(ix2, ox2) - max(ix1, ox1)) * max(0, min(iy2, oy2) - max(iy1, oy1))
    return overlap / area


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def describe_position(obj: SamObject, width: int, height: int) -> str:
    cx, cy = obj.center
    horizontal = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "center"
    vertical = "top" if cy < height / 3 else "bottom" if cy > 2 * height / 3 else "middle"
    return f"{vertical}-{horizontal}"


def build_scene(objects: Iterable[SamObject], width: int, height: int, zones: dict[str, tuple[int, int, int, int]] | None = None, frame: int = 0, near_ratio: float = 0.05) -> Scene:
    """オブジェクト同士の空間関係を計算してシーンを作る。"""
    objects = list(objects)
    relations: list[Relation] = []
    diag = ((width**2 + height**2) ** 0.5) or 1.0
    for a in objects:
        for b in objects:
            if a is b:
                continue
            ratio = inside_ratio(a.box(), b.box())
            if ratio > 0.8 and a.box_area < b.box_area:
                relations.append(Relation(a.object_id, "inside", b.object_id, ratio))
            elif ratio > 0.1 and a.object_id < b.object_id:
                relations.append(Relation(a.object_id, "overlaps", b.object_id, iou(a.box(), b.box())))
        for b in objects:
            if a is b or a.object_id >= b.object_id:
                continue
            ax, ay = a.center
            bx, by = b.center
            dx, dy = bx - ax, by - ay
            gap_x = max(a.x1, b.x1) - min(a.x2, b.x2)
            gap_y = max(a.y1, b.y1) - min(a.y2, b.y2)
            if gap_x <= 0 and gap_y <= 0:
                continue  # 重なりは上で扱った
            distance = max(gap_x, 0) ** 2 + max(gap_y, 0) ** 2
            if distance ** 0.5 <= near_ratio * diag:
                relations.append(Relation(a.object_id, "near", b.object_id, distance**0.5 / diag))
            if abs(dy) > abs(dx) * 1.5:
                relations.append(Relation(a.object_id, "above" if dy > 0 else "below", b.object_id, abs(dy) / (height or 1)))
            elif abs(dx) > abs(dy) * 1.5:
                relations.append(Relation(a.object_id, "left_of" if dx > 0 else "right_of", b.object_id, abs(dx) / (width or 1)))
    return Scene(width=width, height=height, objects=objects, relations=relations, zones=dict(zones or {}), frame=frame)


__all__ = ["Scene", "Relation", "build_scene", "inside_ratio", "iou", "describe_position"]
