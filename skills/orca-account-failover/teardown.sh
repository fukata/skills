#!/usr/bin/env bash
# setup.sh が登録した hook を Claude Code の settings.json から取り除く。何度実行しても同じ結果になる。
# スキルを削除する前に実行する（削除だけだと、無くなったスクリプトを指す登録が残る）。
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/settings.sh
source "$dir/lib/settings.sh"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq が必要です" >&2
  exit 1
fi
if [ ! -f "$(settings_path)" ]; then
  echo "登録はありません（$(settings_path) がありません）"
  exit 0
fi

rewrite_settings '
  if (.hooks.StopFailure? // null) == null then .
  else
    .hooks.StopFailure |= without_failover
    | if (.hooks.StopFailure | length) == 0 then del(.hooks.StopFailure) else . end
  end
'

if [ "$settings_changed" = 1 ]; then
  echo "登録を取り除きました: $(settings_path)"
else
  echo "登録はありません"
fi
