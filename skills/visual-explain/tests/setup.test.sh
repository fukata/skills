#!/usr/bin/env bash
# setup.sh / teardown.sh のテスト。一時的な HOME で、Claude の settings.json・Codex の hooks.json・
# OpenCode の plugins/visual-explain.js への登録を確かめる。
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
  T="$(mktemp -d "$ROOT/home.XXXX")"; mkdir -p "$T/.claude" "$T/.codex" "$T/.config/opencode/plugins"
  printf '{"permissions":{"allow":["Bash(ls:*)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}\n' > "$T/.claude/settings.json"
  printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo other"}]}]}}\n' > "$T/.codex/hooks.json"
}
run() { env -u CLAUDE_CONFIG_DIR -u CODEX_HOME -u OPENCODE_CONFIG_DIR -u XDG_CONFIG_HOME HOME="$T" bash "$SKILL/$1" "${@:2}"; }
# count <JSON> <含む文字列> <イベント>: そのイベントで、コマンドに文字列を含む hook の数
count() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(sys.argv[2] in h["command"] for g in d.get("hooks",{}).get(sys.argv[3],[]) for h in g["hooks"]))' "$@"; }
# OpenCode の設定フォルダに置く登録ファイル
plug() { echo "$T/.config/opencode/plugins/visual-explain.js"; }

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
check "OpenCode の plugins に登録ファイルを 1 つ置く" "[ \$(grep -c 'export { default } from' \"\$(plug)\") = 1 ] && grep -q \"$SKILL/opencode/plugin.js\" \"\$(plug)\""
check "登録ファイルはスキルの plugin を読み込むだけ" "[ \$(wc -l < \"\$(plug)\") -le 3 ]"
check "OpenCode を起動し直す案内をする" "grep -q '起動し直すと効く' <<<\"\$out\""

echo "== OpenCode の plugin"
fresh
mkdir -p "$T/.config/opencode/plugins"
printf '// ほかのツールの plugin\n' > "$T/.config/opencode/plugins/orca.js"
run setup.sh >/dev/null 2>&1
check "ほかの plugin は残す" "grep -q 'ほかのツールの plugin' '$T/.config/opencode/plugins/orca.js'"
run setup.sh >/dev/null 2>&1
check "2 回実行しても 1 つだけ（置き直す）" "[ \$(ls '$T/.config/opencode/plugins' | wc -l) = 2 ] && [ \$(grep -c 'export { default } from' \"\$(plug)\") = 1 ]"
fresh
run setup.sh --opencode >/dev/null 2>&1
check "--opencode なら Claude と Codex には足さない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 0 ] && [ -e \"\$(plug)\" ]"
fresh
run setup.sh --codex >/dev/null 2>&1
check "--codex なら OpenCode には置かない" "[ ! -e \"\$(plug)\" ]"
fresh
ln -s "$SKILL" "$ROOT/oc-link-$$"
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME -u OPENCODE_CONFIG_DIR -u XDG_CONFIG_HOME HOME="$T" \
  bash "$ROOT/oc-link-$$/setup.sh" --opencode >/dev/null 2>&1
check "呼び出されたパスをそのまま書く（リンクでも解決しない）" "grep -q '$ROOT/oc-link-$$/opencode/plugin.js' \"\$(plug)\""
fresh
mkdir -p "$T/.config/opencode/plugins"
printf '// 同じ名前の別の plugin\n' > "$(plug)"
out="$(run setup.sh --opencode 2>&1)"; code=$?
check "同じ名前の別のファイルは書き換えずに止める" "[ $code -ne 0 ] && grep -q '別のファイル' <<<\"\$out\" && grep -q '同じ名前の別の plugin' \"\$(plug)\""
fresh
rm -rf "$T/.config/opencode"
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME -u OPENCODE_CONFIG_DIR -u XDG_CONFIG_HOME HOME="$T" PATH="/usr/bin:/bin" \
  bash "$SKILL/setup.sh" >/dev/null 2>&1
check "OpenCode の設定フォルダも opencode も無ければ登録しない" "[ ! -e \"\$(plug)\" ]"

echo "== 再実行・別のパス・リンク"
fresh
run setup.sh >/dev/null 2>&1
# 後ろにほかのツールの hook がある（Codex の信頼はグループの番号で記録されるので、位置を変えない）
python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["hooks"]["PreToolUse"].append({"hooks":[{"type":"command","command":"echo later"}]}); json.dump(d,open(p,"w"))' "$T/.codex/hooks.json"
run setup.sh >/dev/null 2>&1
check "再実行しても同じ位置で置き換える" "python3 -c 'import json; g=json.load(open(\"$T/.codex/hooks.json\"))[\"hooks\"][\"PreToolUse\"]; assert len(g)==2 and \"ask_hook.py\" in g[0][\"hooks\"][0][\"command\"] and g[1][\"hooks\"][0][\"command\"]==\"echo later\"'"
ln -s "$SKILL" "$ROOT/link-$$"
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME="$T" bash "$ROOT/link-$$/setup.sh" >/dev/null 2>&1
check "別のパス（リンク）から実行しても二重にしない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 1 ]"
fresh
mv "$T/.claude/settings.json" "$T/dotfiles-settings.json"; ln -s "$T/dotfiles-settings.json" "$T/.claude/settings.json"; chmod 600 "$T/dotfiles-settings.json"
run setup.sh >/dev/null 2>&1
check "リンクの settings.json はリンクのまま、リンク先を書き換える" "[ -L '$T/.claude/settings.json' ] && grep -q ask_hook.py '$T/dotfiles-settings.json'"
check "ファイルの権限を保つ" "[ \$(stat -c %a '$T/dotfiles-settings.json') = 600 ]"
fresh
printf '{ broken json\n' > "$T/.codex/hooks.json"
out="$(run setup.sh 2>&1)"; code=$?
check "壊れた JSON は書き換えず、分かる文で止める" "[ $code -ne 0 ] && grep -q 'JSON' <<<\"\$out\" && ! grep -q Traceback <<<\"\$out\" && grep -q 'broken json' '$T/.codex/hooks.json'"

echo "== 対象の絞り込みと検出"
fresh; rm -rf "$T/.codex"
run setup.sh >/dev/null 2>&1
check "~/.codex が無ければ Codex は登録しない" "[ ! -e '$T/.codex/hooks.json' ]"
fresh
run setup.sh --codex >/dev/null 2>&1
check "--codex なら Claude には足さない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 1 ]"
fresh
printf '{"enabledPlugins":{"visual-explain@some-market":true}}\n' > "$T/.claude/settings.json"
out="$(run setup.sh 2>&1)"; code=$?
check "plugin 版の visual-explain が有効なら止める" "[ $code -ne 0 ] && grep -q '/plugin uninstall visual-explain@some-market' <<<\"\$out\" && ! grep -q ask_hook.py '$T/.claude/settings.json' && ! grep -q ask_hook.py '$T/.codex/hooks.json'"
fresh
printf '{"enabledPlugins":{"visual-explain@some-market":false,"other@x":true}}\n' > "$T/.claude/settings.json"
run setup.sh >/dev/null 2>&1
check "無効の plugin 版・別の plugin では止めない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ]"
fresh
out="$(run setup.sh --bogus 2>&1)"; code=$?
check "知らない引数は使い方の誤り" "[ $code -eq 2 ]"

echo "== teardown.sh"
fresh
run setup.sh >/dev/null 2>&1
run teardown.sh >/dev/null 2>&1
check "足した項目だけを外す" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py SessionStart) = 0 ] && grep -q 'echo keep' '$T/.claude/settings.json' && grep -q 'echo other' '$T/.codex/hooks.json'"
check "空になったイベントは消す" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert \"PreToolUse\" not in d[\"hooks\"]'"
check "OpenCode の登録ファイルを外す" "[ ! -e '$T/.config/opencode/plugins/visual-explain.js' ]"
fresh
run setup.sh --opencode >/dev/null 2>&1
mkdir -p "$T/.config/opencode/plugins"
printf '// 別のツールの plugin\n' > "$T/.config/opencode/plugins/other.js"
run teardown.sh --opencode >/dev/null 2>&1
check "OpenCode は自分の登録だけを外し、ほかを残す" "[ ! -e '$T/.config/opencode/plugins/visual-explain.js' ] && grep -q '別のツールの plugin' '$T/.config/opencode/plugins/other.js'"
fresh
mkdir -p "$T/.config/opencode/plugins"
printf '// 自分が置いたものではない\n' > "$T/.config/opencode/plugins/visual-explain.js"
run teardown.sh --opencode >/dev/null 2>&1
check "自分が置いていない同名ファイルは外さない" "grep -q '自分が置いたものではない' '$T/.config/opencode/plugins/visual-explain.js'"

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
