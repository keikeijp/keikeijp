"""SceneJudge のシナリオ定義。

シナリオ = 「SAM に何を探させるか (prompts)」+「ゾーン」+「Jev に何を判断させるか (questions)」+「判断の後に何をするか (actions)」。
JSON/YAML ファイルでも、Python でも定義できる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jevlab.core import Choice, Noul, Score


@dataclass
class Rule:
    """判断結果 → アクション (通知など) の閾値ルール。"""

    question: str
    when: str  # "noul>=0.8" / "level>=2" / "choice==dirty" のような簡易式
    action: str  # "notify" | "log" | "flag"
    message: str = ""

    def matches(self, answer: Any) -> bool:
        return evaluate_condition(self.when, answer)


@dataclass
class Scenario:
    name: str
    description: str
    prompts: list[str]
    questions: dict[str, Any]  # 質問名 → Choice/Score/Noul
    zones: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)  # 比率座標 (0..1)
    rules: list[Rule] = field(default_factory=list)
    context: str = ""  # Jev に一緒に渡す前提説明

    def zones_px(self, width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
        return {name: (int(z[0] * width), int(z[1] * height), int(z[2] * width), int(z[3] * height)) for name, z in self.zones.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompts": self.prompts,
            "zones": {k: list(v) for k, v in self.zones.items()},
            "context": self.context,
            "questions": {name: _question_to_json(q) for name, q in self.questions.items()},
            "rules": [{"question": r.question, "when": r.when, "action": r.action, "message": r.message} for r in self.rules],
        }


def _question_to_json(question: Any) -> dict[str, Any]:
    if hasattr(question, "to_wire"):
        return question.to_wire()
    return dict(question)


def _question_from_json(data: Mapping[str, Any]) -> Any:
    kind = data.get("type")
    if kind == "choice":
        return Choice(data["criteria"], data.get("instructions"))
    if kind == "score":
        return Score(data["criteria"], data.get("instructions"))
    if kind == "noul":
        return Noul(data.get("instructions"), data.get("criteria"))
    raise ValueError(f"不明な質問タイプ: {kind}")


def scenario_from_dict(data: Mapping[str, Any]) -> Scenario:
    return Scenario(
        name=data["name"],
        description=data.get("description", ""),
        prompts=list(data["prompts"]),
        questions={name: _question_from_json(q) for name, q in data["questions"].items()},
        zones={name: tuple(zone) for name, zone in (data.get("zones") or {}).items()},  # type: ignore[misc]
        rules=[Rule(**rule) for rule in data.get("rules", [])],
        context=data.get("context", ""),
    )


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as error:
            raise RuntimeError("YAML シナリオを読むには `pip install pyyaml` が必要です (JSON なら不要)") from error
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return scenario_from_dict(data)


def evaluate_condition(expression: str, answer: Any) -> bool:
    """`noul>=0.8` / `level>=2` / `score<1.5` / `choice==dirty` / `confidence>=0.7` を評価する。"""
    import re

    match = re.fullmatch(r"\s*(noul|level|score|choice|confidence)\s*(>=|<=|==|!=|>|<)\s*([\w.\-]+)\s*", expression)
    if not match:
        raise ValueError(f"条件式を解釈できません: {expression!r}")
    attribute, operator, raw = match.groups()
    left = getattr(answer, attribute, None)
    if left is None:
        return False
    if attribute == "choice":
        right: Any = raw
    else:
        right = float(raw)
        left = float(left)
    return {
        ">=": left >= right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
        ">": left > right,
        "<": left < right,
    }[operator]


# ---------------------------------------------------------------------------
# 内蔵シナリオ
# ---------------------------------------------------------------------------

BUILTIN: dict[str, Scenario] = {
    "desk": Scenario(
        name="desk",
        description="机の散らかり具合を採点し、片付けるべき物を選ぶ",
        prompts=["cup", "bottle", "paper", "book", "cable", "laptop", "phone", "trash"],
        context="A photo of a work desk. Objects are listed with normalized boxes (0..1), area ratios and spatial relations.",
        questions={
            "tidiness": Score(["spotless: almost nothing on the desk", "tidy: a few items in place", "cluttered: many items scattered", "messy: items overlap, trash visible"], "How tidy is the desk?"),
            "first_to_remove": Choice({"cup": "cups and mugs", "bottle": "bottles", "paper": "loose paper", "trash": "trash", "cable": "loose cables", "nothing": "nothing needs removing"}, "Which object should be removed first to improve tidiness?"),
            "needs_cleanup": Noul("Should the owner be reminded to clean up?"),
        },
        rules=[Rule("needs_cleanup", "noul>=0.7", "notify", "机を片付けよう")],
    ),
    "pet": Scenario(
        name="pet",
        description="ペットがソファ/ベッドに乗っているか、食器が空かを判定",
        prompts=["dog", "cat", "couch", "bed", "food bowl"],
        context="Home camera frame. Check whether a pet is on furniture and whether the food bowl is present.",
        zones={"kitchen": (0.0, 0.5, 0.4, 1.0)},
        questions={
            "pet_on_furniture": Noul("Is a dog or cat on the couch or bed? (an animal box mostly inside a couch/bed box)"),
            "pet_location": Choice({"on_furniture": "inside couch or bed", "kitchen": "in the kitchen zone", "elsewhere": "somewhere else", "absent": "no pet detected"}, "Where is the pet?"),
            "activity_level": Score(["no pet", "resting", "moving around"], "How active does the scene look?"),
        },
        rules=[Rule("pet_on_furniture", "noul>=0.8", "notify", "ペットが家具に乗っています")],
    ),
    "parking": Scenario(
        name="parking",
        description="駐車スペースの空き状況",
        prompts=["car", "motorcycle", "bicycle", "person"],
        context="Parking lot camera. Zones are parking spots. A spot is occupied when a car box covers most of it.",
        zones={"spot_1": (0.0, 0.4, 0.33, 1.0), "spot_2": (0.33, 0.4, 0.66, 1.0), "spot_3": (0.66, 0.4, 1.0, 1.0)},
        questions={
            "free_spots": Score(["0 free", "1 free", "2 free", "3 free"], "How many parking spots are free?"),
            "recommended_spot": Choice({"spot_1": None, "spot_2": None, "spot_3": None, "none": "no free spot"}, "Which spot should the next driver take?"),
            "pedestrian_present": Noul("Is a person walking in the lot?"),
        },
        rules=[Rule("free_spots", "level<=0", "notify", "満車です")],
    ),
    "kitchen": Scenario(
        name="kitchen",
        description="キッチンの日常チェック (安全設備の代替ではない)",
        prompts=["pot", "pan", "knife", "child", "stove", "cutting board", "towel"],
        context="Kitchen camera. This is a convenience check only, not a safety system.",
        questions={
            "attention": Score(["nothing notable", "worth a glance", "someone should check now"], "Does the kitchen need attention?"),
            "reason": Choice({"knife_reachable": "a knife near a child", "towel_near_stove": "towel near or on the stove", "pan_on_stove": "pan or pot on the stove with nobody around", "none": "nothing"}, "Main reason for attention"),
            "child_present": Noul("Is a child in the frame?"),
        },
        rules=[Rule("attention", "level>=2", "notify", "キッチンを確認してください")],
    ),
    "shelf": Scenario(
        name="shelf",
        description="棚の在庫補充が必要か",
        prompts=["bottle", "box", "can", "empty shelf space"],
        context="Retail shelf camera. Large empty shelf space means restocking is needed.",
        questions={
            "stock_level": Score(["empty", "low", "half", "full"], "How stocked is the shelf?"),
            "restock": Noul("Should staff restock this shelf now?"),
            "dominant_item": Choice({"bottle": None, "box": None, "can": None, "mixed": "no single dominant item"}, "What is the dominant product type?"),
        },
        rules=[Rule("restock", "noul>=0.75", "notify", "補充が必要です")],
    ),
}


def get_scenario(name_or_path: str) -> Scenario:
    if name_or_path in BUILTIN:
        return BUILTIN[name_or_path]
    return load_scenario(name_or_path)
