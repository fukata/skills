#!/usr/bin/env bash
# setup.sh / teardown.sh が settings.json を正しく書き換えることを確かめる。
# 実際の設定には触れない（CLAUDE_CONFIG_DIR を一時ディレクトリへ向ける）。
#   bash tests/settings.test.sh
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export CLAUDE_CONFIG_DIR="$work/config"
mkdir -p "$CLAUDE_CONFIG_DIR"
settings="$CLAUDE_CONFIG_DIR/settings.json"
expected_command="bash '$skill_dir/hook.sh'"
failures=0

check() {
  local name="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    echo "ok   $name"
  else
    echo "FAIL $name"
    echo "       expected: $expected"
    echo "       actual:   $actual"
    failures=$((failures + 1))
  fi
}
# query <Python の式> [<JSON>]: JSON を d に読み、式の値を詰めた JSON で出す
query() {
  python3 -c 'import json, sys
d = json.load(open(sys.argv[2], encoding="utf-8"))
print(json.dumps(eval(sys.argv[1]), ensure_ascii=False, separators=(",", ":")))' "$1" "${2:-$settings}"
}

# 1. settings.json が無い状態から登録できる
bash "$skill_dir/setup.sh" >/dev/null
check 'ファイルが無くても登録できる' "$(query 'd["hooks"]["StopFailure"]')" \
  "[{\"matcher\":\"rate_limit\",\"hooks\":[{\"type\":\"command\",\"command\":\"$expected_command\",\"timeout\":10}]}]"

# 2. 他の hook と他の設定を残し、古い場所を指す登録だけを付け替える
cat >"$settings" <<'JSON'
{
  "model": "opus",
  "hooks": {
    "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo start"}]}],
    "StopFailure": [
      {"matcher": "rate_limit", "hooks": [{"type": "command", "command": "bash '/old/place/orca-account-failover/hook.sh'", "timeout": 10}]},
      {"hooks": [{"type": "command", "command": "echo other", "timeout": 10}]},
      {"matcher": "rate_limit", "hooks": [
        {"type": "command", "command": "echo mixed"},
        {"type": "command", "command": "bash '/another/orca-account-failover/hook.sh'"}
      ]}
    ]
  },
  "language": "japanese"
}
JSON
bash "$skill_dir/setup.sh" >/dev/null
check '登録は先頭に 1 件だけ' "$(query '[h["command"] for g in d["hooks"]["StopFailure"] for h in g["hooks"]]')" \
  "[\"$expected_command\",\"echo other\",\"echo mixed\"]"
check '他の hook と設定は変えない' "$(query '[d["model"], d["language"], d["hooks"]["SessionStart"][0]["hooks"][0]["command"], ",".join(d)]')" \
  '["opus","japanese","echo start","model,hooks,language"]'
check '変更前の内容を残す' "$(query 'len(d["hooks"]["StopFailure"])' "$settings.orca-account-failover.bak")" '3'

# 3. もう一度実行しても変わらない
cp "$settings" "$work/after-setup.json"
second="$(bash "$skill_dir/setup.sh")"
check '2 回目は書き換えない' "$(cmp -s "$settings" "$work/after-setup.json" && echo same || echo changed)" 'same'
check '2 回目は登録済みと表示する' "${second%%:*}" '登録済みです'

# 4. 解除すると、この hook の登録だけが消える
bash "$skill_dir/teardown.sh" >/dev/null
check '解除で自分の登録だけ消える' "$(query '[h["command"] for g in d["hooks"]["StopFailure"] for h in g["hooks"]]')" '["echo other","echo mixed"]'
check '解除後も他の設定は変えない' "$(query '[d["model"], d["language"], d["hooks"]["SessionStart"][0]["hooks"][0]["command"]]')" '["opus","japanese","echo start"]'
cp "$settings" "$work/after-teardown.json"
bash "$skill_dir/teardown.sh" >/dev/null
check '2 回目の解除は書き換えない' "$(cmp -s "$settings" "$work/after-teardown.json" && echo same || echo changed)" 'same'

# 5. 自分の登録しか無ければ StopFailure ごと消す。hooks が無い設定は触らない
printf '{"hooks":{"Stop":[]}}\n' >"$settings"
bash "$skill_dir/setup.sh" >/dev/null
bash "$skill_dir/teardown.sh" >/dev/null
check '空になった StopFailure は残さない' "$(query 'd')" '{"hooks":{"Stop":[]}}'
printf '{"model":"opus"}\n' >"$settings"
bash "$skill_dir/teardown.sh" >/dev/null
check 'hooks が無い設定は解除で変わらない' "$(query 'd')" '{"model":"opus"}'

# 6. シンボリックリンク経由で呼ぶと、リンク側のパスを登録する（実体のパスへ解決しない）
mkdir "$work/linked"
ln -s "$skill_dir" "$work/linked/orca-account-failover"
printf '{}\n' >"$settings"
bash "$work/linked/orca-account-failover/setup.sh" >/dev/null
check 'リンク経由ならリンク側のパスを登録する' "$(query 'd["hooks"]["StopFailure"][0]["hooks"][0]["command"]')" \
  "\"bash '$work/linked/orca-account-failover/hook.sh'\""
bash "$skill_dir/setup.sh" >/dev/null
check '呼び出すパスを変えると付け替わる' "$(query '[h["command"] for g in d["hooks"]["StopFailure"] for h in g["hooks"]]')" "[\"$expected_command\"]"

# 別名のディレクトリからは登録しない（解除のときに自分の登録を見分けられなくなる）
ln -s "$skill_dir" "$work/renamed-skill"
cp "$settings" "$work/before-renamed.json"
if bash "$work/renamed-skill/setup.sh" >/dev/null 2>&1; then status=succeeded; else status=failed; fi
check '別名のディレクトリからの登録は失敗する' "$status" 'failed'
check '別名のディレクトリからは書き換えない' "$(cmp -s "$settings" "$work/before-renamed.json" && echo same || echo changed)" 'same'

# 7. 壊れた settings.json は書き換えずに失敗する
printf '{ broken\n' >"$settings"
if bash "$skill_dir/setup.sh" >/dev/null 2>&1; then status=succeeded; else status=failed; fi
check '壊れた設定では失敗する' "$status" 'failed'
check '壊れた設定は書き換えない' "$(cat "$settings")" '{ broken'
check '一時ファイルを残さない' "$(find "$CLAUDE_CONFIG_DIR" -name 'settings.json.*' ! -name '*.bak' | wc -l)" '0'

# 8. settings.json がシンボリックリンクなら、リンクを保ってリンク先を書き換える。権限も保つ
mkdir "$work/dotfiles"
printf '{"model":"opus"}\n' >"$work/dotfiles/settings.json"
chmod 600 "$work/dotfiles/settings.json"
rm -f "$settings"
ln -s "$work/dotfiles/settings.json" "$settings"
bash "$skill_dir/setup.sh" >/dev/null
check 'リンクのままにする' "$([ -L "$settings" ] && echo link || echo file)" 'link'
check 'リンク先に登録する' "$(query 'len(d["hooks"]["StopFailure"])' "$work/dotfiles/settings.json")" '1'
check '権限を保つ' "$(stat -c %a "$work/dotfiles/settings.json")" '600'

echo
if [ "$failures" -eq 0 ]; then
  echo "all passed"
else
  echo "$failures failed"
  exit 1
fi
