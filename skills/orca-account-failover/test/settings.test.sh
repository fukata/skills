#!/usr/bin/env bash
# setup.sh / teardown.sh が settings.json を正しく書き換えることを確かめる。
# 実際の設定には触れない（CLAUDE_CONFIG_DIR を一時ディレクトリへ向ける）。
#   bash test/settings.test.sh
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
work="$(mktemp -d)"
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
query() { jq -c "$1" "$settings"; }

# 1. settings.json が無い状態から登録できる
bash "$skill_dir/setup.sh" >/dev/null
check 'ファイルが無くても登録できる' "$(query '.hooks.StopFailure')" \
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
check '登録は先頭に 1 件だけ' "$(query '[.hooks.StopFailure[].hooks[].command]')" \
  "[\"$expected_command\",\"echo other\",\"echo mixed\"]"
check '他の hook と設定は変えない' "$(query '[.model, .language, .hooks.SessionStart[0].hooks[0].command, (keys_unsorted | join(","))]')" \
  '["opus","japanese","echo start","model,hooks,language"]'
check '変更前の内容を残す' "$(jq -c '.hooks.StopFailure | length' "$settings.orca-account-failover.bak")" '3'

# 3. もう一度実行しても変わらない
cp "$settings" "$work/after-setup.json"
second="$(bash "$skill_dir/setup.sh")"
check '2 回目は書き換えない' "$(cmp -s "$settings" "$work/after-setup.json" && echo same || echo changed)" 'same'
check '2 回目は登録済みと表示する' "${second%%:*}" '登録済みです'

# 4. 解除すると、この hook の登録だけが消える
bash "$skill_dir/teardown.sh" >/dev/null
check '解除で自分の登録だけ消える' "$(query '[.hooks.StopFailure[].hooks[].command]')" '["echo other","echo mixed"]'
check '解除後も他の設定は変えない' "$(query '[.model, .language, .hooks.SessionStart[0].hooks[0].command]')" '["opus","japanese","echo start"]'
cp "$settings" "$work/after-teardown.json"
bash "$skill_dir/teardown.sh" >/dev/null
check '2 回目の解除は書き換えない' "$(cmp -s "$settings" "$work/after-teardown.json" && echo same || echo changed)" 'same'

# 5. 自分の登録しか無ければ StopFailure ごと消す。hooks が無い設定は触らない
printf '{"hooks":{"Stop":[]}}\n' >"$settings"
bash "$skill_dir/setup.sh" >/dev/null
bash "$skill_dir/teardown.sh" >/dev/null
check '空になった StopFailure は残さない' "$(query '.')" '{"hooks":{"Stop":[]}}'
printf '{"model":"opus"}\n' >"$settings"
bash "$skill_dir/teardown.sh" >/dev/null
check 'hooks が無い設定は解除で変わらない' "$(query '.')" '{"model":"opus"}'

# 6. 壊れた settings.json は書き換えずに失敗する
printf '{ broken\n' >"$settings"
if bash "$skill_dir/setup.sh" >/dev/null 2>&1; then status=succeeded; else status=failed; fi
check '壊れた設定では失敗する' "$status" 'failed'
check '壊れた設定は書き換えない' "$(cat "$settings")" '{ broken'
check '一時ファイルを残さない' "$(find "$CLAUDE_CONFIG_DIR" -name 'settings.json.??????' | wc -l)" '0'

echo
if [ "$failures" -eq 0 ]; then
  echo "all passed"
else
  echo "$failures failed"
  exit 1
fi
