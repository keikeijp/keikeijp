"""28. jevlike: 可変長の選択肢集合を一度に評価する小型モデルの学習スキャフォールド。

元ネタ: vinnylarouge/jevlike

- `make_dataset(n, seed)`: テンプレートから (state, options, gold) を合成 (意図ルーティング / 感情 / 算数比較 / キーワード一致)
- `HashedLinearScorer`: (state トークン × option トークン) ペアを特徴ハッシュした線形スコアラ。
  選択肢集合上の softmax 交差エントロピーを SGD で学習。依存ゼロ・純 Python で本当に学習できる
- `build_torch_set_scorer` / `train_torch`: torch があれば cross-encoder-lite (共有エンコーダ + 集合 softmax) を学習
- `JevlikeBackend`: core の Backend プロトコル。choice=集合スコアリング、score=ルーブリック段階を選択肢化、noul=yes/no 選択肢化

```
jevlab jevlike gen --n 2000 --out data.jsonl
jevlab jevlike train --data data.jsonl --out model.json
jevlab jevlike eval --data data.jsonl --model model.json
jevlab jevlike serve-as-backend
```
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import zlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jevlab.core.backends import JevError, tokenize

# ---------------------------------------------------------------------------
# 合成データ
# ---------------------------------------------------------------------------

INTENTS: dict[str, list[str]] = {
    "refund": ["I was charged twice, please refund me", "want my money back for the order", "refund the duplicate payment"],
    "bug": ["the app crashes when I open settings", "getting an error on login", "the page is broken and shows a bug"],
    "shipping": ["where is my package", "the delivery is late", "track my shipment please"],
    "account": ["cannot reset my password", "change the email on my account", "locked out of my account"],
    "sales": ["do you offer a discount for teams", "what does the enterprise plan cost", "pricing for 50 seats"],
}
SENTIMENTS: dict[str, list[str]] = {
    "positive": ["this is wonderful, I love it", "great service, very happy", "excellent and fast, thank you"],
    "negative": ["terrible experience, very disappointed", "awful, I hate this", "slow and useless, angry"],
    "neutral": ["the meeting is at noon", "the file has three pages", "it is a table with four legs"],
}
TOPICS = ["python", "cooking", "finance", "travel", "music", "gardening", "chess", "astronomy"]
TITLE_TEMPLATES = ["notes about {t}", "a guide to {t}", "{t} for beginners", "weekly {t} digest"]


def _sample_options(rng: random.Random, gold: str, pool: Sequence[str], k_min: int = 2, k_max: int = 5) -> tuple[list[str], int]:
    others = [p for p in pool if p != gold]
    k = rng.randint(k_min, min(k_max, len(pool)))
    options = rng.sample(others, k - 1) + [gold]
    rng.shuffle(options)
    return options, options.index(gold)


def make_example(rng: random.Random) -> dict[str, Any]:
    task = rng.choice(["intent", "sentiment", "math", "keyword"])
    if task == "intent":
        gold = rng.choice(list(INTENTS))
        state = f"route the ticket: {rng.choice(INTENTS[gold])}"
        options, index = _sample_options(rng, gold, list(INTENTS))
    elif task == "sentiment":
        gold = rng.choice(list(SENTIMENTS))
        state = f"sentiment of: {rng.choice(SENTIMENTS[gold])}"
        options, index = _sample_options(rng, gold, list(SENTIMENTS))
    elif task == "math":
        a, b = rng.randint(0, 9), rng.randint(0, 9)
        gold = str(a + b)
        state = f"what is {a} plus {b}"
        pool = [str(v) for v in range(0, 19)]
        options, index = _sample_options(rng, gold, pool, 2, 4)
    else:
        topic = rng.choice(TOPICS)
        state = f"find the document about {topic}"
        gold = rng.choice(TITLE_TEMPLATES).format(t=topic)
        pool = [rng.choice(TITLE_TEMPLATES).format(t=t) for t in TOPICS if t != topic]
        options, index = _sample_options(rng, gold, pool + [gold])
    return {"task": task, "state": state, "options": options, "gold": index}


def make_dataset(n: int, seed: int = 0) -> list[dict[str, Any]]:
    """決定的な合成データ。同じ (n, seed) なら同じ列。"""
    rng = random.Random(seed)
    return [make_example(rng) for _ in range(n)]


def write_jsonl(path: str, rows: Iterable[Mapping[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------------
# 特徴ハッシュ線形スコアラ (純 Python)
# ---------------------------------------------------------------------------

OVERLAP_FEATURE = "__overlap__"
BIAS_FEATURE = "__bias__"


def _hash(text: str, n: int) -> int:
    return zlib.crc32(text.encode("utf-8")) % n


class HashedLinearScorer:
    """score(state, option) = w · φ(state, option)。φ は

    - (state token × option token) ペア
    - option token 単独 (選択肢の事前バイアス)
    - option token が state に含まれる回数 (語彙重なり; 未学習でも MockBackend 風に動く)

    を 1 本の重みベクトルにハッシュしたもの。可変長の選択肢集合に対し softmax で確率化する。
    """

    def __init__(self, n_features: int = 1 << 18, lr: float = 0.2, l2: float = 1e-5, prior_overlap: float = 1.0, seed: int = 0):
        self.n_features = n_features
        self.lr = lr
        self.l2 = l2
        self.seed = seed
        self.w: dict[int, float] = {}
        if prior_overlap:
            self.w[_hash(OVERLAP_FEATURE, n_features)] = prior_overlap
        self.history: list[dict[str, float]] = []

    def features(self, state: Any, option: str) -> dict[int, float]:
        state_tokens = tokenize(state)
        option_tokens = tokenize(option)
        feats: dict[int, float] = {}
        state_set = set(state_tokens)

        def add(key: str, value: float = 1.0) -> None:
            index = _hash(key, self.n_features)
            feats[index] = feats.get(index, 0.0) + value

        for o in option_tokens:
            add(f"o:{o}")
            for s in state_set:
                add(f"p:{s}|{o}")
        overlap = sum(1 for o in option_tokens if o in state_set)
        add(OVERLAP_FEATURE, overlap / max(1, len(option_tokens)))
        add(BIAS_FEATURE)
        return feats

    def score_features(self, feats: Mapping[int, float]) -> float:
        w = self.w
        return sum(w.get(i, 0.0) * v for i, v in feats.items())

    def score(self, state: Any, option: str) -> float:
        return self.score_features(self.features(state, option))

    @staticmethod
    def _softmax(scores: Sequence[float]) -> list[float]:
        top = max(scores)
        exps = [math.exp(s - top) for s in scores]
        total = sum(exps)
        return [e / total for e in exps]

    def predict_proba(self, state: Any, options: Sequence[str]) -> list[float]:
        if not options:
            return []
        return self._softmax([self.score(state, o) for o in options])

    def predict(self, state: Any, options: Sequence[str]) -> int:
        probs = self.predict_proba(state, options)
        return max(range(len(probs)), key=lambda i: probs[i])

    def fit(self, dataset: Sequence[Mapping[str, Any]], epochs: int = 5, lr: float | None = None, log: Any = None) -> "HashedLinearScorer":
        """選択肢集合上の softmax 交差エントロピーを SGD で最小化。特徴は例ごとに 1 回だけ計算する。"""
        lr = self.lr if lr is None else lr
        rng = random.Random(self.seed)
        cached = [([self.features(ex["state"], o) for o in ex["options"]], int(ex["gold"])) for ex in dataset]
        for epoch in range(epochs):
            rng.shuffle(cached)
            loss = 0.0
            correct = 0
            for feats_list, gold in cached:
                scores = [self.score_features(f) for f in feats_list]
                probs = self._softmax(scores)
                loss -= math.log(max(probs[gold], 1e-12))
                if max(range(len(probs)), key=lambda i: probs[i]) == gold:
                    correct += 1
                for j, feats in enumerate(feats_list):
                    grad = probs[j] - (1.0 if j == gold else 0.0)
                    if abs(grad) < 1e-9:
                        continue
                    for i, v in feats.items():
                        self.w[i] = self.w.get(i, 0.0) * (1 - lr * self.l2) - lr * grad * v
            record = {"epoch": epoch, "loss": round(loss / max(1, len(cached)), 4), "accuracy": round(correct / max(1, len(cached)), 4)}
            self.history.append(record)
            if log:
                log(record)
        return self

    def evaluate(self, dataset: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        correct = sum(1 for ex in dataset if self.predict(ex["state"], ex["options"]) == int(ex["gold"]))
        return {"n": len(dataset), "accuracy": correct / max(1, len(dataset))}

    def to_dict(self) -> dict[str, Any]:
        return {"n_features": self.n_features, "lr": self.lr, "l2": self.l2, "w": {str(i): v for i, v in self.w.items() if v}}

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HashedLinearScorer":
        scorer = cls(n_features=int(data["n_features"]), lr=float(data.get("lr", 0.2)), l2=float(data.get("l2", 1e-5)), prior_overlap=0.0)
        scorer.w = {int(i): float(v) for i, v in data["w"].items()}
        return scorer

    @classmethod
    def load(cls, path: str) -> "HashedLinearScorer":
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# ---------------------------------------------------------------------------
# torch 版 (遅延 import)
# ---------------------------------------------------------------------------


def _hashed_ids(text: Any, vocab: int) -> list[int]:
    return [_hash(f"t:{t}", vocab) for t in tokenize(text)] or [0]


def build_torch_set_scorer(vocab: int = 1 << 16, dim: int = 64) -> Any:
    """cross-encoder-lite: 共有 EmbeddingBag で state と option を符号化し、[s; o; s*o] → MLP → スカラー。
    集合内の各 option のスカラーを softmax する。torch が無ければ JevError。"""
    try:
        import torch
        from torch import nn
    except ImportError as error:
        raise JevError("TorchSetScorer には torch が必要です: pip install 'jevlab[local]'") from error

    class TorchSetScorer(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embed = nn.EmbeddingBag(vocab, dim, mode="mean")
            self.head = nn.Sequential(nn.Linear(dim * 3, dim), nn.ReLU(), nn.Linear(dim, 1))

        def encode(self, texts: Sequence[Any]) -> Any:
            ids, offsets = [], []
            for text in texts:
                offsets.append(len(ids))
                ids.extend(_hashed_ids(text, vocab))
            return self.embed(torch.tensor(ids), torch.tensor(offsets))

        def forward(self, state: Any, options: Sequence[str]) -> Any:
            s = self.encode([state]).expand(len(options), -1)
            o = self.encode(options)
            return self.head(torch.cat([s, o, s * o], dim=-1)).squeeze(-1)  # (K,)

    return TorchSetScorer()


def train_torch(dataset: Sequence[Mapping[str, Any]], epochs: int = 3, lr: float = 1e-2, log: Any = None) -> Any:
    """集合 softmax 交差エントロピーで TorchSetScorer を学習して返す。"""
    model = build_torch_set_scorer()
    import torch

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    for epoch in range(epochs):
        total = 0.0
        for ex in dataset:
            logits = model(ex["state"], ex["options"])
            loss = torch.nn.functional.cross_entropy(logits.unsqueeze(0), torch.tensor([int(ex["gold"])]))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss)
        if log:
            log({"epoch": epoch, "loss": total / max(1, len(dataset))})
    return model


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class JevlikeBackend:
    """HashedLinearScorer を core の Backend として使う。

    - choice: 選択肢 = "label: 説明"、state に instructions を前置
    - score : ルーブリック段階を選択肢として集合スコアリング → 期待値
    - noul  : ["yes", "no"] を選択肢にし、instructions と criteria を state に含める
    """

    name = "jevlike"

    def __init__(self, scorer: HashedLinearScorer | None = None, model_name: str = "jevlike-hashed"):
        self.scorer = scorer or HashedLinearScorer()
        self.model = model_name

    def decide(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        answers = {name: self._answer(state, q) for name, q in questions.items()}
        return {"answers": answers, "model": self.model, "usage": {"input_tokens": 0, "output_tokens": 0}}, self.model

    @staticmethod
    def _text(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def _answer(self, state: Any, question: Mapping[str, Any]) -> dict[str, Any]:
        qtype = question["type"]
        instructions = question.get("instructions")
        prefix = f"{self._text(instructions)} :: " if instructions is not None else ""
        composed = prefix + self._text(state)
        if qtype == "choice":
            criteria = question["criteria"]
            labels = list(criteria)
            options = [label.replace("_", " ") if criteria[label] is None else f"{label.replace('_', ' ')}: {self._text(criteria[label])}" for label in labels]
            probs = self.scorer.predict_proba(composed, options)
            probabilities = dict(zip(labels, probs))
            choice = max(probabilities.items(), key=lambda kv: kv[1])[0]
            ranked = sorted(probs, reverse=True)
            return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)}
        if qtype == "score":
            levels = [self._text(level) for level in question["criteria"]]
            probs = self.scorer.predict_proba(composed, levels)
            probabilities = {str(i): p for i, p in enumerate(probs)}
            return {
                "type": "score",
                "score": round(sum(i * p for i, p in enumerate(probs)), 4),
                "legend": {str(i): level for i, level in enumerate(question["criteria"])},
                "probabilities": probabilities,
                "confidence": max(probs),
            }
        criteria = question.get("criteria") or {}
        yes = f"yes: {self._text(criteria['true'])}" if criteria.get("true") is not None else "yes"
        no = f"no: {self._text(criteria['false'])}" if criteria.get("false") is not None else "no"
        probs = self.scorer.predict_proba(composed, [yes, no])
        return {"type": "noul", "noul": probs[0]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab jevlike", description="可変長選択肢集合を評価する小型モデルの学習")
    parser.add_argument("--backend", default="jevlike", help="serve-as-backend の説明用 (jevlike 固定)")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("gen", help="合成データを JSONL に書く")
    gen.add_argument("--n", type=int, default=2000)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--out", required=True)
    train = sub.add_parser("train", help="HashedLinearScorer (または --torch) を学習")
    train.add_argument("--data", required=True)
    train.add_argument("--out", required=True)
    train.add_argument("--epochs", type=int, default=5)
    train.add_argument("--holdout", type=float, default=0.2)
    train.add_argument("--torch", action="store_true", help="TorchSetScorer を学習 (torch 必須)")
    ev = sub.add_parser("eval", help="学習済みモデルを評価")
    ev.add_argument("--data", required=True)
    ev.add_argument("--model", required=True)
    sub.add_parser("serve-as-backend", help="Jev バックエンドとして使う方法を表示")
    args = parser.parse_args(argv)

    try:
        if args.command == "gen":
            write_jsonl(args.out, make_dataset(args.n, args.seed))
            print(json.dumps({"written": args.n, "out": args.out}))
        elif args.command == "train":
            data = read_jsonl(args.data)
            split = int(len(data) * (1 - args.holdout))
            train_set, held = data[:split], data[split:]
            if args.torch:
                model = train_torch(train_set, epochs=args.epochs, log=lambda r: print(json.dumps(r)))
                import torch

                torch.save(model.state_dict(), args.out)
                print(json.dumps({"saved": args.out, "kind": "torch"}))
            else:
                scorer = HashedLinearScorer().fit(train_set, epochs=args.epochs, log=lambda r: print(json.dumps(r)))
                scorer.save(args.out)
                print(json.dumps({"saved": args.out, "holdout": scorer.evaluate(held) if held else None}))
        elif args.command == "eval":
            scorer = HashedLinearScorer.load(args.model)
            print(json.dumps(scorer.evaluate(read_jsonl(args.data))))
        else:
            print("from jevlab.core import Jev\nfrom jevlab.local.jevlike import JevlikeBackend, HashedLinearScorer\n"
                  "jev = Jev(JevlikeBackend(HashedLinearScorer.load('model.json')))\n"
                  "または `jevlab serve --engine jevlike --model model.json` で HTTP API として公開")
    except JevError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
