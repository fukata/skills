#!/usr/bin/env bash
# すべてのスキルのテストを順に実行する。スキルごとの入口は skills/<名前>/tests/run.sh。
#   usage: bash test.sh [<スキル名>...]   （無ければすべて）
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -gt 0 ]; then names=("$@"); else names=(); for d in "$ROOT"/skills/*/; do names+=("$(basename "$d")"); done; fi
failed=()
for name in "${names[@]}"; do
  echo "#### $name"
  run="$ROOT/skills/$name/tests/run.sh"
  if [ ! -f "$run" ]; then
    echo "FAIL skills/$name/tests/run.sh が無い"
    failed+=("$name")
  elif ! bash "$run"; then
    failed+=("$name")
  fi
done
echo
if [ "${#failed[@]}" -eq 0 ]; then
  echo "すべて通った: ${names[*]}"
else
  echo "失敗した: ${failed[*]}"
  exit 1
fi
