#!/usr/bin/env bash
# visual-explain の受け箱の hook を、Claude Code の settings.json と Codex の hooks.json に登録し、
# OpenCode の設定フォルダの plugins/ に受け箱の plugin を登録する。
#   usage: bash <skill>/setup.sh [--claude] [--codex] [--opencode]   （どれも無ければ、設定があるものすべて）
set -euo pipefail
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 呼び出されたパスをそのまま登録する
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
OPENCODE_DIR="${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}"
want_claude=0; want_codex=0; want_opencode=0
for a in "$@"; do
  case "$a" in
    --claude) want_claude=1 ;;
    --codex) want_codex=1 ;;
    --opencode) want_opencode=1 ;;
    *) echo "不明な引数: $a（使い方: bash $SKILL/setup.sh [--claude] [--codex] [--opencode]）" >&2; exit 2 ;;
  esac
done
if [ "$want_claude" = 0 ] && [ "$want_codex" = 0 ] && [ "$want_opencode" = 0 ]; then
  if [ -d "$CLAUDE_DIR" ]; then want_claude=1; fi
  if [ -d "$CODEX_DIR" ]; then want_codex=1; fi
  if [ -d "$OPENCODE_DIR" ] || command -v opencode >/dev/null; then want_opencode=1; fi
fi
command -v python3 >/dev/null || { echo "python3 が見つからない" >&2; exit 1; }
if [ "$want_claude" = 0 ] && [ "$want_codex" = 0 ] && [ "$want_opencode" = 0 ]; then
  echo "Claude Code（$CLAUDE_DIR）も Codex（$CODEX_DIR）も OpenCode（$OPENCODE_DIR）も見つからない" >&2
  exit 1
fi
# plugin 版の visual-explain と一緒に入れると、受け箱の hook が二重に動き、配信サーバーがポートを取り合う
if [ -f "$CLAUDE_DIR/settings.json" ] && plugin="$(python3 -I -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
names = [k for k, v in (d.get("enabledPlugins") or {}).items() if v and k.split("@")[0] == "visual-explain"]
print(names[0] if names else "")
sys.exit(0 if names else 1)' "$CLAUDE_DIR/settings.json")"; then
  echo "plugin 版の visual-explain（$plugin）が有効になっている。受け箱の hook が二重に動くので、先に外す:" >&2
  echo "  /plugin uninstall $plugin" >&2
  exit 1
fi
if [ "$want_claude" = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" add --agent claude --file "$CLAUDE_DIR/settings.json" --skill "$SKILL"
  echo "Claude Code: $CLAUDE_DIR/settings.json に SessionStart と PreToolUse（AskUserQuestion）の hook を登録した"
fi
if [ "$want_codex" = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" add --agent codex --file "$CODEX_DIR/hooks.json" --skill "$SKILL"
  echo "Codex: $CODEX_DIR/hooks.json に SessionStart と PreToolUse（request_user_input）の hook を登録した"
  echo "  Codex は信頼していない hook を動かさない。Codex を開いて /hooks で visual-explain の 2 つの hook を信頼する"
  echo "  （Codex の質問のツールは、既定では Plan モードでだけ使われる）"
fi
if [ "$want_opencode" = 1 ]; then
  python3 -I "$SKILL/scripts/opencode_plugin.py" add --dir "$OPENCODE_DIR" --skill "$SKILL"
  echo "OpenCode: $OPENCODE_DIR/plugins/visual-explain.js に受け箱の plugin を登録した"
  echo "  OpenCode を起動し直すと効く（質問の question ツールに、資料のページで答えられるようになる）"
fi
echo "受け箱の URL: python3 $SKILL/scripts/serve.py ask-config"
