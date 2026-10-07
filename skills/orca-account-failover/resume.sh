#!/usr/bin/env bash
# worker.js が Orca の端末へ打ち込むコマンド。worker.js が保存した引数（NUL 区切り）で claude を起動し直す。
# 引数をコマンド行に直接書かないのは、プロンプトや設定 JSON をシェルの引用に通さないため。
set -euo pipefail

args_file="${1:?usage: resume.sh <args-file>}"
mapfile -d '' -t args <"$args_file"
exec claude "${args[@]}"
