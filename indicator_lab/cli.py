"""コマンドライン: indicator-lab screen prices.csv / indicator-lab demo"""

from __future__ import annotations

import argparse

import pandas as pd

from .indicators import candidate_indicators, zscore
from .screen import screen
from .select import extra_trees_importance, lasso_select
from .evaluate import forward_returns


def _load(path: str, date_col: str, close_col: str) -> tuple[pd.Series, pd.DataFrame]:
    df = pd.read_csv(path, parse_dates=[date_col]).set_index(date_col).sort_index()
    close = df[close_col].astype(float)
    extra = df.drop(columns=[close_col]).select_dtypes("number")
    # OHLCV の列は指標ではないので除く。それ以外の数値列は自作の指標として評価に含める
    extra = extra.drop(columns=[c for c in extra.columns
                                if c.lower() in {"open", "high", "low", "volume", "adj close"}])
    return close, extra


def _report(close: pd.Series, indicators: pd.DataFrame, args) -> None:
    table = screen(close, indicators, horizon=args.horizon, z_window=args.z_window,
                   threshold=args.threshold, cost=args.cost)
    with pd.option_context("display.width", 200, "display.max_columns", 20,
                           "display.float_format", "{:.4f}".format):
        print(f"== 指標スクリーニング (horizon={args.horizon}, 試した指標 {indicators.shape[1]} 個) ==")
        print(table)
    if args.select:
        feats = indicators.apply(lambda c: zscore(c, window=args.z_window))
        fwd = forward_returns(close, args.horizon)
        print("\n== LASSO (係数 0 は不採用) ==")
        print(lasso_select(feats, fwd).round(6))
        print("\n== ExtraTrees 重要度 (max_depth=2) ==")
        print(extra_trees_importance(feats, fwd).round(4))
    print("\n※ candidate=True でも採用前に「なぜ効くのか」を説明できるか確認すること。")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="indicator-lab", description="投資指標の探索・評価")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--horizon", type=int, default=1, help="何本先までのリターンを予測するか")
        sp.add_argument("--z-window", type=int, default=250, help="Z スコアを計算する過去の本数")
        sp.add_argument("--threshold", type=float, default=1.0, help="|Z| がこれを超えたら建てる")
        sp.add_argument("--cost", type=float, default=0.0, help="片道の取引コスト (0.0005 = 5bp)")
        sp.add_argument("--select", action="store_true", help="LASSO / ExtraTrees も実行")

    s = sub.add_parser("screen", help="CSV の終値と自作指標を評価する")
    s.add_argument("csv")
    s.add_argument("--date-col", default="Date")
    s.add_argument("--close-col", default="Close")
    s.add_argument("--no-builtin", action="store_true", help="組み込みのテクニカル指標を使わない")
    common(s)

    d = sub.add_parser("demo", help="答えの分かっている合成データで動作確認")
    d.add_argument("--ic", type=float, default=0.1)
    d.add_argument("--n", type=int, default=3000)
    common(d)

    args = p.parse_args(argv)
    if args.cmd == "screen":
        close, extra = _load(args.csv, args.date_col, args.close_col)
        parts = [] if args.no_builtin else [candidate_indicators(close)]
        indicators = pd.concat(parts + [extra], axis=1)
        if indicators.empty:
            p.error("評価する指標がありません")
    else:
        from .synthetic import make_market

        close, indicators = make_market(n=args.n, ic=args.ic)
    _report(close, indicators, args)


if __name__ == "__main__":
    main()
