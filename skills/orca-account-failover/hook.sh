#!/usr/bin/env bash
# Claude Code の StopFailure hook（matcher: rate_limit）の入口。setup.sh が settings.json に登録する。
# 利用上限で止まったセッションを別の Claude アカウントで再開する worker.js を、切り離して起動する。
# worker.js は止まった claude 自身を終了させるので、その子として動かすと一緒に落ちる。
#
# 止めたいとき: 状態ディレクトリに disabled という名前のファイルを置く。
# 試すとき: ORCA_ACCOUNT_FAILOVER_DRY_RUN=1（判定を記録するだけ）
set -u

# Orca の端末で動いているセッションだけを対象にする。claude -p や他の端末のセッションでは、
# 再開コマンドを打ち込む先の端末が無い
[ -n "${ORCA_TERMINAL_HANDLE-}" ] || exit 0

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ORCA_ACCOUNT_FAILOVER_STATE_DIR="${ORCA_ACCOUNT_FAILOVER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/orca-account-failover}"
# 止める目印はスクリプトの置き場所ではなく状態ディレクトリに置く。スキルの更新で消えないようにするため
[ -e "$ORCA_ACCOUNT_FAILOVER_STATE_DIR/disabled" ] && exit 0
mkdir -p "$ORCA_ACCOUNT_FAILOVER_STATE_DIR/events" || exit 0
payload="$(mktemp "$ORCA_ACCOUNT_FAILOVER_STATE_DIR/events/XXXXXXXX.json")" || exit 0
cat >"$payload"

# hook はシェルを挟んで起動されるので、親をたどって claude 本体を探す
claude_pid=""
pid=$PPID
for _ in 1 2 3 4 5; do
  [ -n "$pid" ] && [ "$pid" -gt 1 ] || break
  if [ "$(ps -o comm= -p "$pid" 2>/dev/null)" = "claude" ]; then
    claude_pid=$pid
    break
  fi
  pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
done

# Orca の Electron を ELECTRON_RUN_AS_NODE=1 で node として使う（orca CLI と同じ起動方法）。
# 同梱の RuntimeClient がそのまま読め、PATH 上の node の有無に左右されない
find_orca_electron() {
  local cli real app candidate
  for cli in "$(command -v orca 2>/dev/null)" "$(command -v orca-ide 2>/dev/null)" /usr/bin/orca-ide /opt/Orca/resources/bin/orca-ide; do
    [ -n "$cli" ] && [ -e "$cli" ] || continue
    real="$(readlink -f "$cli")" || continue
    # <app>/resources/bin/<cli> → <app>
    app="$(dirname "$(dirname "$(dirname "$real")")")"
    for candidate in orca-ide orca Orca; do
      if [ -f "$app/$candidate" ] && [ -x "$app/$candidate" ]; then
        printf '%s\n' "$app/$candidate"
        return 0
      fi
    done
  done
  return 1
}

log="$ORCA_ACCOUNT_FAILOVER_STATE_DIR/failover.log"
if ! electron="$(find_orca_electron)"; then
  printf '{"ts":"%s","event":"error","message":"Orca executable not found"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$log"
  exit 0
fi

# NODE_OPTIONS は orca CLI のラッパーと同じく外す（Electron の node モードに持ち込むと起動に失敗しうる）
env -u NODE_OPTIONS -u NODE_REPL_EXTERNAL_MODULE ELECTRON_RUN_AS_NODE=1 \
  setsid "$electron" "$dir/worker.js" --payload "$payload" --claude-pid "$claude_pid" \
  >>"$log" 2>&1 </dev/null &
exit 0
