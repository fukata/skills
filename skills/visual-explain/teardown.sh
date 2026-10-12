#!/usr/bin/env bash
# setup.sh が登録した受け箱の hook を、Claude Code の settings.json と Codex の hooks.json から外す。
#   usage: bash <skill>/teardown.sh [--claude] [--codex]   （どちらも無ければ、設定フォルダがあるものすべて）
set -euo pipefail
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
want_claude=0; want_codex=0
for a in "$@"; do
  case "$a" in
    --claude) want_claude=1 ;;
    --codex) want_codex=1 ;;
    *) echo "不明な引数: $a（使い方: bash $SKILL/teardown.sh [--claude] [--codex]）" >&2; exit 2 ;;
  esac
done
if [ "$want_claude" = 0 ] && [ "$want_codex" = 0 ]; then
  if [ -d "$CLAUDE_DIR" ]; then want_claude=1; fi
  if [ -d "$CODEX_DIR" ]; then want_codex=1; fi
fi
command -v python3 >/dev/null || { echo "python3 が見つからない" >&2; exit 1; }
if [ "$want_claude" = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" remove --agent claude --file "$CLAUDE_DIR/settings.json" --skill "$SKILL"
  echo "Claude Code: $CLAUDE_DIR/settings.json から visual-explain の hook を外した"
fi
if [ "$want_codex" = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" remove --agent codex --file "$CODEX_DIR/hooks.json" --skill "$SKILL"
  echo "Codex: $CODEX_DIR/hooks.json から visual-explain の hook を外した"
fi
echo "受け箱の配信サーバーも止めるなら: python3 $SKILL/scripts/serve.py ask-config --stop"
