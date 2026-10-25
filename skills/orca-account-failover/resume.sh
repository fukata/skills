#!/usr/bin/env bash
# worker.js が Orca の端末へ打ち込むコマンド。worker.js が保存した引数（NUL 区切り）で claude を起動し直す。
# 引数をコマンド行に直接書かないのは、プロンプトや設定 JSON をシェルの引用に通さないため。
#   usage: resume.sh <args-file> <config-dir>   （<config-dir> が空なら System Default）
#
# Orca は端末を開いたときの設定フォルダを CLAUDE_CONFIG_DIR で渡し、端末のシェルの claude 関数（Orca の rcfile）が
# 起動のたびに選択中のフォルダへ差し替える。この bash には関数が引き継がれないので、ここで同じ差し替えをする。
# しないと、端末を開いた時点のアカウント（上限に達したもの）のまま起動する。
set -euo pipefail

args_file="${1:?usage: resume.sh <args-file> <config-dir>}"
config_dir="${2?usage: resume.sh <args-file> <config-dir>}"
mapfile -d '' -t args <"$args_file"

if [ -n "$config_dir" ]; then
  export CLAUDE_CONFIG_DIR="$config_dir" ORCA_CLAUDE_INJECTED_CONFIG_DIR="$config_dir"
elif [ -n "${ORCA_CLAUDE_USER_CONFIG_DIR:-}" ]; then
  # Orca が端末を開くときに置き換えた、利用者自身の設定フォルダへ戻す
  unset ORCA_CLAUDE_INJECTED_CONFIG_DIR
  export CLAUDE_CONFIG_DIR="$ORCA_CLAUDE_USER_CONFIG_DIR"
else
  unset CLAUDE_CONFIG_DIR ORCA_CLAUDE_INJECTED_CONFIG_DIR
fi
exec claude "${args[@]}"
