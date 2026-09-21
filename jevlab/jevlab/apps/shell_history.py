"""12. jev-shell-history (mrnugget/jev-shell-history の再実装): 入力途中の文字列に合う過去コマンドを Jev が選ぶ zsh 補完。

流れ: zsh の ZLE ウィジェット (shell_history.zsh) が `$BUFFER` と `$PWD` を渡す →
      Python 側が履歴を安価に前絞り込み (prefix / 部分列 / 頻度 / 新しさ) →
      上位 20 件から Jev が 1 つ Choice で選ぶ → BUFFER を置き換える。

- `load_history(path)`                 : zsh 拡張履歴 (`: 1699999999:0;cmd`) と素の履歴の両方を読む
- `candidates(prefix, history, cwd)`   : Jev に渡す前の決定的な前絞り込み
- `pick(jev, typed, candidates, ctx)`  : Choice (criteria = {index: command}) + confidence 閾値
- ディスクキャッシュ + タイムアウトでシェルを止めない

    python -m jevlab.apps.shell_history pick --typed "git pu" --cwd "$PWD" --history ~/.zsh_history
    python -m jevlab.apps.shell_history --install     # .zshrc に書く 1 行を表示
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jevlab.core import Choice, Jev

MAX_CANDIDATES = 20
DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "jevlab", "shell_history.json")
ZSH_WIDGET_PATH = Path(__file__).with_suffix(".zsh")

_EXTENDED_RE = re.compile(r"^: (\d+):(\d+);(.*)$", re.S)


@dataclass
class HistoryEntry:
    command: str
    timestamp: int = 0
    index: int = 0


@dataclass
class PickResult:
    best: str | None
    confidence: float
    ranked: list[dict[str, Any]] = field(default_factory=list)
    source: str = "jev"  # jev / cache / prefilter / none
    latency_ms: float = 0.0
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 履歴の読み込み
# ---------------------------------------------------------------------------


def parse_history(text: str) -> list[HistoryEntry]:
    """zsh 拡張形式 (`: ts:dur;cmd`) と素の 1 行 1 コマンド形式を混在で受ける。`\\` 終わりの行は継続行として結合。"""
    entries: list[HistoryEntry] = []
    pending: str | None = None
    pending_ts = 0
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if pending is not None:
            pending += "\n" + line
            if line.endswith("\\"):
                continue
            entries.append(HistoryEntry(pending.replace("\\\n", "\n").strip(), pending_ts, len(entries)))
            pending = None
            continue
        if not line.strip():
            continue
        match = _EXTENDED_RE.match(line)
        if match:
            ts, cmd = int(match.group(1)), match.group(3)
        else:
            ts, cmd = 0, line
        if cmd.endswith("\\"):
            pending, pending_ts = cmd, ts
            continue
        entries.append(HistoryEntry(cmd.strip(), ts, len(entries)))
    return entries


def load_history(path: str | os.PathLike[str] | None = None) -> list[HistoryEntry]:
    path = Path(path or os.environ.get("HISTFILE") or os.path.join(os.path.expanduser("~"), ".zsh_history"))
    if not path.is_file():
        return []
    return parse_history(path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# 前絞り込み (Jev を呼ぶ前の安価なフィルタ)
# ---------------------------------------------------------------------------


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def match_score(prefix: str, command: str) -> float:
    """prefix と command の字面の一致度。0 は不一致。"""
    if not prefix:
        return 1.0
    p, c = prefix.lower(), command.lower()
    if c == p:
        return 0.0  # 既に打ち終えたものは候補にしない
    if c.startswith(p):
        return 4.0
    words = c.split()
    if all(any(w.startswith(tok) for w in words) for tok in p.split()):
        return 3.0
    if p in c:
        return 2.0
    if _is_subsequence(p.replace(" ", ""), c):
        return 1.0
    return 0.0


def candidates(prefix: str, history: Sequence[HistoryEntry], cwd: str | None = None, limit: int = MAX_CANDIDATES) -> list[str]:
    """字面一致 + 新しさ + 頻度 + cwd の手がかりで並べた候補コマンド (重複除去)。"""
    if not history:
        return []
    stats: dict[str, dict[str, float]] = {}
    total = len(history)
    for entry in history:
        cmd = entry.command
        if not cmd:
            continue
        item = stats.setdefault(cmd, {"count": 0.0, "last": 0.0, "ts": 0.0})
        item["count"] += 1
        item["last"] = max(item["last"], entry.index / max(1, total - 1))  # 0..1 (新しいほど 1)
        item["ts"] = max(item["ts"], float(entry.timestamp))
    cwd_name = os.path.basename(cwd.rstrip("/")) if cwd else ""
    scored: list[tuple[float, str]] = []
    for cmd, item in stats.items():
        base = match_score(prefix, cmd)
        if base <= 0:
            continue
        score = base + 1.5 * item["last"] + 0.5 * math.log1p(item["count"])
        if cwd_name and cwd_name in cmd:
            score += 0.75
        scored.append((score, cmd))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [cmd for _, cmd in scored[:limit]]


# ---------------------------------------------------------------------------
# Jev による選択
# ---------------------------------------------------------------------------


def pick(jev: Jev, typed: str, cands: Sequence[str], context: Mapping[str, Any] | None = None, threshold: float = 0.35, top_k: int = 3) -> PickResult:
    """候補 (最大 20) から Jev が 1 つ選ぶ。confidence が閾値未満なら best=None (シェル側は何もしない)。"""
    cands = list(cands)[:MAX_CANDIDATES]
    if not cands:
        return PickResult(None, 0.0, [], source="none")
    if len(cands) == 1:
        return PickResult(cands[0], 1.0, [{"command": cands[0], "probability": 1.0}], source="prefilter")
    context = dict(context or {})
    state = {
        "typed": typed,
        "cwd": context.get("cwd", ""),
        "last_commands": list(context.get("last_commands", []))[-5:],
        "hint": "ユーザーは typed を打ちかけている。続きとして最も実行したそうな候補を選ぶ",
    }
    question = Choice({str(i): cmd for i, cmd in enumerate(cands)}, "typed の続きとしてユーザーが実行したい過去コマンドはどれか?")
    started = time.perf_counter()
    answer = jev.decide(state, {"pick": question}).choice("pick")
    latency = (time.perf_counter() - started) * 1000
    ranked = [{"command": cands[int(label)], "probability": round(prob, 4)} for label, prob in answer.ranked()[:top_k] if label.isdigit() and int(label) < len(cands)]
    best = cands[int(answer.choice)] if answer.choice.isdigit() and answer.confidence >= threshold else None
    return PickResult(best, round(answer.confidence, 4), ranked, source="jev", latency_ms=round(latency, 1))


# ---------------------------------------------------------------------------
# キャッシュ + タイムアウト (シェルを待たせない)
# ---------------------------------------------------------------------------


class PickCache:
    """(typed, 候補列, cwd) → PickResult の JSON ファイルキャッシュ。壊れていても無視する。"""

    def __init__(self, path: str | os.PathLike[str] | None = DEFAULT_CACHE, max_items: int = 500):
        self.path = Path(path) if path else None
        self.max_items = max_items
        self._items: dict[str, dict[str, Any]] = {}
        if self.path and self.path.is_file():
            try:
                self._items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._items = {}

    @staticmethod
    def key(typed: str, cands: Sequence[str], cwd: str) -> str:
        return hashlib.sha1(json.dumps([typed, list(cands), cwd], ensure_ascii=False).encode("utf-8")).hexdigest()

    def get(self, key: str) -> PickResult | None:
        item = self._items.get(key)
        if item is None:
            return None
        return PickResult(item.get("best"), float(item.get("confidence", 0)), list(item.get("ranked", [])), source="cache")

    def put(self, key: str, result: PickResult) -> None:
        self._items[key] = {"best": result.best, "confidence": result.confidence, "ranked": result.ranked}
        while len(self._items) > self.max_items:
            self._items.pop(next(iter(self._items)))
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass


def pick_with_timeout(jev: Jev, typed: str, cands: Sequence[str], context: Mapping[str, Any], timeout: float = 1.5, cache: PickCache | None = None, threshold: float = 0.35) -> PickResult:
    """キャッシュ → Jev (timeout 秒まで) → 間に合わなければ前絞り込み 1 位、の順に返す。"""
    cands = list(cands)[:MAX_CANDIDATES]
    if not cands:
        return PickResult(None, 0.0, [], source="none")
    key = PickCache.key(typed, cands, str(context.get("cwd", "")))
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(pick, jev, typed, cands, context, threshold)
    try:
        result = future.result(timeout=timeout)
    except FutureTimeout:
        # Jev のスレッドは放置する (呼び出し側で os._exit するとプロセス終了時の join を待たない)
        return PickResult(cands[0], 0.0, [{"command": cands[0], "probability": 0.0}], source="prefilter", timed_out=True)
    finally:
        pool.shutdown(wait=False)
    if cache is not None and result.source == "jev":
        cache.put(key, result)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def install_line() -> str:
    return f"source {ZSH_WIDGET_PATH}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab shell-history", description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    parser.add_argument("--install", action="store_true", help=".zshrc に追加する source 行を表示")
    sub = parser.add_subparsers(dest="command")
    p_pick = sub.add_parser("pick", help="typed に合う過去コマンドを選ぶ")
    p_pick.add_argument("--typed", default="")
    p_pick.add_argument("--cwd", default=os.getcwd())
    p_pick.add_argument("--history", default=None, help="履歴ファイル (既定 $HISTFILE / ~/.zsh_history)")
    p_pick.add_argument("--limit", type=int, default=MAX_CANDIDATES)
    p_pick.add_argument("--threshold", type=float, default=0.35)
    p_pick.add_argument("--timeout", type=float, default=1.5)
    p_pick.add_argument("--cache", default=DEFAULT_CACHE, help="キャッシュファイル。'' で無効")
    p_pick.add_argument("--plain", action="store_true", help="最良のコマンドだけを出力 (zsh ウィジェット用)")
    p_pick.add_argument("--json", action="store_true")
    p_cand = sub.add_parser("candidates", help="前絞り込みの結果だけを表示 (Jev を呼ばない)")
    p_cand.add_argument("--typed", default="")
    p_cand.add_argument("--cwd", default=os.getcwd())
    p_cand.add_argument("--history", default=None)
    p_cand.add_argument("--limit", type=int, default=MAX_CANDIDATES)
    args = parser.parse_args(argv)

    if args.install:
        print(install_line())
        return 0
    if args.command is None:
        parser.print_help()
        return 2
    history = load_history(args.history)
    cands = candidates(args.typed, history, args.cwd, limit=args.limit)
    if args.command == "candidates":
        print("\n".join(cands))
        return 0
    context = {"cwd": args.cwd, "last_commands": [e.command for e in history[-5:]]}
    cache = PickCache(args.cache) if args.cache else None
    result = pick_with_timeout(Jev(args.backend), args.typed, cands, context, timeout=args.timeout, cache=cache, threshold=args.threshold)
    if args.plain:
        if result.best:
            print(result.best)
        code = 0 if result.best else 1
        if result.timed_out:  # 取り残した Jev スレッドを待たずに即終了 (シェルを止めない)
            sys.stdout.flush()
            os._exit(code)
        return code
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"best: {result.best or '(none)'}  confidence={result.confidence:.2f} source={result.source}")
        for item in result.ranked:
            print(f"  {item['probability']:.2f}  {item['command']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
