# 12. jev-shell-history — 入力途中に合う過去コマンドを Jev が選ぶ zsh 補完

## 目的

zsh で打ちかけの文字列 (`$BUFFER`) と現在ディレクトリを手がかりに、履歴から「今実行したいコマンド」を Jev が 1 つ選び、
バッファを置き換える。Python 側は履歴の安価な前絞り込み (前方一致 / 単語一致 / 部分列 + 新しさ + 頻度) を行い、
上位 20 件だけを Choice (criteria = {index: command}) として Jev に渡す。

## 元ネタ

- GitHub: `mrnugget/jev-shell-history`

## 使い方

```bash
python -m jevlab.apps.shell_history --install            # → source /.../jevlab/apps/shell_history.zsh
echo 'source /.../jevlab/apps/shell_history.zsh' >> ~/.zshrc

# 手動確認
python -m jevlab.apps.shell_history pick --typed "git pu" --cwd "$PWD" --history ~/.zsh_history --json
python -m jevlab.apps.shell_history pick --typed "git pu" --plain        # ウィジェットが使う形式 (最良の 1 行だけ)
python -m jevlab.apps.shell_history candidates --typed "git"             # 前絞り込みだけ (Jev を呼ばない)
```

zsh ウィジェット (`shell_history.zsh`) は既定で ctrl-j (`^J`) に `jev-history-pick` を割り当てる。
環境変数: `JEV_HISTORY_KEY` (キー)、`JEV_HISTORY_PYTHON`、`JEV_HISTORY_TIMEOUT` (既定 1.5 秒)、`JEV_BACKEND`。

## 応答性の工夫

- `PickCache` (`~/.cache/jevlab/shell_history.json`): (typed, 候補列, cwd) が同じなら Jev を呼ばない
- `--timeout` (既定 1.5 秒) を超えたら前絞り込み 1 位を返し、Jev のスレッドを待たずに終了する
- confidence が `--threshold` (既定 0.35) 未満なら何もしない (バッファを壊さない)

## 必要な追加依存

なし。zsh 側は ZLE のみ。

## 課金・外部送信の注意

キー 1 回 = Jev 1 回。state には打ちかけの文字列、cwd、直近 5 コマンド、候補 20 件が含まれる。
履歴にトークンやパスワードを含むコマンドがあると外部 API に送られるので、`HISTORY_IGNORE` などで履歴から除外しておくこと。

## 制限

- zsh 拡張履歴 (`: ts:dur;cmd`) と素の形式のみ。fish / bash の形式は未対応 (`--history` に変換済みファイルを渡せば動く)。
- 前絞り込みは字面ベースなので、typed とまったく共通部分がないコマンドは候補に入らない。
