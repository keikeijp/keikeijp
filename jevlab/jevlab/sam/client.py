"""SAM 3.1 クライアント (Meta Model API)。

Meta Model API は OpenAI Responses API 互換:

    POST https://api.meta.ai/v1/responses
    Authorization: Bearer $META_API_KEY   (MODEL_API_KEY も可)
    {"model": "sam-3.1", "input": [{"role": "user", "content": [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,..."},
        {"type": "input_text", "text": "dog"}]}]}

戻りは output_text レーン (`jevlab.sam.protocol` 参照)。openai SDK は不要で stdlib だけで呼ぶ。
モック (`MockSam`) は API なしで同じインターフェイスを提供し、ラベル付き矩形や単色ブロブ検出で
オブジェクトを返す。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jevlab.sam.protocol import Mask, ParseResult, SamObject, parse_output_text


class SamError(RuntimeError):
    pass


@dataclass
class Segmentation:
    """1 枚の画像 (または 1 本の動画) に対する、複数プロンプトの結果。"""

    objects: list[SamObject]
    width: int
    height: int
    prompts: list[str]
    raw_outputs: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    source: str = ""

    def by_prompt(self) -> dict[str, list[SamObject]]:
        groups: dict[str, list[SamObject]] = {prompt: [] for prompt in self.prompts}
        for obj in self.objects:
            groups.setdefault(obj.prompt, []).append(obj)
        return groups

    def by_frame(self) -> dict[int, list[SamObject]]:
        frames: dict[int, list[SamObject]] = {}
        for obj in self.objects:
            frames.setdefault(obj.frame, []).append(obj)
        return frames

    def to_dict(self, include_masks: bool = False) -> dict[str, Any]:
        return {
            "source": self.source,
            "size": [self.width, self.height],
            "prompts": list(self.prompts),
            "objects": [obj.to_dict(include_masks) for obj in self.objects],
            "diagnostics": list(self.diagnostics),
        }


class SamBackend(Protocol):
    name: str

    def segment(self, image: "ImageInput", prompt: str) -> tuple[ParseResult, str]: ...


@dataclass
class ImageInput:
    """画像入力。ファイルパス / bytes / data URL のどれでも。"""

    data: bytes
    mime: str = "image/jpeg"
    name: str = ""
    width: int = 0
    height: int = 0

    @classmethod
    def from_path(cls, path: str | Path) -> "ImageInput":
        path = Path(path)
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        image = cls(data=path.read_bytes(), mime=mime, name=str(path))
        image.width, image.height = probe_size(image.data)
        return image

    @classmethod
    def from_bytes(cls, data: bytes, mime: str = "image/jpeg", name: str = "") -> "ImageInput":
        image = cls(data=data, mime=mime, name=name)
        image.width, image.height = probe_size(data)
        return image

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def probe_size(data: bytes) -> tuple[int, int]:
    """PNG / JPEG / GIF のヘッダから幅と高さを読む (Pillow 不要)。不明なら (0, 0)。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            index += 2 + length
    return 0, 0


class MetaSamBackend:
    """Meta Model API 経由の SAM 3.1。"""

    name = "meta"

    def __init__(self, api_key: str | None = None, model: str = "sam-3.1", base_url: str | None = None, timeout: float = 60.0, extra_body: Mapping[str, Any] | None = None):
        self.api_key = api_key or os.environ.get("META_API_KEY", "").strip() or os.environ.get("MODEL_API_KEY", "").strip()
        if not self.api_key:
            raise SamError("META_API_KEY (または MODEL_API_KEY) が設定されていません")
        self.model = model
        self.base_url = (base_url or os.environ.get("META_API_BASE_URL", "").strip() or "https://api.meta.ai/v1").rstrip("/")
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})

    def segment(self, image: ImageInput, prompt: str) -> tuple[ParseResult, str]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": image.data_url()},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
        }
        body.update(self.extra_body)
        request = urllib.request.Request(f"{self.base_url}/responses", data=json.dumps(body).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise SamError(f"Meta Model API error {error.code}: {error.read().decode('utf-8', 'replace')[:300]}") from error
        except (urllib.error.URLError, OSError) as error:
            raise SamError(f"Meta Model API に接続できません: {error}") from error
        output_text = extract_output_text(payload)
        return parse_output_text(output_text, prompt), output_text


def extract_output_text(payload: Mapping[str, Any]) -> str:
    """Responses API のレスポンスから output_text を連結して取り出す。"""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


class MockSam:
    """API なしの SAM もどき。

    - `objects` に {prompt: [(x1, y1, x2, y2), ...]} を渡すと、そのプロンプトに対して矩形を返す
    - `detector` に関数 (image, prompt) -> [(x1,y1,x2,y2), ...] を渡すと任意の検出器を差し込める
    - どちらもなければ、画像に対して空の結果を返す
    """

    name = "mock"

    def __init__(self, objects: Mapping[str, Sequence[tuple[int, int, int, int]]] | None = None, detector: Any = None):
        self.objects = {prompt: list(boxes) for prompt, boxes in (objects or {}).items()}
        self.detector = detector
        self.calls: list[str] = []

    def segment(self, image: ImageInput, prompt: str) -> tuple[ParseResult, str]:
        self.calls.append(prompt)
        boxes = self.objects.get(prompt)
        if boxes is None and self.detector is not None:
            boxes = self.detector(image, prompt)
        objects = []
        for index, (x1, y1, x2, y2) in enumerate(boxes or []):
            objects.append(
                SamObject(
                    object_id=str(index),
                    frame=0,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    frame_width=image.width or max(x2, 1),
                    frame_height=image.height or max(y2, 1),
                    mask=Mask("one_bit", "!", x2 - x1, y2 - y1, _raster=bytes([1]) * ((x2 - x1) * (y2 - y1))),
                    prompt=prompt,
                )
            )
        return ParseResult(objects=objects, text_lines=[], diagnostics=[]), ""


class Sam:
    """複数プロンプトをまとめて投げる高レベル API。"""

    def __init__(self, backend: SamBackend | None = None, max_workers: int = 4):
        if backend is None:
            backend = backend_from_env()
        self.backend = backend
        self.max_workers = max_workers

    def segment(self, image: ImageInput | str | Path | bytes, prompts: Iterable[str]) -> Segmentation:
        if isinstance(image, (str, Path)):
            image = ImageInput.from_path(image)
        elif isinstance(image, bytes):
            image = ImageInput.from_bytes(image)
        prompts = [p for p in dict.fromkeys(prompts) if p]
        results: list[tuple[ParseResult, str]]
        if len(prompts) <= 1 or self.max_workers <= 1:
            results = [self.backend.segment(image, prompt) for prompt in prompts]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                results = list(pool.map(lambda prompt: self.backend.segment(image, prompt), prompts))
        objects: list[SamObject] = []
        raw: dict[str, str] = {}
        diagnostics: list[str] = []
        width, height = image.width, image.height
        for prompt, (parsed, output_text) in zip(prompts, results):
            for obj in parsed.objects:
                obj.prompt = prompt
                obj.object_id = f"{prompt}#{obj.object_id}"
                width = width or obj.frame_width
                height = height or obj.frame_height
            objects.extend(parsed.objects)
            raw[prompt] = output_text
            diagnostics.extend(parsed.diagnostics)
        return Segmentation(objects=objects, width=width, height=height, prompts=prompts, raw_outputs=raw, diagnostics=diagnostics, source=image.name)


def backend_from_env(prefer: str | None = None) -> SamBackend:
    choice = (prefer or os.environ.get("SAM_BACKEND", "")).strip().lower()
    if not choice:
        choice = "meta" if (os.environ.get("META_API_KEY") or os.environ.get("MODEL_API_KEY")) else "mock"
    if choice == "meta":
        return MetaSamBackend()
    if choice == "mock":
        return MockSam()
    raise SamError(f"不明な SAM_BACKEND: {choice}")
