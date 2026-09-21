"""5. fast-jev-compaction: Claude Code の会話履歴からもう要らないツール呼び出しを Jev で削る。

元ネタ: tamaratran/fast-jev-compaction

入力は Claude Code のセッション JSONL (1 行 1 メッセージ、`type` が user/assistant で
`message.content` にブロックが並ぶ) または素の Anthropic messages 形式 (`[{role, content:[...]}]`)。

アルゴリズム:
1. 末尾 N メッセージは固定 (preserve_recent_messages)
2. それより前の tool_use / tool_result ペアごとに Jev へ 2 つの Noul を 1 回で聞く
   - keep_call            : このツール呼び出しはタスク継続に今も必要か
   - keep_result_verbatim : 結果テキストを一字一句そのまま後で参照する必要があるか
3. 段階的に圧縮する
   - 呼び出しも結果も不要 → ペアごと削除 (メッセージが空になる場合のみ。他に内容があれば結果を 1 行のメモに置換)
   - 結果だけ不要        → 結果を 300 文字 + 1 行メモに切り詰め
   - 古い tool_use の input は 1000 → 200 → 60 文字に縮める
   - tool_result 内の長いテキストは head+tail
   user/assistant の本文テキスト (text ブロック) は決して変更しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jevlab.core import Jev, Noul

RESULT_KEEP_CHARS = 300
TEXT_NOTE_CHARS = 200
INPUT_NOTE_CHARS = 60
INPUT_LIMITS = (1000, 200, 60)  # 新しい → 古い

QUESTIONS = {
    "keep_call": Noul(
        "Look at the tool invocation marked <<CANDIDATE>>. Does this tool invocation still matter for continuing the task? "
        "Answer yes if later steps depend on knowing it happened (e.g. a file edit, a command with side effects, a lookup whose "
        "outcome shaped the plan). Answer no if it was exploratory, superseded by later calls, or irrelevant to the remaining work.",
        {"true": "later work depends on this call having happened", "false": "exploratory, superseded, or irrelevant now"},
    ),
    "keep_result_verbatim": Noul(
        "Would the assistant need the exact result text of the <<CANDIDATE>> tool call again (line numbers, exact strings, "
        "error messages it still has to act on)? Answer no if a short summary would be enough.",
        {"true": "exact text is still needed", "false": "a short note is enough"},
    ),
}


# ---------------------------------------------------------------------------
# メッセージの正規化 (Claude Code JSONL / Anthropic messages の両対応)
# ---------------------------------------------------------------------------


def _role(message: dict[str, Any]) -> str | None:
    if "role" in message and "message" not in message:
        return str(message["role"])
    if message.get("type") in {"user", "assistant"}:
        inner = message.get("message")
        return str(inner.get("role", message["type"])) if isinstance(inner, dict) else str(message["type"])
    return None


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """content をブロック配列として返す (文字列は text ブロック 1 つに変換)。"""
    holder = message["message"] if "message" in message and isinstance(message["message"], dict) else message
    content = holder.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content]
    return []


def _set_blocks(message: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    new = dict(message)
    if "message" in message and isinstance(message["message"], dict):
        new["message"] = dict(message["message"], content=blocks)
    else:
        new["content"] = blocks
    return new


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _set_result_text(block: dict[str, Any], text: str) -> dict[str, Any]:
    new = dict(block)
    new["content"] = text if isinstance(block.get("content"), str) or block.get("content") is None else [{"type": "text", "text": text}]
    return new


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n...[{len(text) - limit} chars omitted]...\n{text[-tail:]}"


def _shrink_input(tool_input: Any, limit: int) -> Any:
    """tool_use.input を limit 文字に収める。dict は各値を切り詰め、超える場合は文字列表現にする。"""
    dumped = json.dumps(tool_input, ensure_ascii=False, default=str)
    if len(dumped) <= limit:
        return tool_input
    if isinstance(tool_input, dict):
        per_key = max(20, limit // max(1, len(tool_input)))
        shrunk = {k: (_clip(v, per_key) if isinstance(v, str) else v) for k, v in tool_input.items()}
        dumped = json.dumps(shrunk, ensure_ascii=False, default=str)
        if len(dumped) <= limit:
            return shrunk
    return {"_truncated": _clip(dumped, limit)}


# ---------------------------------------------------------------------------
# ツール呼び出しペアの検出と状態の要約
# ---------------------------------------------------------------------------


@dataclass
class ToolPair:
    call_msg: int
    call_block: int
    result_msg: int | None
    result_block: int | None
    name: str
    tool_use_id: str
    keep_call: float = 1.0
    keep_result: float = 1.0
    action: str = "keep"  # keep / truncate / drop


def find_tool_pairs(messages: Sequence[dict[str, Any]]) -> list[ToolPair]:
    pairs: list[ToolPair] = []
    by_id: dict[str, ToolPair] = {}
    for mi, message in enumerate(messages):
        role = _role(message)
        if role is None:
            continue
        for bi, block in enumerate(_blocks(message)):
            if block.get("type") == "tool_use":
                pair = ToolPair(mi, bi, None, None, str(block.get("name", "tool")), str(block.get("id", f"{mi}:{bi}")))
                pairs.append(pair)
                by_id[pair.tool_use_id] = pair
            elif block.get("type") == "tool_result":
                pair = by_id.get(str(block.get("tool_use_id", "")))
                if pair is not None and pair.result_msg is None:
                    pair.result_msg, pair.result_block = mi, bi
    return pairs


def message_note(message: dict[str, Any], candidate: ToolPair | None = None, index: int = -1) -> str:
    role = _role(message) or message.get("type", "?")
    parts: list[str] = []
    for bi, block in enumerate(_blocks(message)):
        mark = ""
        if candidate is not None and ((index == candidate.call_msg and bi == candidate.call_block) or (index == candidate.result_msg and bi == candidate.result_block)):
            mark = "<<CANDIDATE>> "
        kind = block.get("type")
        if kind == "text":
            parts.append(mark + _clip(str(block.get("text", "")), TEXT_NOTE_CHARS))
        elif kind == "tool_use":
            parts.append(f"{mark}tool {block.get('name')}({_clip(json.dumps(block.get('input', {}), ensure_ascii=False, default=str), INPUT_NOTE_CHARS)})")
        elif kind == "tool_result":
            parts.append(f"{mark}result({'error' if block.get('is_error') else 'ok'}): {_clip(_result_text(block), INPUT_NOTE_CHARS)}")
        elif kind:
            parts.append(f"{mark}[{kind}]")
    return f"[{index} {role}] " + " | ".join(parts)


def build_state(messages: Sequence[dict[str, Any]], pair: ToolPair, window: int = 8, tail: int = 12) -> dict[str, Any]:
    """候補ペアの前後 + 末尾 + 冒頭だけの短い会話要約。巨大な履歴でも state が膨らまないようにする。"""
    n = len(messages)
    wanted: set[int] = set(range(0, min(3, n))) | set(range(max(0, n - tail), n))
    wanted |= set(range(max(0, pair.call_msg - window), min(n, (pair.result_msg or pair.call_msg) + window + 1)))
    notes: list[str] = []
    last = -1
    for i in sorted(wanted):
        if i != last + 1 and last >= 0:
            notes.append(f"... ({i - last - 1} messages omitted) ...")
        notes.append(message_note(messages[i], pair, i))
        last = i
    call_block = _blocks(messages[pair.call_msg])[pair.call_block]
    result_preview = _clip(_result_text(_blocks(messages[pair.result_msg])[pair.result_block]), 300) if pair.result_msg is not None else ""
    return {
        "conversation": notes,
        "candidate": {
            "tool": pair.name,
            "input": _clip(json.dumps(call_block.get("input", {}), ensure_ascii=False, default=str), 200),
            "result_preview": result_preview,
            "messages_after_candidate": n - 1 - (pair.result_msg if pair.result_msg is not None else pair.call_msg),
        },
    }


# ---------------------------------------------------------------------------
# 圧縮本体
# ---------------------------------------------------------------------------


@dataclass
class CompactionReport:
    messages_before: int = 0
    messages_after: int = 0
    chars_before: int = 0
    chars_after: int = 0
    pairs_total: int = 0
    pairs_pinned: int = 0
    pairs_kept: int = 0
    pairs_truncated: int = 0
    pairs_dropped: int = 0
    jev_calls: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reduction_ratio(self) -> float:
        return 1.0 - self.chars_after / self.chars_before if self.chars_before else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "decisions"}
        data["reduction_ratio"] = round(self.reduction_ratio, 4)
        return data


def _chars(messages: Sequence[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, default=str))


def _input_limit(index: int, n_messages: int, preserve: int) -> int:
    age = n_messages - index
    if age <= preserve * 2:
        return INPUT_LIMITS[0]
    if age <= preserve * 4:
        return INPUT_LIMITS[1]
    return INPUT_LIMITS[2]


def _apply(messages: Sequence[dict[str, Any]], pairs: list[ToolPair], preserve: int, max_result_chars: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    n = len(messages)
    pinned_from = max(0, n - preserve)
    drop_blocks: dict[int, set[int]] = {}
    replace_result: dict[tuple[int, int], str] = {}
    counts = {"keep": 0, "truncate": 0, "drop": 0}
    for pair in pairs:
        if pair.action == "keep":
            counts["keep"] += 1
            continue
        if pair.action == "drop":
            call_only = all(b.get("type") == "tool_use" for b in _blocks(messages[pair.call_msg]))
            result_only = pair.result_msg is not None and all(b.get("type") == "tool_result" for b in _blocks(messages[pair.result_msg]))
            if call_only and (result_only or pair.result_msg is None):
                drop_blocks.setdefault(pair.call_msg, set()).add(pair.call_block)
                if pair.result_msg is not None:
                    drop_blocks.setdefault(pair.result_msg, set()).add(pair.result_block)  # type: ignore[arg-type]
                counts["drop"] += 1
                continue
            # メッセージに他の内容がある場合は tool_use を残して結果をメモだけにする
            if pair.result_msg is not None:
                replace_result[(pair.result_msg, pair.result_block)] = f"[tool result omitted by compaction: {pair.name}]"  # type: ignore[index]
            counts["truncate"] += 1
            continue
        if pair.result_msg is not None:
            text = _result_text(_blocks(messages[pair.result_msg])[pair.result_block])  # type: ignore[index]
            if len(text) > max_result_chars:
                replace_result[(pair.result_msg, pair.result_block)] = text[:max_result_chars] + f"\n[compacted: {len(text) - max_result_chars} chars of {pair.name} result omitted]"  # type: ignore[index]
        counts["truncate"] += 1

    out: list[dict[str, Any]] = []
    for mi, message in enumerate(messages):
        role = _role(message)
        if role is None or mi >= pinned_from:
            out.append(message)
            continue
        blocks = _blocks(message)
        new_blocks: list[dict[str, Any]] = []
        changed = False
        limit = _input_limit(mi, n, preserve)
        for bi, block in enumerate(blocks):
            if bi in drop_blocks.get(mi, set()):
                changed = True
                continue
            kind = block.get("type")
            if kind == "tool_use":
                original = block.get("input", {})
                shrunk = _shrink_input(original, limit)
                if shrunk is not original:
                    block = dict(block, input=shrunk)
                    changed = True
            elif kind == "tool_result":
                if (mi, bi) in replace_result:
                    block = _set_result_text(block, replace_result[(mi, bi)])
                    changed = True
                else:
                    text = _result_text(block)
                    if len(text) > max_result_chars * 10:
                        block = _set_result_text(block, _head_tail(text, max_result_chars * 10))
                        changed = True
            new_blocks.append(block)
        if not new_blocks and blocks:
            continue  # 中身が全部消えたメッセージは落とす
        out.append(_set_blocks(message, new_blocks) if changed else message)
    return out, counts


def compact_messages(
    messages: Sequence[dict[str, Any]],
    jev: Jev,
    preserve_recent_messages: int = 4,
    target_ratio: float | None = None,
    keep_call_threshold: float = 0.5,
    keep_result_threshold: float = 0.5,
    max_result_chars: int = RESULT_KEEP_CHARS,
) -> tuple[list[dict[str, Any]], CompactionReport]:
    """履歴を圧縮して (新しいメッセージ列, レポート) を返す。

    target_ratio (0..1, 例 0.5 = 半分に) を指定すると、達成するまで keep 閾値を段階的に厳しくする。
    """
    messages = list(messages)
    report = CompactionReport(messages_before=len(messages), chars_before=_chars(messages))
    n = len(messages)
    pinned_from = max(0, n - preserve_recent_messages)
    pairs = find_tool_pairs(messages)
    report.pairs_total = len(pairs)
    candidates = [p for p in pairs if p.call_msg < pinned_from and (p.result_msg is None or p.result_msg < pinned_from)]
    report.pairs_pinned = len(pairs) - len(candidates)

    if candidates:
        decisions = jev.decide_many((build_state(messages, pair), QUESTIONS) for pair in candidates)
        report.jev_calls = len(decisions)
        for pair, decision in zip(candidates, decisions):
            pair.keep_call = decision.noul("keep_call").noul
            pair.keep_result = decision.noul("keep_result_verbatim").noul

    def assign(call_threshold: float, result_threshold: float) -> None:
        for pair in candidates:
            if pair.keep_result >= result_threshold and pair.keep_call >= call_threshold:
                pair.action = "keep"
            elif pair.keep_call < call_threshold and pair.keep_result < result_threshold:
                pair.action = "drop"
            else:
                pair.action = "truncate"

    assign(keep_call_threshold, keep_result_threshold)
    new_messages, counts = _apply(messages, pairs, preserve_recent_messages, max_result_chars)
    if target_ratio is not None:
        # 目標に届かなければ「残す」判定の閾値を上げていく (確信の低いものから削る)
        for step in (0.6, 0.7, 0.8, 0.9, 0.97, 1.01):
            if 1.0 - _chars(new_messages) / max(1, report.chars_before) >= target_ratio:
                break
            assign(max(keep_call_threshold, step), max(keep_result_threshold, step))
            new_messages, counts = _apply(messages, pairs, preserve_recent_messages, max_result_chars)

    report.pairs_kept = counts["keep"] + report.pairs_pinned
    report.pairs_truncated = counts["truncate"]
    report.pairs_dropped = counts["drop"]
    report.messages_after = len(new_messages)
    report.chars_after = _chars(new_messages)
    report.decisions = [
        {"tool": p.name, "id": p.tool_use_id, "keep_call": round(p.keep_call, 3), "keep_result_verbatim": round(p.keep_result, 3), "action": p.action} for p in candidates
    ]
    return new_messages, report


# ---------------------------------------------------------------------------
# 入出力
# ---------------------------------------------------------------------------


def load_transcript(path: str) -> tuple[list[dict[str, Any]], str]:
    """JSONL または JSON 配列を読む。戻り値の 2 つ目は書き出し形式 ("jsonl" / "json")。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(stripped)
        return [m for m in data if isinstance(m, dict)], "json"
    messages = [json.loads(line) for line in text.splitlines() if line.strip()]
    return messages, "jsonl"


def dump_transcript(messages: Sequence[dict[str, Any]], path: str, fmt: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        if fmt == "json":
            json.dump(list(messages), handle, ensure_ascii=False, indent=2)
        else:
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab compaction", description="Claude Code 履歴の不要なツール呼び出しを Jev で圧縮する")
    parser.add_argument("--input", required=True, help="セッション JSONL (または messages の JSON 配列)")
    parser.add_argument("--output", help="書き出し先 (省略時は stdout に JSONL)")
    parser.add_argument("--preserve", type=int, default=4, help="末尾で固定するメッセージ数")
    parser.add_argument("--target-ratio", type=float, default=None, help="目標削減率 0..1 (例 0.5)")
    parser.add_argument("--max-result-chars", type=int, default=RESULT_KEEP_CHARS)
    parser.add_argument("--stats", action="store_true", help="レポートを stderr に出す")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    messages, fmt = load_transcript(args.input)
    jev = Jev(args.backend)
    new_messages, report = compact_messages(messages, jev, args.preserve, args.target_ratio, max_result_chars=args.max_result_chars)
    if args.output:
        dump_transcript(new_messages, args.output, fmt)
    else:
        for message in new_messages:
            print(json.dumps(message, ensure_ascii=False))
    if args.stats:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
