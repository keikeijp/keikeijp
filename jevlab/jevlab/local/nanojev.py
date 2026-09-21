"""29. NanoJev: 1 つの state に対する K 個の質問を 1 パスで答える並列判断モデル + ゲーム制御デモ。

元ネタ: TianyuCodings/NanoJev (Qwen3-0.6B ベース、並列判断ヘッド、重み + 学習コード + ゲーム制御)

設計 (ParallelJudgmentHead の推論契約):
  入力  : state と K 個の型付き質問 (choice / score / noul)
  出力  : K 個の回答を 1 回の forward で得る
  実現  : 質問ごとに「回答スロット」(英字 or Yes/No) を持つ 1 つのプロンプトに詰め、
          各スロット位置の次トークン分布から候補トークンの log 確率を読む。
          `SlotModel.slot_logprobs(prompt, slots)` がその境界。HF 実装ではテンプレート中の
          スロットに中立プレースホルダを置いて 1 回 forward し、各スロット直前位置の logits を読む
          (後続スロットは前のスロットの真の回答ではなくプレースホルダに条件付く = 並列ヘッドの近似)。
          本家は LoRA 学習でこの近似を埋める。ここでは学習データのエクスポータと LoRA 学習関数 (遅延 import) を用意する。

```
jevlab nanojev demo --steps 50 --fake
jevlab nanojev export --log decisions.jsonl --out sft.jsonl
```
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jevlab.core.backends import JevError, overlap_score, tokenize

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
YES, NO = "Yes", "No"
SLOT = "<slot>"
DEFAULT_MODEL = "Qwen/Qwen3-0.6B"


# ---------------------------------------------------------------------------
# SlotModel: 1 プロンプト・複数スロット
# ---------------------------------------------------------------------------


class SlotModel(Protocol):
    name: str

    def slot_logprobs(self, prompt: str, slots: list[list[str]]) -> list[dict[str, float]]:
        """prompt 中の i 番目の `<slot>` について slots[i] の各候補トークンの log 確率を返す。"""
        ...


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _softmax_dict(logits: Mapping[str, float]) -> dict[str, float]:
    top = max(logits.values())
    exps = {k: math.exp(v - top) for k, v in logits.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


@dataclass
class PackedPrompt:
    """複数質問を詰めたプロンプトと、スロットごとのメタデータ。"""

    prompt: str
    slots: list[list[str]]
    names: list[str]
    kinds: list[str]  # choice / score / noul
    labels: list[list[Any]]  # choice: ラベル列, score: 段階列, noul: [YES, NO]


def pack_questions(state: Any, questions: Mapping[str, Mapping[str, Any]]) -> PackedPrompt:
    """state + K 質問 → 1 プロンプト。回答部は JSON 風で各値が `<slot>`。"""
    lines = ["State:", _text(state), "", "Questions:"]
    slots: list[list[str]] = []
    names: list[str] = []
    kinds: list[str] = []
    labels: list[list[Any]] = []
    for number, (name, question) in enumerate(questions.items(), start=1):
        qtype = question["type"]
        instructions = question.get("instructions")
        head = f"Q{number} [{name}]"
        if qtype == "choice":
            criteria = question["criteria"]
            lines.append(f"{head}: {_text(instructions) if instructions is not None else 'Which option fits best?'}")
            letters = []
            for index, (label, description) in enumerate(criteria.items()):
                if index >= len(LETTERS):
                    raise JevError("選択肢は最大 26 個までです")
                letters.append(LETTERS[index])
                text = label.replace("_", " ") if description is None else f"{label.replace('_', ' ')}: {_text(description)}"
                lines.append(f"  {LETTERS[index]}. {text}")
            slots.append(letters)
            labels.append(list(criteria))
        elif qtype == "score":
            levels = list(question["criteria"])
            lines.append(f"{head}: {_text(instructions) if instructions is not None else 'Which level fits best?'}")
            letters = []
            for index, level in enumerate(levels):
                letters.append(LETTERS[index])
                lines.append(f"  {LETTERS[index]}. {_text(level)}")
            slots.append(letters)
            labels.append(levels)
        else:
            criteria = question.get("criteria") or {}
            lines.append(f"{head}: {_text(instructions) if instructions is not None else 'Is it true?'} (Yes or No)")
            if criteria.get("true") is not None:
                lines.append(f"  Yes. {_text(criteria['true'])}")
            if criteria.get("false") is not None:
                lines.append(f"  No. {_text(criteria['false'])}")
            slots.append([YES, NO])
            labels.append([YES, NO])
        names.append(name)
        kinds.append(qtype)
    answer = ", ".join(f'"{name}": "{SLOT}"' for name in names)
    lines.append("")
    lines.append("Answers (JSON): {" + answer + "}")
    return PackedPrompt("\n".join(lines), slots, names, kinds, labels)


_Q_LINE = re.compile(r"^Q(\d+) \[([^\]]+)\]: (.*)$")
_OPT_LINE = re.compile(r"^  ([A-Za-z]+)\. (.*)$")


def parse_packed_prompt(prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """PackedPrompt.prompt → (state 文, [{name, question, options{token: text}}])。FakeSlotModel 用。"""
    state_part, _, rest = prompt.partition("\nQuestions:\n")
    state_text = state_part.replace("State:", "", 1).strip()
    blocks: list[dict[str, Any]] = []
    for line in rest.splitlines():
        q = _Q_LINE.match(line)
        if q:
            blocks.append({"name": q.group(2), "question": q.group(3).replace("(Yes or No)", "").strip(), "options": {}})
            continue
        o = _OPT_LINE.match(line)
        if o and blocks:
            blocks[-1]["options"][o.group(1)] = o.group(2)
    return state_text, blocks


class FakeSlotModel:
    """テスト用: 各スロットの候補を state との語彙重なりで採点する。`hints` {質問名: 候補トークン} で固定可。"""

    name = "fake-slot"
    _NEGATIONS = ("not", "no", "never", "ない", "いいえ", "なし", "false", "off")

    def __init__(self, hints: Mapping[str, str] | None = None, scale: float = 6.0):
        self.hints = dict(hints or {})
        self.scale = scale
        self.calls: list[str] = []

    def slot_logprobs(self, prompt: str, slots: list[list[str]]) -> list[dict[str, float]]:
        self.calls.append(prompt)
        state_text, blocks = parse_packed_prompt(prompt)
        if len(blocks) != len(slots):
            raise JevError(f"スロット数 {len(slots)} と質問ブロック数 {len(blocks)} が一致しません")
        state_tokens = tokenize(state_text)
        results: list[dict[str, float]] = []
        for block, candidates in zip(blocks, slots):
            hint = self.hints.get(block["name"])
            if hint in candidates:
                logits = {c: (4.0 if c == hint else 0.0) for c in candidates}
            elif set(candidates) == {YES, NO}:
                base = overlap_score(state_tokens, block["question"])
                yes_hint = overlap_score(state_tokens, block["options"].get(YES))
                no_hint = overlap_score(state_tokens, block["options"].get(NO))
                negated = any(neg in state_tokens for neg in self._NEGATIONS)
                logit = (base + yes_hint - no_hint) * 4.0 - (1.5 if negated else 0.0) - 1.0
                logits = {YES: logit / 2, NO: -logit / 2}
            else:
                logits = {}
                for c in candidates:
                    label, _, description = block["options"].get(c, "").partition(": ")
                    logits[c] = (overlap_score(state_tokens, label) * 1.5 + overlap_score(state_tokens, description or None)) * self.scale
            probs = _softmax_dict(logits)
            results.append({k: math.log(max(v, 1e-12)) for k, v in probs.items()})
        return results


class HFSlotModel:
    """transformers 実装 (遅延 import)。スロットをプレースホルダで埋めて 1 回 forward し、各スロット直前の logits を読む。"""

    def __init__(self, model_id: str = DEFAULT_MODEL, device: str | None = None, placeholder: str = "_"):
        self.name = model_id
        self.model_id = model_id
        self.device = device or os.environ.get("JEV_LOCAL_DEVICE", "").strip() or None
        self.placeholder = placeholder
        self._model = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as error:  # pragma: no cover
            raise JevError("HFSlotModel には torch と transformers が必要です: pip install 'jevlab[local]'") from error
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self._model = AutoModelForCausalLM.from_pretrained(self.model_id).eval()
        self.device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(self.device)

    def _single_ids(self, token: str) -> list[int]:
        ids = []
        for variant in (token, " " + token):
            encoded = self._tokenizer.encode(variant, add_special_tokens=False)
            if len(encoded) == 1 and encoded[0] not in ids:
                ids.append(encoded[0])
        return ids or [self._tokenizer.encode(token, add_special_tokens=False)[0]]

    def slot_logprobs(self, prompt: str, slots: list[list[str]]) -> list[dict[str, float]]:
        self._load()
        import torch

        segments = prompt.split(SLOT)
        if len(segments) != len(slots) + 1:
            raise JevError("プロンプトのスロット数が一致しません")
        ids: list[int] = []
        positions: list[int] = []  # 各スロット直前トークンの位置
        for index, segment in enumerate(segments):
            ids.extend(self._tokenizer.encode(segment, add_special_tokens=False))
            if index < len(slots):
                positions.append(len(ids) - 1)
                ids.extend(self._tokenizer.encode(self.placeholder, add_special_tokens=False))
        with torch.no_grad():
            logits = self._model(torch.tensor([ids]).to(self.device)).logits[0].float()
        logprobs = torch.log_softmax(logits, dim=-1)
        return [{c: max(float(logprobs[pos, i]) for i in self._single_ids(c)) for c in candidates} for pos, candidates in zip(positions, slots)]


# ---------------------------------------------------------------------------
# MultiHeadDecider / Backend
# ---------------------------------------------------------------------------


class MultiHeadDecider:
    """SlotModel を包み、K 質問を 1 パスで答える。"""

    def __init__(self, model: SlotModel, temperature: float = 1.0):
        self.model = model
        self.temperature = max(temperature, 1e-6)

    def decide(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        packed = pack_questions(state, questions)
        per_slot = self.model.slot_logprobs(packed.prompt, packed.slots)
        answers: dict[str, dict[str, Any]] = {}
        for name, kind, labels, candidates, logprobs in zip(packed.names, packed.kinds, packed.labels, packed.slots, per_slot):
            probs = _softmax_dict({c: logprobs.get(c, -30.0) / self.temperature for c in candidates})
            ranked = sorted(probs.values(), reverse=True)
            confidence = ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)
            if kind == "choice":
                probabilities = {label: probs[c] for label, c in zip(labels, candidates)}
                choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
                answers[name] = {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}
            elif kind == "score":
                probabilities = {str(i): probs[c] for i, c in enumerate(candidates)}
                answers[name] = {
                    "type": "score",
                    "score": round(sum(int(k) * p for k, p in probabilities.items()), 4),
                    "legend": {str(i): level for i, level in enumerate(labels)},
                    "probabilities": probabilities,
                    "confidence": confidence,
                }
            else:
                answers[name] = {"type": "noul", "noul": probs[YES]}
        return answers


class NanoJevBackend:
    """core の Backend プロトコル。1 decide = 1 forward。"""

    name = "nanojev"

    def __init__(self, model: SlotModel | None = None, temperature: float = 1.0):
        self.decider = MultiHeadDecider(model or HFSlotModel(), temperature)
        self.model = getattr(self.decider.model, "name", "nanojev")

    def decide(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        answers = self.decider.decide(state, questions)
        return {"answers": answers, "model": self.model, "usage": {"input_tokens": 0, "output_tokens": 0}}, self.model


# ---------------------------------------------------------------------------
# 学習データエクスポート + LoRA 学習 (遅延 import)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = "You are a parallel judgment model. Answer every question at once as compact JSON."


def compact_answer(answer: Mapping[str, Any]) -> Any:
    """Decision ログの回答 dict → SFT ターゲット (choice: ラベル, score: レベル int, noul: bool)。"""
    qtype = answer.get("type")
    if qtype == "choice":
        return answer.get("choice", answer.get("winner"))
    if qtype == "score":
        if "level" in answer:
            return int(answer["level"])
        probs = answer.get("probabilities") or {}
        if probs:
            return int(max(probs.items(), key=lambda kv: float(kv[1]))[0])
        return int(round(float(answer.get("score", 0))))
    value = answer.get("noul", answer.get("probability", 0.5))
    return bool(float(value) >= 0.5)


def export_sft(logs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """jevlab の Decision ログ ({state, questions, answers}) → chat 形式 SFT レコード。"""
    records = []
    for entry in logs:
        questions = entry["questions"]
        packed = pack_questions(entry["state"], questions)
        user = packed.prompt.split("\nAnswers (JSON):")[0].rstrip()
        target = {name: compact_answer(entry["answers"][name]) for name in packed.names}
        records.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
                ],
                "slots": {name: kind for name, kind in zip(packed.names, packed.kinds)},
            }
        )
    return records


def train_lora(records: Sequence[Mapping[str, Any]], base_model: str = DEFAULT_MODEL, out_dir: str = "nanojev-lora", epochs: int = 1, lr: float = 2e-4) -> str:
    """peft + transformers で LoRA SFT。依存が無ければ JevError (pip install torch transformers peft)。"""
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise JevError("train_lora には torch, transformers, peft が必要です: pip install 'jevlab[local]' peft") from error
    tokenizer = AutoTokenizer.from_pretrained(base_model)
    model = get_peft_model(AutoModelForCausalLM.from_pretrained(base_model), LoraConfig(r=8, lora_alpha=16, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"))
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    for _ in range(epochs):
        for record in records:
            text = tokenizer.apply_chat_template(record["messages"], tokenize=False)
            batch = tokenizer(text, return_tensors="pt", truncation=True, max_length=2048)
            loss = model(**batch, labels=batch["input_ids"]).loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
    model.save_pretrained(out_dir)
    return out_dir


# ---------------------------------------------------------------------------
# ゲーム制御デモ: 3 レーンの避けゲー
# ---------------------------------------------------------------------------


@dataclass
class DodgeGame:
    """3 レーン、障害物が距離 3 から 1 ずつ近づく。距離 0 で同じレーンなら衝突 (low 障害物はジャンプで回避可)。"""

    seed: int = 0
    lanes: int = 3
    player: int = 1
    tick: int = 0
    alive: bool = True
    dodged: int = 0
    obstacles: list[dict[str, Any]] = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def state(self) -> dict[str, Any]:
        """判断に必要な最小限の構造化 state。安全な移動候補を明示し、Jev はその中から選ぶ。"""
        threats = [o for o in self.obstacles if o["lane"] == self.player]
        nearest = min((o["distance"] for o in threats), default=None)
        moves = {"stay": self.player, "left": self.player - 1, "right": self.player + 1}
        safe_moves = [
            name for name, lane in moves.items()
            if 0 <= lane < self.lanes and not any(o["lane"] == lane and o["distance"] <= 2 for o in self.obstacles)
        ]
        return {
            "tick": self.tick,
            "player_lane": self.player,
            "obstacles": [{"lane": o["lane"], "distance": o["distance"], "low": o["low"]} for o in self.obstacles],
            "threat": "imminent" if nearest == 1 else "near" if nearest == 2 else "none",
            "threat_low": any(o["low"] for o in threats if o["distance"] == 1),
            "safe_moves": safe_moves,
        }

    def step(self, action: str, jump: bool) -> None:
        if not self.alive:
            return
        self.tick += 1
        if action == "left":
            self.player = max(0, self.player - 1)
        elif action == "right":
            self.player = min(self.lanes - 1, self.player + 1)
        for o in self.obstacles:
            o["distance"] -= 1
        for o in [o for o in self.obstacles if o["distance"] == 0]:
            if o["lane"] == self.player and not (jump and o["low"]):
                self.alive = False
            else:
                self.dodged += 1
        self.obstacles = [o for o in self.obstacles if o["distance"] > 0]
        if self.tick % 2 == 0:
            self.obstacles.append({"lane": self.rng.randrange(self.lanes), "distance": 3, "low": self.rng.random() < 0.4})


GAME_QUESTIONS: dict[str, dict[str, Any]] = {
    "action": {"type": "choice", "criteria": {"stay": "stay in the current lane", "left": "move to the left lane", "right": "move to the right lane"}, "instructions": "Which move keeps the player safe? Pick one of the safe moves."},
    "danger": {"type": "score", "criteria": ["none: no threat", "near: threat near", "imminent: threat imminent"], "instructions": "How dangerous is the current situation?"},
    "should_jump": {"type": "noul", "instructions": "Should the player jump now? Jump only when threat imminent and threat low true"},
}


def run_demo(steps: int = 50, model: SlotModel | None = None, seed: int = 0, log: Any = None) -> dict[str, Any]:
    """各ティックで {action, danger, should_jump} を 1 パスで決めて進める。"""
    decider = MultiHeadDecider(model or FakeSlotModel())
    game = DodgeGame(seed=seed)
    for _ in range(steps):
        if not game.alive:
            break
        state = game.state()
        answers = decider.decide(state, GAME_QUESTIONS)
        action = answers["action"]["choice"]
        jump = answers["should_jump"]["noul"] >= 0.5
        if log:
            log({"tick": game.tick, "state": state, "action": action, "danger": answers["danger"]["score"], "jump": jump})
        game.step(action, jump)
    return {"ticks": game.tick, "alive": game.alive, "dodged": game.dodged}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab nanojev", description="並列判断モデルのデモとデータエクスポート")
    parser.add_argument("--backend", default="nanojev", help="nanojev (固定; --fake で FakeSlotModel)")
    parser.add_argument("--fake", action="store_true", help="モデルを読まず FakeSlotModel を使う")
    parser.add_argument("--model", default=None, help="HF モデル ID (既定 Qwen/Qwen3-0.6B)")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="避けゲーを並列判断で制御")
    demo.add_argument("--steps", type=int, default=50)
    demo.add_argument("--seed", type=int, default=0)
    demo.add_argument("--verbose", action="store_true")
    export = sub.add_parser("export", help="Decision ログ JSONL → SFT JSONL")
    export.add_argument("--log", required=True)
    export.add_argument("--out", required=True)
    train = sub.add_parser("train", help="SFT JSONL で LoRA 学習 (peft 必須)")
    train.add_argument("--sft", required=True)
    train.add_argument("--out-dir", default="nanojev-lora")
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            model = FakeSlotModel() if args.fake else HFSlotModel(args.model or DEFAULT_MODEL)
            result = run_demo(args.steps, model, args.seed, log=(lambda r: print(json.dumps(r, ensure_ascii=False))) if args.verbose else None)
            print(json.dumps(result))
        elif args.command == "export":
            with open(args.log, encoding="utf-8") as handle:
                logs = [json.loads(line) for line in handle if line.strip()]
            records = export_sft(logs)
            with open(args.out, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps({"records": len(records), "out": args.out}))
        else:
            with open(args.sft, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            print(train_lora(records, args.model or DEFAULT_MODEL, args.out_dir))
    except JevError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
