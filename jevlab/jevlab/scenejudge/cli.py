"""`jevlab scenejudge` コマンド。

    jevlab scenejudge image desk.jpg --scenario desk --overlay out.png
    jevlab scenejudge video clip.mp4 --scenario pet --fps 1 --report report.json
    jevlab scenejudge scenarios
    jevlab scenejudge demo               # API なしで合成画像 + MockSam で一通り動かす
    jevlab scenejudge watch ./frames --scenario parking   # ディレクトリに追加される画像を順次判定 (カメラ連携)

環境変数:
    META_API_KEY (or MODEL_API_KEY) : SAM 3.1 (Meta Model API)。無ければ SAM_BACKEND=mock
    TYPESAFE_API_KEY / OPENROUTER_API_KEY : Jev。無ければ JEV_BACKEND=mock
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from jevlab.core import Jev
from jevlab.sam import MockSam, Sam
from jevlab.scenejudge.judge import SceneJudge, extract_frames, render_overlay, write_report
from jevlab.scenejudge.scenarios import BUILTIN, get_scenario


def _build_judge(args: argparse.Namespace, mock_objects: dict | None = None) -> SceneJudge:
    sam = Sam(MockSam(mock_objects)) if (args.sam_backend == "mock" or mock_objects is not None) else Sam(None if args.sam_backend is None else _sam_backend(args.sam_backend))
    jev = Jev(args.backend, cache=True)
    return SceneJudge(sam=sam, jev=jev)


def _sam_backend(name: str):
    from jevlab.sam.client import backend_from_env

    return backend_from_env(name)


def cmd_image(args: argparse.Namespace) -> int:
    judge = _build_judge(args)
    scenario = get_scenario(args.scenario)
    verdict = judge.judge_image(args.image, scenario)
    print(verdict.summary())
    if args.json:
        print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
    if args.overlay:
        print(f"overlay -> {render_overlay(args.image, verdict, args.overlay)}")
    if args.report:
        write_report([verdict], [], args.report)
    return 0


def cmd_video(args: argparse.Namespace) -> int:
    judge = _build_judge(args)
    scenario = get_scenario(args.scenario)
    source = Path(args.video)
    if source.is_dir():
        frames = sorted(p for p in source.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    else:
        frames = extract_frames(source, fps=args.fps)
    verdicts, events = judge.judge_video(frames, scenario, every=args.every)
    for verdict in verdicts:
        print(verdict.summary())
    for event in events:
        print(f"event: frame {event.frame}: {event.label} {event.kind} {event.zone}")
    if args.report:
        print(f"report -> {write_report(verdicts, events, args.report)}")
    if args.overlay_dir:
        out_dir = Path(args.overlay_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for frame_path, verdict in zip(frames[:: args.every], verdicts):
            render_overlay(frame_path, verdict, out_dir / f"{Path(frame_path).stem}_judged.png")
        print(f"overlays -> {out_dir}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """ディレクトリを監視し、新しい画像が来るたびに判定する (カメラのスナップショット連携用)。"""
    judge = _build_judge(args)
    scenario = get_scenario(args.scenario)
    seen: set[Path] = set()
    folder = Path(args.folder)
    print(f"watching {folder} (Ctrl-C で終了)")
    try:
        while True:
            for path in sorted(folder.glob("*")):
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png"} or path in seen:
                    continue
                seen.add(path)
                verdict = judge.judge_image(path, scenario)
                print(verdict.summary())
                for hit in verdict.triggered:
                    _notify(args.notify, hit["message"], verdict)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _notify(target: str | None, message: str, verdict) -> None:
    if not target:
        return
    if target.startswith("http"):
        import urllib.request

        data = json.dumps({"text": message, "verdict": verdict.to_dict(include_objects=False)}).encode("utf-8")
        request = urllib.request.Request(target, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=10).read()
    else:
        Path(target).open("a", encoding="utf-8").write(json.dumps({"ts": time.time(), "message": message, "scenario": verdict.scenario}, ensure_ascii=False) + "\n")


def cmd_scenarios(args: argparse.Namespace) -> int:
    for name, scenario in BUILTIN.items():
        print(f"{name:10s} {scenario.description}")
        print(f"{'':10s} prompts: {', '.join(scenario.prompts)}")
        print(f"{'':10s} questions: {', '.join(scenario.questions)}")
    if args.export:
        Path(args.export).write_text(json.dumps(BUILTIN[args.name].to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"exported {args.name} -> {args.export}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """API 不要のデモ。合成画像を作って MockSam で「机」を判定し、オーバーレイも書く。"""
    from jevlab.scenejudge.demo import run_demo

    return run_demo(Path(args.out_dir), backend=args.backend)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevlab scenejudge", description="SAM 3.1 x Jev: 画像/動画から型付きの判断を作る")
    parser.add_argument("--backend", default=None, help="Jev バックエンド (typesafe|openrouter|mock|local)")
    parser.add_argument("--sam-backend", default=None, help="SAM バックエンド (meta|mock)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("image", help="1 枚の画像を判定")
    p.add_argument("image")
    p.add_argument("--scenario", "-s", default="desk", help="内蔵シナリオ名か JSON/YAML のパス")
    p.add_argument("--overlay", help="判定結果を描いた PNG の出力先")
    p.add_argument("--report", help="JSON レポートの出力先")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("video", help="動画 (または画像ディレクトリ) を判定してイベントを出す")
    p.add_argument("video")
    p.add_argument("--scenario", "-s", default="pet")
    p.add_argument("--fps", type=float, default=1.0)
    p.add_argument("--every", type=int, default=1)
    p.add_argument("--report")
    p.add_argument("--overlay-dir")
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("watch", help="フォルダを監視して新しい画像を判定 (通知は --notify URL|file)")
    p.add_argument("folder")
    p.add_argument("--scenario", "-s", default="pet")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--notify", help="Webhook URL か追記する JSONL ファイル")
    p.add_argument("--once", action="store_true", help="1 回走査して終了 (テスト用)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("scenarios", help="内蔵シナリオ一覧 / エクスポート")
    p.add_argument("--export", help="JSON 出力先")
    p.add_argument("--name", default="desk")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("demo", help="API 無しのデモ")
    p.add_argument("--out-dir", default="./scenejudge_demo")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
