"""jev-usecases コマンドラインインターフェース。

  jev-usecases list                                  # ユースケース一覧
  jev-usecases run triage --demo --backend mock      # API なしでデモ
  jev-usecases run guard --text "rm -rf build"       # 1 件だけ判定
  jev-usecases run feed --input posts.jsonl --json   # JSONL を流して JSONL で出す
  jev-usecases run review --dry-run --demo           # 送信するリクエスト本文だけ見る
  jev-usecases eval triage --dataset labeled.jsonl   # 正解ラベルと比較して速度・費用・正確さを出す
  jev-usecases models                                # 使えるモデル一覧 (要 API キー)
  jev-usecases cost --items 10000 --tokens 2000      # 費用の目安

TYPESAFE_API_KEY を設定すると本物の Jev (https://api.typesafe.ai) を呼ぶ。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import HttpBackend, JevClient, JevError
from .evaluate import estimate_batch_cost, evaluate, load_jsonl
from .usecases import USECASES, get_usecase


def _client(args: argparse.Namespace) -> JevClient:
    return JevClient.from_env(args.backend, model=args.model)


def _read_items(args: argparse.Namespace, usecase) -> list[Any]:
    if args.demo:
        return usecase.example_items()
    if args.text is not None:
        return [usecase.from_text(args.text)]
    if args.input == "-" or args.input is None:
        if sys.stdin.isatty():
            print("入力がありません。--demo, --text, --input <file.jsonl> のいずれかを指定してください。", file=sys.stderr)
            sys.exit(2)
        return [json.loads(l) for l in sys.stdin if l.strip()]
    return load_jsonl(args.input)


def cmd_list(_: argparse.Namespace) -> int:
    for name, cls in USECASES.items():
        print(f"{name:12} {cls.title:18} {cls.summary}  [{cls.article_ref}]")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    usecase = get_usecase(args.usecase)
    items = _read_items(args, usecase)
    if args.dry_run:
        for body in usecase.dry_run(items, model=args.model or "jev-latest"):
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    client = _client(args)
    outcomes = usecase.run(client, items, concurrency=args.concurrency)
    for o in outcomes:
        if args.json:
            print(json.dumps(o.to_dict(), ensure_ascii=False))
        else:
            print(usecase.format(o))
    if not args.json:
        print(f"-- {client.total_calls} calls, {client.total_input_tokens} input tokens, ~${client.total_cost_usd:.5f} (model {client.model})", file=sys.stderr)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    usecase = get_usecase(args.usecase)
    rows = load_jsonl(args.dataset)
    report = evaluate(usecase, _client(args), rows, concurrency=args.concurrency)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
        for label, preds in report.confusion.items():
            print(f"  {label:20} -> {dict(preds)}")
        for e in report.errors[: args.show_errors]:
            print(f"  miss: label={e['label']} predicted={e['predicted']} human={e['needs_human']}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    for m in HttpBackend().list_models():
        print(f"{m.get('name'):16} {m.get('release_date', ''):12} {m.get('description', '')}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    print(json.dumps(estimate_batch_cost(args.items, args.tokens, args.usd_per_mtoken), ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jev-usecases", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="ユースケース一覧").set_defaults(fn=cmd_list)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--backend", choices=["http", "mock"], default="http", help="http: 本物の API / mock: オフラインの代替")
        sp.add_argument("--model", default=None, help="jev-latest, jev-preview, jev-1.13.0 など")
        sp.add_argument("--concurrency", type=int, default=1)
        sp.add_argument("--json", action="store_true", help="JSONL で出力")

    r = sub.add_parser("run", help="ユースケースを実行")
    r.add_argument("usecase", choices=list(USECASES))
    r.add_argument("--input", help="JSONL ファイル (- で stdin)")
    r.add_argument("--text", help="1 件の入力を文字列で")
    r.add_argument("--demo", action="store_true", help="組み込みのデモ入力を使う")
    r.add_argument("--dry-run", action="store_true", help="API を呼ばずリクエスト本文だけ表示")
    common(r)
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("eval", help="正解ラベル付きデータで評価")
    e.add_argument("usecase", choices=list(USECASES))
    e.add_argument("--dataset", required=True, help='JSONL: {"input": ..., "label": ...}')
    e.add_argument("--show-errors", type=int, default=10)
    common(e)
    e.set_defaults(fn=cmd_eval)

    sub.add_parser("models", help="利用可能なモデル (要 API キー)").set_defaults(fn=cmd_models)

    c = sub.add_parser("cost", help="費用の目安")
    c.add_argument("--items", type=int, default=10_000)
    c.add_argument("--tokens", type=int, default=2_000)
    c.add_argument("--usd-per-mtoken", type=float, default=0.042)
    c.set_defaults(fn=cmd_cost)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except JevError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
