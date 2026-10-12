#!/usr/bin/env bash
# setup.sh が登録した hook を Claude Code の settings.json から取り除く。何度実行しても同じ結果になる。
# スキルを削除する前に実行する（削除だけだと、無くなったスクリプトを指す登録が残る）。
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が必要です" >&2
  exit 1
fi
if [ ! -f "$settings" ]; then
  echo "登録はありません（$settings がありません）"
  exit 0
fi

result="$(python3 -I "$dir/lib/settings.py" remove --file "$settings")"

if [ "$result" = changed ]; then
  echo "登録を取り除きました: $settings"
else
  echo "登録はありません"
fi
