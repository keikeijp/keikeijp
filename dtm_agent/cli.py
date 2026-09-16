"""dtm-agent コマンドラインインターフェース。

  dtm-agent index ~/Samples                       # ローカルサンプルをインデックス化
  dtm-agent analyze song.wav --segment 1:05-1:21  # 参照区間の解析 (API 不要)
  dtm-agent match song.wav --segment 1:05-1:21 --library ~/Samples --query "kick"   # 類似サンプル (API 不要)
  dtm-agent run "この曲みたいなローファイを 8 小節作って" --audio song.wav --segment 0:32-0:48 \
      --library ~/Samples --daw reaper --out ./out   # Claude エージェント
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import analyze_file, parse_segment
from .samples import LocalLibrary


def cmd_index(args: argparse.Namespace) -> int:
    lib = LocalLibrary(args.folder, max_seconds=args.max_seconds)

    def progress(i: int, n: int, path: Path) -> None:
        print(f"[{i}/{n}] {path.name}", file=sys.stderr)

    count = lib.build(progress=progress)
    print(f"indexed {count} files -> {lib.index_path}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    start, end = parse_segment(args.segment)
    profile = analyze_file(args.audio, start, end)
    if args.json:
        print(profile.model_dump_json(indent=2))
    else:
        print(profile.describe())
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    start, end = parse_segment(args.segment)
    profile = analyze_file(args.audio, start, end)
    print(f"reference: {profile.describe()}")
    lib = LocalLibrary(args.library)
    if not lib.entries:
        print("index not found; run `dtm-agent index <folder>` first", file=sys.stderr)
        return 1
    for h in lib.search(query=args.query, reference=profile, limit=args.limit):
        print(f"{h.score:+.3f}  {h.path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Session, run_agent

    session = Session(out_dir=args.out, library_dir=args.library, daw=args.daw,
                      ableton_host=args.ableton_host, ableton_port=args.ableton_port)
    prompt = args.prompt
    context = []
    if args.url:
        context.append(f"参照曲 URL: {args.url}")
    if args.audio:
        context.append(f"参照音声ファイル: {args.audio}" + (f" (区間 {args.segment})" if args.segment else ""))
    if context:
        prompt = "\n".join(context) + "\n\n" + prompt

    def on_message(msg) -> None:
        for block in msg.content:
            if block.type == "tool_use":
                print(f"  -> {block.name}", file=sys.stderr)
            elif block.type == "text" and args.verbose:
                print(block.text, file=sys.stderr)

    try:
        text = run_agent(prompt, session, model=args.model, on_message=on_message)
    except TypeError as exc:
        if "authentication" in str(exc).lower():
            print("ANTHROPIC_API_KEY が未設定です (または `ant auth login` を実行してください)", file=sys.stderr)
            return 2
        raise
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dtm-agent", description="DTM 特化 AI エージェント")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("index", help="ローカルサンプルをインデックス化")
    s.add_argument("folder")
    s.add_argument("--max-seconds", type=float, default=30.0)
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("analyze", help="音声区間を解析")
    s.add_argument("audio")
    s.add_argument("--segment", default=None, help="例: 1:05-1:21")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("match", help="参照区間に近いローカルサンプルを探す")
    s.add_argument("audio")
    s.add_argument("--segment", default=None)
    s.add_argument("--library", required=True)
    s.add_argument("--query", default=None)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("run", help="Claude エージェントで制作")
    s.add_argument("prompt")
    s.add_argument("--url", default=None, help="Spotify トラック URL")
    s.add_argument("--audio", default=None, help="参照音声ファイル")
    s.add_argument("--segment", default=None)
    s.add_argument("--library", default=None)
    s.add_argument("--daw", default="file", choices=["file", "reaper", "ableton"])
    s.add_argument("--out", default="./dtm_agent_out")
    s.add_argument("--model", default=None)
    s.add_argument("--ableton-host", default="127.0.0.1")
    s.add_argument("--ableton-port", type=int, default=11000)
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "model", None) is None and args.command == "run":
        from .agent.runner import DEFAULT_MODEL

        args.model = DEFAULT_MODEL
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
