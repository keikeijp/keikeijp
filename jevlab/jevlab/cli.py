"""`jevlab` コマンド。`jevlab list` で一覧、`jevlab <app> --help` で各アプリの使い方。"""

from __future__ import annotations

import importlib
import json
import sys

from jevlab.apps import APPS


def _print_list() -> None:
    width = max(len(name) for name in APPS)
    for name, (_, description) in APPS.items():
        print(f"  {name.ljust(width)}  {description}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print("usage: jevlab <app> [args...]\n\napps:")
        _print_list()
        print("\nbackend: JEV_BACKEND=typesafe|openrouter|mock|local (未設定なら API キーの有無で自動選択)")
        return 0
    if argv[0] == "list":
        _print_list()
        return 0
    if argv[0] == "ask":
        return _ask(argv[1:])
    name = argv[0]
    if name not in APPS:
        print(f"unknown app: {name}\n", file=sys.stderr)
        _print_list()
        return 2
    module_name, _ = APPS[name]
    module = importlib.import_module(module_name)
    return int(module.main(argv[1:]) or 0)


def _ask(argv: list[str]) -> int:
    """`jevlab ask "state" --choice a,b,c` のような 1 発判定。"""
    import argparse

    from jevlab.core import Choice, Jev, Noul, Score

    parser = argparse.ArgumentParser(prog="jevlab ask", description="Jev に 1 回だけ質問する")
    parser.add_argument("state", help="判定対象のテキスト (または @file.json)")
    parser.add_argument("--choice", help="カンマ区切りの選択肢")
    parser.add_argument("--score", help="カンマ区切りの順序付きルーブリック")
    parser.add_argument("--noul", help="はい/いいえ で答える質問")
    parser.add_argument("--instructions", "-i", default=None)
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)
    state: object = args.state
    if isinstance(state, str) and state.startswith("@"):
        with open(state[1:], encoding="utf-8") as handle:
            state = json.load(handle)
    questions: dict[str, object] = {}
    if args.choice:
        questions["choice"] = Choice.of(*[c.strip() for c in args.choice.split(",")], instructions=args.instructions)
    if args.score:
        questions["score"] = Score([c.strip() for c in args.score.split(",")], args.instructions)
    if args.noul:
        questions["noul"] = Noul(args.noul)
    if not questions:
        parser.error("--choice / --score / --noul のいずれかが必要です")
    decision = Jev(args.backend).decide(state, questions)  # type: ignore[arg-type]
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
