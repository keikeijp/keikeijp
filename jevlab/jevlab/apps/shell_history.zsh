# jev-shell-history: 入力途中の BUFFER に合う過去コマンドを Jev が選んで置き換える ZLE ウィジェット。
#
#   source /path/to/jevlab/apps/shell_history.zsh      # `python -m jevlab.apps.shell_history --install` が出力する 1 行
#
# 環境変数:
#   JEV_HISTORY_KEY      バインドするキー (既定 ^J = ctrl-j)
#   JEV_HISTORY_PYTHON   python コマンド (既定 python3)
#   JEV_HISTORY_TIMEOUT  Jev 待ちの上限秒 (既定 1.5)。超えたら前絞り込みの 1 位を使う
#   JEV_BACKEND          jevlab のバックエンド (mock で API なし動作)

jev-history-pick() {
  local py="${JEV_HISTORY_PYTHON:-python3}"
  local histfile="${HISTFILE:-$HOME/.zsh_history}"
  # 直近の履歴をファイルに書き出してから読む (INC_APPEND_HISTORY / SHARE_HISTORY なら既に書かれている)
  fc -W 2>/dev/null
  local picked
  picked="$("$py" -m jevlab.apps.shell_history pick \
      --typed "$BUFFER" --cwd "$PWD" --history "$histfile" \
      --timeout "${JEV_HISTORY_TIMEOUT:-1.5}" --plain 2>/dev/null)"
  if [[ -n "$picked" ]]; then
    BUFFER="$picked"
    CURSOR=${#BUFFER}
  else
    zle -M "jev: 候補なし"
  fi
  zle redisplay
}

zle -N jev-history-pick
bindkey "${JEV_HISTORY_KEY:-^J}" jev-history-pick
