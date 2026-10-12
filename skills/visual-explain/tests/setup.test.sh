#!/usr/bin/env bash
# setup.sh / teardown.sh のテスト。一時的な HOME で、Claude の settings.json と Codex の hooks.json への登録を確かめる。
#   usage: bash skills/visual-explain/tests/setup.test.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$(cd "$HERE/.." && pwd)"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
fail=0
ok() { printf '  ok   %s\n' "$1"; }
ng() { printf '  FAIL %s\n' "$1"; fail=1; }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else ng "$1"; fi; }
fresh() {
  T="$(mktemp -d "$ROOT/home.XXXX")"; mkdir -p "$T/.claude" "$T/.codex"
  printf '{"permissions":{"allow":["Bash(ls:*)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}\n' > "$T/.claude/settings.json"
  printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo other"}]}]}}\n' > "$T/.codex/hooks.json"
}
run() { env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME="$T" bash "$SKILL/$1" "${@:2}"; }
# count <JSON> <含む文字列> <イベント>: そのイベントで、コマンドに文字列を含む hook の数
count() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(sys.argv[2] in h["command"] for g in d.get("hooks",{}).get(sys.argv[3],[]) for h in g["hooks"]))' "$@"; }

echo "== setup.sh"
fresh
out="$(run setup.sh 2>&1)"
check "Claude の SessionStart と PreToolUse を足す" "[ \$(count '$T/.claude/settings.json' ask_hook.py SessionStart) = 1 ] && [ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ]"
check "Claude の matcher は AskUserQuestion" "python3 -c 'import json; d=json.load(open(\"$T/.claude/settings.json\")); assert any(g.get(\"matcher\")==\"AskUserQuestion\" and \"--agent claude\" in g[\"hooks\"][0][\"command\"] and g[\"hooks\"][0][\"timeout\"]==7200 for g in d[\"hooks\"][\"PreToolUse\"])'"
check "Codex の matcher は request_user_input" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert any(g.get(\"matcher\")==\"request_user_input\" and \"--agent codex\" in g[\"hooks\"][0][\"command\"] and g[\"hooks\"][0][\"statusMessage\"] for g in d[\"hooks\"][\"PreToolUse\"])'"
check "Codex の SessionStart は --agent codex" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert any(\"--agent codex --session-start\" in h[\"command\"] for g in d[\"hooks\"][\"SessionStart\"] for h in g[\"hooks\"])'"
check "既存の設定を残す" "grep -q 'echo keep' '$T/.claude/settings.json' && grep -q 'Bash(ls:\*)' '$T/.claude/settings.json' && grep -q 'echo other' '$T/.codex/hooks.json'"
check "呼び出されたパスをそのまま書く" "grep -q '$SKILL/scripts/ask_hook.py' '$T/.codex/hooks.json'"
check "Codex の /hooks での信頼を案内する" "grep -q '/hooks' <<<\"\$out\""
run setup.sh >/dev/null 2>&1
check "2 回実行しても重複しない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 1 ]"

echo "== 対象の絞り込みと検出"
fresh; rm -rf "$T/.codex"
run setup.sh >/dev/null 2>&1
check "~/.codex が無ければ Codex は登録しない" "[ ! -e '$T/.codex/hooks.json' ]"
fresh
run setup.sh --codex >/dev/null 2>&1
check "--codex なら Claude には足さない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 1 ]"
fresh
printf '{"enabledPlugins":{"visual-explain@zaicodev":true}}\n' > "$T/.claude/settings.json"
out="$(run setup.sh 2>&1)"; code=$?
check "zaicodev 版が有効なら止める" "[ $code -ne 0 ] && grep -q 'visual-explain@zaicodev' <<<\"\$out\" && ! grep -q ask_hook.py '$T/.claude/settings.json' && ! grep -q ask_hook.py '$T/.codex/hooks.json'"
fresh
out="$(run setup.sh --bogus 2>&1)"; code=$?
check "知らない引数は使い方の誤り" "[ $code -eq 2 ]"

echo "== teardown.sh"
fresh
run setup.sh >/dev/null 2>&1
run teardown.sh >/dev/null 2>&1
check "足した項目だけを外す" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py SessionStart) = 0 ] && grep -q 'echo keep' '$T/.claude/settings.json' && grep -q 'echo other' '$T/.codex/hooks.json'"
check "空になったイベントは消す" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert \"PreToolUse\" not in d[\"hooks\"]'"

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
