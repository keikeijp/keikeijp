"""27. SemIf (旧 OpenJev): 公開モデルの logit から型付き選択肢の確率を直接読む。

元ネタ: TheoLeeCJ/SemIf

- トークンをサンプリングしない。プロンプト末尾 "Answer:" の次トークン分布から
  候補トークン (A/B/C... または Yes/No) の log 確率を 1 回の forward で読む
- choice → 選択肢を英字で列挙、score → ルーブリック段階を英字で列挙、noul → Yes/No
- log 確率 → softmax → 確率。confidence = 1 位 - 2 位、score = 期待値
- `LogitModel` プロトコルに合わせれば HF 以外 (vLLM / SGLang の logprobs 等) も差し込める

```
jevlab semif ask --state "二重に課金された。返金して" --choice refund,bug,other --fake
jevlab semif bench --jsonl cases.jsonl --fake
JEV_BACKEND=local JEV_LOCAL_MODEL=Qwen/Qwen2.5-0.5B-Instruct python -c "from jevlab.core import Jev; print(Jev().choose('...', ['a','b']))"
```
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import string
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from jevlab.core.backends import JevError, MockBackend, overlap_score, tokenize

LETTERS = list(string.ascii_uppercase)
YES, NO = "Yes", "No"
DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

# ---------------------------------------------------------------------------
# LogitModel プロトコル
# ---------------------------------------------------------------------------


class LogitModel(Protocol):
    """`prompt` の次トークンとして各候補トークンが出る log 確率を返す。"""

    name: str

    def next_token_logprobs(self, prompt: str, candidate_tokens: list[str]) -> dict[str, float]: ...


# ---------------------------------------------------------------------------
# プロンプト
# ---------------------------------------------------------------------------

STATE_HEADER = "State:"
QUESTION_HEADER = "Question:"
OPTIONS_HEADER = "Options:"
ANSWER_TAIL = "Answer:"


def _state_text(state: Any) -> str:
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def letter_for(index: int) -> str:
    if index >= len(LETTERS):
        raise JevError(f"選択肢は最大 {len(LETTERS)} 個までです (index={index})")
    return LETTERS[index]


def build_choice_prompt(state: Any, criteria: Mapping[str, Any], instructions: Any = None) -> tuple[str, list[str]]:
    """choice 用プロンプト。返り値: (prompt, 候補トークン=英字)。"""
    lines = [STATE_HEADER, _state_text(state), ""]
    lines.append(f"{QUESTION_HEADER} {_state_text(instructions) if instructions is not None else 'Which option fits best?'}")
    lines.append(OPTIONS_HEADER)
    letters = []
    for index, (label, description) in enumerate(criteria.items()):
        letter = letter_for(index)
        letters.append(letter)
        text = label.replace("_", " ") if description is None else f"{label.replace('_', ' ')}: {_state_text(description)}"
        lines.append(f"{letter}. {text}")
    lines.append(ANSWER_TAIL)
    return "\n".join(lines), letters


def build_score_prompt(state: Any, levels: Sequence[Any], instructions: Any = None) -> tuple[str, list[str]]:
    """score 用: ルーブリック段階を英字で列挙する (順序を保つ)。"""
    lines = [STATE_HEADER, _state_text(state), ""]
    lines.append(f"{QUESTION_HEADER} {_state_text(instructions) if instructions is not None else 'Which level fits best?'}")
    lines.append(OPTIONS_HEADER)
    letters = []
    for index, level in enumerate(levels):
        letter = letter_for(index)
        letters.append(letter)
        lines.append(f"{letter}. {_state_text(level)}")
    lines.append(ANSWER_TAIL)
    return "\n".join(lines), letters


def build_noul_prompt(state: Any, instructions: Any = None, criteria: Mapping[str, Any] | None = None) -> tuple[str, list[str]]:
    lines = [STATE_HEADER, _state_text(state), ""]
    question = _state_text(instructions) if instructions is not None else "Is the statement true?"
    lines.append(f"{QUESTION_HEADER} {question}")
    lines.append(OPTIONS_HEADER)
    yes_text = _state_text(criteria.get("true")) if criteria and criteria.get("true") is not None else "yes"
    no_text = _state_text(criteria.get("false")) if criteria and criteria.get("false") is not None else "no"
    lines.append(f"{YES}. {yes_text}")
    lines.append(f"{NO}. {no_text}")
    lines.append(f"{ANSWER_TAIL.rstrip(':')} (Yes or No):")
    return "\n".join(lines), [YES, NO]


_OPTION_LINE = re.compile(r"^([A-Za-z]+)\. (.*)$")


def parse_prompt(prompt: str) -> tuple[str, str, dict[str, str]]:
    """プロンプトを (state 文, question 文, {候補トークン: 選択肢テキスト}) に戻す。FakeLogitModel とテスト用。"""
    state_part, _, rest = prompt.partition(f"\n{QUESTION_HEADER}")
    state_text = state_part.replace(STATE_HEADER, "", 1).strip()
    question_part, _, options_part = rest.partition(f"\n{OPTIONS_HEADER}")
    options: dict[str, str] = {}
    for line in options_part.splitlines():
        match = _OPTION_LINE.match(line.strip())
        if match:
            options[match.group(1)] = match.group(2)
    return state_text, question_part.strip(), options


# ---------------------------------------------------------------------------
# 確率ユーティリティ
# ---------------------------------------------------------------------------


def logprobs_to_probs(logprobs: Mapping[str, float], temperature: float = 1.0) -> dict[str, float]:
    if not logprobs:
        return {}
    temperature = max(temperature, 1e-6)
    top = max(logprobs.values())
    exps = {k: math.exp((v - top) / temperature) for k, v in logprobs.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


def temperature_scale(probs: Mapping[str, float], temperature: float) -> dict[str, float]:
    """温度スケーリングによる較正。T>1 で平坦化、T<1 で先鋭化。確率 0 は log(1e-12) 扱い。"""
    logs = {k: math.log(max(float(v), 1e-12)) for k, v in probs.items()}
    return logprobs_to_probs(logs, temperature)


def confidence_of(probs: Mapping[str, float]) -> float:
    ranked = sorted(probs.values(), reverse=True)
    if not ranked:
        return 0.0
    return ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)


# ---------------------------------------------------------------------------
# モデル実装
# ---------------------------------------------------------------------------


class FakeLogitModel:
    """テスト用。プロンプト中の選択肢テキストと state の語彙重なりを logit にする (MockBackend と同じ発想)。

    `bias` で候補トークンごとに logit を足せる (例: {"Yes": 2.0})。
    """

    name = "fake-logit"
    _NEGATIONS = ("not", "no", "never", "ない", "いいえ", "なし", "false", "off")

    def __init__(self, bias: Mapping[str, float] | None = None, scale: float = 6.0):
        self.bias = dict(bias or {})
        self.scale = scale
        self.calls: list[str] = []

    def next_token_logprobs(self, prompt: str, candidate_tokens: list[str]) -> dict[str, float]:
        self.calls.append(prompt)
        state_text, question, options = parse_prompt(prompt)
        state_tokens = tokenize(state_text)
        logits: dict[str, float] = {}
        if set(candidate_tokens) == {YES, NO}:
            base = overlap_score(state_tokens, question)
            yes_hint = overlap_score(state_tokens, options.get(YES)) if options.get(YES, "yes") != "yes" else 0.0
            no_hint = overlap_score(state_tokens, options.get(NO)) if options.get(NO, "no") != "no" else 0.0
            negated = any(neg in state_tokens for neg in self._NEGATIONS)
            logit = (base + yes_hint - no_hint) * 4.0 - (1.5 if negated else 0.0) - 1.0
            logits = {YES: logit / 2, NO: -logit / 2}
        else:
            for token in candidate_tokens:
                text = options.get(token, "")
                label, _, description = text.partition(": ")
                logits[token] = (overlap_score(state_tokens, label) * 1.5 + overlap_score(state_tokens, description or None)) * self.scale
        for token in candidate_tokens:
            logits[token] = logits.get(token, -5.0) + self.bias.get(token, 0.0)
        # 正規化した log 確率にして返す (実モデルの出力形に揃える)
        probs = logprobs_to_probs(logits)
        return {k: math.log(max(v, 1e-12)) for k, v in probs.items()}


class HFLogitModel:
    """transformers の CausalLM から次トークン log 確率を読む。torch / transformers は遅延 import。"""

    def __init__(self, model_id: str = DEFAULT_MODEL, device: str | None = None, dtype: str | None = None):
        self.name = model_id
        self.model_id = model_id
        self.device = device or os.environ.get("JEV_LOCAL_DEVICE", "").strip() or None
        self.dtype = dtype
        self._model = None
        self._tokenizer = None
        self._token_cache: dict[str, list[int]] = {}

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as error:  # pragma: no cover - 環境依存
            raise JevError("HFLogitModel には torch と transformers が必要です: pip install 'jevlab[local]'") from error
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        kwargs: dict[str, Any] = {}
        if self.dtype:
            kwargs["torch_dtype"] = getattr(torch, self.dtype)
        self._model = AutoModelForCausalLM.from_pretrained(self.model_id, **kwargs)
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(device)
        self._model.eval()
        self.device = device

    def _candidate_ids(self, token: str) -> list[int]:
        """候補トークンを単一トークンに落とす。先頭スペース有無の変種を試し、単一トークンになるものを集める。"""
        if token in self._token_cache:
            return self._token_cache[token]
        ids: list[int] = []
        for variant in (token, " " + token, token.lower(), " " + token.lower()):
            encoded = self._tokenizer.encode(variant, add_special_tokens=False)
            if len(encoded) == 1 and encoded[0] not in ids:
                ids.append(encoded[0])
        if not ids:  # 単一トークンにならない場合は先頭トークンで近似
            ids = [self._tokenizer.encode(token, add_special_tokens=False)[0]]
        self._token_cache[token] = ids
        return ids

    def next_token_logprobs(self, prompt: str, candidate_tokens: list[str]) -> dict[str, float]:
        self._load()
        import torch

        inputs = self._tokenizer(prompt, return_tensors="pt").to(self.device)
        with torch.no_grad():
            logits = self._model(**inputs).logits[0, -1].float()
        logprobs = torch.log_softmax(logits, dim=-1)
        return {token: max(float(logprobs[i]) for i in self._candidate_ids(token)) for token in candidate_tokens}


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class LocalLogitBackend:
    """core の Backend プロトコル実装。質問ごとに 1 回の forward (トークン生成なし)。"""

    name = "local"

    def __init__(self, model: LogitModel | None = None, temperature: float = 1.0, model_name: str | None = None):
        self.model_impl = model if model is not None else HFLogitModel()
        self.temperature = temperature
        self.model = model_name or getattr(self.model_impl, "name", "local-logit")
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls, **kwargs: Any) -> "LocalLogitBackend":
        """JEV_LOCAL_MODEL (既定 Qwen/Qwen2.5-0.5B-Instruct、"fake" で FakeLogitModel)、JEV_LOCAL_DEVICE を読む。"""
        if kwargs.get("model") is not None:
            return cls(**kwargs)
        model_id = os.environ.get("JEV_LOCAL_MODEL", "").strip() or DEFAULT_MODEL
        device = os.environ.get("JEV_LOCAL_DEVICE", "").strip() or None
        kwargs.pop("model", None)
        if model_id.lower() == "fake":
            return cls(model=FakeLogitModel(), **kwargs)
        return cls(model=HFLogitModel(model_id, device=device), **kwargs)

    def decide(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        self.calls.append({"state": state, "questions": dict(questions)})
        answers = {name: self.answer_one(state, question) for name, question in questions.items()}
        return {"answers": answers, "model": self.model, "usage": {"input_tokens": 0, "output_tokens": 0}}, self.model

    def answer_one(self, state: Any, question: Mapping[str, Any]) -> dict[str, Any]:
        qtype = question["type"]
        if qtype == "choice":
            prompt, letters = build_choice_prompt(state, question["criteria"], question.get("instructions"))
            probs = logprobs_to_probs(self.model_impl.next_token_logprobs(prompt, letters), self.temperature)
            labels = list(question["criteria"])
            probabilities = {label: probs[letter] for label, letter in zip(labels, letters)}
            choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
            return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence_of(probabilities)}
        if qtype == "score":
            levels = list(question["criteria"])
            prompt, letters = build_score_prompt(state, levels, question.get("instructions"))
            probs = logprobs_to_probs(self.model_impl.next_token_logprobs(prompt, letters), self.temperature)
            probabilities = {str(i): probs[letter] for i, letter in enumerate(letters)}
            expected = sum(int(k) * p for k, p in probabilities.items())
            legend = {str(i): level for i, level in enumerate(levels)}
            return {"type": "score", "score": round(expected, 4), "legend": legend, "probabilities": probabilities, "confidence": confidence_of(probabilities)}
        prompt, tokens = build_noul_prompt(state, question.get("instructions"), question.get("criteria"))
        probs = logprobs_to_probs(self.model_impl.next_token_logprobs(prompt, tokens), self.temperature)
        return {"type": "noul", "noul": probs[YES]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _make_backend(args: argparse.Namespace) -> Any:
    if args.backend == "mock":
        return MockBackend()
    if args.fake or args.backend == "fake":
        return LocalLogitBackend(model=FakeLogitModel(), temperature=args.temperature)
    model_id = args.model or os.environ.get("JEV_LOCAL_MODEL", "").strip() or DEFAULT_MODEL
    return LocalLogitBackend(model=HFLogitModel(model_id), temperature=args.temperature)


def _questions_from_args(args: argparse.Namespace) -> dict[str, Any]:
    from jevlab.core import Choice, Noul, Score

    questions: dict[str, Any] = {}
    if args.choice:
        questions["choice"] = Choice.of(*[c.strip() for c in args.choice.split(",") if c.strip()], instructions=args.instructions)
    if args.score:
        questions["score"] = Score([c.strip() for c in args.score.split(",") if c.strip()], args.instructions)
    if args.noul:
        questions["noul"] = Noul(args.noul)
    if not questions:
        raise JevError("--choice / --score / --noul のいずれかを指定してください")
    return questions


def run_bench(jev: Any, path: str) -> dict[str, Any]:
    """JSONL: {"state": ..., "options": [...] | {label: desc}, "expected": "label", "instructions": "..."}"""
    total = correct = 0
    failures: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            answer = jev.choose(case["state"], case["options"], case.get("instructions"))
            total += 1
            if answer.choice == case["expected"]:
                correct += 1
            else:
                failures.append({"state": case["state"], "expected": case["expected"], "got": answer.choice, "confidence": round(answer.confidence, 3)})
    return {"total": total, "correct": correct, "accuracy": round(correct / total, 4) if total else 0.0, "failures": failures[:20]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab semif", description="公開モデルの logit から選択肢確率を読む")
    parser.add_argument("--backend", default="local", help="local (HF) / fake / mock")
    parser.add_argument("--fake", action="store_true", help="FakeLogitModel を使う (モデル不要)")
    parser.add_argument("--model", default=None, help="HF モデル ID (既定 JEV_LOCAL_MODEL または Qwen/Qwen2.5-0.5B-Instruct)")
    parser.add_argument("--temperature", type=float, default=1.0)
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="1 つの state に質問する")
    ask.add_argument("--state", required=True)
    ask.add_argument("--choice", default=None, help="カンマ区切りの選択肢")
    ask.add_argument("--score", default=None, help="カンマ区切りのルーブリック")
    ask.add_argument("--noul", default=None, help="はい/いいえ質問")
    ask.add_argument("--instructions", default=None)
    bench = sub.add_parser("bench", help="JSONL ケースで精度を測る")
    bench.add_argument("--jsonl", required=True)
    args = parser.parse_args(argv)

    from jevlab.core import Jev

    try:
        jev = Jev(_make_backend(args))
        if args.command == "ask":
            decision = jev.decide(args.state, _questions_from_args(args))
            print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(run_bench(jev, args.jsonl), ensure_ascii=False, indent=2))
    except JevError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
