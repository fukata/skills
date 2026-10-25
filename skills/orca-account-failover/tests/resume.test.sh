#!/usr/bin/env bash
# resume.sh が、指定した設定フォルダで claude を起動し直すことを確かめる。
# claude は代役（受け取った環境変数と引数を書き出すだけ）を使う。
#   bash tests/resume.test.sh
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0

mkdir -p "$work/bin"
cat >"$work/bin/claude" <<'SH'
#!/usr/bin/env bash
{
  echo "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR-<unset>}"
  echo "ORCA_CLAUDE_INJECTED_CONFIG_DIR=${ORCA_CLAUDE_INJECTED_CONFIG_DIR-<unset>}"
  printf 'arg=%s\n' "$@"
} >"$CLAUDE_STUB_OUT"
SH
chmod +x "$work/bin/claude"

# 端末を開いたときに Orca が渡した値（上限に達したアカウントのフォルダ）
export CLAUDE_CONFIG_DIR="$work/profiles/limited/home"
export ORCA_CLAUDE_INJECTED_CONFIG_DIR="$work/profiles/limited/home"
export CLAUDE_STUB_OUT="$work/out"
export PATH="$work/bin:$PATH"
args_file="$work/args"
printf '%s\0' --dangerously-skip-permissions --resume 00000000-0000-4000-8000-000000000000 $'続けて\n"引用" と $HOME' >"$args_file"

check() {
  local name="$1" expected="$2"
  local actual
  actual="$(cat "$work/out" 2>/dev/null || echo '<claude が起動していない>')"
  if [ "$actual" = "$expected" ]; then
    echo "ok   $name"
  else
    echo "FAIL $name"
    echo "       expected: $expected"
    echo "       actual:   $actual"
    failures=$((failures + 1))
  fi
  rm -f "$work/out"
}

args_lines=$'arg=--dangerously-skip-permissions\narg=--resume\narg=00000000-0000-4000-8000-000000000000\narg=続けて\n"引用" と $HOME'

# 1. 管理アカウントのフォルダを渡すと、そのフォルダで起動する
bash "$skill_dir/resume.sh" "$args_file" "$work/profiles/target/home"
check '管理アカウントのフォルダで起動する' \
  "CLAUDE_CONFIG_DIR=$work/profiles/target/home
ORCA_CLAUDE_INJECTED_CONFIG_DIR=$work/profiles/target/home
$args_lines"

# 2. 空を渡すと System Default。利用者が自分で指定していたフォルダがあれば、それに戻す
ORCA_CLAUDE_USER_CONFIG_DIR="$work/user-config" bash "$skill_dir/resume.sh" "$args_file" ''
check 'System Default は利用者のフォルダに戻す' \
  "CLAUDE_CONFIG_DIR=$work/user-config
ORCA_CLAUDE_INJECTED_CONFIG_DIR=<unset>
$args_lines"

# 3. 利用者のフォルダが無ければ、フォルダの指定を外す（~/.claude を使う）
bash "$skill_dir/resume.sh" "$args_file" ''
check 'System Default はフォルダの指定を外す' \
  "CLAUDE_CONFIG_DIR=<unset>
ORCA_CLAUDE_INJECTED_CONFIG_DIR=<unset>
$args_lines"

# 4. フォルダの指定が無ければ起動しない（どのアカウントで動くか決められない）
if bash "$skill_dir/resume.sh" "$args_file" 2>/dev/null; then
  echo "FAIL フォルダの指定が無ければ失敗する（成功した）"
  failures=$((failures + 1))
fi
check 'フォルダの指定が無ければ起動しない' '<claude が起動していない>'

if [ "$failures" -gt 0 ]; then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
